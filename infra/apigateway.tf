# --- API Gateway HTTP API ---
#
# Moved out of lambda.tf, where the house stack kept all four aws_apigatewayv2_*
# resources plus the Lambda permission. The new piece is the JWT authorizer: the
# house stack has no authorizer anywhere, which is why its Cognito login is purely
# cosmetic — anyone could call its API unauthenticated (design 7.4).
#
# No cors_configuration. /api/* is served from the same CloudFront domain as the
# SPA, so every call is same-origin and CORS is dead weight (design 7.1).

resource "aws_apigatewayv2_api" "api" {
  name          = "${var.project}-api"
  protocol_type = "HTTP"
}

# Access-log destination for the stage below. Declared here rather than in cloudwatch.tf
# so it sits next to its only consumer; same explicit-retention reasoning as the Lambda
# group, which is that an unmanaged log group defaults to never-expire and survives
# terraform destroy.
resource "aws_cloudwatch_log_group" "apigw_access" {
  name              = "/aws/apigateway/${var.project}-api"
  retention_in_days = var.log_retention_days
}

resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 30000 # HARD API Gateway ceiling — make it visible in code
}

# The authorizer the house stack lacks. Audience is the app client id; issuer is the
# pool's OIDC URL. The SPA gets an IdToken from InitiateAuth and sends it as
# `Authorization: Bearer`, which the /api/* CloudFront behaviour already forwards.
resource "aws_apigatewayv2_authorizer" "jwt" {
  api_id           = aws_apigatewayv2_api.api.id
  authorizer_type  = "JWT"
  name             = "${var.project}-jwt"
  identity_sources = ["$request.header.Authorization"]

  jwt_configuration {
    audience = [aws_cognito_user_pool_client.main.id]
    issuer   = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.main.id}"
  }
}

resource "aws_apigatewayv2_route" "default" {
  api_id             = aws_apigatewayv2_api.api.id
  route_key          = "$default"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorization_type = "JWT" # house stack: absent -> login was cosmetic
  authorizer_id      = aws_apigatewayv2_authorizer.jwt.id
}

# --- The public, API-key-protected scan endpoint ---
#
# ROUTE KEYS CARRY THE LITERAL /api PREFIX, and that is not a typo. The CloudFront
# api-gateway origin sets no origin_path and performs no path rewrite, so it forwards
# the viewer path verbatim: a request to https://email-scan.example.com/api/public/scan
# arrives here with rawPath "/api/public/scan". handler.py then does
# removeprefix("/api") to get "/public/scan".
#
# Verified live against the deployed stack rather than assumed:
#   GET /api/health      -> 200
#   GET /api/api/health  -> 404 {"error":"no route for GET /api/health"}
# The second response is the proof. The Lambda saw "/api/api/health", stripped one
# "/api", and reported the remainder — so CloudFront did not strip anything. A route
# key of "POST /public/scan" would therefore never match, and every public call would
# fall through to the $default route, hit the JWT authorizer, and 401 with no
# explanation.
#
# authorization_type NONE is the whole point: these routes must be callable by anyone,
# from anywhere, with no Cognito token. Authentication is the Lambda's x-api-key check.
# Note that the Lambda's x-origin-secret check still applies, so these routes are
# reachable through CloudFront only, not by calling the API Gateway URL directly.
resource "aws_apigatewayv2_route" "public_scan" {
  api_id             = aws_apigatewayv2_api.api.id
  route_key          = "POST /api/public/scan"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorization_type = "NONE"
}

# CORS preflight. Declared as a real route rather than left to $default, because
# $default carries the JWT authorizer and a browser preflight sends no Authorization
# header — it would 401 before the Lambda ever saw it, and the POST that follows would
# never be attempted. The Lambda answers this with a 204 and the CORS headers, and makes
# no model call.
resource "aws_apigatewayv2_route" "public_scan_options" {
  api_id             = aws_apigatewayv2_api.api.id
  route_key          = "OPTIONS /api/public/scan"
  target             = "integrations/${aws_apigatewayv2_integration.lambda.id}"
  authorization_type = "NONE"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.api.id
  name        = "$default"
  auto_deploy = true

  # First of three independent brakes on the public endpoint. This one is enforced at
  # the edge of the gateway, before the Lambda is ever invoked, so it costs nothing to
  # reject with. The other two are the Lambda's own x-api-key check and its
  # DynamoDB-backed daily cap (PUBLIC_DAILY_CAP), which is what stops a leaked key from
  # running an unbounded bill overnight while staying politely under 10 req/s.
  #
  # Applied to the POST route only. Preflight is throttled by the default settings
  # below: it makes no model call, and throttling it in lockstep with the POST would
  # mean a browser client burns its budget twice per scan.
  route_settings {
    route_key              = aws_apigatewayv2_route.public_scan.route_key
    throttling_rate_limit  = 10
    throttling_burst_limit = 20
  }

  # Deliberately higher than the public route so a live demo is never rate-limited by an
  # abuse control aimed at strangers. Still bounded rather than left at the account
  # default of 10 000 rps: the UI routes sit behind Cognito, but a bug in a retry loop
  # is a cheaper way to spend money than an attacker.
  default_route_settings {
    throttling_rate_limit  = 50
    throttling_burst_limit = 100
  }

  # tasks.md T3.5 asked for this and never delivered it. It matters more now that one
  # route is public: without access logs a 4xx on /api/public/scan is invisible, and
  # "the endpoint is broken" and "someone is hammering it with a bad key" look identical.
  # Gateway-level rejections (throttles, a preflight that missed its route) never reach
  # the Lambda, so the Lambda's own logs cannot substitute.
  #
  # No header is logged. In particular x-api-key is never written here.
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.apigw_access.arn
    format = jsonencode({
      requestId               = "$context.requestId"
      ip                      = "$context.identity.sourceIp"
      userAgent               = "$context.identity.userAgent"
      requestTime             = "$context.requestTime"
      httpMethod              = "$context.httpMethod"
      path                    = "$context.path"
      routeKey                = "$context.routeKey"
      status                  = "$context.status"
      protocol                = "$context.protocol"
      responseLength          = "$context.responseLength"
      responseLatency         = "$context.responseLatency"
      integrationStatus       = "$context.integrationStatus"
      integrationErrorMessage = "$context.integrationErrorMessage"
      errorMessage            = "$context.error.message"
    })
  }
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.api.execution_arn}/*/*"
}

output "cloudfront_url" {
  value = "https://${var.domain_name}"
}

output "cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.main.id
}

output "frontend_bucket" {
  value = aws_s3_bucket.frontend.id
}

output "api_gateway_url" {
  value = aws_apigatewayv2_api.api.api_endpoint
}

output "cognito_user_pool_id" {
  value = aws_cognito_user_pool.main.id
}

output "cognito_client_id" {
  value = aws_cognito_user_pool_client.main.id
}

# The JWT authorizer's issuer URL. Emitted so a failing 401 can be diagnosed by
# comparing this against the `iss` claim in the token the SPA actually sent.
output "cognito_issuer" {
  value = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.main.id}"
}

# T4.2 waits on this reaching ISSUED before any curl against the distribution.
output "acm_certificate_arn" {
  value = aws_acm_certificate.main.arn
}

output "api_base_url" {
  value = "https://${var.domain_name}/api"
}

# --- Public endpoint ---
#
# The CloudFront URL, not the API Gateway one. Callers must come through CloudFront: it is
# what injects x-origin-secret, and the Lambda fails closed without it. Handing anyone the
# execute-api hostname would produce a 403 that has nothing to do with their API key.
output "public_endpoint" {
  description = "API-key-protected POST endpoint, callable from anywhere."
  value       = "https://${var.domain_name}/api/public/scan"
}

# Sensitive, so `terraform output` redacts it and it stays out of any CI log that echoes
# outputs wholesale. Read it deliberately with `terraform output -raw public_api_key`. The
# UI also receives it, but only on GET /runs, which sits behind the Cognito JWT authorizer.
output "public_api_key" {
  description = "Value for the x-api-key header on the public endpoint."
  value       = random_password.public_api_key.result
  sensitive   = true
}

output "runs_table" {
  description = "DynamoDB table holding run history and the public daily-quota counter."
  value       = aws_dynamodb_table.runs.name
}

# Ready-to-paste probe. Emitted because the two ways to get a confusing 403 here are a
# stripped x-api-key and a call that bypassed CloudFront, and both are avoided by using
# this exact shape.
output "public_endpoint_curl" {
  description = "Copy-paste smoke test for the public endpoint."
  value       = "curl -sS -X POST https://${var.domain_name}/api/public/scan -H 'content-type: application/json' -H \"x-api-key: $(terraform output -raw public_api_key)\" -d '{\"text\":\"From: security@paypa1-verify.com\\nSubject: Urgent\\n\\nVerify at http://bit.ly/x9\"}'"
}

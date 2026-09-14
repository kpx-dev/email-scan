# --- Lambda ---
#
# IAM, packaging and the function itself. The API Gateway resources that the house
# stack kept in this file have moved to apigateway.tf, where the JWT authorizer
# now lives alongside them.

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.project}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

# Shared secret between the CloudFront origin and the Lambda, so the API Gateway
# endpoint cannot be called directly (design 7.4). 48 chars, no specials — it
# travels as an HTTP header value and must stay a clean token.
resource "random_password" "origin_secret" {
  length  = 48
  special = false
}

# The public endpoint's x-api-key. Travels as an HTTP header value, so it must be
# header-safe: uppercase, lowercase and digits only, no specials, no padding, nothing that
# needs quoting or percent-encoding by a curl on someone else's machine.
#
# 43 chars of [A-Za-z0-9] is ~256 bits of entropy, which puts brute force out of reach and
# leaves the gateway throttle and the daily cap to handle the case that actually happens:
# the key leaks because it was pasted somewhere.
#
# It is a Terraform-managed secret, so it lands in terraform.tfstate in cleartext (as
# origin_secret already does). That is the existing posture of this stack — local,
# unversioned, gitignored state — not a new exposure. Rotation is `terraform taint` on this
# resource plus an apply.
resource "random_password" "public_api_key" {
  length  = 43
  special = false
  upper   = true
  lower   = true
  numeric = true
}

resource "aws_iam_role_policy" "lambda" {
  name = "${var.project}-lambda-policy"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:*"
      },
      # Primary lane — google.gemma-4-26b-a4b on bedrock-mantle. Verbatim from the
      # BedrockMantleAPIs statement of the AWS-managed AmazonBedrockLimitedAccess
      # policy. Gemma 4 has NO foundation-model ARN at all (get-foundation-model
      # rejects the identifier), so Resource "*" is the only option, not laziness.
      # Narrow this once the T0.3 spike reports the minimal sufficient action.
      {
        Effect   = "Allow"
        Resource = "*"
        Action = [
          "bedrock-mantle:CallWithBearerToken",
          "bedrock-mantle:Get*",
          "bedrock-mantle:List*",
          "bedrock-mantle:CreateInference",
        ]
      },
      # Fallback lane — google.gemma-3-27b-it over Converse/ConverseStream. The
      # house policy granted only bedrock:InvokeModel, so this lane 403s without
      # the streaming and Converse actions.
      {
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
          "bedrock:Converse",
          "bedrock:ConverseStream",
        ]
        Resource = [
          "arn:aws:bedrock:*::foundation-model/*",
          "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*"
        ]
      },
      # Model discovery for GET /models, so the dropdown can offer every Gemma model this
      # account can actually see rather than only the two the registry has verified by hand.
      # Read-only and account-scoped: ListFoundationModels takes no resource ARN, which is why
      # Resource is "*" here. Without this the call returns AccessDeniedException and
      # model_discovery degrades to the registry alone with the exception shown in the UI --
      # a working degrade, not an outage.
      {
        Effect   = "Allow"
        Action   = ["bedrock:ListFoundationModels"]
        Resource = "*"
      },
      # Run history and the daily-quota counter. Scoped to this one table ARN and to the
      # four actions the handler actually calls — PutItem writes a run, UpdateItem does the
      # atomic ADD on the QUOTA item, GetItem reads it back for the UI's dailyUsage, Query
      # lists runs descending. No wildcard on either action or resource: DeleteItem, Scan,
      # BatchWriteItem and every control-plane call are absent, so a compromised function
      # cannot drop the table or read its way across the account.
      {
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:GetItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query",
        ]
        Resource = aws_dynamodb_table.runs.arn
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Package lambda
#
# archive_file zips source_dir verbatim, so once a local `python -c` check has run,
# __pycache__/*.pyc land in the package and change output_base64sha256 on every
# run — forcing a spurious function update on every apply. deploy.sh purges them
# too; belt and braces.
data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/../backend"
  output_path = "${path.module}/../backend.zip"
  excludes    = ["__pycache__", "*.pyc"]
}

resource "aws_lambda_function" "api" {
  function_name = var.project
  role          = aws_iam_role.lambda.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.12" # stock runtime: 0 pip deps, 0 layers, no Docker
  # 29 s, not the house 120 s. API Gateway hard-caps at 30 s, so a longer Lambda
  # timeout only guarantees a bare gateway 504 while the function keeps billing.
  # Timeout chain: read_timeout 25 s < Lambda 29 s < API GW 30 s < CF origin 120 s.
  timeout          = 29
  memory_size      = 512
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  environment {
    variables = {
      REGION = var.region
      # Which region Bedrock is invoked in, which is NOT where the stack lives. handler.py
      # reads BEDROCK_REGION first and falls back to REGION, so setting this explicitly is
      # what keeps the EU model default from being tied to moving the whole stack. Frankfurt
      # by default -- see variables.tf for the latency it costs and the fallback lane it loses.
      BEDROCK_REGION = var.bedrock_region
      # Model IDs are literals, not variables: the (model, endpoint, path, region)
      # tuple is a verified routing rule (design 3.2), not a knob to turn.
      PRIMARY_MODEL_ID  = "google.gemma-4-26b-a4b" # mantle /openai/v1/chat/completions
      FALLBACK_MODEL_ID = "google.gemma-3-27b-it"  # bedrock-runtime Converse, plain base ID
      ORIGIN_SECRET     = random_password.origin_secret.result

      # Every run is persisted here, whether it came from the UI or from POST
      # /public/scan, so the SCAN RESULTS tab can list them all.
      RUNS_TABLE = aws_dynamodb_table.runs.name

      # Authenticates POST /api/public/scan. The gateway lets that route through with
      # authorization_type NONE; this value is what the Lambda compares the x-api-key
      # header against, in constant time.
      PUBLIC_API_KEY = random_password.public_api_key.result

      # Lambda-enforced ceiling on public requests per UTC day, counted atomically in the
      # QUOTA item. The gateway's 10 req/s throttle bounds the burst; this bounds the bill.
      # 10 req/s sustained for 24 h would be 864 000 scans, so the per-second cap alone is
      # not an answer to a leaked key. Sent as a string because Lambda env vars are strings.
      PUBLIC_DAILY_CAP = tostring(var.public_daily_cap)

      # The endpoint GET /runs advertises to the UI's API INTEGRATION tab. Supplied rather than left
      # to the handler's compiled-in default, so a domain_name change cannot leave the tab
      # printing a ready-to-run curl against a hostname that no longer resolves.
      PUBLIC_ENDPOINT_URL = "https://${var.domain_name}/api/public/scan"
    }
  }

  # Bind to the managed log group so the function never races ahead and creates an
  # unmanaged one with no retention.
  depends_on = [aws_cloudwatch_log_group.lambda]
}

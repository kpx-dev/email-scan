variable "region" {
  description = "AWS region for everything except the CloudFront ACM cert, which is pinned to us-east-1 via the aws.us_east_1 alias."
  type        = string
  default     = "us-east-1"
}

# Deliberately separate from var.region. Where the stack RUNS and where Bedrock is CALLED are
# two different decisions: Bedrock is reached over the network with SigV4 for the target
# region, so the Lambda can sit in us-east-1 and invoke Frankfurt. Collapsing the two into one
# variable would mean an EU model default could only be had by moving the API, the DynamoDB
# table and the Cognito pool as well.
#
# eu-central-1 is the EU default because it is the only EU region the primary model answers in
# (eu-west-1 returns 404 for it). It costs ~270ms against us-east-1 -- 758 vs 489 measured --
# and the FALLBACK model cannot run there at all, so the Lambda logs a fallback-lane warning at
# every cold start in this default. All three facts are in backend/model_registry.py.
variable "bedrock_region" {
  description = "Region Bedrock is invoked in, independent of where the stack is deployed. Must be a region the primary model answers in: us-east-1 or eu-central-1."
  type        = string
  default     = "eu-central-1"

  validation {
    condition     = contains(["us-east-1", "eu-central-1"], var.bedrock_region)
    error_message = "bedrock_region must be us-east-1 or eu-central-1. The primary model (google.gemma-4-26b-a4b) returns 404 in eu-west-1 and is not offered anywhere else, so any other value fails the Lambda's cold-start lane check."
  }
}

# Every resource name derives from this: S3 bucket, Lambda, IAM role, IAM policy,
# Cognito pool, Cognito client, API, OAC, log group. The house default was
# a pre-existing stack in the same account, whose Terraform
# state is local and unversioned. Leaving that default in place means one omitted
# tfvars line silently collides with a running demo (design R9).
variable "project" {
  description = "Name prefix for every resource in this stack."
  type        = string
  default     = "email-scan"

  validation {
    condition     = length(trimspace(var.project)) > 0 && can(regex("^[a-z0-9][a-z0-9-]{1,30}$", var.project))
    error_message = "project must be 2-31 lowercase alphanumeric/hyphen characters. Every resource name derives from it, so pick one no other stack in the account uses."
  }
}

# aws_route53_record issues an UPSERT, and an UPSERT shows NO destroy in the plan.
# A stale domain_name would silently re-point a live A-alias at our distribution
# and plan review would not catch it. Hence the guard.
variable "domain_name" {
  description = "Domain name for the app (e.g. email-scan.example.com)"
  type        = string

  # Derived from hosted_zone_name rather than hardcoded to one zone. It used to name a specific
  # domain, which meant publishing this file also published the zone -- and redacting the zone
  # silently broke the rule for everyone else, because their real domain no longer matched the
  # literal. `terraform validate` does not catch that: variable validations are only evaluated
  # once a plan has actual values, so the breakage would surface as a failed apply.
  validation {
    condition     = endswith(var.domain_name, ".${var.hosted_zone_name}")
    error_message = "The domain_name must be a subdomain of hosted_zone_name, which is the only zone this stack may write to."
  }

  validation {
    condition     = startswith(var.domain_name, "${var.project}.")
    error_message = "The domain_name must not be a live demo domain; the Route53 record is an UPSERT and would hijack it with no destroy shown in the plan."
  }
}

variable "hosted_zone_name" {
  description = "Route53 hosted zone name (e.g. example.com)"
  type        = string
}

variable "cognito_demo_username" {
  description = "Username of the admin-created demo user."
  type        = string
  default     = "demo"
}

variable "cognito_demo_password" {
  description = "Password for the demo Cognito user"
  type        = string
  sensitive   = true
}

# --- Auth fallback (design 7.4) ---
# If the Cognito local-exec provisioner misbehaves on demo day, flipping this one
# variable swaps in the CloudFront Function Basic auth that is already live on
# a pre-existing stack. Off by default: Cognito plus the JWT authorizer is the
# real design.
variable "basic_auth_enabled" {
  description = "Swap Cognito for the edge Basic-auth CloudFront Function. Recovery lever only."
  type        = bool
  default     = false
}

variable "basic_auth_username" {
  description = "Username for the Basic-auth fallback."
  type        = string
  default     = "demo"
}

variable "basic_auth_password" {
  description = "Password for the Basic-auth fallback. Base64-baked into the function body: obfuscation, not encryption."
  type        = string
  sensitive   = true
  # No default, deliberately. A secret variable with a default is the anti-pattern: the
  # value lives in version control forever, and removing terraform.tfvars does not remove
  # it. Terraform now demands it at apply time. Supply it in terraform.tfvars, which is
  # gitignored -- see terraform.tfvars.example.
}

# The house stack leaves the Lambda log group implicit, so the live one has
# retention null = never expire and survives terraform destroy. A benchmark run at
# high concurrency writes a lot of lines.
variable "log_retention_days" {
  description = "Retention for the Lambda and API Gateway access log groups."
  type        = number
  default     = 14
}

# --- Public endpoint abuse control ---
# The gateway's route-level throttle (10 req/s, burst 20) bounds the rate; this bounds the
# total. Both are needed: 10 req/s sustained for a day is 864 000 scans, so a per-second cap
# on its own does not stop a leaked key from running an overnight bill. Enforced in the
# Lambda against an atomically incremented DynamoDB counter, which returns 429 with the UTC
# reset time once the cap is reached.
variable "public_daily_cap" {
  description = "Maximum requests per UTC day accepted on POST /api/public/scan."
  type        = number
  default     = 5000

  validation {
    condition     = var.public_daily_cap > 0 && floor(var.public_daily_cap) == var.public_daily_cap
    error_message = "The public_daily_cap must be a positive whole number; it is compared against an integer counter."
  }
}

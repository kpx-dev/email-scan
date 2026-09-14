terraform {
  required_version = ">= 1.5"
  required_providers {
    aws  = { source = "hashicorp/aws", version = "~> 5.0" }
    null = { source = "hashicorp/null", version = "~> 3.0" }
    # archive is USED but UNDECLARED in the house stack (it resolves by implicit
    # install, which is a silent dependency). random is new here — it backs
    # random_password.origin_secret and random_password.public_api_key.
    #
    # No provider was added for the public-endpoint work: the DynamoDB table, the two
    # new API Gateway routes, the stage throttling and the access log group are all
    # hashicorp/aws, and random was already declared.
    archive = { source = "hashicorp/archive", version = "~> 2.0" }
    random  = { source = "hashicorp/random", version = "~> 3.0" }
  }
}

provider "aws" {
  region = var.region
}

# Alias for us-east-1 (required for CloudFront ACM certs)
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

data "aws_route53_zone" "main" {
  name = var.hosted_zone_name
}

# --- S3 Buckets ---
#
# One bucket only. The house stack also carried an `images` bucket (plus its
# public_access_block and a CORS rule) for generated artifacts; email-scan
# generates none — every response streams back through /api/*. Deleted here, and
# the IAM s3:* statement that referenced it is deleted in lambda.tf. They must go
# together or `apply` fails on a dangling reference (tasks 9, correction 3).

# Frontend hosting bucket
resource "aws_s3_bucket" "frontend" {
  bucket        = "${var.project}-frontend-${data.aws_caller_identity.current.account_id}"
  force_destroy = true # a PoC should tear down cleanly
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket                  = aws_s3_bucket.frontend.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

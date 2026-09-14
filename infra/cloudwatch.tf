# --- CloudWatch Logs ---
#
# Declared explicitly rather than left to Lambda's implicit creation, which is what
# the prior-art stack does: its Lambda log group has retention null =
# never expire, is unmanaged, and survives terraform destroy. A benchmark ramp at
# concurrency 150 writes a lot of lines, so this is cost as well as tidiness — and
# it is the reason `terraform destroy` cleans up after this PoC.
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.project}"
  retention_in_days = var.log_retention_days
}

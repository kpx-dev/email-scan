# --- Auth fallback: edge HTTP Basic auth (design 7.4) ---
#
# Off by default (count = 0). Cognito plus the API Gateway JWT authorizer is the
# real design; this exists because the Cognito demo user is created by a local-exec
# provisioner that shells out to the AWS CLI, and if that misbehaves on demo day,
# flipping basic_auth_enabled is a five-minute recovery. Ported from
# an internal prior-art stack, where the same pattern is in production.
#
# It runs on viewer-request, before any cache lookup or origin dispatch, so the
# association in cloudfront.tf covers BOTH behaviours — the S3 default and /api/* —
# which means it protects the API path too, something the Cognito path only achieves
# via the authorizer.

locals {
  # CloudFront Functions get no environment variables and no network access, so the
  # credential is base64-baked into the function body. That is obfuscation, not
  # encryption — anyone with cloudfront:GetFunction can read it back.
  basic_auth_expected = "Basic ${base64encode("${var.basic_auth_username}:${var.basic_auth_password}")}"
}

resource "aws_cloudfront_function" "basic_auth" {
  count   = var.basic_auth_enabled ? 1 : 0
  name    = "${var.project}-basic-auth"
  runtime = "cloudfront-js-2.0"
  comment = "Basic auth fallback for ${var.project}; enabled via basic_auth_enabled"
  publish = true

  # CloudFront Functions run a JavaScript subset (ES5.1-ish): no template literals,
  # no const destructuring.
  code = <<-EOT
    function handler(event) {
        var req = event.request;
        var h = req.headers.authorization && req.headers.authorization.value;
        if (h === ${jsonencode(local.basic_auth_expected)}) {
            return req;
        }
        return {
            statusCode: 401,
            statusDescription: 'Unauthorized',
            headers: {
                'www-authenticate': { value: 'Basic realm="${var.project}", charset="UTF-8"' },
                'cache-control': { value: 'no-store' }
            }
        };
    }
  EOT
}

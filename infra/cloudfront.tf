# --- ACM Certificate ---
resource "aws_acm_certificate" "main" {
  provider          = aws.us_east_1
  domain_name       = var.domain_name
  validation_method = "DNS"
  lifecycle { create_before_destroy = true }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.main.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }
  zone_id = data.aws_route53_zone.main.zone_id
  name    = each.value.name
  type    = each.value.type
  records = [each.value.record]
  ttl     = 60
}

resource "aws_acm_certificate_validation" "main" {
  provider                = aws.us_east_1
  certificate_arn         = aws_acm_certificate.main.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

# --- CloudFront ---
resource "aws_cloudfront_origin_access_control" "frontend" {
  name                              = "${var.project}-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

locals {
  apigw_domain = replace(replace(aws_apigatewayv2_api.api.api_endpoint, "https://", ""), "/", "")
}

resource "aws_cloudfront_distribution" "main" {
  enabled             = true
  default_root_object = "index.html"
  aliases             = [var.domain_name]
  price_class         = "PriceClass_100"

  # S3 frontend origin
  origin {
    domain_name              = aws_s3_bucket.frontend.bucket_regional_domain_name
    origin_id                = "s3-frontend"
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend.id
  }

  # API Gateway origin
  origin {
    domain_name = local.apigw_domain
    origin_id   = "api-gateway"

    # Defence in depth: the API Gateway endpoint is public, so the Lambda rejects
    # any request that does not carry this header (hmac.compare_digest). Only
    # CloudFront can add it.
    custom_header {
      name  = "x-origin-secret"
      value = random_password.origin_secret.result
    }

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
      # Outermost link in the timeout chain, so the inner limits fire first and
      # yield a readable error instead of a bare gateway 504:
      # read_timeout 25 s < Lambda 29 s < API GW 30 s < this 120 s.
      origin_read_timeout      = 120
      origin_keepalive_timeout = 5
    }
  }

  # API path -> API Gateway
  ordered_cache_behavior {
    path_pattern           = "/api/*"
    target_origin_id       = "api-gateway"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods         = ["GET", "HEAD"]
    # false, not the house `true`. Harmless while responses are buffered JSON;
    # breaks the moment real SSE lands, because CloudFront buffers to compress.
    compress = false

    # Legacy forwarded_values, deliberately NOT migrated to a managed cache policy.
    # This stack references zero policy IDs, and mixing forwarded_values with
    # cache_policy_id in one behaviour is a hard Terraform error. It already
    # forwards Authorization, so the new JWT authorizer works unchanged.
    #
    # x-api-key is in this list because a legacy forwarded_values header list is an
    # ALLOWLIST, not an addition to some default: CloudFront forwards exactly these
    # headers and silently drops everything else. Without it, x-api-key never reaches
    # the Lambda, POST /api/public/scan sees no key on a request that plainly carried
    # one, and every public call comes back 401 "missing x-api-key" — the Lambda answers
    # 401 for a key problem and reserves 403 for x-origin-secret, so the status names the
    # layer but not the cause. Nothing in the request or the response hints at the edge as
    # the culprit, which makes this the most expensive one-line omission available here.
    forwarded_values {
      query_string = true
      headers      = ["Authorization", "Content-Type", "x-api-key"]
      cookies { forward = "none" }
    }

    min_ttl     = 0
    default_ttl = 0
    max_ttl     = 0

    dynamic "function_association" {
      for_each = var.basic_auth_enabled ? [1] : []
      content {
        event_type   = "viewer-request"
        function_arn = aws_cloudfront_function.basic_auth[0].arn
      }
    }
  }

  # Default -> S3
  default_cache_behavior {
    target_origin_id       = "s3-frontend"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    forwarded_values {
      query_string = false
      cookies { forward = "none" }
    }

    min_ttl     = 0
    default_ttl = 3600
    max_ttl     = 86400

    dynamic "function_association" {
      for_each = var.basic_auth_enabled ? [1] : []
      content {
        event_type   = "viewer-request"
        function_arn = aws_cloudfront_function.basic_auth[0].arn
      }
    }
  }

  # NO custom_error_response blocks.
  #
  # The house stack rewrote 403 -> 200 /index.html and 404 -> 200 /index.html as an
  # SPA fallback. Those blocks are DISTRIBUTION-WIDE, so a genuine 403/404 from the
  # API origin — including a 401 from our new JWT authorizer — comes back as the HTML
  # shell with HTTP 200. That is a maddening thing to debug live: the app loads,
  # looks fine, and every API call silently returns HTML instead of JSON. The SPA is
  # a single index.html served by default_root_object, so it needs no fallback.

  restrictions {
    geo_restriction { restriction_type = "none" }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate.main.arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }

  depends_on = [aws_acm_certificate_validation.main]
}

# S3 bucket policy for CloudFront OAC
resource "aws_s3_bucket_policy" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowCloudFrontOAC"
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.frontend.arn}/*"
      Condition = {
        StringEquals = {
          "AWS:SourceArn" = aws_cloudfront_distribution.main.arn
        }
      }
    }]
  })
}

# --- Route53 ---
resource "aws_route53_record" "main" {
  zone_id = data.aws_route53_zone.main.zone_id
  name    = var.domain_name
  type    = "A"
  alias {
    name                   = aws_cloudfront_distribution.main.domain_name
    zone_id                = aws_cloudfront_distribution.main.hosted_zone_id
    evaluate_target_health = false
  }
}

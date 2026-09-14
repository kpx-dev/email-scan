# --- Cognito ---
resource "aws_cognito_user_pool" "main" {
  name = var.project

  password_policy {
    minimum_length    = 8
    require_lowercase = false
    require_numbers   = false
    require_symbols   = false
    require_uppercase = false
  }

  auto_verified_attributes = ["email"]

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  schema {
    name                = "email"
    attribute_data_type = "String"
    required            = true
    mutable             = true
  }
}

resource "aws_cognito_user_pool_client" "main" {
  name                                 = "${var.project}-client"
  user_pool_id                         = aws_cognito_user_pool.main.id
  explicit_auth_flows                  = ["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_SRP_AUTH"]
  supported_identity_providers         = ["COGNITO"]
  allowed_oauth_flows_user_pool_client = false
}

# Create demo user
#
# Two traps carried over from the house stack, both worth knowing before apply:
# the provisioner shells out to the AWS CLI and does NOT inherit the provider's
# credentials, so apply needs AWS_PROFILE=aws exported and a live SSO session; and
# the create ends `2>/dev/null || true`, which hides a genuine failure. Always
# confirm the user independently afterwards:
#   aws cognito-idp admin-get-user --user-pool-id <pool> --username demo \
#     --query '{S:UserStatus,E:Enabled}'      # -> CONFIRMED / true
resource "null_resource" "cognito_user" {
  count      = var.basic_auth_enabled ? 0 : 1
  depends_on = [aws_cognito_user_pool.main, aws_cognito_user_pool_client.main]

  # The house stack has no triggers block, so changing the password silently
  # no-ops: the provisioner only ever runs on create.
  triggers = {
    pw   = var.cognito_demo_password
    user = var.cognito_demo_username
    pool = aws_cognito_user_pool.main.id
  }

  provisioner "local-exec" {
    command = <<-EOT
      aws cognito-idp admin-create-user \
        --user-pool-id ${aws_cognito_user_pool.main.id} \
        --username ${var.cognito_demo_username} \
        --user-attributes Name=email,Value=demo@example.com Name=email_verified,Value=true \
        --temporary-password "${var.cognito_demo_password}" \
        --message-action SUPPRESS \
        --region ${var.region} 2>/dev/null || true

      aws cognito-idp admin-set-user-password \
        --user-pool-id ${aws_cognito_user_pool.main.id} \
        --username ${var.cognito_demo_username} \
        --password "${var.cognito_demo_password}" \
        --permanent \
        --region ${var.region}
    EOT
  }
}

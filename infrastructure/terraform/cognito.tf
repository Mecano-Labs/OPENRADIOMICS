# OpenRadiomics - AWS Cognito Configuration
# HIPAA-compliant authentication with mandatory MFA

# Cognito User Pool
resource "aws_cognito_user_pool" "main" {
  name = "${var.project_name}-${var.environment}-users"

  # Username configuration
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  # HIPAA-compliant password policy
  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 1
  }

  # Mandatory MFA for HIPAA compliance
  mfa_configuration = "ON"

  software_token_mfa_configuration {
    enabled = true
  }

  # Account recovery via verified email only
  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  # User attribute schema
  schema {
    name                     = "email"
    attribute_data_type      = "String"
    mutable                  = true
    required                 = true
    developer_only_attribute = false

    string_attribute_constraints {
      min_length = 5
      max_length = 254
    }
  }

  schema {
    name                     = "name"
    attribute_data_type      = "String"
    mutable                  = true
    required                 = false
    developer_only_attribute = false

    string_attribute_constraints {
      min_length = 1
      max_length = 255
    }
  }

  # Email configuration (using Cognito default for now)
  email_configuration {
    email_sending_account = "COGNITO_DEFAULT"
  }

  # Verification message customization
  verification_message_template {
    default_email_option = "CONFIRM_WITH_CODE"
    email_subject        = "OpenRadiomics - Verify your email"
    email_message        = "Your verification code is {####}. This code expires in 24 hours."
  }

  # User pool add-ons for advanced security
  user_pool_add_ons {
    advanced_security_mode = "ENFORCED"
  }

  # Admin create user config
  admin_create_user_config {
    allow_admin_create_user_only = false

    invite_message_template {
      email_subject = "OpenRadiomics - Your temporary password"
      email_message = "Your username is {username} and temporary password is {####}. Please login and set a new password."
      sms_message   = "Your username is {username} and temporary password is {####}"
    }
  }

  # Device tracking for security
  device_configuration {
    challenge_required_on_new_device      = true
    device_only_remembered_on_user_prompt = true
  }

  tags = {
    Name        = "${var.project_name}-${var.environment}-user-pool"
    Environment = var.environment
    HIPAA       = "true"
  }
}

# Cognito User Pool Domain (for hosted UI)
resource "aws_cognito_user_pool_domain" "main" {
  domain       = "${var.project_name}-${var.environment}-${data.aws_caller_identity.current.account_id}"
  user_pool_id = aws_cognito_user_pool.main.id
}

# Cognito App Client (for web application)
resource "aws_cognito_user_pool_client" "web" {
  name         = "${var.project_name}-${var.environment}-web-client"
  user_pool_id = aws_cognito_user_pool.main.id

  # No client secret for public SPA clients
  generate_secret = false

  # OAuth2 configuration
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = ["email", "openid", "profile"]
  supported_identity_providers         = ["COGNITO"]

  # Callback URLs - production and local development
  callback_urls = [
    "https://ezj2rdbghf.us-east-1.awsapprunner.com/auth/callback",
    "http://localhost:5000/auth/callback",
    "http://127.0.0.1:5000/auth/callback"
  ]

  logout_urls = [
    "https://ezj2rdbghf.us-east-1.awsapprunner.com/login",
    "http://localhost:5000/login",
    "http://127.0.0.1:5000/login"
  ]

  # Token validity - HIPAA compliant short sessions
  access_token_validity  = 15  # 15 minutes
  id_token_validity      = 15  # 15 minutes
  refresh_token_validity = 8   # 8 hours

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "hours"
  }

  # Prevent user existence errors (security best practice)
  prevent_user_existence_errors = "ENABLED"

  # Enable token revocation
  enable_token_revocation = true

  # Auth flows
  explicit_auth_flows = [
    "ALLOW_REFRESH_TOKEN_AUTH",
    "ALLOW_USER_SRP_AUTH",
    "ALLOW_USER_PASSWORD_AUTH"
  ]

  # Read/write attributes
  read_attributes = [
    "email",
    "email_verified",
    "name",
    "sub"
  ]

  write_attributes = [
    "email",
    "name"
  ]
}

# Resource server for API scopes (optional, for future API access control)
resource "aws_cognito_resource_server" "api" {
  identifier   = "openradiomics-api"
  name         = "OpenRadiomics API"
  user_pool_id = aws_cognito_user_pool.main.id

  scope {
    scope_name        = "read"
    scope_description = "Read access to studies and images"
  }

  scope {
    scope_name        = "write"
    scope_description = "Write access (annotations, measurements)"
  }

  scope {
    scope_name        = "admin"
    scope_description = "Administrative access"
  }
}

# User Pool Groups for RBAC
resource "aws_cognito_user_group" "admin" {
  name         = "admin"
  user_pool_id = aws_cognito_user_pool.main.id
  description  = "Administrators with full access"
  precedence   = 1
}

resource "aws_cognito_user_group" "clinician" {
  name         = "clinician"
  user_pool_id = aws_cognito_user_pool.main.id
  description  = "Clinicians with read/write access to assigned studies"
  precedence   = 10
}

resource "aws_cognito_user_group" "researcher" {
  name         = "researcher"
  user_pool_id = aws_cognito_user_pool.main.id
  description  = "Researchers with read access to anonymized data"
  precedence   = 20
}

resource "aws_cognito_user_group" "viewer" {
  name         = "viewer"
  user_pool_id = aws_cognito_user_pool.main.id
  description  = "View-only access to shared studies"
  precedence   = 30
}

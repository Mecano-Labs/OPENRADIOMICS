"""
OpenRadiomics - Authentication Module
HIPAA-compliant authentication using AWS Cognito.

Provides:
- JWT validation using Cognito JWKS
- @requires_auth decorator for protected routes
- @requires_role decorator for RBAC
- Audit logging for all access
- User registration with Cognito
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from functools import wraps
from typing import Optional, Tuple, List

import boto3
import requests
from botocore.exceptions import ClientError
from flask import g, redirect, request, url_for, jsonify
from jose import jwt, JWTError

# Get logger
logger = logging.getLogger("openradiomics.auth")

# Environment configuration
COGNITO_REGION = os.environ.get("AWS_REGION", "us-east-1")
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
COGNITO_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")
COGNITO_DOMAIN = os.environ.get("COGNITO_DOMAIN", "")

# Log Cognito configuration at module load
def _log_cognito_config():
    """Log Cognito configuration for debugging."""
    _logger = logging.getLogger("openradiomics.auth")
    _logger.info(f"Cognito Config - Region: {COGNITO_REGION}")
    _logger.info(f"Cognito Config - User Pool ID: {COGNITO_USER_POOL_ID or 'NOT SET'}")
    _logger.info(f"Cognito Config - Client ID: {COGNITO_CLIENT_ID or 'NOT SET'}")
    _logger.info(f"Cognito Config - Domain: {COGNITO_DOMAIN or 'NOT SET'}")

_log_cognito_config()

# Initialize Cognito client
_cognito_client = None

def get_cognito_client():
    """Get or create Cognito Identity Provider client."""
    global _cognito_client
    if _cognito_client is None:
        _cognito_client = boto3.client(
            "cognito-idp",
            region_name=COGNITO_REGION
        )
    return _cognito_client

# JWKS cache
_jwks_cache = None
_jwks_cache_time = 0
JWKS_CACHE_TTL = 3600  # 1 hour

# Role hierarchy (higher index = more privileges)
ROLE_HIERARCHY = {
    "viewer": 0,
    "researcher": 1,
    "clinician": 2,
    "admin": 3
}

# Public routes that don't require authentication
PUBLIC_ROUTES = {
    "/login",
    "/register",
    "/auth/callback",
    "/auth/logout",
    "/auth/login",
    "/auth/register",
    "/auth/verify",
    "/auth/resend-code",
    "/auth/set-password",
    "/auth/check-email",
    "/auth/forgot-password",
    "/auth/reset-password",
    "/api/health",
    "/static",
}


def get_jwks_url() -> str:
    """Get the JWKS URL for the Cognito User Pool."""
    return f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}/.well-known/jwks.json"


def get_jwks() -> dict:
    """
    Fetch and cache the JWKS from Cognito.
    Caches for 1 hour to avoid excessive requests.
    """
    global _jwks_cache, _jwks_cache_time

    now = time.time()
    if _jwks_cache and (now - _jwks_cache_time) < JWKS_CACHE_TTL:
        return _jwks_cache

    try:
        response = requests.get(get_jwks_url(), timeout=5)
        response.raise_for_status()
        _jwks_cache = response.json()
        _jwks_cache_time = now
        return _jwks_cache
    except Exception as e:
        logger.error(f"fetching JWKS: {e}")
        if _jwks_cache:
            return _jwks_cache
        raise


def get_signing_key(token: str) -> Optional[dict]:
    """Get the signing key for a token from JWKS."""
    try:
        headers = jwt.get_unverified_headers(token)
        kid = headers.get("kid")
        if not kid:
            return None

        jwks = get_jwks()
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                return key
        return None
    except Exception:
        return None


def validate_token(token: str) -> Optional[dict]:
    """
    Validate a JWT token from Cognito.
    Returns the decoded claims if valid, None otherwise.
    """
    if not token:
        return None

    if not COGNITO_USER_POOL_ID or not COGNITO_CLIENT_ID:
        logger.warning("Cognito not configured, authentication disabled")
        return None

    try:
        # Get the signing key
        signing_key = get_signing_key(token)
        if not signing_key:
            logger.warning("Could not find signing key for token")
            return None

        # Verify the token
        issuer = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}"

        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=COGNITO_CLIENT_ID,
            issuer=issuer,
            options={
                "verify_at_hash": False,  # Access tokens don't have at_hash
                "require_exp": True,
                "require_iat": True,
            }
        )

        # Verify token_use (should be 'access' or 'id')
        token_use = claims.get("token_use")
        if token_use not in ("access", "id"):
            logger.warning(f"Invalid token_use: {token_use}")
            return None

        return claims

    except JWTError as e:
        logger.warning(f"JWT validation error: {e}")
        return None
    except Exception as e:
        logger.warning(f"Token validation error: {e}")
        return None


def get_token_from_request() -> Optional[str]:
    """Extract the access token from the request."""
    # Check Authorization header first
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]

    # Check cookie
    return request.cookies.get("access_token")


def get_current_user() -> Optional[dict]:
    """
    Get the current authenticated user from the request.
    Returns user info dict or None if not authenticated.
    """
    if hasattr(g, "current_user"):
        return g.current_user

    token = get_token_from_request()
    if not token:
        return None

    claims = validate_token(token)
    if not claims:
        return None

    # Build user info from claims
    user = {
        "sub": claims.get("sub"),
        "email": claims.get("email"),
        "name": claims.get("name") or claims.get("email", "").split("@")[0],
        "groups": claims.get("cognito:groups", []),
        "token_use": claims.get("token_use"),
    }

    # Determine role from groups (highest privilege wins)
    role = "viewer"  # Default role
    max_level = -1
    for group in user["groups"]:
        level = ROLE_HIERARCHY.get(group, -1)
        if level > max_level:
            max_level = level
            role = group
    user["role"] = role

    # Cache in request context
    g.current_user = user
    return user


def get_user_role() -> str:
    """Get the current user's role."""
    user = get_current_user()
    return user.get("role", "viewer") if user else "viewer"


def is_authenticated() -> bool:
    """Check if the current request is authenticated."""
    return get_current_user() is not None


def requires_auth(f):
    """
    Decorator to require authentication for a route.
    Redirects to login page for web requests, returns 401 for API requests.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_authenticated():
            if request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def requires_role(min_role: str):
    """
    Decorator to require a minimum role for a route.
    Checks role hierarchy: viewer < researcher < clinician < admin
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            user = get_current_user()
            if not user:
                if request.path.startswith("/api/"):
                    return jsonify({"error": "Authentication required"}), 401
                return redirect(url_for("login"))

            user_level = ROLE_HIERARCHY.get(user.get("role", "viewer"), 0)
            required_level = ROLE_HIERARCHY.get(min_role, 0)

            if user_level < required_level:
                if request.path.startswith("/api/"):
                    return jsonify({"error": "Insufficient permissions"}), 403
                return jsonify({"error": "You do not have permission to access this resource"}), 403

            return f(*args, **kwargs)
        return decorated
    return decorator


def log_audit(
    action: str,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    response_status: Optional[int] = None,
    db_connection=None
):
    """
    Log an access event to the audit_log table.

    Args:
        action: The action being performed (e.g., 'view_study', 'login', 'logout')
        resource_type: Type of resource accessed (e.g., 'study', 'series')
        resource_id: ID of the resource
        response_status: HTTP response status code
        db_connection: Database connection (if None, tries to get from context)
    """
    user = get_current_user()

    # Get user_id from database if user is authenticated
    user_id = None
    if user and db_connection:
        try:
            cursor = db_connection.cursor()
            cursor.execute(
                "SELECT id FROM users WHERE cognito_sub = %s",
                (user.get("sub"),)
            )
            result = cursor.fetchone()
            if result:
                user_id = result[0]
        except Exception as e:
            logger.error(f"looking up user_id for audit: {e}")

    # Get client IP
    ip_address = request.headers.get("X-Forwarded-For", request.remote_addr)
    if ip_address and "," in ip_address:
        ip_address = ip_address.split(",")[0].strip()

    if db_connection:
        try:
            cursor = db_connection.cursor()
            cursor.execute(
                """
                INSERT INTO audit_log
                    (user_id, action, resource_type, resource_id, ip_address, request_path, response_status)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s)
                """,
                (user_id, action, resource_type, resource_id, ip_address, request.path, response_status)
            )
            db_connection.commit()
        except Exception as e:
            logger.error(f"writing audit log: {e}")
    else:
        # Fallback to console logging if no DB connection
        logger.info(f"AUDIT: {datetime.utcnow().isoformat()} | user={user.get('email') if user else 'anonymous'} | action={action} | resource={resource_type}:{resource_id} | path={request.path} | ip={ip_address}")


def is_public_route(path: str) -> bool:
    """Check if a route is public (doesn't require authentication)."""
    for public_path in PUBLIC_ROUTES:
        if path == public_path or path.startswith(public_path + "/"):
            return True
    return False


def get_cognito_login_url(redirect_uri: str) -> str:
    """Build the Cognito hosted UI login URL."""
    return (
        f"https://{COGNITO_DOMAIN}/login?"
        f"client_id={COGNITO_CLIENT_ID}&"
        f"response_type=code&"
        f"scope=email+openid+profile&"
        f"redirect_uri={redirect_uri}"
    )


def get_cognito_logout_url(redirect_uri: str) -> str:
    """Build the Cognito logout URL."""
    return (
        f"https://{COGNITO_DOMAIN}/logout?"
        f"client_id={COGNITO_CLIENT_ID}&"
        f"logout_uri={redirect_uri}"
    )


def get_cognito_signup_url(redirect_uri: str) -> str:
    """Build the Cognito hosted UI signup URL."""
    return (
        f"https://{COGNITO_DOMAIN}/signup?"
        f"client_id={COGNITO_CLIENT_ID}&"
        f"response_type=code&"
        f"scope=email+openid+profile&"
        f"redirect_uri={redirect_uri}"
    )


def exchange_code_for_tokens(code: str, redirect_uri: str) -> Optional[dict]:
    """
    Exchange an authorization code for tokens.
    Returns dict with access_token, id_token, refresh_token, or None on error.
    """
    token_url = f"https://{COGNITO_DOMAIN}/oauth2/token"

    try:
        response = requests.post(
            token_url,
            data={
                "grant_type": "authorization_code",
                "client_id": COGNITO_CLIENT_ID,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=10
        )

        if response.status_code != 200:
            logger.warning(f"Token exchange failed: {response.status_code} - {response.text}")
            return None

        return response.json()

    except Exception as e:
        logger.error(f"exchanging code for tokens: {e}")
        return None


def refresh_tokens(refresh_token: str) -> Optional[dict]:
    """
    Use a refresh token to get new access/id tokens.
    Returns dict with new tokens, or None on error.
    """
    token_url = f"https://{COGNITO_DOMAIN}/oauth2/token"

    try:
        response = requests.post(
            token_url,
            data={
                "grant_type": "refresh_token",
                "client_id": COGNITO_CLIENT_ID,
                "refresh_token": refresh_token,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=10
        )

        if response.status_code != 200:
            logger.warning(f"Token refresh failed: {response.status_code} - {response.text}")
            return None

        return response.json()

    except Exception as e:
        logger.error(f"refreshing tokens: {e}")
        return None


def ensure_user_in_db(user: dict, db_connection) -> Optional[int]:
    """
    Ensure the authenticated user exists in the local users table.
    Creates the user if they don't exist.
    Returns the user's database ID.
    """
    if not user or not db_connection:
        return None

    try:
        cursor = db_connection.cursor()

        # Check if user exists
        cursor.execute(
            "SELECT id FROM users WHERE cognito_sub = %s",
            (user.get("sub"),)
        )
        result = cursor.fetchone()

        if result:
            # Update last_login
            cursor.execute(
                "UPDATE users SET last_login = NOW() WHERE id = %s",
                (result[0],)
            )
            db_connection.commit()
            return result[0]

        # Create new user
        cursor.execute(
            """
            INSERT INTO users (cognito_sub, email, display_name, role, last_login)
            VALUES (%s, %s, %s, %s, NOW())
            RETURNING id
            """,
            (
                user.get("sub"),
                user.get("email"),
                user.get("name"),
                user.get("role", "viewer")
            )
        )
        result = cursor.fetchone()
        db_connection.commit()

        return result[0] if result else None

    except Exception as e:
        logger.error(f"ensuring user in database: {e}")
        db_connection.rollback()
        return None


# ============================================================================
# PASSWORD VALIDATION
# ============================================================================

def validate_password_strength(password: str) -> Tuple[bool, List[str]]:
    """
    Validate password meets HIPAA-compliant strength requirements.

    Requirements:
    - At least 12 characters
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one number
    - At least one special character

    Returns:
        Tuple of (is_valid, list_of_error_messages)
    """
    errors = []

    if len(password) < 12:
        errors.append("Password must be at least 12 characters long")

    if not re.search(r"[A-Z]", password):
        errors.append("Password must contain at least one uppercase letter")

    if not re.search(r"[a-z]", password):
        errors.append("Password must contain at least one lowercase letter")

    if not re.search(r"[0-9]", password):
        errors.append("Password must contain at least one number")

    if not re.search(r"[!@#$%^&*()_+\-=\[\]{};':\"\\|,.<>/?]", password):
        errors.append("Password must contain at least one special character (!@#$%^&*)")

    return (len(errors) == 0, errors)


# ============================================================================
# REGISTRATION DATA VALIDATION
# ============================================================================

def validate_email(email: str) -> bool:
    """Validate email format."""
    if not email:
        return False
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return bool(re.match(pattern, email))


def validate_phone(phone: str) -> bool:
    """
    Validate phone number format.
    Expects E.164 format or at least 10 digits.
    """
    if not phone:
        return False
    # Remove all non-digit characters except +
    cleaned = re.sub(r"[^\d+]", "", phone)
    # Check for E.164 format or at least 10 digits
    if cleaned.startswith("+"):
        return len(cleaned) >= 11  # + and at least 10 digits
    return len(cleaned) >= 10


def normalize_phone(phone: str) -> str:
    """
    Normalize phone number to E.164 format.
    Assumes US number if no country code provided.
    """
    # Remove all non-digit characters except +
    cleaned = re.sub(r"[^\d+]", "", phone)

    if cleaned.startswith("+"):
        return cleaned

    # Assume US number if no country code
    if len(cleaned) == 10:
        return f"+1{cleaned}"
    elif len(cleaned) == 11 and cleaned.startswith("1"):
        return f"+{cleaned}"

    return f"+{cleaned}"


def validate_registration_data(form_data: dict) -> Tuple[bool, dict]:
    """
    Validate all registration form data (full validation including password).

    Args:
        form_data: Dictionary containing form field values

    Returns:
        Tuple of (is_valid, errors_dict)
        errors_dict maps field names to error messages
    """
    errors = {}

    # Required text fields (only name fields required upfront)
    required_fields = {
        "first_name": "First name is required",
        "last_name": "Last name is required",
    }

    for field, message in required_fields.items():
        value = form_data.get(field, "").strip()
        if not value:
            errors[field] = message

    # Email is always required (primary contact method)
    email = form_data.get("email", "").strip()
    if not email:
        errors["email"] = "Email address is required"
    elif not validate_email(email):
        errors["email"] = "Please enter a valid email address"

    # Validate password
    password = form_data.get("password", "")
    confirm_password = form_data.get("confirm_password", "")

    if not password:
        errors["password"] = "Password is required"
    else:
        is_strong, pw_errors = validate_password_strength(password)
        if not is_strong:
            errors["password"] = pw_errors[0]  # Show first error

    if password and password != confirm_password:
        errors["confirm_password"] = "Passwords do not match"

    # Validate consent checkboxes
    if not form_data.get("terms_accepted"):
        errors["terms_accepted"] = "You must agree to the Terms of Service"

    if not form_data.get("hipaa_acknowledged"):
        errors["hipaa_acknowledged"] = "You must acknowledge HIPAA compliance requirements"

    return (len(errors) == 0, errors)


def validate_registration_data_step1(form_data: dict) -> Tuple[bool, dict]:
    """
    Validate Step 1 registration form data (NO password).

    Args:
        form_data: Dictionary containing form field values

    Returns:
        Tuple of (is_valid, errors_dict)
        errors_dict maps field names to error messages
    """
    errors = {}

    # Required text fields
    required_fields = {
        "first_name": "First name is required",
        "last_name": "Last name is required",
    }

    for field, message in required_fields.items():
        value = form_data.get(field, "").strip()
        if not value:
            errors[field] = message

    # At least one contact method is required (email OR phone)
    email = form_data.get("email", "").strip()
    phone = form_data.get("phone", "").strip()

    if not email and not phone:
        errors["email"] = "Email or phone number is required"
    else:
        # Validate email format if provided
        if email and not validate_email(email):
            errors["email"] = "Please enter a valid email address"
        # Validate phone format if provided
        if phone and not validate_phone(phone):
            errors["phone"] = "Please enter a valid phone number"

    # Validate consent checkboxes
    if not form_data.get("terms_accepted"):
        errors["terms_accepted"] = "You must agree to the Terms of Service"

    if not form_data.get("hipaa_acknowledged"):
        errors["hipaa_acknowledged"] = "You must acknowledge HIPAA compliance requirements"

    return (len(errors) == 0, errors)


# ============================================================================
# COGNITO USER CREATION
# ============================================================================

def create_cognito_user(
    username: str,
    password: str,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Create a new user in Cognito User Pool.

    Args:
        username: The username (typically email or phone)
        password: The user's password
        email: Optional email address
        phone: Optional phone number (E.164 format)
        first_name: User's first name
        last_name: User's last name

    Returns:
        Tuple of (success, user_sub, error_message)
        - success: True if user was created
        - user_sub: The Cognito user sub (UUID) if successful
        - error_message: Error description if failed
    """
    if not COGNITO_USER_POOL_ID or not COGNITO_CLIENT_ID:
        return (False, None, "Cognito is not configured")

    client = get_cognito_client()

    # Build user attributes
    user_attributes = []

    if email:
        user_attributes.append({
            "Name": "email",
            "Value": email
        })

    if phone:
        normalized_phone = normalize_phone(phone)
        user_attributes.append({
            "Name": "phone_number",
            "Value": normalized_phone
        })

    if first_name:
        user_attributes.append({
            "Name": "given_name",
            "Value": first_name
        })

    if last_name:
        user_attributes.append({
            "Name": "family_name",
            "Value": last_name
        })

    # Combine first and last name for the name attribute
    if first_name or last_name:
        full_name = f"{first_name or ''} {last_name or ''}".strip()
        user_attributes.append({
            "Name": "name",
            "Value": full_name
        })

    try:
        # Use SignUp API for self-registration
        response = client.sign_up(
            ClientId=COGNITO_CLIENT_ID,
            Username=username,
            Password=password,
            UserAttributes=user_attributes,
        )

        user_sub = response.get("UserSub")
        logger.info(f"REGISTRATION: User created successfully: {username} (sub: {user_sub})")

        return (True, user_sub, None)

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        error_message = e.response.get("Error", {}).get("Message", str(e))

        logger.error(f"REGISTRATION ERROR: {error_code} - {error_message}")

        # Map Cognito errors to user-friendly messages
        if error_code == "UsernameExistsException":
            return (False, None, "An account with this email or phone already exists")
        elif error_code == "InvalidPasswordException":
            return (False, None, "Password does not meet requirements")
        elif error_code == "InvalidParameterException":
            if "phone" in error_message.lower():
                return (False, None, "Invalid phone number format. Please use format: +1234567890")
            elif "email" in error_message.lower():
                return (False, None, "Invalid email address format")
            return (False, None, f"Invalid input: {error_message}")
        elif error_code == "CodeDeliveryFailureException":
            return (False, None, "Failed to send verification code. Please check your email/phone.")
        else:
            return (False, None, f"Registration failed: {error_message}")

    except Exception as e:
        logger.error(f"REGISTRATION ERROR: Unexpected error - {e}")
        return (False, None, "An unexpected error occurred during registration")


def resend_confirmation_code(username: str) -> Tuple[bool, Optional[str]]:
    """
    Resend the confirmation code to a user.

    Args:
        username: The username (email or phone)

    Returns:
        Tuple of (success, error_message)
    """
    if not COGNITO_CLIENT_ID:
        return (False, "Cognito is not configured")

    client = get_cognito_client()

    try:
        client.resend_confirmation_code(
            ClientId=COGNITO_CLIENT_ID,
            Username=username,
        )
        return (True, None)

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        error_message = e.response.get("Error", {}).get("Message", str(e))

        if error_code == "UserNotFoundException":
            return (False, "User not found")
        elif error_code == "LimitExceededException":
            return (False, "Too many attempts. Please try again later.")
        else:
            return (False, f"Failed to resend code: {error_message}")

    except Exception as e:
        return (False, f"Unexpected error: {e}")


def confirm_user_signup(username: str, code: str) -> Tuple[bool, Optional[str]]:
    """
    Confirm a user's signup with the verification code.

    Args:
        username: The username (email)
        code: The 6-digit verification code

    Returns:
        Tuple of (success, error_message)
    """
    if not COGNITO_CLIENT_ID:
        return (False, "Cognito is not configured")

    client = get_cognito_client()

    try:
        client.confirm_sign_up(
            ClientId=COGNITO_CLIENT_ID,
            Username=username,
            ConfirmationCode=code,
        )
        logger.info(f"VERIFICATION: User confirmed successfully: {username}")
        return (True, None)

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        error_message = e.response.get("Error", {}).get("Message", str(e))

        logger.error(f"VERIFICATION ERROR: {error_code} - {error_message}")

        if error_code == "CodeMismatchException":
            return (False, "Invalid verification code. Please check and try again.")
        elif error_code == "ExpiredCodeException":
            return (False, "Verification code has expired. Please request a new code.")
        elif error_code == "UserNotFoundException":
            return (False, "User not found. Please register again.")
        elif error_code == "NotAuthorizedException":
            return (False, "User is already confirmed.")
        elif error_code == "LimitExceededException":
            return (False, "Too many attempts. Please try again later.")
        else:
            return (False, f"Verification failed: {error_message}")

    except Exception as e:
        logger.error(f"VERIFICATION ERROR: Unexpected error - {e}")
        return (False, "An unexpected error occurred during verification")


# ============================================================================
# SESSION-BASED VERIFICATION (for three-step registration flow)
# ============================================================================

def generate_verification_code() -> str:
    """Generate a 6-digit verification code."""
    import secrets
    return ''.join([str(secrets.randbelow(10)) for _ in range(6)])


def send_verification_email(email: str, code: str) -> Tuple[bool, Optional[str]]:
    """
    Send verification code via AWS SES.

    In development mode (SES not configured), logs the code to console and succeeds.

    Args:
        email: Recipient email address
        code: 6-digit verification code

    Returns:
        Tuple of (success, error_message)
    """
    # Always log the code for development
    logger.info("=" * 50)
    logger.info(f"VERIFICATION CODE for {email}: {code}")
    logger.info("=" * 50)

    try:
        ses_client = boto3.client('ses', region_name=COGNITO_REGION)

        # Debug logging for SES
        logger.debug(f"SES: Attempting to send from samir8787@gmail.com to {email}")
        logger.debug(f"SES: Using region {COGNITO_REGION}")

        subject = 'OpenRadiomics - Verification Code'
        body_text = f"""Your OpenRadiomics verification code is: {code}

This code will expire in 10 minutes.

If you did not request this code, please ignore this email.

- The OpenRadiomics Team
"""
        body_html = f"""
<html>
<head></head>
<body>
  <h2>OpenRadiomics Verification</h2>
  <p>Your verification code is:</p>
  <h1 style="color: #4fc3f7; font-size: 32px; letter-spacing: 5px;">{code}</h1>
  <p>This code will expire in 10 minutes.</p>
  <p style="color: #888;">If you did not request this code, please ignore this email.</p>
  <hr>
  <p style="color: #888; font-size: 12px;">- The OpenRadiomics Team</p>
</body>
</html>
"""

        ses_client.send_email(
            Source='samir8787@gmail.com',  # Verified in SES
            Destination={'ToAddresses': [email]},
            Message={
                'Subject': {'Data': subject, 'Charset': 'UTF-8'},
                'Body': {
                    'Text': {'Data': body_text, 'Charset': 'UTF-8'},
                    'Html': {'Data': body_html, 'Charset': 'UTF-8'},
                }
            }
        )

        logger.info(f"VERIFICATION: Email sent successfully to {email}")
        return (True, None)

    except Exception as e:
        error_msg = str(e)
        logger.error(f"SES ERROR: {error_msg}")
        # Return the actual error so user knows email failed
        return (False, f"Failed to send email: {error_msg}")


def send_verification_sms(phone: str, code: str) -> Tuple[bool, Optional[str]]:
    """
    Send verification code via AWS SNS.

    Args:
        phone: Recipient phone number (E.164 format)
        code: 6-digit verification code

    Returns:
        Tuple of (success, error_message)
    """
    try:
        normalized_phone = normalize_phone(phone)
        sns_client = boto3.client('sns', region_name=COGNITO_REGION)

        message = f"Your OpenRadiomics verification code is: {code}. This code expires in 10 minutes."

        sns_client.publish(
            PhoneNumber=normalized_phone,
            Message=message,
            MessageAttributes={
                'AWS.SNS.SMS.SenderID': {
                    'DataType': 'String',
                    'StringValue': 'OpenRadio'
                },
                'AWS.SNS.SMS.SMSType': {
                    'DataType': 'String',
                    'StringValue': 'Transactional'
                }
            }
        )

        logger.info(f"VERIFICATION: Sent SMS code to {normalized_phone}")
        return (True, None)

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        error_message = e.response.get("Error", {}).get("Message", str(e))
        logger.error(f"SNS ERROR: {error_code} - {error_message}")

        if error_code == "InvalidParameter":
            return (False, "Invalid phone number format. Please use format: +1234567890")
        else:
            return (False, f"Failed to send SMS: {error_message}")

    except Exception as e:
        logger.error(f"VERIFICATION ERROR: Failed to send SMS - {e}")
        # In development, log the code and succeed
        logger.info(f"DEV MODE: Verification code for {phone}: {code}")
        return (True, None)


# ============================================================================
# PENDING REGISTRATIONS (Database-backed with rate limiting)
# ============================================================================

# Rate limiting constants
RESEND_COOLDOWN_SECONDS = 60  # Minimum seconds between resend attempts
MAX_SENDS_PER_REGISTRATION = 5  # Maximum email/SMS sends per registration attempt
CODE_EXPIRY_MINUTES = 10  # Verification code expiry time


def create_pending_registration(
    db_connection,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    form_data: Optional[dict] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Create or update a pending registration record and send verification code.

    Args:
        db_connection: Database connection
        email: Email address
        phone: Phone number (E.164 format)
        first_name: User's first name
        last_name: User's last name
        form_data: Additional form data to store as JSON

    Returns:
        Tuple of (success, verification_code, error_message)
    """
    from datetime import datetime, timedelta, timezone

    if not email and not phone:
        return (False, None, "Email or phone is required")

    code = generate_verification_code()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=CODE_EXPIRY_MINUTES)

    try:
        cursor = db_connection.cursor()

        # Check if registration already exists
        lookup_field = "email" if email else "phone"
        lookup_value = email if email else normalize_phone(phone)

        cursor.execute(
            f"SELECT id, email_send_count, sms_send_count, last_email_sent_at, last_sms_sent_at "
            f"FROM pending_registrations WHERE {lookup_field} = %s",
            (lookup_value,)
        )
        existing = cursor.fetchone()

        if existing:
            reg_id, email_count, sms_count, last_email, last_sms = existing

            # Check rate limiting
            send_count = email_count if email else sms_count
            last_sent = last_email if email else last_sms

            if send_count >= MAX_SENDS_PER_REGISTRATION:
                return (False, None, "Maximum verification attempts reached. Please try again later or contact support.")

            if last_sent:
                seconds_since_last = (datetime.now(timezone.utc) - last_sent).total_seconds()
                if seconds_since_last < RESEND_COOLDOWN_SECONDS:
                    wait_time = int(RESEND_COOLDOWN_SECONDS - seconds_since_last)
                    return (False, None, f"Please wait {wait_time} seconds before requesting another code.")

            # Update existing record
            if email:
                cursor.execute(
                    """
                    UPDATE pending_registrations
                    SET verification_code = %s,
                        code_expires_at = %s,
                        last_email_sent_at = NOW(),
                        email_send_count = email_send_count + 1,
                        first_name = COALESCE(%s, first_name),
                        last_name = COALESCE(%s, last_name),
                        form_data = COALESCE(%s, form_data),
                        status = 'pending',
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (code, expires_at, first_name, last_name, json.dumps(form_data) if form_data else None, reg_id)
                )
            else:
                cursor.execute(
                    """
                    UPDATE pending_registrations
                    SET verification_code = %s,
                        code_expires_at = %s,
                        last_sms_sent_at = NOW(),
                        sms_send_count = sms_send_count + 1,
                        first_name = COALESCE(%s, first_name),
                        last_name = COALESCE(%s, last_name),
                        form_data = COALESCE(%s, form_data),
                        status = 'pending',
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (code, expires_at, first_name, last_name, json.dumps(form_data) if form_data else None, reg_id)
                )
        else:
            # Create new record
            normalized_phone = normalize_phone(phone) if phone else None
            cursor.execute(
                """
                INSERT INTO pending_registrations
                    (email, phone, first_name, last_name, verification_code, code_expires_at,
                     last_email_sent_at, last_sms_sent_at, email_send_count, sms_send_count, form_data)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    email,
                    normalized_phone,
                    first_name,
                    last_name,
                    code,
                    expires_at,
                    datetime.now(timezone.utc) if email else None,
                    datetime.now(timezone.utc) if phone and not email else None,
                    1 if email else 0,
                    1 if phone and not email else 0,
                    json.dumps(form_data) if form_data else None
                )
            )

        db_connection.commit()

        # Send verification code
        if email:
            success, error = send_verification_email(email, code)
        else:
            success, error = send_verification_sms(phone, code)

        if not success:
            return (False, None, error)

        return (True, code, None)

    except Exception as e:
        db_connection.rollback()
        logger.error(f"PENDING REGISTRATION ERROR: {e}")
        return (False, None, f"Database error: {str(e)}")


def verify_pending_registration(
    db_connection,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    code: str = ""
) -> Tuple[bool, Optional[dict], Optional[str]]:
    """
    Verify a pending registration code.

    Args:
        db_connection: Database connection
        email: Email address
        phone: Phone number
        code: 6-digit verification code

    Returns:
        Tuple of (success, registration_data, error_message)
    """
    from datetime import datetime, timezone

    if not email and not phone:
        return (False, None, "Email or phone is required")

    if not code or len(code) != 6:
        return (False, None, "Invalid verification code format")

    try:
        cursor = db_connection.cursor()

        lookup_field = "email" if email else "phone"
        lookup_value = email if email else normalize_phone(phone)

        cursor.execute(
            f"""
            SELECT id, email, phone, first_name, last_name, verification_code,
                   code_expires_at, form_data, status
            FROM pending_registrations
            WHERE {lookup_field} = %s
            """,
            (lookup_value,)
        )
        result = cursor.fetchone()

        if not result:
            return (False, None, "Registration not found. Please start over.")

        (reg_id, reg_email, reg_phone, first_name, last_name,
         stored_code, expires_at, form_data, status) = result

        # Check if already verified
        if status == 'verified':
            return (True, {
                "id": reg_id,
                "email": reg_email,
                "phone": reg_phone,
                "first_name": first_name,
                "last_name": last_name,
                "form_data": form_data
            }, None)

        # Check expiration
        if datetime.now(timezone.utc) > expires_at:
            return (False, None, "Verification code has expired. Please request a new code.")

        # Check code
        if code != stored_code:
            return (False, None, "Invalid verification code. Please check and try again.")

        # Mark as verified
        cursor.execute(
            """
            UPDATE pending_registrations
            SET status = 'verified', updated_at = NOW()
            WHERE id = %s
            """,
            (reg_id,)
        )
        db_connection.commit()

        return (True, {
            "id": reg_id,
            "email": reg_email,
            "phone": reg_phone,
            "first_name": first_name,
            "last_name": last_name,
            "form_data": form_data
        }, None)

    except Exception as e:
        db_connection.rollback()
        logger.error(f"VERIFICATION ERROR: {e}")
        return (False, None, f"Database error: {str(e)}")


def get_pending_registration(
    db_connection,
    email: Optional[str] = None,
    phone: Optional[str] = None
) -> Optional[dict]:
    """
    Get a pending registration by email or phone.

    Returns:
        Registration data dict or None if not found
    """
    if not email and not phone:
        return None

    try:
        cursor = db_connection.cursor()

        lookup_field = "email" if email else "phone"
        lookup_value = email if email else normalize_phone(phone)

        cursor.execute(
            f"""
            SELECT id, email, phone, first_name, last_name, status, form_data
            FROM pending_registrations
            WHERE {lookup_field} = %s
            """,
            (lookup_value,)
        )
        result = cursor.fetchone()

        if not result:
            return None

        return {
            "id": result[0],
            "email": result[1],
            "phone": result[2],
            "first_name": result[3],
            "last_name": result[4],
            "status": result[5],
            "form_data": result[6]
        }

    except Exception as e:
        logger.error(f"GET PENDING REGISTRATION ERROR: {e}")
        return None


def complete_pending_registration(
    db_connection,
    email: Optional[str] = None,
    phone: Optional[str] = None
) -> bool:
    """
    Mark a pending registration as completed (after user account is created).

    Returns:
        True if updated, False otherwise
    """
    if not email and not phone:
        return False

    try:
        cursor = db_connection.cursor()

        lookup_field = "email" if email else "phone"
        lookup_value = email if email else normalize_phone(phone)

        cursor.execute(
            f"""
            UPDATE pending_registrations
            SET status = 'completed', updated_at = NOW()
            WHERE {lookup_field} = %s AND status = 'verified'
            """,
            (lookup_value,)
        )
        db_connection.commit()
        return cursor.rowcount > 0

    except Exception as e:
        db_connection.rollback()
        logger.error(f"COMPLETE REGISTRATION ERROR: {e}")
        return False


def delete_pending_registration(
    db_connection,
    email: Optional[str] = None,
    phone: Optional[str] = None
) -> bool:
    """
    Delete a pending registration (cleanup after completion or expiry).

    Returns:
        True if deleted, False otherwise
    """
    if not email and not phone:
        return False

    try:
        cursor = db_connection.cursor()

        lookup_field = "email" if email else "phone"
        lookup_value = email if email else normalize_phone(phone)

        cursor.execute(
            f"DELETE FROM pending_registrations WHERE {lookup_field} = %s",
            (lookup_value,)
        )
        db_connection.commit()
        return cursor.rowcount > 0

    except Exception as e:
        db_connection.rollback()
        logger.error(f"DELETE PENDING REGISTRATION ERROR: {e}")
        return False


# ============================================================================
# USER EXISTENCE CHECK
# ============================================================================

def check_user_exists(db_connection, email: str) -> dict:
    """
    Check if user exists and return their state.

    Checks:
    1. users table (completed registration)
    2. pending_registrations table (incomplete registration)
    3. Cognito User Pool

    Args:
        db_connection: Database connection
        email: Email address to check

    Returns:
        {
            "exists": bool,
            "state": "completed" | "pending" | "cognito_only" | "none",
            "message": str,
            "options": ["login", "reset_password", "continue_registration", "start_over", "resend_verification"]
        }
    """
    if not email:
        return {
            "exists": False,
            "state": "none",
            "message": "Email is required",
            "options": []
        }

    result = {
        "exists": False,
        "state": "none",
        "message": "",
        "options": []
    }

    try:
        cursor = db_connection.cursor()

        # Check users table (completed registration)
        cursor.execute(
            "SELECT id, email FROM users WHERE email = %s",
            (email,)
        )
        user = cursor.fetchone()

        if user:
            result["exists"] = True
            result["state"] = "completed"
            result["message"] = "An account with this email already exists."
            result["options"] = ["login", "reset_password"]
            return result

        # Check pending_registrations table
        cursor.execute(
            "SELECT id, email, status FROM pending_registrations WHERE email = %s",
            (email,)
        )
        pending = cursor.fetchone()

        if pending:
            pending_status = pending[2] if len(pending) > 2 else "pending"
            result["exists"] = True
            result["state"] = "pending"
            if pending_status == "verified":
                result["message"] = "You started registration and verified your email, but didn't complete it."
                result["options"] = ["continue_registration", "start_over"]
            else:
                result["message"] = "You started registration but didn't complete verification."
                result["options"] = ["continue_registration", "start_over"]
            return result

        # Check Cognito (if configured)
        if COGNITO_USER_POOL_ID:
            try:
                client = get_cognito_client()
                response = client.admin_get_user(
                    UserPoolId=COGNITO_USER_POOL_ID,
                    Username=email
                )
                # User exists in Cognito but not in our database
                user_status = response.get("UserStatus", "")
                result["exists"] = True
                result["state"] = "cognito_only"

                if user_status == "UNCONFIRMED":
                    result["message"] = "Account exists but email needs verification."
                    result["options"] = ["resend_verification", "start_over"]
                else:
                    result["message"] = "Account exists in authentication system."
                    result["options"] = ["login", "reset_password"]
                return result

            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "")
                if error_code != "UserNotFoundException":
                    logger.warning(f"Cognito check error: {e}")
                # User not found in Cognito - that's fine, continue

        # User doesn't exist anywhere
        result["exists"] = False
        result["state"] = "none"
        result["message"] = ""
        result["options"] = []
        return result

    except Exception as e:
        logger.error(f"CHECK USER EXISTS ERROR: {e}")
        return {
            "exists": False,
            "state": "error",
            "message": "Unable to check user status",
            "options": []
        }


# ============================================================================
# DIRECT AUTHENTICATION (USER_PASSWORD_AUTH flow)
# ============================================================================

def authenticate_user(email: str, password: str) -> Tuple[bool, Optional[dict], Optional[str]]:
    """
    Authenticate a user with email and password using Cognito USER_PASSWORD_AUTH flow.

    Args:
        email: User's email address (username)
        password: User's password

    Returns:
        Tuple of (success, result_data, error_message)
        - On success: (True, {"tokens": {...}}, None)
        - On MFA required: (True, {"mfaRequired": True, "session": "...", "challengeName": "..."}, None)
        - On error: (False, None, error_message)
    """
    logger.info(f"AUTH: authenticate_user called for {email}")
    logger.info(f"AUTH: COGNITO_CLIENT_ID configured: {bool(COGNITO_CLIENT_ID)}")

    if not COGNITO_CLIENT_ID:
        logger.error("AUTH: COGNITO_CLIENT_ID not configured!")
        return (False, None, "Authentication service is not configured")

    if not email or not password:
        logger.warning("AUTH: Missing email or password")
        return (False, None, "Email and password are required")

    client = get_cognito_client()
    logger.info(f"AUTH: Calling Cognito initiate_auth with USER_PASSWORD_AUTH")

    try:
        response = client.initiate_auth(
            AuthFlow='USER_PASSWORD_AUTH',
            ClientId=COGNITO_CLIENT_ID,
            AuthParameters={
                'USERNAME': email,
                'PASSWORD': password
            }
        )
        logger.info(f"AUTH: Cognito response keys: {response.keys()}")

        # Check if MFA challenge is required
        if response.get('ChallengeName'):
            challenge_name = response['ChallengeName']
            session = response.get('Session')

            if challenge_name in ('SOFTWARE_TOKEN_MFA', 'SMS_MFA'):
                logger.info(f"LOGIN: MFA required for {email}")
                return (True, {
                    "mfaRequired": True,
                    "session": session,
                    "challengeName": challenge_name
                }, None)
            elif challenge_name == 'NEW_PASSWORD_REQUIRED':
                return (False, None, "You must set a new password. Please use the password reset flow.")
            else:
                return (False, None, f"Unsupported authentication challenge: {challenge_name}")

        # Success - return tokens
        auth_result = response.get('AuthenticationResult', {})
        tokens = {
            "access_token": auth_result.get('AccessToken'),
            "id_token": auth_result.get('IdToken'),
            "refresh_token": auth_result.get('RefreshToken'),
            "expires_in": auth_result.get('ExpiresIn')
        }

        logger.info(f"LOGIN: User authenticated successfully: {email}")
        return (True, {"tokens": tokens}, None)

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        error_message = e.response.get("Error", {}).get("Message", str(e))

        logger.warning(f"LOGIN ERROR: {error_code} - {error_message}")

        if error_code == "NotAuthorizedException":
            return (False, None, "Invalid email or password")
        elif error_code == "UserNotFoundException":
            # Don't reveal that user doesn't exist
            return (False, None, "Invalid email or password")
        elif error_code == "UserNotConfirmedException":
            return (False, None, "Your email has not been verified. Please complete registration.")
        elif error_code == "PasswordResetRequiredException":
            return (False, None, "You must reset your password before signing in.")
        elif error_code == "TooManyRequestsException":
            return (False, None, "Too many sign-in attempts. Please try again later.")
        else:
            return (False, None, f"Authentication failed: {error_message}")

    except Exception as e:
        logger.error(f"LOGIN ERROR: Unexpected error - {e}")
        return (False, None, "An unexpected error occurred during sign in")


def respond_to_mfa_challenge(
    email: str,
    session: str,
    mfa_code: str,
    challenge_name: str = "SOFTWARE_TOKEN_MFA"
) -> Tuple[bool, Optional[dict], Optional[str]]:
    """
    Respond to an MFA challenge with the user's code.

    Args:
        email: User's email (username)
        session: Session token from initiate_auth
        mfa_code: 6-digit MFA code from authenticator app
        challenge_name: Type of MFA challenge (SOFTWARE_TOKEN_MFA or SMS_MFA)

    Returns:
        Tuple of (success, tokens_dict, error_message)
    """
    if not COGNITO_CLIENT_ID:
        return (False, None, "Authentication service is not configured")

    if not session or not mfa_code:
        return (False, None, "Session and MFA code are required")

    client = get_cognito_client()

    try:
        # Build challenge responses based on challenge type
        challenge_responses = {
            'USERNAME': email
        }

        if challenge_name == 'SOFTWARE_TOKEN_MFA':
            challenge_responses['SOFTWARE_TOKEN_MFA_CODE'] = mfa_code
        elif challenge_name == 'SMS_MFA':
            challenge_responses['SMS_MFA_CODE'] = mfa_code

        response = client.respond_to_auth_challenge(
            ClientId=COGNITO_CLIENT_ID,
            ChallengeName=challenge_name,
            Session=session,
            ChallengeResponses=challenge_responses
        )

        # Check for additional challenges
        if response.get('ChallengeName'):
            return (False, None, "Additional authentication required. Please contact support.")

        # Success - return tokens
        auth_result = response.get('AuthenticationResult', {})
        tokens = {
            "access_token": auth_result.get('AccessToken'),
            "id_token": auth_result.get('IdToken'),
            "refresh_token": auth_result.get('RefreshToken'),
            "expires_in": auth_result.get('ExpiresIn')
        }

        logger.info(f"LOGIN: MFA verification successful for {email}")
        return (True, tokens, None)

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        error_message = e.response.get("Error", {}).get("Message", str(e))

        logger.warning(f"MFA ERROR: {error_code} - {error_message}")

        if error_code == "CodeMismatchException":
            return (False, None, "Invalid MFA code. Please try again.")
        elif error_code == "ExpiredCodeException":
            return (False, None, "MFA code has expired. Please sign in again.")
        elif error_code == "NotAuthorizedException":
            return (False, None, "Session expired. Please sign in again.")
        else:
            return (False, None, f"MFA verification failed: {error_message}")

    except Exception as e:
        logger.error(f"MFA ERROR: Unexpected error - {e}")
        return (False, None, "An unexpected error occurred during MFA verification")


# ============================================================================
# PASSWORD RESET (Cognito)
# ============================================================================

def initiate_password_reset(email: str) -> Tuple[bool, Optional[str]]:
    """
    Request password reset code via Cognito's forgot_password flow.

    Args:
        email: User's email address

    Returns:
        Tuple of (success, error_message)
    """
    if not COGNITO_CLIENT_ID:
        return (False, "Cognito is not configured")

    if not email:
        return (False, "Email is required")

    client = get_cognito_client()

    try:
        client.forgot_password(
            ClientId=COGNITO_CLIENT_ID,
            Username=email
        )
        logger.info(f"PASSWORD RESET: Code sent to {email}")
        return (True, None)

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        error_message = e.response.get("Error", {}).get("Message", str(e))

        logger.error(f"PASSWORD RESET ERROR: {error_code} - {error_message}")

        if error_code == "UserNotFoundException":
            # Don't reveal that user doesn't exist for security
            return (True, None)
        elif error_code == "InvalidParameterException":
            if "verified" in error_message.lower():
                return (False, "Email address has not been verified. Please register again.")
            return (False, f"Invalid request: {error_message}")
        elif error_code == "LimitExceededException":
            return (False, "Too many password reset attempts. Please try again later.")
        elif error_code == "NotAuthorizedException":
            return (False, "Cannot reset password for this account. Please contact support.")
        else:
            return (False, f"Failed to initiate password reset: {error_message}")

    except Exception as e:
        logger.error(f"PASSWORD RESET ERROR: Unexpected error - {e}")
        return (False, "An unexpected error occurred")


def confirm_password_reset(email: str, code: str, new_password: str) -> Tuple[bool, Optional[str]]:
    """
    Confirm password reset with verification code and new password.

    Args:
        email: User's email address
        code: 6-digit verification code from email
        new_password: New password to set

    Returns:
        Tuple of (success, error_message)
    """
    if not COGNITO_CLIENT_ID:
        return (False, "Cognito is not configured")

    if not email or not code or not new_password:
        return (False, "Email, code, and new password are required")

    # Validate password strength
    is_strong, pw_errors = validate_password_strength(new_password)
    if not is_strong:
        return (False, pw_errors[0] if pw_errors else "Password does not meet requirements")

    client = get_cognito_client()

    try:
        client.confirm_forgot_password(
            ClientId=COGNITO_CLIENT_ID,
            Username=email,
            ConfirmationCode=code,
            Password=new_password
        )
        logger.info(f"PASSWORD RESET: Password successfully reset for {email}")
        return (True, None)

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        error_message = e.response.get("Error", {}).get("Message", str(e))

        logger.error(f"PASSWORD RESET CONFIRM ERROR: {error_code} - {error_message}")

        if error_code == "CodeMismatchException":
            return (False, "Invalid verification code. Please check and try again.")
        elif error_code == "ExpiredCodeException":
            return (False, "Verification code has expired. Please request a new code.")
        elif error_code == "UserNotFoundException":
            return (False, "User not found. Please check your email address.")
        elif error_code == "InvalidPasswordException":
            return (False, "Password does not meet requirements.")
        elif error_code == "LimitExceededException":
            return (False, "Too many attempts. Please try again later.")
        else:
            return (False, f"Failed to reset password: {error_message}")

    except Exception as e:
        logger.error(f"PASSWORD RESET CONFIRM ERROR: Unexpected error - {e}")
        return (False, "An unexpected error occurred")


# ============================================================================
# LINK-BASED PASSWORD RESET (Token-based, no code entry)
# ============================================================================

def generate_reset_token() -> str:
    """Generate a secure random token for password reset links."""
    import secrets
    return secrets.token_urlsafe(48)  # 64 characters URL-safe


def create_password_reset_token(db_connection, email: str) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Create a password reset token and store it in the database.

    Args:
        db_connection: Database connection
        email: User's email address

    Returns:
        Tuple of (success, token, error_message)
    """
    from datetime import datetime, timedelta, timezone

    if not email:
        return (False, None, "Email is required")

    token = generate_reset_token()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

    try:
        cursor = db_connection.cursor()

        # Invalidate any existing tokens for this email
        cursor.execute(
            """
            UPDATE password_reset_tokens
            SET used_at = NOW()
            WHERE email = %s AND used_at IS NULL
            """,
            (email,)
        )

        # Create new token
        cursor.execute(
            """
            INSERT INTO password_reset_tokens (email, token, expires_at)
            VALUES (%s, %s, %s)
            """,
            (email, token, expires_at)
        )

        db_connection.commit()
        logger.info(f"PASSWORD RESET: Token created for {email}")
        return (True, token, None)

    except Exception as e:
        db_connection.rollback()
        logger.error(f"PASSWORD RESET TOKEN ERROR: {e}")
        return (False, None, f"Failed to create reset token: {str(e)}")


def validate_reset_token(db_connection, token: str) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Validate a password reset token.

    Args:
        db_connection: Database connection
        token: The reset token from the URL

    Returns:
        Tuple of (is_valid, email, error_message)
    """
    from datetime import datetime, timezone

    if not token:
        return (False, None, "Reset token is required")

    try:
        cursor = db_connection.cursor()

        cursor.execute(
            """
            SELECT email, expires_at, used_at
            FROM password_reset_tokens
            WHERE token = %s
            """,
            (token,)
        )
        result = cursor.fetchone()

        if not result:
            return (False, None, "Invalid or expired reset link. Please request a new one.")

        email = result[0] if isinstance(result, tuple) else result.get("email")
        expires_at = result[1] if isinstance(result, tuple) else result.get("expires_at")
        used_at = result[2] if isinstance(result, tuple) else result.get("used_at")

        if used_at is not None:
            return (False, None, "This reset link has already been used. Please request a new one.")

        if datetime.now(timezone.utc) > expires_at:
            return (False, None, "This reset link has expired. Please request a new one.")

        return (True, email, None)

    except Exception as e:
        logger.error(f"VALIDATE RESET TOKEN ERROR: {e}")
        return (False, None, f"Failed to validate token: {str(e)}")


def mark_reset_token_used(db_connection, token: str) -> bool:
    """
    Mark a reset token as used.

    Args:
        db_connection: Database connection
        token: The reset token

    Returns:
        True if updated, False otherwise
    """
    try:
        cursor = db_connection.cursor()

        cursor.execute(
            """
            UPDATE password_reset_tokens
            SET used_at = NOW()
            WHERE token = %s AND used_at IS NULL
            """,
            (token,)
        )

        db_connection.commit()
        return cursor.rowcount > 0

    except Exception as e:
        db_connection.rollback()
        logger.error(f"MARK TOKEN USED ERROR: {e}")
        return False


def admin_set_user_password(email: str, new_password: str) -> Tuple[bool, Optional[str]]:
    """
    Set a user's password using Cognito admin API.
    This bypasses the normal forgot password flow.

    Args:
        email: User's email address (username)
        new_password: New password to set

    Returns:
        Tuple of (success, error_message)
    """
    if not COGNITO_USER_POOL_ID:
        return (False, "Cognito is not configured")

    if not email or not new_password:
        return (False, "Email and new password are required")

    # Validate password strength
    is_strong, pw_errors = validate_password_strength(new_password)
    if not is_strong:
        return (False, pw_errors[0] if pw_errors else "Password does not meet requirements")

    client = get_cognito_client()

    try:
        client.admin_set_user_password(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=email,
            Password=new_password,
            Permanent=True
        )

        logger.info(f"PASSWORD RESET: Admin set password for {email}")
        return (True, None)

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        error_message = e.response.get("Error", {}).get("Message", str(e))

        logger.error(f"ADMIN SET PASSWORD ERROR: {error_code} - {error_message}")

        if error_code == "UserNotFoundException":
            return (False, "User not found. Please check your email address.")
        elif error_code == "InvalidPasswordException":
            return (False, "Password does not meet requirements.")
        elif error_code == "NotAuthorizedException":
            return (False, "Not authorized to reset this password.")
        else:
            return (False, f"Failed to reset password: {error_message}")

    except Exception as e:
        logger.error(f"ADMIN SET PASSWORD ERROR: Unexpected error - {e}")
        return (False, "An unexpected error occurred")


def send_password_reset_link(email: str, reset_url: str) -> Tuple[bool, Optional[str]]:
    """
    Send password reset link via AWS SES.

    Args:
        email: Recipient email address
        reset_url: Full URL for password reset

    Returns:
        Tuple of (success, error_message)
    """
    # Log for development
    logger.info("=" * 50)
    logger.info(f"PASSWORD RESET LINK for {email}:")
    logger.info(reset_url)
    logger.info("=" * 50)

    try:
        ses_client = boto3.client('ses', region_name=COGNITO_REGION)

        subject = 'OpenRadiomics - Reset Your Password'
        body_text = f"""You requested to reset your password for OpenRadiomics.

Click the link below to reset your password:
{reset_url}

This link will expire in 1 hour.

If you did not request this password reset, please ignore this email.

- The OpenRadiomics Team
"""
        body_html = f"""
<html>
<head></head>
<body>
  <h2>Reset Your Password</h2>
  <p>You requested to reset your password for OpenRadiomics.</p>
  <p>Click the button below to reset your password:</p>
  <p style="margin: 25px 0;">
    <a href="{reset_url}" style="background: #4fc3f7; color: #000; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold;">Reset Password</a>
  </p>
  <p style="color: #888; font-size: 13px;">Or copy and paste this link into your browser:</p>
  <p style="color: #4fc3f7; font-size: 12px; word-break: break-all;">{reset_url}</p>
  <p style="color: #888;">This link will expire in 1 hour.</p>
  <hr style="border: none; border-top: 1px solid #333; margin: 25px 0;">
  <p style="color: #888; font-size: 12px;">If you did not request this password reset, please ignore this email.</p>
  <p style="color: #888; font-size: 12px;">- The OpenRadiomics Team</p>
</body>
</html>
"""

        ses_client.send_email(
            Source='samir8787@gmail.com',  # Verified in SES
            Destination={'ToAddresses': [email]},
            Message={
                'Subject': {'Data': subject, 'Charset': 'UTF-8'},
                'Body': {
                    'Text': {'Data': body_text, 'Charset': 'UTF-8'},
                    'Html': {'Data': body_html, 'Charset': 'UTF-8'},
                }
            }
        )

        logger.info(f"PASSWORD RESET: Email sent successfully to {email}")
        return (True, None)

    except Exception as e:
        error_msg = str(e)
        logger.error(f"SES ERROR: {error_msg}")
        return (False, f"Failed to send email: {error_msg}")


# ============================================================================
# TRUSTED DEVICE FUNCTIONS (for MFA bypass on trusted devices)
# ============================================================================

TRUSTED_DEVICE_EXPIRY_DAYS = 90
DEVICE_TOKEN_COOKIE_NAME = "device_token"


def generate_device_token() -> str:
    """Generate a secure random token for device identification."""
    import secrets
    return secrets.token_urlsafe(48)  # 64 characters URL-safe


def get_device_token_from_request() -> Optional[str]:
    """Get the device token from the request cookie."""
    return request.cookies.get(DEVICE_TOKEN_COOKIE_NAME)


def is_device_trusted(db_connection, device_token: str, email: str) -> bool:
    """
    Check if a device token is valid and trusted for the given user.

    Args:
        db_connection: Database connection
        device_token: The device token from cookie
        email: User's email address

    Returns:
        True if device is trusted and not expired, False otherwise
    """
    from datetime import datetime, timezone

    if not device_token or not email:
        return False

    try:
        cursor = db_connection.cursor()

        cursor.execute(
            """
            SELECT id, expires_at, is_active
            FROM trusted_devices
            WHERE device_token = %s AND user_email = %s
            """,
            (device_token, email)
        )
        result = cursor.fetchone()

        if not result:
            return False

        # Handle both dict-style and tuple-style cursor results
        if isinstance(result, dict):
            expires_at = result["expires_at"]
            is_active = result["is_active"]
            device_id = result["id"]
        else:
            device_id, expires_at, is_active = result

        if not is_active:
            return False

        if datetime.now(timezone.utc) > expires_at:
            # Device trust has expired
            return False

        # Update last_used_at timestamp
        cursor.execute(
            """
            UPDATE trusted_devices
            SET last_used_at = NOW()
            WHERE id = %s
            """,
            (device_id,)
        )
        db_connection.commit()

        logger.info(f"TRUSTED DEVICE: Device recognized for {email}")
        return True

    except Exception as e:
        logger.error(f"TRUSTED DEVICE CHECK ERROR: {e}")
        return False


def trust_device(
    db_connection,
    email: str,
    device_token: str,
    user_agent: Optional[str] = None,
    ip_address: Optional[str] = None,
    device_name: Optional[str] = None
) -> Tuple[bool, Optional[str]]:
    """
    Store a trusted device for a user.

    Args:
        db_connection: Database connection
        email: User's email address
        device_token: The device token to trust
        user_agent: Browser user agent string
        ip_address: Client IP address
        device_name: Optional human-readable device name

    Returns:
        Tuple of (success, error_message)
    """
    from datetime import datetime, timedelta, timezone

    if not email or not device_token:
        return (False, "Email and device token are required")

    expires_at = datetime.now(timezone.utc) + timedelta(days=TRUSTED_DEVICE_EXPIRY_DAYS)

    # Generate device name from user agent if not provided
    if not device_name and user_agent:
        device_name = _parse_device_name(user_agent)

    try:
        cursor = db_connection.cursor()

        # Check if this device token already exists for this user
        cursor.execute(
            """
            SELECT id FROM trusted_devices
            WHERE device_token = %s AND user_email = %s
            """,
            (device_token, email)
        )
        existing = cursor.fetchone()

        if existing:
            # Update existing device trust
            cursor.execute(
                """
                UPDATE trusted_devices
                SET expires_at = %s,
                    last_used_at = NOW(),
                    is_active = TRUE,
                    user_agent = COALESCE(%s, user_agent),
                    ip_address = COALESCE(%s, ip_address),
                    device_name = COALESCE(%s, device_name)
                WHERE device_token = %s AND user_email = %s
                """,
                (expires_at, user_agent, ip_address, device_name, device_token, email)
            )
        else:
            # Create new trusted device record
            cursor.execute(
                """
                INSERT INTO trusted_devices
                    (user_email, device_token, device_name, user_agent, ip_address, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (email, device_token, device_name, user_agent, ip_address, expires_at)
            )

        db_connection.commit()
        logger.info(f"TRUSTED DEVICE: Device trusted for {email} until {expires_at}")
        return (True, None)

    except Exception as e:
        db_connection.rollback()
        logger.error(f"TRUST DEVICE ERROR: {e}")
        return (False, f"Failed to trust device: {str(e)}")


def revoke_device_trust(db_connection, device_token: str, email: str) -> bool:
    """
    Revoke trust for a specific device.

    Args:
        db_connection: Database connection
        device_token: The device token to revoke
        email: User's email address

    Returns:
        True if revoked, False otherwise
    """
    try:
        cursor = db_connection.cursor()

        cursor.execute(
            """
            UPDATE trusted_devices
            SET is_active = FALSE
            WHERE device_token = %s AND user_email = %s
            """,
            (device_token, email)
        )

        db_connection.commit()
        return cursor.rowcount > 0

    except Exception as e:
        db_connection.rollback()
        logger.error(f"REVOKE DEVICE TRUST ERROR: {e}")
        return False


def revoke_all_device_trusts(db_connection, email: str) -> int:
    """
    Revoke trust for all devices for a user.

    Args:
        db_connection: Database connection
        email: User's email address

    Returns:
        Number of devices revoked
    """
    try:
        cursor = db_connection.cursor()

        cursor.execute(
            """
            UPDATE trusted_devices
            SET is_active = FALSE
            WHERE user_email = %s AND is_active = TRUE
            """,
            (email,)
        )

        db_connection.commit()
        count = cursor.rowcount
        if count > 0:
            logger.info(f"TRUSTED DEVICE: Revoked {count} devices for {email}")
        return count

    except Exception as e:
        db_connection.rollback()
        logger.error(f"REVOKE ALL DEVICE TRUSTS ERROR: {e}")
        return 0


def _parse_device_name(user_agent: str) -> str:
    """
    Parse user agent string to create a human-readable device name.

    Args:
        user_agent: Browser user agent string

    Returns:
        Human-readable device name (e.g., "Chrome on Windows")
    """
    if not user_agent:
        return "Unknown Device"

    ua_lower = user_agent.lower()

    # Detect browser
    browser = "Unknown Browser"
    if "firefox" in ua_lower:
        browser = "Firefox"
    elif "edg" in ua_lower:
        browser = "Edge"
    elif "chrome" in ua_lower:
        browser = "Chrome"
    elif "safari" in ua_lower:
        browser = "Safari"

    # Detect OS
    os_name = "Unknown OS"
    if "windows" in ua_lower:
        os_name = "Windows"
    elif "mac os" in ua_lower or "macintosh" in ua_lower:
        os_name = "macOS"
    elif "linux" in ua_lower:
        os_name = "Linux"
    elif "android" in ua_lower:
        os_name = "Android"
    elif "iphone" in ua_lower or "ipad" in ua_lower:
        os_name = "iOS"

    return f"{browser} on {os_name}"


def create_verified_cognito_user(
    username: str,
    password: str,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Create a Cognito user with email already verified.

    Uses admin_create_user + admin_set_user_password to create a user
    that bypasses the normal verification flow (since we've already
    verified the email/phone ourselves).

    Args:
        username: The username (typically email)
        password: The user's password
        email: Email address (will be marked as verified)
        phone: Optional phone number (E.164 format)
        first_name: User's first name
        last_name: User's last name

    Returns:
        Tuple of (success, user_sub, error_message)
    """
    if not COGNITO_USER_POOL_ID or not COGNITO_CLIENT_ID:
        return (False, None, "Authentication service is not yet deployed. Please check back later or contact the administrator.")

    client = get_cognito_client()

    # Build user attributes
    user_attributes = []

    if email:
        user_attributes.append({
            "Name": "email",
            "Value": email
        })
        # Mark email as verified
        user_attributes.append({
            "Name": "email_verified",
            "Value": "true"
        })

    if phone:
        normalized_phone = normalize_phone(phone)
        user_attributes.append({
            "Name": "phone_number",
            "Value": normalized_phone
        })

    if first_name:
        user_attributes.append({
            "Name": "given_name",
            "Value": first_name
        })

    if last_name:
        user_attributes.append({
            "Name": "family_name",
            "Value": last_name
        })

    # Combine first and last name for the name attribute
    if first_name or last_name:
        full_name = f"{first_name or ''} {last_name or ''}".strip()
        user_attributes.append({
            "Name": "name",
            "Value": full_name
        })

    try:
        # First, create the user with admin API (suppresses welcome email)
        response = client.admin_create_user(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=username,
            UserAttributes=user_attributes,
            MessageAction='SUPPRESS',  # Don't send welcome email
        )

        user_sub = response.get("User", {}).get("Attributes", [])
        user_sub = next(
            (attr["Value"] for attr in user_sub if attr["Name"] == "sub"),
            None
        )

        # Now set the permanent password
        client.admin_set_user_password(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=username,
            Password=password,
            Permanent=True
        )

        logger.info(f"REGISTRATION: Verified user created successfully: {username} (sub: {user_sub})")
        return (True, user_sub, None)

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        error_message = e.response.get("Error", {}).get("Message", str(e))

        logger.error(f"REGISTRATION ERROR: {error_code} - {error_message}")

        # Map Cognito errors to user-friendly messages
        if error_code == "UsernameExistsException":
            return (False, None, "An account with this email already exists")
        elif error_code == "InvalidPasswordException":
            return (False, None, "Password does not meet requirements")
        elif error_code == "InvalidParameterException":
            if "phone" in error_message.lower():
                return (False, None, "Invalid phone number format. Please use format: +1234567890")
            elif "email" in error_message.lower():
                return (False, None, "Invalid email address format")
            return (False, None, f"Invalid input: {error_message}")
        else:
            return (False, None, f"Registration failed: {error_message}")

    except Exception as e:
        logger.error(f"REGISTRATION ERROR: Unexpected error - {e}")
        return (False, None, "An unexpected error occurred during registration")

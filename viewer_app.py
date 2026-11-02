#!/usr/bin/env python3
"""
OpenRadiomics - 3D Voxel MRI Viewer
Flask Backend serving MRI volume data to Three.js frontend.

Supports both local filesystem and S3 storage for DICOM files.
HIPAA-compliant authentication via AWS Cognito.
"""

from __future__ import annotations

import io
import json
import logging
import os
import secrets
import sys
import tempfile
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from functools import lru_cache
from urllib.parse import urljoin

import numpy as np
import pydicom
from flask import Flask, jsonify, render_template, request, redirect, url_for, make_response, session

# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================

def setup_logging():
    """Configure timestamped logging to both file and console."""
    # Create logs directory if it doesn't exist
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)

    # Log format with timestamp
    log_format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    # Create formatter
    formatter = logging.Formatter(log_format, datefmt=date_format)

    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Clear any existing handlers
    root_logger.handlers = []

    # File handler - rotates daily, keeps 30 days
    log_file = log_dir / f"app_{datetime.now().strftime('%Y-%m-%d')}.log"
    file_handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8"
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # Reduce noise from other libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)

    return logging.getLogger("openradiomics")

# Initialize logging
logger = setup_logging()

app = Flask(__name__)

# Secret key for Flask sessions (set via environment variable in production)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# Max upload size: 2GB (DICOM studies can be very large)
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024

# Import auth module
from auth import (
    get_current_user,
    is_authenticated,
    is_public_route,
    requires_auth,
    requires_role,
    log_audit,
    get_cognito_login_url,
    get_cognito_logout_url,
    exchange_code_for_tokens,
    refresh_tokens,
    ensure_user_in_db,
    validate_registration_data,
    validate_registration_data_step1,
    validate_password_strength,
    create_cognito_user,
    confirm_user_signup,
    resend_confirmation_code,
    normalize_phone,
    generate_verification_code,
    send_verification_email,
    send_verification_sms,
    create_verified_cognito_user,
    check_user_exists,
    initiate_password_reset,
    confirm_password_reset,
    authenticate_user,
    respond_to_mfa_challenge,
    create_password_reset_token,
    validate_reset_token,
    mark_reset_token_used,
    admin_set_user_password,
    send_password_reset_link,
    is_device_trusted,
    trust_device,
    generate_device_token,
    get_device_token_from_request,
    DEVICE_TOKEN_COOKIE_NAME,
    TRUSTED_DEVICE_EXPIRY_DAYS,
    COGNITO_CLIENT_ID,
    COGNITO_DOMAIN,
)

# Configuration
BASE_DIR = Path(__file__).parent

# Database configuration (PostgreSQL on RDS)
DATABASE_URL = os.environ.get("DATABASE_URL")
DB_HOST = os.environ.get("DB_HOST", "openradiomics-dev-db.cwznsagquzwy.us-east-1.rds.amazonaws.com")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "openradiomics")
DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")

# S3 configuration (set via environment variables)
S3_BUCKET_NAME = os.environ.get("S3_BUCKET_NAME")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
USE_S3 = bool(S3_BUCKET_NAME)

# Initialize S3 client if configured
s3_client = None
if USE_S3:
    import boto3
    s3_client = boto3.client("s3", region_name=AWS_REGION)
    logger.info(f"S3 storage enabled: {S3_BUCKET_NAME}")
else:
    logger.info(f"Local storage mode. Database: {DB_HOST}:{DB_PORT}/{DB_NAME}")

# Volume cache for orthogonal slicing
volume_cache = {}

# Local cache directory for S3 files
CACHE_DIR = Path(tempfile.gettempdir()) / "dicom_cache"
CACHE_DIR.mkdir(exist_ok=True)

# Auth configuration
# Always enable auth - redirects to login page even if Cognito isn't configured yet
AUTH_ENABLED = True


def get_callback_url():
    """Get the OAuth callback URL based on the request."""
    # Use X-Forwarded headers if behind a proxy/load balancer
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{scheme}://{host}/auth/callback"


def get_base_url():
    """Get the base URL for redirects."""
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{scheme}://{host}"


# ============================================================================
# AUTHENTICATION MIDDLEWARE
# ============================================================================

@app.before_request
def check_authentication():
    """Check authentication before each request."""
    # Skip auth check if Cognito is not configured
    if not AUTH_ENABLED:
        return None

    # Allow public routes
    if is_public_route(request.path):
        return None

    # Allow static files
    if request.path.startswith("/static/"):
        return None

    # Check if authenticated
    if not is_authenticated():
        if request.path.startswith("/api/"):
            return jsonify({"error": "Authentication required", "code": "AUTH_REQUIRED"}), 401
        return redirect(url_for("login"))

    return None


# ============================================================================
# AUTHENTICATION ROUTES
# ============================================================================

@app.route("/login")
def login():
    """Render the login page."""
    # If already authenticated, redirect to home
    if is_authenticated():
        return redirect(url_for("index"))

    # Pass Cognito config to template
    return render_template(
        "login.html",
        cognito_domain=COGNITO_DOMAIN,
        client_id=COGNITO_CLIENT_ID,
        callback_url=get_callback_url(),
    )


@app.route("/auth/login", methods=["POST"])
def auth_login():
    """
    Handle user login form submission.
    Authenticates directly with Cognito using USER_PASSWORD_AUTH flow.
    Supports trusted device feature to skip MFA on recognized devices.
    """
    logger.info("=" * 60)
    logger.info("LOGIN: Request received")
    logger.info(f"LOGIN: Method: {request.method}")
    logger.info(f"LOGIN: Headers: X-Requested-With={request.headers.get('X-Requested-With')}")

    # Check if this is an AJAX request
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    logger.info(f"LOGIN: Is AJAX: {is_ajax}")

    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    mfa_code = request.form.get("mfa_code", "").strip()
    mfa_session = request.form.get("mfa_session", "")
    trust_device_checked = request.form.get("trust_device") == "on"

    logger.info(f"LOGIN: Email: {email}")
    logger.info(f"LOGIN: Password provided: {bool(password)}")
    logger.info(f"LOGIN: MFA code provided: {bool(mfa_code)}")
    logger.info(f"LOGIN: MFA session provided: {bool(mfa_session)}")
    logger.info(f"LOGIN: Trust device checked: {trust_device_checked}")

    # If MFA session is provided, this is an MFA challenge response
    if mfa_session and mfa_code:
        logger.info("LOGIN: Processing MFA challenge response")
        success, result, error = respond_to_mfa_challenge(
            email=email,
            session=mfa_session,
            mfa_code=mfa_code,
            challenge_name="SOFTWARE_TOKEN_MFA"  # Default to TOTP
        )
        logger.info(f"LOGIN: MFA result - success={success}, error={error}")

        if not success:
            logger.warning(f"LOGIN: MFA failed for {email}: {error}")
            if is_ajax:
                return jsonify({"success": False, "error": error}), 400
            return render_template(
                "login.html",
                error=error,
                cognito_domain=COGNITO_DOMAIN,
                client_id=COGNITO_CLIENT_ID,
                callback_url=get_callback_url(),
            )

        # Success - set cookies and redirect
        tokens = result
        logger.info(f"LOGIN: MFA success, setting cookies for {email}")
        response = make_response(jsonify({
            "success": True,
            "redirectUrl": url_for("index")
        })) if is_ajax else make_response(redirect(url_for("index")))

        _set_auth_cookies(response, tokens)

        # If user checked "Trust this device", store the device trust
        if trust_device_checked:
            _handle_trust_device(response, email)

        logger.info(f"LOGIN: User authenticated with MFA: {email}")
        return response

    # Regular login (email + password)
    if not email:
        error_msg = "Email address is required."
        logger.warning(f"LOGIN: Missing email")
        if is_ajax:
            return jsonify({"success": False, "error": error_msg}), 400
        return render_template(
            "login.html",
            error=error_msg,
            cognito_domain=COGNITO_DOMAIN,
            client_id=COGNITO_CLIENT_ID,
            callback_url=get_callback_url(),
        )

    if not password:
        error_msg = "Password is required."
        logger.warning(f"LOGIN: Missing password for {email}")
        if is_ajax:
            return jsonify({"success": False, "error": error_msg}), 400
        return render_template(
            "login.html",
            error=error_msg,
            cognito_domain=COGNITO_DOMAIN,
            client_id=COGNITO_CLIENT_ID,
            callback_url=get_callback_url(),
        )

    # Authenticate with Cognito
    logger.info(f"LOGIN: Calling authenticate_user for {email}")
    success, result, error = authenticate_user(email, password)
    logger.info(f"LOGIN: Auth result - success={success}, error={error}")
    logger.info(f"LOGIN: Auth result keys: {result.keys() if isinstance(result, dict) else 'N/A'}")

    if not success:
        logger.warning(f"LOGIN: Authentication failed for {email}: {error}")
        if is_ajax:
            return jsonify({"success": False, "error": error}), 400
        return render_template(
            "login.html",
            error=error,
            cognito_domain=COGNITO_DOMAIN,
            client_id=COGNITO_CLIENT_ID,
            callback_url=get_callback_url(),
        )

    # Check if MFA is required
    if result.get("mfaRequired"):
        logger.info(f"LOGIN: MFA required for {email}, challenge={result.get('challengeName')}")

        # Check if device is already trusted - if so, skip MFA
        device_token = get_device_token_from_request()
        if device_token:
            try:
                conn = get_db_connection()
                if is_device_trusted(conn, device_token, email):
                    logger.info(f"LOGIN: Trusted device found for {email}, bypassing MFA")
                    conn.close()

                    # We need to complete the MFA challenge differently for trusted devices
                    # Since Cognito still requires MFA, we can't truly skip it at the API level
                    # But we can prompt the user that this is a trusted device
                    # For now, we still require MFA but could use device remembering in Cognito
                    # Return that MFA is required but indicate device is recognized
                    if is_ajax:
                        return jsonify({
                            "success": True,
                            "mfaRequired": True,
                            "session": result.get("session"),
                            "challengeName": result.get("challengeName"),
                            "trustedDevice": True,
                            "message": "Trusted device recognized. MFA still required by security policy."
                        })
                conn.close()
            except Exception as e:
                logger.error(f"LOGIN: Error checking trusted device: {e}")

        if is_ajax:
            return jsonify({
                "success": True,
                "mfaRequired": True,
                "session": result.get("session"),
                "challengeName": result.get("challengeName")
            })
        # For non-AJAX, we'd need to render an MFA form
        # but the JS handles this client-side
        return render_template(
            "login.html",
            error="MFA is required. Please use a modern browser.",
            cognito_domain=COGNITO_DOMAIN,
            client_id=COGNITO_CLIENT_ID,
            callback_url=get_callback_url(),
        )

    # Success - set cookies and redirect
    tokens = result.get("tokens", {})
    logger.info(f"LOGIN: Success! Setting cookies. Token keys: {tokens.keys() if tokens else 'None'}")
    response = make_response(jsonify({
        "success": True,
        "redirectUrl": url_for("index")
    })) if is_ajax else make_response(redirect(url_for("index")))

    _set_auth_cookies(response, tokens)
    logger.info(f"LOGIN: User authenticated successfully: {email}")
    logger.info("=" * 60)
    return response


def _handle_trust_device(response, email: str):
    """
    Handle the "Trust this device" checkbox by storing device trust
    and setting the device token cookie.

    Args:
        response: Flask response object to set cookie on
        email: User's email address
    """
    try:
        # Get or create device token
        device_token = get_device_token_from_request()
        if not device_token:
            device_token = generate_device_token()

        # Get request metadata
        user_agent = request.headers.get("User-Agent", "")
        ip_address = request.headers.get("X-Forwarded-For", request.remote_addr)
        if ip_address and "," in ip_address:
            ip_address = ip_address.split(",")[0].strip()

        # Store trusted device in database
        conn = get_db_connection()
        success, error = trust_device(
            conn,
            email=email,
            device_token=device_token,
            user_agent=user_agent,
            ip_address=ip_address
        )
        conn.close()

        if success:
            # Set device token cookie (90 days)
            response.set_cookie(
                DEVICE_TOKEN_COOKIE_NAME,
                device_token,
                httponly=True,
                secure=request.scheme == "https",
                samesite="Lax",
                max_age=TRUSTED_DEVICE_EXPIRY_DAYS * 24 * 60 * 60,  # 90 days in seconds
            )
            logger.info(f"LOGIN: Device trusted and cookie set for {email}")
        else:
            logger.warning(f"LOGIN: Failed to trust device for {email}: {error}")

    except Exception as e:
        logger.error(f"LOGIN: Error handling trust device: {e}")


def _set_auth_cookies(response, tokens):
    """Helper to set authentication cookies on a response."""
    # Access token cookie (short-lived, used for API calls)
    response.set_cookie(
        "access_token",
        tokens.get("access_token", ""),
        httponly=True,
        secure=request.scheme == "https",
        samesite="Lax",
        max_age=900,  # 15 minutes
    )

    # ID token cookie (contains user info)
    response.set_cookie(
        "id_token",
        tokens.get("id_token", ""),
        httponly=True,
        secure=request.scheme == "https",
        samesite="Lax",
        max_age=900,
    )

    # Refresh token cookie (longer-lived, used to get new access tokens)
    if tokens.get("refresh_token"):
        response.set_cookie(
            "refresh_token",
            tokens.get("refresh_token", ""),
            httponly=True,
            secure=request.scheme == "https",
            samesite="Lax",
            max_age=28800,  # 8 hours
        )


@app.route("/register")
def register():
    """Render the registration page."""
    # If already authenticated, redirect to home
    if is_authenticated():
        return redirect(url_for("index"))

    return render_template("register.html")


@app.route("/auth/register", methods=["POST"])
def auth_register():
    """
    Handle user registration form submission - Step 1 (no password).

    Collects basic info and sends verification code. User is NOT created in
    Cognito yet - that happens after verification + password setup in Step 3.
    """
    from datetime import datetime, timedelta

    # Check if this is an AJAX request
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    # Collect form data (NO password in Step 1)
    form_data = {
        "first_name": request.form.get("first_name", "").strip(),
        "last_name": request.form.get("last_name", "").strip(),
        "email": request.form.get("email", "").strip(),
        "phone": request.form.get("phone", "").strip(),  # Optional
        "street_address": request.form.get("street_address", "").strip(),  # Optional
        "city": request.form.get("city", "").strip(),  # Optional
        "state": request.form.get("state", "").strip(),  # Optional
        "postal_code": request.form.get("postal_code", "").strip(),  # Optional
        "country": request.form.get("country", "").strip(),  # Optional
        "terms_accepted": request.form.get("terms_accepted") == "on",
        "hipaa_acknowledged": request.form.get("hipaa_acknowledged") == "on",
    }

    # Validate Step 1 form data (no password validation)
    is_valid, errors = validate_registration_data_step1(form_data)

    if not is_valid:
        error_message = ". ".join(errors.values())
        if is_ajax:
            return jsonify({"success": False, "error": error_message}), 400
        return render_template(
            "register.html",
            error=error_message,
            form_data=form_data,
        )

    # Determine primary contact method (prefer email)
    email = form_data.get("email") or None
    phone = form_data.get("phone") or None
    primary_contact = "email" if email else "phone"

    if not email and not phone:
        error_msg = "Email or phone number is required"
        if is_ajax:
            return jsonify({"success": False, "error": error_msg}), 400
        return render_template("register.html", error=error_msg, form_data=form_data)

    # Generate verification code
    code = generate_verification_code()
    code_expiry = datetime.utcnow() + timedelta(minutes=10)

    # Try to send verification code (but don't block progress if it fails)
    send_error = None
    if primary_contact == "email" and email:
        success, error_msg = send_verification_email(email, code)
        if not success:
            send_error = error_msg or "Failed to send verification email. Click 'Resend' to try again."
        masked_contact = mask_email(email)
    else:
        success, error_msg = send_verification_sms(phone, code)
        if not success:
            send_error = error_msg or "Failed to send verification SMS. Click 'Resend' to try again."
        masked_contact = mask_phone(phone)

    # Store in session for verification step (always, even if send failed)
    session["pending_registration"] = {
        "form_data": form_data,
        "verification_code": code,
        "code_expiry": code_expiry.isoformat(),
        "primary_contact": primary_contact,
        "contact_value": email if primary_contact == "email" else phone,
    }

    # Always progress to Step 2, but include any send error so user knows to resend
    if is_ajax:
        return jsonify({
            "success": True,
            "requiresVerification": True,
            "maskedContact": masked_contact,
            "maskedEmail": masked_contact,  # For backwards compatibility
            "sendError": send_error,  # Will be displayed on Step 2 if present
        })

    # For non-AJAX fallback
    return render_template(
        "register.html",
        success="Verification code sent! Please check your email/phone." if not send_error else None,
        error=send_error,
        show_verification=True,
        masked_email=masked_contact,
    )


def mask_email(email: str) -> str:
    """Mask email for display (j***@example.com)."""
    email_parts = email.split("@")
    if len(email_parts) == 2:
        local = email_parts[0]
        domain = email_parts[1]
        if len(local) > 1:
            return local[0] + "***@" + domain
        return "***@" + domain
    return "***"


def mask_phone(phone: str) -> str:
    """Mask phone for display (***-***-1234)."""
    # Get last 4 digits
    digits = ''.join(c for c in phone if c.isdigit())
    if len(digits) >= 4:
        return "***-***-" + digits[-4:]
    return "***"


@app.route("/auth/verify", methods=["POST"])
def auth_verify():
    """
    Verify user's email/phone with the confirmation code - Step 2.

    Uses session-based verification (not Cognito) since user hasn't been
    created in Cognito yet.
    """
    from datetime import datetime

    # Check if this is an AJAX request
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    # Get verification code from form
    code = request.form.get("code", "").strip()

    # Get pending registration from session
    pending = session.get("pending_registration")

    if not pending:
        error_msg = "Session expired. Please register again."
        if is_ajax:
            return jsonify({"success": False, "error": error_msg, "sessionExpired": True}), 400
        return render_template("register.html", error=error_msg)

    if not code:
        error_msg = "Please enter the verification code."
        if is_ajax:
            return jsonify({"success": False, "error": error_msg}), 400
        return render_template("register.html", error=error_msg)

    # Validate code format (6 digits)
    if not code.isdigit() or len(code) != 6:
        error_msg = "Verification code must be 6 digits."
        if is_ajax:
            return jsonify({"success": False, "error": error_msg}), 400
        return render_template("register.html", error=error_msg)

    # Check code expiry
    code_expiry = datetime.fromisoformat(pending.get("code_expiry", ""))
    if datetime.utcnow() > code_expiry:
        error_msg = "Verification code has expired. Please request a new code."
        if is_ajax:
            return jsonify({"success": False, "error": error_msg}), 400
        return render_template("register.html", error=error_msg)

    # Verify the code
    stored_code = pending.get("verification_code")
    if code != stored_code:
        error_msg = "Invalid verification code. Please check and try again."
        if is_ajax:
            return jsonify({"success": False, "error": error_msg}), 400
        return render_template("register.html", error=error_msg)

    # Mark as verified in session (keep data for Step 3)
    pending["verified"] = True
    session["pending_registration"] = pending

    if is_ajax:
        return jsonify({
            "success": True,
            "requiresPassword": True,  # Tell frontend to show Step 3
            "message": "Verification successful! Now set your password.",
        })

    # For non-AJAX fallback, show step 3 message
    return render_template(
        "register.html",
        success="Verification successful! Now set your password.",
        show_password_step=True,
    )


@app.route("/auth/set-password", methods=["POST"])
def auth_set_password():
    """
    Set password and complete registration - Step 3.

    Creates the user in Cognito with verified email and the chosen password.
    """
    # Check if this is an AJAX request
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    # Get password from form
    password = request.form.get("password", "")

    # Get pending registration from session
    pending = session.get("pending_registration")

    if not pending or not pending.get("verified"):
        error_msg = "Session expired or not verified. Please register again."
        if is_ajax:
            return jsonify({"success": False, "error": error_msg, "sessionExpired": True}), 400
        return render_template("register.html", error=error_msg)

    # Validate password
    if not password:
        error_msg = "Password is required."
        if is_ajax:
            return jsonify({"success": False, "error": error_msg}), 400
        return render_template("register.html", error=error_msg)

    is_strong, pw_errors = validate_password_strength(password)
    if not is_strong:
        error_msg = pw_errors[0] if pw_errors else "Password does not meet requirements"
        if is_ajax:
            return jsonify({"success": False, "error": error_msg}), 400
        return render_template("register.html", error=error_msg)

    # Get form data from session
    form_data = pending.get("form_data", {})
    email = form_data.get("email")
    phone = form_data.get("phone")

    # Create user in Cognito with verified email
    success, user_sub, error_message = create_verified_cognito_user(
        username=email,
        password=password,
        email=email,
        phone=phone if phone else None,
        first_name=form_data.get("first_name"),
        last_name=form_data.get("last_name"),
    )

    if not success:
        if is_ajax:
            return jsonify({"success": False, "error": error_message}), 400
        return render_template("register.html", error=error_message, form_data=form_data)

    # Store user profile in local database
    try:
        # Add password placeholder for store_user_profile (not stored, just for validation)
        form_data["password"] = "[redacted]"
        store_user_profile(user_sub, form_data)
    except Exception as e:
        logger.warning(f"Failed to store user profile in database: {e}")
        # Continue anyway - user was created in Cognito

    # Clear the session
    session.pop("pending_registration", None)

    if is_ajax:
        return jsonify({
            "success": True,
            "message": "Registration complete! You can now sign in.",
            "redirectUrl": url_for("login"),
        })

    # For non-AJAX, redirect to login with success message
    return render_template(
        "login.html",
        success="Registration complete! You can now sign in and set up MFA.",
        cognito_domain=COGNITO_DOMAIN,
        client_id=COGNITO_CLIENT_ID,
        callback_url=get_callback_url(),
    )


@app.route("/auth/resend-code", methods=["POST"])
def auth_resend_code():
    """Resend the verification code to user's email/phone."""
    from datetime import datetime, timedelta

    # Check if this is an AJAX request
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    # Get pending registration from session
    pending = session.get("pending_registration")

    if not pending:
        error_msg = "Session expired. Please register again."
        if is_ajax:
            return jsonify({"success": False, "error": error_msg, "sessionExpired": True}), 400
        return render_template("register.html", error=error_msg)

    # Generate new verification code
    code = generate_verification_code()
    code_expiry = datetime.utcnow() + timedelta(minutes=10)

    # Determine contact method
    primary_contact = pending.get("primary_contact", "email")
    contact_value = pending.get("contact_value")

    if not contact_value:
        error_msg = "No contact information found. Please register again."
        if is_ajax:
            return jsonify({"success": False, "error": error_msg, "sessionExpired": True}), 400
        return render_template("register.html", error=error_msg)

    # Send verification code
    if primary_contact == "email":
        success, error_msg = send_verification_email(contact_value, code)
    else:
        success, error_msg = send_verification_sms(contact_value, code)

    if not success:
        if is_ajax:
            return jsonify({"success": False, "error": error_msg or "Failed to send verification code"}), 400
        return render_template("register.html", error=error_msg or "Failed to send verification code")

    # Update session with new code
    pending["verification_code"] = code
    pending["code_expiry"] = code_expiry.isoformat()
    session["pending_registration"] = pending

    if is_ajax:
        return jsonify({
            "success": True,
            "message": f"A new verification code has been sent to your {primary_contact}.",
        })

    return render_template(
        "register.html",
        success=f"A new verification code has been sent to your {primary_contact}.",
        show_verification=True,
    )


def store_user_profile(cognito_sub: str, form_data: dict) -> bool:
    """
    Store user profile data in the PostgreSQL database.

    This stores additional HIPAA-required information that isn't stored in Cognito.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO users (
                cognito_sub, email, display_name, role,
                first_name, last_name, phone,
                street_address, city, state, postal_code, country,
                terms_accepted_at, hipaa_acknowledged_at,
                created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        """, (
            cognito_sub,
            form_data.get("email"),
            f"{form_data['first_name']} {form_data['last_name']}",
            "viewer",  # Default role
            form_data["first_name"],
            form_data["last_name"],
            form_data.get("phone"),  # Optional
            form_data.get("street_address", ""),  # Optional
            form_data.get("city", ""),  # Optional
            form_data.get("state", ""),  # Optional
            form_data.get("postal_code", ""),  # Optional
            form_data.get("country", ""),  # Optional
            datetime.utcnow() if form_data.get("terms_accepted") else None,
            datetime.utcnow() if form_data.get("hipaa_acknowledged") else None,
        ))

        conn.commit()
        conn.close()
        logger.info(f"REGISTRATION: User profile stored for {form_data.get('email')}")
        return True

    except Exception as e:
        logger.error(f"Error storing user profile: {e}")
        return False


@app.route("/auth/callback")
def auth_callback():
    """Handle OAuth callback from Cognito."""
    code = request.args.get("code")
    error = request.args.get("error")
    error_description = request.args.get("error_description")

    if error:
        return render_template(
            "login.html",
            error=error_description or error,
            cognito_domain=COGNITO_DOMAIN,
            client_id=COGNITO_CLIENT_ID,
            callback_url=get_callback_url(),
        )

    if not code:
        return redirect(url_for("login"))

    # Exchange code for tokens
    tokens = exchange_code_for_tokens(code, get_callback_url())
    if not tokens:
        return render_template(
            "login.html",
            error="Failed to authenticate. Please try again.",
            cognito_domain=COGNITO_DOMAIN,
            client_id=COGNITO_CLIENT_ID,
            callback_url=get_callback_url(),
        )

    # Set cookies with tokens
    response = make_response(redirect(url_for("index")))

    # Access token cookie (short-lived, used for API calls)
    response.set_cookie(
        "access_token",
        tokens.get("access_token", ""),
        httponly=True,
        secure=request.scheme == "https",
        samesite="Lax",
        max_age=900,  # 15 minutes
    )

    # ID token cookie (contains user info)
    response.set_cookie(
        "id_token",
        tokens.get("id_token", ""),
        httponly=True,
        secure=request.scheme == "https",
        samesite="Lax",
        max_age=900,
    )

    # Refresh token cookie (longer-lived, used to get new access tokens)
    response.set_cookie(
        "refresh_token",
        tokens.get("refresh_token", ""),
        httponly=True,
        secure=request.scheme == "https",
        samesite="Lax",
        max_age=28800,  # 8 hours
    )

    # Log the login event
    logger.info("LOGIN: User authenticated successfully")

    return response


@app.route("/auth/logout")
def auth_logout():
    """Log out the user."""
    # Clear cookies (but keep device_token - it should persist for trusted device feature)
    response = make_response(redirect(url_for("login")))
    response.delete_cookie("access_token")
    response.delete_cookie("id_token")
    response.delete_cookie("refresh_token")
    # Note: We intentionally do NOT delete the device_token cookie
    # This allows the trusted device feature to work on next login

    # If Cognito is configured, redirect to Cognito logout
    if COGNITO_DOMAIN:
        logout_url = get_cognito_logout_url(f"{get_base_url()}/login")
        response = make_response(redirect(logout_url))
        response.delete_cookie("access_token")
        response.delete_cookie("id_token")
        response.delete_cookie("refresh_token")

    return response


@app.route("/auth/refresh", methods=["POST"])
def auth_refresh():
    """Refresh the access token using the refresh token."""
    refresh_token_value = request.cookies.get("refresh_token")

    if not refresh_token_value:
        return jsonify({"error": "No refresh token"}), 401

    tokens = refresh_tokens(refresh_token_value)
    if not tokens:
        return jsonify({"error": "Token refresh failed"}), 401

    response = make_response(jsonify({"success": True}))

    # Update access token cookie
    response.set_cookie(
        "access_token",
        tokens.get("access_token", ""),
        httponly=True,
        secure=request.scheme == "https",
        samesite="Lax",
        max_age=900,
    )

    # Update ID token if provided
    if tokens.get("id_token"):
        response.set_cookie(
            "id_token",
            tokens.get("id_token", ""),
            httponly=True,
            secure=request.scheme == "https",
            samesite="Lax",
            max_age=900,
        )

    return response


@app.route("/api/auth/user")
def get_user_info():
    """Get the current authenticated user's information."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401

    return jsonify({
        "email": user.get("email"),
        "name": user.get("name"),
        "role": user.get("role"),
        "groups": user.get("groups", []),
    })


@app.route("/api/user/profile")
@requires_auth
def get_user_profile():
    """
    Get the current user's profile including user_type.

    Returns user profile data from the database, including whether they've
    selected their account type (patient/provider).
    """
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """SELECT user_type, first_name, last_name, role, email
               FROM users WHERE cognito_sub = %s""",
            (user.get("sub"),)
        )
        row = cursor.fetchone()
        conn.close()

        if row:
            return jsonify({
                "user_type": row["user_type"],
                "first_name": row["first_name"],
                "last_name": row["last_name"],
                "role": row["role"],
                "email": row["email"],
            })
        else:
            # User exists in Cognito but not yet in local DB
            return jsonify({
                "user_type": None,
                "first_name": user.get("given_name"),
                "last_name": user.get("family_name"),
                "role": "viewer",
                "email": user.get("email"),
            })

    except Exception as e:
        logger.error(f"Error fetching user profile: {e}")
        return jsonify({"error": "Failed to fetch profile"}), 500


@app.route("/api/user/type", methods=["POST"])
@requires_auth
def set_user_type():
    """
    Set the user's account type (patient or provider).

    This is typically called when a new user first logs in and is prompted
    to identify themselves. The user_type affects their UI/UX experience.
    """
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400

    user_type = data.get("user_type")
    if user_type not in ("patient", "provider", "organization"):
        return jsonify({"error": "Invalid user type. Must be 'patient', 'provider', or 'organization'."}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Try to update existing user
        cursor.execute(
            """UPDATE users SET user_type = %s, updated_at = NOW()
               WHERE cognito_sub = %s
               RETURNING id""",
            (user_type, user.get("sub"))
        )
        result = cursor.fetchone()

        if not result:
            # User doesn't exist in local DB yet - create them
            cursor.execute(
                """INSERT INTO users (cognito_sub, email, user_type, role, first_name, last_name, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, NOW())""",
                (
                    user.get("sub"),
                    user.get("email"),
                    user_type,
                    "viewer",
                    user.get("given_name"),
                    user.get("family_name"),
                )
            )

        conn.commit()
        conn.close()

        logger.info(f"USER TYPE SET: {user.get('email')} -> {user_type}")

        return jsonify({
            "success": True,
            "user_type": user_type,
        })

    except Exception as e:
        logger.error(f"Error setting user type: {e}")
        return jsonify({"error": "Failed to update user type"}), 500


@app.route("/api/user/has-data")
@requires_auth
def check_user_has_data():
    """Check if the current user has any accessible studies."""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Get user's internal ID
        cursor.execute(
            "SELECT id FROM users WHERE cognito_sub = %s",
            (user.get("sub"),)
        )
        user_row = cursor.fetchone()

        if not user_row:
            conn.close()
            return jsonify({"has_data": False, "study_count": 0})

        # Check study_access table for any accessible studies
        cursor.execute(
            "SELECT COUNT(*) as count FROM study_access WHERE user_id = %s",
            (user_row["id"],)
        )
        result = cursor.fetchone()
        conn.close()

        count = result["count"] if result else 0
        return jsonify({
            "has_data": count > 0,
            "study_count": count
        })

    except Exception as e:
        logger.error(f"Error checking user data: {e}")
        return jsonify({"error": "Failed to check data"}), 500


@app.route("/api/upload/dicom", methods=["POST"])
@requires_auth
def upload_dicom():
    """
    Upload DICOM files to S3 and create database entries.
    Accepts multipart/form-data with 'files' field.
    Returns summary of uploaded studies.
    """
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401

    if not USE_S3:
        return jsonify({"error": "S3 storage not configured"}), 500

    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files provided"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Get user's internal ID
        cursor.execute(
            "SELECT id FROM users WHERE cognito_sub = %s",
            (user.get("sub"),)
        )
        user_row = cursor.fetchone()
        if not user_row:
            # Create user in local DB if not exists
            cursor.execute(
                """INSERT INTO users (cognito_sub, email, role, created_at)
                   VALUES (%s, %s, 'viewer', NOW()) RETURNING id""",
                (user.get("sub"), user.get("email"))
            )
            user_row = cursor.fetchone()
            conn.commit()

        user_id = user_row["id"]

        uploaded_count = 0
        errors = []
        studies_affected = set()

        for file in files:
            try:
                # Read file into memory
                file_bytes = file.read()
                file.seek(0)

                # Validate it's a DICOM file
                try:
                    dcm = pydicom.dcmread(io.BytesIO(file_bytes))
                except Exception as e:
                    errors.append({"file": file.filename, "error": f"Not a valid DICOM file: {str(e)}"})
                    continue

                # Extract minimal metadata
                patient_id = str(getattr(dcm, 'PatientID', 'UNKNOWN'))
                patient_name = str(getattr(dcm, 'PatientName', '')) or None
                study_uid = str(getattr(dcm, 'StudyInstanceUID', None))
                series_uid = str(getattr(dcm, 'SeriesInstanceUID', None))
                sop_uid = str(getattr(dcm, 'SOPInstanceUID', None))

                if not study_uid or not series_uid or not sop_uid:
                    errors.append({"file": file.filename, "error": "Missing required DICOM UIDs"})
                    continue

                # Generate S3 key
                s3_key = f"patients/{patient_id}/studies/{study_uid}/series/{series_uid}/{sop_uid}.dcm"

                # Upload to S3
                s3_client.put_object(
                    Bucket=S3_BUCKET_NAME,
                    Key=s3_key,
                    Body=file_bytes,
                    ContentType="application/dicom",
                    ServerSideEncryption="AES256"
                )

                # Create/update database records using upserts

                # Patient
                cursor.execute("""
                    INSERT INTO patients (patient_uid, name, created_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (patient_uid) DO UPDATE SET updated_at = NOW()
                    RETURNING id
                """, (patient_id, patient_name))
                patient_db_id = cursor.fetchone()["id"]

                # Study
                study_desc = str(getattr(dcm, 'StudyDescription', '')) or None
                study_date_str = str(getattr(dcm, 'StudyDate', '')) or None
                study_date = None
                if study_date_str and len(study_date_str) == 8:
                    try:
                        study_date = datetime.strptime(study_date_str, "%Y%m%d").date()
                    except ValueError:
                        pass

                cursor.execute("""
                    INSERT INTO studies (patient_id, study_uid, description, study_date, created_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (study_uid) DO UPDATE SET updated_at = NOW()
                    RETURNING id
                """, (patient_db_id, study_uid, study_desc, study_date))
                study_db_id = cursor.fetchone()["id"]
                studies_affected.add(study_db_id)

                # Series
                series_desc = str(getattr(dcm, 'SeriesDescription', '')) or None
                modality = str(getattr(dcm, 'Modality', 'OT')) or 'OT'

                cursor.execute("""
                    INSERT INTO series (study_id, series_uid, description, modality, created_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (series_uid) DO UPDATE SET updated_at = NOW()
                    RETURNING id
                """, (study_db_id, series_uid, series_desc, modality))
                series_db_id = cursor.fetchone()["id"]

                # Instance
                instance_number = getattr(dcm, 'InstanceNumber', None)
                if instance_number is not None:
                    instance_number = int(instance_number)

                cursor.execute("""
                    INSERT INTO instances (series_id, sop_uid, s3_key, instance_number, created_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (sop_uid) DO UPDATE SET s3_key = EXCLUDED.s3_key
                    RETURNING id
                """, (series_db_id, sop_uid, s3_key, instance_number))

                uploaded_count += 1
                logger.info(f"UPLOAD: {file.filename} -> {s3_key}")

            except Exception as e:
                logger.error(f"Error uploading {file.filename}: {e}")
                errors.append({"file": file.filename, "error": str(e)})
                continue

        # Grant user access to all affected studies
        for study_db_id in studies_affected:
            cursor.execute("""
                INSERT INTO study_access (user_id, study_id, access_level, is_owner, granted_at)
                VALUES (%s, %s, 'admin', TRUE, NOW())
                ON CONFLICT (user_id, study_id) DO NOTHING
            """, (user_id, study_db_id))

        conn.commit()

        logger.info(f"UPLOAD COMPLETE: {uploaded_count} files, {len(studies_affected)} studies by user {user.get('email')}")

        return jsonify({
            "success": True,
            "uploaded": uploaded_count,
            "studies": len(studies_affected),
            "errors": errors if errors else None
        })

    except Exception as e:
        conn.rollback()
        logger.error(f"Upload error: {e}")
        return jsonify({"error": f"Upload failed: {str(e)}"}), 500

    finally:
        conn.close()


@app.route("/auth/check-email", methods=["POST"])
def auth_check_email():
    """
    Check if an email already exists in the system.
    Returns user state and appropriate options.
    """
    email = request.form.get("email", "").strip()

    if not email:
        return jsonify({
            "success": False,
            "error": "Email is required"
        }), 400

    try:
        conn = get_db_connection()
        result = check_user_exists(conn, email)
        conn.close()

        return jsonify({
            "success": True,
            "exists": result["exists"],
            "state": result["state"],
            "message": result["message"],
            "options": result["options"]
        })

    except Exception as e:
        logger.error(f"Check email error: {e}")
        return jsonify({
            "success": False,
            "error": "Unable to check email status"
        }), 500


@app.route("/auth/forgot-password", methods=["GET", "POST"])
def auth_forgot_password():
    """Handle forgot password request - send reset link."""
    # Check if this is an AJAX request
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    if request.method == "GET":
        return render_template("forgot_password.html")

    # POST: Generate token and send reset link
    email = request.form.get("email", "").strip()

    if not email:
        error_msg = "Email address is required."
        if is_ajax:
            return jsonify({"success": False, "error": error_msg}), 400
        return render_template("forgot_password.html", error=error_msg)

    # Create reset token in database
    try:
        conn = get_db_connection()
        success, token, error = create_password_reset_token(conn, email)
        conn.close()

        if success and token:
            # Build reset URL
            reset_url = f"{get_base_url()}/auth/reset-password?token={token}"

            # Send email with reset link
            email_success, email_error = send_password_reset_link(email, reset_url)

            if not email_success:
                logger.warning(f"Failed to send reset email to {email}: {email_error}")
                # Still show success to avoid leaking user existence info

        # Always show success message (security: don't reveal if email exists)
        success_msg = "If an account exists with this email, you will receive a password reset link."

        if is_ajax:
            return jsonify({
                "success": True,
                "message": success_msg
            })
        return render_template("forgot_password.html", success=success_msg)

    except Exception as e:
        logger.error(f"Password reset error: {e}")
        error_msg = "An error occurred. Please try again."
        if is_ajax:
            return jsonify({"success": False, "error": error_msg}), 500
        return render_template("forgot_password.html", error=error_msg, email=email)


@app.route("/auth/reset-password", methods=["GET", "POST"])
def auth_reset_password():
    """Handle password reset - validate token and set new password."""
    # Check if this is an AJAX request
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    if request.method == "GET":
        # Get token from URL parameter
        token = request.args.get("token", "").strip()

        if not token:
            # No token provided, redirect to forgot password
            return redirect(url_for("auth_forgot_password"))

        # Validate token
        try:
            conn = get_db_connection()
            is_valid, email, error = validate_reset_token(conn, token)
            conn.close()

            if not is_valid:
                return render_template("reset_password.html", token_error=error)

            return render_template(
                "reset_password.html",
                token=token,
                email=email,
                masked_email=mask_email(email)
            )

        except Exception as e:
            logger.error(f"Token validation error: {e}")
            return render_template("reset_password.html", token_error="An error occurred. Please try again.")

    # POST: Reset password
    token = request.form.get("token", "").strip()
    new_password = request.form.get("password", "")

    if not token:
        error_msg = "Reset token is missing. Please request a new password reset link."
        if is_ajax:
            return jsonify({"success": False, "error": error_msg, "tokenExpired": True}), 400
        return render_template("reset_password.html", token_error=error_msg)

    if not new_password:
        error_msg = "New password is required."
        if is_ajax:
            return jsonify({"success": False, "error": error_msg}), 400
        return render_template("reset_password.html", error=error_msg, token=token)

    try:
        conn = get_db_connection()

        # Validate token again
        is_valid, email, error = validate_reset_token(conn, token)

        if not is_valid:
            conn.close()
            if is_ajax:
                return jsonify({"success": False, "error": error, "tokenExpired": True}), 400
            return render_template("reset_password.html", token_error=error)

        # Set new password in Cognito
        success, error = admin_set_user_password(email, new_password)

        if not success:
            conn.close()
            if is_ajax:
                return jsonify({"success": False, "error": error}), 400
            return render_template(
                "reset_password.html",
                error=error,
                token=token,
                email=email,
                masked_email=mask_email(email)
            )

        # Mark token as used
        mark_reset_token_used(conn, token)
        conn.close()

        logger.info(f"PASSWORD RESET: Successfully reset password for {email}")

        if is_ajax:
            return jsonify({
                "success": True,
                "message": "Password reset successfully! You can now sign in.",
                "redirectUrl": url_for("login")
            })
        return render_template(
            "login.html",
            success="Password reset successfully! You can now sign in.",
            cognito_domain=COGNITO_DOMAIN,
            client_id=COGNITO_CLIENT_ID,
            callback_url=get_callback_url(),
        )

    except Exception as e:
        logger.error(f"Password reset error: {e}")
        error_msg = "An error occurred. Please try again."
        if is_ajax:
            return jsonify({"success": False, "error": error_msg}), 500
        return render_template("reset_password.html", error=error_msg, token=token)


def get_db_connection():
    """Get PostgreSQL database connection."""
    import psycopg2
    from psycopg2.extras import RealDictCursor

    if DATABASE_URL:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    else:
        conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            cursor_factory=RealDictCursor
        )
    return conn


def load_dicom_file(file_path_or_s3_key: str) -> pydicom.Dataset:
    """
    Load a DICOM file from local filesystem or S3.
    Uses local cache for S3 files to avoid repeated downloads.
    """
    if USE_S3:
        # Check local cache first
        cache_path = CACHE_DIR / file_path_or_s3_key.replace("/", "_")

        if cache_path.exists():
            return pydicom.dcmread(cache_path)

        # Download from S3
        try:
            response = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=file_path_or_s3_key)
            dicom_bytes = response["Body"].read()

            # Cache locally
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(dicom_bytes)

            return pydicom.dcmread(io.BytesIO(dicom_bytes))
        except Exception as e:
            raise RuntimeError(f"Failed to load from S3: {file_path_or_s3_key} - {e}")
    else:
        # Load from local filesystem
        return pydicom.dcmread(file_path_or_s3_key)


def get_file_path_column():
    """Return the column name for file paths (file_path for local, s3_key for cloud)."""
    return "s3_key" if USE_S3 else "file_path"


def load_volume_cached(study_id: str, series_name: str):
    """Load and cache a 3D volume for a study/series."""
    cache_key = f"{study_id}:{series_name}"

    if cache_key in volume_cache:
        return volume_cache[cache_key]

    conn = get_db_connection()
    cursor = conn.cursor()

    file_col = get_file_path_column()

    cursor.execute(f"""
        SELECT {file_col} as file_path, InstanceNumber, ImagePositionPatient, PixelSpacing, SliceThickness
        FROM dicom_files
        WHERE StudyInstanceUID = %s AND SeriesDescription = %s
        ORDER BY InstanceNumber
    """, (study_id, series_name))

    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return None

    # Get spacing info
    pixel_spacing = [1.0, 1.0]
    slice_thickness = 1.0

    if rows[0]["PixelSpacing"]:
        try:
            pixel_spacing = json.loads(rows[0]["PixelSpacing"])
        except:
            pass

    if rows[0]["SliceThickness"]:
        slice_thickness = float(rows[0]["SliceThickness"])

    # Load all slices
    slices = []
    raw_slices = []
    for row in rows:
        try:
            dcm = load_dicom_file(row["file_path"])
            pixel_array = dcm.pixel_array.astype(np.float32)
            raw_slices.append(pixel_array.copy())
            slices.append(pixel_array)
        except Exception as e:
            print(f"Error loading {row['file_path']}: {e}")
            continue

    if not slices:
        return None

    # Stack into 3D volume (z, y, x)
    volume = np.stack(slices, axis=0)
    raw_volume = np.stack(raw_slices, axis=0)

    # Normalize
    vmin, vmax = volume.min(), volume.max()
    if vmax > vmin:
        normalized = (volume - vmin) / (vmax - vmin)
    else:
        normalized = np.zeros_like(volume)

    result = {
        "volume": normalized,
        "raw_volume": raw_volume,
        "dimensions": volume.shape,  # (z, y, x)
        "pixel_spacing": pixel_spacing,
        "slice_thickness": slice_thickness,
        "vmin": float(vmin),
        "vmax": float(vmax)
    }

    # Cache (limit to 3 volumes to avoid memory issues)
    if len(volume_cache) >= 3:
        oldest = next(iter(volume_cache))
        del volume_cache[oldest]
    volume_cache[cache_key] = result

    return result


@app.route("/")
def index():
    """Serve the main viewer page."""
    return render_template("viewer.html")


@app.route("/api/studies")
def get_studies():
    """Get list of available studies."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT StudyInstanceUID, StudyDescription, StudyDate,
               COUNT(DISTINCT SeriesDescription) as sequence_count,
               COUNT(*) as slice_count
        FROM dicom_files
        GROUP BY StudyInstanceUID
        ORDER BY StudyDate DESC
    """)
    studies = []
    for row in cursor.fetchall():
        date = row["StudyDate"]
        # Format date as YYYY-MM-DD
        if date and len(date) == 8:
            date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
        studies.append({
            "id": row["StudyInstanceUID"],
            "description": row["StudyDescription"],
            "date": date,
            "sequences": row["sequence_count"],
            "slices": row["slice_count"]
        })
    conn.close()
    return jsonify(studies)


@app.route("/api/sequences/<study_id>")
def get_sequences(study_id):
    """Get list of sequences for a specific study."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT SeriesDescription, COUNT(*) as slice_count
        FROM dicom_files
        WHERE StudyInstanceUID = %s
        GROUP BY SeriesDescription
        ORDER BY slice_count DESC
    """, (study_id,))
    sequences = [
        {"name": row["SeriesDescription"], "slices": row["slice_count"]}
        for row in cursor.fetchall()
    ]
    conn.close()
    return jsonify(sequences)


@app.route("/api/volume/<study_id>/<series_name>")
def get_volume(study_id, series_name):
    """
    Get voxel data for a specific MRI sequence within a study.
    Returns downsampled voxel positions and intensities.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    file_col = get_file_path_column()

    # Get all files for this series within the study, ordered by instance number
    cursor.execute(f"""
        SELECT {file_col} as file_path, InstanceNumber, ImagePositionPatient, PixelSpacing, SliceThickness
        FROM dicom_files
        WHERE StudyInstanceUID = %s AND SeriesDescription = %s
        ORDER BY InstanceNumber
    """, (study_id, series_name))

    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return jsonify({"error": "Series not found"}), 404

    # Get spacing info from first row
    pixel_spacing = [1.0, 1.0]
    slice_thickness = 1.0

    if rows[0]["PixelSpacing"]:
        try:
            pixel_spacing = json.loads(rows[0]["PixelSpacing"])
        except:
            pass

    if rows[0]["SliceThickness"]:
        slice_thickness = float(rows[0]["SliceThickness"])

    # Load DICOM slices and build volume
    slices = []
    for row in rows:
        try:
            dcm = load_dicom_file(row["file_path"])
            pixel_array = dcm.pixel_array.astype(np.float32)
            slices.append(pixel_array)
        except Exception as e:
            print(f"Error loading {row['file_path']}: {e}")
            continue

    if not slices:
        return jsonify({"error": "Could not load any slices"}), 500

    # Stack into 3D volume (z, y, x)
    volume = np.stack(slices, axis=0)

    # Get volume dimensions
    nz, ny, nx = volume.shape

    # Normalize intensity to 0-1 range
    vmin, vmax = volume.min(), volume.max()
    if vmax > vmin:
        volume = (volume - vmin) / (vmax - vmin)

    # Downsample for browser performance
    # Skip every Nth voxel, send ALL voxels (filtering done client-side)
    step = 4  # Sample every 4th voxel
    min_threshold = 0.05  # Only skip very dark background

    voxels = []
    for z in range(0, nz, step):
        for y in range(0, ny, step):
            for x in range(0, nx, step):
                intensity = float(volume[z, y, x])
                if intensity > min_threshold:
                    voxels.append({
                        "x": x - nx // 2,  # Center the volume
                        "y": y - ny // 2,
                        "z": z - nz // 2,
                        "i": round(intensity, 3)
                    })

    # Limit total voxels if still too many
    max_voxels = 150000
    if len(voxels) > max_voxels:
        # Sort by intensity and keep brightest
        voxels.sort(key=lambda v: v["i"], reverse=True)
        voxels = voxels[:max_voxels]

    return jsonify({
        "dimensions": [nx, ny, nz],
        "pixelSpacing": pixel_spacing,
        "sliceThickness": slice_thickness,
        "voxelCount": len(voxels),
        "step": step,
        "voxels": voxels
    })


@app.route("/api/slice/<study_id>/<series_name>/<int:slice_num>")
def get_slice(study_id, series_name, slice_num):
    """
    Get a single 2D slice as pixel intensity array.
    Returns normalized pixel values for canvas rendering.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    file_col = get_file_path_column()

    # Get all files for this series within the study, ordered by instance number
    cursor.execute(f"""
        SELECT {file_col} as file_path, InstanceNumber
        FROM dicom_files
        WHERE StudyInstanceUID = %s AND SeriesDescription = %s
        ORDER BY InstanceNumber
    """, (study_id, series_name))

    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return jsonify({"error": "Series not found"}), 404

    total_slices = len(rows)

    # Clamp slice number
    slice_num = max(0, min(slice_num, total_slices - 1))

    # Load the specific slice
    try:
        dcm = load_dicom_file(rows[slice_num]["file_path"])
        pixel_array = dcm.pixel_array.astype(np.float32)
    except Exception as e:
        return jsonify({"error": f"Could not load slice: {e}"}), 500

    ny, nx = pixel_array.shape

    # Normalize to 0-255 for canvas rendering
    vmin, vmax = pixel_array.min(), pixel_array.max()
    if vmax > vmin:
        normalized = ((pixel_array - vmin) / (vmax - vmin) * 255).astype(np.uint8)
    else:
        normalized = np.zeros_like(pixel_array, dtype=np.uint8)

    # Flatten to list for JSON
    pixels = normalized.flatten().tolist()

    return jsonify({
        "width": nx,
        "height": ny,
        "pixels": pixels,
        "sliceNum": slice_num,
        "totalSlices": total_slices
    })


@app.route("/api/orthoslice/<study_id>/<series_name>/<plane>/<int:index>")
def get_orthoslice(study_id, series_name, plane, index):
    """
    Get a 2D slice from any orthogonal plane.
    plane: 'axial' (Z), 'sagittal' (X), or 'coronal' (Y)
    For sagittal/coronal, scales Z axis to account for slice thickness vs pixel spacing.
    """
    vol_data = load_volume_cached(study_id, series_name)
    if vol_data is None:
        return jsonify({"error": "Volume not found"}), 404

    volume = vol_data["volume"]
    nz, ny, nx = vol_data["dimensions"]
    pixel_spacing = vol_data["pixel_spacing"]
    slice_thickness = vol_data["slice_thickness"]

    # Calculate Z scale factor (slice thickness / pixel spacing)
    z_scale = slice_thickness / pixel_spacing[0] if pixel_spacing[0] > 0 else 1.0

    if plane == "axial":
        # XY plane at Z=index - no scaling needed
        index = max(0, min(index, nz - 1))
        slice_2d = volume[index, :, :]
        width, height = nx, ny
        total = nz
        # No resize needed for axial
        normalized = (slice_2d * 255).astype(np.uint8)
    elif plane == "sagittal":
        # YZ plane at X=index
        index = max(0, min(index, nx - 1))
        slice_2d = volume[:, :, index]  # shape: (nz, ny)
        # Scale Z dimension to correct aspect ratio
        from scipy.ndimage import zoom
        slice_scaled = zoom(slice_2d, (z_scale, 1.0), order=1)
        # Flip vertically so superior (top of head) is at top of image
        slice_scaled = np.flipud(slice_scaled)
        # Use actual zoomed dimensions (zoom may round differently)
        height, width = slice_scaled.shape  # (scaled_nz, ny)
        total = nx
        normalized = (slice_scaled * 255).astype(np.uint8)
    elif plane == "coronal":
        # XZ plane at Y=index
        index = max(0, min(index, ny - 1))
        slice_2d = volume[:, index, :]  # shape: (nz, nx)
        # Scale Z dimension to correct aspect ratio
        from scipy.ndimage import zoom
        slice_scaled = zoom(slice_2d, (z_scale, 1.0), order=1)
        # Flip vertically so superior (top of head) is at top of image
        slice_scaled = np.flipud(slice_scaled)
        # Use actual zoomed dimensions
        height, width = slice_scaled.shape  # (scaled_nz, nx)
        total = ny
        normalized = (slice_scaled * 255).astype(np.uint8)
    else:
        return jsonify({"error": "Invalid plane"}), 400

    pixels = normalized.flatten().tolist()

    return jsonify({
        "width": width,
        "height": height,
        "pixels": pixels,
        "sliceIndex": index,
        "totalSlices": total,
        "plane": plane,
        "zScale": z_scale
    })


@app.route("/api/voxel-metadata/<study_id>/<series_name>/<int:x>/<int:y>/<int:z>")
def get_voxel_metadata(study_id, series_name, x, y, z):
    """
    Get comprehensive metadata for a specific voxel location.
    Returns slice metadata + computed voxel values.
    """
    vol_data = load_volume_cached(study_id, series_name)
    if vol_data is None:
        return jsonify({"error": "Volume not found"}), 404

    nz, ny, nx = vol_data["dimensions"]

    # Clamp coordinates
    x = max(0, min(x, nx - 1))
    y = max(0, min(y, ny - 1))
    z = max(0, min(z, nz - 1))

    # Get voxel values
    raw_value = float(vol_data["raw_volume"][z, y, x])
    norm_value = float(vol_data["volume"][z, y, x])

    # Calculate world position
    pixel_spacing = vol_data["pixel_spacing"]
    slice_thickness = vol_data["slice_thickness"]

    # Get metadata from the database for this slice
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT * FROM dicom_files
        WHERE StudyInstanceUID = %s AND SeriesDescription = %s
        ORDER BY InstanceNumber
        LIMIT 1 OFFSET ?
    """, (study_id, series_name, z))

    row = cursor.fetchone()
    conn.close()

    if not row:
        return jsonify({"error": "Slice metadata not found"}), 404

    # Parse ImagePositionPatient for world coordinates
    world_pos = [0, 0, 0]
    if row["ImagePositionPatient"]:
        try:
            origin = json.loads(row["ImagePositionPatient"])
            world_pos = [
                origin[0] + x * pixel_spacing[1],
                origin[1] + y * pixel_spacing[0],
                origin[2]
            ]
        except:
            pass

    # Build comprehensive metadata response
    metadata = {
        # Voxel info
        "voxel": {
            "x": x, "y": y, "z": z,
            "rawValue": raw_value,
            "normValue": round(norm_value, 4),
            "worldPos": [round(p, 2) for p in world_pos]
        },
        # Volume info
        "volume": {
            "dimensions": [nx, ny, nz],
            "pixelSpacing": pixel_spacing,
            "sliceThickness": slice_thickness,
            "valueRange": [vol_data["vmin"], vol_data["vmax"]]
        },
        # MRI Parameters
        "mri": {
            "TR": row["RepetitionTime"],
            "TE": row["EchoTime"],
            "TI": row["InversionTime"],
            "flipAngle": row["FlipAngle"],
            "fieldStrength": row["MagneticFieldStrength"],
            "scanningSequence": row["ScanningSequence"],
            "sequenceVariant": row["SequenceVariant"],
            "acquisitionType": row["MRAcquisitionType"]
        },
        # Spatial
        "spatial": {
            "sliceLocation": row["SliceLocation"],
            "sliceThickness": row["SliceThickness"],
            "imagePosition": row["ImagePositionPatient"],
            "imageOrientation": row["ImageOrientationPatient"]
        },
        # Study/Series
        "study": {
            "description": row["StudyDescription"],
            "date": row["StudyDate"],
            "time": row["StudyTime"]
        },
        "series": {
            "description": row["SeriesDescription"],
            "number": row["SeriesNumber"],
            "modality": row["Modality"]
        },
        # Instance
        "instance": {
            "number": row["InstanceNumber"],
            "acquisitionDate": row["AcquisitionDate"],
            "acquisitionTime": row["AcquisitionTime"]
        },
        # Equipment
        "equipment": {
            "manufacturer": row["Manufacturer"],
            "model": row["ManufacturerModelName"],
            "station": row["StationName"],
            "institution": row["InstitutionName"]
        },
        # Display
        "display": {
            "windowCenter": row["WindowCenter"],
            "windowWidth": row["WindowWidth"]
        }
    }

    return jsonify(metadata)


@app.route("/api/volume-info/<study_id>/<series_name>")
def get_volume_info(study_id, series_name):
    """Get volume dimensions and spacing without loading full volume data."""
    vol_data = load_volume_cached(study_id, series_name)
    if vol_data is None:
        return jsonify({"error": "Volume not found"}), 404

    nz, ny, nx = vol_data["dimensions"]
    return jsonify({
        "dimensions": {"x": nx, "y": ny, "z": nz},
        "pixelSpacing": vol_data["pixel_spacing"],
        "sliceThickness": vol_data["slice_thickness"]
    })


@app.route("/api/health")
def health_check():
    """Health check endpoint for load balancers."""
    return jsonify({"status": "healthy", "storage": "s3" if USE_S3 else "local"})


if __name__ == "__main__":
    logger.info(f"Storage mode: {'S3 (' + S3_BUCKET_NAME + ')' if USE_S3 else 'Local'}")
    logger.info(f"Database: {DB_HOST}:{DB_PORT}/{DB_NAME}")
    logger.info("Starting server at http://127.0.0.1:5000")
    app.run(debug=True, host="127.0.0.1", port=5000)

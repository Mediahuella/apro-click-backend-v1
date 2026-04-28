"""Auth handler — login, logout, password recovery, first-time password change."""
from __future__ import annotations

import sys
from pathlib import Path

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver
from aws_lambda_powertools.event_handler.exceptions import (
    BadRequestError,
    InternalServerError,
    UnauthorizedError,
)
from aws_lambda_powertools.utilities.typing import LambdaContext

# ============================================================================
# CONFIGURAR PATHS PRIMERO
# ============================================================================
service_root = Path(__file__).resolve().parent.parent
if str(service_root) not in sys.path:
    sys.path.insert(0, str(service_root))

lambda_root = "/var/task"
if lambda_root not in sys.path:
    sys.path.insert(0, lambda_root)

possible_shared_paths = [
    Path("/var/task/shared"),
    service_root / "shared",
]

for path in possible_shared_paths:
    if path.exists() and (path / "cognito").exists():
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
        break

# ============================================================================
# IMPORTS DESPUES DE CONFIGURAR PATHS
# ============================================================================
from services.auth_service import AuthService

logger = Logger()
tracer = Tracer()
app = APIGatewayHttpResolver()
auth_service = AuthService()


@app.post("/api/v1/auth/login")
@tracer.capture_method
def login():
    try:
        body = app.current_event.json_body or {}
        email = body.get("email")
        password = body.get("password")

        if not email or not password:
            raise BadRequestError("'email' and 'password' are required")

        result = auth_service.login(email, password)

        if "challenge" in result:
            return {"statusCode": 200, "message": "Challenge required", "data": result}

        return {"statusCode": 200, "message": "Login successful", "data": result}

    except BadRequestError:
        raise
    except ValueError as e:
        raise UnauthorizedError(str(e))
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except Exception as e:
        error_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if error_code in ("NotAuthorizedException", "UserNotFoundException"):
            raise UnauthorizedError("Invalid email or password")
        logger.exception("Error during login")
        raise InternalServerError("Error during login")


@app.post("/api/v1/auth/change-password")
@tracer.capture_method
def change_first_time_password():
    try:
        body = app.current_event.json_body or {}
        email = body.get("email")
        new_password = body.get("new_password")
        session = body.get("session")

        if not all([email, new_password, session]):
            raise BadRequestError("'email', 'new_password' and 'session' are required")

        result = auth_service.change_first_time_password(email, new_password, session)
        return {"statusCode": 200, "message": "Password changed", "data": result}

    except BadRequestError:
        raise
    except Exception as e:
        error_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if error_code == "InvalidPasswordException":
            raise BadRequestError("Password does not meet requirements")
        if error_code == "CodeMismatchException":
            raise BadRequestError("Invalid or expired session")
        logger.exception("Error changing password")
        raise InternalServerError("Error changing password")


@app.post("/api/v1/auth/forgot-password")
@tracer.capture_method
def forgot_password():
    try:
        body = app.current_event.json_body or {}
        email = body.get("email")

        if not email:
            raise BadRequestError("'email' is required")

        result = auth_service.forgot_password(email)
        return {"statusCode": 200, "message": "Verification code sent", "data": result}

    except BadRequestError:
        raise
    except Exception as e:
        error_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if error_code == "UserNotFoundException":
            return {"statusCode": 200, "message": "If the email exists, a code was sent", "data": {}}
        logger.exception("Error in forgot password")
        raise InternalServerError("Error processing request")


@app.post("/api/v1/auth/confirm-forgot-password")
@tracer.capture_method
def confirm_forgot_password():
    try:
        body = app.current_event.json_body or {}
        email = body.get("email")
        code = body.get("confirmation_code")
        new_password = body.get("new_password")

        if not all([email, code, new_password]):
            raise BadRequestError("'email', 'confirmation_code' and 'new_password' are required")

        result = auth_service.confirm_forgot_password(email, code, new_password)
        return {"statusCode": 200, "message": "Password reset successful", "data": result}

    except BadRequestError:
        raise
    except Exception as e:
        error_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
        if error_code == "CodeMismatchException":
            raise BadRequestError("Invalid verification code")
        if error_code == "ExpiredCodeException":
            raise BadRequestError("Verification code has expired")
        if error_code == "InvalidPasswordException":
            raise BadRequestError("Password does not meet requirements")
        logger.exception("Error confirming password reset")
        raise InternalServerError("Error processing request")


@app.post("/api/v1/auth/logout")
@tracer.capture_method
def logout():
    try:
        body = app.current_event.json_body or {}
        access_token = body.get("access_token")
        refresh_token = body.get("refresh_token")

        if not access_token and not refresh_token:
            raise BadRequestError("'access_token' or 'refresh_token' is required")

        result = auth_service.logout(
            access_token=access_token,
            refresh_token=refresh_token,
        )
        return {"statusCode": 200, "message": "Logged out", "data": result}

    except BadRequestError:
        raise
    except Exception as e:
        logger.exception("Error during logout")
        raise InternalServerError("Error during logout")


def lambda_handler(event: dict, context: LambdaContext):
    return app.resolve(event, context)

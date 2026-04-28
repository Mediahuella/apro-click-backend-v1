"""Auth business-logic service — Cognito authentication flows."""
from __future__ import annotations

import sys
from typing import Any

from aws_lambda_powertools import Logger
from pathlib import Path

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
from sqlalchemy import select

from cognito.client import (
    initiate_auth,
    respond_to_auth_challenge,
    forgot_password,
    confirm_forgot_password,
    global_sign_out,
    revoke_token,
    get_app_client_id,
)
from database.engine import get_session
from database.models.user import User

logger = Logger()


class AuthService:
    """Handles authentication flows against Cognito."""

    def login(self, email: str, password: str) -> dict[str, Any]:
        """Authenticate a user.  Returns tokens or a challenge."""
        with get_session() as session:
            user = session.execute(
                select(User).where(User.email == email)
            ).scalar_one_or_none()

        if not user:
            raise ValueError("User not found in the system")

        if user.status == "DISABLED":
            raise PermissionError("User account is disabled")

        response = initiate_auth(email, password)

        if "ChallengeName" in response:
            challenge = response["ChallengeName"]
            logger.info("Auth challenge received", extra={"challenge": challenge, "email": email})
            return {
                "challenge": challenge,
                "session": response["Session"],
                "challenge_parameters": response.get("ChallengeParameters", {}),
            }

        if user.status == "PENDING":
            logger.warning("Login with PENDING status — password was already changed outside flow", extra={"email": email})
            self._activate_user(email)

        auth_result = response["AuthenticationResult"]
        logger.info("Login successful", extra={"email": email})
        return {
            "access_token": auth_result["AccessToken"],
            "id_token": auth_result["IdToken"],
            "refresh_token": auth_result["RefreshToken"],
            "expires_in": auth_result["ExpiresIn"],
            "token_type": auth_result["TokenType"],
        }

    def change_first_time_password(
        self,
        email: str,
        new_password: str,
        session: str,
    ) -> dict[str, Any]:
        """Respond to NEW_PASSWORD_REQUIRED challenge after first login."""
        response = respond_to_auth_challenge(
            session=session,
            challenge_name="NEW_PASSWORD_REQUIRED",
            challenge_responses={
                "USERNAME": email,
                "NEW_PASSWORD": new_password,
            },
        )

        if "AuthenticationResult" in response:
            self._activate_user(email)

            auth_result = response["AuthenticationResult"]
            logger.info("First-time password changed and user activated", extra={"email": email})
            return {
                "access_token": auth_result["AccessToken"],
                "id_token": auth_result["IdToken"],
                "refresh_token": auth_result["RefreshToken"],
                "expires_in": auth_result["ExpiresIn"],
                "token_type": auth_result["TokenType"],
            }

        return {"challenge": response.get("ChallengeName"), "session": response.get("Session")}

    def forgot_password(self, email: str) -> dict[str, Any]:
        """Trigger forgot-password flow (Cognito sends verification code)."""
        response = forgot_password(email)
        delivery = response.get("CodeDeliveryDetails", {})
        logger.info("Forgot password initiated", extra={"email": email})
        return {
            "message": "Verification code sent",
            "delivery_medium": delivery.get("DeliveryMedium"),
            "destination": delivery.get("Destination"),
        }

    def confirm_forgot_password(
        self,
        email: str,
        confirmation_code: str,
        new_password: str,
    ) -> dict[str, Any]:
        """Confirm password reset with verification code."""
        confirm_forgot_password(email, confirmation_code, new_password)
        logger.info("Password reset confirmed", extra={"email": email})
        return {"message": "Password reset successful"}

    def _activate_user(self, email: str) -> None:
        """Set user status to ACTIVE in PostgreSQL."""
        with get_session() as session:
            user = session.execute(
                select(User).where(User.email == email)
            ).scalar_one_or_none()
            if user and user.status == "PENDING":
                user.status = "ACTIVE"
                session.commit()
                logger.info("User status changed to ACTIVE", extra={"email": email})

    def logout(
        self,
        access_token: str | None = None,
        refresh_token: str | None = None,
    ) -> dict[str, Any]:
        """Sign out the user (global sign-out + revoke refresh token)."""
        if access_token:
            global_sign_out(access_token)

        if refresh_token:
            revoke_token(refresh_token)

        logger.info("User logged out")
        return {"message": "Logged out successfully"}

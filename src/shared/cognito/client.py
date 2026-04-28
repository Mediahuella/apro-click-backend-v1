"""Cognito Identity Provider client — shared across services."""
from __future__ import annotations

import os
from typing import Any

import boto3
from aws_lambda_powertools import Logger

logger = Logger()

_cognito_client = None


def _get_cognito_client():
    global _cognito_client
    if _cognito_client is None:
        region = os.getenv("REGION", "us-east-2")
        _cognito_client = boto3.client("cognito-idp", region_name=region)
    return _cognito_client


def get_user_pool_id() -> str:
    return os.environ["COGNITO_USER_POOL_ID"]


def get_app_client_id() -> str:
    return os.environ["COGNITO_APP_CLIENT_ID"]


# ---------------------------------------------------------------------------
# Admin helpers (server-side, no SRP)
# ---------------------------------------------------------------------------

def admin_create_user(
    email: str,
    *,
    temporary_password: str | None = None,
    attributes: dict[str, str] | None = None,
    suppress_invitation: bool = False,
) -> dict[str, Any]:
    """Create a user in Cognito and return the full response."""
    client = _get_cognito_client()
    user_pool_id = get_user_pool_id()

    user_attributes = [
        {"Name": "email", "Value": email},
        {"Name": "email_verified", "Value": "true"},
    ]
    if attributes:
        user_attributes.extend(
            {"Name": k, "Value": v} for k, v in attributes.items()
        )

    params: dict[str, Any] = {
        "UserPoolId": user_pool_id,
        "Username": email,
        "UserAttributes": user_attributes,
        "DesiredDeliveryMediums": ["EMAIL"],
    }
    if temporary_password:
        params["TemporaryPassword"] = temporary_password
    if suppress_invitation:
        params["MessageAction"] = "SUPPRESS"

    logger.info("Creating Cognito user", extra={"email": email})
    return client.admin_create_user(**params)


def admin_get_user(username: str) -> dict[str, Any]:
    """Get user details from Cognito."""
    client = _get_cognito_client()
    return client.admin_get_user(
        UserPoolId=get_user_pool_id(),
        Username=username,
    )


def admin_update_user_attributes(
    username: str, attributes: dict[str, str]
) -> dict[str, Any]:
    """Update user attributes in Cognito."""
    client = _get_cognito_client()
    return client.admin_update_user_attributes(
        UserPoolId=get_user_pool_id(),
        Username=username,
        UserAttributes=[
            {"Name": k, "Value": v} for k, v in attributes.items()
        ],
    )


def admin_disable_user(username: str) -> dict[str, Any]:
    client = _get_cognito_client()
    logger.info("Disabling Cognito user", extra={"username": username})
    return client.admin_disable_user(
        UserPoolId=get_user_pool_id(),
        Username=username,
    )


def admin_enable_user(username: str) -> dict[str, Any]:
    client = _get_cognito_client()
    logger.info("Enabling Cognito user", extra={"username": username})
    return client.admin_enable_user(
        UserPoolId=get_user_pool_id(),
        Username=username,
    )


def admin_delete_user(username: str) -> dict[str, Any]:
    client = _get_cognito_client()
    logger.info("Deleting Cognito user", extra={"username": username})
    return client.admin_delete_user(
        UserPoolId=get_user_pool_id(),
        Username=username,
    )


def admin_add_user_to_group(username: str, group_name: str) -> dict[str, Any]:
    client = _get_cognito_client()
    return client.admin_add_user_to_group(
        UserPoolId=get_user_pool_id(),
        GroupName=group_name,
        Username=username,
    )


def admin_remove_user_from_group(username: str, group_name: str) -> dict[str, Any]:
    client = _get_cognito_client()
    return client.admin_remove_user_from_group(
        UserPoolId=get_user_pool_id(),
        GroupName=group_name,
        Username=username,
    )


def admin_list_groups_for_user(username: str) -> list[dict[str, Any]]:
    client = _get_cognito_client()
    resp = client.admin_list_groups_for_user(
        UserPoolId=get_user_pool_id(),
        Username=username,
    )
    return resp.get("Groups", [])


# ---------------------------------------------------------------------------
# Auth-flow helpers (used by the auth service)
# ---------------------------------------------------------------------------

def initiate_auth(email: str, password: str) -> dict[str, Any]:
    """Admin-initiated auth (USER_PASSWORD_AUTH flow, server-side)."""
    client = _get_cognito_client()
    return client.admin_initiate_auth(
        UserPoolId=get_user_pool_id(),
        ClientId=get_app_client_id(),
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={
            "USERNAME": email,
            "PASSWORD": password,
        },
    )


def respond_to_auth_challenge(
    session: str,
    challenge_name: str,
    challenge_responses: dict[str, str],
) -> dict[str, Any]:
    """Respond to a Cognito auth challenge (e.g. NEW_PASSWORD_REQUIRED)."""
    client = _get_cognito_client()
    return client.admin_respond_to_auth_challenge(
        UserPoolId=get_user_pool_id(),
        ClientId=get_app_client_id(),
        ChallengeName=challenge_name,
        Session=session,
        ChallengeResponses=challenge_responses,
    )


def forgot_password(email: str) -> dict[str, Any]:
    """Initiate forgot-password flow (sends code to user email)."""
    client = _get_cognito_client()
    return client.forgot_password(
        ClientId=get_app_client_id(),
        Username=email,
    )


def confirm_forgot_password(
    email: str, confirmation_code: str, new_password: str
) -> dict[str, Any]:
    """Confirm forgot-password with the code the user received."""
    client = _get_cognito_client()
    return client.confirm_forgot_password(
        ClientId=get_app_client_id(),
        Username=email,
        ConfirmationCode=confirmation_code,
        Password=new_password,
    )


def global_sign_out(access_token: str) -> dict[str, Any]:
    """Invalidate all tokens for the user (global sign-out)."""
    client = _get_cognito_client()
    return client.global_sign_out(AccessToken=access_token)


def revoke_token(refresh_token: str) -> dict[str, Any]:
    """Revoke a specific refresh token."""
    client = _get_cognito_client()
    return client.revoke_token(
        Token=refresh_token,
        ClientId=get_app_client_id(),
    )

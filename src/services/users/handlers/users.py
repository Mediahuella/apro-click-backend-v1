"""Users CRUD handler — servicio: apro-click-admin-users"""
from __future__ import annotations

import sys
from pathlib import Path

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver
from aws_lambda_powertools.event_handler.exceptions import (
    BadRequestError,
    NotFoundError,
    InternalServerError,
)
from aws_lambda_powertools.utilities.typing import LambdaContext

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

from services.user_service import UserService
from utils.company_ids import company_ids_from_request_body

logger = Logger()
tracer = Tracer()
app = APIGatewayHttpResolver()
user_service = UserService()


@app.post("/api/v1/users")
@tracer.capture_method
def create_user():
    try:
        body = app.current_event.json_body or {}

        email = body.get("email")
        given_name = body.get("given_name", "")
        family_name = body.get("family_name", "")
        role = body.get("role", "SALES")
        temporary_password = body.get("temporary_password")
        if not email:
            raise BadRequestError("'email' is required")

        try:
            company_ids = company_ids_from_request_body(body)
        except ValueError as e:
            raise BadRequestError(str(e)) from e

        result = user_service.create_user(
            email=email,
            given_name=given_name,
            family_name=family_name,
            role=role,
            temporary_password=temporary_password,
            company_ids=company_ids,
        )
        return {"statusCode": 201, "message": "User created", "data": result}

    except ValueError as e:
        raise BadRequestError(str(e))
    except Exception:
        logger.exception("Error creating user")
        raise InternalServerError("Error creating user")


@app.get("/api/v1/users")
@tracer.capture_method
def list_users():
    try:
        params = app.current_event.query_string_parameters or {}
        limit = int(params.get("limit", "50"))
        offset = int(params.get("offset", "0"))

        result = user_service.list_users(limit=limit, offset=offset)
        return {"statusCode": 200, "message": "Users retrieved", "data": result}

    except Exception:
        logger.exception("Error listing users")
        raise InternalServerError("Error listing users")


@app.get("/api/v1/users/<user_id>")
@tracer.capture_method
def get_user(user_id: str):
    try:
        result = user_service.get_user(user_id)
        if not result:
            raise NotFoundError(f"User '{user_id}' not found")
        return {"statusCode": 200, "message": "User retrieved", "data": result}

    except NotFoundError:
        raise
    except Exception:
        logger.exception("Error getting user")
        raise InternalServerError("Error getting user")


@app.put("/api/v1/users/<user_id>")
@tracer.capture_method
def update_user(user_id: str):
    try:
        body = app.current_event.json_body or {}
        if not body:
            raise BadRequestError("Request body is required")

        result = user_service.update_user(user_id, body)
        return {"statusCode": 200, "message": "User updated", "data": result}

    except ValueError as e:
        raise BadRequestError(str(e))
    except Exception:
        logger.exception("Error updating user")
        raise InternalServerError("Error updating user")


@app.post("/api/v1/users/<user_id>/link-shopify-staff")
@tracer.capture_method
def link_shopify_staff(user_id: str):
    try:
        result = user_service.link_shopify_staff(user_id)
        return {
            "statusCode": 200,
            "message": "Shopify staff link attempted",
            "data": result,
        }
    except ValueError as e:
        msg = str(e)
        if "not found" in msg.lower():
            raise NotFoundError(msg)
        raise BadRequestError(msg)
    except Exception:
        logger.exception("Error linking Shopify staff")
        raise InternalServerError("Error linking Shopify staff")


@app.post("/api/v1/users/<user_id>/associate-shopify-staff")
@tracer.capture_method
def associate_shopify_staff(user_id: str):
    """
    Asocia un colaborador ya creado en Shopify con el usuario del panel, usando
    el GID `gid://shopify/StaffMember/...` (p. ej. API GraphQL, Admin o app).
    Cuerpo JSON: { "shopify_staff_member_gid": "..." },
    opcional: { "skip_email_verification": true }.
    """
    try:
        body = app.current_event.json_body or {}
        result = user_service.associate_shopify_staff(user_id, body)
        return {
            "statusCode": 200,
            "message": "Shopify staff association saved",
            "data": result,
        }
    except ValueError as e:
        msg = str(e)
        if "not found" in msg.lower():
            raise NotFoundError(msg)
        raise BadRequestError(msg)
    except Exception:
        logger.exception("Error associating Shopify staff by GID")
        raise InternalServerError("Error associating Shopify staff by GID")


@app.delete("/api/v1/users/<user_id>")
@tracer.capture_method
def delete_user(user_id: str):
    try:
        user_service.delete_user(user_id)
        return {"statusCode": 200, "message": "User deleted"}

    except ValueError as e:
        raise BadRequestError(str(e))
    except Exception:
        logger.exception("Error deleting user")
        raise InternalServerError("Error deleting user")


def lambda_handler(event: dict, context: LambdaContext):
    return app.resolve(event, context)

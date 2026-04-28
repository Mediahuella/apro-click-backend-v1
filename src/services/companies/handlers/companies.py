"""Companies CRUD handler — servicio: apro-click-admin-companies"""
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
    if path.exists() and (path / "database").exists():
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
        break

from services.company_service import CompanyService

logger = Logger()
tracer = Tracer()
app = APIGatewayHttpResolver()
company_service = CompanyService()


@app.post("/api/v1/companies")
@tracer.capture_method
def create_company():
    try:
        body = app.current_event.json_body or {}
        name = body.get("name")
        company_type = body.get("company_type", "SMALL")
        payment_type = body.get("payment_type", "DIRECT")

        result = company_service.create_company(
            name=name,
            company_type=company_type,
            payment_type=payment_type,
        )
        return {"statusCode": 201, "message": "Company created", "data": result}

    except ValueError as e:
        raise BadRequestError(str(e))
    except Exception:
        logger.exception("Error creating company")
        raise InternalServerError("Error creating company")


@app.get("/api/v1/companies")
@tracer.capture_method
def list_companies():
    try:
        params = app.current_event.query_string_parameters or {}
        limit = int(params.get("limit", "50"))
        offset = int(params.get("offset", "0"))

        result = company_service.list_companies(limit=limit, offset=offset)
        return {"statusCode": 200, "message": "Companies retrieved", "data": result}

    except Exception:
        logger.exception("Error listing companies")
        raise InternalServerError("Error listing companies")


@app.get("/api/v1/companies/<company_id>")
@tracer.capture_method
def get_company(company_id: str):
    try:
        result = company_service.get_company(company_id)
        if not result:
            raise NotFoundError(f"Company '{company_id}' not found")
        return {"statusCode": 200, "message": "Company retrieved", "data": result}

    except ValueError as e:
        raise BadRequestError(str(e))
    except NotFoundError:
        raise
    except Exception:
        logger.exception("Error getting company")
        raise InternalServerError("Error getting company")


@app.put("/api/v1/companies/<company_id>")
@tracer.capture_method
def update_company(company_id: str):
    try:
        body = app.current_event.json_body or {}
        if not body:
            raise BadRequestError("Request body is required")

        result = company_service.update_company(company_id, body)
        return {"statusCode": 200, "message": "Company updated", "data": result}

    except ValueError as e:
        raise BadRequestError(str(e))
    except Exception:
        logger.exception("Error updating company")
        raise InternalServerError("Error updating company")


@app.delete("/api/v1/companies/<company_id>")
@tracer.capture_method
def delete_company(company_id: str):
    try:
        company_service.delete_company(company_id)
        return {"statusCode": 200, "message": "Company deleted"}

    except ValueError as e:
        raise BadRequestError(str(e))
    except Exception:
        logger.exception("Error deleting company")
        raise InternalServerError("Error deleting company")


def lambda_handler(event: dict, context: LambdaContext):
    return app.resolve(event, context)

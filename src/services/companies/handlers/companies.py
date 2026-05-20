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

from services.address_service import AddressService
from services.company_service import CompanyService
from services.contact_service import ContactService

logger = Logger()
tracer = Tracer()
app = APIGatewayHttpResolver()
company_service = CompanyService()
address_service = AddressService()
contact_service = ContactService()


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


@app.get("/api/v1/companies/<company_id>/addresses")
@tracer.capture_method
def list_company_addresses(company_id: str):
    try:
        result = address_service.list_addresses(company_id)
        return {"statusCode": 200, "message": "Addresses retrieved", "data": result}
    except ValueError as e:
        raise BadRequestError(str(e))
    except Exception:
        logger.exception("Error listing company addresses")
        raise InternalServerError("Error listing addresses")


@app.post("/api/v1/companies/<company_id>/addresses")
@tracer.capture_method
def create_company_address(company_id: str):
    try:
        body = app.current_event.json_body or {}
        result = address_service.create_address(company_id, body)
        return {"statusCode": 201, "message": "Address created", "data": result}
    except ValueError as e:
        raise BadRequestError(str(e))
    except LookupError as e:
        raise InternalServerError(str(e))
    except Exception:
        logger.exception("Error creating company address")
        raise InternalServerError("Error creating address")


@app.get("/api/v1/companies/<company_id>/addresses/<address_id>")
@tracer.capture_method
def get_company_address(company_id: str, address_id: str):
    try:
        result = address_service.get_address(company_id, address_id)
        if not result:
            raise NotFoundError(f"Address '{address_id}' not found")
        return {"statusCode": 200, "message": "Address retrieved", "data": result}
    except ValueError as e:
        raise BadRequestError(str(e))
    except NotFoundError:
        raise
    except Exception:
        logger.exception("Error getting company address")
        raise InternalServerError("Error getting address")


@app.put("/api/v1/companies/<company_id>/addresses/<address_id>")
@tracer.capture_method
def update_company_address(company_id: str, address_id: str):
    try:
        body = app.current_event.json_body or {}
        if not body:
            raise BadRequestError("Request body is required")
        result = address_service.update_address(company_id, address_id, body)
        return {"statusCode": 200, "message": "Address updated", "data": result}
    except ValueError as e:
        raise BadRequestError(str(e))
    except LookupError as e:
        raise InternalServerError(str(e))
    except Exception:
        logger.exception("Error updating company address")
        raise InternalServerError("Error updating address")


@app.delete("/api/v1/companies/<company_id>/addresses/<address_id>")
@tracer.capture_method
def delete_company_address(company_id: str, address_id: str):
    try:
        address_service.delete_address(company_id, address_id)
        return {"statusCode": 200, "message": "Address deleted"}
    except ValueError as e:
        raise BadRequestError(str(e))
    except LookupError as e:
        raise InternalServerError(str(e))
    except Exception:
        logger.exception("Error deleting company address")
        raise InternalServerError("Error deleting address")


@app.get("/api/v1/companies/<company_id>/contacts")
@tracer.capture_method
def list_company_contacts(company_id: str):
    try:
        result = contact_service.list_contacts(company_id)
        return {"statusCode": 200, "message": "Contacts retrieved", "data": result}
    except ValueError as e:
        raise BadRequestError(str(e))
    except Exception:
        logger.exception("Error listing company contacts")
        raise InternalServerError("Error listing contacts")


@app.post("/api/v1/companies/<company_id>/contacts")
@tracer.capture_method
def create_company_contact(company_id: str):
    try:
        body = app.current_event.json_body or {}
        result = contact_service.create_contact(company_id, body)
        return {"statusCode": 201, "message": "Contact created", "data": result}
    except ValueError as e:
        raise BadRequestError(str(e))
    except LookupError as e:
        raise InternalServerError(str(e))
    except Exception:
        logger.exception("Error creating company contact")
        raise InternalServerError("Error creating contact")


@app.put("/api/v1/companies/<company_id>/contacts/<client_id>")
@tracer.capture_method
def update_company_contact(company_id: str, client_id: str):
    try:
        body = app.current_event.json_body or {}
        if not body:
            raise BadRequestError("Request body is required")
        result = contact_service.update_contact(company_id, client_id, body)
        return {"statusCode": 200, "message": "Contact updated", "data": result}
    except ValueError as e:
        raise BadRequestError(str(e))
    except LookupError as e:
        raise InternalServerError(str(e))
    except Exception:
        logger.exception("Error updating company contact")
        raise InternalServerError("Error updating contact")


@app.delete("/api/v1/companies/<company_id>/contacts/<client_id>")
@tracer.capture_method
def delete_company_contact(company_id: str, client_id: str):
    try:
        contact_service.delete_contact(company_id, client_id)
        return {"statusCode": 200, "message": "Contact removed"}
    except ValueError as e:
        raise BadRequestError(str(e))
    except LookupError as e:
        raise InternalServerError(str(e))
    except Exception:
        logger.exception("Error deleting company contact")
        raise InternalServerError("Error deleting contact")


@app.post("/api/v1/companies/<company_id>/contacts/<client_id>/main")
@tracer.capture_method
def set_company_main_contact(company_id: str, client_id: str):
    try:
        result = contact_service.set_main_contact(company_id, client_id)
        return {"statusCode": 200, "message": "Main contact updated", "data": result}
    except ValueError as e:
        raise BadRequestError(str(e))
    except LookupError as e:
        raise InternalServerError(str(e))
    except Exception:
        logger.exception("Error setting main contact")
        raise InternalServerError("Error setting main contact")


@app.get("/api/v1/companies/<company_id>/contacts/<client_id>/locations")
@tracer.capture_method
def list_contact_locations(company_id: str, client_id: str):
    try:
        result = contact_service.list_contact_locations(company_id, client_id)
        return {"statusCode": 200, "message": "Locations retrieved", "data": result}
    except ValueError as e:
        raise BadRequestError(str(e))
    except LookupError as e:
        raise InternalServerError(str(e))
    except Exception:
        logger.exception("Error listing contact locations")
        raise InternalServerError("Error listing contact locations")


@app.put("/api/v1/companies/<company_id>/contacts/<client_id>/locations")
@tracer.capture_method
def set_contact_locations(company_id: str, client_id: str):
    try:
        body = app.current_event.json_body or {}
        address_ids = body.get("address_ids")
        if not isinstance(address_ids, list):
            raise BadRequestError("Body debe incluir 'address_ids' como array")
        result = contact_service.set_contact_locations(
            company_id, client_id, address_ids
        )
        return {"statusCode": 200, "message": "Locations updated", "data": result}
    except ValueError as e:
        raise BadRequestError(str(e))
    except LookupError as e:
        raise InternalServerError(str(e))
    except Exception:
        logger.exception("Error setting contact locations")
        raise InternalServerError("Error setting contact locations")


def lambda_handler(event: dict, context: LambdaContext):
    return app.resolve(event, context)

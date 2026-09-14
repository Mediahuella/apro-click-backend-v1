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
    UnauthorizedError,
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
from utils.cognito_access import (
    get_user_by_cognito_access_token,
    parse_bearer_authorization,
)
from apro.credit_client import consultar_credito, esta_configurado
from utils.public_api_key import require_companies_public_api_key

logger = Logger()
tracer = Tracer()
app = APIGatewayHttpResolver()
company_service = CompanyService()
address_service = AddressService()
contact_service = ContactService()


def _headers_dict() -> dict[str, str]:
    raw = app.current_event.headers
    if not raw:
        return {}
    return {str(k): (v if v is not None else "") for k, v in raw.items()}


def _params() -> dict[str, str]:
    return app.current_event.query_string_parameters or {}



def _require_panel_user() -> dict:
    """Exige ``Authorization: Bearer`` con un access token válido del panel.

    Las rutas de panel no tienen authorizer en API Gateway: sin esta guarda
    respondían 200 a cualquiera que conociera la URL del API. No filtra por rol
    —eso lo resuelve cada vista— solo verifica que haya una sesión activa.
    """
    token = parse_bearer_authorization(_headers_dict())
    if not token:
        raise UnauthorizedError("Se requiere Authorization: Bearer con access token")
    try:
        return get_user_by_cognito_access_token(token)
    except PermissionError as e:
        raise UnauthorizedError(str(e))


def _resolve_storefront_company_id(shopify_company_id: str) -> str:
    """Resuelve el UUID interno de ``Company`` a partir de ``shopify_company_id``.

    Nunca acepta un UUID interno desde el cliente: el storefront solo conoce
    el GID de Shopify de la sesión B2B logueada (``customer.current_company.id``).
    """
    shopify_company_id = str(shopify_company_id or "").strip()
    if not shopify_company_id:
        raise BadRequestError("shopify_company_id es obligatorio")
    company = company_service.get_company_by_shopify_id(shopify_company_id)
    if not company:
        raise NotFoundError("Empresa no encontrada")
    return company["id"]


# ---------------------------------------------------------------------------
# Storefront público — crédito de la empresa (checkout UI extension)
# ---------------------------------------------------------------------------
@app.get("/api/v1/storefront/company/credit")
@tracer.capture_method
def storefront_company_credit():
    """Condiciones de crédito de la empresa B2B logueada, para el checkout.

    Lo consume la checkout UI extension ``credito-b2b`` cuando el comprador
    elige pagar con crédito. Devuelve:

    - ``enabled``: si la empresa tiene crédito autorizado. **Es el CRM**
      (``payment_type``), la misma fuente que el metafield que usa la Function
      para mostrar u ocultar el medio de pago — así el banner nunca contradice
      lo que el comprador ve en la lista de pagos.
    - ``credit_folio``: el folio del crédito en el DMS de APRO (``CR-NNNNNN``),
      cuando se puede consultar. Es informativo.
    - ``limit`` / ``available`` / ``currency``: **siempre ``null``**. El
      servicio de créditos de APRO (``GET /credits/client/{rut}``) informa la
      existencia del crédito, no el cupo; si algún día publican montos, estas
      claves ya están en el contrato y la extension del checkout las muestra
      sola. Mientras tanto el banner explica las condiciones sin cifras.

    La empresa se resuelve por ``shopify_company_id`` de la sesión B2B; el RUT
    con que se consulta el DMS sale de la BD, nunca del cliente.
    """
    try:
        require_companies_public_api_key(_headers_dict())
        shopify_company_id = str(_params().get("shopify_company_id", "")).strip()
        if not shopify_company_id:
            raise BadRequestError("shopify_company_id es obligatorio")
        company = company_service.get_company_by_shopify_id(shopify_company_id)
        if not company:
            raise NotFoundError("Empresa no encontrada")

        enabled = str(company.get("payment_type") or "").upper() == "CREDIT"
        credito = consultar_credito(company.get("billing_rut") or "") if enabled else None
        data = {
            "enabled": enabled,
            "company_name": company.get("name"),
            "credit_folio": (credito or {}).get("folio"),
            "limit": None,
            "available": None,
            "currency": "CLP",
            "source": "apro" if credito else "crm",
            "quota_configured": esta_configurado(),
        }
        return {"statusCode": 200, "message": "OK", "data": data}
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except ValueError as e:
        raise BadRequestError(str(e))
    except (BadRequestError, NotFoundError, UnauthorizedError):
        raise
    except Exception:
        logger.exception("Error resolviendo el crédito de la empresa (storefront)")
        raise InternalServerError("Error resolviendo el crédito de la empresa")


# ---------------------------------------------------------------------------
# Storefront público — autogestión de sucursales (theme app extension)
# ---------------------------------------------------------------------------
@app.get("/api/v1/storefront/company/addresses")
@tracer.capture_method
def storefront_list_addresses():
    try:
        require_companies_public_api_key(_headers_dict())
        company_id = _resolve_storefront_company_id(
            _params().get("shopify_company_id", "")
        )
        result = address_service.list_addresses(company_id)
        return {"statusCode": 200, "message": "OK", "data": result}
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except ValueError as e:
        raise BadRequestError(str(e))
    except (BadRequestError, NotFoundError, UnauthorizedError):
        raise
    except Exception:
        logger.exception("Error listando sucursales (storefront)")
        raise InternalServerError("Error listando sucursales")


@app.post("/api/v1/storefront/company/addresses")
@tracer.capture_method
def storefront_create_address():
    try:
        require_companies_public_api_key(_headers_dict())
        body = app.current_event.json_body or {}
        company_id = _resolve_storefront_company_id(
            str(body.get("shopify_company_id") or "")
        )
        result = address_service.create_address(company_id, body)
        return {"statusCode": 201, "message": "Address created", "data": result}
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except ValueError as e:
        raise BadRequestError(str(e))
    except (BadRequestError, NotFoundError, UnauthorizedError):
        raise
    except Exception:
        logger.exception("Error creando sucursal (storefront)")
        raise InternalServerError("Error creando sucursal")


@app.put("/api/v1/storefront/company/addresses/<address_id>")
@tracer.capture_method
def storefront_update_address(address_id: str):
    try:
        require_companies_public_api_key(_headers_dict())
        company_id = _resolve_storefront_company_id(
            _params().get("shopify_company_id", "")
        )
        body = app.current_event.json_body or {}
        if not body:
            raise BadRequestError("Request body is required")
        result = address_service.update_address(company_id, address_id, body)
        return {"statusCode": 200, "message": "Address updated", "data": result}
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except ValueError as e:
        raise BadRequestError(str(e))
    except (BadRequestError, NotFoundError, UnauthorizedError):
        raise
    except Exception:
        logger.exception("Error actualizando sucursal (storefront)")
        raise InternalServerError("Error actualizando sucursal")


@app.delete("/api/v1/storefront/company/addresses/<address_id>")
@tracer.capture_method
def storefront_delete_address(address_id: str):
    try:
        require_companies_public_api_key(_headers_dict())
        company_id = _resolve_storefront_company_id(
            _params().get("shopify_company_id", "")
        )
        address_service.delete_address(company_id, address_id)
        return {"statusCode": 200, "message": "Address deleted"}
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except ValueError as e:
        raise BadRequestError(str(e))
    except (BadRequestError, NotFoundError, UnauthorizedError):
        raise
    except Exception:
        logger.exception("Error eliminando sucursal (storefront)")
        raise InternalServerError("Error eliminando sucursal")


@app.get("/api/v1/storefront/company/contacts")
@tracer.capture_method
def storefront_list_contacts():
    try:
        require_companies_public_api_key(_headers_dict())
        company_id = _resolve_storefront_company_id(
            _params().get("shopify_company_id", "")
        )
        result = contact_service.list_contacts(company_id)
        return {"statusCode": 200, "message": "OK", "data": result}
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except ValueError as e:
        raise BadRequestError(str(e))
    except (BadRequestError, NotFoundError, UnauthorizedError):
        raise
    except Exception:
        logger.exception("Error listando contactos (storefront)")
        raise InternalServerError("Error listando contactos")


@app.post("/api/v1/companies")
@tracer.capture_method
def create_company():
    _require_panel_user()
    try:
        body = app.current_event.json_body or {}
        name = body.get("name")
        company_type = body.get("company_type", "SMALL")
        payment_type = body.get("payment_type", "DIRECT")

        result = company_service.create_company(
            name=name,
            company_type=company_type,
            payment_type=payment_type,
            billing_rut=body.get("billing_rut"),
            billing_razon_social=body.get("billing_razon_social"),
            billing_giro=body.get("billing_giro"),
            billing_region=body.get("billing_region"),
            billing_direccion=body.get("billing_direccion"),
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
    _require_panel_user()
    try:
        params = app.current_event.query_string_parameters or {}
        limit = int(params.get("limit", "50"))
        offset = int(params.get("offset", "0"))
        assigned_user_id = params.get("assigned_user_id") or None
        referred_by_user_id = params.get("referred_by_user_id") or None
        q = params.get("q") or None
        exclude_system = str(params.get("exclude_system", "")).lower() in {
            "1",
            "true",
        }

        result = company_service.list_companies(
            limit=limit,
            offset=offset,
            assigned_user_id=assigned_user_id,
            referred_by_user_id=referred_by_user_id,
            q=q,
            exclude_system=exclude_system,
        )
        return {"statusCode": 200, "message": "Companies retrieved", "data": result}

    except ValueError as e:
        raise BadRequestError(str(e))
    except Exception:
        logger.exception("Error listing companies")
        raise InternalServerError("Error listing companies")


@app.get("/api/v1/companies/<company_id>")
@tracer.capture_method
def get_company(company_id: str):
    _require_panel_user()
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
    _require_panel_user()
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


@app.put("/api/v1/companies/<company_id>/assigned-user")
@tracer.capture_method
def set_company_assigned_user(company_id: str):
    _require_panel_user()
    try:
        body = app.current_event.json_body or {}
        user_id = body.get("user_id") or None

        result = company_service.set_assigned_user(company_id, user_id)
        return {"statusCode": 200, "message": "Assigned user updated", "data": result}

    except ValueError as e:
        raise BadRequestError(str(e))
    except Exception:
        logger.exception("Error setting company assigned user")
        raise InternalServerError("Error setting assigned user")


@app.delete("/api/v1/companies/<company_id>")
@tracer.capture_method
def delete_company(company_id: str):
    _require_panel_user()
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
    _require_panel_user()
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
    _require_panel_user()
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
    _require_panel_user()
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
    _require_panel_user()
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
    _require_panel_user()
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
    _require_panel_user()
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
    _require_panel_user()
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


@app.get("/api/v1/companies/<company_id>/contacts/<client_id>")
@tracer.capture_method
def get_company_contact(company_id: str, client_id: str):
    _require_panel_user()
    try:
        result = contact_service.get_contact(company_id, client_id)
        if not result:
            raise NotFoundError(f"Contact '{client_id}' not found")
        return {"statusCode": 200, "message": "Contact retrieved", "data": result}
    except ValueError as e:
        raise BadRequestError(str(e))
    except NotFoundError:
        raise
    except Exception:
        logger.exception("Error getting company contact")
        raise InternalServerError("Error getting contact")


@app.put("/api/v1/companies/<company_id>/contacts/<client_id>")
@tracer.capture_method
def update_company_contact(company_id: str, client_id: str):
    _require_panel_user()
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
    _require_panel_user()
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
    _require_panel_user()
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
    _require_panel_user()
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
    _require_panel_user()
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

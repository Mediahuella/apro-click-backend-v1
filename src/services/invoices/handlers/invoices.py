"""Consulta de facturas por Orden de Compra — servicio: apro-click-admin-invoices"""
from __future__ import annotations

import sys
from pathlib import Path

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver
from aws_lambda_powertools.event_handler.exceptions import (
    BadRequestError,
    InternalServerError,
    NotFoundError,
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

from services.company_lookup import (
    CompanyNotFoundError,
    MissingBillingRutError,
    get_billing_rut,
)
from services.dms_invoices_client import consultar_facturas_por_ocs
from services.quote_lookup import list_oc_folios
from utils.public_api_key import require_invoices_public_api_key

logger = Logger()
tracer = Tracer()
app = APIGatewayHttpResolver()

MAX_OC_PER_COMPANY = 20


def _headers_dict() -> dict[str, str]:
    raw = app.current_event.headers
    if not raw:
        return {}
    return {str(k): (v if v is not None else "") for k, v in raw.items()}


def _params() -> dict[str, str]:
    return app.current_event.query_string_parameters or {}


@app.get("/api/v1/storefront/invoices")
@tracer.capture_method
def storefront_invoices_list():
    try:
        require_invoices_public_api_key(_headers_dict())

        shopify_company_id = str(_params().get("shopify_company_id") or "").strip()
        if not shopify_company_id:
            raise BadRequestError("shopify_company_id es obligatorio")

        try:
            rut = get_billing_rut(shopify_company_id)
        except CompanyNotFoundError as e:
            raise NotFoundError(str(e))
        except MissingBillingRutError as e:
            raise BadRequestError(str(e))

        ocs = list_oc_folios(shopify_company_id, limit=MAX_OC_PER_COMPANY)
        items = consultar_facturas_por_ocs(ocs, rut)
        # Solo se listan las OC que ya tienen factura en el DMS.
        items = [item for item in items if item["status"] == "found"]

        return {"statusCode": 200, "message": "OK", "data": {"items": items}}
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except (BadRequestError, NotFoundError, UnauthorizedError):
        raise
    except Exception:
        logger.exception("Error listando facturas por OC (storefront)")
        raise InternalServerError("Error listando facturas")


def lambda_handler(event: dict, context: LambdaContext):
    return app.resolve(event, context)

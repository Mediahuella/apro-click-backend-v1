"""Shopify OAuth — inicio (redirect a Shopify) y callback (redirect al conector)."""
from __future__ import annotations

import sys
from pathlib import Path

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver
from aws_lambda_powertools.event_handler.api_gateway import Response
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

from services.shopify_oauth_service import ShopifyOAuthService  # noqa: E402

logger = Logger()
tracer = Tracer()
app = APIGatewayHttpResolver()
oauth_service = ShopifyOAuthService()


def _query_dict() -> dict[str, str]:
    """Query string como dict str->str (HTTP API v2)."""
    raw = app.current_event.query_string_parameters
    if not raw:
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if v is None:
            continue
        if isinstance(v, list):
            out[k] = v[0] if v else ""
        else:
            out[k] = str(v)
    return out


@app.get("/api/v1/shopify/oauth/start")
@tracer.capture_method
def oauth_start():
    """
    Inicia OAuth: redirige al usuario a Shopify /admin/oauth/authorize.

    Query opcional: `shop` — dominio de la tienda (misjoyascl.myshopify.com o misjoyascl).
    Si se omite, se usa la tienda activa en shopify_app_installations.
    """
    try:
        qs = _query_dict()
        shop_qs = qs.get("shop") or qs.get("Shop")
        result = oauth_service.start_oauth(shop_param=shop_qs)
        url = result["authorize_url"]
        return Response(status_code=302, headers={"Location": url}, body="")
    except LookupError as e:
        raise NotFoundError(str(e))
    except ValueError as e:
        raise BadRequestError(str(e))
    except RuntimeError as e:
        logger.error(str(e))
        raise InternalServerError(str(e))
    except Exception:
        logger.exception("Shopify OAuth start failed")
        raise InternalServerError("Shopify OAuth start failed")


@app.get("/api/v1/shopify/oauth/callback")
@tracer.capture_method
def oauth_callback_redirect():
    """
    Callback OAuth: valida HMAC, intercambia code por token, guarda ARN en BD
    (access_token en Secrets Manager) y redirige al conector con ?oauth_status=success.
    """
    try:
        qs = _query_dict()
        location = oauth_service.complete_oauth_callback(qs)
        return Response(status_code=302, headers={"Location": location}, body="")
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except ValueError as e:
        raise BadRequestError(str(e))
    except RuntimeError as e:
        logger.error(str(e))
        raise InternalServerError(str(e))
    except Exception:
        logger.exception("Shopify OAuth callback failed")
        raise InternalServerError("Shopify OAuth callback failed")


def lambda_handler(event: dict, context: LambdaContext):
    return app.resolve(event, context)

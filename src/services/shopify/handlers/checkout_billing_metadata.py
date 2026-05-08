"""POST/OPTIONS `/api/checkout/billing-metadata` — extensión Checkout Manejo Factura."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver
from aws_lambda_powertools.event_handler.api_gateway import Response
from aws_lambda_powertools.event_handler.exceptions import BadRequestError, InternalServerError
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

from services.billing_metadata_service import (  # noqa: E402
    normalize_shop_domain,
    resolve_billing_for_checkout,
)

logger = Logger()
tracer = Tracer()
app = APIGatewayHttpResolver()


def _cors_headers() -> dict[str, str]:
    # Preflight puede pedir Authorization si la extensión aún lo envía; no es obligatorio.
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "authorization, content-type",
    }


def _validate_campaign(campaign: object) -> None:
    if not isinstance(campaign, dict):
        raise BadRequestError("campaign must be an object")
    c = campaign
    if "discount_codes" not in c or "discount_allocations" not in c:
        raise BadRequestError("campaign requires discount_codes and discount_allocations")
    codes = c["discount_codes"]
    allocs = c["discount_allocations"]
    if not isinstance(codes, list) or not isinstance(allocs, list):
        raise BadRequestError("discount_codes and discount_allocations must be arrays")
    for x in codes:
        if not isinstance(x, str):
            raise BadRequestError("each discount_codes item must be a string")
    for alloc in allocs:
        if not isinstance(alloc, dict):
            raise BadRequestError("each discount_allocations item must be an object")
        t = alloc.get("type")
        if t == "code":
            if not isinstance(alloc.get("code"), str):
                raise BadRequestError("allocation type code requires string code")
        elif t in ("automatic", "custom"):
            if not isinstance(alloc.get("title"), str):
                raise BadRequestError(
                    f"allocation type {t} requires string title"
                )
        else:
            raise BadRequestError("invalid discount allocation type")


@app.route("/api/checkout/billing-metadata", method="OPTIONS")
@tracer.capture_method
def billing_metadata_options():
    return Response(
        status_code=204,
        headers=_cors_headers(),
        body="",
    )


@app.post("/api/checkout/billing-metadata")
@tracer.capture_method
def billing_metadata_post():
    try:
        body = app.current_event.json_body
    except json.JSONDecodeError:
        raise BadRequestError("invalid JSON body") from None

    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise BadRequestError("JSON body must be an object")

    shop_raw = body.get("shop")
    if not shop_raw or not isinstance(shop_raw, str):
        raise BadRequestError("shop is required")

    shop_norm = normalize_shop_domain(shop_raw)
    if not shop_norm:
        raise BadRequestError("invalid shop")

    shopify_company_id = body.get("shopify_company_id")
    if not shopify_company_id or not isinstance(shopify_company_id, str):
        raise BadRequestError("shopify_company_id is required")
    shopify_company_id_raw = shopify_company_id.strip()
    if not shopify_company_id_raw:
        raise BadRequestError("shopify_company_id is required")

    campaign = body.get("campaign")
    if campaign is None:
        raise BadRequestError("campaign is required")
    _validate_campaign(campaign)

    checkout_token = body.get("checkout_token")
    if checkout_token is not None and not isinstance(checkout_token, str):
        raise BadRequestError("checkout_token must be a string when present")

    try:
        note_attributes, billing = resolve_billing_for_checkout(
            shop_norm,
            shopify_company_id_raw,
            checkout_token,
            campaign,
        )
        if not isinstance(note_attributes, dict) or not isinstance(billing, dict):
            raise RuntimeError("resolver returned invalid shape")
        for k, v in note_attributes.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise RuntimeError("note_attributes must be string keys and string values")
        for k, v in billing.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise RuntimeError("billing must be string keys and string values")
    except RuntimeError as e:
        logger.error(str(e))
        raise InternalServerError("billing metadata resolution failed") from e
    except Exception:
        logger.exception("billing metadata resolution failed")
        raise InternalServerError("billing metadata resolution failed") from None

    return Response(
        status_code=200,
        content_type="application/json",
        headers=_cors_headers(),
        body=json.dumps(
            {"note_attributes": note_attributes, "billing": billing},
        ),
    )


def lambda_handler(event: dict, context: LambdaContext):
    return app.resolve(event, context)

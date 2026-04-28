"""Shopify OAuth: authorize, callback con exchange + persistencia, redirect al conector."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

_MYSHOPIFY_HOST_RE = re.compile(
    r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?\.myshopify\.com$"
)

from aws_lambda_powertools import Logger
from sqlalchemy import select

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

from database.engine import get_session  # noqa: E402
from database.models.shopify import ShopifyAppInstallation  # noqa: E402

logger = Logger()


def _build_hmac_message(params: dict[str, str]) -> str:
    filtered = {
        k: str(v)
        for k, v in params.items()
        if k not in ("hmac", "signature") and v is not None
    }
    sorted_keys = sorted(filtered.keys())
    return "&".join(f"{k}={filtered[k]}" for k in sorted_keys)


def verify_hmac(params: dict[str, str], client_secret: str) -> bool:
    provided = (params.get("hmac") or "").strip()
    if not provided:
        return False
    message = _build_hmac_message(params)
    digest = hmac.new(
        client_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(digest, provided)


def _verify_timestamp_fresh(params: dict[str, str], max_age_seconds: int = 600) -> None:
    raw = params.get("timestamp")
    if raw is None or str(raw).strip() == "":
        return
    try:
        ts = int(str(raw).strip())
    except ValueError as e:
        raise ValueError("Invalid timestamp") from e
    if ts < 1_000_000_000:
        return
    now = int(time.time())
    if abs(now - ts) > max_age_seconds:
        raise ValueError("OAuth callback timestamp is too old or invalid")


def exchange_code_for_token(
    shop: str,
    code: str,
    client_id: str,
    client_secret: str,
) -> dict[str, Any]:
    url = f"https://{shop}/admin/oauth/access_token"
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
        }
    ).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode() if e.fp else ""
        logger.error(
            "Shopify token exchange failed",
            extra={"status": e.code, "body": err_body[:2000]},
        )
        raise ValueError("Shopify rejected the token exchange") from e


def _normalize_shop_domain(shop: str) -> str:
    s = (shop or "").strip().lower()
    if not s:
        return s
    s = s.removeprefix("https://").removeprefix("http://").split("/")[0]
    if s.endswith(".myshopify.com"):
        return s
    if "." in s:
        raise ValueError(
            "Usa el subdominio de la tienda (ej. mis-tienda) o el host "
            "completo mis-tienda.myshopify.com."
        )
    return f"{s}.myshopify.com"


def _validate_myshopify_host(shop: str) -> None:
    if not shop or not _MYSHOPIFY_HOST_RE.match(shop):
        raise ValueError(
            "Dominio de tienda inválido. Usa el host myshopify.com "
            "(p. ej. mi-tienda.myshopify.com o solo mi-tienda)."
        )


def resolve_shop_domain(shop_param: str | None) -> str:
    """
    Si `shop_param` viene en el request, se usa (normalizado y validado).
    Si no, se toma la instalación activa en shopify_app_installations.
    """
    raw = (shop_param or "").strip()
    if raw:
        shop = _normalize_shop_domain(raw)
        _validate_myshopify_host(shop)
        return shop
    return get_active_shop_domain()


def get_active_shop_domain() -> str:
    """Dominio myshopify.com de la instalación vigente (tabla shopify_app_installations)."""
    with get_session() as session:
        row = session.execute(
            select(ShopifyAppInstallation)
            .where(ShopifyAppInstallation.uninstalled_at.is_(None))
            .order_by(ShopifyAppInstallation.installed_at.desc().nulls_last())
            .limit(1)
        ).scalar_one_or_none()

        if row is None:
            raise LookupError(
                "No hay tienda configurada en shopify_app_installations. "
                "Indica ?shop= en la URL o crea el registro con shop_domain."
            )

        shop = _normalize_shop_domain(row.shop_domain or "")
        if not shop:
            raise LookupError("La instalación Shopify no tiene shop_domain.")
        _validate_myshopify_host(shop)

        return shop


def build_authorize_url(*, shop: str, state: str) -> str:
    client_id = os.environ.get("SHOPIFY_CLIENT_ID", "").strip()
    redirect_uri = os.environ.get("SHOPIFY_OAUTH_REDIRECT_URI", "").strip()
    scopes = os.environ.get("SHOPIFY_OAUTH_SCOPES", "").strip()

    missing = []
    if not client_id:
        missing.append("SHOPIFY_CLIENT_ID")
    if not redirect_uri:
        missing.append("SHOPIFY_OAUTH_REDIRECT_URI")
    if not scopes:
        missing.append("SHOPIFY_OAUTH_SCOPES")
    if missing:
        raise RuntimeError(
            "OAuth no está configurado en el servidor. Faltan variables: "
            + ", ".join(missing)
        )

    shop = _normalize_shop_domain(shop)

    query: dict[str, str] = {
        "client_id": client_id,
        "scope": scopes,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    qs = urllib.parse.urlencode(query)
    return f"https://{shop}/admin/oauth/authorize?{qs}"


def new_oauth_state() -> str:
    return secrets.token_urlsafe(32)


def build_safe_connector_success_url(
    connector_base_url: str,
    *,
    shop: str,
    installation_id: str,
) -> str:
    """Redirige al conector sin `code` ni `hmac` (solo estado no sensible)."""
    base = connector_base_url.strip().rstrip("/")
    if not base:
        raise RuntimeError("SHOPIFY_CONNECTOR_URL no está configurada")
    safe = {
        "shop": shop,
        "oauth_status": "success",
        "installation_id": installation_id,
    }
    qs = urllib.parse.urlencode(safe)
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{qs}"


class ShopifyOAuthService:
    def start_oauth(self, shop_param: str | None = None) -> dict[str, Any]:
        """Construye la URL de authorize (uso desde handler con redirect 302)."""
        shop = resolve_shop_domain(shop_param)
        state = new_oauth_state()
        url = build_authorize_url(shop=shop, state=state)
        logger.info("Shopify OAuth start", extra={"shop": shop})
        return {"authorize_url": url, "shop_domain": shop, "state": state}

    def complete_oauth_callback(self, params: dict[str, str]) -> str:
        """
        Valida HMAC, intercambia code → access_token, persiste `shopify_access_token` en BD,
        devuelve URL del conector para 302 (sin exponer token ni code).
        """
        client_id = os.environ.get("SHOPIFY_CLIENT_ID", "").strip()
        client_secret = os.environ.get("SHOPIFY_CLIENT_SECRET", "").strip()

        if not client_id or not client_secret:
            raise RuntimeError(
                "OAuth incompleto en servidor: configura SHOPIFY_CLIENT_ID y "
                "SHOPIFY_CLIENT_SECRET en la Lambda"
            )

        shop_raw = (params.get("shop") or "").strip()
        code = (params.get("code") or "").strip()
        if not code:
            raise ValueError("Falta el parámetro 'code' en el callback de Shopify")
        if not shop_raw:
            raise ValueError("Falta el parámetro 'shop' en el callback de Shopify")

        shop = _normalize_shop_domain(shop_raw)
        _validate_myshopify_host(shop)

        _verify_timestamp_fresh(params)
        if not verify_hmac(params, client_secret):
            raise PermissionError("HMAC inválido: el callback no es auténtico")

        token_payload = exchange_code_for_token(shop, code, client_id, client_secret)
        access_token = token_payload.get("access_token")
        if not access_token or not isinstance(access_token, str):
            raise ValueError("Shopify no devolvió access_token")

        scope_str = token_payload.get("scope")
        scopes = scope_str if isinstance(scope_str, str) else None

        now = datetime.now(timezone.utc)
        with get_session() as session:
            existing = session.execute(
                select(ShopifyAppInstallation).where(
                    ShopifyAppInstallation.shop_domain == shop
                )
            ).scalar_one_or_none()

            if existing:
                existing.shopify_access_token = access_token
                existing.access_token_secret_id = None
                existing.scopes = scopes
                existing.installed_at = now
                existing.uninstalled_at = None
                session.commit()
                session.refresh(existing)
                inst_id: UUID = existing.id
            else:
                row = ShopifyAppInstallation(
                    shop_domain=shop,
                    shopify_access_token=access_token,
                    access_token_secret_id=None,
                    scopes=scopes,
                    installed_at=now,
                )
                session.add(row)
                session.commit()
                session.refresh(row)
                inst_id = row.id

        logger.info(
            "Shopify OAuth persisted",
            extra={"shop": shop, "installation_id": str(inst_id)},
        )

        connector = os.environ.get("SHOPIFY_CONNECTOR_URL", "").strip()
        return build_safe_connector_success_url(
            connector,
            shop=shop,
            installation_id=str(inst_id),
        )

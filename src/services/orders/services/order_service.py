"""Pedidos Shopify: sync webhook y API de intervención."""
from __future__ import annotations

import re
import sys
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy import and_, desc, select
from sqlalchemy.orm import Session
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
from database.models.company import Company  # noqa: E402
from database.models.shopify import ShopifyAppInstallation  # noqa: E402
from database.models.shopify_order import ShopifyOrder  # noqa: E402

_GID_SHOPIFY_COMPANY = re.compile(r"^gid://shopify/Company/(\d+)$")


def _parse_shopify_ts(raw: str | None) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    s = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _parse_decimal(val: Any) -> Decimal | None:
    if val is None or isinstance(val, (dict, list)):
        return None
    try:
        return Decimal(str(val))
    except (InvalidOperation, ValueError):
        return None


def internal_status_from_financial(financial_status: str | None) -> str:
    fs = (financial_status or "").strip().lower()
    if fs == "paid":
        return "CLOSED"
    return "PENDING"


def _safe_int(val: Any, default: int = 0) -> int:
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        try:
            return int(float(val))
        except (TypeError, ValueError):
            return default


def _coerce_shopify_order_id(raw: Any) -> str:
    if raw is None:
        raise ValueError("Payload de orden inválido: sin id")
    s = str(raw).strip()
    if not s:
        raise ValueError("id de pedido vacío")
    return s


def _line_items_for_storage(order: dict[str, Any]) -> list[dict[str, Any]]:
    raw = order.get("line_items")
    if raw is None:
        raw = order.get("lineItems")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for li in raw:
        if not isinstance(li, dict):
            continue
        pid = li.get("product_id")
        vid = li.get("variant_id")
        lid = li.get("id")
        out.append(
            {
                "id": str(lid) if lid is not None else "",
                "title": (li.get("title") or "")[:500],
                "name": (li.get("name") or "")[:500],
                "quantity": _safe_int(li.get("quantity"), 0),
                "sku": li.get("sku"),
                "variant_title": li.get("variant_title"),
                "product_id": str(pid) if pid is not None else None,
                "variant_id": str(vid) if vid is not None else None,
                "price": str(li.get("price")) if li.get("price") is not None else None,
                "total_discount": str(li.get("total_discount"))
                if li.get("total_discount") is not None
                else None,
                "vendor": li.get("vendor"),
            }
        )
    return out


def _total_from_payload(order: dict[str, Any]) -> Decimal | None:
    v = order.get("current_total_price")
    if v is not None and not isinstance(v, (dict, list)):
        d = _parse_decimal(v)
        if d is not None:
            return d
    v2 = order.get("total_price")
    return _parse_decimal(v2)


def _normalize_b2b_company_id_str(raw: Any) -> str | None:
    """Id numérico B2B de Shopify (Company) alineado con ``companies.shopify_company_id``."""
    if raw is None or isinstance(raw, (dict, list, bool)):
        return None
    if isinstance(raw, int):
        s = str(raw)
        return s if s.isdigit() else None
    s = str(raw).strip()
    if not s:
        return None
    m = _GID_SHOPIFY_COMPANY.match(s)
    if m:
        return m.group(1)
    if s.isdigit():
        return s
    return None


def _shopify_b2b_company_id_from_order_payload(
    order: dict[str, Any]
) -> str | None:
    """
    Intenta leer el id de Company B2B del payload REST/wh (pedido B2B).

    Orden: ``company.id`` (recurso Order), luego ``purchasing_entity`` /
    ``purchasingEntity`` (B2B / GraphQL-embed).
    """
    c = order.get("company")
    if isinstance(c, dict):
        r = _normalize_b2b_company_id_str(c.get("id"))
        if r:
            return r

    for key in ("purchasing_entity", "purchasingEntity"):
        pe = order.get(key)
        if not isinstance(pe, dict):
            continue
        pc = pe.get("PurchasingCompany") or pe.get("purchasing_company")
        if isinstance(pc, dict):
            comp = pc.get("company")
            if isinstance(comp, dict):
                r = _normalize_b2b_company_id_str(comp.get("id"))
                if r:
                    return r
        comp2 = pe.get("company")
        if isinstance(comp2, dict):
            r = _normalize_b2b_company_id_str(comp2.get("id"))
            if r:
                return r
    return None


def _resolve_order_crm_company_id(
    session: Session,
    inst: ShopifyAppInstallation | None,
    order: dict[str, Any],
) -> uuid.UUID | None:
    """
    Prioridad: 1) CRM ``companies`` cuyo ``shopify_company_id`` coincide con el
    B2B del pedido; 2) ``shopify_app_installations.company_id`` de la tienda.
    """
    b2b = _shopify_b2b_company_id_from_order_payload(order)
    if b2b:
        row = session.scalar(
            select(Company).where(Company.shopify_company_id == b2b)
        )
        if row is not None:
            return row.id
    if inst and inst.company_id is not None:
        return inst.company_id
    return None


def upsert_from_shopify_payload(
    shop_domain: str, order: dict[str, Any]
) -> dict[str, Any]:
    if not order or "id" not in order:
        raise ValueError("Payload de orden inválido")

    shopify_order_id = _coerce_shopify_order_id(order["id"])
    soid_str = (order.get("name") or "").strip() or f"#{shopify_order_id}"
    lines = _line_items_for_storage(order)
    fin = order.get("financial_status")
    fin_s = fin if isinstance(fin, str) else None
    internal = internal_status_from_financial(fin_s)
    total = _total_from_payload(order)
    subtotal = _parse_decimal(
        order.get("subtotal_price") or order.get("subtotalPrice")
    )
    email = order.get("email")
    if isinstance(email, str):
        email = email.strip() or None
    else:
        email = None
    fulfill = order.get("fulfillment_status")
    if not isinstance(fulfill, str):
        fulfill = None
    cur = order.get("currency")
    if not isinstance(cur, str):
        cur = None
    updated = _parse_shopify_ts(
        order.get("updated_at") or order.get("updatedAt")
    )

    with get_session() as session:
        inst = session.scalar(
            select(ShopifyAppInstallation).where(
                ShopifyAppInstallation.shop_domain == shop_domain
            )
        )
        company_id = _resolve_order_crm_company_id(session, inst, order)

        row = session.scalar(
            select(ShopifyOrder).where(
                and_(
                    ShopifyOrder.shop_domain == shop_domain,
                    ShopifyOrder.shopify_order_id == shopify_order_id,
                )
            )
        )
        if row is None:
            row = ShopifyOrder(
                id=uuid.uuid4(),
                shopify_order_id=shopify_order_id,
                shop_domain=shop_domain,
                company_id=company_id,
            )
            session.add(row)

        row.order_name = soid_str
        row.email = email
        row.financial_status = fin_s
        row.fulfillment_status = fulfill
        row.currency = cur
        row.subtotal_price = subtotal
        row.total_price = total
        row.internal_status = internal
        row.shopify_updated_at = updated
        # Sincronizar siempre con la instalación: si antes era NULL y ya vinculaste
        # empresa en OAuth, el próximo webhook deja de dejar el pedido fuera del IN (…).
        if company_id:
            row.company_id = company_id
        row.line_items = lines
        session.flush()
        out = row.to_dict()
        session.commit()
    return out


def _scoped_where(user: dict[str, Any]):
    """
    Cláusula SQL de alcance para el listado/lectura vía filas ``shopify_orders``.

    - Plataforma (SUPERADMIN, ADMIN): sin restricción por empresa.
    - Resto: ``shopify_orders.company_id`` debe estar en ``user['order_company_ids']``
      (M2M ``user_companies`` + ``users.company_id`` al adjuntar el contexto), sin
      depender de ``?company_id=`` para el alcance base. Filas con ``shopify_orders.company_id``
      nulos no coinciden (convienen backfill vía instalación / webhook).
    """
    from utils.cognito_order_access import (  # noqa: WPS433
        is_platform_admin,
        order_accessible_company_uuids,
    )

    if is_platform_admin(user):
        return sa.true()
    uuids = order_accessible_company_uuids(user)
    if not uuids:
        return sa.false()
    return ShopifyOrder.company_id.in_(uuids)


def list_orders_for_user(
    user: dict[str, Any],
    *,
    limit: int,
    offset: int,
    status: str | None,
    company_id: str | None,
) -> list[dict[str, Any]]:
    """
    Lista pedidos visibles al usuario autenticado (token Cognito → usuario BD).

    Staff no plataforma: siempre restringido a
    ``shopify_orders.company_id IN (order_company_ids)`` derivado de Cognito
    + ``user_companies``. El query ``company_id`` es **opcional** y solo acota
    a una empresa **dentro** de ese conjunto; no hace falta enviarlo para
    recibir el listado de todas las empresas asignadas.
    """
    from utils.cognito_order_access import (  # noqa: WPS433
        is_platform_admin,
        order_accessible_company_uuids,
    )

    with get_session() as session:
        q = select(ShopifyOrder).where(_scoped_where(user))
        if company_id and str(company_id).strip():
            try:
                cf = uuid.UUID(str(company_id).strip())
            except ValueError:
                return []
            if is_platform_admin(user):
                q = q.where(ShopifyOrder.company_id == cf)
            else:
                if cf not in order_accessible_company_uuids(user):
                    return []
                q = q.where(ShopifyOrder.company_id == cf)
        if status in ("PENDING", "CLOSED"):
            q = q.where(ShopifyOrder.internal_status == status)
        q = q.order_by(
            desc(ShopifyOrder.shopify_updated_at), desc(ShopifyOrder.created_at)
        )
        q = q.offset(offset).limit(min(max(limit, 1), 200))
        rows = list(session.scalars(q).all())
    return [r.to_dict() for r in rows]


def get_order_for_user(user: dict[str, Any], order_id: str) -> dict[str, Any] | None:
    from utils.cognito_order_access import (  # noqa: WPS433
        can_read_order,
        get_order_by_id,
    )

    with get_session() as session:
        row = get_order_by_id(session, order_id)
        if not row or not can_read_order(user, row):
            return None
        return row.to_dict()


def apply_order_updates(
    user: dict[str, Any], order_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    """PATCH unificado: notas CRM (solo PENDING) y/o sincronización con Shopify (Order Edit)."""
    from services.shopify_order_graphql import (  # noqa: WPS433
        apply_order_edits_via_shopify,
        fetch_order_rest,
    )
    from utils.cognito_order_access import (  # noqa: WPS433
        can_read_order,
        can_shopify_edit_order,
        can_write_intervention,
        get_order_by_id,
    )

    wants_notes = "intervention_notes" in body
    raw_li = body.get("shopify_line_items")
    wants_shopify_lines = isinstance(raw_li, list) and len(raw_li) > 0
    raw_sh = body.get("shopify_shipping")
    wants_shopify_ship = (
        isinstance(raw_sh, dict)
        and raw_sh
        and (
            str(raw_sh.get("title") or "").strip() != ""
            or raw_sh.get("price") not in (None, "")
        )
    )
    wants_shopify = wants_shopify_lines or wants_shopify_ship

    if not wants_notes and not wants_shopify:
        raise ValueError("No se indicaron cambios (intervention_notes o shopify_*)")

    uid = uuid.UUID(user["id"])
    shop_domain: str
    shopify_oid: str
    order_currency: str

    with get_session() as session:
        row: ShopifyOrder | None = get_order_by_id(session, order_id)
        if not row:
            raise LookupError("Pedido no encontrado")
        if not can_read_order(user, row):
            raise PermissionError("Sin permiso para ver este pedido")
        if wants_shopify:
            if row.internal_status == "CLOSED":
                raise PermissionError(
                    "Los pedidos cerrados no se pueden editar en Shopify"
                )
            if not can_shopify_edit_order(user, row):
                raise PermissionError("Sin permiso para editar el pedido en Shopify")
        if wants_notes:
            if not can_write_intervention(user, row):
                raise PermissionError(
                    "Sin permiso para notas o el pedido no está pendiente"
                )
            notes = body.get("intervention_notes")
            if notes is not None and not isinstance(notes, str):
                raise ValueError("intervention_notes debe ser texto")
            if notes is not None and len(notes) > 16000:
                raise ValueError("intervention_notes demasiado largo")
            row.intervention_notes = notes.strip() or None

        shop_domain = row.shop_domain
        shopify_oid = row.shopify_order_id
        order_currency = (row.currency or "USD").strip()
        session.commit()

    if wants_shopify:
        with get_session() as session:
            inst = session.scalar(
                select(ShopifyAppInstallation).where(
                    ShopifyAppInstallation.shop_domain == shop_domain
                )
            )
            token = (inst.shopify_access_token if inst else None) or ""
        if not str(token).strip():
            raise ValueError(
                "La tienda no tiene token OAuth; conecta Shopify en el admin"
            )

        staff_raw = body.get("shopify_staff_note")
        staff_note: str | None
        if staff_raw is None or staff_raw == "":
            staff_note = None
        elif isinstance(staff_raw, str):
            staff_note = staff_raw.strip() or None
        else:
            raise ValueError("shopify_staff_note debe ser texto")

        restock = body.get("shopify_restock_on_decrease")
        if restock is None:
            restock_on_decrease = True
        elif isinstance(restock, bool):
            restock_on_decrease = restock
        else:
            raise ValueError("shopify_restock_on_decrease debe ser booleano")

        line_payload: list[dict[str, Any]] | None = None
        if wants_shopify_lines:
            line_payload = []
            for item in raw_li:
                if not isinstance(item, dict):
                    raise ValueError("Cada shopify_line_items[] debe ser un objeto")
                line_payload.append(item)

        ship_payload: dict[str, Any] | None = None
        if wants_shopify_ship:
            ship_payload = raw_sh

        tok = str(token).strip()
        apply_order_edits_via_shopify(
            shop_domain,
            tok,
            shopify_oid,
            line_items=line_payload,
            shipping=ship_payload,
            order_currency=order_currency,
            restock_on_decrease=restock_on_decrease,
            staff_note=staff_note,
        )
        fresh = fetch_order_rest(shop_domain, tok, shopify_oid)
        upsert_from_shopify_payload(shop_domain, fresh)

    with get_session() as session:
        row3 = get_order_by_id(session, order_id)
        if row3:
            row3.last_intervened_by_user_id = uid
            session.commit()

    out = get_order_for_user(user, order_id)
    if not out:
        raise LookupError("Pedido no encontrado")
    return out


def get_line_item_featured_images_for_order(
    user: dict[str, Any], order_id: str
) -> dict[str, str | None] | None:
    """Mapa product_id (str) -> URL imagen destacada. None si pedido inaccesible."""
    from utils.cognito_order_access import (  # noqa: WPS433
        can_read_order,
        get_order_by_id,
    )

    from services.shopify_product_images import (  # noqa: WPS433
        fetch_product_featured_images_bulk,
    )

    with get_session() as session:
        row = get_order_by_id(session, order_id)
        if not row or not can_read_order(user, row):
            return None
        inst = session.scalar(
            select(ShopifyAppInstallation).where(
                ShopifyAppInstallation.shop_domain == row.shop_domain
            )
        )
        token = (inst.shopify_access_token if inst else None) or ""
        if not token.strip():
            return {}
        pids: list[str] = []
        raw_lines = row.line_items
        if isinstance(raw_lines, list):
            for li in raw_lines:
                if isinstance(li, dict) and li.get("product_id"):
                    pids.append(str(li["product_id"]))
        return fetch_product_featured_images_bulk(row.shop_domain, token, pids)

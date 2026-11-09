"""Lógica de negocio de cotizaciones B2B (RFQ).

- ``create_public_request``: el cliente solicita desde el carrito (storefront).
  Crea ``QuoteRequest`` en ``SOLICITADA`` (sin precios) y avisa a SALES.
- Listado / detalle con alcance por empresa (Cognito).
- ``update_quote_lines``: SALES fija precios netos / descuentos por línea.
- Negociación: ``negotiate_draft`` (crea Draft Order, asigna folio, genera PDF,
  avisa al cliente), ``send_invoice``, ``complete``.
"""
from __future__ import annotations

import os
import re
import sys
import unicodedata
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from aws_lambda_powertools import Logger
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

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
from database.models.client import Client  # noqa: E402
from database.models.company import Company  # noqa: E402
from database.models.quote_request import (  # noqa: E402
    QuoteRequest,
    QuoteRequestLine,
)
from database.models.shopify_order import ShopifyOrder  # noqa: E402
from database.models.user import User  # noqa: E402
from database.models.user_company import UserCompany  # noqa: E402
from shopify.store import resolve_store_token  # noqa: E402
from notifications.sqs_publisher import (  # noqa: E402
    encolar_confirmacion_solicitud,
    encolar_cotizacion_apro_ia_estado,
    encolar_cotizacion_lista,
    encolar_nueva_cotizacion,
    encolar_pago_credito_cliente,
    encolar_transferencia_estado,
)
from services import apro_ia_client  # noqa: E402
from services import draft_order_service  # noqa: E402
from services import quote_pdf_service  # noqa: E402
from utils.cognito_quote_access import (  # noqa: E402
    accessible_company_uuids,
    is_platform_admin,
)
from utils.quotes_api_key import sign_pay_token, sign_pdf_token  # noqa: E402

logger = Logger()


def _normalize_variant_gid(value: str) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    if s.startswith("gid://shopify/ProductVariant/"):
        return s
    digits = s.rsplit("/", 1)[-1]
    return f"gid://shopify/ProductVariant/{digits}"


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _public_base_url() -> str:
    return (os.environ.get("QUOTES_PUBLIC_BASE_URL") or "").strip().rstrip("/")


def pdf_email_url(quote_id: str) -> str:
    base = _public_base_url()
    token = sign_pdf_token(str(quote_id))
    suffix = f"/api/v1/quotes/{quote_id}/pdf?token={token}"
    return f"{base}{suffix}" if base else suffix


#: Estados en que la cotización se puede pagar: ya tiene Draft Order
#: negociado y todavía no se convirtió en pedido.
PAYABLE_STATUSES = ("COTIZADA", "INVOICE_SENT")


def pay_url(quote_id: str) -> str:
    """Link «Ir a pagar»: redirige al ``invoiceUrl`` del Draft Order."""
    base = _public_base_url()
    token = sign_pay_token(str(quote_id))
    suffix = f"/api/v1/quotes/{quote_id}/pay?token={token}"
    return f"{base}{suffix}" if base else suffix


def _is_payable(d: dict[str, Any]) -> bool:
    return (
        bool(d.get("draft_order_gid"))
        and not d.get("order_gid")
        and d.get("status") in PAYABLE_STATUSES
    )


def _enrich(d: dict[str, Any]) -> dict[str, Any]:
    """Agrega los links tokenizados: ``pdf_download_url`` si la cotización tiene
    PDF y ``pay_url`` si ya tiene Draft Order y aún no es pedido."""
    if d.get("pdf_s3_key"):
        d["pdf_download_url"] = pdf_email_url(str(d.get("id")))
    if _is_payable(d) and sign_pay_token(str(d.get("id"))):
        d["pay_url"] = pay_url(str(d.get("id")))
    return d


def _registrar_pedido(quote: QuoteRequest, order: dict[str, Any]) -> bool:
    """Enlaza el pedido que Shopify generó desde el Draft Order de la cotización.

    El webhook ``orders/create`` de ``orders`` no puede hacerlo: el payload REST
    del pedido no trae el id del Draft Order de origen. Por eso, cuando el
    cliente paga el link de la cotización, la única forma de saberlo es
    preguntarle al Draft Order (``status: COMPLETED`` + ``order``).
    Devuelve ``True`` si cambió algo.
    """
    order_gid = str(order.get("id") or "").strip()
    if not order_gid:
        return False
    cambio = False
    if quote.order_gid != order_gid:
        quote.order_gid = order_gid
        cambio = True
    if quote.status in PAYABLE_STATUSES:
        quote.status = "ACEPTADA"
        cambio = True
    return cambio


def sync_completed_drafts(session: Session, quotes: list[QuoteRequest]) -> dict[str, dict]:
    """Detecta las cotizaciones cuyo Draft Order ya se convirtió en pedido.

    Solo consulta las que siguen «pagables» en la BD (draft sin pedido), en
    una llamada ``nodes`` por tienda. Persiste ``order_gid`` + ``ACEPTADA`` en
    la sesión (el commit es del llamador) y devuelve ``{order_gid: order}``
    con lo que informó Shopify, para mostrar el pago aunque el webhook del
    pedido todavía no haya llegado a ``shopify_orders``. Best-effort: si
    Shopify falla, se registra y la cotización queda como estaba.
    """
    por_tienda: dict[str, list[QuoteRequest]] = {}
    for q in quotes:
        if q.draft_order_gid and not q.order_gid and q.status in PAYABLE_STATUSES:
            por_tienda.setdefault(q.shop_domain, []).append(q)
    pedidos: dict[str, dict] = {}
    for shop_domain, grupo in por_tienda.items():
        try:
            estados = draft_order_service.fetch_drafts_state(
                session, shop_domain, [q.draft_order_gid for q in grupo]
            )
        except Exception:
            logger.exception(
                "No se pudo consultar el estado de los Draft Orders",
                extra={"shop_domain": shop_domain, "drafts": len(grupo)},
            )
            continue
        for q in grupo:
            order = (estados.get(q.draft_order_gid) or {}).get("order") or {}
            if order.get("id") and _registrar_pedido(q, order):
                pedidos[order["id"]] = order
                logger.info(
                    "Cotización enlazada al pedido de su Draft Order",
                    extra={"quote_id": str(q.id), "order": order.get("name")},
                )
    return pedidos


#: ``financial_status`` de Shopify (REST, minúsculas) → estado de pago que ve
#: el cliente. Lo que no está aquí se informa tal cual.
_PAYMENT_STATES = {
    "paid": "PAGADA",
    "partially_refunded": "PAGADA",
    "pending": "PENDIENTE",
    "authorized": "PENDIENTE",
    "partially_paid": "PENDIENTE",
    "refunded": "REEMBOLSADA",
    "voided": "ANULADA",
}


def _attach_order(
    session: Session, quotes: list[dict[str, Any]], desde_shopify: dict[str, dict] | None = None
) -> None:
    """Agrega ``order`` (``{id, name, financial_status, payment_state}``) a las
    cotizaciones que ya son pedido.

    Fuente: ``shopify_orders`` (al día por los webhooks ``orders/*``); si el
    pedido aún no llegó ahí, lo que devolvió Shopify en ``sync_completed_drafts``.
    """
    desde_shopify = desde_shopify or {}
    ids = {
        _gid_tail(d.get("order_gid")): d
        for d in quotes
        if d.get("order_gid")
    }
    if not ids:
        return
    filas = {
        str(r.shopify_order_id): r
        for r in session.scalars(
            select(ShopifyOrder).where(ShopifyOrder.shopify_order_id.in_(list(ids)))
        )
    }
    for order_id, d in ids.items():
        fila = filas.get(order_id)
        if fila is not None:
            name = fila.order_name or None
            financial = (fila.financial_status or "").lower() or None
        else:
            remoto = desde_shopify.get(d["order_gid"]) or {}
            name = remoto.get("name")
            financial = (remoto.get("displayFinancialStatus") or "").lower() or None
        d["order"] = {
            "id": order_id,
            "name": name,
            "financial_status": financial,
            "payment_state": _PAYMENT_STATES.get(financial or "", (financial or "").upper() or None),
        }


def _attach_seller(session: Session, quotes: list[dict[str, Any]]) -> None:
    """Añade ``assigned_sales_user`` (vendedor de la empresa) a cada cotización.

    Usa la misma resolución canónica que el PDF y APRO IA: primero
    ``Company.referred_by_user_id`` (empresa registrada con el link de invitación
    de un vendedor) y, si no hay, el SALES de cartera en ``user_companies``.
    Resuelto en batch para no pegarle N veces a la BD en el listado.
    """
    for q in quotes:
        q["assigned_sales_user"] = None
    company_ids = {
        uuid.UUID(str(q["company_id"])) for q in quotes if q.get("company_id")
    }
    if not company_ids:
        return

    # 1) Vendedor referente por empresa (link de invitación).
    referred_by_company: dict[str, uuid.UUID] = {}
    for cid, ref_id in session.execute(
        select(Company.id, Company.referred_by_user_id).where(
            Company.id.in_(company_ids), Company.referred_by_user_id.is_not(None)
        )
    ).all():
        referred_by_company[str(cid)] = ref_id

    # 2) Vendedor de cartera (fallback) para las empresas sin referente.
    fallback_ids = [
        cid for cid in company_ids if str(cid) not in referred_by_company
    ]
    sales_by_company: dict[str, uuid.UUID] = {}
    if fallback_ids:
        for cid, uid in session.execute(
            select(UserCompany.company_id, User.id)
            .join(User, User.id == UserCompany.user_id)
            .where(
                UserCompany.company_id.in_(fallback_ids),
                User.role == "SALES",
                User.status == "ACTIVE",
            )
            .order_by(UserCompany.company_id, User.id)
        ).all():
            sales_by_company.setdefault(str(cid), uid)

    user_ids = set(referred_by_company.values()) | set(sales_by_company.values())
    if not user_ids:
        return
    users = {
        u.id: {
            "id": str(u.id),
            "given_name": u.given_name,
            "family_name": u.family_name,
            "email": u.email,
        }
        for u in session.scalars(select(User).where(User.id.in_(user_ids))).all()
    }
    for q in quotes:
        key = str(q.get("company_id") or "")
        uid = referred_by_company.get(key) or sales_by_company.get(key)
        if uid:
            q["assigned_sales_user"] = users.get(uid)


def _gid_tail(value: str | None) -> str:
    """``gid://shopify/Customer/123`` y ``123`` comparan igual."""
    return str(value or "").strip().rsplit("/", 1)[-1]


def _attach_contact(session: Session, quotes: list[dict[str, Any]]) -> None:
    """Añade ``company_rut`` y ``contact`` ({name, email, phone}) a cada cotización.

    El contacto es el ``Client`` de la empresa que pidió la cotización (por
    email o por customer de Shopify); si no está registrado como contacto
    —cotización creada desde APRO IA, email cambiado— cae al contacto
    principal o, si la empresa no tiene uno marcado, el primer contacto
    con teléfono. El teléfono solo existe en ``Client``: la cotización guarda
    nombre y email del solicitante, no su teléfono.
    """
    for q in quotes:
        q["company_rut"] = None
        q["contact"] = None
    company_ids = {
        uuid.UUID(str(q["company_id"])) for q in quotes if q.get("company_id")
    }
    if not company_ids:
        return

    ruts = dict(
        session.execute(
            select(Company.id, Company.billing_rut).where(Company.id.in_(company_ids))
        ).all()
    )
    clients_by_company: dict[str, list[Client]] = {}
    for c in session.scalars(
        select(Client)
        .where(Client.company_id.in_(company_ids))
        .order_by(Client.is_main_contact.desc(), Client.created_at)
    ).all():
        clients_by_company.setdefault(str(c.company_id), []).append(c)

    for q in quotes:
        cid = str(q.get("company_id") or "")
        if not cid:
            continue
        q["company_rut"] = ruts.get(uuid.UUID(cid))
        clients = clients_by_company.get(cid, [])
        email = (q.get("requested_by_email") or "").strip().lower()
        customer = _gid_tail(q.get("requested_by_customer_id"))
        match = next(
            (c for c in clients if email and (c.email or "").strip().lower() == email),
            None,
        ) or next(
            (
                c
                for c in clients
                if customer and _gid_tail(c.shopify_customer_id) == customer
            ),
            None,
        )
        # Las cotizaciones creadas desde APRO IA no traen solicitante y hay
        # empresas sin contacto principal marcado: en ese caso sirve cualquier
        # contacto, mejor si tiene teléfono.
        if match is None:
            match = (
                next((c for c in clients if c.is_main_contact), None)
                or next((c for c in clients if (c.phone or "").strip()), None)
                or next(iter(clients), None)
            )
        if match is not None:
            q["contact"] = {
                "name": match.name,
                "email": match.email,
                "phone": match.phone,
                "is_main_contact": bool(match.is_main_contact),
            }


def _vendedor_name(session: Session, company: Company | None) -> str:
    if company and company.referred_by_user_id:
        u = session.get(User, company.referred_by_user_id)
        if u:
            return f"{u.given_name} {u.family_name}".strip()
    return ""


def _admin_emails(session: Session, company: Company | None) -> list[str]:
    emails: list[str] = []
    if company and company.referred_by_user_id:
        u = session.get(User, company.referred_by_user_id)
        if u and u.email and u.status == "ACTIVE":
            emails.append(u.email)
    admins = session.scalars(
        select(User).where(
            User.role.in_(("SUPERADMIN", "ADMIN")),
            User.status == "ACTIVE",
        )
    ).all()
    for a in admins:
        if a.email and a.email not in emails:
            emails.append(a.email)
    return emails


# ---------------------------------------------------------------------------
# Público (storefront)
# ---------------------------------------------------------------------------
def create_public_request(shop_domain: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Crea una solicitud de cotización desde el carrito. Estado ``SOLICITADA``."""
    shop = (shop_domain or "").strip().lower()
    if not shop:
        raise ValueError("shop_domain es obligatorio")
    shopify_company_id = str(payload.get("shopify_company_id") or "").strip()
    if not shopify_company_id:
        raise ValueError("Se requiere una empresa B2B (shopify_company_id)")

    raw_lines = payload.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        raise ValueError("La cotización debe tener al menos una línea")

    with get_session() as session:
        company = session.scalar(
            select(Company).where(Company.shopify_company_id == shopify_company_id)
        )
        raw_shipping = payload.get("shipping_line")
        shipping_line = None
        if isinstance(raw_shipping, dict) and raw_shipping.get("title") and raw_shipping.get("price") is not None:
            try:
                shipping_line = {
                    "title": str(raw_shipping["title"]).strip(),
                    "price": str(Decimal(str(raw_shipping["price"]))),
                }
            except (InvalidOperation, ValueError):
                shipping_line = None

        raw_cart_token = str(payload.get("cart_token") or "").strip()
        cart_token = raw_cart_token[:64] if raw_cart_token else None

        quote = QuoteRequest(
            company_id=company.id if company else None,
            shopify_company_id=shopify_company_id,
            shop_domain=shop,
            requested_by_email=(str(payload.get("customer_email") or "").strip() or None),
            requested_by_customer_id=(str(payload.get("customer_id") or "").strip() or None),
            requested_by_name=(str(payload.get("customer_name") or "").strip() or None),
            comment=(str(payload.get("comment") or "").strip() or None),
            cart_token=cart_token,
            shipping_line=shipping_line,
            status="SOLICITADA",
        )
        for i, rl in enumerate(raw_lines, start=1):
            if not isinstance(rl, dict):
                continue
            variant_gid = _normalize_variant_gid(rl.get("variant_id") or rl.get("variantId"))
            if not variant_gid:
                continue
            try:
                qty = int(rl.get("quantity") or rl.get("qty") or 1)
            except (TypeError, ValueError):
                qty = 1
            quote.lines.append(
                QuoteRequestLine(
                    line_number=i,
                    variant_gid=variant_gid,
                    sku=(str(rl.get("sku")).strip() if rl.get("sku") else None),
                    title=(str(rl.get("title")).strip() if rl.get("title") else None),
                    quantity=max(1, qty),
                    on_demand=bool(rl.get("on_demand") or rl.get("onDemand")),
                    requested_unit_price=_to_decimal(rl.get("price")),
                )
            )
        if not quote.lines:
            raise ValueError("Ninguna línea válida en la solicitud")
        session.add(quote)
        session.flush()
        quote_id = str(quote.id)
        company_name = (company.name if company else None) or quote.requested_by_name or shopify_company_id
        line_count = len(quote.lines)
        admin_emails = _admin_emails(session, company)
        contact_name = quote.requested_by_name
        contact_email = quote.requested_by_email
        lines_summary = [
            {"title": ln.title, "sku": ln.sku, "quantity": ln.quantity}
            for ln in quote.lines
        ]
        # get_session() NO hace commit automático: sin esto la transacción se
        # descarta al cerrar la sesión y la cotización nunca se persiste.
        session.commit()

    # Notificar a SALES siempre
    encolar_nueva_cotizacion(
        admin_emails=admin_emails,
        company_name=company_name,
        quote_id=quote_id,
        line_count=line_count,
        contact_name=contact_name,
        contact_email=contact_email,
    )

    # Auto-proceso:
    #  1. Sincronizar con APRO IA (crea la cotización allá y descarga el PDF real)
    #  2. Crear Draft Order en Shopify
    #  3. Asignar folio interno
    # Si APRO IA falla cae a reportlab; en ese caso NO se envía el PDF al cliente
    # y SALES procesa la cotización manualmente via negotiate_draft.
    final_status = "SOLICITADA"
    folio = None
    total_str = None
    apro_ia_ok = False
    try:
        with get_session() as session:
            quote = session.get(QuoteRequest, uuid.UUID(quote_id))
            if quote:
                seller_email = _seller_email(session, quote.company)
                _sync_apro_ia_and_pdf(session, quote, seller_email=seller_email)
                apro_ia_ok = bool(quote.apro_ia_folio)
                draft_order_service.create_quote_draft(session, quote)
                _assign_folio(session, quote)
                quote.status = "COTIZADA"
                session.flush()
                final_status = "COTIZADA"
                folio = quote.folio
                _, _, _, total = quote_pdf_service.quote_totals(quote)
                total_str = quote_pdf_service._fmt_clp(total)
                session.commit()
    except Exception:
        logger.exception(
            "Auto-proceso de cotización falló — queda en SOLICITADA para proceso manual",
            extra={"quote_id": quote_id},
        )

    # Si APRO IA entregó el PDF → enviar cotización al cliente con el link real.
    # Si APRO IA falló → solo confirmación de recepción; SALES reintentará via negotiate_draft.
    if contact_email:
        if folio and apro_ia_ok:
            encolar_cotizacion_lista(
                contact_email=contact_email,
                contact_name=contact_name,
                company_name=company_name,
                folio=folio,
                pdf_url=pdf_email_url(quote_id),
                total=total_str,
            )
        else:
            encolar_confirmacion_solicitud(
                contact_email=contact_email,
                contact_name=contact_name,
                company_name=company_name,
                quote_id=quote_id,
                lines=lines_summary,
            )

    return {"id": quote_id, "status": final_status, "lines": line_count}


# ---------------------------------------------------------------------------
# Lectura (panel + storefront acotado)
# ---------------------------------------------------------------------------
def _scope_filter(stmt, *, user: dict | None, shopify_company_id: str | None):
    if user is not None and not is_platform_admin(user):
        ids = accessible_company_uuids(user)
        stmt = stmt.where(QuoteRequest.company_id.in_(ids or [uuid.uuid4()]))
    if shopify_company_id:
        stmt = stmt.where(QuoteRequest.shopify_company_id == shopify_company_id)
    return stmt


def _searchable(expr):
    """Columna lista para comparar: minúsculas, sin acentos y sin espacios dobles."""
    return func.regexp_replace(
        func.unaccent(func.lower(func.coalesce(expr, ""))), r"\s+", " ", "g"
    )


def _normalize_term(value: str) -> str:
    """Mismo tratamiento del lado del término buscado."""
    folded = unicodedata.normalize("NFKD", value or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", folded).strip().lower()


def _search_filter(stmt, term: str):
    """Filtra por folio, folio APRO IA, empresa o solicitante.

    Va en el servidor: el panel filtraba en cliente sobre la página cargada
    (100 filas) y las cotizaciones más antiguas eran inbuscables por folio
    aunque existieran. Los espacios repetidos se colapsan a ambos lados para
    que un nombre pegado con doble espacio igual matchee.
    """
    like = f"%{term}%"

    def _like(column):
        return _searchable(column).like(like)

    company_match = select(Company.id).where(
        _searchable(Company.name).like(like),
        Company.id == QuoteRequest.company_id,
    )
    return stmt.where(
        or_(
            _like(QuoteRequest.folio),
            _like(QuoteRequest.apro_ia_folio),
            _like(QuoteRequest.apro_ia_qa_folio),
            _like(QuoteRequest.transfer_folio),
            _like(QuoteRequest.requested_by_name),
            _like(QuoteRequest.requested_by_email),
            company_match.exists(),
        )
    )


def list_quotes(
    *,
    user: dict | None = None,
    shopify_company_id: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    term = _normalize_term(q or "")
    with get_session() as session:
        # ``company`` se eager-loadea porque ``to_dict()`` expone
        # ``company_name``; sin esto sería una consulta extra por fila.
        stmt = select(QuoteRequest).options(
            selectinload(QuoteRequest.lines), selectinload(QuoteRequest.company)
        )
        stmt = _scope_filter(stmt, user=user, shopify_company_id=shopify_company_id)
        if status:
            stmt = stmt.where(QuoteRequest.status == status)
        if term:
            stmt = _search_filter(stmt, term)
        count_stmt = _scope_filter(
            select(func.count(QuoteRequest.id)),
            user=user,
            shopify_company_id=shopify_company_id,
        )
        if status:
            count_stmt = count_stmt.where(QuoteRequest.status == status)
        if term:
            count_stmt = _search_filter(count_stmt, term)
        total = session.scalar(count_stmt) or 0
        rows = session.scalars(
            stmt.order_by(QuoteRequest.created_at.desc()).limit(limit).offset(offset)
        ).all()
        pedidos = sync_completed_drafts(session, list(rows))
        if pedidos:
            session.flush()
        items = [_enrich(r.to_dict()) for r in rows]
        _attach_seller(session, items)
        _attach_contact(session, items)
        _attach_order(session, items, pedidos)
        if pedidos:
            session.commit()
        return {"total": total, "items": items}


def get_quote(
    quote_id: str,
    *,
    user: dict | None = None,
    shopify_company_id: str | None = None,
    sync_order: bool = True,
) -> dict[str, Any] | None:
    """Detalle de la cotización dentro del alcance de ``user``/``shopify_company_id``.

    ``sync_order=False`` evita consultar el Draft Order en Shopify: para los
    chequeos de alcance, que solo necesitan saber si la cotización es visible.
    """
    try:
        qid = uuid.UUID(quote_id)
    except ValueError:
        return None
    with get_session() as session:
        quote = session.get(QuoteRequest, qid)
        if not quote:
            return None
        if user is not None and not is_platform_admin(user):
            if quote.company_id not in accessible_company_uuids(user):
                return None
        if shopify_company_id and quote.shopify_company_id != shopify_company_id:
            return None
        pedidos = sync_completed_drafts(session, [quote]) if sync_order else {}
        if pedidos:
            session.flush()
        data = _enrich(quote.to_dict())
        _attach_seller(session, [data])
        _attach_contact(session, [data])
        _attach_order(session, [data], pedidos)
        if pedidos:
            session.commit()
        return data


# ---------------------------------------------------------------------------
# Webhook APRO IA — sync de estado (ver docs/Webhook eventos.md)
# ---------------------------------------------------------------------------
APRO_IA_WEBHOOK_EVENTS = (
    "quotation.created",
    "quotation.updated",
    "quotation.approved",
    "quotation.rejected",
)


def _normalize_rut(value: str | None) -> str:
    """Deja solo dígitos y el dígito verificador ``K`` (sin puntos ni guión).

    APRO IA envía el RUT del webhook sin separadores (``762175053``) mientras
    que ``Company.billing_rut`` se guarda con guión (``76217505-3``) — sin
    normalizar, una comparación exacta no matchea aunque la empresa exista
    (caso real detectado: folio CO-076039).
    """
    return re.sub(r"[^0-9kK]", "", value or "").upper()


# ---------------------------------------------------------------------------
# Ambiente QA de APRO IA
# ---------------------------------------------------------------------------
#: APRO IA mantiene un DMS de pruebas (``dms-qa.apro.cl``) con credenciales
#: propias. Solo las empresas listadas acá participan de ese ambiente: el
#: espejo de salida se hace únicamente para ellas y los webhooks que entran por
#: la ruta ``/qa`` se descartan si el RUT no está en la lista. Sin esto, una
#: prueba en QA podría crear Draft Orders o marcar pedidos pagados en la tienda
#: de producción de un cliente real.
def _qa_allowed_ruts() -> set[str]:
    raw = os.environ.get("APRO_IA_QA_ALLOWED_RUTS") or ""
    return {r for r in (_normalize_rut(x) for x in raw.split(",")) if r}


def _qa_habilita_rut(rut: str | None) -> bool:
    """``True`` si esa empresa está habilitada para el ambiente QA de APRO IA."""
    normalizado = _normalize_rut(rut)
    return bool(normalizado) and normalizado in _qa_allowed_ruts()


def _find_company_by_rut(session: Session, rut: str) -> Company | None:
    normalized = _normalize_rut(rut)
    if not normalized:
        return None
    for company in session.scalars(
        select(Company).where(Company.billing_rut.isnot(None))
    ).all():
        if _normalize_rut(company.billing_rut) == normalized:
            return company
    return None


def _unique_own_folio(session: Session, folio: str, apro_ia_id: str) -> str:
    """El folio de APRO IA no es único entre cotizaciones (caso real: folio
    ``AC-000007`` reusado por dos cotizaciones de clientes distintos), pero
    nuestro ``QuoteRequest.folio`` sí tiene constraint ``unique`` — si el
    folio de APRO IA ya está tomado por otra cotización, se desambigua con
    un sufijo del ``apro_ia_id`` para no romper el insert.
    """
    taken = session.scalars(
        select(QuoteRequest.id).where(QuoteRequest.folio == folio)
    ).first()
    if not taken:
        return folio
    suffix = (apro_ia_id or uuid.uuid4().hex)[-6:]
    return f"{folio}-{suffix}"


def _create_quote_from_apro_ia_payload(
    session: Session,
    payload: dict[str, Any],
    *,
    folio: str,
    apro_ia_id: str,
    status: str,
) -> QuoteRequest | None:
    """Crea una ``QuoteRequest`` para una cotización originada directamente en
    APRO IA (``quotation.created`` sin contraparte en nuestra BD).

    Reglas:

    - La empresa debe existir en nuestro sistema (``Company.billing_rut`` vs
      ``client.rut``, comparados normalizados — ver ``_normalize_rut``) y
      tener ``shopify_company_id`` — sin eso no se puede resolver la
      CompanyLocation/contacto que exige Shopify para el Draft Order B2B. Si
      no existe, se omite la creación y se avisa a los admins para revisión
      manual (no se inventa una empresa).
    - Cada producto se resuelve por SKU contra el catálogo de Shopify
      (``resolve_variant_by_sku``); los SKU que no matchean se descartan con
      un warning en vez de abortar toda la cotización.
    - El folio y el ``id`` los define APRO IA (ya es la fuente de verdad de
      esta cotización) — no se genera folio propio ni se vuelve a crear la
      cotización del lado de APRO IA (ya existe allá).
    - Fecha: APRO IA no incluye una fecha de emisión en el payload del
      webhook; se usa ``created_at`` (server default) como fecha de ingreso
      a Apro Click y ``validity_days`` por defecto (14) para la vigencia.
    - Si la empresa y los SKUs resuelven, se intenta crear el Draft Order de
      inmediato para que el cliente pueda seguir el flujo de venta
      (``negotiate_draft`` desde el panel sirve de reintento manual si esto
      falla — no vuelve a golpear APRO IA, solo Shopify).
    """
    client = payload.get("client") or {}
    client_rut = str(client.get("rut") or "").strip()
    client_name = str(client.get("name") or "").strip()
    products = payload.get("products") or []

    if not client_rut or not isinstance(products, list) or not products:
        logger.warning(
            "Webhook APRO IA quotation.created: payload sin client.rut o "
            "products — no se puede crear la cotización",
            extra={"folio": folio, "apro_ia_id": apro_ia_id},
        )
        return None

    company = _find_company_by_rut(session, client_rut)
    if not company or not company.shopify_company_id:
        logger.warning(
            "Webhook APRO IA quotation.created: empresa no encontrada (o sin "
            "Shopify Company) para el RUT del cliente — se omite creación "
            "automática, requiere alta manual de la empresa",
            extra={"folio": folio, "apro_ia_id": apro_ia_id, "rut": client_rut},
        )
        encolar_cotizacion_apro_ia_estado(
            admin_emails=_admin_emails(session, None),
            company_name=client_name or client_rut,
            folio=folio,
            quote_id="",
            event="quotation.created",
            status=(
                f"{status or 'Pendiente'} — empresa RUT {client_rut} no existe en "
                "Apro Click, no se pudo crear el Draft Order automáticamente"
            ),
        )
        return None

    try:
        shop_domain, token = resolve_store_token(session)
    except Exception:
        logger.exception(
            "Webhook APRO IA quotation.created: no se pudo resolver la tienda "
            "Shopify activa",
            extra={"folio": folio},
        )
        return None

    quote = QuoteRequest(
        company_id=company.id,
        shopify_company_id=company.shopify_company_id,
        shop_domain=shop_domain,
        requested_by_name=client_name or company.name,
        comment=(str(payload.get("comment") or "").strip() or None),
        status="SOLICITADA",
        apro_ia_id=apro_ia_id or None,
        apro_ia_folio=folio or None,
        apro_ia_status=status or None,
        folio=_unique_own_folio(session, folio, apro_ia_id) if folio else None,
        shipping_line=_shipping_from_apro_ia(products),
    )
    line_no = 1
    for p in products:
        if not isinstance(p, dict):
            continue
        sku = str(p.get("sku") or "").strip()
        if not sku or apro_ia_client.is_shipping_sku(sku):
            continue
        variant = draft_order_service.resolve_variant_by_sku(shop_domain, token, sku)
        if not variant:
            logger.warning(
                "Webhook APRO IA quotation.created: SKU no encontrado en "
                "Shopify — se omite la línea",
                extra={"folio": folio, "sku": sku},
            )
            continue
        title = (
            variant.get("displayName")
            or str(p.get("name") or "").strip()
            or sku
        )
        try:
            qty = max(1, int(p.get("amount") or 1))
        except (TypeError, ValueError):
            qty = 1
        quote.lines.append(
            QuoteRequestLine(
                line_number=line_no,
                variant_gid=variant["id"],
                sku=variant.get("sku") or sku,
                title=title,
                quantity=qty,
                on_demand=False,
                negotiated_unit_price=_to_decimal(p.get("price")),
                comment=_line_comment(_apro_ia_product_comment(p)),
                currency="CLP",
            )
        )
        line_no += 1

    if not quote.lines:
        logger.warning(
            "Webhook APRO IA quotation.created: ningún SKU pudo resolverse en "
            "Shopify — no se crea la cotización",
            extra={"folio": folio},
        )
        return None

    session.add(quote)
    session.flush()

    if quote.apro_ia_folio:
        try:
            pdf_bytes = apro_ia_client.descargar_pdf(quote.apro_ia_folio)
            quote_pdf_service.store_quote_pdf(quote, pdf_bytes)
        except Exception:
            logger.exception(
                "Webhook APRO IA quotation.created: no se pudo descargar el "
                "PDF de APRO IA",
                extra={"folio": folio, "quote_id": str(quote.id)},
            )

    try:
        draft_order_service.create_quote_draft(session, quote)
        quote.status = "COTIZADA"
    except Exception:
        logger.exception(
            "Webhook APRO IA quotation.created: cotización creada pero falló "
            "el Draft Order en Shopify — queda en SOLICITADA para reintento "
            "manual (botón 'crear draft' del panel)",
            extra={"folio": folio, "quote_id": str(quote.id)},
        )

    return quote


def _shipping_from_apro_ia(products: Any) -> dict | None:
    """El envío que trae ``products[]`` (producto ``COSTO DE ENVIO``), o ``None``.

    Devuelve ``{title, price}`` con el formato de ``QuoteRequest.shipping_line``.
    El título es el que mandamos (``COSTO DE ENVIO - <título>``) sin el prefijo.
    """
    if not isinstance(products, list):
        return None
    for p in products:
        if not isinstance(p, dict) or not apro_ia_client.is_shipping_sku(p.get("sku")):
            continue
        price = _to_decimal(p.get("price"))
        if price is None:
            return None
        name = str(p.get("name") or "").strip()
        prefix = f"{apro_ia_client.SHIPPING_NAME} - "
        title = name[len(prefix):].strip() if name.upper().startswith(prefix) else ""
        return {"title": title or "Envío", "price": str(price)}
    return None


def _sync_shipping_from_apro_ia(quote: QuoteRequest, products: list[Any]) -> bool:
    """Aplica el precio del envío que viene de APRO IA. ``True`` si cambió.

    Si APRO IA no trae el producto de envío no se toca ``shipping_line``: las
    cotizaciones anteriores a que el envío viajara como línea no lo tienen
    allá, y borrarlo acá dejaría el Draft Order sin despacho.
    """
    incoming = _shipping_from_apro_ia(products)
    if incoming is None:
        return False
    current = quote.shipping_line if isinstance(quote.shipping_line, dict) else None
    if current and current.get("title"):
        # Se conserva el título que eligió el cliente o SALES (la tarifa de
        # Shopify); de APRO IA solo manda el precio.
        if _to_decimal(current.get("price")) == _to_decimal(incoming["price"]):
            return False
        quote.shipping_line = {**current, "price": incoming["price"]}
        return True
    quote.shipping_line = incoming
    return True


def _sync_lines_from_apro_ia_products(
    session: Session, quote: QuoteRequest, products: list[Any]
) -> bool:
    """Sincroniza cantidad/precio/líneas de ``quote`` con ``products[]`` del
    webhook APRO IA. APRO IA es la fuente de verdad del precio/cantidad
    negociado una vez que el Gerente Comercial edita o aprueba la cotización
    — sin esto, el Draft Order en Shopify queda desactualizado respecto a lo
    realmente autorizado (caso real: folio CO-076041, cantidad 1→5 y precio
    12000→20000 nunca llegaron a la línea ni al draft).

    - SKU existente en ``quote.lines``: actualiza cantidad/precio/título.
    - SKU nuevo (agregado en APRO IA): se resuelve contra Shopify y se agrega
      como línea nueva; si no matchea ningún variant, se omite con warning.
    - SKU que ya no viene en ``products``: se elimina la línea (APRO IA quitó
      ese producto de la cotización).

    Devuelve ``True`` si hubo algún cambio (para decidir si vale la pena
    reflejarlo en el Draft Order de Shopify).
    """
    if not isinstance(products, list) or not products:
        return False

    changed = _sync_shipping_from_apro_ia(quote, products)
    by_sku = {(ln.sku or "").strip().lower(): ln for ln in quote.lines}
    seen_skus: set[str] = set()
    shop_domain: str | None = None
    token: str | None = None
    next_num = max((ln.line_number for ln in quote.lines), default=0) + 1

    for p in products:
        if not isinstance(p, dict):
            continue
        sku = str(p.get("sku") or "").strip()
        if not sku or apro_ia_client.is_shipping_sku(sku):
            continue
        sku_key = sku.lower()
        seen_skus.add(sku_key)
        price = _to_decimal(p.get("price"))
        try:
            qty = max(1, int(p.get("amount") or 1))
        except (TypeError, ValueError):
            qty = 1
        title = str(p.get("name") or "").strip() or None

        existing = by_sku.get(sku_key)
        if existing:
            # El comentario no va al Draft Order: se guarda sin marcar
            # ``changed`` para no reescribir el draft por un comentario.
            if apro_ia_client.LINE_COMMENT_FIELD in p or "comment" in p:
                existing.comment = _line_comment(_apro_ia_product_comment(p))
            if existing.quantity != qty:
                existing.quantity = qty
                changed = True
            if price is not None and existing.negotiated_unit_price != price:
                existing.negotiated_unit_price = price
                changed = True
            if title and existing.title != title:
                existing.title = title
                changed = True
            continue

        # SKU nuevo (agregado del lado de APRO IA) — resolverlo en Shopify.
        if shop_domain is None:
            try:
                shop_domain, token = resolve_store_token(session, quote.shop_domain)
            except Exception:
                logger.exception(
                    "Webhook APRO IA: no se pudo resolver la tienda Shopify "
                    "para sincronizar una línea nueva",
                    extra={"quote_id": str(quote.id), "sku": sku},
                )
                continue
        variant = draft_order_service.resolve_variant_by_sku(shop_domain, token, sku)
        if not variant:
            logger.warning(
                "Webhook APRO IA: SKU nuevo no encontrado en Shopify — se omite",
                extra={"quote_id": str(quote.id), "sku": sku},
            )
            continue
        new_line = QuoteRequestLine(
            quote_request_id=quote.id,
            line_number=next_num,
            variant_gid=variant["id"],
            sku=variant.get("sku") or sku,
            title=title or variant.get("displayName") or sku,
            quantity=qty,
            on_demand=False,
            negotiated_unit_price=price,
            comment=_line_comment(_apro_ia_product_comment(p)),
            currency="CLP",
        )
        session.add(new_line)
        quote.lines.append(new_line)
        next_num += 1
        changed = True

    # Líneas que ya no vienen en products[] — APRO IA quitó ese producto.
    for sku_key, ln in list(by_sku.items()):
        if sku_key not in seen_skus:
            quote.lines.remove(ln)
            session.delete(ln)
            changed = True

    return changed


def handle_apro_ia_webhook(
    payload: dict[str, Any], *, env: str = apro_ia_client.ENV_PROD
) -> dict[str, Any]:
    """Procesa un evento del webhook de eventos de APRO IA.

    Ubica la ``QuoteRequest`` por ``apro_ia_id`` (respaldo: ``apro_ia_folio``
    si el payload no trae ``id``) y persiste el último ``apro_ia_status``
    reportado por el DMS externo. El folio de APRO IA no es único entre
    cotizaciones (caso real: folio ``AC-000007`` reusado por dos cotizaciones
    de clientes distintos), así que no puede ser el criterio principal de
    matching — usarlo pisaría la cotización equivocada. Este
    campo es informativo del flujo de aprobación gerencial de APRO IA — no
    reemplaza ``QuoteRequest.status`` (ciclo Shopify draft/order), que sigue
    dependiendo del webhook ``orders/create`` y de las acciones de SALES.

    Si ``quotation.created`` llega para una cotización que no existe en
    nuestra BD (se originó directo en el DMS de APRO IA, no en nuestro
    carrito), se crea el registro y su Draft Order — ver
    ``_create_quote_from_apro_ia_payload``.

    Para cualquier evento sobre una cotización que ya existe, ``products[]``
    se sincroniza a las líneas (``_sync_lines_from_apro_ia_products``) y, si
    cambió algo y ya hay Draft Order, se actualiza en Shopify — APRO IA es la
    fuente de verdad del precio/cantidad negociado en cualquier punto del
    flujo de aprobación gerencial, no solo al crear.

    Solo notifica a SALES por email en ``quotation.approved``/``rejected`` y
    únicamente si el estado cambió, para no reenviar el mismo aviso en
    reintentos del webhook.
    """
    if not isinstance(payload, dict):
        raise ValueError("Payload inválido")
    event = str(payload.get("event") or "").strip()
    if event not in APRO_IA_WEBHOOK_EVENTS:
        raise ValueError(f"Evento desconocido: {event}")

    folio = str(payload.get("folio") or "").strip()
    apro_ia_id = str(payload.get("id") or "").strip()
    if not folio and not apro_ia_id:
        raise ValueError("El payload debe incluir folio o id")

    status = str(payload.get("status") or "").strip()
    es_qa = env == apro_ia_client.ENV_QA

    # La ruta /qa solo atiende a las empresas de la allowlist. Cualquier otra
    # cosa que llegue por ahí se descarta con 200 (un 4xx haría que el DMS de
    # QA reintente en loop) y queda registrada para poder auditarla.
    if es_qa:
        rut_cliente = str((payload.get("client") or {}).get("rut") or "").strip()
        if not _qa_habilita_rut(rut_cliente):
            logger.warning(
                "Webhook APRO IA QA: RUT fuera de la allowlist — se descarta",
                extra={"event": event, "folio": folio, "rut": rut_cliente},
            )
            return {"received": True, "ignored": True, "matched": False}

    with get_session() as session:
        # QA y producción matchean contra columnas distintas: son secuencias de
        # IDs independientes y un id de QA puede coincidir con el de una
        # cotización real de otro cliente.
        if es_qa:
            criterio = (
                QuoteRequest.apro_ia_qa_id == apro_ia_id
                if apro_ia_id
                else QuoteRequest.apro_ia_qa_folio == folio
            )
        else:
            criterio = (
                QuoteRequest.apro_ia_id == apro_ia_id
                if apro_ia_id
                else QuoteRequest.apro_ia_folio == folio
            )
        quote = session.scalars(select(QuoteRequest).where(criterio)).first()

        if not quote:
            # A diferencia de producción, un quotation.created de QA sin
            # contraparte NO auto-crea la cotización: eso abriría un Draft
            # Order real en la tienda productiva desde una prueba.
            if es_qa:
                logger.warning(
                    "Webhook APRO IA QA: sin cotización espejada para el evento "
                    "— no se auto-crea (evitaría un Draft Order real)",
                    extra={"event": event, "folio": folio, "apro_ia_id": apro_ia_id},
                )
                return {"received": True, "matched": False, "created": False}
            if event != "quotation.created":
                logger.warning(
                    "Webhook APRO IA: no se encontró QuoteRequest para el evento",
                    extra={"event": event, "folio": folio, "apro_ia_id": apro_ia_id},
                )
                session.commit()
                return {"received": True, "matched": False}

            created = _create_quote_from_apro_ia_payload(
                session, payload, folio=folio, apro_ia_id=apro_ia_id, status=status
            )
            if not created:
                session.commit()
                return {"received": True, "matched": False, "created": False}

            quote_id = str(created.id)
            company_name = (
                (created.company.name if created.company else None)
                or created.requested_by_name
                or created.shopify_company_id
            )
            admin_emails = _admin_emails(session, created.company)
            line_count = len(created.lines)
            session.commit()

            encolar_nueva_cotizacion(
                admin_emails=admin_emails,
                company_name=company_name or "",
                quote_id=quote_id,
                line_count=line_count,
            )
            return {
                "received": True,
                "matched": False,
                "created": True,
                "quote_id": quote_id,
                "status": status,
            }

        status_changed = status != "" and quote.apro_ia_status != status
        if status:
            quote.apro_ia_status = status

        lines_changed = _sync_lines_from_apro_ia_products(
            session, quote, payload.get("products") or []
        )
        if lines_changed and quote.draft_order_gid:
            try:
                draft_order_service.update_quote_draft(session, quote)
            except Exception:
                logger.exception(
                    "Webhook APRO IA: no se pudo actualizar el Draft Order en "
                    "Shopify tras sincronizar productos",
                    extra={"quote_id": str(quote.id), "folio": quote.folio},
                )

        company_name = (
            (quote.company.name if quote.company else None)
            or quote.requested_by_name
            or quote.shopify_company_id
        )
        admin_emails = _admin_emails(session, quote.company)
        quote_id = str(quote.id)
        quote_folio = quote.folio or folio
        session.commit()

    if status_changed and event in ("quotation.approved", "quotation.rejected"):
        encolar_cotizacion_apro_ia_estado(
            admin_emails=admin_emails,
            company_name=company_name,
            folio=quote_folio,
            quote_id=quote_id,
            event=event,
            status=status,
        )

    return {"received": True, "matched": True, "quote_id": quote_id, "status": status}


# ---------------------------------------------------------------------------
# Webhook APRO IA — pagos: transferencias y créditos
# (ver docs/Transferencias APRO IA.md)
# ---------------------------------------------------------------------------
#: Estados finales que documenta APRO IA para una transferencia. La comparación
#: es sobre el texto normalizado (sin tildes/mayúsculas) porque el DMS los
#: envía con distinta capitalización según la pantalla que los genera.
#: Estado de la ``QuoteRequest`` cuando APRO IA rechaza el pago. Es propio del
#: medio de pago y no se confunde con ``RECHAZADA``, que significa que el
#: cliente rechazó la cotización.
QUOTE_STATUS_PAGO_RECHAZADO = "PAGO_RECHAZADO"

TRANSFER_STATUS_APROBADA = "APROBADA"
#: Todo estado final que no sea aprobación. Antes se llamaba ``ANULADA`` y
#: anulaba el pedido en Shopify; hoy solo lo etiqueta (ver
#: ``draft_order_service.tag_order_payment_rejected``).
TRANSFER_STATUS_RECHAZADA = "RECHAZADA"

_TRANSFER_STATUS_MAP = {
    "aprobada": TRANSFER_STATUS_APROBADA,
    "aprobado": TRANSFER_STATUS_APROBADA,
    "descartada": TRANSFER_STATUS_RECHAZADA,
    "descartado": TRANSFER_STATUS_RECHAZADA,
    "en papelera": TRANSFER_STATUS_RECHAZADA,
    "papelera": TRANSFER_STATUS_RECHAZADA,
    "descartada / en papelera": TRANSFER_STATUS_RECHAZADA,
    "anulada": TRANSFER_STATUS_RECHAZADA,
    "anulado": TRANSFER_STATUS_RECHAZADA,
    "rechazada": TRANSFER_STATUS_RECHAZADA,
    "rechazado": TRANSFER_STATUS_RECHAZADA,
}


def _normalize_transfer_status(value: str) -> str | None:
    """Mapea el ``status`` crudo del webhook a ``APROBADA``/``RECHAZADA``.

    Devuelve ``None`` si el estado no es uno de los finales documentados (el
    DMS puede emitir estados intermedios que no deben mover el pedido).
    """
    raw = " ".join(str(value or "").strip().lower().split())
    if not raw:
        return None
    normalized = (
        raw.replace("á", "a").replace("é", "e").replace("í", "i")
        .replace("ó", "o").replace("ú", "u")
    )
    return _TRANSFER_STATUS_MAP.get(normalized)


_ORDER_GID_PREFIX = "gid://shopify/Order/"


def _extract_shopify_order_id(order_ref: str) -> str:
    """Devuelve el número de pedido de Shopify contenido en ``order_ref``.

    Acepta el order name que manda APRO IA, con o sin ``#`` (``1013`` /
    ``#1013``), el ID interno pelado (``6723309600952``) y el GID completo
    (``gid://shopify/Order/6723309600952``). Devuelve ``""`` si ``order_ref``
    no parece un pedido (ej. un folio ``CO-000040``).
    """
    ref = str(order_ref or "").strip()
    if ref.startswith(_ORDER_GID_PREFIX):
        ref = ref.rsplit("/", 1)[-1]
    ref = ref.lstrip("#").strip()
    return ref if ref.isdigit() else ""


def _rut_matches(company: Company | None, normalized_rut: str) -> bool:
    """Sin RUT en el payload no se puede verificar; con RUT tiene que calzar."""
    if not normalized_rut:
        return True
    if company is None:
        return False
    return _normalize_rut(company.billing_rut) == normalized_rut


def _find_transfer_target(
    session: Session, order_ref: str, rut: str
) -> dict[str, Any] | None:
    """Resuelve a qué pedido (y opcionalmente qué cotización) apunta ``order``.

    APRO IA envía el **order name del pedido de Shopify** (``1013``, el número
    visible que ve el cliente; también se acepta el ID interno o el GID): el
    pago se genera en el
    checkout (transferencia como medio de pago manual, que deja el pedido
    impago) y ellos solo confirman después que la transferencia se recibió. Por
    eso el sujeto del webhook es el pedido y la ``QuoteRequest`` es opcional —
    un pedido B2B puede no venir de un RFQ nuestro.

    Se sigue aceptando un folio ``CO-NNNNNN`` como ``order`` por
    compatibilidad: en ese caso se resuelve la cotización y de ahí el pedido.

    El RUT es la guarda en ambos caminos. Si el pedido pertenece a otra
    empresa, o no lo tenemos sincronizado (no llegó el webhook ``orders/create``
    y por lo tanto no hay con qué verificar de quién es), no se procesa: marcar
    pagado el pedido equivocado es peor que no procesar.

    Devuelve ``{order_gid, quote, shopify_order, company, shop_domain}`` o
    ``None``.
    """
    ref = str(order_ref or "").strip()
    if not ref:
        return None
    normalized_rut = _normalize_rut(rut)

    order_id = _extract_shopify_order_id(ref)
    if order_id:
        shopify_order = session.scalars(
            select(ShopifyOrder).where(ShopifyOrder.shopify_order_id == order_id)
        ).first()
        if shopify_order is None:
            # El caso normal: APRO IA manda el número visible del pedido
            # (#1013) y no el ID interno; son valores distintos y no se pueden
            # derivar entre sí. Sin colisión posible: los IDs tienen 13 dígitos
            # y los order names 4.
            shopify_order = session.scalars(
                select(ShopifyOrder).where(
                    ShopifyOrder.order_name.in_([ref, f"#{order_id}", order_id])
                )
            ).first()
        if shopify_order is None:
            logger.warning(
                "Webhook pagos APRO IA: el pedido no está sincronizado "
                "en Apro Click — no se puede verificar a qué empresa pertenece",
                extra={"order": ref, "rut": rut},
            )
            return None

        order_gid = f"{_ORDER_GID_PREFIX}{shopify_order.shopify_order_id}"
        quote = session.scalars(
            select(QuoteRequest).where(QuoteRequest.order_gid == order_gid)
        ).first()
        company = shopify_order.company or (quote.company if quote else None)
        if not _rut_matches(company, normalized_rut):
            logger.warning(
                "Webhook pagos APRO IA: el pedido pertenece a otra "
                "empresa que la del RUT informado — no se procesa",
                extra={"order": ref, "rut": rut},
            )
            return None
        return {
            "order_gid": order_gid,
            "quote": quote,
            "shopify_order": shopify_order,
            "company": company,
            "shop_domain": shopify_order.shop_domain
            or (quote.shop_domain if quote else ""),
        }

    quote = _find_quote_by_folio(session, ref, normalized_rut)
    if quote is None:
        return None
    return {
        "order_gid": quote.order_gid or "",
        "quote": quote,
        "shopify_order": None,
        "company": quote.company,
        "shop_domain": quote.shop_domain,
    }


def _find_quote_by_folio(
    session: Session, ref: str, normalized_rut: str
) -> QuoteRequest | None:
    """Camino de compatibilidad: ``order`` traído como folio de cotización.

    Busca por ``apro_ia_folio`` y por nuestro ``folio`` (incluido el que lleva
    sufijo de desambiguación, ver ``_unique_own_folio``). El folio de APRO IA
    **no es único** entre cotizaciones (caso real ``AC-000007``, ver
    ``handle_apro_ia_webhook``), así que el RUT desempata.
    """
    candidates = list(
        session.scalars(
            select(QuoteRequest)
            .where(
                (QuoteRequest.apro_ia_folio == ref)
                | (QuoteRequest.folio == ref)
                | (QuoteRequest.folio.like(f"{ref}-%"))
            )
            .order_by(QuoteRequest.created_at.desc())
        ).all()
    )
    if not candidates:
        return None
    if not normalized_rut:
        return candidates[0] if len(candidates) == 1 else None

    matching = [q for q in candidates if _rut_matches(q.company, normalized_rut)]
    if len(matching) == 1:
        return matching[0]
    if not matching:
        logger.warning(
            "Webhook pagos APRO IA: el folio existe pero pertenece a "
            "otra empresa que la del RUT informado — no se procesa",
            extra={"order": ref, "rut": _rut_hint(normalized_rut), "candidatos": len(candidates)},
        )
        return None
    logger.warning(
        "Webhook pagos APRO IA: más de una cotización coincide con el "
        "folio y el RUT — no se procesa para no afectar la equivocada",
        extra={"order": ref, "candidatos": len(matching)},
    )
    return None


def _rut_hint(normalized_rut: str) -> str:
    """RUT enmascarado para logs (no exponer el dato completo en CloudWatch)."""
    return f"…{normalized_rut[-4:]}" if len(normalized_rut) > 4 else "…"


def _ensure_order_for_transfer(session: Session, quote: QuoteRequest) -> str:
    """Crea el pedido en Shopify para una cotización que aún no lo generó.

    Camino de respaldo: lo normal es que el pedido ya exista porque el cliente
    pagó en el checkout. Pero si APRO IA confirma la transferencia de una
    cotización que nunca llegó a pedido (quedó en el Draft Order, o ni
    siquiera), se arma acá: se crea el draft si falta y se completa con
    ``paymentPending`` para que el pedido nazca impago y sea la transferencia
    la que lo marque pagado.

    Devuelve el ``order_gid`` resultante.
    """
    if quote.order_gid:
        return quote.order_gid
    if not quote.draft_order_gid:
        draft_order_service.create_quote_draft(session, quote)
        quote.status = "COTIZADA"
    draft_order_service.complete_quote(session, quote, payment_pending=True)
    if not quote.order_gid:
        raise ValueError(
            "draftOrderComplete no devolvió el pedido creado; no se puede "
            "marcar la transferencia como pagada"
        )
    return quote.order_gid


#: Medios de pago que confirma APRO IA. Cada uno entra por su propia ruta y
#: solo cambia el tag/nota en Shopify y el texto del aviso a SALES: el ciclo
#: (confirmar o anular un pedido que nació impago del checkout) es el mismo.
PAYMENT_METHOD_TRANSFER = "transferencia"
PAYMENT_METHOD_CREDIT = "credito"


def handle_apro_ia_credit_webhook(
    payload: dict[str, Any], *, env: str = apro_ia_client.ENV_PROD
) -> dict[str, Any]:
    """Webhook de pagos con **crédito** de APRO IA.

    Mismo payload y mismo comportamiento que
    ``handle_apro_ia_transfer_webhook``; la única diferencia es el medio de
    pago con que queda marcado el pedido en Shopify (tag ``credito``) y el
    texto del aviso a SALES. Va por una ruta distinta porque APRO IA emite los
    dos documentos por separado.
    """
    return handle_apro_ia_transfer_webhook(
        payload, env=env, payment_method=PAYMENT_METHOD_CREDIT
    )


def handle_apro_ia_transfer_webhook(
    payload: dict[str, Any],
    *,
    env: str = apro_ia_client.ENV_PROD,
    payment_method: str = PAYMENT_METHOD_TRANSFER,
) -> dict[str, Any]:
    """Procesa el webhook de pagos de APRO IA (transferencia o crédito).

    Payload (``docs/Transferencias AproClick.pdf``): ``folio`` (folio interno
    del documento de pago en APRO IA), ``rut`` (cliente), ``status`` (estado
    final del documento) y ``order`` (ID del pedido en Shopify).

    El pago se genera en el **checkout de Shopify** (transferencia y crédito
    son medios de pago manuales, que dejan el pedido impago); APRO IA solo
    confirma después que el pago se recibió. Por eso el sujeto de este webhook
    es el pedido, no la cotización: un pedido B2B puede no venir de un RFQ
    nuestro y igual debe quedar pagado.

    - ``Aprobada`` → ``orderMarkAsPaid`` + tag del medio (``transferencia`` o
      ``credito``) y nota con el folio. Si hay cotización asociada pasa a
      ``ACEPTADA``; y en el caso de borde en que la cotización nunca llegó a
      pedido, se crea (draft → ``draftOrderComplete`` impago) antes de marcarlo.
    - ``Descartada``/``En papelera``/``Anulada``/``Rechazada`` → el pedido
      **no** se anula: queda abierto y pendiente de pago en Shopify, con el tag
      del rechazo (``transferencia-rechazada`` / ``credito-rechazado``) y una
      nota con el folio. Si solo había Draft Order, tampoco se elimina. La
      cotización, si existe, pasa a ``PAGO_RECHAZADO`` para que SALES la vea en
      el panel.

    Idempotente: un reenvío del mismo estado no vuelve a tocar Shopify ni
    reenvía el email. Con cotización la guarda es ``transfer_status`` /
    ``transfer_paid_at``; sin cotización, el propio estado del pedido en
    Shopify (``canMarkAsPaid`` para el pago, el tag ya puesto para el
    rechazo). Los dos medios comparten
    esas columnas —el ciclo es el mismo— y ``payment_method`` queda persistido
    para poder distinguirlos después.
    """
    if not isinstance(payload, dict):
        raise ValueError("Payload inválido")

    transfer_folio = str(payload.get("folio") or "").strip()
    rut = str(payload.get("rut") or "").strip()
    raw_status = str(payload.get("status") or "").strip()
    order_ref = str(payload.get("order") or "").strip()

    # Rastro de entrada: deja el aviso en CloudWatch aunque después no matchee
    # o el payload venga incompleto. Sin esto, un webhook que no aplica nada es
    # indistinguible de uno que nunca llegó.
    logger.info(
        "Webhook pagos APRO IA recibido",
        extra={
            "medio": payment_method,
            "folio": transfer_folio,
            "order": order_ref,
            "status": raw_status,
            "rut": rut,
        },
    )

    if not order_ref:
        raise ValueError("El payload debe incluir order")
    if not raw_status:
        raise ValueError("El payload debe incluir status")

    # Misma regla que el webhook de eventos: la ruta /qa solo opera sobre las
    # empresas habilitadas. Sin esta guarda, una transferencia de prueba podría
    # marcar pagado o anular el pedido real de otro cliente.
    if env == apro_ia_client.ENV_QA and not _qa_habilita_rut(rut):
        logger.warning(
            "Webhook pagos APRO IA QA: RUT fuera de la allowlist "
            "— se descarta",
            extra={"order": order_ref, "folio": transfer_folio, "rut": rut},
        )
        return {"received": True, "ignored": True, "matched": False}

    estado = _normalize_transfer_status(raw_status)
    if estado is None:
        logger.info(
            "Webhook pagos APRO IA: estado no final, se ignora",
            extra={"status": raw_status, "order": order_ref},
        )
        return {"received": True, "matched": False, "applied": False, "status": raw_status}

    notify: dict[str, Any] | None = None
    notify_cliente: dict[str, Any] | None = None
    with get_session() as session:
        target = _find_transfer_target(session, order_ref, rut)
        if target is None:
            logger.warning(
                "Webhook pagos APRO IA: no se encontró el pedido",
                extra={"order": order_ref, "folio": transfer_folio},
            )
            return {"received": True, "matched": False, "applied": False}

        quote: QuoteRequest | None = target["quote"]
        shopify_order: ShopifyOrder | None = target["shopify_order"]
        order_gid: str = target["order_gid"]
        shop_domain: str = target["shop_domain"]

        if quote is not None and _transferencia_ya_aplicada(quote, raw_status, estado):
            return {
                "received": True,
                "matched": True,
                "applied": False,
                "quote_id": str(quote.id),
                "order_gid": order_gid,
                "status": raw_status,
            }

        if quote is not None:
            quote.transfer_folio = transfer_folio or quote.transfer_folio
            quote.transfer_status = raw_status
            quote.payment_method = payment_method

        # Los fallos de Shopify se re-lanzan como RuntimeError (→ HTTP 500) y no
        # como ValueError (→ 400): un 400 le diría a APRO IA que el payload está
        # mal y que no vale la pena reintentar, cuando en realidad el pago sí
        # ocurrió y solo falló nuestro lado.
        try:
            if estado == TRANSFER_STATUS_APROBADA:
                if not order_gid and quote is not None:
                    order_gid = _ensure_order_for_transfer(session, quote)
                if not order_gid:
                    raise ValueError(
                        "La transferencia no tiene pedido de Shopify al que aplicarse"
                    )
                resultado = draft_order_service.mark_order_paid(
                    session,
                    order_gid,
                    shop_domain=shop_domain,
                    transfer_folio=transfer_folio,
                    payment_method=payment_method,
                )
                aplicado = not resultado.get("already_paid")
                if quote is not None:
                    quote.transfer_paid_at = datetime.now(timezone.utc)
                    quote.status = "ACEPTADA"
                if shopify_order is not None:
                    shopify_order.financial_status = "paid"
            else:
                # Rechazo: el pedido **no** se anula. Queda abierto y pendiente
                # de pago en Shopify, solo etiquetado, y el seguimiento pasa a
                # ser responsabilidad de SALES desde el panel.
                if order_gid:
                    resultado = draft_order_service.tag_order_payment_rejected(
                        session,
                        order_gid,
                        shop_domain=shop_domain,
                        transfer_folio=transfer_folio,
                        payment_method=payment_method,
                        raw_status=raw_status,
                    )
                    aplicado = not resultado.get("already_tagged")
                else:
                    # Sin pedido solo hay Draft Order (o nada): tampoco se
                    # elimina — el cliente puede retomar el pago y el draft es
                    # el precio ya negociado.
                    aplicado = quote is not None
                if quote is not None:
                    quote.status = QUOTE_STATUS_PAGO_RECHAZADO
        except Exception as e:
            logger.exception(
                "Webhook pagos APRO IA: falló la operación en Shopify",
                extra={
                    "medio": payment_method,
                    "order": order_ref,
                    "order_gid": order_gid,
                    "estado": estado,
                    "quote_id": str(quote.id) if quote else None,
                },
            )
            raise RuntimeError(
                f"No se pudo aplicar la transferencia en Shopify: {e}"
            ) from e

        company = target["company"]
        if aplicado:
            notify = {
                "admin_emails": _admin_emails(session, company),
                "company_name": (
                    (company.name if company else None)
                    or (quote.requested_by_name if quote else None)
                    or ""
                ),
                "folio": (
                    (quote.folio if quote else None)
                    or (shopify_order.order_name if shopify_order else None)
                    or order_ref
                ),
                "quote_id": str(quote.id) if quote else "",
                "transfer_folio": transfer_folio,
                "estado": estado,
                "status": raw_status,
                "payment_method": payment_method,
            }
            if payment_method == PAYMENT_METHOD_CREDIT:
                notify_cliente = _aviso_pago_cliente(
                    session, company, quote, shopify_order, order_ref,
                    aprobado=estado == TRANSFER_STATUS_APROBADA,
                )
        result = {
            "received": True,
            "matched": True,
            "applied": aplicado,
            "quote_id": str(quote.id) if quote else None,
            "order_gid": order_gid,
            "quote_status": quote.status if quote else None,
            "status": raw_status,
        }
        session.commit()

    if notify:
        encolar_transferencia_estado(**notify)
    if notify_cliente:
        encolar_pago_credito_cliente(**notify_cliente)
    return result


def _aviso_pago_cliente(
    session: Session,
    company: Company | None,
    quote: QuoteRequest | None,
    shopify_order: ShopifyOrder | None,
    order_ref: str,
    *,
    aprobado: bool,
) -> dict[str, Any] | None:
    """Arma el aviso al cliente del resultado de su pago con crédito.

    Destinatario, en orden: quien hizo el pedido en Shopify, quien pidió la
    cotización y, si no hay ninguno, el contacto principal de la empresa.
    Devuelve ``None`` si no hay a quién escribirle.
    """
    contactos: list[Client] = []
    if company is not None:
        contactos = list(
            session.scalars(
                select(Client)
                .where(Client.company_id == company.id, Client.email.is_not(None))
                .order_by(Client.is_main_contact.desc())
            ).all()
        )
    email = (
        (shopify_order.email if shopify_order else None)
        or (quote.requested_by_email if quote else None)
        or (contactos[0].email if contactos else None)
        or ""
    ).strip()
    if not email:
        logger.warning(
            "Pago con crédito: sin email del cliente para avisarle",
            extra={"order": order_ref, "company_id": str(company.id) if company else None},
        )
        return None
    nombre = next(
        (c.name for c in contactos if (c.email or "").strip().lower() == email.lower()),
        None,
    ) or (quote.requested_by_name if quote else None)

    total = None
    if shopify_order is not None and shopify_order.total_price is not None:
        total = f"${shopify_order.total_price:,.0f} {shopify_order.currency or 'CLP'}".replace(",", ".")
    order_name = (
        (shopify_order.order_name if shopify_order else None)
        or (quote.folio if quote else None)
        or order_ref
    )
    if not order_name.startswith("#") and order_name.isdigit():
        order_name = f"#{order_name}"
    return {
        "contact_email": email,
        "contact_name": nombre,
        "company_name": (company.name if company else None) or "",
        "order_name": order_name,
        "aprobado": aprobado,
        "total": total,
    }


def _etiqueta_medio(payment_method: str) -> str:
    """Nombre del medio de pago para notas y textos visibles."""
    return "Crédito" if payment_method == PAYMENT_METHOD_CREDIT else "Transferencia"


def _transferencia_ya_aplicada(
    quote: QuoteRequest, raw_status: str, estado: str
) -> bool:
    """``True`` si este mismo evento ya se procesó sobre la cotización.

    APRO IA no documenta su política de reintentos, así que se asume que puede
    reenviar el mismo evento; sin esta guarda un reenvío duplicaría el email a
    SALES.
    """
    if quote.transfer_status != raw_status:
        return False
    if estado == TRANSFER_STATUS_APROBADA:
        return quote.transfer_paid_at is not None
    return quote.status == QUOTE_STATUS_PAGO_RECHAZADO


# ---------------------------------------------------------------------------
# Negociación (SALES)
# ---------------------------------------------------------------------------
def _load_for_negotiation(session: Session, quote_id: str) -> QuoteRequest:
    try:
        qid = uuid.UUID(quote_id)
    except ValueError as e:
        raise LookupError("Cotización no encontrada") from e
    quote = session.get(QuoteRequest, qid)
    if not quote:
        raise LookupError("Cotización no encontrada")
    return quote


#: Tope del comentario por línea: es una nota, no un documento.
LINE_COMMENT_MAX_LEN = 1000


def _line_comment(value: Any) -> str | None:
    text_value = str(value or "").strip()
    return text_value[:LINE_COMMENT_MAX_LEN] or None


def _apro_ia_product_comment(product: dict[str, Any]) -> Any:
    """Comentario de un producto del webhook de APRO IA.

    El campo es ``comment_product``; ``comment`` se acepta como respaldo.
    """
    if apro_ia_client.LINE_COMMENT_FIELD in product:
        return product.get(apro_ia_client.LINE_COMMENT_FIELD)
    return product.get("comment")


def _only_line_comments(body: dict[str, Any]) -> bool:
    """El PATCH trae solo comentarios de líneas existentes (``{id, comment}``)."""
    lines = body.get("lines")
    if set(body) != {"lines"} or not isinstance(lines, list) or not lines:
        return False
    return all(isinstance(rl, dict) and set(rl) <= {"id", "comment"} for rl in lines)


def update_quote_lines(quote_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """SALES fija precios netos / descuentos, cantidades y líneas por línea y condiciones."""
    with get_session() as session:
        quote = _load_for_negotiation(session, quote_id)
        by_id = {str(ln.id): ln for ln in quote.lines}

        # Actualizar líneas existentes (precio, descuento, cantidad)
        lines_to_delete: list[QuoteRequestLine] = []
        for rl in (body.get("lines") or []):
            if not isinstance(rl, dict):
                continue
            ln = by_id.get(str(rl.get("id")))
            if not ln:
                continue
            if "negotiated_unit_price" in rl:
                ln.negotiated_unit_price = _to_decimal(rl.get("negotiated_unit_price"))
            if "applied_discount" in rl:
                ad = rl.get("applied_discount")
                ln.applied_discount = ad if isinstance(ad, dict) else None
            if "comment" in rl:
                ln.comment = _line_comment(rl.get("comment"))
            if "quantity" in rl:
                try:
                    qty = int(rl["quantity"])
                except (TypeError, ValueError):
                    qty = ln.quantity
                if qty <= 0:
                    lines_to_delete.append(ln)
                else:
                    ln.quantity = qty

        for ln in lines_to_delete:
            quote.lines.remove(ln)
            session.delete(ln)

        # Agregar nuevas líneas por SKU
        new_lines_raw = body.get("new_lines") or []
        if new_lines_raw:
            shop_domain, token = resolve_store_token(session, quote.shop_domain)
            existing_skus = {(ln.sku or "").strip().lower() for ln in quote.lines}
            next_num = max((ln.line_number for ln in quote.lines), default=0) + 1
            default_currency = quote.lines[0].currency if quote.lines else "CLP"
            for nl in new_lines_raw:
                if not isinstance(nl, dict):
                    continue
                sku = str(nl.get("sku") or "").strip()
                if not sku or sku.lower() in existing_skus:
                    continue
                qty = int(nl.get("quantity") or 1)
                if qty <= 0:
                    continue
                variant = draft_order_service.resolve_variant_by_sku(shop_domain, token, sku)
                if not variant:
                    raise ValueError(f"SKU no encontrado en Shopify: {sku}")
                title = (
                    variant.get("displayName")
                    or f"{(variant.get('product') or {}).get('title', '')} — {variant.get('title', '')}".strip(" —")
                    or sku
                )
                new_ln = QuoteRequestLine(
                    quote_request_id=quote.id,
                    line_number=next_num,
                    variant_gid=variant["id"],
                    sku=variant.get("sku") or sku,
                    title=title,
                    quantity=qty,
                    on_demand=False,
                    negotiated_unit_price=_to_decimal(nl.get("negotiated_unit_price")),
                    comment=_line_comment(nl.get("comment")),
                    currency=default_currency,
                )
                session.add(new_ln)
                quote.lines.append(new_ln)
                existing_skus.add(sku.lower())
                next_num += 1

        if "order_applied_discount" in body:
            oad = body.get("order_applied_discount")
            quote.order_applied_discount = oad if isinstance(oad, dict) else None
        if "payment_terms_label" in body:
            quote.payment_terms_label = (
                str(body["payment_terms_label"]).strip() or None
            )
        if "validity_days" in body:
            try:
                quote.validity_days = int(body["validity_days"])
            except (TypeError, ValueError):
                pass
        if "comment" in body:
            quote.comment = str(body["comment"]).strip() or None
        if "staff_note" in body:
            quote.staff_note = str(body["staff_note"]).strip() or None
        if "shipping_line" in body:
            raw_sl = body.get("shipping_line")
            if raw_sl is None:
                quote.shipping_line = None
            elif isinstance(raw_sl, dict) and raw_sl.get("title") and raw_sl.get("price") is not None:
                try:
                    quote.shipping_line = {
                        "title": str(raw_sl["title"]).strip(),
                        "price": str(Decimal(str(raw_sl["price"]))),
                    }
                except (InvalidOperation, ValueError):
                    pass
        # Si solo cambiaron comentarios de línea no se toca Shopify (el Draft
        # Order no los lleva). Viajan a APRO IA —y se baja su PDF— solo con
        # ``APRO_IA_LINE_COMMENTS`` activo y si la cotización ya existe allá.
        if _only_line_comments(body):
            if (
                apro_ia_client.line_comments_enabled()
                and quote.apro_ia_folio
                and quote.status in ("COTIZADA", "INVOICE_SENT")
            ):
                _sync_apro_ia_and_pdf(session, quote)
            result = quote.to_dict()
            session.commit()
            return result
        # Si ya tiene Draft Order, actualizarlo con los nuevos precios/envío.
        if quote.draft_order_gid and quote.status in ("COTIZADA", "INVOICE_SENT"):
            try:
                draft_order_service.update_quote_draft(session, quote)
            except Exception:
                logger.exception(
                    "update_quote_lines: error actualizando draft en Shopify",
                    extra={"quote_id": quote_id, "draft_gid": quote.draft_order_gid},
                )
        # Regenerar PDF con los nuevos precios si ya tiene folio.
        if quote.folio and quote.status in ("COTIZADA", "INVOICE_SENT"):
            _sync_apro_ia_and_pdf(session, quote)
        result = quote.to_dict()
        session.commit()
        return result


def _assign_folio(session: Session, quote: QuoteRequest) -> None:
    if quote.folio:
        return
    n = session.execute(select(func.nextval("quote_folio_seq"))).scalar_one()
    quote.folio = f"CO-{int(n):06d}"


def _company_assigned_sales_user(session: Session, company: Company | None) -> User | None:
    """Vendedor de cartera de la empresa vía ``UserCompany`` (asignación manual o
    auto-selección Ecommerce en la aprobación de la solicitud de registro).

    Distinto de ``Company.referred_by_user_id``, que solo se llena si la empresa
    se registró a través del link de invitación de un vendedor (``?from=<code>``).
    Una empresa puede no tener ``referred_by_user_id`` y aun así tener vendedor
    asignado solo por esta tabla — sin este fallback, `_seller_email`/`_seller_sap_code`
    caían siempre al vendedor por defecto para esos casos.
    """
    if not company:
        return None
    return session.scalar(
        select(User)
        .join(UserCompany, UserCompany.user_id == User.id)
        .where(
            UserCompany.company_id == company.id,
            User.role == "SALES",
            User.status == "ACTIVE",
        )
        .order_by(User.id)
        .limit(1)
    )


def _seller_email(session: Session, company: Company | None) -> str:
    if company and company.referred_by_user_id:
        u = session.get(User, company.referred_by_user_id)
        if u and u.email:
            return u.email
    u = _company_assigned_sales_user(session, company)
    if u and u.email:
        return u.email
    # Fallback: vendedor por defecto (debe ser un usuario registrado en APRO IA)
    return (os.environ.get("APRO_IA_DEFAULT_SELLER_EMAIL") or "").strip()


def _seller_sap_code(session: Session, seller_email: str, company: Company | None) -> str:
    """Resuelve el código SAP del vendedor — APRO IA lo usa para identificar
    al vendedor porque sus emails no siempre coinciden con los nuestros.
    """
    user: User | None = None
    if seller_email:
        user = session.scalar(select(User).where(User.email == seller_email))
    if not user and company and company.referred_by_user_id:
        user = session.get(User, company.referred_by_user_id)
    if not user:
        user = _company_assigned_sales_user(session, company)
    if user and user.codigo_sap:
        return user.codigo_sap
    # Fallback: código SAP genérico para cotizaciones sin vendedor identificado.
    return (os.environ.get("APRO_IA_DEFAULT_SELLER_SAP_CODE") or "000").strip() or "000"


def _regenerate_pdf(session: Session, quote: QuoteRequest) -> None:
    vendedor = _vendedor_name(session, quote.company)
    pdf_bytes = quote_pdf_service.generate_quote_pdf(quote, vendedor_name=vendedor)
    quote_pdf_service.store_quote_pdf(quote, pdf_bytes)


def _sync_apro_ia_and_pdf(
    session: Session,
    quote: QuoteRequest,
    *,
    seller_email: str = "",
) -> None:
    """Sincroniza la cotización con APRO IA y guarda su PDF en S3 como respaldo.

    Si APRO IA falla en cualquier punto, cae a generar el PDF localmente con
    reportlab para no interrumpir el flujo principal.
    """
    company = quote.company
    client_rut = ""
    client_nombre = ""
    if company:
        client_rut = (company.billing_rut or "").strip()
        client_nombre = (company.billing_razon_social or company.name or "").strip()

    if not seller_email:
        seller_email = _seller_email(session, company)

    productos = apro_ia_client.build_productos(quote.lines, quote.shipping_line)

    try:
        if quote.apro_ia_folio:
            data = apro_ia_client.editar_cotizacion(
                quote.apro_ia_folio,
                productos=productos,
                comment=quote.comment,
            )
            if data.get("status"):
                quote.apro_ia_status = data["status"]
                session.flush()
        else:
            if not seller_email:
                raise RuntimeError("seller_email requerido para crear en APRO IA")
            seller_sap_code = _seller_sap_code(session, seller_email, company)
            data = apro_ia_client.crear_cotizacion(
                client_rut=client_rut,
                client_nombre=client_nombre,
                seller_email=seller_email,
                seller_sap_code=seller_sap_code,
                productos=productos,
                comment=quote.comment,
            )
            quote.apro_ia_id = data["id"]
            quote.apro_ia_folio = data["folio"]
            # El folio de APRO IA es el número oficial de la cotización en todo el sistema.
            quote.folio = data["folio"]
            if data.get("status"):
                quote.apro_ia_status = data["status"]
            session.flush()

        pdf_bytes = apro_ia_client.descargar_pdf(quote.apro_ia_folio)
        quote_pdf_service.store_quote_pdf(quote, pdf_bytes)
        logger.info(
            "PDF APRO IA guardado como respaldo en S3",
            extra={"apro_ia_folio": quote.apro_ia_folio, "quote_id": str(quote.id)},
        )
    except Exception:
        logger.exception(
            "APRO IA sync falló — generando PDF local con reportlab",
            extra={"quote_id": str(quote.id)},
        )
        _regenerate_pdf(session, quote)

    # Después del sync productivo y fuera de su try: el espejo es independiente
    # (si producción falló igual queremos la copia en QA, y viceversa).
    _espejar_en_qa(
        session,
        quote,
        client_rut=client_rut,
        client_nombre=client_nombre,
        seller_email=seller_email,
        productos=productos,
    )


def _espejar_en_qa(
    session: Session,
    quote: QuoteRequest,
    *,
    client_rut: str,
    client_nombre: str,
    seller_email: str,
    productos: list[dict[str, Any]],
) -> None:
    """Replica la cotización en el DMS de QA, solo para empresas habilitadas.

    Es *fire-and-forget* por diseño: cualquier fallo se loguea y se traga. QA
    no puede romper ni demorar el flujo productivo, así que no toca ``folio``,
    ``apro_ia_folio`` ni el PDF — esos siguen viniendo de producción, que es la
    fuente de verdad. Los identificadores del espejo van a
    ``apro_ia_qa_id``/``apro_ia_qa_folio``, que es contra lo que matchea el
    webhook de la ruta ``/qa``.
    """
    if not _qa_habilita_rut(client_rut):
        return
    if not apro_ia_client.env_configurado(apro_ia_client.ENV_QA):
        logger.info(
            "Espejo QA APRO IA omitido: el ambiente no tiene base URL o token",
            extra={"quote_id": str(quote.id), "rut": client_rut},
        )
        return

    try:
        if quote.apro_ia_qa_folio:
            apro_ia_client.editar_cotizacion(
                quote.apro_ia_qa_folio,
                productos=productos,
                comment=quote.comment,
                env=apro_ia_client.ENV_QA,
            )
        else:
            if not seller_email:
                raise RuntimeError("seller_email requerido para crear en APRO IA QA")
            data = apro_ia_client.crear_cotizacion(
                client_rut=client_rut,
                client_nombre=client_nombre,
                seller_email=seller_email,
                seller_sap_code=_seller_sap_code(session, seller_email, quote.company),
                productos=productos,
                comment=quote.comment,
                env=apro_ia_client.ENV_QA,
            )
            quote.apro_ia_qa_id = str(data.get("id") or "") or None
            quote.apro_ia_qa_folio = data.get("folio") or None
            session.flush()
        logger.info(
            "Cotización espejada en el ambiente QA de APRO IA",
            extra={
                "quote_id": str(quote.id),
                "apro_ia_qa_folio": quote.apro_ia_qa_folio,
                "apro_ia_folio": quote.apro_ia_folio,
            },
        )
    except Exception:
        logger.exception(
            "Espejo QA APRO IA falló — se ignora, no afecta el flujo productivo",
            extra={"quote_id": str(quote.id), "rut": client_rut},
        )


def negotiate_draft(quote_id: str, *, seller_email: str = "") -> dict[str, Any]:
    """Crea o actualiza el Draft Order, sincroniza con APRO IA y guarda el PDF en S3."""
    with get_session() as session:
        quote = _load_for_negotiation(session, quote_id)
        if quote.draft_order_gid:
            draft = draft_order_service.update_quote_draft(session, quote)
        else:
            draft = draft_order_service.create_quote_draft(session, quote)
        _assign_folio(session, quote)
        quote.status = "COTIZADA"
        _sync_apro_ia_and_pdf(session, quote, seller_email=seller_email)
        session.flush()
        result = quote.to_dict()
        contact_email = quote.requested_by_email
        contact_name = quote.requested_by_name
        company_name = (
            (quote.company.name if quote.company else None)
            or quote.requested_by_name
            or quote.shopify_company_id
        )
        folio = quote.folio or ""
        quote_pid = str(quote.id)
        _, _, _, total = quote_pdf_service.quote_totals(quote)
        total_str = quote_pdf_service._fmt_clp(total)
        result["draft_order"] = draft
        session.commit()

    if contact_email and folio:
        encolar_cotizacion_lista(
            contact_email=contact_email,
            contact_name=contact_name,
            company_name=company_name,
            folio=folio,
            pdf_url=pdf_email_url(quote_pid),
            total=total_str,
        )
    return result


def send_invoice(quote_id: str) -> dict[str, Any]:
    with get_session() as session:
        quote = _load_for_negotiation(session, quote_id)
        draft = draft_order_service.send_quote_invoice(session, quote)
        quote.status = "INVOICE_SENT"
        result = quote.to_dict()
        result["draft_order"] = draft
        session.commit()
        return result


def complete(quote_id: str) -> dict[str, Any]:
    with get_session() as session:
        quote = _load_for_negotiation(session, quote_id)
        draft = draft_order_service.complete_quote(session, quote)
        quote.status = "ACEPTADA"
        result = quote.to_dict()
        result["draft_order"] = draft
        session.commit()
        return result


class QuoteNotPayableError(ValueError):
    """La cotización no se puede pagar (sin Draft Order, ya es pedido, anulada…)."""


def get_payment_url(quote_id: str) -> str:
    """Devuelve a dónde mandar al cliente que abre el link «Ir a pagar».

    - Draft Order abierto → su ``invoiceUrl``: el mismo link de pago que
      Shopify manda por correo con ``draftOrderInvoiceSend``. Al pagarlo, el
      checkout convierte el Draft Order en pedido. No envía el correo ni
      cambia el estado de la cotización.
    - Draft Order ya convertido en pedido (cotización pagada, o el link se abre
      de nuevo desde un correo viejo) → enlaza el pedido a la cotización y
      devuelve su ``statusPageUrl``, para que el cliente vea su pedido en vez
      de un error.
    """
    with get_session() as session:
        quote = _load_for_negotiation(session, quote_id)
        if not quote.draft_order_gid:
            raise QuoteNotPayableError("Esta cotización aún no está lista para pagar.")
        draft = draft_order_service.fetch_invoice_url(session, quote)
        order = draft.get("order") or {}
        if order.get("id"):
            if _registrar_pedido(quote, order):
                session.commit()
            status_url = str(order.get("statusPageUrl") or "").strip()
            if status_url.startswith("https://"):
                return status_url
            raise QuoteNotPayableError(
                f"Esta cotización ya generó el pedido {order.get('name') or ''}.".replace(" .", ".")
            )
        if quote.order_gid:
            raise QuoteNotPayableError("Esta cotización ya generó un pedido.")
        if quote.status not in PAYABLE_STATUSES:
            raise QuoteNotPayableError("Esta cotización ya no se puede pagar.")
        url = str(draft.get("invoiceUrl") or "").strip()
        if not url.startswith("https://"):
            raise QuoteNotPayableError("Shopify no entregó el link de pago de la cotización.")
        return url


def get_shipping_rates(quote_id: str, auth_header: str = "") -> list[dict]:
    """Devuelve las tarifas de envío disponibles para la cotización."""
    with get_session() as session:
        quote = _load_for_negotiation(session, quote_id)
        return draft_order_service.fetch_shipping_rates(session, quote, auth_header)


def get_pdf_presigned_url(quote_id: str) -> str | None:
    try:
        qid = uuid.UUID(quote_id)
    except ValueError:
        return None
    with get_session() as session:
        quote = session.get(QuoteRequest, qid)
        if not quote:
            return None
        # Lazy fetch: si no tenemos copia local pero sí folio en APRO IA, descargarlo ahora.
        if not quote.pdf_s3_key and quote.apro_ia_folio:
            try:
                pdf_bytes = apro_ia_client.descargar_pdf(quote.apro_ia_folio)
                quote_pdf_service.store_quote_pdf(quote, pdf_bytes)
                session.commit()
                logger.info(
                    "PDF APRO IA recuperado en lazy fetch",
                    extra={"apro_ia_folio": quote.apro_ia_folio, "quote_id": quote_id},
                )
            except Exception:
                logger.warning(
                    "Lazy fetch PDF APRO IA falló",
                    extra={"apro_ia_folio": quote.apro_ia_folio, "quote_id": quote_id},
                )
        return quote_pdf_service.quote_pdf_presigned_url(quote)

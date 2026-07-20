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
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from aws_lambda_powertools import Logger
from sqlalchemy import func, select
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
from database.models.company import Company  # noqa: E402
from database.models.quote_request import (  # noqa: E402
    QuoteRequest,
    QuoteRequestLine,
)
from database.models.user import User  # noqa: E402
from database.models.user_company import UserCompany  # noqa: E402
from shopify.store import resolve_store_token  # noqa: E402
from notifications.sqs_publisher import (  # noqa: E402
    encolar_confirmacion_solicitud,
    encolar_cotizacion_apro_ia_estado,
    encolar_cotizacion_lista,
    encolar_nueva_cotizacion,
)
from services import apro_ia_client  # noqa: E402
from services import draft_order_service  # noqa: E402
from services import quote_pdf_service  # noqa: E402
from utils.cognito_quote_access import (  # noqa: E402
    accessible_company_uuids,
    is_platform_admin,
)
from utils.quotes_api_key import sign_pdf_token  # noqa: E402

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


def _enrich(d: dict[str, Any]) -> dict[str, Any]:
    """Agrega ``pdf_download_url`` (link tokenizado) si la cotización tiene PDF."""
    if d.get("pdf_s3_key"):
        d["pdf_download_url"] = pdf_email_url(str(d.get("id")))
    return d


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


def list_quotes(
    *,
    user: dict | None = None,
    shopify_company_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    with get_session() as session:
        stmt = select(QuoteRequest).options(selectinload(QuoteRequest.lines))
        stmt = _scope_filter(stmt, user=user, shopify_company_id=shopify_company_id)
        if status:
            stmt = stmt.where(QuoteRequest.status == status)
        count_stmt = _scope_filter(
            select(func.count(QuoteRequest.id)),
            user=user,
            shopify_company_id=shopify_company_id,
        )
        if status:
            count_stmt = count_stmt.where(QuoteRequest.status == status)
        total = session.scalar(count_stmt) or 0
        rows = session.scalars(
            stmt.order_by(QuoteRequest.created_at.desc()).limit(limit).offset(offset)
        ).all()
        return {"total": total, "items": [_enrich(r.to_dict()) for r in rows]}


def get_quote(
    quote_id: str,
    *,
    user: dict | None = None,
    shopify_company_id: str | None = None,
) -> dict[str, Any] | None:
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
        return _enrich(quote.to_dict())


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
    )
    line_no = 1
    for p in products:
        if not isinstance(p, dict):
            continue
        sku = str(p.get("sku") or "").strip()
        if not sku:
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

    changed = False
    by_sku = {(ln.sku or "").strip().lower(): ln for ln in quote.lines}
    seen_skus: set[str] = set()
    shop_domain: str | None = None
    token: str | None = None
    next_num = max((ln.line_number for ln in quote.lines), default=0) + 1

    for p in products:
        if not isinstance(p, dict):
            continue
        sku = str(p.get("sku") or "").strip()
        if not sku:
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


def handle_apro_ia_webhook(payload: dict[str, Any]) -> dict[str, Any]:
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

    with get_session() as session:
        stmt = select(QuoteRequest)
        stmt = stmt.where(
            QuoteRequest.apro_ia_id == apro_ia_id
            if apro_ia_id
            else QuoteRequest.apro_ia_folio == folio
        )
        quote = session.scalars(stmt).first()

        if not quote:
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

    productos = apro_ia_client.build_productos(quote.lines)

    try:
        if quote.apro_ia_folio:
            apro_ia_client.editar_cotizacion(
                quote.apro_ia_folio,
                productos=productos,
                comment=quote.comment,
            )
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

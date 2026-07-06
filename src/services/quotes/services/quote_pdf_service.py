"""Genera el PDF de la cotización (replica el formato de CO-156036) y lo guarda en S3.

Layout (reportlab/platypus):
  - Cabecera: "///APRO" + FOLIO + FECHA EMISIÓN.
  - Dos columnas: ENVIADA POR (Apro, datos fijos + Vendedor/a) y SOLICITADO POR (cliente).
  - Tabla: N° | SKU | DESCRIPCIÓN | CANT | PRECIO UN | TOTAL | ENTREGA  (netos, sin IVA).
  - Totales: SUBTOTAL (neto), Impuesto 19%, TOTAL.
  - CONDICIONES COMERCIALES + nota al pie.

Precios netos: el subtotal es la suma de líneas; el total = subtotal * 1.19.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any

import boto3
from aws_lambda_powertools import Logger
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

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

from database.models.quote_request import QuoteRequest  # noqa: E402

logger = Logger()

IVA_RATE = Decimal("0.19")

# Datos fijos del emisor (Apro). Configurables por env si cambian.
APRO_NAME = "Apro LTDA"
APRO_RUT = "86.887.200-4"
APRO_WEB = "https://www.apro.cl"
APRO_ADDRESS = "Avenida Americo Vespucio Norte 401, Quilicura"

_GREEN = colors.HexColor("#16a34a")
_DARK = colors.HexColor("#1f2937")
_HEADER_BG = colors.HexColor("#111827")
_TOTAL_BG = colors.HexColor("#FEF9C3")
_MUTED = colors.HexColor("#6B7280")


@lru_cache(maxsize=1)
def _s3() -> Any:
    return boto3.client("s3")


def _bucket() -> str:
    name = (os.environ.get("QUOTES_PDF_BUCKET") or "").strip()
    if not name:
        raise RuntimeError("QUOTES_PDF_BUCKET no está configurado en el entorno")
    return name


def _fmt_clp(value: Decimal) -> str:
    n = int(value.quantize(Decimal("1")))
    return "$" + f"{n:,}".replace(",", ".")


def _fmt_date(dt: datetime | None) -> str:
    d = dt or datetime.now(timezone.utc)
    return d.strftime("%d-%m-%Y")


def _line_net_total(ln: Any) -> Decimal:
    unit = ln.negotiated_unit_price
    if unit is None:
        unit = ln.requested_unit_price
    if unit is None:
        unit = Decimal("0")
    return Decimal(unit) * Decimal(int(ln.quantity or 1))


def _shipping_net(quote: QuoteRequest) -> Decimal:
    """Devuelve el monto neto del envío (0 si no hay)."""
    sl = quote.shipping_line
    if not isinstance(sl, dict):
        return Decimal("0")
    try:
        return Decimal(str(sl.get("price") or "0"))
    except Exception:
        return Decimal("0")


def quote_totals(quote: QuoteRequest) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Devuelve ``(subtotal_neto, shipping_neto, iva, total)``."""
    subtotal = sum((_line_net_total(ln) for ln in quote.lines), Decimal("0"))
    shipping = _shipping_net(quote)
    base_imponible = subtotal + shipping
    iva = (base_imponible * IVA_RATE).quantize(Decimal("1"))
    total = base_imponible + iva
    return subtotal, shipping, iva, total


def generate_quote_pdf(
    quote: QuoteRequest,
    *,
    vendedor_name: str = "",
    generated_at: datetime | None = None,
) -> bytes:
    """Renderiza el PDF de la cotización y devuelve los bytes."""
    company = quote.company
    cliente = (
        (company.billing_razon_social if company else None)
        or (company.name if company else None)
        or quote.requested_by_name
        or "-"
    )
    rut = (company.billing_rut if company else None) or "-"

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=f"Cotización {quote.folio or ''}".strip(),
    )

    styles = getSampleStyleSheet()
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8, leading=11)
    small_muted = ParagraphStyle("small_muted", parent=small, textColor=_MUTED)
    label = ParagraphStyle(
        "label", parent=small, textColor=_MUTED, fontSize=7.5, leading=10
    )
    cell = ParagraphStyle("cell", parent=styles["Normal"], fontSize=8, leading=10)
    head_cell = ParagraphStyle(
        "head_cell", parent=cell, textColor=colors.white, fontName="Helvetica-Bold"
    )

    elems: list[Any] = []

    # ---- Cabecera: logo + folio/fecha
    logo = Paragraph(
        '<font color="#16a34a"><b>///APRO</b></font>',
        ParagraphStyle("logo", parent=styles["Normal"], fontSize=22),
    )
    meta = Paragraph(
        f'<font color="#6B7280">FOLIO: {quote.folio or "-"}<br/>'
        f"FECHA EMISIÓN: {_fmt_date(generated_at or quote.pdf_generated_at)}</font>",
        ParagraphStyle("meta", parent=small, alignment=2, fontSize=9, leading=13),
    )
    head = Table([[logo, meta]], colWidths=[90 * mm, 84 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    elems.append(head)
    elems.append(Spacer(1, 10))

    # ---- Dos columnas: ENVIADA POR / SOLICITADO POR
    enviada = [
        Paragraph("ENVIADA POR", label),
        Paragraph(f"<b>{APRO_NAME}</b>", small),
        Paragraph(f"<b>Rut:</b> {APRO_RUT}", small),
        Paragraph(f"<b>Sitio web:</b> {APRO_WEB}", small),
        Paragraph(f"<b>Dirección:</b> {APRO_ADDRESS}", small),
        Paragraph(f"<b>Vendedor/a:</b> {vendedor_name or '-'}", small),
        Paragraph(f"<b>Asistente:</b> {vendedor_name or '-'}", small),
    ]
    solicitada = [
        Paragraph("SOLICITADO POR", label),
        Paragraph(f"<b>Cliente:</b> {cliente}", small),
        Paragraph(f"<b>RUT:</b> {rut}", small),
        Paragraph(f"<b>Contacto:</b> {quote.requested_by_name or '-'}", small),
        Paragraph(f"<b>Email:</b> {quote.requested_by_email or '-'}", small),
        Paragraph("<b>Teléfono:</b> -", small),
    ]
    rows = max(len(enviada), len(solicitada))
    enviada += [Spacer(1, 1)] * (rows - len(enviada))
    solicitada += [Spacer(1, 1)] * (rows - len(solicitada))
    two_col = Table(
        [[enviada[i], solicitada[i]] for i in range(rows)],
        colWidths=[87 * mm, 87 * mm],
    )
    two_col.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEABOVE", (0, 0), (-1, 0), 0.5, colors.HexColor("#E5E7EB")),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]
        )
    )
    elems.append(two_col)
    elems.append(Spacer(1, 14))

    # ---- Tabla de líneas
    table_header = [
        Paragraph("N°", head_cell),
        Paragraph("SKU", head_cell),
        Paragraph("DESCRIPCIÓN", head_cell),
        Paragraph("CANT", head_cell),
        Paragraph("PRECIO UN", head_cell),
        Paragraph("TOTAL", head_cell),
        Paragraph("ENTREGA", head_cell),
    ]
    data: list[list[Any]] = [table_header]
    for idx, ln in enumerate(sorted(quote.lines, key=lambda x: x.line_number), start=1):
        unit = ln.negotiated_unit_price
        if unit is None:
            unit = ln.requested_unit_price or Decimal("0")
        total = _line_net_total(ln)
        data.append(
            [
                Paragraph(str(idx), cell),
                Paragraph(ln.sku or "-", cell),
                Paragraph(ln.title or "-", cell),
                Paragraph(str(ln.quantity or 1), cell),
                Paragraph(_fmt_clp(Decimal(unit)), cell),
                Paragraph(_fmt_clp(total), cell),
                Paragraph("-", cell),
            ]
        )
    table = Table(
        data,
        colWidths=[8 * mm, 28 * mm, 70 * mm, 12 * mm, 20 * mm, 20 * mm, 16 * mm],
        repeatRows=1,
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), _HEADER_BG),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LINEBELOW", (0, 1), (-1, -1), 0.4, colors.HexColor("#E5E7EB")),
                ("ALIGN", (3, 0), (6, -1), "CENTER"),
                ("ALIGN", (4, 0), (5, -1), "RIGHT"),
            ]
        )
    )
    elems.append(table)
    elems.append(Spacer(1, 10))

    # ---- Totales
    subtotal, shipping, iva, total = quote_totals(quote)
    bold_right = ParagraphStyle(
        "bold_right", parent=cell, alignment=2, fontName="Helvetica-Bold"
    )
    shipping_label = "Despacho"
    if isinstance(quote.shipping_line, dict) and quote.shipping_line.get("title"):
        shipping_label = str(quote.shipping_line["title"])
    totals_data = [
        [Paragraph("SUBTOTAL:", bold_right), Paragraph(_fmt_clp(subtotal), bold_right)],
    ]
    if shipping > 0:
        totals_data.append(
            [Paragraph(f"{shipping_label}:", bold_right), Paragraph(_fmt_clp(shipping), bold_right)]
        )
    totals_data += [
        [Paragraph("Impuesto 19%:", bold_right), Paragraph(_fmt_clp(iva), bold_right)],
        [Paragraph("TOTAL:", bold_right), Paragraph(_fmt_clp(total), bold_right)],
    ]
    totals = Table(
        totals_data,
        colWidths=[40 * mm, 30 * mm],
        hAlign="RIGHT",
    )
    total_row = len(totals_data) - 1
    totals.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, total_row), (-1, total_row), _TOTAL_BG),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    elems.append(totals)
    elems.append(Spacer(1, 16))

    # ---- Condiciones comerciales
    elems.append(Paragraph("CONDICIONES COMERCIALES", label))
    elems.append(Spacer(1, 4))
    cond = Table(
        [
            [
                Paragraph("Forma de pago", label),
                Paragraph("Validez de la oferta", label),
                Paragraph("Moneda", label),
            ],
            [
                Paragraph(quote.payment_terms_label or "Efectivo", small),
                Paragraph(f"{quote.validity_days or 14} días", small),
                Paragraph("CLP - Pesos Chilenos", small),
            ],
        ],
        colWidths=[58 * mm, 58 * mm, 58 * mm],
    )
    cond.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, 0), (-1, 0), 0.5, colors.HexColor("#E5E7EB")),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    elems.append(cond)
    if quote.comment:
        elems.append(Spacer(1, 10))
        elems.append(Paragraph("Comentario", label))
        elems.append(Paragraph(quote.comment, small))
    elems.append(Spacer(1, 14))
    elems.append(
        Paragraph(
            '<font color="#2563EB">- FAVOR HACER REFERENCIA AL NÚMERO DE '
            "COTIZACIÓN EN SU ORDEN DE COMPRA</font>",
            small,
        )
    )

    doc.build(elems)
    return buf.getvalue()


def _pdf_key(quote: QuoteRequest, generated_at: datetime) -> str:
    return f"quotes/{generated_at:%Y/%m}/{quote.id}/cotizacion.pdf"


def store_quote_pdf(quote: QuoteRequest, pdf_bytes: bytes) -> tuple[str, str]:
    """Sube el PDF a S3 y devuelve ``(bucket, key)``. Persiste en la quote."""
    bucket = _bucket()
    now = datetime.now(timezone.utc)
    key = _pdf_key(quote, now)
    try:
        _s3().put_object(
            Bucket=bucket,
            Key=key,
            Body=pdf_bytes,
            ContentType="application/pdf",
            ServerSideEncryption="AES256",
            Metadata={"quote-id": str(quote.id), "folio": quote.folio or ""},
        )
    except Exception as e:
        logger.exception("Error subiendo PDF de cotización a S3", extra={"key": key})
        raise RuntimeError(f"Error subiendo PDF a S3: {e}") from e
    quote.pdf_s3_bucket = bucket
    quote.pdf_s3_key = key
    quote.pdf_generated_at = now
    return bucket, key


def quote_pdf_presigned_url(quote: QuoteRequest, expires_in: int = 3600) -> str | None:
    """Presigned GET del PDF (o ``None`` si aún no se generó)."""
    if not quote.pdf_s3_bucket or not quote.pdf_s3_key:
        return None
    return _s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": quote.pdf_s3_bucket, "Key": quote.pdf_s3_key},
        ExpiresIn=expires_in,
    )

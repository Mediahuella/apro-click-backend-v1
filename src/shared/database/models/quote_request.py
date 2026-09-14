"""Cotizaciones B2B (RFQ) — fuente de verdad del flujo de precios negociados.

El cliente solicita desde el carrito (líneas a pedido, ``stock <= 0``); SALES
negocia precios y crea un Draft Order en Shopify. El ciclo de vida y la
auditoría viven en esta tabla; el Draft Order es solo el vehículo de
precio + checkout (Shopify purga drafts inactivos tras 1 año).

Estados: ``SOLICITADA → COTIZADA → INVOICE_SENT → ACEPTADA`` (o
``RECHAZADA``/``EXPIRADA``). Espejan ``OPEN → INVOICE_SENT → COMPLETED`` del draft.
``PAGO_RECHAZADO`` la fija el webhook de pagos de APRO IA (transferencia o
crédito) cuando el pago queda ``Descartada``/``Anulada``: el pedido en Shopify
**no** se anula —sigue abierto y pendiente de pago, solo etiquetado— así que el
estado existe para que SALES lo vea y le haga seguimiento. ``ANULADA`` queda
para la anulación manual y para las filas anteriores a ese cambio
(ver ``docs/Transferencias APRO IA.md``).

Convención de impuestos: los precios (``requested_unit_price`` /
``negotiated_unit_price``) se guardan **netos** (sin IVA). El IVA 19% se calcula
al renderizar el PDF / al armar el draft según la config de impuestos de la tienda.
"""
from __future__ import annotations

import uuid as uuid_mod
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base, DictMixin, TimestampMixin

QUOTE_STATUSES = (
    "SOLICITADA",
    "COTIZADA",
    "INVOICE_SENT",
    "ACEPTADA",
    "RECHAZADA",
    "EXPIRADA",
    "ANULADA",
    "PAGO_RECHAZADO",
)


class QuoteRequest(Base, TimestampMixin, DictMixin):
    __tablename__ = "quote_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('SOLICITADA', 'COTIZADA', 'INVOICE_SENT', "
            "'ACEPTADA', 'RECHAZADA', 'EXPIRADA', 'ANULADA', "
            "'PAGO_RECHAZADO')",
            name="ck_quote_requests_status",
        ),
    )

    id: Mapped[uuid_mod.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid_mod.uuid4
    )
    #: Folio formal ``CO-NNNNNN`` — se asigna al pasar a COTIZADA (secuencia BD).
    folio: Mapped[str | None] = mapped_column(
        String(32), nullable=True, unique=True, index=True
    )
    company_id: Mapped[uuid_mod.UUID | None] = mapped_column(
        Uuid, ForeignKey("companies.id"), nullable=True, index=True
    )
    shopify_company_id: Mapped[str | None] = mapped_column(String, nullable=True)
    company_location_gid: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    shop_domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    requested_by_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    requested_by_customer_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    requested_by_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="SOLICITADA", index=True
    )

    draft_order_gid: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )
    order_gid: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    #: Descuento global negociado a nivel orden (``{valueType, value, title, description}``).
    order_applied_discount: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    #: Condiciones comerciales para el PDF.
    payment_terms_label: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    validity_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=14, server_default="14"
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    staff_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Token del carrito de Shopify al momento de solicitar (trazabilidad).
    cart_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    #: Envío solicitado por el cliente desde el carrito (``{title, price}`` neto).
    shipping_line: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    #: PDF de la cotización formal (generado al pasar a COTIZADA).
    pdf_s3_bucket: Mapped[str | None] = mapped_column(String, nullable=True)
    pdf_s3_key: Mapped[str | None] = mapped_column(String, nullable=True)
    pdf_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Referencia al registro en APRO IA (DMS externo). Criterio principal de
    #: matching del webhook — el folio no es único entre cotizaciones (ver
    #: ``handle_apro_ia_webhook``).
    apro_ia_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    apro_ia_folio: Mapped[str | None] = mapped_column(
        String(20), nullable=True, index=True
    )
    #: Último estado reportado por el webhook de APRO IA (``Pendiente``,
    #: ``Pendiente Gerente Comercial``, ``Autorizada``, ``Rechazada``). No
    #: reemplaza ``status`` (ciclo Shopify/draft); es informativo del DMS externo.
    apro_ia_status: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: Identificadores del **espejo en el ambiente QA** de APRO IA
    #: (``dms-qa.apro.cl``). Solo se pueblan para las empresas de
    #: ``APRO_IA_QA_ALLOWED_RUTS``. Van aparte de ``apro_ia_id``/
    #: ``apro_ia_folio`` a propósito: QA y producción son secuencias de IDs
    #: independientes y un ``id`` de QA puede colisionar con el de una
    #: cotización real de otro cliente. El webhook de la ruta ``/qa`` matchea
    #: solo contra estas columnas y el de producción solo contra las otras.
    apro_ia_qa_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    apro_ia_qa_folio: Mapped[str | None] = mapped_column(
        String(20), nullable=True, index=True
    )

    #: Folio interno que APRO IA asigna al **documento de pago** (transferencia
    #: o crédito), distinto de ``apro_ia_folio`` (folio de la cotización/OC).
    #: Lo reporta el webhook de pagos.
    transfer_folio: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    #: Último estado reportado para ese documento de pago (``Aprobada``,
    #: ``Descartada``/``En papelera``, ``Anulada``). Crudo, tal cual lo envía
    #: APRO IA — la normalización vive en el servicio.
    transfer_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Momento en que el pedido de Shopify quedó marcado como pagado
    #: (``orderMarkAsPaid`` exitoso). Sirve de guarda de idempotencia ante
    #: reintentos del webhook.
    transfer_paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Medio de pago que confirmó APRO IA sobre las columnas ``transfer_*``:
    #: ``transferencia`` o ``credito``. Los dos webhooks comparten esas
    #: columnas porque el ciclo es idéntico (confirmar o anular un pedido que
    #: nació impago del checkout); esta columna es lo único que los distingue
    #: a posteriori. ``NULL`` en las filas previas a la migración ``029``, que
    #: por definición son transferencias.
    payment_method: Mapped[str | None] = mapped_column(String(16), nullable=True)

    company = relationship("Company")
    lines = relationship(
        "QuoteRequestLine",
        back_populates="quote",
        cascade="all, delete-orphan",
        order_by="QuoteRequestLine.line_number",
    )

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data["lines"] = [ln.to_dict() for ln in (self.lines or [])]
        # Nombre de la empresa CRM. El frontend no debe resolverlo cruzando
        # ``company_id`` contra su listado de empresas: ese viene paginado y,
        # cuando la empresa cae fuera, la tabla terminaba mostrando el nombre
        # de la persona que pidió la cotización en vez del de la empresa.
        data["company_name"] = self.company.name if self.company else None
        return data


class QuoteRequestLine(Base, TimestampMixin, DictMixin):
    __tablename__ = "quote_request_lines"

    id: Mapped[uuid_mod.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid_mod.uuid4
    )
    quote_request_id: Mapped[uuid_mod.UUID] = mapped_column(
        Uuid,
        ForeignKey("quote_requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    variant_gid: Mapped[str] = mapped_column(String, nullable=False)
    sku: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: Era a pedido (``stock <= 0``) al momento de solicitar.
    on_demand: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    #: Precio NETO (sin IVA) que veía el cliente / catálogo de segmento.
    requested_unit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 4), nullable=True
    )
    #: Precio NETO (sin IVA) negociado por SALES — ``priceOverride`` del draft.
    negotiated_unit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 4), nullable=True
    )
    #: Descuento por línea opcional (``{valueType, value, title}``).
    applied_discount: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    currency: Mapped[str] = mapped_column(
        String(8), nullable=False, default="CLP", server_default="CLP"
    )

    quote = relationship("QuoteRequest", back_populates="lines")

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        for k, v in list(data.items()):
            if isinstance(v, Decimal):
                data[k] = str(v) if v is not None else None
        return data

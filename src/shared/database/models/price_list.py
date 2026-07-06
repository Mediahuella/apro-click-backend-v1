"""Price-list ORM models — log de cargas Excel + IDs de catálogos B2B en Shopify.

La fuente de verdad de los precios vive en Shopify (B2B Catalogs / PriceLists).
Estas tablas sólo son para auditoría de uploads y para conservar los GIDs de
los recursos creados en Shopify por el servicio ``prices``.
"""
from __future__ import annotations

import uuid as uuid_mod
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base, DictMixin, TimestampMixin

# Status válidos para ``PriceListUpload.status``.
UPLOAD_STATUSES = frozenset(
    {
        "PENDING",  # subido, mensaje SQS encolado
        "PROCESSING",  # worker tomó el job
        "PUSHED",  # bulk ops creadas en Shopify (esperan completarse)
        "COMPLETED",  # las 3 bulk ops terminaron OK
        "PARTIAL",  # alguna bulk op quedó FAILED y otras OK
        "FAILED",  # error fatal antes de poder mandar a Shopify
    }
)

# Status que devuelve Shopify para ``BulkOperation``: CREATED, RUNNING,
# COMPLETED, FAILED, CANCELED, EXPIRED. Lo guardamos como texto plano.

VALID_SEGMENTS = frozenset({"PYME", "MEDIANA", "GRAN_EMPRESA"})

# Status válidos para ``B2cPriceUpload.status`` (carga de precios B2C/WEB en la
# tienda Shopify B2C — sin segmentos, escribe el precio por defecto de cada
# variante).
B2C_UPLOAD_STATUSES = frozenset(
    {
        "PENDING",  # subido, mensaje SQS encolado
        "PROCESSING",  # worker tomó el job (o continuación re-encolada)
        "COMPLETED",  # todas las variantes objetivo quedaron actualizadas
        "PARTIAL",  # hubo errores en algunas variantes
        "FAILED",  # error fatal antes de poder escribir en Shopify
    }
)


class PriceListUpload(Base, TimestampMixin, DictMixin):
    """Log de cada Excel subido por el admin para actualizar precios B2B.

    Se llena en dos momentos:

    1. Al recibir el upload (``status='PENDING'`` + datos del archivo en S3).
    2. Al ejecutar el worker (``status`` actualizándose hasta ``COMPLETED`` /
       ``FAILED`` y los GIDs de las 3 bulk operations en Shopify).
    """

    __tablename__ = "price_list_uploads"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','PROCESSING','PUSHED','COMPLETED','PARTIAL','FAILED')",
            name="ck_price_list_uploads_status",
        ),
    )

    id: Mapped[uuid_mod.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid_mod.uuid4
    )
    source_filename: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )
    s3_bucket: Mapped[str | None] = mapped_column(String(255), nullable=True)
    s3_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PENDING", server_default="PENDING"
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Métricas del parser (rellenadas por el worker).
    parsed_items: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    duplicates_overwritten: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    rows_skipped: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # Resultado del lookup SKU → variantId.
    variants_resolved: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    variants_missing: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    missing_skus_sample: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )

    # Bulk operations de Shopify por segmento.
    pyme_bulk_operation_gid: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    mediana_bulk_operation_gid: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    gran_empresa_bulk_operation_gid: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    pyme_bulk_status: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    mediana_bulk_status: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    gran_empresa_bulk_status: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )

    uploaded_by_user_id: Mapped[uuid_mod.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


class ShopifyPriceSegment(Base, TimestampMixin, DictMixin):
    """Catálogo B2B + PriceList de Shopify por segmento de empresa.

    Una fila por cada uno de PYME / MEDIANA / GRAN_EMPRESA. El servicio
    ``prices`` la rellena (``catalog_gid``, ``price_list_gid``) la primera vez
    que se sube un Excel: si el segmento todavía no tiene catálogo, lo crea
    contra Shopify y guarda los GIDs aquí; uploads posteriores reutilizan los
    mismos recursos y sólo le sobreescriben los precios.
    """

    __tablename__ = "shopify_price_segments"
    __table_args__ = (
        CheckConstraint(
            "segment IN ('PYME', 'MEDIANA', 'GRAN_EMPRESA')",
            name="ck_shopify_price_segments_segment",
        ),
    )

    id: Mapped[uuid_mod.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid_mod.uuid4
    )
    segment: Mapped[str] = mapped_column(
        String(32), nullable=False, unique=True
    )
    shop_domain: Mapped[str] = mapped_column(String(255), nullable=False)
    catalog_gid: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    price_list_gid: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="CLP", server_default="CLP"
    )
    company_location_ids: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="CSV de gid://shopify/CompanyLocation/... asociados al catálogo.",
    )


class B2cPriceUpload(Base, TimestampMixin, DictMixin):
    """Log de cada Excel subido para actualizar precios B2C/WEB.

    A diferencia de :class:`PriceListUpload` (que arma PriceLists/Catalogs B2B
    por segmento en la tienda ``aproclick``), este flujo escribe el **precio por
    defecto** de cada variante (``price`` + ``compareAtPrice`` iguales al valor
    de la columna "B2C/WEB (iva incluido)") en una tienda B2C **distinta**
    (``aproltda``), cuyas credenciales viven en variables de entorno del Lambda,
    no en ``shopify_app_installations``.

    El worker es idempotente: ``productVariantsBulkUpdate`` fija valores
    absolutos y el diff omite las variantes ya correctas, así que si una
    invocación se queda sin tiempo re-encola el mismo ``id`` y la siguiente
    continúa donde quedó.
    """

    __tablename__ = "b2c_price_uploads"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','PROCESSING','COMPLETED','PARTIAL','FAILED')",
            name="ck_b2c_price_uploads_status",
        ),
    )

    id: Mapped[uuid_mod.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid_mod.uuid4
    )
    source_filename: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )
    s3_bucket: Mapped[str | None] = mapped_column(String(255), nullable=True)
    s3_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="PENDING", server_default="PENDING"
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Dominio de la tienda B2C objetivo (informativo / para linkear al admin).
    shop_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Métricas del parser + del diff/aplicación (rellenadas por el worker).
    parsed_items: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    variants_matched: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    variants_updated: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    variants_already_ok: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    variants_missing: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    missing_skus_sample: Mapped[str | None] = mapped_column(Text, nullable=True)

    uploaded_by_user_id: Mapped[uuid_mod.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

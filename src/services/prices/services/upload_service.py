"""Capa pública usada por los handlers HTTP del servicio prices.

Crea la fila ``price_list_uploads``, sube el Excel a S3 y encola el job para
el worker. También expone listar / detallar / refrescar.
"""
from __future__ import annotations

import json
import os
import sys
import uuid as uuid_mod
from functools import lru_cache
from pathlib import Path
from typing import Any

import boto3
from aws_lambda_powertools import Logger

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

from sqlalchemy import func, select  # noqa: E402

from database.engine import get_session  # noqa: E402
from database.models.price_list import (  # noqa: E402
    PriceListUpload,
    ShopifyPriceSegment,
)

from services.s3_storage import put_excel  # noqa: E402

logger = Logger()


@lru_cache(maxsize=1)
def _sqs():
    return boto3.client("sqs")


def _queue_url() -> str:
    url = (os.environ.get("PRICES_UPLOADS_QUEUE_URL") or "").strip()
    if not url:
        raise RuntimeError(
            "PRICES_UPLOADS_QUEUE_URL no está configurado en el entorno"
        )
    return url


def _resolve_uuid(raw: Any, *, label: str) -> uuid_mod.UUID:
    if isinstance(raw, uuid_mod.UUID):
        return raw
    s = (str(raw) if raw is not None else "").strip()
    if not s:
        raise ValueError(f"{label} es obligatorio")
    try:
        return uuid_mod.UUID(s)
    except ValueError as e:
        raise ValueError(f"{label} inválido: {raw!r}") from e


def _serialize_upload(row: PriceListUpload) -> dict[str, Any]:
    """``to_dict`` + agrupación por segmento para la API."""
    d = row.to_dict()
    segments = {
        "PYME": {
            "bulk_operation_gid": d.pop("pyme_bulk_operation_gid", None),
            "bulk_status": d.pop("pyme_bulk_status", None),
        },
        "MEDIANA": {
            "bulk_operation_gid": d.pop("mediana_bulk_operation_gid", None),
            "bulk_status": d.pop("mediana_bulk_status", None),
        },
        "GRAN_EMPRESA": {
            "bulk_operation_gid": d.pop("gran_empresa_bulk_operation_gid", None),
            "bulk_status": d.pop("gran_empresa_bulk_status", None),
        },
    }
    d["segments"] = segments
    return d


class PriceUploadService:
    """Operaciones expuestas a la capa HTTP del servicio prices."""

    # -------- escritura --------

    def create_upload(
        self,
        *,
        file_bytes: bytes,
        filename: str | None,
        notes: str | None,
        uploaded_by_user_id: uuid_mod.UUID | None,
    ) -> dict[str, Any]:
        """Crea fila + sube Excel a S3 + encola SQS. Devuelve el header."""
        upload_id = uuid_mod.uuid4()
        bucket, key = put_excel(
            str(upload_id),
            file_bytes=file_bytes,
            filename=filename,
        )

        with get_session() as session:
            row = PriceListUpload(
                id=upload_id,
                source_filename=filename or None,
                s3_bucket=bucket,
                s3_key=key,
                status="PENDING",
                notes=notes or None,
                uploaded_by_user_id=uploaded_by_user_id,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            data = _serialize_upload(row)

        # Encolar el job (worker async).
        try:
            _sqs().send_message(
                QueueUrl=_queue_url(),
                MessageBody=json.dumps({"upload_id": str(upload_id)}),
            )
        except Exception as e:
            logger.exception(
                "Error enviando mensaje SQS — el upload queda en PENDING; "
                "puede reintentarse llamando POST /uploads/{id}/refresh",
                extra={"upload_id": str(upload_id)},
            )
            # Marcar como FAILED para que el admin vea el error.
            with get_session() as session:
                row = session.get(PriceListUpload, upload_id)
                if row is not None:
                    row.status = "FAILED"
                    row.error_message = (
                        f"No se pudo encolar el job de procesamiento: {e}"
                    )[:2000]
                    session.commit()
                    data = _serialize_upload(row)
        return data

    # -------- lecturas --------

    def list_uploads(
        self, *, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        with get_session() as session:
            rows = list(
                session.scalars(
                    select(PriceListUpload)
                    .order_by(PriceListUpload.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                ).all()
            )
            total = int(
                session.scalar(select(func.count(PriceListUpload.id))) or 0
            )
            uploads = [_serialize_upload(r) for r in rows]
        return {
            "uploads": uploads,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get_upload(self, upload_id: Any) -> dict[str, Any] | None:
        uid = _resolve_uuid(upload_id, label="upload_id")
        with get_session() as session:
            row = session.get(PriceListUpload, uid)
            if not row:
                return None
            return _serialize_upload(row)

    def list_segments(self) -> dict[str, Any]:
        with get_session() as session:
            rows = list(
                session.scalars(
                    select(ShopifyPriceSegment).order_by(
                        ShopifyPriceSegment.segment.asc()
                    )
                ).all()
            )
            segments = [r.to_dict() for r in rows]
        return {"segments": segments}

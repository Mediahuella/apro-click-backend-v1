"""Capa pública de los handlers HTTP B2C del servicio prices.

Crea la fila ``b2c_price_uploads``, sube el Excel a S3 (prefijo ``b2c/``) y
encola el job para el worker B2C. También expone listar / detallar.
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
from database.models.price_list import B2cPriceUpload  # noqa: E402

from services.s3_storage import put_excel  # noqa: E402

logger = Logger()

S3_PREFIX = "b2c"


@lru_cache(maxsize=1)
def _sqs():
    return boto3.client("sqs")


def b2c_queue_url() -> str:
    url = (os.environ.get("B2C_PRICE_UPLOADS_QUEUE_URL") or "").strip()
    if not url:
        raise RuntimeError(
            "B2C_PRICE_UPLOADS_QUEUE_URL no está configurado en el entorno"
        )
    return url


def enqueue_b2c_upload(upload_id: uuid_mod.UUID) -> None:
    """Encola (o re-encola, para continuación) el job del worker B2C."""
    _sqs().send_message(
        QueueUrl=b2c_queue_url(),
        MessageBody=json.dumps({"upload_id": str(upload_id)}),
    )


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


class B2cPriceUploadService:
    """Operaciones expuestas a la capa HTTP B2C del servicio prices."""

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
            prefix=S3_PREFIX,
        )

        with get_session() as session:
            row = B2cPriceUpload(
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
            data = row.to_dict()

        try:
            enqueue_b2c_upload(upload_id)
        except Exception as e:
            logger.exception(
                "Error enviando mensaje SQS B2C — el upload queda en FAILED",
                extra={"upload_id": str(upload_id)},
            )
            with get_session() as session:
                row = session.get(B2cPriceUpload, upload_id)
                if row is not None:
                    row.status = "FAILED"
                    row.error_message = (
                        f"No se pudo encolar el job de procesamiento: {e}"
                    )[:2000]
                    session.commit()
                    data = row.to_dict()
        return data

    def list_uploads(
        self, *, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        with get_session() as session:
            rows = list(
                session.scalars(
                    select(B2cPriceUpload)
                    .order_by(B2cPriceUpload.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                ).all()
            )
            total = int(
                session.scalar(select(func.count(B2cPriceUpload.id))) or 0
            )
            uploads = [r.to_dict() for r in rows]
        return {
            "uploads": uploads,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get_upload(self, upload_id: Any) -> dict[str, Any] | None:
        uid = _resolve_uuid(upload_id, label="upload_id")
        with get_session() as session:
            row = session.get(B2cPriceUpload, uid)
            if not row:
                return None
            return row.to_dict()

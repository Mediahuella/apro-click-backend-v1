"""S3 helper para guardar / leer los Excel subidos por el admin."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

import boto3
from aws_lambda_powertools import Logger

logger = Logger()


@lru_cache(maxsize=1)
def _client() -> Any:
    return boto3.client("s3")


def uploads_bucket() -> str:
    name = (os.environ.get("PRICES_UPLOADS_BUCKET") or "").strip()
    if not name:
        raise RuntimeError(
            "PRICES_UPLOADS_BUCKET no está configurado en el entorno"
        )
    return name


def build_object_key(upload_id: str, *, filename: str | None) -> str:
    """Convención del path en S3.

    ``YYYY/MM/<upload_id>/<filename or excel.xlsx>``. El upload_id va incluido
    para no colisionar y para hacer fácil el clean-up manual si hace falta.
    """
    now = datetime.now(timezone.utc)
    safe_name = (filename or "").strip().replace("/", "_") or "lista.xlsx"
    return f"{now:%Y/%m}/{upload_id}/{safe_name}"


def put_excel(
    upload_id: str,
    *,
    file_bytes: bytes,
    filename: str | None,
    content_type: str = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
) -> tuple[str, str]:
    """Sube ``file_bytes`` a S3 bajo una key derivada de ``upload_id``.

    Devuelve ``(bucket, key)``. Lanza ``RuntimeError`` si el bucket no está
    configurado o si la llamada a S3 falla.
    """
    bucket = uploads_bucket()
    key = build_object_key(upload_id, filename=filename)
    metadata = {"upload-id": upload_id}
    if filename:
        metadata["original-filename"] = filename[:200]
    try:
        _client().put_object(
            Bucket=bucket,
            Key=key,
            Body=file_bytes,
            ContentType=content_type,
            ServerSideEncryption="AES256",
            Metadata=metadata,
        )
    except Exception as e:
        logger.exception("Error subiendo Excel a S3", extra={"key": key})
        raise RuntimeError(f"Error subiendo Excel a S3: {e}") from e
    return bucket, key


def get_excel(bucket: str, key: str) -> bytes:
    """Descarga el objeto y devuelve el body completo."""
    try:
        resp = _client().get_object(Bucket=bucket, Key=key)
        return resp["Body"].read()
    except Exception as e:
        logger.exception(
            "Error descargando Excel de S3",
            extra={"bucket": bucket, "key": key},
        )
        raise RuntimeError(f"Error descargando Excel de S3: {e}") from e

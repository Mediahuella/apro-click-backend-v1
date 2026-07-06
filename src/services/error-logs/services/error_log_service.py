"""Persistencia de logs de errores del frontend en DynamoDB.

Diseño de claves (volumen bajo, una sola partición):
- PK (HASH)  = "LOG"  (constante)
- SK (RANGE) = "<created_at ISO-8601>#<uid>"  → query ScanIndexForward=False = más reciente primero
TTL de 30 días sobre el atributo `ttl` (epoch en segundos).
"""
from __future__ import annotations

import base64
import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3
import boto3.dynamodb.conditions as cond

PARTITION = "LOG"
TTL_SECONDS = 30 * 24 * 60 * 60  # 30 días
MAX_FIELD_LEN = 10_000  # acota stack/message para mantener el item pequeño


def _truncate(value: Any, limit: int = MAX_FIELD_LEN) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text[:limit]


def _encode_cursor(key: dict[str, Any] | None) -> str | None:
    if not key:
        return None
    return base64.urlsafe_b64encode(json.dumps(key).encode()).decode()


def _decode_cursor(cursor: str | None) -> dict[str, Any] | None:
    if not cursor:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    except (ValueError, json.JSONDecodeError) as e:
        raise ValueError("cursor inválido") from e


class ErrorLogService:
    def __init__(self) -> None:
        self._table_name = os.environ["ERROR_LOGS_TABLE"]
        self._table = boto3.resource("dynamodb").Table(self._table_name)

    def create(self, payload: dict[str, Any], user: dict[str, Any]) -> dict[str, str]:
        now = datetime.now(timezone.utc)
        created_at = now.isoformat()
        uid = uuid.uuid4().hex

        item = {
            "PK": PARTITION,
            "SK": f"{created_at}#{uid}",
            "id": uid,
            "created_at": created_at,
            "ttl": int(time.time()) + TTL_SECONDS,
            "message": _truncate(payload.get("message")) or "(sin mensaje)",
            "name": _truncate(payload.get("name"), 200),
            "stack": _truncate(payload.get("stack")),
            "url": _truncate(payload.get("url"), 1000),
            "user_agent": _truncate(payload.get("userAgent"), 500),
            "environment": _truncate(payload.get("environment"), 50),
            "occurred_at": _truncate(payload.get("occurredAt"), 50),
            "context": _truncate(payload.get("context")),
            "user_id": user.get("id"),
            "user_email": user.get("email"),
            "role": user.get("role"),
        }
        # DynamoDB no acepta atributos None.
        item = {k: v for k, v in item.items() if v is not None}
        self._table.put_item(Item=item)
        return {"id": uid}

    def list(self, limit: int = 100, cursor: str | None = None) -> dict[str, Any]:
        limit = max(1, min(limit, 200))
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": cond.Key("PK").eq(PARTITION),
            "ScanIndexForward": False,
            "Limit": limit,
        }
        start_key = _decode_cursor(cursor)
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key

        resp = self._table.query(**kwargs)
        items = [
            {k: v for k, v in row.items() if k not in ("PK", "SK", "ttl")}
            for row in resp.get("Items", [])
        ]
        return {
            "items": items,
            "next_cursor": _encode_cursor(resp.get("LastEvaluatedKey")),
        }

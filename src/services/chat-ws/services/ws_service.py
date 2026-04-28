"""Gestión de conexiones WebSocket en DynamoDB."""
from __future__ import annotations

import os
import time
from typing import Any

import boto3
import boto3.dynamodb.conditions as cond
from aws_lambda_powertools import Logger

logger = Logger()

_CONNECTION_TTL_SECONDS = 7200  # 2 horas; se renueva en cada mensaje recibido


class WsService:
    def __init__(self) -> None:
        self._table_name = os.environ["WS_CONNECTIONS_TABLE"]
        self._ddb = boto3.resource("dynamodb")
        self._table = self._ddb.Table(self._table_name)

    def register_connection(
        self,
        connection_id: str,
        conversation_id: str,
        sender_type: str,
        actor_id: str,
    ) -> None:
        self._table.put_item(
            Item={
                "connection_id": connection_id,
                "conversation_id": conversation_id,
                "sender_type": sender_type,
                "actor_id": actor_id,
                "ttl": int(time.time()) + _CONNECTION_TTL_SECONDS,
            }
        )
        logger.info(
            "Conexión WS registrada",
            extra={
                "connection_id": connection_id,
                "conversation_id": conversation_id,
                "sender_type": sender_type,
            },
        )

    def remove_connection(self, connection_id: str) -> None:
        self._table.delete_item(Key={"connection_id": connection_id})

    def get_connection(self, connection_id: str) -> dict[str, Any] | None:
        resp = self._table.get_item(Key={"connection_id": connection_id})
        return resp.get("Item")

    def get_conversation_connections(self, conversation_id: str) -> list[str]:
        """Devuelve todos los connection_id activos para una conversación (vía GSI)."""
        resp = self._table.query(
            IndexName="conversation-index",
            KeyConditionExpression=cond.Key("conversation_id").eq(conversation_id),
            ProjectionExpression="connection_id",
        )
        return [item["connection_id"] for item in resp.get("Items", [])]

    def refresh_ttl(self, connection_id: str) -> None:
        """Prolonga el TTL en cada mensaje para no cerrar sesiones activas."""
        self._table.update_item(
            Key={"connection_id": connection_id},
            UpdateExpression="SET #t = :ttl",
            ExpressionAttributeNames={"#t": "ttl"},
            ExpressionAttributeValues={":ttl": int(time.time()) + _CONNECTION_TTL_SECONDS},
        )

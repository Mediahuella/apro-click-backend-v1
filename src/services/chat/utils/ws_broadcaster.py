"""Broadcast de nuevos mensajes a conexiones WebSocket activas."""
from __future__ import annotations

import json
from typing import Any

import boto3
import boto3.dynamodb.conditions as cond
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

logger = Logger()


def broadcast_new_message(
    conversation_id: str,
    message: dict[str, Any],
    region: str,
    stage: str,
    ws_api_id: str,
    connections_table: str,
) -> None:
    """Envía el mensaje a todas las conexiones WS activas de la conversación.
    Silencioso en caso de fallo: el mensaje ya fue persistido en PostgreSQL.
    """
    if not ws_api_id or not connections_table:
        return

    ddb = boto3.resource("dynamodb", region_name=region)
    table = ddb.Table(connections_table)

    try:
        resp = table.query(
            IndexName="conversation-index",
            KeyConditionExpression=cond.Key("conversation_id").eq(conversation_id),
            ProjectionExpression="connection_id",
        )
    except Exception:
        logger.warning("No se pudo consultar conexiones WS para broadcast")
        return

    connection_ids = [item["connection_id"] for item in resp.get("Items", [])]
    if not connection_ids:
        return

    endpoint = f"https://{ws_api_id}.execute-api.{region}.amazonaws.com/{stage}"
    mgmt = boto3.client(
        "apigatewaymanagementapi",
        endpoint_url=endpoint,
        region_name=region,
    )

    payload = json.dumps({"type": "new_message", "message": message}).encode("utf-8")
    stale_ids: list[str] = []

    for conn_id in connection_ids:
        try:
            mgmt.post_to_connection(ConnectionId=conn_id, Data=payload)
        except ClientError as e:
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 410:
                stale_ids.append(conn_id)
            else:
                logger.warning(
                    "Error al hacer broadcast WS",
                    extra={"connection_id": conn_id, "error": str(e)},
                )

    for conn_id in stale_ids:
        try:
            table.delete_item(Key={"connection_id": conn_id})
        except Exception:
            pass

"""Envía mensajes a conexiones WebSocket activas vía ApiGatewayManagementApi."""
from __future__ import annotations

import json
from typing import Any

import boto3
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

logger = Logger()


class Broadcaster:
    def __init__(self, domain: str, stage: str) -> None:
        endpoint = f"https://{domain}/{stage}"
        self._client = boto3.client(
            "apigatewaymanagementapi",
            endpoint_url=endpoint,
        )

    def send(self, connection_id: str, payload: dict[str, Any]) -> bool:
        """
        Envía payload JSON a la conexión.
        Retorna True si el envío fue exitoso, False si la conexión está muerta (410 Gone).
        """
        try:
            self._client.post_to_connection(
                ConnectionId=connection_id,
                Data=json.dumps(payload).encode("utf-8"),
            )
            return True
        except ClientError as e:
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 410:
                logger.info(
                    "Conexión WS ya no existe (Gone), será limpiada",
                    extra={"connection_id": connection_id},
                )
                return False
            logger.warning(
                "Error al enviar a conexión WS",
                extra={"connection_id": connection_id, "error": str(e)},
            )
            return False

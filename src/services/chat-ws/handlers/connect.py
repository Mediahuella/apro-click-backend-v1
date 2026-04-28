"""$connect: valida auth y registra connectionId en DynamoDB."""
from __future__ import annotations

import sys
from pathlib import Path

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext

service_root = Path(__file__).resolve().parent.parent
if str(service_root) not in sys.path:
    sys.path.insert(0, str(service_root))
if "/var/task" not in sys.path:
    sys.path.insert(0, "/var/task")

for _p in [Path("/var/task/shared"), service_root / "shared"]:
    if _p.exists() and (_p / "database").exists():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from services.ws_service import WsService  # noqa: E402
from utils.ws_auth import authenticate_connection  # noqa: E402

logger = Logger()
_svc = WsService()


def lambda_handler(event: dict, context: LambdaContext) -> dict:
    connection_id: str = event["requestContext"]["connectionId"]
    query_params: dict = event.get("queryStringParameters") or {}

    try:
        identity = authenticate_connection(query_params)
    except PermissionError as exc:
        logger.warning("WS $connect rechazado", extra={"reason": str(exc)})
        return {"statusCode": 401, "body": str(exc)}

    conv_id = (query_params.get("conv_id") or "").strip()
    if not conv_id:
        return {"statusCode": 400, "body": "conv_id requerido en query string"}

    try:
        _svc.register_connection(
            connection_id=connection_id,
            conversation_id=conv_id,
            sender_type=identity["sender_type"],
            actor_id=identity["actor_id"],
        )
    except Exception:
        logger.exception("Error registrando conexión WS")
        return {"statusCode": 500, "body": "Error interno al registrar conexión"}

    return {"statusCode": 200, "body": "Connected"}

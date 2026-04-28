"""$disconnect: elimina connectionId de DynamoDB."""
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

logger = Logger()
_svc = WsService()


def lambda_handler(event: dict, context: LambdaContext) -> dict:
    connection_id: str = event["requestContext"]["connectionId"]
    try:
        _svc.remove_connection(connection_id)
        logger.info("WS desconectado", extra={"connection_id": connection_id})
    except Exception:
        logger.exception("Error eliminando conexión WS")
    return {"statusCode": 200, "body": "Disconnected"}

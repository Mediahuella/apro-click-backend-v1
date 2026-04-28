"""Enrutador de eventos WebSocket: typing, ping, $default."""
from __future__ import annotations

import json
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
from utils.broadcaster import Broadcaster  # noqa: E402

logger = Logger()
_svc = WsService()


def lambda_handler(event: dict, context: LambdaContext) -> dict:
    ctx = event["requestContext"]
    connection_id: str = ctx["connectionId"]
    domain: str = ctx["domainName"]
    stage: str = ctx["stage"]

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return {"statusCode": 400, "body": "JSON inválido"}

    action = body.get("action", "$default")

    # --- ping / keepalive ---
    if action == "ping":
        _svc.refresh_ttl(connection_id)
        Broadcaster(domain, stage).send(connection_id, {"type": "pong"})
        return {"statusCode": 200, "body": "pong"}

    # --- typing indicator ---
    if action == "typing":
        conv_id = (body.get("conversation_id") or "").strip()
        if not conv_id:
            return {"statusCode": 400, "body": "conversation_id requerido"}

        conn = _svc.get_connection(connection_id)
        if not conn:
            return {"statusCode": 410, "body": "Conexión no registrada"}

        _svc.refresh_ttl(connection_id)

        broadcaster = Broadcaster(domain, stage)
        peers = _svc.get_conversation_connections(conv_id)
        payload = {
            "type": "typing",
            "conversation_id": conv_id,
            "sender_type": conn.get("sender_type"),
        }

        stale_ids: list[str] = []
        for peer_id in peers:
            if peer_id == connection_id:
                continue
            ok = broadcaster.send(peer_id, payload)
            if not ok:
                stale_ids.append(peer_id)

        for peer_id in stale_ids:
            _svc.remove_connection(peer_id)

        return {"statusCode": 200, "body": "ok"}

    logger.debug("Acción WS no reconocida", extra={"action": action, "connection_id": connection_id})
    return {"statusCode": 200, "body": "ok"}

"""Health check HTTP API — servicio: apro-click-admin-companies"""
from __future__ import annotations

import sys
from pathlib import Path

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver
from aws_lambda_powertools.utilities.typing import LambdaContext

# ============================================================================
# CONFIGURAR PATHS PRIMERO (raíz del servicio = carpeta del servicio)
# ============================================================================
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

logger = Logger()
tracer = Tracer()
app = APIGatewayHttpResolver()

SERVICE_NAME = "apro-click-admin-companies"


@app.get("/api/v1/health-companies")
@tracer.capture_method
def health():
    logger.info("Health check requested")
    return {
        "status": "healthy",
        "service": SERVICE_NAME,
    }


def lambda_handler(event: dict, context: LambdaContext):
    return app.resolve(event, context)

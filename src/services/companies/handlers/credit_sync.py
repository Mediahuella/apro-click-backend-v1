"""Job programado (EventBridge) — sincroniza el crédito con el DMS de APRO.

Recorre las empresas del CRM con RUT de facturación, le pregunta a APRO si cada
una tiene crédito vigente (``GET /credits/client/{rut}``) y ajusta
``Company.payment_type`` + el metafield ``aproclick.credito`` de Shopify, que es
lo que decide si el crédito se ve en el checkout.

Corre una vez al día: los créditos se otorgan en el DMS días después del alta
del cliente, así que consultar solo al aprobar la empresa no alcanza.

Se invoca por un ``schedule: rate(1 day)`` definido en ``serverless.yml``.
"""
from __future__ import annotations

import sys
from pathlib import Path

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.utilities.typing import LambdaContext

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

from services import credit_sync_service  # noqa: E402

logger = Logger()
tracer = Tracer()


@logger.inject_lambda_context
@tracer.capture_lambda_handler
def lambda_handler(event, context: LambdaContext):
    """Sincroniza todas las empresas. Nunca lanza: el job vuelve mañana."""
    try:
        resultado = credit_sync_service.sincronizar_todas()
    except Exception:
        logger.exception("Falló la sincronización diaria de crédito con APRO")
        return {"ok": False}

    return {
        "ok": True,
        "configurado": resultado["configurado"],
        "revisadas": resultado["revisadas"],
        "cambios": len(resultado["cambios"]),
        "indeterminadas": resultado["indeterminadas"],
    }

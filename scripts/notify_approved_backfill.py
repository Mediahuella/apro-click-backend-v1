#!/usr/bin/env python3
"""Backfill: encola emails de aprobación/rechazo para solicitudes que nunca fueron notificadas.

Procesa `company_registration_requests` con:
  - `status = 'APPROVED' AND approval_notified_at IS NULL`  (default)
  - `status = 'REJECTED' AND rejection_notified_at IS NULL` (con --incluir-rechazos)

Para cada fila encola el mensaje en la cola SQS del servicio `notifications` y marca el
timestamp correspondiente. Idempotente: una segunda corrida no re-envía nada.

Uso:
  # 1) Ver qué se enviaría sin tocar nada (siempre empezar acá):
  python3 scripts/notify_approved_backfill.py --dry-run

  # 2) Ejecutar de verdad (encola SQS + marca timestamp):
  python3 scripts/notify_approved_backfill.py

  # 3) Incluir también rechazos:
  python3 scripts/notify_approved_backfill.py --incluir-rechazos

  # 4) Limitar para una corrida controlada:
  python3 scripts/notify_approved_backfill.py --limit 5

Resolución de la cola SQS (en orden):
  1. `--queue-url <url>` explícito
  2. CloudFormation export `apro-click-<stage>-notifications-queue-url`
     (`--stage dev` por default, `--aws-profile mh-prod` por default)
  3. Variable de entorno `NOTIFICATIONS_QUEUE_URL`

Variables de entorno (.env raíz):
  DATABASE_URL              — PostgreSQL (obligatoria)
  AWS_PROFILE / region      — usados por boto3 si no se pasa --aws-profile
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

import boto3
from botocore.exceptions import ClientError

_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parent

sys.path.insert(0, str(_SCRIPTS))
from dotenv_loader import load_root_dotenv_defaults  # noqa: E402

load_root_dotenv_defaults(_ROOT)

_SHARED = _ROOT / "src" / "shared"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

from sqlalchemy import select  # noqa: E402

from database.engine import get_session  # noqa: E402
from database.models.registration_request import CompanyRegistrationRequest  # noqa: E402


DEFAULT_REGION = "us-east-2"


def _resolve_queue_url(args: argparse.Namespace) -> str:
    if args.queue_url:
        return args.queue_url
    env_url = os.environ.get("NOTIFICATIONS_QUEUE_URL", "").strip()

    export_name = f"apro-click-{args.stage}-notifications-queue-url"
    try:
        session = boto3.Session(profile_name=args.aws_profile, region_name=args.region)
        cfn = session.client("cloudformation")
        resp = cfn.list_exports()
        exports = resp.get("Exports", [])
        while resp.get("NextToken"):
            resp = cfn.list_exports(NextToken=resp["NextToken"])
            exports.extend(resp.get("Exports", []))
        for exp in exports:
            if exp.get("Name") == export_name:
                return exp.get("Value", "")
    except ClientError as e:
        print(f"  · aviso: no se pudo leer CFN exports ({e.response['Error']['Code']})")

    if env_url:
        return env_url
    raise RuntimeError(
        f"no se pudo resolver la cola SQS — pasa --queue-url o configura NOTIFICATIONS_QUEUE_URL "
        f"(export buscado: {export_name})"
    )


def _build_body(tipo: str, params: dict[str, Any]) -> str:
    import json

    return json.dumps({"tipo": tipo, "params": params}, separators=(",", ":"))


def _build_approval_message(req: CompanyRegistrationRequest) -> dict[str, Any] | None:
    payload = req.payload if isinstance(req.payload, dict) else {}
    contact_email = (payload.get("contact_email") or req.submitted_email or "").strip()
    if not contact_email:
        return None
    return {
        "contact_email": contact_email,
        "contact_name": payload.get("contact_name") or "",
        "company_name": payload.get("company_name") or "",
    }


def _build_rejection_message(req: CompanyRegistrationRequest) -> dict[str, Any] | None:
    payload = req.payload if isinstance(req.payload, dict) else {}
    contact_email = (payload.get("contact_email") or req.submitted_email or "").strip()
    if not contact_email:
        return None
    reason: str | None = None
    notes = (req.notes or "").strip()
    if notes:
        for line in notes.splitlines():
            line = line.strip()
            if line.lower().startswith("rechazo:"):
                reason = line.split(":", 1)[1].strip() or None
                break
    return {
        "contact_email": contact_email,
        "contact_name": payload.get("contact_name") or "",
        "company_name": payload.get("company_name") or "",
        "reason": reason,
    }


def _fetch_pending(
    *, include_rejected: bool, limit: int | None
) -> list[CompanyRegistrationRequest]:
    rows: list[CompanyRegistrationRequest] = []
    with get_session() as session:
        stmt = (
            select(CompanyRegistrationRequest)
            .where(
                CompanyRegistrationRequest.status == "APPROVED",
                CompanyRegistrationRequest.approval_notified_at.is_(None),
            )
            .order_by(CompanyRegistrationRequest.id)
        )
        rows.extend(session.scalars(stmt).all())
        if include_rejected:
            stmt_r = (
                select(CompanyRegistrationRequest)
                .where(
                    CompanyRegistrationRequest.status == "REJECTED",
                    CompanyRegistrationRequest.rejection_notified_at.is_(None),
                )
                .order_by(CompanyRegistrationRequest.id)
            )
            rows.extend(session.scalars(stmt_r).all())
    if limit is not None:
        rows = rows[: max(0, limit)]
    return rows


def _mark_notified(request_id: UUID, columna: str) -> None:
    with get_session() as session:
        req = session.get(CompanyRegistrationRequest, request_id)
        if req is None:
            return
        setattr(req, columna, dt.datetime.now(dt.UTC))
        session.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="no encola ni marca; solo reporta")
    parser.add_argument("--incluir-rechazos", action="store_true", help="incluir status REJECTED")
    parser.add_argument("--limit", type=int, default=None, help="procesar como máximo N filas")
    parser.add_argument("--queue-url", default=None, help="URL SQS explícita (sobrescribe CFN/env)")
    parser.add_argument("--stage", default="dev", help="stage para resolver CFN export (default: dev)")
    parser.add_argument("--region", default=DEFAULT_REGION, help=f"AWS region (default: {DEFAULT_REGION})")
    parser.add_argument("--aws-profile", default="mh-prod", help="perfil AWS para boto3 (default: mh-prod)")
    args = parser.parse_args()

    rows = _fetch_pending(include_rejected=args.incluir_rechazos, limit=args.limit)
    print(f"Solicitudes pendientes de notificar: {len(rows)}")
    if not rows:
        return 0

    queue_url = ""
    if not args.dry_run:
        queue_url = _resolve_queue_url(args)
        print(f"Cola SQS: {queue_url}")

    sqs = (
        boto3.Session(profile_name=args.aws_profile, region_name=args.region).client("sqs")
        if not args.dry_run
        else None
    )

    enviados = 0
    omitidos: list[tuple[str, str]] = []

    for req in rows:
        is_approval = req.status == "APPROVED"
        tipo = "solicitud_aprobada" if is_approval else "solicitud_rechazada"
        params = (
            _build_approval_message(req)
            if is_approval
            else _build_rejection_message(req)
        )
        if not params:
            omitidos.append((str(req.id), "sin contact_email"))
            continue

        label = f"  [{tipo}] {req.id}  → {params['contact_email']}  ({params.get('company_name') or '∅'})"
        if args.dry_run:
            print(label + "  · DRY-RUN")
            continue

        try:
            sqs.send_message(QueueUrl=queue_url, MessageBody=_build_body(tipo, params))
            _mark_notified(
                req.id,
                "approval_notified_at" if is_approval else "rejection_notified_at",
            )
            enviados += 1
            print(label + "  · ENCOLADO")
        except ClientError as e:
            omitidos.append((str(req.id), f"SQS error: {e.response['Error']['Code']}"))

    print("---")
    print(f"Encolados: {enviados}   Omitidos: {len(omitidos)}   Dry-run: {args.dry_run}")
    for rid, motivo in omitidos:
        print(f"  · {rid}  → {motivo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

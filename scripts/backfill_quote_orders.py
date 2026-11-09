#!/usr/bin/env python3
"""Backfill: enlaza las cotizaciones cuyo Draft Order ya se convirtió en pedido.

El webhook ``orders/create`` no puede cerrar la cotización cuando el cliente
paga el link del Draft Order: el payload REST del pedido no trae el id del
draft de origen. Desde ahora ``quotes`` lo detecta al listar o ver cotizaciones
(``quote_service.sync_completed_drafts``), pero las que nadie vuelve a abrir
siguen en ``COTIZADA``/``INVOICE_SENT`` sin ``order_gid``. Este script recorre
todas las pagables, consulta sus drafts en Shopify en lote y deja
``order_gid`` + ``ACEPTADA`` en las que ya son pedido.

Uso:
  python3 scripts/backfill_quote_orders.py            # dry-run: reporta, no escribe
  python3 scripts/backfill_quote_orders.py --apply    # aplica

Variables de entorno (desde .env raíz o shell):
  DATABASE_URL — PostgreSQL (obligatoria). El token de la tienda sale de la BD.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parent

sys.path.insert(0, str(_SCRIPTS))
from dotenv_loader import load_root_dotenv_defaults  # noqa: E402

load_root_dotenv_defaults(_ROOT)

_SERVICE = _ROOT / "src" / "services" / "quotes"
_SHARED = _ROOT / "src" / "shared"
for p in (_SHARED, _SERVICE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from sqlalchemy import select  # noqa: E402

from database.engine import get_session  # noqa: E402
from database.models.quote_request import QuoteRequest  # noqa: E402
from services import quote_service  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="Escribe en la BD (por defecto, dry-run).")
    args = parser.parse_args()

    with get_session() as session:
        quotes = list(
            session.scalars(
                select(QuoteRequest).where(
                    QuoteRequest.draft_order_gid.isnot(None),
                    QuoteRequest.order_gid.is_(None),
                    QuoteRequest.status.in_(quote_service.PAYABLE_STATUSES),
                )
            )
        )
        antes = {q.id: q.status for q in quotes}
        pedidos = quote_service.sync_completed_drafts(session, quotes)
        enlazadas = [q for q in quotes if q.order_gid]
        pagos = Counter(
            (pedidos.get(q.order_gid) or {}).get("displayFinancialStatus") for q in enlazadas
        )
        print(f"Cotizaciones pagables revisadas: {len(quotes)}")
        print(f"Ya convertidas en pedido:        {len(enlazadas)}  {dict(pagos)}")
        for q in enlazadas:
            pedido = pedidos.get(q.order_gid) or {}
            print(
                f"  {q.folio or q.id}  {antes[q.id]} → {q.status}  "
                f"pedido {pedido.get('name')} ({pedido.get('displayFinancialStatus')})"
            )
        if args.apply:
            session.commit()
            print("Aplicado.")
        else:
            session.rollback()
            print("Dry-run: no se escribió nada. Usa --apply para aplicar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

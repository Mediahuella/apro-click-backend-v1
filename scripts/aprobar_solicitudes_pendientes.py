#!/usr/bin/env python3
"""Aprueba en lote las solicitudes de registro pendientes y las reparte entre vendedores.

Usa el mismo `RegistrationRequestService.approve_request` que el panel, así que cada
aprobación hace lo mismo que el botón «Aprobar»: crea la Company B2B en Shopify (contacto
+ sucursal), la agrega al catálogo del segmento, decide el crédito según APRO, registra al
cliente en APRO IA, asigna el vendedor en `user_companies` y encola el email de aprobación
al cliente.

Reparto:
  - Si la solicitud llegó con link de invitación (`referred_by_user_id`) de uno de los
    vendedores indicados, se respeta ese vendedor.
  - El resto se reparte en rotación, empezando por el vendedor con menos empresas
    asignadas, para que el lote quede parejo (diferencia máxima de 1).

Uso (siempre desde backend-v1/):
  # 1) Plan, sin tocar nada (default):
  python3 scripts/aprobar_solicitudes_pendientes.py

  # 2) Probar con una sola solicitud:
  python3 scripts/aprobar_solicitudes_pendientes.py --apply --limit 1

  # 3) Aprobar todas y dejar el Excel en la raíz del monorepo:
  python3 scripts/aprobar_solicitudes_pendientes.py --apply

Opciones útiles:
  --vendedores a@apro.cl,b@apro.cl   (default: Bryan Ramírez y Matías Guerra)
  --segmento PYME|MEDIANA|GRAN_EMPRESA  (default: PYME; ignora lo que pidió la empresa)
  --segmento SOLICITADO               respeta el segmento que pidió cada empresa
  --ruts 76123456-7,77...             solo esas solicitudes
  --excel ruta.xlsx                   (default: ../empresas_aprobadas_<fecha>.xlsx)
  --solo-excel --ruts ...             regenera el Excel de empresas ya aprobadas

Variables (.env raíz de backend-v1): DATABASE_URL, APRO_IA_*, APRO_CREDIT_FORCE_RUTS.
La cola de notificaciones se resuelve del export de CloudFormation
`apro-click-<stage>-notifications-queue-url` con el perfil `mh-prod`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parent
_SERVICE = _ROOT / "src" / "services" / "company-registration"

sys.path.insert(0, str(_SCRIPTS))
from dotenv_loader import load_root_dotenv_defaults  # noqa: E402

load_root_dotenv_defaults(_ROOT)

import sqlalchemy as sa  # noqa: E402

VENDEDORES_DEFAULT = "bryan.ramirez@apro.cl,matias.guerra@apro.cl"
APROBADOR_DEFAULT = "sebastian@mediahuella.com"
SEGMENTO_A_TIPO = {"PYME": "SMALL", "MEDIANA": "MEDIUM", "GRAN_EMPRESA": "BIG"}
TIPO_A_SEGMENTO = {v: k for k, v in SEGMENTO_A_TIPO.items()}


def _normalizar_rut(rut: str) -> str:
    return rut.replace(".", "").replace(" ", "").upper()


def _engine() -> sa.Engine:
    return sa.create_engine(os.environ["DATABASE_URL"])


def _cargar_vendedores(conn: sa.Connection, emails: list[str]) -> list[dict[str, Any]]:
    filas = conn.execute(
        sa.text(
            """select u.id::text, trim(u.given_name) || ' ' || trim(u.family_name), lower(u.email),
                      u.role, u.status,
                      (select count(*) from user_companies uc where uc.user_id = u.id),
                      (select count(*) from user_companies uc
                         join company_registration_requests r on r.resolved_company_id = uc.company_id
                        where uc.user_id = u.id and r.status = 'APPROVED'
                          and (r.updated_at at time zone 'America/Santiago')::date
                              = (now() at time zone 'America/Santiago')::date)
               from users u where lower(u.email) = any(:e)"""
        ),
        {"e": [e.lower() for e in emails]},
    ).all()
    por_email = {f[2]: f for f in filas}
    vendedores = []
    for email in emails:
        f = por_email.get(email.lower())
        if not f:
            sys.exit(f"Vendedor no encontrado: {email}")
        if f[3] != "SALES" or f[4] != "ACTIVE":
            sys.exit(f"{email} no es un vendedor SALES activo (rol={f[3]}, estado={f[4]})")
        vendedores.append(
            {"id": f[0], "nombre": f[1], "email": f[2], "empresas": f[5], "hoy": f[6]}
        )
    return vendedores


def _cargar_pendientes(conn: sa.Connection, ruts: list[str] | None) -> list[dict[str, Any]]:
    sql = """select id::text, payload->>'company_name', payload->>'rut', payload->>'company_type',
                    payload->>'payment_type', payload->>'contact_email', referred_by_user_id::text
             from company_registration_requests where status = 'PENDING'"""
    params: dict[str, Any] = {}
    if ruts:
        sql += " and upper(replace(payload->>'rut', '.', '')) = any(:r)"
        params["r"] = ruts
    sql += " order by created_at"
    return [
        {
            "id": f[0],
            "empresa": " ".join((f[1] or "").split()),
            "rut": f[2],
            "tipo_solicitado": (f[3] or "SMALL").upper(),
            "pago_solicitado": f[4],
            "email": f[5],
            "referido": f[6],
        }
        for f in conn.execute(sa.text(sql), params).all()
    ]


def _armar_plan(
    pendientes: list[dict[str, Any]], vendedores: list[dict[str, Any]], segmento: str
) -> list[dict[str, Any]]:
    ids = {v["id"]: v for v in vendedores}
    # Lo aprobado hoy cuenta: una prueba con --limit 1 o una corrida reanudada no
    # desbalancea el lote del día.
    carga = {v["id"]: v["hoy"] for v in vendedores}
    plan = []
    # Primero las referidas (su vendedor está fijo), luego el resto equilibrando el lote.
    for p in pendientes:
        if p["referido"] in ids:
            carga[p["referido"]] += 1
    for p in pendientes:
        if p["referido"] in ids:
            vendedor, motivo = ids[p["referido"]], "link de invitación"
        else:
            vendedor = min(vendedores, key=lambda v: (carga[v["id"]], v["empresas"]))
            carga[vendedor["id"]] += 1
            motivo = "rotación" + (" (referida por otro usuario)" if p["referido"] else "")
        tipo = p["tipo_solicitado"] if segmento == "SOLICITADO" else SEGMENTO_A_TIPO[segmento]
        plan.append({**p, "vendedor": vendedor, "tipo": tipo, "motivo": motivo})
    return plan


def _queue_url(stage: str, perfil: str) -> str:
    nombre = f"apro-click-{stage}-notifications-queue-url"
    salida = subprocess.run(
        [
            "aws", "cloudformation", "list-exports", "--region", "us-east-2",
            "--profile", perfil, "--output", "text",
            "--query", f"Exports[?Name=='{nombre}'].Value",
        ],
        capture_output=True, text=True,
    )
    url = salida.stdout.strip()
    if salida.returncode != 0 or not url:
        sys.exit(
            f"No se pudo resolver la cola {nombre}: {salida.stderr.strip()}\n"
            f"Si la sesión expiró: aws sso login --profile {perfil}"
        )
    return url


def _aprobar(plan: list[dict[str, Any]], aprobador_id: str, queue_url: str, perfil: str) -> list[str]:
    os.environ["NOTIFICATIONS_QUEUE_URL"] = queue_url
    os.environ.setdefault("AWS_PROFILE", perfil)
    os.environ.setdefault("AWS_REGION", "us-east-2")
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")
    sys.path.insert(0, str(_SERVICE))
    from services.registration_request_service import RegistrationRequestService

    svc = RegistrationRequestService()
    aprobados: list[str] = []
    for p in plan:
        etiqueta = f"{p['rut']:11} {p['vendedor']['nombre'][:16]:16} {p['empresa'][:55]}"
        try:
            out = svc.approve_request(
                p["id"],
                approver_user_id=aprobador_id,
                sales_user_id=p["vendedor"]["id"],
                company_type=p["tipo"],
            )
            co = out["created_company"]
            aprobados.append(_normalizar_rut(p["rut"]))
            print(
                f"OK  {etiqueta} {co['company_type']} {co['payment_type']} "
                f"shopify={co.get('shopify_company_id')}",
                flush=True,
            )
        except Exception as ex:  # noqa: BLE001 — se informa y se sigue con el resto
            print(f"ERR {etiqueta} -> {ex}", flush=True)
    return aprobados


def _escribir_excel(conn: sa.Connection, ruts: list[str], destino: Path) -> int:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    filas = conn.execute(
        sa.text(
            r"""select trim(u.given_name) || ' ' || trim(u.family_name),
                       regexp_replace(c.name, '\s+', ' ', 'g'), c.billing_rut,
                       cl.name, cl.email, cl.phone, c.company_type, r.payload->>'company_type',
                       case when c.payment_type = 'CREDIT' then 'Sí' else 'No' end,
                       (r.updated_at at time zone 'America/Santiago')::timestamp
                from companies c
                join user_companies uc on uc.company_id = c.id
                join users u on u.id = uc.user_id and u.role = 'SALES'
                left join clients cl on cl.company_id = c.id and cl.is_main_contact
                left join company_registration_requests r on r.resolved_company_id = c.id
                where upper(c.billing_rut) = any(:r)
                order by 1, 2"""
        ),
        {"r": ruts},
    ).all()
    encabezado = [
        "Vendedor", "Empresa", "RUT", "Contacto", "Email", "Teléfono",
        "Lista de precios", "Segmento solicitado", "Crédito APRO", "Aprobación",
    ]
    anchos = [18, 58, 13, 34, 36, 16, 15, 19, 13, 17]
    datos = [
        [f[0], f[1], f[2], f[3], f[4], f[5], TIPO_A_SEGMENTO.get(f[6], f[6]),
         TIPO_A_SEGMENTO.get(f[7] or "", f[7]), f[8], f[9]]
        for f in filas
    ]

    def hoja(ws, filas_hoja: list[list[Any]]) -> None:
        ws.append(encabezado)
        for celda in ws[1]:
            celda.font = Font(bold=True, color="FFFFFF")
            celda.fill = PatternFill("solid", fgColor="1F2A44")
            celda.alignment = Alignment(vertical="center")
        for f in filas_hoja:
            ws.append(f)
        for (celda,) in ws.iter_rows(min_row=2, min_col=10, max_col=10):
            celda.number_format = "DD-MM-YYYY HH:MM"
        for i, ancho in enumerate(anchos):
            ws.column_dimensions[chr(65 + i)].width = ancho
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

    wb = Workbook()
    resumen = wb.active
    resumen.title = "Resumen"
    resumen.append(["Vendedor", "Empresas", "Con crédito APRO"])
    for celda in resumen[1]:
        celda.font = Font(bold=True, color="FFFFFF")
        celda.fill = PatternFill("solid", fgColor="1F2A44")
    vendedores = sorted({d[0] for d in datos})
    for v in vendedores:
        propias = [d for d in datos if d[0] == v]
        resumen.append([v, len(propias), sum(1 for d in propias if d[8] == "Sí")])
    resumen.append(["Total", len(datos), sum(1 for d in datos if d[8] == "Sí")])
    resumen[resumen.max_row][0].font = Font(bold=True)
    for col, ancho in zip("ABC", [22, 12, 18]):
        resumen.column_dimensions[col].width = ancho

    hoja(wb.create_sheet("Todas"), datos)
    for v in vendedores:
        hoja(wb.create_sheet(v.split()[0]), [d for d in datos if d[0] == v])
    destino.parent.mkdir(parents=True, exist_ok=True)
    wb.save(destino)
    return len(datos)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="aprueba de verdad (default: solo plan)")
    ap.add_argument("--limit", type=int, help="aprueba solo las primeras N del plan")
    ap.add_argument("--vendedores", default=VENDEDORES_DEFAULT, help="emails separados por coma")
    ap.add_argument("--segmento", default="PYME", choices=[*SEGMENTO_A_TIPO, "SOLICITADO"])
    ap.add_argument("--ruts", help="RUT separados por coma (default: todas las pendientes)")
    ap.add_argument("--aprobador", default=APROBADOR_DEFAULT, help="email del usuario que aprueba")
    ap.add_argument("--excel", help="ruta del Excel (default: ../empresas_aprobadas_<fecha>.xlsx)")
    ap.add_argument("--solo-excel", action="store_true", help="solo regenera el Excel de --ruts")
    ap.add_argument("--stage", default="dev")
    ap.add_argument("--aws-profile", default="mh-prod")
    args = ap.parse_args()

    ruts = [_normalizar_rut(r) for r in args.ruts.split(",")] if args.ruts else None
    hoy = dt.date.today().isoformat()
    excel = Path(args.excel) if args.excel else _ROOT.parent / f"empresas_aprobadas_{hoy}.xlsx"
    engine = _engine()

    if args.solo_excel:
        if not ruts:
            sys.exit("--solo-excel requiere --ruts")
        with engine.connect() as conn:
            n = _escribir_excel(conn, ruts, excel)
        print(f"Excel: {excel} ({n} empresas)")
        return

    with engine.connect() as conn:
        vendedores = _cargar_vendedores(conn, [e.strip() for e in args.vendedores.split(",") if e.strip()])
        aprobador = conn.execute(
            sa.text("select id::text from users where lower(email) = :e and role in ('SUPERADMIN','ADMIN')"),
            {"e": args.aprobador.lower()},
        ).scalar()
        if not aprobador:
            sys.exit(f"Aprobador no encontrado o sin rol ADMIN/SUPERADMIN: {args.aprobador}")
        pendientes = _cargar_pendientes(conn, ruts)

    if ruts:
        faltan = set(ruts) - {_normalizar_rut(p["rut"]) for p in pendientes}
        if faltan:
            print(f"AVISO: sin solicitud PENDING para: {', '.join(sorted(faltan))}")
    if not pendientes:
        print("No hay solicitudes pendientes.")
        return

    plan = _armar_plan(pendientes, vendedores, args.segmento)
    if args.limit:
        plan = plan[: args.limit]

    print(f"Plan ({len(plan)} solicitudes, segmento {args.segmento}):")
    for p in plan:
        cambio = "" if p["tipo"] == p["tipo_solicitado"] else f" (pidió {TIPO_A_SEGMENTO.get(p['tipo_solicitado'])})"
        print(
            f"  {p['rut']:11} {p['vendedor']['nombre'][:16]:16} {TIPO_A_SEGMENTO[p['tipo']]}{cambio:<22} "
            f"{p['motivo']:<12} {p['empresa'][:60]}"
        )
    for v in vendedores:
        n = sum(1 for p in plan if p["vendedor"]["id"] == v["id"])
        print(f"  → {v['nombre']}: {n} nuevas + {v['hoy']} ya aprobadas hoy (total asignadas: {v['empresas']})")

    if not args.apply:
        print("\nDry-run: no se aprobó nada. Repite con --apply para ejecutar.")
        return

    aprobados = _aprobar(plan, aprobador, _queue_url(args.stage, args.aws_profile), args.aws_profile)
    print(f"\nAprobadas {len(aprobados)} de {len(plan)}.")
    if aprobados:
        with engine.connect() as conn:
            n = _escribir_excel(conn, aprobados, excel)
        print(f"Excel: {excel} ({n} empresas)")


if __name__ == "__main__":
    main()

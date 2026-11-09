#!/usr/bin/env python3
"""Exporta a Excel las empresas registradas con sus contactos (una fila por contacto).

Solo lectura: consulta la BD y la Admin API de Shopify, no modifica nada.

Fuentes por columna:
  - Contacto (email, nombre, teléfono, ID Shopify): `clients`. Las solicitudes que no
    llegaron a crear empresa (pendientes o rechazadas) salen del payload del formulario.
  - Fecha de registro: `company_registration_requests.created_at` (o la de la empresa si
    se creó desde el panel, sin solicitud).
  - Fecha de validación: `companies.created_at`, que es el momento en que se aprobó la
    solicitud (`approval_notified_at` no sirve: hubo reenvíos masivos del correo).
  - Marketing y rol: `Customer.emailMarketingConsent` y `companyContactProfiles` de Shopify.
  - Pedidos y monto: `shopify_orders` de la empresa, excluyendo anulados y reembolsados;
    el monto neto es `subtotal_price` (productos sin IVA, sin despacho).

Uso (desde backend-v1/):
  python3 scripts/exportar_empresas.py
  python3 scripts/exportar_empresas.py --sin-shopify          # sin columnas de Shopify
  python3 scripts/exportar_empresas.py --excel ruta.xlsx --csv
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parent
sys.path.insert(0, str(_SCRIPTS))
from dotenv_loader import load_root_dotenv_defaults  # noqa: E402

load_root_dotenv_defaults(_ROOT)

import sqlalchemy as sa  # noqa: E402
from openpyxl import Workbook  # noqa: E402
from openpyxl.styles import Font, PatternFill  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402

TZ = ZoneInfo("America/Santiago")

REGIONES = {
    "AP": "Arica y Parinacota", "TA": "Tarapacá", "AN": "Antofagasta", "AT": "Atacama",
    "CO": "Coquimbo", "VS": "Valparaíso", "RM": "Metropolitana de Santiago",
    "LI": "Libertador General Bernardo O'Higgins", "ML": "Maule", "NB": "Ñuble",
    "BI": "Biobío", "AR": "La Araucanía", "LR": "Los Ríos", "LL": "Los Lagos",
    "AI": "Aysén del General Carlos Ibáñez del Campo",
    "MA": "Magallanes y de la Antártica Chilena",
}
ESTADOS = {
    "PENDING": "pendiente", "NEEDS_INFO": "pendiente",
    "APPROVED": "validada", "REJECTED": "rechazada",
}
MARKETING = {
    "SUBSCRIBED": "Sí", "NOT_SUBSCRIBED": "No", "UNSUBSCRIBED": "No (se dio de baja)",
    "PENDING": "Pendiente de confirmar", "REDACTED": "No", "INVALID": "No",
}
COLUMNAS = [
    "Correo electrónico", "Nombre y apellido", "Teléfono", "RUT empresa", "Razón social",
    "Fecha de registro", "Estado de la cuenta", "Fecha de validación",
    "Acepta marketing", "Fecha consentimiento marketing", "Región", "Comuna", "Giro",
    "Vendedor asignado", "N° de pedidos", "Monto total comprado (neto)",
    "Fecha última compra", "ID cliente Shopify", "Rol del usuario",
]


def _rut(valor: Any) -> str:
    s = str(valor or "").replace(".", "").replace(" ", "").upper()
    if s and "-" not in s and len(s) > 1:
        s = f"{s[:-1]}-{s[-1]}"
    return s


def _fecha(valor: dt.datetime | None) -> str:
    if valor is None:
        return ""
    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=dt.timezone.utc)
    return valor.astimezone(TZ).date().isoformat()


def _region(codigo: str | None) -> str:
    c = (codigo or "").upper().removeprefix("CL-")
    return REGIONES.get(c, c)


# ---------------------------------------------------------------- BD


def _cargar_bd(conn: sa.Connection) -> list[dict[str, Any]]:
    empresas = conn.execute(sa.text("""
        select c.id, c.name, c.billing_rut, c.billing_razon_social, c.billing_giro,
               c.created_at, r.created_at as registrada, r.status
          from companies c
          left join lateral (
                select created_at, status from company_registration_requests
                 where resolved_company_id = c.id order by created_at limit 1) r on true
         where not c.is_system
    """)).mappings().all()

    contactos: dict[Any, list[dict]] = defaultdict(list)
    for k in conn.execute(sa.text("""
        select company_id, email, name, phone, shopify_customer_id, is_main_contact
          from clients order by is_main_contact desc, created_at
    """)).mappings():
        contactos[k["company_id"]].append(dict(k))

    direcciones = {
        d["company_id"]: d
        for d in conn.execute(sa.text("""
            select distinct on (company_id) company_id, zone_code, city
              from company_addresses order by company_id, is_primary desc, created_at
        """)).mappings()
    }

    vendedores: dict[Any, list[str]] = defaultdict(list)
    for v in conn.execute(sa.text("""
        select uc.company_id, trim(trim(u.given_name) || ' ' || trim(u.family_name)) as nombre
          from user_companies uc join users u on u.id = uc.user_id order by nombre
    """)).mappings():
        vendedores[v["company_id"]].append(v["nombre"])

    pedidos = {
        p["company_id"]: p
        for p in conn.execute(sa.text("""
            select company_id, count(*) as n, coalesce(sum(subtotal_price), 0) as neto,
                   max(created_at) as ultima
              from shopify_orders
             where company_id is not null
               and coalesce(financial_status, '') not in ('voided', 'refunded')
             group by company_id
        """)).mappings()
    }

    filas: list[dict[str, Any]] = []
    for e in empresas:
        dir_ = direcciones.get(e["id"], {})
        ped = pedidos.get(e["id"], {})
        base = {
            "RUT empresa": _rut(e["billing_rut"]),
            "Razón social": e["billing_razon_social"] or e["name"],
            "Fecha de registro": _fecha(e["registrada"] or e["created_at"]),
            "Estado de la cuenta": ESTADOS.get(e["status"] or "APPROVED", "validada"),
            "Fecha de validación": _fecha(e["created_at"]),
            "Región": _region(dir_.get("zone_code")),
            "Comuna": dir_.get("city") or "",
            "Giro": e["billing_giro"] or "",
            "Vendedor asignado": ", ".join(vendedores.get(e["id"], [])),
            "N° de pedidos": int(ped.get("n") or 0),
            "Monto total comprado (neto)": int(Decimal(ped.get("neto") or 0)),
            "Fecha última compra": _fecha(ped.get("ultima")),
            "_company_shopify_id": None,
        }
        for k in contactos.get(e["id"]) or [{}]:
            filas.append({
                **base,
                "Correo electrónico": (k.get("email") or "").strip().lower(),
                "Nombre y apellido": (k.get("name") or "").strip(),
                "Teléfono": k.get("phone") or "",
                "ID cliente Shopify": k.get("shopify_customer_id") or "",
                "_main": bool(k.get("is_main_contact")),
            })

    # Solicitudes que no crearon empresa (pendientes o rechazadas): datos del formulario.
    for r in conn.execute(sa.text("""
        select status, payload, submitted_email, created_at
          from company_registration_requests
         where resolved_company_id is null order by created_at
    """)).mappings():
        p = r["payload"] or {}
        filas.append({
            "Correo electrónico": (p.get("contact_email") or r["submitted_email"] or "").strip().lower(),
            "Nombre y apellido": (p.get("contact_name") or "").strip(),
            "Teléfono": p.get("contact_phone") or "",
            "RUT empresa": _rut(p.get("rut")),
            "Razón social": p.get("company_name") or "",
            "Fecha de registro": _fecha(r["created_at"]),
            "Estado de la cuenta": ESTADOS.get(r["status"], r["status"].lower()),
            "Fecha de validación": "",
            "Región": _region(p.get("shipping_zone_code")),
            "Comuna": p.get("shipping_city") or p.get("city") or "",
            "Giro": p.get("giro") or "",
            "Vendedor asignado": "",
            "N° de pedidos": 0,
            "Monto total comprado (neto)": 0,
            "Fecha última compra": "",
            "ID cliente Shopify": "",
            "_main": True,
        })
    return filas


# ---------------------------------------------------------------- Shopify

_QUERY = """
query($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on Customer {
      id
      emailMarketingConsent { marketingState consentUpdatedAt }
      companyContactProfiles {
        isMainContact
        roleAssignments(first: 5) { nodes { role { name } } }
      }
    }
  }
}
"""


def _tienda(conn: sa.Connection) -> tuple[str, str]:
    fila = conn.execute(sa.text("""
        select shop_domain, shopify_access_token from shopify_app_installations
         where uninstalled_at is null and shopify_access_token is not null
         order by created_at desc limit 1
    """)).first()
    if not fila:
        raise RuntimeError("No hay instalación Shopify activa con token")
    return fila[0], fila[1].strip()


def _graphql(shop: str, token: str, variables: dict) -> dict:
    version = (os.environ.get("SHOPIFY_API_VERSION") or "2026-04").strip()
    req = urllib.request.Request(
        f"https://{shop}/admin/api/{version}/graphql.json",
        data=json.dumps({"query": _QUERY, "variables": variables}).encode(),
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
        method="POST",
    )
    for intento in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503) and intento < 4:
                time.sleep(2 * (intento + 1))
                continue
            raise
        errores = body.get("errors") or []
        if any((x.get("extensions") or {}).get("code") == "THROTTLED" for x in errores):
            time.sleep(2 * (intento + 1))
            continue
        if errores:
            raise RuntimeError(f"GraphQL: {errores}")
        return body["data"]
    raise RuntimeError("Shopify sigue limitando las consultas")


def _datos_shopify(conn: sa.Connection, ids: list[str]) -> dict[str, dict]:
    shop, token = _tienda(conn)
    salida: dict[str, dict] = {}
    unicos = sorted({i for i in ids if i})
    for n in range(0, len(unicos), 50):
        lote = unicos[n:n + 50]
        data = _graphql(shop, token, {"ids": [f"gid://shopify/Customer/{i}" for i in lote]})
        for nodo in data.get("nodes") or []:
            if not nodo:
                continue
            cid = nodo["id"].rsplit("/", 1)[-1]
            consent = nodo.get("emailMarketingConsent") or {}
            roles = sorted({
                ra["role"]["name"]
                for perfil in nodo.get("companyContactProfiles") or []
                for ra in (perfil.get("roleAssignments") or {}).get("nodes") or []
                if ra.get("role")
            })
            salida[cid] = {
                "estado": consent.get("marketingState"),
                "fecha": consent.get("consentUpdatedAt"),
                "roles": roles,
                "principal": any(p.get("isMainContact") for p in nodo.get("companyContactProfiles") or []),
            }
        print(f"  Shopify: {min(n + 50, len(unicos))}/{len(unicos)}", end="\r", flush=True)
    print()
    return salida


ROLES = {"Location admin": "Administrador", "Ordering only": "Comprador"}


def _rol(fila: dict, sh: dict | None) -> str:
    if fila["Estado de la cuenta"] != "validada":
        return ""
    principal = fila.get("_main") or bool(sh and sh.get("principal"))
    if sh and sh.get("roles"):
        roles = ", ".join(sorted({ROLES.get(r, r) for r in sh["roles"]}))
        return f"Contacto principal ({roles})" if principal else roles
    return "Contacto principal" if principal else "Comprador"


# ---------------------------------------------------------------- salida


def _escribir_excel(filas: list[dict], ruta: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Empresas"
    ws.append(COLUMNAS)
    for celda in ws[1]:
        celda.font = Font(bold=True, color="FFFFFF")
        celda.fill = PatternFill("solid", fgColor="1F3A5F")
    for f in filas:
        ws.append([f.get(c, "") for c in COLUMNAS])
    for i, col in enumerate(COLUMNAS, start=1):
        ancho = max(len(col), *(len(str(f.get(col, ""))) for f in filas[:500])) if filas else len(col)
        ws.column_dimensions[get_column_letter(i)].width = min(ancho + 2, 60)
    monto = COLUMNAS.index("Monto total comprado (neto)") + 1
    for (c,) in ws.iter_rows(min_row=2, min_col=monto, max_col=monto):
        c.number_format = '"$"#,##0'
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.save(ruta)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    hoy = dt.datetime.now(TZ).date().isoformat()
    ap.add_argument("--excel", type=Path, default=_ROOT.parent / f"empresas_export_{hoy}.xlsx")
    ap.add_argument("--csv", action="store_true", help="genera además un CSV junto al Excel")
    ap.add_argument("--sin-shopify", action="store_true", help="no consulta Shopify")
    args = ap.parse_args()

    engine = sa.create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        filas = _cargar_bd(conn)
        print(f"Filas (contactos): {len(filas)}")
        shopify = {} if args.sin_shopify else _datos_shopify(conn, [f["ID cliente Shopify"] for f in filas])

    for f in filas:
        sh = shopify.get(str(f["ID cliente Shopify"]))
        if sh:
            f["Acepta marketing"] = MARKETING.get(sh["estado"] or "", sh["estado"] or "")
            f["Fecha consentimiento marketing"] = (sh["fecha"] or "")[:10]
        f["Rol del usuario"] = _rol(f, sh)

    filas.sort(key=lambda f: (f["Fecha de registro"], f["RUT empresa"], not f.get("_main")))
    _escribir_excel(filas, args.excel)
    print(f"Excel: {args.excel}")
    if args.csv:
        ruta_csv = args.excel.with_suffix(".csv")
        with ruta_csv.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=COLUMNAS, extrasaction="ignore", delimiter=";")
            w.writeheader()
            w.writerows(filas)
        print(f"CSV:   {ruta_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

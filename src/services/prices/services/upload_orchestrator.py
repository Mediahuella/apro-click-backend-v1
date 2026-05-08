"""Orquestador del worker de uploads de listas de precio.

Ejecutado por la Lambda SQS ``handlers/upload_worker.py``. Para un
``upload_id`` recibido en la cola:

1. Marca ``status='PROCESSING'`` y descarga el Excel desde S3.
2. Parsea el Excel.
3. Resuelve ``SAP → variantId`` consultando todas las variantes de Shopify.
4. Para cada segmento (PYME / MEDIANA / GRAN_EMPRESA), de forma SECUENCIAL:
   - Asegura que existan ``PriceList`` (y opcionalmente ``Catalog``) en
     Shopify para ese segmento (los GIDs se persisten en
     ``shopify_price_segments``).
   - Filtra los items del Excel que tienen precio para ese segmento + variant
     resuelto.
   - Sube el JSONL a Shopify y dispara ``bulkOperationRunMutation``.
   - Espera a que termine (Shopify sólo permite UNA bulk mutation a la vez).
5. Actualiza la fila ``price_list_uploads`` con el resultado final.

Idempotencia: si el worker se reintenta y el upload tiene
``*_bulk_operation_gid`` ya seteado, se reanuda el polling de ese segmento
en lugar de crear uno nuevo.
"""
from __future__ import annotations

import os
import sys
import uuid as uuid_mod
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from aws_lambda_powertools import Logger

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

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database.engine import get_session  # noqa: E402
from database.models.company import Company  # noqa: E402
from database.models.price_list import (  # noqa: E402
    PriceListUpload,
    ShopifyPriceSegment,
)

from services.excel_parser import (  # noqa: E402
    ParsedItem,
    parse_price_list_excel,
)
from services.s3_storage import get_excel  # noqa: E402
from services.shop_resolver import resolve_shop_and_token  # noqa: E402
from services.shopify_b2b import (  # noqa: E402
    PriceItem,
    SEGMENT_LABELS,
    SEGMENT_PRICE_COLUMN,
    VALID_SEGMENTS,
    ensure_catalog,
    ensure_price_list,
    get_price_list_summary,
    poll_bulk_operation,
    run_bulk_price_update,
    wait_for_bulk,
)
from services.shopify_companies import (  # noqa: E402
    fetch_locations_for_companies,
)
from services.shopify_variants import (  # noqa: E402
    fetch_all_sku_to_variant_id,
    resolve_variants_for_skus,
)

logger = Logger()


# ---------------------------------------------------------------------------
# Atributos por segmento
# ---------------------------------------------------------------------------

_SEGMENT_ATTRS: dict[str, dict[str, str]] = {
    "PYME": {
        "bulk_gid": "pyme_bulk_operation_gid",
        "bulk_status": "pyme_bulk_status",
        "env_locations": "PRICES_B2B_PYME_LOCATION_IDS",
        "company_type": "SMALL",
    },
    "MEDIANA": {
        "bulk_gid": "mediana_bulk_operation_gid",
        "bulk_status": "mediana_bulk_status",
        "env_locations": "PRICES_B2B_MEDIANA_LOCATION_IDS",
        "company_type": "MEDIUM",
    },
    "GRAN_EMPRESA": {
        "bulk_gid": "gran_empresa_bulk_operation_gid",
        "bulk_status": "gran_empresa_bulk_status",
        "env_locations": "PRICES_B2B_GRAN_EMPRESA_LOCATION_IDS",
        "company_type": "BIG",
    },
}

#: Mapping segmento → ``company_type`` en BD (consultado al resolver locations).
SEGMENT_COMPANY_TYPE: dict[str, str] = {
    seg: attrs["company_type"] for seg, attrs in _SEGMENT_ATTRS.items()
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _b2b_currency() -> str:
    return (os.environ.get("PRICES_B2B_CURRENCY") or "CLP").strip().upper()


def _env_company_locations_for(segment: str) -> list[str]:
    """Fallback: lee CSV desde la variable de entorno (override manual)."""
    raw = (os.environ.get(_SEGMENT_ATTRS[segment]["env_locations"]) or "").strip()
    if not raw:
        return []
    out: list[str] = []
    for s in raw.split(","):
        s2 = s.strip()
        if s2:
            out.append(s2)
    return out


def _db_company_ids_for_segment(session: Session, segment: str) -> list[str]:
    """Lista los ``shopify_company_id`` (numéricos) de las companies del segmento.

    Sólo incluye companies con ``shopify_company_id`` no nulo (las que el
    flujo de aprobación creó realmente en Shopify).
    """
    company_type = SEGMENT_COMPANY_TYPE[segment]
    rows = session.scalars(
        select(Company.shopify_company_id).where(
            Company.company_type == company_type,
            Company.shopify_company_id.is_not(None),
        )
    ).all()
    out: list[str] = []
    for r in rows:
        s = (str(r) if r is not None else "").strip()
        if s:
            out.append(s)
    return out


def resolve_company_location_ids(
    segment: str,
    *,
    shop_domain: str,
    access_token: str,
) -> tuple[list[str], dict[str, Any]]:
    """Resuelve los ``CompanyLocation`` GIDs a asociar al Catalog del segmento.

    Estrategia:
        1. Si ``PRICES_B2B_<segment>_LOCATION_IDS`` está seteado, lo usa
           tal cual (override manual del operador).
        2. Si no, lee la BD ``companies`` filtrando por
           ``company_type = SEGMENT_COMPANY_TYPE[segment]`` con
           ``shopify_company_id`` no nulo y resuelve sus locations en Shopify.

    Devuelve ``(location_gids, debug)`` donde ``debug`` describe la fuente
    usada y los IDs intermedios para logging.
    """
    env_locs = _env_company_locations_for(segment)
    if env_locs:
        return env_locs, {"source": "env", "company_locations": env_locs}

    with get_session() as session:
        company_ids = _db_company_ids_for_segment(session, segment)

    if not company_ids:
        return [], {
            "source": "db",
            "company_ids_in_db": 0,
            "company_locations": [],
        }

    location_gids, missing = fetch_locations_for_companies(
        shop_domain, access_token, company_ids
    )
    debug: dict[str, Any] = {
        "source": "db",
        "company_ids_in_db": company_ids,
        "missing_companies_in_shopify": missing,
        "company_locations": location_gids,
    }
    return location_gids, debug


def _format_clp(amount: Decimal) -> str:
    """Devuelve un string apto para Shopify ``MoneyInput.amount``.

    CLP no usa decimales en la práctica; truncamos a entero salvo que el
    Excel tenga decimales (ej. 187836.975 — Shopify lo acepta como string).
    """
    try:
        if amount == amount.to_integral_value():
            return str(int(amount))
    except Exception:
        pass
    return format(amount.normalize(), "f")


def _get_segment_config(
    session: Session, segment: str, shop_domain: str
) -> ShopifyPriceSegment:
    row = session.scalar(
        select(ShopifyPriceSegment).where(ShopifyPriceSegment.segment == segment)
    )
    if row is None:
        row = ShopifyPriceSegment(
            segment=segment,
            shop_domain=shop_domain,
            currency=_b2b_currency(),
        )
        session.add(row)
        session.flush()
    return row


def _refresh_upload(session: Session, upload_id: uuid_mod.UUID) -> PriceListUpload:
    row = session.get(PriceListUpload, upload_id)
    if row is None:
        raise LookupError(f"PriceListUpload no encontrado: {upload_id}")
    return row


def _set_status(
    upload_id: uuid_mod.UUID,
    status: str,
    *,
    error_message: str | None = None,
) -> None:
    with get_session() as session:
        row = _refresh_upload(session, upload_id)
        row.status = status
        if error_message is not None:
            row.error_message = error_message[:2000]
        session.commit()


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def _build_price_items(
    items: list[ParsedItem],
    segment: str,
    sku_to_variant: dict[str, str],
) -> tuple[list[PriceItem], list[str]]:
    """Para un segmento, devuelve ``(price_items, missing_skus)``.

    ``missing_skus`` incluye los SAPs sin variant en Shopify Y los que no
    tienen precio para ese segmento.
    """
    col = SEGMENT_PRICE_COLUMN[segment]
    price_items: list[PriceItem] = []
    missing: list[str] = []
    for it in items:
        amount = getattr(it, col)
        if amount is None:
            continue
        vid = sku_to_variant.get(it.sap_code)
        if not vid:
            missing.append(it.sap_code)
            continue
        if not isinstance(amount, Decimal):
            amount = Decimal(str(amount))
        price_items.append(
            PriceItem(variant_id=vid, amount=_format_clp(amount))
        )
    return price_items, missing


def _process_segment(
    *,
    upload_id: uuid_mod.UUID,
    segment: str,
    shop_domain: str,
    access_token: str,
    items: list[ParsedItem],
    sku_to_variant: dict[str, str],
) -> tuple[str, str | None]:
    """Procesa un segmento. Devuelve ``(final_status, error_message)``.

    ``final_status`` ∈ ``{COMPLETED, FAILED, RUNNING}``. ``RUNNING`` se usa
    cuando el bulk no terminó dentro del polling y se deja para refresh
    posterior.
    """
    attrs = _SEGMENT_ATTRS[segment]
    currency = _b2b_currency()

    # Snapshot de los GIDs que necesitamos fuera de la sesión para llamar a
    # Shopify y no toparnos con DetachedInstanceError.
    price_list_gid: str | None = None
    prev_bulk_gid: str | None = None

    with get_session() as session:
        upload = _refresh_upload(session, upload_id)
        seg_cfg = _get_segment_config(session, segment, shop_domain)

        # 1) Asegurar PriceList (lo más importante: si no existe, crear).
        if not seg_cfg.price_list_gid:
            try:
                gid = ensure_price_list(
                    shop_domain,
                    access_token,
                    name=SEGMENT_LABELS[segment],
                    currency=currency,
                )
            except Exception as e:
                logger.exception(
                    "Error creando PriceList",
                    extra={"segment": segment},
                )
                return "FAILED", f"priceListCreate: {e}"
            seg_cfg.price_list_gid = gid

        price_list_gid = seg_cfg.price_list_gid
        prev_bulk_gid = getattr(upload, attrs["bulk_gid"]) or None
        session.commit()

    # 2) Asegurar Catalog (fuera de la sesión para evitar lock largo).
    # Si hay companies del segmento (env override o BD), creamos el Catalog
    # apuntando al PriceList. Si Shopify falla, no abortamos: el bulk de
    # precios todavía se puede ejecutar porque el PriceList existe.
    with get_session() as session:
        seg_cfg = _get_segment_config(session, segment, shop_domain)
        catalog_already = bool(seg_cfg.catalog_gid)
    if not catalog_already and price_list_gid:
        try:
            location_gids, debug = resolve_company_location_ids(
                segment,
                shop_domain=shop_domain,
                access_token=access_token,
            )
            if location_gids:
                cat_gid = ensure_catalog(
                    shop_domain,
                    access_token,
                    title=SEGMENT_LABELS[segment],
                    company_location_ids=location_gids,
                    price_list_id=price_list_gid,
                )
                with get_session() as s2:
                    seg_cfg2 = _get_segment_config(s2, segment, shop_domain)
                    seg_cfg2.catalog_gid = cat_gid
                    seg_cfg2.company_location_ids = ",".join(location_gids)
                    s2.commit()
                logger.info(
                    "Catalog creado",
                    extra={
                        "segment": segment,
                        "catalog_gid": cat_gid,
                        "locations": len(location_gids),
                        "source": debug.get("source"),
                    },
                )
            else:
                logger.warning(
                    "Sin companyLocationIds para %s — Catalog no se crea "
                    "(el PriceList se mantiene sin Catalog hasta que haya "
                    "companies aprobadas en Shopify).",
                    segment,
                    extra=debug,
                )
        except Exception as e:
            # No fatal: el PriceList ya existe; el admin puede linkear el
            # Catalog manualmente en Shopify si la mutación falla.
            logger.warning(
                "No se pudo crear el Catalog para %s: %s; "
                "se continúa sólo con el PriceList",
                segment,
                e,
            )

    # ----- Idempotencia: si ya había un bulk_gid de un intento previo,
    # revisamos qué pasó con él antes de relanzar.
    if prev_bulk_gid:
        try:
            state = poll_bulk_operation(shop_domain, access_token, prev_bulk_gid)
            prev_status = (state.get("status") or "").upper()
        except LookupError:
            prev_status, state = "EXPIRED", {}
        if prev_status == "COMPLETED":
            with get_session() as s2:
                u2 = _refresh_upload(s2, upload_id)
                setattr(u2, attrs["bulk_status"], "COMPLETED")
                s2.commit()
            return "COMPLETED", None
        if prev_status in ("RUNNING", "CREATED"):
            logger.info(
                "bulk op previo aún %s, esperándolo",
                prev_status,
                extra={"gid": prev_bulk_gid},
            )
            try:
                final = wait_for_bulk(
                    shop_domain, access_token, prev_bulk_gid
                )
            except TimeoutError:
                return "RUNNING", None
            final_status = (final.get("status") or "").upper()
            with get_session() as s2:
                u2 = _refresh_upload(s2, upload_id)
                setattr(u2, attrs["bulk_status"], final_status)
                s2.commit()
            if final_status == "COMPLETED":
                return "COMPLETED", None
            return "FAILED", final.get("errorCode")
        # FAILED/CANCELED/EXPIRED → relanzamos pisando el GID anterior.

    # 3) Construir items del segmento.
    price_items, missing = _build_price_items(items, segment, sku_to_variant)
    if not price_items:
        return (
            "FAILED",
            f"Segmento {segment}: no hay precios válidos para subir "
            f"(missing skus: {len(missing)})",
        )

    if not price_list_gid:
        return "FAILED", "PriceList GID no resuelto"

    # 4) Lanzar bulk.
    try:
        bulk_gid = run_bulk_price_update(
            shop_domain,
            access_token,
            price_list_id=price_list_gid,
            items=price_items,
            currency=currency,
            client_identifier=f"upload:{upload_id}:{segment}",
        )
    except Exception as e:
        logger.exception(
            "Error lanzando bulkOperationRunMutation",
            extra={"segment": segment, "items": len(price_items)},
        )
        return "FAILED", f"bulkOperationRunMutation: {e}"

    with get_session() as session:
        upload = _refresh_upload(session, upload_id)
        setattr(upload, attrs["bulk_gid"], bulk_gid)
        setattr(upload, attrs["bulk_status"], "RUNNING")
        session.commit()

    # 5) Polling.
    try:
        final = wait_for_bulk(shop_domain, access_token, bulk_gid)
    except TimeoutError:
        # Lo dejamos en RUNNING; el admin puede llamar /refresh después.
        return "RUNNING", None

    final_status = (final.get("status") or "").upper()
    with get_session() as session:
        upload = _refresh_upload(session, upload_id)
        setattr(upload, attrs["bulk_status"], final_status)
        session.commit()

    if final_status == "COMPLETED":
        return "COMPLETED", None
    return "FAILED", final.get("errorCode") or final_status


def process_upload(upload_id: uuid_mod.UUID) -> dict[str, Any]:
    """Pipeline completo: parse → resolve variantes → bulk x3 segmentos."""
    _set_status(upload_id, "PROCESSING", error_message=None)

    # 1) Cargar upload + descargar Excel.
    with get_session() as session:
        upload = _refresh_upload(session, upload_id)
        if not upload.s3_bucket or not upload.s3_key:
            raise ValueError("upload sin s3_bucket/s3_key")
        bucket = upload.s3_bucket
        key = upload.s3_key

    excel_bytes = get_excel(bucket, key)

    # 2) Parsear.
    parse_result = parse_price_list_excel(excel_bytes)
    items = parse_result.items
    with get_session() as session:
        upload = _refresh_upload(session, upload_id)
        upload.parsed_items = len(items)
        upload.duplicates_overwritten = parse_result.duplicates_overwritten
        upload.rows_skipped = parse_result.rows_skipped
        session.commit()

    if not items:
        _set_status(
            upload_id,
            "FAILED",
            error_message="El Excel no tiene filas con precios para cargar",
        )
        return {"upload_id": str(upload_id), "status": "FAILED"}

    # 3) Shop + token + lookup variantes.
    shop_domain, access_token = resolve_shop_and_token()
    sku_to_variant = fetch_all_sku_to_variant_id(shop_domain, access_token)
    skus = [it.sap_code for it in items]
    matched, missing = resolve_variants_for_skus(sku_to_variant, skus)

    sample = ",".join(missing[:50])
    with get_session() as session:
        upload = _refresh_upload(session, upload_id)
        upload.variants_resolved = len(matched)
        upload.variants_missing = len(missing)
        upload.missing_skus_sample = sample or None
        session.commit()

    if not matched:
        _set_status(
            upload_id,
            "FAILED",
            error_message=(
                "Ningún SAP del Excel coincide con un SKU de Shopify. "
                "Revise que los productos estén importados con el SAP en el SKU."
            ),
        )
        return {"upload_id": str(upload_id), "status": "FAILED"}

    # 4) Procesar 3 segmentos (Shopify limita a 1 bulk a la vez).
    results: dict[str, dict[str, Any]] = {}
    any_failed = False
    any_running = False
    for segment in VALID_SEGMENTS:
        status, err = _process_segment(
            upload_id=upload_id,
            segment=segment,
            shop_domain=shop_domain,
            access_token=access_token,
            items=items,
            sku_to_variant=matched,
        )
        results[segment] = {"status": status, "error": err}
        if status == "FAILED":
            any_failed = True
            logger.error(
                "Segmento falló",
                extra={"segment": segment, "error": err},
            )
        elif status == "RUNNING":
            any_running = True

    # 5) Status final del upload.
    if any_running:
        final = "PUSHED"  # uno o más siguen corriendo en Shopify
    elif any_failed:
        final = "PARTIAL" if any(
            r["status"] == "COMPLETED" for r in results.values()
        ) else "FAILED"
    else:
        final = "COMPLETED"

    _set_status(upload_id, final)

    logger.info(
        "Upload procesado",
        extra={
            "upload_id": str(upload_id),
            "final_status": final,
            "results": results,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return {"upload_id": str(upload_id), "status": final, "results": results}


def rebuild_segment_catalog(segment: str) -> dict[str, Any]:
    """Crea (o intenta crear) el ``Catalog`` Shopify del segmento.

    Pensado para ejecutarse fuera del flujo de upload (endpoint admin) cuando:
    - El servicio creó el PriceList pero no llegó a crear el Catalog porque no
      había companies aprobadas en BD.
    - Se aprobaron nuevas companies y queremos refrescar las associations.

    Reglas:
    - Si el segmento ya tiene ``catalog_gid`` persistido y el catálogo
      todavía existe en Shopify (vía ``priceList.catalog``), NO recrea: sólo
      devuelve el estado actual.
    - Si no hay ``price_list_gid`` para el segmento, lanza ``ValueError``: hay
      que subir el primer Excel para tener un PriceList.

    Devuelve un dict con el estado de la operación.
    """
    if segment not in VALID_SEGMENTS:
        raise ValueError(
            f"Segmento inválido '{segment}'. Válidos: {VALID_SEGMENTS}"
        )

    shop_domain, access_token = resolve_shop_and_token()
    with get_session() as session:
        seg_cfg = _get_segment_config(session, segment, shop_domain)
        price_list_gid = seg_cfg.price_list_gid
        existing_catalog_gid = seg_cfg.catalog_gid
        existing_locations = seg_cfg.company_location_ids
        session.commit()

    if not price_list_gid:
        raise ValueError(
            f"Segmento {segment} no tiene PriceList todavía. "
            "Subí primero un Excel para crearlo."
        )

    # Verificar el estado real del PriceList y del Catalog en Shopify.
    try:
        pl_summary = get_price_list_summary(
            shop_domain, access_token, price_list_gid
        )
    except LookupError:
        return {
            "segment": segment,
            "status": "FAILED",
            "error": (
                f"PriceList {price_list_gid} no existe en Shopify "
                "(probablemente fue borrado). Subí un Excel nuevo."
            ),
        }

    pl_catalog = pl_summary.get("catalog") or None

    if pl_catalog and isinstance(pl_catalog, dict):
        # Ya existe. Si difiere del cache local, sincronizamos la BD.
        cat_id = pl_catalog.get("id")
        if cat_id and cat_id != existing_catalog_gid:
            with get_session() as s2:
                seg_cfg2 = _get_segment_config(s2, segment, shop_domain)
                seg_cfg2.catalog_gid = cat_id
                s2.commit()
        return {
            "segment": segment,
            "status": "ALREADY_LINKED",
            "price_list_gid": price_list_gid,
            "catalog_gid": cat_id,
            "catalog_title": pl_catalog.get("title"),
            "catalog_status": pl_catalog.get("status"),
            "fixed_prices_count": pl_summary.get("fixedPricesCount"),
            "company_location_ids_db": existing_locations,
        }

    # No tiene catalog. Resolvemos locations y creamos.
    location_gids, debug = resolve_company_location_ids(
        segment,
        shop_domain=shop_domain,
        access_token=access_token,
    )
    if not location_gids:
        return {
            "segment": segment,
            "status": "NO_LOCATIONS",
            "price_list_gid": price_list_gid,
            "fixed_prices_count": pl_summary.get("fixedPricesCount"),
            "message": (
                f"No hay CompanyLocations para {segment}: agregá "
                f"companies con company_type='{SEGMENT_COMPANY_TYPE[segment]}' "
                "y shopify_company_id, o usá la env "
                f"{_SEGMENT_ATTRS[segment]['env_locations']}."
            ),
            "debug": debug,
        }

    try:
        cat_gid = ensure_catalog(
            shop_domain,
            access_token,
            title=SEGMENT_LABELS[segment],
            company_location_ids=location_gids,
            price_list_id=price_list_gid,
        )
    except Exception as e:
        logger.exception(
            "rebuild_segment_catalog: catalogCreate falló",
            extra={"segment": segment, "locations": location_gids},
        )
        return {
            "segment": segment,
            "status": "FAILED",
            "error": f"catalogCreate: {e}",
            "price_list_gid": price_list_gid,
            "company_locations": location_gids,
            "debug": debug,
        }

    with get_session() as session:
        seg_cfg = _get_segment_config(session, segment, shop_domain)
        seg_cfg.catalog_gid = cat_gid
        seg_cfg.company_location_ids = ",".join(location_gids)
        session.commit()

    logger.info(
        "rebuild_segment_catalog: Catalog creado",
        extra={
            "segment": segment,
            "catalog_gid": cat_gid,
            "locations": len(location_gids),
            "source": debug.get("source"),
        },
    )
    return {
        "segment": segment,
        "status": "CREATED",
        "price_list_gid": price_list_gid,
        "catalog_gid": cat_gid,
        "company_location_ids": location_gids,
        "fixed_prices_count": pl_summary.get("fixedPricesCount"),
        "source": debug.get("source"),
    }


def refresh_upload_status(upload_id: uuid_mod.UUID) -> dict[str, Any]:
    """Re-consulta los bulk operations en Shopify y actualiza la fila.

    Útil cuando el worker quedó en ``PUSHED`` (algún bulk seguía RUNNING al
    expirar el polling) y el frontend quiere saber el estado actual.
    """
    with get_session() as session:
        upload = _refresh_upload(session, upload_id)
        snapshot = {
            attrs["bulk_gid"]: getattr(upload, attrs["bulk_gid"])
            for _, attrs in _SEGMENT_ATTRS.items()
        }
        seg_status_attrs = {
            seg: attrs["bulk_status"] for seg, attrs in _SEGMENT_ATTRS.items()
        }

    shop_domain, access_token = resolve_shop_and_token()
    new_states: dict[str, str | None] = {}
    for seg, attrs in _SEGMENT_ATTRS.items():
        gid = snapshot.get(attrs["bulk_gid"])
        if not gid:
            new_states[seg] = None
            continue
        try:
            state = poll_bulk_operation(shop_domain, access_token, gid)
            new_states[seg] = (state.get("status") or "").upper() or None
        except LookupError:
            new_states[seg] = "EXPIRED"

    with get_session() as session:
        upload = _refresh_upload(session, upload_id)
        for seg, st in new_states.items():
            setattr(upload, seg_status_attrs[seg], st)
        # Re-evaluar status global.
        statuses = list(new_states.values())
        if any(s == "RUNNING" or s == "CREATED" for s in statuses):
            upload.status = "PUSHED"
        elif all(s == "COMPLETED" for s in statuses if s):
            upload.status = "COMPLETED"
        elif any(s == "COMPLETED" for s in statuses) and any(
            s in ("FAILED", "CANCELED", "EXPIRED") for s in statuses if s
        ):
            upload.status = "PARTIAL"
        elif all(
            s in ("FAILED", "CANCELED", "EXPIRED")
            for s in statuses
            if s
        ):
            upload.status = "FAILED"
        session.commit()
        out = upload.to_dict()
    return out

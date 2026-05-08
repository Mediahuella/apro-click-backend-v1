"""Cliente Shopify B2B para el servicio prices.

Funciones expuestas:

- ``ensure_segment_resources``: crea (la primera vez) ``PriceList`` y
  ``Catalog`` en Shopify para un segmento (PYME / MEDIANA / GRAN_EMPRESA).
  Idempotente: si ya hay GIDs guardados en la BD, los reutiliza.
- ``run_bulk_price_update``: stage del JSONL + ``bulkOperationRunMutation``
  con ``priceListFixedPricesAdd`` para subir miles de precios sin pegar
  contra el rate limit.
- ``poll_bulk_operation``: consulta ``BulkOperation`` por GID.
- ``current_bulk_operation``: consulta el bulk en curso (sin filtrar por GID,
  útil para detectar conflictos: "ya hay uno corriendo").

Restricción Shopify: **una sola bulk mutation a la vez por shop**, por eso el
worker procesa los 3 segmentos en serie.
"""
from __future__ import annotations

import io
import json
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from aws_lambda_powertools import Logger

service_root = Path(__file__).resolve().parent.parent
if str(service_root) not in sys.path:
    sys.path.insert(0, str(service_root))

lambda_root = "/var/task"
if lambda_root not in sys.path:
    sys.path.insert(0, lambda_root)

from utils.shopify_graphql import format_user_errors, graphql_call  # noqa: E402

logger = Logger()


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

#: Mapping segmento → nombre amigable para el ``Catalog`` y el ``PriceList``
#: en Shopify. El nombre del PriceList debe ser único en la tienda.
SEGMENT_LABELS: dict[str, str] = {
    "PYME": "Apro Click — PYME",
    "MEDIANA": "Apro Click — Empresa-Distribuidor (Mediana)",
    "GRAN_EMPRESA": "Apro Click — Gran Empresa",
}

#: Mapping segmento → columna del Excel parseado de la que sale el precio.
SEGMENT_PRICE_COLUMN: dict[str, str] = {
    "PYME": "price_pyme_neto",
    "MEDIANA": "price_distribuidor_neto",
    "GRAN_EMPRESA": "price_gran_empresa_neto",
}

VALID_SEGMENTS = tuple(SEGMENT_LABELS.keys())


# ---------------------------------------------------------------------------
# GraphQL operations (constantes)
# ---------------------------------------------------------------------------

_PRICE_LIST_CREATE = """
mutation PriceListCreate($input: PriceListCreateInput!) {
  priceListCreate(input: $input) {
    priceList { id name currency parent { adjustment { type value } } }
    userErrors { field message code }
  }
}
"""

_CATALOG_CREATE = """
mutation CatalogCreate($input: CatalogCreateInput!) {
  catalogCreate(input: $input) {
    catalog {
      id
      title
      status
      priceList { id }
    }
    userErrors { field message code }
  }
}
"""

_CATALOG_CONTEXT_UPDATE = """
mutation CatalogContextUpdate(
  $catalogId: ID!,
  $contextsToAdd: [String!],
  $contextsToRemove: [String!]
) {
  catalogContextUpdate(
    catalogId: $catalogId,
    contextsToAdd: $contextsToAdd,
    contextsToRemove: $contextsToRemove
  ) {
    catalog { id title status }
    userErrors { field message code }
  }
}
"""

_PRICE_LIST_BY_ID = """
query PriceListById($id: ID!) {
  priceList(id: $id) {
    id
    name
    currency
    fixedPricesCount
    catalog { id title status }
  }
}
"""

_STAGED_UPLOADS_CREATE = """
mutation StagedUploadsCreate($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets {
      url
      resourceUrl
      parameters { name value }
    }
    userErrors { field message }
  }
}
"""

_BULK_OPERATION_RUN_MUTATION = """
mutation BulkOpRun($mutation: String!, $stagedUploadPath: String!, $clientIdentifier: String) {
  bulkOperationRunMutation(
    mutation: $mutation
    stagedUploadPath: $stagedUploadPath
    clientIdentifier: $clientIdentifier
  ) {
    bulkOperation { id status type }
    userErrors { field message code }
  }
}
"""

_BULK_OPERATION_BY_ID = """
query BulkOpById($id: ID!) {
  node(id: $id) {
    ... on BulkOperation {
      id
      status
      errorCode
      objectCount
      url
      partialDataUrl
      createdAt
      completedAt
    }
  }
}
"""

_CURRENT_BULK_OPERATION = """
query CurrentBulkOp {
  currentBulkOperation(type: MUTATION) {
    id
    status
  }
}
"""

# Texto de la mutación que Shopify ejecuta por cada línea del JSONL.
# El selection set se mantiene chico a propósito: lo único que nos interesa
# es saber si la mutación falló (``userErrors``); los precios cargados se
# verifican consultando el PriceList si hace falta.
PRICE_LIST_FIXED_PRICES_ADD_TEMPLATE = (
    "mutation call($priceListId: ID!, $prices: [PriceListPriceInput!]!) {"
    "  priceListFixedPricesAdd(priceListId: $priceListId, prices: $prices) {"
    "    userErrors { field code message }"
    "  }"
    "}"
)


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------


def _check_user_errors(payload: dict[str, Any], op_label: str) -> None:
    errs = payload.get("userErrors") or []
    if errs:
        raise ValueError(
            f"Shopify {op_label} userErrors: {format_user_errors(errs)}"
        )


def _format_amount(value: Decimal | float | int | str) -> str:
    """Devuelve un string apto para ``MoneyInput.amount``.

    Shopify acepta strings tipo ``'12345.67'``. Para CLP no usamos decimales
    en la práctica pero los conservamos por si Shopify los necesita para evitar
    redondeos.
    """
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value)


def _post_staged_target(target: dict[str, Any], file_bytes: bytes) -> None:
    """Sube ``file_bytes`` al ``url`` del target con multipart manual.

    Shopify devuelve un upload "Google Cloud Storage" o S3. Usamos POST
    multipart con los ``parameters`` listados (nombre/valor) y un campo
    ``file`` final con el body.
    """
    url = target.get("url")
    parameters = target.get("parameters") or []
    if not url:
        raise ValueError("staged upload sin URL")

    boundary = "----aproclickbulkupload" + str(int(time.time() * 1000))
    crlf = b"\r\n"
    parts: list[bytes] = []
    for p in parameters:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "")
        val = str(p.get("value") or "")
        if not name:
            continue
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{val}"
            ).encode("utf-8")
        )
    parts.append(
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="bulk.jsonl"\r\n'
            "Content-Type: text/jsonl\r\n\r\n"
        ).encode("utf-8")
    )
    body = crlf.join(parts) + crlf + file_bytes + crlf + f"--{boundary}--\r\n".encode("utf-8")

    req = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    try:
        with urlopen(req, timeout=120) as resp:
            status = resp.getcode()
            if status >= 300:
                raise ValueError(
                    f"Staged upload POST falló: HTTP {status}"
                )
    except HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except OSError:
            detail = str(e.code)
        raise ValueError(
            f"Staged upload POST HTTP {e.code}: {detail[:500]}"
        ) from e
    except OSError as e:
        raise ValueError(f"Staged upload POST error: {e}") from e


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PriceItem:
    """Una línea de precio para el bulk import."""

    variant_id: str  # gid://shopify/ProductVariant/...
    amount: str  # decimal as string (e.g. "12345.50")
    compare_at_amount: str | None = None


def ensure_price_list(
    shop_domain: str,
    access_token: str,
    *,
    name: str,
    currency: str,
    catalog_id: str | None = None,
) -> str:
    """Crea un PriceList con adjustment 0% (precios fijos puros).

    Shopify exige un ``parent.adjustment``. Usamos PERCENTAGE_DECREASE 0% que
    funciona como "no adjustment" — los precios fijos definen la realidad.

    Devuelve el GID. Si Shopify ya tiene uno con el mismo nombre, lanza
    ``ValueError`` (queda a cargo del caller verificar antes de llamar).
    """
    inp: dict[str, Any] = {
        "name": name,
        "currency": currency,
        "parent": {
            "adjustment": {
                "type": "PERCENTAGE_DECREASE",
                "value": 0,
            }
        },
    }
    if catalog_id:
        inp["catalogId"] = catalog_id
    data = graphql_call(
        shop_domain, access_token, _PRICE_LIST_CREATE, {"input": inp}
    )
    payload = data.get("priceListCreate") or {}
    _check_user_errors(payload, "priceListCreate")
    pl = payload.get("priceList") or {}
    gid = pl.get("id")
    if not isinstance(gid, str):
        raise ValueError("priceListCreate no devolvió id")
    return gid


def ensure_catalog(
    shop_domain: str,
    access_token: str,
    *,
    title: str,
    company_location_ids: list[str],
    price_list_id: str | None,
    status: str = "ACTIVE",
) -> str:
    """Crea un Catalog asociado a las companyLocations indicadas.

    Devuelve el GID. Lanza ``ValueError`` con userErrors si falla.

    Nota: en ``CatalogCreateInput`` el campo se llama ``companyLocationIds``
    (sin envoltorio ``context`` en el schema actual de la Admin API). Si
    Shopify cambia el shape, el ``userErrors`` lo va a reflejar.
    """
    inp: dict[str, Any] = {
        "title": title,
        "status": status,
        "context": {"companyLocationIds": company_location_ids},
    }
    if price_list_id:
        inp["priceListId"] = price_list_id
    data = graphql_call(
        shop_domain, access_token, _CATALOG_CREATE, {"input": inp}
    )
    payload = data.get("catalogCreate") or {}
    _check_user_errors(payload, "catalogCreate")
    cat = payload.get("catalog") or {}
    gid = cat.get("id")
    if not isinstance(gid, str):
        raise ValueError("catalogCreate no devolvió id")
    return gid


def update_catalog_locations(
    shop_domain: str,
    access_token: str,
    *,
    catalog_id: str,
    add_company_location_ids: list[str] | None = None,
    remove_company_location_ids: list[str] | None = None,
) -> None:
    """Agrega o quita ``CompanyLocation`` de un Catalog ya existente.

    Útil cuando se aprueba/rechaza una company y hay que mover su location
    al/del catálogo del segmento. Lanza ``ValueError`` con userErrors si falla.
    """
    add = list(add_company_location_ids or [])
    rm = list(remove_company_location_ids or [])
    if not add and not rm:
        return
    data = graphql_call(
        shop_domain,
        access_token,
        _CATALOG_CONTEXT_UPDATE,
        {
            "catalogId": catalog_id,
            "contextsToAdd": add or None,
            "contextsToRemove": rm or None,
        },
    )
    payload = data.get("catalogContextUpdate") or {}
    _check_user_errors(payload, "catalogContextUpdate")


def get_price_list_summary(
    shop_domain: str, access_token: str, price_list_id: str
) -> dict[str, Any]:
    """Devuelve ``{id, name, currency, fixedPricesCount, catalog: {id,title,status}|None}``.

    Lanza ``LookupError`` si Shopify no encuentra el PriceList.
    """
    data = graphql_call(
        shop_domain, access_token, _PRICE_LIST_BY_ID, {"id": price_list_id}
    )
    pl = data.get("priceList")
    if not isinstance(pl, dict):
        raise LookupError(f"PriceList no encontrado: {price_list_id}")
    return {
        "id": pl.get("id"),
        "name": pl.get("name"),
        "currency": pl.get("currency"),
        "fixedPricesCount": pl.get("fixedPricesCount"),
        "catalog": pl.get("catalog"),
    }


def stage_bulk_jsonl(
    shop_domain: str,
    access_token: str,
    *,
    file_bytes: bytes,
    filename: str = "prices.jsonl",
) -> str:
    """Sube el JSONL a Shopify y devuelve el ``stagedUploadPath`` resultante.

    El path es lo que se le pasa después a ``bulkOperationRunMutation``.
    """
    data = graphql_call(
        shop_domain,
        access_token,
        _STAGED_UPLOADS_CREATE,
        {
            "input": [
                {
                    "filename": filename,
                    "mimeType": "text/jsonl",
                    "resource": "BULK_MUTATION_VARIABLES",
                    "httpMethod": "POST",
                }
            ]
        },
    )
    payload = data.get("stagedUploadsCreate") or {}
    _check_user_errors(payload, "stagedUploadsCreate")
    targets = payload.get("stagedTargets") or []
    if not targets:
        raise ValueError("stagedUploadsCreate no devolvió targets")
    target = targets[0]
    _post_staged_target(target, file_bytes)
    # Shopify expone el path como param "key" (S3-like).
    for p in target.get("parameters") or []:
        if isinstance(p, dict) and (p.get("name") or "").lower() == "key":
            return str(p.get("value") or "")
    raise ValueError(
        "stagedUploadsCreate target sin parameter 'key'"
    )


def run_bulk_price_update(
    shop_domain: str,
    access_token: str,
    *,
    price_list_id: str,
    items: Iterable[PriceItem],
    currency: str,
    chunk_size: int = 250,
    client_identifier: str | None = None,
) -> str:
    """Lanza una bulk mutation para upsertar precios.

    Genera un JSONL donde cada línea es ``{priceListId, prices: [...]}`` con
    hasta ``chunk_size`` precios por línea (Shopify acepta listas; cada línea
    es una invocación de la mutación).

    Devuelve el ``BulkOperation`` GID. Lanza ``ValueError`` si Shopify ya
    tiene una bulk mutation corriendo (caller debe esperar antes de reintentar).
    """
    items_list = list(items)
    if not items_list:
        raise ValueError("No hay precios para subir")

    buf = io.StringIO()
    chunk: list[dict[str, Any]] = []

    def _flush() -> None:
        if not chunk:
            return
        line = {
            "priceListId": price_list_id,
            "prices": list(chunk),
        }
        buf.write(json.dumps(line, ensure_ascii=False))
        buf.write("\n")
        chunk.clear()

    for it in items_list:
        entry: dict[str, Any] = {
            "variantId": it.variant_id,
            "price": {"amount": it.amount, "currencyCode": currency},
        }
        if it.compare_at_amount:
            entry["compareAtPrice"] = {
                "amount": it.compare_at_amount,
                "currencyCode": currency,
            }
        chunk.append(entry)
        if len(chunk) >= chunk_size:
            _flush()
    _flush()

    jsonl_bytes = buf.getvalue().encode("utf-8")
    staged_path = stage_bulk_jsonl(
        shop_domain, access_token, file_bytes=jsonl_bytes
    )
    data = graphql_call(
        shop_domain,
        access_token,
        _BULK_OPERATION_RUN_MUTATION,
        {
            "mutation": PRICE_LIST_FIXED_PRICES_ADD_TEMPLATE,
            "stagedUploadPath": staged_path,
            "clientIdentifier": client_identifier,
        },
    )
    payload = data.get("bulkOperationRunMutation") or {}
    _check_user_errors(payload, "bulkOperationRunMutation")
    op = payload.get("bulkOperation") or {}
    gid = op.get("id")
    if not isinstance(gid, str):
        raise ValueError("bulkOperationRunMutation no devolvió id")
    logger.info(
        "bulkOperationRunMutation iniciada",
        extra={
            "bulk_op_gid": gid,
            "price_list_id": price_list_id,
            "items": len(items_list),
            "client_identifier": client_identifier,
        },
    )
    return gid


def poll_bulk_operation(
    shop_domain: str,
    access_token: str,
    bulk_op_gid: str,
) -> dict[str, Any]:
    """Devuelve ``{status, errorCode, objectCount, url, partialDataUrl, completedAt}``.

    Si Shopify devuelve ``node = null`` (op desconocida o expirada), lanza
    ``LookupError``.
    """
    data = graphql_call(
        shop_domain, access_token, _BULK_OPERATION_BY_ID, {"id": bulk_op_gid}
    )
    node = data.get("node")
    if not isinstance(node, dict):
        raise LookupError(f"BulkOperation no encontrada: {bulk_op_gid}")
    return {
        "id": node.get("id"),
        "status": node.get("status"),
        "errorCode": node.get("errorCode"),
        "objectCount": node.get("objectCount"),
        "url": node.get("url"),
        "partialDataUrl": node.get("partialDataUrl"),
        "completedAt": node.get("completedAt"),
    }


def current_bulk_mutation(
    shop_domain: str, access_token: str
) -> dict[str, Any] | None:
    """Devuelve la bulk mutation en curso o ``None`` si no hay ninguna."""
    data = graphql_call(
        shop_domain, access_token, _CURRENT_BULK_OPERATION, {}
    )
    op = data.get("currentBulkOperation")
    if not isinstance(op, dict):
        return None
    return op


def wait_for_bulk(
    shop_domain: str,
    access_token: str,
    bulk_op_gid: str,
    *,
    poll_interval_sec: float = 5.0,
    max_wait_sec: float = 240.0,
) -> dict[str, Any]:
    """Polling síncrono. Devuelve el último estado.

    No lanza si termina en ``FAILED``: devuelve el dict para que el caller
    decida (marcar el segmento como FAILED y seguir con el siguiente).

    Lanza ``TimeoutError`` si supera ``max_wait_sec``.
    """
    start = time.monotonic()
    state: dict[str, Any] = {}
    while True:
        state = poll_bulk_operation(shop_domain, access_token, bulk_op_gid)
        status = (state.get("status") or "").upper()
        if status in ("COMPLETED", "FAILED", "CANCELED", "EXPIRED"):
            return state
        if time.monotonic() - start >= max_wait_sec:
            raise TimeoutError(
                f"BulkOperation {bulk_op_gid} no terminó en {max_wait_sec}s "
                f"(último status: {status})"
            )
        time.sleep(poll_interval_sec)

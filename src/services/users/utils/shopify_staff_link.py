"""Vincular usuarios del panel con StaffMember de Shopify (mismo email).

La **API GraphQL pública** de Admin (2026-04) **no** expone una mutación para
crear/invitar staff: no existe `staffMemberCreate` en el esquema. Por eso, por
defecto solo se hace búsqueda con `staffMembers` y, si ya existe, se guarda el GID.

Para acercar el flujo a “crear si no existe”:
- Respuesta con `shopify_admin_users_url` hacia *Settings → Users* de la tienda.
- Si defines `SHOPIFY_STAFF_PROVISIONER_URL`, tras un *no match* se hace un POST
  opcional a vuestro servicio (n8n, Lambda privada, integración con ayuda de
  Shopify, etc.); si devuelve un GID `gid://shopify/StaffMember/...`, se persiste
  como *LINKED* (contrato bajo vuestro control).

Requisitos: token de `shopify_app_installations`, scope `read_users` en la app,
y acceso a la query `staffMembers` (suele requerir Plus/Advanced o habilitación).
"""
from __future__ import annotations

import json
import os
import uuid as uuid_mod
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from aws_lambda_powertools import Logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from database.engine import get_session
from database.models.company import Company
from database.models.shopify import ShopifyAppInstallation
from database.models.user import User

logger = Logger()

LINK_STATUS_LINKED = "LINKED"
LINK_STATUS_NOT_FOUND = "NOT_FOUND"
LINK_STATUS_SKIPPED_ROLE = "SKIPPED_ROLE"
LINK_STATUS_SKIPPED_NO_SHOP = "SKIPPED_NO_SHOP"
LINK_STATUS_ERROR = "ERROR"

# Textos para el cliente API (se devuelven en el JSON, no se persisten en BD)
MSG_NOT_FOUND = (
    "En Shopify aún no hay un staff con este email. La API pública de Shopify no "
    "puede invitar/crear staff: invítaelos en Admin (mismo email) usando "
    "shopify_admin_users_url, o configura SHOPIFY_STAFF_PROVISIONER_URL, y vuelve a "
    "vincular con POST /api/v1/users/{id}/link-shopify-staff."
)
MSG_PROVISIONER_FAILED = (
    "No se encontró staff en Shopify; el aprovisionador externo (si está configurado) "
    "no devolvió un GID válido. Revisad logs o invitad manualmente y reintentad."
)
MSG_SKIPPED_NO_SHOP = (
    "No hay instalación de la app con token de Shopify (empresa plataforma o cualquier "
    "tienda activa). Revisa OAuth y que shopify_app_installations tenga access_token."
)
MSG_SKIPPED_ROLE = "Este rol no se vincula con staff de Shopify (p. ej. KPI_VISUALIZERS)."
MSG_LINKED = "Vínculo guardado: StaffMember encontrado en Shopify con el mismo email."

_ROLES_WITH_SHOPIFY_STAFF = frozenset({"SUPERADMIN", "ADMIN", "SALES"})

_STAFF_BY_EMAIL = """
query StaffMembersByEmail($query: String!) {
  staffMembers(first: 25, query: $query) {
    nodes {
      id
      email
    }
    edges {
      node {
        id
        email
      }
    }
  }
}
"""

_STAFF_MEMBER_BY_ID = """
query StaffMemberById($id: ID!) {
  staffMember(id: $id) {
    id
    email
  }
}
"""

MSG_ASSOCIATED_VERIFIED = (
    "Staff asociado: el GID es válido y el email del StaffMember en Shopify "
    "coincide con el del usuario en el panel."
)
MSG_ASSOCIATED_UNVERIFIED = (
    "Staff asociado por GID sin comprobar email en Shopify (sin token de tienda o "
    "skip_email_verification=true). Verificad manualmente que sea el colaborador correcto."
)


def _api_version() -> str:
    return (os.environ.get("SHOPIFY_API_VERSION") or "2026-04").strip()


def _apro_click_company_id(session: Session) -> uuid_mod.UUID | None:
    row = session.scalars(
        select(Company)
        .where(Company.is_system.is_(True))
        .order_by(Company.id.asc())
        .limit(1)
    ).first()
    if row is not None:
        return row.id
    raw = os.environ.get("APRO_CLICK_COMPANY_ID", "").strip()
    if not raw:
        return None
    try:
        cid = uuid_mod.UUID(raw)
    except ValueError:
        return None
    return cid if session.get(Company, cid) is not None else None


def _resolve_shop_token(session: Session) -> tuple[str, str] | None:
    platform_id = _apro_click_company_id(session)
    if platform_id is not None:
        row = session.scalar(
            select(ShopifyAppInstallation)
            .where(
                ShopifyAppInstallation.company_id == platform_id,
                ShopifyAppInstallation.uninstalled_at.is_(None),
                ShopifyAppInstallation.shopify_access_token.is_not(None),
            )
            .order_by(ShopifyAppInstallation.installed_at.desc())
        )
        if row and row.shopify_access_token:
            return row.shop_domain, row.shopify_access_token.strip()
    row = session.scalar(
        select(ShopifyAppInstallation)
        .where(
            ShopifyAppInstallation.uninstalled_at.is_(None),
            ShopifyAppInstallation.shopify_access_token.is_not(None),
        )
        .order_by(ShopifyAppInstallation.installed_at.desc())
    )
    if row and row.shopify_access_token:
        return row.shop_domain, row.shopify_access_token.strip()
    return None


def _admin_users_settings_url(shop_domain: str) -> str:
    return f"https://{shop_domain}/admin/settings/users"


def _http_post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    h: dict[str, str] = {"Content-Type": "application/json", **(headers or {})}
    req = Request(url, data=data, method="POST", headers=h)
    with urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _extract_gid_from_provisioner_response(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    for key in (
        "shopifyStaffMemberGid",
        "staffMemberGid",
        "shopify_staff_member_id",
    ):
        v = body.get(key)
        if isinstance(v, str) and v.startswith("gid://shopify/StaffMember/"):
            return v
    nested = body.get("data")
    if isinstance(nested, dict):
        g = _extract_gid_from_provisioner_response(nested)
        if g:
            return g
    vid = body.get("id")
    if isinstance(vid, str) and vid.startswith("gid://shopify/StaffMember/"):
        return vid
    return None


def _try_external_staff_provisioner(
    shop_domain: str,
    user_id: uuid_mod.UUID,
    email: str,
    given_name: str,
    family_name: str,
) -> tuple[str | None, str | None]:
    """
    Si SHOPIFY_STAFF_PROVISIONER_URL apunta a un servicio vuestro que invoque
    a Shopify (o copie un flujo manual), y devuelve un GID, lo usamos.
    Returns (gid, error_message)
    """
    raw_url = (os.environ.get("SHOPIFY_STAFF_PROVISIONER_URL") or "").strip()
    if not raw_url:
        return None, None
    api_key = (os.environ.get("SHOPIFY_STAFF_PROVISIONER_API_KEY") or "").strip()
    h: dict[str, str] = {}
    if api_key:
        h["X-Api-Key"] = api_key
    payload: dict[str, Any] = {
        "userId": str(user_id),
        "email": email.strip(),
        "givenName": (given_name or "").strip(),
        "familyName": (family_name or "").strip(),
        "shopDomain": shop_domain,
    }
    try:
        body = _http_post_json(raw_url, payload, headers=h or None)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        logger.exception(
            "Error llamando a SHOPIFY_STAFF_PROVISIONER_URL",
            extra={"user_id": str(user_id), "email": email},
        )
        return None, str(e)
    gid = _extract_gid_from_provisioner_response(body)
    if gid:
        return gid, None
    return None, "Respuesta del aprovisionador sin GID StaffMember reconocible"


def _graphql(
    shop_domain: str, access_token: str, query: str, variables: dict[str, Any]
) -> dict[str, Any]:
    ver = _api_version()
    url = f"https://{shop_domain}/admin/api/{ver}/graphql.json"
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": access_token,
        },
    )
    with urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _iter_staff_nodes(conn: dict[str, Any]) -> list[dict[str, Any]]:
    """Acepta conexión staffMembers vía `nodes` o `edges[].node` (versiones de API)."""
    out: list[dict[str, Any]] = []
    raw = conn.get("nodes")
    if isinstance(raw, list):
        out.extend(n for n in raw if isinstance(n, dict))
    for edge in conn.get("edges") or []:
        if isinstance(edge, dict) and isinstance(edge.get("node"), dict):
            out.append(edge["node"])
    return out


def _first_graphql_error_message(payload: dict[str, Any]) -> str | None:
    errs = payload.get("errors")
    if not errs or not isinstance(errs, list):
        return None
    first = errs[0] if errs else None
    if not isinstance(first, dict):
        return str(first)
    return first.get("message") or first.get("extensions", {}).get("code") or str(first)


def _email_search_query(email: str) -> str:
    """Sintaxis de búsqueda Admin API; comillas si el email tiene espacios o comillas."""
    e = email.strip()
    if any(ch in e for ch in " \"'+"):
        escaped = e.replace("\\", "\\\\").replace('"', '\\"')
        return f'email:"{escaped}"'
    return f"email:{e}"


def _pick_staff_gid(email: str, payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """
    Returns (gid, graph_error_message). graph_error_message se setea con errores
    top-level de GraphQL (p. ej. acceso denegado a `staffMembers` sin `read_users`).
    """
    gql_err = _first_graphql_error_message(payload)
    if gql_err:
        logger.warning(
            "Shopify GraphQL errors en staffMembers",
            extra={"graphql_error": gql_err, "errors": payload.get("errors")},
        )
        return None, gql_err
    data = payload.get("data")
    if data is None:
        return None, "Respuesta GraphQL sin campo data"
    conn = data.get("staffMembers")
    if conn is None:
        return None, "Campo data.staffMembers ausente o null (revisar permisos read_users / plan de la tienda)"
    if not isinstance(conn, dict):
        return None, "staffMembers con formato inesperado"
    em = email.strip().lower()
    for node in _iter_staff_nodes(conn):
        if (node.get("email") or "").strip().lower() != em:
            continue
        gid = node.get("id")
        if isinstance(gid, str) and gid.startswith("gid://shopify/StaffMember/"):
            return gid, None
    return None, None


def _persist_user_link(
    user_id: uuid_mod.UUID,
    *,
    staff_gid: str | None,
    status: str,
) -> None:
    with get_session() as session:
        user = session.get(User, user_id)
        if user is None:
            return
        user.shopify_staff_member_id = staff_gid
        user.shopify_staff_link_status = status
        session.commit()


def _with_admin_url(shop_domain: str | None, d: dict[str, Any]) -> dict[str, Any]:
    if shop_domain:
        return {
            **d,
            "shopify_admin_users_url": _admin_users_settings_url(shop_domain),
        }
    return d


def is_valid_shopify_staff_member_gid(gid: str) -> bool:
    s = gid.strip()
    if not s.startswith("gid://shopify/StaffMember/"):
        return False
    rest = s.removeprefix("gid://shopify/StaffMember/").strip()
    return bool(rest) and not rest.isspace()


def _staff_member_email_by_gid(
    shop_domain: str,
    access_token: str,
    staff_member_gid: str,
) -> tuple[str | None, str | None]:
    """Devuelve (email_en_minúsculas, error_mensaje)."""
    body = _graphql(
        shop_domain, access_token, _STAFF_MEMBER_BY_ID, {"id": staff_member_gid.strip()}
    )
    gql_err = _first_graphql_error_message(body)
    if gql_err:
        return None, gql_err
    data = body.get("data") or {}
    sm = data.get("staffMember")
    if sm is None:
        return None, "staffMember devolvió null (GID inválido, sin acceso o read_users faltante)"
    if not isinstance(sm, dict):
        return None, "Respuesta inesperada de staffMember"
    em = sm.get("email")
    if not isinstance(em, str) or "@" not in em:
        return None, "El StaffMember no devolvió un email válido"
    return em.strip().lower(), None


def associate_shopify_staff_by_gid(
    *,
    user_id: uuid_mod.UUID,
    user_email: str,
    role: str,
    staff_member_gid: str,
    skip_email_verification: bool = False,
) -> dict[str, Any]:
    """
    Asocia manualmente un StaffMember **ya creado en Shopify** (copiando su GID
    desde el Admin) con el usuario del panel. Por defecto verifica vía
    `staffMember(id:)` que el email coincida.
    """
    if role not in _ROLES_WITH_SHOPIFY_STAFF:
        _persist_user_link(user_id, staff_gid=None, status=LINK_STATUS_SKIPPED_ROLE)
        return {
            "shopify_staff_member_id": None,
            "shopify_staff_link_status": LINK_STATUS_SKIPPED_ROLE,
            "shopify_staff_link_message": MSG_SKIPPED_ROLE,
        }

    raw = staff_member_gid.strip()
    if not is_valid_shopify_staff_member_gid(raw):
        raise ValueError(
            "shopify_staff_member_gid debe ser un GID con formato "
            "gid://shopify/StaffMember/<id> (cópialo de Shopify Admin → Usuarios)."
        )

    uemail = user_email.strip().lower()

    with get_session() as session:
        resolved = _resolve_shop_token(session)

    if not resolved:
        if not skip_email_verification:
            raise ValueError(
                "No hay instalación con token de Shopify; no se puede verificar el GID. "
                "Conecta la app en la tienda o indica skip_email_verification: true "
                "solo si aceptas asociar sin comprobar el email (riesgo de ligar a otro staff)."
            )
        _persist_user_link(user_id, staff_gid=raw, status=LINK_STATUS_LINKED)
        return {
            "shopify_staff_member_id": raw,
            "shopify_staff_link_status": LINK_STATUS_LINKED,
            "shopify_staff_link_message": MSG_ASSOCIATED_UNVERIFIED,
        }

    shop_domain, token = resolved

    if skip_email_verification:
        _persist_user_link(user_id, staff_gid=raw, status=LINK_STATUS_LINKED)
        return _with_admin_url(
            shop_domain,
            {
                "shopify_staff_member_id": raw,
                "shopify_staff_link_status": LINK_STATUS_LINKED,
                "shopify_staff_link_message": MSG_ASSOCIATED_UNVERIFIED,
            },
        )

    try:
        em_shop, v_err = _staff_member_email_by_gid(shop_domain, token, raw)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        logger.exception("associate_shopify_staff: error al consultar staffMember", extra={})
        _persist_user_link(user_id, staff_gid=None, status=LINK_STATUS_ERROR)
        return {
            "shopify_staff_member_id": None,
            "shopify_staff_link_status": LINK_STATUS_ERROR,
            "shopify_staff_link_error": str(e),
            "shopify_staff_link_message": "Error al verificar el GID con la API de Shopify",
        }

    if v_err or not em_shop:
        _persist_user_link(user_id, staff_gid=None, status=LINK_STATUS_ERROR)
        return _with_admin_url(
            shop_domain,
            {
                "shopify_staff_member_id": None,
                "shopify_staff_link_status": LINK_STATUS_ERROR,
                "shopify_staff_link_error": v_err or "email desconocido",
                "shopify_staff_link_message": (
                    "No se pudo leer el StaffMember en Shopify. Revisá GID, scope read_users y el mensaje en shopify_staff_link_error."
                ),
            },
        )

    if em_shop != uemail:
        raise ValueError(
            f"El email del StaffMember en Shopify ({em_shop}) no coincide con el "
            f"email del usuario en el panel ({uemail}). Corregí el usuario, el staff en "
            f"Shopify, o usá skip_email_verification: true con cuidado."
        )

    _persist_user_link(user_id, staff_gid=raw, status=LINK_STATUS_LINKED)
    return _with_admin_url(
        shop_domain,
        {
            "shopify_staff_member_id": raw,
            "shopify_staff_link_status": LINK_STATUS_LINKED,
            "shopify_staff_link_message": MSG_ASSOCIATED_VERIFIED,
        },
    )


def try_link_shopify_staff_for_user(
    *,
    user_id: uuid_mod.UUID,
    email: str,
    role: str,
    given_name: str = "",
    family_name: str = "",
) -> dict[str, Any]:
    """Busca un StaffMember con el mismo email; opcionalmente pide al provisionador GID.

    No lanza por fallos de red/API: devuelve `shopify_staff_link_status=ERROR` y log.
    """
    if role not in _ROLES_WITH_SHOPIFY_STAFF:
        _persist_user_link(user_id, staff_gid=None, status=LINK_STATUS_SKIPPED_ROLE)
        return {
            "shopify_staff_member_id": None,
            "shopify_staff_link_status": LINK_STATUS_SKIPPED_ROLE,
            "shopify_staff_link_message": MSG_SKIPPED_ROLE,
        }

    try:
        with get_session() as session:
            resolved = _resolve_shop_token(session)
        if not resolved:
            _persist_user_link(user_id, staff_gid=None, status=LINK_STATUS_SKIPPED_NO_SHOP)
            return {
                "shopify_staff_member_id": None,
                "shopify_staff_link_status": LINK_STATUS_SKIPPED_NO_SHOP,
                "shopify_staff_link_message": MSG_SKIPPED_NO_SHOP,
            }
        shop_domain, token = resolved
        q = _email_search_query(email)
        body = _graphql(
            shop_domain,
            token,
            _STAFF_BY_EMAIL,
            {"query": q},
        )
        gid, graph_err = _pick_staff_gid(email, body)
        if graph_err:
            _persist_user_link(user_id, staff_gid=None, status=LINK_STATUS_ERROR)
            return _with_admin_url(
                shop_domain,
                {
                    "shopify_staff_member_id": None,
                    "shopify_staff_link_status": LINK_STATUS_ERROR,
                    "shopify_staff_link_error": graph_err,
                    "shopify_staff_link_message": (
                        "Shopify rechazó o no expuso staffMembers. "
                        "Comprueba scope read_users, plan Plus/Advanced y el mensaje en shopify_staff_link_error."
                    ),
                },
            )
        if gid:
            _persist_user_link(user_id, staff_gid=gid, status=LINK_STATUS_LINKED)
            return _with_admin_url(
                shop_domain,
                {
                    "shopify_staff_member_id": gid,
                    "shopify_staff_link_status": LINK_STATUS_LINKED,
                    "shopify_staff_link_message": MSG_LINKED,
                },
            )
        # No hay staff aún: intento opcional a servicio de aprovisionamiento (misma invitación vía n8n/Plus/etc.)
        prov_gid, prov_err = _try_external_staff_provisioner(
            shop_domain,
            user_id,
            email,
            given_name,
            family_name,
        )
        if prov_gid:
            _persist_user_link(user_id, staff_gid=prov_gid, status=LINK_STATUS_LINKED)
            return _with_admin_url(
                shop_domain,
                {
                    "shopify_staff_member_id": prov_gid,
                    "shopify_staff_link_status": LINK_STATUS_LINKED,
                    "shopify_staff_link_message": (
                        "Vínculo guardado: GID de Staff recibido del aprovisionador HTTP "
                        "(SHOPIFY_STAFF_PROVISIONER_URL)."
                    ),
                },
            )
        _persist_user_link(user_id, staff_gid=None, status=LINK_STATUS_NOT_FOUND)
        if (os.environ.get("SHOPIFY_STAFF_PROVISIONER_URL") or "").strip() and prov_err:
            return _with_admin_url(
                shop_domain,
                {
                    "shopify_staff_member_id": None,
                    "shopify_staff_link_status": LINK_STATUS_NOT_FOUND,
                    "shopify_staff_link_error": prov_err,
                    "shopify_staff_link_message": MSG_PROVISIONER_FAILED,
                },
            )
        return _with_admin_url(
            shop_domain,
            {
                "shopify_staff_member_id": None,
                "shopify_staff_link_status": LINK_STATUS_NOT_FOUND,
                "shopify_staff_link_message": MSG_NOT_FOUND,
            },
        )
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        logger.exception(
            "Error vinculando staff Shopify",
            extra={"user_id": str(user_id), "email": email},
        )
        _persist_user_link(user_id, staff_gid=None, status=LINK_STATUS_ERROR)
        return {
            "shopify_staff_member_id": None,
            "shopify_staff_link_status": LINK_STATUS_ERROR,
            "shopify_staff_link_error": str(e),
            "shopify_staff_link_message": f"Error de red o al parsear la respuesta: {e!s}",
        }

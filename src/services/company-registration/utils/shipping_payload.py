"""Normalización de dirección de envío para solicitudes de registro (Shopify B2B).

Acepta nombres alternativos y el objeto anidado `shipping_address` para alinear
formularios distintos (theme extension, admin) con las claves canónicas `shipping_*`.
"""
from __future__ import annotations

from typing import Any


def _strip_str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        s = str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)
        return s if s else None
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def merge_body_shipping_into_payload(
    payload: dict[str, Any], body: dict[str, Any]
) -> dict[str, Any]:
    """Combina el body del POST con el payload guardado y normaliza claves `shipping_*`."""
    extra: dict[str, Any] = {}
    for k, v in body.items():
        if k.startswith("shipping") or k == "shipping_address":
            extra[k] = v
    for k in (
        "address1",
        "address2",
        "city",
        "postal_code",
        "zip",
        "country_code",
        "country",
        "region",
        "province",
        "state",
    ):
        if k in body and body[k] not in (None, ""):
            extra[k] = body[k]
    return canonical_shipping_payload({**payload, **extra})


def canonical_shipping_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Devuelve copia de `raw` con claves `shipping_*` rellenadas desde alias y `shipping_address`.

    No elimina claves extra; solo rellena vacíos cuando hay equivalente en otro nombre.
    """
    out: dict[str, Any] = dict(raw)

    nested = raw.get("shipping_address")
    nested_d: dict[str, Any] = nested if isinstance(nested, dict) else {}

    def fill_canonical(field: str, nested_keys: tuple[str, ...], top_keys: tuple[str, ...]) -> None:
        if _strip_str(out.get(field)):
            return
        for key in nested_keys:
            if key in nested_d:
                s = _strip_str(nested_d.get(key))
                if s:
                    out[field] = s
                    return
        for key in top_keys:
            s = _strip_str(out.get(key))
            if s:
                out[field] = s
                return

    fill_canonical(
        "shipping_address1",
        ("address1", "line1", "street"),
        ("shipping_address_line1", "address_line1", "address1", "line1", "street"),
    )
    fill_canonical(
        "shipping_address2",
        ("address2", "line2"),
        ("shipping_address_line2", "address_line2", "address2", "line2"),
    )
    fill_canonical(
        "shipping_city",
        ("city", "municipality", "comuna"),
        ("city",),
    )
    fill_canonical(
        "shipping_zip",
        ("zip", "postal_code", "postcode", "zip_code"),
        ("postal_code", "zip", "zip_code", "postcode"),
    )
    fill_canonical(
        "shipping_country_code",
        ("country_code", "country"),
        ("country_code", "country"),
    )
    fill_canonical(
        "shipping_zone_code",
        ("zone_code", "region", "province", "state"),
        ("shipping_region", "shipping_province", "region", "province", "state", "zone_code"),
    )
    fill_canonical(
        "shipping_first_name",
        ("first_name", "firstName"),
        ("first_name", "firstName"),
    )
    fill_canonical(
        "shipping_last_name",
        ("last_name", "lastName"),
        ("last_name", "lastName"),
    )

    cc = _strip_str(out.get("shipping_country_code"))
    if cc:
        out["shipping_country_code"] = cc.upper()

    return out


def shipping_for_shopify_b2b(
    payload: dict[str, Any],
    *,
    first_name: str | None,
    last_name: str | None,
) -> dict[str, Any]:
    """Construye el dict interno para `CompanyLocationInput` o lanza `ValueError` claro.

    `shipping_zone_code` en Shopify es el código de **región o estado** (p. ej. Chile: RM, VIII;
    EE. UU.: CA). Es opcional si la tienda no lo requiere.
    """
    merged = canonical_shipping_payload(payload)

    a1 = (merged.get("shipping_address1") or "").strip()
    city = (merged.get("shipping_city") or "").strip()
    zip_code = (merged.get("shipping_zip") or "").strip()
    country = (merged.get("shipping_country_code") or "CL").strip().upper()

    missing_labels: list[str] = []
    if not a1:
        missing_labels.append(
            "calle y número (shipping_address1 o shipping_address.address1)"
        )
    if not city:
        missing_labels.append("ciudad o comuna (shipping_city o shipping_address.city)")
    if not zip_code:
        missing_labels.append(
            "código postal (shipping_zip o shipping_address.postal_code)"
        )

    if missing_labels:
        zone_help = (
            "shipping_zone_code es opcional: código de región o estado para Shopify "
            "(Chile: ej. RM, VIII; no es la comuna)."
        )
        raise ValueError(
            "No se puede aprobar: faltan datos de dirección para crear la empresa B2B en Shopify. "
            f"Falta: {'. '.join(missing_labels)}. "
            f"{zone_help} "
            "Si la solicitud se creó sin formulario de dirección, el cliente debe enviar de nuevo "
            "el registro con dirección completa."
        )

    out: dict[str, Any] = {
        "address1": a1,
        "city": city,
        "zip": zip_code,
        "country_code": country,
    }
    a2 = (merged.get("shipping_address2") or "").strip()
    if a2:
        out["address2"] = a2
    zc = (merged.get("shipping_zone_code") or "").strip()
    if zc:
        out["zone_code"] = zc
    sf = (merged.get("shipping_first_name") or "").strip()
    sl = (merged.get("shipping_last_name") or "").strip()
    out["first_name"] = sf or first_name
    out["last_name"] = sl or last_name
    return out

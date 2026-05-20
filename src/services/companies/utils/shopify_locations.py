"""Operaciones Shopify B2B: crear/actualizar/borrar CompanyLocation.

Usa el cliente GraphQL local del servicio (``shopify_graphql.graphql_call``).
Las mutations fueron validadas con ``validate_graphql_codeblocks`` contra el
schema Admin API.
"""
from __future__ import annotations

from typing import Any

from utils.shopify_graphql import format_user_errors, graphql_call


_MUTATION_COMPANY_LOCATION_CREATE = """
mutation CompanyLocationCreate($companyId: ID!, $input: CompanyLocationInput!) {
  companyLocationCreate(companyId: $companyId, input: $input) {
    companyLocation { id name }
    userErrors { field message code }
  }
}
"""

_MUTATION_COMPANY_LOCATION_UPDATE = """
mutation CompanyLocationUpdate($companyLocationId: ID!, $input: CompanyLocationUpdateInput!) {
  companyLocationUpdate(companyLocationId: $companyLocationId, input: $input) {
    companyLocation { id name phone }
    userErrors { field message code }
  }
}
"""

_MUTATION_COMPANY_LOCATION_ASSIGN_ADDRESS = """
mutation CompanyLocationAssignAddress(
  $locationId: ID!,
  $address: CompanyAddressInput!,
  $addressTypes: [CompanyAddressType!]!
) {
  companyLocationAssignAddress(
    locationId: $locationId,
    address: $address,
    addressTypes: $addressTypes
  ) {
    addresses { id address1 city zip countryCode }
    userErrors { field message code }
  }
}
"""

_MUTATION_COMPANY_LOCATION_DELETE = """
mutation CompanyLocationDelete($companyLocationId: ID!) {
  companyLocationDelete(companyLocationId: $companyLocationId) {
    deletedCompanyLocationId
    userErrors { field message code }
  }
}
"""


def to_company_gid(company_id: str) -> str:
    s = str(company_id).strip()
    if s.startswith("gid://shopify/Company/"):
        return s
    if s.isdigit():
        return f"gid://shopify/Company/{s}"
    raise ValueError(f"company id Shopify inválido: {company_id!r}")


def _build_address(
    *,
    address1: str,
    city: str,
    country_code: str,
    address2: str | None,
    zone_code: str | None,
    zip_code: str | None,
    phone: str | None,
    first_name: str | None,
    last_name: str | None,
) -> dict[str, Any]:
    addr: dict[str, Any] = {
        "firstName": (first_name or "").strip() or "—",
        "lastName": (last_name or "").strip() or "—",
        "address1": address1.strip(),
        "city": city.strip(),
        "countryCode": country_code.strip().upper(),
    }
    if address2 and address2.strip():
        addr["address2"] = address2.strip()
    if zone_code and zone_code.strip():
        addr["zoneCode"] = zone_code.strip()
    if zip_code and str(zip_code).strip():
        addr["zip"] = str(zip_code).strip()
    if phone and phone.strip():
        addr["phone"] = phone.strip()
    return addr


def create_company_location(
    shop_domain: str,
    access_token: str,
    *,
    shopify_company_id: str,
    name: str,
    address1: str,
    city: str,
    country_code: str = "CL",
    address2: str | None = None,
    zone_code: str | None = None,
    zip_code: str | None = None,
    phone: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> str:
    """Crea una CompanyLocation en Shopify B2B y devuelve su GID.

    Lanza ``ValueError`` con detalle de userErrors si Shopify rechaza la mutation.
    """
    address = _build_address(
        address1=address1,
        city=city,
        country_code=country_code,
        address2=address2,
        zone_code=zone_code,
        zip_code=zip_code,
        phone=phone,
        first_name=first_name,
        last_name=last_name,
    )
    location_input: dict[str, Any] = {
        "name": name.strip(),
        "shippingAddress": address,
        "billingSameAsShipping": True,
    }
    if phone and phone.strip():
        location_input["phone"] = phone.strip()

    data = graphql_call(
        shop_domain,
        access_token,
        _MUTATION_COMPANY_LOCATION_CREATE,
        {"companyId": to_company_gid(shopify_company_id), "input": location_input},
    )
    payload = data.get("companyLocationCreate") or {}
    errs = payload.get("userErrors") or []
    loc = payload.get("companyLocation") or {}
    gid = loc.get("id")
    if errs or not gid:
        raise ValueError(
            f"companyLocationCreate userErrors: {format_user_errors(errs)}"
        )
    return str(gid)


def update_company_location(
    shop_domain: str,
    access_token: str,
    *,
    location_gid: str,
    name: str | None = None,
    phone: str | None = None,
    address: dict[str, Any] | None = None,
) -> None:
    """Actualiza nombre/teléfono y/o dirección (shipping+billing) de una location.

    ``address`` debe ser el dict ya construido con :func:`_build_address`.
    Lanza ``ValueError`` si Shopify devuelve userErrors.
    """
    update_input: dict[str, Any] = {}
    if name is not None:
        nm = name.strip()
        if nm:
            update_input["name"] = nm
    if phone is not None:
        ph = phone.strip()
        update_input["phone"] = ph if ph else None

    if update_input:
        data = graphql_call(
            shop_domain,
            access_token,
            _MUTATION_COMPANY_LOCATION_UPDATE,
            {"companyLocationId": location_gid, "input": update_input},
        )
        payload = data.get("companyLocationUpdate") or {}
        errs = payload.get("userErrors") or []
        if errs:
            raise ValueError(
                f"companyLocationUpdate userErrors: {format_user_errors(errs)}"
            )

    if address is not None:
        data = graphql_call(
            shop_domain,
            access_token,
            _MUTATION_COMPANY_LOCATION_ASSIGN_ADDRESS,
            {
                "locationId": location_gid,
                "address": address,
                "addressTypes": ["SHIPPING", "BILLING"],
            },
        )
        payload = data.get("companyLocationAssignAddress") or {}
        errs = payload.get("userErrors") or []
        if errs:
            raise ValueError(
                f"companyLocationAssignAddress userErrors: {format_user_errors(errs)}"
            )


def delete_company_location(
    shop_domain: str,
    access_token: str,
    location_gid: str,
) -> None:
    """Elimina una CompanyLocation en Shopify B2B.

    Lanza ``ValueError`` si Shopify devuelve userErrors.
    """
    data = graphql_call(
        shop_domain,
        access_token,
        _MUTATION_COMPANY_LOCATION_DELETE,
        {"companyLocationId": location_gid},
    )
    payload = data.get("companyLocationDelete") or {}
    errs = payload.get("userErrors") or []
    if errs:
        raise ValueError(
            f"companyLocationDelete userErrors: {format_user_errors(errs)}"
        )


# Re-export para que address_service pueda construir addresses sin duplicar lógica.
build_address = _build_address

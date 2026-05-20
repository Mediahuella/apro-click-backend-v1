"""Operaciones Shopify B2B: Customer + CompanyContact.

- ``find_customer_by_email`` / ``create_customer`` / ``update_customer``: Customer
  como entidad transversal (no por empresa).
- ``assign_customer_as_contact`` / ``remove_company_contact`` / ``set_main_contact``:
  vínculo del Customer con la Company B2B.
- ``get_company_contacts``: lectura del estado actual de contactos de la company
  (para saber qué CompanyContact GID corresponde a un customer_numeric_id).

Las mutations fueron validadas contra el schema Admin API.
"""
from __future__ import annotations

import re
from typing import Any

from utils.shopify_graphql import format_user_errors, graphql_call


_GID_CUSTOMER = re.compile(r"^gid://shopify/Customer/(\d+)$")
_GID_COMPANY = re.compile(r"^gid://shopify/Company/(\d+)$")


def to_customer_gid(customer_id: str) -> str:
    s = str(customer_id).strip()
    if s.startswith("gid://shopify/Customer/"):
        return s
    if s.isdigit():
        return f"gid://shopify/Customer/{s}"
    raise ValueError(f"customer id Shopify inválido: {customer_id!r}")


def to_company_gid(company_id: str) -> str:
    s = str(company_id).strip()
    if s.startswith("gid://shopify/Company/"):
        return s
    if s.isdigit():
        return f"gid://shopify/Company/{s}"
    raise ValueError(f"company id Shopify inválido: {company_id!r}")


def customer_gid_to_numeric(gid: str) -> str:
    m = _GID_CUSTOMER.match(gid.strip())
    if m:
        return m.group(1)
    if gid.strip().isdigit():
        return gid.strip()
    raise ValueError(f"customer GID inesperado: {gid!r}")


def _email_query(email: str) -> str:
    esc = email.replace("\\", "\\\\").replace('"', '\\"')
    return f'email:"{esc}"'


_QUERY_FIND_CUSTOMER = """
query FindCustomer($first: Int!, $query: String!) {
  customers(first: $first, query: $query) {
    edges {
      node {
        id
        defaultEmailAddress { emailAddress }
      }
    }
  }
}
"""


def find_customer_numeric_by_email(
    shop_domain: str, access_token: str, email: str
) -> str | None:
    if not email:
        return None
    data = graphql_call(
        shop_domain,
        access_token,
        _QUERY_FIND_CUSTOMER,
        {"first": 5, "query": _email_query(email.strip())},
    )
    edges = (data.get("customers") or {}).get("edges") or []
    if not edges:
        return None
    gid = (edges[0].get("node") or {}).get("id")
    return customer_gid_to_numeric(gid) if isinstance(gid, str) else None


_MUTATION_CUSTOMER_CREATE = """
mutation CustomerCreate($input: CustomerInput!) {
  customerCreate(input: $input) {
    customer { id }
    userErrors { field message }
  }
}
"""


def create_customer(
    shop_domain: str,
    access_token: str,
    *,
    email: str,
    first_name: str | None,
    last_name: str | None,
    phone: str | None,
) -> str:
    """Crea un Customer Shopify. Si ya existe (email duplicado), lo busca y devuelve el id numérico."""
    payload: dict[str, Any] = {"email": email.strip()}
    fn = (first_name or "").strip()
    ln = (last_name or "").strip()
    ph = (phone or "").strip()
    if fn:
        payload["firstName"] = fn
    if ln:
        payload["lastName"] = ln
    if ph:
        payload["phone"] = ph

    data = graphql_call(
        shop_domain,
        access_token,
        _MUTATION_CUSTOMER_CREATE,
        {"input": payload},
    )
    cc = data.get("customerCreate") or {}
    user_errors = cc.get("userErrors") or []
    cust = cc.get("customer") or {}
    cust_gid = cust.get("id")

    if cust_gid:
        return customer_gid_to_numeric(str(cust_gid))

    existing = find_customer_numeric_by_email(shop_domain, access_token, email)
    if existing:
        return existing

    raise ValueError(
        f"customerCreate userErrors: {format_user_errors(user_errors)}"
    )


_MUTATION_CUSTOMER_UPDATE = """
mutation CustomerUpdate($input: CustomerInput!) {
  customerUpdate(input: $input) {
    customer {
      id
      firstName
      lastName
      defaultPhoneNumber { phoneNumber }
      defaultEmailAddress { emailAddress }
    }
    userErrors { field message }
  }
}
"""


def update_customer(
    shop_domain: str,
    access_token: str,
    *,
    customer_id: str,
    first_name: str | None = None,
    last_name: str | None = None,
    phone: str | None = None,
) -> None:
    """Actualiza un Customer en Shopify. Solo se envían los campos provistos."""
    input_payload: dict[str, Any] = {"id": to_customer_gid(customer_id)}
    if first_name is not None:
        input_payload["firstName"] = first_name.strip()
    if last_name is not None:
        input_payload["lastName"] = last_name.strip()
    if phone is not None:
        ph = phone.strip()
        input_payload["phone"] = ph if ph else None

    if len(input_payload) == 1:
        # Solo id, nada que actualizar.
        return

    data = graphql_call(
        shop_domain,
        access_token,
        _MUTATION_CUSTOMER_UPDATE,
        {"input": input_payload},
    )
    payload = data.get("customerUpdate") or {}
    errs = payload.get("userErrors") or []
    if errs:
        raise ValueError(f"customerUpdate userErrors: {format_user_errors(errs)}")


_MUTATION_ASSIGN_CONTACT = """
mutation CompanyAssignCustomerAsContact($companyId: ID!, $customerId: ID!) {
  companyAssignCustomerAsContact(companyId: $companyId, customerId: $customerId) {
    companyContact {
      id
      customer { id }
    }
    userErrors { field message code }
  }
}
"""


def assign_customer_as_contact(
    shop_domain: str,
    access_token: str,
    *,
    company_id: str,
    customer_id: str,
) -> str:
    """Asigna un Customer como CompanyContact y devuelve el GID del contacto."""
    data = graphql_call(
        shop_domain,
        access_token,
        _MUTATION_ASSIGN_CONTACT,
        {
            "companyId": to_company_gid(company_id),
            "customerId": to_customer_gid(customer_id),
        },
    )
    payload = data.get("companyAssignCustomerAsContact") or {}
    errs = payload.get("userErrors") or []
    contact = payload.get("companyContact") or {}
    contact_gid = contact.get("id")
    if errs or not contact_gid:
        raise ValueError(
            f"companyAssignCustomerAsContact userErrors: {format_user_errors(errs)}"
        )
    return str(contact_gid)


_MUTATION_ASSIGN_MAIN_CONTACT = """
mutation CompanyAssignMainContact($companyId: ID!, $companyContactId: ID!) {
  companyAssignMainContact(companyId: $companyId, companyContactId: $companyContactId) {
    company {
      id
      mainContact { id customer { id } }
    }
    userErrors { field message code }
  }
}
"""


def set_main_contact(
    shop_domain: str,
    access_token: str,
    *,
    company_id: str,
    contact_gid: str,
) -> None:
    data = graphql_call(
        shop_domain,
        access_token,
        _MUTATION_ASSIGN_MAIN_CONTACT,
        {
            "companyId": to_company_gid(company_id),
            "companyContactId": contact_gid,
        },
    )
    payload = data.get("companyAssignMainContact") or {}
    errs = payload.get("userErrors") or []
    if errs:
        raise ValueError(
            f"companyAssignMainContact userErrors: {format_user_errors(errs)}"
        )


_MUTATION_REMOVE_CONTACT = """
mutation CompanyContactRemoveFromCompany($companyContactId: ID!) {
  companyContactRemoveFromCompany(companyContactId: $companyContactId) {
    removedCompanyContactId
    userErrors { field message code }
  }
}
"""


def remove_company_contact(
    shop_domain: str, access_token: str, contact_gid: str
) -> None:
    """Revoca un CompanyContact (no borra el Customer subyacente)."""
    data = graphql_call(
        shop_domain,
        access_token,
        _MUTATION_REMOVE_CONTACT,
        {"companyContactId": contact_gid},
    )
    payload = data.get("companyContactRemoveFromCompany") or {}
    errs = payload.get("userErrors") or []
    if errs:
        raise ValueError(
            f"companyContactRemoveFromCompany userErrors: {format_user_errors(errs)}"
        )


_QUERY_COMPANY_CONTACT_ROLES = """
query CompanyContactRoles($id: ID!) {
  company(id: $id) {
    id
    contactRoles(first: 10) {
      nodes { id name }
    }
  }
}
"""


def get_ordering_only_role_id(
    shop_domain: str, access_token: str, company_id: str
) -> str:
    """Devuelve el GID del rol 'Ordering Only' del company. Si no existe, lanza ValueError.

    Shopify expone roles por empresa via ``company.contactRoles``; los nombres
    estándar son ``Location Admin`` y ``Ordering Only``.
    """
    data = graphql_call(
        shop_domain,
        access_token,
        _QUERY_COMPANY_CONTACT_ROLES,
        {"id": to_company_gid(company_id)},
    )
    company = data.get("company") or {}
    roles = (company.get("contactRoles") or {}).get("nodes") or []
    # Match case-insensitive del nombre.
    for node in roles:
        if isinstance(node, dict):
            name = (node.get("name") or "").strip().lower()
            if name in {"ordering only", "ordering_only", "orderingonly"}:
                gid = node.get("id")
                if isinstance(gid, str):
                    return gid
    # Fallback: si solo hay un rol, úsalo; si no, fallar.
    if len(roles) == 1 and isinstance(roles[0], dict):
        gid = roles[0].get("id")
        if isinstance(gid, str):
            return gid
    raise ValueError(
        "No se encontró el rol 'Ordering Only' en la empresa Shopify"
    )


_QUERY_CONTACT_ROLE_ASSIGNMENTS = """
query CompanyContactRoleAssignments($id: ID!) {
  companyContact(id: $id) {
    id
    roleAssignments(first: 100) {
      nodes {
        id
        role { id name }
        companyLocation { id }
      }
    }
  }
}
"""


def get_contact_role_assignments(
    shop_domain: str, access_token: str, contact_gid: str
) -> list[dict[str, Any]]:
    """Devuelve la lista de asignaciones del contacto: ``[{assignment_gid, location_gid, role_name}, ...]``."""
    data = graphql_call(
        shop_domain,
        access_token,
        _QUERY_CONTACT_ROLE_ASSIGNMENTS,
        {"id": contact_gid},
    )
    contact = data.get("companyContact") or {}
    nodes = (contact.get("roleAssignments") or {}).get("nodes") or []
    out: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        loc = node.get("companyLocation") or {}
        role = node.get("role") or {}
        out.append(
            {
                "assignment_gid": node.get("id"),
                "location_gid": loc.get("id"),
                "role_gid": role.get("id"),
                "role_name": role.get("name"),
            }
        )
    return out


_MUTATION_CONTACT_ASSIGN_ROLE = """
mutation CompanyContactAssignRole(
  $companyContactId: ID!,
  $companyContactRoleId: ID!,
  $companyLocationId: ID!
) {
  companyContactAssignRole(
    companyContactId: $companyContactId,
    companyContactRoleId: $companyContactRoleId,
    companyLocationId: $companyLocationId
  ) {
    companyContactRoleAssignment {
      id
      role { id name }
      companyLocation { id }
    }
    userErrors { field message code }
  }
}
"""


def assign_contact_to_location(
    shop_domain: str,
    access_token: str,
    *,
    contact_gid: str,
    role_gid: str,
    location_gid: str,
) -> str:
    """Asigna un contacto a una sucursal con el rol indicado. Devuelve el GID de la asignación."""
    data = graphql_call(
        shop_domain,
        access_token,
        _MUTATION_CONTACT_ASSIGN_ROLE,
        {
            "companyContactId": contact_gid,
            "companyContactRoleId": role_gid,
            "companyLocationId": location_gid,
        },
    )
    payload = data.get("companyContactAssignRole") or {}
    errs = payload.get("userErrors") or []
    assignment = payload.get("companyContactRoleAssignment") or {}
    gid = assignment.get("id")
    if errs or not gid:
        raise ValueError(
            f"companyContactAssignRole userErrors: {format_user_errors(errs)}"
        )
    return str(gid)


_MUTATION_CONTACT_REVOKE_ROLE = """
mutation CompanyContactRevokeRole(
  $companyContactId: ID!,
  $companyContactRoleAssignmentId: ID!
) {
  companyContactRevokeRole(
    companyContactId: $companyContactId,
    companyContactRoleAssignmentId: $companyContactRoleAssignmentId
  ) {
    revokedCompanyContactRoleAssignmentId
    userErrors { field message code }
  }
}
"""


def revoke_contact_role(
    shop_domain: str,
    access_token: str,
    *,
    contact_gid: str,
    assignment_gid: str,
) -> None:
    data = graphql_call(
        shop_domain,
        access_token,
        _MUTATION_CONTACT_REVOKE_ROLE,
        {
            "companyContactId": contact_gid,
            "companyContactRoleAssignmentId": assignment_gid,
        },
    )
    payload = data.get("companyContactRevokeRole") or {}
    errs = payload.get("userErrors") or []
    if errs:
        raise ValueError(
            f"companyContactRevokeRole userErrors: {format_user_errors(errs)}"
        )


_QUERY_COMPANY_CONTACTS = """
query CompanyContacts($id: ID!) {
  company(id: $id) {
    id
    mainContact { id customer { id } }
    contacts(first: 100) {
      nodes {
        id
        customer {
          id
          firstName
          lastName
          defaultEmailAddress { emailAddress }
          defaultPhoneNumber { phoneNumber }
        }
      }
    }
  }
}
"""


def get_company_contacts(
    shop_domain: str, access_token: str, company_id: str
) -> dict[str, Any]:
    """Devuelve ``{main_contact_gid, contacts: [{contact_gid, customer_id, email, ...}]}``."""
    data = graphql_call(
        shop_domain,
        access_token,
        _QUERY_COMPANY_CONTACTS,
        {"id": to_company_gid(company_id)},
    )
    company = data.get("company") or {}
    main = company.get("mainContact") or {}
    main_gid = main.get("id") if isinstance(main, dict) else None

    out_contacts: list[dict[str, Any]] = []
    for node in (company.get("contacts") or {}).get("nodes") or []:
        if not isinstance(node, dict):
            continue
        customer = node.get("customer") or {}
        cust_gid = customer.get("id")
        out_contacts.append(
            {
                "contact_gid": node.get("id"),
                "customer_gid": cust_gid,
                "customer_numeric_id": (
                    customer_gid_to_numeric(str(cust_gid)) if cust_gid else None
                ),
                "first_name": customer.get("firstName"),
                "last_name": customer.get("lastName"),
                "email": (
                    (customer.get("defaultEmailAddress") or {}).get("emailAddress")
                ),
                "phone": (
                    (customer.get("defaultPhoneNumber") or {}).get("phoneNumber")
                ),
            }
        )

    return {
        "main_contact_gid": main_gid,
        "contacts": out_contacts,
    }

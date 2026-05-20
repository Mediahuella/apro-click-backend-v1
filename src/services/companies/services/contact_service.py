"""Service de contactos/usuarios de una empresa B2B.

Cada ``Client`` local se enlaza con un Customer en Shopify (``shopify_customer_id``)
y, mientras esté asignado a la company, con un CompanyContact GID que se consulta
en Shopify on-demand (no se persiste para evitar drift).

Operaciones disponibles:
  - list_contacts(company_id): listado de Clients + estado mainContact desde Shopify
  - create_contact(company_id, payload): crea/reusa Customer + asigna como CompanyContact
  - update_contact(client_id, payload): customerUpdate (firstName/lastName/phone)
  - delete_contact(client_id): revoca CompanyContact en Shopify y borra el Client local
  - set_main_contact(company_id, client_id): companyAssignMainContact
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

from aws_lambda_powertools import Logger
from sqlalchemy import select

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

from database.engine import get_session
from database.models.client import Client
from database.models.company import Company
from database.models.company_address import CompanyAddress

from utils.shopify_catalog_sync import _resolve_shop_and_token
from utils.shopify_contacts import (
    assign_contact_to_location,
    assign_customer_as_contact,
    create_customer,
    find_customer_numeric_by_email,
    get_company_contacts,
    get_contact_role_assignments,
    get_ordering_only_role_id,
    remove_company_contact,
    revoke_contact_role,
    set_main_contact,
    update_customer,
)

logger = Logger()


def _split_name(full: str | None) -> tuple[str | None, str | None]:
    if not full:
        return None, None
    parts = full.strip().split(maxsplit=1)
    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], parts[1]


class ContactService:
    # ------------------------------------------------------------------
    # Lecturas
    # ------------------------------------------------------------------
    def list_contacts(self, company_id: str) -> dict[str, Any]:
        self._validate_uuid(company_id)
        with get_session() as session:
            company = session.get(Company, uuid.UUID(company_id))
            if not company:
                raise ValueError(f"Company '{company_id}' not found")
            shopify_company_id = company.shopify_company_id
            stmt = (
                select(Client)
                .where(Client.company_id == uuid.UUID(company_id))
                .order_by(Client.created_at.asc())
            )
            rows = [c.to_dict() for c in session.scalars(stmt)]

        main_customer_numeric: str | None = None
        contact_gid_by_customer: dict[str, str] = {}
        if shopify_company_id:
            try:
                shop_domain, access_token = _resolve_shop_and_token()
                state = get_company_contacts(
                    shop_domain, access_token, shopify_company_id
                )
                for c in state["contacts"]:
                    if c.get("customer_numeric_id") and c.get("contact_gid"):
                        contact_gid_by_customer[str(c["customer_numeric_id"])] = (
                            str(c["contact_gid"])
                        )
                main_gid = state.get("main_contact_gid")
                if main_gid:
                    for c in state["contacts"]:
                        if c.get("contact_gid") == main_gid:
                            main_customer_numeric = c.get("customer_numeric_id")
                            break
            except Exception as e:
                logger.warning(
                    "list_contacts: no se pudo leer estado Shopify — %s", e
                )

        enriched: list[dict[str, Any]] = []
        for row in rows:
            cust = row.get("shopify_customer_id")
            row["shopify_contact_gid"] = (
                contact_gid_by_customer.get(str(cust)) if cust else None
            )
            row["is_main_contact"] = bool(
                cust and main_customer_numeric and str(cust) == str(main_customer_numeric)
            )
            enriched.append(row)
        return {"contacts": enriched}

    # ------------------------------------------------------------------
    # Mutaciones
    # ------------------------------------------------------------------
    def create_contact(
        self, company_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self._validate_uuid(company_id)
        data = self._validate_payload(payload, require_email=True)

        with get_session() as session:
            company = session.get(Company, uuid.UUID(company_id))
            if not company:
                raise ValueError(f"Company '{company_id}' not found")
            if not company.shopify_company_id:
                raise ValueError(
                    "La empresa no tiene shopify_company_id; no se puede asignar "
                    "el contacto en Shopify."
                )
            shopify_company_id = company.shopify_company_id
            existing = session.scalar(
                select(Client).where(
                    Client.company_id == uuid.UUID(company_id),
                    Client.email == data["email"],
                )
            )
            if existing:
                raise ValueError(
                    f"Ya existe un contacto con el correo '{data['email']}' "
                    "para esta empresa."
                )

        shop_domain, access_token = _resolve_shop_and_token()

        first_name = data.get("first_name")
        last_name = data.get("last_name")
        if not first_name and not last_name and data.get("name"):
            first_name, last_name = _split_name(data["name"])

        customer_numeric = find_customer_numeric_by_email(
            shop_domain, access_token, data["email"]
        )
        if customer_numeric is None:
            customer_numeric = create_customer(
                shop_domain,
                access_token,
                email=data["email"],
                first_name=first_name,
                last_name=last_name,
                phone=data.get("phone"),
            )
        else:
            try:
                update_customer(
                    shop_domain,
                    access_token,
                    customer_id=customer_numeric,
                    first_name=first_name,
                    last_name=last_name,
                    phone=data.get("phone"),
                )
            except Exception as e:
                logger.warning(
                    "create_contact: customerUpdate previo falló — %s", e
                )

        try:
            assign_customer_as_contact(
                shop_domain,
                access_token,
                company_id=shopify_company_id,
                customer_id=customer_numeric,
            )
        except ValueError as e:
            # Si el contacto ya estaba asignado a la empresa, dejamos pasar.
            msg = str(e).lower()
            if "already" not in msg and "exist" not in msg:
                raise

        full_name = " ".join(filter(None, [first_name, last_name])).strip() or None

        with get_session() as session:
            row = Client(
                company_id=uuid.UUID(company_id),
                shopify_customer_id=customer_numeric,
                email=data["email"],
                name=full_name,
                phone=data.get("phone"),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            result = row.to_dict()

        logger.info(
            "Contacto creado",
            extra={
                "client_id": result["id"],
                "company_id": company_id,
                "shopify_customer_id": customer_numeric,
            },
        )
        return result

    def update_contact(
        self, company_id: str, client_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self._validate_uuid(company_id)
        self._validate_uuid(client_id)
        data = self._validate_payload(payload, require_email=False)
        # Email no es editable.
        data.pop("email", None)

        with get_session() as session:
            row = session.get(Client, uuid.UUID(client_id))
            if not row or str(row.company_id) != company_id:
                raise ValueError(f"Contact '{client_id}' not found")
            shopify_customer_id = row.shopify_customer_id
            current = row.to_dict()

        if not any(k in data for k in ("name", "first_name", "last_name", "phone")):
            return current

        first_name = data.get("first_name")
        last_name = data.get("last_name")
        if "name" in data and first_name is None and last_name is None:
            first_name, last_name = _split_name(data["name"])

        if shopify_customer_id:
            shop_domain, access_token = _resolve_shop_and_token()
            update_customer(
                shop_domain,
                access_token,
                customer_id=shopify_customer_id,
                first_name=first_name if first_name is not None else None,
                last_name=last_name if last_name is not None else None,
                phone=data.get("phone") if "phone" in data else None,
            )

        full_name = (
            " ".join(filter(None, [first_name, last_name])).strip()
            if first_name or last_name
            else data.get("name")
        )

        with get_session() as session:
            row = session.get(Client, uuid.UUID(client_id))
            if not row:
                raise ValueError(f"Contact '{client_id}' not found")
            if full_name is not None:
                row.name = full_name
            if "phone" in data:
                row.phone = data.get("phone")
            session.commit()
            session.refresh(row)
            result = row.to_dict()

        logger.info(
            "Contacto actualizado",
            extra={"client_id": client_id, "company_id": company_id},
        )
        return result

    def delete_contact(self, company_id: str, client_id: str) -> bool:
        self._validate_uuid(company_id)
        self._validate_uuid(client_id)
        with get_session() as session:
            row = session.get(Client, uuid.UUID(client_id))
            if not row or str(row.company_id) != company_id:
                raise ValueError(f"Contact '{client_id}' not found")
            shopify_customer_id = row.shopify_customer_id

        # Localizar el CompanyContact GID asociado al customer en Shopify.
        with get_session() as session:
            company = session.get(Company, uuid.UUID(company_id))
            shopify_company_id = company.shopify_company_id if company else None

        if shopify_company_id and shopify_customer_id:
            shop_domain, access_token = _resolve_shop_and_token()
            state = get_company_contacts(
                shop_domain, access_token, shopify_company_id
            )
            contact_gid: str | None = None
            for c in state["contacts"]:
                if str(c.get("customer_numeric_id") or "") == str(
                    shopify_customer_id
                ):
                    contact_gid = c.get("contact_gid")
                    break
            if contact_gid:
                if state.get("main_contact_gid") == contact_gid:
                    raise ValueError(
                        "No se puede eliminar el contacto principal. "
                        "Marca otro como principal antes."
                    )
                remove_company_contact(shop_domain, access_token, contact_gid)

        with get_session() as session:
            row = session.get(Client, uuid.UUID(client_id))
            if row:
                session.delete(row)
                session.commit()

        logger.info(
            "Contacto eliminado",
            extra={"client_id": client_id, "company_id": company_id},
        )
        return True

    def set_main_contact(
        self, company_id: str, client_id: str
    ) -> dict[str, Any]:
        self._validate_uuid(company_id)
        self._validate_uuid(client_id)
        with get_session() as session:
            row = session.get(Client, uuid.UUID(client_id))
            if not row or str(row.company_id) != company_id:
                raise ValueError(f"Contact '{client_id}' not found")
            shopify_customer_id = row.shopify_customer_id
            company = session.get(Company, uuid.UUID(company_id))
            shopify_company_id = company.shopify_company_id if company else None

        if not shopify_customer_id or not shopify_company_id:
            raise ValueError(
                "El contacto o la empresa no están enlazados con Shopify."
            )

        shop_domain, access_token = _resolve_shop_and_token()
        state = get_company_contacts(
            shop_domain, access_token, shopify_company_id
        )
        contact_gid: str | None = None
        for c in state["contacts"]:
            if str(c.get("customer_numeric_id") or "") == str(shopify_customer_id):
                contact_gid = c.get("contact_gid")
                break
        if not contact_gid:
            raise ValueError(
                "Shopify no devolvió el CompanyContact para este cliente; "
                "probablemente no esté asignado a la empresa."
            )

        set_main_contact(
            shop_domain,
            access_token,
            company_id=shopify_company_id,
            contact_gid=contact_gid,
        )

        logger.info(
            "Main contact actualizado",
            extra={"client_id": client_id, "company_id": company_id},
        )
        return {"client_id": client_id, "is_main_contact": True}

    # ------------------------------------------------------------------
    # Asignaciones contacto ↔ sucursal (CompanyLocation)
    # ------------------------------------------------------------------
    def list_contact_locations(
        self, company_id: str, client_id: str
    ) -> dict[str, Any]:
        """Devuelve direcciones locales + estado de asignación del contacto en Shopify."""
        self._validate_uuid(company_id)
        self._validate_uuid(client_id)
        client, company, addresses = self._load_for_locations(
            company_id, client_id
        )

        addr_by_gid: dict[str, dict[str, Any]] = {}
        for a in addresses:
            if a.shopify_location_id:
                addr_by_gid[a.shopify_location_id] = {
                    "id": str(a.id),
                    "label": a.label,
                    "address1": a.address1,
                    "city": a.city,
                    "shopify_location_id": a.shopify_location_id,
                    "is_assigned": False,
                    "assignment_gid": None,
                    "role_name": None,
                }

        if client.shopify_customer_id and company.shopify_company_id:
            shop_domain, access_token = _resolve_shop_and_token()
            contact_gid = self._resolve_contact_gid(
                shop_domain,
                access_token,
                shopify_company_id=company.shopify_company_id,
                shopify_customer_id=client.shopify_customer_id,
            )
            if contact_gid:
                assignments = get_contact_role_assignments(
                    shop_domain, access_token, contact_gid
                )
                for asg in assignments:
                    gid = asg.get("location_gid")
                    if not gid:
                        continue
                    entry = addr_by_gid.get(str(gid))
                    if entry is None:
                        # Asignada en Shopify a una location que no existe localmente.
                        addr_by_gid[str(gid)] = {
                            "id": None,
                            "label": "(sin etiqueta local)",
                            "address1": None,
                            "city": None,
                            "shopify_location_id": gid,
                            "is_assigned": True,
                            "assignment_gid": asg.get("assignment_gid"),
                            "role_name": asg.get("role_name"),
                        }
                    else:
                        entry["is_assigned"] = True
                        entry["assignment_gid"] = asg.get("assignment_gid")
                        entry["role_name"] = asg.get("role_name")

        return {"locations": list(addr_by_gid.values())}

    def set_contact_locations(
        self,
        company_id: str,
        client_id: str,
        address_ids: list[str],
    ) -> dict[str, Any]:
        """Diff de asignaciones: agrega faltantes y revoca las quitadas.

        ``address_ids`` son IDs locales de ``company_addresses``; se traducen a
        ``shopify_location_id`` y se sincronizan con Shopify.
        """
        self._validate_uuid(company_id)
        self._validate_uuid(client_id)
        if not isinstance(address_ids, list):
            raise ValueError("'address_ids' debe ser un array de UUIDs")
        for aid in address_ids:
            self._validate_uuid(aid)

        client, company, addresses = self._load_for_locations(
            company_id, client_id
        )

        if not client.shopify_customer_id:
            raise ValueError(
                "El contacto no tiene shopify_customer_id; no se puede asignar."
            )
        if not company.shopify_company_id:
            raise ValueError(
                "La empresa no tiene shopify_company_id; no se puede asignar."
            )

        addr_by_id = {str(a.id): a for a in addresses}
        target_gids: set[str] = set()
        for aid in address_ids:
            addr = addr_by_id.get(aid)
            if not addr:
                raise ValueError(
                    f"La dirección '{aid}' no pertenece a esta empresa"
                )
            if not addr.shopify_location_id:
                raise ValueError(
                    f"La dirección '{addr.label}' aún no está sincronizada con "
                    "Shopify (sin shopify_location_id)."
                )
            target_gids.add(addr.shopify_location_id)

        shop_domain, access_token = _resolve_shop_and_token()
        contact_gid = self._resolve_contact_gid(
            shop_domain,
            access_token,
            shopify_company_id=company.shopify_company_id,
            shopify_customer_id=client.shopify_customer_id,
        )
        if not contact_gid:
            raise ValueError(
                "El contacto no está asignado a la empresa en Shopify."
            )

        current = get_contact_role_assignments(
            shop_domain, access_token, contact_gid
        )
        current_by_loc: dict[str, str] = {
            str(a["location_gid"]): str(a["assignment_gid"])
            for a in current
            if a.get("location_gid") and a.get("assignment_gid")
        }
        current_gids = set(current_by_loc.keys())

        to_add = target_gids - current_gids
        to_remove = current_gids - target_gids

        role_id: str | None = None
        if to_add:
            role_id = get_ordering_only_role_id(
                shop_domain, access_token, company.shopify_company_id
            )

        for location_gid in to_add:
            assign_contact_to_location(
                shop_domain,
                access_token,
                contact_gid=contact_gid,
                role_gid=role_id,  # type: ignore[arg-type]
                location_gid=location_gid,
            )

        for location_gid in to_remove:
            revoke_contact_role(
                shop_domain,
                access_token,
                contact_gid=contact_gid,
                assignment_gid=current_by_loc[location_gid],
            )

        logger.info(
            "Asignaciones contacto↔sucursal sincronizadas",
            extra={
                "client_id": client_id,
                "company_id": company_id,
                "added": len(to_add),
                "removed": len(to_remove),
            },
        )
        return self.list_contact_locations(company_id, client_id)

    # ------------------------------------------------------------------
    # Helpers privados de asignaciones
    # ------------------------------------------------------------------
    @staticmethod
    def _load_for_locations(
        company_id: str, client_id: str
    ) -> tuple[Client, Company, list[CompanyAddress]]:
        with get_session() as session:
            client = session.get(Client, uuid.UUID(client_id))
            if not client or str(client.company_id) != company_id:
                raise ValueError(f"Contact '{client_id}' not found")
            company = session.get(Company, uuid.UUID(company_id))
            if not company:
                raise ValueError(f"Company '{company_id}' not found")
            addresses = list(
                session.scalars(
                    select(CompanyAddress)
                    .where(CompanyAddress.company_id == uuid.UUID(company_id))
                    .order_by(CompanyAddress.created_at.asc())
                )
            )
            # Forzar carga antes de cerrar la sesión.
            session.expunge(client)
            session.expunge(company)
            for a in addresses:
                session.expunge(a)
        return client, company, addresses

    @staticmethod
    def _resolve_contact_gid(
        shop_domain: str,
        access_token: str,
        *,
        shopify_company_id: str,
        shopify_customer_id: str,
    ) -> str | None:
        state = get_company_contacts(
            shop_domain, access_token, shopify_company_id
        )
        for c in state["contacts"]:
            if str(c.get("customer_numeric_id") or "") == str(shopify_customer_id):
                return c.get("contact_gid")
        return None

    # ------------------------------------------------------------------
    # Validación
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_uuid(value: str) -> None:
        try:
            uuid.UUID(value)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid id (expected UUID): '{value}'") from e

    @staticmethod
    def _validate_payload(
        payload: dict[str, Any], *, require_email: bool
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Body must be a JSON object")

        allowed = {"name", "first_name", "last_name", "email", "phone"}
        clean: dict[str, Any] = {}
        for k, v in payload.items():
            if k not in allowed:
                continue
            if v is None:
                if k in {"phone", "first_name", "last_name", "name"}:
                    clean[k] = None
                continue
            s = str(v).strip()
            if s:
                clean[k] = s

        if "email" in clean:
            email = clean["email"].lower()
            if "@" not in email or len(email) < 5:
                raise ValueError("'email' inválido")
            clean["email"] = email

        if require_email and not clean.get("email"):
            raise ValueError("Campo obligatorio faltante: email")

        return clean

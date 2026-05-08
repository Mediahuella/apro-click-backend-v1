# Modelo de datos de dominio (v1)

**Estado:** documento de referencia para implementación en PostgreSQL (SQLAlchemy + Alembic).  
**Relacionado:** `docs/RDS_POSTGRESQL_ORM_IMPLEMENTATION_PLAN.md` (infra y proceso).

Este documento fija **reglas de negocio** y **tablas conceptuales** acordadas para el backend Apro Click Admin. Los nombres de columnas y tipos exactos se definirán en migraciones; aquí se describe el **qué** y el **porqué**.

---

## 1. Reglas de negocio globales

### 1.1 Usuarios del sistema (vendedores / staff)

- Son filas en **`users`**, autenticados con **Amazon Cognito**; el vínculo obligatorio es **`cognito_sub`** (único, estable).
- **No** es obligatorio que un vendedor pertenezca a una compañía del CRM: `company_id` puede ser **NULL**.
- Opción explícita: existe una compañía **plataforma** (p. ej. **Apro** / **Apro Click**) que representa al **dueño del sistema**; se usa cuando conviene anclar datos o permisos a “la plataforma” frente a empresas cliente.

### 1.2 Compañías (`companies`)

- Representan empresas **cliente** del CRM y, además, pueden incluir **una fila sistema** para Apro.
- **Campos de dominio ya definidos** (alineados con el API actual): `company_type` ∈ {`SMALL`, `MEDIUM`, `BIG`}, `payment_type` ∈ {`DIRECT`, `CREDIT`}.
- La compañía sistema debe distinguirse con un indicador (p. ej. **`is_system`** `BOOLEAN`, o un `slug` reservado `apro`) para no mezclarla con clientes B2B en informes o permisos.

### 1.3 Clientes externos (`clients`)

- Un **cliente** siempre está ligado a **una** compañía del CRM: **`company_id` NOT NULL**.
- Además debe identificarse al **cliente en Shopify** (misma tienda que usa la app): almacenar **`shopify_customer_id`** (o el identificador oficial que elija el equipo: GID como texto, o id numérico) según el contrato con la API Admin.
- **Unicidad recomendada:** dentro del mismo contexto de tienda (una sola instalación global; ver §3), evitar duplicados con `UNIQUE (company_id, shopify_customer_id)` o equivalente.

### 1.4 Registro de auditoría (`audit_logs`)

- Objetivo: **trazar acciones realizadas por usuarios del sistema** (staff internos).
- Solo se registran eventos con **`actor_user_id` NOT NULL** (FK a `users`). No se modelan aquí acciones puramente automáticas del sistema salvo que cambie el requisito.

### 1.5 Integración Shopify (OAuth / token)

- La **instalación OAuth y el token de acceso** corresponden a **una sola tienda principal** asociada a Apro Click (instalación de la app, recuperación del token).
- **No** se requiere una fila de conexión Shopify **por cada** `company` del CRM; la relación es **plataforma ↔ tienda principal**.
- El **token** no debe guardarse en texto plano en la base: referencia a **AWS Secrets Manager** (u otro almacén seguro) desde la tabla de configuración o desde variables de entorno gestionadas por la Lambda.

### 1.6 Solicitudes de formulario (nuevas empresas)

- Tabla de **cola / intención** (`company_registration_requests` o nombre final): datos del formulario antes de crear `companies` y usuarios definitivos; estados (`PENDING`, `APPROVED`, …) y vínculo opcional a la compañía creada.

### 1.7 Comunicación vendedor ↔ cliente

- Hilos (**`conversations`**) entre un **vendedor** (`users`) y un **`client`**.
- Mensajes en **`messages`** con `sender_type` ∈ {`USER`, `CLIENT`} y FKs mutuamente excluyentes al emisor según tipo.

---

## 2. Diagrama entidad-relación (resumen)

```text
companies (incluye fila is_system para Apro)
    ↑
    ├── users.company_id (opcional)
    ├── clients.company_id (obligatorio)
    └── company_registration_requests.resolved_company_id (opcional)

users
    ├── audit_logs.actor_user_id (NOT NULL)
    ├── conversations.seller_user_id
    └── messages (sender cuando sender_type = USER)

clients
    └── conversations.client_id

conversations
    └── messages

shopify_app_installation (singleton lógico: una fila / tienda principal)
    └── (no FK por company CRM; es configuración global)

company_registration_requests
```

---

## 3. Tablas conceptuales

### 3.1 `companies`

| Campo | Descripción |
|-------|-------------|
| `id` | UUID PK. |
| `name` | Nombre comercial. |
| `company_type` | SMALL \| MEDIUM \| BIG. |
| `payment_type` | DIRECT \| CREDIT. |
| `is_system` | `true` solo para la compañía plataforma (Apro). |
| `created_at`, `updated_at` | `timestamptz`. |

*Seed: crear la compañía Apro con `is_system = true` en migración o script de arranque.*

### 3.2 `users`

| Campo | Descripción |
|-------|-------------|
| `id` | UUID PK. |
| `cognito_sub` | `TEXT` UNIQUE NOT NULL. |
| `email` | Único (normalizado en aplicación). |
| `given_name`, `family_name` | Opcionales. |
| `role` | Coherente con grupos Cognito / API (p. ej. SUPERADMIN, SALES, …). |
| `status` | ACTIVE, DISABLED, PENDING, etc. |
| `company_id` | UUID FK → `companies`, **NULL** permitido (vendedor sin compañía cliente). |
| `shopify_staff_member_id` | `TEXT` NULL: GID GraphQL `gid://shopify/StaffMember/...` si se vinculó con la tienda. |
| `shopify_staff_link_status` | `TEXT` NULL: resultado del último intento (p. ej. LINKED, NOT_FOUND, SKIPPED_ROLE). |
| `codigo_sap` | `VARCHAR` NULL para roles distintos de `SALES`. Con rol SALES la API exige valor no vacío en alta y al pasar el usuario a SALES o al editar ese campo. |
| `created_at`, `updated_at` | `timestamptz`. |

### 3.3 `clients`

| Campo | Descripción |
|-------|-------------|
| `id` | UUID PK. |
| `company_id` | UUID FK NOT NULL → `companies`. |
| `shopify_customer_id` | Identificador del cliente en Shopify (formato acordado con integración). |
| `email`, `name`, `phone` | Opcionales según producto; email útil para búsqueda. |
| `created_at`, `updated_at` | `timestamptz`. |

**Índice único sugerido:** `(company_id, shopify_customer_id)`.

### 3.4 `audit_logs`

| Campo | Descripción |
|-------|-------------|
| `id` | PK (UUID o bigserial). |
| `entity_type` | Texto: `company`, `user`, `conversation`, … |
| `entity_id` | UUID o texto según entidad. |
| `action` | `CREATE`, `UPDATE`, `DELETE`, etc. |
| `actor_user_id` | UUID FK → `users` **NOT NULL**. |
| `metadata` | `JSONB` opcional (diff, payload resumido). |
| `created_at` | `timestamptz`. |

### 3.5 `company_registration_requests`

| Campo | Descripción |
|-------|-------------|
| `id` | UUID PK. |
| `status` | PENDING, APPROVED, REJECTED, … |
| `payload` | `JSONB` con datos del formulario. |
| `submitted_email` | Contacto principal. |
| `resolved_company_id` | FK → `companies` NULL hasta aprobación. |
| `resolved_by_user_id` | FK → `users` NULL. |
| `notes` | Texto interno. |
| `created_at`, `updated_at` | `timestamptz`. |

### 3.6 `conversations`

| Campo | Descripción |
|-------|-------------|
| `id` | UUID PK. |
| `company_id` | FK → `companies` (contexto; debe ser coherente con `clients.company_id`). |
| `seller_user_id` | FK → `users` (vendedor). |
| `client_id` | FK → `clients`. |
| `status` | OPEN, CLOSED, … |
| `last_message_at` | `timestamptz` opcional. |
| `closed_at` | `timestamptz` NULL; rellenado al pasar a `CLOSED` (PATCH staff), borrado al reabrir (`OPEN`). |
| `created_at`, `updated_at` | `timestamptz`. |

**Restricción de unicidad:** a lo sumo una fila `OPEN` por `(company_id, client_id)`; varias `CLOSED` permitidas (migración 012).

### 3.7 `messages`

| Campo | Descripción |
|-------|-------------|
| `id` | UUID PK. |
| `conversation_id` | FK → `conversations`. |
| `sender_type` | `USER` \| `CLIENT`. |
| `sender_user_id` | FK → `users`, NULL si `sender_type = CLIENT`. |
| `sender_client_id` | FK → `clients`, NULL si `sender_type = USER`. |
| `body` | `TEXT` o `JSONB`. |
| `created_at` | `timestamptz`. |

*Validación en aplicación o CHECK: coherencia entre `sender_type` y FKs de emisor.*

### 3.8 `shopify_app_installation` (configuración global)

Una fila (o restricción única global) para la **tienda principal** de Apro Click.

| Campo | Descripción |
|-------|-------------|
| `id` | UUID PK. |
| `shop_domain` | `*.myshopify.com`, UNIQUE. |
| `shopify_access_token` | Token Admin API (OAuth); columna `TEXT` (migración 002). |
| `access_token_secret_id` | Opcional / legado; puede quedar NULL si el token vive en columna. |
| `scopes` | Texto o JSON según lo que devuelva OAuth. |
| `installed_at` | `timestamptz`. |
| `uninstalled_at` | NULL si activa. |
| `created_at`, `updated_at` | `timestamptz`. |

**OAuth state / nonce** de corta duración puede vivir en tabla auxiliar `shopify_oauth_states` (`state`, `expires_at`) o en cache; documentar en la implementación del flujo OAuth.

---

## 4. Decisiones cerradas (para implementación)

| Tema | Decisión |
|------|----------|
| Vendedor vs `company` | `users.company_id` opcional; compañía **Apro** como `is_system` para representar plataforma. |
| Cliente | Siempre `company_id` + identidad Shopify (`shopify_customer_id`). |
| Auditoría | Solo usuarios con `actor_user_id` NOT NULL. |
| Shopify | Una instalación / tienda principal; access token en columna `shopify_access_token` (exponer solo en servidor). |

---

## 5. Preguntas abiertas menores (para migraciones)

1. Formato exacto de **`shopify_customer_id`**: GID string vs id numérico (Admin API).
2. Política de **borrado** de `users` con filas en `audit_logs` (RESTRICT vs soft-delete).
3. Necesidad de **`shopify_oauth_states`** como tabla persistente vs solo Redis/memoria.

---

## 6. Historial

| Versión | Fecha | Cambios |
|---------|-------|---------|
| 1.0 | 2026-04 | Modelo inicial v1: reglas de negocio y tablas conceptuales. |

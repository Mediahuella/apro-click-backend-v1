# Guía de desarrollo — Apro Click Admin Backend v1

Backend serverless en **AWS Lambda** con **Serverless Framework 4**, **Python 3.13**, **AWS Lambda Powertools**, **HTTP API** (API Gateway v2) y **WebSocket API** para chat, **PostgreSQL 17** (AWS RDS) y **SQLAlchemy 2.x** como ORM. Autenticación con **Amazon Cognito**. Integración con **Shopify Admin API** (OAuth, pedidos, inventario, listas de precio B2B, CarrierService). Persistencia adicional: **S3** (adjuntos chat, Excel de precios), **SQS** (workers asíncronos), **DynamoDB** (conexiones WebSocket).

## Requisitos

| Herramienta | Uso |
|-------------|-----|
| **Node.js** + npm | `serverless` / `npx sls`, scripts en `package.json` |
| **Python 3** (`python3`) | Scripts en `scripts/*.py` (multiplataforma; no depender de bash) |
| **AWS CLI / credenciales** | Perfil por defecto `mh-prod` en deploys (configurable) |
| **Terraform** | Infraestructura RDS PostgreSQL y S3 chat (`infra/`) |
| **Docker** | Requerido por `serverless-python-requirements` (`dockerizePip: true`) |

Instalación de dependencias Node (raíz del repo):

```bash
npm install
```

Instalación de dependencias Python de desarrollo (ORM, migraciones, scripts):

```bash
pip install -r requirements-dev.txt
```

## Estructura del repositorio

```text
backend-v1/
├── package.json              # Scripts npm (deploy, sync, crear servicio, sync stock)
├── serverless.yml            # Raíz mínima (servicios viven en src/services/)
├── requirements-dev.txt      # Deps dev: sqlalchemy, psycopg2-binary, alembic, openpyxl, etc.
├── alembic.ini               # Configuración Alembic (migraciones)
├── alembic/                  # Migraciones de base de datos
│   ├── env.py                # Carga .env raíz vía scripts/dotenv_loader.py
│   └── versions/             # 001_initial_schema.py … 016_companies_checkout_billing.py
├── infra/                    # Terraform — RDS PostgreSQL, VPC, SG, S3 chat
│   ├── main.tf               # RDS + Security Group + Subnet Group
│   ├── s3_chat.tf            # Bucket S3 para adjuntos del chat
│   ├── variables.tf
│   ├── outputs.tf
│   └── terraform.tfvars      # Gitignored (contiene credenciales)
├── scripts/                  # Automatización Python
│   ├── create_service.py     # npm run create:service
│   ├── deploy_all.py         # npm run deploy:all
│   ├── deploy_service.py     # npm run deploy:service
│   ├── sls_service.py        # npm run sls:service
│   ├── sync_shared.py        # npm run shared:sync
│   ├── sls_cli.py            # Comando base `npx sls deploy`
│   ├── dotenv_loader.py      # Carga .env raíz sin pisar exports de shell
│   ├── build_shipping_data.py# npm run shipping:build-data
│   ├── sync_stock_bulk.py    # npm run sync:stock / sync:stock:all / sync:stock:dry
│   ├── rebuild_catalogs.py   # Reconstruye Catalogs B2B en Shopify por segmento
│   └── backfill_company_contacts.py  # Enlaza mainContact a empresas Shopify sin contacto principal
├── src/
│   ├── serverless.provider.yml          # Provider base (runtime, layers, httpApi, CORS)
│   ├── serverless.package.yml           # Patrones de empaquetado por servicio
│   ├── serverless.pythonRequirements.yml# plugin serverless-python-requirements
│   ├── shared/                          # Código compartido FUENTE
│   │   ├── cognito/                     # Cliente Cognito IdP (admin + auth flows)
│   │   └── database/
│   │       ├── engine.py                # Singleton SQLAlchemy (pool_size=1)
│   │       ├── base.py                  # Declarative Base
│   │       ├── user_context.py
│   │       └── models/                  # Modelos ORM (fuente única)
│   └── services/
│       └── <nombre-servicio>/
│           ├── serverless.yml
│           ├── requirements.txt
│           ├── handlers/                # Entry points Lambda
│           ├── models/                  # Re-exporta modelos desde shared/database/models/
│           ├── services/                # Lógica de negocio (paquete Python)
│           ├── utils/
│           ├── data/                    # (opcional) JSON estático, p. ej. shipping/
│           └── shared/                  # Copia generada por sync (gitignored)
└── docs/
    ├── ARCHITECTURE_GUIDE_V1.md         # Imports, sys.path, plantillas
    ├── DATA_MODEL_DOMAIN_V1.md          # Modelo relacional (reglas de negocio)
    ├── RDS_POSTGRESQL_ORM_IMPLEMENTATION_PLAN.md
    ├── CRM_SHOPIFY_ARCHITECTURE.md
    ├── CHAT_SERVICE_INTEGRATION.md
    ├── COMPANY_REGISTRATION_SERVICE.md
    ├── SHIPPING_QUOTE_SERVICE.md
    ├── SHOPIFY_OAUTH_FRONTEND.md
    ├── SYNC_STOCK_PAYMENT_CUSTOMIZATION.md
    ├── BACKEND_EMPRESAS_Y_SOLICITUDES.md
    ├── FRONTEND_*.md, GUIA_FRONTEND_*.md# Guías para los frontends
    └── postman/                         # Colección + environment Postman
```

**Importante:** dentro de cada servicio **no** hay otra carpeta `src/`. La raíz del paquete Python es la carpeta del servicio (`handlers/`, `models/`, etc. al mismo nivel que `serverless.yml`).

## Servicios desplegados

Cada subdirectorio de `src/services/` es un stack Serverless independiente (un `serverless.yml`, una pila CloudFormation):

| Servicio | Nombre stack | Recursos / responsabilidad |
|----------|--------------|----------------------------|
| `auth` | `apro-click-admin-auth` | Login, change password, forgot/confirm, logout (Cognito) |
| `users` | `apro-click-admin-users` | CRUD usuarios + **owner del Cognito User Pool**, App Client y Groups |
| `companies` | `apro-click-admin-companies` | CRUD empresas (B2B + sistema) |
| `company-registration` | `apro-click-admin-company-registration` | Form público de solicitud + aprobación |
| `common` | `apro-click-admin-common` | Health/utilidades compartidas |
| `shopify` | `apro-click-admin-shopify` | OAuth Shopify, checkout billing metadata |
| `orders` | `apro-click-admin-orders` | CRUD pedidos, webhooks Shopify (pedidos + inventario) + **SQS FIFO** `inventory-stock-sync` |
| `prices` | `apro-click-admin-prices` | Upload Excel listas de precio B2B + worker SQS + **S3** bucket de uploads |
| `shipping` | `apro-click-admin-shipping` | Cotización envíos + Shopify CarrierService callback + registro |
| `quotes` | `apro-click-admin-quotes` | Cotizaciones (handlers de negocio) |
| `leads` | `apro-click-admin-leads` | Leads (placeholder + health) |
| `notifications` | `apro-click-admin-notifications` | Cola SQS `NotificationsQueue` + worker Lambda `emailWorker` (SES). Dueño del envío de emails de la plataforma. |
| `chat` | `apro-click-admin-chat` | API HTTP del chat |
| `chat-ws` | `apro-click-admin-chat-ws` | **WebSocket API** + **DynamoDB** `ws-connections` (TTL + GSI por conversación) |
| `error-logs` | `apro-click-admin-error-logs` | Logs de errores del frontend. **DynamoDB** `error-logs` (PK `LOG` + SK `<ISO>#<uid>`, TTL 30 días). `POST /api/v1/error-logs` autenticado (adjunta usuario del token); `GET` solo SUPERADMIN. Auth vía `utils/cognito_staff.py` (espejo de users/company-registration). |
| `invoices` | `apro-click-admin-invoices` | Lista de facturas de la empresa por OC. `GET /api/v1/storefront/invoices?shopify_company_id=` (canal público storefront, `X-Api-Key` = `INVOICES_PUBLIC_API_KEY`) resuelve `Company.billing_rut`, toma las `QuoteRequest.apro_ia_folio` (folio OC) más recientes de la empresa (máx. 20) y consulta el DMS externo APRO (`dms.apro.cl`) en paralelo, mismo `APRO_IA_BEARER_TOKEN` que `quotes`/`company-registration`. Sin persistencia propia (proxy de lectura). |

**Dependencias entre stacks (CloudFormation `Fn::ImportValue`):** el stack `users` exporta `cognito-user-pool-id`, `cognito-user-pool-arn` y `cognito-app-client-id`. El stack `notifications` exporta `notifications-queue-url` y `notifications-queue-arn`. En un entorno nuevo **`users` y `notifications` deben deployarse antes que el resto**.

Outputs adicionales relevantes:
- `notifications` → `notifications-queue-url`, `notifications-queue-arn`, `notifications-dlq-url`
- `prices` → `prices-uploads-bucket`, `prices-uploads-queue-url`, `prices-uploads-queue-arn`
- `orders` → `inventory-sync-queue-url`, `inventory-sync-queue-arn`, `inventory-sync-dlq-url`
- `chat-ws` → `ws-api-id`, `ws-endpoint`, `ws-connections-table-arn`

## Base de datos — PostgreSQL (RDS)

### Stack de persistencia

| Componente | Tecnología |
|------------|-----------|
| **Motor** | PostgreSQL 17 en AWS RDS (`db.t4g.micro`) |
| **ORM** | SQLAlchemy 2.x (modelos declarativos) |
| **Migraciones** | Alembic |
| **Infraestructura** | Terraform (`infra/`) |
| **Driver** | `psycopg2-binary` |

### Modelos ORM

Los modelos SQLAlchemy viven en `src/shared/database/models/` y se copian a cada servicio vía `shared:sync`. Cada servicio re-exporta los modelos que necesita en su carpeta `models/`. `src/shared/database/models/__init__.py` importa **todos** los modelos para que Alembic los descubra.

| Modelo | Tabla | Archivo |
|--------|-------|---------|
| `Company` | `companies` | `database/models/company.py` |
| `User` | `users` | `database/models/user.py` |
| `UserCompany` | `user_companies` (M2M) | `database/models/user_company.py` |
| `Client` | `clients` | `database/models/client.py` |
| `AuditLog` | `audit_logs` | `database/models/audit_log.py` |
| `CompanyRegistrationRequest` | `company_registration_requests` | `database/models/registration_request.py` |
| `Conversation` | `conversations` | `database/models/conversation.py` |
| `Message` | `messages` | `database/models/message.py` |
| `ShopifyAppInstallation` | `shopify_app_installations` | `database/models/shopify.py` |
| `ShopifyOrder` | `shopify_orders` | `database/models/shopify_order.py` |
| `PriceListUpload` | (uploads de Excel) | `database/models/price_list.py` |
| `ShopifyPriceSegment` | (segmentos B2B) | `database/models/price_list.py` |

**Link de invitación (`?from=<code>`)** — `users.referral_code` (unique). Se genera automáticamente para `role` en `{SALES, SUPERADMIN}`:

- En **`create_user`** y al **promover a SALES/SUPERADMIN** en `update_user` (helper `_mint_unique_referral_code`).
- On-demand vía `GET /api/v1/users/me/referral-link` (servicio `users`, auth Cognito) — devuelve `{ code, role }`; 403 si el rol no aplica. Pieza clave: el handler propaga `UnauthorizedError/ForbiddenError` antes del `except Exception` genérico.

El form público de registro lo reenvía como `referral_code` en el body. `RegistrationRequestService.create_public_request` lo resuelve a `referred_by_user_id` filtrando `User.role.in_(["SALES","SUPERADMIN"]) AND status=ACTIVE` — si no matchea, se persiste sin atribución (no falla la solicitud). Al aprobar (`approve_request`), se propaga a `companies.referred_by_user_id` y se inserta un `UserCompany` automático si el admin no override `sales_user_id`. Migración: `018_referrals.py` (con backfill para SALES preexistentes).

**Endpoints derivados:**

- `GET /api/v1/companies?assigned_user_id=<uuid>` (stack `companies`) — filtra por `OUTER JOIN UserCompany OR companies.referred_by_user_id`, `DISTINCT`. Backbone de la pestaña "Mis empresas" del panel.
- `GET /api/v1/company-registration-requests/referral-stats` (stack `company-registration`, auth ADMIN/SUPERADMIN) — agrega por vendedor con `func.sum(case(...))`: `total`, `pending`, `approved`, `rejected`, `companies_count`. Devuelve también `totals`. **Importante**: en `serverless.yml` esta ruta se declara **antes** del `{request_id}` para evitar match con el path param.

**Relaciones SQLAlchemy críticas** — con dos FKs entre `companies` y `users` (`users.company_id` ↔ `companies.referred_by_user_id`), las relaciones explicitan `foreign_keys=`. Ver `Company.users` y `User.company` en `database/models/`.

**Sucursales (`CompanyAddress`) y contactos (`Client`)** — CRUD en el stack `companies` (`services/address_service.py`, `services/contact_service.py`):

- `is_primary` (addresses) e `is_main_contact` (clients, persistido) son únicos por empresa vía índice único parcial (`WHERE is_primary/is_main_contact = true`, migración `024`). `create_address`/`update_address` desmarcan el resto de la empresa en la misma transacción al marcar una nueva primaria; `delete_address` bloquea el borrado de la sucursal principal (mismo guardrail que ya tenía `delete_contact` para el contacto principal).
- Editar el email de un contacto (`update_contact`) **no** usa `customerUpdate` de Shopify (el email es el login del Customer) — resuelve/crea el Customer del nuevo email y reasigna el `CompanyContact`, desvinculando el anterior.
- `GET /api/v1/companies/{company_id}/contacts/{client_id}` (individual) existe igual que para addresses.
- **Canal público storefront** (autogestión desde `aproclick-connector`): `GET/POST/PUT/DELETE /api/v1/storefront/company/addresses[/{address_id}]`, autenticado con header `X-Api-Key` (`COMPANIES_PUBLIC_API_KEY`, mismo patrón que `QUOTES_PUBLIC_API_KEY`). Nunca acepta el UUID interno de `Company`: resuelve `company_id` a partir de `shopify_company_id` (`CompanyService.get_company_by_shopify_id`) — el storefront solo conoce el GID de Shopify de su propia sesión B2B (`customer.current_company.id`). Delega en `AddressService` ya existente, heredando automáticamente las protecciones de unicidad/borrado.

### Conexión

- La variable `DATABASE_URL` contiene el string PostgreSQL completo (con `sslmode=require`).
- Engine: `src/shared/database/engine.py` — singleton Lambda-friendly (`pool_size=1`, `max_overflow=0`, `pool_pre_ping=True`).
- API: `get_engine()` y `get_session()` (context manager). Los servicios obtienen sesión con `with get_session() as session:` y commitean explícitamente.
- Cada `serverless.yml` expone `DATABASE_URL: ${env:DATABASE_URL, ''}`.

### Migraciones con Alembic

`alembic/env.py` carga automáticamente el archivo **`.env` en la raíz del repo** vía `scripts/dotenv_loader.py` (las variables que ya exportaste en la shell **no** se pisan). No hace falta `export $(cat .env | xargs)` salvo que quieras forzar valores desde otro archivo.

```bash
npm run db:generate -- "descripcion_del_cambio"  # alembic revision --autogenerate
npm run db:migrate                                # alembic upgrade head
npm run db:rollback                               # alembic downgrade -1
npm run db:current                                # revisión actual
npm run db:history                                # historial completo
```

Todas estas tareas exportan `PYTHONPATH=src/shared` para que Alembic resuelva `from database.models import …`.

### Cognito (módulo compartido)

`src/shared/cognito/client.py` encapsula el cliente boto3 `cognito-idp` (singleton + helpers para auth flows y administración de usuarios/grupos). Se importa con `from cognito.client import …` tras añadir `shared/` al `sys.path`.

### Infraestructura con Terraform

```bash
cd infra
terraform init          # Solo la primera vez
terraform plan          # Revisar cambios
terraform apply         # Aplicar cambios
```

`infra/main.tf` define RDS + Security Group + Subnet Group sobre la VPC por defecto. `infra/s3_chat.tf` crea el bucket de adjuntos del chat. Outputs útiles tras apply: `rds_endpoint`, `rds_hostname`, `database_url`.

**Nota:** `infra/terraform.tfvars` contiene la contraseña de la DB y está en `.gitignore`. No commitear.

## Variables de entorno

- Cada `serverless.yml` de servicio usa `useDotenv: true` → lee el `.env` ubicado en el `cwd` de Serverless (que es la carpeta del servicio).
- Los scripts `deploy_service.py` / `deploy_all.py` / `sls_service.py` **cargan adicionalmente el `.env` de la raíz del repo** (vía `scripts/dotenv_loader.py`) y lo fusionan al entorno del proceso, así Serverless puede sustituir `${env:DATABASE_URL, ...}` aunque el cwd sea `src/services/<servicio>/`. Las variables ya exportadas en la shell tienen prioridad.
- Archivos `.env` (raíz) están en `.gitignore`; ver `.env.example` para el catálogo completo (Cognito, DATABASE_URL, Shopify OAuth/admin/scopes/versión API, métricos `prices`, `shipping`, API keys públicas, etc.).

## Flujo de trabajo diario

### 1. Sincronizar código compartido

Tras cambiar algo en `src/shared/` (incluidos modelos ORM o el cliente Cognito), copiar a todos los servicios:

```bash
npm run shared:sync
```

Equivale a `python3 scripts/sync_shared.py`. La carpeta `src/services/*/shared/` está en `.gitignore`.

### 2. Crear un servicio nuevo

```bash
npm run create:service -- <slug>
# ejemplo: npm run create:service -- billing
```

Genera un servicio mínimo con `handlers/health.py` + `serverless.yml` (importa los YAML compartidos de `src/`). Opciones: `--name <nombre-serverless>`, `--dry-run`, `--no-sync`.

### 3. Validar configuración Serverless (sin desplegar)

```bash
npm run sls:service -- auth print
```

Equivale a `npx sls print` ejecutado dentro de `src/services/auth/`.

### 4. Desplegar

Comando base usado por los scripts (equivalente manual):

```bash
npx sls deploy --verbose --aws-profile mh-prod --stage dev
```

**Un solo servicio:**

```bash
npm run deploy:service -- auth
npm run deploy:service -- auth prod
npm run deploy:service -- common dev --aws-profile mh-prod
```

Perfil por defecto: `mh-prod`. Sobrescribir con `--aws-profile` o variable de entorno `DEPLOY_AWS_PROFILE`.

**Todos los servicios** (cada carpeta bajo `src/services/` con `serverless.yml`):

```bash
npm run deploy:all
npm run deploy:all -- -s prod --aws-profile mh-prod
```

Por defecto ejecuta `shared:sync` antes del deploy. Para omitir: `--no-sync`. Si un servicio falla, usar `--continue` para seguir con el resto (el proceso termina con error si hubo fallos).

> ⚠️ El primer deploy de un entorno nuevo: **`users` antes que cualquier otro stack** porque exporta los outputs de Cognito.

### 5. Sincronización masiva de stock (operativo)

`scripts/sync_stock_bulk.py` lee `bulk-stock.jsonl` y actualiza el metafield `aproclick.stock` de las variantes de Shopify (offline token Admin con `write_products`). Usa los scripts npm correspondientes (`sync:stock`, `sync:stock:all`, `sync:stock:dry`). Variables: `SHOPIFY_SHOP`, `SHOPIFY_ADMIN_TOKEN`, `SHOPIFY_API_VERSION`, `SHOPIFY_STOCK_NAMESPACE`, `SHOPIFY_STOCK_KEY`. Ver `docs/SYNC_STOCK_PAYMENT_CUSTOMIZATION.md`.

## Scripts npm (resumen)

| Script | Descripción |
|--------|-------------|
| `npm run shared:sync` | Copia `src/shared/` → `src/services/*/shared/` |
| `npm run create:service -- <slug>` | Scaffold de servicio + `health` + `serverless.yml` |
| `npm run deploy:service -- <svc> [stage]` | `npx sls deploy` con verbose + aws-profile |
| `npm run deploy:all` | Deploy secuencial de todos los servicios (corre `shared:sync` antes salvo `--no-sync`) |
| `npm run sls:service -- <svc> <cmd...>` | Ejecuta `npx sls <cmd>` en el directorio del servicio |
| `npm run db:migrate` | Aplica migraciones pendientes (`alembic upgrade head`) |
| `npm run db:rollback` | Revierte la última migración (`alembic downgrade -1`) |
| `npm run db:generate -- "desc"` | Genera migración autogenerada desde cambios en modelos |
| `npm run db:current` | Revisión actual de la DB |
| `npm run db:history` | Historial de migraciones |
| `npm run shipping:build-data` | Regenera el JSON estático de localidades para `shipping/data/` |
| `npm run sync:stock` | Sync masivo de stock a metafields Shopify (solo positivos) |
| `npm run sync:stock:all` | Igual, incluyendo stock 0 / negativo |
| `npm run sync:stock:dry` | Dry-run del sync de stock |

## Scripts operativos de mantenimiento Shopify B2B

Ejecutar desde la raíz de `backend-v1/` (requieren `.env` con `DATABASE_URL` y opcionalmente `SHOPIFY_SHOP`):

| Script | Uso |
|--------|-----|
| `python3 scripts/rebuild_catalogs.py` | Reconstruye Catalogs B2B para uno o todos los segmentos (PYME / MEDIANA / GRAN_EMPRESA). Útil cuando hay empresas aprobadas sin catálogo. `--dry-run` para solo ver estado. |
| `python3 scripts/backfill_company_contacts.py` | Detecta empresas Shopify sin `mainContact` y lo corrige. Soporta `--dry-run` y `--company-id <id>` para procesar una sola empresa. |
| `python3 scripts/notify_approved_backfill.py` | Encola emails de aprobación (y opcionalmente rechazo) para `company_registration_requests` que nunca fueron notificadas (`approval_notified_at IS NULL`). Idempotente: una segunda corrida no reenvía. **Siempre empezar con `--dry-run`.** Resuelve la cola SQS vía CFN export `apro-click-<stage>-notifications-queue-url` o flag `--queue-url`. Útil tras habilitar SES por primera vez. |

## Convenciones de código

- Handlers: `handlers/<nombre>.py` → en YAML: `handler: handlers/<nombre>.lambda_handler`.
- **Imports entre módulos del mismo servicio sin prefijo `src.`**. La raíz del servicio se inyecta en `sys.path` con el patrón estándar al inicio de cada handler (ver `docs/ARCHITECTURE_GUIDE_V1.md`).
- Acceso a `shared/`: probar primero `/var/task/shared` (Lambda) y caer al `service_root/shared` local.
- Powertools: `Logger`, `Tracer`, `APIGatewayHttpResolver` (REST) o handlers crudos para WebSocket / SQS.
- Modelos ORM: definir en `src/shared/database/models/`, re-exportar en `<servicio>/models/`. Importar en `__init__.py` de shared para que Alembic los detecte.
- Sesiones DB: `with get_session() as session:` + `session.commit()` explícito. **No** dejar sesiones abiertas entre invocaciones.
- Cognito: usar `shared/cognito/client.py` (no boto3 directo) para mantener helpers consistentes.
- Notificaciones por email: usar `shared/notifications/sqs_publisher.py` (`encolar_nueva_solicitud`, `encolar_aprobacion`, `encolar_rechazo`) en lugar de llamar SES directamente. El servicio `notifications` es el único dueño de las credenciales SES. Para agregar un nuevo tipo: añadir `notify_xxx()` en `shared/notifications/ses_email.py`, registrarlo en `_HANDLERS` de `notifications/handlers/email_worker.py`, correr `npm run shared:sync`, redeploy `notifications`.
- Paginación: `offset` + `limit` (no `last_evaluated_key` de DynamoDB).
- Respuestas HTTP API: payload 2.0; CORS abierto en `httpApi` (provider base) para desarrollo.
- Versión Shopify Admin API por defecto: `2026-04` (overridable con `SHOPIFY_API_VERSION`).
- **Shopify B2B — `mainContact` obligatorio:** al crear una empresa con `companyCreate` sin `companyContact` (caso cliente existente), Shopify crea el contacto con `companyAssignCustomerAsContact` pero **no lo designa como `mainContact`** automáticamente. Siempre llamar `companyAssignMainContact` después. El helper `set_main_contact()` en `company-registration/utils/shopify_customers.py` lo hace. Sin `mainContact` el cliente no puede comprar B2B.
- **Funciones públicas en `company-registration/utils/shopify_customers.py`:** `ensure_shopify_b2b_company` (crea empresa + contacto + mainContact), `get_company_contact_info` (consulta mainContact y lista de contactos), `assign_customer_as_contact`, `set_main_contact`.

## Antes de modificar infraestructura o desplegar

- **Cambios en modelos ORM** → generar migración Alembic (`npm run db:generate -- "desc"`) y verificar el archivo en `alembic/versions/` **antes** de `npm run shared:sync` + deploy.
- **Recursos RDS** (instancia, security group, subnet group) y el **bucket S3 de chat** se manejan con Terraform en `infra/`. No crear ni modificar manualmente en consola AWS.
- **Recursos creados por Serverless** (Cognito Pool, S3 prices, SQS, DynamoDB WS, exports CloudFormation) viven dentro del `serverless.yml` del servicio dueño; tocarlos requiere redeploy de ese stack.
- Usar los scripts documentados aquí (no inventar comandos `serverless`/`sls` distintos) salvo petición explícita del usuario.

## Documentación ampliada

- **Arquitectura e imports:** `docs/ARCHITECTURE_GUIDE_V1.md`
- **Modelo de datos (dominio):** `docs/DATA_MODEL_DOMAIN_V1.md`
- **Plan de implementación PostgreSQL:** `docs/RDS_POSTGRESQL_ORM_IMPLEMENTATION_PLAN.md`
- **CRM B2B + Shopify (dominio y servicios):** `docs/CRM_SHOPIFY_ARCHITECTURE.md`
- **Chat (HTTP + WebSocket + adjuntos):** `docs/CHAT_SERVICE_INTEGRATION.md`, `docs/GUIA_FRONTEND_CHAT_*.md`
- **Solicitudes de registro de empresa:** `docs/COMPANY_REGISTRATION_SERVICE.md`, `docs/BACKEND_EMPRESAS_Y_SOLICITUDES.md`
- **Shipping (CarrierService + cotizador):** `docs/SHIPPING_QUOTE_SERVICE.md`
- **Sync de stock + payment customization:** `docs/SYNC_STOCK_PAYMENT_CUSTOMIZATION.md`
- **Checkout billing metadata:** `docs/checkout-billing-metadata-backend.md`, `docs/GUIA_FRONTEND_CHECKOUT_BILLING_METADATA.md`
- **OAuth Shopify (frontend conector):** `docs/SHOPIFY_OAUTH_FRONTEND.md`
- **Postman:** `docs/postman/Apro-Click-Admin-API.postman_collection.json` + environment
- **Scripts Python:** comentarios en cabecera de `scripts/*.py` y `scripts/sls_cli.py` (comando de deploy)

## Agentes de IA (Cursor / Claude)

- **`AGENTS.md`** (este archivo): proceso de desarrollo y comandos.
- **`CLAUDE.md`**: resumen ejecutivo para asistentes y enlace a esta guía.
- **`.cursor/rules/backend-v1.mdc`**: reglas del proyecto para el agente en Cursor.

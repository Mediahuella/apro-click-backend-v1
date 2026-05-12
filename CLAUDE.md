# Instrucciones para asistentes (Claude Code / Claude en IDE)

Este repositorio es el **backend serverless v1** de **Apro Click Admin**: **Serverless Framework 4**, **Python 3.13**, **AWS Lambda + API Gateway HTTP API + WebSocket API**, **AWS Lambda Powertools**, **PostgreSQL 17** (RDS), **SQLAlchemy 2.x**, **Alembic**, **Amazon Cognito** para autenticación, integración con **Shopify Admin API** (OAuth, pedidos, inventario, listas B2B, CarrierService) y persistencia auxiliar en **S3**, **SQS** y **DynamoDB**. Despliegue con **`npx sls deploy`**, infraestructura RDS y S3-chat con **Terraform** (`infra/`).

## Fuente de verdad del flujo de desarrollo

Lee y sigue **`AGENTS.md`** en la raíz: requisitos, estructura completa, lista de servicios, scripts npm (`shared:sync`, `create:service`, `deploy:service`, `deploy:all`, `db:*`, `sync:stock*`, `shipping:build-data`), perfil AWS por defecto (`mh-prod`), migraciones Alembic, variables de entorno y convenciones.

## Detalles que suelen afectar al código

1. **Sin `src/` dentro de cada servicio** — la raíz del paquete Python es `src/services/<nombre>/` (`handlers/`, `models/`, `services/` de negocio, `utils/`, `shared/` generado).
2. **`src/shared/`** es la fuente; **`src/services/*/shared/`** se genera con `npm run shared:sync` y está gitignored (no editar como fuente principal).
3. **Funciones Lambda** se declaran solo en el **`serverless.yml` de cada servicio**; provider/package/pythonRequirements compartidos están en `src/serverless.*.yml`.
4. **Patrón de imports** (`docs/ARCHITECTURE_GUIDE_V1.md`): inyectar el `service_root` en `sys.path` al inicio del archivo; luego importar `from models.X import …`, `from services.X import …`, `from utils.X import …` sin prefijo `src.`. Para `shared/`, probar `/var/task/shared` (Lambda) y caer al local.
5. **Dependencias entre stacks**: el stack `users` exporta los outputs de Cognito (User Pool, ARN, App Client) que el resto importa vía `Fn::ImportValue`. En un entorno nuevo, **`users` se despliega primero**.

## Servicios actuales

`auth`, `users` (owner del Cognito Pool), `companies`, `company-registration`, `common`, `shopify` (OAuth + billing metadata), `orders` (incluye webhooks Shopify + SQS FIFO de inventory sync), `prices` (Excel B2B → S3 + worker SQS), `shipping` (cotizador + Shopify CarrierService), `quotes`, `leads`, `notifications`, `chat` (HTTP), `chat-ws` (WebSocket API + DynamoDB de conexiones).

## Base de datos y ORM

1. **PostgreSQL 17** en RDS (`db.t4g.micro`), gestionado con **Terraform** desde `infra/main.tf`.
2. **SQLAlchemy 2.x** — modelos declarativos en `src/shared/database/models/`. `__init__.py` los importa todos para que Alembic los descubra. Cada servicio re-exporta los modelos que necesita en su `models/`.
3. **Modelos vigentes**: `Company`, `User`, `UserCompany` (M2M user↔company), `Client`, `AuditLog`, `CompanyRegistrationRequest`, `Conversation`, `Message`, `ShopifyAppInstallation`, `ShopifyOrder`, `PriceListUpload`, `ShopifyPriceSegment`. Ver `docs/DATA_MODEL_DOMAIN_V1.md` para reglas de negocio.
4. **Alembic** — migraciones en `alembic/versions/` (001 → 016 a la fecha). Comandos: `npm run db:generate -- "desc"`, `npm run db:migrate`, `npm run db:current`, `npm run db:history`, `npm run db:rollback`. Todos exportan `PYTHONPATH=src/shared`. `alembic/env.py` carga el `.env` raíz vía `scripts/dotenv_loader.py` sin pisar variables exportadas.
5. **Engine** (`src/shared/database/engine.py`): singleton con `pool_size=1`, `max_overflow=0`, `pool_pre_ping=True` (optimizado para Lambda). API pública: `get_engine()` y `get_session()` (context manager — commit explícito).
6. **Conexión**: variable `DATABASE_URL` (con `sslmode=require`). Cada `serverless.yml` la expone como `${env:DATABASE_URL, ''}`; los scripts de deploy cargan el `.env` raíz para que la sustitución funcione aunque el cwd sea la carpeta del servicio.

## Cognito y Shopify

- **Cognito**: el módulo compartido `src/shared/cognito/client.py` envuelve `boto3.cognito-idp` con helpers para `admin_*` y para los flujos de auth (login, change password, forgot/confirm, logout). Usarlo desde los servicios en lugar de `boto3` directo.
- **Roles API** (mayúsculas): `SUPERADMIN`, `ADMIN`, `SALES`, `KPI_VISUALIZERS`. **Grupos Cognito** (minúsculas): `superadmin`, `admin`, `sales`, `kpi_visualizers`. **Estados**: `ACTIVE | DISABLED | PENDING`.
- **Shopify Admin API**: versión por defecto `2026-04` (override con `SHOPIFY_API_VERSION`). OAuth en `shopify`, pedidos+inventario en `orders`, listas B2B en `prices`, CarrierService en `shipping`. Token offline para scripts: `SHOPIFY_SHOP` + `SHOPIFY_ADMIN_TOKEN`.

## Antes de sugerir deploy

- Preferir los scripts documentados en `AGENTS.md` (`deploy:service`, `deploy:all`, `sls:service`) en lugar de inventar comandos `serverless`/`sls` distintos, salvo que el usuario pida otra cosa. El comando base es `npx sls deploy --verbose --aws-profile mh-prod --stage <stage>`.
- Si hay cambios en modelos ORM (`src/shared/database/models/`), generar migración Alembic y ejecutar `npm run shared:sync` antes de deployar.
- En un entorno nuevo, deployar primero el stack `users` (Cognito Pool) para que el resto pueda importarlo.

## Antes de cambiar infraestructura

- **RDS** (instancia, security group, subnet group) y el **bucket S3 de adjuntos del chat** se manejan con Terraform en `infra/` (`main.tf`, `s3_chat.tf`). No crear ni modificar manualmente en la consola AWS.
- Recursos creados por Serverless (Cognito, bucket S3 de prices, SQS FIFO de inventory, DynamoDB de WebSocket) viven en el `serverless.yml` del servicio dueño; modificarlos requiere redeploy de ese stack.
- `infra/terraform.tfvars` contiene credenciales y está en `.gitignore`. No commitear.

## Notas operativas

- Idioma del proyecto: **español** (commits, docs, comentarios). Mantenerlo al editar/crear documentación.
- Hay una colección Postman en `docs/postman/` para probar endpoints.
- `bulk-stock.jsonl` en la raíz es input del sync masivo de stock (`npm run sync:stock`).

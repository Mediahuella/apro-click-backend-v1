# Guía de desarrollo — Apro Click Admin Backend v1

Backend serverless en **AWS Lambda** con **Serverless Framework 4**, **Python 3.13**, **AWS Lambda Powertools**, **HTTP API** (API Gateway v2), **PostgreSQL** (AWS RDS) y **SQLAlchemy 2.x** como ORM.

## Requisitos

| Herramienta | Uso |
|-------------|-----|
| **Node.js** + npm | `serverless` / `npx sls`, scripts en `package.json` |
| **Python 3** (`python3`) | Scripts en `scripts/*.py` (multiplataforma; no depender de bash) |
| **AWS CLI / credenciales** | Perfil por defecto `mh-prod` en deploys (configurable) |
| **Terraform** | Infraestructura RDS PostgreSQL (`infra/`) |

Instalación de dependencias Node (raíz del repo):

```bash
npm install
```

Instalación de dependencias Python de desarrollo (ORM, migraciones):

```bash
pip install -r requirements-dev.txt
```

## Estructura del repositorio

```text
backend-v1/
├── package.json              # Scripts npm (deploy, sync, crear servicio)
├── serverless.yml            # Raíz mínima (servicios viven en src/services/)
├── requirements-dev.txt      # Deps dev: sqlalchemy, psycopg2-binary, alembic
├── alembic.ini               # Configuración Alembic (migraciones)
├── alembic/                  # Migraciones de base de datos
│   ├── env.py
│   └── versions/
├── infra/                    # Terraform — RDS PostgreSQL, VPC, Security Groups
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   └── terraform.tfvars      # Gitignored (contiene credenciales)
├── scripts/                  # Automatización (Python)
├── src/
│   ├── serverless.provider.yml
│   ├── serverless.package.yml
│   ├── serverless.pythonRequirements.yml
│   ├── shared/               # Código compartido fuente (se copia a cada servicio)
│   │   └── database/         # ORM: base, engine, models/
│   └── services/
│       └── <nombre-servicio>/
│           ├── serverless.yml
│           ├── requirements.txt
│           ├── handlers/
│           ├── models/       # Re-exportan modelos desde shared/database/models/
│           ├── services/     # Lógica de negocio (paquete Python)
│           ├── utils/
│           └── shared/       # Copia generada (gitignored); no editar a mano
└── docs/
    ├── ARCHITECTURE_GUIDE_V1.md
    ├── DATA_MODEL_DOMAIN_V1.md        # Modelo de datos relacional
    └── RDS_POSTGRESQL_ORM_IMPLEMENTATION_PLAN.md
```

**Importante:** dentro de cada servicio **no** hay otra carpeta `src/`. La raíz del paquete Python es la carpeta del servicio (`handlers/`, `models/`, etc. al mismo nivel que `serverless.yml`).

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

Los modelos SQLAlchemy viven en `src/shared/database/models/` y se copian a cada servicio vía `shared:sync`. Cada servicio re-exporta los modelos que necesita en su carpeta `models/`.

| Modelo | Tabla | Archivo |
|--------|-------|---------|
| `Company` | `companies` | `database/models/company.py` |
| `User` | `users` | `database/models/user.py` |
| `Client` | `clients` | `database/models/client.py` |
| `AuditLog` | `audit_logs` | `database/models/audit_log.py` |
| `CompanyRegistrationRequest` | `company_registration_requests` | `database/models/registration_request.py` |
| `Conversation` | `conversations` | `database/models/conversation.py` |
| `Message` | `messages` | `database/models/message.py` |
| `ShopifyAppInstallation` | `shopify_app_installations` | `database/models/shopify.py` |

### Conexión

- La variable de entorno `DATABASE_URL` contiene el string de conexión completo con `sslmode=require`.
- El engine se configura en `src/shared/database/engine.py` (singleton optimizado para Lambda: `pool_size=1`, `pool_pre_ping=True`).
- Cada `serverless.yml` de servicio expone `DATABASE_URL: ${env:DATABASE_URL, ''}`.

### Migraciones con Alembic

`alembic/env.py` carga automáticamente el archivo **`.env` en la raíz del repo** vía `scripts/dotenv_loader.py` (las variables que ya exportaste en la shell **no** se pisan). No hace falta `export $(cat .env | xargs)` salvo que quieras forzar valores desde otro archivo.

Crear nueva migración:

```bash
npm run db:generate -- "descripcion_del_cambio"
```

Aplicar migraciones:

```bash
npm run db:migrate
```

Ver estado actual:

```bash
npm run db:current
```

### Infraestructura con Terraform

La instancia RDS y recursos asociados (Security Group, Subnet Group) se manejan desde `infra/`.

```bash
cd infra
terraform init          # Solo la primera vez
terraform plan          # Revisar cambios
terraform apply         # Aplicar cambios
```

Outputs útiles tras apply: `rds_endpoint`, `rds_hostname`, `database_url`.

**Nota:** `infra/terraform.tfvars` contiene la contraseña de la DB y está en `.gitignore`. No commitear.

## Variables de entorno

- Cada `serverless.yml` de servicio usa `useDotenv: true`.
- Archivos `.env` en la raíz del repo están en `.gitignore`; usar `.env.example` para documentar claves sin valores secretos.
- Variable principal: `DATABASE_URL` (string de conexión PostgreSQL).

## Flujo de trabajo diario

### 1. Sincronizar código compartido

Tras cambiar algo en `src/shared/` (incluidos modelos ORM), copiar a todos los servicios:

```bash
npm run shared:sync
```

Equivale a `python3 scripts/sync_shared.py`. La carpeta `src/services/*/shared/` está en `.gitignore`.

### 2. Crear un servicio nuevo

```bash
npm run create:service -- <slug>
# ejemplo: npm run create:service -- billing
```

Opciones: `--name <nombre-serverless>`, `--dry-run`, `--no-sync`.

### 3. Validar configuración Serverless (sin desplegar)

Desde la carpeta del servicio o con el script genérico:

```bash
npm run sls:service -- auth print
```

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

Los scripts `deploy:service`, `deploy:all` y `sls:service` cargan el archivo **`.env` de la raíz del repo** y lo fusionan con el entorno del proceso (si ya exportaste `DATABASE_URL` u otra variable en la shell, ese valor tiene prioridad). Así Serverless puede sustituir `${env:DATABASE_URL, ...}` y el resto de claves aunque el `cwd` sea `src/services/<servicio>/`.

**Todos los servicios** (cada carpeta bajo `src/services/` con `serverless.yml`):

```bash
npm run deploy:all
npm run deploy:all -- -s prod --aws-profile mh-prod
```

Por defecto ejecuta `shared:sync` antes del deploy. Para omitir: `--no-sync`. Si un servicio falla, usar `--continue` para seguir con el resto (el proceso termina con error si hubo fallos).

## Scripts npm (resumen)

| Script | Descripción |
|--------|-------------|
| `npm run shared:sync` | Copia `src/shared/` → `src/services/*/shared/` |
| `npm run create:service -- <slug>` | Scaffold de servicio + `health` + `serverless.yml` |
| `npm run deploy:service -- <svc> [stage]` | `npx sls deploy` con verbose + aws-profile |
| `npm run deploy:all` | Deploy secuencial de todos los servicios |
| `npm run sls:service -- <svc> <cmd...>` | Ejecuta `npx sls <cmd>` en el directorio del servicio |
| `npm run db:migrate` | Aplica todas las migraciones pendientes (`alembic upgrade head`) |
| `npm run db:rollback` | Revierte la última migración (`alembic downgrade -1`) |
| `npm run db:generate -- "desc"` | Genera migración automática desde cambios en modelos |
| `npm run db:current` | Muestra la revisión actual de la DB |
| `npm run db:history` | Lista el historial de migraciones |

## Convenciones de código

- Handlers: `handlers/<nombre>.py` → en YAML: `handler: handlers/<nombre>.lambda_handler`.
- Imports entre módulos del mismo servicio **sin** prefijo `src.` (ver `docs/ARCHITECTURE_GUIDE_V1.md`).
- Powertools: `Logger`, `Tracer`, `APIGatewayHttpResolver` según la guía de arquitectura.
- Modelos ORM: definir en `src/shared/database/models/`, re-exportar en `<servicio>/models/`.
- Servicios de negocio: usar `get_session()` de `database.engine` para obtener sesiones SQLAlchemy.
- Paginación: basada en `offset` + `limit` (no `last_evaluated_key` de DynamoDB).

## Documentación ampliada

- **Arquitectura e imports:** `docs/ARCHITECTURE_GUIDE_V1.md`
- **Modelo de datos (dominio):** `docs/DATA_MODEL_DOMAIN_V1.md`
- **Plan de implementación PostgreSQL:** `docs/RDS_POSTGRESQL_ORM_IMPLEMENTATION_PLAN.md`
- **CRM B2B + Shopify (dominio y servicios):** `docs/CRM_SHOPIFY_ARCHITECTURE.md`
- **Scripts Python:** comentarios en cabecera de `scripts/*.py` y `scripts/sls_cli.py` (comando de deploy).

## Agentes de IA (Cursor / Claude)

- **`AGENTS.md`** (este archivo): proceso de desarrollo y comandos.
- **`CLAUDE.md`**: resumen para asistentes y enlace a esta guía.
- **`.cursor/rules/`**: reglas del proyecto para el agente en Cursor.

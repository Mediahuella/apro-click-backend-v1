# Instrucciones para asistentes (Claude Code / Claude en IDE)

Este repositorio es el **backend serverless v1** (Apro Click Admin): **Serverless Framework 4**, **Python 3.13**, **AWS Lambda**, **Powertools**, **PostgreSQL 17** (RDS), **SQLAlchemy 2.x**, **Alembic**, despliegue con **`npx sls deploy`**, infraestructura con **Terraform**.

## Fuente de verdad del flujo de desarrollo

Lee y sigue **`AGENTS.md`** en la raíz del proyecto: requisitos, estructura, comandos `npm` (`shared:sync`, `create:service`, `deploy:service`, `deploy:all`), perfil AWS por defecto (`mh-prod`), migraciones Alembic y convenciones.

## Detalles que suelen afectar al código

1. **Sin `src/` dentro de cada servicio** — la raíz del paquete es `src/services/<nombre>/` (`handlers/`, `models/`, `utils/`, paquete `services/` de negocio).
2. **`src/shared/`** es la fuente; **`src/services/*/shared/`** se genera con `npm run shared:sync` (no editar la copia como fuente principal).
3. **Funciones Lambda** se declaran solo en el **`serverless.yml` de cada servicio**; provider/package/pythonRequirements compartidos están en `src/serverless.*.yml`.
4. **Documentación de imports y plantillas:** `docs/ARCHITECTURE_GUIDE_V1.md`.

## Base de datos y ORM

1. **PostgreSQL 17** en RDS (`db.t4g.micro`), gestionado con **Terraform** desde `infra/`.
2. **SQLAlchemy 2.x** — modelos declarativos en `src/shared/database/models/`. Cada servicio re-exporta los modelos en su `models/`.
3. **Alembic** — migraciones en `alembic/versions/`. Correr con `PYTHONPATH=src/shared alembic upgrade head` (requiere `DATABASE_URL` en entorno).
4. **Engine** (`src/shared/database/engine.py`): singleton con `pool_size=1`, `pool_pre_ping=True` (optimizado para Lambda).
5. **Conexión**: variable `DATABASE_URL` con `sslmode=require`. Cada `serverless.yml` la expone como `${env:DATABASE_URL, ''}`.
6. **Modelo de datos:** `docs/DATA_MODEL_DOMAIN_V1.md` — 8 tablas: companies, users, clients, audit_logs, company_registration_requests, conversations, messages, shopify_app_installations.

## Antes de sugerir deploy

- Preferir ejecutar los scripts documentados en `AGENTS.md` en lugar de inventar comandos `serverless`/`sls` distintos, salvo que el usuario pida otra cosa.
- Si hay cambios en modelos ORM (`src/shared/database/models/`), generar migración Alembic y ejecutar `shared:sync` antes de deployar.

## Antes de cambiar infraestructura

- Los recursos RDS (instancia, security group, subnet group) se manejan con **Terraform** en `infra/`. No crear ni modificar estos recursos manualmente en la consola AWS.
- `infra/terraform.tfvars` contiene credenciales y está en `.gitignore`.

# Apro Click Admin — Backend v1

Backend administrativo **serverless** para Apro Click: funciones en **AWS Lambda**, API **HTTP** (API Gateway v2), persistencia en **PostgreSQL 17** (Amazon RDS) y ORM **SQLAlchemy 2.x**. El despliegue se gestiona con **Serverless Framework 4** y la base relacional con **Alembic**; la infraestructura de RDS y red se define en **Terraform** (`infra/`).

## Requisitos

| Herramienta | Uso |
|-------------|-----|
| **Node.js** y **npm** | Serverless y scripts del repositorio |
| **Python 3** | Lambdas, scripts en `scripts/` |
| **AWS CLI** y credenciales | Despliegue (perfil por defecto documentado: `mh-prod`) |
| **Terraform** | RDS y recursos asociados |

## Inicio rápido

1. Clonar el repositorio e instalar dependencias de Node en la raíz:

   ```bash
   npm install
   ```

2. Instalar dependencias de desarrollo Python (ORM, migraciones):

   ```bash
   pip install -r requirements-dev.txt
   ```

3. Configurar variables de entorno: copiar `.env.example` a `.env` y completar los valores (por ejemplo `DATABASE_URL`, Cognito, Shopify según el servicio). Los archivos `.env` no se versionan.

4. Tras modificar código en `src/shared/`, sincronizar la copia en cada servicio:

   ```bash
   npm run shared:sync
   ```

La guía detallada de flujo de trabajo, convenciones y comandos está en **[AGENTS.md](./AGENTS.md)**.

## Estructura del repositorio

- **`src/shared/`** — Código compartido (por ejemplo `database/`: motor, modelos ORM). Es la fuente de verdad; se replica en cada servicio con `npm run shared:sync` hacia `src/services/*/shared/` (carpeta generada, ignorada por Git).
- **`src/services/<nombre>/`** — Un servicio Serverless por carpeta: `serverless.yml`, `handlers/`, `models/`, `services/`, `utils/`. No hay una carpeta `src/` anidada dentro del servicio.
- **`alembic/`** — Migraciones de base de datos.
- **`infra/`** — Terraform (RDS, VPC, grupos de seguridad, etc.).
- **`docs/`** — Arquitectura, modelo de datos e integraciones.

## Servicios Serverless

Cada carpeta bajo `src/services/` con su propio `serverless.yml` es un despliegue independiente, por ejemplo:

- `auth`, `users`, `companies`, `common`
- `shopify`, `orders`, `company-registration`
- `chat`, `chat-ws`
- `leads`, `quotes`, `notifications`

Validar o desplegar un servicio concreto:

```bash
npm run sls:service -- <nombre-servicio> print
npm run deploy:service -- <nombre-servicio> [stage]
```

Desplegar todos los servicios (por defecto ejecuta `shared:sync` antes):

```bash
npm run deploy:all
```

Opciones y perfiles AWS: ver **AGENTS.md**.

## Base de datos

- Conexión mediante **`DATABASE_URL`** (incluir `sslmode=require` según el entorno).
- Migraciones:

  ```bash
  npm run db:generate -- "descripcion_del_cambio"
  npm run db:migrate
  npm run db:current
  ```

- Tras cambiar modelos en `src/shared/database/models/`, generar migración Alembic y ejecutar `npm run shared:sync` antes de desplegar.

## Infraestructura

RDS y recursos relacionados se administran solo vía Terraform en `infra/`. No modificar la base de datos de producción manualmente en la consola de AWS. `terraform.tfvars` contiene secretos y no debe versionarse.

## Documentación

| Documento | Contenido |
|-----------|-----------|
| [AGENTS.md](./AGENTS.md) | Comandos npm, deploy, migraciones, convenciones |
| [docs/ARCHITECTURE_GUIDE_V1.md](./docs/ARCHITECTURE_GUIDE_V1.md) | Arquitectura e imports |
| [docs/DATA_MODEL_DOMAIN_V1.md](./docs/DATA_MODEL_DOMAIN_V1.md) | Modelo de datos |
| [CLAUDE.md](./CLAUDE.md) | Resumen para asistentes de código |

## Licencia

Ver el campo `license` en `package.json` del repositorio.

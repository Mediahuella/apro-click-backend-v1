# Plan de implementación: PostgreSQL (RDS) + ORM + refactor de datos

**Estado:** borrador para alinear decisiones — **no** sustituye aún el código existente (DynamoDB, handlers actuales).

**Objetivo:** disponer de una guía ejecutable para migrar la persistencia hacia **PostgreSQL en AWS**, usar un **ORM con migraciones** (flujo similar a Prisma en Node) y **replantear el esquema relacional** de tablas.

---

## 1. Contexto actual (referencia)

- Backend **Serverless Framework 4**, **Python 3.13**, **AWS Lambda**, HTTP API.
- Varios servicios bajo `src/services/<nombre>/`; datos hoy en **DynamoDB** donde aplica (p. ej. usuarios, compañías).
- Región de despliegue habitual: **us-east-2** (validar en `serverless` / cuenta).

Este documento asume que la implementación seguirá **`AGENTS.md`** (deploy, `shared:sync`, estructura sin `src/` anidado en cada servicio) y la guía de imports en `docs/ARCHITECTURE_GUIDE_V1.md`.

---

## 2. Elección de base de datos en AWS

### 2.1 RDS PostgreSQL vs Aurora PostgreSQL

| Criterio | RDS PostgreSQL | Aurora PostgreSQL |
|----------|----------------|-------------------|
| Coste típico en **dev** | Suele ser **más bajo** con instancias **db.t4g.micro** / **db.t3.micro** + almacenamiento mínimo (p. ej. GP3). | Mínimos de capacidad (p. ej. Aurora Serverless v2 con ACU mínimos) suelen ser **más caros** para cargas muy bajas y constantes. |
| Operación | Una instancia (o Multi-AZ si se requiere HA). | Clúster Aurora, réplicas opcionales, modelo distinto de facturación. |
| Uso recomendado aquí | **Candidato principal** para “plan lo más bajo posible” en desarrollo/staging. | Valorar si el equipo necesita escalado automático, réplicas de lectura o características Aurora concretas. |

**Decisión pendiente (marcar antes de implementar):**

- [ ] **RDS PostgreSQL** (recomendado por coste dev)  
- [ ] **Aurora PostgreSQL** (justificar: ___)

### 2.2 Red y seguridad (obligatorio con Lambda)

- **VPC:** la instancia RDS debe residir en **subnets privadas**; las Lambdas que accedan a la BD deben estar en **subnets** que enruten al RDS (misma VPC / security groups).
- **Security groups:** reglas explícitas **Lambda SG → RDS SG** puerto **5432** (o el que se configure).
- **Credenciales:** preferible **AWS Secrets Manager** (o parámetro en SSM) referenciado por el rol de la Lambda; evitar usuario/clave en texto plano en `serverless.yml`.
- **Acceso desde local:** bastion host, **SSM Session Manager** port forwarding, o VPN según política de la organización — **decidir** flujo de desarrollo.

### 2.3 Conexiones y Lambda

- Sin capa intermedia, muchas Lambdas concurrentes pueden **agotar conexiones** al motor.
- **Amazon RDS Proxy** (recomendado en producción / carga variable): coste adicional; pooling hacia RDS.
- **Decisión pendiente:**

- [ ] RDS sin proxy en dev; proxy solo en staging/prod  
- [ ] RDS Proxy en todos los entornos  
- [ ] Otra estrategia (p. ej. pool mínimo documentada): ___

---

## 3. ORM y migraciones (equivalente “Prisma-like”)

### 3.1 Stack propuesto

| Rol | Herramienta | Notas |
|-----|-------------|--------|
| ORM | **SQLAlchemy 2.x** | Estándar en Python; modelos, relaciones, sesiones. |
| Migraciones | **Alembic** | Versionado de esquema; flujo análogo a `prisma migrate`. |
| Opcional | **SQLModel** | Capa sobre SQLAlchemy + Pydantic; útil si se unifican modelos API/DB. |

**Decisión pendiente:**

- [ ] SQLAlchemy + Alembic solamente  
- [ ] SQLAlchemy + Alembic + SQLModel  
- [ ] Evaluar otro ORM (documentar motivo): ___

### 3.2 Dónde viviría el código

Opciones a acordar (no implementado aún):

1. **Paquete compartido** en `src/shared/` (y `npm run shared:sync` a cada servicio) para: engine, sesión, `Base`, utilidades de transacción.
2. **Repositorio aparte** (menos habitual en este monorepo).
3. **Un solo servicio “dueño” del esquema** (no escala bien si muchos servicios escriben la misma BD).

**Decisión pendiente:** ¿un único esquema PostgreSQL compartido por varios microservicios o **bounded contexts** con esquemas separados / bases separadas?

- [ ] Una base, un esquema `public` (o `app`)  
- [ ] Una base, **varios esquemas** (`users`, `companies`, …)  
- [ ] Varias bases (mayor coste/operación): ___

---

## 4. Refactor y diseño de tablas

### 4.1 Principios

- Pasar de modelo **clave-valor / documento** (DynamoDB) a modelo **relacional**: PK/FK, integridad referencial, índices para consultas reales.
- Tipos enumerados en dominio (p. ej. tamaño de compañía, tipo de pago): decidir entre **`ENUM` PostgreSQL**, **tablas catálogo** o **CHECK** + `TEXT` (según evolución y compatibilidad con Alembic).
- Identificadores: **UUID** (`uuid` / `gen_random_uuid()`) alineados con APIs actuales si ya exponen UUID.

### 4.2 Inventario y modelo de dominio

El inventario de tablas, reglas de negocio (usuarios, compañías Apro, clientes + Shopify, auditoría, mensajería, instalación Shopify global) y relaciones está documentado en:

**[`docs/DATA_MODEL_DOMAIN_V1.md`](./DATA_MODEL_DOMAIN_V1.md)**

Antes de la primera migración Alembic, revisar ese documento y la tabla “Migración de datos” (§4.3) para DynamoDB existente.

### 4.3 Migración de datos existentes

- Si hay datos en DynamoDB en uso: plan **ETL** (export → transformación → carga en Postgres), ventana de mantenimiento o **doble escritura** (complejidad alta).
- Si solo hay dev: recrear datos de prueba o scripts de seed.

**Decisión pendiente:**

- [ ] Solo entornos nuevos / sin datos críticos  
- [ ] Migración con downtime documentada  
- [ ] Estrategia dual-write / backfill: ___

---

## 5. Cambios en infraestructura y despliegue (Serverless)

A alto nivel, cada servicio que acceda a RDS necesitará:

- **VPC:** `vpcId`, `subnetIds`, `securityGroupIds` en la función Lambda (o a nivel de provider, según cómo organice el equipo el `serverless.yml`).
- **IAM:** permisos para **Secrets Manager** (o SSM) si aplica; sin permisos amplios innecesarios.
- **Variables de entorno:** referencia al secreto o URL de conexión (mejor secret + construcción de URL en cold start o capa fina).
- **Dependencias Python:** `sqlalchemy`, `alembic`, driver **psycopg** (v3 recomendado: `psycopg[binary]`) — versión fijada en `requirements.txt` / layer según política del repo.

**Decisión pendiente:** ¿las migraciones Alembic se ejecutan **en CI/CD**, **manualmente**, o **Lambda one-off** / **CodeBuild**?

---

## 6. Fases de implementación sugeridas (orden)

1. **Decisiones de arquitectura** — cerrar secciones 2.1, 2.3, 3.2, 4.3 y migraciones (sección 5).
2. **Infra** — crear RDS (y Proxy si aplica), VPC, SG, Secrets; validar conectividad desde un entorno de prueba.
3. **Esquema inicial** — modelos SQLAlchemy + primera revisión Alembic en rama; revisión en PR.
4. **Capa de acceso a datos** — repositorios o servicios que sustituyan llamadas DynamoDB **por servicio** o por dominio acordado.
5. **Handlers** — adaptar respuestas/errores; mantener contratos API donde sea posible o versionar (`/v2`).
6. **Pruebas** — tests de integración contra Postgres en CI (Docker `postgres:16` o similar) si el equipo lo adopta.
7. **Apagado DynamoDB** — solo cuando no quede tráfico ni dependencias.

---

## 7. Preguntas abiertas (rellenar antes de codificar)

Use esta lista como checklist en la próxima reunión:

1. **Región y cuenta AWS** definitivas para RDS (¿siempre us-east-2?).
2. **Presupuesto mensual aproximado** aceptable para dev (RDS + posible Proxy + Secrets).
3. **Una base compartida vs varias**; **un esquema vs varios**.
4. **Cognito + usuarios:** ¿la tabla SQL es fuente de verdad junto a Cognito o solo proyección? ¿Qué pasa con grupos/roles?
5. **Compañías y otros dominios:** ¿relación usuario–compañía (N:M, 1:N)? ¿Multi-tenant por `company_id`?
6. **Entornos:** ¿dev/staging/prod con instancias separadas o una sola instancia con bases distintas?
7. **Herramienta de migraciones en pipeline:** GitHub Actions / manual / otro.
8. **Compatibilidad API:** ¿mantener payloads JSON actuales 1:1 o se permite breaking change documentado?

---

## 8. Referencias internas

- `AGENTS.md` — comandos, deploy, `shared:sync`.
- `docs/ARCHITECTURE_GUIDE_V1.md` — imports, handlers, estructura por servicio.
- `docs/DATA_MODEL_DOMAIN_V1.md` — modelo relacional de dominio (v1), tablas conceptuales y decisiones cerradas.
- `src/serverless.*.yml` — empaquetado y capas Python.

---

## 9. Historial del documento

| Versión | Fecha | Cambios |
|---------|-------|---------|
| 0.1 | *creación* | Plan inicial; sin cambios en código. |
| 0.2 | 2026-04 | Enlace a `DATA_MODEL_DOMAIN_V1.md` (§4.2). |

*Actualizar la tabla anterior cuando se cierren decisiones o se añadan fases.*

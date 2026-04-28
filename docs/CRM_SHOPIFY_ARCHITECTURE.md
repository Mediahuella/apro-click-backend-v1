# CRM B2B y Shopify — arquitectura de dominio

Este documento describe el diseño del CRM conectado a una tienda **Shopify B2B**: pipeline comercial (leads → cotizaciones → pedidos), contacto con vendedores y autorización de empresas. El backend está particionado en **servicios Serverless** independientes; cada uno expone hoy solo un endpoint de **health** hasta que se añadan rutas de negocio.

## Objetivo

- **Trazar el recorrido comercial** desde el lead hasta el pedido, con referencias a objetos en Shopify cuando existan.
- **Separar responsabilidades**: el CRM orquesta pipeline, tareas y aprobaciones; Shopify sigue siendo la fuente de verdad para catálogo, precios B2B, borradores de pedido y pedidos completados.
- **Escalar por dominio** desplegando cada servicio por separado (ver `AGENTS.md`).

## Flujo de negocio (alto nivel)

1. **Leads** — Captura y cualificación; aún puede no existir cliente en la tienda.
2. **Cotizaciones** — En CRM como entidad propia; en Shopify suele materializarse como **Draft Order** (líneas, notas, validez).
3. **Contacto con vendedor** — Actividades y tareas en CRM; opcionalmente notas o metadatos enlazados a **Customer** / **Draft Order** en Admin.
4. **Autorización de empresas (B2B)** — Flujo de revisión en CRM; al aprobar, creación o activación de **Company** / ubicaciones e invitación de compradores en Shopify.
5. **Pedidos** — Sincronización y lectura desde **Order** de Shopify; el CRM mantiene vínculos (`quote_id`, `lead_id`) para trazabilidad.

Cadena de trazabilidad recomendada en datos:

`lead → oportunidad/cotización (quote) → draft order (Shopify) → order (Shopify)`

## Mapeo conceptual con Shopify

| Concepto CRM | Shopify (B2B / Admin) |
|--------------|------------------------|
| Empresa aprobada | **Company**, **Company location**, compradores como **Customers** |
| Cotización aceptada / en curso | **Draft Order** (+ metafields para IDs del CRM si aplica) |
| Pedido confirmado | **Order** |
| Comprador | **Customer** vinculado a la compañía |
| Eventos de tienda | **Webhooks** + cola (p. ej. SQS) para procesamiento idempotente |

La **Admin API** (GraphQL/REST) se usará desde el servicio de integración para crear o actualizar draft orders, companies y leer pedidos. Los detalles de autenticación (app custom vs private) se documentan cuando se implemente.

## Servicios backend

Cada servicio vive en `src/services/<slug>/`, con `serverless.yml` propio y un handler `handlers/health.py`. Tras cambios en `src/shared/`, ejecutar `npm run shared:sync`.

| Slug | Nombre Serverless | Responsabilidad prevista | Health (GET) |
|------|-------------------|---------------------------|--------------|
| `leads` | `apro-click-admin-leads` | Leads, pipeline, cualificación | `/api/v1/health-leads` |
| `quotes` | `apro-click-admin-quotes` | Cotizaciones; orquestación futura con Draft Orders | `/api/v1/health-quotes` |
| `companies` | `apro-click-admin-companies` | Empresas B2B, flujos de aprobación, espejo lógico vs Shopify Company | `/api/v1/health-companies` |
| `orders` | `apro-click-admin-orders` | Pedidos, estado y vínculos con quotes/leads; consumo de webhooks | `/api/v1/health-orders` |
| `notifications` | `apro-click-admin-notifications` | Emails, tareas, recordatorios (interno o proveedor) | `/api/v1/health-notifications` |
| `shopify` | `apro-click-admin-shopify` | Cliente API Shopify, registro de webhooks, utilidades de sync | `/api/v1/health-shopify` |

Servicios ya existentes en el monorepo (`auth`, `users`, `common`, etc.) siguen aplicando para identidad y piezas transversales.

## Estado actual

- Cada servicio CRM listado arriba expone **únicamente** el endpoint de health indicado.
- No hay aún rutas de negocio ni persistencia específica del CRM en estos paquetes; se añadirán por servicio según prioridad.

## Próximos pasos (referencia)

- Definir modelo de datos (DynamoDB u otro) y metafields en Shopify para IDs del CRM.
- Implementar **webhooks** (orders, customers, companies, draft orders según API disponible) hacia Lambdas en `orders` y/o `shopify`.
- Endpoints REST bajo convención común (p. ej. `/api/v1/...`) y autorización reutilizando `auth`.

## Convenciones del repositorio

- Estructura por servicio sin carpeta `src/` anidada: ver `docs/ARCHITECTURE_GUIDE_V1.md`.
- Comandos de despliegue: `AGENTS.md` (`npm run deploy:service -- <slug>`, `deploy:all`).

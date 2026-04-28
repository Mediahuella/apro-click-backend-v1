# Servicio `company-registration` — integración (panel admin + theme app extension)

Servicio Serverless: **`apro-click-admin-company-registration`**. Expone solicitudes de alta de empresa: canal **público** para el formulario embebido en la tienda Shopify (API key **opcional** — ver abajo), y rutas **privadas** para el dashboard (Bearer Cognito).

---

## URL base (HTTP API)

Cada deploy genera un API Gateway HTTP API propio. Obtén la URL actual:

```bash
# Desde la raíz del repo
npm run sls:service -- company-registration info --verbose
```

En la salida, usa el **HttpApiUrl** del stack CloudFormation (trailing slash opcional).

**Ejemplo stage `dev` (referencia; confirma en tu cuenta):**

| Uso | Base |
|-----|------|
| API | `https://40uw16kay0.execute-api.us-east-2.amazonaws.com` |

Todas las rutas documentadas van **prefijadas** con esa base (p. ej. `GET {base}/api/v1/health-company-registration`).

**Health**

- `GET /api/v1/health-company-registration`  
- Respuesta: `{ "status": "healthy", "service": "apro-click-admin-company-registration" }` (vía Lambda Powertools).

---

## Convención de respuestas

Cuerpo típico:

```json
{
  "statusCode": 200,
  "message": "texto",
  "data": { }
}
```

Errores: `{ "statusCode": <código>, "message": "<detalle>" }` (Powertools / API Gateway).

---

## 1. Panel admin (frontend `aproclick-frontend`)

### Autenticación

- Header: `Authorization: Bearer <AccessToken de Cognito>` (mismo flujo que el resto del admin: token de sesión tras login).

Usuarios válidos en BD con rol **`SUPERADMIN`**, **`ADMIN`** o **`SALES`**. Otros roles reciben 401/403.

### Endpoints

| Método | Ruta | Descripción |
|--------|------|-------------|
| `GET` | `/api/v1/company-registration-requests` | Listado. Query: `status` opcional (`pending_review`, `approved`, `rejected`, `needs_info`), `limit` (default 50), `offset` (default 0). |
| `GET` | `/api/v1/company-registration-requests/{request_id}` | Detalle (UUID). |
| `POST` | `/api/v1/company-registration-requests/{request_id}/approve` | Aprueba: crea `Company` + `Client` en PostgreSQL y empresa B2B + cliente en Shopify (Admin GraphQL). |
| `POST` | `/api/v1/company-registration-requests/{request_id}/reject` | Body opcional: `{ "reason": "..." }`. |

**`data` en listado**

```json
{
  "requests": [ { ... } ]
}
```

Campos útiles por ítem (entre otros): `id`, `status` (`pending_review` \| `approved` \| `rejected` \| `needs_info`), `company_name`, `rut`, `contact_name`, `contact_email`, `contact_phone`, `company_type`, `payment_type`, `notes`, `shop_domain`, `source`, dirección (`shipping_*`), `created_at`, `resolved_company_id`, `resolved_by_user_id`.

**`approve` — `data` adicional**

Incluye `created_company` y `created_client` con los registros generados, y `shopify_b2b` con referencia a ids de Shopify.

Para el **frontend** (errores de aprobación, forma de `data`, checklist de pantallas): [GUIA_FRONTEND_THEME_REGISTRO_EMPRESA.md](./GUIA_FRONTEND_THEME_REGISTRO_EMPRESA.md) (sección *Frontend admin*).

### CORS

El API tiene CORS permisivo en gateway; el front debe llamar con credenciales solo si lo requiere la política del navegador (normalmente `fetch` con `Authorization` sin cookies cross-site está bien).

---

## 2. Theme app extension (formulario en la tienda)

### Autenticación (API key opcional)

- Si en la Lambda **`COMPANY_REGISTRATION_API_KEY`** está **vacía** (no definida o string vacío), el **`POST` público no exige** header `X-Api-Key` (útil en desarrollo).
- Si la variable tiene **valor**, el cliente debe enviar **`X-Api-Key`** con ese mismo valor; si falta o no coincide → 401.

Para producción conviene fijar la clave y pasarla desde **configuración de la app** o **theme settings** (no en repos públicos). No uses el token de sesión de Shopify del comprador como sustituto de este diseño.

### Alta pública

| Método | Ruta |
|--------|------|
| `POST` | `/api/v1/company-registration-requests` |

**Body JSON (snake_case)**

| Campo | Obligatorio | Notas |
|--------|-------------|--------|
| `company_name` | Sí | Razón social / nombre comercial. |
| `rut` | Sí | RUT chileno; el backend valida DV y normaliza. |
| `contact_name` | Sí | Nombre del contacto. |
| `contact_email` | Sí | Email de contacto. |
| `contact_phone` | No | |
| `company_type` | No | `SMALL` \| `MEDIUM` \| `BIG` (default `SMALL`). |
| `payment_type` | No | `DIRECT` \| `CREDIT` (default `DIRECT`). |
| `notes` | No | Texto libre. |
| `shop_domain` | **Recomendado** | Dominio Shopify de la tienda, p. ej. `mi-marca.myshopify.com`. Asocia la solicitud a la instalación correcta al **aprobar** (token en tabla `shopify_app_installations`). |
| `source` | No | Default `shopify_theme`. |
| `shipping_address1` | Ver guía | Dirección B2B (línea 1). Obligatorios para **aprobar**; el `POST` puede aceptarlos vacíos pero conviene validar en formulario. |
| `shipping_address2` | No | Línea 2 (depto., etc.). |
| `shipping_city` | Ver guía | Ciudad / comuna. |
| `shipping_zone_code` | No | Región (`zoneCode` en Shopify). |
| `shipping_zip` | Ver guía | Código postal. |
| `shipping_country_code` | No | ISO-2 (ej. `CL`). Default backend `CL` al aprobar. |
| `shipping_first_name` / `shipping_last_name` | No | Si se omiten, se usan el nombre/apellido del `contact_name`. |

Guía detallada (validación UX, ejemplos y admin): [GUIA_FRONTEND_THEME_REGISTRO_EMPRESA.md](./GUIA_FRONTEND_THEME_REGISTRO_EMPRESA.md).

**Ejemplo (fetch desde JS de la extensión)**

```javascript
const API_BASE = "https://40uw16kay0.execute-api.us-east-2.amazonaws.com"; // sustituir por tu HttpApiUrl
const API_KEY = undefined; // opcional: string desde theme settings solo si el backend exige clave

const headers = { "Content-Type": "application/json" };
if (API_KEY) headers["X-Api-Key"] = API_KEY;

await fetch(`${API_BASE}/api/v1/company-registration-requests`, {
  method: "POST",
  headers,
  body: JSON.stringify({
    company_name: "Ejemplo SpA",
    rut: "76123456-7",
    contact_name: "María Pérez",
    contact_email: "maria@ejemplo.cl",
    contact_phone: "+56 9 1234 5678",
    company_type: "MEDIUM",
    payment_type: "DIRECT",
    shop_domain: Shopify.shop, // si el runtime lo expone; si no, inyectar vía settings.schema + theme
    source: "shopify_theme",
  }),
});
```

En Liquid, el dominio de tienda suele estar disponible como `{{ shop.permanent_domain }}` o similar según contexto; pásalo a la extensión como setting o variable.

### Errores habituales (público)

- Con **`COMPANY_REGISTRATION_API_KEY`** definida en Lambda: sin `X-Api-Key` o clave incorrecta → 401.  
- RUT inválido o duplicado en cola pendiente → 400.

---

## 3. Backend: aprobación y Shopify

- El **access token** de Admin API **solo** se lee de PostgreSQL, tabla **`shopify_app_installations`**, columna **`shopify_access_token`** (OAuth del servicio `shopify`).  
- Al aprobar, se usa **Admin GraphQL** B2B (`companyCreate` / `companyAssignCustomerAsContact` si el cliente ya existía). Versión API configurable (`SHOPIFY_API_VERSION`, default **`2026-04`**).  
- La tienda debe ser **Shopify Plus** (Companies). La app en Partners debe incluir scopes acordes a clientes y empresas (p. ej. **`read_customers`**, **`write_customers`**, y los de **companies** según la consola actual).

### Mapeo: `payload` en BD → `companyCreate` (Shopify)

Lo que se guarda en **`company_registration_requests.payload`** (JSON) se usa al aprobar para armar **`CompanyCreateInput`**. Resumen:

| Guardado en BD | Uso en Shopify | ¿Suficiente? |
|----------------|----------------|--------------|
| `company_name` | `company.name` | Sí (obligatorio en Shopify). |
| `contact_email` | `companyContact.email` o búsqueda de cliente existente | Sí. |
| `contact_name` (partido) | `companyContact.firstName` / `lastName`; fallback nombres en dirección | Sí; si solo hay un nombre, el apellido puede quedar vacío y el backend envía un marcador. |
| `contact_phone` | `companyContact.phone`, teléfono en ubicación y en `shippingAddress` si aplica | Sí; formato E.164 recomendado (Shopify puede validar). |
| `shipping_*` / `shipping_address` / alias (`city`, `postal_code`, …) | `companyLocation.shippingAddress` (`address1`, `city`, `zip`, `countryCode`, `zoneCode` opcional, etc.) | Sí **si** al aprobar existen calle, ciudad y CP (normalizados por `shipping_payload`). |
| *(default)* `shipping_country_code` | `countryCode` | Si no viene en el formulario, al aprobar se usa **`CL`**. |
| `shop_domain` | No va al GraphQL; sirve para elegir **token** (`shopify_app_installations`) | Muy recomendable; sin tienda resuelta, falla el approve. |
| Id de la solicitud (UUID) | `company.externalId` | Sí (correlación con tu BD). |
| `rut`, `company_type`, `payment_type`, `notes`, `source` | **No** se envían hoy a Shopify | Solo negocio en PostgreSQL. Si hace falta RUT o notas en Admin Shopify, habría que mapearlos a `company.note`, metafields o similar. |

**Conclusión:** para **`companyCreate`** el formulario **sí cubre** lo que la API exige como mínimo típico (empresa + contacto + ubicación con dirección), **siempre que** la dirección completa esté en el payload al aprobar. Lo que **no** replica en Shopify son RUT, tipo de empresa y tipo de pago del formulario: siguen solo en tu modelo `companies` / solicitud.

**Riesgos puntuales (no son de “campos faltantes” en BD):** tienda no Plus, scopes, `countryCode` / `zoneCode` que Shopify rechace para ciertos países, o `userErrors` de políticas B2B de la tienda; en esos casos el error viene de GraphQL tras el approve.

---

## 4. Prerrequisitos de infraestructura

1. Stack **`users`** desplegado antes (export CloudFormation del User Pool Cognito para el IAM de este servicio).  
2. **`DATABASE_URL`** en el entorno usado por `deploy:service` (archivo `.env` en la raíz del repo, según `AGENTS.md`). **`COMPANY_REGISTRATION_API_KEY`** es opcional hasta que quieras exigir `X-Api-Key` en el `POST` público.  
3. Instalación Shopify con OAuth completado (`servicio shopify`) para que exista fila con token en **`shopify_app_installations`**.

---

## 5. Deploy

```bash
npm run shared:sync
npm run deploy:service -- company-registration [stage]
```

Otro stage (p. ej. `prod`): `npm run deploy:service -- company-registration prod`.

---

## Referencia cruzada

- Formulario tienda + admin (dirección, campos, ejemplos): [GUIA_FRONTEND_THEME_REGISTRO_EMPRESA.md](./GUIA_FRONTEND_THEME_REGISTRO_EMPRESA.md).  
- Visión funcional y modelo de datos de negocio: [BACKEND_EMPRESAS_Y_SOLICITUDES.md](./BACKEND_EMPRESAS_Y_SOLICITUDES.md).  
- Proceso de desarrollo: [AGENTS.md](../AGENTS.md).

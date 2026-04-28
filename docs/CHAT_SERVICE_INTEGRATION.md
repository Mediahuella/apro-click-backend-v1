# Servicio `chat` — integración (panel admin + theme app extension)

Servicio Serverless: **`apro-click-admin-chat`**. Expone mensajes entre **clientes de la tienda Shopify** (canal público con `X-Api-Key` opcional) y el **equipo en el CRM** (Bearer Cognito), persistiendo en PostgreSQL en las tablas **`conversations`** y **`messages`**.

---

## URL base (HTTP API)

Cada deploy genera un API Gateway HTTP API propio. Obtén la URL actual:

```bash
# Desde la raíz del repo
npm run sls:service -- chat info --verbose
```

En la salida, usa el **HttpApiUrl** del stack CloudFormation (trailing slash opcional).

Todas las rutas documentadas van **prefijadas** con esa base (p. ej. `GET {base}/api/v1/health-chat`).

**Health**

- `GET /api/v1/health-chat`  
- Respuesta: `{ "status": "healthy", "service": "apro-click-admin-chat" }` (vía Lambda Powertools).

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

### Mensaje en `data`

Cada ítem de mensaje incluye:

| Campo | Descripción |
|--------|-------------|
| `id` | UUID del mensaje. |
| `conversation_id` | UUID del hilo. |
| `sender_type` | `USER` (panel / vendedor CRM) o `CLIENT` (tienda). |
| `body` | Texto del mensaje. |
| `created_at` | ISO8601 (timezone). |

---

## 1. Panel admin (frontend `aproclick-frontend` u otro SPA)

### Autenticación

- Header: `Authorization: Bearer <AccessToken de Cognito>` (mismo flujo que el resto del admin: token de sesión tras login).

Roles permitidos: **`SUPERADMIN`**, **`ADMIN`**, **`SALES`**. Otros roles (p. ej. `KPI_VISUALIZERS`) reciben 401/403.

### Alcance por empresa

- **`SUPERADMIN`** y **`ADMIN`**: administración plataforma; pueden listar todas las conversaciones o filtrar con query **`company_id`** cuando la UI lo requiera.
- **`SALES`**: el alcance es el conjunto **`order_company_ids`** calculado en backend a partir de **`user_companies`** (N:N) más **`users.company_id`** si faltaba en el M2M. Las conversaciones se filtran con `conversations.company_id IN (ese conjunto)`. La empresa plataforma (`companies.is_system`) **no** entra en ese conjunto para `SALES` (ni para `KPI_VISUALIZERS` en otros servicios), para no mezclar comercios B2B con la fila interna del sistema.
- El query **`company_id`** en listados es **opcional** para `SALES`: sirve solo para acotar a una empresa **dentro** de su alcance; si se envía y no está permitido, la API responde error de permisos.
- **`SUPERADMIN`** sin empresa asignada en flujos de UI debe enviar **`company_id`** donde el contrato lo exige (ver guía frontend).

**Documentación orientada al frontend** (checklist, errores, multi-empresa): [GUIA_FRONTEND_CHAT_EXTENSION_ADMIN.md](./GUIA_FRONTEND_CHAT_EXTENSION_ADMIN.md) §3.4.

### Endpoints

| Método | Ruta | Descripción |
|--------|------|-------------|
| `GET` | `/api/v1/chat/conversations` | Listado de hilos. Query: `limit` (default 30), `offset` (default 0), `status` opcional (`OPEN` \| `CLOSED`). |
| `PATCH` | `/api/v1/chat/conversations/{conv_id}` | Cerrar o reabrir hilo. Body JSON: `{ "status": "CLOSED" }` o `"OPEN"`. Al cerrar se guarda **`closed_at`** (UTC, ISO8601); al reabrir queda `null`. Mismas reglas de query `company_id` que en mensajes (superadmin sin empresa asignada). |
| `GET` | `/api/v1/chat/conversations/{conv_id}/messages` | Mensajes del hilo (orden cronológico ascendente en la página solicitada). Query: `limit` (default 100), `offset` (default 0). |
| `POST` | `/api/v1/chat/conversations/{conv_id}/messages` | Enviar respuesta del staff. Body JSON: `{ "body": "..." }`. No permitido si el hilo está `CLOSED` (reabrir con `PATCH` antes). |

**Query `company_id` (superadmin)**

- En **`GET /conversations`**: obligatorio si el usuario es **SUPERADMIN** sin empresa asignada; filtra por esa empresa CRM.
- En **`GET` y `POST` …/conversations/{conv_id}/messages**: obligatorio en el mismo caso; debe coincidir con la empresa de la conversación.

**`data` en listado de conversaciones**

```json
{
  "data": [
    {
      "id": "<uuid>",
      "company_id": "<uuid>",
      "seller_user_id": "<uuid>",
      "client_id": "<uuid>",
      "status": "OPEN",
      "closed_at": null,
      "last_message_at": "...",
      "updated_at": "..."
    }
  ]
}
```

(`closed_at`: ISO8601 si `CLOSED`, si no `null`.)

**`data` en listado de mensajes (panel)**

```json
{
  "conversation_id": "<uuid>",
  "messages": [ { "id", "conversation_id", "sender_type", "body", "created_at" } ]
}
```

**`POST` mensaje (panel) — `data`**

Un solo objeto mensaje, mismos campos que en el listado.

### UX recomendada (admin)

1. Pantalla **bandeja**: `GET /chat/conversations` con paginación; mostrar `last_message_at`, estado y enlace al detalle (resolver nombre de cliente vía vuestro propio `GET /clients` o dato enriquecido si añadís endpoint).
2. Pantalla **hilo**: `GET /chat/conversations/{id}/messages`; formulario que hace `POST` con el mismo `conv_id`.
3. **Actualización en tiempo casi real**: la API no expone WebSockets; usad **polling** (p. ej. cada 10–30 s) o un intervalo al tener el hilo abierto.

### CORS

El API tiene CORS permisivo en gateway; el front puede usar `fetch` con `Authorization` sin credenciales cruzadas habituales.

---

## 2. Theme app extension (tienda)

### Requisito: cliente autenticado

El diseño actual **exige** un **`shopify_customer_id`** (comprador logueado). Visitantes anónimos no pueden abrir hilo vía este API hasta que el modelo de datos y reglas de negocio soporten clientes sin ID de Shopify (no soportado hoy en este servicio).

### Autenticación (API key opcional)

- Si en la Lambda **`CHAT_STOREFRONT_API_KEY`** está **vacía** (no definida o string vacío), el canal público **no exige** `X-Api-Key` (útil en desarrollo).
- Si la variable tiene **valor**, cada `fetch` debe incluir **`X-Api-Key`** con el mismo string; si falta o no coincide → 401.

Para producción, definid la clave en **variables de entorno** de la Lambda y pasadla a la extensión vía **theme settings** o **app settings** (nunca en repositorios públicos).

### Varias empresas: enrutar el hilo (UUID fijo vs B2B en sesión)

No es solo un dato fijo en el theme: el backend acepta **dos** formas de decir a qué empresa va el mensaje (en conjunto, **al menos una**; el error aplica si **no** enviáis ni UUID de CRM ni contexto B2B con compañía):

| Origen (theme) | Parámetro API | Uso |
|----------------|---------------|-----|
| Metacampo de tienda, setting de bloque, etc. | `company_id` (string) | **UUID** de `companies.id` en el CRM. Sirve con **una sola** empresa por tienda o cuando fijáis un valor constante. Ej.: `custom.apro_crm_company_id` → pasarlo a JS. |
| Comprador B2B, compañía actual en Shopify (cambia si cambia de compañía / sesión) | `shopify_company_id` (string) | Id de **Company** B2B en Shopify (id **numérico** o GID `gid://shopify/Company/...`). En Liquid, si está disponible, el id de la compañía de la sesión (p. ej. contexto `customer` + compañía B2B, según plantilla; la API hace de resto: busca en `companies.shopify_company_id` y resuelve a `companies.id`). Misma lógica que en el alta de empresas (`company-registration` persiste el id numérico en `companies.shopify_company_id`). |

**Reglas en el backend (resumen):**

- Si enviáis **los dos**, deben referir a la **misma** fila en `companies` (el UUID y el B2B deben ser coherentes).
- Si en **`shopify_app_installations`** la tienda tiene **`company_id` rellenado** (vínculo “una sola” empresa con esa instalación), la empresa **resuelta** debe ser **esa** (en UUID-only, B2B-only o ambos). Si además usáis B2B y la sesión pasa a otra compañía, dejará de coincidir (comportamiento esperado al cambiar de contexto).
- Si **`shopify_app_installations.company_id` es `null`** (misma app / tienda con **varias** empresas lógicas en CRM, enrutadas por B2B), **hace falta** enviar al menos **`shopify_company_id`**; no basta con un UUID fijo a menos que fijéis el vínculo en la instalación (ver error si solo UUID sin instalación).

Cada `POST` / `GET` al API público puede mandar: **`company_id` solo si lo tenéis**; **`shopify_company_id` solo** si el cliente es B2B con compañía actual. La **clave de `localStorage` del hilo** debería incluir **conversación + empresa** (p. ej. `conversation_id` y `company_id` resuelto, y el B2B numérico si aplica) para no mezclar hilos al cambiar de compañía.

### Endpoints (storefront)

| Método | Ruta |
|--------|------|
| `POST` | `/api/v1/chat/public/messages` |
| `GET` | `/api/v1/chat/public/conversations` |
| `GET` | `/api/v1/chat/public/conversations/{conv_id}/messages` |

**Varios hilos por cliente:** si el staff cierra un hilo (`CLOSED`), el siguiente `POST` de la tienda **abre un hilo nuevo** (`OPEN`) en lugar de reutilizar el cerrado. En base de datos solo puede haber **como mucho una** conversación `OPEN` por par `(company_id, client_id)`; los históricos quedan `CLOSED`.

**`GET /api/v1/chat/public/conversations` — query**

| Parámetro | Obligatorio | Notas |
|-----------|-------------|--------|
| `shop` | Sí | Igual que en el `POST`. |
| `shopify_customer_id` | Sí | Dueño de los hilos. |
| `company_id` / `shopify_company_id` | Uno u otro | Misma resolución de empresa que el `POST`. |
| `status` | No | Filtrar `OPEN` o `CLOSED`. Sin filtro: todas las del cliente en esa empresa. |
| `limit` | No | Default 30, máximo 100. |
| `offset` | No | Default 0. |

**Respuesta 200 — `data`**

```json
{
  "company_id": "<uuid>",
  "shopify_company_id": "<id o null>",
  "data": [
    {
      "id": "<uuid>",
      "status": "OPEN",
      "is_closed": false,
      "isClosed": false,
      "closed": false,
      "closed_at": null,
      "state": 0,
      "last_message_at": "...",
      "updated_at": "..."
    }
  ]
}
```

(`state`: `0` = abierta, `1` = cerrada, para normalizadores que esperan numérico.)

**`closed_at` en tienda:** `null` en hilos `OPEN`. En `CLOSED` coincide con el cierre hecho por staff (misma semántica que en panel); ISO8601 con zona.

**`POST` — body JSON (snake_case)**

| Campo | Obligatorio | Notas |
|--------|-------------|--------|
| `shop` | Sí | Dominio Shopify, p. ej. `mi-tienda.myshopify.com` o solo subdominio `mi-tienda` (el backend lo normaliza). |
| `company_id` | Uno u otro* | *Junto con `shopify_customer_id` y el resto: debe haber **al menos** `company_id` **o** `shopify_company_id`. UUID de `companies.id` (CRM). |
| `shopify_company_id` | Uno u otro* | Id B2B de Company en Shopify (numérico o GID). Se resuelve contra `companies.shopify_company_id` en la base de datos. |
| `shopify_customer_id` | Sí | ID numérico del **Customer** en Shopify. Acepta también GID: `gid://shopify/Customer/123` → se normaliza a `123`. En Liquid: `{{ customer.id }}` (solo si `customer` existe). |
| `body` | Sí | Texto del mensaje; no vacío. |
| `email` | No | Ayuda a rellenar/actualizar el registro `Client` en CRM. |
| `name` | No | Igual. |

**Respuesta `201` — `data`**

Incluye **`conversation`** y campos duplicados al nivel raíz (`status`, `is_closed`, `isClosed`, `state`, `closed_at`) para que el theme no dependa solo del cuerpo del mensaje.

```json
{
  "conversation_id": "<uuid>",
  "company_id": "<uuid>",
  "shopify_company_id": "<id numérico o null, según companies>",
  "status": "OPEN",
  "is_closed": false,
  "isClosed": false,
  "closed_at": null,
  "state": 0,
  "conversation": {
    "id": "<uuid>",
    "status": "OPEN",
    "is_closed": false,
    "isClosed": false,
    "closed": false,
    "closed_at": null,
    "state": 0,
    "last_message_at": "...",
    "updated_at": "..."
  },
  "message": {
    "id": "<uuid>",
    "conversation_id": "<uuid>",
    "sender_type": "CLIENT",
    "body": "...",
    "created_at": "..."
  }
}
```

Guardad **`conversation_id`** (y, si aplica, los identificadores de empresa devueltos) en `localStorage` o estado; diseñad la clave de almacenamiento para que no se mezclen conversaciones al cambiar de compañía B2B.

**`GET` — query string**

| Parámetro | Obligatorio | Notas |
|-----------|-------------|--------|
| `shop` | Sí | Misma semántica que en el `POST`. |
| `company_id` | Uno u otro* | *Al menos con `shopify_company_id` como en el `POST`. Mismas reglas de validación. Acepta también nombres alternativos: `companyId` (conveniencia de query). |
| `shopify_company_id` | Uno u otro* | Idem. Alternativa: `shopifyCompanyId` en query. |
| `shopify_customer_id` | Sí | Debe ser el **mismo** cliente dueño del hilo. |
| `limit` | No | Default 50, máximo 100 en lógica del servicio. |
| `offset` | No | Default 0. |

**Respuesta 200 — `data`**

Misma información de estado que el `POST` (`conversation`, `status`, `is_closed`, `isClosed`, `state`, `closed_at`).

```json
{
  "conversation_id": "<uuid>",
  "company_id": "<uuid>",
  "shopify_company_id": "<id o null>",
  "status": "OPEN",
  "is_closed": false,
  "isClosed": false,
  "closed_at": null,
  "state": 0,
  "conversation": { "id": "...", "status": "OPEN", "is_closed": false, "state": 0 },
  "messages": [ { "id", "conversation_id", "sender_type", "body", "created_at" } ]
}
```

### Liquid (inyectar datos al bloque de la app)

En el **schema** de la extensión podéis definir ajustes para `api_base` y opcionalmente la clave (o solo la base y la clave inyectada por la app vía `{% schema %}` + metadatos, según vuestra arquitectura).

Ejemplo mínimo en un snippet del bloque (valores reales vía settings):

```liquid
{% if customer %}
  <div
    id="apro-chat-root"
    data-api-base="{{ block.settings.api_base | escape }}"
    data-api-key="{{ block.settings.api_key | escape }}"
    data-shop="{{ shop.permanent_domain | escape }}"
    data-company-id="{{ block.settings.crm_company_id | escape }}"
    data-customer-id="{{ customer.id }}"
  ></div>
  <script src="{{ 'apro-chat.js' | asset_url }}" defer></script>
{% else %}
  <p>Debe iniciar sesión para chatear con nosotros.</p>
{% endif %}
```

- `{{ shop.permanent_domain }}` suele ser el dominio canónico myshopify (coherente con `shopify_app_installations.shop_domain` en base de datos).
- `{{ customer.id }}` es el identificador numérico que el backend espera (también podéis pasar GID y el backend lo normaliza).
- **`company_id` (CRM)** inyectar vía metafield (p. ej. `shop.metafields.custom.apro_crm_company_id` / vuestro key), **setting** del bloque, o lógica de app. Debe ser el **UUID** de `companies.id` cuando tengáis un valor fijo por tienda.
- **`shopify_company_id` (B2B)**: en plantillas con contexto B2B, el id de la compañía de sesión (según el objeto expuesto: p. ej. asociado a `customer` / compañía actual). Ese id es el que almacenáis en `companies.shopify_company_id` vía vuestro flujo de aprobación; el chat lo acepta y resuelve al UUID interno.

| Nombre en la API | Qué es |
|------------------|--------|
| `company_id` | UUID de `companies.id` (PostgreSQL). |
| `shopify_company_id` | Id B2B de **Company** en Shopify (misma columna `companies.shopify_company_id` que en registro/approval B2B). Acepta GID. |

### JavaScript (fetch, ejemplo)

Sustituid `API_BASE` por el valor de **HttpApiUrl** del servicio `chat` (misma idea que en [COMPANY_REGISTRATION_SERVICE.md](./COMPANY_REGISTRATION_SERVICE.md)).

```javascript
const API_BASE = "https://xxxxxxxx.execute-api.us-east-2.amazonaws.com";
const API_KEY = ""; // opcional: desde data-api-key o theme setting si el backend exige clave

function chatHeaders() {
  const h = { "Content-Type": "application/json" };
  if (API_KEY) h["X-Api-Key"] = API_KEY;
  return h;
}

/** @param crmCompanyId - UUID fijo o undefined @param b2bCompanyId - id B2B de la sesión o undefined; al menos uno. */
export async function sendMessage({
  shop,
  crmCompanyId,
  b2bCompanyId,
  shopifyCustomerId,
  body,
  email,
  name,
}) {
  const r = await fetch(`${API_BASE}/api/v1/chat/public/messages`, {
    method: "POST",
    headers: chatHeaders(),
    body: JSON.stringify({
      shop,
      ...(crmCompanyId != null && String(crmCompanyId).trim()
        ? { company_id: String(crmCompanyId) }
        : {}),
      ...(b2bCompanyId != null && String(b2bCompanyId).trim()
        ? { shopify_company_id: String(b2bCompanyId) }
        : {}),
      shopify_customer_id: String(shopifyCustomerId),
      body,
      email,
      name,
    }),
  });
  const j = await r.json();
  if (!r.ok) throw new Error(j.message || r.statusText);
  return j.data; // { conversation_id, message }
}

/** Cargar historial (misma tienda y mismo customer que el hilo). */
export async function listMessages({
  shop,
  crmCompanyId,
  b2bCompanyId,
  shopifyCustomerId,
  conversationId,
  limit = 50,
  offset = 0,
}) {
  const q = new URLSearchParams({ shop, shopify_customer_id: String(shopifyCustomerId), limit: String(limit), offset: String(offset) });
  if (crmCompanyId != null && String(crmCompanyId).trim()) q.set("company_id", String(crmCompanyId));
  if (b2bCompanyId != null && String(b2bCompanyId).trim()) q.set("shopify_company_id", String(b2bCompanyId));
  const r = await fetch(
    `${API_BASE}/api/v1/chat/public/conversations/${encodeURIComponent(conversationId)}/messages?${q}`,
    { headers: chatHeaders() }
  );
  const j = await r.json();
  if (!r.ok) throw new Error(j.message || r.statusText);
  return j.data; // { conversation_id, messages }
}
```

### Errores habituales (público)

- Con **`CHAT_STOREFRONT_API_KEY`** definida en Lambda: sin `X-Api-Key` o clave incorrecta → 401.  
- Ni **`company_id`** ni **`shopify_company_id`** en el request → 400.  
- `company_id` con formato inválido, o fila inexistente en `companies` → 400 / 404.  
- `shopify_company_id` inexistente en `companies.shopify_company_id` → 404.  
- Ambos enviados pero apuntan a **distintas** filas en `companies` → 404.  
- Empresa **resuelta** distinta de **`shopify_app_installations.company_id`** si esa columna **está rellenada** (modo “una sola” empresa vinculada a la tienda) → 404.  
- Solo `company_id` (UUID) pero **`shopify_app_installations.company_id` es `null`** (varias empresas vía B2B): → 404 (pedir B2B o rellenar la instalación).  
- `shop` o `shopify_customer_id` inválidos o conversación de otro cliente / otra empresa → 400 / 404.  
- Instalación **sin** fila en `shopify_app_installations` → 404.  
- (Ya no aplica el bloqueo por vendedor.) El mensaje del cliente se guarda aunque no haya aún vendedor; el hilo puede tener `seller_user_id` nulo hasta que alguien del staff responde (en la **primera** respuesta se asigna a quien escribe).

---

## 3. Prerrequisitos de datos (backend / operación)

1. **Stack `users` desplegado** (IAM de la Lambda importa el ARN del User Pool Cognito), igual que otros servicios con Cognito.  
2. **`DATABASE_URL`** y despliegue del servicio **`chat`** (ver sección Deploy).  
3. **`CHAT_STOREFRONT_API_KEY`**: opcional en desarrollo; recomendable en producción.  
4. **Tienda en `shopify_app_installations`**: con **una** empresa fija, rellenad **`company_id`** con el UUID de `companies` (el theme puede entonces validar con solo el metacampo/UUID, o con B2B que coincida). Con **varias** empresas B2B en la **misma** tienda, dejad **`shopify_app_installations.company_id` en `null`** y enrutad por **`shopify_company_id`** en el request (cada `companies` debe tener **`shopify_company_id`** poblado para esa Company B2B).  
5. (Opcional, recomendado) Tener al menos un usuario **`ACTIVE`** con rol **`ADMIN`** o **`SALES`** asociado a la empresa: si hay, los **nuevos** hilos usan a ese vendedor por defecto cuando aplica; si no hay nadie, el mensaje del cliente se guarda igual (hilo en cola, `seller_user_id` nulo) hasta la **primera respuesta** desde el panel.  

---

## 4. Modelo lógico (resumen)

- **Hilo** (`conversations`): une **empresa** (`company_id`) y **cliente** (`client_id`). El **vendedor** (`seller_user_id`) puede ser `null` mientras el hilo no lo haya “tomado” nadie; al **primera respuesta** del staff se asigna a quien escribe.  
- **Mensajes** (`messages`): `sender_type` = `USER` (respuesta del panel) o `CLIENT` (tienda).  
- Desde el theme, si existe un `ADMIN`/`SALES` activo, se usa como vendedor en **nuevos** hilos; si no, el mensaje del cliente se guarda y queda a la espera.

---

## 5. Deploy

```bash
npm run shared:sync
npm run deploy:service -- chat
```

Otro stage (p. ej. `prod`): `npm run deploy:service -- chat prod`.

Tras el deploy, actualizad la **URL base** en el theme (settings) y en el admin (variable de entorno del frontend).

---

## Referencia cruzada

- **Checklist y correcciones (theme + admin):** [GUIA_FRONTEND_CHAT_EXTENSION_ADMIN.md](./GUIA_FRONTEND_CHAT_EXTENSION_ADMIN.md).  
- Registro de empresa (patrón similar de API pública + panel): [COMPANY_REGISTRATION_SERVICE.md](./COMPANY_REGISTRATION_SERVICE.md).  
- Proceso de desarrollo: [AGENTS.md](../AGENTS.md).

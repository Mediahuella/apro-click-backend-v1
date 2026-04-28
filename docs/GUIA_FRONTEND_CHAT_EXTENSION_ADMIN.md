# Guía — Chat (theme app extension + panel admin)

Esta guía es para **alinear o corregir** la **Theme App Extension** (tienda) y el **front admin** (`aproclick-frontend` u otro SPA) con el servicio de chat. La referencia de API completa sigue en [CHAT_SERVICE_INTEGRATION.md](./CHAT_SERVICE_INTEGRATION.md).

**Servicio backend:** `chat` (Serverless `apro-click-admin-chat`).

**URL base:** obtenedla con `npm run sls:service -- chat info --verbose` (HttpApiUrl) y configuradla en **variable de entorno** del admin y en **settings** o **metadatos** de la extensión (no commitear URLs de producción con secretos en repos públicos).

---

## 1. Qué corregir — checklist rápida

| Capa | Qué verificar |
|------|----------------|
| **Extensión** | Cada `POST` / `GET` público envía **al menos uno** de: `company_id` (UUID de `companies.id`) **o** `shopify_company_id` (B2B / sesión), **más** `shop` y `shopify_customer_id`. |
| **Extensión** | **Solo UUID fijo (metacampo / `custom.apro_crm_company_id`, setting):** coherente con `shopify_app_installations` si la instalación fija **una** empresa, o acompañad de B2B en tiendas con varias empresas (instalación con `company_id` null y filas en `companies` con `shopify_company_id`). |
| **Extensión** | **Solo B2B:** en Liquid, el id de compañía de la **sesión** (cambia si el comprador cambia de compañía) → `shopify_company_id` en el body/query. La clave de `localStorage` del hilo debe incluir **empresa** (p. ej. `company_id` + B2B) para no mezclar al cambiar de compañía. |
| **Extensión** | El mensaje/validación de error en el bloque: mostrarlo si **faltan** ambos: ni UUID de CRM ni id B2B. |
| **Extensión** | Solo comercio **logueado** (`customer` en Liquid). Visitantes anónimos: deshabilitar o mensaje. |
| **Extensión** | Header `X-Api-Key` si `CHAT_STOREFRONT_API_KEY` está definida en la Lambda. |
| **Extensión** | Guardar `conversation_id` (y, si aplica, `company_id` / B2B devueltos en `data`) al cargar historial. |
| **Admin** | Todas las rutas bajo el mismo `API_BASE` del servicio `chat` (puede ser distinto al de `company-registration`). |
| **Admin** | `Authorization: Bearer` con el **access token** de Cognito (mismo mecanismo que otras pantallas). |
| **Admin** | Usuarios **SUPERADMIN** sin `company_id` en sesión/BD: enviar **`?company_id=<uuid>`** en listar hilos, ver mensajes y enviar. |
| **Admin** | **SALES** (y alcance de pedidos): el backend usa **`order_company_ids`** (tabla `user_companies` + `users.company_id`); la empresa plataforma (`companies.is_system`) **no** cuenta para el alcance. Ver §3.4. |

---

## 2. Theme app extension (Shopify)

### 2.1 Configuración que debe exponer el bloque o la app

| Dato | Uso en código |
|------|----------------|
| `api_base` (URL del HTTP API) | Base sin barra final → `${api_base}/api/v1/chat/...` |
| `api_key` (opcional) | Misma que `CHAT_STOREFRONT_API_KEY` en la Lambda, header `X-Api-Key` |
| `crm_company_id` (opcional) | UUID = **`companies.id`**; metacampo p. ej. `custom.apro_crm_company_id` o setting del bloque, cuando haya **una** empresa fija o valor por tienda. |
| (sin setting) B2B | Id de compañía de la sesión en Liquid, según plantilla: enviar como `shopify_company_id` (numérico o GID). El backend resuelve con `companies.shopify_company_id`. |

> **`companies.id` (UUID)** y **`companies.shopify_company_id` (B2B)** son distintos. El chat acepta **ambas** vías: podéis enviar `company_id` solo, `shopify_company_id` solo, o **ambas** (deben apuntar a la misma fila en CRM).

### 2.2 Datos a inyectar desde Liquid

| Dato | Origen Liquid típico | Incorporación a JS |
|------|------------------------|--------------------|
| Tienda | `{{ shop.permanent_domain }}` | `shop` en body/query |
| Cliente (requerido) | `{{ customer.id }}` | `shopify_customer_id` (solo si `{% if customer %}`) |
| Empresa CRM fija | Metafield / `{{ block.settings.crm_company_id }}` | `company_id` en body/query, si aplica |
| B2B sesión | Id compañía actual (p. ej. `customer` + contexto B2B según theme) | `shopify_company_id` (si aplica) |

### 2.3 Contrato que debe cumplir el `fetch` del theme

**Enviar mensaje**

- `POST /api/v1/chat/public/messages`
- JSON: `shop`, al menos **uno** de `company_id` o `shopify_company_id`, `shopify_customer_id`, `body` (y opcionales `email` / `name`).

**Cargar historial**

- `GET /api/v1/chat/public/conversations/{conversation_id}/messages?...`
- **Obligatorios:** `shop`, al menos `company_id` **o** `shopify_company_id`, `shopify_customer_id` (misma coherencia que al crear el hilo).

### 2.4 Persistencia del hilo

1. Tras un `201`, leed `data.conversation_id`, `data.company_id` y `data.shopify_company_id` (estos dos últimos ayudan a armar claves y revalidar en `GET`).
2. **Clave de almacenamiento** (ej. `localStorage`): incluid `shop` + `customer` + **contexto de empresa** (`company_id` y/o B2B) para no reutilizar un hilo de otra compañía al cambiar de sesión B2B.
3. Mismo `conversation_id` reabre el hilo mientras vendedor/cliente/empresa sigan alineados con el backend.

### 2.5 Errores frecuentes (extensión)

| Síntoma | Causa probable |
|--------|-----------------|
| 401 en público | Falta o mal `X-Api-Key` con la clave de Lambda. |
| 400 | Falta **ambos** `company_id` y `shopify_company_id` en el request. |
| 404 "no corresponde a la tienda" / instalación | Empresa resuelta no coincide con `shopify_app_installations.company_id` si esa columna está fijada, o requiere B2B y solo mandasteis UUID (varias empresas). |
| 404 "no existe la empresa" | UUID o B2B inexistente en `companies` (revisar `companies.shopify_company_id` para B2B). |
| 404 "no registrada en shopify_app_installations" | Dominio de `shop` no coincide con el guardado (normalizad a `*.myshopify.com`). |
| (histórico) | Antes fallaba sin vendedor; ahora el mensaje se acepta y el listado puede mostrar `seller_user_id: null` hasta el primer cierre. |

### 2.6 CORS y entorno

El API trae CORS permisivo en el gateway; comprobad en navegador que la `api_base` es **https** y que no mezcláis stage `dev` / `prod` con datos de otra base.

---

## 3. Panel admin (SPA)

### 3.1 Configuración

| Requisito | Notas |
|------------|--------|
| **Base URL** | Variable de entorno (p. ej. `VITE_CHAT_API_URL` o un solo `VITE_API_BASE` si apuntan al mismo origen) — debe ser el **HttpApiUrl** del stack `chat`. |
| **Auth** | Mismo `Authorization: Bearer` que el resto del admin (Cognito). |
| **Roles** | Pueden usar el chat: `SUPERADMIN`, `ADMIN`, `SALES`. Otros: mostrar 403 o ocultar el módulo. |

### 3.2 Llamadas a implementar o corregir

| Acción de UI | Método y ruta | Query / body | Detalle a corregir |
|-------------|----------------|--------------|---------------------|
| Bandeja de hilos | `GET /api/v1/chat/conversations` | `limit`, `offset`, `status` opcional | **SUPERADMIN** sin `company_id` de usuario: añadid **`company_id` obligatorio** (selector de empresa o contexto fijo). |
| Mensajes del hilo | `GET /api/v1/chat/conversations/{id}/messages` | `limit`, `offset` | Mismo criterio de **`?company_id=`** para superadmin sin empresa. |
| Responder | `POST` mismo path | Body `{ "body": "texto" }` | Mismo criterio de **`company_id`** en query. |

`status` al listar: solo `OPEN` o `CLOSED` si se filtra.

### 3.3 Superadmin sin empresa

Si el token representa a un `SUPERADMIN` que **no** tiene `company_id` en la app:

- No omitáis `company_id` en las peticiones: el backend lo exige en ese escenario.
- En la UI: desplegable de empresas (cargar desde vuestro endpoint de companies u otra fuente) y pasar su UUID en cada `GET` / `POST` de chat.

### 3.4 Alcance multi-empresa: SALES, `order_company_ids` y chat

El listado **`GET /api/v1/chat/conversations`** (y el acceso a mensajes) filtra por **`conversations.company_id`** igual que los pedidos filtran por **`shopify_orders.company_id`**: el usuario solo ve hilos cuyo `company_id` está en su **alcance de empresas**.

**Cómo lo calcula el backend (resumen para el front):**

| Fuente en BD | Uso |
|--------------|-----|
| Tabla **`user_companies`** (N:N usuario ↔ empresa) | Lista principal de empresas asignadas al usuario (orden estable). |
| Columna **`users.company_id`** | Se añade al alcance si no estaba ya en `user_companies` (compatibilidad con datos anteriores al M2M). |
| Empresa **plataforma** (`companies.is_system = true`) | Para roles **`SALES`** y **`KPI_VISUALIZERS`** se **excluye** del alcance: es la fila interna “Apro Click”, no un comercio B2B. Las conversaciones reales siempre cuelgan de empresas cliente. |

**Roles:**

- **`SUPERADMIN`** y **`ADMIN`**: en backend se tratan como **administración plataforma** para el chat: ven todos los hilos si no pasáis `company_id`, o filtran con `?company_id=` cuando aplica (ver §3.3).
- **`SALES`**: alcance = `order_company_ids` tras la lógica anterior. **No** hace falta enviar `company_id` en query para listar: recibís los hilos de **todas** las empresas que tenga asignadas (M2M + `users.company_id` útil, sin contar la plataforma).

**Qué debe hacer el frontend:**

1. **Pantalla de chat (SALES):** llamad a `GET /api/v1/chat/conversations` **sin** forzar un `company_id` salvo queráis acotar a una empresa concreta **dentro** del alcance del usuario (opcional). Si enviáis `company_id`, debe ser uno de los UUID que el usuario tiene asignados; si no, el API responde error de permisos.
2. **Gestión de usuarios:** al dar de alta o editar vendedores, usad el API de **`users`** con **`company_ids`** (lista de UUID de `companies`) para rellenar `user_companies`. El listado `GET /api/v1/users` devuelve **`order_company_ids`** ya calculado con la misma regla que chat y pedidos (útil para mostrar en el panel qué empresas ve ese usuario).
3. **Errores:** si un `SALES` no tiene ninguna empresa en alcance tras excluir la plataforma (p. ej. sin filas en `user_companies` y solo `company_id` apuntando a la empresa sistema), el chat puede responder **401** con mensaje del estilo *“Sin empresas asignadas…”* — en UI, guiar al administrador a asignar empresas en el panel de usuarios.

**Paridad con pedidos:** el mismo arreglo `order_company_ids` alimenta el servicio **`orders`**; el front puede asumir que **empresa visible en pedidos = empresa visible en chat** para un mismo token.

### 3.5 Usuarios con empresa (ADMIN / SALES / SUPERADMIN con company)

- Para **SALES**: el alcance efectivo es el de §3.4, no solo el campo `company_id` del usuario en el DTO.
- Para **ADMIN** de plataforma: no hace falta `company_id` en query salvo el caso de §3.3.
- Asegurad que el cliente HTTP no añade un `company_id` erróneo (si lo enviáis, debe coincidir con la conversación o estar en el alcance del usuario).

### 3.6 Datos a mostrar

La API devuelve `client_id` y `seller_user_id` como UUID; para **nombres o email** haced join en front con vuestro API de `clients` / `users` o enriqueced en otra capa. El listado de conversaciones **no** incluye aún el nombre legible del cliente en el DTO mínimo del backend.

### 3.7 Tiempo real

No hay WebSocket: usad **polling** (cada 10–30 s) con el hilo abierto o al volver a la bandeja.

### 3.8 Errores frecuentes (admin)

| Síntoma | Causa probable |
|--------|-----------------|
| 401 | Token expirado o sin `Bearer`. |
| 403 "Sin permisos" | Rol no en `SUPERADMIN` / `ADMIN` / `SALES`. |
| 400 "Indique company_id" (superadmin) | Falta query `company_id`. |
| 404 al abrir hilo de otra empresa | `company_id` de query no coincide; usuario sin permiso. |
| Lista vacía (SALES) con token válido | Usuario sin empresas en alcance: revisar **`company_ids`** en API `users` / tabla `user_companies`; no basta con la empresa plataforma. |
| 401 “Sin empresas asignadas” | Mismo caso: administrador debe asociar al menos un comercio (UUID `companies.id`) al usuario. |

---

## 4. Orden de despliegue y datos

1. **Deploy** del servicio `chat` (backend) y anotar **HttpApiUrl**.  
2. **SQL / operación:** `shopify_app_installations.company_id` = UUID de `companies` de esa tienda.  
3. Poner en **Lambda** `CHAT_STOREFRONT_API_KEY` si usáis clave.  
4. **Extensión:** settings con `api_base`, `api_key` (si aplica), `crm_company_id`.  
5. **Admin:** variable de entorno con la **misma** base URL y flujo de token ya existente.  

---

## 5. Referencia cruzada

- **Contrato y ejemplos de `fetch`:** [CHAT_SERVICE_INTEGRATION.md](./CHAT_SERVICE_INTEGRATION.md)  
- **Patrón similar (público + admin + key):** [GUIA_FRONTEND_THEME_REGISTRO_EMPRESA.md](./GUIA_FRONTEND_THEME_REGISTRO_EMPRESA.md) (registro de empresa)  
- **Proceso de deploy backend:** [AGENTS.md](../AGENTS.md)  

# Guía — Pedidos Shopify en el panel admin

Documentación para implementar el módulo **Pedidos** en el SPA admin: listado, detalle, imágenes por línea, **notas de intervención** (CRM) y **edición en Shopify** (cantidades por SKU, método de envío) vía el mismo `PATCH`.

Los datos entran por **webhooks** `orders/*` desde Shopify; el panel puede **leer** y **escribir** según los permisos descritos abajo.

**Servicio backend:** `orders` (Serverless `apro-click-admin-orders`).

### URL base del API (dev)

Tras un deploy exitoso del servicio, la salida incluye `HttpApiUrl`, por ejemplo:

`https://4pc9lvxj23.execute-api.us-east-2.amazonaws.com`

En el front usad esa raíz **sin** barra final, p. ej. `VITE_ORDERS_API_BASE_URL`.

Para otro stage o perfil AWS:

```bash
npm run deploy:service -- orders <stage>
# o solo inspeccionar:
npm run sls:service -- orders info --verbose
```

Buscad `HttpApiUrl` en la salida. Si más adelante unificáis varios servicios bajo un solo API Gateway, la misma convención de rutas `/api/v1/orders/...` puede vivir en otra raíz.

---

## 1. Autenticación y roles

| Requisito | Valor |
|-----------|--------|
| Header | `Authorization: Bearer <access_token>` |
| Token | **Access token** de Cognito (el mismo que en usuarios, empresas, chat, etc.) |

### Roles y permisos

Definidos en el backend (`READ_ROLES` / `INTERVENTION_ROLES`):

| Rol | Ver listado / detalle | Filtrar por `?company_id=` | Notas CRM (`intervention_notes`) | Shopify (`shopify_line_items`, `shopify_shipping`, …) |
|-----|------------------------|----------------------------|-----------------------------------|--------------------------------------------------------|
| `SUPERADMIN`, `ADMIN` | Sí, todas las empresas | Sí, cualquier empresa | Sí si `internal_status === 'PENDING'` | Sí si puede leer el pedido |
| `SALES` | Sí, pedidos cuyo `shopify_orders.company_id` está en `user_companies` | Sí, **solo** si el UUID es uno de esos (mismo criterio que en el store `order_company_ids`) | Sí si `PENDING` y pedido de su empresa | Sí si puede leer el pedido |
| `KPI_VISUALIZERS` | Sí, mismo alcance lectura | Igual que `SALES` (solo empresas asignadas) | **No** | **No** |

- Sin rol de lectura → **401**.
- `PATCH` con notas sin permiso o pedido no `PENDING` → **403**.
- `PATCH` con campos `shopify_*` sin rol `SUPERADMIN`/`ADMIN`/`SALES` (p. ej. KPI) → **403**.
- Pedidos sin `company_id` en BD: solo visibles/edición para `SUPERADMIN`/`ADMIN` (según reglas de lectura).

---

## 2. Contrato de respuesta HTTP

Las rutas exitosas devuelven cuerpo JSON con esta forma:

```json
{
  "statusCode": 200,
  "message": "OK",
  "data": { ... }
}
```

En listados, `data` es un **array** de pedidos. En detalle o PATCH, `data` es un **objeto** pedido.

Los errores siguen el comportamiento de **Lambda Powertools** + API Gateway (p. ej. mensaje en cuerpo y código 400/401/403/404/500 según el caso).

---

## 3. Endpoints

### 3.1 Health

```
GET /api/v1/health-orders
```

Respuesta típica: `{ "status": "healthy", "service": "..." }`.

### 3.2 Listar pedidos

```
GET /api/v1/orders
```

**Query parameters:**

| Parámetro | Obligatorio | Descripción |
|-----------|-------------|-------------|
| `limit` | No | Por defecto `50`. Máximo **200**. |
| `offset` | No | Por defecto `0`. Paginación clásica. |
| `status` | No | `PENDING` o `CLOSED`. Filtra por `internal_status`. |
| `company_id` | No | UUID de empresa. `SUPERADMIN`/`ADMIN`: cualquiera. Resto: **solo** un id que el usuario tenga en `user_companies` (en API suele ir como `order_company_ids` en el perfil). Si el uuid no está permitido, el listado devuelve `[]`. |

**Ejemplo:**

```http
GET /api/v1/orders?limit=25&offset=0&status=PENDING
Authorization: Bearer eyJ...
```

```http
GET /api/v1/orders?company_id=550e8400-e29b-41d4-a716-446655440000
Authorization: Bearer eyJ...
```

#### Perfil CRM en el front (p. ej. Zustand `setCrmProfile`)

El backend **no** lee el estado del cliente: el alcance lo calcula con el **Bearer** (Cognito → `users` → filas en `user_companies`). Eso produce el mismo conjunto que guardáis como `order_company_ids` en el perfil, si el store está sincronizado con la API de usuarios / sesión.

| Querés en la UI | Qué pedir al API |
|-----------------|------------------|
| Todos los pedidos de **todas** las empresas asignadas al usuario | `GET /api/v1/orders?...` **sin** `company_id` (solo `limit`, `offset`, `status` si aplica). |
| Solo la empresa **seleccionada** en el CRM | `GET /api/v1/orders?company_id=<uuid>&...` donde `<uuid>` es un id que figure en `payload.order_company_ids` **o** la empresa de contexto que sea una de esas. |
| `payload.company_id` es el `users.company_id` (p. ej. empresa sistema) y **no** está en `order_company_ids` | **No** uses ese valor para filtrar pedidos: los pedidos van por `shopify_orders.company_id` ∈ `user_companies`. Filtrar con el uuid “principal” del usuario puede devolver **lista vacía**. Elegí un `company_id` de la lista M2M o no enviés el parámetro. |

No hace falta (ni es recomendable) enviar `order_company_ids` en el cuerpo: el servidor no los usa para autorizar; la fuente de verdad es la BD detrás del token.

**Ejemplo (query a partir del store):** si `selectedCompanyId` es la empresa activa y debe ser una de `order_company_ids`, construí la URL solo cuando `selectedCompanyId` esté en ese array; si el usuario mira “todas”, omití `company_id`.

### 3.3 Detalle de un pedido

```
GET /api/v1/orders/{order_id}
```

`order_id` es el **UUID interno** del registro (`id` en la respuesta), **no** el id de Shopify.

### 3.4 Imágenes destacadas por línea (producto)

```
GET /api/v1/orders/{order_id}/line-images
Authorization: Bearer <access_token>
```

Respuesta: `data` es un objeto **`{ [product_id: string]: url | null }`** (URL de imagen destacada en Shopify por cada `product_id` presente en `line_items`). Requiere token de tienda en la instalación OAuth.

**Uso en el admin:** con `fetch` + `Authorization` (no podéis poner esta URL directa en `<img src>` si el endpoint exige Bearer). Opciones: pedir JSON y pintar `<img src={url}>` con las URLs devueltas, o blobs si más adelante servís binario.

### 3.5 Intervención y edición sincronizada con Shopify

```
PATCH /api/v1/orders/{order_id}
Content-Type: application/json
```

Podés enviar **una o varias** claves en el mismo cuerpo.

#### Notas internas (solo CRM)

```json
{
  "intervention_notes": "Texto libre para el equipo..."
}
```

- Roles: `SUPERADMIN`, `ADMIN`, `SALES`.
- Solo si `internal_status === "PENDING"`.
- `intervention_notes`: `null` o string (vacío → `null`). Máx. **16000** caracteres.

#### Cantidades por SKU + envío (Shopify Order Edit API)

Los cambios se aplican en **Shopify** y luego se **refresca** el registro en el CRM (REST + mismo flujo que el webhook).

```json
{
  "shopify_line_items": [
    { "sku": "MI-SKU-01", "quantity": 2 }
  ],
  "shopify_shipping": {
    "title": "Envío express",
    "price": "9.99"
  },
  "shopify_restock_on_decrease": true,
  "shopify_staff_note": "Opcional: nota interna en el commit del pedido en Shopify"
}
```

| Campo | Descripción |
|-------|-------------|
| `shopify_line_items` | Lista de `{ "sku", "quantity" }`. La cantidad es la **nueva** cantidad (0 quita la línea en Shopify). El SKU debe existir en el pedido y estar informado en la variante en Shopify. |
| `shopify_shipping` | Opcional. El backend **elimina todas** las líneas de envío actuales del pedido en Shopify y **agrega una sola** con el `title` y `price` indicados (moneda = `currency` del pedido en CRM). Si el pedido no tenía envío, solo se agrega la nueva línea. |
| `shopify_restock_on_decrease` | Por defecto `true`: al bajar cantidad, Shopify puede reponer inventario. |
| `shopify_staff_note` | Opcional; se envía al `orderEditCommit` de Shopify. |

- Roles: `SUPERADMIN`, `ADMIN`, `SALES` (misma visibilidad que lectura del pedido). **No** `KPI_VISUALIZERS`.
- No exige `PENDING` solo por esta parte: Shopify decide si el pedido admite edición (pago pendiente, cumplidos, etc.).
- Requiere instalación OAuth con, como mínimo: **`read_orders`**, **`write_order_edits`**, **`read_order_edits`**, y **`read_products`** si el admin puede **añadir** líneas por SKU (`orderEditAddVariant` tras resolver variante con `productVariants`). Token válido en `shopify_app_installations`.
- Las operaciones GraphQL del servicio `orders` están alineadas con el schema **Admin API** documentado en [shopify.dev](https://shopify.dev/docs/api/admin-graphql/latest/objects/CalculatedOrder) (p. ej. `CalculatedOrder.lineItems` como connection con `nodes`, `shippingLines` como lista).

Si editás el pedido **solo en el admin de Shopify**, el webhook `orders/updated` sigue actualizando el CRM como hasta ahora.

#### Combinar notas + Shopify

En un solo `PATCH` podés mandar `intervention_notes` y campos `shopify_*`. Las notas CRM siguen exigiendo `PENDING`; la parte Shopify valida permisos de edición en tienda por separado.

Tras cualquier escritura exitosa se actualiza `last_intervened_by_user_id`.

---

## 4. Modelo de pedido (`data`)

Todos los campos siguen el `to_dict()` del ORM; conviene tiparlos en TypeScript como abajo.

| Campo | Tipo en API | Notas |
|-------|-------------|--------|
| `id` | `string` | UUID del registro en CRM (clave para `GET`/`PATCH`). |
| `shopify_order_id` | **`string`** | Id del pedido en Shopify. En BD y API es texto (p. ej. `"5678901234"`). Tratarlo siempre como string en el front. |
| `shop_domain` | `string` | Dominio tienda, p. ej. `mi-tienda.myshopify.com`. |
| `company_id` | `string \| null` | UUID empresa CRM; puede ser `null` si aún no se asocia. |
| `order_name` | `string` | Nombre humano tipo `#1001` o fallback `#<shopify_order_id>`. |
| `email` | `string \| null` | |
| `financial_status` | `string \| null` | Valores Shopify (p. ej. `paid`, `pending`). |
| `fulfillment_status` | `string \| null` | |
| `currency` | `string \| null` | Código ISO, p. ej. `CLP`. |
| `subtotal_price` | `string \| null` | Decimal serializado como string. |
| `total_price` | `string \| null` | Igual. |
| `internal_status` | `"PENDING" \| "CLOSED"` | `CLOSED` cuando `financial_status` es `paid` (regla backend). |
| `shopify_updated_at` | `string \| null` | ISO 8601. |
| `intervention_notes` | `string \| null` | |
| `last_intervened_by_user_id` | `string \| null` | UUID usuario. |
| `line_items` | `ShopifyOrderLineItem[]` | Ver §5. Siempre array (vacío si no hay). |
| `created_at` | `string` | ISO 8601. |
| `updated_at` | `string` | ISO 8601. |

### 4.1 Enlace al admin de Shopify

Para abrir el pedido en Shopify (nueva admin):

- Extraed **handle** de tienda: parte antes de `.myshopify.com` en `shop_domain`.
- URL típica: `https://admin.shopify.com/store/<handle>/orders/<shopify_order_id>`

Ejemplo: `shop_domain = "acme.myshopify.com"`, `shopify_order_id = "5678901234"` →  
`https://admin.shopify.com/store/acme/orders/5678901234`

Mantened `shopify_order_id` como string al concatenar.

---

## 5. Líneas de pedido (`line_items`)

Cada elemento es un objeto JSON con al menos:

| Campo | Tipo | Notas |
|-------|------|--------|
| `id` | **`string`** | Id de línea en Shopify; en API como texto. |
| `title` | `string` | |
| `name` | `string` | |
| `quantity` | `number` | Entero. |
| `sku` | `string \| null` | |
| `variant_title` | `string \| null` | |
| `product_id` | **`string \| null`** | |
| `variant_id` | **`string \| null`** | |
| `price` | `string \| null` | |
| `total_discount` | `string \| null` | |
| `vendor` | `string \| null` | |

Los ids (`id`, `product_id`, `variant_id`) deben mostrarse y almacenarse en estado local como **string** para evitar pérdida de precisión con enteros grandes.

---

## 6. Tipos TypeScript sugeridos

```typescript
// src/types/shopify-order.ts

export type InternalOrderStatus = 'PENDING' | 'CLOSED';

export interface ShopifyOrderLineItem {
  id: string;
  title: string;
  name: string;
  quantity: number;
  sku: string | null;
  variant_title: string | null;
  product_id: string | null;
  variant_id: string | null;
  price: string | null;
  total_discount: string | null;
  vendor: string | null;
}

export interface ShopifyOrder {
  id: string;
  shopify_order_id: string;
  shop_domain: string;
  company_id: string | null;
  order_name: string;
  email: string | null;
  financial_status: string | null;
  fulfillment_status: string | null;
  currency: string | null;
  subtotal_price: string | null;
  total_price: string | null;
  internal_status: InternalOrderStatus;
  shopify_updated_at: string | null;
  intervention_notes: string | null;
  last_intervened_by_user_id: string | null;
  line_items: ShopifyOrderLineItem[];
  created_at: string;
  updated_at: string;
}

export interface OrdersListResponse {
  statusCode: number;
  message: string;
  data: ShopifyOrder[];
}

export interface OrderDetailResponse {
  statusCode: number;
  message: string;
  data: ShopifyOrder;
}

/** Cuerpo del PATCH unificado */
export interface PatchShopifyLineItem {
  sku: string;
  quantity: number;
}

export interface PatchShopifyShipping {
  title: string;
  /** string o número; backend serializa a decimal */
  price: string | number;
}

export interface PatchOrderBody {
  intervention_notes?: string | null;
  shopify_line_items?: PatchShopifyLineItem[];
  shopify_shipping?: PatchShopifyShipping;
  shopify_restock_on_decrease?: boolean;
  shopify_staff_note?: string | null;
}

/** Mapa product_id → URL imagen o null */
export type LineImagesResponse = Record<string, string | null>;
```

---

## 7. Cliente HTTP (ejemplo)

Alineado con `FRONTEND_ADMIN_GUIDE.md` (axios + interceptor que añade el Bearer).

```typescript
// src/api/orders.api.ts
import type {
  InternalOrderStatus,
  PatchOrderBody,
  ShopifyOrder,
} from '../types/shopify-order';

const ORDERS_BASE = import.meta.env.VITE_ORDERS_API_BASE_URL; // sin slash final

export async function fetchOrders(params: {
  limit?: number;
  offset?: number;
  status?: InternalOrderStatus;
  companyId?: string;
  accessToken: string;
}): Promise<ShopifyOrder[]> {
  const q = new URLSearchParams();
  if (params.limit != null) q.set('limit', String(params.limit));
  if (params.offset != null) q.set('offset', String(params.offset));
  if (params.status) q.set('status', params.status);
  if (params.companyId) q.set('company_id', params.companyId);

  const res = await fetch(`${ORDERS_BASE}/api/v1/orders?${q}`, {
    headers: { Authorization: `Bearer ${params.accessToken}` },
  });
  if (!res.ok) throw new Error(await res.text());
  const body = await res.json();
  return body.data as ShopifyOrder[];
}

export async function fetchOrderById(
  orderId: string,
  accessToken: string,
): Promise<ShopifyOrder> {
  const res = await fetch(`${ORDERS_BASE}/api/v1/orders/${orderId}`, {
    headers: { Authorization: `Bearer ${accessToken}` },
  });
  if (!res.ok) throw new Error(await res.text());
  const body = await res.json();
  return body.data as ShopifyOrder;
}

export async function fetchOrderLineImages(
  orderId: string,
  accessToken: string,
): Promise<Record<string, string | null>> {
  const res = await fetch(
    `${ORDERS_BASE}/api/v1/orders/${orderId}/line-images`,
    { headers: { Authorization: `Bearer ${accessToken}` } },
  );
  if (!res.ok) throw new Error(await res.text());
  const body = await res.json();
  return body.data as Record<string, string | null>;
}

/** PATCH: notas CRM, y/o líneas Shopify por SKU, y/o envío */
export async function patchOrder(
  orderId: string,
  payload: PatchOrderBody,
  accessToken: string,
): Promise<ShopifyOrder> {
  const res = await fetch(`${ORDERS_BASE}/api/v1/orders/${orderId}`, {
    method: 'PATCH',
    headers: {
      Authorization: `Bearer ${accessToken}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await res.text());
  const body = await res.json();
  return body.data as ShopifyOrder;
}
```

---

## 8. Checklist de implementación en el admin

1. **Variables:** `VITE_ORDERS_API_BASE_URL` = `HttpApiUrl` del stack `apro-click-admin-orders` (sin `/` final).
2. **Auth:** mismo `access_token` Cognito que el resto del panel; interceptor axios/fetch con `Authorization: Bearer`.
3. **Rutas React:** p. ej. `/orders`, `/orders/:orderId` (el param es el UUID `id` del listado).
4. **Listado:** `GET /api/v1/orders` con paginación `limit`/`offset` y filtro `status`; opcional `company_id` para acotar a **una** empresa (admins: cualquiera; resto: solo ids en `order_company_ids` del perfil — ver §3.2 “Perfil CRM”).
5. **Detalle:** `GET /api/v1/orders/{id}`; botón “Abrir en Shopify” (§4.1).
6. **Imágenes:** `GET .../line-images` y cruzar por `line_items[].product_id` (ver §8.1 borde + refresco tras edición).
7. **Formulario notas:** visible solo si rol puede intervenir y `internal_status === 'PENDING'`; `patchOrder({ intervention_notes })`.
8. **Formulario edición Shopify:** filas por SKU (inputs cantidad) + opcional bloque “Envío” (`title`, `price`); enviar `shopify_line_items` / `shopify_shipping`; deshabilitar para `KPI_VISUALIZERS`. Mostrar mensajes de error del cuerpo (Shopify suele devolver 400 con texto claro).
9. **OAuth tienda:** scopes mínimos recomendados `read_orders`, `read_products`, `write_order_edits`, `read_order_edits`; reinstalar la app en la tienda si cambiáis la lista (ver `.env.example`).

### 8.1 Miniaturas de línea: borde y datos tras cerrar la edición

- **Bordesito:** evita que la foto “flote” sobre fondos claros/oscuros; con MUI suele bastar un borde sutil y `object-fit: cover`.

```tsx
// Ejemplo: celda de imagen al lado del SKU
<Box
  component="img"
  src={url ?? undefined}
  alt=""
  sx={{
    width: 48,
    height: 48,
    display: 'block',
    objectFit: 'cover',
    borderRadius: 1,
    border: '1px solid',
    borderColor: 'divider',
    bgcolor: 'action.hover',
  }}
/>
```

- **Imagen correcta después de cerrar el modal/drawer de edición Shopify:** al cambiar SKU/cantidades, el pedido que devuelve el `PATCH` trae `line_items` actualizados, pero el mapa `product_id → url` que obtuviste con `GET .../line-images` **sigue siendo el anterior** hasta que lo vuelvas a pedir. Tras un `patchOrder` **exitoso**, antes de cerrar el diálogo (o en el mismo flujo de cierre):

  1. `const order = await fetchOrderById(orderId, token)` (o usá el cuerpo del PATCH si ya incluye todo lo que mostrás).
  2. `const images = await fetchOrderLineImages(orderId, token)`.
  3. Actualizá el estado del detalle con **ambos** (`order` + `images`).

Si usás TanStack Query: `invalidateQueries` para la query del pedido **y** para `line-images` del mismo `orderId` al completar el PATCH. Sin eso, al cerrar la edición seguirás viendo miniaturas viejas o `undefined` para líneas nuevas.

---

## 9. Sugerencias de UI (MUI)

- **Listado (`/orders`):** `DataGrid` o `Table` con columnas: `order_name`, `shop_domain`, `shopify_order_id`, `financial_status`, `internal_status`, `total_price` + `currency`, `shopify_updated_at`, `company_id` (solo admins o tooltip).
- **Filtros:** chips o `Select` para `PENDING` / `CLOSED`; `Autocomplete` de empresa (solo ids ∈ `order_company_ids` del usuario, o todos si es plataforma) + query `company_id` cuando haya una empresa elegida.
- **Detalle:** drawer o página con resumen monetario (parsear precios con `decimal.js` o similar si calculáis), tabla de `line_items` (SKU, título, cantidad, precio).
- **Shopify:** botón “Abrir en Shopify” usando la URL del §4.1.
- **Intervención (CRM):** `TextField` multiline solo si aplica permiso **y** `internal_status === 'PENDING'`.
- **Edición Shopify:** sección “Sincronizar con Shopify”: tabla editable de `line_items` con columna cantidad indexada por **SKU** (validar SKU no vacío en filas que se envían); opcional `TextField` título envío + precio. Botón “Guardar cambios en Shopify” llama a `patchOrder` con `shopify_line_items` / `shopify_shipping`. Confirmación si el pedido está `paid` (Shopify puede generar saldo / reembolso). Tras éxito, refrescar pedido + `line-images` (§8.1).
- **Miniaturas en la tabla de líneas:** mismo tratamiento de borde que §8.1 para consistencia con el listado del modal de edición.
- **KPI_VISUALIZERS:** ocultar formularios de escritura; solo lectura.
- **Paginación:** `limit`/`offset`; mostrar “Cargar más” o paginador numérico.

---

## 10. Errores frecuentes

| Síntoma | Causa probable |
|--------|----------------|
| 401 en todas las rutas | Token ausente, expirado o rol fuera de `READ_ROLES`. |
| Listado vacío (no admin) | Pedidos sin `company_id` o de otra empresa; instalación Shopify sin vínculo a empresa. |
| 403 al guardar notas | Rol `KPI_VISUALIZERS`, pedido ya `CLOSED`, o pedido de otra empresa. |
| 404 en detalle | UUID de `order_id` inválido o pedido no visible por alcance. |
| 400 con mensaje Shopify | SKU inexistente, pedido no editable en Shopify, envío sin línea, o scopes OAuth sin `write_order_edits` / `read_order_edits`. |

---

## 11. Referencias

- Guía general del admin: [FRONTEND_ADMIN_GUIDE.md](./FRONTEND_ADMIN_GUIDE.md)
- Desarrollo y deploy backend: [AGENTS.md](../AGENTS.md)

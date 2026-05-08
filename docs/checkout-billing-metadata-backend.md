# Backend: datos de facturación en checkout (`billing-metadata`)

Este documento describe cómo debe comportarse la API que atiende `POST /api/checkout/billing-metadata`, consumida desde la Checkout UI Extension **Manejo Factura**. La implementación en este monorepo backend es el servicio **apro-click-admin-shopify** (Lambda + HTTP API). Podés seguir usando la misma ruta en una app Next.js si preferís.

## Rol en el flujo

1. En checkout, la extensión arma un “snapshot” de **campaña/descuentos** y envía **`shop`**, **`shopify_company_id`** (Company B2B en Shopify), **`campaign`** (y opcional `checkout_token`).
2. El backend busca la fila **`companies`** con ese `shopify_company_id`, lee campos **`billing_*`** y un **vendedor** (`users.codigo_sap`, rol SALES asociado a la compañía) y arma **`note_attributes`**.
3. La extensión escribe cada par como **atributo del carrito** con `applyAttributeChange`; al completarse el pedido esos valores aparecen como **`note_attributes`** en la orden (misma `name`/`value`).

Los atributos que la extensión intenta escriturar después de llamar al API incluyen también:

- `blue_method_from_cart_selected`: `shipping` o `pickup` (inferido desde el checkout).
- `from_cart`: `true`.

Las claves típicas de facturación (las que vos devolvés desde el backend) pueden alinearse con:

- `Documento`, `Rut`, `Razon Social`, `Giro`, `Region`, `Dirección`, `Vendedor`, etc.

## HTTP

| Aspecto       | Valor |
|---------------|-------|
| Métodos       | `POST` (principal), `OPTIONS` (preflight CORS desde la extensión) |
| Ruta esperada por la extensión | `{billing_api_base_url}/api/checkout/billing-metadata` |
| Content-Type  | `application/json` |

### CORS

La extensión corre en un Web Worker sin origen fijo. El servidor debe responder con al menos:

- `Access-Control-Allow-Origin: *`
- `Access-Control-Allow-Methods: POST, OPTIONS`
- `Access-Control-Allow-Headers: authorization, content-type`

(`authorization` puede figurar en preflight aunque no enviés Bearer obligatorio.)

En `OPTIONS`, `204 No Content` con esas cabeceras es suficiente.

### Autenticación

**No es obligatoria** para el handler desplegado en este backend: basta con **CORS abierto** y el cuerpo JSON. La extensión puede llamar con `fetch` sin cabecera `Authorization`.

**Consideración:** sin JWT de sesión de Shopify, el **`shop` del body no está criptográficamente atado a la tienda**; cualquier cliente podría enviar otro dominio. Si más adelante necesitás asegurar tienda o datos sensibles, volvé a exigir `Authorization: Bearer <session_token>` y validación en servidor.

## Cuerpo de la petición (`POST`)

```typescript
type BillingDiscountAllocationPayload =
  | { type: 'code'; code: string }
  | { type: 'automatic'; title: string }
  | { type: 'custom'; title: string }

type BillingMetadataRequestBody = {
  /** Dominio `.myshopify.com` del checkout */
  shop: string
  /**
   * Id de **Company** B2B en Shopify (`companies.shopify_company_id`), string numérico
   * o `gid://shopify/Company/...`.
   */
  shopify_company_id: string
  /** Opcional; token estable del checkout cuando existe */
  checkout_token?: string
  campaign: {
    discount_codes: string[]
    discount_allocations: BillingDiscountAllocationPayload[]
  }
}
```

### Semántica de `campaign`

- **`discount_codes`**: Códigos de descuento actualmente aplicados en el checkout (puede haber repetidos en origen; el cliente deduplica al armar payloads internos donde aplica).
- **`discount_allocations`**: Lista de aplicaciones de descuento en el carrito:
  - **`code`**: descontador por código (incluye el string `code`).
  - **`automatic`**: descuento automático con `title` (título configurado por el merchant).
  - **`custom`**: descuento vía Shopify Functions u origen similar, también con `title`.

Usá esta información para correlacionar con campañas, listas en ERP, matrices “código/automático ↔ datos de facturación”, etc.

### `shopify_company_id`

- Debe coincidir con **`companies.shopify_company_id`** en Postgres (mismo id que la **Company** en Shopify B2B).
- Podés enviar el id numérico como string o el GID `gid://shopify/Company/<id>`.
- Si no hay fila con ese id, o si la instalación de la app para `shop` tiene `company_id` distinto al de la company encontrada (inconsistencia de datos), la API responde **`200`** con **`note_attributes: {}`** y **`billing: {}`**.

## Cuerpo de la respuesta (`200 OK`)

La extensión usa **`note_attributes`** para **`applyAttributeChange`**. Además el backend devuelve **`billing`**: el mismo contenido con **claves estables** (`snake_case`) para pantallas, logs o validaciones sin parsear los nombres de atributo de Shopify.

```typescript
type BillingMetadataResponseBody = {
  note_attributes: Record<string, string>
  /** Misma información con claves estables; sólo incluye claves con valor. */
  billing: {
    documento?: string
    rut?: string
    razon_social?: string
    giro?: string
    region?: string
    direccion?: string
    /** Código SAP / vendedor */
    vendedor?: string
  }
}
```

Ejemplo:

```json
{
  "note_attributes": {
    "Documento": "Factura",
    "Rut": "76486379-8",
    "Razon Social": "Productos y servicios Agua Pura Chile SpA",
    "Giro": "Comercializacion",
    "Region": "RM",
    "Dirección": "Rosas 2082",
    "Vendedor": "000"
  },
  "billing": {
    "documento": "Factura",
    "rut": "76486379-8",
    "razon_social": "Productos y servicios Agua Pura Chile SpA",
    "giro": "Comercializacion",
    "region": "RM",
    "direccion": "Rosas 2082",
    "vendedor": "000"
  }
}
```

Si no hay datos para una clave en particular, simplemente no la incluyas en cada objeto; la extensión sólo aplicará las claves que conoce su lista configurada las que lleguen definidas tras el merge con `blue_method_from_cart_selected` y `from_cart`.

## Errores

Convenciones útiles para el cliente (la extensión muestra mensajes genéricos al comprador pero logueá el detalle en servidor):

| HTTP | Cuándo |
|------|--------|
| `400` | JSON inválido, falta `shop` / `shopify_company_id`, falta o forma inválida de `campaign` |
| `500` | Error interno (DB, timeouts) |
| `502` | Upstream opcional |

Cuerpo sugerido: `{ "error": "mensaje breve técnico" }`.

## Cambios en la Checkout UI Extension (si antes enviabas JWT)

- Podés **omitir** `shopify.sessionToken.get()` y la cabecera `Authorization` en el `fetch` a este endpoint.
- El **body** incluye `shop`, **`shopify_company_id`**, `campaign`, `checkout_token` opcional.

## Implementación en este repo (serverless)

- Handler: `src/services/shopify/handlers/checkout_billing_metadata.py`
- Lógica: `src/services/shopify/services/billing_metadata_service.py` → `resolve_billing_for_checkout` (`note_attributes` + `billing`)

## Deployment

1. Misma ruta relativa **`/api/checkout/billing-metadata`** **o** ajustá **`billing_api_base_url`** en el Checkout Editor.
2. CORS como arriba.
3. Body JSON según `BillingMetadataRequestBody`; respuesta **`note_attributes`** y **`billing`**.

Para desplegar: `npm run deploy:service -- shopify` (y `company-registration` si cambiaste alta/aprobación de empresas).

Guía paso a paso para la Checkout UI Extension (URL base, settings, fetch, `applyAttributeChange`): [`GUIA_FRONTEND_CHECKOUT_BILLING_METADATA.md`](./GUIA_FRONTEND_CHECKOUT_BILLING_METADATA.md).

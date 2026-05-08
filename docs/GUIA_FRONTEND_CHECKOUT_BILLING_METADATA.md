# Guía frontend: Checkout UI Extension — facturación (`billing-metadata`)

Integración de la extensión **Manejo Factura** (u otra Checkout UI Extension) con el backend serverless que expone `POST /api/checkout/billing-metadata`.

Contrato detallado del API: [`checkout-billing-metadata-backend.md`](./checkout-billing-metadata-backend.md).

---

## 1. Deploy del backend (ya hecho para dev)

Desde la raíz del repo backend:

```bash
npm run shared:sync
npm run deploy:service -- shopify
```

Opcional otro stage: `npm run deploy:service -- shopify prod`.

Al terminar, en la salida de Serverless buscá **Stack Outputs** o **endpoints**:

| Uso en la extensión | Valor |
|---------------------|--------|
| **`billing_api_base_url`** (Checkout Editor / settings del bloque) | **`HttpApiUrl`** sin barra final, p. ej. `https://b39p9d9es5.execute-api.us-east-2.amazonaws.com` |
| URL completa del fetch | `{billing_api_base_url}/api/checkout/billing-metadata` |

Ejemplo de endpoint desplegado (dev):

- `POST https://<HttpApiId>.execute-api.us-east-2.amazonaws.com/api/checkout/billing-metadata`
- `OPTIONS` en la misma ruta (CORS preflight).

Si cambiás de cuenta, región o stage, la URL cambia: volvé a copiar `HttpApiUrl` tras cada deploy.

---

## 2. Configuración en Shopify (Checkout Editor)

1. Abrí **Settings → Checkout → Customize** (o el editor de checkout vuestro).
2. Seleccioná el bloque de la extensión que llama al API.
3. En la configuración del bloque (metacampos definidos en `shopify.extension.toml`), asigná **`billing_api_base_url`** = `HttpApiUrl` del deploy (solo el host del API, **sin** `/api/checkout/...`).

La extensión debe concatenar: `` `${billing_api_base_url}/api/checkout/billing-metadata` ``.

---

## 3. Qué envía la extensión (sin JWT)

No hace falta `shopify.sessionToken.get()` ni cabecera `Authorization` para el despliegue actual del backend.

**POST** con `Content-Type: application/json` y cuerpo:

```ts
type BillingMetadataRequestBody = {
  shop: string; // dominio `*.myshopify.com`
  /** Id de Company B2B en Shopify (numérico o `gid://shopify/Company/...`) */
  shopify_company_id: string;
  checkout_token?: string;
  campaign: {
    discount_codes: string[];
    discount_allocations: Array<
      | { type: 'code'; code: string }
      | { type: 'automatic'; title: string }
      | { type: 'custom'; title: string }
    >;
  };
};
```

- **`shop`**: el dominio de la tienda en formato hostname, p. ej. `tu-tienda.myshopify.com`. En Checkout UI Extensions suele obtenerse del API de Shopify (p. ej. `myshopifyDomain` u objeto `shop` según la versión del paquete `@shopify/ui-extensions`).
- **`shopify_company_id`** (obligatorio): identificador de la **company** B2B en Shopify para la que cerrás el checkout. Suele estar disponible en el contexto de checkout B2B (p. ej. company asociada al comprador corporativo): enviadlo como **string numérico** o como **GID completo**; el backend lo normaliza y lo compara con `companies.shopify_company_id` en Postgres.
- **`checkout_token`**: opcional; si la API de checkout lo expone, enviarlo ayuda al backend a correlacionar.
- **`campaign`**: descuentos actuales (códigos + asignaciones). El backend puede usarlos en reglas futuras; hoy resuelve facturación y vendedor desde la **`companies`** vinculada al `shopify_company_id`.

**Datos que el admin debe tener cargados en el CRM**: en la empresa (`companies`) el mismo `shopify_company_id`, campos opcionales `billing_*`, y un usuario **SALES** `ACTIVE` con `codigo_sap` enlazado a esa compañía (`users.company_id` o tabla `user_companies`) para rellenar **`Vendedor`** en los note attributes.

---

## 4. Respuesta esperada y escritura en el carrito

El backend responde **200** con:

```json
{
  "note_attributes": { "Clave": "valor", ... },
  "billing": {
    "rut": "...",
    "razon_social": "...",
    "giro": "...",
    "region": "...",
    "direccion": "...",
    "vendedor": "..."
  }
}
```

En ambos objetos **sólo vienen claves con datos**. **`note_attributes`** usa los nombres que espera Shopify para los note attributes; **`billing`** repite la información con claves estables para la UI.

La extensión debe:

1. Hacer **`fetch`** al endpoint (manejar errores de red y HTTP ≠ 200 de forma amigable; loguear detalles en consola solo en desarrollo).
2. Parsear JSON y leer **`note_attributes`**.
3. Por cada par `(name, value)` que quieran persistir en la orden, llamar **`applyAttributeChange`** (u API equivalente) para fijar **note attributes** del checkout con ese `name` y `value`.

Además, según vuestro flujo, pueden seguir escribiendo atributos propios como `blue_method_from_cart_selected` y `from_cart` (ver documento del backend).

---

## 5. Ejemplo mínimo de `fetch` (patrón)

Ajustá imports y lectura de `shop` / descuentos a la versión de `@shopify/ui-extensions` que use el proyecto.

```ts
const baseUrl = settings.billing_api_base_url.replace(/\/$/, '');
const url = `${baseUrl}/api/checkout/billing-metadata`;

const res = await fetch(url, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    shop: shopDomain,
    shopify_company_id: shopifyCompanyId,
    checkout_token: checkoutToken, // si existe
    campaign: {
      discount_codes: discountCodes,
      discount_allocations: discountAllocations,
    },
  }),
});

if (!res.ok) {
  // mensaje genérico al comprador + log técnico
  throw new Error(`billing-metadata ${res.status}`);
}

const data = await res.json() as { note_attributes: Record<string, string> };
// Iterar data.note_attributes y applyAttributeChange...
```

---

## 6. CORS y preflight

El API responde con CORS permisivo (`Access-Control-Allow-Origin: *`, métodos `POST, OPTIONS`). El navegador puede enviar preflight **`OPTIONS`**; no tenéis que llamar OPTIONS a mano desde la extensión.

---

## 7. Seguridad (consciente)

Sin Bearer/JWT, **`shop` en el body no está firmado**: el backend confía en el string que envía el cliente. Para datos de facturación sensibles o por-tienda muy estrictos, valorad volver a validar sesión en servidor (documentado en el backend como opción futura).

---

## 8. Checklist rápido

- [ ] `npm run deploy:service -- shopify` y copiar **`HttpApiUrl`** a **`billing_api_base_url`** en el checkout.
- [ ] URL final = `HttpApiUrl` + `/api/checkout/billing-metadata`.
- [ ] Body con `shop`, **`shopify_company_id`**, `campaign`; opcional `checkout_token`.
- [ ] Sin `Authorization` (salvo que el backend vuelva a exigirlo).
- [ ] Aplicar **`note_attributes`** al checkout con **`applyAttributeChange`** (o API actual equivalente).

---

## 9. Probar sin extensión

Migración aplicada + Lambda redeployadas: el servidor lee Postgres. Si **no** hay fila `companies` con ese `shopify_company_id`, o está desalineada con `shopify_app_installations.company_id`, la respuesta es `{"note_attributes":{}, "billing":{}}`.

```bash
curl -sS -X POST \
  'https://<HttpApiUrl-host>/api/checkout/billing-metadata' \
  -H 'Content-Type: application/json' \
  -d '{
    "shop": "tu-tienda.myshopify.com",
    "shopify_company_id": "123456789",
    "campaign": {
      "discount_codes": [],
      "discount_allocations": []
    }
  }'
```

Antes conviene poblar la company en la base (misma `shopify_company_id`, columnas `billing_*`, usuario SALES con `codigo_sap`).

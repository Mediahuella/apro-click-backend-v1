# Guía — Formulario de registro de empresa (theme extension + frontend admin)

Esta guía describe cómo implementar en **Shopify Theme App Extension** (formulario en tienda) y en el **panel admin** (`aproclick-frontend`) el envío y la visualización de solicitudes de alta de empresa, incluyendo **dirección de envío** y campos necesarios para que el backend pueda crear la empresa B2B en Shopify al **aprobar**.

**Servicio backend:** `company-registration` — ver [COMPANY_REGISTRATION_SERVICE.md](./COMPANY_REGISTRATION_SERVICE.md) (URL base, API key, health).

---

## 1. Flujo resumido

1. El comprador o visitante envía el formulario → `POST /api/v1/company-registration-requests` (público).
2. El payload se guarda en PostgreSQL (`company_registration_requests.payload`).
3. Un usuario del admin **aprueba** → `POST .../approve` → el backend crea `Company` + `Client` en BD y la **Company B2B** en Shopify (GraphQL `companyCreate` / `companyAssignCustomerAsContact`), usando la **dirección** guardada en el payload.

**Importante:** la dirección **no es obligatoria** en el `POST` público a nivel de validación del backend, pero **sí es obligatoria en la práctica** para poder **aprobar**: sin `shipping_address1`, `shipping_city` y `shipping_zip`, la aprobación falla. Conviene **validar en el formulario** y mostrar los campos como obligatorios.

---

## 2. Alta pública — `POST /api/v1/company-registration-requests`

### Headers

| Header | Uso |
|--------|-----|
| `Content-Type` | `application/json` |
| `X-Api-Key` | Solo si en la Lambda está definida `COMPANY_REGISTRATION_API_KEY` (valor idéntico). Si la variable está vacía, no hace falta. |

### Cuerpo JSON (`snake_case`)

#### Datos de empresa y contacto (ya existentes)

| Campo | Obligatorio (backend) | Notas |
|-------|------------------------|--------|
| `company_name` | Sí | Razón social / nombre comercial. |
| `rut` | Sí | RUT chileno; el backend valida y normaliza. |
| `contact_name` | Sí | Nombre completo del contacto (se parte en nombre/apellido al aprobar). |
| `contact_email` | Sí | Email válido (minúsculas internamente). |
| `contact_phone` | No | Teléfono E.164 recomendado (p. ej. `+56912345678`). |
| `company_type` | No | `SMALL` \| `MEDIUM` \| `BIG`. Default `SMALL`. |
| `payment_type` | No | `DIRECT` \| `CREDIT`. Default `DIRECT`. |
| `notes` | No | Texto libre. |
| `shop_domain` | Muy recomendado | Ej. `mi-tienda.myshopify.com`. Permite elegir la instalación correcta al aprobar (token en `shopify_app_installations`). |
| `source` | No | Default `shopify_theme`. Útil para distinguir canal (theme vs otra app). |

#### Facturación (opcional; se guardan en `companies.billing_*` al **aprobar**)

| Campo | Obligatorio (backend) | Notas |
|-------|------------------------|--------|
| `giro` | No | Giro comercial → `companies.billing_giro`. |
| `direccion` | No | Dirección de facturación. Si se omite, al aprobar se usa una línea armada con `shipping_address1`, `shipping_address2` y `shipping_city` (cuando existan en el payload). |

El **RUT** ya se envía como `rut` (obligatoria) y se persiste en `companies.billing_rut` al aprobar. `company_name` queda también como `billing_razon_social` al crear la empresa.

#### Dirección de envío (B2B — nueva ubicación en Shopify)

Recomendación UX: tratar como **obligatorios** en el formulario.

También puedes enviar un objeto **`shipping_address`** con las mismas ideas (p. ej. `address1`, `city`, `postal_code`, `region`); el backend lo normaliza a las claves `shipping_*`.

| Campo | Obligatorio para aprobar | Notas |
|-------|---------------------------|--------|
| `shipping_address1` | Sí | Calle y número (equivalente a `address1` en Shopify). |
| `shipping_city` | Sí | Comuna / ciudad. |
| `shipping_zip` | Sí | Código postal. |
| `shipping_country_code` | No (default backend) | ISO 3166-1 alpha-2, **mayúsculas** (ej. `CL`). Si no se envía, al aprobar se usa **`CL`**. |
| `shipping_zone_code` | No | **Región o estado** (en Shopify Admin: `zoneCode`). No es la comuna ni la ciudad: en Chile suele ser el código de región (`RM`, `VIII`, `XI`, etc.). Si el formulario usa otro nombre, también puede enviarse como `shipping_region`, `region` o `province` (o dentro de `shipping_address`). |
| `shipping_address2` | No | Depto., oficina, etc. |
| `shipping_first_name` | No | Si se omite, se usan el primer nombre / apellido derivados de `contact_name`. |
| `shipping_last_name` | No | Igual que arriba. |

---

## 3. Ejemplo — `fetch` desde JS (theme extension o bloque Liquid)

```javascript
const API_BASE = "https://<TU_HTTP_API_URL>"; // npm run sls:service -- company-registration info
const API_KEY = undefined; // o string desde theme settings si el backend exige clave

const headers = { "Content-Type": "application/json" };
if (API_KEY) headers["X-Api-Key"] = API_KEY;

const body = {
  company_name: "Ejemplo SpA",
  rut: "76123456-7",
  contact_name: "María Pérez",
  contact_email: "maria@ejemplo.cl",
  contact_phone: "+56912345678",
  company_type: "MEDIUM",
  payment_type: "DIRECT",
  shop_domain: "mi-tienda.myshopify.com", // {{ shop.permanent_domain }} en Liquid
  source: "shopify_theme",

  shipping_address1: "Av. Apoquindo 1234",
  shipping_address2: "Oficina 56",
  shipping_city: "Las Condes",
  shipping_zone_code: "RM",
  shipping_zip: "7550000",
  shipping_country_code: "CL",
  // opcional: distinto al contacto
  // shipping_first_name: "María",
  // shipping_last_name: "Pérez",
};

const res = await fetch(`${API_BASE}/api/v1/company-registration-requests`, {
  method: "POST",
  headers,
  body: JSON.stringify(body),
});

const json = await res.json();
// Éxito típico: json.statusCode === 201, json.data con id, status pending_review, campos reflejados
```

### Validación mínima recomendada (cliente)

Antes de enviar:

- `company_name`, `rut`, `contact_name`, `contact_email` presentes.
- Email con formato básico.
- **Dirección:** `shipping_address1`, `shipping_city`, `shipping_zip` no vacíos.
- `shop_domain` presente siempre que puedas obtenerlo (Liquid: `{{ shop.permanent_domain }}`).

---

## 4. Theme App Extension — buenas prácticas

1. **URL del API**  
   No hardcodear en código público la URL de producción si el repo es visible: usar **theme settings** o **metafields de tienda** inyectados por la app, o variables de build.

2. **`shop_domain`**  
   En Liquid del theme: `{{ shop.permanent_domain }}` suele coincidir con `*.myshopify.com`. Pásalo al JS del bloque/extension como data attribute o setting.

3. **API key**  
   Si usas `COMPANY_REGISTRATION_API_KEY`, guarda el valor en **configuración privada de la app** (no en el theme público sin cuidado). Alternativa: proxy en tu propio backend; aquí el diseño actual es header `X-Api-Key` directo al API Gateway.

4. **Errores**  
   - 400: validación (RUT, campos faltantes según reglas actuales).  
   - 401: API key incorrecta o ausente.  
   - RUT duplicado en cola pendiente: mensaje del backend indicando solicitud pendiente.

---

## 5. Frontend admin — listado, detalle y aprobación

### Listado y detalle

Los objetos serializados incluyen los mismos campos planos que el payload, entre otros:

- `shipping_address1`, `shipping_address2`, `shipping_city`, `shipping_zone_code`, `shipping_zip`, `shipping_country_code`, `shipping_first_name`, `shipping_last_name`

**Recomendación:** mostrar una sección “Dirección de envío” en el detalle de la solicitud para que el revisor verifique datos antes de aprobar.

El backend también **normaliza alias** al aprobar (p. ej. datos dentro de `shipping_address` o `city` / `postal_code` en el JSON guardado). Si el listado muestra vacíos pero el detalle crudo del payload tiene otro formato, revisar el objeto `payload` en red o en BD; lo ideal es que el theme envíe siempre las claves `shipping_*` para evitar confusiones en UI.

### Aprobar — petición

```http
POST /api/v1/company-registration-requests/{request_id}/approve
Authorization: Bearer <access_token Cognito>
```

Body: vacío `{}` (no es obligatorio enviar cuerpo).

**Roles:** mismos que el resto del admin (`SUPERADMIN`, `ADMIN`, `SALES`); ver [COMPANY_REGISTRATION_SERVICE.md](./COMPANY_REGISTRATION_SERVICE.md).

### Aprobar — errores que debe manejar el frontend

| Situación | HTTP típico | Qué mostrar al usuario |
|-----------|--------------|-------------------------|
| Dirección incompleta en la solicitud | 400 | Mensaje del backend: indica qué falta (calle, ciudad, CP). Incluye aclaración de que `shipping_zone_code` es región opcional. |
| Solicitud no pendiente | 400 | “Solo se pueden aprobar solicitudes pendientes” / estado cambió. |
| Sin instalación Shopify / sin token | 400 | Mensaje sobre `shopify_app_installations` y OAuth. |
| Fallo GraphQL Shopify (Plus, scopes, etc.) | 400 | Mensaje genérico B2B; revisar logs y Partners. |
| No encontrada | 404 | Solicitud inexistente. |
| Sin autenticación o rol | 401/403 | Redirigir a login o mostrar permisos. |

**UX recomendada:** deshabilitar el botón “Aprobar” o mostrar aviso si faltan `shipping_address1`, `shipping_city` o `shipping_zip` en los datos mostrados (alineado con la validación del backend). Si `shop_domain` está vacío, avisar que la aprobación puede fallar al resolver la tienda.

### Aprobar — respuesta exitosa (`data`)

Tras un **200**, el cuerpo sigue la convención del servicio (`statusCode`, `message`, `data`). Dentro de **`data`** conviene usar estos bloques en el frontend:

| Clave | Uso en UI |
|-------|-----------|
| `id`, `status`, `resolved_company_id`, … | Estado de la solicitud actualizada (`approved`). |
| `created_company` | Registro PostgreSQL de la empresa; incluye **`shopify_company_id`** (id numérico en Shopify) si el flujo B2B terminó bien. Mostrar enlaces de soporte o copiar id para abrir en Admin de Shopify. |
| `created_client` | Cliente en PostgreSQL; **`shopify_customer_id`** (numérico). |
| `shopify_b2b` | Referencia técnica del último paso GraphQL: `shopify_company_gid`, `shopify_company_numeric_id`, `shopify_customer_numeric_id`. Útil para depuración y mensajes “Empresa creada en Shopify”. |

**Ejemplo simplificado de `data` tras aprobar:**

```json
{
  "id": "uuid-de-la-solicitud",
  "status": "approved",
  "resolved_company_id": "uuid-de-company-en-bd",
  "company_name": "Ejemplo SpA",
  "shipping_address1": "Av. …",
  "created_company": {
    "id": "uuid",
    "name": "Ejemplo SpA",
    "company_type": "MEDIUM",
    "payment_type": "DIRECT",
    "shopify_company_id": "1234567890",
    "created_at": "…",
    "updated_at": "…"
  },
  "created_client": {
    "id": "uuid",
    "company_id": "uuid",
    "shopify_customer_id": "9876543210",
    "email": "maria@ejemplo.cl",
    "name": "María Pérez",
    "phone": "+569…"
  },
  "shopify_b2b": {
    "shopify_company_gid": "gid://shopify/Company/1234567890",
    "shopify_company_numeric_id": "1234567890",
    "shopify_customer_numeric_id": "9876543210"
  }
}
```

Puedes mostrar un **toast de éxito** con el nombre de la empresa y, en pantalla de detalle o modal, los ids de Shopify para el equipo operativo.

### Qué hace el backend al aprobar (para copy y soporte)

Orden lógico que puedes reflejar en tooltips o documentación interna:

1. Lee la solicitud y valida estado **pendiente**.
2. Construye la dirección para Shopify B2B (ubicación de la company) a partir del payload; si falta lo obligatorio → **400** con mensaje claro.
3. Obtiene **`shop_domain`** del payload y el **access token** Admin desde la base (`shopify_app_installations`); sin token válido → error.
4. **Si ya existe** un cliente en Shopify con el mismo **email** del formulario: crea la **Company** con nombre + ubicación y luego **asigna** ese cliente como contacto (`companyAssignCustomerAsContact`).
5. **Si no existe** cliente con ese email: una sola mutación **`companyCreate`** crea company + contacto + ubicación; Shopify crea el **Customer** asociado.
6. Guarda **Company** y **Client** en PostgreSQL con **`shopify_company_id`** y **`shopify_customer_id`** (ids numéricos).
7. Marca la solicitud como **approved** y devuelve `data` como arriba.

Documentación oficial de la mutación principal: [companyCreate (Admin GraphQL)](https://shopify.dev/docs/api/admin-graphql/latest/mutations/companyCreate).

### Checklist de implementación (frontend admin)

- [ ] Listado con filtro por `status` (`pending_review`, etc.).
- [ ] Detalle con bloques: empresa, contacto, dirección, `shop_domain`, notas.
- [ ] Botón aprobar con confirmación; manejo de **400** mostrando `message` del API.
- [ ] Tras éxito: refrescar detalle o navegar y mostrar ids de `created_company` / `created_client` / `shopify_b2b` si aplica.
- [ ] No confundir **`shopify_company_id`** (empresa B2B) con **`shopify_customer_id`** (persona que compra por la empresa).

---

## 5.1 Herramientas de desarrollo (Shopify MCP) — no aplica al runtime del frontend

En Cursor puede estar configurado el **MCP de Shopify** (`learn_shopify_api`, `search_docs_chunks`, `validate_graphql_codeblocks`). Sirve para **documentación y validación de GraphQL** durante el desarrollo del backend o integraciones; **la app React del admin no llama al MCP**. El flujo productivo es siempre: **frontend → API Gateway → Lambda → Shopify Admin API** con el token guardado en base de datos.

---

## 6. Requisitos Shopify (referencia)

- Tienda **Shopify Plus** para **Companies (B2B)**.
- App con scopes acordes a Admin API; el validador de schema suele exigir para estas mutaciones algo equivalente a **`read_customers`**, **`write_customers`**, **`read_companies`**, **`write_companies`** (confirmar en Partners / [companyCreate](https://shopify.dev/docs/api/admin-graphql/latest/mutations/companyCreate)).
- Instalación con OAuth completada para persistir `shopify_access_token` en `shopify_app_installations`.

Versión API GraphQL por defecto en Lambda: `SHOPIFY_API_VERSION` (p. ej. `2026-04`).

---

## 7. Referencias en este repo

| Documento | Contenido |
|-----------|-----------|
| [COMPANY_REGISTRATION_SERVICE.md](./COMPANY_REGISTRATION_SERVICE.md) | URL, health, API key, deploy. |
| [BACKEND_EMPRESAS_Y_SOLICITUDES.md](./BACKEND_EMPRESAS_Y_SOLICITUDES.md) | Visión funcional del dominio. |
| [AGENTS.md](../AGENTS.md) | Comandos `deploy`, `shared:sync`, migraciones. |

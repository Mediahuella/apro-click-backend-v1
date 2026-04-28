# Backend — empresas y solicitudes de alta

Referencia para alinear lambdas/API con el panel admin (`aproclick-frontend`). Los nombres en JSON se muestran en **snake_case** (habitual en API); el front puede mapear si el contrato difiere.

---

## 1. Empresa (`apro-click-admin-companies`)

### Modelo sugerido (respuesta `data`)

| Campo | Tipo | Notas |
|--------|------|--------|
| `id` | UUID string | PK |
| `name` | string | Razón social / nombre comercial |
| `rut` | string | Normalizado recomendado: sin puntos, con guion antes del DV (`76123456-7` o `761234567-K`) |
| `company_type` | `SMALL` \| `MEDIUM` \| `BIG` | |
| `payment_type` | `DIRECT` \| `CREDIT` | |
| `email` | string \| null | Opcional |
| `phone` | string \| null | Opcional |
| `address` | string \| null | Opcional |
| `is_system` | boolean | |
| `created_at` | ISO 8601 | |
| `updated_at` | ISO 8601 | |

### `POST /api/v1/companies`

Body ejemplo:

```json
{
  "name": "Mi Empresa SpA",
  "rut": "761234567",
  "company_type": "MEDIUM",
  "payment_type": "DIRECT",
  "email": "contacto@empresa.cl",
  "phone": "+56 9 1234 5678",
  "address": "Av. Principal 123, Santiago"
}
```

- Validar RUT chileno en servidor (mismo criterio que cliente: dígito verificador).
- Aceptar `rut` con o sin puntos/guion; persistir formato acordado internamente.

### `PUT /api/v1/companies/{company_id}`

Mismos campos opcionales que el create (parcial). Incluir `rut` si se permite corrección.

### Errores

Convención ya usada en el front: `{ "statusCode": number, "message": string }`.

Casos útiles: RUT duplicado, RUT inválido, conflicto de negocio.

---

## 2. Solicitudes de registro de empresa (canal público → revisión en panel)

**Servicio desplegado:** `apro-click-admin-company-registration`. Contrato real, URLs, autenticación (API key opcional en público + Cognito en panel), theme extension y health: **[COMPANY_REGISTRATION_SERVICE.md](./COMPANY_REGISTRATION_SERVICE.md)**.

Flujo: el **formulario web u otra integración** crea solicitudes; el **panel** solo **lista** y **aprueba/rechaza**. No vive el formulario de alta en el admin.

### Estado

| Valor | Uso |
|--------|-----|
| `pending_review` | En cola |
| `approved` | Aceptada (p. ej. crea o vincula empresa) |
| `rejected` | Rechazada |

### Modelo sugerido (ítem de listado / detalle)

| Campo | Tipo | Notas |
|--------|------|--------|
| `id` | UUID string | |
| `company_name` | string | Nombre declarado por el solicitante |
| `rut` | string | RUT de la empresa solicitada |
| `contact_name` | string | |
| `contact_email` | string | |
| `contact_phone` | string | |
| `company_type` | string | p. ej. `SMALL` \| `MEDIUM` \| `BIG` \| `UNKNOWN` |
| `notes` | string | Opcional |
| `status` | ver tabla estados | |
| `created_at` | ISO 8601 | |

(Ampliar según negocio: `source`, `external_id`, `shop_domain`, etc.)

### Endpoints (implementados en `company-registration`)

Ver **[COMPANY_REGISTRATION_SERVICE.md](./COMPANY_REGISTRATION_SERVICE.md)** para base URL, headers y ejemplos.

1. **`GET /api/v1/company-registration-requests`**  
   Query: `status`, `limit`, `offset` (o cursor).  
   Respuesta: `{ "data": { "requests": [ ... ] } }` (o nombre acordado).

2. **`GET /api/v1/company-registration-requests/{id}`**  
   Detalle para el modal de revisión.

3. **`POST /api/v1/company-registration-requests/{id}/approve`** (o `PATCH` con body `{ "status": "approved" }`)  
   - Valida rol (vendedor/admin).  
   - Efecto esperado: marcar aprobada y, si aplica, **crear** registro en `companies` o **vincular** a empresa existente (regla de negocio en backend).

4. **`POST /api/v1/company-registration-requests/{id}/reject`**  
   Body opcional: `{ "reason": "..." }`.

5. **Alta desde canal público** (fuera del admin):  
   `POST /api/v1/company-registration-requests` o ruta expuesta solo en API pública con rate limit / API key según diseño.

Autenticación en panel: mismo esquema que el resto (`Authorization: Bearer` con JWT Cognito). Restringir por grupo (`admin`, `sales`, `superadmin` según producto).

---

## 3. Front que consume esto

- CRUD empresas: `src/lib/api/companies.api.ts` y tipos `src/types/company.ts`.
- Solicitudes: `src/types/company-registration.ts` y bandeja en `src/app/(dashboard)/solicitudes-empresa/` (hoy datos mock; sustituir por hooks que llamen a los `GET`/`POST`/`PATCH` anteriores).

Cuando el contrato real esté cerrado, basta con ajustar tipos y URLs si difieren de lo anterior.

---

## 4. Health

El dashboard consulta el health de cada servicio según su HTTP API; p. ej. `GET {BASE}/api/v1/health-companies` y **`GET {BASE}/api/v1/health-company-registration`** para este flujo (`{BASE}` es la URL del API Gateway de cada stack). Respuesta esperada: `{ "status": "healthy", "service": "<nombre-del-servicio-serverless>" }`.

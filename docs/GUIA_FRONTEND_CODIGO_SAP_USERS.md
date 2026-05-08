# Frontend — código SAP en usuarios (rol SALES)

Guía para implementar **`codigo_sap`** en el panel de usuarios (`apro-click-admin-users`). El campo identifica en SAP al vendedor y es **obligatorio** cuando el usuario tiene rol **`SALES`** en alta y cuando se **promueve** a SALES desde otro rol.

**Relacionado:** contrato API §6.2 en [FRONTEND_ADMIN_GUIDE.md](./FRONTEND_ADMIN_GUIDE.md).

---

## 1. Contrato backend

### 1.1 Respuesta (`User`)

Tras crear, listar o editar usuario, **`data`** incluye el campo nuevo:

| Campo        | Tipo            | Ejemplo |
|-------------|-----------------|--------|
| `codigo_sap` | `string \| null` | `"VEND-0142"` o `null` |

- Si el rol **no** es `SALES`, el backend fuerza **`null`** en el guardado y en la respuesta.
- También llegan como siempre `id`, `sub` / `cognito_sub`, `email`, `role`, `status`, `company_ids` (multi-empresa), etc.

### 1.2 Crear usuario — `POST /api/v1/users`

Body JSON habitual más:

```json
{
  "email": "vendedora@ejemplo.cl",
  "given_name": "Ana",
  "family_name": "Pérez",
  "role": "SALES",
  "codigo_sap": "VEND-0142",
  "temporary_password": "opcional",
  "company_ids": ["uuid-empresa-1"]
}
```

| Rol en el body       | `codigo_sap` en el client |
|----------------------|---------------------------|
| **`SALES`**          | **Obligatorio** — string con al menos un carácter no espacio (backend hace `.trim()`; vacío ⇒ error **400**). |
| **`ADMIN`**, `SUPERADMIN`, `KPI_VISUALIZERS` | Omitir o ignorar — el backend guarda **`null`** aunque el front envíe valor. |

**Importante:** el default del campo `role` en el API es **`SALES`**. Si el formulario omite rol y muestra SALES solo visualmente pero no envía `role`, igualmente aplica SALES ⇒ **hay que pedir código SAP antes de crear**.

---

### 1.3 Editar usuario — `PUT /api/v1/users/:user_id`

*`user_id` = UUID PostgreSQL (`id`) **o** `cognito_sub`.*

| Escenario                                      | Qué hacer en frontend |
|-----------------------------------------------|--------------------------|
| Usuario ya **SALES**, solo otros campos (nombre, status, empresas) | **`codigo_sap` no obligatorio en el PATCH** si no lo tocás. |
| Cambiar rol **de otro a `SALES`**             | Envía **`codigo_sap`** con texto no vacío en el **mismo** request (`role` + `codigo_sap`). |
| Usuario **SALES** y querés cambiar solo el código | Incluye **`codigo_sap`** con el nuevo valor; no puede ir vacío. |
| Cambiar rol **desde SALES** a ADMIN / SUPERADMIN / KPI | Podés omitir `codigo_sap` — el backend **borra** el código (`null`). |
| **`codigo_sap` presente pero string vacío o solo espacio** cuando el resultado es SALES | El API responde **400** (“no puede quedar vacío” / obligatorio). |

Tipo: si mandás **`codigo_sap`**, debe ser **string**. Otros tipos ⇒ **400** (“debe ser texto”).

---

## 2. Errores HTTP típicos (mensajes útiles para la UI)

| Código | Mensaje aproximado (español) | Traducción UI |
|--------|------------------------------|----------------|
| 400 | `codigo_sap es obligatorio cuando el rol es SALES ...` | Mostrar campo requerido al crear usuario con rol ventas (o cuando el rol por defecto es SALES sin envío explícito). |
| 400 | `codigo_sap es obligatorio al asignar el rol SALES ...` | Al promover a ventas sin código: pedir código antes del submit o mostrar campo requerido. |
| 400 | `codigo_sap no puede estar vacío para usuarios con rol SALES` | Borrar contenido del campo cuando sigue SALES ⇒ bloquear envío / validación inline. |

---

## 3. UX recomendada

### 3.1 Formulario alta de usuario

1. Campo **“Código SAP”** (`codigo_sap`):
   - Visible y **requerido** cuando `role === 'SALES'`.
   - Oculto o deshabilitado (y no incluido en el JSON, o omitido) cuando el rol **no** es SALES — evita confusiones.

2. Al cambiar el selector de rol a **no SALES** → limpiar el input visualmente para no parecer persistido entre roles.

### 3.2 Formulario edición de usuario

1. Mostrar **Código SAP** solo si **`role === 'SALES'`** (o cuando el usuario está editando rol hacia SALES).
2. Al pasar rol de **ADMIN** (ej.) **a SALES**, mostrar campo requerido y validar antes de `PUT`.
3. Al pasar **de SALES a otro**, podés ocultar el campo (el backend ignora valores residuales y deja `null`).
4. **Usuarios viejos** que ya son SALES pero `codigo_sap === null`:
   - Cualquier `PUT` sin tocar codigo puede seguir funcionando.
   - Conviene en UI mostrar el campo opcional/disabled hasta que el administrador cargue código (primer guardado incluyendo `codigo_sap`).

---

## 4. Snippets TypeScript

### 4.1 Tipos (extendé los tipos locales)

```typescript
export type UserRole =
  | 'SUPERADMIN'
  | 'ADMIN'
  | 'SALES'
  | 'KPI_VISUALIZERS';

export interface User {
  id: string;
  sub: string;
  cognito_sub: string;
  email: string;
  given_name: string;
  family_name: string;
  role: UserRole;
  status: 'ACTIVE' | 'DISABLED' | 'PENDING';
  company_id: string | null;
  codigo_sap: string | null;
  created_at: string;
  updated_at: string;
  /** Si el API lista multi-empresa */
  company_ids?: string[];
}

export interface CreateUserPayload {
  email: string;
  given_name?: string;
  family_name?: string;
  role?: UserRole;
  /** Obligatorio si role es SALES o se omite role (API default SALES). */
  codigo_sap?: string;
  temporary_password?: string;
  company_ids?: string[];
}

export interface UpdateUserPayload {
  given_name?: string;
  family_name?: string;
  role?: UserRole;
  status?: 'ACTIVE' | 'DISABLED' | 'PENDING';
  codigo_sap?: string;
  company_ids?: string[];
}

/** Trim; vacío ⇒ undefined (omitir del body salvo querer borrar explícito en edición SALES → no válido si queda SALES). */
export function normalizedCodigoSap(value: string | undefined): string | undefined {
  if (value == null) return undefined;
  const t = value.trim();
  return t.length ? t : undefined;
}

export function codigoSapRequiredForCreate(role: UserRole | undefined): boolean {
  const r = role ?? 'SALES';
  return r === 'SALES';
}

export type UpdateUserDraft = Partial<Pick<User, 'role' | 'codigo_sap'>> & Partial<User>;

/** Validación cliente antes de POST */
export function validateCreateUserForm(body: CreateUserPayload): string | undefined {
  const role = body.role ?? 'SALES';
  const sap = normalizedCodigoSap(body.codigo_sap);
  if (role === 'SALES' && !sap) {
    return 'El código SAP es obligatorio para usuarios con rol ventas (SALES).';
  }
  return undefined;
}

/**
 * Validación antes de PUT cuando el rol final será SALES.
 * `initialRole`: rol del usuario antes de aplicar los cambios del formulario.
 */
export function validateUpdateUserForm(
  initialRole: UserRole,
  draft: Pick<UpdateUserPayload, 'role' | 'codigo_sap'>,
): string | undefined {
  const targetRole = draft.role ?? initialRole;
  const sapFromForm = normalizedCodigoSap(draft.codigo_sap);
  const touchedSap = Object.prototype.hasOwnProperty.call(draft, 'codigo_sap');

  if (targetRole !== 'SALES') return undefined;

  const transitionedToSales = initialRole !== 'SALES' && targetRole === 'SALES';

  if (transitionedToSales && !sapFromForm) {
    return 'Para asignar el rol ventas (SALES) debés cargar el código SAP.';
  }
  if (touchedSap && !sapFromForm) {
    return 'El código SAP no puede estar vacío para usuarios ventas.';
  }
  return undefined;
}
```

Ajustá `payload` antes de enviar:

- En **CREATE** con rol no SALES: podés omitir `codigo_sap` por completo.
- En **UPDATE** igual; solo incluye `codigo_sap` si el usuario lo editó o si promovés a SALES con valor nuevo.

---

## 5. Checklist QA manual

1. **Crear** usuario con `role: SALES` sin `codigo_sap` → debe fallar cliente y/o servidor **400**.
2. **Crear** con `role: ADMIN` sin `codigo_sap` → **201**.
3. Editar ADMIN → cambiar solo nombre → OK.
4. Editar ADMIN → rol **SALES** sin código → **400** (pedir código en la misma acción).
5. Editar SALES con código → cambiar a ADMIN → siguiente GET sin `codigo_sap`.
6. Listado/grid: columna opcional “Código SAP” solo valores para filas SALES (el resto muestra `-`).

---

## 6. Migración de datos / legado

Puede haber usuarios **`SALES` en BD sin `codigo_sap`** (creados antes del cambio). El backend **no** rompe otros `PATCH` hasta que incluyas `codigo_sap` vacío o promuevas sin código. Planeá desde producto banners o filtros para completar código en esos registros cuando convenga.

---

## 7. Alcance fuera de este trabajo

- **Registro público de empresa** (`company-registration-requests`) **no crea usuarios del panel**: no necesita campo SAP.
- **Shopify Staff** (`link-shopify-staff`): sin cambios; el código SAP es sólo PostgreSQL/users.

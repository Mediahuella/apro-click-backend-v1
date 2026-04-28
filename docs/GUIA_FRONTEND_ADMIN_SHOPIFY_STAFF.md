# Guía: Shopify Staff en el administrador (frontend)

Vincular usuarios del panel Apro con **colaboradores (staff) de la tienda Shopify** para trazabilidad (quién interviene en pedidos, etc.). El servicio de backend es **`apro-click-admin-users`**.

> **Lectura previa (producto):** no existe invitación/creación automática de staff vía la API pública de Shopify. El comercio crea o invita al colaborador en **Shopify Admin → Configuración → Usuarios y permisos**; luego el panel asocia ese staff con nuestro usuario (por email o por GID). Detalle en `src/services/users/utils/shopify_staff_link.py`.

---

## 1. Autenticación y base URL

- **Mismo patrón** que el resto del admin: `Authorization: Bearer <access_token de Cognito>`.
- **Base URL** del API de usuarios: la de API Gateway v2 (HTTP API) apuntada al despliegue `apro-click-admin-users` (p. ej. `https://xxxxx.execute-api....amazonaws.com` o el dominio custom que tengáis en producción), **misma base** que usáis en `GET /api/v1/users`.

---

## 2. Identificador de usuario en la ruta: `{user_id}`

Puede ser:

| Valor en la URL | Uso |
|-----------------|-----|
| `users.id` (UUID) | Recomendado: es lo que devuelve `GET /api/v1/users` y el detalle. |
| `cognito_sub` (UUID) | Válido; el backend acepta ambos. |

No mezclar: copiar el `id` de la fila o el `sub` de la API tal cual a la ruta.

---

## 3. Campos en el modelo de usuario (API)

Añadid a vuestro tipo `User` (y estados) lo que el backend expone:

```typescript
export type ShopifyStaffLinkStatus =
  | 'LINKED'
  | 'NOT_FOUND'
  | 'SKIPPED_ROLE'      // p. ej. KPI_VISUALIZERS
  | 'SKIPPED_NO_SHOP'  // no hay token de app en la tienda
  | 'ERROR';

export interface UserShopifyStaffFields {
  shopify_staff_member_id?: string | null;  // p. ej. gid://shopify/StaffMember/123
  shopify_staff_link_status?: ShopifyStaffLinkStatus | string | null;
  // Solo en respuestas (no en lista si no las incluís):
  shopify_staff_link_message?: string;
  shopify_staff_link_error?: string;
  shopify_admin_users_url?: string;  // enlace a Configuración → Usuarios (Shopify)
}
```

- Tras **crear** usuario, el JSON `data` de `201` puede incluir los campos de vinculación (intento automático) además de `id`, `email`, etc.
- `GET` usuario: los campos persistidos vienen de la BD (`shopify_staff_member_id`, `shopify_staff_link_status`).

---

## 4. Flujo A: vinculación por **email** (automático en backend)

**Cuándo usarlo:** el colaborador en Shopify tiene **el mismo email** que el usuario en el panel.

| Método | Ruta | Body | Respuesta típica |
|--------|------|------|------------------|
| `POST` | `/api/v1/users/{user_id}/link-shopify-staff` | vacío (sin body) | `200 { message, data }` |

**`data` incluye (entre otros):**

- `shopify_staff_link_status`:
  - `LINKED` — Encontró un `StaffMember` con el mismo email; guardado el GID.
  - `NOT_FOUND` — No hay staff con ese email aún: mostrar `shopify_staff_link_message` y, si existe, `shopify_admin_users_url` (abrir en nueva pestaña para invitar al usuario en Shopify con ese email).
  - `ERROR` — Revisar `shopify_staff_link_error` (permisos, plan, `read_users`, etc.).
  - `SKIPPED_NO_SHOP` / `SKIPPED_ROLE` — Según el caso.

- **UI sugerida:** botón *«Sincronizar con Shopify (por email)»* o *«Reintentar vínculo Shopify»* en ficha de usuario, que llame a este `POST` y muestre el resultado (toast + texto de `shopify_staff_link_message` en `Alert` si no es `LINKED`).

---

## 5. Flujo B: asociación **manual por GID** (colaborador ya creado en Shopify)

**Cuándo usarlo:** el staff **ya existe** en Shopify (invitado o activo) y queréis fijar el enlace aunque no hayáis conseguido el match por email, o copiáis el GID desde otra parte.

| Método | Ruta | Body (JSON) | Respuesta |
|--------|------|------------|-----------|
| `POST` | `/api/v1/users/{user_id}/associate-shopify-staff` | Ver tabla siguiente | `200 { message, data }` |

**Cuerpo:**

| Campo | Obligatorio | Descripción |
|--------|------------|-------------|
| `shopify_staff_member_gid` | Sí | GID, formato: `gid://shopify/StaffMember/<número>`. (Alternativa de nombre: `shopifyStaffMemberGid`.) Cópialo: GraphQL, documentación, o (si aplica) herramientas de inspección del Admin. |
| `skip_email_verification` | No (default `false`) | `true` solo si aceptáis asociar **sin** comprobar en Shopify que el email del staff coincida con el del usuario. Usar con cuidado. |

**Comportamiento por defecto (`skip_email_verification: false`):**

- Con instalación y token de la app en la tienda, el backend llama a `staffMember(id:)` y exige **mismo email** que el usuario del panel. Si no coincide, **400** con mensaje explicando el conflicto.
- Sin token y sin `skip_email_verification: true` → **400** (no puede verificar).

**UI sugerida:**

1. Ficha de usuario: sección *«Colaborador Shopify»*.
2. Campo **texto** (o un solo renglón) para el GID, con `placeholder` o ayuda: *«gid://shopify/StaffMember/…»*.
3. Checkbox *«Asociar sin verificar email»* (solo superadmin, si restringes por rol) con leyenda de riesgo.
4. Botón *«Asociar GID con Shopify»* → `POST associate-shopify-staff` con el JSON.
5. En éxito, refrescar el usuario (`GET`) y mostrar el chip `shopify_staff_link_status === 'LINKED'` y el GID trunchado o tooltip.

---

## 6. Errores HTTP

| Código | Caso |
|--------|------|
| `400` | GID inválido, cuerpo sin `shopify_staff_member_gid`, emails que no coinciden (asociar con verificación), o falta de token cuando se exige verificación. |
| `404` | Usuario inexistente (`user_id` incorrecto). |
| `500` | Error de servidor. |

**Nota:** En flujo A, un `200` con `shopify_staff_link_status: 'ERROR'` o `'NOT_FOUND'` **no** es 400: el panel debe mostrar el mensaje y la URL de admin según el `data`.

---

## 7. Resumen de experiencia (UX)

1. Tras **crear** usuario, mostrar en la ficha/respuesta de creación el estado de Shopify si vuelve en `data` (puede quedar en `NOT_FOUND` hasta que exista el staff o se asocie GID).
2. Botón **sincronizar por email** → `link-shopify-staff`.
3. Si el admin ya creó el colaborador en Shopify y sabe el GID, **asociar por GID** → `associate-shopify-staff` (con o sin `skip_email_verification`).
4. Incluir enlace a `shopify_admin_users_url` cuando venga, para *«Abrir Usuarios en Shopify»* (misma org/tienda).

---

## 8. Dónde encaja con la estructura del `FRONTEND_ADMIN_GUIDE`

- Rutas: `/users`, `/users/new`, `/users/:id` (ficha detalle: ideal sitio para sección Shopify).
- Misma sección o pestaña *«Cuenta / Shopify»* en `UserDetailPage` o `EditUserPage` según vuestro diseño.

Documentación general de API y layout: [FRONTEND_ADMIN_GUIDE.md](./FRONTEND_ADMIN_GUIDE.md) (§ 6.2 y § 8.3).

---

## 9. Health check (ops)

- `GET /api/v1/health-users` (sin auth en muchos despliegues) — comprueba que el servicio `users` esté arriba.

---

## 10. Requisitos en Shopify (no es frontend, pero afecta al éxito)

- App con scope **`read_users`** (y tienda/ plan que habilite `staffMember` / `staffMembers` según Shopify).
- Instalación con **access token** guardada en el backend (tabla de instalación OAuth).

Si falla siempre con `ERROR`, el mensaje en `shopify_staff_link_error` o la consola de red ayudan a soporte, no a corregir solo el front.

# Frontend — Apro Click Admin (React + MUI)

Guía completa para generar el panel de administración que consume el backend serverless descrito en `AGENTS.md`. El stack recomendado es **React 18+**, **TypeScript**, **Material UI (MUI) v6**, **React Router v7** y **Vite**.

---

## 1. Stack y dependencias

```bash
# Scaffold
npm create vite@latest apro-click-admin -- --template react-ts
cd apro-click-admin

# Core UI
npm i @mui/material @mui/icons-material @emotion/react @emotion/styled

# Data grid, charts y date pickers (MUI X)
npm i @mui/x-data-grid @mui/x-charts @mui/x-date-pickers dayjs

# Routing
npm i react-router

# HTTP / estado
npm i axios react-query          # o @tanstack/react-query v5

# Formularios y validación
npm i react-hook-form zod @hookform/resolvers

# Utilidades
npm i jwt-decode notistack        # decodificar tokens Cognito + snackbars
```

### Variables de entorno (`.env`)

```dotenv
VITE_API_BASE_URL=https://<api-id>.execute-api.us-east-2.amazonaws.com
VITE_ORDERS_API_BASE_URL=https://<api-id-orders>.execute-api.us-east-2.amazonaws.com
VITE_SHOPIFY_CONNECTOR_URL=https://admin.ejemplo.com/shopify/connector
```

> Cada servicio del backend se despliega con su propio API Gateway. Si se usa un dominio personalizado o un API Gateway compartido, `VITE_API_BASE_URL` apunta a la raíz común (`/api/v1/...`). El servicio **orders** expone su propia `HttpApiUrl` hasta que unifiquéis gateways: ver [GUIA_FRONTEND_ADMIN_PEDIDOS_SHOPIFY.md](./GUIA_FRONTEND_ADMIN_PEDIDOS_SHOPIFY.md).

---

## 2. Estructura de carpetas sugerida

```text
src/
├── api/                  # Clientes HTTP (axios instances + interceptors)
│   ├── client.ts         # Axios base con interceptor de token
│   ├── auth.api.ts
│   ├── users.api.ts
│   ├── companies.api.ts
│   └── shopify.api.ts
├── auth/                 # Contexto y lógica de autenticación
│   ├── AuthContext.tsx
│   ├── AuthProvider.tsx
│   ├── useAuth.ts
│   └── RequireAuth.tsx   # Route guard
├── components/           # Componentes reutilizables
│   ├── layout/
│   │   ├── DashboardLayout.tsx
│   │   ├── Sidebar.tsx
│   │   └── TopBar.tsx
│   ├── forms/
│   │   ├── LoginForm.tsx
│   │   ├── ForgotPasswordForm.tsx
│   │   ├── ChangePasswordForm.tsx
│   │   └── ConfirmForgotPasswordForm.tsx
│   └── ui/
│       ├── LoadingScreen.tsx
│       ├── ConfirmDialog.tsx
│       └── StatusChip.tsx
├── features/             # Módulos de negocio (páginas + lógica)
│   ├── dashboard/
│   ├── users/
│   ├── companies/
│   ├── leads/
│   ├── quotes/
│   ├── orders/
│   ├── shopify/
│   └── notifications/
├── hooks/                # Custom hooks globales
├── theme/                # MUI theme customization
│   └── index.ts
├── types/                # TypeScript interfaces compartidas
│   ├── api.ts
│   ├── auth.ts
│   ├── user.ts
│   ├── company.ts
│   └── shopify.ts
├── utils/
│   ├── token.ts          # Gestión localStorage / refresh
│   └── constants.ts
├── router.tsx            # Definición de rutas
├── App.tsx
└── main.tsx
```

---

## 3. Tema MUI (theme)

```typescript
// src/theme/index.ts
import { createTheme } from '@mui/material/styles';

const theme = createTheme({
  palette: {
    primary:   { main: '#1565C0' },   // azul corporativo Apro
    secondary: { main: '#43A047' },
    error:     { main: '#E53935' },
    background: {
      default: '#F5F7FA',
      paper:   '#FFFFFF',
    },
  },
  typography: {
    fontFamily: '"Inter", "Roboto", sans-serif',
    h4: { fontWeight: 700 },
    h5: { fontWeight: 600 },
    h6: { fontWeight: 600 },
  },
  shape: { borderRadius: 10 },
  components: {
    MuiButton: {
      defaultProps: { disableElevation: true },
      styleOverrides: {
        root: { textTransform: 'none', fontWeight: 600 },
      },
    },
    MuiCard: {
      defaultProps: { variant: 'outlined' },
    },
    MuiTextField: {
      defaultProps: { variant: 'outlined', size: 'small' },
    },
  },
});

export default theme;
```

---

## 4. Autenticación (Cognito vía backend)

El backend maneja **toda** la comunicación con Cognito; el frontend solo consume los endpoints REST del servicio `auth`. Los tokens devueltos son JWTs estándar de Cognito.

### 4.1 Tipos

```typescript
// src/types/auth.ts

export interface LoginRequest {
  email: string;
  password: string;
}

export interface AuthTokens {
  access_token: string;
  id_token: string;
  refresh_token: string;
  expires_in: number;   // segundos (3600 = 1 h)
  token_type: string;   // "Bearer"
}

export interface AuthChallenge {
  challenge: string;               // e.g. "NEW_PASSWORD_REQUIRED"
  session: string;
  challenge_parameters: Record<string, string>;
}

export interface ChangePasswordRequest {
  email: string;
  new_password: string;
  session: string;
}

export interface ForgotPasswordRequest {
  email: string;
}

export interface ConfirmForgotPasswordRequest {
  email: string;
  confirmation_code: string;
  new_password: string;
}

export interface LogoutRequest {
  access_token?: string;
  refresh_token?: string;
}

export type LoginResponse =
  | { type: 'tokens'; data: AuthTokens }
  | { type: 'challenge'; data: AuthChallenge };
```

### 4.2 API client

```typescript
// src/api/auth.api.ts
import { apiClient } from './client';
import type {
  LoginRequest, ChangePasswordRequest, ForgotPasswordRequest,
  ConfirmForgotPasswordRequest, LogoutRequest,
} from '../types/auth';

const BASE = '/api/v1/auth';

export const authApi = {
  login:                 (body: LoginRequest) =>
    apiClient.post(`${BASE}/login`, body),

  changeFirstPassword:   (body: ChangePasswordRequest) =>
    apiClient.post(`${BASE}/change-password`, body),

  forgotPassword:        (body: ForgotPasswordRequest) =>
    apiClient.post(`${BASE}/forgot-password`, body),

  confirmForgotPassword: (body: ConfirmForgotPasswordRequest) =>
    apiClient.post(`${BASE}/confirm-forgot-password`, body),

  logout:                (body: LogoutRequest) =>
    apiClient.post(`${BASE}/logout`, body),
};
```

### 4.3 Flujo de autenticación (diagrama)

```text
┌────────────┐       POST /auth/login        ┌──────────┐     AdminInitiateAuth     ┌─────────┐
│  Frontend   │ ──────────────────────────────▶│  Lambda  │ ──────────────────────────▶│ Cognito │
│  LoginForm  │                                │  (auth)  │                            │         │
└──────┬─────┘                                └────┬─────┘                            └────┬────┘
       │                                           │                                      │
       │  ◄── 200 { data: { access_token, ... } } │  ◄── AuthenticationResult            │
       │      o bien                               │      o bien                          │
       │  ◄── 200 { data: { challenge, session } } │  ◄── ChallengeName                  │
       │                                           │                                      │
       ▼                                           │                                      │
 ¿challenge?───SI──▶ ChangePasswordForm            │                                      │
       │              POST /auth/change-password   │  AdminRespondToAuthChallenge          │
       │              ─────────────────────────────▶│ ─────────────────────────────────────▶│
       │              ◄── 200 { data: tokens }     │ ◄── AuthenticationResult              │
       │                                           │                                      │
       NO                                          │                                      │
       │                                           │                                      │
       ▼                                           │                                      │
  Guardar tokens                                   │                                      │
  en localStorage                                  │                                      │
  Redirigir a /                                    │                                      │
```

### 4.4 AuthContext / AuthProvider

```typescript
// src/auth/AuthContext.tsx
import { createContext } from 'react';
import type { AuthTokens } from '../types/auth';

export interface DecodedUser {
  sub: string;
  email: string;
  given_name?: string;
  family_name?: string;
  'cognito:groups'?: string[];
}

export interface AuthContextValue {
  user: DecodedUser | null;
  tokens: AuthTokens | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  login: (email: string, password: string) => Promise<'ok' | 'challenge'>;
  completeChallenge: (newPassword: string) => Promise<void>;
  logout: () => Promise<void>;
}

export const AuthContext = createContext<AuthContextValue | null>(null);
```

```typescript
// src/auth/AuthProvider.tsx (esquema)
import { useState, useEffect, useCallback, useMemo } from 'react';
import { jwtDecode } from 'jwt-decode';
import { AuthContext, type DecodedUser, type AuthContextValue } from './AuthContext';
import { authApi } from '../api/auth.api';
import type { AuthTokens, AuthChallenge } from '../types/auth';

const TOKEN_KEY = 'apro_tokens';

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [tokens, setTokens]           = useState<AuthTokens | null>(null);
  const [user, setUser]               = useState<DecodedUser | null>(null);
  const [isLoading, setIsLoading]     = useState(true);
  const [challenge, setChallenge]     = useState<AuthChallenge | null>(null);
  const [pendingEmail, setPendingEmail] = useState('');

  // Restaurar sesión al montar
  useEffect(() => {
    const raw = localStorage.getItem(TOKEN_KEY);
    if (raw) {
      try {
        const saved: AuthTokens = JSON.parse(raw);
        setTokens(saved);
        setUser(jwtDecode<DecodedUser>(saved.id_token));
      } catch { /* token corrupto */ }
    }
    setIsLoading(false);
  }, []);

  const persistTokens = useCallback((t: AuthTokens) => {
    localStorage.setItem(TOKEN_KEY, JSON.stringify(t));
    setTokens(t);
    setUser(jwtDecode<DecodedUser>(t.id_token));
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const { data: res } = await authApi.login({ email, password });
    const payload = res.data;

    if (payload.challenge) {
      setChallenge(payload as AuthChallenge);
      setPendingEmail(email);
      return 'challenge' as const;
    }
    persistTokens(payload as AuthTokens);
    return 'ok' as const;
  }, [persistTokens]);

  const completeChallenge = useCallback(async (newPassword: string) => {
    if (!challenge) throw new Error('No pending challenge');
    const { data: res } = await authApi.changeFirstPassword({
      email: pendingEmail,
      new_password: newPassword,
      session: challenge.session,
    });
    persistTokens(res.data as AuthTokens);
    setChallenge(null);
  }, [challenge, pendingEmail, persistTokens]);

  const logout = useCallback(async () => {
    try {
      await authApi.logout({
        access_token:  tokens?.access_token,
        refresh_token: tokens?.refresh_token,
      });
    } catch { /* best effort */ }
    localStorage.removeItem(TOKEN_KEY);
    setTokens(null);
    setUser(null);
  }, [tokens]);

  const value: AuthContextValue = useMemo(() => ({
    user,
    tokens,
    isAuthenticated: !!tokens,
    isLoading,
    login,
    completeChallenge,
    logout,
  }), [user, tokens, isLoading, login, completeChallenge, logout]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
```

### 4.5 Axios interceptor (inyectar Bearer token)

```typescript
// src/api/client.ts
import axios from 'axios';

const TOKEN_KEY = 'apro_tokens';

export const apiClient = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL,
  headers: { 'Content-Type': 'application/json' },
});

apiClient.interceptors.request.use((config) => {
  const raw = localStorage.getItem(TOKEN_KEY);
  if (raw) {
    const { access_token } = JSON.parse(raw);
    config.headers.Authorization = `Bearer ${access_token}`;
  }
  return config;
});

apiClient.interceptors.response.use(
  (res) => res,
  (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem(TOKEN_KEY);
      window.location.href = '/login';
    }
    return Promise.reject(error);
  },
);
```

### 4.6 Route guard

```typescript
// src/auth/RequireAuth.tsx
import { Navigate, useLocation } from 'react-router';
import { useAuth } from './useAuth';
import { LoadingScreen } from '../components/ui/LoadingScreen';

interface Props {
  children: React.ReactNode;
  allowedRoles?: string[];  // e.g. ['superadmin', 'admin']
}

export function RequireAuth({ children, allowedRoles }: Props) {
  const { isAuthenticated, isLoading, user } = useAuth();
  const location = useLocation();

  if (isLoading) return <LoadingScreen />;
  if (!isAuthenticated) return <Navigate to="/login" state={{ from: location }} replace />;

  if (allowedRoles && user) {
    const groups = user['cognito:groups'] ?? [];
    const hasRole = allowedRoles.some((r) => groups.includes(r));
    if (!hasRole) return <Navigate to="/unauthorized" replace />;
  }

  return <>{children}</>;
}
```

---

## 5. Roles y permisos

Cognito define 4 grupos con precedencia:

| Grupo Cognito | Rol en app | Precedencia | Acceso sugerido en frontend |
|---------------|-----------|-------------|----------------------------|
| `superadmin` | SUPERADMIN | 0 | Todo: usuarios, empresas, Shopify, config global |
| `admin` | ADMIN | 10 | Usuarios de su empresa, empresas, leads, cotizaciones, pedidos |
| `sales` | SALES | 20 | Leads propios, cotizaciones, conversaciones |
| `kpi_visualizers` | KPI_VISUALIZERS | 30 | Dashboards read-only |

### Mapeo de navegación por rol

```typescript
// src/utils/navigation.ts
import type { DecodedUser } from '../auth/AuthContext';
import DashboardIcon from '@mui/icons-material/Dashboard';
import PeopleIcon from '@mui/icons-material/People';
import BusinessIcon from '@mui/icons-material/Business';
import LeaderboardIcon from '@mui/icons-material/Leaderboard';
// ... más iconos

interface NavItem {
  label: string;
  path: string;
  icon: React.ElementType;
  roles: string[];  // grupos Cognito permitidos
}

export const NAV_ITEMS: NavItem[] = [
  { label: 'Dashboard',     path: '/',              icon: DashboardIcon,    roles: ['superadmin', 'admin', 'sales', 'kpi_visualizers'] },
  { label: 'Usuarios',      path: '/users',         icon: PeopleIcon,       roles: ['superadmin', 'admin'] },
  { label: 'Empresas',      path: '/companies',     icon: BusinessIcon,     roles: ['superadmin', 'admin'] },
  { label: 'Leads',         path: '/leads',         icon: LeaderboardIcon,  roles: ['superadmin', 'admin', 'sales'] },
  { label: 'Cotizaciones',  path: '/quotes',        icon: LeaderboardIcon,  roles: ['superadmin', 'admin', 'sales'] },
  { label: 'Pedidos',       path: '/orders',        icon: LeaderboardIcon,  roles: ['superadmin', 'admin', 'sales'] },
  { label: 'Shopify',       path: '/shopify',       icon: LeaderboardIcon,  roles: ['superadmin'] },
];

export function getNavForUser(user: DecodedUser | null): NavItem[] {
  if (!user) return [];
  const groups = user['cognito:groups'] ?? [];
  return NAV_ITEMS.filter((item) =>
    item.roles.some((r) => groups.includes(r))
  );
}
```

---

## 6. Endpoints del backend — referencia completa

### 6.1 Auth (`apro-click-admin-auth`)

| Método | Ruta | Body | Respuesta exitosa |
|--------|------|------|-------------------|
| POST | `/api/v1/auth/login` | `{ email, password }` | `{ statusCode, message, data: AuthTokens \| AuthChallenge }` |
| POST | `/api/v1/auth/change-password` | `{ email, new_password, session }` | `{ data: AuthTokens }` |
| POST | `/api/v1/auth/forgot-password` | `{ email }` | `{ data: { message, delivery_medium, destination } }` |
| POST | `/api/v1/auth/confirm-forgot-password` | `{ email, confirmation_code, new_password }` | `{ data: { message } }` |
| POST | `/api/v1/auth/logout` | `{ access_token?, refresh_token? }` | `{ data: { message } }` |

**Errores comunes:**
- `400` — campos faltantes, política de contraseña incumplida, código expirado
- `401` — credenciales incorrectas, usuario deshabilitado, usuario no encontrado
- `500` — error inesperado de Cognito

### 6.2 Users (`apro-click-admin-users`)

| Método | Ruta | Body / Query | Respuesta |
|--------|------|-------------|-----------|
| POST | `/api/v1/users` | `{ email, given_name?, family_name?, role?, temporary_password? }` | `201 { data: User }` |
| GET | `/api/v1/users` | `?limit=50&offset=0` | `{ data: { users: User[] } }` |
| GET | `/api/v1/users/{user_id}` | — | `{ data: User & { cognito_groups } }` |
| PUT | `/api/v1/users/{user_id}` | `{ given_name?, family_name?, role?, status? }` | `{ data: User }` |
| POST | `/api/v1/users/{user_id}/link-shopify-staff` | — | `{ data: { shopify_staff_member_id?, shopify_staff_link_status, shopify_staff_link_message?, ... } }` (vínculo con Staff de Shopify buscando por el mismo **email** del usuario) |
| POST | `/api/v1/users/{user_id}/associate-shopify-staff` | `{ shopify_staff_member_gid, skip_email_verification? }` | `200` — asocia un Staff **ya creado** en Shopify por GID `gid://shopify/StaffMember/...` |
| DELETE | `/api/v1/users/{user_id}` | — | `{ message: "User deleted" }` |

**`user_id` en el path** = `users.id` (UUID) **o** `cognito_sub` (el backend acepta ambos).

**Guía detallada (Shopify staff en el admin, UX, tipos):** [GUIA_FRONTEND_ADMIN_SHOPIFY_STAFF.md](./GUIA_FRONTEND_ADMIN_SHOPIFY_STAFF.md).

**Tipo User (frontend):**

```typescript
// src/types/user.ts
export type UserRole   = 'SUPERADMIN' | 'ADMIN' | 'SALES' | 'KPI_VISUALIZERS';
export type UserStatus = 'ACTIVE' | 'DISABLED' | 'PENDING';

export interface User {
  id: string;           // UUID (PK en PostgreSQL)
  sub: string;          // cognito_sub (alias)
  cognito_sub: string;
  email: string;
  given_name: string;
  family_name: string;
  role: UserRole;
  status: UserStatus;
  company_id: string | null;
  created_at: string;   // ISO 8601
  updated_at: string;
}

export interface UserWithGroups extends User {
  cognito_groups: string[];
}

export interface CreateUserRequest {
  email: string;
  given_name?: string;
  family_name?: string;
  role?: UserRole;
  temporary_password?: string;
}

export interface UpdateUserRequest {
  given_name?: string;
  family_name?: string;
  role?: UserRole;
  status?: UserStatus;
}
```

### 6.3 Companies (`apro-click-admin-companies`)

| Método | Ruta | Body / Query | Respuesta |
|--------|------|-------------|-----------|
| POST | `/api/v1/companies` | `{ name, company_type?, payment_type? }` | `201 { data: Company }` |
| GET | `/api/v1/companies` | `?limit=50&offset=0` | `{ data: { companies: Company[] } }` |
| GET | `/api/v1/companies/{company_id}` | — | `{ data: Company }` |
| PUT | `/api/v1/companies/{company_id}` | `{ name?, company_type?, payment_type? }` | `{ data: Company }` |
| DELETE | `/api/v1/companies/{company_id}` | — | `{ message: "Company deleted" }` |

```typescript
// src/types/company.ts
export type CompanyType  = 'SMALL' | 'MEDIUM' | 'BIG';
export type PaymentType  = 'DIRECT' | 'CREDIT';

export interface Company {
  id: string;
  name: string;
  company_type: CompanyType;
  payment_type: PaymentType;
  is_system: boolean;
  created_at: string;
  updated_at: string;
}

export interface CreateCompanyRequest {
  name: string;
  company_type?: CompanyType;
  payment_type?: PaymentType;
}

export interface UpdateCompanyRequest {
  name?: string;
  company_type?: CompanyType;
  payment_type?: PaymentType;
}
```

### 6.4 Shopify (`apro-click-admin-shopify`)

| Método | Ruta | Query | Comportamiento |
|--------|------|-------|---------------|
| GET | `/api/v1/shopify/oauth/start` | `?shop=tienda.myshopify.com` (opcional) | **302** redirect a Shopify authorize |
| GET | `/api/v1/shopify/oauth/callback` | `code, shop, hmac, ...` (Shopify envía) | Valida HMAC, intercambia token, **302** a `SHOPIFY_CONNECTOR_URL` |

El flujo OAuth es server-side. El frontend solo necesita:

1. **Botón "Conectar Shopify"** → abre `GET /api/v1/shopify/oauth/start?shop=<dominio>` (en misma ventana o popup).
2. **Página Connector** (`SHOPIFY_CONNECTOR_URL`) recibe los query params: `shop`, `oauth_status=success`, `installation_id`.
3. Mostrar estado de conexión exitosa en el panel.

```typescript
// src/types/shopify.ts
export interface ShopifyInstallation {
  id: string;
  shop_domain: string;
  scopes: string | null;
  installed_at: string | null;
  uninstalled_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ShopifyConnectorParams {
  shop: string;
  oauth_status: 'success';
  installation_id: string;
}
```

### 6.5 Health (todos los servicios)

```
GET /api/v1/health-auth
GET /api/v1/health-users
GET /api/v1/health-companies
GET /api/v1/health-shopify
GET /api/v1/health-common
GET /api/v1/health-orders
GET /api/v1/health-leads
GET /api/v1/health-quotes
GET /api/v1/health-notifications
```

Respuesta: `{ "status": "healthy", "service": "<nombre>" }`.

### 6.6 Pedidos Shopify (API implementada)

El servicio **orders** expone listado, detalle e intervención sobre pedidos sincronizados vía webhooks. Contrato, roles, tipos TypeScript y ejemplos de cliente HTTP:

**[GUIA_FRONTEND_ADMIN_PEDIDOS_SHOPIFY.md](./GUIA_FRONTEND_ADMIN_PEDIDOS_SHOPIFY.md)**

Health: `GET /api/v1/health-orders`.

### 6.7 Otros servicios (solo health o rutas parciales)

| Servicio | Notas |
|----------|--------|
| **leads** | Rutas según evolución del servicio |
| **quotes** | Cotizaciones, borradores Shopify, etc. |
| **notifications** | Emails, tareas, recordatorios |

---

## 7. Modelo de datos del dominio (resumen para frontend)

```text
┌──────────────┐    1:N    ┌──────────────┐    1:N    ┌──────────────────┐
│  companies   │───────────│    users      │───────────│   audit_logs     │
│              │           │ (cognito_sub) │           │                  │
└──────┬───────┘           └──────┬────────┘           └──────────────────┘
       │ 1:N                      │ 1:N
       ▼                          ▼
┌──────────────┐           ┌──────────────┐
│   clients    │           │conversations │
│(shopify_id?) │           │ seller+client│
└──────┬───────┘           └──────┬───────┘
       │                          │ 1:N
       │                          ▼
       │                   ┌──────────────┐
       │                   │   messages    │
       │                   │ USER|CLIENT  │
       │                   └──────────────┘
       │
┌──────┴────────────────────┐         ┌─────────────────────────────┐
│ company_registration_     │         │  shopify_app_installations  │
│ requests                  │         │  (singleton por tienda)     │
└───────────────────────────┘         └─────────────────────────────┘
```

---

## 8. Páginas y componentes MUI por módulo

### 8.1 Login (`/login`)

**Componentes MUI:** `Container`, `Card`, `CardContent`, `TextField`, `Button`, `Alert`, `CircularProgress`, `Typography`, `Box`, `InputAdornment` (con `Visibility`/`VisibilityOff` icons).

**Subpáginas del flujo de login:**

| Ruta | Componente | Descripción |
|------|-----------|-------------|
| `/login` | `LoginPage` | Email + password, botón "Iniciar sesión" |
| `/login/change-password` | `ChangePasswordPage` | Se muestra si login devuelve challenge `NEW_PASSWORD_REQUIRED` |
| `/forgot-password` | `ForgotPasswordPage` | Solicitar código de recuperación |
| `/forgot-password/confirm` | `ConfirmForgotPasswordPage` | Código + nueva contraseña |

**Ejemplo de LoginForm:**

```tsx
// src/components/forms/LoginForm.tsx
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import {
  TextField, Button, Alert, Box, InputAdornment, IconButton,
} from '@mui/material';
import { Visibility, VisibilityOff } from '@mui/icons-material';
import { useState } from 'react';

const schema = z.object({
  email: z.string().email('Email inválido'),
  password: z.string().min(1, 'Requerido'),
});

type FormValues = z.infer<typeof schema>;

interface Props {
  onSubmit: (values: FormValues) => Promise<void>;
  error?: string | null;
  loading?: boolean;
}

export function LoginForm({ onSubmit, error, loading }: Props) {
  const [showPw, setShowPw] = useState(false);
  const { register, handleSubmit, formState: { errors } } = useForm<FormValues>({
    resolver: zodResolver(schema),
  });

  return (
    <Box component="form" onSubmit={handleSubmit(onSubmit)} noValidate sx={{ mt: 2 }}>
      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

      <TextField
        label="Correo electrónico"
        fullWidth
        autoFocus
        margin="normal"
        {...register('email')}
        error={!!errors.email}
        helperText={errors.email?.message}
      />

      <TextField
        label="Contraseña"
        type={showPw ? 'text' : 'password'}
        fullWidth
        margin="normal"
        {...register('password')}
        error={!!errors.password}
        helperText={errors.password?.message}
        slotProps={{
          input: {
            endAdornment: (
              <InputAdornment position="end">
                <IconButton onClick={() => setShowPw(!showPw)} edge="end">
                  {showPw ? <VisibilityOff /> : <Visibility />}
                </IconButton>
              </InputAdornment>
            ),
          },
        }}
      />

      <Button type="submit" variant="contained" fullWidth sx={{ mt: 3 }} disabled={loading}>
        {loading ? 'Ingresando...' : 'Iniciar sesión'}
      </Button>
    </Box>
  );
}
```

### 8.2 Dashboard Layout

**Componentes MUI:** `AppBar`, `Toolbar`, `Drawer` (permanente en desktop, temporal en mobile), `List`, `ListItemButton`, `ListItemIcon`, `ListItemText`, `Avatar`, `Menu`, `MenuItem`, `Divider`, `Box`, `CssBaseline`.

```text
┌─────────────────────────────────────────────────────────┐
│  AppBar  [Logo]           [nombre usuario ▼]  [Logout]  │
├─────────┬───────────────────────────────────────────────┤
│         │                                               │
│ Drawer  │            <Outlet />                         │
│         │         (contenido de página)                 │
│ - Dash  │                                               │
│ - Users │                                               │
│ - Co.   │                                               │
│ - Leads │                                               │
│ - ...   │                                               │
│         │                                               │
├─────────┴───────────────────────────────────────────────┤
│  Footer (opcional)                                       │
└─────────────────────────────────────────────────────────┘
```

**Ancho del Drawer:** `240px`. En mobile (`<md`): drawer temporal con hamburger en `AppBar`.

### 8.3 Gestión de Usuarios (`/users`)

| Ruta | Componente | MUI principal |
|------|-----------|---------------|
| `/users` | `UsersListPage` | `DataGrid` (MUI X), `Button`, `Chip`, `TextField` (search) |
| `/users/new` | `CreateUserPage` | `Card`, `TextField`, `Select`, `Button` |
| `/users/:id` | `UserDetailPage` | `Card`, `Tabs`, `Tab`, `Chip`, `Button` (añadir sección *Colaborador Shopify*: post `link-shopify-staff` / `associate-shopify-staff`, ver [GUIA_FRONTEND_ADMIN_SHOPIFY_STAFF.md](./GUIA_FRONTEND_ADMIN_SHOPIFY_STAFF.md)) |
| `/users/:id/edit` | `EditUserPage` | `TextField`, `Select`, `Switch`, `Button` |

**DataGrid columns (lista):**

```typescript
const columns: GridColDef[] = [
  { field: 'email', headerName: 'Email', flex: 1 },
  { field: 'given_name', headerName: 'Nombre', width: 150 },
  { field: 'family_name', headerName: 'Apellido', width: 150 },
  {
    field: 'role', headerName: 'Rol', width: 140,
    renderCell: (params) => <Chip label={params.value} size="small" color="primary" variant="outlined" />,
  },
  {
    field: 'status', headerName: 'Estado', width: 120,
    renderCell: (params) => (
      <Chip
        label={params.value}
        size="small"
        color={params.value === 'ACTIVE' ? 'success' : params.value === 'PENDING' ? 'warning' : 'error'}
      />
    ),
  },
  { field: 'created_at', headerName: 'Creado', width: 160, valueFormatter: (value) => dayjs(value).format('DD/MM/YYYY HH:mm') },
];
```

**Formulario de creación:**
- `email` — `TextField` (required)
- `given_name` — `TextField`
- `family_name` — `TextField`
- `role` — `Select` con opciones: SUPERADMIN, ADMIN, SALES, KPI_VISUALIZERS
- `temporary_password` — `TextField` (optional; si se omite Cognito genera una)

### 8.4 Gestión de Empresas (`/companies`)

| Ruta | Componente | MUI principal |
|------|-----------|---------------|
| `/companies` | `CompaniesListPage` | `DataGrid`, `Button`, `Chip` |
| `/companies/new` | `CreateCompanyPage` | `Card`, `TextField`, `Select` |
| `/companies/:id` | `CompanyDetailPage` | `Card`, `Tabs` (info, usuarios, clientes) |
| `/companies/:id/edit` | `EditCompanyPage` | `TextField`, `Select` |

**Campos del formulario:**
- `name` — `TextField` (required)
- `company_type` — `Select`: SMALL, MEDIUM, BIG
- `payment_type` — `Select`: DIRECT, CREDIT

### 8.5 Conexión Shopify (`/shopify`)

| Ruta | Componente | MUI principal |
|------|-----------|---------------|
| `/shopify` | `ShopifyDashboardPage` | `Card`, `Button`, `Alert`, `Typography` |
| `/shopify/connector` | `ShopifyConnectorPage` | `Card`, `CircularProgress`, `Alert` |

**Flujo UI:**

1. **Panel Shopify:** muestra estado de conexión actual (dominio, fecha de instalación, scopes).
2. **Botón "Conectar tienda"**: abre input para dominio `.myshopify.com` → redirige a `/api/v1/shopify/oauth/start?shop=<dominio>`.
3. **Página Connector** (`/shopify/connector`): parsea `?shop=...&oauth_status=success&installation_id=...` de la URL de retorno, muestra confirmación y redirige al dashboard.

```tsx
// Esquema del botón de conexión
<Card>
  <CardContent>
    <Typography variant="h6">Conexión Shopify</Typography>
    {installation ? (
      <Alert severity="success">
        Conectado a <strong>{installation.shop_domain}</strong>
      </Alert>
    ) : (
      <>
        <TextField
          label="Dominio de la tienda"
          placeholder="tu-tienda.myshopify.com"
          value={shopDomain}
          onChange={(e) => setShopDomain(e.target.value)}
        />
        <Button
          variant="contained"
          onClick={() => {
            window.location.href =
              `${API_BASE}/api/v1/shopify/oauth/start?shop=${shopDomain}`;
          }}
        >
          Conectar con Shopify
        </Button>
      </>
    )}
  </CardContent>
</Card>
```

### 8.6 Módulos futuros (placeholder)

Para los servicios que hoy solo tienen health, crear páginas stub con `Card` + `Typography` indicando "Módulo en construcción":

| Módulo | Ruta | Componentes previstos |
|--------|------|----------------------|
| **Leads** | `/leads` | `DataGrid`, formulario de captura, pipeline Kanban (`@mui/material` Drag & Drop o lib externa) |
| **Cotizaciones** | `/quotes` | `DataGrid`, detalle con líneas de producto, enlace a Shopify Draft Order |
| **Pedidos** | `/orders` | Ver [GUIA_FRONTEND_ADMIN_PEDIDOS_SHOPIFY.md](./GUIA_FRONTEND_ADMIN_PEDIDOS_SHOPIFY.md): `DataGrid`, detalle, notas de intervención, enlace a Shopify |
| **Notificaciones** | `/notifications` | `List`, `Badge`, `Snackbar` |

---

## 9. Routing completo

```tsx
// src/router.tsx
import { createBrowserRouter } from 'react-router';
import { DashboardLayout } from './components/layout/DashboardLayout';
import { RequireAuth } from './auth/RequireAuth';

// Auth pages (públicas)
import { LoginPage } from './features/auth/LoginPage';
import { ChangePasswordPage } from './features/auth/ChangePasswordPage';
import { ForgotPasswordPage } from './features/auth/ForgotPasswordPage';
import { ConfirmForgotPasswordPage } from './features/auth/ConfirmForgotPasswordPage';

// Protected pages
import { DashboardPage } from './features/dashboard/DashboardPage';
import { UsersListPage } from './features/users/UsersListPage';
import { CreateUserPage } from './features/users/CreateUserPage';
import { UserDetailPage } from './features/users/UserDetailPage';
import { CompaniesListPage } from './features/companies/CompaniesListPage';
import { CreateCompanyPage } from './features/companies/CreateCompanyPage';
import { CompanyDetailPage } from './features/companies/CompanyDetailPage';
import { ShopifyDashboardPage } from './features/shopify/ShopifyDashboardPage';
import { ShopifyConnectorPage } from './features/shopify/ShopifyConnectorPage';

export const router = createBrowserRouter([
  // --- Public ---
  { path: '/login', element: <LoginPage /> },
  { path: '/login/change-password', element: <ChangePasswordPage /> },
  { path: '/forgot-password', element: <ForgotPasswordPage /> },
  { path: '/forgot-password/confirm', element: <ConfirmForgotPasswordPage /> },
  { path: '/shopify/connector', element: <ShopifyConnectorPage /> },

  // --- Protected ---
  {
    element: (
      <RequireAuth>
        <DashboardLayout />
      </RequireAuth>
    ),
    children: [
      { index: true, element: <DashboardPage /> },

      // Users (superadmin, admin)
      { path: 'users',      element: <RequireAuth allowedRoles={['superadmin', 'admin']}><UsersListPage /></RequireAuth> },
      { path: 'users/new',  element: <RequireAuth allowedRoles={['superadmin', 'admin']}><CreateUserPage /></RequireAuth> },
      { path: 'users/:id',  element: <RequireAuth allowedRoles={['superadmin', 'admin']}><UserDetailPage /></RequireAuth> },

      // Companies (superadmin, admin)
      { path: 'companies',      element: <RequireAuth allowedRoles={['superadmin', 'admin']}><CompaniesListPage /></RequireAuth> },
      { path: 'companies/new',  element: <RequireAuth allowedRoles={['superadmin', 'admin']}><CreateCompanyPage /></RequireAuth> },
      { path: 'companies/:id',  element: <RequireAuth allowedRoles={['superadmin', 'admin']}><CompanyDetailPage /></RequireAuth> },

      // Shopify (superadmin)
      { path: 'shopify', element: <RequireAuth allowedRoles={['superadmin']}><ShopifyDashboardPage /></RequireAuth> },

      // Future modules
      { path: 'leads',         element: <PlaceholderPage title="Leads" /> },
      { path: 'quotes',        element: <PlaceholderPage title="Cotizaciones" /> },
      { path: 'orders',        element: <PlaceholderPage title="Pedidos" /> },
      { path: 'notifications', element: <PlaceholderPage title="Notificaciones" /> },
    ],
  },
]);
```

---

## 10. App entry point

```tsx
// src/App.tsx
import { RouterProvider } from 'react-router';
import { ThemeProvider, CssBaseline } from '@mui/material';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { SnackbarProvider } from 'notistack';
import { AuthProvider } from './auth/AuthProvider';
import { router } from './router';
import theme from './theme';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false },
  },
});

export default function App() {
  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <QueryClientProvider client={queryClient}>
        <SnackbarProvider maxSnack={3} anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}>
          <AuthProvider>
            <RouterProvider router={router} />
          </AuthProvider>
        </SnackbarProvider>
      </QueryClientProvider>
    </ThemeProvider>
  );
}
```

---

## 11. Hooks reutilizables con React Query

```typescript
// src/features/users/useUsers.ts
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { usersApi } from '../../api/users.api';
import type { CreateUserRequest, UpdateUserRequest } from '../../types/user';

export function useUsers(limit = 50, offset = 0) {
  return useQuery({
    queryKey: ['users', { limit, offset }],
    queryFn: () => usersApi.list(limit, offset).then((r) => r.data.data.users),
  });
}

export function useUser(userId: string) {
  return useQuery({
    queryKey: ['users', userId],
    queryFn: () => usersApi.get(userId).then((r) => r.data.data),
    enabled: !!userId,
  });
}

export function useCreateUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: CreateUserRequest) => usersApi.create(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['users'] }),
  });
}

export function useUpdateUser(userId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: UpdateUserRequest) => usersApi.update(userId, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['users'] });
      qc.invalidateQueries({ queryKey: ['users', userId] });
    },
  });
}

export function useDeleteUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (userId: string) => usersApi.delete(userId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['users'] }),
  });
}
```

```typescript
// src/api/users.api.ts
import { apiClient } from './client';
import type { CreateUserRequest, UpdateUserRequest } from '../types/user';

const BASE = '/api/v1/users';

export const usersApi = {
  list:   (limit = 50, offset = 0) => apiClient.get(BASE, { params: { limit, offset } }),
  get:    (id: string)             => apiClient.get(`${BASE}/${id}`),
  create: (body: CreateUserRequest)           => apiClient.post(BASE, body),
  update: (id: string, body: UpdateUserRequest) => apiClient.put(`${BASE}/${id}`, body),
  delete: (id: string)             => apiClient.delete(`${BASE}/${id}`),
};
```

Mismo patrón aplica para `companies.api.ts` y hooks de empresas.

---

## 12. Política de contraseñas (Cognito — para validación en frontend)

Configurada en el User Pool:

| Regla | Valor |
|-------|-------|
| Largo mínimo | 8 caracteres |
| Mayúscula | requerida |
| Minúscula | requerida |
| Número | requerido |
| Símbolo | **no** requerido |
| Contraseña temporal válida | 7 días |

```typescript
// Validación Zod para formularios de contraseña
const passwordSchema = z
  .string()
  .min(8, 'Mínimo 8 caracteres')
  .regex(/[A-Z]/, 'Debe incluir al menos una mayúscula')
  .regex(/[a-z]/, 'Debe incluir al menos una minúscula')
  .regex(/[0-9]/, 'Debe incluir al menos un número');
```

---

## 13. Manejo de errores (convención)

Todos los endpoints devuelven:

```json
{
  "statusCode": 400,
  "message": "Descripción del error"
}
```

El interceptor de Axios puede extraer `message` para mostrar en un `Snackbar`:

```typescript
apiClient.interceptors.response.use(
  (res) => res,
  (error) => {
    const msg = error.response?.data?.message ?? 'Error inesperado';
    // Emitir a notistack u otro sistema de notificaciones
    return Promise.reject(new Error(msg));
  },
);
```

---

## 14. Paginación

El backend usa paginación **offset + limit** (no cursor). Implementar con el componente `TablePagination` de MUI o la paginación integrada de `DataGrid`:

```typescript
// DataGrid con paginación server-side
<DataGrid
  rows={users}
  columns={columns}
  paginationMode="server"
  rowCount={totalCount}
  paginationModel={{ page, pageSize: limit }}
  onPaginationModelChange={({ page, pageSize }) => {
    setPage(page);
    setLimit(pageSize);
  }}
  pageSizeOptions={[10, 25, 50]}
/>
```

---

## 15. Deploy sugerido

| Opción | Detalle |
|--------|---------|
| **AWS S3 + CloudFront** | Bucket privado + distribución CDN; ideal para integrar con el dominio del API |
| **Vercel** | Zero-config para Vite + React |
| **AWS Amplify Hosting** | Integración directa con el repo |

Variables de entorno de producción:
- `VITE_API_BASE_URL` → URL del API Gateway (o dominio custom)
- `VITE_SHOPIFY_CONNECTOR_URL` → URL de la página connector para redirección post-OAuth

---

## 16. Checklist de implementación

- [ ] Scaffold Vite + React + TypeScript
- [ ] Instalar MUI v6 + MUI X DataGrid + MUI X Charts
- [ ] Configurar tema (`src/theme/index.ts`)
- [ ] Implementar `AuthProvider` + `useAuth` + `RequireAuth`
- [ ] Crear `LoginPage` con flujo completo (login → challenge → change password)
- [ ] Crear `ForgotPasswordPage` + `ConfirmForgotPasswordPage`
- [ ] Implementar `DashboardLayout` (AppBar + Drawer + Outlet)
- [ ] Navegación por roles (filtrar menú según `cognito:groups`)
- [ ] CRUD Usuarios (`DataGrid` + formularios)
- [ ] CRUD Empresas (`DataGrid` + formularios)
- [ ] Panel Shopify (estado de conexión + botón OAuth)
- [ ] Página Connector Shopify (recibe redirección post-OAuth)
- [ ] Dashboard con cards resumen (total usuarios, empresas, estado health)
- [ ] Placeholder pages para leads, quotes, orders, notifications
- [ ] Manejo de errores global (interceptor + Snackbar)
- [ ] Deploy a S3+CloudFront / Vercel / Amplify

# Guía Frontend — Chat con WebSocket, Typing Indicators y Adjuntos

Esta guía documenta la integración del chat **v2** con soporte de WebSocket (mensajes en tiempo real, typing indicator) y adjuntos (imágenes, Excel, PDF).

**Estado actual (stage `dev`):**

| Servicio | URL |
|---|---|
| REST API (chat) | `https://30yyq9wfd2.execute-api.us-east-2.amazonaws.com` |
| WebSocket API | `wss://eenu72b5v4.execute-api.us-east-2.amazonaws.com/dev` |
| S3 bucket adjuntos | `aproclick-dev-chat-attachments` (acceso privado, solo por presigned URLs) |

---

## Índice

1. [Cambios en el modelo de mensaje](#1-cambios-en-el-modelo-de-mensaje)
2. [Panel admin (staff) — REST + WebSocket](#2-panel-admin-staff--rest--websocket)
3. [Theme App Extension (storefront Shopify)](#3-theme-app-extension-storefront-shopify)
4. [Typing indicator](#4-typing-indicator)
5. [Adjuntos — panel admin](#5-adjuntos--panel-admin)
6. [Adjuntos — theme app extension](#6-adjuntos--theme-app-extension)
7. [Cliente JS reutilizable — panel admin](#7-cliente-js-reutilizable--panel-admin)
8. [Cliente JS reutilizable — theme extension](#8-cliente-js-reutilizable--theme-extension)
9. [Errores frecuentes](#9-errores-frecuentes)

---

## 1. Cambios en el modelo de mensaje

El objeto mensaje ahora incluye dos campos nuevos:

```json
{
  "id": "uuid",
  "conversation_id": "uuid",
  "sender_type": "USER | CLIENT",
  "message_type": "TEXT | IMAGE | FILE",
  "body": "texto del mensaje o null si es solo adjunto",
  "attachment_url": "https://presigned-url-s3... (null si no hay adjunto)",
  "created_at": "2026-04-23T18:00:00+00:00"
}
```

| Campo | Descripción |
|---|---|
| `message_type` | `TEXT` (default), `IMAGE` (imagen), `FILE` (Excel, PDF) |
| `attachment_url` | Presigned URL de S3 con **1 hora** de validez. Si expira, listar mensajes de nuevo para URLs frescas. |
| `body` | Puede ser `null` si el mensaje es solo un adjunto. |

---

## 2. Panel admin (staff) — REST + WebSocket

### 2.1 Endpoints REST disponibles

| Método | Ruta | Auth | Descripción |
|---|---|---|---|
| `GET` | `/api/v1/chat/conversations` | Bearer | Listar conversaciones |
| `GET` | `/api/v1/chat/conversations/{conv_id}/messages` | Bearer | Mensajes del hilo |
| `POST` | `/api/v1/chat/conversations/{conv_id}/messages` | Bearer | Enviar mensaje (texto o adjunto) |
| `POST` | `/api/v1/chat/conversations/{conv_id}/upload` | Bearer | Solicitar presigned URL para subir adjunto |

### 2.2 Conexión WebSocket

```
wss://eenu72b5v4.execute-api.us-east-2.amazonaws.com/dev
  ?token=<CognitoAccessToken>
  &sender_type=USER
  &conv_id=<conversation_id_uuid>
```

| Parámetro | Descripción |
|---|---|
| `token` | Access Token de Cognito (mismo que el `Bearer` del REST) |
| `sender_type` | Siempre `USER` para el panel |
| `conv_id` | UUID de la conversación activa |

**Regla:** conectar al abrir un hilo, desconectar al cerrarlo o cambiar de conversación.

### 2.3 Reconexión automática con backoff

API Gateway cierra conexiones inactivas tras **10 minutos**. Implementar reconexión:

```javascript
const BACKOFF_MS = [1000, 2000, 4000, 8000, 16000];
let retryCount = 0;

function connectAdmin(convId, token) {
  const url = `wss://eenu72b5v4.execute-api.us-east-2.amazonaws.com/dev`
    + `?token=${encodeURIComponent(token)}`
    + `&sender_type=USER`
    + `&conv_id=${convId}`;

  const ws = new WebSocket(url);

  ws.onopen = () => { retryCount = 0; startKeepalive(ws); };
  ws.onmessage = ({ data }) => handleWsMessage(JSON.parse(data));
  ws.onerror = () => ws.close();
  ws.onclose = (e) => {
    stopKeepalive();
    if (e.code !== 1000) { // 1000 = cierre limpio intencional
      const delay = BACKOFF_MS[Math.min(retryCount++, BACKOFF_MS.length - 1)];
      setTimeout(() => connectAdmin(convId, token), delay);
    }
  };
  return ws;
}
```

### 2.4 Keepalive (evitar corte por inactividad)

```javascript
let keepaliveInterval = null;

function startKeepalive(ws) {
  keepaliveInterval = setInterval(() => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ action: 'ping' }));
    }
  }, 8 * 60 * 1000); // cada 8 minutos
}

function stopKeepalive() {
  clearInterval(keepaliveInterval);
  keepaliveInterval = null;
}
```

El servidor responde `{ "type": "pong" }` — ignorar en la UI.

---

## 3. Theme App Extension (storefront Shopify)

### 3.1 Endpoints REST públicos disponibles

| Método | Ruta | Auth | Descripción |
|---|---|---|---|
| `POST` | `/api/v1/chat/public/messages` | X-Api-Key (opcional) | Enviar mensaje (texto o adjunto) |
| `GET` | `/api/v1/chat/public/conversations/{conv_id}/messages` | X-Api-Key (opcional) | Listar mensajes del hilo |
| `POST` | `/api/v1/chat/public/conversations/{conv_id}/upload` | X-Api-Key (opcional) | Solicitar presigned URL para subir imagen |

> **Nota sobre adjuntos:** desde el canal público solo se permiten **imágenes** (jpeg, png, webp, gif). Los archivos Excel y PDF solo pueden subirlos los vendedores desde el panel admin.

### 3.2 Parámetros comunes a todos los endpoints públicos

Todos los POST/GET públicos requieren identificar la empresa y el cliente:

| Campo | Dónde va | Descripción |
|---|---|---|
| `shop` | body/query | Dominio de la tienda, ej. `mi-tienda.myshopify.com` |
| `shopify_customer_id` | body/query | ID numérico del Customer de Shopify (solo clientes logueados) |
| `company_id` | body/query | UUID de `companies.id` en el CRM (al menos uno de los dos) |
| `shopify_company_id` | body/query | ID B2B numérico de Shopify Company (al menos uno de los dos) |

### 3.3 Configuración en Liquid

```liquid
{% comment %}
  Inyectar los datos de configuración en el bloque del chat.
  Los valores sensibles (api_key) deben venir de metafields o app proxy,
  nunca hardcodeados en el tema público.
{% endcomment %}

{% if customer %}
  <div
    id="apro-chat-root"
    data-rest-base="https://30yyq9wfd2.execute-api.us-east-2.amazonaws.com"
    data-ws-base="wss://eenu72b5v4.execute-api.us-east-2.amazonaws.com/dev"
    data-shop="{{ shop.permanent_domain | escape }}"
    data-customer-id="{{ customer.id | escape }}"
    data-customer-email="{{ customer.email | escape }}"
    data-customer-name="{{ customer.first_name | escape }} {{ customer.last_name | escape }}"
    data-company-id="{{ block.settings.crm_company_id | escape }}"
    data-api-key="{{ block.settings.api_key | escape }}"
  ></div>
  <script src="{{ 'apro-chat.js' | asset_url }}" defer></script>
{% else %}
  <p class="apro-chat-login">
    <a href="{{ routes.account_login_url }}">Inicia sesión</a> para chatear con nosotros.
  </p>
{% endif %}
```

**Schema del bloque** (en el `{% schema %}` del bloque Liquid):

```json
{
  "name": "Chat Apro Click",
  "settings": [
    {
      "type": "text",
      "id": "crm_company_id",
      "label": "UUID de empresa (CRM)",
      "info": "UUID de companies.id. Obtener desde el panel de administración."
    },
    {
      "type": "text",
      "id": "api_key",
      "label": "API Key del chat (opcional)",
      "info": "Dejar vacío si no se requiere autenticación en dev."
    }
  ]
}
```

### 3.4 Flujo completo del cliente (primera visita y visitas posteriores)

```
Primera visita (sin conversación guardada):
  1. Cliente escribe mensaje
  2. POST /public/messages → crea conversación + mensaje, retorna conversation_id
  3. Guardar conversation_id en localStorage
  4. Conectar WebSocket con el conv_id recibido

Visitas posteriores (conv_id en localStorage):
  1. Al montar el widget, GET /public/conversations/{conv_id}/messages → cargar historial
  2. Conectar WebSocket directamente con el conv_id guardado
  3. Los mensajes nuevos llegan por WS en tiempo real
```

### 3.5 Conexión WebSocket desde el tema

```
wss://eenu72b5v4.execute-api.us-east-2.amazonaws.com/dev
  ?sender_type=CLIENT
  &client_id=<shopify_customer_id_numerico>
  &conv_id=<conversation_id_uuid>
  &api_key=<CHAT_STOREFRONT_API_KEY>   ← solo si la Lambda tiene clave configurada
```

**Importante:** el WebSocket solo se puede conectar cuando ya existe `conv_id`. Si el cliente no ha enviado ningún mensaje todavía, no hay `conv_id` y no hay WebSocket — el primer mensaje se envía por REST.

```javascript
function connectStorefront(convId, customerId, apiKey) {
  let url = `wss://eenu72b5v4.execute-api.us-east-2.amazonaws.com/dev`
    + `?sender_type=CLIENT`
    + `&client_id=${customerId}`
    + `&conv_id=${convId}`;

  if (apiKey) url += `&api_key=${encodeURIComponent(apiKey)}`;

  const ws = new WebSocket(url);
  ws.onmessage = ({ data }) => handleWsMessage(JSON.parse(data));
  ws.onerror = () => ws.close();
  ws.onclose = () => clearInterval(keepaliveInterval);

  keepaliveInterval = setInterval(() => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ action: 'ping' }));
    }
  }, 8 * 60 * 1000);

  return ws;
}
```

### 3.6 Enviar primer mensaje (crea la conversación)

```javascript
const REST_BASE = 'https://30yyq9wfd2.execute-api.us-east-2.amazonaws.com';

async function sendFirstMessage({ shop, customerId, crmCompanyId, b2bCompanyId, apiKey, text, email, name }) {
  const headers = { 'Content-Type': 'application/json' };
  if (apiKey) headers['X-Api-Key'] = apiKey;

  const body = {
    shop,
    shopify_customer_id: String(customerId),
    body: text,
    message_type: 'TEXT',
    ...(email ? { email } : {}),
    ...(name ? { name } : {}),
  };
  if (crmCompanyId) body.company_id = crmCompanyId;
  if (b2bCompanyId) body.shopify_company_id = String(b2bCompanyId);

  const r = await fetch(`${REST_BASE}/api/v1/chat/public/messages`, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
  });
  const json = await r.json();
  if (!r.ok) throw new Error(json.message || r.statusText);

  // Retorna { conversation_id, company_id, shopify_company_id, message }
  return json.data;
}
```

### 3.7 Cargar historial de mensajes

```javascript
async function loadHistory({ convId, shop, customerId, crmCompanyId, b2bCompanyId, apiKey, limit = 50, offset = 0 }) {
  const headers = {};
  if (apiKey) headers['X-Api-Key'] = apiKey;

  const q = new URLSearchParams({
    shop,
    shopify_customer_id: String(customerId),
    limit,
    offset,
  });
  if (crmCompanyId) q.set('company_id', crmCompanyId);
  if (b2bCompanyId) q.set('shopify_company_id', String(b2bCompanyId));

  const r = await fetch(
    `${REST_BASE}/api/v1/chat/public/conversations/${convId}/messages?${q}`,
    { headers }
  );
  const json = await r.json();
  if (!r.ok) throw new Error(json.message || r.statusText);
  return json.data; // { conversation_id, messages: [...] }
}
```

### 3.8 Clave de localStorage (evitar mezcla de hilos)

Usar una clave que incluya la empresa para no reutilizar un hilo de otra compañía al cambiar de sesión B2B:

```javascript
function convStorageKey(shop, customerId, crmCompanyId, b2bCompanyId) {
  const companyRef = crmCompanyId || b2bCompanyId || 'default';
  return `apro_conv_${shop}_${customerId}_${companyRef}`;
}

function saveConvId(key, convId) {
  try { localStorage.setItem(key, convId); } catch { /* private browsing */ }
}

function loadConvId(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
```

---

## 4. Typing indicator

### 4.1 Enviar "está escribiendo"

```javascript
// Con debounce para no saturar (enviar máximo 1 evento cada 2 segundos)
let lastTypingSent = 0;

function sendTyping(ws, conversationId) {
  const now = Date.now();
  if (ws?.readyState === WebSocket.OPEN && now - lastTypingSent > 2000) {
    ws.send(JSON.stringify({ action: 'typing', conversation_id: conversationId }));
    lastTypingSent = now;
  }
}

// En el textarea:
textarea.addEventListener('input', () => sendTyping(ws, currentConversationId));
```

### 4.2 Recibir y mostrar el indicador

```javascript
let typingHideTimeout = null;

function handleWsMessage(msg) {
  switch (msg.type) {
    case 'typing':
      // sender_type "USER" → vendedor escribe (mostrar en tienda)
      // sender_type "CLIENT" → cliente escribe (mostrar en panel admin)
      showTypingIndicator(msg.sender_type);
      clearTimeout(typingHideTimeout);
      typingHideTimeout = setTimeout(hideTypingIndicator, 3000);
      break;
    case 'new_message':
      appendMessage(msg.message);
      break;
    case 'pong':
      break; // keepalive, ignorar
  }
}
```

---

## 5. Adjuntos — panel admin

Solo el staff puede subir **imágenes, Excel y PDF**.

### 5.1 Tipos de archivo permitidos (panel)

| Tipo MIME | Extensión |
|---|---|
| `image/jpeg` | .jpg / .jpeg |
| `image/png` | .png |
| `image/webp` | .webp |
| `image/gif` | .gif |
| `application/pdf` | .pdf |
| `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | .xlsx |
| `application/vnd.ms-excel` | .xls |

### 5.2 Flujo en 3 pasos

**Paso 1 — Pedir presigned URL**

```
POST /api/v1/chat/conversations/{conv_id}/upload
Authorization: Bearer <token>
Content-Type: application/json

{ "filename": "catalogo.xlsx", "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" }
```

Respuesta `200`:
```json
{
  "data": {
    "upload_url": "https://aproclick-dev-chat-attachments.s3.amazonaws.com/...?X-Amz-...",
    "attachment_key": "uuid-empresa/uuid-conv/uuid-file/catalogo.xlsx",
    "expires_in": 300
  }
}
```

**Paso 2 — Subir directamente a S3** (sin pasar por Lambda)

```javascript
await fetch(upload_url, {
  method: 'PUT',
  headers: { 'Content-Type': file.type },
  body: file,
  // NO incluir Authorization — la URL ya está firmada
});
```

**Paso 3 — Confirmar el mensaje**

```
POST /api/v1/chat/conversations/{conv_id}/messages
Authorization: Bearer <token>

{
  "message_type": "FILE",
  "attachment_key": "uuid-empresa/uuid-conv/uuid-file/catalogo.xlsx",
  "body": null
}
```

---

## 6. Adjuntos — theme app extension

Los clientes de la tienda solo pueden subir **imágenes** (jpeg, png, webp, gif).

### 6.1 Flujo en 3 pasos (cliente)

**Paso 1 — Pedir presigned URL**

```
POST /api/v1/chat/public/conversations/{conv_id}/upload
X-Api-Key: <CHAT_STOREFRONT_API_KEY>  ← solo si está configurada
Content-Type: application/json

{
  "shop": "mi-tienda.myshopify.com",
  "shopify_customer_id": "1234567890",
  "company_id": "uuid-empresa",
  "filename": "foto-producto.jpg",
  "content_type": "image/jpeg"
}
```

Respuesta `200`:
```json
{
  "data": {
    "upload_url": "https://aproclick-dev-chat-attachments.s3.amazonaws.com/...?X-Amz-...",
    "attachment_key": "uuid-empresa/uuid-conv/uuid-file/foto-producto.jpg",
    "expires_in": 300
  }
}
```

**Paso 2 — Subir directamente a S3**

```javascript
await fetch(upload_url, {
  method: 'PUT',
  headers: { 'Content-Type': 'image/jpeg' },
  body: file,
});
```

**Paso 3 — Confirmar el mensaje**

```
POST /api/v1/chat/public/messages
X-Api-Key: <CHAT_STOREFRONT_API_KEY>

{
  "shop": "mi-tienda.myshopify.com",
  "shopify_customer_id": "1234567890",
  "company_id": "uuid-empresa",
  "message_type": "IMAGE",
  "attachment_key": "uuid-empresa/uuid-conv/uuid-file/foto-producto.jpg",
  "body": null
}
```

### 6.2 Función completa de envío de imagen (storefront)

```javascript
async function sendImageStorefront({ file, shop, customerId, crmCompanyId, b2bCompanyId, convId, apiKey }) {
  const headers = { 'Content-Type': 'application/json' };
  if (apiKey) headers['X-Api-Key'] = apiKey;

  const uploadBody = {
    shop,
    shopify_customer_id: String(customerId),
    filename: file.name,
    content_type: file.type,
  };
  if (crmCompanyId) uploadBody.company_id = crmCompanyId;
  if (b2bCompanyId) uploadBody.shopify_company_id = String(b2bCompanyId);

  // 1. Pedir URL
  const uploadRes = await fetch(
    `${REST_BASE}/api/v1/chat/public/conversations/${convId}/upload`,
    { method: 'POST', headers, body: JSON.stringify(uploadBody) }
  );
  if (!uploadRes.ok) {
    const err = await uploadRes.json();
    throw new Error(err.message || 'Error al obtener URL de subida');
  }
  const { upload_url, attachment_key } = (await uploadRes.json()).data;

  // 2. Subir a S3
  await fetch(upload_url, {
    method: 'PUT',
    headers: { 'Content-Type': file.type },
    body: file,
  });

  // 3. Confirmar mensaje
  const msgBody = {
    shop,
    shopify_customer_id: String(customerId),
    message_type: 'IMAGE',
    attachment_key,
    body: null,
  };
  if (crmCompanyId) msgBody.company_id = crmCompanyId;
  if (b2bCompanyId) msgBody.shopify_company_id = String(b2bCompanyId);

  const msgRes = await fetch(
    `${REST_BASE}/api/v1/chat/public/messages`,
    { method: 'POST', headers, body: JSON.stringify(msgBody) }
  );
  const msgJson = await msgRes.json();
  if (!msgRes.ok) throw new Error(msgJson.message || 'Error al confirmar mensaje');
  return msgJson.data;
}
```

### 6.3 Input de archivo en el widget

```html
<!-- Botón de adjuntar imagen en el widget del tema -->
<label class="apro-attach-btn" aria-label="Adjuntar imagen">
  <input
    type="file"
    accept="image/jpeg,image/png,image/webp,image/gif"
    style="display:none"
    id="apro-file-input"
  />
  📎
</label>
```

```javascript
document.getElementById('apro-file-input').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  if (!file) return;

  if (file.size > 5 * 1024 * 1024) {
    alert('La imagen no puede superar 5 MB');
    return;
  }

  try {
    showUploadProgress();
    await sendImageStorefront({ file, shop, customerId, crmCompanyId, convId, apiKey });
    // El mensaje llegará por WebSocket en tiempo real
  } catch (err) {
    alert('Error al enviar la imagen: ' + err.message);
  } finally {
    hideUploadProgress();
    e.target.value = ''; // limpiar input para permitir subir el mismo archivo de nuevo
  }
});
```

---

## 7. Cliente JS reutilizable — panel admin

```javascript
// AdminChatClient.js
const CHAT_REST_BASE = 'https://30yyq9wfd2.execute-api.us-east-2.amazonaws.com';
const CHAT_WS_BASE   = 'wss://eenu72b5v4.execute-api.us-east-2.amazonaws.com/dev';

export class AdminChatClient {
  constructor({ cognitoToken, onMessage, onTyping }) {
    this._token = cognitoToken;
    this._onMessage = onMessage;
    this._onTyping = onTyping;
    this._ws = null;
    this._keepalive = null;
    this._retryCount = 0;
    this._convId = null;
  }

  // ── REST ─────────────────────────────────────────────────────────────────

  async listConversations({ limit = 30, offset = 0, status, companyId } = {}) {
    const q = new URLSearchParams({ limit, offset });
    if (status) q.set('status', status);
    if (companyId) q.set('company_id', companyId);
    return this._get(`/api/v1/chat/conversations?${q}`);
  }

  async listMessages(convId, { limit = 100, offset = 0, companyId } = {}) {
    const q = new URLSearchParams({ limit, offset });
    if (companyId) q.set('company_id', companyId);
    return this._get(`/api/v1/chat/conversations/${convId}/messages?${q}`);
  }

  async sendText(convId, text, companyId) {
    return this._post(
      `/api/v1/chat/conversations/${convId}/messages`,
      { body: text, message_type: 'TEXT' },
      companyId,
    );
  }

  async sendFile(convId, file) {
    const { upload_url, attachment_key } = await this._post(
      `/api/v1/chat/conversations/${convId}/upload`,
      { filename: file.name, content_type: file.type },
    );
    await fetch(upload_url, {
      method: 'PUT',
      headers: { 'Content-Type': file.type },
      body: file,
    });
    return this._post(`/api/v1/chat/conversations/${convId}/messages`, {
      message_type: file.type.startsWith('image/') ? 'IMAGE' : 'FILE',
      attachment_key,
    });
  }

  // ── WebSocket ─────────────────────────────────────────────────────────────

  connectToConversation(convId) {
    this.disconnect();
    this._convId = convId;
    this._openWs();
  }

  disconnect() {
    this._convId = null;
    this._retryCount = 0;
    clearInterval(this._keepalive);
    if (this._ws) {
      this._ws.onclose = null;
      this._ws.close(1000);
      this._ws = null;
    }
  }

  sendTyping() {
    if (this._ws?.readyState === WebSocket.OPEN && this._convId) {
      this._ws.send(JSON.stringify({ action: 'typing', conversation_id: this._convId }));
    }
  }

  // ── Privado ───────────────────────────────────────────────────────────────

  _openWs() {
    const url = `${CHAT_WS_BASE}?token=${encodeURIComponent(this._token)}&sender_type=USER&conv_id=${this._convId}`;
    this._ws = new WebSocket(url);

    this._ws.onopen = () => {
      this._retryCount = 0;
      this._keepalive = setInterval(() => {
        if (this._ws?.readyState === WebSocket.OPEN) {
          this._ws.send(JSON.stringify({ action: 'ping' }));
        }
      }, 8 * 60 * 1000);
    };

    this._ws.onmessage = ({ data }) => {
      const msg = JSON.parse(data);
      if (msg.type === 'new_message') this._onMessage(msg.message);
      if (msg.type === 'typing') this._onTyping(msg.sender_type);
    };

    this._ws.onerror = () => this._ws.close();
    this._ws.onclose = (e) => {
      clearInterval(this._keepalive);
      if (e.code !== 1000 && this._convId) {
        const delays = [1000, 2000, 4000, 8000, 16000];
        const delay = delays[Math.min(this._retryCount++, delays.length - 1)];
        setTimeout(() => this._openWs(), delay);
      }
    };
  }

  async _get(path) {
    const r = await fetch(`${CHAT_REST_BASE}${path}`, {
      headers: { Authorization: `Bearer ${this._token}` },
    });
    const json = await r.json();
    if (!r.ok) throw new Error(json.message || r.statusText);
    return json.data;
  }

  async _post(path, body, companyId) {
    const url = companyId
      ? `${CHAT_REST_BASE}${path}?company_id=${companyId}`
      : `${CHAT_REST_BASE}${path}`;
    const r = await fetch(url, {
      method: 'POST',
      headers: { Authorization: `Bearer ${this._token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const json = await r.json();
    if (!r.ok) throw new Error(json.message || r.statusText);
    return json.data;
  }
}
```

### 7.1 Uso en React

```jsx
import { useEffect, useRef, useState } from 'react';
import { AdminChatClient } from './AdminChatClient';

function ChatPanel({ conversation, cognitoToken }) {
  const [messages, setMessages] = useState([]);
  const [isTyping, setIsTyping] = useState(false);
  const clientRef = useRef(null);
  const typingTimer = useRef(null);

  useEffect(() => {
    const client = new AdminChatClient({
      cognitoToken,
      onMessage: (msg) => setMessages((prev) => [...prev, msg]),
      onTyping: () => {
        setIsTyping(true);
        clearTimeout(typingTimer.current);
        typingTimer.current = setTimeout(() => setIsTyping(false), 3000);
      },
    });
    clientRef.current = client;
    client.listMessages(conversation.id).then(({ messages }) => setMessages(messages));
    client.connectToConversation(conversation.id);
    return () => client.disconnect();
  }, [conversation.id, cognitoToken]);

  return (
    <div className="chat-panel">
      <div className="chat-messages">
        {messages.map((m) => <MessageBubble key={m.id} message={m} />)}
        {isTyping && <div className="typing-dots">El cliente está escribiendo...</div>}
      </div>
      <ChatInput
        onTyping={() => clientRef.current?.sendTyping()}
        onSendText={(text) => clientRef.current?.sendText(conversation.id, text)}
        onSendFile={(file) => clientRef.current?.sendFile(conversation.id, file)}
      />
    </div>
  );
}

function MessageBubble({ message }) {
  const isOwn = message.sender_type === 'USER';
  return (
    <div className={`bubble ${isOwn ? 'own' : 'other'}`}>
      {message.message_type === 'IMAGE' && (
        <img src={message.attachment_url} alt="imagen" className="chat-image" />
      )}
      {message.message_type === 'FILE' && (
        <a href={message.attachment_url} target="_blank" rel="noreferrer" download>
          📎 {message.attachment_url?.split('/').pop()?.split('?')[0] ?? 'archivo'}
        </a>
      )}
      {message.message_type === 'TEXT' && <p>{message.body}</p>}
      <span className="timestamp">{new Date(message.created_at).toLocaleTimeString()}</span>
    </div>
  );
}
```

---

## 8. Cliente JS reutilizable — theme extension

```javascript
// StorefrontChatClient.js
const REST_BASE = 'https://30yyq9wfd2.execute-api.us-east-2.amazonaws.com';
const WS_BASE   = 'wss://eenu72b5v4.execute-api.us-east-2.amazonaws.com/dev';

export class StorefrontChatClient {
  /**
   * @param {object} opts
   * @param {string} opts.shop - Dominio myshopify.com
   * @param {string|number} opts.customerId - ID numérico del Customer en Shopify
   * @param {string} [opts.customerEmail]
   * @param {string} [opts.customerName]
   * @param {string} [opts.crmCompanyId] - UUID de companies.id
   * @param {string|number} [opts.b2bCompanyId] - ID B2B de Company en Shopify
   * @param {string} [opts.apiKey] - CHAT_STOREFRONT_API_KEY (si está configurada)
   * @param {function} opts.onMessage - Callback con el mensaje nuevo
   * @param {function} opts.onTyping - Callback con sender_type cuando alguien escribe
   */
  constructor({ shop, customerId, customerEmail, customerName, crmCompanyId, b2bCompanyId, apiKey, onMessage, onTyping }) {
    this._shop = shop;
    this._customerId = String(customerId);
    this._email = customerEmail || null;
    this._name = customerName || null;
    this._crmCompanyId = crmCompanyId || null;
    this._b2bCompanyId = b2bCompanyId ? String(b2bCompanyId) : null;
    this._apiKey = apiKey || null;
    this._onMessage = onMessage;
    this._onTyping = onTyping;
    this._ws = null;
    this._keepalive = null;
    this._convId = this._loadConvId();
  }

  // ── Inicialización ────────────────────────────────────────────────────────

  /** Carga el historial si hay conversación y conecta el WebSocket. */
  async init() {
    if (this._convId) {
      try {
        const data = await this.loadHistory();
        return { conversation_id: this._convId, messages: data.messages };
      } catch {
        // Si falla (conv expirada/no encontrada), limpiar y empezar de cero
        this._clearConvId();
        this._convId = null;
      }
    }
    return { conversation_id: null, messages: [] };
  }

  // ── REST ─────────────────────────────────────────────────────────────────

  async sendText(text) {
    const data = await this._post('/api/v1/chat/public/messages', {
      ...this._companyParams(),
      shopify_customer_id: this._customerId,
      body: text,
      message_type: 'TEXT',
      ...(this._email ? { email: this._email } : {}),
      ...(this._name ? { name: this._name } : {}),
    });
    this._onFirstConv(data.conversation_id);
    return data;
  }

  async sendImage(file) {
    if (!this._convId) throw new Error('Envía un mensaje de texto primero para iniciar la conversación');

    // 1. Pedir presigned URL
    const { upload_url, attachment_key } = await this._post(
      `/api/v1/chat/public/conversations/${this._convId}/upload`,
      {
        ...this._companyParams(),
        shopify_customer_id: this._customerId,
        filename: file.name,
        content_type: file.type,
      },
    );

    // 2. Subir a S3
    await fetch(upload_url, {
      method: 'PUT',
      headers: { 'Content-Type': file.type },
      body: file,
    });

    // 3. Confirmar mensaje
    const data = await this._post('/api/v1/chat/public/messages', {
      ...this._companyParams(),
      shopify_customer_id: this._customerId,
      message_type: 'IMAGE',
      attachment_key,
      body: null,
    });
    return data;
  }

  async loadHistory({ limit = 50, offset = 0 } = {}) {
    if (!this._convId) return { messages: [] };
    const q = new URLSearchParams({
      shop: this._shop,
      shopify_customer_id: this._customerId,
      limit,
      offset,
    });
    if (this._crmCompanyId) q.set('company_id', this._crmCompanyId);
    if (this._b2bCompanyId) q.set('shopify_company_id', this._b2bCompanyId);
    return this._get(`/api/v1/chat/public/conversations/${this._convId}/messages?${q}`);
  }

  // ── WebSocket ─────────────────────────────────────────────────────────────

  connectWs() {
    if (!this._convId) return;
    this.disconnectWs();

    let url = `${WS_BASE}?sender_type=CLIENT&client_id=${this._customerId}&conv_id=${this._convId}`;
    if (this._apiKey) url += `&api_key=${encodeURIComponent(this._apiKey)}`;

    this._ws = new WebSocket(url);

    this._ws.onopen = () => {
      this._keepalive = setInterval(() => {
        if (this._ws?.readyState === WebSocket.OPEN) {
          this._ws.send(JSON.stringify({ action: 'ping' }));
        }
      }, 8 * 60 * 1000);
    };

    this._ws.onmessage = ({ data }) => {
      const msg = JSON.parse(data);
      if (msg.type === 'new_message') this._onMessage(msg.message);
      if (msg.type === 'typing') this._onTyping(msg.sender_type);
    };

    this._ws.onerror = () => this._ws?.close();
    this._ws.onclose = () => clearInterval(this._keepalive);
  }

  disconnectWs() {
    clearInterval(this._keepalive);
    if (this._ws) {
      this._ws.onclose = null;
      this._ws.close(1000);
      this._ws = null;
    }
  }

  sendTyping() {
    if (this._ws?.readyState === WebSocket.OPEN && this._convId) {
      this._ws.send(JSON.stringify({ action: 'typing', conversation_id: this._convId }));
    }
  }

  // ── Privado ───────────────────────────────────────────────────────────────

  _companyParams() {
    return {
      shop: this._shop,
      ...(this._crmCompanyId ? { company_id: this._crmCompanyId } : {}),
      ...(this._b2bCompanyId ? { shopify_company_id: this._b2bCompanyId } : {}),
    };
  }

  _onFirstConv(convId) {
    if (!this._convId && convId) {
      this._convId = convId;
      this._saveConvId(convId);
      this.connectWs(); // conectar WS ahora que tenemos conv_id
    }
  }

  _storageKey() {
    const ref = this._crmCompanyId || this._b2bCompanyId || 'default';
    return `apro_conv_${this._shop}_${this._customerId}_${ref}`;
  }

  _loadConvId() {
    try { return localStorage.getItem(this._storageKey()) || null; } catch { return null; }
  }

  _saveConvId(id) {
    try { localStorage.setItem(this._storageKey(), id); } catch { /* private browsing */ }
  }

  _clearConvId() {
    try { localStorage.removeItem(this._storageKey()); } catch { /* ok */ }
  }

  _headers() {
    const h = { 'Content-Type': 'application/json' };
    if (this._apiKey) h['X-Api-Key'] = this._apiKey;
    return h;
  }

  async _get(path) {
    const r = await fetch(`${REST_BASE}${path}`, { headers: this._headers() });
    const json = await r.json();
    if (!r.ok) throw new Error(json.message || r.statusText);
    return json.data;
  }

  async _post(path, body) {
    const r = await fetch(`${REST_BASE}${path}`, {
      method: 'POST',
      headers: this._headers(),
      body: JSON.stringify(body),
    });
    const json = await r.json();
    if (!r.ok) throw new Error(json.message || r.statusText);
    return json.data;
  }
}
```

### 8.1 Inicialización desde el bundle del tema

```javascript
// apro-chat.js — entry point del bundle del tema
import { StorefrontChatClient } from './StorefrontChatClient';

const root = document.getElementById('apro-chat-root');
if (!root) throw new Error('Elemento #apro-chat-root no encontrado');

const client = new StorefrontChatClient({
  shop:           root.dataset.shop,
  customerId:     root.dataset.customerId,
  customerEmail:  root.dataset.customerEmail || null,
  customerName:   (root.dataset.customerName || '').trim() || null,
  crmCompanyId:   root.dataset.companyId || null,
  b2bCompanyId:   root.dataset.b2bCompanyId || null, // si tienes sesión B2B
  apiKey:         root.dataset.apiKey || null,
  onMessage:      (msg) => chatUI.appendMessage(msg),
  onTyping:       (senderType) => {
    if (senderType === 'USER') chatUI.showTypingIndicator('El vendedor está escribiendo...');
  },
});

// Inicializar: cargar historial y conectar WS si hay conversación previa
client.init().then(({ messages }) => {
  chatUI.renderMessages(messages);
  client.connectWs(); // si ya existe conv_id, conecta; si no, conectará tras el primer mensaje
});

// Evento de envío de texto
chatUI.onSendText = (text) => client.sendText(text).catch(chatUI.showError);

// Evento de envío de imagen
chatUI.onSendImage = (file) => {
  if (file.size > 5 * 1024 * 1024) return chatUI.showError('La imagen no puede superar 5 MB');
  client.sendImage(file).catch(chatUI.showError);
};

// Evento de typing
chatUI.onTyping = () => client.sendTyping();
```

---

## 9. Errores frecuentes

### WebSocket

| Código / síntoma | Causa | Solución |
|---|---|---|
| `401` al conectar | Token o api_key inválido | Renovar Cognito token antes de reconectar |
| `400` al conectar | `conv_id` ausente | Asegurar que `conv_id` va en la URL |
| Cierre inesperado | Red inestable o >10 min sin actividad | Implementar keepalive cada 8 min y reconexión con backoff |
| `410 Gone` al enviar | Conexión ya cerrada por el servidor | El broadcaster la limpia automáticamente; el cliente debe reconectar |

### REST — Adjuntos

| Error | Causa | Solución |
|---|---|---|
| `400 Tipo de archivo no permitido desde la tienda` | El cliente intenta subir Excel/PDF | Desde el storefront solo se permiten imágenes |
| `400 Tipo de archivo no permitido` (panel) | MIME no está en la lista | Verificar el tipo antes de enviar |
| `400 attachment_key requerido` | Se envió `message_type: IMAGE` sin completar el paso 2 | Subir a S3 antes de confirmar el mensaje |
| URL de adjunto expirada (403 S3) | Presigned URL de 1h caducó | Llamar de nuevo a `GET /messages` para URLs frescas |

### REST — Mensajes generales

| Error | Causa | Solución |
|---|---|---|
| `400 El mensaje no puede estar vacío` | `body` vacío y sin `attachment_key` | Enviar texto o adjunto |
| `401` | Token Cognito expirado (panel) o `X-Api-Key` incorrecta (tienda) | Renovar token o verificar la clave |
| `404 Conversación no encontrada` | UUID inválido, expirado o de otro cliente | Limpiar `localStorage` y empezar nueva conversación |
| `400 Indique company_id` | Falta empresa en el request | Siempre enviar `company_id` o `shopify_company_id` |

---

## Referencias

- **API REST completa:** [CHAT_SERVICE_INTEGRATION.md](./CHAT_SERVICE_INTEGRATION.md)
- **Guía de integración anterior:** [GUIA_FRONTEND_CHAT_EXTENSION_ADMIN.md](./GUIA_FRONTEND_CHAT_EXTENSION_ADMIN.md)
- **Proceso de deploy backend:** [AGENTS.md](../AGENTS.md)

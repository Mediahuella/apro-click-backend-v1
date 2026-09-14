# Ambiente QA de APRO IA

Permite que las cotizaciones de **una lista acotada de empresas** vivan en los
dos ambientes del DMS de APRO (producción y QA) sin alterar en nada el flujo
productivo del resto de los clientes.

Caso que lo motivó: poder probar contra el QA de APRO IA usando la empresa de
pruebas **Seba SPA** (`76486379-8`), mientras producción sigue operando normal.

---

## Los dos ambientes

| | Producción | QA |
|---|---|---|
| Cotizaciones | `APRO_IA_BASE_URL` = `https://dms.apro.cl` | `APRO_IA_BASE_URL_QA` = `https://dms-qa.apro.cl` |
| Token | `APRO_IA_BEARER_TOKEN` | `APRO_IA_BEARER_TOKEN_QA` |
| Columnas de match | `apro_ia_id`, `apro_ia_folio` | `apro_ia_qa_id`, `apro_ia_qa_folio` |
| Ruta de eventos | `POST /api/v1/quotes/webhooks/apro-ia` | `POST /api/v1/quotes/webhooks/apro-ia/qa` |
| Ruta de transferencias | `POST .../apro-ia/transferencias` | `POST .../apro-ia/qa/transferencias` |

**Los tokens no son intercambiables:** el de producción devuelve
`403 Token inválido` contra `dms-qa.apro.cl` (verificado).

---

## La allowlist es la única puerta

`APRO_IA_QA_ALLOWED_RUTS` (RUTs coma-separados, se comparan normalizados —
`76486379-8`, `764863798` y `76.486.379-8` son el mismo valor).

- **Salida:** el espejo a QA solo corre para esas empresas.
- **Entrada:** las rutas `/qa` descartan cualquier payload cuyo RUT no esté en
  la lista, respondiendo `200 {"ignored": true}`.

Variable vacía = QA apagado por completo.

Se responde **200 y no 4xx** a propósito: un 4xx le diría al DMS de QA que
reintente, y no hay nada que reintentar.

---

## Por qué el ambiente se decide por la URL

APRO IA firma sus webhooks con una única `x-api-key` (`APRO_IA_WEBHOOK_API_KEY`)
y no hay garantía de que puedan emitir una distinta por ambiente. Separar por
**ruta** hace que la elección dependa de la URL que ellos configuran en cada
DMS, y vuelve imposible que un evento de QA entre por la ruta productiva.

---

## Por qué los identificadores van en columnas propias

QA y producción son **secuencias de IDs independientes**: un `id` emitido por QA
puede coincidir con el `apro_ia_id` de una cotización real de otro cliente. Como
el matching del webhook de eventos es por `apro_ia_id`
(`quote_service.handle_apro_ia_webhook`), reusar esas columnas haría que un
evento de prueba pisara una cotización productiva.

Es exactamente el bug que ya ocurrió con los folios repetidos (APRO IA reusó
`AC-000007` entre dos clientes distintos), documentado en el docstring de
`handle_apro_ia_webhook` y cubierto por
`test_folio_duplicado_no_actualiza_cotizacion_de_otro_cliente`.

Por eso la migración `026` agrega `apro_ia_qa_id` y `apro_ia_qa_folio`, y cada
ruta matchea **solo** contra las columnas de su ambiente.

---

## Espejo de salida

En `quote_service._espejar_en_qa`, invocado al final de
`_sync_apro_ia_and_pdf` (o sea, al negociar el Draft Order):

1. Si el RUT no está en la allowlist → no hace nada.
2. Si QA no tiene base URL o token → lo saltea con un `INFO` (permite deployar
   antes de tener credenciales).
3. Crea la cotización en QA (o la edita si ya existe `apro_ia_qa_folio`) y
   guarda `apro_ia_qa_id` / `apro_ia_qa_folio`.

Es **fire-and-forget**: cualquier excepción se loguea y se traga. Producción
sigue siendo la fuente de verdad de `folio`, `apro_ia_folio` y el PDF — el
espejo nunca los toca.

---

## Entrada desde QA

Las rutas `/qa` procesan igual que producción **salvo en un punto**:
`quotation.created` sin cotización espejada **no auto-crea** la `QuoteRequest`.
En producción sí lo hace (crea el registro y su Draft Order en Shopify); hacerlo
desde QA abriría un Draft Order real en la tienda productiva a partir de una
prueba.

Consecuencia práctica: una cotización solo existe en QA si **nosotros** la
espejamos primero. Una creada directo en el DMS de QA no se replica hacia acá.

---

## Configuración

```bash
APRO_IA_BASE_URL_QA=https://dms-qa.apro.cl
APRO_IA_BEARER_TOKEN_QA=<token de QA>
APRO_IA_QA_ALLOWED_RUTS=76486379-8
```

Para activar hace falta, además, que APRO IA apunte los webhooks de su DMS de QA
a las rutas `/qa` (la `x-api-key` sigue siendo la misma).

---

## Tests

`src/services/quotes/tests/test_apro_ia_qa.py` — allowlist y normalización de
RUT, descarte de clientes ajenos en ambas rutas, aislamiento de identificadores
en los dos sentidos, no-autocreación desde QA, y el espejo (creación, saltado
sin credenciales, error tragado).

"""Tests del crédito B2B: metafield que habilita el pago y cupo del DMS.

Cubren las dos piezas del backend que sostienen la Shopify Function
``manejo-de-credito`` y la extension ``credito-b2b``:

- ``utils/shopify_credit_sync.credit_metafield_value`` — traducción
  ``payment_type`` → valor del metafield boolean.
- ``apro/credit_client.consultar_credito`` — consulta al servicio de créditos
  de APRO. Lo que más importa es que **nunca lance** y que distinga "no tiene
  crédito" (404) de "no se pudo saber" (``None``).
- ``services/credit_sync_service`` — APRO como fuente de verdad de
  ``payment_type``, con la regla de que lo indeterminado no cambia nada.

Ejecutar con:
  cd backend-v1 && python3 -m pytest src/services/companies/tests -v
"""
from __future__ import annotations

import json
import sys
import urllib.error
from io import BytesIO
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parent.parent
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))
SHARED_ROOT = SERVICE_ROOT / "shared"
if str(SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(SHARED_ROOT))

# Los imports van arriba (no dentro de los tests) a propósito: se resuelven
# durante la colección, justo después del conftest que aísla el `utils` de
# este servicio. Diferidos, al ejecutarse ya los habría tapado otro servicio.
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402

from contextlib import contextmanager  # noqa: E402

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database.base import Base  # noqa: E402
import database.models  # noqa: E402,F401  registra los modelos en Base
from database.models.company import Company  # noqa: E402
from services import company_service as company_service_mod  # noqa: E402
from apro import credit_client  # noqa: E402
from services import credit_sync_service  # noqa: E402
from utils import shopify_credit_sync  # noqa: E402
from utils.shopify_credit_sync import credit_metafield_value  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    """Los modelos usan JSONB (PostgreSQL); en SQLite se compila como JSON."""
    return "JSON"


# ---------------------------------------------------------------------------
# payment_type → metafield
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payment_type,esperado",
    [
        ("CREDIT", "true"),
        ("credit", "true"),
        (" CREDIT ", "true"),
        ("DIRECT", "false"),
        ("", "false"),
        (None, "false"),
        ("CUALQUIER_COSA", "false"),
    ],
)
def test_credit_metafield_value(payment_type, esperado):
    """Solo CREDIT habilita: cualquier otro valor cae en `false` (falla cerrada)."""
    assert credit_metafield_value(payment_type) == esperado


def test_sync_sin_shopify_company_id_no_llama_a_shopify(monkeypatch):
    """Empresa del CRM que aún no existe en Shopify: no hay dónde escribir."""

    def _explota(*args, **kwargs):
        raise AssertionError("no debería resolver la tienda")

    monkeypatch.setattr(shopify_credit_sync, "_resolve_shop_and_token", _explota)
    assert shopify_credit_sync.sync_company_credit_metafield(None, "CREDIT") is False
    assert shopify_credit_sync.sync_company_credit_metafield("", "CREDIT") is False


def test_sync_escribe_el_metafield_con_el_gid_normalizado(monkeypatch):
    llamadas: list[dict] = []

    def _fake_graphql(shop, token, query, variables):
        llamadas.append(variables)
        return {"metafieldsSet": {"metafields": [{"id": "gid://…"}], "userErrors": []}}

    monkeypatch.setattr(shopify_credit_sync, "graphql_call", _fake_graphql)

    ok = shopify_credit_sync.sync_company_credit_metafield(
        "3567190200", "CREDIT", shop_domain="aproclick.myshopify.com", access_token="t"
    )

    assert ok is True
    metafield = llamadas[0]["metafields"][0]
    assert metafield["ownerId"] == "gid://shopify/Company/3567190200"
    assert (metafield["namespace"], metafield["key"]) == ("aproclick", "credito")
    assert metafield["type"] == "boolean"
    assert metafield["value"] == "true"


def test_sync_no_lanza_si_shopify_falla(monkeypatch):
    """El guardado en BD no puede quedar bloqueado por la Admin API."""

    def _explota(*args, **kwargs):
        raise ValueError("Shopify HTTP 500")

    monkeypatch.setattr(shopify_credit_sync, "graphql_call", _explota)
    assert (
        shopify_credit_sync.sync_company_credit_metafield(
            "3567190200", "CREDIT", shop_domain="x.myshopify.com", access_token="t"
        )
        is False
    )


# ---------------------------------------------------------------------------
# Servicio de créditos de APRO — ¿esta empresa tiene crédito?
# ---------------------------------------------------------------------------
@pytest.fixture()
def apro_configurado(monkeypatch):
    """Créditos reusa la configuración de la API de clientes de APRO."""
    monkeypatch.setenv("APRO_IA_CLIENTS_BASE_URL", "https://clientes.apro.cl")
    monkeypatch.setenv("APRO_IA_CLIENTS_BASE_URL_QA", "https://clientes-qa.apro.cl")
    monkeypatch.setenv("APRO_CREDIT_PATH_TEMPLATE", "/credits/client/{rut}")
    monkeypatch.setenv("APRO_IA_BEARER_TOKEN", "token")
    monkeypatch.setenv("APRO_IA_BEARER_TOKEN_QA", "token-qa")
    monkeypatch.delenv("APRO_CREDIT_FORCE_RUTS", raising=False)


class _RespuestaFake(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _responder(monkeypatch, payload: dict) -> list:
    """Intercepta la llamada HTTP y devuelve la lista de requests hechos."""
    hechos: list = []

    def _fake_urlopen(req, timeout=0):
        hechos.append(req)
        return _RespuestaFake(json.dumps(payload).encode())

    monkeypatch.setattr(credit_client.urllib.request, "urlopen", _fake_urlopen)
    return hechos


_CREDITO_OK = {
    "success": True,
    "message": "Crédito encontrado correctamente",
    "credit": {"folio": "CR-007700", "createdAt": "2025-09-03T19:40:50.746Z"},
}


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        ("76.486.379-8", "764863798"),
        ("76486379-8", "764863798"),
        (" 764863798 ", "764863798"),
        ("12345678-k", "12345678K"),
        (None, ""),
    ],
)
def test_normalizar_rut(entrada, esperado):
    assert credit_client.normalizar_rut(entrada) == esperado


def test_sin_configuracion_es_indeterminado(monkeypatch):
    """Sin URL no se puede saber: nadie debe cambiar el crédito de nadie."""
    monkeypatch.delenv("APRO_IA_CLIENTS_BASE_URL", raising=False)
    monkeypatch.delenv("APRO_CREDIT_FORCE_RUTS", raising=False)
    assert credit_client.esta_configurado() is False
    assert credit_client.consultar_credito("76486379-8") is None


def test_credito_encontrado_devuelve_folio(apro_configurado, monkeypatch):
    hechos = _responder(monkeypatch, _CREDITO_OK)

    credito = credit_client.consultar_credito("76.486.379-8")

    assert credito == {
        "tiene_credito": True,
        "folio": "CR-007700",
        "creado_en": "2025-09-03T19:40:50.746Z",
    }
    # El RUT viaja normalizado dentro de la ruta, no como query string.
    assert hechos[0].full_url == "https://clientes.apro.cl/credits/client/764863798"
    assert hechos[0].headers["Authorization"] == "Bearer token"


def test_sin_path_template_usa_el_default(apro_configurado, monkeypatch):
    """El `serverless.yml` manda la variable vacía: las llaves de `{rut}` las
    tomaba el parser de Serverless como una variable suya."""
    monkeypatch.setenv("APRO_CREDIT_PATH_TEMPLATE", "")
    hechos = _responder(monkeypatch, _CREDITO_OK)

    credit_client.consultar_credito("76486379-8")

    assert hechos[0].full_url == "https://clientes.apro.cl/credits/client/764863798"


def test_404_es_sin_credito_no_error(apro_configurado, monkeypatch):
    """La respuesta documentada para "no tiene crédito" es un 404."""

    cuerpo = json.dumps(
        {
            "success": False,
            "message": "No se encontró crédito para el cliente con RUT: 201628114",
            "credit": None,
        }
    ).encode()

    def _fake_urlopen(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, BytesIO(cuerpo))

    monkeypatch.setattr(credit_client.urllib.request, "urlopen", _fake_urlopen)
    assert credit_client.consultar_credito("20162811-4") == {
        "tiene_credito": False,
        "folio": None,
        "creado_en": None,
    }


def test_404_de_ruta_inexistente_es_indeterminado(apro_configurado, monkeypatch):
    """`Cannot GET /credits/...` es un host sin el módulo desplegado, no un
    cliente sin crédito: tratarlo como "sin crédito" apagaría el crédito de
    todas las empresas de una sola pasada."""
    cuerpo = json.dumps(
        {
            "message": "Cannot GET /credits/client/764863798",
            "error": "Not Found",
            "statusCode": 404,
        }
    ).encode()

    def _fake_urlopen(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, BytesIO(cuerpo))

    monkeypatch.setattr(credit_client.urllib.request, "urlopen", _fake_urlopen)
    assert credit_client.consultar_credito("76486379-8") is None


def test_ambiente_qa_usa_su_host_y_su_token(apro_configurado, monkeypatch):
    hechos = _responder(monkeypatch, _CREDITO_OK)

    credit_client.consultar_credito("76486379-8", env=credit_client.ENV_QA)

    assert hechos[0].full_url == "https://clientes-qa.apro.cl/credits/client/764863798"
    assert hechos[0].headers["Authorization"] == "Bearer token-qa"


def test_error_del_dms_es_indeterminado(apro_configurado, monkeypatch):
    """Un 500 no es "sin crédito": apagarlo dejaría al cliente sin su medio de pago."""

    def _fake_urlopen(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 500, "Boom", {}, BytesIO(b""))

    monkeypatch.setattr(credit_client.urllib.request, "urlopen", _fake_urlopen)
    assert credit_client.consultar_credito("76486379-8") is None


def test_red_caida_es_indeterminado(apro_configurado, monkeypatch):
    def _explota(*args, **kwargs):
        raise OSError("connection reset")

    monkeypatch.setattr(credit_client.urllib.request, "urlopen", _explota)
    assert credit_client.consultar_credito("76486379-8") is None


def test_rut_forzado_no_consulta_al_dms(monkeypatch):
    """Seba SPA debe tener crédito siempre, aunque el DMS diga que no."""
    monkeypatch.setenv("APRO_CREDIT_FORCE_RUTS", "76486379-8, 11111111-1")

    def _explota(*args, **kwargs):
        raise AssertionError("no debería consultar al DMS un RUT forzado")

    monkeypatch.setattr(credit_client.urllib.request, "urlopen", _explota)
    credito = credit_client.consultar_credito("76.486.379-8")
    assert credito["tiene_credito"] is True
    assert credito["forzado"] is True


def test_rut_vacio_no_consulta(apro_configurado, monkeypatch):
    def _explota(*args, **kwargs):
        raise AssertionError("no debería llamar al DMS sin RUT")

    monkeypatch.setattr(credit_client.urllib.request, "urlopen", _explota)
    assert credit_client.consultar_credito("") is None


# ---------------------------------------------------------------------------
# Sincronización CRM ← APRO
# ---------------------------------------------------------------------------
def _empresa(**kw) -> Company:
    base = dict(
        name="ACME SPA",
        company_type="SMALL",
        payment_type="DIRECT",
        billing_rut="76486379-8",
        billing_documento="FACTURA",
        shopify_company_id="3538649272",
        is_system=False,
    )
    base.update(kw)
    return Company(**base)


@pytest.fixture()
def metafields_escritos(monkeypatch) -> list:
    escritos: list = []

    def _fake(shopify_company_id, payment_type, *, shop_domain="", access_token=""):
        escritos.append((shopify_company_id, payment_type))
        return True

    monkeypatch.setattr(credit_sync_service, "sync_company_credit_metafield", _fake)
    return escritos


def _dms_responde(monkeypatch, valor):
    monkeypatch.setattr(
        credit_sync_service, "consultar_credito", lambda rut, env=None: valor
    )


def test_sync_enciende_el_credito(monkeypatch, metafields_escritos):
    _dms_responde(monkeypatch, {"tiene_credito": True})
    empresa = _empresa(payment_type="DIRECT")

    r = credit_sync_service.sincronizar_empresa(None, empresa)

    assert empresa.payment_type == "CREDIT"
    assert r["cambio"] is True and r["despues"] == "CREDIT"
    assert metafields_escritos == [("3538649272", "CREDIT")]


def test_sync_apaga_el_credito(monkeypatch, metafields_escritos):
    _dms_responde(monkeypatch, {"tiene_credito": False})
    empresa = _empresa(payment_type="CREDIT")

    r = credit_sync_service.sincronizar_empresa(None, empresa)

    assert empresa.payment_type == "DIRECT"
    assert r["cambio"] is True
    assert metafields_escritos == [("3538649272", "DIRECT")]


def test_sync_sin_cambios_no_escribe_en_shopify(monkeypatch, metafields_escritos):
    _dms_responde(monkeypatch, {"tiene_credito": True})
    empresa = _empresa(payment_type="CREDIT")

    r = credit_sync_service.sincronizar_empresa(None, empresa)

    assert r["cambio"] is False
    assert metafields_escritos == []


def test_sync_indeterminado_deja_la_empresa_como_esta(monkeypatch, metafields_escritos):
    """El DMS no respondió: no se toca nada (ni BD ni Shopify)."""
    _dms_responde(monkeypatch, None)
    empresa = _empresa(payment_type="CREDIT")

    r = credit_sync_service.sincronizar_empresa(None, empresa)

    assert empresa.payment_type == "CREDIT"
    assert r["indeterminado"] is True and r["cambio"] is False
    assert metafields_escritos == []


def test_sync_sin_rut_no_consulta(monkeypatch, metafields_escritos):
    def _explota(rut, env=None):
        raise AssertionError("no debería consultar sin RUT")

    monkeypatch.setattr(credit_sync_service, "consultar_credito", _explota)
    empresa = _empresa(billing_rut=None, payment_type="DIRECT")

    r = credit_sync_service.sincronizar_empresa(None, empresa)

    assert r["cambio"] is False
    assert metafields_escritos == []


# ---------------------------------------------------------------------------
def test_get_company_by_shopify_id_acepta_gid_y_numerico(monkeypatch):
    """El theme manda el id numérico y el checkout el GID; en BD está numérico.

    Sin normalizar, el canal del checkout recibía 404 aunque la empresa
    existiera (detectado al probar el endpoint recién desplegado).
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    @contextmanager
    def _get_session():
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()

    with _get_session() as session:
        session.add(
            Company(
                name="COBRE Y METALES LOGISTICA SPA",
                company_type="SMALL",
                payment_type="CREDIT",
                # Así está en producción: numérico pelado, sin el prefijo GID.
                shopify_company_id="3740172472",
            )
        )
        session.commit()

    monkeypatch.setattr(company_service_mod, "get_session", _get_session)
    servicio = company_service_mod.CompanyService()

    por_gid = servicio.get_company_by_shopify_id("gid://shopify/Company/3740172472")
    por_numero = servicio.get_company_by_shopify_id("3740172472")

    assert por_gid is not None, "el checkout manda el GID y debe resolver"
    assert por_numero is not None, "el theme manda el numérico y debe seguir resolviendo"
    assert por_gid["id"] == por_numero["id"]
    assert servicio.get_company_by_shopify_id("  ") is None
    assert servicio.get_company_by_shopify_id("gid://shopify/Company/999") is None

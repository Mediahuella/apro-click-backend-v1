"""Cálculo de tarifa de envío por peso físico + volumétrico.

Reglas (extraídas del Excel ``Cotizador - Tarifa COM. DE ART. DE PROTECCION``):

1. El cobro es **kg-based** y por tramo (0-5, 6-10, …, 5001-10000).
2. Conversión peso/volumen: ``alto_cm * largo_cm * ancho_cm / 1_000_000`` m³
   por bulto, multiplicado por ``250`` kg/m³.
3. ``kg_cobrable = max(1, kg_fisico_total, kg_volumetrico_total)``.
4. Tarifa por ruta: ``ORIGEN-DESTINO-ZONA-TRAMO`` → ``{tarifa, minimo}``.
5. ``subtotal_T1 = max(minimo, kg_cobrable * tarifa)``.
6. **Regla del tramo anterior** (notas del Excel): ningún precio puede ser
   inferior al máximo del tramo previo. Tomamos el ``to_kg`` del tramo
   anterior, lo multiplicamos por ``tarifa_previa`` y lo comparamos con el
   ``minimo_previo``: el mayor de los dos es el ``subtotal_T2``.
7. ``total_neto = max(subtotal_T1, subtotal_T2)``.
8. Si la respuesta debe incluir IVA (Chile, 19%): ``total_iva = round(total_neto * 1.19)``.
9. CLP no usa decimales; redondear el resultado final a entero.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _SERVICE_ROOT / "data"


@dataclass(frozen=True)
class Package:
    weight_kg: float
    height_cm: float
    length_cm: float
    width_cm: float

    def physical_kg(self) -> float:
        return max(0.0, float(self.weight_kg or 0))

    def volumetric_kg(self) -> float:
        h = max(0.0, float(self.height_cm or 0))
        l = max(0.0, float(self.length_cm or 0))
        w = max(0.0, float(self.width_cm or 0))
        m3 = (h * l * w) / 1_000_000.0
        return m3 * 250.0


@dataclass(frozen=True)
class TramoMeta:
    tramo: str
    from_kg: float
    to_kg: float


@dataclass
class QuoteResult:
    origin: str
    destination_sucursal: str
    destination_zona: str
    locality_input: str
    locality_matched: str
    kg_fisico: float
    kg_volumetrico: float
    kg_cobrable: float
    tramo: str
    tramo_anterior: str | None
    tarifa_clp_kg: float
    minimo_clp: float
    subtotal_t1_clp: float
    subtotal_t2_clp: float
    total_neto_clp: int
    total_con_iva_clp: int
    iva_pct: float

    def to_dict(self) -> dict:
        return {
            "origin": self.origin,
            "destination_sucursal": self.destination_sucursal,
            "destination_zona": self.destination_zona,
            "locality_input": self.locality_input,
            "locality_matched": self.locality_matched,
            "kg_fisico": round(self.kg_fisico, 3),
            "kg_volumetrico": round(self.kg_volumetrico, 3),
            "kg_cobrable": round(self.kg_cobrable, 3),
            "tramo": self.tramo,
            "tramo_anterior": self.tramo_anterior,
            "tarifa_clp_kg": self.tarifa_clp_kg,
            "minimo_clp": self.minimo_clp,
            "subtotal_t1_clp": round(self.subtotal_t1_clp, 2),
            "subtotal_t2_clp": round(self.subtotal_t2_clp, 2),
            "total_neto_clp": int(self.total_neto_clp),
            "total_con_iva_clp": int(self.total_con_iva_clp),
            "iva_pct": self.iva_pct,
        }


class LocalityNotFoundError(LookupError):
    """La localidad de destino no está en LOCALIDADES."""


class RouteNotFoundError(LookupError):
    """No hay tarifa registrada para la combinación origen/destino/zona/tramo."""


class WeightOutOfRangeError(ValueError):
    """El kg cobrable excede el tramo máximo del Excel (5001-10000)."""


def _read_json(path: Path):
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def _localidades() -> dict[str, dict]:
    return _read_json(_DATA_DIR / "localidades.json")


@lru_cache(maxsize=1)
def _tarifas() -> dict[str, dict]:
    return _read_json(_DATA_DIR / "tarifas.json")


@lru_cache(maxsize=1)
def _tramos() -> list[TramoMeta]:
    raw = _read_json(_DATA_DIR / "tramos.json")
    out = [
        TramoMeta(
            tramo=item["tramo"],
            from_kg=float(item["from_kg"]),
            to_kg=float(item["to_kg"]),
        )
        for item in raw
    ]
    out.sort(key=lambda t: t.from_kg)
    return out


def normalize_locality(name: str) -> str:
    """Mismo algoritmo que ``scripts/build_shipping_data.py``.

    Mayúsculas, sin tildes, no alfanuméricos colapsados a espacios.
    """
    s = (name or "").strip().upper()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def resolve_locality(name: str) -> tuple[str, dict]:
    """Devuelve ``(clave_normalizada, {sucursal, zona, display_name})``.

    Lanza :class:`LocalityNotFoundError` si no está en el listado.
    """
    key = normalize_locality(name)
    if not key:
        raise LocalityNotFoundError("destino vacío")
    data = _localidades().get(key)
    if not data:
        raise LocalityNotFoundError(
            f"Localidad '{name}' no está en el listado del cotizador "
            "(consultar al área de ventas)"
        )
    return key, data


def find_tramo(kg: float) -> TramoMeta:
    """Devuelve el tramo que contiene ``kg`` (extremos inclusivos por arriba).

    El tramo ``0-5`` cubre 0 < kg <= 5; ``6-10`` cubre 5 < kg <= 10; etc.
    Lanza :class:`WeightOutOfRangeError` si supera el último tramo.
    """
    kg = max(1.0, float(kg))
    last: TramoMeta | None = None
    for t in _tramos():
        last = t
        # tramos contiguos: 0-5, 6-10, 11-20 → tratamos como rangos cerrados arriba
        if kg <= t.to_kg:
            return t
    if last is None:
        raise RouteNotFoundError("no hay tramos cargados")
    if kg > last.to_kg:
        raise WeightOutOfRangeError(
            f"kg cobrable ({kg}) supera el tramo máximo {last.tramo}; "
            "requiere cotización manual"
        )
    return last


def previous_tramo(t: TramoMeta) -> TramoMeta | None:
    prev: TramoMeta | None = None
    for cur in _tramos():
        if cur.tramo == t.tramo:
            return prev
        prev = cur
    return None


def _route_rate(
    origin: str, destination: str, zona: str, tramo: str
) -> tuple[float, float]:
    rates = _tarifas().get(origin, {}).get(destination, {}).get(zona, {})
    entry = rates.get(tramo)
    if not entry:
        raise RouteNotFoundError(
            f"sin tarifa para {origin}->{destination} zona={zona} tramo={tramo}"
        )
    return float(entry.get("tarifa") or 0.0), float(entry.get("minimo") or 0.0)


def quote(
    *,
    destination_locality: str,
    packages: list[Package],
    origin: str = "SCL",
    iva_pct: float = 19.0,
) -> QuoteResult:
    """Calcula la tarifa para una entrega.

    :param destination_locality: ej. "Antofagasta", "Viña del Mar".
    :param packages: lista de :class:`Package`. Vacía → kg_cobrable = 1.
    :param origin: sucursal de origen (default SCL/Santiago).
    :param iva_pct: 19 para Chile. Pasar 0 para devolver solo el neto.
    """
    if not packages:
        packages = []

    matched_key, dest = resolve_locality(destination_locality)
    sucursal = str(dest["sucursal"]).strip().upper()
    zona = str(dest["zona"]).strip().upper()
    origin_norm = (origin or "SCL").strip().upper()

    kg_fisico = sum(p.physical_kg() for p in packages)
    kg_vol = sum(p.volumetric_kg() for p in packages)
    kg_cobrable = max(1.0, kg_fisico, kg_vol)

    t1 = find_tramo(kg_cobrable)
    tarifa, minimo = _route_rate(origin_norm, sucursal, zona, t1.tramo)
    subtotal_t1 = max(minimo, kg_cobrable * tarifa)

    t2 = previous_tramo(t1)
    subtotal_t2 = 0.0
    tramo_anterior_str: str | None = None
    if t2 is not None:
        try:
            tarifa_prev, minimo_prev = _route_rate(
                origin_norm, sucursal, zona, t2.tramo
            )
        except RouteNotFoundError:
            tarifa_prev, minimo_prev = 0.0, 0.0
        subtotal_t2 = max(minimo_prev, t2.to_kg * tarifa_prev)
        tramo_anterior_str = t2.tramo

    total_neto = max(subtotal_t1, subtotal_t2)
    total_neto_int = int(round(total_neto))
    iva = max(0.0, float(iva_pct or 0))
    total_con_iva = int(round(total_neto * (1.0 + iva / 100.0)))

    return QuoteResult(
        origin=origin_norm,
        destination_sucursal=sucursal,
        destination_zona=zona,
        locality_input=destination_locality,
        locality_matched=matched_key,
        kg_fisico=kg_fisico,
        kg_volumetrico=kg_vol,
        kg_cobrable=kg_cobrable,
        tramo=t1.tramo,
        tramo_anterior=tramo_anterior_str,
        tarifa_clp_kg=tarifa,
        minimo_clp=minimo,
        subtotal_t1_clp=subtotal_t1,
        subtotal_t2_clp=subtotal_t2,
        total_neto_clp=total_neto_int,
        total_con_iva_clp=total_con_iva,
        iva_pct=iva,
    )


def parse_packages(raw: object) -> list[Package]:
    """Convierte una lista de dicts del request a ``Package``.

    Acepta keys ``weight_kg`` o ``peso_kg``; ``height_cm`` o ``alto_cm``;
    ``length_cm`` o ``largo_cm``; ``width_cm`` o ``ancho_cm``.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("packages debe ser una lista")

    out: list[Package] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"packages[{idx}] debe ser objeto")

        def num(*keys: str) -> float:
            for k in keys:
                if k in item and item[k] is not None:
                    try:
                        return float(item[k])
                    except (TypeError, ValueError) as e:
                        raise ValueError(
                            f"packages[{idx}].{k} debe ser numérico"
                        ) from e
            return 0.0

        out.append(
            Package(
                weight_kg=num("weight_kg", "peso_kg"),
                height_cm=num("height_cm", "alto_cm"),
                length_cm=num("length_cm", "largo_cm"),
                width_cm=num("width_cm", "ancho_cm"),
            )
        )
    return out


def get_origin_default() -> str:
    """SCL salvo override por env (compat futuro multi-bodega)."""
    import os

    return (os.environ.get("SHIPPING_ORIGIN_SUCURSAL") or "SCL").strip().upper()


def get_iva_pct_default() -> float:
    import os

    raw = (os.environ.get("SHIPPING_IVA_PCT") or "19").strip()
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 19.0


def list_localidades() -> list[dict]:
    """Listado para autocompletes en el UI del cotizador."""
    out: list[dict] = []
    for key, value in sorted(_localidades().items()):
        out.append(
            {
                "key": key,
                "display_name": value.get("display_name") or key,
                "sucursal": value.get("sucursal"),
                "zona": value.get("zona"),
            }
        )
    return out


__all__ = [
    "LocalityNotFoundError",
    "Package",
    "QuoteResult",
    "RouteNotFoundError",
    "TramoMeta",
    "WeightOutOfRangeError",
    "find_tramo",
    "get_iva_pct_default",
    "get_origin_default",
    "list_localidades",
    "normalize_locality",
    "parse_packages",
    "previous_tramo",
    "quote",
    "resolve_locality",
]


# Smoke check sólo cuando se ejecuta el módulo directamente (no en cold-start).
if __name__ == "__main__":  # pragma: no cover
    sys.path.insert(0, str(_SERVICE_ROOT))
    r = quote(
        destination_locality="Antofagasta",
        packages=[Package(weight_kg=1, height_cm=0, length_cm=0, width_cm=0)],
    )
    print(r.to_dict())

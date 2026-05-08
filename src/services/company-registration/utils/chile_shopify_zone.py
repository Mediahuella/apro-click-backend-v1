"""ISO 3166-2:CL → `zoneCode` para direcciones Shopify (B2B / Admin GraphQL).

Shopify espera subdivisiones estándar, p. ej. ``CL-RM`` para Región Metropolitana,
no códigos informales tipo ``RM`` solos ni ``VIII`` como única clave cuando la API valida contra ISO.

Referencias: Shopify ``provinceCode`` / ISO 3166-2:CL.
"""
from __future__ import annotations

import re
import unicodedata


def _fold_accents_upper(s: str) -> str:
    nfc = unicodedata.normalize("NFD", s.strip())
    no_marks = "".join(c for c in nfc if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", no_marks.upper()).strip()


# Códigos oficiales (sufijo ISO 3166-2:CL) → valor completo
_SUFFIX_TO_ISO: frozenset[str] = frozenset(
    {"AP", "TA", "AN", "AT", "CO", "VS", "RM", "LI", "ML", "NB", "BI", "AR", "LR", "LL", "AI", "MA"}
)


# Numeración oficial chilena (romanos) tras creación XVI Ñuble (~2018)
_ROMAN_REGION: dict[str, str] = {
    "XV": "CL-AP",
    "I": "CL-TA",
    "II": "CL-AN",
    "III": "CL-AT",
    "IV": "CL-CO",
    "V": "CL-VS",
    "VI": "CL-LI",
    "VII": "CL-ML",
    "XVI": "CL-NB",
    "VIII": "CL-BI",
    "IX": "CL-AR",
    "XIV": "CL-LR",
    "X": "CL-LL",
    "XI": "CL-AI",
    "XII": "CL-MA",
    # Metropolitana (no coincide con orden romano estándar)
    "RM": "CL-RM",
    "XIII": "CL-RM",
}


# Alias comunes después de `_fold_accents_upper` (sin REGION / DE prefixes strictly required)
_REGION_NAME_TO_ISO: dict[str, str] = {
    "ARICA Y PARINACOTA": "CL-AP",
    "TARAPACA": "CL-TA",
    "ANTOFAGASTA": "CL-AN",
    "ATACAMA": "CL-AT",
    "COQUIMBO": "CL-CO",
    "VALPARAISO": "CL-VS",
    "REGION METROPOLITANA DE SANTIAGO": "CL-RM",
    "REGION METROPOLITANA": "CL-RM",
    "METROPOLITANA DE SANTIAGO": "CL-RM",
    "METROPOLITANA": "CL-RM",
    "REGION DEL LIBERTADOR GENERAL BERNARDO OHIGGINS": "CL-LI",
    "LIBERTADOR BERNARDO OHIGGINS": "CL-LI",
    "OHIGGINS": "CL-LI",
    "REGION DEL MAULE": "CL-ML",
    "MAULE": "CL-ML",
    "REGION DE NUBLE": "CL-NB",
    "REGION DEL NUBLE": "CL-NB",
    "NUBLE": "CL-NB",
    "REGION DEL BIOBIO": "CL-BI",
    "REGION DEL BIO BIO": "CL-BI",
    "BIOBIO": "CL-BI",
    "REGION DE LA ARAUCANIA": "CL-AR",
    "LA ARAUCANIA": "CL-AR",
    "ARUCANIA": "CL-AR",
    "ARAUCANIA": "CL-AR",
    "REGION DE LOS RIOS": "CL-LR",
    "LOS RIOS": "CL-LR",
    "REGION DE LOS LAGOS": "CL-LL",
    "LOS LAGOS": "CL-LL",
    "REGION DE AISEN DEL GENERAL CARLOS IBANEZ DEL CAMPO": "CL-AI",
    "REGION DE AYSEN": "CL-AI",
    "AISEN": "CL-AI",
    "AYSEN": "CL-AI",
    "REGION DE MAGALLANES Y DE LA ANTARTICA CHILENA": "CL-MA",
    "MAGALLANES": "CL-MA",
}

_CL_ISO_PATTERN = re.compile(r"^CL-[A-Z]{2}$")


def _try_chile_roman_region(folded: str) -> str | None:
    """Interpreta romanos regionales sin confundir con una 'calle tipo I' u otra palabra suelta."""
    s = folded.strip()
    for _ in range(6):
        ns = re.sub(
            r"^(REGION|REGIONAL|COMUNA|PROVINCE|STATE)\s+",
            "",
            s,
        ).strip()
        ns = re.sub(r"^(DE|DEL|DE LA|LAS?|LOS?|LA|EL)\s+", "", ns).strip()
        if ns == s:
            break
        s = ns
    collapsed = re.sub(r"\s+", "", s)
    if collapsed in _ROMAN_REGION:
        return _ROMAN_REGION[collapsed]
    m = re.match(
        r"^(XVI|XV|XIV|XIII|XII|XI|X|IX|VIII|VII|VI|V|IV|III|II|I|RM)(?:\s+|$)",
        s,
    )
    if m and m.group(1) in _ROMAN_REGION:
        return _ROMAN_REGION[m.group(1)]
    return None


def normalize_chile_shopify_zone_code(zone_raw: str) -> str:
    """Devuelve un código tipo ``CL-XX`` recognizable por Shopify para ``countryCode`` CL."""
    zone = zone_raw.strip()
    if not zone:
        return zone

    u_raw = zone.upper()
    if _CL_ISO_PATTERN.match(u_raw):
        return u_raw

    folded = _fold_accents_upper(zone)

    # "CL RM" → CL-RM
    folded_nospace = folded.replace(" ", "")
    if _CL_ISO_PATTERN.match(folded_nospace):
        return folded_nospace
    if folded_nospace.startswith("CL") and len(folded_nospace) == 4:
        # CL + 2-letter suffix pasted without hyphen
        maybe = "CL-" + folded_nospace[2:]
        if _CL_ISO_PATTERN.match(maybe):
            return maybe

    token = folded
    token = re.sub(r"^(REGION|REGIONAL|COMUNA|PROVINCE|STATE)\s+", "", token)
    token = re.sub(r"^(DE|DEL|DE LA|LAS?|LOS?|LA|EL)\s+", "", token)
    token_stripped = token.strip()

    if token_stripped in _REGION_NAME_TO_ISO:
        return _REGION_NAME_TO_ISO[token_stripped]

    # Nombres más largos: contiene substring distintivo (evitar falsos positivos cortos)
    for name, iso in sorted(_REGION_NAME_TO_ISO.items(), key=lambda kv: len(kv[0]), reverse=True):
        if len(name) >= 8 and name in folded:
            return iso

    roman_iso = _try_chile_roman_region(folded)
    if roman_iso:
        return roman_iso

    # Sufijo de dos letras sin prefijo CL
    suffix_only = folded.replace("-", "").replace(" ", "").upper()
    if len(suffix_only) == 2 and suffix_only in _SUFFIX_TO_ISO:
        return f"CL-{suffix_only}"

    return u_raw


__all__ = ["normalize_chile_shopify_zone_code"]

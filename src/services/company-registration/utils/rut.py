"""Normalización y validación de RUT chileno (empresa o persona)."""
from __future__ import annotations

import re


def normalize_rut_digits(rut: str) -> str:
    """Solo dígitos del cuerpo + dígito verificador (K mayúscula)."""
    s = rut.strip().upper().replace(".", "").replace("-", "")
    s = re.sub(r"[^0-9K]", "", s)
    return s


def _calc_dv(body_digits: str) -> str:
    if not body_digits.isdigit():
        raise ValueError("Cuerpo del RUT inválido")
    s = 0
    m = 2
    for ch in reversed(body_digits):
        s += int(ch) * m
        m = m + 1 if m < 7 else 2
    r = 11 - (s % 11)
    if r == 11:
        return "0"
    if r == 10:
        return "K"
    return str(r)


def rut_is_valid(rut: str) -> bool:
    """Valida dígito verificador."""
    raw = normalize_rut_digits(rut)
    if len(raw) < 2:
        return False
    body, dv = raw[:-1], raw[-1]
    if not body.isdigit():
        return False
    try:
        return _calc_dv(body) == dv
    except ValueError:
        return False


def format_rut_stored(rut: str) -> str:
    """Formato interno: `76123456-7` o `76123456-K`."""
    raw = normalize_rut_digits(rut)
    if len(raw) < 2:
        raise ValueError("RUT demasiado corto")
    body, dv = raw[:-1], raw[-1]
    if not rut_is_valid(raw):
        raise ValueError("RUT inválido (dígito verificador)")
    return f"{body}-{dv}"

"""Carga opcional del archivo `.env` en la raíz del repo (sin dependencia python-dotenv)."""
from __future__ import annotations

import os
from pathlib import Path


def parse_dotenv_file(path: Path) -> dict[str, str]:
    """Lectura mínima de .env (líneas KEY=VALUE)."""
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip().strip('"').strip("'")
        out[key] = value
    return out


def load_root_dotenv_defaults(project_root: Path | None = None) -> None:
    """
    Aplica variables de ``<project_root>/.env`` con ``os.environ.setdefault``:
    no pisa claves ya exportadas en el shell (mismo criterio que muchas herramientas).
    """
    root = project_root or Path(__file__).resolve().parent.parent
    for key, value in parse_dotenv_file(root / ".env").items():
        os.environ.setdefault(key, value)


def environ_with_root_dotenv(project_root: Path) -> dict[str, str]:
    """
    Entorno para subprocess: valores del .env como base y luego el proceso actual
    (las variables exportadas en shell tienen prioridad).
    """
    file_vars = parse_dotenv_file(project_root / ".env")
    merged = dict(file_vars)
    merged.update(os.environ)
    return merged

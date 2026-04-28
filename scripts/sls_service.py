#!/usr/bin/env python3
"""Ejecuta serverless en el directorio de un servicio."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
from sls_cli import environ_with_root_dotenv


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def main() -> int:
    argv = sys.argv[1:]
    if len(argv) < 2:
        print(
            "uso: sls_service.py <servicio> <comando serverless...>",
            file=sys.stderr,
        )
        print("ejemplo: sls_service.py auth print", file=sys.stderr)
        return 1

    service = argv[0]
    sls_args = argv[1:]

    root = project_root()
    svc_dir = root / "src" / "services" / service
    if not svc_dir.is_dir():
        print(f"error: servicio no encontrado: {svc_dir}", file=sys.stderr)
        return 1

    cmd = ["npx", "sls", *sls_args]
    env = environ_with_root_dotenv(root)
    return subprocess.call(cmd, cwd=svc_dir, env=env)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Copia src/shared/ a cada servicio bajo src/services/<nombre>/shared/."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def main() -> int:
    root = project_root()
    shared_src = root / "src" / "shared"
    if not shared_src.is_dir():
        print(f"error: no existe {shared_src}", file=sys.stderr)
        return 1

    services_dir = root / "src" / "services"
    if not services_dir.is_dir():
        print(f"error: no existe {services_dir}", file=sys.stderr)
        return 1

    for svc_dir in sorted(services_dir.iterdir()):
        if not svc_dir.is_dir():
            continue
        name = svc_dir.name
        dest = svc_dir / "shared"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(shared_src, dest)
        print(f"sync-shared: {name} <- src/shared")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

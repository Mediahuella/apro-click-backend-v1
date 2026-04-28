#!/usr/bin/env python3
"""Crea la estructura de un servicio Serverless nuevo bajo src/services/<slug>/."""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from string import Template

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def validate_slug(slug: str) -> str:
    slug = slug.strip().lower()
    if not SLUG_RE.match(slug):
        raise ValueError(
            "el nombre debe ser slug: minúsculas, números y guiones; "
            "debe empezar con letra (ej: orders, user-profiles)"
        )
    if slug in {"common", "shared", "src", "services"}:
        raise ValueError(f"nombre reservado: {slug}")
    return slug


def write_text(path: Path, content: str, dry_run: bool) -> None:
    if dry_run:
        print(f"  [dry-run] crear {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"  creado {path.relative_to(project_root())}")


def serverless_yml(slug: str, service_name: str) -> str:
    # Paths relativos al directorio del servicio (src/services/<slug>/).
    # Todas las functions se declaran solo en este archivo (ej. health mínimo + las demás).
    return f"""service: {service_name}
frameworkVersion: '4'

useDotenv: true

custom:
  serviceSlug: {slug}
  pythonRequirements: ${{file(../../serverless.pythonRequirements.yml):pythonRequirements}}

package: ${{file(../../serverless.package.yml):package}}

provider: ${{file(../../serverless.provider.yml):providerBase}}

functions:
  health:
    handler: handlers/health.lambda_handler
    events:
      - httpApi:
          path: /api/v1/health-${{self:custom.serviceSlug}}
          method: get
"""


def health_py(slug: str, service_name: str) -> str:
    t = Template(
        '''"""Health check HTTP API — servicio: $service_name"""
from __future__ import annotations

import sys
from pathlib import Path

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver
from aws_lambda_powertools.utilities.typing import LambdaContext

# ============================================================================
# CONFIGURAR PATHS PRIMERO (raíz del servicio = carpeta del servicio)
# ============================================================================
service_root = Path(__file__).resolve().parent.parent
if str(service_root) not in sys.path:
    sys.path.insert(0, str(service_root))

lambda_root = "/var/task"
if lambda_root not in sys.path:
    sys.path.insert(0, lambda_root)

possible_shared_paths = [
    Path("/var/task/shared"),
    service_root / "shared",
]

for path in possible_shared_paths:
    if path.exists() and (path / "database").exists():
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
        break

logger = Logger()
tracer = Tracer()
app = APIGatewayHttpResolver()

SERVICE_NAME = "$service_name"


@app.get("/api/v1/health-$slug")
@tracer.capture_method
def health():
    logger.info("Health check requested")
    return {
        "status": "healthy",
        "service": SERVICE_NAME,
    }


def lambda_handler(event: dict, context: LambdaContext):
    return app.resolve(event, context)
'''
    )
    return t.substitute(slug=slug, service_name=service_name)


REQUIREMENTS_TXT = """# Dependencias propias del servicio (Powertools va en layer; ver serverless.pythonRequirements.yml)
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Crea un servicio en src/services/<slug>/ (handlers/, models/, … sin src/ anidado)",
    )
    parser.add_argument(
        "slug",
        help="Nombre corto del servicio (ej: orders, billing)",
    )
    parser.add_argument(
        "--name",
        dest="service_name",
        help="Nombre lógico en serverless (default: apro-click-admin-<slug>)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Solo muestra qué se crearía",
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="No ejecutar sync de src/shared al final",
    )
    args = parser.parse_args()

    try:
        slug = validate_slug(args.slug)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    service_name = args.service_name or f"apro-click-admin-{slug}"
    root = project_root()
    svc_dir = root / "src" / "services" / slug

    if svc_dir.exists():
        print(f"error: ya existe {svc_dir}", file=sys.stderr)
        return 1

    dry = args.dry_run
    print(f"Creando servicio '{slug}' ({service_name}) en {svc_dir}")

    files: list[tuple[Path, str]] = [
        (svc_dir / "handlers" / "__init__.py", ""),
        (svc_dir / "handlers" / "health.py", health_py(slug, service_name)),
        (svc_dir / "models" / "__init__.py", ""),
        (svc_dir / "services" / "__init__.py", ""),
        (svc_dir / "utils" / "__init__.py", ""),
        (svc_dir / "requirements.txt", REQUIREMENTS_TXT),
        (svc_dir / "serverless.yml", serverless_yml(slug, service_name)),
    ]

    for path, content in files:
        write_text(path, content, dry)

    if not dry and not args.no_sync:
        sync_script = root / "scripts" / "sync_shared.py"
        print("Sincronizando shared/ ...")
        r = subprocess.run(
            [sys.executable, str(sync_script)],
            cwd=root,
        )
        if r.returncode != 0:
            return r.returncode

    if dry:
        print("Dry-run: no se escribieron archivos.")
    else:
        print("Listo. Siguiente: revisar serverless.yml y ejecutar deploy cuando toque.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

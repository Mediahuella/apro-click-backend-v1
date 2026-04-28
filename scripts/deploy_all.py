#!/usr/bin/env python3
"""Despliega todos los servicios que tengan serverless.yml bajo src/services/."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
from sls_cli import DEFAULT_AWS_PROFILE, environ_with_root_dotenv, sls_deploy_cmd


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def discover_services(root: Path) -> list[str]:
    base = root / "src" / "services"
    if not base.is_dir():
        return []
    names: list[str] = []
    for p in sorted(base.iterdir()):
        if p.is_dir() and (p / "serverless.yml").is_file():
            names.append(p.name)
    return names


def run_sync(root: Path) -> int:
    sync = root / "scripts" / "sync_shared.py"
    print(">>> shared:sync")
    return subprocess.call([sys.executable, str(sync)], cwd=root)


def run_deploy(
    service: str,
    root: Path,
    stage: str,
    aws_profile: str,
    extra: list[str],
) -> int:
    svc_dir = root / "src" / "services" / service
    cmd = sls_deploy_cmd(stage, aws_profile, extra)
    print(f">>> deploy: {service} ({' '.join(cmd)})")
    env = environ_with_root_dotenv(root)
    return subprocess.call(cmd, cwd=svc_dir, env=env)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ejecuta `npx sls deploy` (verbose + aws-profile) en cada servicio con serverless.yml.",
    )
    parser.add_argument(
        "-s",
        "--stage",
        default="dev",
        help="Stage de Serverless (default: dev)",
    )
    parser.add_argument(
        "--aws-profile",
        default=DEFAULT_AWS_PROFILE,
        metavar="PERFIL",
        help=f"Perfil AWS (default: {DEFAULT_AWS_PROFILE} o env DEPLOY_AWS_PROFILE)",
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="No ejecutar scripts/sync_shared.py antes de desplegar",
    )
    parser.add_argument(
        "--continue",
        dest="continue_on_error",
        action="store_true",
        help="Seguir con el siguiente servicio si uno falla",
    )
    parser.add_argument(
        "sls_extra",
        nargs="*",
        help="Argumentos extra al final de cada `sls deploy` (después de --stage)",
    )
    args = parser.parse_args()

    root = project_root()
    services = discover_services(root)
    if not services:
        print("error: no hay servicios con serverless.yml en src/services/", file=sys.stderr)
        return 1

    print(f"Servicios a desplegar ({len(services)}): {', '.join(services)}")
    print(f"Stage: {args.stage}  |  AWS profile: {args.aws_profile}\n")

    if not args.no_sync:
        rc = run_sync(root)
        if rc != 0:
            return rc
        print()

    failed: list[str] = []
    for name in services:
        rc = run_deploy(name, root, args.stage, args.aws_profile, args.sls_extra)
        if rc != 0:
            failed.append(name)
            if not args.continue_on_error:
                print(f"error: deploy falló en {name}", file=sys.stderr)
                return rc
        print()

    if failed:
        print(f"error: fallaron: {', '.join(failed)}", file=sys.stderr)
        return 1

    print("deploy:all completado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

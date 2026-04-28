#!/usr/bin/env python3
"""Despliegue con `npx sls deploy` de un solo servicio: src/services/<nombre>/.

Comando base (stage y perfil ajustables):
  npx sls deploy --verbose --aws-profile mh-prod --stage <stage>

Uso:
  python3 scripts/deploy_service.py <servicio> [stage] [--aws-profile PERFIL] [args extra para sls...]

Ejemplos:
  python3 scripts/deploy_service.py auth
  python3 scripts/deploy_service.py auth dev
  python3 scripts/deploy_service.py auth prod --aws-profile mh-staging

Antes de desplegar conviene: npm run shared:sync
"""
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deploy de un servicio con npx sls deploy --verbose --aws-profile ...",
    )
    parser.add_argument("service", help="Carpeta bajo src/services/")
    parser.add_argument(
        "stage",
        nargs="?",
        default="dev",
        help="Stage Serverless (default: dev)",
    )
    parser.add_argument(
        "--aws-profile",
        default=DEFAULT_AWS_PROFILE,
        metavar="PERFIL",
        help=f"Perfil AWS (default: {DEFAULT_AWS_PROFILE} o env DEPLOY_AWS_PROFILE)",
    )
    args, extra = parser.parse_known_args()

    root = project_root()
    svc_dir = root / "src" / "services" / args.service
    if not svc_dir.is_dir():
        print(f"error: servicio no encontrado: {svc_dir}", file=sys.stderr)
        return 1

    cmd = sls_deploy_cmd(args.stage, args.aws_profile, extra)
    env = environ_with_root_dotenv(root)
    return subprocess.call(cmd, cwd=svc_dir, env=env)


if __name__ == "__main__":
    raise SystemExit(main())

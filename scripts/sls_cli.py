"""Constantes y comando base para invocaciones de Serverless Framework vía `npx sls`."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv_loader import environ_with_root_dotenv

# Re-export para scripts que importaban desde aquí
__all__ = ["DEFAULT_AWS_PROFILE", "environ_with_root_dotenv", "sls_deploy_cmd"]

# Perfil AWS por defecto para deploy (sobrescribible con --aws-profile o DEPLOY_AWS_PROFILE)
DEFAULT_AWS_PROFILE = os.environ.get("DEPLOY_AWS_PROFILE", "mh-prod")


def sls_deploy_cmd(stage: str, aws_profile: str, extra: list[str]) -> list[str]:
    """Equivale a: npx sls deploy --verbose --aws-profile <profile> --stage <stage> [extra...]"""
    return [
        "npx",
        "sls",
        "deploy",
        "--verbose",
        "--aws-profile",
        aws_profile,
        "--stage",
        stage,
        *extra,
    ]

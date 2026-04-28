"""DynamoDB table helper — shared across services."""
from __future__ import annotations

import os
from functools import lru_cache

import boto3
from aws_lambda_powertools import Logger

logger = Logger()

_dynamodb_resource = None


def _get_dynamodb_resource():
    global _dynamodb_resource
    if _dynamodb_resource is None:
        region = os.getenv("REGION", "us-east-2")
        _dynamodb_resource = boto3.resource("dynamodb", region_name=region)
    return _dynamodb_resource


@lru_cache(maxsize=16)
def get_table(table_key: str, *, full_table_name: str | None = None):
    """Return a DynamoDB Table resource.

    Parameters
    ----------
    table_key:
        Logical short name (used only for the cache key).
    full_table_name:
        Physical table name in AWS.  When *None* a default naming
        convention ``apro-click-{stage}-{table_key}`` is applied.
    """
    stage = os.getenv("STAGE", "dev")
    name = full_table_name or f"apro-click-{stage}-{table_key}"
    logger.debug("Resolving DynamoDB table", extra={"table": name})
    return _get_dynamodb_resource().Table(name)

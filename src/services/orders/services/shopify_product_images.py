"""URLs de imagen destacada del producto vía Shopify Admin REST API."""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError
from urllib.request import Request, urlopen

_MAX_PRODUCT_IDS = 50
_TIMEOUT_SEC = 10


def _api_version() -> str:
    return (os.environ.get("SHOPIFY_API_VERSION") or "2024-10").strip()


def _fetch_one_featured_src(
    shop_domain: str, access_token: str, product_id: str
) -> str | None:
    shop = shop_domain.strip().lower()
    ver = _api_version()
    url = f"https://{shop}/admin/api/{ver}/products/{product_id}.json?fields=id,image"
    req = Request(url, headers={"X-Shopify-Access-Token": access_token})
    try:
        with urlopen(req, timeout=_TIMEOUT_SEC) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        if e.code != 404:
            pass
        return None
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    prod = body.get("product") if isinstance(body, dict) else None
    if not isinstance(prod, dict):
        return None
    img = prod.get("image")
    if not isinstance(img, dict):
        return None
    src = img.get("src")
    return src if isinstance(src, str) and src.strip() else None


def fetch_product_featured_images_bulk(
    shop_domain: str, access_token: str, product_ids: list[str]
) -> dict[str, str | None]:
    uniq: list[str] = []
    seen: set[str] = set()
    for pid in product_ids:
        s = str(pid).strip() if pid is not None else ""
        if not s or s in seen:
            continue
        seen.add(s)
        uniq.append(s)
        if len(uniq) >= _MAX_PRODUCT_IDS:
            break

    if not uniq:
        return {}

    out: dict[str, str | None] = {pid: None for pid in uniq}
    workers = min(8, len(uniq))

    def job(pid: str) -> tuple[str, str | None]:
        return pid, _fetch_one_featured_src(shop_domain, access_token, pid)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(job, pid) for pid in uniq]
        for fut in as_completed(futures):
            pid, src = fut.result()
            out[pid] = src
    return out

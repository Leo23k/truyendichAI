"""Copy referenced Supabase Storage chapter and cover objects to Backblaze B2.

The source and database are read-only. Existing B2 objects are retained.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlparse

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from app.storage import configured, exists, upload


def headers() -> dict[str, str]:
    key = os.getenv("SUPABASE_SERVICE_KEY", "") or os.getenv("SUPABASE_SECRET_KEY", "")
    if not key:
        raise SystemExit("Set SUPABASE_SERVICE_KEY (or SUPABASE_SECRET_KEY) first.")
    result = {"apikey": key}
    if key.startswith("eyJ"):
        result["Authorization"] = f"Bearer {key}"
    return result


def rows(client: httpx.Client, table: str, select: str) -> list[dict]:
    base = os.environ["SUPABASE_URL"].rstrip("/")
    result: list[dict] = []
    offset = 0
    while True:
        response = client.get(
            f"{base}/rest/v1/{table}",
            params={"select": select, "limit": "1000", "offset": str(offset)}, headers=headers(),
        )
        response.raise_for_status()
        page = response.json()
        result.extend(page)
        if len(page) < 1000:
            return result
        offset += len(page)


def object_path(value: str) -> tuple[str, str] | None:
    parsed = urlparse(value)
    path = parsed.path if parsed.scheme else value
    marker = "/storage/v1/object/public/"
    if marker in path:
        path = path.split(marker, 1)[1]
    path = path.lstrip("/")
    bucket, separator, key = path.partition("/")
    if bucket in {"chapters", "covers"} and separator and key and ".." not in key.split("/"):
        return bucket, key
    return None


def main() -> None:
    if not configured():
        raise SystemExit("Set B2_ENDPOINT, B2_KEY_ID, B2_APPLICATION_KEY and B2_BUCKET first.")
    if not os.getenv("SUPABASE_URL"):
        raise SystemExit("Set SUPABASE_URL first.")
    with httpx.Client(timeout=90, trust_env=False) as source:
        paths = {
            item for row in rows(source, "chapters", "content_path")
            if (item := object_path(str(row.get("content_path") or "")))
        }
        paths.update(
            item for row in rows(source, "novels", "cover_url")
            if (item := object_path(str(row.get("cover_url") or "")))
        )
        base = os.environ["SUPABASE_URL"].rstrip("/")
        def copy_one(item: tuple[str, str]) -> bool:
            bucket, key = item
            if exists(f"{bucket}/{key}"):
                return False
            response = source.get(f"{base}/storage/v1/object/{bucket}/{quote(key, safe='/')}", headers=headers())
            response.raise_for_status()
            content_type = response.headers.get("content-type", "application/octet-stream")
            return upload(f"{bucket}/{key}", response.content, content_type)
        workers = max(1, min(16, int(os.getenv("B2_MIGRATION_WORKERS", "12"))))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(pool.map(copy_one, sorted(paths)))
        copied = sum(outcomes)
        skipped = len(outcomes) - copied
    print(f"B2 migration complete: {copied} copied, {skipped} unchanged, {len(paths)} verified.")


if __name__ == "__main__":
    main()

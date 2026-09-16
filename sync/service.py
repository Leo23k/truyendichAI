"""Incremental Cloudflare R2 synchronization for LeoTruyen data."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import time
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import PROJECT_ROOT


logger = logging.getLogger(__name__)
R2_PREFIX = "data"
SYNC_DIRECTORIES = ("covers", "translations", "audio_cache", "tts_cache", "audio")
RETRY_ATTEMPTS = 3


class R2SyncError(RuntimeError):
    pass


def r2_configured() -> bool:
    return all(
        os.getenv(name)
        for name in (
            "CLOUDFLARE_ACCOUNT_ID",
            "CLOUDFLARE_R2_BUCKET",
            "CLOUDFLARE_ACCESS_KEY",
            "CLOUDFLARE_SECRET_KEY",
        )
    )


def data_root() -> Path:
    configured = os.getenv("R2_DATA_DIR")
    return Path(configured) if configured else PROJECT_ROOT / "data"


def r2_client():
    if not r2_configured():
        raise R2SyncError("Cloudflare R2 environment variables are not configured.")
    account_id = os.environ["CLOUDFLARE_ACCOUNT_ID"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["CLOUDFLARE_ACCESS_KEY"],
        aws_secret_access_key=os.environ["CLOUDFLARE_SECRET_KEY"],
        config=Config(retries={"max_attempts": RETRY_ATTEMPTS, "mode": "standard"}),
        region_name="auto",
    )


def bucket_name() -> str:
    return os.environ["CLOUDFLARE_R2_BUCKET"]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def object_key(path: Path) -> str:
    return f"{R2_PREFIX}/{path.relative_to(data_root()).as_posix()}"


def local_files() -> list[Path]:
    root = data_root()
    files: list[Path] = []
    database = root / "database.db"
    if database.exists():
        files.append(database)
    for directory in SYNC_DIRECTORIES:
        path = root / directory
        if path.exists():
            files.extend(item for item in path.rglob("*") if item.is_file())
    return files


def with_retry(operation, description: str):
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return operation()
        except (BotoCoreError, ClientError, OSError) as exc:
            if attempt == RETRY_ATTEMPTS:
                raise R2SyncError(f"R2 {description} failed after {attempt} attempts: {exc}") from exc
            logger.warning("R2 %s failed (%s/%s): %s", description, attempt, RETRY_ATTEMPTS, exc)
            time.sleep(attempt)


def remote_sha(client, key: str) -> str | None:
    try:
        response = with_retry(lambda: client.head_object(Bucket=bucket_name(), Key=key), f"inspect {key}")
    except R2SyncError as exc:
        error = exc.__cause__
        if isinstance(error, ClientError) and error.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise
    return response.get("Metadata", {}).get("sha256")


def push_to_r2() -> dict[str, int]:
    client = r2_client()
    uploaded = skipped = 0
    for path in local_files():
        key = object_key(path)
        checksum = sha256(path)
        if remote_sha(client, key) == checksum:
            skipped += 1
            continue
        with_retry(
            lambda: client.upload_file(
                str(path), bucket_name(), key, ExtraArgs={"Metadata": {"sha256": checksum}}
            ),
            f"upload {key}",
        )
        uploaded += 1
    logger.info("R2 push complete: uploaded=%s skipped=%s", uploaded, skipped)
    return {"uploaded": uploaded, "skipped": skipped}


def remote_keys(client) -> list[str]:
    keys: list[str] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in with_retry(lambda: paginator.paginate(Bucket=bucket_name(), Prefix=f"{R2_PREFIX}/"), "list"):
        keys.extend(item["Key"] for item in page.get("Contents", []))
    return keys


def local_path_for_key(key: str) -> Path | None:
    prefix = f"{R2_PREFIX}/"
    if not key.startswith(prefix):
        return None
    relative = Path(key.removeprefix(prefix))
    if ".." in relative.parts:
        return None
    if not relative.parts or (
        relative.parts[0] != "database.db" and relative.parts[0] not in SYNC_DIRECTORIES
    ):
        return None
    return data_root() / relative


def pull_from_r2() -> dict[str, int]:
    client = r2_client()
    downloaded = skipped = 0
    for key in remote_keys(client):
        path = local_path_for_key(key)
        if path is None:
            continue
        checksum = remote_sha(client, key)
        if path.exists() and checksum and sha256(path) == checksum:
            skipped += 1
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with_retry(lambda: client.download_file(bucket_name(), key, str(path)), f"download {key}")
        downloaded += 1
    logger.info("R2 pull complete: downloaded=%s skipped=%s", downloaded, skipped)
    return {"downloaded": downloaded, "skipped": skipped}


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync LeoTruyen data with Cloudflare R2")
    parser.add_argument("command", choices=("push", "pull", "watch"))
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "push":
        push_to_r2()
    elif args.command == "pull":
        pull_from_r2()
    else:
        while True:
            push_to_r2()
            time.sleep(max(10, args.interval))


if __name__ == "__main__":
    main()

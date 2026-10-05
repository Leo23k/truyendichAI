"""Object storage abstraction for Backblaze B2 with Supabase fallback."""

import hashlib
import os
from functools import lru_cache
from pathlib import PurePosixPath

import boto3
from botocore.client import Config


def configured() -> bool:
    return all(os.getenv(name) for name in ("B2_ENDPOINT", "B2_KEY_ID", "B2_APPLICATION_KEY", "B2_BUCKET"))


def _safe(path: str) -> str:
    value = PurePosixPath(path)
    if value.is_absolute() or ".." in value.parts:
        raise ValueError("Invalid object path")
    return value.as_posix()


@lru_cache(maxsize=1)
def client():
    endpoint = os.environ["B2_ENDPOINT"].strip()
    if not endpoint.startswith("https://"):
        endpoint = f"https://{endpoint}"
    return boto3.client(
        "s3", endpoint_url=endpoint,
        aws_access_key_id=os.environ["B2_KEY_ID"],
        aws_secret_access_key=os.environ["B2_APPLICATION_KEY"],
        config=Config(signature_version="s3v4"),
    )


def upload(path: str, content: bytes, content_type: str) -> bool:
    path = _safe(path)
    digest = hashlib.sha256(content).hexdigest()
    s3 = client()
    try:
        head = s3.head_object(Bucket=os.environ["B2_BUCKET"], Key=path)
        if head.get("Metadata", {}).get("sha256") == digest:
            return False
    except Exception:
        pass
    s3.put_object(Bucket=os.environ["B2_BUCKET"], Key=path, Body=content, ContentType=content_type, Metadata={"sha256": digest})
    return True


def exists(path: str) -> bool:
    try:
        client().head_object(Bucket=os.environ["B2_BUCKET"], Key=_safe(path))
        return True
    except Exception:
        return False


def download(path: str) -> bytes:
    return client().get_object(Bucket=os.environ["B2_BUCKET"], Key=_safe(path))["Body"].read()


def presigned_download_url(path: str) -> str:
    """Return a short-lived URL for an object in the private B2 bucket."""
    expires_in = int(os.getenv("B2_PRESIGNED_URL_TTL_SECONDS", "900"))
    return client().generate_presigned_url(
        "get_object",
        Params={"Bucket": os.environ["B2_BUCKET"], "Key": _safe(path)},
        ExpiresIn=max(60, min(expires_in, 3600)),
    )

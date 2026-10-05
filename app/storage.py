"""Object storage abstraction for Backblaze B2 with Supabase fallback."""

import hashlib
import os
from pathlib import PurePosixPath
from urllib.parse import quote

import boto3
from botocore.client import Config


def configured() -> bool:
    return all(os.getenv(name) for name in ("B2_ENDPOINT", "B2_KEY_ID", "B2_APPLICATION_KEY", "B2_BUCKET"))


def _safe(path: str) -> str:
    value = PurePosixPath(path)
    if value.is_absolute() or ".." in value.parts:
        raise ValueError("Invalid object path")
    return value.as_posix()


def public_url(path: str) -> str:
    path = _safe(path)
    base = os.getenv("B2_PUBLIC_URL_BASE", "").rstrip("/")
    if base:
        return f"{base}/{quote(path, safe='/')}"
    endpoint = os.environ["B2_ENDPOINT"].rstrip("/")
    return f"{endpoint}/{os.environ['B2_BUCKET']}/{quote(path, safe='/')}"


def client():
    return boto3.client(
        "s3", endpoint_url=os.environ["B2_ENDPOINT"],
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


def download(path: str) -> bytes:
    return client().get_object(Bucket=os.environ["B2_BUCKET"], Key=_safe(path))["Body"].read()

"""Helpers for public novel snapshots published through jsDelivr."""

import os
from pathlib import PurePosixPath

import httpx


CDN_BASE_URL = os.getenv(
    "JSDELIVR_DATA_BASE_URL",
    "https://cdn.jsdelivr.net/gh/Leo23k/truyendichAI-data@main/data",
).rstrip("/")


class CdnDataError(RuntimeError):
    pass


def cdn_url(path: str) -> str:
    relative = PurePosixPath(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise CdnDataError("Invalid CDN path")
    return f"{CDN_BASE_URL}/{relative.as_posix()}"


def load_cdn_json(path: str) -> dict:
    try:
        with httpx.Client(timeout=15, trust_env=False) as client:
            response = client.get(cdn_url(path))
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CdnDataError(f"Could not load CDN data: {path}") from exc
    if not isinstance(payload, dict):
        raise CdnDataError(f"Invalid CDN JSON: {path}")
    return payload

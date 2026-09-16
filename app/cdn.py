"""Helpers for public novel snapshots published through jsDelivr."""

import json
import os
from pathlib import Path, PurePosixPath

import httpx


CDN_BASE_URL = os.getenv(
    "JSDELIVR_DATA_BASE_URL",
    "https://cdn.jsdelivr.net/gh/Leo23k/truyendichAI-data@main/data",
).rstrip("/")
GITHUB_RAW_DATA_BASE_URL = os.getenv(
    "GITHUB_RAW_DATA_BASE_URL",
    "https://raw.githubusercontent.com/Leo23k/truyendichAI-data/main/data",
).rstrip("/")
LOCAL_SYNC_DATA_DIR = Path(
    os.getenv("SYNCED_DATA_PATH", Path(__file__).resolve().parent.parent / "temp" / "truyendichAI-data" / "data")
)


class CdnDataError(RuntimeError):
    pass


def cdn_url(path: str) -> str:
    relative = PurePosixPath(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise CdnDataError("Invalid CDN path")
    return f"{CDN_BASE_URL}/{relative.as_posix()}"


def raw_data_url(path: str) -> str:
    relative = PurePosixPath(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise CdnDataError("Invalid CDN path")
    return f"{GITHUB_RAW_DATA_BASE_URL}/{relative.as_posix()}"


def load_cdn_json(path: str) -> dict:
    relative = PurePosixPath(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise CdnDataError("Invalid CDN path")
    for url in (cdn_url(path), raw_data_url(path)):
        try:
            with httpx.Client(timeout=15, trust_env=False) as client:
                response = client.get(url)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                return payload
        except (httpx.HTTPError, ValueError):
            continue
    try:
        payload = json.loads(LOCAL_SYNC_DATA_DIR.joinpath(*relative.parts).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CdnDataError(f"Could not load CDN data: {path}") from exc
    if not isinstance(payload, dict):
        raise CdnDataError(f"Invalid CDN JSON: {path}")
    return payload


def load_chapters_index(slug: str) -> list[dict]:
    payload = load_cdn_json(f"novels/{slug}/chapters_index.json")
    chapters = payload.get("chapters", [])
    if not isinstance(chapters, list):
        raise CdnDataError(f"Invalid chapters index: {slug}")
    return [chapter for chapter in chapters if isinstance(chapter, dict)]

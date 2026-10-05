"""Copy local object files to Backblaze B2 without deleting the source."""

import mimetypes
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.storage import configured, upload


def main() -> None:
    if not configured():
        raise SystemExit("Set B2_ENDPOINT, B2_KEY_ID, B2_APPLICATION_KEY and B2_BUCKET first.")
    data = ROOT / "data"
    mappings = ((data / "audio", "audio"), (data / "tts_cache", "audio"))
    total = uploaded = 0
    for source, bucket in mappings:
        if not source.exists():
            continue
        for file in source.rglob("*"):
            if not file.is_file():
                continue
            total += 1
            key = f"{bucket}/{file.relative_to(source).as_posix()}"
            content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            if upload(key, file.read_bytes(), content_type):
                uploaded += 1
                print(f"uploaded {key}")
    print(f"Completed: {uploaded} uploaded, {total} checked. Local files were retained.")


if __name__ == "__main__":
    main()

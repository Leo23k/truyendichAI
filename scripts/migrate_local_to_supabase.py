"""One-shot migration from the local LeoTruyen SQLite database to Supabase."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sync.service import SyncError, sync_once


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        result = sync_once()
    except SyncError as exc:
        raise SystemExit(f"Migration failed: {exc}") from exc
    print(
        "Migration complete: "
        f"{result['novels']} novels, {result['chapters']} chapters, "
        f"{result['json_uploaded']} JSON, {result['audio_uploaded']} MP3, "
        f"{result['covers_uploaded']} covers uploaded."
    )


if __name__ == "__main__":
    main()

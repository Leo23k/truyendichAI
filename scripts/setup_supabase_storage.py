"""Create or make public the LeoTruyen Supabase Storage buckets."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sync.service import SupabaseSync, SyncError


def main() -> None:
    try:
        SupabaseSync().ensure_buckets()
    except SyncError as exc:
        raise SystemExit(f"Storage setup failed: {exc}") from exc
    print("Storage buckets ready: covers, chapters, audio")


if __name__ == "__main__":
    main()

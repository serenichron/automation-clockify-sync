#!/usr/bin/env python3
"""Compatibility entrypoint for the canonical collector implementation.

Keep all collector behavior in ``scripts.clockify_sync_collect``. This wrapper
preserves the historical top-level command without maintaining a second copy.
"""

from scripts.clockify_sync_collect import *  # noqa: F401,F403
from scripts.clockify_sync_collect import main


if __name__ == "__main__":
    raise SystemExit(main())

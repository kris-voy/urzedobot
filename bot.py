#!/usr/bin/env python3
"""Long-running notify-only watcher entry point."""
from __future__ import annotations

import asyncio
import logging
import sys

from config import Config
from watcher import Watcher


def main() -> int:
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        config = Config()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 4
    watcher = Watcher(config)
    try:
        asyncio.run(watcher.run_forever())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

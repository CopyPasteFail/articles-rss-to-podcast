#!/usr/bin/env python
"""Load feed-specific env vars then run the full RSS-to-podcast pipeline."""

import os
import pathlib
import sys

from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent


def main():
    """Entry point for operators: pick a slug, load .env files, run pipeline.py."""
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        sys.stdout = sys.stdout
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    if len(sys.argv) != 2:
        print("Usage: python run_feed.py <feed_slug>")
        sys.exit(2)

    slug = sys.argv[1]
    global_env = ROOT / ".env"
    feed_env = ROOT / "configs" / f"{slug}.env"

    if global_env.exists():
        load_dotenv(dotenv_path=global_env, override=False)
    else:
        print("Warning: global .env not found")

    if not feed_env.exists():
        sys.exit(f"Missing feed env: {feed_env}")
    load_dotenv(dotenv_path=feed_env, override=True)

    print(f"[run] feed={slug} env={feed_env}")
    try:
        import pipeline

        pipeline.main()
    except SystemExit:
        raise
    except Exception as exc:
        sys.exit(f"Pipeline failed: {exc}")


if __name__ == "__main__":
    main()

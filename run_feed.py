#!/usr/bin/env python
"""Load feed-specific env vars then run the full RSS-to-podcast pipeline."""

import sys, pathlib, subprocess
from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent

def main():
    """Entry point for operators: pick a slug, load .env files, run pipeline.py."""
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

    py = sys.executable
    print(f"[run] feed={slug} env={feed_env}")
    subprocess.check_call([py, str(ROOT / "pipeline.py")])

if __name__ == "__main__":
    main()

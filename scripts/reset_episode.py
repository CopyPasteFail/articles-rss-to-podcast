#!/usr/bin/env python
"""Reset a single episode so the pipeline can regenerate/upload it from scratch.

Removes the local MP3/sidecar, drops the entry from Cloudflare KV, and leaves the
feed ready for a rerun. Use this when an IA item was taken offline or you need to
retry with new credentials.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any, cast

from dotenv import load_dotenv


def load_env(slug: str, root: pathlib.Path) -> None:
    """Load the shared .env then the feed-specific env."""
    global_env = root / ".env"
    if global_env.exists():
        load_dotenv(dotenv_path=global_env, override=False)
    feed_env = root / "configs" / f"{slug}.env"
    if not feed_env.exists():
        raise SystemExit(f"Missing feed env: {feed_env}")
    load_dotenv(dotenv_path=feed_env, override=True)


def delete_path(path: pathlib.Path, *, dry_run: bool) -> bool:
    """Delete a file if present, respecting dry-run."""
    if not path.exists():
        return False
    if dry_run:
        print(f"[dry-run] Would delete {path}")
        return True
    path.unlink()
    print(f"Deleted {path}")
    return True


def cleanup_local(link: str, *, root: pathlib.Path, out_dir: pathlib.Path, dry_run: bool) -> list[str]:
    """Remove all local artifacts for the target article."""
    removed: list[str] = []
    seen: set[pathlib.Path] = set()

    for sidecar in out_dir.glob("*.mp3.rssmeta.json"):
        try:
            with sidecar.open("r", encoding="utf-8") as f:
                meta = cast(dict[str, Any], json.load(f))
        except Exception:
            continue
        if meta.get("article_link") != link:
            continue

        mp3_path = pathlib.Path(str(meta.get("mp3_local_path") or "")).expanduser()
        if mp3_path and not mp3_path.is_absolute():
            mp3_path = (root / mp3_path).resolve()
        if not mp3_path.exists():
            guess = sidecar.name.replace(".rssmeta.json", "")
            mp3_path = (sidecar.parent / guess).resolve()

        for target in (mp3_path, sidecar.resolve()):
            if target in seen:
                continue
            seen.add(target)
            if delete_path(target, dry_run=dry_run):
                removed.append(str(target))

    # Also remove the link-hash sidecar used when restoring feed entries.
    try:
        import pipeline  # imported lazily so env is loaded first

        hash_sidecar = out_dir / f"sidecar-{pipeline.link_hash(link)}.json"
        if hash_sidecar.resolve() not in seen and delete_path(hash_sidecar.resolve(), dry_run=dry_run):
            removed.append(str(hash_sidecar.resolve()))
    except Exception:
        pass

    return removed


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset a single episode (local + KV) so it can be regenerated.")
    parser.add_argument("slug", help="Feed slug, e.g. geektime")
    parser.add_argument("article_link", help="Original article URL from the RSS feed")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without modifying anything")
    args = parser.parse_args()

    root = pathlib.Path(__file__).resolve().parent.parent
    load_env(args.slug, root)

    # Ensure repo root is importable, then import after env so pipeline picks up the correct slug/prefix.
    sys.path.insert(0, str(root))
    import pipeline

    if pipeline.SLUG != args.slug:
        print(f"[warn] PODCAST_SLUG={pipeline.SLUG!r} (from env) does not match CLI slug {args.slug!r}")

    identifier = pipeline.ia_identifier_for_link(args.article_link)
    state_key = f"feed:{pipeline.SLUG}"

    state = pipeline._ensure_json_dict(pipeline.kv_get(state_key) or {})
    raw_items: object = state.get("items", {})
    if not isinstance(raw_items, dict):
        raw_items = {}
        state["items"] = raw_items
    items = cast(dict[str, pipeline.JSONDict], raw_items)

    removed_kv = False
    if identifier in items:
        if args.dry_run:
            print(f"[dry-run] Would drop {identifier} from KV state {state_key}")
        else:
            items.pop(identifier, None)
            removed_kv = True
    else:
        print(f"[info] Identifier {identifier} not present in KV state {state_key}")

    if not items:
        for key in ("last_pub_utc", "rss_added", "uploaded_url"):
            state.pop(key, None)

    pipeline.update_latest_state_snapshot(state)

    if removed_kv and not args.dry_run:
        pipeline.kv_put_or_die(state_key, state)
        print(f"[ok] Removed {identifier} from KV state {state_key}")
    elif args.dry_run:
        print("[dry-run] Skipping KV write")

    removed_local = cleanup_local(args.article_link, root=root, out_dir=pipeline.OUT, dry_run=args.dry_run)
    if removed_local:
        print(f"[ok] Removed {len(removed_local)} local file(s)")
    else:
        print("[info] No local artifacts matched that article")

    print("\nNext run `python run_feed.py {}` to regenerate the episode.".format(args.slug))


if __name__ == "__main__":
    main()

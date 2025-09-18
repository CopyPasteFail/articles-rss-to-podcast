#!/usr/bin/env python
import os, sys, json, hashlib, pathlib, subprocess, datetime, shutil, requests

ROOT = pathlib.Path(__file__).resolve().parent
OUT  = pathlib.Path(os.getenv("OUT_DIR", "./out")).resolve()
PUBLIC = (ROOT / "public").resolve()

PY = sys.executable

# Cloudflare vars
CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID", "").strip()
CF_API_TOKEN  = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
CF_PAGES_PROJECT = os.getenv("CF_PAGES_PROJECT", "tts-podcast-feeds").strip()
CF_KV_NAMESPACE_ID = os.getenv("CF_KV_NAMESPACE_ID", "").strip()
CF_KV_NAMESPACE_NAME = os.getenv("CF_KV_NAMESPACE_NAME", "tts-podcast-state").strip()

SLUG = os.getenv("PODCAST_SLUG", "default").strip()
RSS_URL = os.getenv("RSS_URL", "").strip()

def sh(*args, env=None):
    print("→", " ".join(map(str, args)))
    try:
        out = subprocess.check_output(
            list(map(str, args)),
            text=True,
            stderr=subprocess.STDOUT,
            env=env,
        )
        print(out.strip())
        return out
    except subprocess.CalledProcessError as e:
        print(e.output.strip())
        raise

# ---------- Cloudflare KV helpers ----------
def _kv_base():
    if not (CF_ACCOUNT_ID and CF_API_TOKEN):
        raise SystemExit("Missing CLOUDFLARE_API_TOKEN or CF_ACCOUNT_ID")
    return f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces"

def ensure_kv_namespace_id():
    global CF_KV_NAMESPACE_ID
    if CF_KV_NAMESPACE_ID:
        return CF_KV_NAMESPACE_ID
    headers = {"Authorization": f"Bearer {CF_API_TOKEN}"}
    # list
    r = requests.get(_kv_base(), headers=headers, timeout=15)
    if r.ok:
        for ns in r.json().get("result", []):
            if ns.get("title") == CF_KV_NAMESPACE_NAME:
                CF_KV_NAMESPACE_ID = ns["id"]
                print(f"[cf] Using existing KV '{CF_KV_NAMESPACE_NAME}' id={CF_KV_NAMESPACE_ID}")
                return CF_KV_NAMESPACE_ID
    # create
    r = requests.post(_kv_base(), headers=headers, json={"title": CF_KV_NAMESPACE_NAME}, timeout=15)
    if not r.ok:
        raise SystemExit(f"Failed to create KV namespace: {r.status_code} {r.text[:200]}")
    CF_KV_NAMESPACE_ID = r.json()["result"]["id"]
    print(f"[cf] Created KV '{CF_KV_NAMESPACE_NAME}' id={CF_KV_NAMESPACE_ID}")
    return CF_KV_NAMESPACE_ID

def kv_url(key): return f"{_kv_base()}/{ensure_kv_namespace_id()}/values/{key}"

def kv_get(key):
    try:
        r = requests.get(kv_url(key), headers={"Authorization": f"Bearer {CF_API_TOKEN}"}, timeout=15)
        if r.status_code == 200:
            return json.loads(r.text) if r.text else {}
        if r.status_code == 404:
            return None
        print(f"[kv] GET {key} -> {r.status_code}")
        return None
    except Exception as e:
        print(f"[kv] GET error: {e}")
        return None

def kv_put(key, data: dict) -> bool:
    try:
        r = requests.put(
            kv_url(key),
            headers={"Authorization": f"Bearer {CF_API_TOKEN}", "Content-Type": "application/json"},
            data=json.dumps(data, ensure_ascii=False),
            timeout=15,
        )
        if r.status_code in (200, 204):
            return True
        print(f"[kv] PUT {key} -> {r.status_code} {r.text[:200]}")
        return False
    except Exception as e:
        print(f"[kv] PUT error: {e}")
        return False

# ---------- IA helpers ----------
def link_hash(link: str) -> str:
    return hashlib.sha1(link.encode("utf-8")).hexdigest()

def ia_identifier_for_link(link: str) -> str:
    return f"tts-{SLUG}-{link_hash(link)[:16]}"

def ia_has_episode_http(identifier: str) -> bool:
    url = f"https://archive.org/download/{identifier}/episode.mp3"
    try:
        r = requests.head(url, allow_redirects=True, timeout=10)
        return r.status_code == 200
    except Exception:
        return False

# ---------- RSS fetch helpers ----------
def _entry_from_feed(e):
    link = getattr(e, "link", None) or getattr(e, "id", None)
    title = getattr(e, "title", link)
    summary = getattr(e, "summary", "") or getattr(e, "description", "") or ""
    tstruct = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
    if tstruct:
        pub_utc = datetime.datetime(*tstruct[:6], tzinfo=datetime.timezone.utc).isoformat()
    else:
        pub_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    author = getattr(e, "author", "") or getattr(e, "creator", "") or ""
    return {
        "article_title": title,
        "article_summary": summary,
        "article_link": link,
        "article_author": author,
        "article_pub_utc": pub_utc,
    }


def fetch_entries_from_rss(limit=None):
    import feedparser

    p = feedparser.parse(RSS_URL)
    if not p.entries:
        raise SystemExit("RSS has no entries")
    entries = [_entry_from_feed(e) for e in p.entries]
    entries.sort(key=lambda ent: ent["article_pub_utc"])
    if limit is not None:
        entries = entries[-limit:]
    return entries


def update_latest_state_snapshot(state: dict):
    """Maintain legacy top-level keys for backward compatibility."""
    items = state.get("items", {}) or {}
    latest = None
    for data in items.values():
        pub = data.get("last_pub_utc")
        if not pub:
            continue
        if not latest or pub > latest.get("last_pub_utc", ""):
            latest = data
    if latest:
        state["last_pub_utc"] = latest.get("last_pub_utc")
        state["rss_added"] = latest.get("rss_added")
        state["uploaded_url"] = latest.get("uploaded_url")

def newest_sidecar():
    sc = sorted(OUT.glob("*.mp3.rssmeta.json"), key=os.path.getmtime, reverse=True)
    return str(sc[0]) if sc else None

def main():
    if not RSS_URL:
        raise SystemExit("Missing RSS_URL")

    ensure_kv_namespace_id()
    state_key = f"feed:{SLUG}"
    state = kv_get(state_key) or {}
    items = state.setdefault("items", {})

    entries = fetch_entries_from_rss()
    if not entries:
        raise SystemExit("RSS has no entries")

    feed_xml = os.getenv(
        "FEED_PATH",
        str(PUBLIC / (os.getenv("PODCAST_FILE", f"feeds/{SLUG}.xml"))),
    )

    gcp_ready = False
    feed_updated = False
    processed = False

    for entry in entries:
        link = entry.get("article_link")
        if not link:
            print(f"[skip] Entry missing link: {entry['article_title']}")
            continue

        identifier = ia_identifier_for_link(link)
        entry_state = items.get(identifier)
        if not entry_state:
            entry_state = {}
            legacy_pub = state.get("last_pub_utc")
            if legacy_pub and legacy_pub == entry["article_pub_utc"]:
                entry_state.update(
                    {
                        "last_pub_utc": legacy_pub,
                        "rss_added": state.get("rss_added", False),
                        "uploaded_url": state.get("uploaded_url"),
                    }
                )
            items[identifier] = entry_state

        entry_state.setdefault("article_title", entry["article_title"])
        entry_state.setdefault("article_link", link)
        entry_state.setdefault("article_pub_utc", entry["article_pub_utc"])

        ia_present = ia_has_episode_http(identifier)
        last_pub = entry_state.get("last_pub_utc")
        already_in_feed = bool(entry_state.get("rss_added"))

        print("\n[entry]")
        print(f"  title: {entry['article_title']}")
        print(f"  link:  {link}")
        print(f"  id:    {identifier}")
        print(f"  pub:   {entry['article_pub_utc']}")
        print(f"  IA has audio: {ia_present}")
        print(f"  feed already updated: {already_in_feed}")

        if ia_present and last_pub == entry["article_pub_utc"] and already_in_feed:
            print("  → Skipping (already processed)")
            continue

        ia_url = f"https://archive.org/download/{identifier}/episode.mp3"

        # Case: audio exists but feed is missing the episode
        if ia_present and last_pub == entry["article_pub_utc"] and not already_in_feed:
            print("  → Restoring feed entry from existing audio")
            sidecar_path = (OUT / f"sidecar-{link_hash(link)}.json")
            OUT.mkdir(parents=True, exist_ok=True)
            payload = entry | {
                "mp3_local_path": "",
                "mp3_filename": "episode.mp3",
                "generated_il_iso": "",
            }
            with sidecar_path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            sh(PY, str(ROOT / "write_rss.py"), ia_url, str(sidecar_path))

            entry_state.update(
                {
                    "uploaded_url": ia_url,
                    "rss_added": True,
                    "last_pub_utc": entry["article_pub_utc"],
                    "article_pub_utc": entry["article_pub_utc"],
                }
            )
            update_latest_state_snapshot(state)
            kv_put(state_key, state)

            feed_updated = True
            processed = True
            print(f"  Feed updated -> {pathlib.Path(feed_xml).resolve()}")
            print(f"  Audio: {ia_url}")
            continue

        # Otherwise we need to synthesize + upload
        if not gcp_ready:
            sa = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
            if not pathlib.Path(sa).exists():
                raise SystemExit(f"Missing GCP SA key: {sa}")
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = sa
            os.environ.pop("IA_CONFIG_FILE", None)
            gcp_ready = True

        print("  → Generating audio")
        env = os.environ.copy()
        env["TARGET_ENTRY_LINK"] = link or ""
        out1 = sh(PY, str(ROOT / "one_episode.py"), env=env)

        sidecar_path = None
        for line in out1.splitlines()[::-1]:
            if line.startswith("Sidecar: "):
                sidecar_path = line.split("Sidecar: ", 1)[1].strip()
                break
        if not sidecar_path:
            sidecar_path = newest_sidecar()
            if not sidecar_path:
                raise SystemExit("No sidecar found after generation")

        with open(sidecar_path, "r", encoding="utf-8") as f:
            meta = json.load(f)

        print("  → Uploading to Internet Archive")
        out2 = sh(PY, str(ROOT / "upload_to_ia.py"), meta["mp3_local_path"])
        ia_url = None
        for line in out2.splitlines():
            if line.startswith("OK:"):
                ia_url = line.split(" ", 1)[1].strip()
        if not ia_url:
            raise SystemExit("Upload failed or no IA URL captured")

        mp3_path = pathlib.Path(meta["mp3_local_path"])
        if mp3_path.exists():
            try:
                mp3_path.unlink()
                print(f"  → Deleted local MP3: {mp3_path}")
            except Exception as e:
                print(f"  → Warning: could not delete MP3: {e}")

        print("  → Updating RSS feed")
        sh(PY, str(ROOT / "write_rss.py"), ia_url, sidecar_path)

        entry_state.update(
            {
                "uploaded_url": ia_url,
                "rss_added": True,
                "last_pub_utc": entry["article_pub_utc"],
                "article_pub_utc": entry["article_pub_utc"],
            }
        )
        update_latest_state_snapshot(state)
        kv_put(state_key, state)

        feed_updated = True
        processed = True
        print(f"  Feed updated -> {pathlib.Path(feed_xml).resolve()}")
        print(f"  Audio: {ia_url}")

    if not processed:
        print("No pending entries - everything up to date")

    if feed_updated:
        deploy_pages()

def deploy_pages():
    # Optional automatic deploy to Cloudflare Pages with Wrangler
    if CF_API_TOKEN and CF_ACCOUNT_ID and CF_PAGES_PROJECT:
        if shutil.which("wrangler"):
            try:
                sh("wrangler","pages","deploy","public","--project-name",CF_PAGES_PROJECT)
                print("Cloudflare Pages deploy OK")
            except Exception as e:
                print(f"Wrangler deploy failed: {e}")
        else:
            print("Wrangler not in PATH, skipping deploy")
    else:
        print("CF Pages env not set, skipping deploy")

if __name__ == "__main__":
    main()

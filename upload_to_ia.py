#!/usr/bin/env python
import os, sys, pathlib, hashlib, json
import internetarchive

def read_sidecar(mp3_path: pathlib.Path) -> dict:
    sidecar = mp3_path.with_suffix(mp3_path.suffix + ".rssmeta.json")
    if not sidecar.exists():
        sys.exit(f"Sidecar not found: {sidecar}")
    with open(sidecar, "r", encoding="utf-8") as f:
        return json.load(f)

def link_id(link: str) -> str:
    slug = os.getenv("PODCAST_SLUG", "default")
    h = hashlib.sha1(link.encode("utf-8")).hexdigest()[:16]
    return f"tts-{slug}-{h}"

def get_ia_session():
    os.environ.pop("IA_CONFIG_FILE", None)
    ak = os.getenv("IA_ACCESS_KEY")
    sk = os.getenv("IA_SECRET_KEY")
    if ak and sk:
        print("Using IA credentials from env vars")
        return internetarchive.get_session(config={"s3": {"access": ak, "secret": sk}}, config_file="")
    print("Using default IA config from home dir")
    return internetarchive.get_session(config_file="")

def main():
    if len(sys.argv) < 2:
        print("Usage: python upload_to_ia.py path/to/file.mp3")
        sys.exit(2)

    mp3_path = pathlib.Path(sys.argv[1]).resolve()
    if not mp3_path.is_file():
        sys.exit(f"Not found: {mp3_path}")

    meta = read_sidecar(mp3_path)
    identifier = link_id(meta["article_link"])
    remote_name = "episode.mp3"

    session = get_ia_session()
    item = session.get_item(identifier)

    try:
        replacing = any(f.name == remote_name for f in item.get_files())
    except Exception:
        replacing = False
    print(f"{'Replacing' if replacing else 'Creating'} {remote_name} in {identifier}\n")

    to_upload = {remote_name: str(mp3_path)}
    result = item.upload(to_upload, metadata={
        "title": meta["article_title"],
        "mediatype": "audio",
        "language": "und",
        "creator": "Automated RSS to TTS",
        "description": "Auto-generated TTS episode",
        "subject": "podcast;tts;articles",
        "external-identifier": meta.get("article_link",""),
    }, verbose=True)

    ok = all(getattr(r, "ok", False) for r in result)
    if not ok:
        print("Upload failed:")
        for r in result:
            if not getattr(r, "ok", False):
                print(r)
        sys.exit(1)

    url = f"https://archive.org/download/{identifier}/{remote_name}"
    print("OK:", url)

if __name__ == "__main__":
    main()

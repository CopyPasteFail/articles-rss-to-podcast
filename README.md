# RSS → TTS Podcast Pipeline

This project automatically converts articles from an RSS feed into spoken audio (MP3), uploads them to [Internet Archive](https://archive.org/), and publishes a podcast RSS feed via [Cloudflare Pages](https://pages.cloudflare.com/).

- ✅ Runs locally (Linux, macOS, or WSL on Windows)  
- ✅ Stores state in **Cloudflare KV** (no local DB needed)  
- ✅ Supports **multiple RSS → podcast feeds** (each with its own config)  
- ✅ One Cloudflare Pages site serves all feeds at `/feeds/<slug>.xml`  
- ✅ Fully portable — all config in `.env` files  

---

## 1. Prerequisites

Before starting, make sure you have:

1. **Google Cloud Platform (GCP)**
   - A billing-enabled GCP account.
   - Access to the **Text-to-Speech API**.
   - A **service account JSON key** with the role `roles/texttospeech.admin`.

2. **Cloudflare**
   - A Cloudflare account.
   - A Pages project created, e.g. `tts-podcast-feeds`.
   - Permissions to create KV namespaces.

3. **Local environment**
   - Python 3.11 (Linux/macOS/WSL).
   - [ffmpeg](https://ffmpeg.org/download.html) installed (needed by pydub).
   - [Wrangler](https://developers.cloudflare.com/workers/wrangler/install-and-update/) installed for Cloudflare Pages deploys.

---

## 2. One-time setup

### 2.1 Clone and prepare

```bash
git clone <this-repo-url> rss-to-tts
cd rss-to-tts

# create venv
python3.11 -m venv .venv
source .venv/bin/activate

# upgrade pip and install requirements
pip install --upgrade pip
pip install -r requirements.txt
```

### 2.2 Google Cloud Setup

1. Create a **new GCP project** (recommended, e.g. `rss-tts-podcasts`).  
2. Enable the **Text-to-Speech API**:  
   ```bash
   gcloud services enable texttospeech.googleapis.com
   ```
3. Create a **service account**:
   ```bash
   gcloud iam service-accounts create tts-runner --display-name "TTS Runner"
   ```
4. Bind the role:
   ```bash
   gcloud projects add-iam-policy-binding <PROJECT_ID> \
     --member="serviceAccount:tts-runner@<PROJECT_ID>.iam.gserviceaccount.com" \
     --role="roles/texttospeech.admin"
   ```
5. Generate and download the JSON key:
   ```bash
   gcloud iam service-accounts keys create ./tts-sa.json \
     --iam-account=tts-runner@<PROJECT_ID>.iam.gserviceaccount.com
   ```

---

### 2.3 Cloudflare Setup

1. **Create a Pages project**
   - In the Cloudflare dashboard → Pages → Create Project.
   - Name it `tts-podcast-feeds`.
   - Connect it to this repo **or** deploy manually with Wrangler (see below).

2. **Add `_headers` file**
   - Create `public/_headers` with:
     ```
     /feeds/*.xml
       Content-Type: application/rss+xml; charset=utf-8
       Cache-Control: public, max-age=300
     ```

   This ensures podcast clients recognize feeds correctly.

3. **Create a KV namespace**
   - Dashboard → Workers & Pages → KV → Create Namespace.
   - Name it `tts-podcast-state`.

4. **Create one API token**
   - Dashboard → My Profile → API Tokens → Create Custom Token.
   - Permissions:
     - Account → *Cloudflare Pages:Edit*
     - Account → *Workers KV Storage:Edit*
   - Scope to your account.
   - Copy the token.

---

### 2.4 Project configuration

Create a root `.env`:

```ini
# Cloudflare
CLOUDFLARE_API_TOKEN=your_token_here
CLOUDFLARE_ACCOUNT_ID=your_account_id_here
CF_PAGES_PROJECT=tts-podcast-feeds
CF_KV_NAMESPACE_NAME=tts-podcast-state

# Google TTS
GOOGLE_APPLICATION_CREDENTIALS=./tts-sa.json
```

---

## 3. Adding your first feed

Each feed has its own `.env` under `configs/`.

Example: `configs/geektime.env`

```ini
# Source RSS
RSS_URL=https://www.geektime.co.il/feed/

# Branding
PODCAST_SLUG=geektime
PODCAST_TITLE=Geektime TTS
PODCAST_AUTHOR=Omer
PODCAST_DESCRIPTION=Automated TTS of Geektime articles
PODCAST_SITE=https://www.geektime.co.il/

# Feed file and public URL
PODCAST_FILE=feeds/geektime.xml
FEED_URL=https://tts-podcast-feeds.pages.dev/feeds/geektime.xml

# Voice settings
GCP_TTS_VOICE=he-IL-Wavenet-A
GCP_TTS_LANG=he-IL
GCP_TTS_RATE=1.02
GCP_TTS_PITCH=0.0
```

---

## 4. Running the pipeline

Generate & publish one feed:

```bash
python run_feed.py geektime
```

### What happens:
1. Fetch latest article from the RSS.  
2. Check Cloudflare KV to see if it’s already processed.  
3. If new → synthesize MP3 via GCP TTS.  
4. Upload to Internet Archive (IA).  
5. Update `public/feeds/geektime.xml`.  
6. Deploy to Cloudflare Pages.  
7. Update KV with last published article state.  

> Each run processes only the new or changed RSS entries detected since the previous run. Set `PODCAST_FULL_RESCAN=1` to force a full feed rescan if you need to rebuild everything.

---

## 5. Subscribing

Podcast feed URL:  
```
https://tts-podcast-feeds.pages.dev/feeds/geektime.xml
```

Paste this into any podcast app (Apple Podcasts, Pocket Casts, Overcast, etc.).

---

## 6. Adding more feeds

1. Copy `configs/geektime.env` → `configs/<slug>.env`.  
2. Edit `RSS_URL`, `PODCAST_SLUG`, `PODCAST_FILE`, and `FEED_URL`.  
3. Run:
   ```bash
   python run_feed.py <slug>
   ```
4. New podcast feed will appear at:  
   ```
   https://tts-podcast-feeds.pages.dev/feeds/<slug>.xml
   ```

---

## 7. Automation

### Windows Task Scheduler (using CMD to call the script via WSL)
Create a task that runs daily:

```
wsl.exe bash -lic "cd /path/to/repo && source .venv/bin/activate && python run_feed.py geektime"
```

### Linux/macOS cron
```cron
0 8 * * * cd /path/to/repo && source .venv/bin/activate && python run_feed.py geektime
```

> Replace `/path/to/repo` with the repository path inside WSL (e.g. `~/repos/rss-to-tts`).

---

## 8. Requirements

See `requirements.txt`:

```
python-dotenv
feedparser
requests
feedgen
google-cloud-texttospeech
pydub
internetarchive
PyYAML
```

System dependencies:
- `ffmpeg` (for MP3 normalization).
- `wrangler` (for deploying to Cloudflare Pages).

---

## 9. Troubleshooting

- **Error: Empty language code**  
  → Make sure `GCP_TTS_LANG` is set in the feed `.env`.

- **Podcast feed not recognized by player**  
  → Check `public/_headers` exists and has the correct `Content-Type`.

- **Wrangler warns about CF_ACCOUNT_ID**  
  → Use `CLOUDFLARE_ACCOUNT_ID` instead.

- **Runs but reprocesses same article**  
  → Ensure KV is configured properly in `.env` and accessible.

---

✅ That’s it. You now have a self-updating podcast feed from any RSS source.  

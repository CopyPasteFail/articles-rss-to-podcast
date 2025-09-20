# RSS -> TTS Podcast on Cloudflare Pages

Create a podcast from any article RSS feed. This repo fetches new items from a source RSS, cleans the text, synthesizes audio with Google Cloud Text-to-Speech (TTS), uploads the generated MP3 files to Internet Archive, then writes a podcast-ready RSS that is served from Cloudflare Pages.

---

## Contents

- Quick start
- How it works
- One-time setup: GCP, Internet Archive, Cloudflare Pages and KV
- Project configuration
- Run a feed
- Automation (cron and Windows Task Scheduler)
- Caching strategy and "instant update" via purge
- Free tier limits and pricing links
- Troubleshooting
- FAQ
- Security notes

---

## Quick start

```bash
# 1) Clone and prepare Python
git clone https://github.com/CopyPasteFail/articles-rss-to-podcast.git
cd articles-rss-to-podcast
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

# 2) Copy the sample environment file and fill in secrets
cp .env.example .env
$EDITOR .env    # set GOOGLE_APPLICATION_CREDENTIALS, CLOUDFLARE_*, IA_* etc.

# 3) Create configs/<your-feed>.env (see example below)

# 4) Run it
python run_feed.py geektime

# 5) Deploy the generated public/ folder to Cloudflare Pages
#    Direct Upload via Wrangler
npm i -g wrangler
wrangler login
wrangler pages project create tts-podcast-feeds
wrangler pages deploy public --project-name tts-podcast-feeds
```

The resulting RSS is served under `https://<your-pages-domain>/feeds/<slug>.xml`.

---

## How it works

1. Read the source RSS.
2. Determine new items using a small state cursor stored in Cloudflare Workers KV.
3. Fetch and sanitize article content.
4. Synthesize speech with GCP TTS and save MP3.
5. Upload MP3 and metadata to Internet Archive.
6. Generate a podcast RSS XML into `public/feeds/<slug>.xml`.
7. Deploy `public/` to Cloudflare Pages.
8. Optionally purge the single feed URL so clients see the update immediately.

Folder structure:

```
public/
  feeds/
    <slug>.xml         # generated podcast feed(s)
configs/
  <slug>.env           # one file per feed
content_utils.py       # HTML -> clean text helpers
one_episode.py         # build a single episode from an article
write_rss.py           # write or update RSS XML
upload_to_ia.py        # Internet Archive uploads
pipeline.py            # orchestrates the end-to-end flow
run_feed.py            # CLI entry point: python run_feed.py <slug>
```

---

## One-time setup

All commands below assume Ubuntu (also compatible with WSL). Copy/paste the snippets and replace the ALL_CAPS placeholders with your own IDs. Keep a terminal log so you can roll back or audit later.

### A) Install command-line tools (run once per machine)

```bash
# Update base packages
sudo apt-get update
sudo apt-get install -y python3.11 python3.11-venv zip ffmpeg

# Install the Google Cloud CLI (Ubuntu 18.04+)
sudo apt-get install -y apt-transport-https ca-certificates gnupg curl
curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | sudo gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg
echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" | \
  sudo tee /etc/apt/sources.list.d/google-cloud-sdk.list > /dev/null
sudo apt-get update
sudo apt-get install -y google-cloud-cli
gcloud --version

# Authenticate once (opens browser for OAuth)
gcloud auth login
# When running in WSL use --no-browser:
gcloud auth login --no-browser

# Install Node.js tooling for Wrangler (system Node is fine for automation)
sudo apt-get install -y nodejs npm
npm install -g wrangler

# Install the Internet Archive CLI helper (lives in your Python venv or global site-packages)
pip install --upgrade internetarchive
```

> Prefer `nvm`? Install NVM first, then run `nvm install --lts` before `npm install -g wrangler`.

### B) Google Cloud Text-to-Speech project

The pipeline uses a dedicated project, service account, and key. Below is a templated walkthrough. If you already have a billing account in another Google Cloud project you can reuse it; otherwise create one via the console here: https://console.cloud.google.com/billing/create.

#### B.1 Create the project shell

```bash
export PROJECT_ID="articles-rss-to-podcast-$(whoami)"   # must be globally unique
export PROJECT_NAME="Articles RSS to Podcast"

gcloud projects create "$PROJECT_ID" --name="$PROJECT_NAME"
```

#### B.2 Link a billing account

If you already have a billing account attached to another Google Cloud project, reuse its ID (format `NNNNNN-XXXXXX-NNNNNN`). List the accounts you can access:

```bash
gcloud beta billing accounts list
```

If you don't have a billing account yet, create one in the console first: https://console.cloud.google.com/billing/create. Once you know the ID:

```bash
export BILLING_ACCOUNT="XXXXXX-XXXXXX-XXXXXX"
gcloud beta billing projects link "$PROJECT_ID" --billing-account="$BILLING_ACCOUNT"
```

#### B.3 Set the project and enable APIs

```bash
gcloud config set project "$PROJECT_ID"
gcloud services enable texttospeech.googleapis.com
```

#### B.4 Create the service account and key

```bash
export SA_ID="tts-runner"
export SA_EMAIL="${SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
gcloud iam service-accounts create "$SA_ID" --display-name="TTS Runner"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/texttospeech.user"
gcloud iam service-accounts keys create ./tts-key.json \
  --iam-account="$SA_EMAIL"
```

Update your `.env` (copied from `.env.example`) so `GOOGLE_APPLICATION_CREDENTIALS` targets `./tts-key.json`.


### C) Create your .env from the template

Copy the example file and edit it with the credentials you collected above:

```bash
cp .env.example .env
$EDITOR .env
```

Helpful references:
- Product overview: https://cloud.google.com/text-to-speech
- Pricing and free tier: https://cloud.google.com/text-to-speech/pricing
- Quotas: https://cloud.google.com/text-to-speech/quotas
- Billing alerts: https://cloud.google.com/billing/docs/how-to/budgets

### D) Internet Archive credentials (audio storage)

1. Create a free account: https://archive.org/account/login
2. Generate S3-style keys: https://archive.org/account/s3.php
3. Add the keys to your `.env` file (`IA_ACCESS_KEY` and `IA_SECRET_KEY`). Optional: run `ia configure` so the CLI can upload from your shell:

   ```bash
   ia configure
   IA_ACCESS_KEY=YOUR_ACCESS_KEY
   IA_SECRET_KEY=YOUR_SECRET_KEY
   ```

4. Optional sanity check:

   ```bash
   IA_CONFIG_FILE=~/.config/ia.ini ia whoami
   ```

Documentation:
- Quickstart: https://archive.org/developers/internetarchive/quickstart.html
- IA-S3 API details: https://archive.org/developers/ias3.html

### E) Cloudflare Pages + Workers KV

1. Sign up / log in: https://dash.cloudflare.com/

2. Authenticate Wrangler and create a Pages project (Direct Upload flow):

   ```bash
   wrangler login
   wrangler whoami              # prints your Account ID for reference
   wrangler pages project create tts-podcast-feeds
   ```

3. Create a Workers KV namespace to store feed state:

   ```bash
   wrangler kv:namespace create "tts-podcast-state"
   # copy the returned ID into CF_KV_NAMESPACE_ID inside .env
   ```

4. Create a scoped API token (Cloudflare dashboard → My Profile → API Tokens) and copy it to `.env` as `CLOUDFLARE_API_TOKEN`.

   - Template: Custom Token
   - Permissions: Pages · Edit, Workers KV Storage · Edit
   - Scope: account-wide

5. Copy your Cloudflare Account ID into `.env` (shown in the dashboard or via `wrangler whoami`).

6. (Optional) Configure cache purge if you use a custom domain: note your Zone ID from the dashboard.

7. Add feed-friendly headers in `public/_headers` so Pages serves real RSS:

   ```
   /feeds/*.xml
     Content-Type: application/rss+xml; charset=utf-8
     Cache-Control: public, max-age=300
   ```

Reference material:
- Direct Upload docs: https://developers.cloudflare.com/pages/get-started/direct-upload/
- Pages limits: https://developers.cloudflare.com/pages/platform/limits/
- KV limits & pricing: https://developers.cloudflare.com/kv/platform/limits/
- Find Account/Zone IDs: https://developers.cloudflare.com/fundamentals/account/find-account-and-zone-ids/

---

## Project configuration

Create a root `.env` file with secrets and shared settings:

```bash
# Cloudflare
CLOUDFLARE_API_TOKEN=your_cf_api_token
CLOUDFLARE_ACCOUNT_ID=your_cf_account_id
CF_PAGES_PROJECT=tts-podcast-feeds
CF_KV_NAMESPACE_NAME=tts-podcast-state

# Google Cloud
GOOGLE_APPLICATION_CREDENTIALS=./tts-sa.json
```

Create one config per feed in `configs/`. Example `configs/geektime.env`:

```bash
# Source RSS
RSS_URL=https://www.geektime.co.il/feed/

# Branding
PODCAST_SLUG=geektime
PODCAST_TITLE=Geektime TTS
PODCAST_AUTHOR=Omer
PODCAST_DESCRIPTION=Automated TTS of Geektime articles
PODCAST_SITE=https://www.geektime.co.il/

# Output feed file and public URL
PODCAST_FILE=feeds/geektime.xml
FEED_URL=https://tts-podcast-feeds.pages.dev/feeds/geektime.xml

# Voice settings
GCP_TTS_VOICE=he-IL-Wavenet-A
GCP_TTS_LANG=he-IL
GCP_TTS_RATE=1.02
GCP_TTS_PITCH=0.0
```

Add more feeds by adding more files in `configs/` and calling `python run_feed.py <slug>` for each.

---

## Run a feed

```bash
# activate env then
python run_feed.py geektime
```

What happens:

- Pulls items from `RSS_URL` and checks KV for already published entries.
- For each new item: extract and clean text, synthesize MP3 via GCP TTS, upload to Internet Archive.
- Writes or updates `public/feeds/<slug>.xml`.
- You deploy the `public/` folder to Cloudflare Pages:
  ```bash
  wrangler pages deploy public --project-name tts-podcast-feeds
  ```
- Optional purge so clients see it immediately:
  ```bash
  curl -X POST \
    "https://api.cloudflare.com/client/v4/zones/<ZONE_ID>/purge_cache" \
    -H "Authorization: Bearer <CLOUDFLARE_API_TOKEN>" \
    -H "Content-Type: application/json" \
    --data '{"files":["https://YOUR_DOMAIN/feeds/geektime.xml"]}'
  ```

API reference: https://developers.cloudflare.com/api/resources/cache/methods/purge/

---

## Automation

**cron** example every 15 minutes:

```cron
*/15 * * * * cd /opt/rss-to-tts && \
  source .venv/bin/activate && \
  python run_feed.py geektime >> logs/geektime.log 2>&1 && \
  wrangler pages deploy public --project-name tts-podcast-feeds
```

**Windows Task Scheduler** via WSL:

```bat
wsl.exe bash -lc "cd /mnt/c/Users/<you>/articles-rss-to-podcast && \
source .venv/bin/activate && \
python run_feed.py geektime && \
wrangler pages deploy public --project-name tts-podcast-feeds"
```

---

## Caching and "instant update"

- Feeds are served with `Content-Type: application/rss+xml; charset=utf-8` and `Cache-Control: public, max-age=300`.
- Five minutes is a good default. It reduces origin traffic and is usually faster worldwide.
- If you need immediate visibility after a publish, purge the single feed URL as shown above. This keeps cache benefits for everyone else.
- More on purge methods: https://developers.cloudflare.com/cache/how-to/purge-cache/

---

## Free tier limits and pricing links

These change over time. Always check the official pages.

- **Google Cloud Text-to-Speech**

  - Pricing and free tier: https://cloud.google.com/text-to-speech/pricing
  - Quotas: https://cloud.google.com/text-to-speech/quotas
  - Product page: https://cloud.google.com/text-to-speech

- **Cloudflare Pages**

  - Direct Upload guide: https://developers.cloudflare.com/pages/get-started/direct-upload/
  - Pages limits (file count and per-file size): https://developers.cloudflare.com/pages/platform/limits/

- **Cloudflare Workers KV**

  - Limits: https://developers.cloudflare.com/kv/platform/limits/
  - Pricing: https://developers.cloudflare.com/kv/platform/pricing/

- **Cloudflare Cache purge**

  - Purge by single file: https://developers.cloudflare.com/cache/how-to/purge-cache/purge-by-single-file/

- **Internet Archive**

  - IA-S3 API: https://archive.org/developers/ias3.html
  - Python library: https://archive.org/developers/internetarchive/quickstart.html

---

## Troubleshooting

- TTS fails or returns permission errors: confirm billing is enabled on the GCP project and the service account has a TTS role. Verify `GOOGLE_APPLICATION_CREDENTIALS` path.
- MP3 not uploaded: run `ia configure` again and test `ia upload test-item ./README.md`.
- Pages deploy errors: run `wrangler login` and confirm `CF_PAGES_PROJECT`. Try `wrangler pages project list`.
- KV writes fail: reissue the API token with Workers KV Storage Edit. Confirm the namespace name matches `CF_KV_NAMESPACE_NAME`.
- Podcast apps do not show new episodes: purge the feed URL and remember that apps poll on their own schedules.

---

## FAQ

**Why Internet Archive for audio?**  Stable, free public hosting with a permanent URL. If you prefer another host, swap out `upload_to_ia.py`.

**Can I serve audio from Pages?**  Small files are fine, but Pages has a 25 MiB per-asset limit on the Free plan. Use R2 or IA for larger files.

**Do Direct Upload deployments count as builds?**  No. Direct Upload creates a deployment without consuming the Git build quota. See the Direct Upload docs.

**How do I support multiple feeds?**  Create multiple files in `configs/`, then schedule `python run_feed.py <slug>` per feed.

---

## Security notes

- Do not commit secrets. Add `.env` and `tts-sa.json` to `.gitignore`.
- Use a narrow Cloudflare API token. Do not use the Global API Key unless required.
- Rotate tokens and IA keys on a schedule.

# RSS -> TTS Podcast

This repo turns an article RSS feed into a podcast feed:

1. Read source RSS entries.
2. Generate MP3 audio with Google Cloud Text-to-Speech.
3. Upload episode audio to Internet Archive.
4. Write podcast RSS XML into `public/feeds/<slug>.xml`.
5. Deploy `public/` to Cloudflare Pages.

The runtime entrypoint is still `python run_feed.py <feed_slug>`.

## Architectures

### Public GitHub Actions architecture

The public-safe GitHub Actions model is:

- one repo
- one workflow file per pipeline under `.github/workflows/`
- one GitHub environment per pipeline
- scheduled workflows run from the default branch
- no reusable workflows
- no per-pipeline branch
- no per-pipeline repo

Shared Google values live once at the repository level as GitHub repository variables:

- `GCP_PROJECT_ID`
- `GCP_PROJECT_NUMBER`
- `GCP_WIF_POOL_ID`
- `GCP_WIF_PROVIDER_ID`

Each pipeline gets its own GitHub environment, for example `geektime-he` or `eu-startups-en`.

Each environment contains variables:

- `GCP_SERVICE_ACCOUNT_EMAIL`
- `CF_PAGES_PROJECT`
- `CF_KV_NAMESPACE_ID`

Each environment contains secrets:

- `CLOUDFLARE_API_TOKEN`
- `IA_ACCESS_KEY`
- `IA_SECRET_KEY`

If failure email is enabled later, its SMTP or API credentials should also be environment secrets.

Generated workflow files are safe to commit because they reference only GitHub vars and secrets:

- `${{ vars.GCP_PROJECT_ID }}`
- `${{ vars.GCP_PROJECT_NUMBER }}`
- `${{ vars.GCP_WIF_POOL_ID }}`
- `${{ vars.GCP_WIF_PROVIDER_ID }}`
- `${{ vars.GCP_SERVICE_ACCOUNT_EMAIL }}`
- `${{ vars.CF_PAGES_PROJECT }}`
- `${{ vars.CF_KV_NAMESPACE_ID }}`
- `${{ secrets.CLOUDFLARE_API_TOKEN }}`
- `${{ secrets.IA_ACCESS_KEY }}`
- `${{ secrets.IA_SECRET_KEY }}`

### Local architecture

Local mode is still supported and unchanged in spirit:

- root `.env` for local Cloudflare and Internet Archive values
- local Google credentials file referenced by `GOOGLE_APPLICATION_CREDENTIALS`
- per-feed env file under `configs/<feed>.env`
- optional local-only pipeline config overlays under `pipelines/*.local.yaml`
- optional local-only shared Google setup config at `pipelines/shared.yaml`

`pipelines/shared.yaml` is local-only and ignored. It is for setup and preflight tooling that talks to GCP directly through `gcloud`. It is not needed by committed GitHub workflow files.

See [docs/github-actions-oidc.md](/home/omer/repos/articles-rss-to-podcast/docs/github-actions-oidc.md) for the detailed GitHub setup flow.

## Pipeline Configs

Committed pipeline configs are intentionally public-safe and contain only:

- the pipeline id
- the feed slug passed to `run_feed.py`
- the per-feed env file
- the workflow file path
- the branch ref
- the GitHub environment name
- the schedule
- optional failure email routing metadata

Examples:

- [pipelines/geektime-he.yaml](/home/omer/repos/articles-rss-to-podcast/pipelines/geektime-he.yaml)
- [pipelines/example.yaml](/home/omer/repos/articles-rss-to-podcast/pipelines/example.yaml)
- [pipelines/shared.example.yaml](/home/omer/repos/articles-rss-to-podcast/pipelines/shared.example.yaml)

The loader also supports optional local-only overlays:

- `pipelines/<pipeline-id>.local.yaml`
- `pipelines/shared.local.yaml`

Those overlays are merged on top of the tracked file when present.

## Multiple Pipelines

The intended multi-pipeline model is:

- one shared GCP project
- one shared Workload Identity Pool
- one shared Workload Identity Provider
- one dedicated service account per pipeline
- one GitHub environment per pipeline
- one workflow file per pipeline
- one schedule per pipeline

Example workflow layout:

```text
.github/workflows/
  geektime-he.yml
  eu-startups-en.yml
  whatever-next.yml
```

Adding a second pipeline does not require a new branch or a new repository.

## Quick Start

### Local mode

1. Create a Python environment and install dependencies.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

2. Install the pinned Node dependency for Wrangler.

```bash
npm ci
```

3. Create local `.env`.

```bash
cp .env.example .env
```

4. Create or edit `configs/<feed>.env`.

5. If you want to use the GCP setup tooling locally, create local-only `pipelines/shared.yaml` from [pipelines/shared.example.yaml](/home/omer/repos/articles-rss-to-podcast/pipelines/shared.example.yaml).

6. Run local preflight.

```bash
python -m tools.preflight local --pipeline <pipeline-id>
```

7. Run the feed.

```bash
python run_feed.py <feed_slug>
```

### GitHub Actions mode

1. Create the shared repository variables in GitHub:

- `GCP_PROJECT_ID`
- `GCP_PROJECT_NUMBER`
- `GCP_WIF_POOL_ID`
- `GCP_WIF_PROVIDER_ID`

2. Create the pipeline GitHub environment and add:

- variables: `GCP_SERVICE_ACCOUNT_EMAIL`, `CF_PAGES_PROJECT`, `CF_KV_NAMESPACE_ID`
- secrets: `CLOUDFLARE_API_TOKEN`, `IA_ACCESS_KEY`, `IA_SECRET_KEY`

3. Authenticate the operator CLIs.

```bash
gh auth login
gcloud auth login
```

4. If you want to run the local GCP setup scripts, create local-only `pipelines/shared.yaml`.

5. Run GitHub-mode preflight.

```bash
python -m tools.preflight github --pipeline <pipeline-id>
```

6. Create or reconcile the shared GCP OIDC resources.

```bash
scripts/setup-gcp-oidc-shared.sh --pipeline <pipeline-id>
```

7. Create or reconcile the dedicated pipeline service account.

```bash
scripts/setup-gcp-pipeline-sa.sh --pipeline <pipeline-id>
```

8. Push the pipeline environment secrets from local `.env`.

```bash
scripts/push-gh-secrets.sh --pipeline <pipeline-id>
```

9. Generate exactly one workflow file.

```bash
python -m tools.generate_workflow --pipeline <pipeline-id>
```

10. Commit the pipeline config and generated workflow.

## Commands

### Preflight

```bash
python -m tools.preflight local --pipeline geektime-he
python -m tools.preflight github --pipeline geektime-he
python -m tools.preflight github --pipeline geektime-he --json
```

Exit codes:

- `0`: all checks passed
- `10`: at least one dependency or variable or secret is missing
- `11`: at least one item is misconfigured
- `64`: invalid invocation or invalid pipeline config

### GitHub secret sync

```bash
scripts/push-gh-secrets.sh --pipeline geektime-he
scripts/push-gh-secrets.sh --pipeline geektime-he --dry-run
```

Behavior:

- reads local `.env`
- reads the selected pipeline config
- uploads environment secrets to the selected GitHub environment
- never uploads Google key material
- prints secret names only, never secret values

### Shared Google OIDC setup

```bash
scripts/setup-gcp-oidc-shared.sh --pipeline geektime-he
```

Behavior:

- uses local-only shared Google config from `pipelines/shared.yaml`
- creates or reuses one Workload Identity Pool
- creates or updates one shared GitHub OIDC provider
- restricts provider admission to the exact GitHub repository

### Per-pipeline service account setup

```bash
scripts/setup-gcp-pipeline-sa.sh --pipeline geektime-he
```

Behavior:

- uses local-only shared Google config from `pipelines/shared.yaml`
- creates or reuses the pipeline service account
- grants only `roles/serviceusage.serviceUsageConsumer`
- grants `roles/iam.workloadIdentityUser` to the exact workflow file and branch

### Workflow generation

```bash
python -m tools.generate_workflow --pipeline geektime-he
scripts/generate-workflow.py --pipeline geektime-he
```

Behavior:

- loads exactly one selected pipeline config
- sets the GitHub environment on the job
- authenticates to Google with OIDC through `google-github-actions/auth`
- reads shared Google values from repository variables
- reads pipeline-specific values from environment variables and secrets
- generates one public-safe workflow file

## Notes

- Local mode does not require GitHub Actions setup.
- GitHub mode does not store a long-lived Google JSON key in GitHub.
- The committed workflows are safe to keep public because they contain only GitHub expressions, not real values.
- `pipelines/shared.yaml` is intentionally ignored because it can hold real local GCP metadata for setup tooling.

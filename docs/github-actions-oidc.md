# GitHub Actions OIDC Setup

This repo uses a public-safe GitHub Actions model for multiple pipelines:

- one repository
- one workflow file per pipeline
- one GitHub environment per pipeline
- no reusable workflows
- scheduled workflows continue to run from the default branch

## Public GitHub model

Repository-level GitHub variables are shared once for all pipelines:

- `GCP_PROJECT_ID`
- `GCP_PROJECT_NUMBER`
- `GCP_WIF_POOL_ID`
- `GCP_WIF_PROVIDER_ID`

Each pipeline gets one GitHub environment such as `geektime-he` or `eu-startups-en`.

Each environment contains variables:

- `GCP_SERVICE_ACCOUNT_EMAIL`
- `CF_PAGES_PROJECT`
- `CF_KV_NAMESPACE_ID`

Each environment contains secrets:

- `CLOUDFLARE_API_TOKEN`
- `IA_ACCESS_KEY`
- `IA_SECRET_KEY`

Optional failure email credentials should also be stored as environment secrets.

The generated workflow file is safe to commit because it contains only expressions such as:

```yaml
environment: geektime-he

with:
  project_id: ${{ vars.GCP_PROJECT_ID }}
  workload_identity_provider: projects/${{ vars.GCP_PROJECT_NUMBER }}/locations/global/workloadIdentityPools/${{ vars.GCP_WIF_POOL_ID }}/providers/${{ vars.GCP_WIF_PROVIDER_ID }}
  service_account: ${{ vars.GCP_SERVICE_ACCOUNT_EMAIL }}

env:
  CF_PAGES_PROJECT: ${{ vars.CF_PAGES_PROJECT }}
  CF_KV_NAMESPACE_ID: ${{ vars.CF_KV_NAMESPACE_ID }}
  CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}
  IA_ACCESS_KEY: ${{ secrets.IA_ACCESS_KEY }}
  IA_SECRET_KEY: ${{ secrets.IA_SECRET_KEY }}
```

No real project ids, provider resource names, service account emails, Cloudflare identifiers, or Internet Archive credentials need to be committed.

## Local setup model

Local runs remain supported with:

- root `.env`
- local Google credentials file through `GOOGLE_APPLICATION_CREDENTIALS`
- per-feed env files under `configs/`
- optional local-only pipeline overlays under `pipelines/*.local.yaml`

If you want to run the setup scripts that talk to GCP directly, keep the real shared Google config locally in ignored `pipelines/shared.yaml`.

Start from [pipelines/shared.example.yaml](/home/omer/repos/articles-rss-to-podcast/pipelines/shared.example.yaml):

```yaml
google:
  project_id: my-gcp-project
  project_number: "123456789012"
  workload_identity_pool_id: github-actions
  workload_identity_provider_id: github-provider
```

That file is for local tooling only. It is not used by committed workflow files.

## Per-pipeline config

Tracked pipeline configs are public-safe. Example:

```yaml
pipeline_id: geektime-he
feed_slug: geektime
feed_env_file: configs/geektime.env

schedule:
  timezone: Asia/Jerusalem
  interval_hours: 2
  window_start: "07:00"
  window_end: "24:00"

github:
  workflow_file: .github/workflows/geektime-he.yml
  branch_ref: refs/heads/main
  environment: geektime-he
```

If local setup needs overrides, add a matching ignored local overlay:

```text
pipelines/geektime-he.local.yaml
```

Example local-only override:

```yaml
google:
  service_account_id: rss-podcast-geektime-he
```

If omitted, the tooling derives a default service account id from the pipeline id.

## Setup flow

1. Create the GitHub repository variables.
2. Create one GitHub environment for each pipeline.
3. Add the pipeline environment variables and secrets.
4. Authenticate locally:

```bash
gh auth login
gcloud auth login
```

5. Create local-only `pipelines/shared.yaml` if you want to run the GCP setup scripts.
6. Run preflight:

```bash
python -m tools.preflight github --pipeline geektime-he
```

7. Reconcile the shared OIDC pool and provider:

```bash
scripts/setup-gcp-oidc-shared.sh --pipeline geektime-he
```

8. Reconcile the dedicated pipeline service account:

```bash
scripts/setup-gcp-pipeline-sa.sh --pipeline geektime-he
```

9. Push the environment secrets from local `.env`:

```bash
scripts/push-gh-secrets.sh --pipeline geektime-he
```

10. Generate the workflow:

```bash
python -m tools.generate_workflow --pipeline geektime-he
```

## Preflight behavior

GitHub-mode preflight now checks:

- `gh` authentication
- repository context
- shared repository variables
- pipeline environment variables
- pipeline environment secrets
- workflow target path and schedule

If local `pipelines/shared.yaml` is present, it also checks the GCP pool, provider, project, and service account state through `gcloud`.

Local-mode preflight still checks:

- `.env`
- local Google credentials file
- local runtime env values
- Python and Node tooling

## Adding a second pipeline

To add `eu-startups-en`:

1. Create GitHub environment `eu-startups-en`.
2. Add environment variables:
   `GCP_SERVICE_ACCOUNT_EMAIL`, `CF_PAGES_PROJECT`, `CF_KV_NAMESPACE_ID`
3. Add environment secrets:
   `CLOUDFLARE_API_TOKEN`, `IA_ACCESS_KEY`, `IA_SECRET_KEY`
4. Add `pipelines/eu-startups-en.yaml`.
5. Generate `.github/workflows/eu-startups-en.yml`.

The repository-level Google variables stay unchanged. No new branch is needed. No new repository is needed.

## Security notes

- Keep `pipelines/shared.yaml` local-only.
- Keep `.env` local-only.
- Do not commit Google credential JSON files.
- Do not commit real workflow files during any temporary migration step if they still embed real values.
- Commit only public-safe workflow files that reference GitHub vars and secrets.

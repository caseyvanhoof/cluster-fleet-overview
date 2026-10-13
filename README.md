# Cluster Production Cluster Overview

Generate an organization level scorecard for Atlas production clusters. The script collects cluster data from the Atlas projects listed in `projects.txt`, scores each cluster, compares it with the previous report, and writes HTML, JSON, and Excel reports to `reports/`.

## Prerequisites

- Python 3.9 or newer
- MongoDB Atlas CLI (`atlas`) installed and available on your `PATH`
- Access to the Atlas projects and clusters listed in `projects.txt`

**Before running the script, run `atlas auth login` and complete the login flow using your service account user. You do not need separate API keys. The Atlas CLI must be authenticated and logged in; this is a required prerequisite for running the script.**

## Setup

From the repository directory, create and activate a virtual environment, then install the Python dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Ensure the required Atlas project IDs are in `projects.txt`, with one ID per line. Lines beginning with `#` are ignored.

## Run

First authenticate the Atlas CLI with your service account user. No separate API keys are needed:

```bash
atlas auth login
```

Then generate the report:

```bash
python generate_report.py
```

The script reads project IDs from `projects.txt` by default. To provide project IDs directly, use the `--project-ids` option:

```bash
python generate_report.py --project-ids <project-id-1>,<project-id-2>
```

By default, only clusters whose names start with `p-` are included. Use `--prefix` to change that filter, and `--help` to see all options:

```bash
python generate_report.py --prefix p-
python generate_report.py --help
```

## AI summaries (optional)

AI generated comparison summaries are enabled by default when a previous report exists. The script also generates per-cluster AI analysis for RED clusters.

Supported providers are:

- `openai`
- `anthropic`
- `none`

Select a provider with `--ai-provider`, or set `AI_PROVIDER`. Use `--no-ai` to skip all AI summaries. The preferred environment variables are provider-neutral:

```bash
export AI_API_KEY=...
export AI_BASE_URL=...
export AI_PROVIDER=openai        # openai, anthropic, or none
export AI_MODEL=...
```

Provider-specific variables such as `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `ANTHROPIC_API_KEY`, and `ANTHROPIC_BASE_URL` are still supported as fallbacks.

### MongoDB AI Gateway / Grove

The MongoDB AI Gateway / Grove endpoint supports OpenAI-compatible and Anthropic-compatible routes.

For OpenAI-compatible models, use the `openai` provider and configure the SDK to point at the OpenAI-compatible gateway route.

```bash
export AI_API_KEY=$AI_GATEWAY_API_KEY
export AI_BASE_URL=https://ai-gateway.corp.mongodb.com/openai/v1
export AI_PROVIDER=openai
export AI_MODEL=gpt-5.5
```

For Anthropic-compatible models, use the `anthropic` provider and configure the SDK to point at the Anthropic-compatible gateway route.

```bash
export AI_API_KEY=$AI_GATEWAY_API_KEY
export AI_BASE_URL=https://ai-gateway.corp.mongodb.com/anthropic/v1
export AI_PROVIDER=anthropic
export AI_MODEL=claude-opus-4-6
```

For Anthropic, the script normalizes gateway URLs ending in `/v1` before passing them to the Anthropic SDK, because the SDK appends `/v1/messages` internally.

Then run the report normally:

```bash
python generate_report.py
```

You can also override the model per run:

```bash
python generate_report.py --ai-provider openai --ai-model gpt-5.5
python generate_report.py --ai-provider anthropic --ai-model claude-opus-4-6
```

## Output

Reports are saved under `reports/` with the current UTC date in their filenames:

- `org-<date>.html` — interactive scorecard
- `org-<date>.json` — collected data used for future comparisons
- `org-<date>.xlsx` — spreadsheet export

The `reports/` directory is ignored by Git.

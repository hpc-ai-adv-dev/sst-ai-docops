# SST Gap Tracker

The Gap Tracker turns SST Answerer outcomes and thumbs-up/down ratings into a
maintainer report. It counts the three answer outcomes, groups related
documentation gaps, writes local metrics, and can create or update a weekly
GitHub issue.

It does not edit SST documentation. Report formatting is deterministic.
Grouping uses the configured embedding endpoint, not the chat model. If that
endpoint is unavailable, exact text matches are grouped and the report marks
the fallback.

## Quick start

Run from the repository root after asking and rating questions in the demo:

```bash
# Collect the last seven days.
./gap-tracker.sh collect

# Inspect metrics and grouped questions.
./gap-tracker.sh report --up-to metrics
./gap-tracker.sh report --up-to cluster

# Write report.json and print the Markdown report.
./gap-tracker.sh report

# Preview local output and the GitHub issue without writing or posting.
./gap-tracker.sh publish --dry-run
```

`gap-tracker.sh` creates `gap-tracker/.venv/` on first use. Python 3.11 or
later is required.

## Inputs and snapshots

`collect` reads:

| Input | Used for |
|---|---|
| `runtime-data/gap_log.jsonl` | Questions and Answerer outcomes |
| Open WebUI feedback export | Positive and negative ratings and comments |

The event log is copied into `gap-tracker/data/events/`; the runtime copy is
not changed. Open WebUI may be unavailable and the local events will still be
collected. The snapshot records whether feedback was unavailable or simply
had no ratings.

Each run has its own directory:

```text
gap-tracker/data/snapshots/2026-08-26T190337.077113Z/
├── collected.json
└── feedbacks.json
```

Choose a collection window:

```bash
# Configured lookback, seven days by default.
./gap-tracker.sh collect

# Start at an ISO date or timestamp.
./gap-tracker.sh collect --since 2026-08-01
./gap-tracker.sh collect --since 2026-08-01T12:00:00Z

# Read all available events and feedback.
./gap-tracker.sh collect --all

# Print a machine-readable collection summary.
./gap-tracker.sh collect --all --json
```

## Build and inspect a report

Without `--snapshot`, `report` uses the newest `collected.json`:

```bash
./gap-tracker.sh report --up-to metrics
./gap-tracker.sh report --up-to metrics --json

./gap-tracker.sh report --up-to cluster
./gap-tracker.sh report --up-to cluster --json

./gap-tracker.sh report

./gap-tracker.sh report \
  --snapshot gap-tracker/data/snapshots/2026-08-26T190337.077113Z
```

Each grouped gap includes a representative question, frequency, known
contributor count, outcome types, and up to three additional phrasings.
Repeated interactions remain frequency evidence. A negative rating on an
already recorded gap is attached to the same grouped question.

## Publish

`publish` reads the newest `report.json` unless a path is supplied:

```bash
# Print everything and change nothing.
./gap-tracker.sh publish --dry-run

# Write local metrics and publish when GitHub is configured.
./gap-tracker.sh publish

# Publish one reviewed run.
./gap-tracker.sh publish \
  --report gap-tracker/data/snapshots/2026-08-26T190337.077113Z
```

Local output is written under:

```text
gap-tracker/data/output/
├── METRICS.md
└── metrics-history.csv
```

Republishing the same week replaces its CSV row. A GitHub issue with the same
weekly title is updated instead of duplicated.

Configure GitHub in `gap-tracker/config.yaml`, but keep the token in the
environment:

```yaml
github:
  base_url: "https://api.github.com"
  repo: "your-org/your-repo"
  labels: ["doc-gap", "sst"]
```

```bash
export GITHUB_TOKEN='your-token'
./gap-tracker.sh publish --dry-run
./gap-tracker.sh publish
```

## Configuration

The default `gap-tracker/config.yaml` points to the local demo:

```yaml
openwebui:
  url: "http://localhost:3000"
  email: "admin@localhost"
  password: "admin"
  timeout: 30

collection:
  lookback_days: 7

embedding:
  base_url: "http://localhost:8001/v1"
  model: "nomic-embed-text-v1.5.Q8_0.gguf"
  query_prefix: "search_query: "

paths:
  demo_event_log: "../runtime-data/gap_log.jsonl"
  events_dir: "data/events"
  snapshots_dir: "data/snapshots"
  embeddings_dir: "data/embeddings"
  metrics_md: "data/output/METRICS.md"
  metrics_csv: "data/output/metrics-history.csv"

clustering:
  distance_threshold: 0.3
  top_n_clusters: 20
```

Relative paths are resolved from `config.yaml`. Override local Open WebUI
credentials without editing it:

```bash
export SST_GAP_TRACKER_OPENWEBUI_EMAIL='admin@example.com'
export SST_GAP_TRACKER_OPENWEBUI_PASSWORD='your-password'
```

To use another OpenAI-compatible embedding service, change its URL, model
name, and required query prefix. The model name is part of each cache key, so
vectors from different models are not mixed.

## Event format

The Answerer writes a query event for every response:

```json
{
  "event": "query",
  "interaction_id": "ab12cd34",
  "timestamp": "2026-08-26T19:03:37.077113+00:00",
  "tier": "source_only",
  "query": "How can SST read an additional configuration file?",
  "user_id": "user-123",
  "chat_id": "chat-456"
}
```

Source-only and not-found responses also write a gap event with the same
`interaction_id`:

```json
{
  "event": "doc_gap_source_only",
  "interaction_id": "ab12cd34",
  "timestamp": "2026-08-26T19:03:37.077113+00:00",
  "query": "How can SST read an additional configuration file?",
  "user_id": "user-123",
  "chat_id": "chat-456"
}
```

| Query tier | Gap event | Meaning |
|---|---|---|
| `adequate_docs` | none | The answer cites relevant documentation |
| `source_only` | `doc_gap_source_only` | The answer depends on source code |
| `total_gap` | `doc_gap_no_answer` | The corpus does not support an answer |

If a current source-only or not-found query is missing its matching gap event,
the collector derives one. Pairing uses `interaction_id`.

Another answerer can write this JSONL format and set
`paths.demo_event_log` to its file. Package names, labels, and report headings
remain SST-specific unless changed.

## Data handling

Snapshots can contain questions, user IDs, chat IDs, ratings, and comments.
They stay under ignored `gap-tracker/data/` directories. Review reports before
publishing and do not add collected data to Git.

## Tests

Tests mock Open WebUI, GitHub, and embedding responses:

```bash
cd gap-tracker
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock pytest==9.1.1
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
.venv/bin/python -m pytest tests -q
```

## File map

| File | Purpose |
|---|---|
| `src/ai_docops_analytics/__main__.py` | `collect`, `report`, and `publish` CLI |
| `src/ai_docops_analytics/client.py` | Open WebUI authentication and feedback export |
| `src/ai_docops_analytics/collect.py` | Event and feedback collection |
| `src/ai_docops_analytics/metrics.py` | Outcome, rating, and weekly change metrics |
| `src/ai_docops_analytics/cluster.py` | Cached embeddings and gap grouping |
| `src/ai_docops_analytics/report.py` | Maintainer checklist formatting |
| `src/ai_docops_analytics/publish.py` | Local metrics and optional GitHub issue |

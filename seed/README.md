# Building the seeded demo image

This directory is for maintainers. End users should run the versioned image
named by `DEMO_IMAGE`; they do not need generated seed files.

`seed/config-seed/` contains a configured Open WebUI database, uploaded SST
files, and vectors. It is generated, ignored by Git, and copied into the demo
image at build time. Git stores only the refresh tools and
`benchmarks/sst-corpus-lock.json`, a compact record of repository revisions,
collection counts, and a corpus hash.

## Refresh SST documentation and source

The automated refresh reads three persistent Git checkouts. It does not clone,
pull, reset, or delete them.

The default location is:

```text
~/dev/sstsimulator/
├── sst-docs/
├── sst-core/
└── sst-elements/
```

Create the checkouts once:

```bash
mkdir -p ~/dev/sstsimulator

git clone https://github.com/sstsimulator/sst-docs.git \
  ~/dev/sstsimulator/sst-docs
git clone https://github.com/sstsimulator/sst-core.git \
  ~/dev/sstsimulator/sst-core
git clone https://github.com/sstsimulator/sst-elements.git \
  ~/dev/sstsimulator/sst-elements
```

Update them explicitly before a refresh:

```bash
git -C ~/dev/sstsimulator/sst-docs pull --ff-only
git -C ~/dev/sstsimulator/sst-core pull --ff-only
git -C ~/dev/sstsimulator/sst-elements pull --ff-only
```

If they live elsewhere, set one parent directory:

```bash
export SST_REPOS_ROOT=/path/containing/the-three-repositories
```

You can also pass `--sst-root=/path` to the refresh command.

### Review the question bank

Before indexing new commits, update the three corpus commit fields in
`benchmarks/sst-question-bank.json` and review its evidence.

Do not update only the hashes. Recheck cited files and every `source_only` and
`total_gap` question. New documentation may turn a source-only answer into a
documentation-backed answer, and new code or documentation may invalidate a
not-found question. Replace questions whose expected outcome is no longer
correct.

Run the evidence audit without comparing the old corpus lock:

```bash
python3 scripts/audit_question_bank.py --no-lock
```

### Dry run, refresh, and build

From the repository root:

```bash
# Check the repositories and question bank. No seed data is changed.
bash seed/refresh-sst-corpus.sh --dry-run

# Upload and index into a staged Open WebUI copy.
bash seed/refresh-sst-corpus.sh

# Validate generated image inputs, then build the local image.
bash seed/build.sh --check
bash seed/build.sh
```

Add `--fetch` to either refresh command to fetch each checkout and require its
current branch to match the configured upstream:

```bash
bash seed/refresh-sst-corpus.sh --fetch --dry-run
bash seed/refresh-sst-corpus.sh --fetch
```

Fetching does not merge or reset a checkout.

### What the refresh changes

The refresh:

1. reads Git-tracked documentation from `sst-docs/docs`;
2. reads Git-tracked source from `sst-core/src` and `sst-elements/src`;
3. builds an upload plan under `seed/.sst-refresh-work/`;
4. uploads into replacement documentation and source collections;
5. waits for each file to index;
6. validates collection counts, files, vectors, and model links;
7. removes superseded SST uploads and collections from the staged copy; and
8. swaps the staged seed into `seed/config-seed/`.

It never edits or removes the three source checkouts. It also never changes
`runtime-data/`, which belongs to a running end-user demo.

The known-good seed remains unchanged until validation succeeds. After a
successful swap, the previous seed is kept at:

```text
seed/.config-seed-backup/
```

The temporary upload plan can be large because it records every file needed
to resume safely. It lives only under the ignored work directory:

```text
seed/.sst-refresh-work/
├── sst-corpus-manifest.json
├── sst-refresh-state.json
└── data/
```

A successful refresh removes `.sst-refresh-work/`. A failed or interrupted
refresh keeps it. Rerun the same command to resume:

```bash
bash seed/refresh-sst-corpus.sh
```

Discard the staged attempt only when you intend to start over:

```bash
bash seed/refresh-sst-corpus.sh --restart
```

A complete SST re-index can take hours. The default batches files and restarts
the staging Open WebUI process between batches to keep memory bounded. The
Podman machine should have at least 8 GiB:

```bash
podman machine list
podman machine stop
podman machine set --memory 8192 podman-machine-default
podman machine start
```

## Make a configuration-only change

Use the manual seed session when changing the UI configuration or canonical
filter source without rebuilding the corpus:

```bash
# Start the embedding endpoint in one terminal.
bash scripts/start-llama-server.sh --embeddings-only

# Start Open WebUI from the current seed in another terminal.
bash seed/start-seed.sh
```

Open <http://localhost:3000>. If the Answerer filter changed, import and attach
it from a third terminal:

```bash
bash seed/import-filter.sh
```

Return to `start-seed.sh` and press Enter. The script stops Open WebUI,
synchronizes the reviewed filter and model settings, checks the staged
database, and asks before replacing `config-seed/`.

Manual work is staged under `seed/.seed-work/`. A successful capture moves it
into `config-seed/`. A failure keeps it for inspection and leaves the current
seed unchanged.

## Start from a blank Open WebUI

Use a fresh session only when you need a completely new corpus or Open WebUI
schema:

```bash
# Start all three model endpoints in one terminal.
bash scripts/start-llama-server.sh

# Start a blank Open WebUI in another terminal.
bash seed/start-seed.sh --fresh
```

For SST, create two collections:

| Collection | Files |
|---|---|
| `SST Documentation` | `.md` and `.mdx` under `sst-docs/docs` |
| `SST Source Code` | C, C++, headers, and Python under `sst-core/src` and `sst-elements/src` |

Create a model with ID `sst-answerer`, attach both collections, and run:

```bash
bash seed/import-filter.sh
```

The automated refresh is the normal path for later SST updates. A fresh manual
upload is slower and does not provide resumable API ingestion.

## Try another corpus

The citation filter can be tested with another project's documentation and
source code. The automated refresh, seed validation, question bank, and report
wording remain SST-specific.

Start blank:

```bash
# Run these in separate terminals.
bash scripts/start-llama-server.sh
bash seed/start-seed.sh --fresh
```

In Open WebUI:

1. create one documentation collection and one source collection;
2. upload files with stable, distinct filename prefixes;
3. create a project model and attach both collections;
4. import `seed/filters/confidence-gate.py` in Workspace → Functions;
5. attach the filter to the project model; and
6. set its valves to match the model and uploaded paths.

For example:

```text
model_ids = project-answerer
documentation_prefixes = project-docs
source_prefixes = project-core,project-plugins
```

Prefixes are comma-separated, case-insensitive beginnings of uploaded
filenames. Documentation and source prefixes must not overlap. Unknown or
ambiguous cited paths produce the not-found response.

Test the project in the live session. Do not capture it with the unmodified SST
scripts. A distributable non-SST image requires replacing the SST-specific
collection names, model checks, corpus lock, question bank, and Gap Tracker
labels.

## Validate and publish an image

Validate before building:

```bash
bash seed/build.sh --check
bash seed/build.sh
```

Test without replacing an existing runtime directory:

```bash
SMOKE_DIR="$(mktemp -d)"
RUNTIME_DATA_DIR="$SMOKE_DIR" ./start.sh

# Inspect http://localhost:3000, then:
./stop.sh
```

Remove the temporary directory after inspection.

Build and push both release architectures only after local validation:

```bash
RELEASE_IMAGE=ghcr.io/hpc-ai-adv-dev/sst-ai-docops:v0.1.0

podman build --platform linux/arm64 \
  -f seed/Containerfile \
  -t "$RELEASE_IMAGE-arm64" seed
podman build --platform linux/amd64 \
  -f seed/Containerfile \
  -t "$RELEASE_IMAGE-amd64" seed

podman manifest create "$RELEASE_IMAGE" \
  "$RELEASE_IMAGE-arm64" \
  "$RELEASE_IMAGE-amd64"
podman manifest push --all \
  "$RELEASE_IMAGE" \
  "docker://$RELEASE_IMAGE"
```

Publish the immutable image digest with the release.

## File map

| File | Purpose |
|---|---|
| `refresh-sst-corpus.sh` | Resumable staged SST refresh |
| `start-seed.sh` | Manual staged configuration session |
| `import-filter.sh` | Install and attach the Answerer filter |
| `filters/confidence-gate.py` | Check cited support and write outcome events |
| `build.sh` | Validate or build the seeded image |
| `Containerfile` | Build the Open WebUI image from generated seed data |
| `entrypoint.sh` | Copy the seed on first run and start Open WebUI |
| `sync-seed-config.py` | Restore reviewed filter, model, and collection links |
| `patch-open-webui.py` | Apply the pinned Open WebUI collection-read fix |
| `../scripts/refresh_sst_corpus.py` | Build, ingest, finalize, compact, and validate |
| `../scripts/seed_safety.py` | Reject private state and inconsistent seed data |

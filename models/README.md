# Models Directory

Place the three downloaded GGUF model files in this directory. See the root
README for filenames and download commands.

The model files are ignored by Git. `SHA256SUMS` is tracked. `start.sh` checks
all three models before launching inference, and the corpus refresh checks the
embedding model before indexing.

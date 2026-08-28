# Copyright Hewlett Packard Enterprise Development LP.
"""Configuration constants shared by the Gap Tracker CLI."""

# Path keys in config.yaml that should be resolved relative to the config file directory.
# When adding new path fields to config.yaml, add them to this tuple.
RESOLVABLE_PATH_KEYS = (
    "events_dir",
    "snapshots_dir",
    "embeddings_dir",
    "metrics_md",
    "metrics_csv",
    "demo_event_log",
)

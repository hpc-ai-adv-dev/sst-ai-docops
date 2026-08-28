# Copyright Hewlett Packard Enterprise Development LP.
"""CLI entry point for the SST Gap Tracker.

Three commands map to the natural pipeline boundaries:

    sst-gap-tracker collect                    # JSONL + API → snapshot
    sst-gap-tracker report                     # snapshot → report
    sst-gap-tracker report --up-to metrics     # just the numbers
    sst-gap-tracker report --up-to cluster     # inspect gap groupings
    sst-gap-tracker publish                    # report → GitHub + files
    sst-gap-tracker publish --dry-run          # preview first
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, timedelta
from pathlib import Path

import yaml

from ai_docops_analytics.config import RESOLVABLE_PATH_KEYS

REPORT_STAGES = ("metrics", "cluster", "report")


def _load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _resolve_paths(cfg: dict, config_path: str) -> None:
    """Resolve relative paths in config.yaml to absolute paths based on config location."""
    cfg_dir = Path(config_path).resolve().parent
    for key in RESOLVABLE_PATH_KEYS:
        p = cfg.get("paths", {}).get(key, "")
        if p and not Path(p).is_absolute():
            cfg["paths"][key] = str(cfg_dir / p)


def _find_latest(snapshots_dir: str, filename: str) -> Path:
    """Find the most recent dated snapshot containing *filename*."""
    base = Path(snapshots_dir)
    if base.is_dir():
        candidates = sorted(
            (d / filename for d in base.iterdir()
             if d.is_dir() and (d / filename).is_file()),
            key=lambda p: p.parent.name,
            reverse=True,
        )
        if candidates:
            return candidates[0]
    raise SystemExit(
        f"No {filename} found in {base}/*/  — run the previous command first."
    )


def _resolve_snapshot(explicit: str | None, snapshots_dir: str, filename: str) -> Path:
    """Return an explicit path or fall back to the latest snapshot."""
    if explicit:
        p = Path(explicit)
        if p.is_dir():
            p = p / filename
        if not p.is_file():
            raise SystemExit(f"File not found: {p}")
        return p
    return _find_latest(snapshots_dir, filename)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="sst-gap-tracker",
        description="SST Answerer gap detection, metrics, and reporting",
    )
    parser.add_argument("--config", default="config.yaml",
                        help="path to config.yaml (default: %(default)s)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="debug logging")

    sub = parser.add_subparsers(dest="command", required=True)

    # ── collect ──────────────────────────────────────────────────────
    p_collect = sub.add_parser(
        "collect", help="harvest JSONL + OpenWebUI APIs → snapshot")
    p_collect.add_argument("--since",
                           help="only include events after this ISO date")
    p_collect.add_argument(
        "--all",
        action="store_true",
        dest="collect_all",
        help="collect the complete history instead of the configured lookback",
    )
    p_collect.add_argument("--json", action="store_true", dest="json_output",
                           help="print summary as JSON")

    # ── report ───────────────────────────────────────────────────────
    p_report = sub.add_parser(
        "report",
        help="process a collected snapshot → metrics → clusters → gap report")
    p_report.add_argument("--snapshot", metavar="PATH",
                          help="path to collected.json (default: latest)")
    p_report.add_argument("--up-to", choices=REPORT_STAGES, default="report",
                          metavar="STAGE",
                          help="stop after STAGE: metrics, cluster, report "
                               "(default: %(default)s)")
    p_report.add_argument("--json", action="store_true", dest="json_output",
                          help="output as JSON at the stopping stage")

    # ── publish ──────────────────────────────────────────────────────
    p_publish = sub.add_parser(
        "publish", help="post GitHub Issue + write METRICS.md + append CSV")
    p_publish.add_argument("--report", metavar="PATH", dest="report_path",
                           help="path to report.json (default: latest)")
    p_publish.add_argument("--dry-run", action="store_true",
                           help="preview without writing files or posting")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger(__name__)

    cfg = _load_config(args.config)
    _resolve_paths(cfg, args.config)

    log.debug("Resolved paths:")
    for key, val in cfg.get("paths", {}).items():
        log.debug("  paths.%s = %s", key, val)

    {"collect": _cmd_collect, "report": _cmd_report, "publish": _cmd_publish}[
        args.command
    ](args, cfg)


# ── Commands ─────────────────────────────────────────────────────────────────

def _cmd_collect(args, cfg: dict) -> None:
    from ai_docops_analytics.collect import run_collect

    since = args.since
    if not since and not args.collect_all:
        lookback_days = int(cfg.get("collection", {}).get("lookback_days", 7))
        if lookback_days > 0:
            since = (date.today() - timedelta(days=lookback_days)).isoformat()
            logging.getLogger(__name__).info(
                "Using configured %d-day collection window (since %s)",
                lookback_days,
                since,
            )

    collected, snap_dir = run_collect(cfg, since=since)

    snap_file = snap_dir / "collected.json"
    snap_file.write_text(json.dumps(collected, indent=2, default=str))
    logging.getLogger(__name__).info("Snapshot saved: %s", snap_file)

    n_gaps = len(collected["gap_events"]) + len(collected.get("derived_gap_events", []))
    n_fb = len(collected.get("feedbacks") or [])
    summary = {
        "query_events": len(collected["query_events"]),
        "gap_events": len(collected["gap_events"]),
        "derived_gap_events": len(collected.get("derived_gap_events", [])),
        "feedbacks": n_fb,
        "source_status": collected.get("source_status", {}),
        "snapshot": str(snap_file),
    }
    if args.json_output:
        print(json.dumps(summary, indent=2))
    else:
        print(f"\nCollected {summary['query_events']} queries, "
              f"{n_gaps} gaps, {n_fb} feedbacks")
        unavailable = [
            name
            for name, status in summary["source_status"].items()
            if isinstance(status, dict) and not status.get("available", False)
        ]
        if unavailable:
            print("Unavailable sources: " + ", ".join(sorted(unavailable)))
        print(f"Snapshot → {snap_file}\n")


def _cmd_report(args, cfg: dict) -> None:
    log = logging.getLogger(__name__)
    snap_dir = cfg["paths"]["snapshots_dir"]
    snap_path = _resolve_snapshot(args.snapshot, snap_dir, "collected.json")
    log.info("Using snapshot: %s", snap_path)

    collected = json.loads(snap_path.read_text())
    collected_at = collected.get("collection_window", {}).get("collected_at")
    from ai_docops_analytics.timestamps import parse_timestamp
    collected_timestamp = parse_timestamp(collected_at)
    report_date = (
        collected_timestamp.date() if collected_timestamp else date.today()
    )
    period_start = report_date - timedelta(days=report_date.weekday())

    # ── metrics (deterministic) ──────────────────────────────────────
    from ai_docops_analytics.metrics import compute_metrics
    metrics = compute_metrics(collected, cfg, period_start=period_start)

    if args.up_to == "metrics":
        _output_metrics(metrics, args.json_output)
        return

    # ── cluster (deterministic with cached embeddings) ───────────────
    from ai_docops_analytics.cluster import cluster_gaps
    clusters = cluster_gaps(collected, cfg)

    if args.up_to == "cluster":
        _output_clusters(clusters, args.json_output)
        return

    # ── report (deterministic formatting) ───────────────────────────
    from ai_docops_analytics.report import generate_report
    report_md = generate_report(clusters, period_start=period_start)

    # Save the full bundle so `publish` reads this exact output.
    bundle = {
        "period_start": period_start.isoformat(),
        "metrics": metrics,
        "clusters": clusters,
        "report_md": report_md,
    }
    report_file = snap_path.parent / "report.json"
    report_file.write_text(json.dumps(bundle, indent=2, default=str))
    log.info("Report saved: %s", report_file)

    print(report_md)


def _cmd_publish(args, cfg: dict) -> None:
    log = logging.getLogger(__name__)
    snap_dir = cfg["paths"]["snapshots_dir"]
    report_path = _resolve_snapshot(args.report_path, snap_dir, "report.json")
    log.info("Using report: %s", report_path)

    bundle = json.loads(report_path.read_text())

    from ai_docops_analytics.publish import publish
    publish(
        bundle["report_md"],
        bundle["metrics"],
        cfg,
        dry_run=args.dry_run,
        period_start=bundle.get("period_start"),
    )


# ── Output formatters ────────────────────────────────────────────────────────

def _output_metrics(m: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(m, indent=2, default=str))
        return
    print("\n=== Metrics ===\n")
    for key, val in m.items():
        if isinstance(val, dict):
            print(f"  {key}:")
            for k2, v2 in val.items():
                print(f"    {k2}: {v2}")
        else:
            print(f"  {key}: {val}")
    print()


def _output_clusters(clusters: list[dict], as_json: bool) -> None:
    if as_json:
        print(json.dumps(clusters, indent=2, default=str))
        return
    print(f"\n=== {len(clusters)} Gap Clusters ===\n")
    for i, c in enumerate(clusters, 1):
        print(f"  {i}. [{c['size']} queries] {c['representative']}")
        for q in c.get("queries", [])[:3]:
            if q != c["representative"]:
                print(f"     - {q}")
    print()


if __name__ == "__main__":
    main()

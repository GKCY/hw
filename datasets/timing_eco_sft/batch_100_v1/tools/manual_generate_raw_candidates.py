#!/usr/bin/env python3
"""Generate auditable raw-screen candidate shards from the frozen probe."""

from __future__ import annotations

import argparse
import csv
import io
import sys
from pathlib import Path


TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import batch100  # noqa: E402
import manual_rebind_relaxed_v2 as manual  # noqa: E402
from common import BatchError, atomic_write  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--hierarchy",
        choices=("exp", "pipeline", "top_tree"),
        default="pipeline",
    )
    parser.add_argument("--shards", type=int, default=4)
    parser.add_argument("--limit-per-shard", type=int, default=50)
    parser.add_argument("--target-raw-ns", type=float, default=0.02)
    parser.add_argument("--predicted-min-ns", type=float)
    parser.add_argument("--predicted-max-ns", type=float)
    parser.add_argument("--max-baseline-slack-ns", type=float, default=1.0)
    parser.add_argument(
        "--instance-prefix",
        help="only generate candidates whose instance starts with this prefix",
    )
    parser.add_argument(
        "--exclude-screen-dir",
        type=Path,
        help="skip endpoint/instance/new_ref triples already present in screen.tsv files",
    )
    args = parser.parse_args()
    if args.shards <= 0 or args.limit_per_shard <= 0:
        raise BatchError("shard count and limit must be positive")
    if args.output_dir.exists() or args.output_dir.is_symlink():
        raise BatchError(f"output directory already exists: {args.output_dir}")

    _, manifest = batch100._verified_store(args.run_dir)
    probe = batch100._verified_frozen_probe(args.run_dir, manifest["baseline"])
    ladders, source_drive = manual._equivalent_ladders(probe["ladders"])
    paths = {
        row["stable_rank"]: row
        for row in probe["paths"]
        if args.hierarchy in row["hierarchy_groups"].split(",")
        and float(row["slack_ns"]) <= args.max_baseline_slack_ns
    }
    reachability = {
        row["instance"]: {
            endpoint for endpoint in row["endpoints"].split(",") if endpoint
        }
        for row in probe["reachability"]
    }

    ranked: list[tuple[float, str, str, str, str, float, float]] = []
    seen: set[tuple[str, str, str]] = set()
    if args.exclude_screen_dir is not None:
        for screen_path in sorted(args.exclude_screen_dir.glob("*/screen.tsv")):
            with screen_path.open(newline="") as stream:
                for row in csv.DictReader(stream, delimiter="\t"):
                    endpoint = row.get("endpoint")
                    instance = row.get("instance")
                    new_ref = row.get("new_ref")
                    if endpoint and instance and new_ref:
                        seen.add((endpoint, instance, new_ref))
    excluded_count = len(seen)
    for point in probe["points"]:
        path = paths.get(point["stable_rank"])
        if (
            path is None
            or point["delay_kind"] != "cell"
            or not point["inst"]
            or (
                args.instance_prefix is not None
                and not point["inst"].startswith(args.instance_prefix)
            )
            or reachability.get(point["inst"], set()) != {path["endpoint"]}
        ):
            continue
        baseline_ref = point["ref"]
        drive = source_drive.get(baseline_ref)
        if drive is None:
            continue
        delay = float(point["delay_ns"])
        baseline_slack = float(path["slack_ns"])
        for variant_drive, new_ref in ladders.get(baseline_ref, []):
            key = (path["endpoint"], point["inst"], new_ref)
            if new_ref == baseline_ref or key in seen:
                continue
            seen.add(key)
            predicted = baseline_slack - delay * (drive / variant_drive - 1.0)
            if (
                args.predicted_min_ns is not None
                and predicted < args.predicted_min_ns
            ):
                continue
            if (
                args.predicted_max_ns is not None
                and predicted > args.predicted_max_ns
            ):
                continue
            ranked.append(
                (
                    abs(predicted - args.target_raw_ns),
                    path["endpoint"],
                    point["inst"],
                    baseline_ref,
                    new_ref,
                    predicted,
                    baseline_slack,
                )
            )
    ranked.sort()
    selected = ranked[: args.shards * args.limit_per_shard]
    if len(selected) < args.shards:
        raise BatchError("frozen probe produced too few raw candidates")

    args.output_dir.mkdir(parents=True)
    fieldnames = (
        "endpoint",
        "instance",
        "baseline_ref",
        "new_ref",
        "predicted_raw_slack_ns",
        "baseline_slack_ns",
    )
    for shard in range(args.shards):
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, delimiter="\t", fieldnames=fieldnames)
        writer.writeheader()
        for row in selected[shard:: args.shards]:
            _, endpoint, instance, baseline_ref, new_ref, predicted, baseline = row
            writer.writerow(
                {
                    "endpoint": endpoint,
                    "instance": instance,
                    "baseline_ref": baseline_ref,
                    "new_ref": new_ref,
                    "predicted_raw_slack_ns": f"{predicted:.9f}",
                    "baseline_slack_ns": f"{baseline:.9f}",
                }
            )
        atomic_write(
            args.output_dir / f"raw_candidates_{shard}.tsv",
            buffer.getvalue().encode(),
        )
    print(
        f"RAW_CANDIDATES_DONE selected={len(selected)} excluded={excluded_count} "
        f"shards={args.shards} output={args.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Screen frozen-probe RVT alternatives in one non-authoritative Innovus run."""

from __future__ import annotations

import argparse
import csv
import re
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
    parser.add_argument("--slot", required=True, type=int)
    parser.add_argument("--screen-id", required=True)
    parser.add_argument(
        "--raw",
        action="store_true",
        help="rank electrical impact without refinePlace; never emits PASS hits",
    )
    parser.add_argument(
        "--candidate",
        action="append",
        nargs=3,
        metavar=("ENDPOINT", "INSTANCE", "NEW_REF"),
    )
    parser.add_argument(
        "--candidate-file",
        action="append",
        type=Path,
        help="TSV with endpoint, instance, and new_ref columns",
    )
    args = parser.parse_args()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", args.screen_id) is None:
        raise BatchError(f"unsafe screen ID: {args.screen_id!r}")

    _, manifest = batch100._verified_store(args.run_dir)
    probe = batch100._verified_frozen_probe(args.run_dir, manifest["baseline"])
    ladders, _ = manual._equivalent_ladders(probe["ladders"])
    paths = {row["endpoint"]: row for row in probe["paths"]}
    points = {
        (row["stable_rank"], row["inst"]): row
        for row in probe["points"]
        if row["delay_kind"] == "cell" and row["inst"]
    }
    reachability = {
        row["instance"]: {
            endpoint for endpoint in row["endpoints"].split(",") if endpoint
        }
        for row in probe["reachability"]
    }
    candidates = list(args.candidate or [])
    for candidate_file in args.candidate_file or []:
        with candidate_file.open(newline="") as stream:
            for row in csv.DictReader(stream, delimiter="\t"):
                candidates.append(
                    [row["endpoint"], row["instance"], row["new_ref"]]
                )
    if not candidates:
        raise BatchError("manual screen requires at least one candidate")

    records: list[tuple[str, str, str, str]] = []
    for endpoint, instance, new_ref in candidates:
        try:
            path = paths[endpoint]
            point = points[(path["stable_rank"], instance)]
        except KeyError as exc:
            raise BatchError(
                f"screen candidate is absent from the frozen target path: "
                f"{endpoint} {instance}"
            ) from exc
        if reachability.get(instance, set()) != {endpoint}:
            raise BatchError(f"{instance}: screen candidate is not endpoint-local")
        baseline_ref = point["ref"]
        legal_refs = {reference for _, reference in ladders.get(baseline_ref, [])}
        if new_ref == baseline_ref or new_ref not in legal_refs:
            raise BatchError(
                f"{instance}: {new_ref} is not a same-stem equivalent RVT resize"
            )
        records.append((endpoint, instance, baseline_ref, new_ref))

    local_root = args.run_dir / "manual_screen" / args.screen_id
    if local_root.exists() or local_root.is_symlink():
        raise BatchError(f"manual screen leaf already exists: {local_root}")
    local_root.mkdir(parents=True)
    config = ["set B100_SCREEN_CANDIDATES {"]
    for index, record in enumerate(records):
        config.append(
            "  {"
            + " ".join(
                batch100._tcl_atom(value)
                for value in (str(index), *record)
            )
            + "}"
        )
    config.extend(("}", ""))
    config_path = local_root / "screen_config.tcl"
    atomic_write(config_path, "\n".join(config).encode())

    run_id = batch100._run_id(args.run_dir)
    remote_root = (
        f"{batch100.GUEST_RUN_ROOT}/{run_id}/manual_screen/{args.screen_id}"
    )
    mkdir = batch100._ssh(
        args.slot, f"test ! -e {remote_root} && mkdir -p {remote_root}", timeout=30
    )
    if mkdir.returncode:
        raise BatchError(f"remote screen leaf is not fresh: {mkdir.stdout[-1000:]}")
    script = TOOLS / (
        "manual_screen_raw.tcl" if args.raw else "manual_screen_resize.tcl"
    )
    batch100._scp_to(args.slot, script, f"{remote_root}/screen.tcl")
    batch100._scp_to(args.slot, config_path, f"{remote_root}/screen_config.tcl")
    command = (
        f"bash -lc 'cd {remote_root} && "
        f"B100_BASELINE_DIR={batch100.GUEST_BASELINE} "
        f"B100_SCREEN_CONFIG={remote_root}/screen_config.tcl "
        f"B100_SCREEN_OUT={remote_root}/screen.tsv "
        "timeout 14400s innovus -no_gui -files screen.tcl -log screen.log'"
    )
    result = batch100._ssh(args.slot, command, timeout=14500)
    atomic_write(local_root / "innovus.log", (result.stdout or "").encode())
    if (
        result.returncode
        or "B100_MANUAL_SCREEN_COMPLETE" not in (result.stdout or "")
        or "**ERROR:" in (result.stdout or "")
    ):
        raise BatchError(f"manual screen failed; see {local_root / 'innovus.log'}")
    batch100._scp_from(args.slot, f"{remote_root}/screen.tsv", local_root)
    table = local_root / "screen.tsv"
    with table.open(newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    if len(rows) != len(records):
        raise BatchError("manual screen result cardinality mismatch")
    hits = [] if args.raw else [
        row
        for row in rows
        if row["negative_count"] == "1"
        and row["negative_endpoints"] == row["endpoint"]
        and -0.0700001 <= float(row["target_slack_ns"]) <= -0.0499999
    ]
    print(f"MANUAL_SCREEN_DONE rows={len(rows)} hits={len(hits)}")
    for row in hits:
        print(
            "HIT "
            + " ".join(
                row[key]
                for key in (
                    "endpoint",
                    "instance",
                    "baseline_ref",
                    "new_ref",
                    "target_slack_ns",
                )
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

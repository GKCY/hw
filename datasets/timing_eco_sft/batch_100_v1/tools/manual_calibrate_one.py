#!/usr/bin/env python3
"""Calibrate one manually bound Batch-100 card without starting its replays.

This narrow operator helper lets several isolated Firecracker guests measure
different cards in parallel.  It deliberately stops at FROZEN so that each
passing card can later receive its two distinct, supervised replay slots.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import batch100
from common import BatchError, read_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--slot", required=True, type=int)
    args = parser.parse_args()

    store, _ = batch100._verified_store(args.run_dir)
    specs = read_json(batch100.SPECS_PATH)
    by_id = {case["id"]: case for case in specs["cases"]}
    try:
        spec = by_id[args.case_id]
    except KeyError as exc:
        raise BatchError(f"unknown case ID: {args.case_id}") from exc

    state = store.row(args.case_id)["state"]
    if state != "BOUND":
        raise BatchError(
            f"{args.case_id}: calibration-only helper requires BOUND, got {state}"
        )
    print(
        batch100._calibrate_one(args.run_dir, store, spec, args.slot),
        flush=True,
    )
    state = store.row(args.case_id)["state"]
    if state not in {"FROZEN", "PROBE_ELIGIBLE"}:
        raise BatchError(f"{args.case_id}: calibration stopped in state {state}")
    print(f"MANUAL_CALIBRATION_DONE {args.case_id} {state}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

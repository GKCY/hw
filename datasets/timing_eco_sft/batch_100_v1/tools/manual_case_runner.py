#!/usr/bin/env python3
"""Run one already-bound Batch-100 case through its complete real-tool loop.

This is intentionally a narrow operator tool: it does not create bindings or
weaken validation.  It is used when cases are supervised one at a time on the
EDA host, while all durable state remains in the ordinary Batch-100 run
directory.
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
    parser.add_argument("--calibration-slot", required=True, type=int)
    parser.add_argument("--replay-slot-a", required=True, type=int)
    parser.add_argument("--replay-slot-b", required=True, type=int)
    args = parser.parse_args()
    if args.replay_slot_a == args.replay_slot_b:
        raise BatchError("dual replay requires two distinct slots")

    store, _ = batch100._verified_store(args.run_dir)
    specs = read_json(batch100.SPECS_PATH)
    by_id = {case["id"]: case for case in specs["cases"]}
    try:
        spec = by_id[args.case_id]
    except KeyError as exc:
        raise BatchError(f"unknown case ID: {args.case_id}") from exc

    state = store.row(args.case_id)["state"]
    if state == "BOUND":
        print(
            batch100._calibrate_one(
                args.run_dir, store, spec, args.calibration_slot
            ),
            flush=True,
        )
        state = store.row(args.case_id)["state"]
    if state == "FROZEN":
        print(
            batch100._execute_one(
                args.run_dir,
                store,
                spec,
                args.replay_slot_a,
                args.replay_slot_b,
                batch100._functional_pairs(args.run_dir),
            ),
            flush=True,
        )
        state = store.row(args.case_id)["state"]
    if state not in {"VALIDATED", "FINALIZED"}:
        raise BatchError(f"{args.case_id}: stopped in state {state}")
    print(f"MANUAL_CASE_PASS {args.case_id} {state}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

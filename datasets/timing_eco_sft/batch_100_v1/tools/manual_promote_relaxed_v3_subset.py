#!/usr/bin/env python3
"""Promote a verified relaxed-v3 probe for an explicitly selected case subset.

This recovery/operator path is intentionally narrow: it reuses an immutable
probe that completed before a later automatic binding failed, binds only the
named cases, and advances only those cases from PLANNED to BOUND.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import batch100  # noqa: E402
from common import BatchError, read_json, sha256_file  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--case-id", action="append", required=True)
    args = parser.parse_args()

    store, manifest = batch100._verified_store(args.run_dir)
    specs = read_json(batch100.SPECS_PATH, "relaxed-v3 case specs")
    if specs.get("schema_version") != batch100.RELAXED_V3_SCHEMA:
        raise BatchError("subset probe promotion requires relaxed-v3 specs")
    by_id = {case["id"]: case for case in specs["cases"]}
    case_ids = list(dict.fromkeys(args.case_id))
    if len(case_ids) != len(args.case_id):
        raise BatchError("duplicate case ID in subset")
    try:
        selected = [by_id[case_id] for case_id in case_ids]
    except KeyError as exc:
        raise BatchError(f"unknown case ID: {exc.args[0]}") from exc
    for case_id in case_ids:
        if store.row(case_id)["state"] != "PLANNED":
            raise BatchError(f"{case_id}: subset promotion requires PLANNED state")

    probe = batch100._verified_frozen_probe(args.run_dir, manifest["baseline"])
    batch100._bind_relaxed_cases(
        probe,
        {"schema_version": batch100.RELAXED_V3_SCHEMA, "cases": selected},
        args.run_dir,
    )
    for case_id in case_ids:
        binding_path = args.run_dir / "bindings" / f"{case_id}.json"
        store.transition(
            case_id,
            "PROBE_ELIGIBLE",
            "reused completed exact-baseline frozen probe",
        )
        store.transition(
            case_id,
            "BOUND",
            "deterministic relaxed-v3 subset binding",
            binding_sha256=sha256_file(binding_path),
        )
    print(
        f"V3_SUBSET_PROMOTE_PASS paths={len(probe['paths'])} "
        f"bound={','.join(case_ids)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

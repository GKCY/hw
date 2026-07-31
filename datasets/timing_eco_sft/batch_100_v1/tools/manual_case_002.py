#!/usr/bin/env python3
"""Isolated, single-case closure driver for B1_CASE_002.

The driver intentionally narrows the frozen Batch-100 binding to one manually
selected candidate.  It reuses the batch runtime and finalizer, but keeps both
local state and guest task leaves separate from the abandoned unified run.
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
from pathlib import Path


TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
PRODUCTION_RUN = ROOT / "work" / "batch100-v1"
RUN_DIR = ROOT / "work" / "manual_v1" / "B1_CASE_002"
CASE_ID = "B1_CASE_002"
TARGET = "pp_out_l0n09_0_d1_reg_21_/D"
REMOTE_ROOT = "/work/mock_lef_batch100_manual_v1"
SELECTED_CANDIDATE_INDEX = 0

sys.path.insert(0, str(TOOLS))

import batch100  # noqa: E402
import finalize_batch  # noqa: E402
from state_store import StateStore  # noqa: E402


batch100.GUEST_RUN_ROOT = REMOTE_ROOT


def _spec() -> dict:
    specs = batch100.read_json(batch100.SPECS_PATH, "case specs")
    return next(item for item in specs["cases"] if item["id"] == CASE_ID)


def _manifest() -> dict:
    return batch100.read_json(RUN_DIR / "run_manifest.json", "manual manifest")


def _selected_binding(probe: dict) -> dict:
    source = batch100.read_json(
        PRODUCTION_RUN / "bindings" / f"{CASE_ID}.json",
        "source Case002 binding",
    )
    if source["target_endpoints"] != [TARGET]:
        raise batch100.BatchError("source Case002 target identity changed")
    matches = [
        candidate
        for candidate in source["calibration_candidates"]
        if candidate["candidate_index"] == SELECTED_CANDIDATE_INDEX
    ]
    if len(matches) != 1:
        raise batch100.BatchError("selected Case002 candidate is not unique")
    selected = copy.deepcopy(matches[0])
    expected_injection = [
        (
            "u_tree_l0n09/U727",
            "XOR2_X1P4M_A9TR40",
            ["XOR2_X0P7M_A9TR40"],
        ),
        (
            "u_tree_l0n09/U726",
            "OA21A1OI2_X1M_A9TR40",
            ["OA21A1OI2_X0P5M_A9TR40"],
        ),
        (
            "u_tree_l0n09/U585",
            "INV_X1M_A9TR40",
            ["INV_X0P7M_A9TR40"],
        ),
    ]
    actual_injection = [
        (
            operation["instance"],
            operation["baseline_ref"],
            operation["candidate_refs_nearest_first"],
        )
        for operation in selected["injection_operations"]
    ]
    if actual_injection != expected_injection:
        raise batch100.BatchError(
            f"selected Case002 injection changed: {actual_injection!r}"
        )
    expected_repair = (
        "u_mul_37/u_tree_l0n1/U185",
        "XOR2_X0P7M_A9TR40",
        "XOR2_X4M_A9TR40",
    )
    repair = selected["repair_operations"]
    actual_repair = (
        repair[0]["instance"],
        repair[0]["checkpoint_ref"],
        repair[0]["new_ref"],
    )
    if len(repair) != 1 or actual_repair != expected_repair:
        raise batch100.BatchError(
            f"selected Case002 repair changed: {actual_repair!r}"
        )
    return {
        **source,
        "probe_tree_sha256": probe["tree_sha256"],
        "injection_operations": selected["injection_operations"],
        "repair_operations": selected["repair_operations"],
        "calibration_candidates": [selected],
    }


def init() -> None:
    if RUN_DIR.exists() or RUN_DIR.is_symlink():
        raise batch100.BatchError(f"manual run already exists: {RUN_DIR}")
    RUN_DIR.mkdir(parents=True)
    source_manifest = batch100.read_json(
        PRODUCTION_RUN / "run_manifest.json", "source run manifest"
    )
    baseline = source_manifest["baseline"]
    current_baseline = batch100._baseline_binding(batch100.DEFAULT_BASELINE)
    if baseline != current_baseline:
        raise batch100.BatchError("exact local baseline identity changed")
    shutil.copytree(PRODUCTION_RUN / "probe", RUN_DIR / "probe")
    probe = batch100._verified_frozen_probe(RUN_DIR, baseline)
    binding = _selected_binding(probe)
    bindings = RUN_DIR / "bindings"
    bindings.mkdir()
    binding_path = bindings / f"{CASE_ID}.json"
    batch100.atomic_json(binding_path, binding, durable=True)
    hashes = batch100._input_hashes()
    store = StateStore(RUN_DIR)
    store.initialize([CASE_ID], hashes, baseline)
    batch100.atomic_json(
        RUN_DIR / "run_manifest.json",
        {
            "schema_version": "mock_lef_batch100.manual_run_manifest.v1",
            "run_id": CASE_ID,
            "case_id": CASE_ID,
            "input_hashes": hashes,
            "baseline": baseline,
            "probe_tree_sha256": probe["tree_sha256"],
            "remote": {
                "host": batch100.REMOTE_HOST,
                "guest_run_root": REMOTE_ROOT,
                "calibration_slot": "slot0",
                "replay_slots": ["slot1", "slot2"],
            },
        },
        durable=True,
    )
    store.transition(CASE_ID, "PROBE_ELIGIBLE", "frozen probe copied and verified")
    store.transition(
        CASE_ID,
        "BOUND",
        "manual candidate 0 selected from the frozen binding",
        binding_sha256=batch100.sha256_file(binding_path),
    )
    print(
        json.dumps(
            {
                "status": "BOUND",
                "case_id": CASE_ID,
                "target": TARGET,
                "candidate_index": SELECTED_CANDIDATE_INDEX,
                "probe_tree_sha256": probe["tree_sha256"],
                "binding_sha256": batch100.sha256_file(binding_path),
            },
            sort_keys=True,
        )
    )


def calibrate() -> None:
    print(batch100._calibrate_one(RUN_DIR, StateStore(RUN_DIR), _spec(), 0))


def replay() -> None:
    functional = batch100._functional_pairs(RUN_DIR)
    print(
        batch100._execute_one(
            RUN_DIR,
            StateStore(RUN_DIR),
            _spec(),
            1,
            2,
            functional,
        )
    )


def validate() -> None:
    case_root = RUN_DIR / "cases" / CASE_ID
    acceptance = batch100.read_json(batch100.SPECS_PATH)["acceptance"]
    summary = finalize_batch.validate_case(case_root, _spec(), acceptance)
    print(json.dumps(summary, sort_keys=True))


def status() -> None:
    store = StateStore(RUN_DIR)
    print(json.dumps(store.row(CASE_ID), sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("init", "calibrate", "replay", "validate", "status"),
    )
    args = parser.parse_args()
    globals()[args.command]()


if __name__ == "__main__":
    main()

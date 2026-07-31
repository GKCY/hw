#!/usr/bin/env python3
"""Isolated, single-case closure driver for B1_CASE_001.

This intentionally reuses the frozen Batch-100 runtime/finalizer while keeping
both local state and guest task leaves outside the active batch100-v1 run.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path


TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
PRODUCTION_RUN = ROOT / "work" / "batch100-v1"
RUN_DIR = ROOT / "work" / "manual_v1" / "B1_CASE_001"
CASE_ID = "B1_CASE_001"
TARGET = "u_exp/exp_sft_09_reg_1_/D"
REMOTE_ROOT = "/work/mock_lef_batch100_manual_v1"

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


def _forced_binding(probe: dict, spec: dict) -> dict:
    forced_paths = []
    for source in probe["paths"]:
        row = dict(source)
        if row["endpoint"] != TARGET:
            row["hierarchy_groups"] = ""
        forced_paths.append(row)
    forced_probe = {**probe, "paths": forced_paths}
    with tempfile.TemporaryDirectory(prefix=".case001-bind-", dir=RUN_DIR) as tmp:
        output = Path(tmp)
        batch100._bind_cases(forced_probe, {"cases": [spec]}, output)
        binding = batch100.read_json(
            output / "bindings" / f"{CASE_ID}.json", "forced Case001 binding"
        )
    if binding["target_endpoints"] != [TARGET]:
        raise batch100.BatchError("forced Case001 target identity changed")
    expected = (
        "u_exp/U434",
        "XOR2_X2M_A9TR40",
        "XOR2_X0P5M_A9TR40",
    )
    first = binding["calibration_candidates"][0]["injection_operations"][0]
    actual = (
        first["instance"],
        first["baseline_ref"],
        first["candidate_refs_nearest_first"][0],
    )
    if actual != expected:
        raise batch100.BatchError(
            f"forced Case001 first candidate changed: {actual!r}"
        )
    return binding


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
    spec = _spec()
    binding = _forced_binding(probe, spec)
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
        "forced endpoint-local manual binding",
        binding_sha256=batch100.sha256_file(binding_path),
    )
    print(
        json.dumps(
            {
                "status": "BOUND",
                "case_id": CASE_ID,
                "target": TARGET,
                "candidate_count": len(binding["calibration_candidates"]),
                "probe_tree_sha256": probe["tree_sha256"],
                "binding_sha256": batch100.sha256_file(binding_path),
            },
            sort_keys=True,
        )
    )


def calibrate() -> None:
    print(batch100._calibrate_one(RUN_DIR, StateStore(RUN_DIR), _spec(), 0))


def _select_candidate(new_ref: str) -> None:
    binding_path = RUN_DIR / "bindings" / f"{CASE_ID}.json"
    binding = batch100.read_json(binding_path, "manual Case001 binding")
    matches = []
    for candidate in binding["calibration_candidates"]:
        operations = candidate["injection_operations"]
        if (
            len(operations) == 1
            and operations[0]["instance"] == "u_exp/U434"
            and operations[0]["baseline_ref"] == "XOR2_X2M_A9TR40"
            and operations[0]["candidate_refs_nearest_first"] == [new_ref]
        ):
            matches.append(candidate)
    if len(matches) != 1:
        raise batch100.BatchError(
            f"expected exactly one U434 candidate for {new_ref}, got {len(matches)}"
        )
    selected = matches[0]
    narrowed = {
        **binding,
        "injection_operations": selected["injection_operations"],
        "repair_operations": selected["repair_operations"],
        "calibration_candidates": [selected],
    }
    store = StateStore(RUN_DIR)
    if store.row(CASE_ID)["state"] != "BOUND":
        raise batch100.BatchError("candidate selection requires BOUND state")
    store.transition(CASE_ID, "PROBE_ELIGIBLE", f"select measured {new_ref} candidate")
    batch100.atomic_json(binding_path, narrowed, durable=True)
    store.transition(
        CASE_ID,
        "BOUND",
        f"freeze measured {new_ref} candidate",
        binding_sha256=batch100.sha256_file(binding_path),
    )
    print(
        json.dumps(
            {
                "status": "BOUND",
                "selected_ref": new_ref,
                "binding_sha256": batch100.sha256_file(binding_path),
            },
            sort_keys=True,
        )
    )


def select_x0p5() -> None:
    _select_candidate("XOR2_X0P5M_A9TR40")


def select_x0p7() -> None:
    _select_candidate("XOR2_X0P7M_A9TR40")


def select_x1() -> None:
    _select_candidate("XOR2_X1M_A9TR40")


def select_x1p4() -> None:
    _select_candidate("XOR2_X1P4M_A9TR40")


def repair_binding() -> None:
    manifest = _manifest()
    probe = batch100._verified_frozen_probe(RUN_DIR, manifest["baseline"])
    binding = _forced_binding(probe, _spec())
    binding_path = RUN_DIR / "bindings" / f"{CASE_ID}.json"
    store = StateStore(RUN_DIR)
    if store.row(CASE_ID)["state"] != "BOUND":
        raise batch100.BatchError("binding repair requires pristine BOUND state")
    store.transition(
        CASE_ID,
        "PROBE_ELIGIBLE",
        "replace full-tree probe hash with frozen pre-manifest hash",
    )
    batch100.atomic_json(binding_path, binding, durable=True)
    manifest["probe_tree_sha256"] = probe["tree_sha256"]
    batch100.atomic_json(RUN_DIR / "run_manifest.json", manifest, durable=True)
    store.transition(
        CASE_ID,
        "BOUND",
        "correct frozen-probe binding hash",
        binding_sha256=batch100.sha256_file(binding_path),
    )
    print(
        json.dumps(
            {
                "status": "BOUND",
                "probe_tree_sha256": probe["tree_sha256"],
                "binding_sha256": batch100.sha256_file(binding_path),
            },
            sort_keys=True,
        )
    )


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
    row = store.row(CASE_ID)
    print(json.dumps(row, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=(
            "init",
            "repair_binding",
            "select_x0p5",
            "select_x0p7",
            "select_x1",
            "select_x1p4",
            "calibrate",
            "replay",
            "validate",
            "status",
        ),
    )
    args = parser.parse_args()
    globals()[args.command]()


if __name__ == "__main__":
    main()

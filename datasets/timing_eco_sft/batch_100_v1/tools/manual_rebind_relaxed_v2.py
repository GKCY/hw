#!/usr/bin/env python3
"""Manually rebind one relaxed card to frozen-probe resize candidates.

This operator tool changes only the selected case binding.  It preserves a
rejected attempt through the ordinary rebind audit path and leaves calibration,
checkpoint freezing, replay, and final validation to the batch driver.
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
from pathlib import Path
from typing import Any


TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import batch100  # noqa: E402
from common import BatchError, read_json  # noqa: E402


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def _equivalent_ladders(
    rows: list[dict[str, str]],
) -> tuple[dict[str, list[tuple[float, str]]], dict[str, float]]:
    """Build RVT ladders by frozen logical signature, including A/B/M flavor.

    The catalog permits drive-strength and A/B/M flavor changes within the
    available RVT collateral.  The ordinary binder groups identical flavors;
    a supervised manual card may use the broader frozen-probe equivalence set
    while retaining the direct-inverse requirement.
    """

    signatures: dict[tuple[str, str], set[tuple[float, str]]] = {}
    source_signature: dict[str, tuple[str, str]] = {}
    source_drive: dict[str, float] = {}
    for row in rows:
        if row.get("equivalent", "").lower() != "true":
            continue
        signature = row.get("source_signature", "")
        if not signature or row.get("variant_signature") != signature:
            continue
        source_ref = row["source_ref"]
        variant_ref = row["variant_ref"]
        try:
            baseline_drive = float(row["source_drive"])
            variant_drive = float(row["variant_drive"])
        except ValueError:
            continue
        key = (row.get("stem", ""), signature)
        if not key[0]:
            continue
        source_signature[source_ref] = key
        source_drive.setdefault(source_ref, baseline_drive)
        source_drive.setdefault(variant_ref, variant_drive)
        signatures.setdefault(key, set()).add((variant_drive, variant_ref))
    ladders = {
        source_ref: sorted(signatures.get(key, set()))
        for source_ref, key in source_signature.items()
    }
    return ladders, source_drive


def _case_spec(case_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    specs = read_json(batch100.SPECS_PATH, "relaxed case specs")
    if specs.get("schema_version") not in batch100.RELAXED_SCHEMAS:
        raise BatchError("manual rebind requires a supported relaxed specification")
    by_id = {case["id"]: case for case in specs["cases"]}
    try:
        return specs, by_id[case_id]
    except KeyError as exc:
        raise BatchError(f"unknown case ID: {case_id}") from exc


def _recover_interrupted_calibration(
    run_dir: Path,
    store: Any,
    case_id: str,
    available_slots: list[int],
) -> None:
    """Archive a known failed controller leaf after proving Innovus is idle."""

    run_id = batch100._run_id(run_dir)
    remote_task = f"{batch100.GUEST_RUN_ROOT}/{run_id}/tasks/{case_id}"
    for slot in available_slots:
        result = batch100._ssh(
            slot,
            (
                "if ps -eo args= | awk "
                f"'index($0, \"{remote_task}\") && /[i]nnovus/ {{found=1}} "
                "END {exit found ? 0 : 1}'; then exit 9; else exit 0; fi"
            ),
            timeout=30,
        )
        if result.returncode:
            raise BatchError(
                f"{case_id}: slot{slot} still has an Innovus process for this task"
            )
    batch100._clear_remote_retry_leaves(available_slots, run_id, case_id)
    row = store.row(case_id)
    recovery = (
        run_dir
        / "manual_recovery"
        / case_id
        / f"attempt_{int(row['attempt']):03d}"
    )
    if recovery.exists() or recovery.is_symlink():
        raise BatchError(f"{case_id}: manual recovery leaf already exists")
    recovery.mkdir(parents=True)
    binding_path = run_dir / "bindings" / f"{case_id}.json"
    shutil.copy2(binding_path, recovery / "binding.json")
    job = run_dir / "jobs" / case_id
    if not job.is_dir() or job.is_symlink():
        raise BatchError(f"{case_id}: interrupted calibration job is missing")
    shutil.move(str(job), str(recovery / "job"))
    batch100.atomic_json(
        recovery / "recovery.json",
        {
            "schema_version": "mock_lef_batch100.manual_recovery.v1",
            "case_id": case_id,
            "attempt": int(row["attempt"]),
            "reason": "operator tool metadata failure before checkpoint freeze",
            "remote_slots_cleaned": [
                f"slot{slot}" for slot in sorted(available_slots)
            ],
        },
        durable=True,
    )
    batch100._release_rejected_case_fingerprint_claims(run_dir, case_id)
    store.transition(
        case_id,
        "PROBE_ELIGIBLE",
        "failed operator binding archived; no live Innovus process remained",
        slot=None,
        injection_sha256=None,
        repair_sha256=None,
    )


def _candidate(
    *,
    target: str,
    point: dict[str, str],
    requested_ref: str,
    ladders: dict[str, list[tuple[float, str]]],
    source_drive: dict[str, float],
    reachable: set[str],
) -> dict[str, Any]:
    instance = point["inst"]
    baseline_ref = point["ref"]
    try:
        drive = source_drive[baseline_ref]
    except KeyError as exc:
        raise BatchError(f"{instance}: baseline reference lacks a drive ladder") from exc
    legal = [
        (variant_drive, reference)
        for variant_drive, reference in ladders.get(baseline_ref, [])
        if reference != baseline_ref
    ]
    legal.sort(reverse=True)
    legal_by_ref = {reference: variant_drive for variant_drive, reference in legal}
    if requested_ref not in legal_by_ref:
        raise BatchError(
            f"{instance}: requested reference is not an equivalent "
            f"RVT cell: {requested_ref}"
        )
    delay = float(point.get("delay_ns") or 0.0)

    def estimates(items: list[tuple[float, str]]) -> list[float]:
        return [delay * (drive / variant_drive - 1.0) for variant_drive, _ in items]

    requested = [(legal_by_ref[requested_ref], requested_ref)]
    legal_refs = [reference for _, reference in legal]
    legal_estimates = estimates(legal)
    requested_estimates = estimates(requested)
    return {
        "instance": instance,
        "baseline_ref": baseline_ref,
        "new_ref": requested_ref,
        "candidate_refs_nearest_first": [requested_ref],
        "candidate_estimated_slowdown_ns": requested_estimates,
        "candidate_estimated_slowdown_by_endpoint_ns": {
            target: requested_estimates
        },
        "legal_down_refs_nearest_first": legal_refs,
        "legal_down_estimated_slowdown_ns": legal_estimates,
        "legal_down_estimated_slowdown_by_endpoint_ns": {
            target: legal_estimates
        },
        "selected_legal_down_level": legal_refs.index(requested_ref),
        "reachable_endpoints": sorted(reachable),
        "point_index": int(point["point_index"]),
    }


def prepare(
    run_dir: Path,
    case_id: str,
    target: str,
    raw_candidates: list[list[str]],
) -> None:
    store, manifest = batch100._verified_store(run_dir)
    specs, spec = _case_spec(case_id)
    row = store.row(case_id)
    if row["state"] not in {"BOUND", "PROBE_ELIGIBLE", "CALIBRATING"}:
        raise BatchError(
            f"{case_id}: manual rebind requires BOUND, PROBE_ELIGIBLE, "
            f"or an audited CALIBRATING recovery, "
            f"got {row['state']}"
        )
    if row["state"] == "CALIBRATING":
        available_slots = batch100._verify_execution_slots(manifest["baseline"])
        _recover_interrupted_calibration(
            run_dir, store, case_id, available_slots
        )
    elif row["state"] == "PROBE_ELIGIBLE":
        available_slots = batch100._verify_execution_slots(manifest["baseline"])
        batch100._rebind_probe_eligible_case(
            run_dir, store, manifest, specs, case_id, available_slots
        )
    elif (run_dir / "jobs" / case_id).exists():
        raise BatchError(f"{case_id}: BOUND state unexpectedly has a local job leaf")

    probe = batch100._verified_frozen_probe(run_dir, manifest["baseline"])
    path_matches = [
        path
        for path in probe["paths"]
        if path["endpoint"] == target
        and spec["hierarchy"] in path["hierarchy_groups"].split(",")
    ]
    if not path_matches:
        raise BatchError(
            f"{case_id}: target {target} is absent from hierarchy {spec['hierarchy']}"
        )
    target_path = min(
        path_matches, key=lambda path: (float(path["slack_ns"]), int(path["stable_rank"]))
    )
    if (
        float(target_path["cell_delay_fraction"]) < 0.70
        or float(target_path["net_delay_fraction"]) > 0.30
    ):
        raise BatchError(f"{case_id}: target path violates the G0 delay fractions")

    rank = target_path["stable_rank"]
    points = {
        point["inst"]: point
        for point in probe["points"]
        if point["stable_rank"] == rank
        and point["delay_kind"] == "cell"
        and point["inst"]
        and point["sequential"].lower() not in {"1", "true", "yes"}
        and point["clock"].lower() not in {"1", "true", "yes"}
        and point["dont_touch"].lower() not in {"1", "true", "yes"}
    }
    reachability = {
        row["instance"]: {
            endpoint for endpoint in row["endpoints"].split(",") if endpoint
        }
        for row in probe["reachability"]
    }
    ladders, source_drive = _equivalent_ladders(probe["ladders"])

    candidates = []
    for words in raw_candidates:
        instance, *requested_refs = words
        try:
            point = points[instance]
        except KeyError as exc:
            raise BatchError(
                f"{case_id}: {instance} is not a legal cell point on target rank {rank}"
            ) from exc
        reachable = reachability.get(instance, set())
        if reachable != {target}:
            raise BatchError(
                f"{case_id}: {instance} is not endpoint-local to {target}: "
                + ", ".join(sorted(reachable))
            )
        if not requested_refs:
            baseline_ref = point["ref"]
            drive = source_drive.get(baseline_ref)
            requested_refs = [
                reference
                for variant_drive, reference in ladders.get(baseline_ref, [])
                if drive is not None and reference != baseline_ref
            ]
            requested_refs.reverse()
        if not requested_refs:
            raise BatchError(
                f"{case_id}: {instance} has no legal equivalent RVT ref"
            )
        if len(requested_refs) != 1:
            raise BatchError(
                f"{case_id}: each --candidate must name exactly one seed ref; "
                "the calibrator explores its frozen legal ladder"
            )
        requested_ref = requested_refs[0]
        operation = _candidate(
            target=target,
            point=point,
            requested_ref=requested_ref,
            ladders=ladders,
            source_drive=source_drive,
            reachable=reachable,
        )
        repair = {
            "instance": instance,
            "checkpoint_ref": requested_ref,
            "new_ref": operation["baseline_ref"],
            "candidate_refs_nearest_first": [operation["baseline_ref"]],
        }
        candidates.append(
            {
                "candidate_index": len(candidates),
                "injection_operations": [operation],
                "repair_operations": [repair],
            }
        )
    if not candidates:
        raise BatchError(f"{case_id}: at least one --candidate is required")

    first = candidates[0]
    binding = {
        "schema_version": "mock_lef_batch100.binding.v1",
        "case_id": case_id,
        "probe_tree_sha256": probe["tree_sha256"],
        "target_endpoints": [target],
        "target_baseline_slacks_ns": {target: float(target_path["slack_ns"])},
        "protected_endpoints": [],
        "target_path_ranks": [int(rank)],
        "path_fractions": [
            {
                "endpoint": target,
                "cell_delay_fraction": float(target_path["cell_delay_fraction"]),
                "net_delay_fraction": float(target_path["net_delay_fraction"]),
            }
        ],
        "injection_operations": first["injection_operations"],
        "repair_operations": first["repair_operations"],
        "calibration_candidates": candidates,
        "planning_contract": {
            key: spec[key]
            for key in (
                "shape",
                "strategy",
                "difficulty",
                "expected_modification_count",
                "hierarchy",
                "injection_profile",
                "guardrail_axis",
                "topology_tags",
            )
        },
    }
    binding_path = run_dir / "bindings" / f"{case_id}.json"
    if store.row(case_id)["state"] == "BOUND":
        store.transition(
            case_id,
            "PROBE_ELIGIBLE",
            "operator selected frozen-probe endpoint-local candidates",
            slot=None,
        )
    batch100.atomic_json(binding_path, binding, durable=True)
    store.transition(
        case_id,
        "BOUND",
        "manual relaxed binding frozen for real-tool calibration",
        slot=None,
        binding_sha256=batch100.sha256_file(binding_path),
        injection_sha256=None,
        repair_sha256=None,
    )
    print(
        f"{case_id}: BOUND target={target} rank={rank} "
        f"candidates={len(candidates)} binding_sha256="
        f"{batch100.sha256_file(binding_path)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument(
        "--candidate",
        action="append",
        nargs="+",
        metavar=("INSTANCE", "LOWER_REF"),
        required=True,
        help=(
            "repeat for alternatives; name one seed ref, or omit it to use "
            "the nearest lower ref (refinement retains the full legal ladder)"
        ),
    )
    args = parser.parse_args()
    prepare(args.run_dir, args.case_id, args.target, args.candidate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

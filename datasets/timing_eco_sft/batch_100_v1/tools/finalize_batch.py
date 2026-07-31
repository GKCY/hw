#!/usr/bin/env python3
"""Strict dual-replay finalizer for Mock LEF Batch-100."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

import checkpoint_archive
import evidence as raw_evidence
from common import (
    BatchError,
    atomic_json,
    atomic_write,
    parse_fix_tcl,
    read_json,
    require_bool,
    require_hash,
    require_list,
    require_mapping,
    require_number,
    require_real_directory,
    require_real_file,
    same_number,
    sha256_file,
)


HERE = Path(__file__).resolve()
ROOT = HERE.parents[1]
SPECS_PATH = Path(os.environ.get("B100_SPECS_PATH", ROOT / "case_specs.json"))
CHECKPOINT_SUFFIXES = (".enc", ".enc.dat")
TRANSPORT_BUNDLE_SUFFIXES = (".tar", ".tgz", ".tar.gz")
CASE_ARTIFACT_TREE_HASH_ALGORITHM = "sha256-case-artifact-tree-v2"
SYSTEM_PROMPT = (
    "你是一名资深 Cadence Innovus 21.10 物理设计工程师。请仅依据给出的"
    " violating checkpoint 可观察证据诊断 setup 违例，并给出最小化、可重放的 "
    "RVT 组合逻辑 resize ECO。只允许 ecoChangeCell 与 refinePlace -eco true；"
    "不得修改约束、时钟、顺序单元、clock-gating、macro、连接或路由。"
)


def _case_artifact_ledger(case_root: Path) -> list[dict[str, Any]]:
    """Ledger a case while allowing safe links only in its checkpoint tree."""

    require_real_directory(case_root, "case artifact tree")
    rows: list[dict[str, Any]] = []
    stack = [case_root]
    while stack:
        directory = stack.pop()
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            mode = path.lstat().st_mode
            relative = path.relative_to(case_root).as_posix()
            if stat.S_ISDIR(mode):
                stack.append(path)
            elif stat.S_ISREG(mode):
                rows.append(
                    {
                        "path": relative,
                        "type": "file",
                        "bytes": path.stat().st_size,
                        "sha256": sha256_file(path),
                    }
                )
            elif stat.S_ISLNK(mode):
                if not relative.startswith("violating.enc.dat/"):
                    raise BatchError(f"case artifact symlink outside checkpoint: {path}")
                target = os.readlink(path)
                checkpoint_archive.validate_symlink_target(
                    Path(relative).relative_to("violating.enc.dat"),
                    target,
                    checkpoint_archive_baseline_prefix(),
                )
                rows.append(
                    {"path": relative, "type": "symlink", "target": target}
                )
            else:
                raise BatchError(f"case artifact tree contains special file: {path}")
    rows.sort(key=lambda row: row["path"])
    return rows


def _ledger_sha256(rows: Sequence[Mapping[str, Any]]) -> str:
    encoded = json.dumps(
        list(rows), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(b"sha256-case-artifact-tree-v2\n" + encoded).hexdigest()


def case_artifact_tree_sha256(case_root: Path) -> str:
    """Hash one retained case tree, including safe checkpoint symlinks."""

    return _ledger_sha256(_case_artifact_ledger(case_root))


def checkpoint_archive_baseline_prefix() -> str:
    # Fixed by the harness; never accept a run-controlled allowlist prefix.
    return checkpoint_archive.GUEST_BASELINE_PREFIX


def _reject_checkpoint_residue(
    root: Path, *, allowed_checkpoint_paths: Sequence[Path] = ()
) -> None:
    """Allow only canonical retained checkpoints and no transport bundles."""

    require_real_directory(root, "checkpoint residue scan root")
    allowed = set(allowed_checkpoint_paths)
    for path in root.rglob("*"):
        lower_name = path.name.lower()
        if lower_name.endswith(TRANSPORT_BUNDLE_SUFFIXES):
            raise BatchError(f"transient checkpoint transport bundle retained: {path}")
        if lower_name.endswith(CHECKPOINT_SUFFIXES) and path not in allowed:
            raise BatchError(f"non-canonical checkpoint retained: {path}")


def _objects(value: Any, label: str) -> list[Mapping[str, Any]]:
    result = require_list(value, label)
    for index, item in enumerate(result):
        require_mapping(item, f"{label}[{index}]")
    return result


def _operation_fingerprint(operations: Sequence[Mapping[str, Any]]) -> str:
    """Match the scheduler's canonical frozen-operation fingerprint."""

    return hashlib.sha256(
        json.dumps(operations, sort_keys=True).encode()
    ).hexdigest()


def _frozen_operations(
    plan: Mapping[str, Any],
    spec: Mapping[str, Any],
    fix_hash: str,
) -> tuple[list[dict[str, str]], list[dict[str, str]], str, str]:
    """Validate the hidden plan and return its canonical operation ledgers."""

    case_id = spec["id"]
    if plan.get("schema_version") != "mock_lef_batch100.calibration_plan.v1":
        raise BatchError(f"{case_id}: unsupported frozen calibration plan schema")
    if plan.get("case_id") != case_id:
        raise BatchError(f"{case_id}: frozen calibration plan identity mismatch")
    if plan.get("fix_sha256") != fix_hash:
        raise BatchError(f"{case_id}: frozen calibration plan fix hash mismatch")
    for name in (
        "binding_sha256",
        "calibration_tcl_sha256",
        "config_sha256",
        "calibration_tree_sha256",
    ):
        require_hash(plan.get(name), f"{case_id}.calibration_plan.{name}")

    def ledger(name: str, kind: str) -> list[dict[str, str]]:
        raw = _objects(plan.get(name), f"{case_id}.calibration_plan.{name}")
        result: list[dict[str, str]] = []
        for index, operation in enumerate(raw):
            label = f"{case_id}.calibration_plan.{name}[{index}]"
            if set(operation) != {"kind", "instance", "old_ref", "new_ref"}:
                raise BatchError(f"{label}: unexpected/missing operation fields")
            if operation.get("kind") != kind:
                raise BatchError(f"{label}: wrong operation kind")
            row: dict[str, str] = {}
            for key in ("kind", "instance", "old_ref", "new_ref"):
                value = operation.get(key)
                if not isinstance(value, str) or not value:
                    raise BatchError(f"{label}.{key}: must be a non-empty string")
                row[key] = value
            if row["old_ref"] == row["new_ref"]:
                raise BatchError(f"{label}: ineffective resize")
            result.append(row)
        instances = [row["instance"] for row in result]
        if len(instances) != len(set(instances)):
            raise BatchError(f"{case_id}: frozen {kind} instance is repeated")
        return result

    injection = ledger("injection_operations", "injection")
    repair = ledger("repair_operations", "repair")
    expected_count = spec["expected_modification_count"]
    if len(repair) != expected_count:
        raise BatchError(f"{case_id}: frozen repair count mismatch")
    if not injection:
        raise BatchError(f"{case_id}: frozen injection ledger is empty")

    injection_by_instance = {row["instance"]: row for row in injection}
    repair_by_instance = {row["instance"]: row for row in repair}
    injection_instances = set(injection_by_instance)
    repair_instances = set(repair_by_instance)
    if spec["injection_profile"] == "I0":
        if (
            len(injection) != expected_count
            or injection_instances != repair_instances
        ):
            raise BatchError(f"{case_id}: I0 is not the exact direct-inverse set")
        for instance in injection_instances:
            injected = injection_by_instance[instance]
            repaired = repair_by_instance[instance]
            if (
                repaired["old_ref"] != injected["new_ref"]
                or repaired["new_ref"] != injected["old_ref"]
            ):
                raise BatchError(
                    f"{case_id}: I0 refs are not an exact direct inverse for "
                    f"{instance}"
                )
    else:
        if (
            injection_instances == repair_instances
            or not injection_instances - repair_instances
            or not repair_instances - injection_instances
        ):
            raise BatchError(
                f"{case_id}: IA-ID repair/injection sets violate non-inverse policy"
            )
        inverse_ref_pairs = {
            (row["new_ref"], row["old_ref"]) for row in injection
        }
        if any(
            (row["old_ref"], row["new_ref"]) in inverse_ref_pairs
            for row in repair
        ):
            raise BatchError(
                f"{case_id}: IA-ID contains a ref-level direct inverse"
            )

    return (
        injection,
        repair,
        _operation_fingerprint(injection),
        _operation_fingerprint(repair),
    )


def _strings(value: Any, label: str) -> list[str]:
    result = require_list(value, label)
    if any(not isinstance(item, str) or not item for item in result):
        raise BatchError(f"{label} must contain non-empty strings")
    return result


def _zero(value: Any, label: str) -> None:
    if abs(require_number(value, label)) > 1e-12:
        raise BatchError(f"{label} must be zero")


def _nonnegative(value: Any, label: str) -> None:
    if require_number(value, label) < -1e-12:
        raise BatchError(f"{label} must be nonnegative")


def _stage(replay: Mapping[str, Any], name: str, label: str) -> Mapping[str, Any]:
    return require_mapping(replay.get(name), f"{label}.{name}")


def _validate_target_paths(
    before: Mapping[str, Any], targets: Sequence[str], acceptance: Mapping[str, Any], label: str
) -> None:
    paths = require_mapping(before.get("target_paths"), f"{label}.target_paths")
    if set(paths) != set(targets):
        raise BatchError(f"{label}.target_paths does not exactly cover targets")
    for endpoint in targets:
        evidence = require_mapping(paths[endpoint], f"{label}.target_paths[{endpoint}]")
        cell = require_number(evidence.get("cell_delay_ns"), f"{label}.{endpoint}.cell")
        net = require_number(evidence.get("net_delay_ns"), f"{label}.{endpoint}.net")
        path_slack = require_number(
            evidence.get("slack_ns"), f"{label}.{endpoint}.slack"
        )
        denominator = cell + net
        if denominator <= 0:
            raise BatchError(f"{label}.{endpoint}: nonpositive data-delay denominator")
        if cell / denominator + 1e-12 < acceptance["cell_delay_fraction_min"]:
            raise BatchError(f"{label}.{endpoint}: cell-delay fraction below 0.70")
        if net / denominator - 1e-12 > acceptance["net_delay_fraction_max"]:
            raise BatchError(f"{label}.{endpoint}: net-delay fraction above 0.30")
        target_slacks = require_mapping(
            before.get("target_slacks_ns"), f"{label}.target_slacks_ns"
        )
        same_number(
            path_slack,
            target_slacks.get(endpoint),
            0.001,
            f"{label}.{endpoint}.path/setup slack",
        )


def _validate_raw_binding(
    expected: Any, log_path: Path, reports: Path, label: str
) -> Mapping[str, Any]:
    binding = require_mapping(expected, f"{label}.raw_evidence")
    actual = raw_evidence.raw_evidence_binding(log_path, reports)
    if binding != actual:
        raise BatchError(f"{label}: raw Innovus log/reports binding mismatch")
    return binding


def _compare_numeric_map(
    expected: Any, actual: Any, tolerance_ns: float, label: str
) -> None:
    left = require_mapping(expected, f"{label}.recorded")
    right = require_mapping(actual, f"{label}.raw")
    if set(left) != set(right):
        raise BatchError(f"{label}: endpoint/key sets differ")
    for key in left:
        same_number(left[key], right[key], tolerance_ns, f"{label}.{key}")


def _compare_raw_stage(
    recorded: Mapping[str, Any],
    rebuilt: Mapping[str, Any],
    tolerance_ns: float,
    label: str,
) -> None:
    for key in ("setup_wns_ns", "setup_tns_ns", "hold_wns_ns", "hold_tns_ns"):
        if key in rebuilt:
            same_number(
                recorded.get(key), rebuilt.get(key), tolerance_ns, f"{label}.{key}"
            )
    for key in ("negative_endpoints", "drv"):
        if recorded.get(key) != rebuilt.get(key):
            raise BatchError(f"{label}: raw {key} differs from validation")
    for key in (
        "drc_count",
        "connectivity_violations",
        "constraint_sha256",
        "pin_net_connectivity_sha256",
        "instance_set_sha256",
        "topology_sha256",
        "routing_mutations",
    ):
        if key in rebuilt and recorded.get(key) != rebuilt.get(key):
            raise BatchError(f"{label}: raw {key} differs from validation")
    _compare_numeric_map(
        recorded.get("target_slacks_ns"),
        rebuilt.get("target_slacks_ns"),
        tolerance_ns,
        f"{label}.target_slacks_ns",
    )
    if "target_paths" in rebuilt:
        expected_paths = require_mapping(
            recorded.get("target_paths"), f"{label}.target_paths"
        )
        actual_paths = require_mapping(
            rebuilt.get("target_paths"), f"{label}.raw_target_paths"
        )
        if set(expected_paths) != set(actual_paths):
            raise BatchError(f"{label}: raw target-path endpoint sets differ")
        for endpoint in expected_paths:
            _compare_numeric_map(
                expected_paths[endpoint],
                actual_paths[endpoint],
                tolerance_ns,
                f"{label}.target_paths.{endpoint}",
            )


def _validate_replay_raw_evidence(
    replay: Mapping[str, Any],
    raw_root: Path,
    targets: Sequence[str],
    protected: Sequence[str],
    operations: Sequence[Mapping[str, str]],
    tolerance_ns: float,
    label: str,
) -> dict[str, Any]:
    log_path = raw_root / "innovus.log"
    reports = raw_root / "reports"
    binding = _validate_raw_binding(
        replay.get("raw_evidence"), log_path, reports, label
    )
    if replay.get("tool_log_sha256") != binding.get("tool_log_sha256"):
        raise BatchError(f"{label}: tool log hash is not bound to raw replay log")

    recorded_portability = require_mapping(
        replay.get("checkpoint_portability_slacks"),
        f"{label}.checkpoint_portability_slacks",
    )
    rebuilt_portability = raw_evidence.checkpoint_portability_evidence(
        reports, targets, tolerance_ns
    )
    for key in (
        "schema_version",
        "endpoint_count",
        "negative_endpoints",
        "raw_tsv_sha256",
        "canonical_map_sha256",
    ):
        if recorded_portability.get(key) != rebuilt_portability.get(key):
            raise BatchError(f"{label}: portability {key} differs from raw evidence")
    _compare_numeric_map(
        recorded_portability.get("slacks_ns"),
        rebuilt_portability.get("slacks_ns"),
        tolerance_ns,
        f"{label}.portability_slacks",
    )

    rebuilt_before = raw_evidence.stage(reports, "before", targets)
    rebuilt_after_resize = raw_evidence.resize_stage(reports, targets)
    rebuilt_after = raw_evidence.stage(reports, "after_legalize", targets)
    before_cells = rebuilt_before.pop("_cells")
    after_cells = rebuilt_after.pop("_cells")
    _compare_raw_stage(
        _stage(replay, "before", label),
        rebuilt_before,
        tolerance_ns,
        f"{label}.before",
    )
    _compare_raw_stage(
        _stage(replay, "after_resize", label),
        rebuilt_after_resize,
        tolerance_ns,
        f"{label}.after_resize",
    )
    _compare_raw_stage(
        _stage(replay, "after_legalize", label),
        rebuilt_after,
        tolerance_ns,
        f"{label}.after_legalize",
    )
    if require_bool(replay.get("placement_legal"), f"{label}.placement_legal") != (
        raw_evidence.placement_legal(reports / "placement_after_legalize.rpt")
    ):
        raise BatchError(f"{label}: placement result differs from raw report")

    setup_before = raw_evidence.timing(reports / "setup_before.rpt")["slacks"]
    setup_after = raw_evidence.timing(reports / "setup_after_legalize.rpt")["slacks"]
    _compare_numeric_map(
        replay.get("protected_baseline_slacks_ns"),
        {endpoint: setup_before[endpoint] for endpoint in protected},
        tolerance_ns,
        f"{label}.protected_baseline",
    )
    _compare_numeric_map(
        replay.get("protected_after_slacks_ns"),
        {endpoint: setup_after[endpoint] for endpoint in protected},
        tolerance_ns,
        f"{label}.protected_after",
    )

    if set(before_cells) != set(after_cells):
        raise BatchError(f"{label}: raw cell snapshots changed instance set")
    measured_diff = sorted(
        (instance, before_cells[instance], after_cells[instance])
        for instance in before_cells
        if before_cells[instance] != after_cells[instance]
    )
    recorded_diff = sorted(
        (item.get("instance"), item.get("old_ref"), item.get("new_ref"))
        for item in _objects(replay.get("cell_diff"), f"{label}.cell_diff")
    )
    expected_instances = {row["instance"] for row in operations}
    if not expected_instances <= set(before_cells):
        raise BatchError(f"{label}: repair instance missing from raw cell snapshots")
    expected_diff = sorted(
        (row["instance"], row["old_ref"], row["new_ref"])
        for row in operations
    )
    if measured_diff != recorded_diff or measured_diff != expected_diff:
        raise BatchError(f"{label}: raw cell snapshots do not prove frozen cell diff")
    return rebuilt_portability


def _validate_stage_closure(stage: Mapping[str, Any], targets: Sequence[str], label: str) -> None:
    _zero(stage.get("setup_tns_ns"), f"{label}.setup_tns_ns")
    if _strings(stage.get("negative_endpoints"), f"{label}.negative_endpoints"):
        raise BatchError(f"{label}: has negative endpoints")
    slacks = require_mapping(stage.get("target_slacks_ns"), f"{label}.target_slacks_ns")
    if set(slacks) != set(targets):
        raise BatchError(f"{label}: target slack map mismatch")
    for endpoint in targets:
        _nonnegative(slacks[endpoint], f"{label}.{endpoint}.slack")


def _validate_no_regression(
    before: Mapping[str, Any], after: Mapping[str, Any], label: str
) -> None:
    before_drv = require_mapping(before.get("drv"), f"{label}.before.drv")
    after_drv = require_mapping(after.get("drv"), f"{label}.after.drv")
    for name in ("max_transition", "max_capacitance"):
        if int(after_drv.get(name, -1)) > int(before_drv.get(name, -1)):
            raise BatchError(f"{label}: {name} regressed")
    if int(after_drv.get("max_fanout", -1)) != int(before_drv.get("max_fanout", -2)):
        raise BatchError(f"{label}: max_fanout count changed")
    if int(after.get("drc_count", -1)) > int(before.get("drc_count", -1)):
        raise BatchError(f"{label}: DRC regressed")
    if int(after.get("connectivity_violations", -1)) > int(
        before.get("connectivity_violations", -1)
    ):
        raise BatchError(f"{label}: connectivity regressed")
    for key in ("constraint_sha256", "pin_net_connectivity_sha256", "instance_set_sha256"):
        if before.get(key) != after.get(key):
            raise BatchError(f"{label}: {key} changed")


def _validate_replay(
    replay: Mapping[str, Any],
    index: int,
    case_root: Path,
    spec: Mapping[str, Any],
    fix_hash: str,
    operations: Sequence[Mapping[str, str]],
    checkpoint_hash: str,
    checkpoint_tree_hash: str,
    checkpoint_bundle_hash: str,
    acceptance: Mapping[str, Any],
) -> dict[str, Any]:
    label = f"{spec['id']}.replay_{index}"
    if replay.get("schema_version") != "mock_lef_batch100.replay_validation.v2":
        raise BatchError(f"{label}: unsupported schema")
    if replay.get("status") != "PASS":
        raise BatchError(f"{label}: status is not PASS")
    require_hash(replay.get("tool_log_sha256"), f"{label}.tool_log_sha256")
    if replay.get("tool_version") != "21.10-p004_1":
        raise BatchError(f"{label}: wrong Innovus version")
    if replay.get("fix_sha256") != fix_hash:
        raise BatchError(f"{label}: fix hash mismatch")
    if replay.get("violating_checkpoint_sha256") != checkpoint_hash:
        raise BatchError(f"{label}: checkpoint wrapper hash mismatch")
    if replay.get("violating_checkpoint_tree_sha256") != checkpoint_tree_hash:
        raise BatchError(f"{label}: checkpoint tree hash mismatch")
    if (
        replay.get("violating_checkpoint_tree_hash_algorithm")
        != checkpoint_archive.CHECKPOINT_TREE_HASH_ALGORITHM
    ):
        raise BatchError(f"{label}: unsupported checkpoint tree hash algorithm")
    if replay.get("violating_checkpoint_bundle_sha256") != checkpoint_bundle_hash:
        raise BatchError(f"{label}: checkpoint bundle hash mismatch")
    if (
        replay.get("violating_checkpoint_bundle_hash_algorithm")
        != checkpoint_archive.CHECKPOINT_BUNDLE_HASH_ALGORITHM
    ):
        raise BatchError(f"{label}: unsupported checkpoint bundle hash algorithm")
    if require_bool(replay.get("hold_gate_applied"), f"{label}.hold_gate_applied"):
        raise BatchError(f"{label}: hold was incorrectly used as an acceptance gate")
    if not require_bool(replay.get("hold_recorded"), f"{label}.hold_recorded"):
        raise BatchError(f"{label}: hold was not recorded")
    if not require_bool(replay.get("placement_legal"), f"{label}.placement_legal"):
        raise BatchError(f"{label}: placement is not legal")
    if not require_bool(
        replay.get("functional_equivalence_pass"),
        f"{label}.functional_equivalence_pass",
    ):
        raise BatchError(f"{label}: functional/pin/RVT equivalence failed")

    targets = _strings(replay.get("target_endpoints"), f"{label}.target_endpoints")
    protected = _strings(
        replay.get("protected_endpoints"), f"{label}.protected_endpoints"
    )
    if len(targets) != spec["target_endpoint_count"] or len(set(targets)) != len(targets):
        raise BatchError(f"{label}: target endpoint cardinality mismatch")
    if len(protected) != spec["protected_endpoint_count"] or set(targets) & set(protected):
        raise BatchError(f"{label}: protected endpoint cardinality/overlap mismatch")

    portability = _validate_replay_raw_evidence(
        replay,
        case_root / "evidence" / f"replay_{index}",
        targets,
        protected,
        operations,
        acceptance["replay_numeric_tolerance_ps"] / 1000.0,
        label,
    )
    before = _stage(replay, "before", label)
    after_resize = _stage(replay, "after_resize", label)
    after = _stage(replay, "after_legalize", label)
    negatives = _strings(before.get("negative_endpoints"), f"{label}.before.negative_endpoints")
    if set(negatives) != set(targets) or len(negatives) != len(targets):
        raise BatchError(f"{label}: injected negative set is not exactly the target set")
    nominal_ns = -spec["nominal_severity"]["ps"] / 1000.0
    tolerance_ns = spec["nominal_severity"]["tolerance_ps"] / 1000.0
    same_number(before.get("setup_wns_ns"), nominal_ns, tolerance_ns, f"{label}.severity")
    _validate_target_paths(before, targets, acceptance, f"{label}.before")
    _validate_stage_closure(after_resize, targets, f"{label}.after_resize")
    _validate_stage_closure(after, targets, f"{label}.after_legalize")
    _validate_no_regression(before, after, label)
    if after_resize.get("routing_mutations") not in (0, False):
        raise BatchError(f"{label}: resize closure depends on routing")
    if after.get("routing_mutations") not in (0, False):
        raise BatchError(f"{label}: legalization changed routing")
    if before.get("topology_sha256") != after.get("topology_sha256"):
        raise BatchError(f"{label}: topology changed")

    protected_baseline = require_mapping(
        replay.get("protected_baseline_slacks_ns"), f"{label}.protected_baseline"
    )
    protected_after = require_mapping(
        replay.get("protected_after_slacks_ns"), f"{label}.protected_after"
    )
    if set(protected_baseline) != set(protected) or set(protected_after) != set(protected):
        raise BatchError(f"{label}: protected slack maps mismatch")
    max_drop_ns = acceptance["protected_slack_drop_max_ps"] / 1000.0
    for endpoint in protected:
        _nonnegative(protected_after[endpoint], f"{label}.protected[{endpoint}]")
        drop = require_number(protected_baseline[endpoint], "protected baseline") - require_number(
            protected_after[endpoint], "protected after"
        )
        if drop > max_drop_ns + 1e-12:
            raise BatchError(f"{label}: protected endpoint dropped by more than 1 ps")

    diffs = _objects(replay.get("cell_diff"), f"{label}.cell_diff")
    expected_by_instance = {
        operation["instance"]: (operation["old_ref"], operation["new_ref"])
        for operation in operations
    }
    if {item.get("instance") for item in diffs} != set(expected_by_instance):
        raise BatchError(f"{label}: cell diff does not exactly match repair instances")
    if len(diffs) != len(operations):
        raise BatchError(f"{label}: cell diff count mismatch")
    normalized_diff: list[tuple[str, str, str]] = []
    for item in diffs:
        instance = item.get("instance")
        old_ref, new_ref = item.get("old_ref"), item.get("new_ref")
        if (
            (old_ref, new_ref) != expected_by_instance[instance]
            or old_ref == new_ref
        ):
            raise BatchError(f"{label}: ineffective or unexpected resize for {instance}")
        if not all(
            require_bool(item.get(key), f"{label}.{instance}.{key}")
            for key in ("boolean_equivalent", "pin_signature_equivalent", "rvt_equivalent")
        ):
            raise BatchError(f"{label}: non-equivalent resize for {instance}")
        normalized_diff.append((instance, old_ref, new_ref))

    sensitivity = require_mapping(
        replay.get("repair_sensitivity_ns"), f"{label}.repair_sensitivity_ns"
    )
    if set(sensitivity) != set(expected_by_instance):
        raise BatchError(f"{label}: sensitivity set mismatch")
    minimum = acceptance["repair_sensitivity_min_ps"] / 1000.0
    for instance, endpoint_map in sensitivity.items():
        endpoint_map = require_mapping(endpoint_map, f"{label}.sensitivity[{instance}]")
        if not endpoint_map or max(require_number(v, "sensitivity") for v in endpoint_map.values()) < minimum:
            raise BatchError(f"{label}: {instance} has no >=1 ps positive sensitivity")

    return {
        "slot": replay.get("slot"),
        "process_id": replay.get("process_id"),
        "targets": targets,
        "protected": protected,
        "normalized_diff": sorted(normalized_diff),
        "before": before,
        "after_resize": after_resize,
        "after": after,
        "count_evidence": replay.get("count_evidence"),
        "protected_baseline": protected_baseline,
        "protected_after": protected_after,
        "fix_sha256": replay.get("fix_sha256"),
        "constraint_sha256": before.get("constraint_sha256"),
        "topology_sha256": before.get("topology_sha256"),
        "portability": portability,
    }


def _compare_replays(
    left: Mapping[str, Any], right: Mapping[str, Any], tolerance_ns: float, case_id: str
) -> None:
    for key in (
        "targets",
        "protected",
        "normalized_diff",
        "count_evidence",
        "fix_sha256",
        "constraint_sha256",
        "topology_sha256",
    ):
        if left.get(key) != right.get(key):
            raise BatchError(f"{case_id}: replay {key} evidence differs")
    if left.get("process_id") == right.get("process_id"):
        raise BatchError(f"{case_id}: replays did not use fresh Innovus processes")
    if left.get("slot") == right.get("slot"):
        raise BatchError(f"{case_id}: replays were not scheduled to different slots")
    scalar_paths = (
        ("before", "setup_wns_ns"),
        ("before", "setup_tns_ns"),
        ("after_resize", "setup_wns_ns"),
        ("after_resize", "setup_tns_ns"),
        ("after", "setup_wns_ns"),
        ("after", "setup_tns_ns"),
        ("before", "hold_wns_ns"),
        ("before", "hold_tns_ns"),
        ("after", "hold_wns_ns"),
        ("after", "hold_tns_ns"),
    )
    for stage, key in scalar_paths:
        same_number(
            require_mapping(left[stage], "left stage").get(key),
            require_mapping(right[stage], "right stage").get(key),
            tolerance_ns,
            f"{case_id}.dual_replay.{stage}.{key}",
        )
    numeric_maps = (
        ("before", "target_slacks_ns"),
        ("after_resize", "target_slacks_ns"),
        ("after", "target_slacks_ns"),
    )
    for stage, key in numeric_maps:
        left_map = require_mapping(left[stage].get(key), f"left.{stage}.{key}")
        right_map = require_mapping(right[stage].get(key), f"right.{stage}.{key}")
        if set(left_map) != set(right_map):
            raise BatchError(f"{case_id}: replay {stage}.{key} keys differ")
        for name in left_map:
            same_number(
                left_map[name],
                right_map[name],
                tolerance_ns,
                f"{case_id}.dual_replay.{stage}.{key}.{name}",
            )
    for key in ("protected_baseline", "protected_after"):
        left_map = require_mapping(left[key], f"left.{key}")
        right_map = require_mapping(right[key], f"right.{key}")
        if set(left_map) != set(right_map):
            raise BatchError(f"{case_id}: replay {key} keys differ")
        for name in left_map:
            same_number(
                left_map[name],
                right_map[name],
                tolerance_ns,
                f"{case_id}.dual_replay.{key}.{name}",
            )
    left_portability = require_mapping(left["portability"], "left portability")
    right_portability = require_mapping(right["portability"], "right portability")
    for key in ("endpoint_count", "negative_endpoints"):
        if left_portability.get(key) != right_portability.get(key):
            raise BatchError(f"{case_id}: replay portability {key} differs")
    _compare_numeric_map(
        left_portability.get("slacks_ns"),
        right_portability.get("slacks_ns"),
        tolerance_ns,
        f"{case_id}.dual_replay.portability_slacks",
    )
    left_paths = require_mapping(left["before"].get("target_paths"), "left target paths")
    right_paths = require_mapping(right["before"].get("target_paths"), "right target paths")
    if set(left_paths) != set(right_paths):
        raise BatchError(f"{case_id}: replay target-path endpoint sets differ")
    for endpoint in left_paths:
        for key in ("cell_delay_ns", "net_delay_ns"):
            same_number(
                require_mapping(left_paths[endpoint], "left target path").get(key),
                require_mapping(right_paths[endpoint], "right target path").get(key),
                tolerance_ns,
                f"{case_id}.dual_replay.target_paths.{endpoint}.{key}",
            )


def _validate_metrics(
    metrics: Mapping[str, Any],
    replay_one: Mapping[str, Any],
    *,
    case_id: str,
    fix_hash: str,
) -> None:
    label = f"{case_id}.metrics"
    if metrics.get("schema_version") != "mock_lef_batch100.metrics.v1":
        raise BatchError(f"{label}: unsupported schema")
    if metrics.get("case_id") != case_id or metrics.get("status") != "PASS":
        raise BatchError(f"{label}: identity/status mismatch")
    if metrics.get("fix_sha256") != fix_hash:
        raise BatchError(f"{label}: fix hash mismatch")
    if require_bool(metrics.get("hold_gate_applied"), f"{label}.hold_gate_applied"):
        raise BatchError(f"{label}: hold was incorrectly used as an acceptance gate")
    numeric_sources = {
        "setup_wns_before_ns": ("before", "setup_wns_ns"),
        "setup_wns_after_resize_ns": ("after_resize", "setup_wns_ns"),
        "setup_wns_after_legalize_ns": ("after_legalize", "setup_wns_ns"),
        "setup_tns_after_legalize_ns": ("after_legalize", "setup_tns_ns"),
        "hold_before_ns": ("before", "hold_wns_ns"),
        "hold_after_ns": ("after_legalize", "hold_wns_ns"),
    }
    for metric_name, (stage_name, replay_name) in numeric_sources.items():
        measured = require_number(metrics.get(metric_name), f"{label}.{metric_name}")
        stage = require_mapping(
            replay_one.get(stage_name), f"{case_id}.replay_1.{stage_name}"
        )
        replay_value = require_number(
            stage.get(replay_name),
            f"{case_id}.replay_1.{stage_name}.{replay_name}",
        )
        if measured != replay_value:
            raise BatchError(
                f"{label}.{metric_name} does not equal replay 1 evidence"
            )


def validate_case(
    case_root: Path, spec: Mapping[str, Any], acceptance: Mapping[str, Any]
) -> dict[str, Any]:
    case_id = spec["id"]
    if case_root.name != case_id:
        raise BatchError(f"{case_id}: case directory name mismatch")
    _reject_checkpoint_residue(
        case_root,
        allowed_checkpoint_paths=(
            case_root / "violating.enc",
            case_root / "violating.enc.dat",
        ),
    )
    wrapper = require_real_file(case_root / "violating.enc", "violating checkpoint wrapper")
    checkpoint = require_real_directory(
        case_root / "violating.enc.dat", "violating checkpoint data"
    )
    checkpoint_hash = sha256_file(wrapper)
    checkpoint_tree_hash = checkpoint_archive.checkpoint_tree_sha256_v2(
        checkpoint, baseline_prefix=checkpoint_archive_baseline_prefix()
    )
    checkpoint_bundle_hash = (
        checkpoint_archive.checkpoint_bundle_sha256_v1_from_hashes(
            checkpoint_hash, checkpoint_tree_hash
        )
    )
    fix_path = require_real_file(case_root / "fix.tcl", "canonical fix")
    fix_bytes = fix_path.read_bytes()
    try:
        fix_text = fix_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BatchError(f"{case_id}: fix.tcl is not UTF-8") from exc
    operations = parse_fix_tcl(fix_text, spec["expected_modification_count"])
    fix_hash = hashlib.sha256(fix_bytes).hexdigest()
    plan_path = require_real_file(
        case_root / "evidence" / "injection" / "calibration_plan.json",
        "frozen calibration plan evidence",
    )
    plan_sha256 = sha256_file(plan_path)
    plan = require_mapping(
        read_json(plan_path, "frozen calibration plan"),
        f"{case_id}.calibration_plan",
    )
    (
        injection_operations,
        repair_operations,
        injection_fingerprint,
        repair_fingerprint,
    ) = _frozen_operations(plan, spec, fix_hash)
    fix_plan_operations = [
        (operation["instance"], operation["cell"]) for operation in operations
    ]
    frozen_fix_operations = [
        (operation["instance"], operation["new_ref"])
        for operation in repair_operations
    ]
    if fix_plan_operations != frozen_fix_operations:
        raise BatchError(f"{case_id}: fix.tcl does not exactly match frozen repair ops")

    instruction = require_real_file(case_root / "instruction.txt", "instruction").read_text(
        encoding="utf-8"
    )
    if re.search(r"(?i)\b(?:inject|injection|downsize oracle|hidden)\b", instruction):
        raise BatchError(f"{case_id}: instruction leaks hidden injection details")
    answer = require_real_file(case_root / "answer.txt", "answer").read_text(encoding="utf-8")
    fences = re.findall(r"```tcl\n(.*?)```", answer, re.DOTALL)
    if len(fences) != 1 or fences[0].encode() != fix_bytes:
        raise BatchError(f"{case_id}: answer Tcl is not byte-identical to fix.tcl")

    validation = require_mapping(
        read_json(case_root / "validation.json", "validation"), f"{case_id}.validation"
    )
    if validation.get("schema_version") != "mock_lef_batch100.validation.v2":
        raise BatchError(f"{case_id}: unsupported validation schema")
    if validation.get("case_id") != case_id or validation.get("status") != "VALIDATED":
        raise BatchError(f"{case_id}: validation identity/status mismatch")
    if validation.get("validation_class") != "validated_innovus_dual_replay":
        raise BatchError(f"{case_id}: wrong validation class")
    if validation.get("signoff_qualified") is not False:
        raise BatchError(f"{case_id}: must remain non-signoff")
    if validation.get("technology_classification") != "mock_training_non_signoff":
        raise BatchError(f"{case_id}: wrong technology classification")
    if validation.get("fix_sha256") != fix_hash:
        raise BatchError(f"{case_id}: validation fix hash mismatch")
    if (
        validation.get("violating_checkpoint_bundle_sha256")
        != checkpoint_bundle_hash
    ):
        raise BatchError(f"{case_id}: validation checkpoint bundle hash mismatch")
    if (
        validation.get("violating_checkpoint_bundle_hash_algorithm")
        != checkpoint_archive.CHECKPOINT_BUNDLE_HASH_ALGORITHM
    ):
        raise BatchError(f"{case_id}: unsupported checkpoint bundle hash algorithm")
    if (
        validation.get("violating_checkpoint_tree_hash_algorithm")
        != checkpoint_archive.CHECKPOINT_TREE_HASH_ALGORITHM
    ):
        raise BatchError(f"{case_id}: unsupported checkpoint tree hash algorithm")
    if validation.get("calibration_plan_sha256") != plan_sha256:
        raise BatchError(f"{case_id}: frozen calibration plan hash mismatch")
    if validation.get("injection_fingerprint_sha256") != injection_fingerprint:
        raise BatchError(f"{case_id}: injection fingerprint differs from frozen plan")
    if validation.get("repair_fingerprint_sha256") != repair_fingerprint:
        raise BatchError(f"{case_id}: repair fingerprint differs from frozen plan")

    injection = require_mapping(
        validation.get("hidden_injection_audit"), f"{case_id}.hidden_injection_audit"
    )
    _validate_raw_binding(
        validation.get("injection_raw_evidence"),
        case_root / "evidence" / "injection" / "innovus.log",
        case_root / "evidence" / "injection" / "reports",
        f"{case_id}.injection",
    )
    injected = set(_strings(injection.get("injection_instances"), "injection instances"))
    frozen_injected = {operation["instance"] for operation in injection_operations}
    if injected != frozen_injected:
        raise BatchError(f"{case_id}: hidden injection audit differs from frozen plan")

    replays = _objects(validation.get("replays"), f"{case_id}.replays")
    if len(replays) != 2:
        raise BatchError(f"{case_id}: exactly two replay records are required")
    summaries = [
        _validate_replay(
            replay,
            index,
            case_root,
            spec,
            fix_hash,
            repair_operations,
            checkpoint_hash,
            checkpoint_tree_hash,
            checkpoint_bundle_hash,
            acceptance,
        )
        for index, replay in enumerate(replays, start=1)
    ]
    _compare_replays(
        summaries[0],
        summaries[1],
        acceptance["replay_numeric_tolerance_ps"] / 1000.0,
        case_id,
    )
    metrics = require_mapping(
        read_json(case_root / "metrics.json", "metrics"), f"{case_id}.metrics"
    )
    _validate_metrics(
        metrics,
        replays[0],
        case_id=case_id,
        fix_hash=fix_hash,
    )
    return {
        "case_id": case_id,
        "fix_sha256": fix_hash,
        "violating_checkpoint_sha256": checkpoint_hash,
        "violating_checkpoint_tree_sha256": checkpoint_tree_hash,
        "violating_checkpoint_tree_hash_algorithm": (
            checkpoint_archive.CHECKPOINT_TREE_HASH_ALGORITHM
        ),
        "violating_checkpoint_bundle_sha256": checkpoint_bundle_hash,
        "violating_checkpoint_bundle_hash_algorithm": (
            checkpoint_archive.CHECKPOINT_BUNDLE_HASH_ALGORITHM
        ),
        "target_endpoints": summaries[0]["targets"],
        "protected_endpoints": summaries[0]["protected"],
        "repair_instances": sorted(
            operation["instance"] for operation in repair_operations
        ),
        "calibration_plan_sha256": plan_sha256,
        "injection_fingerprint_sha256": injection_fingerprint,
        "repair_fingerprint_sha256": repair_fingerprint,
        "replay_slots": [summary["slot"] for summary in summaries],
    }


def _case_manifest(
    case_root: Path,
    summary: Mapping[str, Any],
    technology_classification: str = "mock_training_non_signoff",
) -> dict[str, Any]:
    ledger = [
        row for row in _case_artifact_ledger(case_root) if row["path"] != "manifest.json"
    ]
    return {
        "schema_version": "mock_lef_batch100.case_manifest.v2",
        **summary,
        "validation_class": "validated_innovus_dual_replay",
        "signoff_qualified": False,
        "technology_classification": technology_classification,
        "artifact_ledger_algorithm": CASE_ARTIFACT_TREE_HASH_ALGORITHM,
        "artifacts": ledger,
    }


def _conversation(
    case_root: Path,
    spec: Mapping[str, Any],
    summary: Mapping[str, Any],
    technology_classification: str = "mock_training_non_signoff",
) -> dict[str, Any]:
    instruction = (case_root / "instruction.txt").read_text(encoding="utf-8").rstrip("\n")
    answer = (case_root / "answer.txt").read_text(encoding="utf-8").rstrip("\n")
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": instruction},
            {"role": "assistant", "content": answer},
        ],
        "metadata": {
            "case_id": spec["id"],
            "shape": spec["shape"],
            "strategy": spec["strategy"],
            "difficulty": spec["difficulty"],
            "validation_class": "validated_innovus_dual_replay",
            "signoff_qualified": False,
            "technology_classification": technology_classification,
            "fix_sha256": summary["fix_sha256"],
            "violating_checkpoint_sha256": summary["violating_checkpoint_sha256"],
            "violating_checkpoint_bundle_sha256": (
                summary["violating_checkpoint_bundle_sha256"]
            ),
            "violating_checkpoint_bundle_hash_algorithm": (
                summary["violating_checkpoint_bundle_hash_algorithm"]
            ),
        },
    }


def finalize(root: Path, specs_path: Path = SPECS_PATH) -> dict[str, Any]:
    specs = require_mapping(read_json(specs_path, "case specs"), "case specs")
    cases = _objects(specs.get("cases"), "case specs.cases")
    if len(cases) != 100:
        raise BatchError("finalization requires exactly 100 case specs")
    technology_classification = {
        "mock_lef_batch100.case_specs.relaxed_v2": (
            "mock_training_relaxed_v2_non_signoff"
        ),
        "mock_lef_batch100.case_specs.relaxed_v3": (
            "mock_training_relaxed_v3_non_signoff"
        ),
    }.get(specs.get("schema_version"), "mock_training_non_signoff")
    run_manifest = require_mapping(
        read_json(root / "run_manifest.json", "run manifest"), "run manifest"
    )
    if run_manifest.get("schema_version") != "mock_lef_batch100.run_manifest.v1":
        raise BatchError("unsupported/missing batch run manifest")
    input_hashes = require_mapping(
        run_manifest.get("input_hashes"), "run manifest input_hashes"
    )
    if input_hashes.get("case_specs") != sha256_file(specs_path):
        raise BatchError("run manifest is not bound to the finalized case specs")
    probe_manifest = require_mapping(
        read_json(root / "probe" / "probe_manifest.json", "probe manifest"),
        "probe manifest",
    )
    output_cases = require_real_directory(root / "cases", "final cases")
    expected_ids = [case["id"] for case in cases]
    actual_ids = sorted(path.name for path in output_cases.iterdir() if path.is_dir())
    if actual_ids != expected_ids:
        raise BatchError("final case directory set is not exactly B1_CASE_001..100")
    _reject_checkpoint_residue(
        root,
        allowed_checkpoint_paths=tuple(
            path
            for case_id in expected_ids
            for path in (
                output_cases / case_id / "violating.enc",
                output_cases / case_id / "violating.enc.dat",
            )
        ),
    )
    summaries: list[dict[str, Any]] = []
    conversations: list[dict[str, Any]] = []
    seen_targets: set[str] = set()
    injection_fingerprints: set[str] = set()
    repair_fingerprints: set[str] = set()
    for spec in cases:
        case_root = output_cases / spec["id"]
        summary = validate_case(case_root, spec, specs["acceptance"])
        overlap = seen_targets & set(summary["target_endpoints"])
        if overlap:
            raise BatchError(f"{spec['id']}: target endpoint reused: {sorted(overlap)}")
        seen_targets.update(summary["target_endpoints"])
        injection_fingerprint = summary["injection_fingerprint_sha256"]
        repair_fingerprint = summary["repair_fingerprint_sha256"]
        if injection_fingerprint in injection_fingerprints:
            raise BatchError(f"{spec['id']}: duplicate injection fingerprint")
        if repair_fingerprint in repair_fingerprints:
            raise BatchError(f"{spec['id']}: duplicate repair fingerprint")
        injection_fingerprints.add(injection_fingerprint)
        repair_fingerprints.add(repair_fingerprint)
        atomic_json(
            case_root / "manifest.json",
            _case_manifest(case_root, summary, technology_classification),
        )
        case_ledger = _case_artifact_ledger(case_root)
        summaries.append(
            {
                **summary,
                "case_manifest_sha256": sha256_file(case_root / "manifest.json"),
                "artifact_tree_hash_algorithm": CASE_ARTIFACT_TREE_HASH_ALGORITHM,
                "artifact_tree_sha256": _ledger_sha256(case_ledger),
            }
        )
        conversations.append(
            _conversation(case_root, spec, summary, technology_classification)
        )

    dataset_bytes = b"".join(
        (
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        for row in conversations
    )
    if dataset_bytes.count(b"\n") != 100:
        raise BatchError("internal error: dataset line count is not 100")
    dataset_path = root / "dataset.jsonl"
    atomic_write(dataset_path, dataset_bytes, durable=True)
    manifest = {
        "schema_version": "mock_lef_batch100.batch_manifest.v2",
        "status": "FINALIZED",
        "case_count": 100,
        "dataset": {
            "path": "dataset.jsonl",
            "lines": 100,
            "bytes": len(dataset_bytes),
            "sha256": hashlib.sha256(dataset_bytes).hexdigest(),
        },
        "source_catalog_sha256": specs["source_catalog"]["sha256"],
        "case_specs_sha256": sha256_file(specs_path),
        "design": specs["design"],
        "baseline": run_manifest.get("baseline"),
        "harness_input_hashes": dict(input_hashes),
        "probe": dict(probe_manifest),
        "checkpoint_policy": {
            "retained_per_case": ["violating.enc", "violating.enc.dat"],
            "fixed_checkpoints_retained": 0,
            "transport_archive_retained": False,
            "tree_hash_algorithm": (
                checkpoint_archive.CHECKPOINT_TREE_HASH_ALGORITHM
            ),
            "bundle_hash_algorithm": (
                checkpoint_archive.CHECKPOINT_BUNDLE_HASH_ALGORITHM
            ),
        },
        "distribution": {
            "shape": dict(Counter(case["shape"] for case in cases)),
            "strategy": dict(Counter(case["strategy"] for case in cases)),
            "difficulty": dict(Counter(case["difficulty"] for case in cases)),
        },
        "cases": summaries,
    }
    atomic_json(root / "manifest.json", manifest, durable=True)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("check-case", "finalize"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--case-id")
    parser.add_argument("--specs", type=Path, default=SPECS_PATH)
    args = parser.parse_args(argv)
    try:
        specs = read_json(args.specs, "case specs")
        if args.command == "check-case":
            if not args.case_id:
                raise BatchError("--case-id is required for check-case")
            by_id = {case["id"]: case for case in specs["cases"]}
            if args.case_id not in by_id:
                raise BatchError(f"unknown case ID: {args.case_id}")
            summary = validate_case(
                args.root / "cases" / args.case_id,
                by_id[args.case_id],
                specs["acceptance"],
            )
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        else:
            result = finalize(args.root, args.specs)
            print(f"finalize PASS: {result['case_count']}/100 cases")
    except (BatchError, OSError, KeyError, TypeError, ValueError) as exc:
        print(f"finalize FAIL: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

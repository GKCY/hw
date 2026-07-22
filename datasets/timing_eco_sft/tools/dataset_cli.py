#!/usr/bin/env python3
"""Export and validate the timing ECO SFT pilot dataset.

The source of truth is one directory per case under ``pilot_10/cases``.  This
module intentionally has no third-party dependencies so it can be run on the
host as well as in the EDA guest.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "timing_eco_sft.v1"
FINALIZER_SCHEMA_VERSION = "timing_eco_pilot_finalizer.v2"
FUNCTIONAL_AUDIT_SCHEMA_VERSION = "timing_eco_functional_audit.v1"
PRIMETIME_SCHEMA_VERSION = "timing_eco_primetime_crosscheck.v4"
REPLAY_COMPARISON_SCHEMA_VERSION = "timing_eco_replay_comparison.v1"
DIAGNOSTIC_CONTEXT_SCHEMA_VERSION = "timing_eco_diagnostic_context.v1"
VIOLATION_LOCALITY_SCHEMA_VERSION = "timing_eco_violation_locality.v1"
INJECTION_PROVENANCE_SCHEMA_VERSION = "timing_eco_injection_provenance.v1"
BASELINE_GUARD_SCHEMA_VERSION = "timing_eco_baseline_guard.v1"
PHYSICAL_NO_REGRESSION_SCHEMA_VERSION = "timing_eco_physical_no_regression.v1"
BASELINE_QUALIFICATION_SCHEMA_VERSION = "smic40_baseline_qualification.v1"
QUALIFIED_LIBERTY_ROLES = {
    "setup_lib": "setup_liberty_ss",
    "hold_lib": "hold_liberty_ff",
}
PT_MODULE_PATH = Path(__file__).with_name("primetime_crosscheck.py")
_PTX_VERIFIER: Any | None = None
DIAGNOSTIC_TARGET_FIELDS = {
    "role",
    "timing",
    "endpoint",
    "beginpoint",
    "slack_ns",
    "net",
    "driver_pin",
    "driver_inst",
    "driver_ref",
    "original_driver",
    "local_cells",
}
DIAGNOSTIC_ORIGINAL_DRIVER_FIELDS = {"inst", "ref"}
DIAGNOSTIC_LOCAL_CELL_FIELDS = {"inst", "ref"}
MAX_DIAGNOSTIC_TARGETS = 64
MAX_DIAGNOSTIC_LOCAL_CELLS = 8
MAX_DIAGNOSTIC_STRING_CHARS = 512
MAX_DIAGNOSTIC_CONTEXT_BYTES = 128 * 1024
EXPECTED_CASES = 10
EXPECTED_MIX = {"setup": 4, "hold": 4, "mixed": 2}
EXPECTED_IDS_BY_TYPE = {
    "setup": tuple(f"SETUP_{index:03d}" for index in range(1, 5)),
    "hold": tuple(f"HOLD_{index:03d}" for index in range(1, 5)),
    "mixed": tuple(f"MIXED_{index:03d}" for index in range(1, 3)),
}
EXPECTED_IDS = frozenset(
    case_id for ids in EXPECTED_IDS_BY_TYPE.values() for case_id in ids
)
VALID_DIFFICULTIES = {"easy", "medium", "hard"}
MIN_FINAL_SLACK_NS = 0.010

SYSTEM_PROMPT = (
    "你是一名资深 Cadence Innovus 21.10 物理设计工程师。请根据给出的 post-route "
    "时序与 QoR 证据诊断违例，并给出可重放的最小化 ECO。不得通过放松时钟或 I/O 约束、"
    "false path、multicycle path 或 disable timing 来隐藏违例；修复 Tcl 将由独立 replay "
    "harness 执行，并由该 harness 复查 setup、hold、DRV、DRC 与 connectivity。"
)

REQUIRED_ARTIFACT_ROLES = (
    "baseline_qualification",
    "inject_tcl",
    "fix_tcl",
    "replay_tcl",
    "metrics",
    "setup_before",
    "hold_before",
    "setup_after",
    "hold_after",
    "drv_after",
    "connectivity_after",
    "drc_before",
    "drc_after",
    "run_log",
    "host_ssh_log",
    "constraint_before",
    "constraint_after",
    "constraint_setup_before",
    "constraint_setup_after",
    "constraint_hold_before",
    "constraint_hold_after",
    "functional_audit",
    "injection_provenance",
    "baseline_guard",
    "physical_no_regression",
    "run_status",
    "success_marker",
    "violating_checkpoint",
    "violating_checkpoint_data",
    "before_netlist",
    "setup_spef_before",
    "hold_spef_before",
    "check_design_after",
    "diagnostic_context",
    "violation_locality",
    "primetime_crosscheck",
    "pt_input_setup_sdc",
    "pt_input_hold_sdc",
    "pt_input_setup_sdc_adapted",
    "pt_input_hold_sdc_adapted",
    "replay_comparison",
    "instruction",
    "answer",
    "replay_2_injection_provenance",
    "replay_2_baseline_guard",
    "replay_2_physical_no_regression",
    "replay_2_run_status",
    "replay_2_host_ssh_log",
    "replay_2_success_marker",
    "replay_2_violating_checkpoint",
    "replay_2_violating_checkpoint_data",
    "replay_2_before_netlist",
    "replay_2_setup_spef_before",
    "replay_2_hold_spef_before",
    "replay_2_check_design_after",
)

NATIVE_ARTIFACT_ROLES = (
    "native_cell_diff",
    "native_selected_terms",
    "replay_2_native_cell_diff",
    "replay_2_native_selected_terms",
)
NATIVE_SELECTED_TERMS_RELATIVE = "reports/native_selected_terms.txt"

# Keep the hierarchy prefix when checking deterministic ECO object names.
# A leaf-only regex falsely rejects exposed objects such as
# ``u_exp/SFT_ECO_SETUP_001_PATH_1``.
ECO_INSTANCE_NAME_RE = re.compile(
    r"(?<![A-Za-z0-9_./:@+$\-])"
    r"((?:[A-Za-z0-9_.:@+$\-]+/)*SFT_ECO_[A-Za-z0-9_.:@+$\-]+)"
)

_ROLE_ALIASES = {
    "baseline_qualification": "baseline_qualification",
    "inject": "inject_tcl",
    "injection": "inject_tcl",
    "inject_tcl": "inject_tcl",
    "fix": "fix_tcl",
    "fix_tcl": "fix_tcl",
    "replay": "replay_tcl",
    "replay_tcl": "replay_tcl",
    "metrics": "metrics",
    "metrics_json": "metrics",
    "before_setup": "setup_before",
    "setup_before": "setup_before",
    "before_hold": "hold_before",
    "hold_before": "hold_before",
    "after_setup": "setup_after",
    "setup_after": "setup_after",
    "after_hold": "hold_after",
    "hold_after": "hold_after",
    "after_drv": "drv_after",
    "drv_after": "drv_after",
    "after_connectivity": "connectivity_after",
    "connectivity_after": "connectivity_after",
    "before_drc": "drc_before",
    "drc_before": "drc_before",
    "after_drc": "drc_after",
    "drc_after": "drc_after",
    "innovus_log": "run_log",
    "run_log": "run_log",
    "log": "run_log",
    "host_ssh_log": "host_ssh_log",
    "constraint_before": "constraint_before",
    "constraint_before_sdc": "constraint_before",
    "constraint_after": "constraint_after",
    "constraint_after_sdc": "constraint_after",
    "constraint_setup_before": "constraint_setup_before",
    "constraint_setup_before_sdc": "constraint_setup_before",
    "constraint_setup_after": "constraint_setup_after",
    "constraint_setup_after_sdc": "constraint_setup_after",
    "constraint_hold_before": "constraint_hold_before",
    "constraint_hold_before_sdc": "constraint_hold_before",
    "constraint_hold_after": "constraint_hold_after",
    "constraint_hold_after_sdc": "constraint_hold_after",
    "functional_audit": "functional_audit",
    "functional_audit_json": "functional_audit",
    "injection_provenance": "injection_provenance",
    "baseline_guard": "baseline_guard",
    "physical_no_regression": "physical_no_regression",
    "run_status": "run_status",
    "success_marker": "success_marker",
    "violating_checkpoint": "violating_checkpoint",
    "violating_checkpoint_data": "violating_checkpoint_data",
    "before_netlist": "before_netlist",
    "setup_spef_before": "setup_spef_before",
    "hold_spef_before": "hold_spef_before",
    "check_design_after": "check_design_after",
    "diagnostic_context": "diagnostic_context",
    "diagnostic_context_json": "diagnostic_context",
    "violation_locality": "violation_locality",
    "violation_locality_json": "violation_locality",
    "primetime_crosscheck": "primetime_crosscheck",
    "primetime_crosscheck_json": "primetime_crosscheck",
    "pt_input_setup_sdc": "pt_input_setup_sdc",
    "pt_input_hold_sdc": "pt_input_hold_sdc",
    "pt_input_setup_sdc_adapted": "pt_input_setup_sdc_adapted",
    "pt_input_hold_sdc_adapted": "pt_input_hold_sdc_adapted",
    "replay_comparison": "replay_comparison",
    "replay_comparison_json": "replay_comparison",
    "native_cell_diff": "native_cell_diff",
    "native_cell_diff_tcl": "native_cell_diff",
    "native_selected_terms": "native_selected_terms",
    "native_selected_terms_txt": "native_selected_terms",
    "instruction": "instruction",
    "instruction_txt": "instruction",
    "answer": "answer",
    "answer_txt": "answer",
    "replay_2_injection_provenance": "replay_2_injection_provenance",
    "replay_2_baseline_guard": "replay_2_baseline_guard",
    "replay_2_physical_no_regression": "replay_2_physical_no_regression",
    "replay_2_run_status": "replay_2_run_status",
    "replay_2_host_ssh_log": "replay_2_host_ssh_log",
    "replay_2_success_marker": "replay_2_success_marker",
    "replay_2_violating_checkpoint": "replay_2_violating_checkpoint",
    "replay_2_violating_checkpoint_data": "replay_2_violating_checkpoint_data",
    "replay_2_before_netlist": "replay_2_before_netlist",
    "replay_2_setup_spef_before": "replay_2_setup_spef_before",
    "replay_2_hold_spef_before": "replay_2_hold_spef_before",
    "replay_2_check_design_after": "replay_2_check_design_after",
    "replay_2_native_cell_diff": "replay_2_native_cell_diff",
    "replay_2_native_selected_terms": "replay_2_native_selected_terms",
}

_FORBIDDEN_TCL = {
    "set_false_path": re.compile(r"(?im)^\s*set_false_path\b"),
    "set_multicycle_path": re.compile(r"(?im)^\s*set_multicycle_path\b"),
    "set_disable_timing": re.compile(r"(?im)^\s*set_disable_timing\b"),
    "set_clock_uncertainty": re.compile(r"(?im)^\s*set_clock_uncertainty\b"),
    "set_clock_latency": re.compile(r"(?im)^\s*set_clock_latency\b"),
    "set_max_delay": re.compile(r"(?im)^\s*set_max_delay\b"),
    "set_min_delay": re.compile(r"(?im)^\s*set_min_delay\b"),
    "reset_path": re.compile(r"(?im)^\s*reset_path\b"),
    "set_input_delay": re.compile(r"(?im)^\s*set_input_delay\b"),
    "set_output_delay": re.compile(r"(?im)^\s*set_output_delay\b"),
    "create_clock": re.compile(r"(?im)^\s*create_clock\b"),
    "set_analysis_view": re.compile(r"(?im)^\s*set_analysis_view\b"),
    "set_interactive_constraint_modes": re.compile(
        r"(?im)^\s*set_interactive_constraint_modes\b"
    ),
}


class DatasetError(RuntimeError):
    """A user-facing dataset validation error."""


def _read_utf8(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DatasetError(f"missing file: {path}") from exc
    except UnicodeDecodeError as exc:
        raise DatasetError(f"file is not valid UTF-8: {path}: {exc}") from exc


def _read_json(path: Path) -> Any:
    text = _read_utf8(path)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise DatasetError(f"invalid JSON in {path}: {exc}") from exc


def _primetime_verifier() -> Any:
    """Load the sibling PT verifier without weakening or duplicating its policy."""

    global _PTX_VERIFIER
    if _PTX_VERIFIER is not None:
        return _PTX_VERIFIER
    if not PT_MODULE_PATH.is_file():
        raise DatasetError(f"missing PrimeTime verifier: {PT_MODULE_PATH}")
    spec = importlib.util.spec_from_file_location(
        "timing_eco_primetime_crosscheck_for_dataset", PT_MODULE_PATH
    )
    if spec is None or spec.loader is None:
        raise DatasetError(f"cannot load PrimeTime verifier: {PT_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise DatasetError(f"cannot initialize PrimeTime verifier: {exc}") from exc
    if getattr(module, "SCHEMA_VERSION", None) != PRIMETIME_SCHEMA_VERSION:
        raise DatasetError(
            "dataset/PrimeTime verifier schema mismatch: expected "
            f"{PRIMETIME_SCHEMA_VERSION}, got {getattr(module, 'SCHEMA_VERSION', None)!r}"
        )
    if not callable(getattr(module, "load_and_verify_summary", None)):
        raise DatasetError("PrimeTime verifier has no load_and_verify_summary entry point")
    _PTX_VERIFIER = module
    return module


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_inventory(path: Path, where: str) -> list[dict[str, Any]]:
    if not path.is_dir() or path.is_symlink():
        raise DatasetError(f"{where} is not a regular directory: {path}")
    files: list[dict[str, Any]] = []
    for item in sorted(path.rglob("*"), key=lambda candidate: candidate.as_posix()):
        if item.is_symlink():
            raise DatasetError(f"{where} contains a symlink: {item}")
        if item.is_dir():
            continue
        if not item.is_file():
            raise DatasetError(f"{where} contains a non-regular entry: {item}")
        files.append(
            {
                "path": item.relative_to(path).as_posix(),
                "sha256": _sha256(item),
                "bytes": item.stat().st_size,
            }
        )
    if not files or sum(int(item["bytes"]) for item in files) <= 0:
        raise DatasetError(f"{where} contains no non-empty file evidence: {path}")
    return files


def _inventory_sha256(inventory: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in inventory:
        digest.update(
            f"{item['path']}\0{item['sha256']}\0{item['bytes']}\n".encode("utf-8")
        )
    return digest.hexdigest()


def _tree_sha256(path: Path, where: str) -> str:
    return _inventory_sha256(_tree_inventory(path, where))


def _normal_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _canonical_role(name: str) -> str | None:
    return _ROLE_ALIASES.get(_normal_key(name))


def _infer_role(path_text: str) -> str | None:
    normalized = _normal_key(path_text)
    matches = {
        role
        for alias, role in _ROLE_ALIASES.items()
        if re.search(rf"(?:^|_){re.escape(alias)}(?:_|$)", normalized)
    }
    return next(iter(matches)) if len(matches) == 1 else None


def _artifact_value(value: Any, where: str) -> tuple[str, str, int, str, int | None]:
    """Load one finalizer-bound artifact declaration.

    Gold export deliberately rejects legacy path-only entries.  A caller must
    present the exact path, digest, and byte count emitted by the finalizer so
    changing an evidence file requires changing a visibly typed Gold manifest.
    """

    if not isinstance(value, Mapping):
        raise DatasetError(
            f"{where} must be a finalizer artifact object with path, sha256, and bytes"
        )
    path = value.get("path")
    digest = value.get("sha256")
    size = value.get("bytes")
    kind = value.get("kind", "file")
    files = value.get("files")
    if not isinstance(path, str) or not path:
        raise DatasetError(f"{where} must contain a non-empty string 'path'")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise DatasetError(f"{where}.sha256 must be a lowercase SHA256 digest")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise DatasetError(f"{where}.bytes must be a positive integer")
    if kind not in {"file", "directory"}:
        raise DatasetError(f"{where}.kind must be file or directory")
    if kind == "directory":
        if isinstance(files, bool) or not isinstance(files, int) or files <= 0:
            raise DatasetError(f"{where}.files must be positive for a directory")
    elif files is not None:
        raise DatasetError(f"{where}.files is valid only for a directory")
    return path, digest, size, kind, files


def _safe_case_path(case_dir: Path, path_text: str, where: str) -> Path:
    declared = Path(path_text)
    if declared.is_absolute() or ".." in declared.parts:
        raise DatasetError(f"{where} must be a case-relative path: {path_text!r}")
    resolved_case = case_dir.resolve()
    resolved = (case_dir / declared).resolve()
    try:
        resolved.relative_to(resolved_case)
    except ValueError as exc:
        raise DatasetError(f"{where} escapes the case directory: {path_text!r}") from exc
    return resolved


def _load_artifacts(
    manifest: Mapping[str, Any],
    case_dir: Path,
    pilot_root: Path,
    required_roles: Sequence[str],
) -> tuple[dict[str, Path], dict[str, dict[str, Any]]]:
    raw = manifest.get("artifacts")
    if not isinstance(raw, (Mapping, list)):
        raise DatasetError(f"{case_dir}/manifest.json: 'artifacts' must be an object or list")

    declared: dict[
        str, tuple[str, tuple[str, str, int, str, int | None]]
    ] = {}
    all_declared: list[tuple[str, tuple[str, str, int, str, int | None]]] = []
    if isinstance(raw, Mapping):
        items: Iterable[tuple[str, Any]] = raw.items()
        for raw_role, value in items:
            if not isinstance(raw_role, str):
                raise DatasetError(f"{case_dir}/manifest.json: artifact role must be a string")
            declaration = _artifact_value(value, f"artifacts.{raw_role}")
            all_declared.append((raw_role, declaration))
            role = _canonical_role(raw_role)
            if role is None:
                continue
            if role in declared:
                raise DatasetError(f"{case_dir}/manifest.json: duplicate artifact role {role!r}")
            declared[role] = (raw_role, declaration)
    else:
        for index, value in enumerate(raw):
            declaration = _artifact_value(value, f"artifacts[{index}]")
            path_text, digest, size, kind, files = declaration
            all_declared.append((f"[{index}]", declaration))
            role = _infer_role(path_text)
            if role is None:
                continue
            if role in declared:
                raise DatasetError(f"{case_dir}/manifest.json: duplicate inferred role {role!r}")
            declared[role] = (f"[{index}]", declaration)

    missing = sorted(set(required_roles) - set(declared))
    if missing:
        raise DatasetError(f"{case_dir}/manifest.json: missing artifact roles: {', '.join(missing)}")

    validated: dict[str, tuple[Path, dict[str, Any]]] = {}
    for raw_role, declaration in all_declared:
        path_text, expected_digest, expected_size, kind, expected_files = declaration
        path = _safe_case_path(case_dir, path_text, f"artifacts.{raw_role}")
        if kind == "directory":
            inventory = _tree_inventory(path, f"artifacts.{raw_role}")
            size = sum(int(item["bytes"]) for item in inventory)
            digest = _inventory_sha256(inventory)
            if len(inventory) != expected_files:
                raise DatasetError(
                    f"artifacts.{raw_role} file count mismatch: expected "
                    f"{expected_files}, got {len(inventory)}"
                )
            metadata = {
                "path": path_text,
                "sha256": digest,
                "bytes": size,
                "kind": "directory",
                "files": len(inventory),
            }
        else:
            if not path.is_file() or path.stat().st_size == 0:
                raise DatasetError(
                    f"artifacts.{raw_role} does not exist or is empty: {path}"
                )
            size = path.stat().st_size
            digest = _sha256(path)
            metadata = {"path": path_text, "sha256": digest, "bytes": size}
            if path.suffix.lower() in {".json", ".log", ".rpt", ".tcl", ".txt"}:
                _read_utf8(path)
        if size != expected_size:
            raise DatasetError(
                f"artifacts.{raw_role} byte count mismatch: expected {expected_size}, got {size}"
            )
        if digest != expected_digest:
            raise DatasetError(
                f"artifacts.{raw_role} SHA256 mismatch: expected {expected_digest}, got {digest}"
            )
        validated[raw_role] = (path, metadata)

    paths: dict[str, Path] = {}
    exported: dict[str, dict[str, Any]] = {}
    for role in required_roles:
        raw_role, declaration = declared[role]
        path_text, _, _, _, _ = declaration
        path, metadata = validated[raw_role]
        try:
            relative = path.relative_to(pilot_root.resolve()).as_posix()
        except ValueError as exc:
            raise DatasetError(f"artifact is outside pilot root: {path}") from exc
        paths[role] = path
        exported[role] = {**metadata, "path": relative}
    return paths, exported


def _require_string(obj: Mapping[str, Any], key: str, where: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DatasetError(f"{where}.{key} must be a non-empty string")
    return value.strip()


def _require_mapping(obj: Mapping[str, Any], key: str, where: str) -> Mapping[str, Any]:
    value = obj.get(key)
    if not isinstance(value, Mapping):
        raise DatasetError(f"{where}.{key} must be an object")
    return value


def _finite_number(obj: Mapping[str, Any], key: str, where: str) -> float:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DatasetError(f"{where}.{key} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise DatasetError(f"{where}.{key} must be finite")
    return result


def _nonnegative_int(obj: Mapping[str, Any], key: str, where: str) -> int:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DatasetError(f"{where}.{key} must be a non-negative integer")
    return value


def _timing_metrics(stage: Mapping[str, Any], stage_name: str) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for check in ("setup", "hold"):
        timing = _require_mapping(stage, check, stage_name)
        result[check] = {
            "wns_ns": _finite_number(timing, "wns_ns", f"{stage_name}.{check}"),
            "tns_ns": _finite_number(timing, "tns_ns", f"{stage_name}.{check}"),
        }
    return result


def _drv_metrics(stage: Mapping[str, Any], stage_name: str) -> dict[str, int]:
    drv = _require_mapping(stage, "drv", stage_name)
    return {
        name: _nonnegative_int(drv, name, f"{stage_name}.drv")
        for name in (
            "max_transition_violations",
            "max_capacitance_violations",
            "max_fanout_violations",
        )
    }


def _drc_metrics(stage: Mapping[str, Any], stage_name: str) -> dict[str, Any]:
    drc = _require_mapping(stage, "drc", stage_name)
    total = _nonnegative_int(drc, "total", f"{stage_name}.drc")
    categories = drc.get("categories")
    if not isinstance(categories, Mapping):
        raise DatasetError(f"{stage_name}.drc.categories must be an object of violation counts")
    normalized: dict[str, int] = {}
    for category, count in categories.items():
        if not isinstance(category, str) or not category:
            raise DatasetError(f"{stage_name}.drc.categories keys must be non-empty strings")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise DatasetError(
                f"{stage_name}.drc.categories.{category} must be a non-negative integer"
            )
        normalized[category] = count
    if sum(normalized.values()) != total:
        raise DatasetError(f"{stage_name}.drc.total must equal the sum of category counts")
    return {"total": total, "categories": dict(sorted(normalized.items()))}


def _connectivity_metrics(stage: Mapping[str, Any], stage_name: str) -> dict[str, int]:
    connectivity = _require_mapping(stage, "connectivity", stage_name)
    return {
        "violations": _nonnegative_int(
            connectivity, "violations", f"{stage_name}.connectivity"
        )
    }


def _true_check(checks: Mapping[str, Any], key: str) -> bool:
    value = checks.get(key)
    if value is not True:
        raise DatasetError(f"metrics.checks.{key} must be true")
    return True


def _validate_metrics(metrics: Any, case_type: str, path: Path) -> dict[str, Any]:
    if not isinstance(metrics, Mapping):
        raise DatasetError(f"{path}: top-level value must be an object")
    if metrics.get("schema_version") != FINALIZER_SCHEMA_VERSION:
        raise DatasetError(
            f"{path}: metrics must carry finalizer schema {FINALIZER_SCHEMA_VERSION}"
        )
    before_raw = _require_mapping(metrics, "before", "metrics")
    after_raw = _require_mapping(metrics, "after", "metrics")

    before_timing = _timing_metrics(before_raw, "metrics.before")
    after_timing = _timing_metrics(after_raw, "metrics.after")
    before_drv = _drv_metrics(before_raw, "metrics.before")
    after_drv = _drv_metrics(after_raw, "metrics.after")
    before_drc = _drc_metrics(before_raw, "metrics.before")
    after_drc = _drc_metrics(after_raw, "metrics.after")
    before_connectivity = _connectivity_metrics(before_raw, "metrics.before")
    after_connectivity = _connectivity_metrics(after_raw, "metrics.after")

    violated_checks = ("setup", "hold") if case_type == "mixed" else (case_type,)
    for check in violated_checks:
        if before_timing[check]["wns_ns"] >= 0:
            raise DatasetError(f"{path}: {case_type} case has no negative before {check} WNS")
        if before_timing[check]["tns_ns"] >= 0:
            raise DatasetError(f"{path}: {case_type} case has no negative before {check} TNS")

    for check in ("setup", "hold"):
        if after_timing[check]["wns_ns"] < MIN_FINAL_SLACK_NS - 1e-12:
            raise DatasetError(
                f"{path}: after {check} WNS must be >= {MIN_FINAL_SLACK_NS:.3f} ns"
            )
        if abs(after_timing[check]["tns_ns"]) > 1e-12:
            raise DatasetError(f"{path}: after {check} TNS must be 0")

    for rule in before_drv:
        if after_drv[rule] > before_drv[rule]:
            raise DatasetError(f"{path}: {rule} regressed after ECO")
    if after_connectivity["violations"] != 0:
        raise DatasetError(f"{path}: connectivity violations remain after ECO")
    if after_drc["total"] > before_drc["total"]:
        raise DatasetError(f"{path}: total DRC count increased after ECO")
    new_drc = sorted(
        category
        for category, count in after_drc["categories"].items()
        if count and before_drc["categories"].get(category, 0) == 0
    )
    if new_drc:
        raise DatasetError(f"{path}: new DRC categories after ECO: {', '.join(new_drc)}")

    replay = _require_mapping(metrics, "replay", "metrics")
    if replay.get("status") != "passed":
        raise DatasetError(f"{path}: metrics.replay.status must be 'passed'")
    runs = _nonnegative_int(replay, "runs", "metrics.replay")
    if runs < 2:
        raise DatasetError(f"{path}: metrics.replay.runs must be at least 2")
    if replay.get("deterministic") is not True:
        raise DatasetError(f"{path}: metrics.replay.deterministic must be true")

    checks = _require_mapping(metrics, "checks", "metrics")
    normalized_checks = {
        key: _true_check(checks, key)
        for key in (
            "constraint_hash_unchanged",
            "functional_audit_passed",
            "primetime_crosscheck_passed",
        )
    }

    return {
        "before": {
            **before_timing,
            "drv": before_drv,
            "drc": before_drc,
            "connectivity": before_connectivity,
        },
        "after": {
            **after_timing,
            "drv": after_drv,
            "drc": after_drc,
            "connectivity": after_connectivity,
        },
        "replay": {
            "status": "passed",
            "runs": runs,
            "deterministic": True,
        },
        "checks": normalized_checks,
    }


def _extract_tcl(answer: str, where: str) -> tuple[str, str]:
    fences = list(re.finditer(r"```([^\n`]*)\n(.*?)```", answer, flags=re.DOTALL))
    if len(fences) != 1 or answer.count("```") != 2:
        raise DatasetError(f"{where}: assistant answer must contain exactly one fenced Tcl block")
    fence = fences[0]
    if fence.group(1).strip().lower() != "tcl":
        raise DatasetError(f"{where}: fenced code language must be exactly 'tcl'")
    diagnosis = answer[: fence.start()].strip()
    trailing = answer[fence.end() :].strip()
    if trailing:
        raise DatasetError(f"{where}: text after the Tcl fence is not allowed")
    if len(diagnosis) < 12:
        raise DatasetError(f"{where}: a substantive diagnosis is required before the Tcl fence")
    sentence_marks = len(re.findall(r"[。！？]|(?<!\d)[.!?](?!\d)", diagnosis))
    if not 2 <= sentence_marks <= 4:
        raise DatasetError(f"{where}: diagnosis must contain 2 to 4 sentences")
    tcl = fence.group(2).strip("\n")
    if not tcl.strip():
        raise DatasetError(f"{where}: Tcl block is empty")
    return diagnosis, tcl


def _normalize_code(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip()


def _validate_hidden_injection(instruction: str, answer: str, injected_tcl: str, fix_tcl: str, where: str) -> None:
    messages = f"{instruction}\n{answer}"
    if re.search(r"(?i)\binject(?:ion)?\.tcl\b", messages):
        raise DatasetError(f"{where}: training messages must not name inject.tcl")
    if re.search(r"(?i)\boriginal_driver_(?:inst|ref)\s*=", messages):
        raise DatasetError(
            f"{where}: training messages expose pre-injection original-driver provenance"
        )
    normalized_inject = _normalize_code(injected_tcl)
    normalized_messages = _normalize_code(messages)
    if normalized_inject and normalized_inject in normalized_messages:
        raise DatasetError(f"{where}: inject.tcl content is embedded in training messages")
    if normalized_inject == _normalize_code(fix_tcl):
        raise DatasetError(f"{where}: fix.tcl must not be identical to inject.tcl")


def _validate_fix_tcl(tcl: str, where: str) -> None:
    for command, pattern in _FORBIDDEN_TCL.items():
        if pattern.search(tcl):
            raise DatasetError(f"{where}: forbidden constraint-changing command: {command}")


def _tcl_literal_option(args: str, option: str, *, where: str) -> str:
    match = re.search(
        rf"(?:^|\s){re.escape(option)}\s+(?:\{{([^{{}}\r\n]+)\}}|([^\s;]+))",
        args,
    )
    if match is None:
        raise DatasetError(f"{where}: missing literal {option} object")
    value = match.group(1) if match.group(1) is not None else match.group(2)
    assert value is not None
    return value


def _validate_fix_object_coverage(
    fix: str,
    context: Mapping[str, Any],
    *,
    case_id: str,
    case_type: str,
    max_eco_cells: int,
) -> set[str]:
    visible_instances = {
        str(cell["inst"])
        for target in context["targets"]
        for cell in target["local_cells"]
    }
    change_commands = list(
        re.finditer(r"(?mi)^\s*ecoChangeCell\b([^\r\n]*)$", fix)
    )
    referenced: set[str] = set()
    for command_number, match in enumerate(change_commands, 1):
        referenced.add(
            _tcl_literal_option(
                match.group(1),
                "-inst",
                where=f"{case_id}: ecoChangeCell command {command_number}",
            )
        )
    hidden = sorted(referenced - visible_instances)
    if hidden:
        raise DatasetError(
            f"{case_id}: fix references instances absent from diagnostic local_cells: "
            f"{', '.join(hidden)}"
        )

    add_commands = list(
        re.finditer(r"(?mi)^\s*ecoAddRepeater\b([^\r\n]*)$", fix)
    )
    if len(change_commands) + len(add_commands) > max_eco_cells:
        raise DatasetError(
            f"{case_id}: physical ECO actions exceed max_eco_cells={max_eco_cells}"
        )
    allowed_roles = {"SETUP", "HOLD"} if case_type == "mixed" else {
        case_type.upper()
    }
    new_instances: list[str] = []
    ordinal_pattern = re.compile(
        rf"^SFT_ECO_{re.escape(case_id)}_(SETUP|HOLD)_([1-9][0-9]*)$"
    )
    for command_number, match in enumerate(add_commands, 1):
        name = _tcl_literal_option(
            match.group(1),
            "-name",
            where=f"{case_id}: ecoAddRepeater command {command_number}",
        )
        parsed = ordinal_pattern.fullmatch(name)
        if parsed is None or parsed.group(1) not in allowed_roles:
            raise DatasetError(
                f"{case_id}: ecoAddRepeater name {name!r} violates the declared convention"
            )
        if int(parsed.group(2)) != command_number:
            raise DatasetError(
                f"{case_id}: ecoAddRepeater ordinals must be continuous from 1"
            )
        new_instances.append(name)
    if len(new_instances) != len(set(new_instances)):
        raise DatasetError(f"{case_id}: ecoAddRepeater names must be unique")

    allowed_names = visible_instances | set(new_instances)
    mentioned_names = set(ECO_INSTANCE_NAME_RE.findall(fix))
    undeclared = sorted(mentioned_names - allowed_names)
    if undeclared:
        raise DatasetError(
            f"{case_id}: fix contains undeclared ECO instance names: "
            f"{', '.join(undeclared)}"
        )
    return allowed_names


def _validate_answer_eco_names(answer: str, allowed_names: set[str], *, case_id: str) -> None:
    mentioned = set(ECO_INSTANCE_NAME_RE.findall(answer))
    undeclared = sorted(mentioned - allowed_names)
    if undeclared:
        raise DatasetError(
            f"{case_id}: answer contains undeclared ECO instance names: "
            f"{', '.join(undeclared)}"
        )


def _analysis_views(manifest: Mapping[str, Any], where: str) -> dict[str, Any]:
    views = _require_mapping(manifest, "analysis_views", where)
    normalized: dict[str, Any] = {}
    for check in ("setup", "hold"):
        value = views.get(check)
        if isinstance(value, str) and value.strip():
            normalized[check] = value.strip()
        elif isinstance(value, list) and value and all(isinstance(item, str) and item for item in value):
            normalized[check] = value
        else:
            raise DatasetError(f"{where}.analysis_views.{check} must be a view name or list")
    return normalized


def _evidence_check_passed(value: Any, where: str) -> None:
    if value is True:
        return
    if isinstance(value, Mapping) and value.get("passed") is True:
        return
    raise DatasetError(f"{where} must pass")


def _diagnostic_token(value: Any, *, where: str, role: bool = False) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise DatasetError(f"{where} must be a non-empty, trimmed string")
    if len(value) > MAX_DIAGNOSTIC_STRING_CHARS:
        raise DatasetError(f"{where} exceeds {MAX_DIAGNOSTIC_STRING_CHARS} characters")
    pattern = (
        r"[A-Za-z][A-Za-z0-9_.:-]*"
        if role
        else r"[A-Za-z0-9_./:@+$\-\[\]<>\\|~^%=,]+"
    )
    if re.fullmatch(pattern, value) is None:
        raise DatasetError(f"{where} contains unsafe or non-object-name characters")
    if "inject" in value.lower():
        raise DatasetError(f"{where} leaks hidden injection terminology")
    if role and any(
        term in value.lower()
        for term in (
            "action",
            "attempt",
            "buffer",
            "delay",
            "downsize",
            "oracle",
            "parameter",
            "repeater",
            "resize",
            "skew",
            "strategy",
            "upsize",
        )
    ):
        raise DatasetError(f"{where} leaks hidden injection action or parameter")
    return value


def _load_diagnostic_context(
    path: Path, *, case_id: str, design: str, max_eco_cells: int
) -> dict[str, Any]:
    if path.stat().st_size > MAX_DIAGNOSTIC_CONTEXT_BYTES:
        raise DatasetError(
            f"{path}: diagnostic context exceeds {MAX_DIAGNOSTIC_CONTEXT_BYTES} bytes"
        )
    raw = _read_json(path)
    expected_top = {
        "schema_version",
        "case_id",
        "design",
        "max_eco_cells",
        "targets",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected_top:
        raise DatasetError(
            f"{path}: diagnostic context must contain exactly {sorted(expected_top)}"
        )
    if raw.get("schema_version") != DIAGNOSTIC_CONTEXT_SCHEMA_VERSION:
        raise DatasetError(f"{path}: unsupported diagnostic-context schema")
    if raw.get("case_id") != case_id or raw.get("design") != design:
        raise DatasetError(f"{path}: diagnostic context is not bound to this case")
    budget = raw.get("max_eco_cells")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget != max_eco_cells:
        raise DatasetError(f"{path}: diagnostic max_eco_cells does not match manifest")
    targets = raw.get("targets")
    if not isinstance(targets, list) or not targets:
        raise DatasetError(f"{path}: diagnostic targets must be a non-empty array")
    if len(targets) > MAX_DIAGNOSTIC_TARGETS:
        raise DatasetError(
            f"{path}: diagnostic target count exceeds {MAX_DIAGNOSTIC_TARGETS}"
        )
    normalized: list[dict[str, Any]] = []
    endpoint_modes: set[tuple[str, str]] = set()
    for index, target in enumerate(targets):
        where = f"{path}: targets[{index}]"
        if not isinstance(target, Mapping) or set(target) != DIAGNOSTIC_TARGET_FIELDS:
            raise DatasetError(
                f"{where} must contain exactly {sorted(DIAGNOSTIC_TARGET_FIELDS)}"
            )
        timing = target.get("timing")
        if timing not in {"late", "early"}:
            raise DatasetError(f"{where}.timing must be late or early")
        endpoint = _diagnostic_token(target.get("endpoint"), where=f"{where}.endpoint")
        endpoint_mode = (timing, endpoint)
        if endpoint_mode in endpoint_modes:
            raise DatasetError(
                f"{path}: duplicate diagnostic target {timing}/{endpoint}"
            )
        endpoint_modes.add(endpoint_mode)
        slack = target.get("slack_ns")
        if (
            isinstance(slack, bool)
            or not isinstance(slack, (int, float))
            or not math.isfinite(float(slack))
            or float(slack) >= 0.0
        ):
            raise DatasetError(f"{where}.slack_ns must be finite and negative")
        driver_inst = _diagnostic_token(
            target.get("driver_inst"), where=f"{where}.driver_inst"
        )
        driver_ref = _diagnostic_token(
            target.get("driver_ref"), where=f"{where}.driver_ref"
        )
        original = target.get("original_driver")
        if not isinstance(original, Mapping) or set(original) != (
            DIAGNOSTIC_ORIGINAL_DRIVER_FIELDS
        ):
            raise DatasetError(
                f"{where}.original_driver must contain exactly "
                f"{sorted(DIAGNOSTIC_ORIGINAL_DRIVER_FIELDS)}"
            )
        original_driver = {
            "inst": _diagnostic_token(
                original.get("inst"), where=f"{where}.original_driver.inst"
            ),
            "ref": _diagnostic_token(
                original.get("ref"), where=f"{where}.original_driver.ref"
            ),
        }
        raw_local_cells = target.get("local_cells")
        if (
            not isinstance(raw_local_cells, list)
            or not raw_local_cells
            or len(raw_local_cells) > MAX_DIAGNOSTIC_LOCAL_CELLS
        ):
            raise DatasetError(
                f"{where}.local_cells must contain 1..{MAX_DIAGNOSTIC_LOCAL_CELLS} cells"
            )
        local_cells: list[dict[str, str]] = []
        local_instances: set[str] = set()
        for cell_index, cell in enumerate(raw_local_cells):
            cell_where = f"{where}.local_cells[{cell_index}]"
            if not isinstance(cell, Mapping) or set(cell) != DIAGNOSTIC_LOCAL_CELL_FIELDS:
                raise DatasetError(
                    f"{cell_where} must contain exactly "
                    f"{sorted(DIAGNOSTIC_LOCAL_CELL_FIELDS)}"
                )
            inst = _diagnostic_token(cell.get("inst"), where=f"{cell_where}.inst")
            ref = _diagnostic_token(cell.get("ref"), where=f"{cell_where}.ref")
            if inst in local_instances:
                raise DatasetError(f"{where}.local_cells contains duplicate instance {inst}")
            local_instances.add(inst)
            local_cells.append({"inst": inst, "ref": ref})
        if local_cells[0] != {"inst": driver_inst, "ref": driver_ref}:
            raise DatasetError(
                f"{where}.local_cells[0] must be the current immediate driver"
            )
        if local_cells[-1]["inst"] != original_driver["inst"]:
            raise DatasetError(
                f"{where}.local_cells must terminate at original_driver.inst"
            )
        normalized.append(
            {
                "role": _diagnostic_token(
                    target.get("role"), where=f"{where}.role", role=True
                ),
                "timing": timing,
                "endpoint": endpoint,
                "beginpoint": _diagnostic_token(
                    target.get("beginpoint"), where=f"{where}.beginpoint"
                ),
                "slack_ns": float(slack),
                "net": _diagnostic_token(target.get("net"), where=f"{where}.net"),
                "driver_pin": _diagnostic_token(
                    target.get("driver_pin"), where=f"{where}.driver_pin"
                ),
                "driver_inst": driver_inst,
                "driver_ref": driver_ref,
                "original_driver": original_driver,
                "local_cells": local_cells,
            }
        )
    return {
        "schema_version": DIAGNOSTIC_CONTEXT_SCHEMA_VERSION,
        "case_id": case_id,
        "design": design,
        "max_eco_cells": max_eco_cells,
        "targets": normalized,
    }


def _load_violation_locality(
    path: Path,
    *,
    case_id: str,
    repair_mode: str,
    case_type: str,
) -> dict[str, Any]:
    if path.stat().st_size > MAX_DIAGNOSTIC_CONTEXT_BYTES:
        raise DatasetError(
            f"{path}: violation locality exceeds {MAX_DIAGNOSTIC_CONTEXT_BYTES} bytes"
        )
    raw = _read_json(path)
    expected_top = {
        "schema_version",
        "case_id",
        "repair_mode",
        "setup",
        "hold",
        "passed",
        "reasons",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected_top:
        raise DatasetError(
            f"{path}: violation locality must contain exactly {sorted(expected_top)}"
        )
    if raw.get("schema_version") != VIOLATION_LOCALITY_SCHEMA_VERSION:
        raise DatasetError(f"{path}: unsupported violation-locality schema")
    if raw.get("case_id") != case_id or raw.get("repair_mode") != repair_mode:
        raise DatasetError(f"{path}: violation locality is not bound to this case")
    if raw.get("passed") is not True or raw.get("reasons") != []:
        raise DatasetError(f"{path}: violation locality is not a clean passing record")

    expected_item_fields = {
        "required",
        "expected_endpoint_count",
        "selected_endpoint_count",
        "violating_endpoint_count",
        "selected_endpoints",
        "selected_endpoint_slacks",
        "violating_endpoints",
        "wns_ns",
        "tns_ns",
        "cardinality_passed",
        "locality_passed",
        "coverage_passed",
        "opposite_headroom_passed",
        "selected_slack_consistency_passed",
    }
    required_checks = {"setup", "hold"} if case_type == "mixed" else {case_type}
    normalized: dict[str, Any] = {
        "schema_version": VIOLATION_LOCALITY_SCHEMA_VERSION,
        "case_id": case_id,
        "repair_mode": repair_mode,
    }
    for check in ("setup", "hold"):
        where = f"{path}: {check}"
        item = raw.get(check)
        if not isinstance(item, Mapping) or set(item) != expected_item_fields:
            raise DatasetError(
                f"{where} must contain exactly {sorted(expected_item_fields)}"
            )
        required = check in required_checks
        if item.get("required") is not required:
            raise DatasetError(f"{where}.required is inconsistent with {case_type}")
        for field in (
            "cardinality_passed",
            "locality_passed",
            "coverage_passed",
            "opposite_headroom_passed",
            "selected_slack_consistency_passed",
        ):
            if item.get(field) is not True:
                raise DatasetError(f"{where}.{field} must pass")
        wns = item.get("wns_ns")
        tns = item.get("tns_ns")
        if (
            isinstance(wns, bool)
            or not isinstance(wns, (int, float))
            or not math.isfinite(float(wns))
            or isinstance(tns, bool)
            or not isinstance(tns, (int, float))
            or not math.isfinite(float(tns))
        ):
            raise DatasetError(f"{where}: WNS/TNS must be finite numbers")

        counts: dict[str, int] = {}
        for field in (
            "expected_endpoint_count",
            "selected_endpoint_count",
            "violating_endpoint_count",
        ):
            value = item.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise DatasetError(f"{where}.{field} must be a nonnegative integer")
            if value > MAX_DIAGNOSTIC_TARGETS:
                raise DatasetError(f"{where}.{field} exceeds {MAX_DIAGNOSTIC_TARGETS}")
            counts[field] = value

        endpoint_lists: dict[str, list[str]] = {}
        for field in ("selected_endpoints", "violating_endpoints"):
            values = item.get(field)
            if not isinstance(values, list):
                raise DatasetError(f"{where}.{field} must be an array")
            parsed = [
                _diagnostic_token(value, where=f"{where}.{field}[{index}]")
                for index, value in enumerate(values)
            ]
            if parsed != sorted(set(parsed)):
                raise DatasetError(f"{where}.{field} must be sorted and unique")
            endpoint_lists[field] = parsed

        selected = endpoint_lists["selected_endpoints"]
        violating = endpoint_lists["violating_endpoints"]
        slack_items = item.get("selected_endpoint_slacks")
        if not isinstance(slack_items, list):
            raise DatasetError(f"{where}.selected_endpoint_slacks must be an array")
        selected_slacks: list[dict[str, Any]] = []
        for index, slack_item in enumerate(slack_items):
            slack_where = f"{where}.selected_endpoint_slacks[{index}]"
            if not isinstance(slack_item, Mapping) or set(slack_item) != {
                "endpoint",
                "slack_ns",
            }:
                raise DatasetError(
                    f"{slack_where} must contain endpoint and slack_ns exactly"
                )
            slack = slack_item.get("slack_ns")
            if (
                isinstance(slack, bool)
                or not isinstance(slack, (int, float))
                or not math.isfinite(float(slack))
            ):
                raise DatasetError(f"{slack_where}.slack_ns must be a finite number")
            selected_slacks.append(
                {
                    "endpoint": _diagnostic_token(
                        slack_item.get("endpoint"), where=f"{slack_where}.endpoint"
                    ),
                    "slack_ns": float(slack),
                }
            )
        slack_endpoints = [entry["endpoint"] for entry in selected_slacks]
        if slack_endpoints != sorted(set(slack_endpoints)):
            raise DatasetError(
                f"{where}.selected_endpoint_slacks must be sorted and endpoint-unique"
            )
        if slack_endpoints != selected:
            raise DatasetError(
                f"{where}: selected endpoint slack coverage does not match selected_endpoints"
            )
        negative_from_exact = [
            entry["endpoint"]
            for entry in selected_slacks
            if float(entry["slack_ns"]) < 0.0
        ]
        if negative_from_exact != violating:
            raise DatasetError(
                f"{where}: exact selected endpoint slacks disagree with violating_endpoints"
            )
        if counts["selected_endpoint_count"] != len(selected):
            raise DatasetError(f"{where}: selected endpoint count does not match its array")
        if counts["violating_endpoint_count"] != len(violating):
            raise DatasetError(f"{where}: violating endpoint count does not match its array")
        expected_count = counts["expected_endpoint_count"]
        if required:
            if expected_count < 1 or selected != violating or len(selected) != expected_count:
                raise DatasetError(
                    f"{where}: required selected/violating endpoint coverage is incomplete"
                )
            exact_values = [float(entry["slack_ns"]) for entry in selected_slacks]
            if abs(min(exact_values) - float(wns)) > 1.0e-6:
                raise DatasetError(
                    f"{where}: exact selected endpoint slacks do not reproduce WNS"
                )
            if abs(sum(exact_values) - float(tns)) > 1.0e-6:
                raise DatasetError(
                    f"{where}: exact selected endpoint slacks do not reproduce TNS"
                )
        elif expected_count != 0 or selected or violating:
            raise DatasetError(f"{where}: non-required timing direction is not clean")

        normalized[check] = {
            "required": required,
            "wns_ns": float(wns),
            "tns_ns": float(tns),
            **counts,
            **endpoint_lists,
            "selected_endpoint_slacks": selected_slacks,
            "cardinality_passed": True,
            "locality_passed": True,
            "coverage_passed": True,
            "opposite_headroom_passed": True,
            "selected_slack_consistency_passed": True,
        }
    normalized["passed"] = True
    normalized["reasons"] = []
    return normalized


def _bind_diagnostic_to_locality(
    *,
    context: Mapping[str, Any],
    locality: Mapping[str, Any],
    before: Mapping[str, Any],
    where: str,
) -> None:
    for check, timing in (("setup", "late"), ("hold", "early")):
        targets = [target for target in context["targets"] if target["timing"] == timing]
        endpoints = sorted(str(target["endpoint"]) for target in targets)
        if endpoints != locality[check]["violating_endpoints"]:
            raise DatasetError(
                f"{where}: diagnostic {check} endpoints do not match violation locality"
            )
        exact_slacks = {
            str(item["endpoint"]): float(item["slack_ns"])
            for item in locality[check]["selected_endpoint_slacks"]
        }
        for target in targets:
            endpoint = str(target["endpoint"])
            if endpoint not in exact_slacks or abs(
                float(target["slack_ns"]) - exact_slacks[endpoint]
            ) > 1.0e-6:
                raise DatasetError(
                    f"{where}: diagnostic {check} slack differs from exact endpoint evidence"
                )
        for field in ("wns_ns", "tns_ns"):
            tolerance = 0.001
            if field == "tns_ns":
                tolerance = max(tolerance, 0.000501 * len(endpoints))
            if abs(float(locality[check][field]) - float(before[check][field])) > (
                tolerance + 1.0e-9
            ):
                raise DatasetError(
                    f"{where}: violation-locality {check} {field} does not match "
                    "before-ECO metrics"
                )
        if not locality[check]["required"]:
            continue
        slacks = [float(target["slack_ns"]) for target in targets]
        reported_wns = float(before[check]["wns_ns"])
        reported_tns = float(before[check]["tns_ns"])
        if abs(min(slacks) - reported_wns) > 0.001000001:
            raise DatasetError(
                f"{where}: diagnostic {check} slack does not reproduce before-ECO WNS"
            )
        tns_tolerance = max(0.001, 0.000501 * len(slacks)) + 1.0e-9
        if abs(sum(slacks) - reported_tns) > tns_tolerance:
            raise DatasetError(
                f"{where}: diagnostic {check} slacks do not reproduce before-ECO TNS"
            )


def _format_number_for_prompt(value: Any) -> str:
    return f"{float(value):+.3f} ns"


def _normalized_verilog_sha256(path: Path, design: str, where: str) -> str:
    text = _read_utf8(path)
    if re.search(rf"(?mi)^\s*module\s+{re.escape(design)}(?:\s|\()", text) is None:
        raise DatasetError(f"{where}: top module {design!r} was not found")
    if re.search(r"(?mi)^\s*endmodule\b", text) is None:
        raise DatasetError(f"{where}: no complete Verilog module was found")
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    body = "\n".join(
        line for line in text.splitlines() if not re.match(r"^\s*//", line)
    )
    canonical = re.sub(r"\s+", " ", body).strip().encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_spef(path: Path, design: str, where: str) -> None:
    text = _read_utf8(path)
    if re.search(r"(?mi)^\*SPEF\s+", text) is None:
        raise DatasetError(f"{where}: missing *SPEF header")
    match = re.search(r'(?mi)^\*DESIGN\s+"?([^"\s]+)"?\s*$', text)
    if match is None or match.group(1) != design:
        raise DatasetError(f"{where}: SPEF design does not match {design!r}")
    if re.search(r"(?mi)^\*D_NET\s+", text) is None:
        raise DatasetError(f"{where}: SPEF contains no *D_NET record")


def _baseline_provenance(
    manifest: Mapping[str, Any], qualification_path: Path, design: str, where: str
) -> dict[str, Any]:
    raw = manifest.get("baseline_provenance")
    if not isinstance(raw, Mapping):
        raise DatasetError(f"{where}: baseline_provenance must be an object")
    normalized: dict[str, Any] = {}
    for key in (
        "baseline_sha256",
        "catalog_sha256",
        "checksum_manifest_sha256",
        "qualification_sha256",
    ):
        value = raw.get(key)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise DatasetError(f"{where}: baseline_provenance.{key} is invalid")
        normalized[key] = value
    expected = {
        "qualification_status": "QUALIFIED_CANDIDATE",
        "technology_classification": "derived_non_signoff",
        "gold_status": False,
        "signoff_eligible": False,
    }
    for key, value in expected.items():
        if raw.get(key) != value:
            raise DatasetError(f"{where}: baseline_provenance.{key} is not {value!r}")
        normalized[key] = value
    sources = raw.get("source_artifacts")
    if not isinstance(sources, Mapping):
        raise DatasetError(f"{where}: qualified source_artifacts are missing")
    normalized_sources: dict[str, dict[str, Any]] = {}
    for role in QUALIFIED_LIBERTY_ROLES.values():
        item = sources.get(role)
        if not isinstance(item, Mapping):
            raise DatasetError(f"{where}: qualified {role} is missing")
        digest = item.get("sha256")
        size = item.get("bytes")
        source_path = item.get("source_path")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise DatasetError(f"{where}: qualified {role} SHA256 is invalid")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise DatasetError(f"{where}: qualified {role} byte count is invalid")
        if not isinstance(source_path, str) or not source_path.startswith("/"):
            raise DatasetError(f"{where}: qualified {role} source path is invalid")
        normalized_sources[role] = {
            "sha256": digest,
            "bytes": size,
            "source_path": source_path,
        }
    normalized["source_artifacts"] = normalized_sources

    if _sha256(qualification_path) != normalized["qualification_sha256"]:
        raise DatasetError(f"{qualification_path}: SHA256 differs from baseline_provenance")
    qualification = _read_json(qualification_path)
    if not isinstance(qualification, Mapping):
        raise DatasetError(f"{qualification_path}: qualification must be an object")
    qualification_expected = {
        "schema_version": BASELINE_QUALIFICATION_SCHEMA_VERSION,
        "status": "QUALIFIED_CANDIDATE",
        "gold_status": False,
        "signoff_eligible": False,
        "technology_classification": "derived_non_signoff",
        "top": design,
    }
    for key, value in qualification_expected.items():
        if qualification.get(key) != value:
            raise DatasetError(f"{qualification_path}: qualification {key} is invalid")
    qualification_sources = qualification.get("source_artifacts")
    promotion = qualification.get("promotion_bindings")
    if not isinstance(qualification_sources, Mapping) or not isinstance(promotion, Mapping):
        raise DatasetError(f"{qualification_path}: qualification source bindings are missing")
    if not isinstance(promotion.get("source_inputs"), Mapping):
        raise DatasetError(f"{qualification_path}: promotion source_inputs are missing")
    for role, expected_item in normalized_sources.items():
        if qualification_sources.get(role) != expected_item:
            raise DatasetError(f"{qualification_path}: {role} differs from manifest")
        if promotion["source_inputs"].get(role) != expected_item:
            raise DatasetError(f"{qualification_path}: promotion {role} binding differs")
    return normalized


def _validate_guard(
    path: Path, *, case_id: str, provenance: Mapping[str, Any]
) -> dict[str, Any]:
    raw = _read_json(path)
    if not isinstance(raw, Mapping) or raw.get("schema_version") != BASELINE_GUARD_SCHEMA_VERSION:
        raise DatasetError(f"{path}: unsupported baseline-guard schema")
    expected = {
        "case_id": case_id,
        "baseline_sha256": provenance["baseline_sha256"],
        "catalog_sha256": provenance["catalog_sha256"],
        "qualification_sha256": provenance["qualification_sha256"],
        "checksum_manifest_sha256": provenance["checksum_manifest_sha256"],
        "qualification_status": provenance["qualification_status"],
        "technology_classification": provenance["technology_classification"],
        "gold_status": False,
        "signoff_eligible": False,
        "passed": True,
    }
    for key, value in expected.items():
        if raw.get(key) != value:
            raise DatasetError(f"{path}: baseline guard {key} is unbound")
    minimum = raw.get("minimum_wns_ns")
    if isinstance(minimum, bool) or not isinstance(minimum, (int, float)) or minimum < 0.02:
        raise DatasetError(f"{path}: invalid baseline minimum WNS")
    for mode in ("setup", "hold"):
        item = raw.get(mode)
        if not isinstance(item, Mapping):
            raise DatasetError(f"{path}: baseline {mode} timing is missing")
        wns = item.get("wns_ns")
        tns = item.get("tns_ns")
        if (
            isinstance(wns, bool)
            or not isinstance(wns, (int, float))
            or float(wns) < float(minimum)
            or isinstance(tns, bool)
            or not isinstance(tns, (int, float))
            or abs(float(tns)) > 1.0e-9
        ):
            raise DatasetError(f"{path}: baseline {mode} timing is not clean")
    return dict(raw)


def _validate_injection_binding(
    path: Path, *, case_id: str, provenance: Mapping[str, Any]
) -> dict[str, Any]:
    raw = _read_json(path)
    if (
        not isinstance(raw, Mapping)
        or raw.get("schema_version") != INJECTION_PROVENANCE_SCHEMA_VERSION
        or raw.get("case_id") != case_id
        or raw.get("calibration_status") != "FROZEN"
        or raw.get("status") != "passed"
    ):
        raise DatasetError(f"{path}: injection provenance is not frozen passing Gold evidence")
    expected = {
        key: provenance[key]
        for key in (
            "baseline_sha256",
            "catalog_sha256",
            "qualification_sha256",
            "checksum_manifest_sha256",
            "qualification_status",
            "technology_classification",
        )
    }
    if raw.get("baseline_binding") != expected:
        raise DatasetError(f"{path}: injection baseline_binding differs from manifest")
    return dict(raw)


def _validate_run_status_binding(
    path: Path, *, replay: int, provenance: Mapping[str, Any]
) -> None:
    raw = _read_json(path)
    expected = {
        "replay": replay,
        "innovus_exit_code": 0,
        "fetched": True,
        "passed": True,
        "expected_marker": "SFT_CASE_PASSED",
        "marker_present": True,
        "baseline_sha256": provenance["baseline_sha256"],
        "catalog_sha256": provenance["catalog_sha256"],
        "baseline_qualification_sha256": provenance["qualification_sha256"],
        "baseline_checksum_manifest_sha256": provenance["checksum_manifest_sha256"],
        "baseline_status": provenance["qualification_status"],
        "guest_baseline_checksums_verified": True,
        "mode": "GOLD_REPLAY",
        "gold_eligible": True,
    }
    if not isinstance(raw, Mapping):
        raise DatasetError(f"{path}: run status must be an object")
    for key, value in expected.items():
        if raw.get(key) != value:
            raise DatasetError(f"{path}: run status {key} is not {value!r}")


def _validate_host_ssh_success_log(path: Path, *, case_id: str) -> None:
    """Require one exact host-captured Tcl success marker and no lookalikes."""

    text = _read_utf8(path)
    expected = f"SFT_CASE_PASSED {case_id}"
    marker_lines = [
        line for line in text.splitlines() if "SFT_CASE_PASSED" in line
    ]
    if marker_lines != [expected]:
        raise DatasetError(
            f"{path}: host SSH log must contain exactly one typed marker line "
            f"{expected!r} and no other SFT_CASE_PASSED text"
        )


def _validate_physical_audit(
    path: Path, *, case_id: str, before: Mapping[str, Any], after: Mapping[str, Any]
) -> None:
    raw = _read_json(path)
    if (
        not isinstance(raw, Mapping)
        or raw.get("schema_version") != PHYSICAL_NO_REGRESSION_SCHEMA_VERSION
        or raw.get("case_id") != case_id
        or raw.get("per_category_no_regression") is not True
        or raw.get("passed") is not True
    ):
        raise DatasetError(f"{path}: physical no-regression audit is invalid")
    def drv(stage: Mapping[str, Any]) -> dict[str, int]:
        values = stage["drv"]
        return {
            "max_transition": int(values["max_transition_violations"]),
            "max_capacitance": int(values["max_capacitance_violations"]),
            "max_fanout": int(values["max_fanout_violations"]),
        }
    expected = {
        "drv_before": drv(before),
        "drv_after": drv(after),
        "drc_before": before["drc"],
        "drc_after": after["drc"],
    }
    for key, value in expected.items():
        if raw.get(key) != value:
            raise DatasetError(f"{path}: {key} differs from canonical metrics")


def _load_and_bind_primetime(
    path: Path,
    *,
    design: str,
    artifacts: Mapping[str, Path],
    constraint_hashes: Mapping[str, str],
    baseline_provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify PT v4 evidence, then bind it to canonical replay-1 artifacts."""

    verifier = _primetime_verifier()
    try:
        summary = verifier.load_and_verify_summary(path)
    except verifier.CrosscheckError as exc:
        raise DatasetError(f"{path}: invalid PrimeTime v4 crosscheck: {exc}") from exc
    if not isinstance(summary, Mapping) or summary.get("schema_version") != (
        PRIMETIME_SCHEMA_VERSION
    ):
        raise DatasetError(f"{path}: unsupported PrimeTime crosscheck schema")
    if summary.get("passed") is not True:
        raise DatasetError(f"{path}: PrimeTime setup/hold crosscheck did not pass")

    root = path.resolve().parent
    inputs = summary.get("inputs")
    adaptation = summary.get("sdc_adaptation")
    corners = summary.get("corners")
    if not all(isinstance(item, Mapping) for item in (inputs, adaptation, corners)):
        raise DatasetError(f"{path}: PrimeTime v4 binding evidence is incomplete")
    assert isinstance(inputs, Mapping)
    assert isinstance(adaptation, Mapping)
    assert isinstance(corners, Mapping)
    if adaptation.get("top") != design:
        raise DatasetError(f"{path}: SDC adaptation top is not bound to {design}")
    for mode in ("setup", "hold"):
        corner = corners.get(mode)
        if not isinstance(corner, Mapping) or corner.get("top") != design:
            raise DatasetError(f"{path}: PrimeTime {mode} corner is not bound to {design}")

        constraint_role = f"constraint_{mode}_after"
        stable_raw_role = f"pt_input_{mode}_sdc"
        canonical_raw = artifacts[constraint_role].resolve()
        if artifacts[stable_raw_role].resolve() != canonical_raw:
            raise DatasetError(
                f"{path}: manifest {stable_raw_role} is not the replay-1 {mode} SDC"
            )
        raw_digest = constraint_hashes[mode]
        raw_size = canonical_raw.stat().st_size
        raw_input_role = f"{mode}_sdc"
        raw_input = inputs.get(raw_input_role)
        if not isinstance(raw_input, Mapping) or any(
            raw_input.get(key) != expected
            for key, expected in (
                ("sha256", raw_digest),
                ("source_sha256", raw_digest),
                ("bytes", raw_size),
                ("source_bytes", raw_size),
            )
        ):
            raise DatasetError(
                f"{path}: PrimeTime raw {mode} SDC is not bound to replay-1 constraints"
            )
        staged_raw = _safe_case_path(
            root,
            str(raw_input.get("path", "")),
            f"{path}: inputs.{raw_input_role}.path",
        )
        if staged_raw.read_bytes() != canonical_raw.read_bytes():
            raise DatasetError(
                f"{path}: staged raw {mode} SDC differs from replay-1 constraints"
            )

        mode_adaptation = adaptation.get(mode)
        if not isinstance(mode_adaptation, Mapping):
            raise DatasetError(f"{path}: missing {mode} SDC adaptation")
        adapted_item = mode_adaptation.get("adapted_artifact")
        if not isinstance(adapted_item, Mapping):
            raise DatasetError(f"{path}: missing {mode} adapted SDC artifact")
        stable_adapted_role = f"pt_input_{mode}_sdc_adapted"
        stable_adapted = artifacts[stable_adapted_role].resolve()
        try:
            stable_relative = stable_adapted.relative_to(root).as_posix()
        except ValueError as exc:
            raise DatasetError(
                f"{path}: manifest {stable_adapted_role} escapes the case root"
            ) from exc
        expected_adapted = {
            "path": stable_relative,
            "sha256": _sha256(stable_adapted),
            "bytes": stable_adapted.stat().st_size,
        }
        if any(adapted_item.get(key) != value for key, value in expected_adapted.items()):
            raise DatasetError(
                f"{path}: manifest {stable_adapted_role} path/hash/bytes do not "
                "match the verified deterministic adaptation"
            )

    for input_role, qualification_role in QUALIFIED_LIBERTY_ROLES.items():
        item = inputs.get(input_role)
        qualified = baseline_provenance["source_artifacts"][qualification_role]
        if not isinstance(item, Mapping) or any(
            item.get(key) != qualified["sha256"]
            for key in ("sha256", "source_sha256")
        ) or any(
            item.get(key) != qualified["bytes"]
            for key in ("bytes", "source_bytes")
        ):
            raise DatasetError(
                f"{path}: PrimeTime {input_role} is not bound to {qualification_role}"
            )
    return dict(summary)


def _validate_gold_evidence(
    *,
    case_id: str,
    case_type: str,
    design: str,
    repair_mode: str,
    max_eco_cells: int,
    answer: str,
    artifacts: Mapping[str, Path],
    raw_metrics: Mapping[str, Any],
    baseline_provenance: Mapping[str, Any],
) -> None:
    """Cross-bind the finalizer's strongest canonical evidence.

    The artifact loader has already checked every manifest SHA256 and byte
    count.  These checks additionally bind the typed contents to the case,
    concrete fix, active constraints, and metrics rather than trusting file
    names or three booleans in ``metrics.json``.
    """

    before_metrics = raw_metrics.get("before")
    after_metrics = raw_metrics.get("after")
    if not isinstance(before_metrics, Mapping) or not isinstance(after_metrics, Mapping):
        raise DatasetError(f"{case_id}: canonical before/after metrics are missing")

    guards = [
        _validate_guard(
            artifacts[role], case_id=case_id, provenance=baseline_provenance
        )
        for role in ("baseline_guard", "replay_2_baseline_guard")
    ]
    for mode in ("setup", "hold"):
        for field in ("wns_ns", "tns_ns"):
            if abs(float(guards[0][mode][field]) - float(guards[1][mode][field])) > 0.001000001:
                raise DatasetError(
                    f"{case_id}: baseline guard replay {mode} {field} differs by more than 1 ps"
                )
    injections = [
        _validate_injection_binding(
            artifacts[role], case_id=case_id, provenance=baseline_provenance
        )
        for role in ("injection_provenance", "replay_2_injection_provenance")
    ]
    if injections[0].get("strategy") != injections[1].get("strategy"):
        raise DatasetError(f"{case_id}: replay injection strategy differs")
    for replay, role in ((1, "run_status"), (2, "replay_2_run_status")):
        _validate_run_status_binding(
            artifacts[role], replay=replay, provenance=baseline_provenance
        )
    for log_role, status_role in (
        ("host_ssh_log", "run_status"),
        ("replay_2_host_ssh_log", "replay_2_run_status"),
    ):
        expected_log = artifacts[status_role].parent / "logs" / "host_ssh.log"
        if artifacts[log_role].resolve() != expected_log.resolve():
            raise DatasetError(
                f"{case_id}: {log_role} is not bound to its canonical replay log path"
            )
        _validate_host_ssh_success_log(artifacts[log_role], case_id=case_id)
    for role in ("success_marker", "replay_2_success_marker"):
        if case_id not in _read_utf8(artifacts[role]):
            raise DatasetError(f"{artifacts[role]}: success marker does not name {case_id}")
    _validate_physical_audit(
        artifacts["physical_no_regression"],
        case_id=case_id,
        before=before_metrics,
        after=after_metrics,
    )
    _validate_physical_audit(
        artifacts["replay_2_physical_no_regression"],
        case_id=case_id,
        before=before_metrics,
        after=after_metrics,
    )
    for role in ("before_netlist", "replay_2_before_netlist"):
        _normalized_verilog_sha256(artifacts[role], design, role)
    for role in (
        "setup_spef_before",
        "hold_spef_before",
        "replay_2_setup_spef_before",
        "replay_2_hold_spef_before",
    ):
        _validate_spef(artifacts[role], design, role)
    for wrapper_role, tree_role in (
        ("violating_checkpoint", "violating_checkpoint_data"),
        ("replay_2_violating_checkpoint", "replay_2_violating_checkpoint_data"),
    ):
        wrapper = _read_utf8(artifacts[wrapper_role])
        if artifacts[tree_role].name not in wrapper or not re.search(
            r"(?m)^\s*(?:read_db|restoreDesign)\b", wrapper
        ):
            raise DatasetError(f"{artifacts[wrapper_role]}: invalid checkpoint wrapper")
    for role in ("check_design_after", "replay_2_check_design_after"):
        primary = artifacts[role] / f"{design}.main.htm.ascii"
        if not primary.is_file() or primary.stat().st_size == 0:
            raise DatasetError(f"{artifacts[role]}: checkDesign main ASCII is missing")
        text = _read_utf8(primary)
        if "checkDesign" not in text or "-all" not in text or "-outDir" not in text:
            raise DatasetError(f"{primary}: checkDesign command binding is missing")

    before_constraint = _sha256(artifacts["constraint_before"])
    after_constraint = _sha256(artifacts["constraint_after"])
    if before_constraint != after_constraint:
        raise DatasetError(f"{case_id}: before/after constraint SHA256 differs")
    constraint_hashes: dict[str, str] = {}
    for mode in ("setup", "hold"):
        before_hash = _sha256(artifacts[f"constraint_{mode}_before"])
        after_hash = _sha256(artifacts[f"constraint_{mode}_after"])
        if before_hash != after_hash:
            raise DatasetError(
                f"{case_id}: {mode} before/after constraint SHA256 differs"
            )
        constraint_hashes[mode] = before_hash
    if before_constraint != constraint_hashes["setup"]:
        raise DatasetError(f"{case_id}: legacy constraint SDC is not the setup-view copy")

    audit_path = artifacts["functional_audit"]
    audit = _read_json(audit_path)
    if not isinstance(audit, Mapping) or audit.get("schema_version") != (
        FUNCTIONAL_AUDIT_SCHEMA_VERSION
    ):
        raise DatasetError(f"{audit_path}: unsupported functional-audit schema")
    if (
        audit.get("case_id") != case_id
        or audit.get("design") != design
        or audit.get("repair_mode") != repair_mode
        or audit.get("passed") is not True
    ):
        raise DatasetError(f"{audit_path}: functional audit is not bound to this Gold case")
    audit_checks = audit.get("checks")
    if not isinstance(audit_checks, Mapping) or not audit_checks:
        raise DatasetError(f"{audit_path}: functional audit checks are missing")
    for name, value in audit_checks.items():
        _evidence_check_passed(value, f"{audit_path}: checks.{name}")

    diagnostic_path = artifacts["diagnostic_context"]
    diagnostic = _load_diagnostic_context(
        diagnostic_path,
        case_id=case_id,
        design=design,
        max_eco_cells=max_eco_cells,
    )
    allowed_answer_eco_names = _validate_fix_object_coverage(
        _read_utf8(artifacts["fix_tcl"]),
        diagnostic,
        case_id=case_id,
        case_type=case_type,
        max_eco_cells=max_eco_cells,
    )
    _validate_answer_eco_names(
        answer,
        allowed_answer_eco_names,
        case_id=case_id,
    )
    locality_path = artifacts["violation_locality"]
    locality = _load_violation_locality(
        locality_path,
        case_id=case_id,
        repair_mode=repair_mode,
        case_type=case_type,
    )
    _bind_diagnostic_to_locality(
        context=diagnostic,
        locality=locality,
        before=before_metrics,
        where=case_id,
    )

    comparison_path = artifacts["replay_comparison"]
    comparison = _read_json(comparison_path)
    if not isinstance(comparison, Mapping) or comparison.get("schema_version") != (
        REPLAY_COMPARISON_SCHEMA_VERSION
    ):
        raise DatasetError(f"{comparison_path}: unsupported replay-comparison schema")
    if (
        comparison.get("case_id") != case_id
        or comparison.get("status") != "passed"
        or comparison.get("runs") != 2
        or comparison.get("deterministic") is not True
    ):
        raise DatasetError(f"{comparison_path}: replay comparison is not passing Gold evidence")
    if comparison.get("constraint_sdc_sha256") != before_constraint:
        raise DatasetError(f"{comparison_path}: replay comparison is not bound to the SDC")
    if comparison.get("constraint_sdc_sha256_by_mode") != constraint_hashes:
        raise DatasetError(
            f"{comparison_path}: replay comparison is not bound to setup/hold SDCs"
        )
    if comparison.get("diagnostic_context_sha256") != _sha256(diagnostic_path):
        raise DatasetError(
            f"{comparison_path}: replay comparison is not bound to diagnostic context"
        )
    if comparison.get("violation_locality_sha256") != _sha256(locality_path):
        raise DatasetError(
            f"{comparison_path}: replay comparison is not bound to violation locality"
        )
    if comparison.get("concrete_fix_sha256") != _sha256(artifacts["fix_tcl"]):
        raise DatasetError(f"{comparison_path}: replay comparison is not bound to fix.tcl")
    list_bindings = {
        "baseline_guard_sha256": ("baseline_guard", "replay_2_baseline_guard"),
        "run_status_sha256": ("run_status", "replay_2_run_status"),
        "violating_checkpoint_sha256": (
            "violating_checkpoint",
            "replay_2_violating_checkpoint",
        ),
        "before_netlist_raw_sha256": ("before_netlist", "replay_2_before_netlist"),
        "setup_spef_before_sha256": (
            "setup_spef_before",
            "replay_2_setup_spef_before",
        ),
        "hold_spef_before_sha256": (
            "hold_spef_before",
            "replay_2_hold_spef_before",
        ),
        "injection_provenance_sha256": (
            "injection_provenance",
            "replay_2_injection_provenance",
        ),
    }
    for key, roles in list_bindings.items():
        expected = [_sha256(artifacts[role]) for role in roles]
        if comparison.get(key) != expected:
            raise DatasetError(f"{comparison_path}: {key} is not bound to canonical evidence")
    tree_bindings = {
        "violating_checkpoint_tree_sha256": (
            "violating_checkpoint_data",
            "replay_2_violating_checkpoint_data",
        ),
        "check_design_tree_sha256": (
            "check_design_after",
            "replay_2_check_design_after",
        ),
    }
    for key, roles in tree_bindings.items():
        expected = [_tree_sha256(artifacts[role], role) for role in roles]
        if comparison.get(key) != expected:
            raise DatasetError(f"{comparison_path}: {key} is not bound to canonical trees")
    primary_hashes = [
        _sha256(artifacts[role] / f"{design}.main.htm.ascii")
        for role in ("check_design_after", "replay_2_check_design_after")
    ]
    if comparison.get("check_design_primary_sha256") != primary_hashes:
        raise DatasetError(f"{comparison_path}: checkDesign primary reports are unbound")
    if comparison.get("physical_no_regression_sha256") != _sha256(
        artifacts["physical_no_regression"]
    ) or comparison.get("physical_no_regression_sha256") != _sha256(
        artifacts["replay_2_physical_no_regression"]
    ):
        raise DatasetError(f"{comparison_path}: physical audit replay binding differs")
    normalized_before = _normalized_verilog_sha256(
        artifacts["before_netlist"], design, "before_netlist"
    )
    if comparison.get("normalized_before_netlist_sha256") != normalized_before:
        raise DatasetError(f"{comparison_path}: normalized before netlist is unbound")
    if _normalized_verilog_sha256(
        artifacts["replay_2_before_netlist"], design, "replay_2_before_netlist"
    ) != normalized_before:
        raise DatasetError(f"{comparison_path}: normalized before netlists differ")
    if comparison.get("baseline_provenance") != baseline_provenance:
        raise DatasetError(f"{comparison_path}: baseline provenance differs from manifest")

    if repair_mode == "native":
        native_path = artifacts["native_cell_diff"]
        commands = [
            line.strip()
            for line in _read_utf8(native_path).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if len(commands) != 1 or re.match(
            r"^set\s+::SFT_NATIVE_CELL_DIFF(?:\s|$)", commands[0]
        ) is None:
            raise DatasetError(f"{native_path}: malformed native cell-diff evidence")
        if comparison.get("native_cell_diff_sha256") != _sha256(native_path):
            raise DatasetError(
                f"{comparison_path}: replay comparison is not bound to native cell diff"
            )
        replay_two_diff = artifacts["replay_2_native_cell_diff"]
        if _sha256(replay_two_diff) != _sha256(native_path):
            raise DatasetError(f"{case_id}: native cell diff differs across replays")

        expected_terms = [str(target["endpoint"]) for target in diagnostic["targets"]]
        terms_paths = (
            artifacts["native_selected_terms"],
            artifacts["replay_2_native_selected_terms"],
        )
        for terms_path in terms_paths:
            terms_text = _read_utf8(terms_path)
            if (
                "\r" in terms_text
                or not terms_text.endswith("\n")
                or terms_text.startswith("\n")
                or terms_text.splitlines() != expected_terms
            ):
                raise DatasetError(
                    f"{terms_path}: native selectedTerms do not exactly match "
                    "diagnostic endpoints"
                )
        if _sha256(terms_paths[0]) != _sha256(terms_paths[1]):
            raise DatasetError(f"{case_id}: native selectedTerms differ across replays")
        if comparison.get("native_selected_terms_sha256") != _sha256(terms_paths[0]):
            raise DatasetError(
                f"{comparison_path}: replay comparison is not bound to native selectedTerms"
            )

        fix_text = _read_utf8(artifacts["fix_tcl"])
        opt_commands = list(
            re.finditer(r"(?mi)^\s*optDesign\b([^\r\n]*)$", fix_text)
        )
        if len(opt_commands) != 1 or re.search(
            r"(?mi)^\s*setEcoMode\b", fix_text
        ):
            raise DatasetError(
                f"{case_id}: native fix requires one optDesign outside ECO batch mode"
            )
        selected_file = _tcl_literal_option(
            opt_commands[0].group(1),
            "-selectedTerms",
            where=f"{case_id}: native optDesign",
        )
        if selected_file != NATIVE_SELECTED_TERMS_RELATIVE:
            raise DatasetError(
                f"{case_id}: native optDesign must name {NATIVE_SELECTED_TERMS_RELATIVE}"
            )
        expected_mode = "-setup" if case_type == "setup" else "-hold"
        args = opt_commands[0].group(1)
        for option in ("-postRoute", expected_mode, "-incr"):
            if re.search(rf"(?:^|\s){re.escape(option)}(?:\s|$)", args) is None:
                raise DatasetError(f"{case_id}: native optDesign is missing {option}")
        opposite_mode = "-hold" if expected_mode == "-setup" else "-setup"
        if re.search(rf"(?:^|\s){re.escape(opposite_mode)}(?:\s|$)", args):
            raise DatasetError(
                f"{case_id}: native optDesign must not include {opposite_mode}"
            )

    pt_path = artifacts["primetime_crosscheck"]
    pt = _load_and_bind_primetime(
        pt_path,
        design=design,
        artifacts=artifacts,
        constraint_hashes=constraint_hashes,
        baseline_provenance=baseline_provenance,
    )

    crosschecks = raw_metrics.get("crosschecks")
    if not isinstance(crosschecks, Mapping):
        raise DatasetError(f"{case_id}: finalizer crosschecks are missing from metrics")
    if crosschecks.get("replay") != comparison:
        raise DatasetError(f"{case_id}: metrics replay crosscheck differs from its artifact")
    constraints = crosschecks.get("constraints")
    if (
        not isinstance(constraints, Mapping)
        or constraints.get("sha256") != before_constraint
        or constraints.get("sha256_by_mode") != constraint_hashes
    ):
        raise DatasetError(f"{case_id}: metrics constraints crosscheck is not bound to its SDC")
    functional = crosschecks.get("functional_audit")
    if (
        not isinstance(functional, Mapping)
        or functional.get("schema_version") != FUNCTIONAL_AUDIT_SCHEMA_VERSION
        or functional.get("passed") is not True
    ):
        raise DatasetError(f"{case_id}: metrics functional-audit crosscheck is invalid")
    primetime = crosschecks.get("primetime")
    if not isinstance(primetime, Mapping) or dict(primetime) != pt:
        raise DatasetError(
            f"{case_id}: metrics PrimeTime crosscheck differs from its verified artifact"
        )
    diagnostic_crosscheck = crosschecks.get("diagnostic_context")
    if (
        not isinstance(diagnostic_crosscheck, Mapping)
        or diagnostic_crosscheck.get("schema_version")
        != DIAGNOSTIC_CONTEXT_SCHEMA_VERSION
        or diagnostic_crosscheck.get("sha256") != _sha256(diagnostic_path)
    ):
        raise DatasetError(f"{case_id}: metrics diagnostic-context crosscheck is invalid")
    locality_crosscheck = crosschecks.get("violation_locality")
    if (
        not isinstance(locality_crosscheck, Mapping)
        or locality_crosscheck.get("schema_version")
        != VIOLATION_LOCALITY_SCHEMA_VERSION
        or locality_crosscheck.get("sha256") != _sha256(locality_path)
    ):
        raise DatasetError(f"{case_id}: metrics violation-locality crosscheck is invalid")
    baseline_crosscheck = crosschecks.get("baseline_provenance")
    if not isinstance(baseline_crosscheck, Mapping):
        raise DatasetError(f"{case_id}: metrics baseline-provenance crosscheck is missing")
    expected_baseline_crosscheck = {
        **baseline_provenance,
        "baseline_guard_sha256": comparison["baseline_guard_sha256"],
        "qualification_artifact_sha256": _sha256(artifacts["baseline_qualification"]),
    }
    if dict(baseline_crosscheck) != expected_baseline_crosscheck:
        raise DatasetError(f"{case_id}: metrics baseline provenance is unbound")
    physical_crosscheck = crosschecks.get("physical_no_regression")
    if physical_crosscheck != {
        "schema_version": PHYSICAL_NO_REGRESSION_SCHEMA_VERSION,
        "sha256": comparison["physical_no_regression_sha256"],
    }:
        raise DatasetError(f"{case_id}: metrics physical no-regression is unbound")

    # The messages must expose every repair-facing object, but no hidden
    # injection action or parameter.  The finalizer owns the exact prose;
    # this check binds the exported prompt back to all typed context values.
    instruction = _read_utf8(artifacts["instruction"]) if "instruction" in artifacts else ""
    if instruction:
        if f"ECO cell 上限为 {max_eco_cells}" not in instruction:
            raise DatasetError(f"{case_id}: instruction omits max_eco_cells")
        naming_convention = f"SFT_ECO_{case_id}_<SETUP|HOLD>_<ordinal>"
        if naming_convention not in instruction:
            raise DatasetError(f"{case_id}: instruction omits deterministic ECO naming")
        for target in diagnostic["targets"]:
            expected_fragments = {
                "role": f"role={target['role']}",
                "timing": f"timing={target['timing']}",
                "slack_ns": f"slack={_format_number_for_prompt(target['slack_ns'])}",
                "endpoint": f"endpoint={target['endpoint']}",
                "beginpoint": f"beginpoint={target['beginpoint']}",
                "net": f"net={target['net']}",
                "driver_pin": f"driver_pin={target['driver_pin']}",
                "driver_inst": f"driver_inst={target['driver_inst']}",
                "driver_ref": f"driver_ref={target['driver_ref']}",
                "local_cells": "local_cells=["
                + ",".join(
                    f"{cell['inst']}(ref={cell['ref']})"
                    for cell in target["local_cells"]
                )
                + "]",
            }
            for field, fragment in expected_fragments.items():
                if fragment not in instruction:
                    raise DatasetError(
                        f"{case_id}: instruction omits diagnostic target field {field}"
                    )


def _load_case(case_dir: Path, pilot_root: Path) -> dict[str, Any]:
    manifest_path = case_dir / "manifest.json"
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, Mapping):
        raise DatasetError(f"{manifest_path}: top-level value must be an object")
    where = manifest_path.as_posix()

    case_id = _require_string(manifest, "id", where)
    if case_id != case_dir.name:
        raise DatasetError(f"{where}: id {case_id!r} must match directory {case_dir.name!r}")
    case_type = _require_string(manifest, "type", where).lower()
    if case_type not in EXPECTED_MIX:
        raise DatasetError(f"{where}: type must be setup, hold, or mixed")
    if case_id not in EXPECTED_IDS_BY_TYPE[case_type]:
        expected = ", ".join(EXPECTED_IDS_BY_TYPE[case_type])
        raise DatasetError(
            f"{where}: ID {case_id!r} does not match type {case_type!r}; expected one of {expected}"
        )
    difficulty = _require_string(manifest, "difficulty", where).lower()
    if difficulty not in VALID_DIFFICULTIES:
        raise DatasetError(f"{where}: difficulty must be easy, medium, or hard")
    design = _require_string(manifest, "design", where)
    tool_version = _require_string(manifest, "tool_version", where)
    views = _analysis_views(manifest, where)
    if manifest.get("status") != "gold":
        raise DatasetError(f"{where}: manifest.status must be 'gold'")
    if manifest.get("finalizer_schema") != FINALIZER_SCHEMA_VERSION:
        raise DatasetError(
            f"{where}: manifest.finalizer_schema must be {FINALIZER_SCHEMA_VERSION}"
        )
    repair_mode = _require_string(manifest, "repair_mode", where).lower()
    if repair_mode not in {"surgical", "native"}:
        raise DatasetError(f"{where}: repair_mode must be surgical or native")
    max_eco_cells = _nonnegative_int(manifest, "max_eco_cells", where)
    if max_eco_cells < 1:
        raise DatasetError(f"{where}: max_eco_cells must be positive")

    required_roles = list(REQUIRED_ARTIFACT_ROLES)
    if repair_mode == "native":
        required_roles.extend(NATIVE_ARTIFACT_ROLES)
    artifact_paths, artifact_metadata = _load_artifacts(
        manifest, case_dir, pilot_root, required_roles
    )
    baseline_provenance = _baseline_provenance(
        manifest,
        artifact_paths["baseline_qualification"],
        design,
        where,
    )
    instruction = _read_utf8(case_dir / "instruction.txt").strip()
    answer = _read_utf8(case_dir / "answer.txt").strip()
    if not instruction:
        raise DatasetError(f"{case_dir}/instruction.txt is empty")
    if not answer:
        raise DatasetError(f"{case_dir}/answer.txt is empty")
    if artifact_paths["instruction"].resolve() != (case_dir / "instruction.txt").resolve():
        raise DatasetError(f"{where}: artifacts.instruction must point to instruction.txt")
    if artifact_paths["answer"].resolve() != (case_dir / "answer.txt").resolve():
        raise DatasetError(f"{where}: artifacts.answer must point to answer.txt")

    _, answer_tcl = _extract_tcl(answer, f"{case_dir}/answer.txt")
    fix_tcl = _read_utf8(artifact_paths["fix_tcl"])
    if _normalize_code(answer_tcl) != _normalize_code(fix_tcl):
        raise DatasetError(f"{case_dir}/answer.txt: fenced Tcl must exactly match fix.tcl")
    _validate_fix_tcl(answer_tcl, f"{case_dir}/answer.txt")
    injected_tcl = _read_utf8(artifact_paths["inject_tcl"])
    _validate_hidden_injection(instruction, answer, injected_tcl, fix_tcl, str(case_dir))

    metrics_file = artifact_paths["metrics"]
    if metrics_file.resolve() != (case_dir / "metrics.json").resolve():
        raise DatasetError(f"{where}: artifacts.metrics must point to metrics.json")
    raw_metrics = _read_json(metrics_file)
    metrics = _validate_metrics(raw_metrics, case_type, metrics_file)
    assert isinstance(raw_metrics, Mapping)
    _validate_gold_evidence(
        case_id=case_id,
        case_type=case_type,
        design=design,
        repair_mode=repair_mode,
        max_eco_cells=max_eco_cells,
        answer=answer,
        artifacts=artifact_paths,
        raw_metrics=raw_metrics,
        baseline_provenance=baseline_provenance,
    )

    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": instruction},
            {"role": "assistant", "content": answer},
        ],
        "metadata": {
            "schema_version": SCHEMA_VERSION,
            "id": case_id,
            "type": case_type,
            "difficulty": difficulty,
            "design": design,
            "analysis_views": views,
            "tool_version": tool_version,
            "repair_mode": repair_mode,
            "max_eco_cells": max_eco_cells,
            "review_status": "gold",
            "baseline_provenance": baseline_provenance,
            "finalizer_schema": FINALIZER_SCHEMA_VERSION,
            "case_path": case_dir.relative_to(pilot_root).as_posix(),
            "artifacts": artifact_metadata,
            "metrics": metrics,
            "replay_status": metrics["replay"]["status"],
        },
    }


def build_records(pilot_root: Path) -> list[dict[str, Any]]:
    """Load and fully validate the ten source cases."""

    pilot_root = pilot_root.resolve()
    cases_root = pilot_root / "cases"
    if not cases_root.is_dir():
        raise DatasetError(f"missing cases directory: {cases_root}")
    manifest_paths = sorted(cases_root.glob("*/manifest.json"))
    if len(manifest_paths) != EXPECTED_CASES:
        raise DatasetError(
            f"expected exactly {EXPECTED_CASES} case manifests, found {len(manifest_paths)}"
        )
    records = [_load_case(path.parent, pilot_root) for path in manifest_paths]
    ids = [record["metadata"]["id"] for record in records]
    duplicates = sorted(case_id for case_id, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise DatasetError(f"duplicate case IDs: {', '.join(duplicates)}")
    if set(ids) != EXPECTED_IDS:
        missing = sorted(EXPECTED_IDS - set(ids))
        extra = sorted(set(ids) - EXPECTED_IDS)
        raise DatasetError(f"pilot IDs mismatch: missing={missing}, extra={extra}")
    actual_mix = Counter(record["metadata"]["type"] for record in records)
    if dict(actual_mix) != EXPECTED_MIX:
        raise DatasetError(f"case type mix must be {EXPECTED_MIX}, got {dict(actual_mix)}")
    return sorted(records, key=lambda record: record["metadata"]["id"])


def export_dataset(pilot_root: Path, output: Path) -> list[dict[str, Any]]:
    """Validate source cases and atomically write canonical JSONL."""

    records = build_records(pilot_root)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for record in records
    ]
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent, prefix=f".{output.name}.", delete=False
        ) as stream:
            temp_name = stream.name
            stream.write("\n".join(lines))
            stream.write("\n")
        os.replace(temp_name, output)
        temp_name = None
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)
    return records


def read_dataset(path: Path) -> list[dict[str, Any]]:
    """Read a UTF-8 JSONL dataset without accepting blank lines."""

    text = _read_utf8(path)
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            raise DatasetError(f"{path}:{line_number}: blank JSONL lines are not allowed")
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
        if not isinstance(record, dict):
            raise DatasetError(f"{path}:{line_number}: each JSONL record must be an object")
        messages = record.get("messages")
        if not isinstance(messages, list) or len(messages) != 3:
            raise DatasetError(f"{path}:{line_number}: messages must contain exactly 3 entries")
        roles = [message.get("role") if isinstance(message, Mapping) else None for message in messages]
        if roles != ["system", "user", "assistant"]:
            raise DatasetError(
                f"{path}:{line_number}: message roles must be system, user, assistant"
            )
        for index, message in enumerate(messages):
            if not isinstance(message.get("content"), str) or not message["content"].strip():
                raise DatasetError(f"{path}:{line_number}: messages[{index}].content is invalid")
        metadata = record.get("metadata")
        if not isinstance(metadata, Mapping):
            raise DatasetError(f"{path}:{line_number}: metadata must be an object")
        records.append(record)
    if len(records) != EXPECTED_CASES:
        raise DatasetError(f"{path}: expected exactly {EXPECTED_CASES} records, found {len(records)}")
    return records


def validate_dataset(pilot_root: Path, dataset: Path) -> list[dict[str, Any]]:
    """Validate sources, JSONL schema, and exact deterministic export content."""

    expected = build_records(pilot_root)
    actual = read_dataset(dataset.resolve())
    if actual != expected:
        expected_by_id = {record["metadata"]["id"]: record for record in expected}
        actual_by_id = {
            record.get("metadata", {}).get("id"): record
            for record in actual
            if isinstance(record.get("metadata"), Mapping)
        }
        missing = sorted(set(expected_by_id) - set(actual_by_id))
        extra = sorted(set(actual_by_id) - set(expected_by_id), key=str)
        detail = []
        if missing:
            detail.append(f"missing IDs={missing}")
        if extra:
            detail.append(f"extra IDs={extra}")
        changed = sorted(
            case_id
            for case_id in set(expected_by_id) & set(actual_by_id)
            if expected_by_id[case_id] != actual_by_id[case_id]
        )
        if changed:
            detail.append(f"stale/changed IDs={changed}")
        suffix = f" ({'; '.join(detail)})" if detail else ""
        raise DatasetError(f"{dataset}: JSONL is not the canonical export of source cases{suffix}")
    return actual


def _default_pilot_root() -> Path:
    return Path(__file__).resolve().parents[1] / "pilot_10"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("export", "validate"):
        sub = subparsers.add_parser(command)
        sub.add_argument(
            "--pilot-root",
            type=Path,
            default=_default_pilot_root(),
            help="pilot directory containing cases/ (default: %(default)s)",
        )
        sub.add_argument(
            "--output",
            type=Path,
            help="dataset JSONL path (default: <pilot-root>/dataset.jsonl)",
        )
    subparsers.choices["validate"].add_argument(
        "--source-only",
        action="store_true",
        help="validate case sources without requiring dataset.jsonl",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output = args.output or (args.pilot_root / "dataset.jsonl")
    try:
        if args.command == "export":
            records = export_dataset(args.pilot_root, output)
            print(f"exported {len(records)} validated cases to {output}")
        elif args.source_only:
            records = build_records(args.pilot_root)
            print(f"validated {len(records)} source cases under {args.pilot_root}")
        else:
            records = validate_dataset(args.pilot_root, output)
            print(f"validated canonical dataset with {len(records)} cases: {output}")
    except DatasetError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

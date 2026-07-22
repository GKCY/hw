#!/usr/bin/env python3
"""Prepare and orchestrate the ten-case Innovus timing-ECO pilot.

The host never guesses design object names.  ``prepare`` renders one Tcl task
per catalog entry; each task resolves its beginpoint, endpoint, driver, and net
from the restored post-route database.  ``run`` copies the immutable baseline
and task scripts to a task-specific qingteng-fc directory, runs fresh Innovus
processes, and fetches every result even when an individual replay fails.

This module intentionally uses only the Python standard library.  It does not
start or stop Firecracker; VM lifecycle is owned by the validation workflow.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCRIPT = Path(__file__).resolve()
DATASET_ROOT = SCRIPT.parents[1]
PILOT_ROOT = DATASET_ROOT / "pilot_10"
CATALOG_PATH = PILOT_ROOT / "catalog.json"
RUNTIME_TEMPLATE = PILOT_ROOT / "templates" / "pilot_runtime.tcl"
REPO_ROOT = SCRIPT.parents[3]
DEFAULT_BASELINE = (
    REPO_ROOT / "pnr" / "innovus" / "smic40" / "build_benchmark" / "baseline"
)
DEFAULT_WORK_ROOT = PILOT_ROOT / "work"
CHECKPOINT_NORMALIZER_PATH = SCRIPT.with_name("materialize_checkpoint_links.py")
CHECKPOINT_MATERIALIZATION_STATUS_SCHEMA = (
    "timing_eco_checkpoint_link_materialization_status.v1"
)

EXPECTED_CASE_IDS = (
    *(f"SETUP_{index:03d}" for index in range(1, 5)),
    *(f"HOLD_{index:03d}" for index in range(1, 5)),
    *(f"MIXED_{index:03d}" for index in range(1, 3)),
)
EXPECTED_TYPES = {"setup": 4, "hold": 4, "mixed": 2}
EXPECTED_REPAIR_MODES = {"surgical": 10}
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SAFE_GUEST_PATH = re.compile(r"^/[A-Za-z0-9_./-]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
BASELINE_REQUIRED_FILES = (
    "base.enc",
    "manifest.tcl",
    "restore.tcl",
    "setup.tcl",
    "cds.lib",
    "qualification.json",
    "QUALIFIED_CANDIDATE",
)
BASELINE_CHECKPOINT_DIRECTORY = "base.enc.dat"
BASELINE_QUALIFICATION_SCHEMA = "smic40_baseline_qualification.v1"
BASELINE_QUALIFICATION_STATUS = "QUALIFIED_CANDIDATE"
BASELINE_TECHNOLOGY_CLASSIFICATION = "derived_non_signoff"
DRC_REPORT_LIMIT = 1_000_000
CALIBRATION_STATUSES = {"PROBE_REQUIRED", "FROZEN"}
DATA_DELAY_INJECTION_STRATEGIES = {
    "insert_data_delay",
    "mixed_data_delay_and_capture_skew",
}
HOLD_DELAY_REPAIR_STRATEGIES = {
    "insert_data_delay",
    "insert_delay_and_downsize",
    "setup_then_hold",
    "coordinated_setup_hold",
}
SAFE_CELL_REFERENCE = re.compile(r"^[A-Za-z0-9_.]+$")
GOLD_REQUIRED_QUALIFICATION_SOURCES = (
    "setup_liberty_ss",
    "hold_liberty_ff",
)
GUEST_REPLAY_CLEANUP_SCRIPT = (
    'target=$1\n'
    # Refuse a missing, non-directory, or symlink leaf.  find -P changes only
    # real directories below the exact replay and never follows nested links.
    'if [ -L "$target" ] || [ ! -d "$target" ]; then exit 124; fi\n'
    'find -P "$target" -xdev -type d -exec chmod u+rwx -- {} + || exit $?\n'
    'rm -rf --one-file-system -- "$target" || exit $?\n'
    'if [ -e "$target" ] || [ -L "$target" ]; then exit 125; fi\n'
)


class PilotError(RuntimeError):
    """An actionable catalog, preparation, or remote-run error."""


_CHECKPOINT_NORMALIZER: Any | None = None


def _checkpoint_normalizer() -> Any:
    """Load the sibling standalone normalizer without relying on sys.path."""

    global _CHECKPOINT_NORMALIZER
    if _CHECKPOINT_NORMALIZER is None:
        spec = importlib.util.spec_from_file_location(
            "timing_eco_checkpoint_normalizer_for_runner",
            CHECKPOINT_NORMALIZER_PATH,
        )
        if spec is None or spec.loader is None:
            raise PilotError(
                f"cannot load checkpoint normalizer: {CHECKPOINT_NORMALIZER_PATH}"
            )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CHECKPOINT_NORMALIZER = module
    return _CHECKPOINT_NORMALIZER


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PilotError(f"missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PilotError(f"invalid JSON in {path}: {exc}") from exc


def _write_json(path: Path, value: Any, *, durable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    serialized = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(serialized)
        stream.flush()
        if durable:
            os.fsync(stream.fileno())
    os.replace(temporary, path)
    if durable:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _require_mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PilotError(f"{where} must be an object")
    return value


def _require_string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PilotError(f"{where} must be a non-empty string")
    return value.strip()


def _require_positive_integer(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PilotError(f"{where} must be a positive integer")
    return value


def _require_finite_number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PilotError(f"{where} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise PilotError(f"{where} must be finite")
    return result


def _validate_injection_contract(case: Mapping[str, Any]) -> None:
    """Require one explicit, directionally frozen injection candidate.

    ``PROBE_REQUIRED`` candidates may be prepared and measured, but the Tcl
    runtime deliberately refuses to promote them to a passing replay.  Once a
    real-tool calibration is complete, only changing the status to ``FROZEN``
    (and recording any discovered injection-cell references) enables the Gold
    gate.
    """

    case_id = str(case["id"])
    case_type = str(case["type"])
    injection = _require_mapping(case.get("injection"), f"{case_id}.injection")
    strategy = _require_string(injection.get("strategy"), f"{case_id}.injection.strategy")
    status = injection.get("calibration_status")
    if status not in CALIBRATION_STATUSES:
        raise PilotError(
            f"{case_id}.injection.calibration_status must be PROBE_REQUIRED or FROZEN"
        )
    for forbidden in ("drive_steps", "delay_cells", "max_attempts", "clock_cell_count"):
        if forbidden in injection:
            raise PilotError(
                f"{case_id}.injection.{forbidden} is a legacy shared/search parameter; "
                "use setup_parameters or hold_parameters"
            )
    setup = _require_mapping(
        injection.get("setup_parameters"), f"{case_id}.injection.setup_parameters"
    )
    hold = _require_mapping(
        injection.get("hold_parameters"), f"{case_id}.injection.hold_parameters"
    )
    allowed_keys = {
        "drive_steps",
        "delay_cells",
        "delay_cell_reference",
        "clock_cell_count",
        "clock_cell_reference",
    }
    for direction, parameters in (("setup", setup), ("hold", hold)):
        unknown = sorted(set(parameters) - allowed_keys)
        if unknown:
            raise PilotError(
                f"{case_id}.injection.{direction}_parameters has unknown keys: "
                f"{', '.join(unknown)}"
            )
        for key in ("drive_steps", "delay_cells", "clock_cell_count"):
            if key in parameters:
                _require_positive_integer(
                    parameters[key], f"{case_id}.injection.{direction}_parameters.{key}"
                )

    expected: dict[str, set[str]] = {
        "downsize_endpoint_driver": {"setup.drive_steps"},
        "insert_data_delay": {"setup.delay_cells"},
        "upsize_endpoint_driver": {"hold.drive_steps"},
        "local_capture_clock_delay": {
            "hold.clock_cell_count",
            "hold.clock_cell_reference",
        },
        "mixed_downsize_and_speedup": {"setup.drive_steps", "hold.drive_steps"},
        "mixed_data_delay_and_capture_skew": {
            "setup.delay_cells",
            "hold.clock_cell_count",
            "hold.clock_cell_reference",
        },
    }
    if strategy not in expected:
        raise PilotError(f"{case_id} has unsupported injection strategy {strategy}")
    uses_data_delay = strategy in DATA_DELAY_INJECTION_STRATEGIES
    if "delay_cell_reference" in hold:
        raise PilotError(
            f"{case_id}.injection.hold_parameters.delay_cell_reference is invalid; "
            "the frozen injection reference belongs to setup_parameters"
        )
    if not uses_data_delay and "delay_cell_reference" in setup:
        raise PilotError(
            f"{case_id} strategy {strategy} must not declare delay_cell_reference"
        )
    if uses_data_delay:
        delay_reference: str | None = None
        if "delay_cell_reference" in setup:
            delay_reference = _require_string(
                setup["delay_cell_reference"],
                f"{case_id}.injection.setup_parameters.delay_cell_reference",
            )
        if status == "FROZEN":
            if delay_reference is None or delay_reference == "PROBE_REQUIRED":
                raise PilotError(
                    f"{case_id} cannot be FROZEN without an exact data-delay cell reference"
                )
            if SAFE_CELL_REFERENCE.fullmatch(delay_reference) is None:
                raise PilotError(
                    f"{case_id} frozen data-delay cell reference contains unsafe characters"
                )
        elif delay_reference is not None and delay_reference != "PROBE_REQUIRED":
            raise PilotError(
                f"{case_id} PROBE_REQUIRED data-delay reference must be omitted or "
                "PROBE_REQUIRED"
            )
    actual = {
        *(f"setup.{key}" for key in setup),
        *(f"hold.{key}" for key in hold),
    }
    if uses_data_delay:
        actual.discard("setup.delay_cell_reference")
    if actual != expected[strategy]:
        raise PilotError(
            f"{case_id} injection parameters are {sorted(actual)}, expected "
            f"{sorted(expected[strategy])} for {strategy}"
        )
    if case_type == "setup" and (not setup or hold):
        raise PilotError(f"{case_id} setup-only injection must have only setup parameters")
    if case_type == "hold" and (setup or not hold):
        raise PilotError(f"{case_id} hold-only injection must have only hold parameters")
    if case_type == "mixed" and (not setup or not hold):
        raise PilotError(f"{case_id} mixed injection must freeze setup and hold independently")
    if case_type in {"setup", "hold"}:
        headroom = _require_finite_number(
            injection.get("opposite_wns_min_ns"),
            f"{case_id}.injection.opposite_wns_min_ns",
        )
        if headroom < 0.0:
            raise PilotError(f"{case_id} opposite timing headroom must be non-negative")
    elif "opposite_wns_min_ns" in injection:
        raise PilotError(f"{case_id} mixed case must not declare opposite timing headroom")

    if "clock_cell_count" in hold:
        reference = _require_string(
            hold.get("clock_cell_reference"),
            f"{case_id}.injection.hold_parameters.clock_cell_reference",
        )
        if status == "FROZEN" and reference == "PROBE_REQUIRED":
            raise PilotError(
                f"{case_id} cannot be FROZEN with a PROBE_REQUIRED clock reference"
            )
        if reference != "PROBE_REQUIRED" and SAFE_CELL_REFERENCE.fullmatch(reference) is None:
            raise PilotError(f"{case_id} frozen clock reference contains unsafe characters")
        early_count = sum(
            int(selector["count"])
            for selector in case["selectors"]
            if selector["timing"] == "early"
        )
        if early_count != 1:
            raise PilotError(
                f"{case_id} capture-clock injection must select exactly one early endpoint"
            )


def _repair_operation_lower_bound(
    case: Mapping[str, Any], repair_delay_cells_per_endpoint: int
) -> int:
    """Return the deterministic minimum number of physical repair edits."""

    strategy = str(case["repair"]["strategy"])
    late_targets = sum(
        int(selector["count"])
        for selector in case["selectors"]
        if selector["timing"] == "late"
    )
    early_targets = sum(
        int(selector["count"])
        for selector in case["selectors"]
        if selector["timing"] == "early"
    )
    setup_delay_cells = int(
        case["injection"]["setup_parameters"].get("delay_cells", 0)
    )
    if strategy == "upsize_endpoint_driver":
        return late_targets
    if strategy == "upsize_driver_and_buffer":
        return late_targets + 1
    if strategy == "replace_delay_and_upsize":
        return late_targets * setup_delay_cells + late_targets
    if strategy == "insert_data_delay":
        return early_targets * repair_delay_cells_per_endpoint
    if strategy == "insert_delay_and_downsize":
        return early_targets * (repair_delay_cells_per_endpoint + 1)
    if strategy == "setup_then_hold":
        return late_targets + early_targets * repair_delay_cells_per_endpoint
    if strategy == "coordinated_setup_hold":
        return (
            late_targets * setup_delay_cells
            + early_targets * repair_delay_cells_per_endpoint
        )
    # Native selected-term optimization has no statically knowable changed-cell
    # lower bound.  Its runtime diff remains bounded by max_eco_cells.
    return 0


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, Any]:
    catalog = _require_mapping(_load_json(path), str(path))
    if catalog.get("schema_version") != "timing_eco_pilot_catalog.v1":
        raise PilotError(f"unsupported catalog schema in {path}")
    cases = catalog.get("cases")
    if not isinstance(cases, list):
        raise PilotError("catalog.cases must be an array")

    ids: list[str] = []
    types: Counter[str] = Counter()
    modes: Counter[str] = Counter()
    selector_intervals: dict[tuple[str, str], list[tuple[int, int, str, str]]] = {}
    for index, raw_case in enumerate(cases):
        case = _require_mapping(raw_case, f"catalog.cases[{index}]")
        case_id = _require_string(case.get("id"), f"catalog.cases[{index}].id")
        case_type = _require_string(case.get("type"), f"{case_id}.type")
        if case_type not in EXPECTED_TYPES:
            raise PilotError(f"{case_id}.type must be setup, hold, or mixed")
        mode = _require_string(case.get("repair_mode"), f"{case_id}.repair_mode")
        if case.get("difficulty") not in {"easy", "medium", "hard"}:
            raise PilotError(f"{case_id}.difficulty must be easy, medium, or hard")
        selectors = case.get("selectors")
        if not isinstance(selectors, list) or not selectors:
            raise PilotError(f"{case_id}.selectors must be a non-empty array")
        for selector_index, raw_selector in enumerate(selectors):
            selector = _require_mapping(raw_selector, f"{case_id}.selectors[{selector_index}]")
            timing = selector.get("timing")
            if timing not in {"late", "early"}:
                raise PilotError(f"{case_id} selector timing must be late or early")
            hierarchy_class = _require_string(
                selector.get("hierarchy_class"), f"{case_id} selector hierarchy_class"
            )
            stable_rank = selector.get("stable_rank")
            count = selector.get("count")
            if not isinstance(stable_rank, int) or stable_rank < 0:
                raise PilotError(f"{case_id} selector stable_rank must be non-negative")
            if not isinstance(count, int) or count < 1:
                raise PilotError(f"{case_id} selector count must be positive")
            interval_key = (timing, hierarchy_class)
            interval_start = stable_rank
            interval_end = stable_rank + count
            role = _require_string(
                selector.get("role"), f"{case_id} selector role"
            )
            for other_start, other_end, other_case, other_role in selector_intervals.get(
                interval_key, []
            ):
                if interval_start < other_end and other_start < interval_end:
                    raise PilotError(
                        f"overlapping deterministic selector intervals for {interval_key}: "
                        f"{case_id}/{role} [{interval_start}, {interval_end}) overlaps "
                        f"{other_case}/{other_role} [{other_start}, {other_end}); "
                        "cases could resolve the same endpoint"
                    )
            selector_intervals.setdefault(interval_key, []).append(
                (interval_start, interval_end, case_id, role)
            )
        selector_timings = {selector["timing"] for selector in selectors}
        expected_selector_timings = (
            {"late", "early"}
            if case_type == "mixed"
            else ({"late"} if case_type == "setup" else {"early"})
        )
        if selector_timings != expected_selector_timings:
            raise PilotError(
                f"{case_id} selectors must cover exactly {sorted(expected_selector_timings)}"
            )
        target_wns = _require_mapping(case.get("target_wns_ns"), f"{case_id}.target_wns_ns")
        expected_checks = {"setup", "hold"} if case_type == "mixed" else {case_type}
        if set(target_wns) != expected_checks:
            raise PilotError(
                f"{case_id}.target_wns_ns must contain exactly {sorted(expected_checks)}"
            )
        for check, interval in target_wns.items():
            if not isinstance(interval, list) or len(interval) != 2:
                raise PilotError(f"{case_id}.target_wns_ns.{check} must be [low, high]")
            low = _require_finite_number(interval[0], f"{case_id} {check} target low")
            high = _require_finite_number(interval[1], f"{case_id} {check} target high")
            if low > high or high >= 0.0:
                raise PilotError(
                    f"{case_id}.target_wns_ns.{check} must be an ordered negative interval"
                )
        _validate_injection_contract(case)
        repair = _require_mapping(case.get("repair"), f"{case_id}.repair")
        repair_strategy = _require_string(
            repair.get("strategy"), f"{case_id}.repair.strategy"
        )
        surgical_repairs = {
            "upsize_endpoint_driver",
            "upsize_driver_and_buffer",
            "replace_delay_and_upsize",
            "insert_data_delay",
            "insert_delay_and_downsize",
            "setup_then_hold",
            "coordinated_setup_hold",
        }
        native_repairs = {"native_selected_terms_setup", "native_selected_terms_hold"}
        expected_repairs = native_repairs if mode == "native" else surgical_repairs
        if repair_strategy not in expected_repairs:
            raise PilotError(
                f"{case_id}.repair.strategy {repair_strategy!r} is invalid for {mode} mode"
            )
        if repair_strategy == "native_selected_terms_setup" and case_type != "setup":
            raise PilotError(f"{case_id} setup native repair requires a setup case")
        if repair_strategy == "native_selected_terms_hold" and case_type != "hold":
            raise PilotError(f"{case_id} hold native repair requires a hold case")
        allowed_repair_keys = {"strategy", "max_eco_cells"}
        uses_hold_delay = repair_strategy in HOLD_DELAY_REPAIR_STRATEGIES
        if uses_hold_delay:
            allowed_repair_keys.update(
                {"delay_cell_reference", "delay_cells_per_endpoint"}
            )
        unknown_repair_keys = sorted(set(repair) - allowed_repair_keys)
        if unknown_repair_keys:
            raise PilotError(
                f"{case_id}.repair has fields unsupported by {repair_strategy}: "
                f"{', '.join(unknown_repair_keys)}"
            )

        has_repair_reference = "delay_cell_reference" in repair
        has_repair_count = "delay_cells_per_endpoint" in repair
        repair_delay_count = 1
        if uses_hold_delay:
            if has_repair_reference != has_repair_count:
                raise PilotError(
                    f"{case_id}.repair.delay_cell_reference and "
                    "delay_cells_per_endpoint must be declared together"
                )
            if case["injection"]["calibration_status"] == "FROZEN" and not (
                has_repair_reference and has_repair_count
            ):
                raise PilotError(
                    f"{case_id} FROZEN hold-delay repair requires an exact "
                    "delay_cell_reference and positive delay_cells_per_endpoint"
                )
            if has_repair_reference:
                repair_reference = _require_string(
                    repair["delay_cell_reference"],
                    f"{case_id}.repair.delay_cell_reference",
                )
                if (
                    repair_reference == "PROBE_REQUIRED"
                    or SAFE_CELL_REFERENCE.fullmatch(repair_reference) is None
                ):
                    raise PilotError(
                        f"{case_id}.repair.delay_cell_reference must be one exact "
                        "safe Liberty cell reference"
                    )
                repair_delay_count = _require_positive_integer(
                    repair["delay_cells_per_endpoint"],
                    f"{case_id}.repair.delay_cells_per_endpoint",
                )
        elif has_repair_reference or has_repair_count:
            raise PilotError(
                f"{case_id} repair strategy {repair_strategy} must not declare "
                "hold-delay repair fields"
            )

        max_eco_cells = _require_positive_integer(
            repair.get("max_eco_cells"), f"{case_id}.repair.max_eco_cells"
        )
        minimum_operations = _repair_operation_lower_bound(case, repair_delay_count)
        if max_eco_cells < minimum_operations:
            raise PilotError(
                f"{case_id}.repair.max_eco_cells={max_eco_cells} is below the "
                f"static repair operation lower bound {minimum_operations}"
            )
        ids.append(case_id)
        types[case_type] += 1
        modes[mode] += 1

    if tuple(ids) != EXPECTED_CASE_IDS:
        raise PilotError(f"catalog cases must be ordered exactly as {EXPECTED_CASE_IDS}")
    if catalog.get("case_order") != list(EXPECTED_CASE_IDS):
        raise PilotError("catalog.case_order does not match the required ten IDs")
    if dict(types) != EXPECTED_TYPES:
        raise PilotError(f"case type mix is {dict(types)}, expected {EXPECTED_TYPES}")
    if dict(modes) != EXPECTED_REPAIR_MODES:
        raise PilotError(
            f"repair-mode mix is {dict(modes)}, expected {EXPECTED_REPAIR_MODES}"
        )

    hierarchy = _require_mapping(
        _require_mapping(catalog.get("design"), "catalog.design").get("hierarchy_classes"),
        "catalog.design.hierarchy_classes",
    )
    for case in cases:
        for selector in case["selectors"]:
            if selector["hierarchy_class"] not in hierarchy:
                raise PilotError(
                    f"{case['id']} references unknown hierarchy class "
                    f"{selector['hierarchy_class']}"
                )
    acceptance = _require_mapping(catalog["design"].get("acceptance"), "catalog.design.acceptance")
    opposite = _require_finite_number(
        acceptance.get("injected_opposite_wns_min_ns"),
        "catalog.design.acceptance.injected_opposite_wns_min_ns",
    )
    if opposite < 0.0:
        raise PilotError("catalog injected opposite timing headroom must be non-negative")
    return dict(catalog)


def _selected_cases(catalog: Mapping[str, Any], requested: Sequence[str]) -> list[dict[str, Any]]:
    by_id = {case["id"]: case for case in catalog["cases"]}
    if not requested or requested == ["all"]:
        return [dict(by_id[case_id]) for case_id in catalog["case_order"]]
    if "all" in requested:
        raise PilotError("--case all cannot be combined with an explicit case ID")
    unknown = sorted(set(requested) - set(by_id))
    if unknown:
        raise PilotError(f"unknown case ID(s): {', '.join(unknown)}")
    if len(set(requested)) != len(requested):
        raise PilotError("duplicate --case selection")
    return [dict(by_id[case_id]) for case_id in catalog["case_order"] if case_id in requested]


def _baseline_qualification(path: Path, catalog: Mapping[str, Any]) -> dict[str, Any]:
    qualification_path = path / "qualification.json"
    qualification = _require_mapping(
        _load_json(qualification_path), f"baseline qualification {qualification_path}"
    )
    expected = {
        "schema_version": BASELINE_QUALIFICATION_SCHEMA,
        "status": BASELINE_QUALIFICATION_STATUS,
        "gold_status": False,
        "signoff_eligible": False,
        "technology_classification": BASELINE_TECHNOLOGY_CLASSIFICATION,
        "top": catalog["design"]["top"],
    }
    for key, value in expected.items():
        if qualification.get(key) != value:
            raise PilotError(
                f"baseline qualification {key}={qualification.get(key)!r}, expected {value!r}"
            )
    drc = _require_mapping(qualification.get("drc"), "baseline qualification drc")
    if drc.get("report_limit") != DRC_REPORT_LIMIT:
        raise PilotError(
            "baseline qualification DRC evidence is not bound to "
            f"report_limit={DRC_REPORT_LIMIT}"
        )
    views = _require_mapping(
        qualification.get("analysis_views"), "baseline qualification analysis_views"
    )
    manifest_path = path / "manifest.tcl"
    expected_views = {
        "setup": _manifest_tcl_value(manifest_path, "::SFT_SETUP_VIEW", ""),
        "hold": _manifest_tcl_value(manifest_path, "::SFT_HOLD_VIEW", ""),
    }
    for check in ("setup", "hold"):
        actual_view = _require_string(
            views.get(check), f"baseline qualification analysis_views.{check}"
        )
        if not expected_views[check] or actual_view != expected_views[check]:
            raise PilotError(
                f"baseline qualification {check} view {actual_view!r} does not match "
                f"manifest.tcl {expected_views[check]!r}"
            )
    minimum = _require_finite_number(
        catalog["design"]["acceptance"]["baseline_wns_min_ns"],
        "catalog baseline WNS minimum",
    )
    timing = _require_mapping(qualification.get("timing"), "baseline qualification timing")
    for check in ("setup", "hold"):
        metric = _require_mapping(timing.get(check), f"baseline qualification timing.{check}")
        wns = _require_finite_number(metric.get("wns_ns"), f"qualification {check} WNS")
        tns = _require_finite_number(metric.get("tns_ns"), f"qualification {check} TNS")
        if wns < minimum or abs(tns) > 1.0e-12:
            raise PilotError(
                f"baseline qualification {check} timing is not clean: "
                f"WNS={wns} TNS={tns}, required WNS>={minimum} and TNS=0"
            )
    marker = (path / "QUALIFIED_CANDIDATE").read_text(encoding="utf-8")
    for required in (
        "status=QUALIFIED_CANDIDATE",
        "gold_status=false",
        "technology_classification=derived_non_signoff",
    ):
        if required not in marker.splitlines():
            raise PilotError(f"baseline QUALIFIED_CANDIDATE marker is missing {required!r}")
    _qualification_source_artifacts(qualification, require=False)
    return dict(qualification)


def _qualification_source_artifacts(
    qualification: Mapping[str, Any], *, require: bool
) -> dict[str, dict[str, Any]]:
    """Validate source hashes needed to bind Gold PrimeTime libraries.

    Older NOT_GOLD calibration bundles may omit this structured field.  Gold
    preparation is deliberately stricter and requires both independently
    qualified Liberty corners.
    """

    raw = qualification.get("source_artifacts")
    if raw is None:
        if require:
            raise PilotError(
                "Gold preparation requires qualified source artifacts: "
                + ", ".join(GOLD_REQUIRED_QUALIFICATION_SOURCES)
            )
        return {}
    if not isinstance(raw, Mapping):
        raise PilotError("baseline qualification source_artifacts must be an object")
    normalized: dict[str, dict[str, Any]] = {}
    for role in GOLD_REQUIRED_QUALIFICATION_SOURCES:
        item = raw.get(role)
        if item is None and not require:
            continue
        if not isinstance(item, Mapping):
            raise PilotError(
                f"baseline qualification source_artifacts.{role} must be an object"
            )
        digest = item.get("sha256")
        size = item.get("bytes")
        source_path = item.get("source_path")
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise PilotError(
                f"baseline qualification source_artifacts.{role}.sha256 is invalid"
            )
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise PilotError(
                f"baseline qualification source_artifacts.{role}.bytes must be positive"
            )
        if (
            not isinstance(source_path, str)
            or not source_path.startswith("/")
            or ".." in Path(source_path).parts
        ):
            raise PilotError(
                f"baseline qualification source_artifacts.{role}.source_path is invalid"
            )
        normalized[role] = {
            "sha256": digest,
            "bytes": size,
            "source_path": source_path,
        }
    missing = sorted(set(GOLD_REQUIRED_QUALIFICATION_SOURCES) - set(normalized))
    if require and missing:
        raise PilotError(
            "Gold preparation requires qualified source artifacts: "
            + ", ".join(missing)
        )
    return normalized


def _validate_baseline(path: Path, catalog: Mapping[str, Any]) -> dict[str, Any]:
    if not path.is_dir():
        raise PilotError(f"baseline bundle is not a directory: {path}")
    declared = catalog["design"]["baseline_contract"]["required_files"]
    if not isinstance(declared, list) or any(
        not isinstance(name, str) or not name for name in declared
    ):
        raise PilotError("catalog baseline required_files must be non-empty strings")
    required = sorted(set(declared) | set(BASELINE_REQUIRED_FILES))
    if BASELINE_CHECKPOINT_DIRECTORY not in required:
        required.append(BASELINE_CHECKPOINT_DIRECTORY)

    errors: list[str] = []
    for name in required:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise PilotError(f"unsafe baseline required_files entry: {name!r}")
        item = path / relative
        if name == BASELINE_CHECKPOINT_DIRECTORY:
            if not item.is_dir():
                errors.append(f"{name} (missing or not a directory)")
                continue
            if not any(
                candidate.is_file() and candidate.stat().st_size > 0
                for candidate in item.rglob("*")
            ):
                errors.append(f"{name} (contains no non-empty regular file)")
            continue
        if not item.is_file():
            errors.append(f"{name} (missing or not a regular file)")
        elif item.stat().st_size == 0:
            errors.append(f"{name} (empty file)")
    if errors:
        raise PilotError(f"invalid baseline bundle {path}: {', '.join(errors)}")
    return _baseline_qualification(path, catalog)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except FileNotFoundError as exc:
        raise PilotError(f"missing file for SHA256: {path}") from exc
    return digest.hexdigest()


def _sha256_tree(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        relative = item.relative_to(path).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        if item.is_file():
            digest.update(item.stat().st_size.to_bytes(8, "big"))
            with item.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def _baseline_checksum_text(path: Path) -> str:
    """Return a GNU sha256sum manifest for independent guest verification."""

    lines: list[str] = []
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise PilotError(f"baseline bundle must not contain symlinks: {item}")
        if not item.is_file():
            continue
        relative = item.relative_to(path).as_posix()
        if any(character in relative for character in ("\n", "\r", "\\")):
            raise PilotError(f"baseline path cannot be represented safely by sha256sum: {relative!r}")
        lines.append(f"{_sha256_file(item)}  {relative}")
    if not lines:
        raise PilotError(f"baseline bundle has no regular files: {path}")
    return "\n".join(lines) + "\n"


def _manifest_tcl_value(path: Path, variable: str, default: str) -> str:
    if not path.is_file():
        return default
    pattern = re.compile(
        rf"(?m)^\s*set\s+{re.escape(variable)}\s+(?:\{{([^}}]+)\}}|\"([^\"]+)\"|(\S+))\s*$"
    )
    match = pattern.search(path.read_text(encoding="utf-8"))
    if not match:
        return default
    return next(group for group in match.groups() if group is not None)


def _tcl_atom(value: Any) -> str:
    text = str(value)
    if "\n" in text or "\r" in text:
        raise PilotError(f"cannot render multiline Tcl atom: {text!r}")
    return "{" + text.replace("\\", "\\\\").replace("}", "\\}") + "}"


def _tcl_list(values: Iterable[Any]) -> str:
    return "[list " + " ".join(_tcl_atom(value) for value in values) + "]"


def _render_case_config(catalog: Mapping[str, Any], case: Mapping[str, Any]) -> str:
    design = catalog["design"]
    hierarchy = design["hierarchy_classes"]
    injection = case["injection"]
    setup_parameters = injection["setup_parameters"]
    hold_parameters = injection["hold_parameters"]
    repair = case["repair"]
    target_wns = case["target_wns_ns"]
    rows: list[tuple[str, Any]] = [
        ("id", case["id"]),
        ("type", case["type"]),
        ("difficulty", case["difficulty"]),
        ("repair_mode", case["repair_mode"]),
        ("injection_strategy", injection["strategy"]),
        ("repair_strategy", repair["strategy"]),
        ("max_eco_cells", repair["max_eco_cells"]),
        (
            "repair_delay_cells_per_endpoint",
            repair.get("delay_cells_per_endpoint", 0),
        ),
        ("calibration_status", injection["calibration_status"]),
        ("setup_drive_steps", setup_parameters.get("drive_steps", 0)),
        ("setup_delay_cells", setup_parameters.get("delay_cells", 0)),
        ("hold_drive_steps", hold_parameters.get("drive_steps", 0)),
        ("hold_delay_cells", hold_parameters.get("delay_cells", 0)),
        ("hold_clock_cell_count", hold_parameters.get("clock_cell_count", 0)),
        ("opposite_wns_min", injection.get(
            "opposite_wns_min_ns",
            catalog["design"]["acceptance"]["injected_opposite_wns_min_ns"],
        )),
        ("setup_target_count", sum(
            int(selector["count"]) for selector in case["selectors"]
            if selector["timing"] == "late"
        )),
        ("hold_target_count", sum(
            int(selector["count"]) for selector in case["selectors"]
            if selector["timing"] == "early"
        )),
        ("setup_wns_low", target_wns.get("setup", [0.0, 0.0])[0]),
        ("setup_wns_high", target_wns.get("setup", [0.0, 0.0])[1]),
        ("hold_wns_low", target_wns.get("hold", [0.0, 0.0])[0]),
        ("hold_wns_high", target_wns.get("hold", [0.0, 0.0])[1]),
        ("baseline_wns_min", catalog["design"]["acceptance"]["baseline_wns_min_ns"]),
        ("final_setup_wns_min", catalog["design"]["acceptance"]["final_setup_wns_min_ns"]),
        ("final_hold_wns_min", catalog["design"]["acceptance"]["final_hold_wns_min_ns"]),
    ]
    lines = [
        "# Generated from pilot_10/catalog.json; do not hand edit.",
        "array unset ::SFT_CASE",
        "array set ::SFT_CASE {",
    ]
    lines.extend(f"    {key} {_tcl_atom(value)}" for key, value in rows)
    lines.append("}")
    lines.append("set ::SFT_SELECTORS [list \\")
    for selector in case["selectors"]:
        hierarchy_rule = hierarchy[selector["hierarchy_class"]]
        fields = [
            "role", _tcl_atom(selector["role"]),
            "timing", _tcl_atom(selector["timing"]),
            "hierarchy_class", _tcl_atom(selector["hierarchy_class"]),
            "stable_rank", selector["stable_rank"],
            "count", selector["count"],
            "include_globs", _tcl_list(hierarchy_rule["include_globs"]),
            "exclude_globs", _tcl_list(hierarchy_rule["exclude_globs"]),
        ]
        lines.append("    [dict create " + " ".join(map(str, fields)) + "] \\")
    lines.append("]")
    cell_policy = design["cell_policy"]
    lines.extend(
        [
            f"set ::SFT_DELAY_CELLS {_tcl_list(cell_policy['delay_candidates'])}",
            f"set ::SFT_FROZEN_DELAY_CELL {_tcl_atom(setup_parameters.get('delay_cell_reference', ''))}",
            f"set ::SFT_REPAIR_DELAY_CELL {_tcl_atom(repair.get('delay_cell_reference', ''))}",
            f"set ::SFT_BUFFER_CELLS {_tcl_list(cell_policy['buffer_candidates'])}",
            f"set ::SFT_CLOCK_CELL_GLOBS {_tcl_list(cell_policy['clock_delay_discovery_globs'])}",
            f"set ::SFT_FROZEN_CLOCK_CELL {_tcl_atom(hold_parameters.get('clock_cell_reference', ''))}",
            f"set ::SFT_ECO_PREFIX {_tcl_atom(cell_policy['eco_instance_prefix'])}",
            f"set ::SFT_UNCERTAINTY_CANDIDATES {_tcl_list(injection.get('candidate_ns', []))}",
            "",
        ]
    )
    return "\n".join(lines)


def _render_inject(case: Mapping[str, Any]) -> str:
    return f"""# Injection oracle for {case['id']}; never include this file in SFT messages.
if {{![llength [info commands ::sft::apply_injection]]}} {{
    error "pilot_runtime.tcl was not loaded"
}}
::sft::apply_injection
"""


def _render_fix(case: Mapping[str, Any]) -> str:
    return f"""# Replay-only materializer for {case['id']}.
# The exported Gold answer is reports/concrete_fix.tcl, never this wrapper.
if {{![llength [info commands ::sft::write_concrete_fix]]}} {{
    error "pilot_runtime.tcl was not loaded"
}}
set concrete_fix [::sft::write_concrete_fix]
source $concrete_fix
"""


def _render_replay(case: Mapping[str, Any]) -> str:
    return f"""# Deterministic replay harness for {case['id']}.
set ::SFT_CASE_DIR [file normalize [file dirname [info script]]]
set ::SFT_REPORT_DIR [file join $::SFT_CASE_DIR reports]
set ::SFT_LOG_DIR [file join $::SFT_CASE_DIR logs]
file mkdir $::SFT_REPORT_DIR
file mkdir $::SFT_LOG_DIR

if {{![info exists ::env(SFT_BASELINE_DIR)]}} {{
    puts stderr "ERROR: SFT_BASELINE_DIR is not set"
    exit 2
}}
set baseline_dir [file normalize $::env(SFT_BASELINE_DIR)]
foreach required {{manifest.tcl restore.tcl}} {{
    if {{![file isfile [file join $baseline_dir $required]]}} {{
        puts stderr "ERROR: missing baseline file: [file join $baseline_dir $required]"
        exit 2
    }}
}}

source [file join $baseline_dir manifest.tcl]
source [file join $baseline_dir restore.tcl]
source [file join $::SFT_CASE_DIR case_config.tcl]
source [file join $::SFT_CASE_DIR pilot_runtime.tcl]

if {{[catch {{
    ::sft::validate_baseline_guard
    ::sft::resolve_targets
    source [file join $::SFT_CASE_DIR inject.tcl]
    if {{[::sft::calibration_only]}} {{
        ::sft::write_calibration_marker
        puts "SFT_NOT_GOLD_CALIBRATION_COMPLETE {case['id']}"
        exit 0
    }}
    ::sft::write_stage_reports before
    ::sft::write_constraint_snapshot before
    ::sft::write_violating_design
    source [file join $::SFT_CASE_DIR fix.tcl]
    ::sft::validate_native_cell_budget
    ::sft::write_stage_reports after
    ::sft::write_constraint_snapshot after
    ::sft::validate_final_timing
    ::sft::write_exported_design
    ::sft::write_functional_audit
    ::sft::write_provisional_metrics
    ::sft::write_success_marker
}} message options]}} {{
    puts stderr "SFT_CASE_FAILED {case['id']}: $message"
    puts stderr [dict get $options -errorinfo]
    exit 1
}}

puts "SFT_CASE_PASSED {case['id']}"
exit 0
"""


def _artifact_map() -> dict[str, str]:
    return {
        "baseline_qualification": "baseline_qualification.json",
        "inject_tcl": "inject.tcl",
        "fix_tcl": "reports/concrete_fix.tcl",
        "replay_tcl": "replay.tcl",
        "metrics": "metrics.json",
        "setup_before": "reports/setup_before.rpt",
        "hold_before": "reports/hold_before.rpt",
        "setup_after": "reports/setup_after.rpt",
        "hold_after": "reports/hold_after.rpt",
        "drv_after": "reports/drv_after.rpt",
        "connectivity_after": "reports/connectivity_after.rpt",
        "drc_before": "reports/drc_before.rpt",
        "drc_after": "reports/drc_after.rpt",
        "run_log": "logs/innovus.log",
        "drv_before": "reports/drv_before.rpt",
        "connectivity_before": "reports/connectivity_before.rpt",
        "constraint_before": "reports/constraint_before.sdc",
        "constraint_after": "reports/constraint_after.sdc",
        "constraint_setup_before": "reports/constraint_setup_before.sdc",
        "constraint_setup_after": "reports/constraint_setup_after.sdc",
        "constraint_hold_before": "reports/constraint_hold_before.sdc",
        "constraint_hold_after": "reports/constraint_hold_after.sdc",
        "functional_audit": "reports/functional_audit.json",
        "injection_provenance": "reports/injection_provenance.json",
        "baseline_guard": "reports/baseline_guard.json",
        "diagnostic_context": "reports/diagnostic_context.json",
        "violation_locality": "reports/violation_locality.json",
        "physical_no_regression": "reports/physical_no_regression.json",
        "native_cell_diff": "reports/native_cell_diff.tcl",
        "native_selected_terms": "reports/native_selected_terms.txt",
        "calibration_status": "reports/calibration_status.json",
        "not_gold_calibration_marker": "NOT_GOLD_CALIBRATION",
        "violating_checkpoint": "violating.enc",
        "violating_checkpoint_data": "violating.enc.dat",
        "before_netlist": "before.v",
        "setup_spef_before": "setup_before.spef",
        "hold_spef_before": "hold_before.spef",
        "fixed_netlist": "fixed.v",
        "setup_spef": "setup_after.spef",
        "hold_spef": "hold_after.spef",
    }


def _write_case(
    run_dir: Path,
    catalog: Mapping[str, Any],
    case: Mapping[str, Any],
    baseline: Path,
    baseline_sha256: str,
    catalog_sha256: str,
    qualification: Mapping[str, Any],
    qualification_sha256: str,
    checksum_manifest_sha256: str,
) -> None:
    case_dir = run_dir / "cases" / case["id"]
    case_dir.mkdir(parents=True)
    (case_dir / "reports").mkdir()
    (case_dir / "logs").mkdir()
    (case_dir / "runs").mkdir()
    (case_dir / "case_config.tcl").write_text(
        _render_case_config(catalog, case), encoding="utf-8"
    )
    (case_dir / "inject.tcl").write_text(_render_inject(case), encoding="utf-8")
    (case_dir / "fix.tcl").write_text(_render_fix(case), encoding="utf-8")
    (case_dir / "replay.tcl").write_text(_render_replay(case), encoding="utf-8")
    shutil.copy2(RUNTIME_TEMPLATE, case_dir / "pilot_runtime.tcl")
    shutil.copy2(baseline / "qualification.json", case_dir / "baseline_qualification.json")

    baseline_manifest = baseline / "manifest.tcl"
    setup_view = _manifest_tcl_value(
        baseline_manifest, "::SFT_SETUP_VIEW", "functional_setup_ss"
    )
    hold_view = _manifest_tcl_value(
        baseline_manifest, "::SFT_HOLD_VIEW", "functional_hold_ff"
    )
    manifest = {
        "id": case["id"],
        "type": case["type"],
        "difficulty": case["difficulty"],
        "design": catalog["design"]["top"],
        "tool_version": catalog["design"]["expected_tool_version"],
        "analysis_views": {"setup": setup_view, "hold": hold_view},
        "repair_mode": case["repair_mode"],
        "catalog_entry": case,
        "baseline_sha256": baseline_sha256,
        "catalog_sha256": catalog_sha256,
        "baseline_checksum_manifest_sha256": checksum_manifest_sha256,
        "baseline_qualification": {
            "schema_version": qualification["schema_version"],
            "status": qualification["status"],
            "gold_status": qualification["gold_status"],
            "signoff_eligible": qualification["signoff_eligible"],
            "technology_classification": qualification["technology_classification"],
            "sha256": qualification_sha256,
            "source_artifacts": _qualification_source_artifacts(
                qualification, require=False
            ),
        },
        "artifacts": _artifact_map(),
        "status": (
            "GOLD_PREPARED"
            if case["injection"]["calibration_status"] == "FROZEN"
            else "NOT_GOLD_CALIBRATION_PREPARED"
        ),
        "gold_eligible": case["injection"]["calibration_status"] == "FROZEN",
    }
    _write_json(case_dir / "manifest.json", manifest)


def prepare_run(args: argparse.Namespace, catalog: Mapping[str, Any]) -> Path:
    baseline = args.baseline_bundle.resolve()
    qualification = _validate_baseline(baseline, catalog)
    baseline_sha256 = _sha256_tree(baseline)
    qualification_sha256 = _sha256_file(baseline / "qualification.json")
    catalog_source = Path(getattr(args, "catalog", CATALOG_PATH)).resolve()
    source_catalog = load_catalog(catalog_source)
    if source_catalog != dict(catalog):
        raise PilotError(
            f"catalog changed between load and prepare, or does not match {catalog_source}"
        )
    catalog_sha256 = _sha256_file(catalog_source)
    guest_root = _check_guest_path(args.guest_root)
    run_id = args.run_id or dt.datetime.now(dt.timezone.utc).strftime("pilot10-%Y%m%dT%H%M%SZ")
    if not SAFE_ID.fullmatch(run_id):
        raise PilotError("--run-id may contain only letters, digits, '.', '_', and '-'")
    run_dir = args.work_root.resolve() / run_id
    if run_dir.exists():
        raise PilotError(f"run directory already exists; choose a new --run-id: {run_dir}")
    if not RUNTIME_TEMPLATE.is_file():
        raise PilotError(f"missing runtime template: {RUNTIME_TEMPLATE}")

    selected = _selected_cases(catalog, args.case)
    unfrozen = [
        case["id"]
        for case in selected
        if case["injection"]["calibration_status"] != "FROZEN"
    ]
    calibration_prepare = bool(getattr(args, "allow_unfrozen_calibration", False))
    if unfrozen and not calibration_prepare:
        raise PilotError(
            "selected cases are PROBE_REQUIRED; explicitly use "
            "prepare --allow-unfrozen-calibration for a NOT_GOLD bundle: "
            + ", ".join(unfrozen)
        )
    if calibration_prepare:
        frozen = sorted({case["id"] for case in selected} - set(unfrozen))
        if frozen:
            raise PilotError(
                "NOT_GOLD calibration preparation accepts only PROBE_REQUIRED cases, not: "
                + ", ".join(frozen)
            )
    else:
        _qualification_source_artifacts(qualification, require=True)
    run_dir.mkdir(parents=True)
    checksum_path = run_dir / "baseline_checksums.sha256"
    checksum_path.write_text(_baseline_checksum_text(baseline), encoding="utf-8")
    checksum_sha256 = _sha256_file(checksum_path)
    for case in selected:
        _write_case(
            run_dir,
            catalog,
            case,
            baseline,
            baseline_sha256,
            catalog_sha256,
            qualification,
            qualification_sha256,
            checksum_sha256,
        )
    prepared_catalog = run_dir / "catalog.json"
    shutil.copy2(catalog_source, prepared_catalog)
    if _sha256_file(prepared_catalog) != catalog_sha256:
        raise PilotError("prepared catalog copy does not match its source SHA256")
    _write_json(
        run_dir / "run_manifest.json",
        {
            "schema_version": "timing_eco_pilot_run.v1",
            "run_id": run_id,
            "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "baseline": {
                "source": str(baseline),
                "sha256": baseline_sha256,
                "checksum_manifest": {
                    "path": checksum_path.name,
                    "sha256": checksum_sha256,
                },
                "qualification": {
                    "sha256": qualification_sha256,
                    "schema_version": qualification["schema_version"],
                    "status": qualification["status"],
                    "gold_status": qualification["gold_status"],
                    "signoff_eligible": qualification["signoff_eligible"],
                    "technology_classification": qualification["technology_classification"],
                },
            },
            "catalog": {
                "source": str(catalog_source),
                "sha256": catalog_sha256,
            },
            "case_ids": [case["id"] for case in selected],
            "guest_root": guest_root,
            "status": (
                "NOT_GOLD_CALIBRATION_PREPARED" if calibration_prepare else "GOLD_PREPARED"
            ),
            "gold_eligible": not calibration_prepare,
        },
    )
    print(run_dir)
    return run_dir


def _load_run(run_dir: Path) -> dict[str, Any]:
    manifest = _require_mapping(_load_json(run_dir / "run_manifest.json"), "run manifest")
    if manifest.get("schema_version") != "timing_eco_pilot_run.v1":
        raise PilotError(f"unsupported run manifest: {run_dir / 'run_manifest.json'}")
    return dict(manifest)


def _check_guest_path(path: str) -> str:
    if (
        not isinstance(path, str)
        or not SAFE_GUEST_PATH.fullmatch(path)
    ):
        raise PilotError(f"unsafe guest path: {path!r}")
    normalized = path.rstrip("/")
    if not normalized:
        raise PilotError("guest path must be below /, not the filesystem root")
    # Reject ambiguous spellings rather than relying on the guest shell or
    # coreutils to normalize them before an opt-in destructive operation.
    # A trailing slash is harmless and is normalized for compatibility with
    # existing prepared manifests; every interior component must be literal.
    if normalized.startswith("//") or any(
        component in {"", ".", ".."}
        for component in normalized[1:].split("/")
    ):
        raise PilotError(f"unsafe guest path: {path!r}")
    return normalized


def _check_safe_id(value: Any, where: str) -> str:
    if not isinstance(value, str) or SAFE_ID.fullmatch(value) is None:
        raise PilotError(
            f"{where} may contain only letters, digits, '.', '_', and '-'"
        )
    return value


def _guest_run_path(guest_root: str, run_id: Any) -> str:
    root = _check_guest_path(guest_root)
    safe_run_id = _check_safe_id(run_id, "run manifest run_id")
    return _check_guest_path(f"{root}/{safe_run_id}")


def _guest_case_path(guest_root: str, run_id: Any, case_id: Any) -> str:
    guest_run = _guest_run_path(guest_root, run_id)
    safe_case_id = _check_safe_id(case_id, "run manifest case_id")
    return _check_guest_path(f"{guest_run}/cases/{safe_case_id}")


def _guest_replay_path(
    guest_root: str, run_id: Any, case_id: Any, replay_index: Any
) -> str:
    guest_case = _guest_case_path(guest_root, run_id, case_id)
    if (
        isinstance(replay_index, bool)
        or not isinstance(replay_index, int)
        or replay_index < 1
    ):
        raise PilotError("replay index must be a positive integer")
    return _check_guest_path(f"{guest_case}/replay_{replay_index}")


def _manifest_case_ids(
    manifest: Mapping[str, Any], catalog: Mapping[str, Any]
) -> list[str]:
    raw = manifest.get("case_ids")
    if not isinstance(raw, list) or not raw:
        raise PilotError("run manifest case_ids must be a non-empty array")
    catalog_ids = {str(case["id"]) for case in catalog["cases"]}
    result: list[str] = []
    for index, value in enumerate(raw):
        case_id = _check_safe_id(value, f"run manifest case_ids[{index}]")
        if case_id not in catalog_ids:
            raise PilotError(f"run manifest contains unknown case_id: {case_id}")
        if case_id in result:
            raise PilotError(f"run manifest contains duplicate case_id: {case_id}")
        result.append(case_id)
    return result


def _display_command(command: Sequence[str]) -> str:
    return shlex.join(map(str, command))


def _execute(
    command: Sequence[str], *, dry_run: bool, capture_path: Path | None = None
) -> subprocess.CompletedProcess[str]:
    print(f"+ {_display_command(command)}", flush=True)
    if dry_run:
        return subprocess.CompletedProcess(command, 0, "", "")
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if capture_path is not None:
        capture_path.parent.mkdir(parents=True, exist_ok=True)
        capture_path.write_text(result.stdout, encoding="utf-8")
    elif result.stdout:
        print(result.stdout, end="")
    return result


def _remote(ssh: str, target: str, argv: Sequence[str]) -> list[str]:
    return [ssh, "-o", "BatchMode=yes", target, shlex.join(argv)]


def _sync_to_guest(
    rsync: str, source: Path, target: str, guest_path: str, dry_run: bool
) -> None:
    command = [rsync, "-az", "--delete"]
    command.extend([f"{source.resolve()}/", f"{target}:{guest_path}/"])
    result = _execute(command, dry_run=dry_run)
    if result.returncode != 0:
        raise PilotError(f"rsync to guest failed ({result.returncode})")


def _sync_file_to_guest(
    rsync: str, source: Path, target: str, guest_path: str, dry_run: bool
) -> None:
    command = [rsync, "-az", str(source.resolve()), f"{target}:{guest_path}"]
    result = _execute(command, dry_run=dry_run)
    if result.returncode != 0:
        raise PilotError(f"rsync file to guest failed ({result.returncode}): {source}")


def _fetch_from_guest(
    rsync: str, target: str, guest_path: str, destination: Path, dry_run: bool
) -> bool:
    destination.mkdir(parents=True, exist_ok=True)
    command = [rsync, "-az"]
    command.extend([f"{target}:{guest_path}/", f"{destination.resolve()}/"])
    result = _execute(command, dry_run=dry_run)
    return result.returncode == 0


def _typed_success_marker_present(
    marker: Path, *, case_id: str, calibration_mode: bool, dry_run: bool
) -> bool:
    if dry_run:
        return True
    expected = (
        f"{case_id} one-shot calibration completed; this is not a Gold replay\n"
        if calibration_mode
        else f"{case_id} replay completed\n"
    )
    try:
        return (
            not marker.is_symlink()
            and marker.is_file()
            and marker.read_bytes() == expected.encode("utf-8")
        )
    except OSError:
        return False


def _materialize_fetched_checkpoint(
    run_dir: Path,
    *,
    case_id: str,
    replay_index: int,
    replay_dir: Path,
) -> Path:
    """Normalize exactly one already-gated fetched replay checkpoint."""

    normalizer = _checkpoint_normalizer()
    context = normalizer._load_context(run_dir)
    if case_id not in context.case_ids:
        raise PilotError(
            f"checkpoint materialization case is absent from run manifest: {case_id}"
        )
    expected_replay = (
        context.run_dir / "cases" / case_id / "runs" / f"replay_{replay_index}"
    )
    if replay_dir.absolute() != expected_replay:
        raise PilotError(
            "checkpoint materialization replay path does not match the run manifest: "
            f"{replay_dir}"
        )
    return normalizer.materialize_replay(
        context, case_id, replay_index, expected_replay
    )


def _checkpoint_materialization_status(
    *,
    candidate: bool,
    dry_run: bool,
) -> dict[str, Any]:
    if not candidate:
        state = "not_candidate"
    elif dry_run:
        state = "dry_run_only"
    else:
        state = "pending"
    return {
        "schema_version": CHECKPOINT_MATERIALIZATION_STATUS_SCHEMA,
        "candidate": candidate,
        "required": candidate and not dry_run,
        "attempted": False,
        "succeeded": None,
        "state": state,
        "report": None,
        "report_sha256": None,
        "report_bytes": None,
        "error": None,
        "dry_run": dry_run,
    }


def _cleanup_guest_replay(
    args: argparse.Namespace,
    *,
    guest_root: str,
    run_id: str,
    case_id: str,
    replay_index: int,
) -> subprocess.CompletedProcess[str]:
    """Delete and verify exactly one validated replay leaf directory."""

    guest_replay = _guest_replay_path(
        guest_root, run_id, case_id, replay_index
    )
    expected_suffix = (
        _check_safe_id(run_id, "run manifest run_id"),
        "cases",
        _check_safe_id(case_id, "run manifest case_id"),
        f"replay_{replay_index}",
    )
    if tuple(Path(guest_replay).parts[-4:]) != expected_suffix:
        raise PilotError(
            f"refusing cleanup outside an exact guest replay leaf: {guest_replay}"
        )
    return _execute(
        _remote(
            args.ssh_bin,
            args.ssh_target,
            [
                "sh",
                "-c",
                GUEST_REPLAY_CLEANUP_SCRIPT,
                "sft-replay-cleanup",
                guest_replay,
            ],
        ),
        dry_run=args.dry_run,
    )


def _case_bundle_files(case_dir: Path) -> Iterable[Path]:
    for name in (
        "case_config.tcl",
        "inject.tcl",
        "fix.tcl",
        "replay.tcl",
        "pilot_runtime.tcl",
        "baseline_qualification.json",
        "manifest.json",
    ):
        yield case_dir / name


def _make_replay_bundle(case_dir: Path, replay_dir: Path) -> None:
    replay_dir.mkdir(parents=True)
    (replay_dir / "reports").mkdir()
    (replay_dir / "logs").mkdir()
    for source in _case_bundle_files(case_dir):
        shutil.copy2(source, replay_dir / source.name)


def run_remote(args: argparse.Namespace) -> None:
    run_dir = args.run_dir.resolve()
    manifest = _load_run(run_dir)
    baseline = Path(manifest["baseline"]["source"])
    catalog = load_catalog(run_dir / "catalog.json")
    qualification = _validate_baseline(baseline, catalog)
    expected_baseline_sha256 = manifest.get("baseline", {}).get("sha256")
    if not isinstance(expected_baseline_sha256, str) or not SHA256_RE.fullmatch(
        expected_baseline_sha256
    ):
        raise PilotError("prepared run manifest has no valid baseline SHA256")
    actual_baseline_sha256 = _sha256_tree(baseline)
    if actual_baseline_sha256 != expected_baseline_sha256:
        raise PilotError(
            "baseline bundle SHA256 changed after prepare; create a fresh prepared run"
        )
    baseline_metadata = _require_mapping(manifest.get("baseline"), "run baseline metadata")
    checksum_metadata = _require_mapping(
        baseline_metadata.get("checksum_manifest"), "run baseline checksum manifest"
    )
    checksum_name = checksum_metadata.get("path")
    checksum_digest = checksum_metadata.get("sha256")
    if checksum_name != "baseline_checksums.sha256" or not isinstance(
        checksum_digest, str
    ) or not SHA256_RE.fullmatch(checksum_digest):
        raise PilotError("prepared run has invalid baseline checksum-manifest metadata")
    checksum_path = run_dir / checksum_name
    if _sha256_file(checksum_path) != checksum_digest:
        raise PilotError("baseline checksum manifest changed after prepare")
    if checksum_path.read_text(encoding="utf-8") != _baseline_checksum_text(baseline):
        raise PilotError("baseline checksum manifest no longer matches the baseline files")
    qualification_metadata = _require_mapping(
        baseline_metadata.get("qualification"), "run baseline qualification metadata"
    )
    qualification_sha256 = qualification_metadata.get("sha256")
    if not isinstance(qualification_sha256, str) or not SHA256_RE.fullmatch(
        qualification_sha256
    ):
        raise PilotError("prepared run has no valid qualification SHA256")
    if _sha256_file(baseline / "qualification.json") != qualification_sha256:
        raise PilotError("baseline qualification SHA256 changed after prepare")
    for key in (
        "schema_version",
        "status",
        "gold_status",
        "signoff_eligible",
        "technology_classification",
    ):
        if qualification_metadata.get(key) != qualification.get(key):
            raise PilotError(f"baseline qualification metadata changed for {key}")
    expected_catalog_sha256 = manifest.get("catalog", {}).get("sha256")
    if not isinstance(expected_catalog_sha256, str) or not SHA256_RE.fullmatch(
        expected_catalog_sha256
    ):
        raise PilotError("prepared run manifest has no valid catalog SHA256")
    if _sha256_file(run_dir / "catalog.json") != expected_catalog_sha256:
        raise PilotError("prepared catalog SHA256 does not match the run manifest")
    guest_root = _check_guest_path(args.guest_root or manifest["guest_root"])
    run_id = _check_safe_id(manifest.get("run_id"), "run manifest run_id")
    case_ids = _manifest_case_ids(manifest, catalog)
    guest_run = _guest_run_path(guest_root, run_id)
    guest_baseline = f"{guest_run}/baseline"
    guest_checksums = f"{guest_run}/baseline_checksums.sha256"
    by_id = {case["id"]: case for case in catalog["cases"]}
    unfrozen = [
        case_id
        for case_id in case_ids
        if by_id[case_id]["injection"]["calibration_status"] != "FROZEN"
    ]
    calibration_mode = bool(getattr(args, "allow_unfrozen_calibration", False))
    if unfrozen and not calibration_mode:
        raise PilotError(
            "prepared run contains PROBE_REQUIRED cases; use "
            "--allow-unfrozen-calibration for a one-shot NOT_GOLD measurement, "
            f"or freeze the catalog first: {', '.join(unfrozen)}"
        )
    if calibration_mode:
        frozen = sorted(set(case_ids) - set(unfrozen))
        if frozen:
            raise PilotError(
                "NOT_GOLD calibration mode accepts only PROBE_REQUIRED cases, not frozen: "
                + ", ".join(frozen)
            )
        if args.replays not in (None, 1):
            raise PilotError("NOT_GOLD calibration mode requires exactly one replay")
    else:
        required_replays = int(catalog["design"]["acceptance"]["replay_runs"])
        if args.replays is not None and args.replays != required_replays:
            raise PilotError(
                f"Gold mode requires exactly {required_replays} fresh replays"
            )

    root_mkdir = _execute(
        _remote(args.ssh_bin, args.ssh_target, ["mkdir", "-p", "--", guest_root]),
        dry_run=args.dry_run,
    )
    if root_mkdir.returncode != 0:
        raise PilotError("failed to create the guest task root")
    run_mkdir = _execute(
        _remote(args.ssh_bin, args.ssh_target, ["mkdir", "--", guest_run]),
        dry_run=args.dry_run,
    )
    if run_mkdir.returncode != 0:
        raise PilotError(
            f"guest run directory already exists or cannot be created: {guest_run}"
        )
    baseline_mkdir = _execute(
        _remote(args.ssh_bin, args.ssh_target, ["mkdir", "--", guest_baseline]),
        dry_run=args.dry_run,
    )
    if baseline_mkdir.returncode != 0:
        raise PilotError(f"failed to create fresh guest baseline directory: {guest_baseline}")
    _sync_to_guest(args.rsync_bin, baseline, args.ssh_target, guest_baseline, args.dry_run)
    _sync_file_to_guest(
        args.rsync_bin,
        checksum_path,
        args.ssh_target,
        guest_checksums,
        args.dry_run,
    )
    verify_baseline = _execute(
        _remote(
            args.ssh_bin,
            args.ssh_target,
            [
                "sh",
                "-lc",
                f"cd {shlex.quote(guest_baseline)} && "
                f"sha256sum --check --strict --quiet {shlex.quote(guest_checksums)}",
            ],
        ),
        dry_run=args.dry_run,
    )
    if verify_baseline.returncode != 0:
        raise PilotError("guest baseline failed content verification after rsync")

    replay_count = 1 if calibration_mode else (
        args.replays or catalog["design"]["acceptance"]["replay_runs"]
    )
    statuses: dict[str, list[dict[str, Any]]] = {}
    failed = False
    cleanup_requested = bool(
        getattr(args, "cleanup_passed_guest_replays", False)
    )
    for case_id in case_ids:
        case_dir = run_dir / "cases" / case_id
        statuses[case_id] = []
        for replay_index in range(1, replay_count + 1):
            local_replay = case_dir / "runs" / f"replay_{replay_index}"
            if local_replay.exists():
                raise PilotError(
                    f"local replay directory already exists; use a fresh run: {local_replay}"
                )
            _make_replay_bundle(case_dir, local_replay)
            guest_replay = _guest_replay_path(
                guest_root, run_id, case_id, replay_index
            )
            guest_case = _guest_case_path(guest_root, run_id, case_id)
            parent_mkdir = _execute(
                _remote(args.ssh_bin, args.ssh_target, ["mkdir", "-p", "--", guest_case]),
                dry_run=args.dry_run,
            )
            if parent_mkdir.returncode != 0:
                raise PilotError(f"failed to create guest case directory for {case_id}")
            replay_mkdir = _execute(
                _remote(args.ssh_bin, args.ssh_target, ["mkdir", "--", guest_replay]),
                dry_run=args.dry_run,
            )
            if replay_mkdir.returncode != 0:
                raise PilotError(
                    "guest replay directory already exists or cannot be created: "
                    f"{guest_replay}"
                )
            _sync_to_guest(
                args.rsync_bin, local_replay, args.ssh_target, guest_replay, args.dry_run
            )

            remote_command = [
                "env",
                f"SFT_BASELINE_DIR={guest_baseline}",
                f"SFT_REPLAY_INDEX={replay_index}",
                f"SFT_BASELINE_SHA256={actual_baseline_sha256}",
                f"SFT_CATALOG_SHA256={expected_catalog_sha256}",
                f"SFT_BASELINE_QUALIFICATION_SHA256={qualification_sha256}",
                f"SFT_BASELINE_CHECKSUMS_SHA256={checksum_digest}",
                f"SFT_BASELINE_STATUS={qualification['status']}",
                f"SFT_TECHNOLOGY_CLASSIFICATION={qualification['technology_classification']}",
            ]
            if calibration_mode:
                remote_command.append("SFT_ALLOW_UNFROZEN_CALIBRATION=1")
            remote_command.extend([
                "timeout",
                str(args.timeout_seconds),
                args.innovus_bin,
                "-cds_lib_file",
                f"{guest_baseline}/cds.lib",
                "-no_gui",
                "-log",
                "logs/innovus.log",
                "-files",
                "replay.tcl",
            ])
            result = _execute(
                _remote(
                    args.ssh_bin,
                    args.ssh_target,
                    ["sh", "-lc", f"cd {shlex.quote(guest_replay)} && {shlex.join(remote_command)}"],
                ),
                dry_run=args.dry_run,
                capture_path=local_replay / "logs" / "host_ssh.log",
            )
            fetched = _fetch_from_guest(
                args.rsync_bin,
                args.ssh_target,
                guest_replay,
                local_replay,
                args.dry_run,
            )
            expected_marker = (
                local_replay / "NOT_GOLD_CALIBRATION"
                if calibration_mode
                else local_replay / "SFT_CASE_PASSED"
            )
            marker_present = _typed_success_marker_present(
                expected_marker,
                case_id=case_id,
                calibration_mode=calibration_mode,
                dry_run=args.dry_run,
            )
            replay_candidate = result.returncode == 0 and fetched and marker_present
            materialization = _checkpoint_materialization_status(
                candidate=replay_candidate,
                dry_run=bool(args.dry_run),
            )
            materialization_gate_satisfied = False
            if replay_candidate and not args.dry_run:
                materialization["attempted"] = True
                try:
                    materialization_report = _materialize_fetched_checkpoint(
                        run_dir,
                        case_id=case_id,
                        replay_index=replay_index,
                        replay_dir=local_replay,
                    )
                    expected_report = local_replay / "checkpoint_link_materialization.json"
                    if (
                        materialization_report.absolute() != expected_report.absolute()
                        or materialization_report.is_symlink()
                        or not materialization_report.is_file()
                    ):
                        raise PilotError(
                            "checkpoint normalizer did not return its exact regular report: "
                            f"{materialization_report}"
                        )
                    report_bytes = materialization_report.stat().st_size
                    report_sha256 = _sha256_file(materialization_report)
                except Exception as exc:
                    materialization["state"] = "failed"
                    materialization["succeeded"] = False
                    materialization["error"] = {
                        "code": "CHECKPOINT_LINK_MATERIALIZATION_FAILED",
                        "type": type(exc).__name__,
                        "message": str(exc),
                    }
                else:
                    materialization["state"] = "materialized_verified"
                    materialization["succeeded"] = True
                    materialization["report"] = materialization_report.name
                    materialization["report_sha256"] = report_sha256
                    materialization["report_bytes"] = report_bytes
                    materialization_gate_satisfied = True
            cleanup_state: dict[str, Any] = {
                "requested": cleanup_requested,
                "eligible": False,
                "state": "not_requested" if not cleanup_requested else "pending",
                "command_issued": False,
                "succeeded": None,
                "verified_absent": None,
                "returncode": None,
                "target": guest_replay if cleanup_requested else None,
                "local_status_persisted_before_command": False,
                "error": None,
                "dry_run": bool(args.dry_run),
            }
            status = {
                "replay": replay_index,
                "innovus_exit_code": result.returncode,
                "fetched": fetched,
                "passed": replay_candidate and materialization_gate_satisfied,
                "expected_marker": expected_marker.name,
                "marker_present": marker_present,
                "checkpoint_link_materialization": materialization,
                "baseline_sha256": actual_baseline_sha256,
                "catalog_sha256": expected_catalog_sha256,
                "baseline_qualification_sha256": qualification_sha256,
                "baseline_checksum_manifest_sha256": checksum_digest,
                "baseline_status": qualification["status"],
                "guest_baseline_checksums_verified": verify_baseline.returncode == 0,
                "mode": "NOT_GOLD_CALIBRATION" if calibration_mode else "GOLD_REPLAY",
                "gold_eligible": not calibration_mode,
                "guest_replay_cleanup": cleanup_state,
            }
            cleanup_state["eligible"] = (
                status["passed"] and materialization["succeeded"] is True
            )
            if cleanup_requested and cleanup_state["eligible"]:
                cleanup_state["state"] = "armed"
                # Once this exact JSON reaches stable local storage, a crash
                # can no longer erase the proof that made deletion eligible.
                cleanup_state["local_status_persisted_before_command"] = True
            elif cleanup_requested:
                cleanup_state["state"] = "retained_not_eligible"
            statuses[case_id].append(status)
            local_status_path = local_replay / "run_status.json"
            # This durable local status, including passed=true and typed-marker
            # proof, must exist before the opt-in destructive remote action.
            _write_json(local_status_path, status, durable=True)
            cleanup_failed = False
            if cleanup_requested and cleanup_state["eligible"]:
                try:
                    cleanup_result = _cleanup_guest_replay(
                        args,
                        guest_root=guest_root,
                        run_id=run_id,
                        case_id=case_id,
                        replay_index=replay_index,
                    )
                except (PilotError, OSError, subprocess.SubprocessError) as exc:
                    # PilotError/OSError can reject the target or fail to spawn
                    # SSH before a command exists remotely.  A SubprocessError
                    # means an invocation began but did not return normally.
                    cleanup_state["command_issued"] = isinstance(
                        exc, subprocess.SubprocessError
                    )
                    cleanup_state["state"] = "failed"
                    cleanup_state["succeeded"] = False
                    cleanup_state["verified_absent"] = False
                    cleanup_state["error"] = f"{type(exc).__name__}: {exc}"
                    cleanup_failed = True
                else:
                    if args.dry_run:
                        cleanup_state["state"] = "dry_run_only"
                    else:
                        cleanup_state["command_issued"] = True
                        cleanup_state["returncode"] = cleanup_result.returncode
                        cleanup_state["succeeded"] = cleanup_result.returncode == 0
                        cleanup_state["verified_absent"] = (
                            cleanup_result.returncode == 0
                        )
                        cleanup_state["state"] = (
                            "deleted_verified"
                            if cleanup_result.returncode == 0
                            else "failed"
                        )
                        if cleanup_result.returncode != 0:
                            cleanup_state["error"] = (
                                "remote permission normalization, deletion, or "
                                "post-delete absence verification failed with exit "
                                f"code {cleanup_result.returncode}"
                            )
                            cleanup_failed = True
                _write_json(local_status_path, status, durable=True)
            failed = failed or (
                not args.dry_run and (not status["passed"] or cleanup_failed)
            )
            if failed and args.fail_fast:
                break
        if failed and args.fail_fast:
            break

    run_status = {
        "run_id": manifest["run_id"],
        "guest_run": guest_run,
        "cases": statuses,
        "passed": not failed and not args.dry_run,
        "dry_run_completed": bool(args.dry_run and not failed),
        "dry_run": args.dry_run,
        "baseline_sha256": actual_baseline_sha256,
        "catalog_sha256": expected_catalog_sha256,
        "baseline_qualification_sha256": qualification_sha256,
        "baseline_checksum_manifest_sha256": checksum_digest,
        "baseline_status": qualification["status"],
        "guest_baseline_checksums_verified": verify_baseline.returncode == 0,
        "mode": "NOT_GOLD_CALIBRATION" if calibration_mode else "GOLD_REPLAY",
        "gold_eligible": not calibration_mode,
        "cleanup_passed_guest_replays": cleanup_requested,
    }
    _write_json(run_dir / "run_status.json", run_status, durable=True)
    if failed:
        raise PilotError(
            "one or more Innovus replays, checkpoint materializations, or "
            "requested guest cleanups failed; "
            f"see {run_dir / 'run_status.json'}"
        )


def fetch_remote(args: argparse.Namespace) -> None:
    run_dir = args.run_dir.resolve()
    manifest = _load_run(run_dir)
    catalog = load_catalog(run_dir / "catalog.json")
    guest_root = _check_guest_path(args.guest_root or manifest["guest_root"])
    run_id = _check_safe_id(manifest.get("run_id"), "run manifest run_id")
    case_ids = _manifest_case_ids(manifest, catalog)
    guest_run = _guest_run_path(guest_root, run_id)
    fetched_any = False
    for case_id in case_ids:
        case_dir = run_dir / "cases" / case_id
        for replay_index in range(1, args.replays + 1):
            guest_replay = _guest_replay_path(
                guest_root, run_id, case_id, replay_index
            )
            local_replay = case_dir / "runs" / f"replay_{replay_index}"
            fetched_any |= _fetch_from_guest(
                args.rsync_bin,
                args.ssh_target,
                guest_replay,
                local_replay,
                args.dry_run,
            )
    if not fetched_any and not args.dry_run:
        raise PilotError(f"no replay artifacts were fetched from {guest_run}")


def list_cases(catalog: Mapping[str, Any]) -> None:
    print("ID          TYPE   LEVEL   MODE      INJECTION                          REPAIR")
    for case in catalog["cases"]:
        print(
            f"{case['id']:<11} {case['type']:<6} {case['difficulty']:<7} "
            f"{case['repair_mode']:<9} {case['injection']['strategy']:<34} "
            f"{case['repair']['strategy']}"
        )


def _common_remote(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ssh-target", default="qingteng-fc")
    parser.add_argument("--ssh-bin", default="ssh")
    parser.add_argument("--rsync-bin", default="rsync")
    parser.add_argument("--guest-root", default=None)
    parser.add_argument("--dry-run", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=CATALOG_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="validate and display the deterministic catalog")

    prepare = subparsers.add_parser("prepare", help="render a new local replay bundle")
    prepare.add_argument("--baseline-bundle", type=Path, default=DEFAULT_BASELINE)
    prepare.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    prepare.add_argument("--run-id")
    prepare.add_argument("--case", action="append", default=[], metavar="ID|all")
    prepare.add_argument("--guest-root", default="/home/host/nvdla_timing_eco_sft")
    prepare.add_argument(
        "--allow-unfrozen-calibration",
        action="store_true",
        help="prepare an explicitly NOT_GOLD bundle containing only PROBE_REQUIRED cases",
    )

    run = subparsers.add_parser("run", help="sync, execute, and fetch a prepared run")
    run.add_argument("--run-dir", type=Path, required=True)
    run.add_argument("--innovus-bin", default="innovus")
    run.add_argument("--timeout-seconds", type=int, default=14400)
    run.add_argument("--replays", type=int, default=None)
    run.add_argument("--fail-fast", action="store_true")
    run.add_argument(
        "--cleanup-passed-guest-replays",
        action="store_true",
        help=(
            "after a replay passes, is fetched, has its typed marker, has its "
            "checkpoint links materialized and verified, and has a durable local "
            "run_status.json, delete only that exact guest replay directory; "
            "disabled by default"
        ),
    )
    run.add_argument(
        "--allow-unfrozen-calibration",
        action="store_true",
        help=(
            "run one fresh, explicitly NOT_GOLD measurement for PROBE_REQUIRED cases; "
            "no repair or Gold success marker is produced"
        ),
    )
    _common_remote(run)

    fetch = subparsers.add_parser("fetch", help="retry artifact fetch without rerunning Innovus")
    fetch.add_argument("--run-dir", type=Path, required=True)
    fetch.add_argument("--replays", type=int, default=2)
    _common_remote(fetch)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        catalog = load_catalog(args.catalog)
        if args.command == "list":
            list_cases(catalog)
        elif args.command == "prepare":
            prepare_run(args, catalog)
        elif args.command == "run":
            if args.timeout_seconds < 1:
                raise PilotError("--timeout-seconds must be positive")
            if args.replays is not None and args.replays < 1:
                raise PilotError("--replays must be positive")
            run_remote(args)
        elif args.command == "fetch":
            if args.replays < 1:
                raise PilotError("--replays must be positive")
            fetch_remote(args)
        else:  # pragma: no cover - argparse enforces the command set.
            parser.error(f"unknown command: {args.command}")
    except PilotError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

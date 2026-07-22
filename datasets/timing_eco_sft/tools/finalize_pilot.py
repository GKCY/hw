#!/usr/bin/env python3
"""Promote two real Innovus replays into one canonical Gold SFT case.

The Innovus runner intentionally writes provisional metrics.  This host-side
finalizer rebuilds every metric from fetched reports, verifies independent
replay and audit evidence, and only then writes ``pilot_10/cases/<ID>``.
Missing or unparseable evidence is always an error; zero is accepted only when
the EDA report explicitly says that the corresponding violation count is zero.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence


SCRIPT = Path(__file__).resolve()
DATASET_ROOT = SCRIPT.parents[1]
DEFAULT_OUTPUT_ROOT = DATASET_ROOT / "pilot_10"
PT_MODULE_PATH = SCRIPT.with_name("primetime_crosscheck.py")

PT_SPEC = importlib.util.spec_from_file_location(
    "timing_eco_primetime_crosscheck_for_finalizer", PT_MODULE_PATH
)
if PT_SPEC is None or PT_SPEC.loader is None:  # pragma: no cover - installation error
    raise RuntimeError(f"cannot load PrimeTime verifier: {PT_MODULE_PATH}")
ptx = importlib.util.module_from_spec(PT_SPEC)
PT_SPEC.loader.exec_module(ptx)


SCHEMA_VERSION = "timing_eco_pilot_finalizer.v2"
REPLAY_SCHEMA_VERSION = "timing_eco_replay_comparison.v1"
FUNCTIONAL_AUDIT_SCHEMA = "timing_eco_functional_audit.v1"
INJECTION_PROVENANCE_SCHEMA = "timing_eco_injection_provenance.v1"
DIAGNOSTIC_CONTEXT_SCHEMA = "timing_eco_diagnostic_context.v1"
VIOLATION_LOCALITY_SCHEMA = "timing_eco_violation_locality.v1"
BASELINE_GUARD_SCHEMA = "timing_eco_baseline_guard.v1"
PHYSICAL_NO_REGRESSION_SCHEMA = "timing_eco_physical_no_regression.v1"
BASELINE_QUALIFICATION_SCHEMA = "smic40_baseline_qualification.v1"
BASELINE_QUALIFICATION_STATUS = "QUALIFIED_CANDIDATE"
BASELINE_TECHNOLOGY_CLASSIFICATION = "derived_non_signoff"
QUALIFIED_LIBERTY_ROLES = {
    "setup_lib": "setup_liberty_ss",
    "hold_lib": "hold_liberty_ff",
}
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
MAX_DIAGNOSTIC_CONTEXT_BYTES = 128 * 1024
MAX_DIAGNOSTIC_TARGETS = 64
MAX_DIAGNOSTIC_LOCAL_CELLS = 8
MAX_DIAGNOSTIC_STRING_CHARS = 512
MAX_INSTRUCTION_CHARS = 64 * 1024
REQUIRED_FUNCTIONAL_CHECKS = {
    "fixed_netlist_written",
    "constraints_unchanged",
    "no_illegal_eco",
    "connectivity_clean",
    "check_design_completed",
    "cell_budget_respected",
    "matching_corner_spef_written",
}
EXPECTED_REPLAYS = 2
MIN_FINAL_SLACK_NS = 0.010
NUMERIC_EPSILON = 1.0e-9
REPLAY_TIMING_TOLERANCE_NS = 0.001
LOCALITY_INTERNAL_TOLERANCE_NS = 1.0e-6
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"
DRC_REPORT_LIMIT = 1_000_000
DRC_TRUNCATION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\btruncat\w*\b",
        r"\b(?:limit|maximum)\b[^\r\n]{0,96}\b(?:reach\w*|exceed\w*|hit|stopp?\w*|terminat\w*)\b",
        r"\b(?:reach\w*|exceed\w*|hit)\b[^\r\n]{0,96}\b(?:limit|maximum)\b",
        r"\b(?:stopp?\w*|terminat\w*)\b[^\r\n]{0,96}\b(?:errors?|violations?)\b",
        r"\b(?:only|first)\s+\d+\s+(?:errors?|violations?)\b[^\r\n]{0,96}\b(?:report\w*|show\w*|list\w*)\b",
    )
)

REPORT_FILES = {
    "setup_before": "reports/setup_before.rpt",
    "hold_before": "reports/hold_before.rpt",
    "setup_after": "reports/setup_after.rpt",
    "hold_after": "reports/hold_after.rpt",
    "drv_before": "reports/drv_before.rpt",
    "drv_after": "reports/drv_after.rpt",
    "connectivity_before": "reports/connectivity_before.rpt",
    "connectivity_after": "reports/connectivity_after.rpt",
    "drc_before": "reports/drc_before.rpt",
    "drc_after": "reports/drc_after.rpt",
}

REPLAY_EVIDENCE_FILES = {
    **REPORT_FILES,
    "concrete_fix": "reports/concrete_fix.tcl",
    "constraint_before": "reports/constraint_before.sdc",
    "constraint_after": "reports/constraint_after.sdc",
    "constraint_setup_before": "reports/constraint_setup_before.sdc",
    "constraint_setup_after": "reports/constraint_setup_after.sdc",
    "constraint_hold_before": "reports/constraint_hold_before.sdc",
    "constraint_hold_after": "reports/constraint_hold_after.sdc",
    "functional_audit": "reports/functional_audit.json",
    "injection_provenance": "reports/injection_provenance.json",
    "baseline_guard": "reports/baseline_guard.json",
    "physical_no_regression": "reports/physical_no_regression.json",
    "resolved_targets": "reports/resolved_targets.tcl",
    "diagnostic_context": "reports/diagnostic_context.json",
    "violation_locality": "reports/violation_locality.json",
    "fixed_netlist": "fixed.v",
    "violating_checkpoint": "violating.enc",
    "before_netlist": "before.v",
    "setup_spef_before": "setup_before.spef",
    "hold_spef_before": "hold_before.spef",
    "setup_spef": "setup_after.spef",
    "hold_spef": "hold_after.spef",
    "innovus_log": "logs/innovus.log",
    "host_ssh_log": "logs/host_ssh.log",
    "success_marker": "SFT_CASE_PASSED",
    "run_status": "run_status.json",
}
REPLAY_EVIDENCE_DIRECTORIES = {
    "violating_checkpoint_data": "violating.enc.dat",
    "check_design_after": "reports/check_design_after",
}
NATIVE_CELL_DIFF_RELATIVE = "reports/native_cell_diff.tcl"
NATIVE_SELECTED_TERMS_RELATIVE = "reports/native_selected_terms.txt"

FORBIDDEN_FIX_COMMANDS = tuple(
    re.compile(rf"(?im)^\s*{command}\b")
    for command in (
        "create_clock",
        "set_clock_uncertainty",
        "set_clock_latency",
        "set_false_path",
        "set_multicycle_path",
        "set_disable_timing",
        "set_max_delay",
        "set_min_delay",
        "reset_path",
        "set_input_delay",
        "set_output_delay",
        "set_analysis_view",
        "set_interactive_constraint_modes",
    )
)

# Bind deterministic ECO names together with their hierarchy.  Matching only
# the leaf would turn ``u_exp/SFT_ECO_...`` into ``SFT_ECO_...`` and reject a
# fully exposed, valid design object.
ECO_INSTANCE_NAME_RE = re.compile(
    r"(?<![A-Za-z0-9_./:@+$\-])"
    r"((?:[A-Za-z0-9_.:@+$\-]+/)*SFT_ECO_[A-Za-z0-9_.:@+$\-]+)"
)


class FinalizeError(RuntimeError):
    """An actionable failure in the Gold-evidence contract."""


def _read_text(path: Path, label: str = "file") -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FinalizeError(f"missing {label}: {path}") from exc
    except UnicodeDecodeError as exc:
        raise FinalizeError(f"{label} is not valid UTF-8: {path}: {exc}") from exc
    if not text:
        raise FinalizeError(f"{label} is empty: {path}")
    return text


def _read_json(path: Path, label: str = "JSON file") -> Any:
    try:
        return json.loads(_read_text(path, label))
    except json.JSONDecodeError as exc:
        raise FinalizeError(f"invalid JSON in {path}: {exc}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except FileNotFoundError as exc:
        raise FinalizeError(f"missing file for SHA256: {path}") from exc
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalized_verilog_sha256(path: Path) -> str:
    """Hash generated netlist semantics while ignoring volatile header comments.

    Cadence may stamp ``saveNetlist`` headers independently in each process.
    Block comments and comment-only ``//`` lines are removed, then whitespace
    is canonicalized.  Inline source text remains part of the hash.  The raw
    replay-1 file is still bound byte-for-byte to the PrimeTime input.
    """

    text = _read_text(path, "fixed gate netlist")
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    body_lines = [
        line for line in text.splitlines() if not re.match(r"^\s*//", line)
    ]
    canonical = re.sub(r"\s+", " ", "\n".join(body_lines)).strip()
    if not re.search(r"(?i)\bmodule\b", canonical) or not re.search(
        r"(?i)\bendmodule\b", canonical
    ):
        raise FinalizeError(f"{path}: fixed netlist has no complete Verilog module")
    return _sha256_bytes(canonical.encode("utf-8"))


def _required_file(root: Path, relative: str, label: str) -> Path:
    declared = Path(relative)
    if declared.is_absolute() or ".." in declared.parts:
        raise FinalizeError(f"unsafe {label} path: {relative!r}")
    root = root.resolve()
    path = (root / declared).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise FinalizeError(f"{label} escapes its evidence root: {path}") from exc
    if not path.is_file():
        raise FinalizeError(f"missing {label}: {path}")
    if path.stat().st_size == 0:
        raise FinalizeError(f"{label} is empty: {path}")
    return path


def _required_directory(root: Path, relative: str, label: str) -> Path:
    declared = Path(relative)
    if declared.is_absolute() or ".." in declared.parts:
        raise FinalizeError(f"unsafe {label} path: {relative!r}")
    root = root.resolve()
    path = (root / declared).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise FinalizeError(f"{label} escapes its evidence root: {path}") from exc
    if not path.is_dir() or path.is_symlink():
        raise FinalizeError(f"missing {label} directory: {path}")
    found_nonempty = False
    for item in path.rglob("*"):
        if item.is_symlink():
            raise FinalizeError(f"{label} contains a symlink: {item}")
        if item.is_dir():
            continue
        if not item.is_file():
            raise FinalizeError(f"{label} contains a non-regular entry: {item}")
        found_nonempty = found_nonempty or item.stat().st_size > 0
    if not found_nonempty:
        raise FinalizeError(f"{label} contains no non-empty file evidence: {path}")
    return path


def _tree_inventory(path: Path, label: str = "evidence tree") -> list[dict[str, Any]]:
    """Return a deterministic, symlink-free inventory for a non-empty tree."""

    path = path.resolve()
    if not path.is_dir() or path.is_symlink():
        raise FinalizeError(f"{label} is not a regular directory: {path}")
    files: list[dict[str, Any]] = []
    for item in sorted(path.rglob("*"), key=lambda candidate: candidate.as_posix()):
        if item.is_symlink():
            raise FinalizeError(f"{label} contains a symlink: {item}")
        if item.is_dir():
            continue
        if not item.is_file():
            raise FinalizeError(f"{label} contains a non-regular entry: {item}")
        files.append(
            {
                "path": item.relative_to(path).as_posix(),
                "sha256": _sha256(item),
                "bytes": item.stat().st_size,
            }
        )
    if not files or sum(int(item["bytes"]) for item in files) <= 0:
        raise FinalizeError(f"{label} contains no non-empty file evidence: {path}")
    return files


def _inventory_sha256(inventory: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in inventory:
        digest.update(
            (
                f"{item['path']}\0{item['sha256']}\0{item['bytes']}\n"
            ).encode("utf-8")
        )
    return digest.hexdigest()


def _tree_sha256(path: Path, label: str = "evidence tree") -> str:
    return _inventory_sha256(_tree_inventory(path, label))


def _artifact(path: Path, root: Path) -> dict[str, Any]:
    path = path.resolve()
    root = root.resolve()
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise FinalizeError(f"artifact is outside case root {root}: {path}") from exc
    if not path.is_file() or path.stat().st_size == 0:
        raise FinalizeError(f"artifact is missing or empty: {path}")
    return {
        "path": relative,
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
    }


def _tree_artifact(path: Path, root: Path, label: str = "evidence tree") -> dict[str, Any]:
    path = path.resolve()
    root = root.resolve()
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise FinalizeError(f"artifact tree is outside case root {root}: {path}") from exc
    inventory = _tree_inventory(path, label)
    return {
        "path": relative,
        "kind": "directory",
        "sha256": _inventory_sha256(inventory),
        "bytes": sum(int(item["bytes"]) for item in inventory),
        "files": len(inventory),
    }


def _normal_tool_version(value: str) -> str:
    value = value.strip()
    value = re.sub(r"(?i)^cadence\s+", "", value)
    value = re.sub(r"(?i)^innovus(?:\(tm\))?\s+", "", value)
    return value.lstrip("v")


def _innovus_report(
    path: Path, *, expected_design: str, expected_tool_version: str
) -> tuple[str, str]:
    text = _read_text(path, "Innovus report")
    generated = re.search(
        r"(?mi)^#\s*Generated by:\s*(?:Cadence\s+)?Innovus\s+([^\s]+)\s*$", text
    )
    if generated is None:
        raise FinalizeError(f"{path}: missing real Innovus 'Generated by' header")
    actual_version = generated.group(1)
    if _normal_tool_version(actual_version) != _normal_tool_version(expected_tool_version):
        raise FinalizeError(
            f"{path}: Innovus version {actual_version!r} does not match "
            f"manifest {expected_tool_version!r}"
        )
    design = re.search(r"(?mi)^#\s*Design:\s*(\S+)\s*$", text)
    if design is None or design.group(1) != expected_design:
        actual = design.group(1) if design is not None else "<missing>"
        raise FinalizeError(
            f"{path}: report design {actual!r} does not match {expected_design!r}"
        )
    return text, actual_version


def _report_command(path: Path, text: str) -> str:
    commands = re.findall(r"(?mi)^#\s*Command:\s*(\S.*\S|\S)\s*$", text)
    if len(commands) != 1:
        raise FinalizeError(
            f"{path}: expected exactly one Innovus Command header, found {len(commands)}"
        )
    return commands[0]


def _require_complete_drc_command(path: Path, command: str) -> None:
    occurrences = re.findall(r"(?:^|\s)-limit(?=\s|$)", command, re.IGNORECASE)
    matches = re.findall(
        r"(?:^|\s)-limit\s+(?:\{(\d+)\}|(\d+))(?=\s|$)",
        command,
        re.IGNORECASE,
    )
    limits = [int(braced or plain) for braced, plain in matches]
    if len(occurrences) != 1 or limits != [DRC_REPORT_LIMIT]:
        raise FinalizeError(
            f"{path}: verify_drc Command must contain exactly one "
            f"-limit {DRC_REPORT_LIMIT}"
        )


def _drc_truncation_signal(text: str) -> str | None:
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if re.search(r"\bnot\b[^\r\n]{0,24}\b(?:reach\w*|exceed\w*)\b", line, re.I):
            continue
        if any(pattern.search(line) for pattern in DRC_TRUNCATION_PATTERNS):
            return line.strip()
    return None


def parse_timing_report(
    path: Path,
    *,
    check: str,
    expected_view: str,
    expected_design: str,
    expected_tool_version: str,
) -> dict[str, Any]:
    """Parse endpoint-based WNS/TNS from a real Innovus ``report_timing`` file."""

    if check not in {"setup", "hold"}:
        raise FinalizeError(f"unsupported timing check: {check}")
    text, version = _innovus_report(
        path,
        expected_design=expected_design,
        expected_tool_version=expected_tool_version,
    )
    starts = list(re.finditer(r"(?mi)^Path\s+\d+\s*:\s*(.+)$", text))
    if not starts:
        raise FinalizeError(f"{path}: no timing path blocks were found")

    expected_label = "Setup Check" if check == "setup" else "Hold Check"
    paths: list[dict[str, Any]] = []
    for index, start in enumerate(starts):
        block_end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        block = text[start.start() : block_end]
        heading = start.group(1)
        if expected_label.lower() not in heading.lower():
            raise FinalizeError(
                f"{path}: {check} report contains non-{check} path heading {heading!r}"
            )
        # Innovus 21.10 prints an equals sign for setup slack but omits it for
        # hold slack.  Keep the whole-line match and the exactly-one-value
        # check below so table headers or duplicate summaries cannot be
        # mistaken for path evidence.
        slack_values = re.findall(
            rf"(?mi)^\s*(?:=\s*)?Slack(?:\s+Time)?\s+({NUMBER})\s*$", block
        )
        if len(slack_values) != 1:
            raise FinalizeError(
                f"{path}: path block {index + 1} has {len(slack_values)} Slack Time values"
            )
        slack = float(slack_values[0])
        if not math.isfinite(slack):
            raise FinalizeError(f"{path}: non-finite slack in path block {index + 1}")
        endpoint = re.search(r"(?mi)^Endpoint:\s+(\S+)", block)
        beginpoint = re.search(r"(?mi)^Beginpoint:\s+(\S+)", block)
        view = re.search(r"(?mi)^Analysis View:\s+(\S+)\s*$", block)
        if endpoint is None or beginpoint is None or view is None:
            raise FinalizeError(
                f"{path}: path block {index + 1} lacks endpoint, beginpoint, or analysis view"
            )
        if view.group(1) != expected_view:
            raise FinalizeError(
                f"{path}: analysis view {view.group(1)!r} does not match {expected_view!r}"
            )
        violated = "VIOLATED" in heading.upper()
        met = re.search(r"\bMET\b", heading, flags=re.IGNORECASE) is not None
        if violated == (slack >= -NUMERIC_EPSILON):
            raise FinalizeError(f"{path}: path status and slack disagree in block {index + 1}")
        if not violated and not met:
            raise FinalizeError(f"{path}: path block {index + 1} is neither MET nor VIOLATED")
        paths.append(
            {
                "endpoint": endpoint.group(1),
                "beginpoint": beginpoint.group(1),
                "slack_ns": slack,
            }
        )

    command_limit = re.search(
        r"(?mi)^#\s*Command:.*\s-max_paths\s+(\d+)\b", text
    )
    if command_limit is not None:
        limit = int(command_limit.group(1))
        if len(paths) >= limit and max(item["slack_ns"] for item in paths) < 0.0:
            raise FinalizeError(
                f"{path}: all {limit} reported paths violate timing; TNS evidence is truncated"
            )

    # Innovus TNS is endpoint based.  If multiple paths to one endpoint were
    # printed, only that endpoint's worst slack contributes.
    by_endpoint: dict[str, dict[str, Any]] = {}
    for timing_path in paths:
        previous = by_endpoint.get(timing_path["endpoint"])
        if previous is None or timing_path["slack_ns"] < previous["slack_ns"]:
            by_endpoint[timing_path["endpoint"]] = timing_path
    endpoint_paths = list(by_endpoint.values())
    worst = min(endpoint_paths, key=lambda item: item["slack_ns"])
    tns = round(
        sum(item["slack_ns"] for item in endpoint_paths if item["slack_ns"] < 0.0),
        9,
    )
    return {
        "wns_ns": worst["slack_ns"],
        "tns_ns": tns,
        "violating_paths": sum(item["slack_ns"] < 0.0 for item in endpoint_paths),
        "reported_paths": len(paths),
        "reported_endpoints": len(endpoint_paths),
        "worst_endpoint": worst["endpoint"],
        "worst_beginpoint": worst["beginpoint"],
        "analysis_view": expected_view,
        "tool_version": version,
    }


def _count_drv_section(segment: str, path: Path, check_type: str) -> int:
    whitespace_rows: list[float] = []
    row_pattern = re.compile(
        rf"(?m)^\s*(?![-+|])\S+(?:\s+[rf])?\s+{NUMBER}\s+{NUMBER}\s+({NUMBER})\s*$",
        flags=re.IGNORECASE,
    )
    for match in row_pattern.finditer(segment):
        slack = float(match.group(1))
        if slack >= -NUMERIC_EPSILON:
            raise FinalizeError(
                f"{path}: {check_type} all-violator table contains non-negative slack {slack}"
            )
        whitespace_rows.append(slack)

    # Innovus 21.10 emits report_constraint -all_violators as:
    # | Pin Name | Required | Actual | Slack | View |
    # Bind numeric pipe rows to exactly one preceding canonical header so an
    # unrelated or structurally malformed pipe table cannot become evidence.
    pipe_headers = list(
        re.finditer(
            r"(?mi)^\s*\|\s*Pin\s+Name\s*\|\s*Required\s*\|\s*Actual\s*\|"
            r"\s*Slack\s*\|\s*View\s*\|\s*$",
            segment,
        )
    )
    pipe_matches = list(
        re.finditer(
            rf"(?mi)^\s*\|\s*[^|\s][^|]*\|\s*{NUMBER}\s*\|\s*{NUMBER}\s*\|"
            rf"\s*({NUMBER})\s*\|\s*[^|\s][^|]*\|\s*$",
            segment,
        )
    )
    if len(pipe_headers) > 1:
        raise FinalizeError(
            f"{path}: {check_type} section has duplicate pipe-table headers"
        )
    if pipe_matches and (
        len(pipe_headers) != 1
        or any(match.start() < pipe_headers[0].end() for match in pipe_matches)
    ):
        raise FinalizeError(
            f"{path}: {check_type} pipe-table row appears without exactly one "
            "preceding canonical header"
        )
    pipe_rows = [float(match.group(1)) for match in pipe_matches]
    for slack in pipe_rows:
        if slack >= -NUMERIC_EPSILON:
            raise FinalizeError(
                f"{path}: {check_type} pipe-table violator row has "
                f"non-negative slack {slack}"
            )
    if whitespace_rows and (pipe_rows or pipe_headers):
        raise FinalizeError(
            f"{path}: {check_type} section ambiguously mixes whitespace and pipe tables"
        )
    numeric_rows = whitespace_rows or pipe_rows

    verbose_slacks = [
        float(value)
        for value in re.findall(
            rf"(?i)slack\s*:\s*({NUMBER})\s*\(\s*VIOLATED\s*\)", segment
        )
    ]
    for slack in verbose_slacks:
        if slack >= -NUMERIC_EPSILON:
            raise FinalizeError(
                f"{path}: {check_type} verbose VIOLATED row has non-negative slack {slack}"
            )
    verbose_rows = len(verbose_slacks)
    zero_marker = re.search(
        r"(?i)\bNo\s+(?:paths?|violations?)\s+(?:were\s+)?found\b|"
        r"\bNo\s+\w*\s*violations?\b",
        segment,
    ) is not None
    if zero_marker and (numeric_rows or verbose_rows):
        raise FinalizeError(
            f"{path}: {check_type} section contains both violation rows and a zero marker"
        )
    if numeric_rows and verbose_rows and len(numeric_rows) != verbose_rows:
        raise FinalizeError(
            f"{path}: ambiguous {check_type} count: table={len(numeric_rows)}, "
            f"verbose={verbose_rows}"
        )
    if numeric_rows:
        return len(numeric_rows)
    if verbose_rows:
        return verbose_rows
    if zero_marker:
        return 0
    raise FinalizeError(
        f"{path}: {check_type} section has neither violation rows nor an explicit zero marker"
    )


def parse_drv_report(
    path: Path, *, expected_design: str, expected_tool_version: str
) -> dict[str, int]:
    """Count max transition/capacitance/fanout rows in ``report_constraint``."""

    text, _ = _innovus_report(
        path,
        expected_design=expected_design,
        expected_tool_version=expected_tool_version,
    )
    command = _report_command(path, text)
    if not re.search(r"(?i)(?:^|\s)report_constraint(?:\s|$)", command) or not re.search(
        r"(?i)(?:^|\s)-all_violators(?:\s|$)", command
    ):
        raise FinalizeError(
            f"{path}: Command header is not report_constraint -all_violators"
        )
    headings = list(
        re.finditer(r"(?mi)^\s*Check\s+type\s*:\s*([a-z_]+)\s*$", text)
    )
    wanted = {"max_transition", "max_capacitance", "max_fanout"}
    found: dict[str, list[int]] = {name: [] for name in wanted}
    for index, heading in enumerate(headings):
        check_type = heading.group(1).lower()
        if check_type not in wanted:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        found[check_type].append(
            _count_drv_section(text[heading.end() : end], path, check_type)
        )
    missing = sorted(name for name, counts in found.items() if not counts)
    if missing:
        raise FinalizeError(
            f"{path}: missing explicit report_constraint sections: {', '.join(missing)}"
        )
    duplicates = sorted(name for name, counts in found.items() if len(counts) != 1)
    if duplicates:
        raise FinalizeError(
            f"{path}: duplicate report_constraint sections: {', '.join(duplicates)}"
        )
    return {
        "max_transition_violations": sum(found["max_transition"]),
        "max_capacitance_violations": sum(found["max_capacitance"]),
        "max_fanout_violations": sum(found["max_fanout"]),
    }


def parse_connectivity_report(
    path: Path, *, expected_design: str, expected_tool_version: str
) -> dict[str, int]:
    """Parse connectivity problems plus warnings from ``verifyConnectivity``."""

    text, _ = _innovus_report(
        path,
        expected_design=expected_design,
        expected_tool_version=expected_tool_version,
    )
    summary = re.search(
        r"(?is)Begin\s+Summary\s*(.*?)\s*End\s+Summary", text
    )
    if summary is None:
        raise FinalizeError(f"{path}: missing verifyConnectivity Begin/End Summary")
    summary_text = summary.group(1)
    candidates: list[tuple[int, int]] = []
    if re.search(r"(?i)Found\s+no\s+problems?\s+or\s+warnings?", summary_text):
        candidates.append((0, 0))
    found = re.search(
        r"(?i)Found\s+(\d+)\s+problems?(?:\s+(?:and|,)\s+(\d+)\s+warnings?)?",
        summary_text,
    )
    if found is not None:
        candidates.append((int(found.group(1)), int(found.group(2) or 0)))
    complete = re.search(
        r"(?i)Verification\s+Complete\s*:\s*(\d+)\s+Viols?\.?(?:\s+(\d+)\s+Wrngs?\.?)?",
        text,
    )
    if complete is not None:
        candidates.append((int(complete.group(1)), int(complete.group(2) or 0)))
    if not candidates:
        raise FinalizeError(f"{path}: connectivity summary has no explicit count or zero marker")
    if len(set(candidates)) != 1:
        raise FinalizeError(f"{path}: inconsistent connectivity summaries: {candidates}")
    problems, warnings = candidates[0]
    return {
        "violations": problems + warnings,
        "problems": problems,
        "warnings": warnings,
    }


def parse_drc_report(
    path: Path, *, expected_design: str, expected_tool_version: str
) -> dict[str, Any]:
    """Parse every violation record and reconcile it to verify_drc's total."""

    text, _ = _innovus_report(
        path,
        expected_design=expected_design,
        expected_tool_version=expected_tool_version,
    )
    command = _report_command(path, text)
    if not re.search(r"(?i)(?:^|\s)verify_drc(?:\s|$)", command):
        raise FinalizeError(f"{path}: Command header is not verify_drc")
    _require_complete_drc_command(path, command)
    truncation = _drc_truncation_signal(text)
    if truncation is not None:
        raise FinalizeError(
            f"{path}: DRC report contains an early-termination/truncation signal: "
            f"{truncation}"
        )
    totals = [
        int(value)
        for value in re.findall(
            r"(?mi)^\s*Total\s+Violations\s*:\s*(\d+)\s+Viols?\.\s*$", text
        )
    ]
    if not totals:
        raise FinalizeError(f"{path}: missing verify_drc total violation marker")
    if len(totals) != 1:
        raise FinalizeError(f"{path}: expected exactly one DRC total, found {totals}")
    total = totals[0]
    if total >= DRC_REPORT_LIMIT:
        raise FinalizeError(
            f"{path}: DRC total {total} reaches the collection limit "
            f"{DRC_REPORT_LIMIT}; completeness is unproven"
        )
    categories: dict[str, int] = {}
    for category in re.findall(
        r"(?m)^\s*([A-Z][A-Z0-9_.-]*):\s+\(", text
    ):
        categories[category] = categories.get(category, 0) + 1
    if sum(categories.values()) != total:
        raise FinalizeError(
            f"{path}: DRC report lists {sum(categories.values())} records but total is {total}; "
            "the report may be truncated"
        )
    return {"total": total, "categories": dict(sorted(categories.items()))}


def _audit_check_passed(value: Any, where: str) -> bool:
    if value is True:
        return True
    if isinstance(value, Mapping) and value.get("passed") is True:
        return True
    raise FinalizeError(f"{where} must be true or an object with passed=true")


def load_functional_audit(
    path: Path,
    case_id: str,
    design: str,
    *,
    repair_mode: str,
    max_eco_cells: int,
) -> dict[str, Any]:
    raw = _read_json(path, "functional audit")
    if not isinstance(raw, Mapping):
        raise FinalizeError(f"{path}: functional audit must be an object")
    if raw.get("schema_version") != FUNCTIONAL_AUDIT_SCHEMA:
        raise FinalizeError(f"{path}: missing or unsupported functional-audit schema")
    if raw.get("case_id") != case_id:
        raise FinalizeError(f"{path}: functional audit case_id does not match {case_id}")
    if "design" in raw and raw.get("design") != design:
        raise FinalizeError(f"{path}: functional audit design does not match {design}")
    if raw.get("passed") is not True:
        raise FinalizeError(f"{path}: functional audit did not pass")
    if raw.get("repair_mode") != repair_mode:
        raise FinalizeError(f"{path}: functional audit repair_mode does not match manifest")
    checks = raw.get("checks")
    if not isinstance(checks, Mapping) or not checks:
        raise FinalizeError(f"{path}: functional audit checks must be a non-empty object")
    missing = sorted(REQUIRED_FUNCTIONAL_CHECKS - set(checks))
    if missing:
        raise FinalizeError(
            f"{path}: functional audit is missing checks: {', '.join(missing)}"
        )
    for name, value in checks.items():
        if not isinstance(name, str) or not name:
            raise FinalizeError(f"{path}: functional audit check names must be non-empty")
        _audit_check_passed(value, f"{path}: checks.{name}")
    evidence = raw.get("evidence")
    if not isinstance(evidence, Mapping):
        raise FinalizeError(f"{path}: functional audit evidence must be an object")
    if evidence.get("max_eco_cells") != max_eco_cells:
        raise FinalizeError(f"{path}: audit max_eco_cells does not match the catalog")
    manual = evidence.get("manual_operations")
    native = evidence.get("native_operations")
    native_changed = evidence.get("native_changed_cells")
    for name, value in (
        ("manual_operations", manual),
        ("native_operations", native),
        ("native_changed_cells", native_changed),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise FinalizeError(f"{path}: evidence.{name} must be a non-negative integer")
    if repair_mode == "surgical":
        if manual < 1 or manual > max_eco_cells or native != 0:
            raise FinalizeError(f"{path}: surgical ECO operation count violates its budget")
    elif repair_mode == "native":
        if native != 1 or native_changed > max_eco_cells:
            raise FinalizeError(f"{path}: native changed-cell count violates its budget")
    else:  # _load_manifest normally catches this first.
        raise FinalizeError(f"{path}: unsupported repair mode {repair_mode!r}")
    return dict(raw)


def validate_native_cell_diff(path: Path) -> str:
    """Require the typed, standalone native before/after leaf-cell diff."""

    text = _read_text(path, "native cell diff")
    commands = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(commands) != 1 or re.match(
        r"^set\s+::SFT_NATIVE_CELL_DIFF(?:\s|$)", commands[0]
    ) is None:
        raise FinalizeError(
            f"{path}: native cell diff must contain exactly one typed "
            "set ::SFT_NATIVE_CELL_DIFF command"
        )
    return text


def validate_native_selected_terms(
    path: Path, diagnostic_context: Mapping[str, Any]
) -> str:
    """Bind the exact optDesign term-list file to runtime-resolved endpoints."""

    text = _read_text(path, "native selectedTerms file")
    if "\r" in text or not text.endswith("\n") or text.startswith("\n"):
        raise FinalizeError(
            f"{path}: native selectedTerms file must use LF with one term per line"
        )
    terms = text.splitlines()
    if any(not term or term != term.strip() for term in terms):
        raise FinalizeError(
            f"{path}: native selectedTerms file contains a blank or untrimmed term"
        )
    if len(terms) != len(set(terms)):
        raise FinalizeError(f"{path}: native selectedTerms file contains duplicate terms")
    expected = [str(target["endpoint"]) for target in diagnostic_context["targets"]]
    if terms != expected:
        raise FinalizeError(
            f"{path}: native selectedTerms do not exactly match diagnostic endpoints"
        )
    return text


def _diagnostic_token(value: Any, *, where: str, role: bool = False) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise FinalizeError(f"{where} must be a non-empty, trimmed string")
    if len(value) > MAX_DIAGNOSTIC_STRING_CHARS:
        raise FinalizeError(
            f"{where} exceeds {MAX_DIAGNOSTIC_STRING_CHARS} characters"
        )
    pattern = (
        r"[A-Za-z][A-Za-z0-9_.:-]*"
        if role
        else r"[A-Za-z0-9_./:@+$\-\[\]<>\\|~^%=,]+"
    )
    if re.fullmatch(pattern, value) is None:
        raise FinalizeError(f"{where} contains unsafe or non-object-name characters")
    if "inject" in value.lower():
        raise FinalizeError(f"{where} leaks hidden injection terminology")
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
        raise FinalizeError(f"{where} leaks hidden injection action or parameter")
    return value


def load_diagnostic_context(
    path: Path,
    *,
    case_id: str,
    design: str,
    max_eco_cells: int,
) -> dict[str, Any]:
    """Validate bounded repair-facing DB evidence without injection details."""

    if path.stat().st_size > MAX_DIAGNOSTIC_CONTEXT_BYTES:
        raise FinalizeError(
            f"{path}: diagnostic context exceeds {MAX_DIAGNOSTIC_CONTEXT_BYTES} bytes"
        )
    raw = _read_json(path, "diagnostic context")
    expected_top = {
        "schema_version",
        "case_id",
        "design",
        "max_eco_cells",
        "targets",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected_top:
        raise FinalizeError(
            f"{path}: diagnostic context must contain exactly {sorted(expected_top)}"
        )
    if raw.get("schema_version") != DIAGNOSTIC_CONTEXT_SCHEMA:
        raise FinalizeError(f"{path}: unsupported diagnostic-context schema")
    if raw.get("case_id") != case_id or raw.get("design") != design:
        raise FinalizeError(f"{path}: diagnostic context is not bound to {case_id}/{design}")
    declared_budget = raw.get("max_eco_cells")
    if (
        isinstance(declared_budget, bool)
        or not isinstance(declared_budget, int)
        or declared_budget != max_eco_cells
    ):
        raise FinalizeError(
            f"{path}: diagnostic max_eco_cells does not match catalog budget {max_eco_cells}"
        )
    targets = raw.get("targets")
    if not isinstance(targets, list) or not targets:
        raise FinalizeError(f"{path}: diagnostic targets must be a non-empty array")
    if len(targets) > MAX_DIAGNOSTIC_TARGETS:
        raise FinalizeError(
            f"{path}: diagnostic target count {len(targets)} exceeds "
            f"{MAX_DIAGNOSTIC_TARGETS}"
        )

    normalized_targets: list[dict[str, Any]] = []
    endpoint_modes: set[tuple[str, str]] = set()
    for index, target in enumerate(targets):
        where = f"{path}: targets[{index}]"
        if not isinstance(target, Mapping) or set(target) != DIAGNOSTIC_TARGET_FIELDS:
            raise FinalizeError(
                f"{where} must contain exactly {sorted(DIAGNOSTIC_TARGET_FIELDS)}"
            )
        timing = target.get("timing")
        if timing not in {"late", "early"}:
            raise FinalizeError(f"{where}.timing must be late or early")
        role = _diagnostic_token(target.get("role"), where=f"{where}.role", role=True)
        endpoint = _diagnostic_token(
            target.get("endpoint"), where=f"{where}.endpoint"
        )
        endpoint_mode = (timing, endpoint)
        if endpoint_mode in endpoint_modes:
            raise FinalizeError(
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
            raise FinalizeError(f"{where}.slack_ns must be finite and negative")
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
            raise FinalizeError(
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
            raise FinalizeError(
                f"{where}.local_cells must contain 1..{MAX_DIAGNOSTIC_LOCAL_CELLS} cells"
            )
        local_cells: list[dict[str, str]] = []
        local_instances: set[str] = set()
        for cell_index, cell in enumerate(raw_local_cells):
            cell_where = f"{where}.local_cells[{cell_index}]"
            if not isinstance(cell, Mapping) or set(cell) != DIAGNOSTIC_LOCAL_CELL_FIELDS:
                raise FinalizeError(
                    f"{cell_where} must contain exactly "
                    f"{sorted(DIAGNOSTIC_LOCAL_CELL_FIELDS)}"
                )
            inst = _diagnostic_token(cell.get("inst"), where=f"{cell_where}.inst")
            ref = _diagnostic_token(cell.get("ref"), where=f"{cell_where}.ref")
            if inst in local_instances:
                raise FinalizeError(f"{where}.local_cells contains duplicate instance {inst}")
            local_instances.add(inst)
            local_cells.append({"inst": inst, "ref": ref})
        if local_cells[0] != {"inst": driver_inst, "ref": driver_ref}:
            raise FinalizeError(
                f"{where}.local_cells[0] must be the current immediate driver"
            )
        if local_cells[-1]["inst"] != original_driver["inst"]:
            raise FinalizeError(
                f"{where}.local_cells must terminate at original_driver.inst"
            )
        normalized_targets.append(
            {
                "role": role,
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
        "schema_version": DIAGNOSTIC_CONTEXT_SCHEMA,
        "case_id": case_id,
        "design": design,
        "max_eco_cells": max_eco_cells,
        "targets": normalized_targets,
    }


def load_violation_locality(
    path: Path,
    *,
    case_id: str,
    repair_mode: str,
    case_type: str,
) -> dict[str, Any]:
    """Validate the endpoint-completeness proof produced before the repair."""

    if path.stat().st_size > MAX_DIAGNOSTIC_CONTEXT_BYTES:
        raise FinalizeError(
            f"{path}: violation locality exceeds {MAX_DIAGNOSTIC_CONTEXT_BYTES} bytes"
        )
    raw = _read_json(path, "violation locality")
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
        raise FinalizeError(
            f"{path}: violation locality must contain exactly {sorted(expected_top)}"
        )
    if raw.get("schema_version") != VIOLATION_LOCALITY_SCHEMA:
        raise FinalizeError(f"{path}: unsupported violation-locality schema")
    if raw.get("case_id") != case_id or raw.get("repair_mode") != repair_mode:
        raise FinalizeError(f"{path}: violation locality is not bound to this case")
    if raw.get("passed") is not True or raw.get("reasons") != []:
        raise FinalizeError(f"{path}: violation locality is not a clean passing record")

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
        "schema_version": VIOLATION_LOCALITY_SCHEMA,
        "case_id": case_id,
        "repair_mode": repair_mode,
    }
    for check in ("setup", "hold"):
        where = f"{path}: {check}"
        item = raw.get(check)
        if not isinstance(item, Mapping) or set(item) != expected_item_fields:
            raise FinalizeError(
                f"{where} must contain exactly {sorted(expected_item_fields)}"
            )
        required = check in required_checks
        if item.get("required") is not required:
            raise FinalizeError(f"{where}.required is inconsistent with {case_type}")
        for field in (
            "cardinality_passed",
            "locality_passed",
            "coverage_passed",
            "opposite_headroom_passed",
            "selected_slack_consistency_passed",
        ):
            if item.get(field) is not True:
                raise FinalizeError(f"{where}.{field} must pass")
        wns = _finite_json_number(item.get("wns_ns"), f"{where}.wns_ns")
        tns = _finite_json_number(item.get("tns_ns"), f"{where}.tns_ns")

        counts: dict[str, int] = {}
        for field in (
            "expected_endpoint_count",
            "selected_endpoint_count",
            "violating_endpoint_count",
        ):
            value = item.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise FinalizeError(f"{where}.{field} must be a nonnegative integer")
            if value > MAX_DIAGNOSTIC_TARGETS:
                raise FinalizeError(
                    f"{where}.{field} exceeds {MAX_DIAGNOSTIC_TARGETS}"
                )
            counts[field] = value

        endpoint_lists: dict[str, list[str]] = {}
        for field in ("selected_endpoints", "violating_endpoints"):
            values = item.get(field)
            if not isinstance(values, list):
                raise FinalizeError(f"{where}.{field} must be an array")
            parsed = [
                _diagnostic_token(value, where=f"{where}.{field}[{index}]")
                for index, value in enumerate(values)
            ]
            if parsed != sorted(set(parsed)):
                raise FinalizeError(f"{where}.{field} must be sorted and unique")
            endpoint_lists[field] = parsed

        selected = endpoint_lists["selected_endpoints"]
        violating = endpoint_lists["violating_endpoints"]
        slack_items = item.get("selected_endpoint_slacks")
        if not isinstance(slack_items, list):
            raise FinalizeError(f"{where}.selected_endpoint_slacks must be an array")
        selected_slacks: list[dict[str, Any]] = []
        for index, slack_item in enumerate(slack_items):
            slack_where = f"{where}.selected_endpoint_slacks[{index}]"
            if not isinstance(slack_item, Mapping) or set(slack_item) != {
                "endpoint",
                "slack_ns",
            }:
                raise FinalizeError(
                    f"{slack_where} must contain endpoint and slack_ns exactly"
                )
            selected_slacks.append(
                {
                    "endpoint": _diagnostic_token(
                        slack_item.get("endpoint"), where=f"{slack_where}.endpoint"
                    ),
                    "slack_ns": _finite_json_number(
                        slack_item.get("slack_ns"), f"{slack_where}.slack_ns"
                    ),
                }
            )
        slack_endpoints = [entry["endpoint"] for entry in selected_slacks]
        if slack_endpoints != sorted(set(slack_endpoints)):
            raise FinalizeError(
                f"{where}.selected_endpoint_slacks must be sorted and endpoint-unique"
            )
        if slack_endpoints != selected:
            raise FinalizeError(
                f"{where}: selected endpoint slack coverage does not match selected_endpoints"
            )
        negative_from_exact = [
            entry["endpoint"]
            for entry in selected_slacks
            if float(entry["slack_ns"]) < 0.0
        ]
        if negative_from_exact != violating:
            raise FinalizeError(
                f"{where}: exact selected endpoint slacks disagree with violating_endpoints"
            )
        if counts["selected_endpoint_count"] != len(selected):
            raise FinalizeError(f"{where}: selected endpoint count does not match its array")
        if counts["violating_endpoint_count"] != len(violating):
            raise FinalizeError(f"{where}: violating endpoint count does not match its array")
        expected_count = counts["expected_endpoint_count"]
        if required:
            if expected_count < 1 or selected != violating or len(selected) != expected_count:
                raise FinalizeError(
                    f"{where}: required selected/violating endpoint coverage is incomplete"
                )
            exact_values = [float(entry["slack_ns"]) for entry in selected_slacks]
            if abs(min(exact_values) - wns) > LOCALITY_INTERNAL_TOLERANCE_NS:
                raise FinalizeError(
                    f"{where}: exact selected endpoint slacks do not reproduce WNS"
                )
            if abs(sum(exact_values) - tns) > LOCALITY_INTERNAL_TOLERANCE_NS:
                raise FinalizeError(
                    f"{where}: exact selected endpoint slacks do not reproduce TNS"
                )
        elif expected_count != 0 or selected or violating:
            raise FinalizeError(f"{where}: non-required timing direction is not clean")

        normalized[check] = {
            "required": required,
            "wns_ns": wns,
            "tns_ns": tns,
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
    """Bind prompt objects and slacks to complete pre-fix violation evidence."""

    for check, timing in (("setup", "late"), ("hold", "early")):
        targets = [target for target in context["targets"] if target["timing"] == timing]
        endpoints = sorted(str(target["endpoint"]) for target in targets)
        if endpoints != locality[check]["violating_endpoints"]:
            raise FinalizeError(
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
            ) > LOCALITY_INTERNAL_TOLERANCE_NS:
                raise FinalizeError(
                    f"{where}: diagnostic {check} slack differs from exact endpoint evidence"
                )
        for field in ("wns_ns", "tns_ns"):
            tolerance = REPLAY_TIMING_TOLERANCE_NS
            if field == "tns_ns":
                tolerance = max(
                    tolerance,
                    0.000501 * int(before[check]["reported_endpoints"]),
                )
            if abs(float(locality[check][field]) - float(before[check][field])) > (
                tolerance + NUMERIC_EPSILON
            ):
                raise FinalizeError(
                    f"{where}: violation-locality {check} {field} does not match "
                    "before-ECO report"
                )
        if not locality[check]["required"]:
            continue
        slacks = [float(target["slack_ns"]) for target in targets]
        reported_wns = float(before[check]["wns_ns"])
        reported_tns = float(before[check]["tns_ns"])
        if abs(min(slacks) - reported_wns) > (
            REPLAY_TIMING_TOLERANCE_NS + NUMERIC_EPSILON
        ):
            raise FinalizeError(
                f"{where}: diagnostic {check} slack does not reproduce before-ECO WNS"
            )
        tns_tolerance = max(
            REPLAY_TIMING_TOLERANCE_NS,
            0.000501 * len(slacks),
        )
        if abs(sum(slacks) - reported_tns) > tns_tolerance + NUMERIC_EPSILON:
            raise FinalizeError(
                f"{where}: diagnostic {check} slacks do not reproduce before-ECO TNS"
            )


def _finite_json_number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FinalizeError(f"{where} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise FinalizeError(f"{where} must be finite")
    return result


def load_injection_provenance(
    path: Path,
    *,
    case_id: str,
    case_type: str,
    before: Mapping[str, Any],
    expected_targets: Mapping[str, Any],
    expected_strategy: str,
    baseline_provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate hidden injection calibration without exposing it to messages."""

    raw = _read_json(path, "injection provenance")
    if not isinstance(raw, Mapping):
        raise FinalizeError(f"{path}: injection provenance must be an object")
    if raw.get("schema_version") != INJECTION_PROVENANCE_SCHEMA:
        raise FinalizeError(f"{path}: missing or unsupported injection-provenance schema")
    if raw.get("case_id") != case_id or raw.get("status") != "passed":
        raise FinalizeError(f"{path}: injection provenance is not a passing record for {case_id}")
    if raw.get("strategy") != expected_strategy:
        raise FinalizeError(f"{path}: injection strategy differs from the prepared catalog")
    if raw.get("calibration_status") != "FROZEN":
        raise FinalizeError(f"{path}: Gold replay calibration_status must be FROZEN")
    binding = raw.get("baseline_binding")
    if not isinstance(binding, Mapping):
        raise FinalizeError(f"{path}: baseline_binding is missing")
    expected_binding = {
        key: baseline_provenance[key]
        for key in (
            "baseline_sha256",
            "catalog_sha256",
            "qualification_sha256",
            "checksum_manifest_sha256",
            "qualification_status",
            "technology_classification",
        )
    }
    if dict(binding) != expected_binding:
        raise FinalizeError(f"{path}: baseline_binding differs from the prepared manifest")
    targets = raw.get("target_wns_ns")
    if not isinstance(targets, Mapping):
        raise FinalizeError(f"{path}: target_wns_ns must be an object")
    normalized_targets: dict[str, list[float]] = {}
    for check in ("setup", "hold"):
        interval = targets.get(check)
        if not isinstance(interval, list) or len(interval) != 2:
            raise FinalizeError(f"{path}: target_wns_ns.{check} must be [low, high]")
        low = _finite_json_number(interval[0], f"{path}: {check} target low")
        high = _finite_json_number(interval[1], f"{path}: {check} target high")
        if low > high:
            raise FinalizeError(f"{path}: target_wns_ns.{check} is reversed")
        normalized_targets[check] = [low, high]
    attempts = raw.get("attempts")
    if not isinstance(attempts, list) or len(attempts) != 1:
        raise FinalizeError(
            f"{path}: Gold replay requires exactly one frozen injection attempt"
        )
    trial = attempts[0]
    if not isinstance(trial, Mapping) or trial.get("attempt") != 1:
        raise FinalizeError(f"{path}: frozen injection trial must be attempt 1")
    if not isinstance(trial.get("parameter"), str) or not trial["parameter"]:
        raise FinalizeError(f"{path}: injection trial parameter is missing")
    required = ("setup", "hold") if case_type == "mixed" else (case_type,)
    for check in required:
        expected = expected_targets.get(check)
        if not isinstance(expected, list) or len(expected) != 2:
            raise FinalizeError(f"catalog target interval is missing for {check}")
        expected_interval = [float(expected[0]), float(expected[1])]
        if normalized_targets[check] != expected_interval:
            raise FinalizeError(
                f"{path}: {check} target interval does not match the prepared catalog"
            )
    for check in ("setup", "hold"):
        measured_wns = _finite_json_number(
            trial.get(f"{check}_wns_ns"), f"{path}: {check}_wns_ns"
        )
        measured_tns = _finite_json_number(
            trial.get(f"{check}_tns_ns"), f"{path}: {check}_tns_ns"
        )
        if abs(measured_wns - float(before[check]["wns_ns"])) > (
            REPLAY_TIMING_TOLERANCE_NS + NUMERIC_EPSILON
        ):
            raise FinalizeError(
                f"{path}: {check} injection WNS does not match before-ECO report"
            )
        # Text reports normally print three decimals.  TNS sums endpoint
        # slacks, so its rounding envelope grows with the reported endpoint
        # count even when the collection-derived provenance is exact.
        tns_tolerance = max(
            REPLAY_TIMING_TOLERANCE_NS,
            0.000501 * int(before[check]["reported_endpoints"]),
        )
        if abs(measured_tns - float(before[check]["tns_ns"])) > (
            tns_tolerance + NUMERIC_EPSILON
        ):
            raise FinalizeError(
                f"{path}: {check} injection TNS does not match before-ECO report"
            )
        if check in required:
            low, high = normalized_targets[check]
            if measured_wns < low - NUMERIC_EPSILON or measured_wns > high + NUMERIC_EPSILON:
                raise FinalizeError(
                    f"{path}: measured {check} WNS is outside its target interval"
                )
    return dict(raw)


def _validate_concrete_fix(
    path: Path, *, repair_mode: str, case_type: str, case_id: str
) -> str:
    text = _read_text(path, "concrete Gold fix")
    if "\r" in text or not text.endswith("\n") or text.startswith("\n"):
        raise FinalizeError(f"{path}: concrete fix must use LF and one nonblank first line")
    if "```" in text:
        raise FinalizeError(f"{path}: concrete fix must not contain Markdown fences")
    if re.search(r"(?i)::sft::apply_repair|source\s+.*fix\.tcl", text):
        raise FinalizeError(f"{path}: fix is an abstract wrapper, not a concrete ECO")
    for pattern in FORBIDDEN_FIX_COMMANDS:
        if pattern.search(text):
            raise FinalizeError(f"{path}: concrete fix changes or relaxes timing constraints")
    allowed_top_level_commands = {
        "set",
        "if",
        "setEcoMode",
        "ecoChangeCell",
        "ecoAddRepeater",
        "optDesign",
        "refinePlace",
        "setNanoRouteMode",
        "ecoRoute",
    }
    top_level_commands: list[str] = []
    for line_number, line_with_ending in enumerate(text.splitlines(keepends=True), 1):
        raw_line = line_with_ending.rstrip("\r\n")
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        command = re.match(r"([A-Za-z_][A-Za-z0-9_:]*)\b", stripped)
        if command is None or command.group(1) not in allowed_top_level_commands:
            name = command.group(1) if command is not None else stripped
            raise FinalizeError(
                f"{path}:{line_number}: unsupported top-level concrete-fix "
                f"command {name!r}"
            )
        if command.group(1) == "set" and re.fullmatch(
            r"set\s+[A-Za-z_][A-Za-z0-9_]*\s+"
            r"\[get_(?:pins|ports|cells)\s+\{[^{}\r\n]+\}\]",
            stripped,
        ) is None:
            raise FinalizeError(
                f"{path}:{line_number}: unsupported concrete-fix target resolver"
            )
        if command.group(1) == "if" and re.fullmatch(
            r"if\s+\{\[sizeof_collection\s+\$[A-Za-z_][A-Za-z0-9_]*\]"
            r"\s*!=\s*1\}\s+\{\s*error\s+\"[^\"\r\n]*\"\s*\}",
            stripped,
        ) is None:
            raise FinalizeError(
                f"{path}:{line_number}: unsupported concrete-fix target guard"
            )
        if ";" in stripped or stripped.endswith("\\"):
            raise FinalizeError(
                f"{path}:{line_number}: concrete-fix commands must be one per line"
            )
        assert command is not None
        top_level_commands.append(command.group(1))
    action_matches = list(
        re.finditer(r"(?mi)^\s*(ecoChangeCell|ecoAddRepeater|optDesign)\b", text)
    )
    actions = [match.group(1) for match in action_matches]
    action_lines = [
        match.group(0).strip()
        for match in re.finditer(
            r"(?mi)^\s*(?:ecoChangeCell|ecoAddRepeater|optDesign)\b[^\r\n]*$",
            text,
        )
    ]
    if not actions:
        raise FinalizeError(f"{path}: concrete fix contains no recognized ECO action")
    seteco_matches = list(
        re.finditer(r"(?mi)^\s*setEcoMode\b[^\r\n]*$", text)
    )
    batch_matches = list(
        re.finditer(
            r"(?mi)^\s*setEcoMode\s+-batchMode\s+(true|false)\s*$", text
        )
    )
    leq_matches = list(
        re.finditer(
            r"(?mi)^\s*setEcoMode\s+-LEQCheck\s+(true|false)\s*$", text
        )
    )
    setnano_matches = list(
        re.finditer(r"(?mi)^\s*setNanoRouteMode\b[^\r\n]*$", text)
    )
    route_timing_matches = list(
        re.finditer(
            r"(?mi)^\s*setNanoRouteMode\s+-routeWithTimingDriven\s+"
            r"(true|false)\s*$",
            text,
        )
    )
    batch_commands = [match.group(1) for match in batch_matches]
    leq_commands = [match.group(1) for match in leq_matches]
    route_timing_commands = [match.group(1) for match in route_timing_matches]
    recognized_setnano_positions = {match.start() for match in route_timing_matches}
    if any(
        match.start() not in recognized_setnano_positions
        for match in setnano_matches
    ):
        raise FinalizeError(
            f"{path}: concrete fix contains unsupported setNanoRouteMode command"
        )
    if repair_mode == "native":
        if actions != ["optDesign"]:
            raise FinalizeError(f"{path}: native fix must contain exactly one optDesign action")
        if seteco_matches:
            raise FinalizeError(f"{path}: native optDesign must execute outside ECO batch mode")
        if setnano_matches:
            raise FinalizeError(
                f"{path}: native optDesign must not change NanoRoute timing-driven mode"
            )
        match = re.search(r"(?mi)^\s*optDesign\b([^\r\n]*)$", text)
        assert match is not None
        args = match.group(1)
        terms_file = _tcl_literal_option(args, "-selectedTerms", where=str(path))
        if terms_file != NATIVE_SELECTED_TERMS_RELATIVE:
            raise FinalizeError(
                f"{path}: native optDesign -selectedTerms must name "
                f"{NATIVE_SELECTED_TERMS_RELATIVE}"
            )
        expected_mode = "-setup" if case_type == "setup" else "-hold"
        for option in ("-postRoute", expected_mode, "-incr"):
            if re.search(rf"(?:^|\s){re.escape(option)}(?:\s|$)", args) is None:
                raise FinalizeError(f"{path}: native optDesign is missing {option}")
        opposite_mode = "-hold" if expected_mode == "-setup" else "-setup"
        if re.search(rf"(?:^|\s){re.escape(opposite_mode)}(?:\s|$)", args):
            raise FinalizeError(
                f"{path}: native optDesign must not include {opposite_mode}"
            )
    else:
        if "optDesign" in actions:
            raise FinalizeError(f"{path}: surgical fix must not invoke optDesign")
        recognized_seteco_positions = {
            match.start() for match in (*batch_matches, *leq_matches)
        }
        if any(
            match.start() not in recognized_seteco_positions
            for match in seteco_matches
        ):
            raise FinalizeError(f"{path}: surgical fix contains unsupported setEcoMode command")
        if batch_commands != ["true", "false"]:
            raise FinalizeError(
                f"{path}: surgical fix must have one setEcoMode true/false batch boundary"
            )
        enter = batch_matches[0].start()
        exit_batch = batch_matches[1].start()
        first_action = min(match.start() for match in action_matches)
        last_action = max(match.start() for match in action_matches)
        if case_id == "SETUP_004":
            if actions != ["ecoChangeCell"] * len(actions):
                raise FinalizeError(
                    f"{path}: SETUP_004 paired-inverter fix must use only ecoChangeCell actions"
                )
            if leq_commands != ["false", "true"]:
                raise FinalizeError(
                    f"{path}: SETUP_004 paired-inverter fix must have one "
                    "setEcoMode -LEQCheck false/true window"
                )
            disable_leq = leq_matches[0].start()
            restore_leq = leq_matches[1].start()
            if not (
                enter
                < disable_leq
                < first_action
                <= last_action
                < exit_batch
                < restore_leq
            ):
                raise FinalizeError(
                    f"{path}: SETUP_004 paired-inverter LEQ/batch ordering is invalid"
                )
            if route_timing_commands != ["false", "true"]:
                raise FinalizeError(
                    f"{path}: SETUP_004 paired-inverter closeout must have one "
                    "setNanoRouteMode -routeWithTimingDriven false/true window"
                )
            disable_route_timing = route_timing_matches[0].start()
            restore_route_timing = route_timing_matches[1].start()
        elif case_id == "HOLD_002":
            if case_type != "hold":
                raise FinalizeError(
                    f"{path}: HOLD_002 fixed repeater location fingerprint "
                    "requires a hold case"
                )
            if actions != ["ecoAddRepeater", "ecoChangeCell"]:
                raise FinalizeError(
                    f"{path}: HOLD_002 fingerprint-bound fix must contain "
                    "one ecoAddRepeater followed by one ecoChangeCell"
                )
            if leq_matches:
                raise FinalizeError(
                    f"{path}: setEcoMode -LEQCheck is allowed only for SETUP_004"
                )
            if setnano_matches:
                raise FinalizeError(
                    f"{path}: HOLD_002 fixed-location repair must retain the "
                    "ordinary timing-driven router mode"
                )
            exact_repeater = re.findall(
                r"(?m)^[ \t]*ecoAddRepeater[ \t]+"
                r"-term[ \t]+mac_out_nan_reg/D[ \t]+"
                r"-cell[ \t]+DLY4_X0P5M_A9TR40[ \t]+"
                r"-name[ \t]+SFT_ECO_HOLD_002_HOLD_1[ \t]+"
                r"-loc[ \t]+\{898\.00[ \t]+248\.08\}[ \t]*$",
                text,
            )
            if len(exact_repeater) != 1:
                raise FinalizeError(
                    f"{path}: HOLD_002 ecoAddRepeater must use the exact "
                    "probe-proven term, cell, name, and -loc {898.00 248.08}"
                )
            exact_change = re.findall(
                r"(?m)^[ \t]*ecoChangeCell[ \t]+"
                r"-inst[ \t]+FE_OFC8281_pp_nan_pvld_d2_0[ \t]+"
                r"-cell[ \t]+BUF_X0P8B_A9TR40[ \t]*$",
                text,
            )
            if len(exact_change) != 1:
                raise FinalizeError(
                    f"{path}: HOLD_002 ecoChangeCell must use its exact "
                    "probe-proven instance and replacement cell"
                )
            if not enter < first_action <= last_action < exit_batch:
                raise FinalizeError(
                    f"{path}: HOLD_002 surgical ECO actions are outside the "
                    "batch boundary"
                )
        elif case_id == "HOLD_003":
            if case_type != "hold":
                raise FinalizeError(
                    f"{path}: HOLD_003 fixed repeater location fingerprint "
                    "requires a hold case"
                )
            expected_actions = [
                "ecoAddRepeater -term pp_nan_mts_d2_reg_8_/D "
                "-cell DLY4_X0P5M_A9TR40 -name SFT_ECO_HOLD_003_HOLD_1 "
                "-loc {914.15 263.20}",
                "ecoAddRepeater -term pp_nan_mts_d2_reg_8_/D "
                "-cell DLY4_X0P5M_A9TR40 -name SFT_ECO_HOLD_003_HOLD_2 "
                "-loc {915.86 263.20}",
            ]
            if action_lines != expected_actions:
                raise FinalizeError(
                    f"{path}: HOLD_003 repair must use the exact two "
                    "probe-proven repeaters and fixed locations"
                )
            if leq_matches:
                raise FinalizeError(
                    f"{path}: setEcoMode -LEQCheck is allowed only for SETUP_004"
                )
            if setnano_matches:
                raise FinalizeError(
                    f"{path}: HOLD_003 fixed-location repair must retain the "
                    "ordinary timing-driven router mode"
                )
            if not enter < first_action <= last_action < exit_batch:
                raise FinalizeError(
                    f"{path}: HOLD_003 surgical ECO actions are outside the "
                    "batch boundary"
                )
        elif case_id == "MIXED_001":
            if case_type != "mixed":
                raise FinalizeError(
                    f"{path}: MIXED_001 fingerprint-bound repair requires a mixed case"
                )
            expected_actions = [
                f"ecoChangeCell -inst u_exp/SFT_ECO_MIXED_001_PATH_{ordinal} "
                "-cell BUF_X2M_A9TR40"
                for ordinal in range(1, 5)
            ]
            expected_actions.append(
                "ecoAddRepeater -term pp_exp_d2_reg_2_/D "
                "-cell DLY4_X0P5M_A9TR40 -name SFT_ECO_MIXED_001_HOLD_1"
            )
            if action_lines != expected_actions:
                raise FinalizeError(
                    f"{path}: MIXED_001 repair must use its exact four BUF_X2M "
                    "restores followed by its exact hold repeater"
                )
            if leq_matches:
                raise FinalizeError(
                    f"{path}: setEcoMode -LEQCheck is allowed only for SETUP_004"
                )
            if setnano_matches:
                raise FinalizeError(
                    f"{path}: setNanoRouteMode is allowed only for SETUP_004"
                )
            if not enter < first_action <= last_action < exit_batch:
                raise FinalizeError(
                    f"{path}: MIXED_001 surgical ECO actions are outside the "
                    "batch boundary"
                )
        else:
            if leq_matches:
                raise FinalizeError(
                    f"{path}: setEcoMode -LEQCheck is allowed only for SETUP_004"
                )
            if setnano_matches:
                raise FinalizeError(
                    f"{path}: setNanoRouteMode is allowed only for SETUP_004"
                )
            if not enter < first_action <= last_action < exit_batch:
                raise FinalizeError(
                    f"{path}: surgical ECO actions are outside the batch boundary"
                )
    refine_commands = list(
        re.finditer(r"(?m)^[ \t]*refinePlace\b[^\r\n]*$", text)
    )
    exact_refine_commands = list(
        re.finditer(r"(?m)^[ \t]*refinePlace[ \t]+-eco[ \t]+true[ \t]*$", text)
    )
    if len(refine_commands) != 1:
        raise FinalizeError(
            f"{path}: concrete fix must contain exactly one refinePlace -eco true command"
        )
    if len(exact_refine_commands) != 1:
        raise FinalizeError(f"{path}: repair closeout must be exactly refinePlace -eco true")

    ecoroute_commands = list(
        re.finditer(r"(?m)^[ \t]*ecoRoute\b[^\r\n]*$", text)
    )
    target_route_commands = list(
        re.finditer(r"(?m)^[ \t]*ecoRoute[ \t]+-target[ \t]*$", text)
    )
    fix_drc_route_commands = list(
        re.finditer(
            r"(?m)^[ \t]*ecoRoute[ \t]+-fix_drc[ \t]+"
            r"\{907\.30[ \t]+263\.40[ \t]+914\.90[ \t]+270\.50\}[ \t]*$",
            text,
        )
    )
    paired_setup_004 = repair_mode == "surgical" and case_id == "SETUP_004"
    if paired_setup_004:
        if len(ecoroute_commands) != 2:
            raise FinalizeError(
                f"{path}: SETUP_004 paired-inverter closeout must contain exactly "
                "two ecoRoute commands"
            )
        if len(target_route_commands) != 1:
            raise FinalizeError(
                f"{path}: SETUP_004 paired-inverter closeout must contain exactly "
                "one ecoRoute -target command"
            )
        if len(fix_drc_route_commands) != 1:
            raise FinalizeError(
                f"{path}: SETUP_004 paired-inverter DRC closeout must be exactly "
                "ecoRoute -fix_drc {907.30 263.40 914.90 270.50}"
            )
    else:
        if len(ecoroute_commands) != 1:
            raise FinalizeError(
                f"{path}: concrete fix must contain exactly one ecoRoute -target command"
            )
        if len(target_route_commands) != 1:
            raise FinalizeError(f"{path}: repair closeout must be exactly ecoRoute -target")

    refine_position = exact_refine_commands[0].start()
    target_route_position = target_route_commands[0].start()
    action_end = max(
        match.start()
        for match in re.finditer(r"(?mi)^\s*(?:ecoChangeCell|ecoAddRepeater|optDesign)\b", text)
    )
    if not action_end < refine_position < target_route_position:
        raise FinalizeError(f"{path}: physical closeout ordering is invalid")
    if repair_mode == "surgical":
        if case_id == "SETUP_004":
            fix_drc_route_position = fix_drc_route_commands[0].start()
            if not (
                restore_leq
                < refine_position
                < target_route_position
                < disable_route_timing
                < fix_drc_route_position
                < restore_route_timing
            ):
                raise FinalizeError(
                    f"{path}: SETUP_004 paired-inverter physical closeout ordering is invalid"
                )
            if top_level_commands[-5:] != [
                "refinePlace",
                "ecoRoute",
                "setNanoRouteMode",
                "ecoRoute",
                "setNanoRouteMode",
            ]:
                raise FinalizeError(
                    f"{path}: SETUP_004 paired-inverter physical closeout commands "
                    "must be adjacent and final"
                )
        elif case_id == "HOLD_002":
            if not exit_batch < refine_position:
                raise FinalizeError(
                    f"{path}: HOLD_002 physical closeout starts before batch exit"
                )
            if top_level_commands[-2:] != ["refinePlace", "ecoRoute"]:
                raise FinalizeError(
                    f"{path}: HOLD_002 fixed-location physical closeout "
                    "commands must be adjacent and final"
                )
        elif not exit_batch < refine_position:
            raise FinalizeError(f"{path}: surgical physical closeout starts before batch exit")
    return text


def _tcl_literal_option(args: str, option: str, *, where: str) -> str:
    match = re.search(
        rf"(?:^|\s){re.escape(option)}\s+(?:\{{([^{{}}\r\n]+)\}}|([^\s;]+))",
        args,
    )
    if match is None:
        raise FinalizeError(f"{where}: missing literal {option} object")
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
    """Bind existing and newly named ECO instances to visible task evidence."""

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
        raise FinalizeError(
            f"{case_id}: concrete fix references instances absent from diagnostic "
            f"local_cells: {', '.join(hidden)}"
        )

    add_commands = list(
        re.finditer(r"(?mi)^\s*ecoAddRepeater\b([^\r\n]*)$", fix)
    )
    if len(change_commands) + len(add_commands) > max_eco_cells:
        raise FinalizeError(
            f"{case_id}: concrete physical ECO actions exceed max_eco_cells={max_eco_cells}"
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
            raise FinalizeError(
                f"{case_id}: ecoAddRepeater name {name!r} violates the declared convention"
            )
        if int(parsed.group(2)) != command_number:
            raise FinalizeError(
                f"{case_id}: ecoAddRepeater ordinals must be continuous from 1"
            )
        new_instances.append(name)
    if len(new_instances) != len(set(new_instances)):
        raise FinalizeError(f"{case_id}: ecoAddRepeater names must be unique")

    allowed_names = visible_instances | set(new_instances)
    mentioned_names = set(ECO_INSTANCE_NAME_RE.findall(fix))
    undeclared = sorted(mentioned_names - allowed_names)
    if undeclared:
        raise FinalizeError(
            f"{case_id}: concrete fix contains undeclared ECO instance names: "
            f"{', '.join(undeclared)}"
        )
    return allowed_names


def _validate_answer_eco_names(answer: str, allowed_names: set[str], *, case_id: str) -> None:
    mentioned = set(ECO_INSTANCE_NAME_RE.findall(answer))
    undeclared = sorted(mentioned - allowed_names)
    if undeclared:
        raise FinalizeError(
            f"{case_id}: answer contains undeclared ECO instance names: "
            f"{', '.join(undeclared)}"
        )


def _load_manifest(case_dir: Path) -> dict[str, Any]:
    path = case_dir / "manifest.json"
    raw = _read_json(path, "prepared case manifest")
    if not isinstance(raw, Mapping):
        raise FinalizeError(f"{path}: manifest must be an object")
    required_strings = (
        "id",
        "type",
        "difficulty",
        "design",
        "tool_version",
        "repair_mode",
    )
    for key in required_strings:
        if not isinstance(raw.get(key), str) or not str(raw[key]).strip():
            raise FinalizeError(f"{path}: {key} must be a non-empty string")
    if raw["id"] != case_dir.name:
        raise FinalizeError(f"{path}: ID does not match directory {case_dir.name}")
    if raw["type"] not in {"setup", "hold", "mixed"}:
        raise FinalizeError(f"{path}: type must be setup, hold, or mixed")
    if raw["difficulty"] not in {"easy", "medium", "hard"}:
        raise FinalizeError(f"{path}: invalid difficulty")
    if raw["repair_mode"] not in {"surgical", "native"}:
        raise FinalizeError(f"{path}: repair_mode must be surgical or native")
    if raw.get("status") != "GOLD_PREPARED" or raw.get("gold_eligible") is not True:
        raise FinalizeError(f"{path}: finalizer accepts only GOLD_PREPARED cases")
    views = raw.get("analysis_views")
    if not isinstance(views, Mapping):
        raise FinalizeError(f"{path}: analysis_views must be an object")
    if any(not isinstance(views.get(mode), str) or not views[mode] for mode in ("setup", "hold")):
        raise FinalizeError(f"{path}: setup and hold analysis views are required")
    catalog = raw.get("catalog_entry")
    if not isinstance(catalog, Mapping):
        raise FinalizeError(f"{path}: catalog_entry is required for Gold calibration checks")
    for key in ("id", "type", "difficulty", "repair_mode"):
        if catalog.get(key) != raw[key]:
            raise FinalizeError(f"{path}: catalog_entry.{key} does not match manifest.{key}")
    target_wns = catalog.get("target_wns_ns")
    repair = catalog.get("repair")
    if not isinstance(target_wns, Mapping) or not isinstance(repair, Mapping):
        raise FinalizeError(f"{path}: catalog target_wns_ns and repair objects are required")
    max_eco_cells = repair.get("max_eco_cells")
    if isinstance(max_eco_cells, bool) or not isinstance(max_eco_cells, int) or max_eco_cells < 1:
        raise FinalizeError(f"{path}: catalog repair.max_eco_cells must be positive")
    injection = catalog.get("injection")
    if not isinstance(injection, Mapping):
        raise FinalizeError(f"{path}: catalog injection object is required")
    if injection.get("calibration_status") != "FROZEN":
        raise FinalizeError(f"{path}: Gold catalog injection must be FROZEN")
    if not isinstance(injection.get("strategy"), str) or not injection["strategy"]:
        raise FinalizeError(f"{path}: Gold catalog injection strategy is missing")
    _manifest_baseline_provenance(raw)
    return dict(raw)


def _verify_run_status(
    replay_dir: Path,
    case_id: str,
    expected_version: str,
    *,
    replay_index: int,
    baseline_provenance: Mapping[str, Any],
) -> dict[str, Any]:
    status_path = _required_file(replay_dir, "run_status.json", "run status")
    status = _read_json(status_path, "run status")
    if not isinstance(status, Mapping):
        raise FinalizeError(f"{status_path}: run status must be an object")
    if status.get("passed") is not True or status.get("innovus_exit_code") != 0:
        raise FinalizeError(f"{status_path}: Innovus replay did not exit successfully")
    expected = {
        "replay": replay_index,
        "fetched": True,
        "expected_marker": "SFT_CASE_PASSED",
        "marker_present": True,
        "baseline_sha256": baseline_provenance["baseline_sha256"],
        "catalog_sha256": baseline_provenance["catalog_sha256"],
        "baseline_qualification_sha256": baseline_provenance["qualification_sha256"],
        "baseline_checksum_manifest_sha256": baseline_provenance[
            "checksum_manifest_sha256"
        ],
        "baseline_status": baseline_provenance["qualification_status"],
        "guest_baseline_checksums_verified": True,
        "mode": "GOLD_REPLAY",
        "gold_eligible": True,
    }
    for key, value in expected.items():
        if status.get(key) != value:
            raise FinalizeError(f"{status_path}: run status {key} is not {value!r}")
    marker_declared = replay_dir / REPLAY_EVIDENCE_FILES["success_marker"]
    if marker_declared.is_symlink():
        raise FinalizeError(f"{marker_declared}: success marker must not be a symlink")
    marker_path = _required_file(
        replay_dir, REPLAY_EVIDENCE_FILES["success_marker"], "success marker"
    )
    expected_marker_bytes = f"{case_id} replay completed\n".encode("utf-8")
    if marker_path.read_bytes() != expected_marker_bytes:
        raise FinalizeError(
            f"{marker_path}: success marker bytes do not exactly name {case_id}"
        )
    log_path = _required_file(replay_dir, "logs/innovus.log", "Innovus log")
    log = _read_text(log_path, "Innovus log")
    version = re.search(r"(?mi)^Version:\s*v?([^,\s]+)", log)
    if version is None or _normal_tool_version(version.group(1)) != _normal_tool_version(
        expected_version
    ):
        actual = version.group(1) if version is not None else "<missing>"
        raise FinalizeError(
            f"{log_path}: Innovus version {actual!r} does not match {expected_version!r}"
        )
    host_log_path = _required_file(
        replay_dir, REPLAY_EVIDENCE_FILES["host_ssh_log"], "host SSH log"
    )
    host_log = _read_text(host_log_path, "host SSH log")
    typed_markers = [
        line
        for line in host_log.splitlines()
        if re.match(r"^SFT_CASE_PASSED(?:\s|$)", line)
    ]
    expected_host_marker = f"SFT_CASE_PASSED {case_id}"
    if typed_markers != [expected_host_marker]:
        raise FinalizeError(
            f"{host_log_path}: expected exactly one typed marker "
            f"{expected_host_marker!r}"
        )
    return dict(status)


def _stage_metrics(replay_dir: Path, manifest: Mapping[str, Any], stage: str) -> dict[str, Any]:
    design = str(manifest["design"])
    version = str(manifest["tool_version"])
    views = manifest["analysis_views"]
    setup = parse_timing_report(
        _required_file(replay_dir, f"reports/setup_{stage}.rpt", f"setup {stage} report"),
        check="setup",
        expected_view=str(views["setup"]),
        expected_design=design,
        expected_tool_version=version,
    )
    hold = parse_timing_report(
        _required_file(replay_dir, f"reports/hold_{stage}.rpt", f"hold {stage} report"),
        check="hold",
        expected_view=str(views["hold"]),
        expected_design=design,
        expected_tool_version=version,
    )
    drv = parse_drv_report(
        _required_file(replay_dir, f"reports/drv_{stage}.rpt", f"DRV {stage} report"),
        expected_design=design,
        expected_tool_version=version,
    )
    connectivity = parse_connectivity_report(
        _required_file(
            replay_dir,
            f"reports/connectivity_{stage}.rpt",
            f"connectivity {stage} report",
        ),
        expected_design=design,
        expected_tool_version=version,
    )
    drc = parse_drc_report(
        _required_file(replay_dir, f"reports/drc_{stage}.rpt", f"DRC {stage} report"),
        expected_design=design,
        expected_tool_version=version,
    )
    return {"setup": setup, "hold": hold, "drv": drv, "drc": drc, "connectivity": connectivity}


def _manifest_baseline_provenance(manifest: Mapping[str, Any]) -> dict[str, Any]:
    def digest(key: str) -> str:
        value = manifest.get(key)
        if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
            raise FinalizeError(f"prepared manifest {key} is not a lowercase SHA256")
        return value

    qualification = manifest.get("baseline_qualification")
    if not isinstance(qualification, Mapping):
        raise FinalizeError("prepared manifest baseline_qualification must be an object")
    expected = {
        "schema_version": BASELINE_QUALIFICATION_SCHEMA,
        "status": BASELINE_QUALIFICATION_STATUS,
        "gold_status": False,
        "signoff_eligible": False,
        "technology_classification": BASELINE_TECHNOLOGY_CLASSIFICATION,
    }
    for key, value in expected.items():
        if qualification.get(key) != value:
            raise FinalizeError(
                f"prepared manifest baseline_qualification.{key} is not {value!r}"
            )
    qualification_sha = qualification.get("sha256")
    if not isinstance(qualification_sha, str) or SHA256_RE.fullmatch(qualification_sha) is None:
        raise FinalizeError("prepared manifest baseline qualification SHA256 is invalid")
    sources = qualification.get("source_artifacts")
    if not isinstance(sources, Mapping):
        raise FinalizeError(
            "prepared manifest baseline qualification source_artifacts must be an object"
        )
    normalized_sources: dict[str, dict[str, Any]] = {}
    for role in QUALIFIED_LIBERTY_ROLES.values():
        item = sources.get(role)
        if not isinstance(item, Mapping):
            raise FinalizeError(f"prepared manifest is missing qualified {role}")
        source_digest = item.get("sha256")
        source_bytes = item.get("bytes")
        source_path = item.get("source_path")
        if not isinstance(source_digest, str) or SHA256_RE.fullmatch(source_digest) is None:
            raise FinalizeError(f"prepared manifest qualified {role} SHA256 is invalid")
        if isinstance(source_bytes, bool) or not isinstance(source_bytes, int) or source_bytes <= 0:
            raise FinalizeError(f"prepared manifest qualified {role} byte count is invalid")
        if not isinstance(source_path, str) or not source_path.startswith("/"):
            raise FinalizeError(f"prepared manifest qualified {role} source path is invalid")
        normalized_sources[role] = {
            "sha256": source_digest,
            "bytes": source_bytes,
            "source_path": source_path,
        }
    return {
        "baseline_sha256": digest("baseline_sha256"),
        "catalog_sha256": digest("catalog_sha256"),
        "checksum_manifest_sha256": digest("baseline_checksum_manifest_sha256"),
        "qualification_sha256": qualification_sha,
        "qualification_status": BASELINE_QUALIFICATION_STATUS,
        "technology_classification": BASELINE_TECHNOLOGY_CLASSIFICATION,
        "gold_status": False,
        "signoff_eligible": False,
        "source_artifacts": normalized_sources,
    }


def _validate_prepared_qualification(
    path: Path, *, manifest: Mapping[str, Any], provenance: Mapping[str, Any]
) -> dict[str, Any]:
    if _sha256(path) != provenance["qualification_sha256"]:
        raise FinalizeError(f"{path}: qualification SHA256 differs from prepared manifest")
    raw = _read_json(path, "prepared baseline qualification")
    if not isinstance(raw, Mapping):
        raise FinalizeError(f"{path}: baseline qualification must be an object")
    expected = {
        "schema_version": BASELINE_QUALIFICATION_SCHEMA,
        "status": BASELINE_QUALIFICATION_STATUS,
        "gold_status": False,
        "signoff_eligible": False,
        "technology_classification": BASELINE_TECHNOLOGY_CLASSIFICATION,
        "top": manifest["design"],
    }
    for key, value in expected.items():
        if raw.get(key) != value:
            raise FinalizeError(f"{path}: qualification {key} is not {value!r}")
    drc = raw.get("drc")
    if not isinstance(drc, Mapping) or drc.get("report_limit") != DRC_REPORT_LIMIT:
        raise FinalizeError(
            f"{path}: qualification DRC evidence is not bound to "
            f"report_limit={DRC_REPORT_LIMIT}"
        )
    sources = raw.get("source_artifacts")
    if not isinstance(sources, Mapping):
        raise FinalizeError(f"{path}: qualification source_artifacts are missing")
    for role, expected_item in provenance["source_artifacts"].items():
        item = sources.get(role)
        if not isinstance(item, Mapping) or {
            key: item.get(key) for key in ("sha256", "bytes", "source_path")
        } != expected_item:
            raise FinalizeError(
                f"{path}: qualification {role} differs from prepared manifest"
            )
    promotion = raw.get("promotion_bindings")
    if not isinstance(promotion, Mapping):
        raise FinalizeError(f"{path}: qualification promotion_bindings are missing")
    source_inputs = promotion.get("source_inputs")
    if not isinstance(source_inputs, Mapping):
        raise FinalizeError(f"{path}: qualification source_inputs binding is missing")
    for role, item in sources.items():
        if source_inputs.get(role) != item:
            raise FinalizeError(
                f"{path}: promotion source_inputs.{role} differs from source_artifacts"
            )
    return dict(raw)


def _load_baseline_guard(
    path: Path, *, case_id: str, provenance: Mapping[str, Any]
) -> dict[str, Any]:
    raw = _read_json(path, "baseline guard")
    if not isinstance(raw, Mapping) or raw.get("schema_version") != BASELINE_GUARD_SCHEMA:
        raise FinalizeError(f"{path}: unsupported baseline-guard schema")
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
            raise FinalizeError(f"{path}: baseline guard {key} is not bound to the manifest")
    minimum = _finite_json_number(raw.get("minimum_wns_ns"), f"{path}: minimum_wns_ns")
    if minimum < 0.020 - NUMERIC_EPSILON:
        raise FinalizeError(f"{path}: baseline guard minimum WNS is below 0.020 ns")
    for mode in ("setup", "hold"):
        item = raw.get(mode)
        if not isinstance(item, Mapping):
            raise FinalizeError(f"{path}: baseline guard {mode} result is missing")
        wns = _finite_json_number(item.get("wns_ns"), f"{path}: {mode}.wns_ns")
        tns = _finite_json_number(item.get("tns_ns"), f"{path}: {mode}.tns_ns")
        if wns < minimum - NUMERIC_EPSILON or abs(tns) > NUMERIC_EPSILON:
            raise FinalizeError(f"{path}: baseline guard {mode} timing is not clean")
    return dict(raw)


def _physical_drv(stage: Mapping[str, Any]) -> dict[str, int]:
    return {
        "max_transition": int(stage["drv"]["max_transition_violations"]),
        "max_capacitance": int(stage["drv"]["max_capacitance_violations"]),
        "max_fanout": int(stage["drv"]["max_fanout_violations"]),
    }


def _load_physical_no_regression(
    path: Path,
    *,
    case_id: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, Any]:
    raw = _read_json(path, "physical no-regression audit")
    if not isinstance(raw, Mapping) or raw.get("schema_version") != PHYSICAL_NO_REGRESSION_SCHEMA:
        raise FinalizeError(f"{path}: unsupported physical no-regression schema")
    if raw.get("case_id") != case_id or raw.get("passed") is not True:
        raise FinalizeError(f"{path}: physical no-regression audit is not passing for {case_id}")
    if raw.get("per_category_no_regression") is not True:
        raise FinalizeError(f"{path}: per-category DRC no-regression is not explicit")
    expected = {
        "drv_before": _physical_drv(before),
        "drv_after": _physical_drv(after),
        "drc_before": before["drc"],
        "drc_after": after["drc"],
    }
    for key, value in expected.items():
        if raw.get(key) != value:
            raise FinalizeError(f"{path}: {key} differs from parsed Innovus reports")
    return dict(raw)


def _validate_gate_netlist(path: Path, design: str, label: str) -> str:
    text = _read_text(path, label)
    if re.search(rf"(?mi)^\s*module\s+{re.escape(design)}(?:\s|\()", text) is None:
        raise FinalizeError(f"{path}: top module {design!r} was not found")
    if re.search(r"(?mi)^\s*endmodule\b", text) is None:
        raise FinalizeError(f"{path}: no complete Verilog module was found")
    return _normalized_verilog_sha256(path)


def _validate_spef(path: Path, design: str, label: str) -> None:
    text = _read_text(path, label)
    if re.search(r"(?mi)^\*SPEF\s+", text) is None:
        raise FinalizeError(f"{path}: missing *SPEF header")
    match = re.search(r'(?mi)^\*DESIGN\s+"?([^"\s]+)"?\s*$', text)
    if match is None or match.group(1) != design:
        raise FinalizeError(f"{path}: SPEF design does not match {design!r}")
    if re.search(r"(?mi)^\*D_NET\s+", text) is None:
        raise FinalizeError(f"{path}: SPEF contains no *D_NET parasitic record")


def _validate_checkpoint_wrapper(path: Path, data_dir: Path, design: str) -> None:
    text = _read_text(path, "violating checkpoint wrapper")
    if data_dir.name not in text:
        raise FinalizeError(f"{path}: checkpoint wrapper does not reference {data_dir.name}")
    if re.search(r"(?m)^\s*(?:read_db|restoreDesign)\b", text) is None:
        raise FinalizeError(f"{path}: checkpoint wrapper has no read_db/restoreDesign command")
    if "restoreDesign" in text and design not in text:
        raise FinalizeError(f"{path}: checkpoint wrapper does not name design {design}")


def _validate_check_design_tree(path: Path, *, design: str, version: str) -> Path:
    primary = _required_file(path, f"{design}.main.htm.ascii", "checkDesign main ASCII")
    text, _ = _innovus_report(
        primary, expected_design=design, expected_tool_version=version
    )
    command = _report_command(primary, text)
    if not re.search(r"(?i)(?:^|\s)checkDesign(?:\s|$)", command):
        raise FinalizeError(f"{primary}: Command is not checkDesign")
    for option in ("-all", "-outDir"):
        if re.search(rf"(?i)(?:^|\s){re.escape(option)}(?:\s|$)", command) is None:
            raise FinalizeError(f"{primary}: Command omits {option}")
    sections = (
        "Physical Library(LEF) Integrity Check",
        "Timing information check",
        "Primitive Net DRC Check",
        "Sub Module Port Definition Check",
        "Top level Floorplan Check",
    )
    for section in sections:
        if len(re.findall(rf"(?mi)^\s*{re.escape(section)}\s*$", text)) != 1:
            raise FinalizeError(f"{primary}: missing or duplicate {section!r} section")
    labels = (
        "Cells with missing LEF",
        "Cells with missing Timing data",
        "Nets with multiple drivers",
        "Verilog nets with multiple drivers",
        "Unplaced I/O Pads",
    )
    for label in labels:
        values = re.findall(rf"(?mi)^\s*{re.escape(label)}\s*:\s*(\d+)\s*$", text)
        if values != ["0"]:
            raise FinalizeError(f"{primary}: critical checkDesign count {label!r} is not zero")
    return primary


def _load_replay(replay_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    case_id = str(manifest["id"])
    replay_match = re.fullmatch(r"replay_(\d+)", replay_dir.name)
    if replay_match is None:
        raise FinalizeError(f"invalid replay directory name: {replay_dir}")
    replay_index = int(replay_match.group(1))
    baseline_provenance = _manifest_baseline_provenance(manifest)
    for role, relative in REPLAY_EVIDENCE_FILES.items():
        _required_file(replay_dir, relative, role.replace("_", " "))
    evidence_directories = {
        role: _required_directory(replay_dir, relative, role.replace("_", " "))
        for role, relative in REPLAY_EVIDENCE_DIRECTORIES.items()
    }
    run_status = _verify_run_status(
        replay_dir,
        case_id,
        str(manifest["tool_version"]),
        replay_index=replay_index,
        baseline_provenance=baseline_provenance,
    )
    before = _stage_metrics(replay_dir, manifest, "before")
    after = _stage_metrics(replay_dir, manifest, "after")
    baseline_guard_path = _required_file(
        replay_dir, REPLAY_EVIDENCE_FILES["baseline_guard"], "baseline guard"
    )
    baseline_guard = _load_baseline_guard(
        baseline_guard_path,
        case_id=case_id,
        provenance=baseline_provenance,
    )
    physical_path = _required_file(
        replay_dir,
        REPLAY_EVIDENCE_FILES["physical_no_regression"],
        "physical no-regression audit",
    )
    physical_no_regression = _load_physical_no_regression(
        physical_path,
        case_id=case_id,
        before=before,
        after=after,
    )

    checkpoint_path = _required_file(
        replay_dir, REPLAY_EVIDENCE_FILES["violating_checkpoint"], "violating checkpoint"
    )
    checkpoint_data = evidence_directories["violating_checkpoint_data"]
    _validate_checkpoint_wrapper(
        checkpoint_path, checkpoint_data, str(manifest["design"])
    )
    before_netlist_path = _required_file(
        replay_dir, REPLAY_EVIDENCE_FILES["before_netlist"], "before-ECO gate netlist"
    )
    normalized_before_netlist_sha256 = _validate_gate_netlist(
        before_netlist_path, str(manifest["design"]), "before-ECO gate netlist"
    )
    for role in ("setup_spef_before", "hold_spef_before"):
        _validate_spef(
            _required_file(replay_dir, REPLAY_EVIDENCE_FILES[role], role.replace("_", " ")),
            str(manifest["design"]),
            role.replace("_", " "),
        )
    check_design_primary = _validate_check_design_tree(
        evidence_directories["check_design_after"],
        design=str(manifest["design"]),
        version=str(manifest["tool_version"]),
    )

    concrete_path = _required_file(
        replay_dir, "reports/concrete_fix.tcl", "concrete Gold fix"
    )
    concrete_text = _validate_concrete_fix(
        concrete_path,
        repair_mode=str(manifest["repair_mode"]),
        case_type=str(manifest["type"]),
        case_id=case_id,
    )
    constraint_before = _required_file(
        replay_dir, "reports/constraint_before.sdc", "before SDC"
    )
    constraint_after = _required_file(
        replay_dir, "reports/constraint_after.sdc", "after SDC"
    )
    constraint_before_hash = _sha256(constraint_before)
    constraint_after_hash = _sha256(constraint_after)
    if constraint_before_hash != constraint_after_hash:
        raise FinalizeError(
            f"{replay_dir}: before/after SDC SHA256 differs; the ECO changed constraints"
        )
    constraint_sha256_by_mode: dict[str, str] = {}
    for mode in ("setup", "hold"):
        mode_before = _required_file(
            replay_dir,
            REPLAY_EVIDENCE_FILES[f"constraint_{mode}_before"],
            f"{mode} before SDC",
        )
        mode_after = _required_file(
            replay_dir,
            REPLAY_EVIDENCE_FILES[f"constraint_{mode}_after"],
            f"{mode} after SDC",
        )
        before_hash = _sha256(mode_before)
        after_hash = _sha256(mode_after)
        if before_hash != after_hash:
            raise FinalizeError(
                f"{replay_dir}: {mode} before/after SDC SHA256 differs; "
                "the ECO changed constraints"
            )
        constraint_sha256_by_mode[mode] = before_hash
    if constraint_sha256_by_mode["setup"] != constraint_before_hash:
        raise FinalizeError(
            f"{replay_dir}: legacy constraint_before/after SDC is not the setup-view copy"
        )
    audit_path = _required_file(
        replay_dir, "reports/functional_audit.json", "functional audit"
    )
    catalog = manifest["catalog_entry"]
    max_eco_cells = int(catalog["repair"]["max_eco_cells"])
    audit = load_functional_audit(
        audit_path,
        case_id,
        str(manifest["design"]),
        repair_mode=str(manifest["repair_mode"]),
        max_eco_cells=max_eco_cells,
    )
    native_cell_diff_path: Path | None = None
    native_cell_diff_sha256: str | None = None
    if manifest["repair_mode"] == "native":
        native_cell_diff_path = _required_file(
            replay_dir, NATIVE_CELL_DIFF_RELATIVE, "native cell diff"
        )
        validate_native_cell_diff(native_cell_diff_path)
        native_cell_diff_sha256 = _sha256(native_cell_diff_path)
    diagnostic_path = _required_file(
        replay_dir, REPLAY_EVIDENCE_FILES["diagnostic_context"], "diagnostic context"
    )
    diagnostic_context = load_diagnostic_context(
        diagnostic_path,
        case_id=case_id,
        design=str(manifest["design"]),
        max_eco_cells=max_eco_cells,
    )
    native_selected_terms_path: Path | None = None
    native_selected_terms_sha256: str | None = None
    if manifest["repair_mode"] == "native":
        native_selected_terms_path = _required_file(
            replay_dir,
            NATIVE_SELECTED_TERMS_RELATIVE,
            "native selectedTerms file",
        )
        validate_native_selected_terms(
            native_selected_terms_path, diagnostic_context
        )
        native_selected_terms_sha256 = _sha256(native_selected_terms_path)
    locality_path = _required_file(
        replay_dir,
        REPLAY_EVIDENCE_FILES["violation_locality"],
        "violation locality",
    )
    violation_locality = load_violation_locality(
        locality_path,
        case_id=case_id,
        repair_mode=str(manifest["repair_mode"]),
        case_type=str(manifest["type"]),
    )
    _bind_diagnostic_to_locality(
        context=diagnostic_context,
        locality=violation_locality,
        before=before,
        where=str(replay_dir),
    )
    allowed_answer_eco_names = _validate_fix_object_coverage(
        concrete_text,
        diagnostic_context,
        case_id=case_id,
        case_type=str(manifest["type"]),
        max_eco_cells=max_eco_cells,
    )
    provenance_path = _required_file(
        replay_dir, "reports/injection_provenance.json", "injection provenance"
    )
    provenance = load_injection_provenance(
        provenance_path,
        case_id=case_id,
        case_type=str(manifest["type"]),
        before=before,
        expected_targets=catalog["target_wns_ns"],
        expected_strategy=str(catalog["injection"]["strategy"]),
        baseline_provenance=baseline_provenance,
    )
    resolved = _required_file(
        replay_dir, "reports/resolved_targets.tcl", "resolved-target evidence"
    )
    return {
        "root": replay_dir,
        "replay_index": replay_index,
        "run_status": run_status,
        "run_status_sha256": _sha256(
            _required_file(replay_dir, "run_status.json", "run status")
        ),
        "host_ssh_log_sha256": _sha256(
            _required_file(
                replay_dir,
                REPLAY_EVIDENCE_FILES["host_ssh_log"],
                "host SSH log",
            )
        ),
        "success_marker_sha256": _sha256(
            _required_file(
                replay_dir,
                REPLAY_EVIDENCE_FILES["success_marker"],
                "success marker",
            )
        ),
        "baseline_provenance": baseline_provenance,
        "baseline_guard": baseline_guard,
        "baseline_guard_path": baseline_guard_path,
        "baseline_guard_sha256": _sha256(baseline_guard_path),
        "physical_no_regression": physical_no_regression,
        "physical_no_regression_path": physical_path,
        "physical_no_regression_sha256": _sha256(physical_path),
        "violating_checkpoint_sha256": _sha256(checkpoint_path),
        "violating_checkpoint_tree_sha256": _tree_sha256(
            checkpoint_data, "violating checkpoint data"
        ),
        "before_netlist_sha256": _sha256(before_netlist_path),
        "normalized_before_netlist_sha256": normalized_before_netlist_sha256,
        "setup_spef_before_sha256": _sha256(
            _required_file(
                replay_dir,
                REPLAY_EVIDENCE_FILES["setup_spef_before"],
                "setup before SPEF",
            )
        ),
        "hold_spef_before_sha256": _sha256(
            _required_file(
                replay_dir,
                REPLAY_EVIDENCE_FILES["hold_spef_before"],
                "hold before SPEF",
            )
        ),
        "check_design_primary_sha256": _sha256(check_design_primary),
        "check_design_tree_sha256": _tree_sha256(
            evidence_directories["check_design_after"], "checkDesign after evidence"
        ),
        "before": before,
        "after": after,
        "concrete_fix_path": concrete_path,
        "concrete_fix_text": concrete_text,
        "concrete_fix_sha256": _sha256(concrete_path),
        "constraint_sha256": constraint_before_hash,
        "constraint_sha256_by_mode": constraint_sha256_by_mode,
        "audit": audit,
        "audit_sha256": _sha256(audit_path),
        "native_cell_diff_path": native_cell_diff_path,
        "native_cell_diff_sha256": native_cell_diff_sha256,
        "native_selected_terms_path": native_selected_terms_path,
        "native_selected_terms_sha256": native_selected_terms_sha256,
        "diagnostic_context": diagnostic_context,
        "diagnostic_context_path": diagnostic_path,
        "diagnostic_context_sha256": _sha256(diagnostic_path),
        "violation_locality": violation_locality,
        "violation_locality_path": locality_path,
        "violation_locality_sha256": _sha256(locality_path),
        "allowed_answer_eco_names": allowed_answer_eco_names,
        "injection_provenance": provenance,
        "injection_provenance_sha256": _sha256(provenance_path),
        "resolved_targets_sha256": _sha256(resolved),
        "fixed_netlist_sha256": _sha256(
            _required_file(replay_dir, "fixed.v", "fixed gate netlist")
        ),
        "normalized_fixed_netlist_sha256": _normalized_verilog_sha256(
            _required_file(replay_dir, "fixed.v", "fixed gate netlist")
        ),
    }


def _timing_metric_only(value: Mapping[str, Any]) -> dict[str, float]:
    return {"wns_ns": float(value["wns_ns"]), "tns_ns": float(value["tns_ns"])}


def _canonical_stage(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "setup": _timing_metric_only(value["setup"]),
        "hold": _timing_metric_only(value["hold"]),
        "drv": dict(value["drv"]),
        "drc": dict(value["drc"]),
        "connectivity": {"violations": int(value["connectivity"]["violations"])},
    }


def _semantic_replay(value: Mapping[str, Any]) -> dict[str, Any]:
    # Keep endpoint/view details in replay comparison even though the public
    # metrics schema exports only WNS/TNS.
    return {"before": value["before"], "after": value["after"]}


def _compare_replay_reports(
    first: Mapping[str, Any], second: Mapping[str, Any], case_id: str
) -> dict[str, Any]:
    timing_deltas: dict[str, dict[str, dict[str, float]]] = {}
    for stage in ("before", "after"):
        timing_deltas[stage] = {}
        for check in ("setup", "hold"):
            left = first[stage][check]
            right = second[stage][check]
            for field in (
                "violating_paths",
                "reported_paths",
                "reported_endpoints",
                "worst_endpoint",
                "worst_beginpoint",
                "analysis_view",
                "tool_version",
            ):
                if left[field] != right[field]:
                    raise FinalizeError(
                        f"{case_id}: replay {stage} {check} {field} differs"
                    )
            timing_deltas[stage][check] = {}
            for field in ("wns_ns", "tns_ns"):
                delta = abs(float(left[field]) - float(right[field]))
                if delta > REPLAY_TIMING_TOLERANCE_NS + NUMERIC_EPSILON:
                    raise FinalizeError(
                        f"{case_id}: replay {stage} {check} {field} delta "
                        f"{delta:.9f} ns exceeds {REPLAY_TIMING_TOLERANCE_NS:.3f} ns"
                    )
                timing_deltas[stage][check][field] = round(delta, 9)
        for physical in ("drv", "drc", "connectivity"):
            if first[stage][physical] != second[stage][physical]:
                raise FinalizeError(
                    f"{case_id}: replay {stage} {physical} evidence differs"
                )
    return timing_deltas


def _compare_replays(replays: Sequence[Mapping[str, Any]], case_id: str) -> dict[str, Any]:
    if len(replays) != EXPECTED_REPLAYS:
        raise FinalizeError(f"{case_id}: exactly two replays are required")
    first, second = replays
    if first["baseline_provenance"] != second["baseline_provenance"]:
        raise FinalizeError(f"{case_id}: replay baseline provenance differs")
    for mode in ("setup", "hold"):
        for field in ("wns_ns", "tns_ns"):
            delta = abs(
                float(first["baseline_guard"][mode][field])
                - float(second["baseline_guard"][mode][field])
            )
            if delta > REPLAY_TIMING_TOLERANCE_NS + NUMERIC_EPSILON:
                raise FinalizeError(
                    f"{case_id}: replay baseline guard {mode} {field} delta "
                    f"{delta:.9f} ns exceeds {REPLAY_TIMING_TOLERANCE_NS:.3f} ns"
                )
    timing_deltas = _compare_replay_reports(first, second, case_id)
    for key, label in (
        ("concrete_fix_sha256", "concrete fix"),
        ("constraint_sha256", "constraint SDC"),
        ("constraint_sha256_by_mode", "setup/hold constraint SDCs"),
        ("resolved_targets_sha256", "resolved targets"),
        ("diagnostic_context_sha256", "diagnostic context"),
        ("violation_locality_sha256", "violation locality"),
        ("normalized_fixed_netlist_sha256", "normalized fixed gate netlist"),
        ("normalized_before_netlist_sha256", "normalized violating gate netlist"),
        ("physical_no_regression_sha256", "physical no-regression audit"),
    ):
        if first[key] != second[key]:
            raise FinalizeError(f"{case_id}: replay_1 and replay_2 {label} SHA256 differs")
    if first.get("native_cell_diff_sha256") != second.get("native_cell_diff_sha256"):
        raise FinalizeError(
            f"{case_id}: replay_1 and replay_2 native cell diff SHA256 differs"
        )
    if first.get("native_selected_terms_sha256") != second.get(
        "native_selected_terms_sha256"
    ):
        raise FinalizeError(
            f"{case_id}: replay_1 and replay_2 native selectedTerms SHA256 differs"
        )
    for field in ("strategy", "target_wns_ns"):
        if first["injection_provenance"][field] != second["injection_provenance"][field]:
            raise FinalizeError(f"{case_id}: replay injection provenance {field} differs")
    if (
        first["injection_provenance"]["attempts"][0]["parameter"]
        != second["injection_provenance"]["attempts"][0]["parameter"]
    ):
        raise FinalizeError(f"{case_id}: frozen injection parameter differs across replays")
    semantic_hashes = [
        _sha256_bytes(
            json.dumps(
                _semantic_replay(replay), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )
        for replay in replays
    ]
    comparison = {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "case_id": case_id,
        "status": "passed",
        "runs": EXPECTED_REPLAYS,
        "deterministic": True,
        "semantic_evidence_sha256": semantic_hashes,
        "timing_tolerance_ns": REPLAY_TIMING_TOLERANCE_NS,
        "timing_deltas_ns": timing_deltas,
        "concrete_fix_sha256": first["concrete_fix_sha256"],
        "constraint_sdc_sha256": first["constraint_sha256"],
        "constraint_sdc_sha256_by_mode": first["constraint_sha256_by_mode"],
        "resolved_targets_sha256": first["resolved_targets_sha256"],
        "diagnostic_context_sha256": first["diagnostic_context_sha256"],
        "violation_locality_sha256": first["violation_locality_sha256"],
        "normalized_fixed_netlist_sha256": first["normalized_fixed_netlist_sha256"],
        "fixed_netlist_raw_sha256": [replay["fixed_netlist_sha256"] for replay in replays],
        "functional_audit_sha256": [replay["audit_sha256"] for replay in replays],
        "injection_provenance_sha256": [
            replay["injection_provenance_sha256"] for replay in replays
        ],
        "baseline_provenance": dict(first["baseline_provenance"]),
        "baseline_guard_sha256": [replay["baseline_guard_sha256"] for replay in replays],
        "physical_no_regression_sha256": first["physical_no_regression_sha256"],
        "run_status_sha256": [replay["run_status_sha256"] for replay in replays],
        "host_ssh_log_sha256": [
            replay["host_ssh_log_sha256"] for replay in replays
        ],
        "success_marker_sha256": [
            replay["success_marker_sha256"] for replay in replays
        ],
        "violating_checkpoint_sha256": [
            replay["violating_checkpoint_sha256"] for replay in replays
        ],
        "violating_checkpoint_tree_sha256": [
            replay["violating_checkpoint_tree_sha256"] for replay in replays
        ],
        "before_netlist_raw_sha256": [replay["before_netlist_sha256"] for replay in replays],
        "normalized_before_netlist_sha256": first["normalized_before_netlist_sha256"],
        "setup_spef_before_sha256": [
            replay["setup_spef_before_sha256"] for replay in replays
        ],
        "hold_spef_before_sha256": [
            replay["hold_spef_before_sha256"] for replay in replays
        ],
        "check_design_primary_sha256": [
            replay["check_design_primary_sha256"] for replay in replays
        ],
        "check_design_tree_sha256": [
            replay["check_design_tree_sha256"] for replay in replays
        ],
    }
    if first.get("native_cell_diff_sha256") is not None:
        comparison["native_cell_diff_sha256"] = first["native_cell_diff_sha256"]
        comparison["native_selected_terms_sha256"] = first[
            "native_selected_terms_sha256"
        ]
    return comparison


def _validate_acceptance(metrics: Mapping[str, Any], case_type: str, case_id: str) -> None:
    before = metrics["before"]
    after = metrics["after"]
    violated = ("setup", "hold") if case_type == "mixed" else (case_type,)
    for check in violated:
        if before[check]["wns_ns"] >= 0.0 or before[check]["tns_ns"] >= 0.0:
            raise FinalizeError(f"{case_id}: before-ECO {check} WNS/TNS is not a violation")
    for check in ("setup", "hold"):
        if after[check]["wns_ns"] < MIN_FINAL_SLACK_NS - NUMERIC_EPSILON:
            raise FinalizeError(
                f"{case_id}: after-ECO {check} WNS {after[check]['wns_ns']} is below "
                f"{MIN_FINAL_SLACK_NS:.3f} ns"
            )
        if abs(after[check]["tns_ns"]) > NUMERIC_EPSILON:
            raise FinalizeError(f"{case_id}: after-ECO {check} TNS is not zero")
    for rule, after_count in after["drv"].items():
        if after_count > before["drv"][rule]:
            raise FinalizeError(f"{case_id}: {rule} regressed after ECO")
    if after["connectivity"]["violations"] != 0:
        raise FinalizeError(f"{case_id}: connectivity violations remain after ECO")
    if after["drc"]["total"] > before["drc"]["total"]:
        raise FinalizeError(f"{case_id}: DRC total increased after ECO")
    new_categories = sorted(
        name
        for name, count in after["drc"]["categories"].items()
        if count and before["drc"]["categories"].get(name, 0) == 0
    )
    if new_categories:
        raise FinalizeError(f"{case_id}: new DRC categories: {', '.join(new_categories)}")


def _verified_declared_artifact(
    root: Path, item: Any, where: str
) -> tuple[Path, str]:
    if not isinstance(item, Mapping):
        raise FinalizeError(f"{where} must be a hashed artifact object")
    declared = item.get("path")
    digest = item.get("sha256")
    size = item.get("bytes")
    if not isinstance(declared, str) or not declared:
        raise FinalizeError(f"{where}.path is missing")
    path = _required_file(root, declared, where)
    actual = _sha256(path)
    if not isinstance(digest, str) or digest.lower() != actual:
        raise FinalizeError(f"{where}: SHA256 mismatch")
    if not isinstance(size, int) or size != path.stat().st_size:
        raise FinalizeError(f"{where}: byte count mismatch")
    return path, declared


def _load_primetime(
    summary_path: Path,
    *,
    design: str,
    constraint_hashes: Mapping[str, str],
    replay: Mapping[str, Any],
    qualified_libraries: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Path]]:
    try:
        summary = ptx.load_and_verify_summary(summary_path)
    except ptx.CrosscheckError as exc:
        raise FinalizeError(f"invalid PrimeTime crosscheck {summary_path}: {exc}") from exc
    if summary.get("passed") is not True:
        raise FinalizeError(f"{summary_path}: PrimeTime setup/hold crosscheck did not pass")

    policy = summary.get("policy")
    if not isinstance(policy, Mapping):
        raise FinalizeError(f"{summary_path}: PrimeTime Gold policy is missing")
    if policy.get("threshold_ns") != MIN_FINAL_SLACK_NS:
        raise FinalizeError(
            f"{summary_path}: PrimeTime Gold threshold must be "
            f"{MIN_FINAL_SLACK_NS:.3f} ns"
        )
    if policy.get("required_tool_version") != ptx.DEFAULT_TOOL_VERSION:
        raise FinalizeError(
            f"{summary_path}: PrimeTime Gold tool version must be "
            f"{ptx.DEFAULT_TOOL_VERSION}"
        )
    if policy.get("synopsys_lc_root") != ptx.DEFAULT_SYNOPSYS_LC_ROOT:
        raise FinalizeError(
            f"{summary_path}: PrimeTime Gold SYNOPSYS_LC_ROOT must be "
            f"{ptx.DEFAULT_SYNOPSYS_LC_ROOT}"
        )
    if policy.get("pt_shell") != ptx.DEFAULT_PT_SHELL:
        raise FinalizeError(
            f"{summary_path}: PrimeTime Gold pt_shell must be {ptx.DEFAULT_PT_SHELL}"
        )
    if policy.get("sdc_roles") != ptx.SDC_ROLE_BY_MODE:
        raise FinalizeError(f"{summary_path}: PrimeTime Gold SDC roles are invalid")
    if policy.get("pt_sdc_roles") != ptx.PT_SDC_ROLE_BY_MODE:
        raise FinalizeError(
            f"{summary_path}: PrimeTime Gold adapted SDC roles are invalid"
        )
    if (
        policy.get("sdc_adapter_schema_version")
        != ptx.SDC_ADAPTER_SCHEMA_VERSION
    ):
        raise FinalizeError(
            f"{summary_path}: PrimeTime Gold SDC adapter schema is invalid"
        )

    for mode in ("setup", "hold"):
        if summary["corners"][mode].get("top") != design:
            raise FinalizeError(f"{summary_path}: PrimeTime {mode} top does not match {design}")

    adaptation = summary.get("sdc_adaptation")
    if not isinstance(adaptation, Mapping) or adaptation.get("top") != design:
        raise FinalizeError(
            f"{summary_path}: PrimeTime SDC adaptation top does not match {design}"
        )
    if (
        adaptation.get("schema_version") != ptx.SDC_ADAPTER_SCHEMA_VERSION
        or adaptation.get("operation") != ptx.SDC_ADAPTER_OPERATION
        or adaptation.get("comment_prefix") != ptx.SDC_ADAPTER_COMMENT_PREFIX
    ):
        raise FinalizeError(
            f"{summary_path}: PrimeTime Gold SDC adaptation contract is invalid"
        )

    root = summary_path.resolve().parent
    copy_paths: dict[str, Path] = {}
    input_paths: dict[str, Path] = {}
    inputs = summary.get("inputs")
    required_inputs = set(ptx.INPUT_ROLES)
    if not isinstance(inputs, Mapping) or not required_inputs.issubset(inputs):
        raise FinalizeError(f"{summary_path}: PrimeTime input hash metadata is incomplete")
    for name in sorted(required_inputs):
        item = inputs[name]
        if not isinstance(item, Mapping):
            raise FinalizeError(f"{summary_path}: inputs.{name} must be an object")
        digest = item.get("sha256")
        size = item.get("bytes")
        source_path = item.get("source_path")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest.lower()):
            raise FinalizeError(f"{summary_path}: inputs.{name}.sha256 is invalid")
        if not isinstance(size, int) or size <= 0 or not isinstance(source_path, str) or not source_path:
            raise FinalizeError(f"{summary_path}: inputs.{name} metadata is incomplete")
        path, declared = _verified_declared_artifact(
            root, item, f"{summary_path}: inputs.{name}"
        )
        input_paths[name] = path
        copy_paths[declared] = path

    template_tcl = Path(ptx.DEFAULT_TCL).resolve()
    if not template_tcl.is_file() or template_tcl.stat().st_size == 0:
        raise FinalizeError(
            f"repository PrimeTime Tcl template is missing or empty: {template_tcl}"
        )
    template_bytes = template_tcl.read_bytes()
    template_digest = _sha256_bytes(template_bytes)
    template_size = len(template_bytes)
    if (
        input_paths["tcl"].read_bytes() != template_bytes
        or inputs["tcl"].get("sha256") != template_digest
        or inputs["tcl"].get("bytes") != template_size
    ):
        raise FinalizeError(
            f"{summary_path}: staged PrimeTime Tcl does not exactly match the "
            "repository Gold template"
        )

    replay_root = Path(replay["root"])
    bindings = {
        "netlist": _required_file(replay_root, "fixed.v", "fixed netlist"),
        "setup_sdc": _required_file(
            replay_root,
            REPLAY_EVIDENCE_FILES["constraint_setup_after"],
            "post-ECO setup SDC",
        ),
        "hold_sdc": _required_file(
            replay_root,
            REPLAY_EVIDENCE_FILES["constraint_hold_after"],
            "post-ECO hold SDC",
        ),
        "setup_spef": _required_file(replay_root, "setup_after.spef", "setup SPEF"),
        "hold_spef": _required_file(replay_root, "hold_after.spef", "hold SPEF"),
    }
    expected_hashes = {name: _sha256(path) for name, path in bindings.items()}
    expected_hashes["setup_sdc"] = constraint_hashes["setup"]
    expected_hashes["hold_sdc"] = constraint_hashes["hold"]
    for name, expected in expected_hashes.items():
        if str(inputs[name]["sha256"]).lower() != expected:
            raise FinalizeError(
                f"{summary_path}: PrimeTime input {name} is not bound to replay_1 evidence"
            )
        if inputs[name]["bytes"] != bindings[name].stat().st_size:
            raise FinalizeError(
                f"{summary_path}: PrimeTime input {name} byte count does not match "
                "replay_1 evidence"
            )

    # Keep the Innovus exports above as the byte-exact constraint evidence,
    # while binding the files actually read by PrimeTime to the one permitted
    # dialect transformation.  Recompute the adaptation from replay 1 here so
    # Gold promotion does not depend only on metadata asserted by the runner.
    for mode in ("setup", "hold"):
        item = adaptation.get(mode)
        if not isinstance(item, Mapping):
            raise FinalizeError(
                f"{summary_path}: PrimeTime {mode} SDC adaptation is missing"
            )
        adapted_path, declared = _verified_declared_artifact(
            root,
            item.get("adapted_artifact"),
            f"{summary_path}: sdc_adaptation.{mode}.adapted_artifact",
        )
        try:
            (
                expected_adapted,
                expected_line,
                expected_design_rule_lines,
            ) = ptx._adapt_innovus_sdc_bytes(
                bindings[f"{mode}_sdc"].read_bytes(), design
            )
        except ptx.CrosscheckError as exc:
            raise FinalizeError(
                f"{summary_path}: invalid replay_1 {mode} SDC adaptation source: {exc}"
            ) from exc
        if adapted_path.read_bytes() != expected_adapted:
            raise FinalizeError(
                f"{summary_path}: PrimeTime {mode} SDC is not the deterministic "
                "bounded-line adaptation of replay_1 evidence"
            )
        if item.get("current_design_line") != expected_line:
            raise FinalizeError(
                f"{summary_path}: PrimeTime {mode} SDC adaptation line is inconsistent"
            )
        if (
            item.get("design_rule_get_designs_lines")
            != expected_design_rule_lines
        ):
            raise FinalizeError(
                f"{summary_path}: PrimeTime {mode} SDC design-rule adaptation "
                "lines are inconsistent"
            )
        copy_paths[declared] = adapted_path

    for input_role, qualification_role in QUALIFIED_LIBERTY_ROLES.items():
        qualified = qualified_libraries.get(qualification_role)
        if not isinstance(qualified, Mapping):
            raise FinalizeError(
                f"{summary_path}: qualified {qualification_role} binding is missing"
            )
        if (
            inputs[input_role].get("sha256") != qualified.get("sha256")
            or inputs[input_role].get("source_sha256") != qualified.get("sha256")
            or inputs[input_role].get("bytes") != qualified.get("bytes")
            or inputs[input_role].get("source_bytes") != qualified.get("bytes")
        ):
            raise FinalizeError(
                f"{summary_path}: PrimeTime {input_role} is not bound to qualified "
                f"{qualification_role}"
            )

    execution = summary.get("execution")
    if not isinstance(execution, Mapping) or set(execution) != {"setup", "hold"}:
        raise FinalizeError(f"{summary_path}: PrimeTime execution evidence is incomplete")
    for mode in ("setup", "hold"):
        item = execution[mode]
        if not isinstance(item, Mapping):
            raise FinalizeError(f"{summary_path}: execution.{mode} must be an object")
        if item.get("exit_code") != 0 or item.get("timed_out") is not False:
            raise FinalizeError(f"{summary_path}: PrimeTime {mode} process did not pass cleanly")
        path, declared = _verified_declared_artifact(
            root, item.get("log"), f"execution.{mode}.log"
        )
        copy_paths[declared] = path
    for mode in ("setup", "hold"):
        for role, evidence in summary["corners"][mode]["evidence"].items():
            path, declared = _verified_declared_artifact(
                root, evidence, f"corners.{mode}.evidence.{role}"
            )
            copy_paths[declared] = path
    return summary, copy_paths


def _format_slack(value: float) -> str:
    return f"{value:+.3f} ns"


def _build_instruction(manifest: Mapping[str, Any], replay: Mapping[str, Any]) -> str:
    before = replay["before"]
    case_type = str(manifest["type"])
    focus = {
        "setup": "setup 方向存在负裕量",
        "hold": "hold 方向存在负裕量",
        "mixed": "setup 与 hold 两个方向同时存在负裕量",
    }[case_type]
    setup = before["setup"]
    hold = before["hold"]
    drv = before["drv"]
    drc = before["drc"]
    connectivity = before["connectivity"]
    diagnostic = replay["diagnostic_context"]
    target_lines = []
    for index, target in enumerate(diagnostic["targets"], 1):
        local_cells = ",".join(
            f"{cell['inst']}(ref={cell['ref']})" for cell in target["local_cells"]
        )
        # diagnostic_context.v1 calls this field ``beginpoint``, but the
        # Innovus collection value is ``launching_point``: the launch flop's
        # clock pin (CK), not the report_timing data beginpoint (Q).  Keep the
        # evidence schema stable while giving the public SFT prompt the
        # semantically correct name.
        target_lines.append(
            f"目标 {index}: role={target['role']}; timing={target['timing']}; "
            f"slack={_format_slack(float(target['slack_ns']))}; "
            f"launch_clock_pin={target['beginpoint']}; endpoint={target['endpoint']}; "
            f"net={target['net']}; driver_pin={target['driver_pin']}; "
            f"driver_inst={target['driver_inst']}; driver_ref={target['driver_ref']}; "
            f"local_cells=[{local_cells}]"
        )
    instruction = (
        f"请在 {manifest['design']} 的已布线 post-route 数据库上完成 {manifest['id']} "
        f"timing ECO；当前 {focus}。真实 Innovus MMMC 报告显示：setup 视图 "
        f"{setup['analysis_view']} 的 WNS/TNS 为 {_format_slack(setup['wns_ns'])}/"
        f"{_format_slack(setup['tns_ns'])}，最差路径为 {setup['worst_beginpoint']} -> "
        f"{setup['worst_endpoint']}；hold 视图 {hold['analysis_view']} 的 WNS/TNS 为 "
        f"{_format_slack(hold['wns_ns'])}/{_format_slack(hold['tns_ns'])}，最差路径为 "
        f"{hold['worst_beginpoint']} -> {hold['worst_endpoint']}。ECO 前 DRV 计数为 "
        f"max_transition={drv['max_transition_violations']}、"
        f"max_capacitance={drv['max_capacitance_violations']}、"
        f"max_fanout={drv['max_fanout_violations']}，DRC={drc['total']}，"
        f"connectivity={connectivity['violations']}。允许修改的 ECO cell 上限为 "
        f"{diagnostic['max_eco_cells']}；若新增 ECO 实例，必须采用确定性命名 "
        f"SFT_ECO_{manifest['id']}_<SETUP|HOLD>_<ordinal>，其中 role 必须匹配修复方向，"
        "ordinal 从 1 连续递增且不得复用；以下是从同一 post-route DB 解析并经双重放一致性校验的"
        "全部目标证据：\n"
        + "\n".join(target_lines)
        + "\n请给出针对这些真实对象的最小化 Innovus Tcl，"
        "不得修改 SDC、放松时钟/I/O 约束或添加 false path、multicycle path、disable timing；"
        "修复 Tcl 只需完成 ECO、增量摆放与 ECO 布线，独立 replay harness 将在执行后复查 "
        "setup、hold、DRV、DRC 和 connectivity，并要求两个"
        f"时序方向 WNS 均至少为 +{MIN_FINAL_SLACK_NS:.3f} ns。"
    )
    if len(instruction) > MAX_INSTRUCTION_CHARS:
        raise FinalizeError(
            f"{manifest['id']}: generated instruction exceeds {MAX_INSTRUCTION_CHARS} characters"
        )
    return instruction


def _fix_kind(fix: str) -> str:
    actions: list[str] = []
    if re.search(r"(?mi)^\s*ecoChangeCell\b", fix):
        actions.append("cell resizing")
    if re.search(r"(?mi)^\s*ecoAddRepeater\b", fix):
        actions.append("repeater insertion")
    if re.search(r"(?mi)^\s*optDesign\b", fix):
        actions.append("selected endpoint native optimization")
    return "、".join(actions)


def _build_answer(manifest: Mapping[str, Any], replay: Mapping[str, Any], fix: str) -> str:
    before = replay["before"]
    after = replay["after"]
    case_type = str(manifest["type"])
    focus = {
        "setup": "setup 负裕量",
        "hold": "hold 负裕量",
        "mixed": "setup/hold 双向负裕量",
    }[case_type]
    diagnosis = (
        f"报告中的{focus}集中在已解析的真实端点，ECO 前 setup/hold WNS 分别为 "
        f"{_format_slack(before['setup']['wns_ns'])} 和 "
        f"{_format_slack(before['hold']['wns_ns'])}。"
        f"修复采用局部 {_fix_kind(fix)}，同时保留原始 SDC，并用增量摆放和 ECO 布线收尾。"
        f"独立 replay harness 两次重放后 setup/hold WNS 分别达到 "
        f"{_format_slack(after['setup']['wns_ns'])} 和 "
        f"{_format_slack(after['hold']['wns_ns'])}，TNS 清零且物理与连接检查未回退。"
    )
    return diagnosis + "\n\n```tcl\n" + fix + "```\n"


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def _copy_tree(source: Path, destination: Path, label: str) -> None:
    _required_directory(source.parent, source.name, label)
    if destination.exists():
        raise FinalizeError(f"refusing to overwrite copied {label}: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=False)
    if _tree_sha256(source, label) != _tree_sha256(destination, f"copied {label}"):
        raise FinalizeError(f"copied {label} tree differs from its source")


def _render_canonical_replay(path: Path) -> str:
    """Redirect the replay-only materializer away from canonical ``fix.tcl``.

    The canonical fix is the concrete answer.  The runtime materializer is
    still needed during a fresh replay because it also records operation and
    functional-proof state consumed by ``functional_audit.json``.
    """

    source = _read_text(path, "prepared replay Tcl")
    old = "[file join $::SFT_CASE_DIR fix.tcl]"
    new = "[file join $::SFT_CASE_DIR replay_fix_materializer.tcl]"
    if source.count(old) != 1:
        raise FinalizeError(
            f"{path}: replay must source exactly one case-local fix.tcl materializer"
        )
    return source.replace(old, new)


def _copy_replay_two_evidence(
    replay_root: Path, destination: Path, *, repair_mode: str
) -> dict[str, str]:
    copied: dict[str, str] = {}
    roles = (
        "setup_before",
        "hold_before",
        "setup_after",
        "hold_after",
        "drv_before",
        "drv_after",
        "connectivity_before",
        "connectivity_after",
        "drc_before",
        "drc_after",
        "concrete_fix",
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
        "resolved_targets",
        "diagnostic_context",
        "violation_locality",
        "innovus_log",
        "host_ssh_log",
        "run_status",
        "success_marker",
        "violating_checkpoint",
        "before_netlist",
        "setup_spef_before",
        "hold_spef_before",
    )
    for role in roles:
        relative = REPLAY_EVIDENCE_FILES[role]
        source = _required_file(replay_root, relative, f"replay_2 {role}")
        target_relative = f"evidence/replay_2/{relative}"
        _copy(source, destination / target_relative)
        copied[f"replay_2_{role}"] = target_relative
    for role, relative in REPLAY_EVIDENCE_DIRECTORIES.items():
        source = _required_directory(replay_root, relative, f"replay_2 {role}")
        target_relative = f"evidence/replay_2/{relative}"
        _copy_tree(source, destination / target_relative, f"replay_2 {role}")
        copied[f"replay_2_{role}"] = target_relative
    if repair_mode == "native":
        for role, relative, label in (
            ("native_cell_diff", NATIVE_CELL_DIFF_RELATIVE, "native cell diff"),
            (
                "native_selected_terms",
                NATIVE_SELECTED_TERMS_RELATIVE,
                "native selectedTerms file",
            ),
        ):
            source = _required_file(
                replay_root, relative, f"replay_2 {label}"
            )
            target_relative = f"evidence/replay_2/{relative}"
            _copy(source, destination / target_relative)
            copied[f"replay_2_{role}"] = target_relative
    return copied


def _prepared_source(case_dir: Path, name: str) -> Path:
    source = _required_file(case_dir, name, f"prepared {name}")
    for replay_index in (1, 2):
        replay_copy = _required_file(
            case_dir / "runs" / f"replay_{replay_index}", name, f"replay {name}"
        )
        if _sha256(replay_copy) != _sha256(source):
            raise FinalizeError(
                f"{case_dir.name}: prepared {name} differs from replay_{replay_index}"
            )
    return source


def _collect_case(case_dir: Path) -> dict[str, Any]:
    case_dir = case_dir.resolve()
    manifest = _load_manifest(case_dir)
    baseline_provenance = _manifest_baseline_provenance(manifest)
    replays = [
        _load_replay(case_dir / "runs" / f"replay_{index}", manifest)
        for index in range(1, EXPECTED_REPLAYS + 1)
    ]
    comparison = _compare_replays(replays, str(manifest["id"]))
    qualification_path = _prepared_source(case_dir, "baseline_qualification.json")
    qualification = _validate_prepared_qualification(
        qualification_path,
        manifest=manifest,
        provenance=baseline_provenance,
    )
    before = _canonical_stage(replays[0]["before"])
    after = _canonical_stage(replays[0]["after"])
    base_metrics = {"before": before, "after": after}
    for index, replay in enumerate(replays, 1):
        replay_metrics = {
            "before": _canonical_stage(replay["before"]),
            "after": _canonical_stage(replay["after"]),
        }
        try:
            _validate_acceptance(
                replay_metrics, str(manifest["type"]), str(manifest["id"])
            )
        except FinalizeError as exc:
            raise FinalizeError(f"replay_{index}: {exc}") from exc

    summary_path = _required_file(case_dir, "primetime_crosscheck.json", "PT summary")
    pt_summary, pt_copy_paths = _load_primetime(
        summary_path,
        design=str(manifest["design"]),
        constraint_hashes=comparison["constraint_sdc_sha256_by_mode"],
        replay=replays[0],
        qualified_libraries=baseline_provenance["source_artifacts"],
    )
    instruction = _build_instruction(manifest, replays[0])
    fix = str(replays[0]["concrete_fix_text"])
    answer = _build_answer(manifest, replays[0], fix)
    _validate_answer_eco_names(
        answer,
        set(replays[0]["allowed_answer_eco_names"]),
        case_id=str(manifest["id"]),
    )
    injection = _read_text(_prepared_source(case_dir, "inject.tcl"), "hidden injection")
    if (
        "inject" in instruction.lower()
        or injection.strip() in instruction
        or re.search(r"(?i)\boriginal_driver_(?:inst|ref)\s*=", instruction)
    ):
        raise FinalizeError(f"{case_dir.name}: generated instruction leaks hidden injection")

    metrics = {
        "schema_version": SCHEMA_VERSION,
        **base_metrics,
        "replay": {"status": "passed", "runs": 2, "deterministic": True},
        "checks": {
            "constraint_hash_unchanged": True,
            "functional_audit_passed": True,
            "primetime_crosscheck_passed": True,
        },
        "crosschecks": {
            "replay": comparison,
            "constraints": {
                "sha256": comparison["constraint_sdc_sha256"],
                "sha256_by_mode": comparison["constraint_sdc_sha256_by_mode"],
            },
            "functional_audit": {
                "schema_version": FUNCTIONAL_AUDIT_SCHEMA,
                "passed": True,
                "replay_sha256": comparison["functional_audit_sha256"],
            },
            "diagnostic_context": {
                "schema_version": DIAGNOSTIC_CONTEXT_SCHEMA,
                "sha256": comparison["diagnostic_context_sha256"],
            },
            "violation_locality": {
                "schema_version": VIOLATION_LOCALITY_SCHEMA,
                "sha256": comparison["violation_locality_sha256"],
            },
            "hidden_injection": {
                "schema_version": INJECTION_PROVENANCE_SCHEMA,
                "status": "passed",
                "replay_sha256": comparison["injection_provenance_sha256"],
            },
            "baseline_provenance": {
                **baseline_provenance,
                "baseline_guard_sha256": comparison["baseline_guard_sha256"],
                "qualification_artifact_sha256": _sha256(qualification_path),
            },
            "physical_no_regression": {
                "schema_version": PHYSICAL_NO_REGRESSION_SCHEMA,
                "sha256": comparison["physical_no_regression_sha256"],
            },
            "primetime": pt_summary,
        },
    }
    return {
        "source": case_dir,
        "manifest": manifest,
        "replays": replays,
        "comparison": comparison,
        "pt_summary": pt_summary,
        "pt_summary_path": summary_path,
        "pt_copy_paths": pt_copy_paths,
        "instruction": instruction,
        "answer": answer,
        "metrics": metrics,
        "prepared_inject": _prepared_source(case_dir, "inject.tcl"),
        "prepared_replay": _prepared_source(case_dir, "replay.tcl"),
        "prepared_materializer": _prepared_source(case_dir, "fix.tcl"),
        "prepared_case_config": _prepared_source(case_dir, "case_config.tcl"),
        "prepared_runtime": _prepared_source(case_dir, "pilot_runtime.tcl"),
        "prepared_qualification": qualification_path,
        "baseline_qualification": qualification,
        "baseline_provenance": baseline_provenance,
    }


def _materialize_case(collected: Mapping[str, Any], destination: Path) -> None:
    source = Path(collected["source"])
    manifest = collected["manifest"]
    replay_one = Path(collected["replays"][0]["root"])
    replay_two = Path(collected["replays"][1]["root"])
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise FinalizeError(f"staging directory is not empty: {destination}")

    _copy(Path(collected["prepared_inject"]), destination / "inject.tcl")
    (destination / "replay.tcl").write_text(
        _render_canonical_replay(Path(collected["prepared_replay"])),
        encoding="utf-8",
    )
    _copy(
        Path(collected["prepared_materializer"]),
        destination / "replay_fix_materializer.tcl",
    )
    _copy(Path(collected["prepared_case_config"]), destination / "case_config.tcl")
    _copy(Path(collected["prepared_runtime"]), destination / "pilot_runtime.tcl")
    _copy(
        Path(collected["prepared_qualification"]),
        destination / "baseline_qualification.json",
    )
    _copy(
        Path(collected["replays"][0]["concrete_fix_path"]),
        destination / "fix.tcl",
    )
    (destination / "instruction.txt").write_text(
        str(collected["instruction"]) + "\n", encoding="utf-8"
    )
    (destination / "answer.txt").write_text(str(collected["answer"]), encoding="utf-8")
    _write_json(destination / "metrics.json", collected["metrics"])
    _write_json(destination / "replay_comparison.json", collected["comparison"])

    canonical_paths: dict[str, str] = {
        "inject_tcl": "inject.tcl",
        "fix_tcl": "fix.tcl",
        "replay_tcl": "replay.tcl",
        "replay_fix_materializer_tcl": "replay_fix_materializer.tcl",
        "case_config_tcl": "case_config.tcl",
        "pilot_runtime_tcl": "pilot_runtime.tcl",
        "baseline_qualification": "baseline_qualification.json",
        "metrics": "metrics.json",
        "instruction": "instruction.txt",
        "answer": "answer.txt",
        "replay_comparison": "replay_comparison.json",
    }
    for role, relative in REPORT_FILES.items():
        source_path = _required_file(replay_one, relative, f"replay_1 {role}")
        _copy(source_path, destination / relative)
        canonical_paths[role] = relative
    for role in (
        "concrete_fix",
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
        "resolved_targets",
        "diagnostic_context",
        "violation_locality",
        "run_status",
        "success_marker",
        "violating_checkpoint",
        "before_netlist",
        "setup_spef_before",
        "hold_spef_before",
    ):
        relative = REPLAY_EVIDENCE_FILES[role]
        _copy(_required_file(replay_one, relative, role), destination / relative)
        canonical_paths[role] = relative
    for role, relative in REPLAY_EVIDENCE_DIRECTORIES.items():
        source_path = _required_directory(replay_one, relative, role)
        _copy_tree(source_path, destination / relative, role)
        canonical_paths[role] = relative
    if manifest["repair_mode"] == "native":
        for role, relative, label in (
            ("native_cell_diff", NATIVE_CELL_DIFF_RELATIVE, "native cell diff"),
            (
                "native_selected_terms",
                NATIVE_SELECTED_TERMS_RELATIVE,
                "native selectedTerms file",
            ),
        ):
            source_path = _required_file(replay_one, relative, label)
            _copy(source_path, destination / relative)
            canonical_paths[role] = relative
    for role in (
        "fixed_netlist",
        "setup_spef",
        "hold_spef",
        "innovus_log",
        "host_ssh_log",
    ):
        relative = REPLAY_EVIDENCE_FILES[role]
        _copy(_required_file(replay_one, relative, role), destination / relative)
        canonical_paths[role] = relative

    canonical_paths.update(
        _copy_replay_two_evidence(
            replay_two, destination, repair_mode=str(manifest["repair_mode"])
        )
    )
    for relative, source_path in collected["pt_copy_paths"].items():
        target = (destination / relative).resolve()
        try:
            target.relative_to(destination.resolve())
        except ValueError as exc:
            raise FinalizeError(f"PrimeTime evidence path escapes destination: {relative}") from exc
        _copy(Path(source_path), target)
        canonical_paths[f"primetime_{re.sub(r'[^a-z0-9]+', '_', relative.lower()).strip('_')}"] = relative
    _copy(Path(collected["pt_summary_path"]), destination / "primetime_crosscheck.json")
    canonical_paths["primetime_crosscheck"] = "primetime_crosscheck.json"

    # Explicitly bind the canonical PT inputs, not just their guest source paths.
    for role, relative in (
        ("pt_input_netlist", "fixed.v"),
        ("pt_input_setup_sdc", REPLAY_EVIDENCE_FILES["constraint_setup_after"]),
        ("pt_input_hold_sdc", REPLAY_EVIDENCE_FILES["constraint_hold_after"]),
        (
            "pt_input_setup_sdc_adapted",
            collected["pt_summary"]["sdc_adaptation"]["setup"][
                "adapted_artifact"
            ]["path"],
        ),
        (
            "pt_input_hold_sdc_adapted",
            collected["pt_summary"]["sdc_adaptation"]["hold"][
                "adapted_artifact"
            ]["path"],
        ),
        ("pt_input_setup_spef", "setup_after.spef"),
        ("pt_input_hold_spef", "hold_after.spef"),
    ):
        canonical_paths[role] = relative

    artifact_manifest: dict[str, dict[str, Any]] = {}
    for role, relative in sorted(canonical_paths.items()):
        path = destination / relative
        artifact_manifest[role] = (
            _tree_artifact(path, destination, role)
            if path.is_dir()
            else _artifact(path, destination)
        )
    final_manifest = {
        "id": manifest["id"],
        "type": manifest["type"],
        "difficulty": manifest["difficulty"],
        "design": manifest["design"],
        "tool_version": manifest["tool_version"],
        "analysis_views": manifest["analysis_views"],
        "repair_mode": manifest["repair_mode"],
        "max_eco_cells": int(manifest["catalog_entry"]["repair"]["max_eco_cells"]),
        "status": "gold",
        "finalizer_schema": SCHEMA_VERSION,
        "baseline_provenance": collected["baseline_provenance"],
        "artifacts": artifact_manifest,
    }
    _write_json(destination / "manifest.json", final_manifest)

    # Re-verify the copied PT tree and the exact answer/fix binding before the
    # staging directory can be promoted.
    try:
        ptx.load_and_verify_summary(destination / "primetime_crosscheck.json")
    except ptx.CrosscheckError as exc:
        raise FinalizeError(f"copied PrimeTime evidence failed verification: {exc}") from exc
    fix = _read_text(destination / "fix.tcl", "canonical fix")
    answer = _read_text(destination / "answer.txt", "canonical answer")
    expected_fence = "```tcl\n" + fix + "```"
    if answer.count("```") != 2 or expected_fence not in answer:
        raise FinalizeError(f"{destination}: answer Tcl fence does not exactly bind fix.tcl")


def finalize_case(case_dir: Path, output_root: Path) -> Path:
    """Validate one prepared case and atomically write its canonical directory."""

    collected = _collect_case(case_dir)
    cases_root = output_root.resolve() / "cases"
    cases_root.mkdir(parents=True, exist_ok=True)
    destination = cases_root / str(collected["manifest"]["id"])
    if destination.exists():
        raise FinalizeError(f"refusing to overwrite existing canonical case: {destination}")
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=cases_root))
    try:
        _materialize_case(collected, temporary)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination


def _case_directories(run_dir: Path, requested: Sequence[str]) -> list[Path]:
    cases_root = run_dir.resolve() / "cases"
    if not cases_root.is_dir():
        raise FinalizeError(f"missing prepared cases directory: {cases_root}")
    available = {
        path.parent.name: path.parent for path in sorted(cases_root.glob("*/manifest.json"))
    }
    if not available:
        raise FinalizeError(f"no prepared case manifests under {cases_root}")
    if not requested or requested == ["all"]:
        return [available[name] for name in sorted(available)]
    if "all" in requested:
        raise FinalizeError("--case all cannot be combined with explicit case IDs")
    unknown = sorted(set(requested) - set(available))
    if unknown:
        raise FinalizeError(f"unknown case IDs: {', '.join(unknown)}")
    if len(set(requested)) != len(requested):
        raise FinalizeError("duplicate --case selection")
    return [available[name] for name in requested]


def finalize_run(
    run_dir: Path, output_root: Path, requested: Sequence[str] = ()
) -> list[Path]:
    """Validate selected cases first, then promote all completed staging trees."""

    case_dirs = _case_directories(run_dir, requested)
    collected = [_collect_case(case_dir) for case_dir in case_dirs]
    cases_root = output_root.resolve() / "cases"
    cases_root.mkdir(parents=True, exist_ok=True)
    destinations = [cases_root / str(item["manifest"]["id"]) for item in collected]
    existing = [path for path in destinations if path.exists()]
    if existing:
        raise FinalizeError(
            "refusing to overwrite existing canonical cases: "
            + ", ".join(str(path) for path in existing)
        )

    staging: list[tuple[Path, Path]] = []
    try:
        for item, destination in zip(collected, destinations):
            temporary = Path(
                tempfile.mkdtemp(prefix=f".{destination.name}.", dir=cases_root)
            )
            staging.append((temporary, destination))
            _materialize_case(item, temporary)
        for temporary, destination in staging:
            os.replace(temporary, destination)
        return destinations
    finally:
        for temporary, _ in staging:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="case ID to finalize; repeat it, or omit/use 'all' for every fetched case",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="run every evidence check without writing canonical case directories",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        selected = _case_directories(args.run_dir, args.case)
        if args.validate_only:
            for case_dir in selected:
                _collect_case(case_dir)
            print(f"validated {len(selected)} case(s); no files written")
        else:
            paths = finalize_run(args.run_dir, args.output_root, args.case)
            print(f"finalized {len(paths)} Gold case(s) under {args.output_root / 'cases'}")
    except FinalizeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

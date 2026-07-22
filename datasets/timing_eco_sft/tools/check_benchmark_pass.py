#!/usr/bin/env python3
"""Evaluate a trusted timing-ECO result bundle against the Pilot-10 hard gates.

The candidate supplies only fix.tcl.  The result bundle, reports, hashes, replay
comparison, and PrimeTime crosscheck must be produced by a trusted harness.
This evaluator deliberately scores outcomes instead of comparing against the
Gold Tcl, so an alternative repair may pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


RESULT_SCHEMA = "timing_eco_benchmark_result.v1"
CRITERIA_SCHEMA = "timing_eco_benchmark_acceptance.v1"
FUNCTIONAL_SCHEMA = "timing_eco_functional_audit.v1"
PHYSICAL_SCHEMA = "timing_eco_physical_no_regression.v1"
REPLAY_SCHEMA = "timing_eco_replay_comparison.v1"
PT_SCHEMA = "timing_eco_primetime_crosscheck.v4"


class BundleError(RuntimeError):
    """The bundle is malformed, incomplete, or not hash-trusted."""


def _json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise BundleError(f"{label} must be a JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_inventory(path: Path) -> list[dict[str, Any]]:
    if not path.is_dir() or path.is_symlink():
        raise BundleError(f"artifact tree is not a regular directory: {path}")
    items: list[dict[str, Any]] = []
    for item in sorted(path.rglob("*"), key=lambda candidate: candidate.as_posix()):
        if item.is_symlink():
            raise BundleError(f"artifact tree contains a symlink: {item}")
        if item.is_dir():
            continue
        if not item.is_file():
            raise BundleError(f"artifact tree contains a non-file entry: {item}")
        items.append(
            {
                "path": item.relative_to(path).as_posix(),
                "sha256": _sha256(item),
                "bytes": item.stat().st_size,
            }
        )
    if not items or sum(int(item["bytes"]) for item in items) <= 0:
        raise BundleError(f"artifact tree is empty: {path}")
    return items


def _tree_sha256(items: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in items:
        digest.update(
            f"{item['path']}\0{item['sha256']}\0{item['bytes']}\n".encode("utf-8")
        )
    return digest.hexdigest()


class Artifacts:
    def __init__(self, case_dir: Path, manifest: Mapping[str, Any]) -> None:
        self.case_dir = case_dir.resolve()
        raw = manifest.get("artifacts")
        if not isinstance(raw, Mapping):
            raise BundleError(f"{case_dir}/manifest.json has no artifact object")
        self.raw = raw
        self.verified: list[str] = []

    def get(self, role: str, *, kind: str = "file") -> Path:
        declaration = self.raw.get(role)
        if not isinstance(declaration, Mapping):
            raise BundleError(f"manifest artifact {role!r} lacks a typed hash declaration")
        path_text = declaration.get("path")
        digest = declaration.get("sha256")
        size = declaration.get("bytes")
        declared_kind = declaration.get("kind", "file")
        if not isinstance(path_text, str) or not path_text:
            raise BundleError(f"manifest artifact {role!r} has no path")
        declared = Path(path_text)
        if declared.is_absolute() or ".." in declared.parts:
            raise BundleError(f"manifest artifact {role!r} is not case-relative")
        path = (self.case_dir / declared).resolve()
        try:
            path.relative_to(self.case_dir)
        except ValueError as exc:
            raise BundleError(f"manifest artifact {role!r} escapes the case directory") from exc
        if declared_kind != kind:
            raise BundleError(
                f"manifest artifact {role!r} kind is {declared_kind!r}, expected {kind!r}"
            )
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise BundleError(f"manifest artifact {role!r} has no lowercase SHA256")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise BundleError(f"manifest artifact {role!r} has invalid byte count")
        if kind == "file":
            if not path.is_file() or path.is_symlink():
                raise BundleError(f"manifest artifact {role!r} is missing: {path}")
            actual_size = path.stat().st_size
            actual_digest = _sha256(path)
        else:
            inventory = _tree_inventory(path)
            actual_size = sum(int(item["bytes"]) for item in inventory)
            actual_digest = _tree_sha256(inventory)
            files = declaration.get("files")
            if isinstance(files, bool) or not isinstance(files, int) or files != len(inventory):
                raise BundleError(f"manifest artifact {role!r} file count is stale")
        if actual_size != size or actual_digest != digest:
            raise BundleError(f"manifest artifact {role!r} byte count or SHA256 is stale")
        self.verified.append(role)
        return path


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BundleError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise BundleError(f"{label} must be finite")
    return result


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BundleError(f"{label} must be a non-negative integer")
    return value


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BundleError(f"{label} must be an object")
    return value


def _gate(
    gates: list[dict[str, Any]],
    gate_id: str,
    passed: bool,
    *,
    required: Any,
    observed: Any,
    failures: Sequence[str] = (),
) -> None:
    gates.append(
        {
            "id": gate_id,
            "passed": bool(passed),
            "required": required,
            "observed": observed,
            "failures": list(failures),
        }
    )


def _timing_gate(
    metrics: Mapping[str, Any], criteria: Mapping[str, Any]
) -> tuple[bool, dict[str, Any], list[str]]:
    after = _mapping(metrics.get("after"), "metrics.after")
    observed: dict[str, Any] = {}
    failures: list[str] = []
    for mode in ("setup", "hold"):
        values = _mapping(after.get(mode), f"metrics.after.{mode}")
        rule = _mapping(criteria.get(mode), f"criteria.innovus_timing.{mode}")
        wns = _finite(values.get("wns_ns"), f"after {mode} WNS")
        tns = _finite(values.get("tns_ns"), f"after {mode} TNS")
        wns_min = _finite(rule.get("wns_min_ns"), f"{mode} WNS minimum")
        tns_max = _finite(rule.get("tns_abs_max_ns"), f"{mode} TNS tolerance")
        observed[mode] = {"wns_ns": wns, "tns_ns": tns}
        if wns < wns_min:
            failures.append(f"{mode} WNS {wns} < {wns_min}")
        if abs(tns) > tns_max:
            failures.append(f"{mode} |TNS| {abs(tns)} > {tns_max}")
    return not failures, observed, failures


def _physical_gate(metrics: Mapping[str, Any]) -> tuple[bool, dict[str, Any], list[str]]:
    before = _mapping(metrics.get("before"), "metrics.before")
    after = _mapping(metrics.get("after"), "metrics.after")
    failures: list[str] = []
    drv_delta: dict[str, int] = {}
    before_drv = _mapping(before.get("drv"), "metrics.before.drv")
    after_drv = _mapping(after.get("drv"), "metrics.after.drv")
    for key in (
        "max_transition_violations",
        "max_capacitance_violations",
        "max_fanout_violations",
    ):
        old = _integer(before_drv.get(key), f"before {key}")
        new = _integer(after_drv.get(key), f"after {key}")
        drv_delta[key] = new - old
        if new > old:
            failures.append(f"{key} regressed: {old} -> {new}")
    before_drc = _mapping(before.get("drc"), "metrics.before.drc")
    after_drc = _mapping(after.get("drc"), "metrics.after.drc")
    before_total = _integer(before_drc.get("total"), "before DRC total")
    after_total = _integer(after_drc.get("total"), "after DRC total")
    if after_total > before_total:
        failures.append(f"DRC total regressed: {before_total} -> {after_total}")
    before_categories = _mapping(before_drc.get("categories"), "before DRC categories")
    after_categories = _mapping(after_drc.get("categories"), "after DRC categories")
    category_delta: dict[str, int] = {}
    for category in sorted(set(before_categories) | set(after_categories)):
        old = _integer(before_categories.get(category, 0), f"before DRC {category}")
        new = _integer(after_categories.get(category, 0), f"after DRC {category}")
        category_delta[str(category)] = new - old
        if new > old:
            failures.append(f"DRC category {category} regressed: {old} -> {new}")
    connectivity = _integer(
        _mapping(after.get("connectivity"), "metrics.after.connectivity").get("violations"),
        "after connectivity violations",
    )
    if connectivity != 0:
        failures.append(f"after connectivity violations is {connectivity}, expected 0")
    observed = {
        "drv_delta": drv_delta,
        "drc_total_delta": after_total - before_total,
        "drc_category_delta": category_delta,
        "connectivity_after": connectivity,
    }
    return not failures, observed, failures


def _tcl_words(line: str) -> list[str]:
    tokens: list[str] = []
    cursor = 0
    token = re.compile(r'\s*(\{[^{}]*\}|"[^"\r\n]*"|[^\s]+)')
    while cursor < len(line):
        match = token.match(line, cursor)
        if match is None:
            raise BundleError(f"cannot tokenize candidate Tcl line: {line}")
        raw = match.group(1)
        if raw.startswith("{") and raw.endswith("}"):
            raw = raw[1:-1]
        elif raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1]
        tokens.append(raw)
        cursor = match.end()
    return tokens


def _options(words: Sequence[str], command: str) -> dict[str, str]:
    tail = list(words[1:])
    if len(tail) % 2:
        raise BundleError(f"{command} must contain literal option/value pairs")
    result: dict[str, str] = {}
    for index in range(0, len(tail), 2):
        option, value = tail[index], tail[index + 1]
        if not option.startswith("-") or option in result:
            raise BundleError(f"{command} has malformed or duplicate option {option!r}")
        result[option] = value
    return result


def _literal_name(value: str, label: str) -> None:
    if re.fullmatch(r"[A-Za-z0-9_./]+", value) is None:
        raise BundleError(f"{label} is not a safe literal design object: {value!r}")


def _fix_policy(
    text: str,
    *,
    case_id: str,
    case_type: str,
    budget: int,
    diagnostic: Mapping[str, Any],
) -> tuple[bool, dict[str, Any], list[str]]:
    targets = diagnostic.get("targets")
    if not isinstance(targets, list) or not targets:
        raise BundleError("diagnostic context contains no targets")
    endpoints: set[str] = set()
    local_cells: set[str] = set()
    for index, raw_target in enumerate(targets, 1):
        target = _mapping(raw_target, f"diagnostic target {index}")
        endpoint = target.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            raise BundleError(f"diagnostic target {index} has no endpoint")
        endpoints.add(endpoint)
        cells = target.get("local_cells")
        if not isinstance(cells, list):
            raise BundleError(f"diagnostic target {index} has no local_cells")
        for raw_cell in cells:
            cell = _mapping(raw_cell, "diagnostic local cell")
            inst = cell.get("inst")
            if isinstance(inst, str) and inst:
                local_cells.add(inst)
    failures: list[str] = []
    actions: list[dict[str, str]] = []
    add_names: list[str] = []
    batch_modes: list[str] = []
    leq_modes: list[str] = []
    route_modes: list[str] = []
    batch_open = False
    refine_positions: list[int] = []
    target_route_positions: list[int] = []
    last_action = -1
    command_count = 0
    allowed = {
        "setEcoMode",
        "setNanoRouteMode",
        "ecoChangeCell",
        "ecoAddRepeater",
        "refinePlace",
        "ecoRoute",
    }
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        command_count += 1
        if re.search(r"[;\[\]$\\]", line):
            failures.append(f"line {line_number}: Tcl substitution/multi-command syntax is forbidden")
            continue
        try:
            words = _tcl_words(line)
        except BundleError as exc:
            failures.append(f"line {line_number}: {exc}")
            continue
        command = words[0] if words else ""
        if command not in allowed:
            failures.append(f"line {line_number}: command {command!r} is not whitelisted")
            continue
        try:
            if command == "setEcoMode":
                if len(words) != 3 or words[1] not in {"-batchMode", "-LEQCheck"} or words[2] not in {"true", "false"}:
                    raise BundleError("setEcoMode form is not whitelisted")
                if words[1] == "-batchMode":
                    batch_modes.append(words[2])
                    batch_open = words[2] == "true"
                else:
                    leq_modes.append(words[2])
            elif command == "setNanoRouteMode":
                if words != ["setNanoRouteMode", "-routeWithTimingDriven", words[-1]] or words[-1] not in {"true", "false"}:
                    raise BundleError("only -routeWithTimingDriven true/false is allowed")
                route_modes.append(words[-1])
            elif command == "ecoChangeCell":
                opts = _options(words, command)
                if set(opts) != {"-inst", "-cell"}:
                    raise BundleError("ecoChangeCell requires only -inst and -cell")
                _literal_name(opts["-inst"], "ecoChangeCell instance")
                _literal_name(opts["-cell"], "ecoChangeCell cell")
                if opts["-inst"] not in local_cells:
                    failures.append(
                        f"line {line_number}: instance {opts['-inst']} is absent from diagnostic local_cells"
                    )
                if not batch_open:
                    failures.append(f"line {line_number}: ECO action is outside batch mode")
                actions.append({"command": command, **opts})
                last_action = command_count
            elif command == "ecoAddRepeater":
                opts = _options(words, command)
                if set(opts) not in ({"-term", "-cell", "-name"}, {"-term", "-cell", "-name", "-loc"}):
                    raise BundleError("ecoAddRepeater has non-whitelisted options")
                for option in ("-term", "-cell", "-name"):
                    _literal_name(opts[option], f"ecoAddRepeater {option}")
                if opts["-term"] not in endpoints:
                    failures.append(
                        f"line {line_number}: term {opts['-term']} is not a diagnostic endpoint"
                    )
                if "-loc" in opts:
                    coords = opts["-loc"].split()
                    if len(coords) != 2 or not all(math.isfinite(float(value)) for value in coords):
                        raise BundleError("ecoAddRepeater -loc must contain two finite numbers")
                if not batch_open:
                    failures.append(f"line {line_number}: ECO action is outside batch mode")
                add_names.append(opts["-name"])
                actions.append({"command": command, **opts})
                last_action = command_count
            elif command == "refinePlace":
                if words != ["refinePlace", "-eco", "true"]:
                    raise BundleError("refinePlace must be exactly -eco true")
                if batch_open:
                    failures.append(f"line {line_number}: refinePlace executed before batch close")
                refine_positions.append(command_count)
            elif command == "ecoRoute":
                if words == ["ecoRoute", "-target"]:
                    target_route_positions.append(command_count)
                elif len(words) == 3 and words[1] == "-fix_drc":
                    coords = words[2].split()
                    if len(coords) != 4 or not all(math.isfinite(float(value)) for value in coords):
                        raise BundleError("ecoRoute -fix_drc must contain four finite numbers")
                else:
                    raise BundleError("ecoRoute form is not whitelisted")
                if batch_open:
                    failures.append(f"line {line_number}: ecoRoute executed before batch close")
        except (BundleError, ValueError) as exc:
            failures.append(f"line {line_number}: {exc}")
    if not actions:
        failures.append("candidate contains no ECO action")
    if len(actions) > budget:
        failures.append(f"ECO action count {len(actions)} exceeds budget {budget}")
    if batch_modes != ["true", "false"] or batch_open:
        failures.append(f"batch mode sequence is {batch_modes}, expected ['true', 'false']")
    if leq_modes and leq_modes != ["false", "true"]:
        failures.append(f"LEQCheck sequence is {leq_modes}, expected ['false', 'true']")
    if route_modes and route_modes != ["false", "true"]:
        failures.append(
            f"routeWithTimingDriven sequence is {route_modes}, expected ['false', 'true']"
        )
    if len(refine_positions) != 1 or len(target_route_positions) != 1:
        failures.append("candidate requires exactly one refinePlace -eco true and one ecoRoute -target")
    elif not (last_action < refine_positions[0] < target_route_positions[0]):
        failures.append("incremental placement/target route must follow all ECO actions in order")
    allowed_roles = {"SETUP", "HOLD"} if case_type == "mixed" else {case_type.upper()}
    name_pattern = re.compile(
        rf"^SFT_ECO_{re.escape(case_id)}_(SETUP|HOLD)_([1-9][0-9]*)$"
    )
    for index, name in enumerate(add_names, 1):
        match = name_pattern.fullmatch(name)
        if match is None or match.group(1) not in allowed_roles or int(match.group(2)) != index:
            failures.append(
                f"new instance {name!r} violates role/contiguous ordinal convention at #{index}"
            )
    if len(add_names) != len(set(add_names)):
        failures.append("new ECO instance names are not unique")
    observed = {
        "commands": command_count,
        "eco_actions": len(actions),
        "budget": budget,
        "new_instances": add_names,
    }
    return not failures, observed, failures


def _before_fingerprint(
    reference_manifest: Mapping[str, Any],
    reference_metrics: Mapping[str, Any],
    reference_artifacts: Artifacts,
    result_manifest: Mapping[str, Any],
    result_metrics: Mapping[str, Any],
    result_artifacts: Artifacts,
    tolerance: float,
) -> tuple[bool, dict[str, Any], list[str]]:
    failures: list[str] = []
    for key in ("id", "type", "design", "tool_version", "max_eco_cells"):
        if result_manifest.get(key) != reference_manifest.get(key):
            failures.append(
                f"manifest {key} differs: {result_manifest.get(key)!r} != {reference_manifest.get(key)!r}"
            )
    ref_before = _mapping(reference_metrics.get("before"), "reference metrics.before")
    got_before = _mapping(result_metrics.get("before"), "result metrics.before")
    timing_delta: dict[str, dict[str, float]] = {}
    for mode in ("setup", "hold"):
        timing_delta[mode] = {}
        ref_mode = _mapping(ref_before.get(mode), f"reference before {mode}")
        got_mode = _mapping(got_before.get(mode), f"result before {mode}")
        for metric in ("wns_ns", "tns_ns"):
            delta = _finite(got_mode.get(metric), f"result before {mode} {metric}") - _finite(
                ref_mode.get(metric), f"reference before {mode} {metric}"
            )
            timing_delta[mode][metric] = delta
            if abs(delta) > tolerance:
                failures.append(f"before {mode} {metric} delta {delta} exceeds {tolerance}")
    for key in ("drv", "drc", "connectivity"):
        if got_before.get(key) != ref_before.get(key):
            failures.append(f"before {key} differs from the reference case")
    ref_diag = reference_artifacts.get("diagnostic_context")
    got_diag = result_artifacts.get("diagnostic_context")
    if _sha256(ref_diag) != _sha256(got_diag):
        failures.append("diagnostic_context differs from the reference case")
    constraint_hashes: dict[str, str] = {}
    for mode in ("setup", "hold"):
        role = f"constraint_{mode}_before"
        ref_path = reference_artifacts.get(role)
        got_path = result_artifacts.get(role)
        ref_hash = _sha256(ref_path)
        got_hash = _sha256(got_path)
        constraint_hashes[mode] = got_hash
        if got_hash != ref_hash:
            failures.append(f"{mode} before constraint differs from the reference case")
    return (
        not failures,
        {"timing_delta_ns": timing_delta, "constraint_sha256": constraint_hashes},
        failures,
    )


def _functional_gate(
    audit: Mapping[str, Any],
    *,
    criteria: Mapping[str, Any],
    case_id: str,
    design: str,
    budget: int,
    action_count: int,
) -> tuple[bool, dict[str, Any], list[str]]:
    failures: list[str] = []
    if audit.get("schema_version") != FUNCTIONAL_SCHEMA:
        failures.append("functional audit schema is unsupported")
    if audit.get("case_id") != case_id or audit.get("design") != design:
        failures.append("functional audit identity differs from the result manifest")
    if audit.get("passed") is not True:
        failures.append("functional audit passed is not true")
    checks = _mapping(audit.get("checks"), "functional audit checks")
    required = criteria.get("required_functional_audit_checks")
    if not isinstance(required, list):
        raise BundleError("criteria required_functional_audit_checks must be a list")
    failed_checks: list[str] = []
    for name in required:
        item = checks.get(name)
        if not isinstance(item, Mapping) or item.get("passed") is not True:
            failed_checks.append(str(name))
    if failed_checks:
        failures.append(f"functional checks are not passing: {', '.join(failed_checks)}")
    evidence = _mapping(audit.get("evidence"), "functional audit evidence")
    if evidence.get("max_eco_cells") != budget:
        failures.append("functional audit budget differs from the reference budget")
    if evidence.get("manual_operations") != action_count:
        failures.append("functional audit operation count differs from candidate Tcl")
    operations = audit.get("operations")
    if not isinstance(operations, list) or len(operations) != action_count:
        failures.append("functional audit operations list differs from candidate Tcl")
    observed = {
        "required_checks": len(required),
        "failed_checks": failed_checks,
        "manual_operations": evidence.get("manual_operations"),
        "max_eco_cells": evidence.get("max_eco_cells"),
    }
    return not failures, observed, failures


def _physical_audit_gate(
    audit: Mapping[str, Any], metrics: Mapping[str, Any], case_id: str
) -> tuple[bool, dict[str, Any], list[str]]:
    failures: list[str] = []
    if audit.get("schema_version") != PHYSICAL_SCHEMA or audit.get("case_id") != case_id:
        failures.append("physical no-regression audit identity/schema is invalid")
    if audit.get("passed") is not True or audit.get("per_category_no_regression") is not True:
        failures.append("physical no-regression audit does not explicitly pass per category")
    before = _mapping(metrics.get("before"), "metrics.before")
    after = _mapping(metrics.get("after"), "metrics.after")

    def drv(stage: Mapping[str, Any]) -> dict[str, int]:
        values = _mapping(stage.get("drv"), "metrics drv")
        return {
            "max_transition": _integer(values.get("max_transition_violations"), "max_transition"),
            "max_capacitance": _integer(values.get("max_capacitance_violations"), "max_capacitance"),
            "max_fanout": _integer(values.get("max_fanout_violations"), "max_fanout"),
        }

    expected = {
        "drv_before": drv(before),
        "drv_after": drv(after),
        "drc_before": before.get("drc"),
        "drc_after": after.get("drc"),
    }
    for key, value in expected.items():
        if audit.get(key) != value:
            failures.append(f"physical audit {key} differs from metrics")
    return not failures, {"matched_metrics": not failures}, failures


def _constraint_gate(artifacts: Artifacts) -> tuple[bool, dict[str, Any], list[str]]:
    failures: list[str] = []
    hashes: dict[str, dict[str, str]] = {}
    for mode in ("setup", "hold"):
        before = artifacts.get(f"constraint_{mode}_before")
        after = artifacts.get(f"constraint_{mode}_after")
        before_hash = _sha256(before)
        after_hash = _sha256(after)
        hashes[mode] = {"before": before_hash, "after": after_hash}
        if before.read_bytes() != after.read_bytes():
            failures.append(f"{mode} before/after constraint bytes differ")
    return not failures, hashes, failures


def _check_design_gate(
    directory: Path, *, design: str, labels: Sequence[str]
) -> tuple[bool, dict[str, Any], list[str]]:
    primary = directory / f"{design}.main.htm.ascii"
    if not primary.is_file():
        raise BundleError(f"checkDesign primary report is missing: {primary}")
    text = primary.read_text(encoding="utf-8", errors="replace")
    failures: list[str] = []
    counts: dict[str, Any] = {}
    for label in labels:
        values = re.findall(rf"(?mi)^\s*{re.escape(label)}\s*:\s*(\d+)\s*$", text)
        counts[label] = values
        if values != ["0"]:
            failures.append(f"checkDesign {label!r} is missing, duplicate, or nonzero: {values}")
    return not failures, counts, failures


def _same_hash_list(value: Any, runs: int) -> bool:
    return (
        isinstance(value, list)
        and len(value) == runs
        and all(isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item) for item in value)
        and len(set(value)) == 1
    )


def _replay_gate(
    comparison: Mapping[str, Any],
    metrics: Mapping[str, Any],
    *,
    case_id: str,
    runs: int,
    tolerance: float,
    fix_sha256: str,
    audit_sha256: str,
    physical_sha256: str,
    diagnostic_sha256: str,
    marker_paths: Sequence[Path],
) -> tuple[bool, dict[str, Any], list[str]]:
    failures: list[str] = []
    if comparison.get("schema_version") != REPLAY_SCHEMA or comparison.get("case_id") != case_id:
        failures.append("replay comparison identity/schema is invalid")
    if comparison.get("status") != "passed" or comparison.get("deterministic") is not True:
        failures.append("replay comparison is not deterministic/passed")
    if comparison.get("runs") != runs:
        failures.append(f"replay comparison runs is not {runs}")
    replay = _mapping(metrics.get("replay"), "metrics.replay")
    if replay.get("status") != "passed" or replay.get("runs") != runs or replay.get("deterministic") is not True:
        failures.append("metrics replay summary is not passing/deterministic")
    if comparison.get("concrete_fix_sha256") != fix_sha256:
        failures.append("replay comparison is not bound to candidate fix.tcl")
    functional_hashes = comparison.get("functional_audit_sha256")
    if not _same_hash_list(functional_hashes, runs) or functional_hashes[0] != audit_sha256:
        failures.append("functional audit hashes differ across replays or from replay 1")
    if comparison.get("physical_no_regression_sha256") != physical_sha256:
        failures.append("physical audit hash differs from replay comparison")
    if comparison.get("diagnostic_context_sha256") != diagnostic_sha256:
        failures.append("diagnostic context hash differs from replay comparison")
    for field in (
        "semantic_evidence_sha256",
        "injection_provenance_sha256",
        "success_marker_sha256",
    ):
        if not _same_hash_list(comparison.get(field), runs):
            failures.append(f"{field} is not identical across {runs} replays")
    deltas = comparison.get("timing_deltas_ns")
    max_delta = 0.0
    if not isinstance(deltas, Mapping):
        failures.append("timing replay deltas are missing")
    else:
        for stage in ("before", "after"):
            stage_value = _mapping(deltas.get(stage), f"replay delta {stage}")
            for mode in ("setup", "hold"):
                mode_value = _mapping(stage_value.get(mode), f"replay delta {stage}/{mode}")
                for metric in ("wns_ns", "tns_ns"):
                    delta = abs(_finite(mode_value.get(metric), f"replay delta {stage}/{mode}/{metric}"))
                    max_delta = max(max_delta, delta)
                    if delta > tolerance:
                        failures.append(
                            f"replay {stage}/{mode}/{metric} delta {delta} exceeds {tolerance}"
                        )
    marker_expected = f"{case_id} replay completed\n".encode("utf-8")
    for path in marker_paths:
        if path.read_bytes() != marker_expected:
            failures.append(f"success marker has wrong content: {path}")
    return not failures, {"runs": runs, "max_timing_delta_ns": max_delta}, failures


def _fast_replay_gate(
    metrics: Mapping[str, Any], marker: Path, case_id: str
) -> tuple[bool, dict[str, Any], list[str]]:
    failures: list[str] = []
    replay = _mapping(metrics.get("replay"), "metrics.replay")
    runs = replay.get("runs")
    if replay.get("status") != "passed" or not isinstance(runs, int) or runs < 1:
        failures.append("RL-fast result has no passing Innovus replay")
    expected = f"{case_id} replay completed\n".encode("utf-8")
    if marker.read_bytes() != expected:
        failures.append("RL-fast success marker has wrong content")
    return not failures, {"runs": runs}, failures


def _primetime_gate(
    crosscheck: Mapping[str, Any],
    metrics: Mapping[str, Any],
    *,
    design: str,
    criteria: Mapping[str, Any],
) -> tuple[bool, dict[str, Any], list[str]]:
    failures: list[str] = []
    if crosscheck.get("schema_version") != PT_SCHEMA or crosscheck.get("passed") is not True:
        failures.append("PrimeTime crosscheck schema/status is not passing")
    policy = _mapping(crosscheck.get("policy"), "PrimeTime policy")
    required_version = criteria.get("required_tool_version")
    if policy.get("required_tool_version") != required_version:
        failures.append("PrimeTime policy tool version differs from criteria")
    threshold = _finite(criteria.get("wns_min_ns"), "PrimeTime WNS minimum")
    tns_max = _finite(criteria.get("tns_abs_max_ns"), "PrimeTime TNS tolerance")
    corners = _mapping(crosscheck.get("corners"), "PrimeTime corners")
    observed: dict[str, Any] = {}
    for mode, delay_key in (("setup", "setup_delay_type"), ("hold", "hold_delay_type")):
        corner = _mapping(corners.get(mode), f"PrimeTime {mode} corner")
        wns = _finite(corner.get("wns_ns"), f"PrimeTime {mode} WNS")
        tns = _finite(corner.get("tns_ns"), f"PrimeTime {mode} TNS")
        observed[mode] = {"wns_ns": wns, "tns_ns": tns}
        expected = {
            "status": "PASS",
            "tool_version": required_version,
            "top": design,
            "delay_type": criteria.get(delay_key),
            "violating_paths": criteria.get("violating_paths"),
            "parasitics_read": criteria.get("parasitics_read"),
            "propagated_clocks": criteria.get("propagated_clocks"),
        }
        for key, value in expected.items():
            if corner.get(key) != value:
                failures.append(f"PrimeTime {mode} {key} differs: {corner.get(key)!r} != {value!r}")
        if wns < threshold:
            failures.append(f"PrimeTime {mode} WNS {wns} < {threshold}")
        if abs(tns) > tns_max:
            failures.append(f"PrimeTime {mode} |TNS| {abs(tns)} > {tns_max}")
    execution = _mapping(crosscheck.get("execution"), "PrimeTime execution")
    for mode in ("setup", "hold"):
        if _mapping(execution.get(mode), f"PrimeTime execution {mode}").get("exit_code") != 0:
            failures.append(f"PrimeTime {mode} process did not exit 0")
    checks = _mapping(metrics.get("checks"), "metrics.checks")
    if checks.get("primetime_crosscheck_passed") is not True:
        failures.append("metrics does not mark PrimeTime crosscheck passed")
    return not failures, observed, failures


def evaluate_case(
    reference_case: Path,
    result_dir: Path,
    criteria_path: Path,
    profile_name: str,
) -> dict[str, Any]:
    criteria = _json(criteria_path, "benchmark criteria")
    if criteria.get("schema_version") != CRITERIA_SCHEMA:
        raise BundleError(f"unsupported criteria schema in {criteria_path}")
    profiles = _mapping(criteria.get("profiles"), "criteria.profiles")
    profile = _mapping(profiles.get(profile_name), f"criteria profile {profile_name}")
    reference_case = reference_case.resolve()
    result_dir = result_dir.resolve()
    reference_manifest = _json(reference_case / "manifest.json", "reference manifest")
    result_manifest = _json(result_dir / "manifest.json", "result manifest")
    case_id = result_manifest.get("id")
    case_type = result_manifest.get("type")
    design = result_manifest.get("design")
    budget = result_manifest.get("max_eco_cells")
    if not isinstance(case_id, str) or not isinstance(case_type, str) or not isinstance(design, str):
        raise BundleError("result manifest identity fields are missing")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise BundleError("result manifest max_eco_cells is invalid")
    if case_type not in {"setup", "hold", "mixed"}:
        raise BundleError(f"unsupported case type: {case_type!r}")
    reference_artifacts = Artifacts(reference_case, reference_manifest)
    result_artifacts = Artifacts(result_dir, result_manifest)
    common_roles = [
        "metrics",
        "fix_tcl",
        "functional_audit",
        "physical_no_regression",
        "diagnostic_context",
        "constraint_setup_before",
        "constraint_setup_after",
        "constraint_hold_before",
        "constraint_hold_after",
        "success_marker",
    ]
    result_paths = {role: result_artifacts.get(role) for role in common_roles}
    result_paths["check_design_after"] = result_artifacts.get(
        "check_design_after", kind="directory"
    )
    reference_paths = {
        role: reference_artifacts.get(role)
        for role in (
            "metrics",
            "diagnostic_context",
            "constraint_setup_before",
            "constraint_hold_before",
        )
    }
    if profile.get("require_replay_determinism") is True:
        result_paths["replay_comparison"] = result_artifacts.get("replay_comparison")
        result_paths["replay_2_success_marker"] = result_artifacts.get(
            "replay_2_success_marker"
        )
    if profile.get("require_primetime") is True:
        result_paths["primetime_crosscheck"] = result_artifacts.get(
            "primetime_crosscheck"
        )
    result_metrics = _json(result_paths["metrics"], "result metrics")
    reference_metrics = _json(reference_paths["metrics"], "reference metrics")
    diagnostic = _json(result_paths["diagnostic_context"], "diagnostic context")
    audit = _json(result_paths["functional_audit"], "functional audit")
    physical_audit = _json(result_paths["physical_no_regression"], "physical audit")
    fix_text = result_paths["fix_tcl"].read_text(encoding="utf-8")
    gates: list[dict[str, Any]] = []

    start_spec = _mapping(criteria.get("start_state"), "criteria.start_state")
    start_ok, start_observed, start_failures = _before_fingerprint(
        reference_manifest,
        reference_metrics,
        reference_artifacts,
        result_manifest,
        result_metrics,
        result_artifacts,
        _finite(start_spec.get("timing_tolerance_ns"), "start timing tolerance"),
    )
    _gate(
        gates,
        "start_state_match",
        start_ok,
        required="reference case fingerprint within 1 ps for timing and exact otherwise",
        observed=start_observed,
        failures=start_failures,
    )

    policy_ok, policy_observed, policy_failures = _fix_policy(
        fix_text,
        case_id=case_id,
        case_type=case_type,
        budget=budget,
        diagnostic=diagnostic,
    )
    _gate(
        gates,
        "candidate_policy",
        policy_ok,
        required="whitelisted local surgical ECO within cell budget",
        observed=policy_observed,
        failures=policy_failures,
    )

    timing_spec = _mapping(criteria.get("innovus_timing"), "criteria.innovus_timing")
    timing_ok, timing_observed, timing_failures = _timing_gate(result_metrics, timing_spec)
    _gate(
        gates,
        "innovus_timing",
        timing_ok,
        required=timing_spec,
        observed=timing_observed,
        failures=timing_failures,
    )

    physical_ok, physical_observed, physical_failures = _physical_gate(result_metrics)
    _gate(
        gates,
        "physical_no_regression",
        physical_ok,
        required="DRV, total DRC, every DRC category non-increasing; connectivity=0",
        observed=physical_observed,
        failures=physical_failures,
    )

    constraint_ok, constraint_observed, constraint_failures = _constraint_gate(
        result_artifacts
    )
    checks = _mapping(result_metrics.get("checks"), "metrics.checks")
    if checks.get("constraint_hash_unchanged") is not True:
        constraint_ok = False
        constraint_failures.append("metrics constraint_hash_unchanged is not true")
    _gate(
        gates,
        "constraints_unchanged",
        constraint_ok,
        required="setup and hold before/after SDC bytes identical",
        observed=constraint_observed,
        failures=constraint_failures,
    )

    functional_spec = _mapping(
        criteria.get("logical_and_constraint_integrity"),
        "criteria.logical_and_constraint_integrity",
    )
    functional_ok, functional_observed, functional_failures = _functional_gate(
        audit,
        criteria=functional_spec,
        case_id=case_id,
        design=design,
        budget=budget,
        action_count=int(policy_observed["eco_actions"]),
    )
    if checks.get("functional_audit_passed") is not True:
        functional_ok = False
        functional_failures.append("metrics functional_audit_passed is not true")
    _gate(
        gates,
        "functional_integrity",
        functional_ok,
        required="all typed functional audit checks pass and operation count matches Tcl",
        observed=functional_observed,
        failures=functional_failures,
    )

    physical_audit_ok, physical_audit_observed, physical_audit_failures = (
        _physical_audit_gate(physical_audit, result_metrics, case_id)
    )
    _gate(
        gates,
        "typed_physical_audit",
        physical_audit_ok,
        required="typed physical audit exactly matches parsed metrics",
        observed=physical_audit_observed,
        failures=physical_audit_failures,
    )

    labels = functional_spec.get("check_design_zero_count_labels")
    if not isinstance(labels, list) or not all(isinstance(item, str) for item in labels):
        raise BundleError("check_design_zero_count_labels must be a string list")
    check_ok, check_observed, check_failures = _check_design_gate(
        result_paths["check_design_after"], design=design, labels=labels
    )
    _gate(
        gates,
        "check_design",
        check_ok,
        required="five critical checkDesign counts equal zero",
        observed=check_observed,
        failures=check_failures,
    )

    if profile.get("require_replay_determinism") is True:
        runs = profile.get("required_innovus_replays")
        if isinstance(runs, bool) or not isinstance(runs, int) or runs < 2:
            raise BundleError("official profile replay count is invalid")
        replay = _json(result_paths["replay_comparison"], "replay comparison")
        replay_spec = _mapping(criteria.get("replay_determinism"), "replay criteria")
        replay_ok, replay_observed, replay_failures = _replay_gate(
            replay,
            result_metrics,
            case_id=case_id,
            runs=runs,
            tolerance=_finite(replay_spec.get("timing_tolerance_ns"), "replay tolerance"),
            fix_sha256=_sha256(result_paths["fix_tcl"]),
            audit_sha256=_sha256(result_paths["functional_audit"]),
            physical_sha256=_sha256(result_paths["physical_no_regression"]),
            diagnostic_sha256=_sha256(result_paths["diagnostic_context"]),
            marker_paths=[
                result_paths["success_marker"],
                result_paths["replay_2_success_marker"],
            ],
        )
        _gate(
            gates,
            "replay_determinism",
            replay_ok,
            required=f"{runs} deterministic fresh Innovus replays within 1 ps",
            observed=replay_observed,
            failures=replay_failures,
        )
    else:
        fast_ok, fast_observed, fast_failures = _fast_replay_gate(
            result_metrics, result_paths["success_marker"], case_id
        )
        _gate(
            gates,
            "innovus_replay_available",
            fast_ok,
            required="at least one trusted passing Innovus replay",
            observed=fast_observed,
            failures=fast_failures,
        )

    if profile.get("require_primetime") is True:
        crosscheck = _json(result_paths["primetime_crosscheck"], "PrimeTime crosscheck")
        pt_spec = _mapping(
            criteria.get("independent_primetime"), "PrimeTime criteria"
        )
        pt_ok, pt_observed, pt_failures = _primetime_gate(
            crosscheck,
            result_metrics,
            design=design,
            criteria=pt_spec,
        )
        _gate(
            gates,
            "primetime_crosscheck",
            pt_ok,
            required=pt_spec,
            observed=pt_observed,
            failures=pt_failures,
        )

    _gate(
        gates,
        "artifact_integrity",
        True,
        required="case-relative manifest byte counts and SHA256 match all consumed artifacts",
        observed={
            "result_roles": sorted(set(result_artifacts.verified)),
            "reference_roles": sorted(set(reference_artifacts.verified)),
        },
    )
    passed = all(gate["passed"] for gate in gates)
    failed_gates = [gate["id"] for gate in gates if not gate["passed"]]
    return {
        "schema_version": RESULT_SCHEMA,
        "criteria_id": criteria.get("criteria_id"),
        "profile": profile_name,
        "decision_label": profile.get("decision_label") if passed else "FAIL",
        "case_id": case_id,
        "passed": passed,
        "official_benchmark_pass": bool(
            passed and profile.get("official_benchmark_pass") is True
        ),
        "valid_bundle": True,
        "binary_reward": 1.0 if passed else 0.0,
        "failed_gates": failed_gates,
        "gates": gates,
        "observations": {
            "innovus_after": timing_observed,
            "physical_delta": physical_observed,
            "eco_actions": policy_observed["eco_actions"],
            "max_eco_cells": budget,
        },
        "classification": criteria.get("classification"),
    }


def _default_criteria() -> Path:
    return Path(__file__).resolve().parents[1] / "pilot_10" / "benchmark_pass_criteria.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-case", required=True, type=Path)
    parser.add_argument("--result-dir", required=True, type=Path)
    parser.add_argument("--criteria", type=Path, default=_default_criteria())
    parser.add_argument("--profile", choices=("rl_fast", "official"), default="official")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compact", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = evaluate_case(
            args.reference_case, args.result_dir, args.criteria, args.profile
        )
        exit_code = 0 if result["passed"] else 1
    except BundleError as exc:
        result = {
            "schema_version": RESULT_SCHEMA,
            "criteria_id": None,
            "profile": args.profile,
            "decision_label": "INVALID_BUNDLE",
            "case_id": args.result_dir.name,
            "passed": False,
            "official_benchmark_pass": False,
            "valid_bundle": False,
            "binary_reward": 0.0,
            "failed_gates": ["bundle_integrity"],
            "gates": [],
            "errors": [str(exc)],
        }
        exit_code = 2
    text = json.dumps(
        result,
        ensure_ascii=False,
        indent=None if args.compact else 2,
        sort_keys=True,
    ) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

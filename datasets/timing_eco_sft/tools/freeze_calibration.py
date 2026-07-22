#!/usr/bin/env python3
"""Freeze one pilot-10 injection only from complete NOT_GOLD evidence.

The NOT_GOLD runtime marker means that one candidate was measured; it does
*not* mean that the candidate hit its window.  This tool therefore validates
the fetched real-tool evidence and its prepared-catalog/baseline bindings
before changing ``injection.calibration_status``.  It never invokes EDA.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence


SCRIPT = Path(__file__).resolve()
TOOLS_ROOT = SCRIPT.parent
PILOT_ROOT = TOOLS_ROOT.parent / "pilot_10"
DEFAULT_CATALOG = PILOT_ROOT / "catalog.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_CELL_REFERENCE_RE = re.compile(r"^[A-Za-z0-9_.]+$")
DATA_DELAY_INJECTION_STRATEGIES = {
    "insert_data_delay",
    "mixed_data_delay_and_capture_skew",
}
CAPTURE_CLOCK_INJECTION_STRATEGIES = {
    "local_capture_clock_delay",
    "mixed_data_delay_and_capture_skew",
}
CALIBRATION_STATUS_SCHEMA = "timing_eco_calibration_status.v1"
INJECTION_PROVENANCE_SCHEMA = "timing_eco_injection_provenance.v1"
NUMERIC_EPSILON = 1.0e-9
LOCALITY_INTERNAL_TOLERANCE_NS = 1.0e-6
MAX_JSON_BYTES = 256 * 1024
MAX_TCL_BYTES = 1024 * 1024


class FreezeError(RuntimeError):
    """A fail-closed calibration evidence or output error."""


def _load_sibling(module_name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        f"timing_eco_freezer_{module_name}", TOOLS_ROOT / filename
    )
    if spec is None or spec.loader is None:  # pragma: no cover - installation error
        raise RuntimeError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run_pilot = _load_sibling("run_pilot", "run_pilot.py")
finalizer = _load_sibling("finalize_pilot", "finalize_pilot.py")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _required_file(path: Path, description: str, *, max_bytes: int | None = None) -> Path:
    try:
        stat = path.lstat()
    except FileNotFoundError as exc:
        raise FreezeError(f"missing {description}: {path}") from exc
    if path.is_symlink() or not path.is_file():
        raise FreezeError(f"{description} must be a regular non-symlink file: {path}")
    if stat.st_size <= 0:
        raise FreezeError(f"{description} is empty: {path}")
    if max_bytes is not None and stat.st_size > max_bytes:
        raise FreezeError(f"{description} exceeds {max_bytes} bytes: {path}")
    return path


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FreezeError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path, description: str) -> Any:
    _required_file(path, description, max_bytes=MAX_JSON_BYTES)
    try:
        return json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except UnicodeDecodeError as exc:
        raise FreezeError(f"{description} is not UTF-8: {path}") from exc
    except json.JSONDecodeError as exc:
        raise FreezeError(f"invalid JSON in {description} {path}: {exc}") from exc


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FreezeError(f"{where} must be an object")
    return value


def _finite(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FreezeError(f"{where} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise FreezeError(f"{where} must be finite")
    return result


def _integer(value: Any, where: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise FreezeError(f"{where} must be an integer >= {minimum}")
    return value


def _valid_sha(value: Any, where: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise FreezeError(f"{where} must be a lowercase SHA256")
    return value


def _catalog(path: Path) -> dict[str, Any]:
    # Strict-load first so duplicate JSON keys cannot be hidden from the
    # shared catalog validator.
    _load_json(path, "catalog")
    try:
        return run_pilot.load_catalog(path)
    except run_pilot.PilotError as exc:
        raise FreezeError(str(exc)) from exc


def _case_by_id(catalog: Mapping[str, Any], case_id: str) -> dict[str, Any]:
    matches = [case for case in catalog["cases"] if case["id"] == case_id]
    if len(matches) != 1:
        raise FreezeError(f"catalog must contain exactly one case {case_id}")
    return dict(matches[0])


def _case_without_freeze_state(case: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize only case fields that calibration freezing may change."""

    normalized = copy.deepcopy(dict(case))
    injection = normalized["injection"]
    injection["calibration_status"] = "PROBE_REQUIRED"
    if injection["strategy"] in CAPTURE_CLOCK_INJECTION_STRATEGIES:
        injection["hold_parameters"]["clock_cell_reference"] = "PROBE_REQUIRED"
    if injection["strategy"] in DATA_DELAY_INJECTION_STRATEGIES:
        # Old prepared calibration catalogs omit this field.  Treat omission,
        # the probe placeholder, and a reference frozen by an earlier stage as
        # the same mutable state while comparing all other candidate bytes.
        injection["setup_parameters"][
            "delay_cell_reference"
        ] = "PROBE_REQUIRED"
    return normalized


def _without_freeze_state(catalog: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize only fields that this tool is allowed to change."""

    normalized = copy.deepcopy(dict(catalog))
    normalized["cases"] = [
        _case_without_freeze_state(case) for case in normalized["cases"]
    ]
    return normalized


def _resolve_replay_dir(path: Path) -> Path:
    candidate = path.resolve()
    if (candidate / "reports").is_dir() and (candidate / "run_status.json").is_file():
        return candidate
    runs = candidate / "runs"
    if runs.is_dir():
        replays = sorted(item for item in runs.iterdir() if item.name.startswith("replay_"))
        if [item.name for item in replays] != ["replay_1"]:
            raise FreezeError(
                f"NOT_GOLD calibration case must contain only runs/replay_1: {candidate}"
            )
        if not replays[0].is_dir() or replays[0].is_symlink():
            raise FreezeError(f"calibration replay must be a non-symlink directory: {replays[0]}")
        return replays[0].resolve()
    raise FreezeError(
        "--calibration-case-dir must be a fetched replay directory or a prepared "
        f"case directory containing only runs/replay_1: {candidate}"
    )


def _find_run_root(replay_dir: Path) -> Path:
    for parent in replay_dir.parents:
        if (parent / "run_manifest.json").is_file() and (parent / "catalog.json").is_file():
            return parent
    raise FreezeError(
        f"cannot find prepared run_manifest.json and catalog.json above {replay_dir}"
    )


def _expected_baseline_binding(manifest: Mapping[str, Any]) -> dict[str, str]:
    qualification = _mapping(
        manifest.get("baseline_qualification"), "case manifest.baseline_qualification"
    )
    expected = {
        "baseline_sha256": _valid_sha(
            manifest.get("baseline_sha256"), "case manifest.baseline_sha256"
        ),
        "catalog_sha256": _valid_sha(
            manifest.get("catalog_sha256"), "case manifest.catalog_sha256"
        ),
        "qualification_sha256": _valid_sha(
            qualification.get("sha256"), "case manifest baseline qualification SHA256"
        ),
        "checksum_manifest_sha256": _valid_sha(
            manifest.get("baseline_checksum_manifest_sha256"),
            "case manifest baseline checksum-manifest SHA256",
        ),
        "qualification_status": str(qualification.get("status", "")),
        "technology_classification": str(
            qualification.get("technology_classification", "")
        ),
    }
    if expected["qualification_status"] != "QUALIFIED_CANDIDATE":
        raise FreezeError("calibration is not bound to a QUALIFIED_CANDIDATE baseline")
    if expected["technology_classification"] != "derived_non_signoff":
        raise FreezeError("calibration technology classification is not derived_non_signoff")
    if qualification.get("gold_status") is not False or qualification.get(
        "signoff_eligible"
    ) is not False:
        raise FreezeError("calibration baseline qualification flags are inconsistent")
    return expected


def _validate_run_contract(
    replay_dir: Path,
    run_root: Path,
    case_id: str,
    prepared_case: Mapping[str, Any],
) -> tuple[dict[str, str], Mapping[str, Any]]:
    manifest = _mapping(
        _load_json(replay_dir / "manifest.json", "prepared case manifest"),
        "prepared case manifest",
    )
    if manifest.get("id") != case_id or manifest.get("catalog_entry") != prepared_case:
        raise FreezeError("replay manifest is not bound to the exact prepared case entry")
    if manifest.get("status") != "NOT_GOLD_CALIBRATION_PREPARED" or manifest.get(
        "gold_eligible"
    ) is not False:
        raise FreezeError("case manifest is not an explicitly NOT_GOLD prepared case")
    if manifest.get("type") != prepared_case["type"] or manifest.get(
        "repair_mode"
    ) != prepared_case["repair_mode"]:
        raise FreezeError("case manifest type/repair_mode differs from the prepared catalog")
    binding = _expected_baseline_binding(manifest)

    run_manifest = _mapping(
        _load_json(run_root / "run_manifest.json", "prepared run manifest"),
        "prepared run manifest",
    )
    if run_manifest.get("schema_version") != "timing_eco_pilot_run.v1":
        raise FreezeError("unsupported prepared run manifest schema")
    if run_manifest.get("status") != "NOT_GOLD_CALIBRATION_PREPARED" or run_manifest.get(
        "gold_eligible"
    ) is not False:
        raise FreezeError("prepared run is not explicitly NOT_GOLD")
    if case_id not in run_manifest.get("case_ids", []):
        raise FreezeError(f"prepared run manifest does not contain {case_id}")
    run_catalog = _mapping(run_manifest.get("catalog"), "run manifest.catalog")
    if run_catalog.get("sha256") != binding["catalog_sha256"]:
        raise FreezeError("run and case manifests disagree on catalog SHA256")
    run_baseline = _mapping(run_manifest.get("baseline"), "run manifest.baseline")
    run_qualification = _mapping(
        run_baseline.get("qualification"), "run manifest baseline qualification"
    )
    run_checksums = _mapping(
        run_baseline.get("checksum_manifest"), "run manifest baseline checksum manifest"
    )
    expected_run_values = {
        "baseline": run_baseline.get("sha256"),
        "qualification": run_qualification.get("sha256"),
        "checksums": run_checksums.get("sha256"),
    }
    actual_run_values = {
        "baseline": binding["baseline_sha256"],
        "qualification": binding["qualification_sha256"],
        "checksums": binding["checksum_manifest_sha256"],
    }
    if expected_run_values != actual_run_values:
        raise FreezeError("run and case manifests disagree on baseline provenance hashes")

    status = _mapping(
        _load_json(replay_dir / "run_status.json", "calibration replay status"),
        "calibration replay status",
    )
    expected_status = {
        "replay": 1,
        "innovus_exit_code": 0,
        "fetched": True,
        "passed": True,
        "expected_marker": "NOT_GOLD_CALIBRATION",
        "marker_present": True,
        "baseline_sha256": binding["baseline_sha256"],
        "catalog_sha256": binding["catalog_sha256"],
        "baseline_qualification_sha256": binding["qualification_sha256"],
        "baseline_checksum_manifest_sha256": binding["checksum_manifest_sha256"],
        "baseline_status": binding["qualification_status"],
        "guest_baseline_checksums_verified": True,
        "mode": "NOT_GOLD_CALIBRATION",
        "gold_eligible": False,
    }
    for key, value in expected_status.items():
        if status.get(key) != value:
            raise FreezeError(f"calibration run_status.{key} is not {value!r}")

    marker = _required_file(
        replay_dir / "NOT_GOLD_CALIBRATION", "NOT_GOLD calibration marker", max_bytes=4096
    ).read_text(encoding="utf-8")
    if case_id not in marker:
        raise FreezeError("NOT_GOLD marker is not bound to the selected case")

    root_status = _mapping(
        _load_json(run_root / "run_status.json", "calibration run status"),
        "calibration run status",
    )
    if (
        root_status.get("passed") is not True
        or root_status.get("dry_run") is not False
        or root_status.get("mode") != "NOT_GOLD_CALIBRATION"
        or root_status.get("gold_eligible") is not False
        or root_status.get("catalog_sha256") != binding["catalog_sha256"]
        or root_status.get("baseline_sha256") != binding["baseline_sha256"]
        or root_status.get("baseline_qualification_sha256")
        != binding["qualification_sha256"]
        or root_status.get("baseline_checksum_manifest_sha256")
        != binding["checksum_manifest_sha256"]
        or root_status.get("baseline_status") != binding["qualification_status"]
        or root_status.get("guest_baseline_checksums_verified") is not True
    ):
        raise FreezeError("top-level calibration run status is not a passing NOT_GOLD run")
    case_statuses = _mapping(root_status.get("cases"), "calibration run status.cases")
    if case_statuses.get(case_id) != [dict(status)]:
        raise FreezeError("top-level and replay calibration statuses do not match exactly")
    return binding, manifest


def _decode_backslash(text: str, index: int, where: str) -> tuple[str, int]:
    if index + 1 >= len(text):
        raise FreezeError(f"{where}: trailing Tcl backslash")
    following = text[index + 1]
    replacements = {"n": "\n", "r": "\r", "t": "\t", "f": "\f", "v": "\v"}
    if following == "\n":
        cursor = index + 2
        while cursor < len(text) and text[cursor] in " \t":
            cursor += 1
        return " ", cursor
    if following in replacements:
        return replacements[following], index + 2
    if following == "x":
        match = re.match(r"[0-9A-Fa-f]{1,2}", text[index + 2 :])
        if match is None:
            return "x", index + 2
        return chr(int(match.group(0), 16)), index + 2 + len(match.group(0))
    if following == "u":
        digits = text[index + 2 : index + 6]
        if len(digits) != 4 or re.fullmatch(r"[0-9A-Fa-f]{4}", digits) is None:
            raise FreezeError(f"{where}: invalid Tcl unicode escape")
        return chr(int(digits, 16)), index + 6
    return following, index + 2


def _parse_tcl_list(text: str, where: str) -> list[str]:
    """Parse a Tcl list without evaluating substitutions or sourcing a file."""

    words: list[str] = []
    cursor = 0
    length = len(text)
    while True:
        while cursor < length and text[cursor].isspace():
            cursor += 1
        if cursor == length:
            return words
        start = text[cursor]
        result: list[str] = []
        if start == "{":
            cursor += 1
            depth = 1
            while cursor < length and depth:
                char = text[cursor]
                if char == "\\":
                    # In a braced Tcl word only backslash-newline is
                    # substituted. Preserve other escapes for a nested parse.
                    if cursor + 1 < length and text[cursor + 1] == "\n":
                        decoded, cursor = _decode_backslash(text, cursor, where)
                        result.append(decoded)
                    else:
                        if cursor + 1 >= length:
                            raise FreezeError(f"{where}: trailing backslash in braced word")
                        result.extend((char, text[cursor + 1]))
                        cursor += 2
                    continue
                if char == "{":
                    depth += 1
                    result.append(char)
                elif char == "}":
                    depth -= 1
                    if depth:
                        result.append(char)
                else:
                    result.append(char)
                cursor += 1
            if depth:
                raise FreezeError(f"{where}: unmatched Tcl brace")
            if cursor < length and not text[cursor].isspace():
                raise FreezeError(f"{where}: characters follow a braced Tcl word")
        elif start == '"':
            cursor += 1
            closed = False
            while cursor < length:
                char = text[cursor]
                if char == '"':
                    cursor += 1
                    closed = True
                    break
                if char in "$[":
                    raise FreezeError(f"{where}: Tcl substitutions are not accepted")
                if char == "\\":
                    decoded, cursor = _decode_backslash(text, cursor, where)
                    result.append(decoded)
                else:
                    result.append(char)
                    cursor += 1
            if not closed:
                raise FreezeError(f"{where}: unmatched Tcl quote")
            if cursor < length and not text[cursor].isspace():
                raise FreezeError(f"{where}: characters follow a quoted Tcl word")
        else:
            while cursor < length and not text[cursor].isspace():
                char = text[cursor]
                if char in "{}\";$[":
                    raise FreezeError(f"{where}: unsafe unquoted Tcl character {char!r}")
                if char == "\\":
                    decoded, cursor = _decode_backslash(text, cursor, where)
                    result.append(decoded)
                else:
                    result.append(char)
                    cursor += 1
        word = "".join(result)
        if any(ord(character) < 0x20 for character in word):
            raise FreezeError(f"{where}: Tcl list element contains a control character")
        words.append(word)


def _parse_tcl_dict(text: str, where: str) -> dict[str, str]:
    words = _parse_tcl_list(text, where)
    if len(words) % 2:
        raise FreezeError(f"{where}: Tcl dict has an odd number of elements")
    result: dict[str, str] = {}
    for index in range(0, len(words), 2):
        key = words[index]
        if key in result:
            raise FreezeError(f"{where}: duplicate Tcl dict key {key}")
        result[key] = words[index + 1]
    return result


def _load_resolved_targets(path: Path) -> list[dict[str, str]]:
    raw = _required_file(
        path, "resolved-target evidence", max_bytes=MAX_TCL_BYTES
    ).read_text(encoding="utf-8")
    commands = [line.strip() for line in raw.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if len(commands) != 1:
        raise FreezeError(f"{path}: resolved-target evidence must contain exactly one command")
    command = _parse_tcl_list(commands[0], str(path))
    if len(command) != 3 or command[:2] != ["set", "::SFT_RESOLVED_TARGETS"]:
        raise FreezeError(f"{path}: expected one literal set ::SFT_RESOLVED_TARGETS command")
    targets = [
        _parse_tcl_dict(item, f"{path}: target[{index}]")
        for index, item in enumerate(_parse_tcl_list(command[2], str(path)))
    ]
    if not targets:
        raise FreezeError(f"{path}: resolved-target list is empty")
    required = {
        "role",
        "timing",
        "endpoint",
        "beginpoint",
        "driver_inst",
        "driver_ref",
        "driver_pin",
        "net",
        "slack",
    }
    for index, target in enumerate(targets):
        if not required.issubset(target):
            raise FreezeError(
                f"{path}: target[{index}] lacks {sorted(required - set(target))}"
            )
        _finite_string(target["slack"], f"{path}: target[{index}].slack")
    return targets


def _finite_string(value: str, where: str) -> float:
    try:
        result = float(value)
    except ValueError as exc:
        raise FreezeError(f"{where} must be numeric") from exc
    if not math.isfinite(result):
        raise FreezeError(f"{where} must be finite")
    return result


def _validate_calibration_status(path: Path, case_id: str, replay_runs: int) -> None:
    raw = _mapping(_load_json(path, "calibration status"), "calibration status")
    expected_fields = {
        "schema_version",
        "case_id",
        "status",
        "gold_eligible",
        "fresh_replays_required_after_freeze",
        "scope_validation",
        "passed",
    }
    if set(raw) != expected_fields:
        raise FreezeError(
            f"{path}: passing calibration status must contain exactly {sorted(expected_fields)}"
        )
    expected = {
        "schema_version": CALIBRATION_STATUS_SCHEMA,
        "case_id": case_id,
        "status": "NOT_GOLD_CALIBRATION_COMPLETE",
        "gold_eligible": False,
        "fresh_replays_required_after_freeze": replay_runs,
        "scope_validation": "passed",
        "passed": True,
    }
    if dict(raw) != expected:
        raise FreezeError("calibration_status.json is not a clean passing scope record")


def _validate_baseline_guard(
    path: Path,
    *,
    case_id: str,
    binding: Mapping[str, str],
    minimum_wns_ns: float,
) -> None:
    raw = _mapping(_load_json(path, "baseline guard"), "baseline guard")
    expected_fields = {
        "schema_version",
        "case_id",
        "minimum_wns_ns",
        "baseline_sha256",
        "catalog_sha256",
        "qualification_sha256",
        "checksum_manifest_sha256",
        "qualification_status",
        "technology_classification",
        "gold_status",
        "signoff_eligible",
        "setup",
        "hold",
        "passed",
    }
    if set(raw) != expected_fields:
        raise FreezeError(f"{path}: baseline guard has an unexpected shape")
    expected_scalar = {
        "schema_version": "timing_eco_baseline_guard.v1",
        "case_id": case_id,
        "baseline_sha256": binding["baseline_sha256"],
        "catalog_sha256": binding["catalog_sha256"],
        "qualification_sha256": binding["qualification_sha256"],
        "checksum_manifest_sha256": binding["checksum_manifest_sha256"],
        "qualification_status": binding["qualification_status"],
        "technology_classification": binding["technology_classification"],
        "gold_status": False,
        "signoff_eligible": False,
        "passed": True,
    }
    for key, expected in expected_scalar.items():
        if raw.get(key) != expected:
            raise FreezeError(f"baseline_guard.{key} is not bound to this calibration")
    declared_minimum = _finite(raw.get("minimum_wns_ns"), "baseline guard minimum WNS")
    if abs(declared_minimum - minimum_wns_ns) > NUMERIC_EPSILON:
        raise FreezeError("baseline guard minimum WNS differs from the catalog")
    for check in ("setup", "hold"):
        item = _mapping(raw.get(check), f"baseline guard.{check}")
        if set(item) != {"wns_ns", "tns_ns"}:
            raise FreezeError(f"baseline guard.{check} has an unexpected shape")
        wns = _finite(item.get("wns_ns"), f"baseline guard {check} WNS")
        tns = _finite(item.get("tns_ns"), f"baseline guard {check} TNS")
        if wns < minimum_wns_ns or abs(tns) > NUMERIC_EPSILON:
            raise FreezeError(
                f"baseline guard {check} does not preserve WNS>={minimum_wns_ns} and TNS=0"
            )


def _selector_counts(case: Mapping[str, Any]) -> dict[str, int]:
    return {
        "setup": sum(int(item["count"]) for item in case["selectors"] if item["timing"] == "late"),
        "hold": sum(int(item["count"]) for item in case["selectors"] if item["timing"] == "early"),
    }


def _validate_selected_slack_evidence(locality: Mapping[str, Any]) -> None:
    """Recheck the exact endpoint proof at the calibration trust boundary."""

    for check in ("setup", "hold"):
        item = _mapping(locality.get(check), f"violation locality {check}")
        selected = item.get("selected_endpoints")
        violating = item.get("violating_endpoints")
        records = item.get("selected_endpoint_slacks")
        if not isinstance(selected, list) or not isinstance(violating, list) or not isinstance(
            records, list
        ):
            raise FreezeError(f"violation locality {check} endpoint slack shape is invalid")
        exact: dict[str, float] = {}
        for index, record in enumerate(records):
            parsed = _mapping(record, f"violation locality {check} selected slack[{index}]")
            if set(parsed) != {"endpoint", "slack_ns"}:
                raise FreezeError(
                    f"violation locality {check} selected slack[{index}] has an unexpected shape"
                )
            endpoint = parsed.get("endpoint")
            if not isinstance(endpoint, str) or endpoint == "" or endpoint in exact:
                raise FreezeError(
                    f"violation locality {check} selected endpoint slack is empty or duplicated"
                )
            exact[endpoint] = _finite(
                parsed.get("slack_ns"),
                f"violation locality {check} selected slack for {endpoint}",
            )
        if list(exact) != selected:
            raise FreezeError(
                f"violation locality {check} selected endpoint slack coverage differs from selected endpoints"
            )
        if [endpoint for endpoint, slack in exact.items() if slack < 0.0] != violating:
            raise FreezeError(
                f"violation locality {check} exact negative endpoints differ from violating endpoints"
            )
        if item.get("required") is True:
            if not exact:
                raise FreezeError(
                    f"violation locality required {check} direction has no exact endpoint slacks"
                )
            wns = _finite(item.get("wns_ns"), f"violation locality {check} WNS")
            tns = _finite(item.get("tns_ns"), f"violation locality {check} TNS")
            if abs(min(exact.values()) - wns) > LOCALITY_INTERNAL_TOLERANCE_NS:
                raise FreezeError(
                    f"violation locality {check} exact endpoint slacks do not reproduce WNS"
                )
            if abs(sum(exact.values()) - tns) > LOCALITY_INTERNAL_TOLERANCE_NS:
                raise FreezeError(
                    f"violation locality {check} exact endpoint slacks do not reproduce TNS"
                )


def _validate_locality_and_context(
    replay_dir: Path, case: Mapping[str, Any], *, design: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    locality_path = replay_dir / "reports" / "violation_locality.json"
    context_path = replay_dir / "reports" / "diagnostic_context.json"
    # The shared validators intentionally focus on schema semantics. Apply
    # this tool's duplicate-key, size, and direct-symlink checks first.
    _load_json(locality_path, "violation locality")
    _load_json(context_path, "diagnostic context")
    try:
        locality = finalizer.load_violation_locality(
            locality_path,
            case_id=case["id"],
            repair_mode=case["repair_mode"],
            case_type=case["type"],
        )
        context = finalizer.load_diagnostic_context(
            context_path,
            case_id=case["id"],
            design=design,
            max_eco_cells=int(case["repair"]["max_eco_cells"]),
        )
    except (FileNotFoundError, OSError, finalizer.FinalizeError) as exc:
        raise FreezeError(str(exc)) from exc

    _validate_selected_slack_evidence(locality)

    counts = _selector_counts(case)
    for check in ("setup", "hold"):
        if locality[check]["expected_endpoint_count"] != counts[check]:
            raise FreezeError(
                f"violation locality {check} count does not match frozen selectors"
            )
    expected_roles = Counter(
        (selector["role"], selector["timing"])
        for selector in case["selectors"]
        for _ in range(int(selector["count"]))
    )
    actual_roles = Counter((target["role"], target["timing"]) for target in context["targets"])
    if actual_roles != expected_roles:
        raise FreezeError("diagnostic target role/timing cardinality differs from selectors")

    before = {
        check: {
            "wns_ns": locality[check]["wns_ns"],
            "tns_ns": locality[check]["tns_ns"],
            "reported_endpoints": locality[check]["violating_endpoint_count"],
        }
        for check in ("setup", "hold")
    }
    try:
        finalizer._bind_diagnostic_to_locality(
            context=context,
            locality=locality,
            before=before,
            where=f"{case['id']} calibration",
        )
    except finalizer.FinalizeError as exc:
        raise FreezeError(str(exc)) from exc
    return locality, context


def _bind_resolved_targets(
    resolved: list[dict[str, str]], context: Mapping[str, Any]
) -> dict[tuple[str, str], dict[str, str]]:
    resolved_by_key: dict[tuple[str, str], dict[str, str]] = {}
    for target in resolved:
        key = (target["timing"], target["endpoint"])
        if key in resolved_by_key:
            raise FreezeError(f"resolved-target evidence duplicates {key[0]}/{key[1]}")
        resolved_by_key[key] = target
    context_by_key = {
        (str(target["timing"]), str(target["endpoint"])): target
        for target in context["targets"]
    }
    if set(resolved_by_key) != set(context_by_key):
        raise FreezeError("resolved targets and diagnostic context select different endpoints")
    for key, target in resolved_by_key.items():
        diagnostic = context_by_key[key]
        comparisons = {
            "role": diagnostic["role"],
            "beginpoint": diagnostic["beginpoint"],
            "driver_inst": diagnostic["original_driver"]["inst"],
            "driver_ref": diagnostic["original_driver"]["ref"],
        }
        for field, expected in comparisons.items():
            if target[field] != expected:
                raise FreezeError(
                    f"resolved target {key[0]}/{key[1]} {field} differs from diagnostic context"
                )
    if len({target["driver_inst"] for target in resolved_by_key.values()}) != len(
        resolved_by_key
    ):
        raise FreezeError("resolved targets reuse an original driver")
    if len({target["net"] for target in resolved_by_key.values()}) != len(resolved_by_key):
        raise FreezeError("resolved targets reuse a data net")
    return resolved_by_key


def _parse_parameter(parameter: Any, where: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    if not isinstance(parameter, str) or not parameter:
        raise FreezeError(f"{where} must be a non-empty Tcl list")
    top = _parse_tcl_dict(parameter, where)
    if set(top) != {"strategy", "setup", "hold", "actions"}:
        raise FreezeError(f"{where} must contain strategy/setup/hold/actions exactly")
    setup = _parse_tcl_dict(top["setup"], f"{where}.setup")
    hold = _parse_tcl_dict(top["hold"], f"{where}.hold")
    if set(setup) != {"drive_steps", "delay_cells"}:
        raise FreezeError(f"{where}.setup has an unexpected shape")
    if set(hold) != {"drive_steps", "clock_cell_count"}:
        raise FreezeError(f"{where}.hold has an unexpected shape")
    for direction in (setup, hold):
        for key, value in direction.items():
            try:
                parsed = int(value)
            except ValueError as exc:
                raise FreezeError(f"{where}.{key} must be an integer") from exc
            if parsed < 0 or str(parsed) != value:
                raise FreezeError(f"{where}.{key} must be a canonical nonnegative integer")
    actions = [
        _parse_tcl_dict(item, f"{where}.actions[{index}]")
        for index, item in enumerate(_parse_tcl_list(top["actions"], f"{where}.actions"))
    ]
    if not actions:
        raise FreezeError(f"{where}.actions must contain measured structural changes")
    top["setup_values"] = setup  # type: ignore[assignment]
    top["hold_values"] = hold  # type: ignore[assignment]
    return top, actions


def _expected_parameter_values(case: Mapping[str, Any]) -> tuple[dict[str, int], dict[str, int]]:
    injection = case["injection"]
    setup_parameters = injection["setup_parameters"]
    hold_parameters = injection["hold_parameters"]
    return (
        {
            "drive_steps": int(setup_parameters.get("drive_steps", 0)),
            "delay_cells": int(setup_parameters.get("delay_cells", 0)),
        },
        {
            "drive_steps": int(hold_parameters.get("drive_steps", 0)),
            "clock_cell_count": int(hold_parameters.get("clock_cell_count", 0)),
        },
    )


def _validate_actions(
    case: Mapping[str, Any],
    parameter: Mapping[str, Any],
    actions: list[dict[str, str]],
    resolved: Mapping[tuple[str, str], Mapping[str, str]],
    context: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    strategy = case["injection"]["strategy"]
    if parameter["strategy"] != strategy:
        raise FreezeError("measured injection strategy differs from the catalog")
    expected_setup, expected_hold = _expected_parameter_values(case)
    actual_setup = {key: int(value) for key, value in parameter["setup_values"].items()}
    actual_hold = {key: int(value) for key, value in parameter["hold_values"].items()}
    if actual_setup != expected_setup or actual_hold != expected_hold:
        raise FreezeError("measured one-shot parameter differs from the catalog candidate")

    instances: set[str] = set()
    by_kind: dict[str, list[dict[str, str]]] = {}
    for index, action in enumerate(actions):
        kind = action.get("kind")
        instance = action.get("inst")
        if not kind or not instance:
            raise FreezeError(f"injection action[{index}] lacks kind/inst")
        if instance in instances:
            raise FreezeError(f"injection actions reuse instance {instance}")
        instances.add(instance)
        by_kind.setdefault(kind, []).append(action)
    allowed_kinds = {"resize", "data_delay_injection", "capture_clock_injection"}
    if not set(by_kind).issubset(allowed_kinds):
        raise FreezeError(f"injection action contains unsupported kinds {sorted(set(by_kind) - allowed_kinds)}")
    capture_actions = by_kind.get("capture_clock_injection", [])
    uses_capture_clock = strategy in CAPTURE_CLOCK_INJECTION_STRATEGIES
    if uses_capture_clock and not capture_actions:
        raise FreezeError(f"{case['id']} has no measured capture-clock evidence")
    if not uses_capture_clock and capture_actions:
        raise FreezeError(f"unexpected capture-clock evidence for {case['id']}")

    diagnostics = {
        (str(item["timing"]), str(item["endpoint"])): item for item in context["targets"]
    }
    expected_resize: dict[str, tuple[str, Mapping[str, Any]]] = {}
    if strategy == "downsize_endpoint_driver":
        expected_resize = {
            target["driver_inst"]: ("down", diagnostics[key])
            for key, target in resolved.items()
            if key[0] == "late"
        }
    elif strategy == "upsize_endpoint_driver":
        expected_resize = {
            target["driver_inst"]: ("up", diagnostics[key])
            for key, target in resolved.items()
            if key[0] == "early"
        }
    elif strategy == "mixed_downsize_and_speedup":
        expected_resize = {
            target["driver_inst"]: (
                "down" if key[0] == "late" else "up",
                diagnostics[key],
            )
            for key, target in resolved.items()
        }
    if expected_resize:
        resize_actions = by_kind.get("resize", [])
        if {item.get("inst") for item in resize_actions} != set(expected_resize):
            raise FreezeError("resize actions do not exactly cover resolved original drivers")
        for action in resize_actions:
            instance = action["inst"]
            direction, diagnostic = expected_resize[instance]
            resolved_item = next(item for item in resolved.values() if item["driver_inst"] == instance)
            required_fields = {"kind", "inst", "from", "to", "direction"}
            if set(action) != required_fields:
                raise FreezeError(f"resize action for {instance} has an unexpected shape")
            if (
                action["direction"] != direction
                or action["from"] != resolved_item["driver_ref"]
                or action["to"] != diagnostic["driver_ref"]
            ):
                raise FreezeError(f"resize action for {instance} disagrees with DB evidence")
    elif by_kind.get("resize"):
        raise FreezeError(f"strategy {strategy} unexpectedly recorded resize actions")

    delay_count = expected_setup["delay_cells"]
    late_keys = [key for key in resolved if key[0] == "late"]
    expected_delay_total = delay_count * len(late_keys)
    delay_actions = by_kind.get("data_delay_injection", [])
    if len(delay_actions) != expected_delay_total:
        raise FreezeError(
            f"data-delay action count is {len(delay_actions)}, expected {expected_delay_total}"
        )
    for action in delay_actions:
        if set(action) != {"kind", "inst", "cell", "term"}:
            raise FreezeError("data-delay action has an unexpected shape")
    delay_reference: str | None = None
    if delay_actions:
        delay_references = {item["cell"] for item in delay_actions}
        if len(delay_references) != 1:
            raise FreezeError(
                "data-delay injection actions do not use one consistent cell reference"
            )
        delay_reference = next(iter(delay_references))
        if SAFE_CELL_REFERENCE_RE.fullmatch(delay_reference) is None:
            raise FreezeError("measured data-delay cell reference is unsafe")
    for key in late_keys:
        endpoint = key[1]
        selected = [item for item in delay_actions if item["term"] == endpoint]
        if len(selected) != delay_count:
            raise FreezeError(f"data-delay actions do not exactly cover {endpoint}")
        diagnostic = diagnostics[key]
        injected_cells = diagnostic["local_cells"][:-1]
        if delay_count:
            original_cell = diagnostic["local_cells"][-1]
            resolved_original = {
                "inst": resolved[key]["driver_inst"],
                "ref": resolved[key]["driver_ref"],
            }
            if original_cell != resolved_original:
                raise FreezeError(
                    f"post-injection local_cells for {endpoint} do not preserve the original driver"
                )
        if {item["inst"] for item in selected} != {item["inst"] for item in injected_cells}:
            raise FreezeError(f"data-delay actions for {endpoint} differ from local_cells")
        evidence_refs = {item["inst"]: item["ref"] for item in injected_cells}
        if any(evidence_refs[item["inst"]] != item["cell"] for item in selected):
            raise FreezeError(f"data-delay cell references for {endpoint} differ from local_cells")
    delay_terms = {item["term"] for item in delay_actions}
    if delay_terms - {key[1] for key in late_keys}:
        raise FreezeError("data-delay actions contain an unselected endpoint")

    expected_capture = expected_hold["clock_cell_count"]
    if len(capture_actions) != expected_capture:
        raise FreezeError(
            f"capture-clock action count is {len(capture_actions)}, expected {expected_capture}"
        )
    clock_reference: str | None = None
    if capture_actions:
        for action in capture_actions:
            if set(action) != {"kind", "inst", "cell", "term"}:
                raise FreezeError("capture-clock action has an unexpected shape")
        clock_references = {action["cell"] for action in capture_actions}
        if len(clock_references) != 1:
            raise FreezeError(
                "capture-clock actions do not use one consistent cell reference"
        )
        clock_reference = next(iter(clock_references))
        if (
            clock_reference == "PROBE_REQUIRED"
            or SAFE_CELL_REFERENCE_RE.fullmatch(clock_reference) is None
        ):
            raise FreezeError(
                "measured capture-clock cell reference is not exact and safe"
            )
        declared_clock_reference = case["injection"]["hold_parameters"].get(
            "clock_cell_reference"
        )
        if declared_clock_reference not in {
            "PROBE_REQUIRED",
            clock_reference,
        }:
            raise FreezeError(
                "measured capture-clock cell reference differs from the "
                "declared calibration candidate"
            )
        early_keys = [key for key in resolved if key[0] == "early"]
        if len(early_keys) != 1:
            raise FreezeError("capture-clock evidence must bind exactly one early endpoint")
        clock_terms = {action["term"] for action in capture_actions}
        if len(clock_terms) != 1:
            raise FreezeError(
                "capture-clock actions do not form one chain at the same clock pin"
            )
        endpoint = early_keys[0][1]
        clock_term = next(iter(clock_terms))
        if "/" not in endpoint or "/" not in clock_term:
            raise FreezeError("capture-clock endpoint/term hierarchy is malformed")
        capture_instance = endpoint.rsplit("/", 1)[0]
        if clock_term.rsplit("/", 1)[0] != capture_instance:
            raise FreezeError(
                "capture-clock action term is not on the selected early endpoint cell"
            )
        expected_repeater_parent = (
            capture_instance.rsplit("/", 1)[0] if "/" in capture_instance else ""
        )
        for action in capture_actions:
            actual_parent = (
                action["inst"].rsplit("/", 1)[0]
                if "/" in action["inst"]
                else ""
            )
            if actual_parent != expected_repeater_parent:
                raise FreezeError(
                    "capture-clock action instance is outside the capture sink hierarchy"
                )
    if uses_capture_clock and clock_reference is None:
        raise FreezeError(f"{case['id']} has no measured capture-clock cell reference")
    if not uses_capture_clock and clock_reference is not None:
        raise FreezeError(f"unexpected capture-clock evidence for {case['id']}")
    if strategy in DATA_DELAY_INJECTION_STRATEGIES and delay_reference is None:
        raise FreezeError(f"{case['id']} has no measured data-delay cell reference")
    if strategy not in DATA_DELAY_INJECTION_STRATEGIES and delay_reference is not None:
        raise FreezeError(f"unexpected data-delay evidence for {case['id']}")
    return delay_reference, clock_reference


def _validate_provenance_and_measurement(
    replay_dir: Path,
    case: Mapping[str, Any],
    binding: Mapping[str, str],
    locality: Mapping[str, Any],
    context: Mapping[str, Any],
    resolved: Mapping[tuple[str, str], Mapping[str, str]],
) -> tuple[str | None, str | None, Mapping[str, Any]]:
    path = replay_dir / "reports" / "injection_provenance.json"
    raw = _mapping(_load_json(path, "injection provenance"), "injection provenance")
    expected_top = {
        "schema_version",
        "case_id",
        "strategy",
        "calibration_status",
        "status",
        "reason",
        "baseline_binding",
        "target_wns_ns",
        "attempts",
    }
    if set(raw) != expected_top:
        raise FreezeError(f"{path}: injection provenance has an unexpected shape")
    if (
        raw.get("schema_version") != INJECTION_PROVENANCE_SCHEMA
        or raw.get("case_id") != case["id"]
        or raw.get("strategy") != case["injection"]["strategy"]
        or raw.get("calibration_status") != "PROBE_REQUIRED"
        or raw.get("status") != "calibration_candidate"
    ):
        raise FreezeError("injection provenance is not the selected NOT_GOLD candidate")
    if dict(_mapping(raw.get("baseline_binding"), "injection baseline binding")) != dict(binding):
        raise FreezeError("injection provenance baseline/catalog binding is inconsistent")

    targets = _mapping(raw.get("target_wns_ns"), "injection target_wns_ns")
    expected_targets = {
        check: [float(value) for value in case["target_wns_ns"].get(check, [0.0, 0.0])]
        for check in ("setup", "hold")
    }
    normalized_targets: dict[str, list[float]] = {}
    for check in ("setup", "hold"):
        interval = targets.get(check)
        if not isinstance(interval, list) or len(interval) != 2:
            raise FreezeError(f"injection target_wns_ns.{check} must be [low, high]")
        normalized_targets[check] = [
            _finite(interval[0], f"injection {check} target low"),
            _finite(interval[1], f"injection {check} target high"),
        ]
    if normalized_targets != expected_targets:
        raise FreezeError("injection target windows differ from the prepared catalog")

    attempts = raw.get("attempts")
    if not isinstance(attempts, list) or len(attempts) != 1:
        raise FreezeError("calibration freeze requires exactly one measured injection attempt")
    trial = _mapping(attempts[0], "injection attempt[0]")
    expected_trial_fields = {
        "attempt",
        "parameter",
        "setup_wns_ns",
        "setup_tns_ns",
        "hold_wns_ns",
        "hold_tns_ns",
    }
    if set(trial) != expected_trial_fields or trial.get("attempt") != 1:
        raise FreezeError("calibration must contain exactly the canonical first attempt")
    measured: dict[str, dict[str, float]] = {}
    for check in ("setup", "hold"):
        measured[check] = {
            "wns": _finite(trial.get(f"{check}_wns_ns"), f"attempt {check} WNS"),
            "tns": _finite(trial.get(f"{check}_tns_ns"), f"attempt {check} TNS"),
        }
        if abs(measured[check]["wns"] - float(locality[check]["wns_ns"])) > NUMERIC_EPSILON:
            raise FreezeError(f"attempt and violation-locality {check} WNS disagree")
        if abs(measured[check]["tns"] - float(locality[check]["tns_ns"])) > NUMERIC_EPSILON:
            raise FreezeError(f"attempt and violation-locality {check} TNS disagree")

    required = {"setup", "hold"} if case["type"] == "mixed" else {case["type"]}
    for check in required:
        low, high = expected_targets[check]
        if measured[check]["wns"] < low or measured[check]["wns"] > high:
            raise FreezeError(
                f"measured {check} WNS {measured[check]['wns']:.6f} is outside [{low}, {high}]"
            )
        if measured[check]["tns"] >= 0.0:
            raise FreezeError(f"required {check} injection has no measured negative TNS")
    if case["type"] != "mixed":
        opposite = "hold" if case["type"] == "setup" else "setup"
        minimum = _finite(
            case["injection"]["opposite_wns_min_ns"],
            f"{case['id']} opposite_wns_min_ns",
        )
        if measured[opposite]["wns"] < minimum:
            raise FreezeError(
                f"measured opposite {opposite} WNS lacks the required {minimum:.6f} ns margin"
            )
        if abs(measured[opposite]["tns"]) > NUMERIC_EPSILON:
            raise FreezeError(f"measured opposite {opposite} TNS is not zero")

    parameter, actions = _parse_parameter(trial["parameter"], "injection attempt parameter")
    delay_reference, clock_reference = _validate_actions(
        case, parameter, actions, resolved, context
    )
    return delay_reference, clock_reference, raw


def _default_output(catalog_path: Path, case_id: str) -> Path:
    return catalog_path.with_name(
        f"{catalog_path.stem}.frozen-{case_id}{catalog_path.suffix or '.json'}"
    )


def _write_json_atomic(path: Path, value: Any, *, replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace:
        raise FreezeError(f"refusing to overwrite existing output: {path}")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    if temporary.exists():
        raise FreezeError(f"temporary output already exists: {temporary}")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        # Validate the exact serialized bytes before replacing anything. This
        # is especially important for the explicitly destructive --in-place
        # path.
        _catalog(temporary)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def freeze_case(args: argparse.Namespace) -> dict[str, Any]:
    catalog_path = Path(args.catalog).resolve()
    catalog = _catalog(catalog_path)
    case_id = str(args.case)
    current_case = _case_by_id(catalog, case_id)
    if current_case["injection"]["calibration_status"] != "PROBE_REQUIRED":
        raise FreezeError(f"{case_id} is not PROBE_REQUIRED in {catalog_path}")

    replay_dir = _resolve_replay_dir(Path(args.calibration_case_dir))
    run_root = _find_run_root(replay_dir)
    prepared_catalog_path = run_root / "catalog.json"
    prepared_catalog = _catalog(prepared_catalog_path)
    prepared_case = _case_by_id(prepared_catalog, case_id)
    if _without_freeze_state(catalog) != _without_freeze_state(prepared_catalog):
        raise FreezeError(
            "input catalog differs from the prepared calibration catalog beyond "
            "previously frozen status/clock/data-delay reference fields"
        )
    if _case_without_freeze_state(prepared_case) != _case_without_freeze_state(
        current_case
    ):
        raise FreezeError(
            "the selected case entry differs from the exact prepared calibration catalog"
        )
    if prepared_case["injection"]["calibration_status"] != "PROBE_REQUIRED":
        raise FreezeError("prepared calibration case was not PROBE_REQUIRED")

    binding, _manifest = _validate_run_contract(
        replay_dir, run_root, case_id, prepared_case
    )
    if _sha256(prepared_catalog_path) != binding["catalog_sha256"]:
        raise FreezeError("prepared catalog bytes do not match calibration catalog SHA256")
    replay_runs = _integer(
        catalog["design"]["acceptance"]["replay_runs"],
        "catalog replay_runs",
        minimum=1,
    )
    _validate_baseline_guard(
        replay_dir / "reports" / "baseline_guard.json",
        case_id=case_id,
        binding=binding,
        minimum_wns_ns=_finite(
            catalog["design"]["acceptance"]["baseline_wns_min_ns"],
            "catalog baseline_wns_min_ns",
        ),
    )
    _validate_calibration_status(
        replay_dir / "reports" / "calibration_status.json", case_id, replay_runs
    )
    locality, context = _validate_locality_and_context(
        replay_dir, current_case, design=str(catalog["design"]["top"])
    )
    resolved_list = _load_resolved_targets(
        replay_dir / "reports" / "resolved_targets.tcl"
    )
    resolved = _bind_resolved_targets(resolved_list, context)
    delay_reference, clock_reference, _provenance = (
        _validate_provenance_and_measurement(
            replay_dir, current_case, binding, locality, context, resolved
        )
    )

    output_catalog = json.loads(json.dumps(catalog))
    output_case = next(case for case in output_catalog["cases"] if case["id"] == case_id)
    if output_case["repair"]["strategy"] in run_pilot.HOLD_DELAY_REPAIR_STRATEGIES:
        repair = output_case["repair"]
        if not {
            "delay_cell_reference",
            "delay_cells_per_endpoint",
        }.issubset(repair):
            raise FreezeError(
                f"{case_id} cannot freeze until its case-local hold-repair "
                "delay_cell_reference and delay_cells_per_endpoint are declared"
            )
    output_case["injection"]["calibration_status"] = "FROZEN"
    if output_case["injection"]["strategy"] in DATA_DELAY_INJECTION_STRATEGIES:
        if not delay_reference:
            raise FreezeError(f"{case_id} cannot freeze without a measured delay reference")
        output_case["injection"]["setup_parameters"][
            "delay_cell_reference"
        ] = delay_reference
    if (
        output_case["injection"]["strategy"]
        in CAPTURE_CLOCK_INJECTION_STRATEGIES
    ):
        if not clock_reference:
            raise FreezeError(f"{case_id} cannot freeze without a measured clock reference")
        output_case["injection"]["hold_parameters"][
            "clock_cell_reference"
        ] = clock_reference

    in_place = bool(args.in_place)
    if in_place and args.output is not None:
        raise FreezeError("--in-place and --output are mutually exclusive")
    output_path = catalog_path if in_place else (
        Path(args.output).resolve() if args.output is not None else _default_output(catalog_path, case_id)
    )
    if not in_place and output_path == catalog_path:
        raise FreezeError("use --in-place explicitly to replace the input catalog")
    _write_json_atomic(output_path, output_catalog, replace=in_place)
    # Re-read with the shared schema validator. This also prevents a clock
    # placeholder from slipping into a newly frozen capture-clock case.
    validated = _catalog(output_path)
    frozen = _case_by_id(validated, case_id)
    if frozen["injection"]["calibration_status"] != "FROZEN":
        raise FreezeError("written catalog did not preserve the frozen status")

    return {
        "schema_version": "timing_eco_calibration_freeze.v1",
        "case_id": case_id,
        "status": "FROZEN_CATALOG_WRITTEN",
        "output_catalog": str(output_path),
        "output_catalog_sha256": _sha256(output_path),
        "calibration_catalog_sha256": binding["catalog_sha256"],
        "calibration_replay": str(replay_dir),
        "evidence_sha256": {
            name: _sha256(replay_dir / relative)
            for name, relative in {
                "calibration_status": "reports/calibration_status.json",
                "baseline_guard": "reports/baseline_guard.json",
                "injection_provenance": "reports/injection_provenance.json",
                "violation_locality": "reports/violation_locality.json",
                "diagnostic_context": "reports/diagnostic_context.json",
                "resolved_targets": "reports/resolved_targets.tcl",
            }.items()
        },
        "delay_cell_reference": delay_reference,
        "clock_cell_reference": clock_reference,
        "gold_replays_completed": False,
        "fresh_gold_replays_required": replay_runs,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Freeze one pilot-10 candidate from complete real NOT_GOLD evidence"
    )
    parser.add_argument("--case", required=True, help="exact pilot case ID")
    parser.add_argument(
        "--calibration-case-dir",
        required=True,
        type=Path,
        help="fetched replay_1 directory or its prepared case directory",
    )
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--output", type=Path, help="new catalog path")
    output.add_argument(
        "--in-place",
        action="store_true",
        help="explicitly replace --catalog after every evidence gate passes",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = freeze_case(args)
    except (FreezeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

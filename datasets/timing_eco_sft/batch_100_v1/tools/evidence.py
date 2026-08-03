#!/usr/bin/env python3
"""Turn raw Innovus reports into strict Batch-100 replay evidence."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import stat
from pathlib import Path
from typing import Any, Mapping, Sequence

from common import BatchError, SAFE_OBJECT_RE, sha256_file


NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?"
EXPECTED_PORTABILITY_ENDPOINTS = 3555
RAW_REPORT_TREE_ALGORITHM = "sha256-raw-report-tree-v1"


def _text(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise BatchError(f"missing real Innovus report: {path}")
    return path.read_text(encoding="utf-8", errors="strict")


def _tsv(path: Path) -> list[dict[str, str]]:
    if not path.is_file() or path.is_symlink():
        raise BatchError(f"missing real evidence TSV: {path}")
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t", strict=True))
    if not rows:
        raise BatchError(f"empty evidence TSV: {path}")
    return rows


def timing(path: Path) -> dict[str, Any]:
    text = _text(path)
    if "Cadence Innovus 21.10-p004_1" not in text:
        raise BatchError(f"{path}: wrong/missing Innovus report version")
    starts = list(re.finditer(r"(?m)^Path\s+\d+:", text))
    if not starts:
        raise BatchError(f"{path}: contains no timing paths")
    slacks: dict[str, float] = {}
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        block = text[start.start() : end]
        endpoint = re.search(r"(?m)^Endpoint:\s+(\S+)", block)
        slack = re.search(
            rf"(?m)^\s*(?:=\s*)?Slack Time\s+({NUMBER})\s*$", block
        )
        if endpoint is None or slack is None:
            raise BatchError(f"{path}: malformed timing path {index + 1}")
        value = float(slack.group(1))
        name = endpoint.group(1)
        slacks[name] = min(value, slacks.get(name, value))
    negative = {name: value for name, value in slacks.items() if value < 0.0}
    return {
        "slacks": slacks,
        "wns_ns": min(slacks.values()),
        "tns_ns": sum(negative.values()),
        "negative_endpoints": sorted(negative),
    }


def observable_target_path_evidence(
    path: Path, *, maximum_cells_per_path: int = 8
) -> dict[str, Any]:
    """Extract model-visible path points from an Innovus text report.

    The source is the replay-before report from the violating checkpoint, not
    the baseline probe or construction plan.  Only currently observable names,
    refs and point delays are returned.
    """

    if (
        not isinstance(maximum_cells_per_path, int)
        or isinstance(maximum_cells_per_path, bool)
        or maximum_cells_per_path < 1
        or maximum_cells_per_path > 32
    ):
        raise BatchError("maximum observable path cells must be in [1, 32]")
    text = _text(path)
    if "Cadence Innovus 21.10-p004_1" not in text:
        raise BatchError(f"{path}: wrong/missing Innovus report version")
    design_match = re.search(r"(?m)^#\s*Design:\s*(\S+)\s*$", text)
    if design_match is None:
        raise BatchError(f"{path}: missing design identity")
    starts = list(re.finditer(r"(?m)^Path\s+\d+:", text))
    if not starts:
        raise BatchError(f"{path}: contains no timing paths")

    result: dict[str, dict[str, Any]] = {}
    analysis_view: str | None = None
    sequential_prefixes = ("DFF", "SDFF", "LATCH", "LAT", "TLAT", "ICG", "CKG")
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        block = text[start.start() : end]
        endpoint_match = re.search(r"(?m)^Endpoint:\s+(\S+)", block)
        beginpoint_match = re.search(r"(?m)^Beginpoint:\s+(\S+)", block)
        view_match = re.search(r"(?m)^Analysis View:\s+(\S+)\s*$", block)
        timing_match = re.search(
            r"(?ms)^\s*Timing Path:\s*(.*?)(?=^\s*Other End Path:)", block
        )
        if None in (endpoint_match, beginpoint_match, view_match, timing_match):
            raise BatchError(f"{path}: malformed observable timing path {index + 1}")
        endpoint = endpoint_match.group(1)
        beginpoint = beginpoint_match.group(1)
        view = view_match.group(1)
        if analysis_view is None:
            analysis_view = view
        elif view != analysis_view:
            raise BatchError(f"{path}: target paths use multiple analysis views")
        if endpoint in result:
            raise BatchError(f"{path}: duplicate observable endpoint {endpoint}")

        started_data = False
        by_instance: dict[str, dict[str, Any]] = {}
        for line in timing_match.group(1).splitlines():
            if not line.lstrip().startswith("|"):
                continue
            fields = [field.strip() for field in line.strip().strip("|").split("|")]
            if len(fields) != 7:
                continue
            pin, _edge, _net, reference, delay_text, _arrival, _required = fields
            if pin == beginpoint:
                started_data = True
                continue
            if not started_data:
                continue
            try:
                delay = float(delay_text)
            except ValueError:
                continue
            pin = re.sub(r"\s*->\s*$", "", pin)
            if delay <= 0.0 or not reference or "/" not in pin:
                continue
            if reference.startswith(sequential_prefixes):
                continue
            instance = pin.rsplit("/", 1)[0]
            if (
                SAFE_OBJECT_RE.fullmatch(instance) is None
                or SAFE_OBJECT_RE.fullmatch(pin) is None
                or SAFE_OBJECT_RE.fullmatch(reference) is None
            ):
                raise BatchError(f"{path}: unsafe object in observable path table")
            previous = by_instance.get(instance)
            if previous is None or delay > previous["delay_ns"]:
                by_instance[instance] = {
                    "instance": instance,
                    "pin": pin,
                    "current_ref": reference,
                    "delay_ns": delay,
                }
        cells = sorted(
            by_instance.values(),
            key=lambda row: (-row["delay_ns"], row["instance"], row["pin"]),
        )[:maximum_cells_per_path]
        if not cells:
            raise BatchError(f"{path}: no observable data-path cells for {endpoint}")
        result[endpoint] = {"beginpoint": beginpoint, "cells": cells}

    return {
        "design": design_match.group(1),
        "analysis_view": analysis_view,
        "paths": result,
    }


def compare_observable_path_evidence(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    tolerance_ns: float = 0.001,
) -> None:
    """Require two fresh replay reports to expose the same prompt evidence."""

    if left.get("design") != right.get("design"):
        raise BatchError("observable replay design identities differ")
    if left.get("analysis_view") != right.get("analysis_view"):
        raise BatchError("observable replay analysis views differ")
    left_paths = left.get("paths")
    right_paths = right.get("paths")
    if not isinstance(left_paths, Mapping) or not isinstance(right_paths, Mapping):
        raise BatchError("observable replay path evidence is malformed")
    if set(left_paths) != set(right_paths):
        raise BatchError("observable replay endpoint sets differ")
    for endpoint in left_paths:
        left_path = left_paths[endpoint]
        right_path = right_paths[endpoint]
        if not isinstance(left_path, Mapping) or not isinstance(right_path, Mapping):
            raise BatchError(f"observable replay path is malformed for {endpoint}")
        if left_path.get("beginpoint") != right_path.get("beginpoint"):
            raise BatchError(f"observable replay beginpoints differ for {endpoint}")
        left_cells = left_path.get("cells")
        right_cells = right_path.get("cells")
        if not isinstance(left_cells, list) or not isinstance(right_cells, list):
            raise BatchError(f"observable replay cells are malformed for {endpoint}")
        left_by_instance = {row["instance"]: row for row in left_cells}
        right_by_instance = {row["instance"]: row for row in right_cells}
        if set(left_by_instance) != set(right_by_instance):
            raise BatchError(f"observable replay cell sets differ for {endpoint}")
        for instance, left_cell in left_by_instance.items():
            right_cell = right_by_instance[instance]
            for key in ("pin", "current_ref"):
                if left_cell.get(key) != right_cell.get(key):
                    raise BatchError(
                        f"observable replay {key} differs for {endpoint}/{instance}"
                    )
            if abs(float(left_cell["delay_ns"]) - float(right_cell["delay_ns"])) > (
                tolerance_ns + 1e-12
            ):
                raise BatchError(
                    f"observable replay delay differs for {endpoint}/{instance}"
                )


def drv(path: Path) -> dict[str, int]:
    text = _text(path)
    headings = list(
        re.finditer(r"(?mi)^\s*Check\s+type\s*:\s*([a-z_]+)\s*$", text)
    )
    result: dict[str, int] = {}
    for index, heading in enumerate(headings):
        name = heading.group(1).lower()
        if name not in {"max_transition", "max_capacitance", "max_fanout"}:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        section = text[heading.end() : end]
        rows = re.findall(
            rf"(?mi)^\s*\|\s*[^|\s][^|]*\|\s*{NUMBER}\s*\|\s*{NUMBER}\s*\|"
            rf"\s*({NUMBER})\s*\|\s*[^|\s][^|]*\|\s*$",
            section,
        )
        if rows:
            if any(float(value) >= 0.0 for value in rows):
                raise BatchError(f"{path}: nonnegative row in {name} violator table")
            result[name] = len(rows)
        elif re.search(r"(?i)\bNo\s+(?:paths?|violations?)\b", section):
            result[name] = 0
        else:
            raise BatchError(f"{path}: cannot prove {name} violation count")
    missing = {"max_transition", "max_capacitance", "max_fanout"} - set(result)
    if missing:
        raise BatchError(f"{path}: missing DRV sections: {sorted(missing)}")
    return result


def connectivity_count(path: Path) -> int:
    text = _text(path)
    summary = re.search(r"(?is)Begin\s+Summary\s*(.*?)\s*End\s+Summary", text)
    if summary is None:
        raise BatchError(f"{path}: missing connectivity summary")
    if re.search(r"(?i)Found\s+no\s+problems?\s+or\s+warnings?", summary.group(1)):
        return 0
    match = re.search(
        r"(?i)Found\s+(\d+)\s+problems?(?:\s+(?:and|,)\s+(\d+)\s+warnings?)?",
        summary.group(1),
    )
    if match is not None:
        return int(match.group(1)) + int(match.group(2) or 0)
    # Innovus 21.10 reports nonzero verifyConnectivity results as one row per
    # IMPVFC category and a total-info line rather than a "Found N" sentence.
    problem_counts = [
        int(value)
        for value in re.findall(
            r"(?mi)^\s*(\d+)\s+Problem\(s\)\s+\(IMPVFC-\d+\):",
            summary.group(1),
        )
    ]
    totals = re.findall(
        r"(?mi)^\s*(\d+)\s+total\s+info\(s\)\s+created\.\s*$",
        summary.group(1),
    )
    if (
        problem_counts
        and len(totals) == 1
        and sum(problem_counts) == int(totals[0])
    ):
        return sum(problem_counts)
    raise BatchError(f"{path}: unrecognized connectivity summary")


def drc_count(path: Path) -> int:
    text = _text(path)
    if re.search(r"(?i)\bNo\s+DRC\s+violations?\s+were\s+found\b", text):
        return 0
    totals = re.findall(
        r"(?mi)^\s*Total\s+Violations\s*:\s*(\d+)\s+Viols?\.\s*$", text
    )
    if len(totals) != 1:
        raise BatchError(f"{path}: missing/ambiguous complete DRC total")
    value = int(totals[0])
    if value >= 1_000_000:
        raise BatchError(f"{path}: DRC report hit its collection limit")
    return value


def placement_legal(path: Path) -> bool:
    text = _text(path)
    if re.search(r"(?i)\b(?:error|illegal|violation)\b", text) and not re.search(
        r"(?i)\b(?:0|no)\s+(?:placement\s+)?(?:errors?|illegal|violations?)\b", text
    ):
        return False
    return bool(text.strip())


def cells(path: Path) -> dict[str, str]:
    rows = _tsv(path)
    result = {row["instance"]: row["ref"] for row in rows}
    if len(result) != len(rows):
        raise BatchError(f"{path}: duplicate cell instance")
    return result


def _instance_set_hash(snapshot: Mapping[str, str]) -> str:
    return hashlib.sha256(
        "".join(f"{name}\n" for name in sorted(snapshot)).encode()
    ).hexdigest()


def normalized_constraint_hash(path: Path) -> str:
    lines = [
        line
        for line in _text(path).splitlines()
        if not re.match(r"^#\s*Generated on:\s+", line)
    ]
    return hashlib.sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest()


def target_paths(path: Path) -> dict[str, dict[str, float]]:
    rows = _tsv(path)
    result: dict[str, dict[str, float]] = {}
    for row in rows:
        result[row["endpoint"]] = {
            "cell_delay_ns": float(row["cell_delay_ns"]),
            "net_delay_ns": float(row["net_delay_ns"]),
            "slack_ns": float(row["slack_ns"]),
        }
    if len(result) != len(rows):
        raise BatchError(f"{path}: duplicate target path")
    return result


def _canonical_slack_map_sha256(slacks: Mapping[str, float]) -> str:
    payload = json.dumps(
        {name: slacks[name] for name in sorted(slacks)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(b"batch100-portability-slack-map-v1\n" + payload).hexdigest()


def checkpoint_portability_slacks(path: Path) -> dict[str, float]:
    rows = _tsv(path)
    if list(rows[0]) != ["endpoint", "slack_ns"]:
        raise BatchError(f"{path}: unexpected portability TSV header")
    result: dict[str, float] = {}
    for row in rows:
        endpoint = row.get("endpoint", "")
        if not endpoint or endpoint in result:
            raise BatchError(f"{path}: empty/duplicate portability endpoint")
        value = float(row["slack_ns"])
        if not math.isfinite(value):
            raise BatchError(f"{path}: non-finite portability slack for {endpoint}")
        result[endpoint] = value
    if len(result) != EXPECTED_PORTABILITY_ENDPOINTS:
        raise BatchError(
            f"{path}: expected {EXPECTED_PORTABILITY_ENDPOINTS} portability "
            f"endpoints, got {len(result)}"
        )
    return result


def checkpoint_portability_evidence(
    reports: Path, targets: Sequence[str], tolerance_ns: float = 0.001
) -> dict[str, Any]:
    path = reports / "checkpoint_portability_slacks.tsv"
    slacks = checkpoint_portability_slacks(path)
    setup = timing(reports / "setup_before.rpt")["slacks"]
    if set(slacks) != set(setup):
        raise BatchError(
            f"{path}: portability/setup_before endpoint sets differ "
            f"({len(slacks)} != {len(setup)})"
        )
    for endpoint, value in slacks.items():
        if abs(value - setup[endpoint]) > tolerance_ns + 1e-12:
            raise BatchError(
                f"{path}: portability/setup_before slack differs by >1 ps "
                f"for {endpoint}"
            )

    target_report = timing(reports / "target_setup_before.rpt")["slacks"]
    target_path_rows = target_paths(reports / "target_paths_before.tsv")
    if set(target_report) != set(targets) or set(target_path_rows) != set(targets):
        raise BatchError(f"{path}: target timing evidence does not exactly cover targets")
    for endpoint in targets:
        for source, value in (
            ("target_setup_before", target_report[endpoint]),
            ("target_paths_before", target_path_rows[endpoint]["slack_ns"]),
        ):
            if abs(slacks[endpoint] - value) > tolerance_ns + 1e-12:
                raise BatchError(
                    f"{path}: portability/{source} slack differs by >1 ps "
                    f"for {endpoint}"
                )
    negative = sorted(name for name, value in slacks.items() if value < 0.0)
    return {
        "schema_version": "mock_lef_batch100.portability_slacks.v1",
        "endpoint_count": len(slacks),
        "negative_endpoints": negative,
        "raw_tsv_sha256": sha256_file(path),
        "canonical_map_sha256": _canonical_slack_map_sha256(slacks),
        "slacks_ns": {name: slacks[name] for name in sorted(slacks)},
    }


def raw_evidence_binding(log_path: Path, reports: Path) -> dict[str, Any]:
    if not log_path.is_file() or log_path.is_symlink():
        raise BatchError(f"missing real Innovus log: {log_path}")
    if not reports.is_dir() or reports.is_symlink():
        raise BatchError(f"missing real Innovus reports directory: {reports}")
    ledger: list[dict[str, Any]] = []
    for path in sorted(reports.rglob("*")):
        relative = path.relative_to(reports).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise BatchError(f"raw reports contain non-regular entry: {path}")
        ledger.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    if not ledger:
        raise BatchError(f"empty Innovus reports directory: {reports}")
    encoded = json.dumps(
        ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "schema_version": "mock_lef_batch100.raw_evidence.v1",
        "tool_log_sha256": sha256_file(log_path),
        "reports_tree_hash_algorithm": RAW_REPORT_TREE_ALGORITHM,
        "reports_tree_sha256": hashlib.sha256(
            b"batch100-raw-report-tree-v1\n" + encoded
        ).hexdigest(),
        "reports_file_count": len(ledger),
    }


def stage(reports: Path, name: str, targets: Sequence[str]) -> dict[str, Any]:
    setup = timing(reports / f"setup_{name}.rpt")
    hold = timing(reports / f"hold_{name}.rpt")
    cell_snapshot = cells(reports / f"cells_{name}.tsv")
    pin_net = reports / f"pin_net_{name}.tsv"
    constraint = reports / (
        "constraint_before.sdc"
        if name in {"before", "after_resize"}
        else "constraint_after.sdc"
    )
    result = {
        "setup_wns_ns": setup["wns_ns"],
        "setup_tns_ns": setup["tns_ns"],
        "hold_wns_ns": hold["wns_ns"],
        "hold_tns_ns": hold["tns_ns"],
        "negative_endpoints": setup["negative_endpoints"],
        "target_slacks_ns": {
            endpoint: setup["slacks"][endpoint] for endpoint in targets
        },
        "drv": drv(reports / f"drv_{name}.rpt"),
        "drc_count": drc_count(reports / f"drc_{name}.rpt"),
        "connectivity_violations": connectivity_count(
            reports / f"connectivity_{name}.rpt"
        ),
        "constraint_sha256": normalized_constraint_hash(constraint),
        "pin_net_connectivity_sha256": sha256_file(pin_net),
        "instance_set_sha256": _instance_set_hash(cell_snapshot),
        "topology_sha256": sha256_file(pin_net),
        "routing_mutations": 0,
        "_cells": cell_snapshot,
    }
    if name == "before":
        result["target_paths"] = target_paths(
            reports / "target_paths_before.tsv"
        )
    return result


def resize_stage(reports: Path, targets: Sequence[str]) -> dict[str, Any]:
    setup = timing(reports / "setup_after_resize.rpt")
    return {
        "setup_wns_ns": setup["wns_ns"],
        "setup_tns_ns": setup["tns_ns"],
        "negative_endpoints": setup["negative_endpoints"],
        "target_slacks_ns": {
            endpoint: setup["slacks"][endpoint] for endpoint in targets
        },
        "routing_mutations": 0,
    }


def replay_record(
    *,
    case_id: str,
    slot: str,
    process_id: str,
    log_path: Path,
    reports: Path,
    targets: Sequence[str],
    protected: Sequence[str],
    fix_sha256: str,
    checkpoint_sha256: str,
    checkpoint_tree_sha256: str,
    checkpoint_tree_hash_algorithm: str,
    checkpoint_bundle_sha256: str,
    checkpoint_bundle_hash_algorithm: str,
    repair_operations: Sequence[Mapping[str, str]],
    sensitivity: Mapping[str, Mapping[str, float]],
    functional_pairs: Mapping[tuple[str, str], bool],
) -> dict[str, Any]:
    before = stage(reports, "before", targets)
    portability = checkpoint_portability_evidence(reports, targets)
    after_resize = resize_stage(reports, targets)
    after = stage(reports, "after_legalize", targets)
    before_cells = before.pop("_cells")
    after_cells = after.pop("_cells")
    if set(before_cells) != set(after_cells):
        raise BatchError(f"{case_id}: replay changed the instance set")
    diffs = []
    for instance in sorted(before_cells):
        if before_cells[instance] == after_cells[instance]:
            continue
        old_ref, new_ref = before_cells[instance], after_cells[instance]
        equivalent = functional_pairs.get((old_ref, new_ref), False)
        diffs.append(
            {
                "instance": instance,
                "old_ref": old_ref,
                "new_ref": new_ref,
                "boolean_equivalent": equivalent,
                "pin_signature_equivalent": equivalent,
                "rvt_equivalent": equivalent,
            }
        )
    expected = {row["instance"]: row["new_ref"] for row in repair_operations}
    if {row["instance"]: row["new_ref"] for row in diffs} != expected:
        raise BatchError(f"{case_id}: measured cell diff differs from frozen repair")
    return {
        "schema_version": "mock_lef_batch100.replay_validation.v2",
        "status": "PASS",
        "slot": slot,
        "process_id": process_id,
        "tool_version": "21.10-p004_1",
        "tool_log_sha256": sha256_file(log_path),
        "raw_evidence": raw_evidence_binding(log_path, reports),
        "checkpoint_portability_slacks": portability,
        "fix_sha256": fix_sha256,
        "violating_checkpoint_sha256": checkpoint_sha256,
        "violating_checkpoint_tree_sha256": checkpoint_tree_sha256,
        "violating_checkpoint_tree_hash_algorithm": checkpoint_tree_hash_algorithm,
        "violating_checkpoint_bundle_sha256": checkpoint_bundle_sha256,
        "violating_checkpoint_bundle_hash_algorithm": (
            checkpoint_bundle_hash_algorithm
        ),
        "hold_gate_applied": False,
        "hold_recorded": True,
        "placement_legal": placement_legal(
            reports / "placement_after_legalize.rpt"
        ),
        "functional_equivalence_pass": all(
            item["boolean_equivalent"] for item in diffs
        ),
        "target_endpoints": list(targets),
        "protected_endpoints": list(protected),
        "before": before,
        "after_resize": after_resize,
        "after_legalize": after,
        "protected_baseline_slacks_ns": {
            endpoint: timing(reports / "setup_before.rpt")["slacks"][endpoint]
            for endpoint in protected
        },
        "protected_after_slacks_ns": {
            endpoint: timing(reports / "setup_after_legalize.rpt")["slacks"][
                endpoint
            ]
            for endpoint in protected
        },
        "cell_diff": diffs,
        "repair_sensitivity_ns": sensitivity,
        "count_evidence": {
            "repair_operations": len(repair_operations),
            "cell_diff": len(diffs),
            "target_endpoints": len(targets),
            "protected_endpoints": len(protected),
            "before_drv": before["drv"],
            "after_drv": after["drv"],
            "before_drc": before["drc_count"],
            "after_drc": after["drc_count"],
            "before_connectivity": before["connectivity_violations"],
            "after_connectivity": after["connectivity_violations"],
        },
    }

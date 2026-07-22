#!/usr/bin/env python3
"""Validate and summarize the Innovus read-only calibration probe.

The collector intentionally cannot produce a Gold decision.  This parser
preserves that boundary: even a complete, fully supported probe is reported as
``PROBE_ONLY_NOT_GOLD``.  It validates typed TSV contracts, checks deterministic
path ranks and exact drive-family flavor, and classifies differences between
the two raw SDC dumps without rewriting either dump.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


class ProbeSummaryError(RuntimeError):
    """Raised when probe evidence violates its typed contract."""


SCHEMAS: dict[str, tuple[str, ...]] = {
    "pre_restore_state.tsv": ("query", "status", "value", "error"),
    "probe_metadata.tsv": ("key", "type", "value"),
    "command_probe.tsv": (
        "command",
        "command_exists",
        "help_status",
        "required_tokens",
        "missing_tokens",
        "raw_help_file",
        "detail",
    ),
    "sdc_write_probe.tsv": (
        "analysis",
        "view",
        "sequence",
        "status",
        "relative_file",
        "bytes",
        "detail",
    ),
    "candidate_paths.tsv": (
        "analysis",
        "view",
        "hierarchy_group",
        "stable_rank",
        "slack_ns",
        "beginpoint",
        "endpoint",
        "driver_pin",
        "driver_inst",
        "driver_ref",
        "net",
        "source_path_index",
    ),
    "path_rejections.tsv": (
        "analysis",
        "source_path_index",
        "slack_ns",
        "beginpoint",
        "endpoint",
        "reason",
        "detail",
    ),
    "lib_signatures.tsv": (
        "reference",
        "lib_cell",
        "library",
        "input_pins",
        "output_functions",
        "signature",
        "status",
        "detail",
    ),
    "drive_families.tsv": (
        "source_ref",
        "source_drive",
        "stem",
        "flavor",
        "tail",
        "variant_ref",
        "variant_drive",
        "active_copy_count",
        "signature_status",
        "equivalent_to_source",
    ),
    "selection_probe.tsv": (
        "query",
        "status",
        "result_count",
        "values",
        "expected",
        "exact_match",
        "error",
    ),
    "report_probe.tsv": (
        "report_command",
        "status",
        "relative_file",
        "bytes",
        "detail",
    ),
    "unsupported_apis.tsv": ("capability", "detail"),
    "artifact_manifest.tsv": ("relative_file", "schema", "row_count", "bytes"),
}

REQUIRED_COMMANDS = {"write_sdc", "rcOut", "refinePlace", "ecoRoute", "optDesign"}
GROUPS = {"exp", "multiplier", "pipeline", "top_tree"}
ANALYSES = {"late", "early"}
DRC_REPORT_LIMIT = 1_000_000
DRIVE_RE = re.compile(r"^(.*)_X([0-9]+(?:P[0-9]+)?)([A-Z]*)_(.+)$")
TIMESTAMP_HINT_RE = re.compile(
    r"(?:generated|created|written|date|timestamp|run\s+at|time\s*:|"
    r"\b(?:mon|tue|wed|thu|fri|sat|sun)\b|"
    r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b|"
    r"\b\d{1,2}:\d{2}(?::\d{2})?\b)",
    re.IGNORECASE,
)
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


def _decode_tsv(value: str) -> str:
    result: list[str] = []
    index = 0
    mapping = {"t": "\t", "n": "\n", "r": "\r", "\\": "\\"}
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            following = value[index + 1]
            if following in mapping:
                result.append(mapping[following])
                index += 2
                continue
        result.append(char)
        index += 1
    return "".join(result)


def _read_tsv(probe_dir: Path, name: str) -> list[dict[str, str]]:
    path = probe_dir / name
    if not path.is_file():
        raise ProbeSummaryError(f"missing required probe artifact: {name}")
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.reader(stream, delimiter="\t", strict=True)
        try:
            raw_header = next(reader)
        except StopIteration as exc:
            raise ProbeSummaryError(f"empty TSV artifact: {name}") from exc
        header = tuple(_decode_tsv(value) for value in raw_header)
        if header != SCHEMAS[name]:
            raise ProbeSummaryError(
                f"{name} header is {header!r}, expected {SCHEMAS[name]!r}"
            )
        rows: list[dict[str, str]] = []
        for line_number, raw_row in enumerate(reader, start=2):
            if len(raw_row) != len(header):
                raise ProbeSummaryError(
                    f"{name}:{line_number} has {len(raw_row)} fields, "
                    f"expected {len(header)}"
                )
            values = [_decode_tsv(value) for value in raw_row]
            rows.append(dict(zip(header, values, strict=True)))
    return rows


def _integer(value: str, field: str, *, minimum: int | None = None) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise ProbeSummaryError(f"{field} is not an integer: {value!r}") from exc
    if minimum is not None and parsed < minimum:
        raise ProbeSummaryError(f"{field} must be >= {minimum}, got {parsed}")
    return parsed


def _number(value: str, field: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ProbeSummaryError(f"{field} is not numeric: {value!r}") from exc
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        raise ProbeSummaryError(f"{field} is not finite: {value!r}")
    return parsed


def _boolean(value: str, field: str) -> bool:
    lowered = value.lower()
    # Tcl's canonical Boolean results are the integers 0 and 1, while the
    # collector also writes the explicit TRUE/FALSE spellings for fields it
    # controls.  Accept exactly those four typed encodings; do not coerce
    # arbitrary truthy strings or numeric values.
    if lowered in {"true", "1"}:
        return True
    if lowered in {"false", "0"}:
        return False
    raise ProbeSummaryError(f"{field} is not a typed Boolean: {value!r}")


def _metadata(rows: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for row in rows:
        key = row["key"]
        if not key or key in values:
            raise ProbeSummaryError(f"duplicate or empty metadata key: {key!r}")
        kind = row["type"]
        raw = row["value"]
        if kind in {"string", "path"}:
            value: Any = raw
        elif kind == "integer":
            value = _integer(raw, f"metadata[{key}]")
        elif kind == "boolean":
            value = _boolean(raw, f"metadata[{key}]")
        else:
            raise ProbeSummaryError(f"unsupported metadata type {kind!r} for {key}")
        values[key] = value
    required = {
        "schema",
        "purpose",
        "process_id",
        "tool_version",
        "baseline_dir",
        "top",
        "setup_view",
        "hold_view",
        "checkpoint",
        "design_mutations",
        "gold_eligible",
    }
    missing = sorted(required - values.keys())
    if missing:
        raise ProbeSummaryError(f"probe metadata lacks keys: {missing}")
    if values["schema"] != "innovus_readonly_probe.v1":
        raise ProbeSummaryError(f"unexpected probe schema: {values['schema']!r}")
    if values["purpose"] != "calibration_only_never_gold":
        raise ProbeSummaryError("collector purpose is not calibration_only_never_gold")
    if values["design_mutations"] != 0 or values["gold_eligible"] is not False:
        raise ProbeSummaryError("collector does not assert zero mutations and non-Gold status")
    return values


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamp_candidate(line: str) -> bool:
    return bool(re.match(r"^\s*#", line) and TIMESTAMP_HINT_RE.search(line))


def _normalize_timestamp_comments(lines: Sequence[str]) -> list[str]:
    return ["# <PROBE_TIMESTAMP_HEADER>\n" if _timestamp_candidate(line) else line for line in lines]


def _changed_line_count(left: Sequence[str], right: Sequence[str]) -> int:
    count = 0
    matcher = difflib.SequenceMatcher(a=left, b=right, autojunk=False)
    for tag, left_start, left_end, right_start, right_end in matcher.get_opcodes():
        if tag != "equal":
            count += max(left_end - left_start, right_end - right_start)
    return count


def _sdc_pair_analysis(probe_dir: Path, label: str, rows: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    selected = sorted(
        (row for row in rows if row["analysis"] == label),
        key=lambda row: _integer(row["sequence"], f"{label}.sequence"),
    )
    if [row["sequence"] for row in selected] != ["1", "2"]:
        raise ProbeSummaryError(f"{label} SDC rows must contain sequences 1 and 2 exactly")
    result: dict[str, Any] = {"analysis": label, "status": "UNSUPPORTED"}
    if any(row["status"] != "OK" for row in selected):
        result["details"] = [row["detail"] for row in selected if row["status"] != "OK"]
        return result
    paths: list[Path] = []
    for row in selected:
        relative = Path(row["relative_file"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ProbeSummaryError(f"unsafe SDC artifact path: {relative}")
        path = probe_dir / relative
        expected_bytes = _integer(row["bytes"], f"{label}.bytes", minimum=1)
        if not path.is_file() or path.stat().st_size != expected_bytes:
            raise ProbeSummaryError(f"{relative} is missing or its byte count changed")
        paths.append(path)
    raw_bytes = [path.read_bytes() for path in paths]
    try:
        text = [value.decode("utf-8") for value in raw_bytes]
    except UnicodeDecodeError as exc:
        raise ProbeSummaryError(f"{label} SDC is not UTF-8 text") from exc
    lines = [value.splitlines(keepends=True) for value in text]
    normalized = [_normalize_timestamp_comments(value) for value in lines]
    raw_identical = raw_bytes[0] == raw_bytes[1]
    normalized_identical = normalized[0] == normalized[1]
    result.update(
        {
            "status": "OK",
            "files": [str(path.relative_to(probe_dir)) for path in paths],
            "sha256": [_sha256(path) for path in paths],
            "raw_identical": raw_identical,
            "normalized_timestamp_comments_identical": normalized_identical,
            "timestamp_header_only_difference": (not raw_identical and normalized_identical),
            "raw_changed_line_count": _changed_line_count(lines[0], lines[1]),
            "normalized_changed_line_count": _changed_line_count(normalized[0], normalized[1]),
            "timestamp_candidate_lines": [
                sum(_timestamp_candidate(line) for line in item) for item in lines
            ],
        }
    )
    return result


def _validate_commands(probe_dir: Path, rows: Sequence[Mapping[str, str]]) -> list[str]:
    names = [row["command"] for row in rows]
    if set(names) != REQUIRED_COMMANDS or len(names) != len(REQUIRED_COMMANDS):
        raise ProbeSummaryError(
            f"command probe names are {sorted(names)}, expected {sorted(REQUIRED_COMMANDS)}"
        )
    unsupported: list[str] = []
    for row in rows:
        exists = _boolean(row["command_exists"], f"command_exists[{row['command']}]")
        if row["help_status"] not in {"SUPPORTED", "UNSUPPORTED"}:
            raise ProbeSummaryError(f"invalid help status for {row['command']}")
        if row["help_status"] != "SUPPORTED":
            unsupported.append(f"help:{row['command']}")
        if row["help_status"] == "SUPPORTED" and row["missing_tokens"]:
            raise ProbeSummaryError(f"supported help for {row['command']} still has missing tokens")
        if row["help_status"] == "SUPPORTED":
            if not exists:
                raise ProbeSummaryError(f"supported command {row['command']} is marked absent")
            relative = Path(row["raw_help_file"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ProbeSummaryError(f"unsafe raw help path: {relative}")
            path = probe_dir / relative
            if not path.is_file() or path.stat().st_size == 0:
                raise ProbeSummaryError(f"raw help evidence is missing for {row['command']}")
    return unsupported


def _validate_candidates(rows: Sequence[Mapping[str, str]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[tuple[int, tuple[Any, ...]]]] = {}
    refs: set[str] = set()
    for index, row in enumerate(rows, start=2):
        analysis = row["analysis"]
        group = row["hierarchy_group"]
        if analysis not in ANALYSES or group not in GROUPS:
            raise ProbeSummaryError(f"candidate_paths.tsv:{index} has invalid analysis/group")
        rank = _integer(row["stable_rank"], f"candidate_paths.tsv:{index}.stable_rank", minimum=0)
        slack = _number(row["slack_ns"], f"candidate_paths.tsv:{index}.slack_ns")
        _integer(row["source_path_index"], f"candidate_paths.tsv:{index}.source_path_index", minimum=0)
        for field in (
            "view",
            "beginpoint",
            "endpoint",
            "driver_pin",
            "driver_inst",
            "driver_ref",
            "net",
        ):
            if not row[field]:
                raise ProbeSummaryError(f"candidate_paths.tsv:{index}.{field} is empty")
        sort_key = (
            slack,
            row["endpoint"],
            row["beginpoint"],
            row["driver_pin"],
            row["driver_inst"],
            row["driver_ref"],
            row["net"],
        )
        grouped.setdefault((analysis, group), []).append((rank, sort_key))
        refs.add(row["driver_ref"])
    counts: dict[str, int] = {}
    for key, values in grouped.items():
        ranks = [rank for rank, _ in values]
        if ranks != list(range(len(values))):
            raise ProbeSummaryError(f"candidate ranks for {key} are not contiguous/in order: {ranks}")
        keys = [sort_key for _, sort_key in values]
        if keys != sorted(keys):
            raise ProbeSummaryError(f"candidate tuples for {key} are not deterministically sorted")
        counts[f"{key[0]}:{key[1]}"] = len(values)
    return {"total_rows": len(rows), "counts": counts, "driver_references": sorted(refs)}


@dataclass(frozen=True)
class DriveName:
    stem: str
    drive: float
    flavor: str
    tail: str


def _parse_drive(reference: str) -> DriveName | None:
    match = DRIVE_RE.fullmatch(reference)
    if not match:
        return None
    return DriveName(
        stem=match.group(1),
        drive=float(match.group(2).replace("P", ".")),
        flavor=match.group(3),
        tail=match.group(4),
    )


def _validate_families(
    rows: Sequence[Mapping[str, str]], candidate_refs: Iterable[str]
) -> tuple[dict[str, Any], list[str]]:
    sources: set[str] = set()
    unsupported: list[str] = []
    variants = 0
    for index, row in enumerate(rows, start=2):
        source = row["source_ref"]
        sources.add(source)
        source_name = _parse_drive(source)
        if source_name is None:
            if row["signature_status"] != "UNSUPPORTED_PATTERN":
                raise ProbeSummaryError(
                    f"drive_families.tsv:{index} invalid source lacks unsupported marker"
                )
            unsupported.append(f"drive_family:{source}")
            continue
        if (
            row["stem"] != source_name.stem
            or row["flavor"] != source_name.flavor
            or row["tail"] != source_name.tail
            or _number(row["source_drive"], f"drive_families.tsv:{index}.source_drive")
            != source_name.drive
        ):
            raise ProbeSummaryError(f"drive_families.tsv:{index} source decomposition mismatch")
        _integer(
            row["active_copy_count"],
            f"drive_families.tsv:{index}.active_copy_count",
            minimum=0,
        )
        variant = row["variant_ref"]
        if not variant:
            unsupported.append(f"drive_family:{source}:{row['signature_status']}")
            continue
        variants += 1
        variant_name = _parse_drive(variant)
        if variant_name is None or (
            variant_name.stem,
            variant_name.flavor,
            variant_name.tail,
        ) != (source_name.stem, source_name.flavor, source_name.tail):
            raise ProbeSummaryError(
                f"drive_families.tsv:{index} changes exact stem/flavor/tail: {source} -> {variant}"
            )
        if _number(row["variant_drive"], f"drive_families.tsv:{index}.variant_drive") != variant_name.drive:
            raise ProbeSummaryError(f"drive_families.tsv:{index} variant drive mismatch")
        if row["signature_status"] != "OK" or row["equivalent_to_source"] != "TRUE":
            unsupported.append(f"drive_signature:{source}->{variant}")
    missing_sources = sorted(set(candidate_refs) - sources)
    if missing_sources:
        raise ProbeSummaryError(f"drive-family evidence lacks candidate references: {missing_sources}")
    return {"source_count": len(sources), "variant_rows": variants}, sorted(set(unsupported))


def _validate_lib_signatures(rows: Sequence[Mapping[str, str]]) -> list[str]:
    unsupported: list[str] = []
    copies: set[str] = set()
    for index, row in enumerate(rows, start=2):
        status = row["status"]
        if status not in {"OK", "UNSUPPORTED"}:
            raise ProbeSummaryError(f"lib_signatures.tsv:{index} has invalid status {status!r}")
        if not row["reference"] or not row["lib_cell"]:
            raise ProbeSummaryError(f"lib_signatures.tsv:{index} lacks reference/lib_cell")
        if row["lib_cell"] in copies:
            raise ProbeSummaryError(f"duplicate active library copy: {row['lib_cell']}")
        copies.add(row["lib_cell"])
        if status == "OK":
            if not row["input_pins"] or not row["output_functions"] or not row["signature"]:
                raise ProbeSummaryError(f"lib_signatures.tsv:{index} has an empty typed signature")
        else:
            unsupported.append(f"lib_signature:{row['reference']}:{row['lib_cell']}")
    return unsupported


def _validate_selection(rows: Sequence[Mapping[str, str]]) -> list[str]:
    expected_queries = {
        "get_db_selected_full_name",
        "get_db_selected_name",
        "dbGet_selected_name",
    }
    if {row["query"] for row in rows} != expected_queries or len(rows) != 3:
        raise ProbeSummaryError("selection probe must contain each of the three exact queries")
    matched = False
    for index, row in enumerate(rows, start=2):
        _integer(row["result_count"], f"selection_probe.tsv:{index}.result_count", minimum=0)
        exact = _boolean(row["exact_match"], f"selection_probe.tsv:{index}.exact_match")
        if row["status"] not in {"OK", "UNSUPPORTED"}:
            raise ProbeSummaryError(f"selection_probe.tsv:{index} has invalid status")
        matched = matched or (row["status"] == "OK" and exact)
    return [] if matched else ["selection:no_exact_query"]


def _validate_probe_drc_report(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ProbeSummaryError(f"DRC report is not UTF-8: {path}: {exc}") from exc
    commands = re.findall(r"(?mi)^#\s*Command:\s*(\S.*\S|\S)\s*$", text)
    if len(commands) != 1 or re.search(
        r"(?i)(?:^|\s)verify_drc(?:\s|$)", commands[0] if commands else ""
    ) is None:
        raise ProbeSummaryError(
            f"{path}: expected exactly one verify_drc Command header"
        )
    command = commands[0]
    occurrences = re.findall(r"(?:^|\s)-limit(?=\s|$)", command, re.IGNORECASE)
    matches = re.findall(
        r"(?:^|\s)-limit\s+(?:\{(\d+)\}|(\d+))(?=\s|$)",
        command,
        re.IGNORECASE,
    )
    limits = [int(braced or plain) for braced, plain in matches]
    if len(occurrences) != 1 or limits != [DRC_REPORT_LIMIT]:
        raise ProbeSummaryError(
            f"{path}: verify_drc Command must contain exactly one "
            f"-limit {DRC_REPORT_LIMIT}"
        )
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if re.search(r"\bnot\b[^\r\n]{0,24}\b(?:reach\w*|exceed\w*)\b", line, re.I):
            continue
        if any(pattern.search(line) for pattern in DRC_TRUNCATION_PATTERNS):
            raise ProbeSummaryError(
                f"{path}: DRC report contains an early-termination/truncation signal: "
                f"{line.strip()}"
            )
    totals = [
        int(value)
        for value in re.findall(
            r"(?mi)^\s*Total\s+Violations\s*:\s*(\d+)\s+Viols?\.\s*$", text
        )
    ]
    if len(totals) != 1:
        raise ProbeSummaryError(f"{path}: expected exactly one DRC total, found {totals}")
    if totals[0] >= DRC_REPORT_LIMIT:
        raise ProbeSummaryError(
            f"{path}: DRC total {totals[0]} reaches collection limit "
            f"{DRC_REPORT_LIMIT}"
        )
    records = re.findall(r"(?m)^\s*([A-Z][A-Z0-9_.-]*):\s+\(", text)
    if len(records) != totals[0]:
        raise ProbeSummaryError(
            f"{path}: DRC report lists {len(records)} records but total is {totals[0]}"
        )


def _validate_report_rows(probe_dir: Path, rows: Sequence[Mapping[str, str]]) -> list[str]:
    expected = {"report_constraint", "verifyConnectivity", "verify_drc"}
    if {row["report_command"] for row in rows} != expected or len(rows) != 3:
        raise ProbeSummaryError("report probe does not contain the three required reports")
    unsupported: list[str] = []
    for row in rows:
        if row["status"] not in {"OK", "UNSUPPORTED"}:
            raise ProbeSummaryError(f"invalid report status for {row['report_command']}")
        bytes_value = _integer(row["bytes"], f"report[{row['report_command']}].bytes", minimum=0)
        relative = Path(row["relative_file"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ProbeSummaryError(f"unsafe report path: {relative}")
        if row["status"] == "OK":
            path = probe_dir / relative
            if not path.is_file() or path.stat().st_size != bytes_value or bytes_value == 0:
                raise ProbeSummaryError(f"report artifact missing or changed: {relative}")
            if row["report_command"] == "verify_drc":
                _validate_probe_drc_report(path)
        else:
            unsupported.append(f"report:{row['report_command']}")
    return unsupported


def _validate_artifact_manifest(
    probe_dir: Path,
    rows_by_name: Mapping[str, Sequence[Mapping[str, str]]],
    manifest_rows: Sequence[Mapping[str, str]],
) -> None:
    entries: dict[str, Mapping[str, str]] = {}
    for row in manifest_rows:
        name = row["relative_file"]
        if name in entries:
            raise ProbeSummaryError(f"duplicate artifact manifest entry: {name}")
        entries[name] = row
    required = set(SCHEMAS) - {"artifact_manifest.tsv"}
    missing = sorted(required - entries.keys())
    if missing:
        raise ProbeSummaryError(f"artifact manifest lacks TSVs: {missing}")
    for name in required:
        row = entries[name]
        row_count = _integer(row["row_count"], f"manifest[{name}].row_count", minimum=0)
        byte_count = _integer(row["bytes"], f"manifest[{name}].bytes", minimum=1)
        if row_count != len(rows_by_name[name]):
            raise ProbeSummaryError(f"artifact manifest row count changed for {name}")
        if (probe_dir / name).stat().st_size != byte_count:
            raise ProbeSummaryError(f"artifact manifest byte count changed for {name}")


def summarize_probe(probe_dir: Path) -> dict[str, Any]:
    probe_dir = probe_dir.resolve()
    if not probe_dir.is_dir():
        raise ProbeSummaryError(f"probe directory does not exist: {probe_dir}")
    rows_by_name = {name: _read_tsv(probe_dir, name) for name in SCHEMAS}
    _validate_artifact_manifest(
        probe_dir,
        rows_by_name,
        rows_by_name["artifact_manifest.tsv"],
    )
    metadata = _metadata(rows_by_name["probe_metadata.tsv"])
    status_path = probe_dir / "probe.status"
    if not status_path.is_file():
        raise ProbeSummaryError("missing probe.status")
    status_text = status_path.read_text(encoding="utf-8")
    if "GOLD_ELIGIBLE=FALSE" not in status_text or "DESIGN_MUTATIONS=0" not in status_text:
        raise ProbeSummaryError("probe.status lacks immutable non-Gold markers")
    complete = "SFT_INNOVUS_READONLY_PROBE_COMPLETE" in status_text
    unsupported: list[str] = []
    unsupported.extend(_validate_commands(probe_dir, rows_by_name["command_probe.tsv"]))
    unsupported.extend(
        f"collector:{row['capability']}" for row in rows_by_name["unsupported_apis.tsv"]
    )
    sdc_analyses = [
        _sdc_pair_analysis(probe_dir, label, rows_by_name["sdc_write_probe.tsv"])
        for label in ("setup", "hold")
    ]
    unsupported.extend(
        f"write_sdc:{item['analysis']}" for item in sdc_analyses if item["status"] != "OK"
    )
    candidate_summary = _validate_candidates(rows_by_name["candidate_paths.tsv"])
    if not rows_by_name["candidate_paths.tsv"]:
        unsupported.append("timing_paths:no_candidates")
    unsupported.extend(_validate_lib_signatures(rows_by_name["lib_signatures.tsv"]))
    family_summary, family_unsupported = _validate_families(
        rows_by_name["drive_families.tsv"], candidate_summary["driver_references"]
    )
    unsupported.extend(family_unsupported)
    unsupported.extend(_validate_selection(rows_by_name["selection_probe.tsv"]))
    unsupported.extend(
        _validate_report_rows(probe_dir, rows_by_name["report_probe.tsv"])
    )
    summary = {
        "schema_version": "innovus_probe_summary.v1",
        "verdict": "PROBE_ONLY_NOT_GOLD",
        "gold_eligible": False,
        "design_mutations": 0,
        "collector_complete": complete,
        "all_probed_apis_supported": not unsupported,
        "unsupported_or_incomplete": sorted(set(unsupported)),
        "metadata": metadata,
        "sdc_diff_analysis": sdc_analyses,
        "candidate_paths": candidate_summary,
        "drive_families": family_summary,
        "path_rejections": {
            "total": len(rows_by_name["path_rejections.tsv"]),
            "by_reason": _count_values(rows_by_name["path_rejections.tsv"], "reason"),
        },
        "report_files": {
            row["report_command"]: row["relative_file"]
            for row in rows_by_name["report_probe.tsv"]
            if row["status"] == "OK"
        },
    }
    return summary


def _count_values(rows: Sequence[Mapping[str, str]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = row[key]
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-dir", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        help="summary JSON path (default: <probe-dir>/probe_summary.json)",
    )
    parser.add_argument(
        "--fail-on-unsupported",
        action="store_true",
        help="return exit 2 after writing the summary if any probed API is unsupported",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        summary = summarize_probe(args.probe_dir)
        output = (args.output or args.probe_dir / "probe_summary.json").resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except ProbeSummaryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"PROBE_ONLY_NOT_GOLD summary={output}")
    if args.fail_on_unsupported and not summary["all_probed_apis_supported"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

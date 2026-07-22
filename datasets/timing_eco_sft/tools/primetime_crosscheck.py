#!/usr/bin/env python3
"""Run, parse, and merge PrimeTime post-ECO setup/hold crosschecks.

``run`` can execute ``pt_shell`` locally or stage inputs on the host and launch
fresh setup/hold processes through SSH.  ``parse`` and ``merge`` are
dependency-free host-side operations for fetched reports.  A Gold metrics
file is never marked as crosschecked from a log substring alone: the parser
validates typed result markers, input/execution bindings, and accompanying
artifact hashes.  Gold execution requires distinct setup-view and hold-view
SDC artifacts; a legacy single SDC is deliberately rejected.  Each immutable
raw Innovus SDC is retained while a deterministic, hash-bound PrimeTime
dialect copy comments its exact ``current_design <top>`` line and translates
only the two exact supported design-rule ``get_designs`` selectors.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence


SCRIPT = Path(__file__).resolve()
DATASET_ROOT = SCRIPT.parents[1]
DEFAULT_TCL = DATASET_ROOT / "pilot_10" / "templates" / "primetime_crosscheck.tcl"
SCHEMA_VERSION = "timing_eco_primetime_crosscheck.v4"
MARKER_PREFIX = "PT_CROSSCHECK_V1"
DEFAULT_PT_SHELL = "/opt/synopsys/prime/R-2020.09-SP4/bin/pt_shell"
DEFAULT_REMOTE_ROOT = "/home/host/nvdla_timing_eco_sft/primetime"
DEFAULT_TOOL_VERSION = "R-2020.09-SP4"
DEFAULT_SYNOPSYS_LC_ROOT = "/opt/synopsys/lc/R-2020.09-SP3"
DEFAULT_THRESHOLD_NS = 0.010
DEFAULT_MAX_VIOLATING_PATHS = 100000
DEFAULT_REPORT_MAX_PATHS = 100
NUMERIC_EPSILON = 1.0e-9
TOKEN_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SSH_TARGET_RE = re.compile(r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9._-]+$")
REMOTE_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_-]{7,95}$")
REMOTE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")

REQUIRED_RESULT_FIELDS = {
    "status",
    "mode",
    "delay_type",
    "top",
    "wns_ns",
    "tns_ns",
    "violating_paths",
    "threshold_ns",
    "parasitics_read",
    "propagated_clocks",
    "clock_count",
    "tool_version",
    "lc_root",
}
ERROR_RESULT_FIELDS = {"status", "mode", "delay_type", "reason"}
REPORT_FILES = {
    "timing": "{mode}_after_pt.rpt",
    "check_timing": "{mode}_check_timing.rpt",
    "global_timing": "{mode}_global_timing.rpt",
    "annotated_parasitics": "{mode}_annotated_parasitics.rpt",
    "unannotated_parasitics": "{mode}_unannotated_parasitics.rpt",
    "analysis_coverage": "{mode}_analysis_coverage.rpt",
    "unconstrained_endpoints": "{mode}_unconstrained_endpoints.rpt",
    "units": "{mode}_units.rpt",
}
INPUT_ROLES = (
    "tcl",
    "netlist",
    "setup_sdc",
    "hold_sdc",
    "setup_lib",
    "hold_lib",
    "setup_spef",
    "hold_spef",
)
STAGED_INPUT_FILENAMES = {
    "tcl": "primetime_crosscheck.tcl",
    "netlist": "post_eco.v",
    "setup_sdc": "constraint_setup_after.sdc",
    "hold_sdc": "constraint_hold_after.sdc",
    "setup_lib": "setup.lib",
    "hold_lib": "hold.lib",
    "setup_spef": "setup.spef",
    "hold_spef": "hold.spef",
}
SDC_ROLE_BY_MODE = {"setup": "setup_sdc", "hold": "hold_sdc"}
SDC_ENV_BY_MODE = {"setup": "PT_SETUP_SDC", "hold": "PT_HOLD_SDC"}
PT_SDC_ROLE_BY_MODE = {"setup": "setup_pt_sdc", "hold": "hold_pt_sdc"}
PT_SDC_FILENAMES = {
    "setup_pt_sdc": "constraint_setup_after.pt.sdc",
    "hold_pt_sdc": "constraint_hold_after.pt.sdc",
}
SDC_ADAPTER_SCHEMA_VERSION = "innovus_to_primetime_sdc.v2"
SDC_ADAPTER_OPERATION = (
    "comment_exact_current_design_and_replace_exact_design_rule_get_designs"
)
SDC_ADAPTER_COMMENT_PREFIX = "# PT_DIALECT_ADAPTER: "
TOP_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:+/-]*$")
PT_LOG_FATAL_RE = re.compile(
    br"(?m)^(?:Error:|PT_CROSSCHECK_ERROR(?:[: ]|$)|"
    br"(?:Warning|Information):[^\r\n]*\(LNK-(?:003|005|043)\)\r?$)"
)


class CrosscheckError(RuntimeError):
    """An actionable PrimeTime crosscheck or evidence error."""


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CrosscheckError(f"missing file: {path}") from exc
    except UnicodeDecodeError as exc:
        raise CrosscheckError(f"file is not valid UTF-8: {path}: {exc}") from exc


def _read_json(path: Path) -> Any:
    try:
        return json.loads(_read_text(path))
    except json.JSONDecodeError as exc:
        raise CrosscheckError(f"invalid JSON in {path}: {exc}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as stream:
            temporary = stream.name
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_nonempty_file(path: Path, label: str) -> Path:
    path = path.resolve()
    if not path.is_file():
        raise CrosscheckError(f"{label} is not a regular file: {path}")
    if path.stat().st_size == 0:
        raise CrosscheckError(f"{label} is empty: {path}")
    return path


def _finite_float(value: str, field: str, where: str) -> float:
    try:
        result = float(value)
    except ValueError as exc:
        raise CrosscheckError(f"{where}: {field} must be numeric, got {value!r}") from exc
    if not math.isfinite(result):
        raise CrosscheckError(f"{where}: {field} must be finite, got {value!r}")
    return result


def _nonnegative_int(value: str, field: str, where: str) -> int:
    if not re.fullmatch(r"[0-9]+", value):
        raise CrosscheckError(
            f"{where}: {field} must be a non-negative integer, got {value!r}"
        )
    return int(value)


def _version_matches(actual: str, required: str | None) -> bool:
    if required is None:
        return True
    return actual == required or actual.startswith(required + "-")


def parse_marker_line(
    line: str,
    *,
    where: str = "PrimeTime result marker",
    expected_mode: str | None = None,
    expected_top: str | None = None,
    expected_threshold_ns: float | None = None,
    required_tool_version: str | None = DEFAULT_TOOL_VERSION,
    expected_lc_root: str | None = DEFAULT_SYNOPSYS_LC_ROOT,
) -> dict[str, Any]:
    """Parse and semantically validate one ``PT_CROSSCHECK_V1`` marker."""

    words = line.strip().split()
    if not words or words[0] != MARKER_PREFIX:
        raise CrosscheckError(f"{where}: missing {MARKER_PREFIX} prefix")
    raw: dict[str, str] = {}
    for word in words[1:]:
        if "=" not in word:
            raise CrosscheckError(f"{where}: malformed marker token {word!r}")
        key, value = word.split("=", 1)
        if not TOKEN_KEY.fullmatch(key) or not value:
            raise CrosscheckError(f"{where}: malformed marker token {word!r}")
        if key in raw:
            raise CrosscheckError(f"{where}: duplicate marker field {key!r}")
        raw[key] = value

    status = raw.get("status")
    if status not in {"PASS", "FAIL", "ERROR"}:
        raise CrosscheckError(f"{where}: status must be PASS, FAIL, or ERROR")
    required = ERROR_RESULT_FIELDS if status == "ERROR" else REQUIRED_RESULT_FIELDS
    missing = sorted(required - set(raw))
    if missing:
        raise CrosscheckError(f"{where}: missing marker fields: {', '.join(missing)}")

    mode = raw["mode"]
    if mode not in {"setup", "hold"}:
        if status == "ERROR" and mode == "unknown":
            if expected_mode is not None:
                raise CrosscheckError(
                    f"{where}: error marker has unknown mode, expected {expected_mode!r}"
                )
            return {
                "status": "ERROR",
                "mode": "unknown",
                "delay_type": raw["delay_type"],
                "reason": raw["reason"],
            }
        raise CrosscheckError(f"{where}: mode must be setup or hold")
    if expected_mode is not None and mode != expected_mode:
        raise CrosscheckError(f"{where}: mode is {mode!r}, expected {expected_mode!r}")

    expected_delay_type = "max" if mode == "setup" else "min"
    if raw["delay_type"] != expected_delay_type:
        raise CrosscheckError(
            f"{where}: {mode} must use delay_type={expected_delay_type}, "
            f"got {raw['delay_type']!r}"
        )
    if status == "ERROR":
        return {
            "status": status,
            "mode": mode,
            "delay_type": expected_delay_type,
            "reason": raw["reason"],
        }

    top = raw["top"]
    if expected_top is not None and top != expected_top:
        raise CrosscheckError(f"{where}: top is {top!r}, expected {expected_top!r}")
    wns = _finite_float(raw["wns_ns"], "wns_ns", where)
    tns = _finite_float(raw["tns_ns"], "tns_ns", where)
    threshold = _finite_float(raw["threshold_ns"], "threshold_ns", where)
    if threshold < 0.0:
        raise CrosscheckError(f"{where}: threshold_ns must be non-negative")
    if expected_threshold_ns is not None and not math.isclose(
        threshold, expected_threshold_ns, abs_tol=NUMERIC_EPSILON
    ):
        raise CrosscheckError(
            f"{where}: threshold_ns={threshold} does not match "
            f"expected {expected_threshold_ns}"
        )
    violating_paths = _nonnegative_int(
        raw["violating_paths"], "violating_paths", where
    )
    parasitics_read = _nonnegative_int(
        raw["parasitics_read"], "parasitics_read", where
    )
    propagated_clocks = _nonnegative_int(
        raw["propagated_clocks"], "propagated_clocks", where
    )
    clock_count = _nonnegative_int(raw["clock_count"], "clock_count", where)
    if parasitics_read != 1:
        raise CrosscheckError(f"{where}: parasitics_read must be 1")
    if propagated_clocks != 1:
        raise CrosscheckError(f"{where}: propagated_clocks must be 1")
    if clock_count < 1:
        raise CrosscheckError(f"{where}: clock_count must be positive")
    if tns > NUMERIC_EPSILON:
        raise CrosscheckError(f"{where}: TNS cannot be positive")
    if violating_paths == 0 and tns < -NUMERIC_EPSILON:
        raise CrosscheckError(f"{where}: negative TNS requires violating paths")
    if violating_paths > 0 and tns >= -NUMERIC_EPSILON:
        raise CrosscheckError(f"{where}: violating paths require negative TNS")
    if violating_paths == 0 and wns < -NUMERIC_EPSILON:
        raise CrosscheckError(f"{where}: negative WNS requires a violating path")
    if violating_paths > 0 and wns >= 0.0:
        raise CrosscheckError(f"{where}: violating paths require negative WNS")

    passes_metrics = (
        wns >= threshold - NUMERIC_EPSILON
        and abs(tns) <= NUMERIC_EPSILON
        and violating_paths == 0
    )
    if (status == "PASS") != passes_metrics:
        raise CrosscheckError(
            f"{where}: status={status} is inconsistent with WNS/TNS/path metrics"
        )
    tool_version = raw["tool_version"]
    if not _version_matches(tool_version, required_tool_version):
        raise CrosscheckError(
            f"{where}: PrimeTime version {tool_version!r} does not match "
            f"required {required_tool_version!r}"
        )
    lc_root = _validate_synopsys_lc_root(raw["lc_root"])
    if expected_lc_root is not None and lc_root != expected_lc_root:
        raise CrosscheckError(
            f"{where}: SYNOPSYS_LC_ROOT {lc_root!r} does not match "
            f"expected {expected_lc_root!r}"
        )

    return {
        "status": status,
        "mode": mode,
        "delay_type": expected_delay_type,
        "top": top,
        "wns_ns": wns,
        "tns_ns": tns,
        "violating_paths": violating_paths,
        "threshold_ns": threshold,
        "parasitics_read": True,
        "propagated_clocks": True,
        "clock_count": clock_count,
        "tool_version": tool_version,
        "lc_root": lc_root,
    }


def parse_result_file(
    path: Path,
    *,
    expected_mode: str,
    expected_top: str | None = None,
    expected_threshold_ns: float | None = None,
    required_tool_version: str | None = DEFAULT_TOOL_VERSION,
    expected_lc_root: str | None = DEFAULT_SYNOPSYS_LC_ROOT,
) -> dict[str, Any]:
    """Parse a result file containing exactly one non-empty marker line."""

    lines = [line.strip() for line in _read_text(path).splitlines() if line.strip()]
    if len(lines) != 1:
        raise CrosscheckError(
            f"{path}: expected exactly one non-empty result marker, found {len(lines)}"
        )
    return parse_marker_line(
        lines[0],
        where=str(path),
        expected_mode=expected_mode,
        expected_top=expected_top,
        expected_threshold_ns=expected_threshold_ns,
        required_tool_version=required_tool_version,
        expected_lc_root=expected_lc_root,
    )


def _artifact(path: Path, root: Path, label: str) -> dict[str, Any]:
    path = _regular_nonempty_file(path, label)
    root = root.resolve()
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise CrosscheckError(f"{label} must be under summary root {root}: {path}") from exc
    return {
        "path": relative,
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
    }


def _approved_tcl_contract() -> dict[str, Any]:
    approved = _regular_nonempty_file(DEFAULT_TCL, "approved PrimeTime crosscheck Tcl")
    return {"sha256": _sha256(approved), "bytes": approved.stat().st_size}


def _verify_pt_text_evidence_clean(path: Path, where: str) -> None:
    data = _regular_nonempty_file(path, where).read_bytes()
    match = PT_LOG_FATAL_RE.search(data)
    if match is not None:
        line_number = data.count(b"\n", 0, match.start()) + 1
        token = match.group(0).decode("ascii", errors="replace")
        raise CrosscheckError(
            f"{where}: fatal PrimeTime transcript marker {token!r} at line {line_number}"
        )


def _adapt_innovus_sdc_bytes(
    raw: bytes, top: str
) -> tuple[bytes, int, dict[str, int]]:
    """Return a byte-preserving PrimeTime dialect adaptation of an Innovus SDC.

    Innovus writes ``current_design <top>`` as a command.  PrimeTime's
    ``current_design`` is a zero-argument query and the design is already made
    current by ``link_design``.  Innovus also emits up to one exact
    ``set_max_fanout`` and ``set_max_transition`` design rule using its
    unsupported ``get_designs`` collection command.  Those two selectors must
    either both be absent or both be present; when present, only their exact
    ``[get_designs {<top>}]`` token is replaced with ``[current_design]``.
    Line endings and every byte outside those bounded command-line edits remain
    untouched.
    """

    if not isinstance(raw, bytes) or not raw:
        raise CrosscheckError("Innovus SDC bytes must be non-empty")
    if not isinstance(top, str) or not TOP_TOKEN_RE.fullmatch(top):
        raise CrosscheckError(
            "PrimeTime top must be a safe unescaped design-name token"
        )
    try:
        top_bytes = top.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CrosscheckError("PrimeTime top is not valid UTF-8") from exc

    lines = raw.splitlines(keepends=True)
    if not lines:
        raise CrosscheckError("Innovus SDC contains no lines")
    candidates: list[tuple[int, bytes, bytes]] = []
    design_rule_candidates: dict[str, tuple[int, bytes, bytes, bytes]] = {}
    escaped_top = re.escape(top_bytes)
    design_rule_patterns = {
        "set_max_fanout": re.compile(
            br"^(set_max_fanout [0-9]+(?:\.[0-9]+)?  )"
            br"\[get_designs \{" + escaped_top + br"\}\]$"
        ),
        "set_max_transition": re.compile(
            br"^(set_max_transition [0-9]+(?:\.[0-9]+)?  )"
            br"\[get_designs \{" + escaped_top + br"\}\]$"
        ),
    }
    for index, line in enumerate(lines):
        if line.endswith(b"\r\n"):
            body, ending = line[:-2], b"\r\n"
        elif line.endswith((b"\n", b"\r")):
            body, ending = line[:-1], line[-1:]
        else:
            body, ending = line, b""
        if re.match(br"^[ \t]*current_design(?:[ \t]|$)", body):
            candidates.append((index, body, ending))
        matched_design_rule = False
        for command, pattern in design_rule_patterns.items():
            match = pattern.fullmatch(body)
            if match is None:
                continue
            if command in design_rule_candidates:
                raise CrosscheckError(
                    f"Innovus SDC contains multiple exact {command} get_designs lines"
                )
            design_rule_candidates[command] = (
                index,
                body,
                ending,
                match.group(1),
            )
            matched_design_rule = True
            break
        if b"get_designs" in body and not matched_design_rule:
            raise CrosscheckError(
                "Innovus SDC contains an unsupported get_designs occurrence; "
                "only the exact design-level set_max_fanout and "
                "set_max_transition forms may be adapted"
            )

    if len(candidates) != 1:
        raise CrosscheckError(
            "Innovus SDC must contain exactly one current_design command line; "
            f"found {len(candidates)}"
        )
    index, body, ending = candidates[0]
    expected = b"current_design " + top_bytes
    if body != expected:
        raise CrosscheckError(
            "Innovus SDC current_design line does not exactly match "
            f"'current_design {top}'"
        )

    prefix = SDC_ADAPTER_COMMENT_PREFIX.encode("ascii")
    adapted_lines = list(lines)
    adapted_lines[index] = prefix + body + ending
    expected_design_rules = {"set_max_fanout", "set_max_transition"}
    present_design_rules = set(design_rule_candidates)
    if present_design_rules not in (set(), expected_design_rules):
        missing = sorted(expected_design_rules - present_design_rules)
        raise CrosscheckError(
            "Innovus SDC design-level get_designs rules must be absent or a "
            f"complete pair; missing {', '.join(missing)}"
        )
    design_rule_lines: dict[str, int] = {}
    for command in sorted(design_rule_candidates):
        rule_index, _rule_body, rule_ending, rule_prefix = design_rule_candidates[
            command
        ]
        adapted_lines[rule_index] = rule_prefix + b"[current_design]" + rule_ending
        design_rule_lines[command] = rule_index + 1
    return b"".join(adapted_lines), index + 1, design_rule_lines


def _input_bundle_digest(
    inputs: Mapping[str, Mapping[str, Any]],
    adapted_sdcs: Mapping[str, Mapping[str, Any]],
    *,
    top: str,
) -> str:
    if not isinstance(top, str) or not TOP_TOKEN_RE.fullmatch(top):
        raise CrosscheckError("PrimeTime bundle top is invalid")
    normalized_inputs: dict[str, dict[str, Any]] = {}
    for role in INPUT_ROLES:
        item = inputs.get(role)
        if not isinstance(item, Mapping):
            raise CrosscheckError(f"PrimeTime bundle input {role} metadata is missing")
        digest = str(item.get("sha256", item.get("source_sha256", ""))).lower()
        size = item.get("bytes", item.get("source_bytes"))
        if (
            not SHA256_RE.fullmatch(digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
        ):
            raise CrosscheckError(f"PrimeTime bundle input {role} metadata is invalid")
        normalized_inputs[role] = {"sha256": digest, "bytes": size}

    normalized_adapted: dict[str, dict[str, Any]] = {}
    for pt_role in PT_SDC_ROLE_BY_MODE.values():
        item = adapted_sdcs.get(pt_role)
        if not isinstance(item, Mapping):
            raise CrosscheckError(f"PrimeTime bundle {pt_role} metadata is missing")
        digest = str(item.get("sha256", "")).lower()
        size = item.get("bytes")
        if (
            not SHA256_RE.fullmatch(digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
        ):
            raise CrosscheckError(f"PrimeTime bundle {pt_role} metadata is invalid")
        normalized_adapted[pt_role] = {"sha256": digest, "bytes": size}

    identity = json.dumps(
        {
            "adapter": {
                "schema_version": SDC_ADAPTER_SCHEMA_VERSION,
                "operation": SDC_ADAPTER_OPERATION,
                "comment_prefix": SDC_ADAPTER_COMMENT_PREFIX,
                "top": top,
            },
            "inputs": normalized_inputs,
            "adapted_sdcs": normalized_adapted,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def _stage_input_bundle(
    source_paths: Mapping[str, Path], output_root: Path, *, top: str
) -> tuple[
    dict[str, Path],
    dict[str, dict[str, Any]],
    dict[str, Any],
]:
    """Copy exact PT inputs into a content-addressed, read-only bundle.

    The source is hashed before and after copying, and every staged file must
    match that digest.  PrimeTime is then pointed only at the staged paths, so
    setup and hold cannot observe different bytes if a caller mutates a source
    file while the two processes are running.
    """

    if set(source_paths) != set(INPUT_ROLES):
        missing = sorted(set(INPUT_ROLES) - set(source_paths))
        extra = sorted(set(source_paths) - set(INPUT_ROLES))
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unexpected {', '.join(extra)}")
        raise CrosscheckError("invalid PrimeTime input bundle: " + "; ".join(details))

    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    sources: dict[str, Path] = {}
    source_metadata: dict[str, dict[str, Any]] = {}
    for role in INPUT_ROLES:
        source = _regular_nonempty_file(source_paths[role], f"PrimeTime {role} source")
        sources[role] = source
        source_metadata[role] = {
            "source_path": str(source),
            "source_sha256": _sha256(source),
            "source_bytes": source.stat().st_size,
        }

    adapted_bytes: dict[str, bytes] = {}
    adapted_line_numbers: dict[str, int] = {}
    adapted_design_rule_lines: dict[str, dict[str, int]] = {}
    adapted_metadata: dict[str, dict[str, Any]] = {}
    for mode in ("setup", "hold"):
        source_role = SDC_ROLE_BY_MODE[mode]
        pt_role = PT_SDC_ROLE_BY_MODE[mode]
        raw = sources[source_role].read_bytes()
        if (
            hashlib.sha256(raw).hexdigest()
            != source_metadata[source_role]["source_sha256"]
            or len(raw) != source_metadata[source_role]["source_bytes"]
        ):
            raise CrosscheckError(
                f"PrimeTime {source_role} source changed before SDC adaptation"
            )
        adapted, line_number, design_rule_lines = _adapt_innovus_sdc_bytes(raw, top)
        adapted_bytes[pt_role] = adapted
        adapted_line_numbers[mode] = line_number
        adapted_design_rule_lines[mode] = design_rule_lines
        adapted_metadata[pt_role] = {
            "sha256": hashlib.sha256(adapted).hexdigest(),
            "bytes": len(adapted),
        }

    bundle_digest = _input_bundle_digest(
        {
            role: {
                "sha256": source_metadata[role]["source_sha256"],
                "bytes": source_metadata[role]["source_bytes"],
            }
            for role in INPUT_ROLES
        },
        adapted_metadata,
        top=top,
    )
    staging_parent = output_root / "inputs"
    staging_parent.mkdir(parents=True, exist_ok=True)
    try:
        staging_parent.resolve().relative_to(output_root)
    except ValueError as exc:
        raise CrosscheckError(
            f"PrimeTime staging directory escapes output root: {staging_parent}"
        ) from exc
    stage_dir = staging_parent / f"primetime_{bundle_digest}"

    temporary: Path | None = None
    if not stage_dir.exists():
        temporary = Path(tempfile.mkdtemp(prefix=".primetime_stage.", dir=staging_parent))
        try:
            for role in INPUT_ROLES:
                destination = temporary / STAGED_INPUT_FILENAMES[role]
                shutil.copyfile(sources[role], destination)
                if _sha256(destination) != source_metadata[role]["source_sha256"]:
                    raise CrosscheckError(
                        f"PrimeTime {role} changed while it was copied into staging"
                    )
                if destination.stat().st_size != source_metadata[role]["source_bytes"]:
                    raise CrosscheckError(
                        f"PrimeTime {role} staged byte count does not match its source"
                    )
                destination.chmod(0o444)
            for pt_role in PT_SDC_ROLE_BY_MODE.values():
                destination = temporary / PT_SDC_FILENAMES[pt_role]
                destination.write_bytes(adapted_bytes[pt_role])
                if (
                    _sha256(destination) != adapted_metadata[pt_role]["sha256"]
                    or destination.stat().st_size
                    != adapted_metadata[pt_role]["bytes"]
                ):
                    raise CrosscheckError(
                        f"PrimeTime {pt_role} changed while it was staged"
                    )
                destination.chmod(0o444)
            temporary.chmod(0o555)
            try:
                temporary.rename(stage_dir)
                temporary = None
            except OSError:
                # A concurrent identical run may have installed the same
                # content-addressed bundle first.  Verify it below.
                if not stage_dir.is_dir():
                    raise
        finally:
            if temporary is not None and temporary.exists():
                temporary.chmod(0o700)
                shutil.rmtree(temporary)
    if not stage_dir.is_dir():
        raise CrosscheckError(f"PrimeTime staged input bundle is not a directory: {stage_dir}")

    staged_paths: dict[str, Path] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for role in INPUT_ROLES:
        staged = _regular_nonempty_file(
            stage_dir / STAGED_INPUT_FILENAMES[role], f"staged PrimeTime {role}"
        )
        expected_digest = source_metadata[role]["source_sha256"]
        expected_bytes = source_metadata[role]["source_bytes"]
        if _sha256(staged) != expected_digest or staged.stat().st_size != expected_bytes:
            raise CrosscheckError(
                f"staged PrimeTime {role} does not match its content-addressed source"
            )
        # Reassert read-only permissions when reusing an existing valid bundle.
        staged.chmod(0o444)
        staged_paths[role] = staged
        metadata[role] = {
            **_artifact(staged, output_root, f"staged PrimeTime {role}"),
            **source_metadata[role],
        }
    for pt_role in PT_SDC_ROLE_BY_MODE.values():
        staged = _regular_nonempty_file(
            stage_dir / PT_SDC_FILENAMES[pt_role],
            f"staged PrimeTime {pt_role}",
        )
        if (
            _sha256(staged) != adapted_metadata[pt_role]["sha256"]
            or staged.stat().st_size != adapted_metadata[pt_role]["bytes"]
            or staged.read_bytes() != adapted_bytes[pt_role]
        ):
            raise CrosscheckError(
                f"staged PrimeTime {pt_role} does not match deterministic adaptation"
            )
        staged.chmod(0o444)
        staged_paths[pt_role] = staged
    stage_dir.chmod(0o555)

    # Detect a source rewrite that occurred after its staged copy was made.
    for role in INPUT_ROLES:
        if (
            _sha256(sources[role]) != source_metadata[role]["source_sha256"]
            or sources[role].stat().st_size != source_metadata[role]["source_bytes"]
        ):
            raise CrosscheckError(
                f"PrimeTime {role} source changed while the immutable bundle was staged"
            )

    adaptation: dict[str, Any] = {
        "schema_version": SDC_ADAPTER_SCHEMA_VERSION,
        "operation": SDC_ADAPTER_OPERATION,
        "comment_prefix": SDC_ADAPTER_COMMENT_PREFIX,
        "top": top,
    }
    for mode in ("setup", "hold"):
        source_role = SDC_ROLE_BY_MODE[mode]
        pt_role = PT_SDC_ROLE_BY_MODE[mode]
        adaptation[mode] = {
            "source_role": source_role,
            "source_artifact": metadata[source_role]["path"],
            "source_sha256": metadata[source_role]["sha256"],
            "source_bytes": metadata[source_role]["bytes"],
            "current_design_line": adapted_line_numbers[mode],
            "design_rule_get_designs_lines": adapted_design_rule_lines[mode],
            "adapted_artifact": _artifact(
                staged_paths[pt_role], output_root, f"staged PrimeTime {pt_role}"
            ),
        }
    return staged_paths, metadata, adaptation


def _execution_input_bindings(
    mode: str,
    input_metadata: Mapping[str, Mapping[str, Any]],
    sdc_adaptation: Mapping[str, Any],
) -> dict[str, str]:
    if mode not in {"setup", "hold"}:
        raise CrosscheckError(f"invalid PrimeTime execution mode: {mode}")
    roles = (
        "tcl",
        "netlist",
        f"{mode}_sdc",
        f"{mode}_lib",
        f"{mode}_spef",
    )
    bindings = {role: str(input_metadata[role]["sha256"]) for role in roles}
    adapted = sdc_adaptation.get(mode)
    if not isinstance(adapted, Mapping):
        raise CrosscheckError(f"missing PrimeTime SDC adaptation for {mode}")
    artifact = adapted.get("adapted_artifact")
    if not isinstance(artifact, Mapping) or not isinstance(artifact.get("sha256"), str):
        raise CrosscheckError(f"invalid PrimeTime SDC adaptation for {mode}")
    bindings[PT_SDC_ROLE_BY_MODE[mode]] = str(artifact["sha256"])
    return bindings


def _sanitized_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return inherited environment state with every PT_* key removed."""

    inherited = os.environ if base is None else base
    return {key: value for key, value in inherited.items() if not key.startswith("PT_")}


def _resolve_local_executable(value: str, label: str) -> str:
    if os.path.sep not in value:
        resolved = shutil.which(value)
        if resolved is None:
            raise CrosscheckError(f"{label} executable not found in PATH: {value}")
        return resolved
    return str(_regular_nonempty_file(Path(value), f"{label} executable"))


def _validate_ssh_target(value: str) -> str:
    if not isinstance(value, str) or not SSH_TARGET_RE.fullmatch(value):
        raise CrosscheckError(f"unsafe SSH target: {value!r}")
    pieces = value.split("@", 1)
    if any(piece.startswith("-") or piece in {".", ".."} for piece in pieces):
        raise CrosscheckError(f"unsafe SSH target: {value!r}")
    return value


def _validate_remote_root(value: str) -> str:
    if (
        not isinstance(value, str)
        or not REMOTE_PATH_RE.fullmatch(value)
        or value.endswith("/")
        or "//" in value
    ):
        raise CrosscheckError(f"unsafe remote root: {value!r}")
    raw_parts = value.split("/")[1:]
    if len(raw_parts) < 3 or any(part in {"", ".", ".."} for part in raw_parts):
        raise CrosscheckError(f"unsafe remote root: {value!r}")
    normalized = PurePosixPath(value).as_posix()
    if normalized != value or normalized in {"/", "/home", "/tmp"}:
        raise CrosscheckError(f"unsafe remote root: {value!r}")
    return normalized


def _validate_synopsys_lc_root(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or not value.startswith("/")
        or not REMOTE_PATH_RE.fullmatch(value)
        or value.endswith("/")
        or "//" in value
        or any(part in {"", ".", ".."} for part in value.split("/")[1:])
        or PurePosixPath(value).as_posix() != value
    ):
        raise CrosscheckError(f"unsafe or empty SYNOPSYS_LC_ROOT: {value!r}")
    return value


def _validate_remote_identifier(value: str) -> str:
    if not isinstance(value, str) or not REMOTE_IDENTIFIER_RE.fullmatch(value):
        raise CrosscheckError(f"unsafe remote invocation identifier: {value!r}")
    return value


def _validate_remote_executable(value: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CrosscheckError(f"unsafe remote {label} executable: {value!r}")
    if value.startswith("/"):
        if (
            not REMOTE_PATH_RE.fullmatch(value)
            or "//" in value
            or any(part in {"", ".", ".."} for part in value.split("/")[1:])
            or PurePosixPath(value).as_posix() != value
        ):
            raise CrosscheckError(f"unsafe remote {label} executable: {value!r}")
        return value
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value) or value.startswith("-"):
        raise CrosscheckError(f"unsafe remote {label} executable: {value!r}")
    return value


def _new_remote_invocation_id() -> str:
    return _validate_remote_identifier(f"run_{uuid.uuid4().hex}")


def _remote_join(root: str, *parts: str) -> str:
    for part in parts:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", part) or part in {".", ".."}:
            raise CrosscheckError(f"unsafe remote path component: {part!r}")
    joined = posixpath.join(root, *parts)
    if not REMOTE_PATH_RE.fullmatch(joined):
        raise CrosscheckError(f"unsafe remote path: {joined!r}")
    return joined


def _ssh_command(ssh_executable: str, target: str, script: str) -> list[str]:
    if not script or "\x00" in script:
        raise CrosscheckError("remote shell script is empty or contains NUL")
    return [ssh_executable, target, "sh -lc " + shlex.quote(script)]


def _run_required_command(
    command: Sequence[str], *, timeout_seconds: int, label: str
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            list(command),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CrosscheckError(f"{label} timed out after {timeout_seconds} seconds") from exc
    except OSError as exc:
        raise CrosscheckError(f"{label} could not start: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise CrosscheckError(
            f"{label} failed with exit code {completed.returncode}{suffix}"
        )
    return completed


def _remote_bootstrap_script(remote_root: str, run_root: str) -> str:
    bundles_parent = _remote_join(remote_root, "runs")
    input_dir = _remote_join(run_root, "inputs")
    report_parent = _remote_join(run_root, "reports")
    report_dir = _remote_join(report_parent, "primetime")
    log_dir = _remote_join(run_root, "logs")
    home_dir = _remote_join(run_root, "home")
    work_dir = _remote_join(run_root, "work")
    return "\n".join(
        (
            "set -eu",
            "umask 077",
            f"mkdir -p -- {shlex.quote(remote_root)} {shlex.quote(bundles_parent)}",
            f"if test -e {shlex.quote(run_root)}; then exit 73; fi",
            f"mkdir -- {shlex.quote(run_root)}",
            f"mkdir -- {shlex.quote(input_dir)} {shlex.quote(report_parent)} "
            f"{shlex.quote(log_dir)} {shlex.quote(home_dir)} {shlex.quote(work_dir)}",
            f"mkdir -- {shlex.quote(report_dir)}",
        )
    )


def _remote_bundle_verify_script(
    remote_bundle: str,
    input_metadata: Mapping[str, Mapping[str, Any]],
    sdc_adaptation: Mapping[str, Any],
) -> str:
    lines = ["set -eu", f"test -d {shlex.quote(remote_bundle)}"]
    for role in INPUT_ROLES:
        remote_file = _remote_join(remote_bundle, STAGED_INPUT_FILENAMES[role])
        digest = str(input_metadata[role]["sha256"])
        size = int(input_metadata[role]["bytes"])
        lines.extend(
            (
                f"test -f {shlex.quote(remote_file)}",
                f"printf '%s  %s\\n' {shlex.quote(digest)} "
                f"{shlex.quote(remote_file)} | sha256sum -c -",
                f"test \"$(wc -c < {shlex.quote(remote_file)})\" -eq {size}",
                f"chmod 0444 -- {shlex.quote(remote_file)}",
            )
        )
    for mode in ("setup", "hold"):
        pt_role = PT_SDC_ROLE_BY_MODE[mode]
        remote_file = _remote_join(remote_bundle, PT_SDC_FILENAMES[pt_role])
        mode_adaptation = sdc_adaptation.get(mode)
        if not isinstance(mode_adaptation, Mapping):
            raise CrosscheckError(f"missing {mode} SDC adaptation metadata")
        artifact = mode_adaptation.get("adapted_artifact")
        if not isinstance(artifact, Mapping):
            raise CrosscheckError(f"invalid {mode} SDC adaptation metadata")
        digest = str(artifact.get("sha256", ""))
        size = artifact.get("bytes")
        if not SHA256_RE.fullmatch(digest) or not isinstance(size, int):
            raise CrosscheckError(f"invalid {mode} adapted SDC hash metadata")
        lines.extend(
            (
                f"test -f {shlex.quote(remote_file)}",
                f"printf '%s  %s\\n' {shlex.quote(digest)} "
                f"{shlex.quote(remote_file)} | sha256sum -c -",
                f"test \"$(wc -c < {shlex.quote(remote_file)})\" -eq {size}",
                f"chmod 0444 -- {shlex.quote(remote_file)}",
            )
        )
    lines.append(f"chmod 0555 -- {shlex.quote(remote_bundle)}")
    return "\n".join(lines)


def _remote_pt_script(
    *,
    mode: str,
    top: str,
    threshold_ns: float,
    max_violating_paths: int,
    report_max_paths: int,
    remote_pt_shell: str,
    synopsys_lc_root: str,
    remote_inputs: Mapping[str, str],
    remote_report_dir: str,
    remote_log_path: str,
    remote_home: str,
    remote_work_dir: str,
) -> str:
    if mode not in {"setup", "hold"}:
        raise CrosscheckError(f"invalid PrimeTime remote mode: {mode}")
    stale = [
        _remote_join(remote_report_dir, f"{mode}.result"),
        *(
            _remote_join(remote_report_dir, pattern.format(mode=mode))
            for pattern in REPORT_FILES.values()
        ),
        remote_log_path,
    ]
    remote_home = _validate_remote_root(remote_home)
    remote_work_dir = _validate_remote_root(remote_work_dir)
    lines = ["set -u"]
    for path in stale:
        lines.append(f"test ! -e {shlex.quote(path)} || exit 73")

    lines.extend(
        (
            f"test -d {shlex.quote(remote_home)}",
            f"test -d {shlex.quote(remote_work_dir)}",
            f"cd -- {shlex.quote(remote_work_dir)}",
        )
    )
    passthrough = (
        'USER="${USER-}"',
        'LOGNAME="${LOGNAME-}"',
        'PATH="${PATH-/usr/local/bin:/usr/bin:/bin}"',
        'TMPDIR="${TMPDIR-/tmp}"',
        'LANG="${LANG-C}"',
        'LC_ALL="${LC_ALL-}"',
        'LD_LIBRARY_PATH="${LD_LIBRARY_PATH-}"',
        'LM_LICENSE_FILE="${LM_LICENSE_FILE-}"',
        'SNPSLMD_LICENSE_FILE="${SNPSLMD_LICENSE_FILE-}"',
    )
    explicit = {
        "HOME": remote_home,
        "SYNOPSYS_LC_ROOT": _validate_synopsys_lc_root(synopsys_lc_root),
        "PT_TOP": top,
        "PT_NETLIST": remote_inputs["netlist"],
        "PT_LIB": remote_inputs[f"{mode}_lib"],
        SDC_ENV_BY_MODE[mode]: remote_inputs[PT_SDC_ROLE_BY_MODE[mode]],
        "PT_SPEF": remote_inputs[f"{mode}_spef"],
        "PT_MODE": mode,
        "PT_REPORT_DIR": remote_report_dir,
        "PT_MIN_SLACK_NS": format(threshold_ns, ".9f"),
        "PT_MAX_VIOLATING_PATHS": str(max_violating_paths),
        "PT_REPORT_MAX_PATHS": str(report_max_paths),
    }
    env_words = ["env", "-i", *passthrough]
    env_words.extend(f"{key}={shlex.quote(value)}" for key, value in explicit.items())
    env_words.extend(
        (
            shlex.quote(remote_pt_shell),
            "-no_init",
            "-f",
            shlex.quote(remote_inputs["tcl"]),
        )
    )
    lines.append(
        "exec "
        + " ".join(env_words)
        + f" > {shlex.quote(remote_log_path)} 2>&1"
    )
    return "\n".join(lines)


def _reject_stale_remote_fetch_targets(
    output_root: Path, report_dir: Path, log_dir: Path
) -> None:
    stale = [output_root / "primetime_crosscheck.json"]
    for mode in ("setup", "hold"):
        stale.append(report_dir / f"{mode}.result")
        stale.extend(
            report_dir / pattern.format(mode=mode) for pattern in REPORT_FILES.values()
        )
        stale.append(log_dir / f"primetime_{mode}.log")
    existing = [path for path in stale if path.exists()]
    if existing:
        shown = ", ".join(str(path) for path in existing[:3])
        raise CrosscheckError(
            "remote PrimeTime output directory contains stale invocation evidence: "
            + shown
        )


def _run_remote_modes(
    args: argparse.Namespace,
    *,
    inputs: Mapping[str, Path],
    input_metadata: Mapping[str, Mapping[str, Any]],
    sdc_adaptation: Mapping[str, Any],
    output_root: Path,
    report_dir: Path,
    log_dir: Path,
    ssh_executable: str,
    rsync_executable: str,
) -> dict[str, Any]:
    target = _validate_ssh_target(str(args.ssh_target))
    remote_root = _validate_remote_root(str(args.remote_root))
    remote_pt_shell = _validate_remote_executable(str(args.pt_shell), "PrimeTime")
    invocation_id = _new_remote_invocation_id()
    remote_run_root = _remote_join(remote_root, "runs", invocation_id)

    stage_dirs = {path.resolve().parent for path in inputs.values()}
    if len(stage_dirs) != 1:
        raise CrosscheckError("PrimeTime staged inputs do not share one immutable bundle")
    local_bundle = stage_dirs.pop()
    bundle_name = _validate_remote_identifier(local_bundle.name)
    remote_bundle = _remote_join(remote_run_root, "inputs", bundle_name)
    remote_report_dir = _remote_join(remote_run_root, "reports", "primetime")
    remote_log_dir = _remote_join(remote_run_root, "logs")
    remote_home = _remote_join(remote_run_root, "home")
    remote_work_dir = _remote_join(remote_run_root, "work")

    _reject_stale_remote_fetch_targets(output_root, report_dir, log_dir)
    bootstrap = _ssh_command(
        ssh_executable,
        target,
        _remote_bootstrap_script(remote_root, remote_run_root),
    )
    _run_required_command(
        bootstrap,
        timeout_seconds=args.timeout_seconds,
        label="remote PrimeTime invocation-directory creation",
    )

    upload = [
        rsync_executable,
        "-a",
        "--checksum",
        "--",
        str(local_bundle) + "/",
        f"{target}:{remote_bundle}/",
    ]
    _run_required_command(
        upload,
        timeout_seconds=args.timeout_seconds,
        label="remote PrimeTime input upload",
    )
    verify = _ssh_command(
        ssh_executable,
        target,
        _remote_bundle_verify_script(remote_bundle, input_metadata, sdc_adaptation),
    )
    _run_required_command(
        verify,
        timeout_seconds=args.timeout_seconds,
        label="remote PrimeTime input verification",
    )

    remote_inputs = {
        role: _remote_join(remote_bundle, STAGED_INPUT_FILENAMES[role])
        for role in INPUT_ROLES
    }
    remote_inputs.update(
        {
            pt_role: _remote_join(remote_bundle, PT_SDC_FILENAMES[pt_role])
            for pt_role in PT_SDC_ROLE_BY_MODE.values()
        }
    )
    pending: dict[str, dict[str, Any]] = {}
    for mode in ("setup", "hold"):
        remote_log = _remote_join(remote_log_dir, f"primetime_{mode}.log")
        script = _remote_pt_script(
            mode=mode,
            top=args.top,
            threshold_ns=args.threshold_ns,
            max_violating_paths=args.max_violating_paths,
            report_max_paths=args.report_max_paths,
            remote_pt_shell=remote_pt_shell,
            synopsys_lc_root=args.synopsys_lc_root,
            remote_inputs=remote_inputs,
            remote_report_dir=remote_report_dir,
            remote_log_path=remote_log,
            remote_home=remote_home,
            remote_work_dir=remote_work_dir,
        )
        command = _ssh_command(ssh_executable, target, script)
        timed_out = False
        try:
            completed = subprocess.run(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=args.timeout_seconds,
                check=False,
            )
            exit_code = completed.returncode
            transport_output = completed.stdout or ""
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = 124
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            transport_output = stdout + stderr
            transport_output += (
                f"\nPT_SSH_TIMEOUT after {args.timeout_seconds} seconds\n"
            )
        except OSError as exc:
            exit_code = 125
            transport_output = f"PT_SSH_START_ERROR: {exc}\n"
        transport_log = log_dir / f"ssh_transport_{mode}.log"
        transport_log.write_text(
            transport_output or f"SSH transport completed with exit code {exit_code}\n",
            encoding="utf-8",
        )
        if exit_code == 73:
            raise CrosscheckError(
                f"remote {mode} output paths existed before PrimeTime execution"
            )
        pending[mode] = {
            "backend": "ssh",
            "mode": mode,
            "command": command,
            "tcl_artifact": input_metadata["tcl"]["path"],
            "tcl_basename": Path(str(input_metadata["tcl"]["path"])).name,
            "sdc_role": SDC_ROLE_BY_MODE[mode],
            "sdc_environment_variable": SDC_ENV_BY_MODE[mode],
            "sdc_artifact": input_metadata[SDC_ROLE_BY_MODE[mode]]["path"],
            "pt_sdc_artifact": sdc_adaptation[mode]["adapted_artifact"]["path"],
            "pt_sdc_sha256": sdc_adaptation[mode]["adapted_artifact"]["sha256"],
            "synopsys_lc_root": args.synopsys_lc_root,
            "input_bindings": _execution_input_bindings(
                mode, input_metadata, sdc_adaptation
            ),
            "exit_code": exit_code,
            "timed_out": timed_out,
            "ssh_target": target,
            "remote_root": remote_root,
            "remote_run_id": invocation_id,
            "remote_bundle": remote_bundle,
            "remote_tcl_path": remote_inputs["tcl"],
            "home": remote_home,
            "working_directory": remote_work_dir,
        }

    report_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    fetch_reports = [
        rsync_executable,
        "-a",
        "--",
        f"{target}:{remote_report_dir}/",
        str(report_dir) + "/",
    ]
    fetch_logs = [
        rsync_executable,
        "-a",
        "--",
        f"{target}:{remote_log_dir}/",
        str(log_dir) + "/",
    ]
    _run_required_command(
        fetch_reports,
        timeout_seconds=args.timeout_seconds,
        label="remote PrimeTime report fetch",
    )
    _run_required_command(
        fetch_logs,
        timeout_seconds=args.timeout_seconds,
        label="remote PrimeTime log fetch",
    )

    execution: dict[str, Any] = {}
    for mode in ("setup", "hold"):
        execution[mode] = {
            **pending[mode],
            "log": _artifact(
                log_dir / f"primetime_{mode}.log",
                output_root,
                f"{mode} remote PrimeTime log",
            ),
        }
    return execution


def build_summary(
    report_dir: Path,
    *,
    summary_root: Path,
    expected_top: str | None = None,
    expected_threshold_ns: float | None = DEFAULT_THRESHOLD_NS,
    required_tool_version: str | None = DEFAULT_TOOL_VERSION,
    expected_lc_root: str = DEFAULT_SYNOPSYS_LC_ROOT,
    max_violating_paths: int = DEFAULT_MAX_VIOLATING_PATHS,
    report_max_paths: int = DEFAULT_REPORT_MAX_PATHS,
    pt_shell: str = DEFAULT_PT_SHELL,
) -> dict[str, Any]:
    """Build a typed two-corner summary and hash every required report."""

    report_dir = report_dir.resolve()
    summary_root = summary_root.resolve()
    if expected_threshold_ns is None or not math.isfinite(expected_threshold_ns):
        raise CrosscheckError("summary slack threshold must be finite")
    if expected_threshold_ns < 0.0:
        raise CrosscheckError("summary slack threshold must be non-negative")
    if not isinstance(required_tool_version, str) or not required_tool_version:
        raise CrosscheckError("summary required PrimeTime version must be non-empty")
    expected_lc_root = _validate_synopsys_lc_root(expected_lc_root)
    for value, label in (
        (max_violating_paths, "max_violating_paths"),
        (report_max_paths, "report_max_paths"),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise CrosscheckError(f"summary {label} must be a positive integer")
    pt_shell = _validate_remote_executable(pt_shell, "PrimeTime")
    approved_tcl = _approved_tcl_contract()
    corners: dict[str, Any] = {}
    for mode in ("setup", "hold"):
        result_path = report_dir / f"{mode}.result"
        parsed = parse_result_file(
            result_path,
            expected_mode=mode,
            expected_top=expected_top,
            expected_threshold_ns=expected_threshold_ns,
            required_tool_version=required_tool_version,
            expected_lc_root=expected_lc_root,
        )
        evidence: dict[str, Any] = {
            "result": _artifact(result_path, summary_root, f"{mode} result marker")
        }
        _verify_pt_text_evidence_clean(result_path, f"{mode} result marker")
        if parsed["status"] != "ERROR":
            for role, pattern in REPORT_FILES.items():
                report_path = report_dir / pattern.format(mode=mode)
                evidence[role] = _artifact(
                    report_path,
                    summary_root,
                    f"{mode} {role} report",
                )
                _verify_pt_text_evidence_clean(
                    report_path, f"{mode} {role} report"
                )
        corners[mode] = {**parsed, "evidence": evidence}

    comparable = [corner for corner in corners.values() if corner["status"] != "ERROR"]
    if len(comparable) == 2:
        if comparable[0]["top"] != comparable[1]["top"]:
            raise CrosscheckError("setup and hold markers name different top designs")
        if not math.isclose(
            comparable[0]["threshold_ns"],
            comparable[1]["threshold_ns"],
            abs_tol=NUMERIC_EPSILON,
        ):
            raise CrosscheckError("setup and hold markers use different slack thresholds")
        if comparable[0]["tool_version"] != comparable[1]["tool_version"]:
            raise CrosscheckError("setup and hold markers use different PrimeTime versions")
        if comparable[0]["lc_root"] != comparable[1]["lc_root"]:
            raise CrosscheckError("setup and hold markers use different SYNOPSYS_LC_ROOT values")

    passed = all(corners[mode]["status"] == "PASS" for mode in ("setup", "hold"))
    return {
        "schema_version": SCHEMA_VERSION,
        "policy": {
            "threshold_ns": expected_threshold_ns,
            "required_tool_version": required_tool_version,
            "sdc_roles": dict(SDC_ROLE_BY_MODE),
            "pt_sdc_roles": dict(PT_SDC_ROLE_BY_MODE),
            "sdc_adapter_schema_version": SDC_ADAPTER_SCHEMA_VERSION,
            "synopsys_lc_root": expected_lc_root,
            "max_violating_paths": max_violating_paths,
            "report_max_paths": report_max_paths,
            "pt_shell": pt_shell,
            "approved_tcl_sha256": approved_tcl["sha256"],
            "approved_tcl_bytes": approved_tcl["bytes"],
        },
        "passed": passed,
        "corners": corners,
    }


def _safe_artifact_path(root: Path, declared: str, where: str) -> Path:
    path = Path(declared)
    if path.is_absolute() or ".." in path.parts:
        raise CrosscheckError(f"{where}.path must be summary-relative: {declared!r}")
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise CrosscheckError(f"{where}.path escapes summary root") from exc
    return resolved


def _verify_artifact_declaration(
    root: Path, item: Any, where: str
) -> tuple[dict[str, Any], Path]:
    if not isinstance(item, Mapping):
        raise CrosscheckError(f"{where} must be a hashed artifact object")
    declared_path = item.get("path")
    digest = item.get("sha256")
    size = item.get("bytes")
    if not isinstance(declared_path, str) or not declared_path:
        raise CrosscheckError(f"{where}.path is invalid")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest.lower()):
        raise CrosscheckError(f"{where}.sha256 is invalid")
    artifact_path = _safe_artifact_path(root, declared_path, where)
    artifact_path = _regular_nonempty_file(artifact_path, where)
    actual_digest = _sha256(artifact_path)
    if actual_digest != digest.lower():
        raise CrosscheckError(f"{where} hash mismatch")
    if not isinstance(size, int) or isinstance(size, bool) or size != artifact_path.stat().st_size:
        raise CrosscheckError(f"{where} size mismatch")
    normalized = dict(item)
    normalized["sha256"] = actual_digest
    return normalized, artifact_path


def _load_and_verify_inputs(
    raw: Any, root: Path, where: str
) -> tuple[dict[str, dict[str, Any]], dict[str, Path]]:
    if not isinstance(raw, Mapping) or set(raw) != set(INPUT_ROLES):
        raise CrosscheckError(
            f"{where}: inputs must contain exactly {', '.join(INPUT_ROLES)}"
        )
    normalized: dict[str, dict[str, Any]] = {}
    paths: dict[str, Path] = {}
    for role in INPUT_ROLES:
        item, artifact_path = _verify_artifact_declaration(
            root, raw[role], f"{where}: inputs.{role}"
        )
        declared = Path(str(item["path"]))
        if not declared.parts or declared.parts[0] != "inputs":
            raise CrosscheckError(
                f"{where}: inputs.{role}.path must name an output-root staging artifact"
            )
        if declared.name != STAGED_INPUT_FILENAMES[role]:
            raise CrosscheckError(
                f"{where}: inputs.{role}.path does not use its canonical staged filename"
            )
        source_path = item.get("source_path")
        source_digest = item.get("source_sha256")
        source_size = item.get("source_bytes")
        if not isinstance(source_path, str) or not source_path:
            raise CrosscheckError(f"{where}: inputs.{role}.source_path is invalid")
        if (
            not isinstance(source_digest, str)
            or not SHA256_RE.fullmatch(source_digest.lower())
            or source_digest.lower() != item["sha256"]
        ):
            raise CrosscheckError(
                f"{where}: inputs.{role} staged/source SHA256 binding is invalid"
            )
        if (
            not isinstance(source_size, int)
            or isinstance(source_size, bool)
            or source_size != item["bytes"]
        ):
            raise CrosscheckError(
                f"{where}: inputs.{role} staged/source byte binding is invalid"
            )
        item["source_sha256"] = source_digest.lower()
        normalized[role] = item
        paths[role] = artifact_path
    bundle_parents = {path.parent for path in paths.values()}
    if len(bundle_parents) != 1:
        raise CrosscheckError(f"{where}: staged inputs do not share one input bundle")
    if paths["setup_sdc"] == paths["hold_sdc"]:
        raise CrosscheckError(f"{where}: setup and hold SDC artifacts are not distinct")
    if (
        normalized["setup_sdc"]["source_path"]
        == normalized["hold_sdc"]["source_path"]
    ):
        raise CrosscheckError(f"{where}: setup and hold SDC sources are not distinct")
    return normalized, paths


def _verify_approved_tcl_input(
    inputs: Mapping[str, Mapping[str, Any]],
    input_paths: Mapping[str, Path],
    *,
    policy_sha256: str,
    policy_bytes: int,
    where: str,
) -> None:
    approved = _regular_nonempty_file(DEFAULT_TCL, "approved PrimeTime crosscheck Tcl")
    approved_digest = _sha256(approved)
    approved_size = approved.stat().st_size
    if policy_sha256 != approved_digest or policy_bytes != approved_size:
        raise CrosscheckError(
            f"{where}: policy approved Tcl identity does not match local DEFAULT_TCL"
        )
    item = inputs["tcl"]
    staged = input_paths["tcl"]
    if (
        item.get("sha256") != approved_digest
        or item.get("bytes") != approved_size
        or staged.read_bytes() != approved.read_bytes()
    ):
        raise CrosscheckError(
            f"{where}: staged PrimeTime Tcl is not the approved DEFAULT_TCL"
        )


def _verify_content_addressed_bundle(
    inputs: Mapping[str, Mapping[str, Any]],
    input_paths: Mapping[str, Path],
    sdc_adaptation: Mapping[str, Any],
    *,
    where: str,
) -> str:
    adapted_metadata = {
        PT_SDC_ROLE_BY_MODE[mode]: sdc_adaptation[mode]["adapted_artifact"]
        for mode in ("setup", "hold")
    }
    digest = _input_bundle_digest(
        inputs,
        adapted_metadata,
        top=str(sdc_adaptation["top"]),
    )
    expected_name = f"primetime_{digest}"
    bundle_names = {path.parent.name for path in input_paths.values()}
    if bundle_names != {expected_name}:
        raise CrosscheckError(
            f"{where}: staged input directory is not content-addressed as {expected_name}"
        )
    return expected_name


def _load_and_verify_sdc_adaptation(
    raw: Any,
    *,
    root: Path,
    inputs: Mapping[str, Mapping[str, Any]],
    input_paths: Mapping[str, Path],
    where: str,
) -> tuple[dict[str, Any], dict[str, Path]]:
    required_top_keys = {
        "schema_version",
        "operation",
        "comment_prefix",
        "top",
        "setup",
        "hold",
    }
    if not isinstance(raw, Mapping) or set(raw) != required_top_keys:
        raise CrosscheckError(
            f"{where}: sdc_adaptation must contain exactly "
            + ", ".join(sorted(required_top_keys))
        )
    if raw.get("schema_version") != SDC_ADAPTER_SCHEMA_VERSION:
        raise CrosscheckError(f"{where}: unsupported SDC adapter schema")
    if raw.get("operation") != SDC_ADAPTER_OPERATION:
        raise CrosscheckError(f"{where}: SDC adapter operation is invalid")
    if raw.get("comment_prefix") != SDC_ADAPTER_COMMENT_PREFIX:
        raise CrosscheckError(f"{where}: SDC adapter comment prefix is invalid")
    top = raw.get("top")
    if not isinstance(top, str) or not TOP_TOKEN_RE.fullmatch(top):
        raise CrosscheckError(f"{where}: sdc_adaptation.top is invalid")

    required_mode_keys = {
        "source_role",
        "source_artifact",
        "source_sha256",
        "source_bytes",
        "current_design_line",
        "design_rule_get_designs_lines",
        "adapted_artifact",
    }
    normalized: dict[str, Any] = {
        "schema_version": SDC_ADAPTER_SCHEMA_VERSION,
        "operation": SDC_ADAPTER_OPERATION,
        "comment_prefix": SDC_ADAPTER_COMMENT_PREFIX,
        "top": top,
    }
    adapted_paths: dict[str, Path] = {}
    for mode in ("setup", "hold"):
        item = raw.get(mode)
        if not isinstance(item, Mapping) or set(item) != required_mode_keys:
            raise CrosscheckError(
                f"{where}: sdc_adaptation.{mode} has invalid fields"
            )
        source_role = SDC_ROLE_BY_MODE[mode]
        source = inputs[source_role]
        if (
            item.get("source_role") != source_role
            or item.get("source_artifact") != source["path"]
            or item.get("source_sha256") != source["sha256"]
            or item.get("source_bytes") != source["bytes"]
        ):
            raise CrosscheckError(
                f"{where}: sdc_adaptation.{mode} is not bound to {source_role}"
            )
        artifact, adapted_path = _verify_artifact_declaration(
            root,
            item.get("adapted_artifact"),
            f"{where}: sdc_adaptation.{mode}.adapted_artifact",
        )
        declared = Path(str(artifact["path"]))
        pt_role = PT_SDC_ROLE_BY_MODE[mode]
        if (
            declared.name != PT_SDC_FILENAMES[pt_role]
            or adapted_path.parent != input_paths[source_role].parent
        ):
            raise CrosscheckError(
                f"{where}: sdc_adaptation.{mode} adapted path is not in the "
                "immutable input bundle"
            )
        expected_bytes, expected_line, expected_design_rule_lines = _adapt_innovus_sdc_bytes(
            input_paths[source_role].read_bytes(), top
        )
        line_number = item.get("current_design_line")
        if (
            not isinstance(line_number, int)
            or isinstance(line_number, bool)
            or line_number != expected_line
        ):
            raise CrosscheckError(
                f"{where}: sdc_adaptation.{mode}.current_design_line is invalid"
            )
        design_rule_lines = item.get("design_rule_get_designs_lines")
        if design_rule_lines != expected_design_rule_lines:
            raise CrosscheckError(
                f"{where}: sdc_adaptation.{mode}.design_rule_get_designs_lines "
                "is invalid"
            )
        if adapted_path.read_bytes() != expected_bytes:
            raise CrosscheckError(
                f"{where}: sdc_adaptation.{mode} is not the deterministic "
                "bounded-line adaptation of its raw SDC"
            )
        normalized[mode] = {**dict(item), "adapted_artifact": artifact}
        adapted_paths[mode] = adapted_path
    if adapted_paths["setup"] == adapted_paths["hold"]:
        raise CrosscheckError(f"{where}: setup and hold adapted SDCs are not distinct")
    return normalized, adapted_paths


def _load_and_verify_execution(
    raw: Any,
    *,
    root: Path,
    corners: Mapping[str, Mapping[str, Any]],
    inputs: Mapping[str, Mapping[str, Any]],
    sdc_adaptation: Mapping[str, Any],
    synopsys_lc_root: str,
    threshold_ns: float,
    max_violating_paths: int,
    report_max_paths: int,
    pt_shell: str,
    where: str,
) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, Mapping) or set(raw) != {"setup", "hold"}:
        raise CrosscheckError(f"{where}: execution must contain exactly setup and hold")
    adapted_metadata = {
        PT_SDC_ROLE_BY_MODE[mode]: sdc_adaptation[mode]["adapted_artifact"]
        for mode in ("setup", "hold")
    }
    expected_bundle_name = "primetime_" + _input_bundle_digest(
        inputs,
        adapted_metadata,
        top=str(sdc_adaptation["top"]),
    )
    normalized: dict[str, dict[str, Any]] = {}
    for mode in ("setup", "hold"):
        item = raw[mode]
        if not isinstance(item, Mapping):
            raise CrosscheckError(f"{where}: execution.{mode} must be an object")
        if item.get("mode") != mode:
            raise CrosscheckError(f"{where}: execution.{mode}.mode is inconsistent")
        command = item.get("command")
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(word, str) or not word for word in command)
        ):
            raise CrosscheckError(f"{where}: execution.{mode}.command is invalid")
        backend = item.get("backend")
        if backend not in {"local", "ssh"}:
            raise CrosscheckError(
                f"{where}: execution.{mode}.backend must be local or ssh"
            )
        expected_tcl_basename = Path(str(inputs["tcl"]["path"])).name
        if item.get("tcl_basename") != expected_tcl_basename:
            raise CrosscheckError(
                f"{where}: execution.{mode}.tcl_basename is inconsistent"
            )
        if backend == "local":
            home = item.get("home")
            working_directory = item.get("working_directory")
            if (
                not isinstance(home, str)
                or not Path(home).is_absolute()
                or working_directory != home
            ):
                raise CrosscheckError(
                    f"{where}: execution.{mode} local HOME/cwd isolation is invalid"
                )
            declared_tcl = Path(str(inputs["tcl"]["path"]))
            expected_command_tcl = str(Path(home) / declared_tcl)
            expected_command = [pt_shell, "-no_init", "-f", expected_command_tcl]
            if command != expected_command:
                raise CrosscheckError(
                    f"{where}: execution.{mode}.command is not the exact isolated "
                    "PrimeTime invocation"
                )
        else:
            target = _validate_ssh_target(str(item.get("ssh_target", "")))
            remote_root = _validate_remote_root(str(item.get("remote_root", "")))
            invocation_id = _validate_remote_identifier(
                str(item.get("remote_run_id", ""))
            )
            remote_bundle = _validate_remote_root(str(item.get("remote_bundle", "")))
            remote_tcl = _validate_remote_executable(
                str(item.get("remote_tcl_path", "")), "Tcl"
            )
            remote_run_root = _remote_join(remote_root, "runs", invocation_id)
            expected_remote_bundle = _remote_join(
                remote_run_root, "inputs", expected_bundle_name
            )
            remote_home = _remote_join(remote_run_root, "home")
            remote_work_dir = _remote_join(remote_run_root, "work")
            if (
                remote_bundle != expected_remote_bundle
                or remote_tcl != _remote_join(
                    remote_bundle, STAGED_INPUT_FILENAMES["tcl"]
                )
                or item.get("home") != remote_home
                or item.get("working_directory") != remote_work_dir
            ):
                raise CrosscheckError(
                    f"{where}: execution.{mode} remote root/bundle/Tcl isolation is inconsistent"
                )
            if len(command) != 3 or "\x00" in command[0] or command[0].startswith("-"):
                raise CrosscheckError(
                    f"{where}: execution.{mode}.command has an invalid SSH executable"
                )
            remote_inputs = {
                role: _remote_join(remote_bundle, STAGED_INPUT_FILENAMES[role])
                for role in INPUT_ROLES
            }
            remote_inputs.update(
                {
                    pt_role: _remote_join(remote_bundle, PT_SDC_FILENAMES[pt_role])
                    for pt_role in PT_SDC_ROLE_BY_MODE.values()
                }
            )
            remote_report_dir = _remote_join(
                remote_run_root, "reports", "primetime"
            )
            remote_log_path = _remote_join(
                remote_run_root, "logs", f"primetime_{mode}.log"
            )
            expected_script = _remote_pt_script(
                mode=mode,
                top=str(sdc_adaptation["top"]),
                threshold_ns=threshold_ns,
                max_violating_paths=max_violating_paths,
                report_max_paths=report_max_paths,
                remote_pt_shell=pt_shell,
                synopsys_lc_root=synopsys_lc_root,
                remote_inputs=remote_inputs,
                remote_report_dir=remote_report_dir,
                remote_log_path=remote_log_path,
                remote_home=remote_home,
                remote_work_dir=remote_work_dir,
            )
            expected_command = _ssh_command(command[0], target, expected_script)
            if command != expected_command:
                raise CrosscheckError(
                    f"{where}: execution.{mode}.command is not the exact isolated "
                    "PrimeTime SSH invocation"
                )
        if item.get("tcl_artifact") != inputs["tcl"]["path"]:
            raise CrosscheckError(
                f"{where}: execution.{mode}.tcl_artifact is inconsistent"
            )
        sdc_role = SDC_ROLE_BY_MODE[mode]
        if item.get("sdc_role") != sdc_role:
            raise CrosscheckError(
                f"{where}: execution.{mode}.sdc_role is inconsistent"
            )
        if item.get("sdc_environment_variable") != SDC_ENV_BY_MODE[mode]:
            raise CrosscheckError(
                f"{where}: execution.{mode}.sdc_environment_variable is inconsistent"
            )
        if item.get("sdc_artifact") != inputs[sdc_role]["path"]:
            raise CrosscheckError(
                f"{where}: execution.{mode}.sdc_artifact is inconsistent"
            )
        adapted_artifact = sdc_adaptation[mode]["adapted_artifact"]
        if (
            item.get("pt_sdc_artifact") != adapted_artifact["path"]
            or item.get("pt_sdc_sha256") != adapted_artifact["sha256"]
        ):
            raise CrosscheckError(
                f"{where}: execution.{mode} adapted SDC binding is inconsistent"
            )
        if item.get("synopsys_lc_root") != synopsys_lc_root:
            raise CrosscheckError(
                f"{where}: execution.{mode}.synopsys_lc_root is inconsistent"
            )
        expected_bindings = _execution_input_bindings(
            mode, inputs, sdc_adaptation
        )
        bindings = item.get("input_bindings")
        if not isinstance(bindings, Mapping) or dict(bindings) != expected_bindings:
            raise CrosscheckError(
                f"{where}: execution.{mode}.input_bindings do not match staged inputs"
            )
        status = corners[mode]["status"]
        expected_exit = {"PASS": 0, "FAIL": 3, "ERROR": 2}[status]
        exit_code = item.get("exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise CrosscheckError(f"{where}: execution.{mode}.exit_code is invalid")
        if exit_code != expected_exit:
            raise CrosscheckError(
                f"{where}: execution.{mode} exit code {exit_code} is inconsistent "
                f"with status {status}"
            )
        if item.get("timed_out") is not False:
            raise CrosscheckError(
                f"{where}: execution.{mode} cannot be timed out with complete evidence"
            )
        log, log_path = _verify_artifact_declaration(
            root, item.get("log"), f"{where}: execution.{mode}.log"
        )
        _verify_pt_text_evidence_clean(
            log_path, f"{where}: execution.{mode}.log"
        )
        normalized[mode] = {
            **dict(item),
            "command": list(command),
            "input_bindings": dict(bindings),
            "log": log,
        }
    backends = {normalized[mode]["backend"] for mode in ("setup", "hold")}
    if len(backends) != 1:
        raise CrosscheckError(f"{where}: setup and hold use different execution backends")
    if backends == {"ssh"}:
        for field in (
            "ssh_target",
            "remote_root",
            "remote_run_id",
            "remote_bundle",
            "home",
            "working_directory",
        ):
            if normalized["setup"].get(field) != normalized["hold"].get(field):
                raise CrosscheckError(
                    f"{where}: setup and hold use different remote {field} values"
                )
    else:
        for field in ("home", "working_directory"):
            if normalized["setup"].get(field) != normalized["hold"].get(field):
                raise CrosscheckError(
                    f"{where}: setup and hold use different local {field} values"
                )
    return normalized


def load_and_verify_summary(path: Path) -> dict[str, Any]:
    """Load a summary and verify reports, execution, and staged input hashes."""

    raw = _read_json(path)
    if not isinstance(raw, Mapping) or raw.get("schema_version") != SCHEMA_VERSION:
        raise CrosscheckError(f"{path}: unsupported or missing crosscheck schema")
    root = path.resolve().parent

    policy = raw.get("policy")
    if not isinstance(policy, Mapping):
        raise CrosscheckError(f"{path}: policy must be an object")
    required_policy_fields = {
        "threshold_ns",
        "required_tool_version",
        "sdc_roles",
        "pt_sdc_roles",
        "sdc_adapter_schema_version",
        "synopsys_lc_root",
        "max_violating_paths",
        "report_max_paths",
        "pt_shell",
        "approved_tcl_sha256",
        "approved_tcl_bytes",
    }
    if set(policy) != required_policy_fields:
        raise CrosscheckError(f"{path}: policy fields are incomplete or unexpected")
    threshold = policy.get("threshold_ns")
    required_version = policy.get("required_tool_version")
    sdc_roles = policy.get("sdc_roles")
    pt_sdc_roles = policy.get("pt_sdc_roles")
    adapter_schema = policy.get("sdc_adapter_schema_version")
    synopsys_lc_root = policy.get("synopsys_lc_root")
    max_violating_paths = policy.get("max_violating_paths")
    report_max_paths = policy.get("report_max_paths")
    pt_shell = policy.get("pt_shell")
    approved_tcl_sha256 = policy.get("approved_tcl_sha256")
    approved_tcl_bytes = policy.get("approved_tcl_bytes")
    if (
        not isinstance(threshold, (int, float))
        or isinstance(threshold, bool)
        or not math.isfinite(float(threshold))
        or float(threshold) < 0.0
    ):
        raise CrosscheckError(f"{path}: policy.threshold_ns is invalid")
    threshold = float(threshold)
    if not isinstance(required_version, str) or not required_version:
        raise CrosscheckError(f"{path}: policy.required_tool_version is invalid")
    if not isinstance(sdc_roles, Mapping) or dict(sdc_roles) != SDC_ROLE_BY_MODE:
        raise CrosscheckError(f"{path}: policy.sdc_roles is invalid")
    if (
        not isinstance(pt_sdc_roles, Mapping)
        or dict(pt_sdc_roles) != PT_SDC_ROLE_BY_MODE
    ):
        raise CrosscheckError(f"{path}: policy.pt_sdc_roles is invalid")
    if adapter_schema != SDC_ADAPTER_SCHEMA_VERSION:
        raise CrosscheckError(f"{path}: policy.sdc_adapter_schema_version is invalid")
    synopsys_lc_root = _validate_synopsys_lc_root(str(synopsys_lc_root or ""))
    for value, field in (
        (max_violating_paths, "max_violating_paths"),
        (report_max_paths, "report_max_paths"),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise CrosscheckError(f"{path}: policy.{field} is invalid")
    pt_shell = _validate_remote_executable(str(pt_shell or ""), "PrimeTime")
    if (
        not isinstance(approved_tcl_sha256, str)
        or not SHA256_RE.fullmatch(approved_tcl_sha256.lower())
        or not isinstance(approved_tcl_bytes, int)
        or isinstance(approved_tcl_bytes, bool)
        or approved_tcl_bytes <= 0
    ):
        raise CrosscheckError(f"{path}: policy approved Tcl identity is invalid")
    approved_tcl_sha256 = approved_tcl_sha256.lower()
    normalized_policy = {
        "threshold_ns": threshold,
        "required_tool_version": required_version,
        "sdc_roles": dict(SDC_ROLE_BY_MODE),
        "pt_sdc_roles": dict(PT_SDC_ROLE_BY_MODE),
        "sdc_adapter_schema_version": SDC_ADAPTER_SCHEMA_VERSION,
        "synopsys_lc_root": synopsys_lc_root,
        "max_violating_paths": max_violating_paths,
        "report_max_paths": report_max_paths,
        "pt_shell": pt_shell,
        "approved_tcl_sha256": approved_tcl_sha256,
        "approved_tcl_bytes": approved_tcl_bytes,
    }

    normalized_inputs, input_paths = _load_and_verify_inputs(
        raw.get("inputs"), root, str(path)
    )
    _verify_approved_tcl_input(
        normalized_inputs,
        input_paths,
        policy_sha256=approved_tcl_sha256,
        policy_bytes=approved_tcl_bytes,
        where=str(path),
    )
    normalized_adaptation, _ = _load_and_verify_sdc_adaptation(
        raw.get("sdc_adaptation"),
        root=root,
        inputs=normalized_inputs,
        input_paths=input_paths,
        where=str(path),
    )
    _verify_content_addressed_bundle(
        normalized_inputs,
        input_paths,
        normalized_adaptation,
        where=str(path),
    )
    corners = raw.get("corners")
    if not isinstance(corners, Mapping) or set(corners) != {"setup", "hold"}:
        raise CrosscheckError(f"{path}: corners must contain exactly setup and hold")
    normalized: dict[str, Any] = {}
    for mode in ("setup", "hold"):
        corner = corners[mode]
        if not isinstance(corner, Mapping):
            raise CrosscheckError(f"{path}: corners.{mode} must be an object")
        status = corner.get("status")
        if status not in {"PASS", "FAIL", "ERROR"}:
            raise CrosscheckError(f"{path}: corners.{mode}.status is invalid")
        if corner.get("mode") != mode:
            raise CrosscheckError(f"{path}: corners.{mode}.mode does not match its key")
        if corner.get("delay_type") != ("max" if mode == "setup" else "min"):
            raise CrosscheckError(f"{path}: corners.{mode}.delay_type is invalid")
        if status != "ERROR":
            marker_fields = {
                "status": status,
                "mode": mode,
                "delay_type": corner["delay_type"],
                "top": str(corner.get("top", "")),
                "wns_ns": str(corner.get("wns_ns", "")),
                "tns_ns": str(corner.get("tns_ns", "")),
                "violating_paths": str(corner.get("violating_paths", "")),
                "threshold_ns": str(corner.get("threshold_ns", "")),
                "parasitics_read": "1" if corner.get("parasitics_read") is True else "0",
                "propagated_clocks": (
                    "1" if corner.get("propagated_clocks") is True else "0"
                ),
                "clock_count": str(corner.get("clock_count", "")),
                "tool_version": str(corner.get("tool_version", "")),
                "lc_root": str(corner.get("lc_root", "")),
            }
            marker = MARKER_PREFIX + " " + " ".join(
                f"{key}={value}" for key, value in marker_fields.items()
            )
            parse_marker_line(
                marker,
                where=f"{path}: corners.{mode}",
                expected_mode=mode,
                expected_threshold_ns=threshold,
                required_tool_version=required_version,
                expected_lc_root=synopsys_lc_root,
            )

        evidence = corner.get("evidence")
        if not isinstance(evidence, Mapping):
            raise CrosscheckError(f"{path}: corners.{mode}.evidence must be an object")
        required_roles = {"result"}
        if status != "ERROR":
            required_roles.update(REPORT_FILES)
        if not required_roles.issubset(evidence):
            missing = sorted(required_roles - set(evidence))
            raise CrosscheckError(
                f"{path}: corners.{mode}.evidence missing: {', '.join(missing)}"
            )
        normalized_evidence: dict[str, Any] = {}
        evidence_paths: dict[str, Path] = {}
        for role in sorted(required_roles):
            item, artifact_path = _verify_artifact_declaration(
                root,
                evidence[role],
                f"{path}: corners.{mode}.evidence.{role}",
            )
            normalized_evidence[role] = item
            evidence_paths[role] = artifact_path
            _verify_pt_text_evidence_clean(
                artifact_path,
                f"{path}: corners.{mode}.evidence.{role}",
            )

        # Bind the typed JSON fields to the actual result marker, not merely
        # to a hash supplied by the same JSON document.
        marker_corner = parse_result_file(
            evidence_paths["result"],
            expected_mode=mode,
            expected_threshold_ns=(threshold if status != "ERROR" else None),
            required_tool_version=required_version,
            expected_lc_root=(synopsys_lc_root if status != "ERROR" else None),
        )
        for key, marker_value in marker_corner.items():
            if key not in corner or corner[key] != marker_value:
                raise CrosscheckError(
                    f"{path}: corners.{mode}.{key} does not match its result marker"
                )
        normalized[mode] = {**dict(corner), "evidence": normalized_evidence}

    comparable = [corner for corner in normalized.values() if corner["status"] != "ERROR"]
    for corner in comparable:
        if corner["top"] != normalized_adaptation["top"]:
            raise CrosscheckError(
                f"{path}: PrimeTime marker top does not match SDC adaptation top"
            )
    if len(comparable) == 2:
        if comparable[0]["top"] != comparable[1]["top"]:
            raise CrosscheckError(f"{path}: setup and hold markers name different top designs")
        if comparable[0]["tool_version"] != comparable[1]["tool_version"]:
            raise CrosscheckError(
                f"{path}: setup and hold markers name different PrimeTime versions"
            )
        if comparable[0]["lc_root"] != comparable[1]["lc_root"]:
            raise CrosscheckError(
                f"{path}: setup and hold markers name different SYNOPSYS_LC_ROOT values"
            )
        if not math.isclose(
            comparable[0]["threshold_ns"],
            comparable[1]["threshold_ns"],
            abs_tol=NUMERIC_EPSILON,
        ):
            raise CrosscheckError(
                f"{path}: setup and hold markers use different slack thresholds"
            )

    normalized_execution = _load_and_verify_execution(
        raw.get("execution"),
        root=root,
        corners=normalized,
        inputs=normalized_inputs,
        sdc_adaptation=normalized_adaptation,
        synopsys_lc_root=synopsys_lc_root,
        threshold_ns=threshold,
        max_violating_paths=max_violating_paths,
        report_max_paths=report_max_paths,
        pt_shell=pt_shell,
        where=str(path),
    )
    passed = all(normalized[mode]["status"] == "PASS" for mode in ("setup", "hold"))
    if raw.get("passed") is not passed:
        raise CrosscheckError(f"{path}: top-level passed is inconsistent with corner statuses")
    return {
        **dict(raw),
        "policy": normalized_policy,
        "inputs": normalized_inputs,
        "sdc_adaptation": normalized_adaptation,
        "execution": normalized_execution,
        "passed": passed,
        "corners": normalized,
    }


def merge_metrics(metrics_path: Path, summary_path: Path, output: Path) -> dict[str, Any]:
    """Set the Gold boolean from verified PT evidence and retain typed details."""

    metrics = _read_json(metrics_path)
    if not isinstance(metrics, Mapping):
        raise CrosscheckError(f"{metrics_path}: metrics must be an object")
    checks = metrics.get("checks")
    if not isinstance(checks, Mapping):
        raise CrosscheckError(f"{metrics_path}: metrics.checks must be an object")
    summary = load_and_verify_summary(summary_path)

    merged = copy.deepcopy(dict(metrics))
    merged["checks"] = dict(checks)
    merged["checks"]["primetime_crosscheck_passed"] = summary["passed"]
    crosschecks = merged.get("crosschecks", {})
    if not isinstance(crosschecks, Mapping):
        raise CrosscheckError(f"{metrics_path}: metrics.crosschecks must be an object")
    merged["crosschecks"] = dict(crosschecks)
    merged["crosschecks"]["primetime"] = summary
    _write_json(output, merged)
    return merged


def run_crosscheck(args: argparse.Namespace) -> int:
    """Run isolated setup and hold PT processes, then emit a verified summary."""

    tcl = _regular_nonempty_file(args.tcl, "PrimeTime crosscheck Tcl")
    approved_tcl = _regular_nonempty_file(DEFAULT_TCL, "approved PrimeTime crosscheck Tcl")
    if tcl.read_bytes() != approved_tcl.read_bytes():
        raise CrosscheckError(
            "--tcl must be byte-identical to the approved DEFAULT_TCL for Gold evidence"
        )
    setup_sdc_arg = getattr(args, "setup_sdc", None)
    hold_sdc_arg = getattr(args, "hold_sdc", None)
    legacy_sdc_arg = getattr(args, "sdc", None)
    if legacy_sdc_arg is not None:
        raise CrosscheckError(
            "--sdc is not valid Gold evidence; provide distinct --setup-sdc and "
            "--hold-sdc artifacts"
        )
    if setup_sdc_arg is None or hold_sdc_arg is None:
        raise CrosscheckError(
            "Gold PrimeTime crosscheck requires both --setup-sdc and --hold-sdc"
        )
    setup_sdc = _regular_nonempty_file(setup_sdc_arg, "setup SDC")
    hold_sdc = _regular_nonempty_file(hold_sdc_arg, "hold SDC")
    if setup_sdc == hold_sdc:
        raise CrosscheckError(
            "--setup-sdc and --hold-sdc must be distinct source artifacts"
        )
    input_sources = {
        "tcl": tcl,
        "netlist": _regular_nonempty_file(args.netlist, "gate netlist"),
        "setup_sdc": setup_sdc,
        "hold_sdc": hold_sdc,
        "setup_lib": _regular_nonempty_file(args.setup_lib, "setup Liberty"),
        "hold_lib": _regular_nonempty_file(args.hold_lib, "hold Liberty"),
        "setup_spef": _regular_nonempty_file(args.setup_spef, "setup SPEF"),
        "hold_spef": _regular_nonempty_file(args.hold_spef, "hold SPEF"),
    }
    if args.threshold_ns < 0.0 or not math.isfinite(args.threshold_ns):
        raise CrosscheckError("--threshold-ns must be a finite non-negative number")
    if args.timeout_seconds < 1:
        raise CrosscheckError("--timeout-seconds must be positive")
    if not isinstance(args.max_violating_paths, int) or args.max_violating_paths < 1:
        raise CrosscheckError("--max-violating-paths must be positive")
    if not isinstance(args.report_max_paths, int) or args.report_max_paths < 1:
        raise CrosscheckError("--report-max-paths must be positive")
    if not isinstance(args.require_tool_version, str) or not args.require_tool_version:
        raise CrosscheckError("--require-tool-version must be non-empty")
    args.synopsys_lc_root = _validate_synopsys_lc_root(
        str(getattr(args, "synopsys_lc_root", DEFAULT_SYNOPSYS_LC_ROOT))
    )

    ssh_target = getattr(args, "ssh_target", None)
    executable = ""
    ssh_executable = ""
    rsync_executable = ""
    policy_pt_shell = ""
    if ssh_target:
        _validate_ssh_target(str(ssh_target))
        _validate_remote_root(
            str(getattr(args, "remote_root", DEFAULT_REMOTE_ROOT))
        )
        policy_pt_shell = _validate_remote_executable(
            str(args.pt_shell), "PrimeTime"
        )
        ssh_executable = _resolve_local_executable(
            str(getattr(args, "ssh_executable", "ssh")), "SSH"
        )
        rsync_executable = _resolve_local_executable(
            str(getattr(args, "rsync_executable", "rsync")), "rsync"
        )
        args.remote_root = str(getattr(args, "remote_root", DEFAULT_REMOTE_ROOT))
    else:
        executable = _resolve_local_executable(str(args.pt_shell), "PrimeTime")
        policy_pt_shell = _validate_remote_executable(executable, "PrimeTime")

    output_root = args.output_dir.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    inputs, input_metadata, sdc_adaptation = _stage_input_bundle(
        input_sources, output_root, top=args.top
    )
    report_dir = output_root / "reports" / "primetime"
    log_dir = output_root / "logs"
    report_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    execution: dict[str, Any] = {}
    if ssh_target:
        execution = _run_remote_modes(
            args,
            inputs=inputs,
            input_metadata=input_metadata,
            sdc_adaptation=sdc_adaptation,
            output_root=output_root,
            report_dir=report_dir,
            log_dir=log_dir,
            ssh_executable=ssh_executable,
            rsync_executable=rsync_executable,
        )
    else:
        for mode in ("setup", "hold"):
            # Never allow a prior run's marker or report to satisfy this run.
            stale_outputs = [report_dir / f"{mode}.result"]
            stale_outputs.extend(
                report_dir / pattern.format(mode=mode) for pattern in REPORT_FILES.values()
            )
            for stale in stale_outputs:
                stale.unlink(missing_ok=True)
            environment = _sanitized_environment()
            environment.update(
                {
                    "HOME": str(output_root),
                    "PT_TOP": args.top,
                    "SYNOPSYS_LC_ROOT": args.synopsys_lc_root,
                    "PT_NETLIST": str(inputs["netlist"]),
                    "PT_LIB": str(inputs[f"{mode}_lib"]),
                    SDC_ENV_BY_MODE[mode]: str(inputs[PT_SDC_ROLE_BY_MODE[mode]]),
                    "PT_SPEF": str(inputs[f"{mode}_spef"]),
                    "PT_MODE": mode,
                    "PT_REPORT_DIR": str(report_dir),
                    "PT_MIN_SLACK_NS": format(args.threshold_ns, ".9f"),
                    "PT_MAX_VIOLATING_PATHS": str(args.max_violating_paths),
                    "PT_REPORT_MAX_PATHS": str(args.report_max_paths),
                }
            )
            command = [executable, "-no_init", "-f", str(inputs["tcl"])]
            log_path = log_dir / f"primetime_{mode}.log"
            timed_out = False
            try:
                completed = subprocess.run(
                    command,
                    cwd=output_root,
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=args.timeout_seconds,
                    check=False,
                )
                exit_code = completed.returncode
                output = completed.stdout
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                exit_code = 124
                stdout = exc.stdout or ""
                stderr = exc.stderr or ""
                if isinstance(stdout, bytes):
                    stdout = stdout.decode("utf-8", errors="replace")
                if isinstance(stderr, bytes):
                    stderr = stderr.decode("utf-8", errors="replace")
                output = stdout + stderr
                output += f"\nPT_HOST_TIMEOUT after {args.timeout_seconds} seconds\n"
            log_path.write_text(output, encoding="utf-8")
            execution[mode] = {
                "backend": "local",
                "mode": mode,
                "command": command,
                "tcl_artifact": input_metadata["tcl"]["path"],
                "tcl_basename": Path(str(input_metadata["tcl"]["path"])).name,
                "sdc_role": SDC_ROLE_BY_MODE[mode],
                "sdc_environment_variable": SDC_ENV_BY_MODE[mode],
                "sdc_artifact": input_metadata[SDC_ROLE_BY_MODE[mode]]["path"],
                "pt_sdc_artifact": sdc_adaptation[mode]["adapted_artifact"]["path"],
                "pt_sdc_sha256": sdc_adaptation[mode]["adapted_artifact"]["sha256"],
                "synopsys_lc_root": args.synopsys_lc_root,
                "home": str(output_root),
                "working_directory": str(output_root),
                "input_bindings": _execution_input_bindings(
                    mode, input_metadata, sdc_adaptation
                ),
                "exit_code": exit_code,
                "timed_out": timed_out,
                "log": _artifact(log_path, output_root, f"{mode} PrimeTime log"),
            }

    summary_path = output_root / "primetime_crosscheck.json"
    try:
        summary = build_summary(
            report_dir,
            summary_root=output_root,
            expected_top=args.top,
            expected_threshold_ns=args.threshold_ns,
            required_tool_version=args.require_tool_version,
            expected_lc_root=args.synopsys_lc_root,
            max_violating_paths=args.max_violating_paths,
            report_max_paths=args.report_max_paths,
            pt_shell=policy_pt_shell,
        )
        expected_exit_codes = {"PASS": 0, "FAIL": 3, "ERROR": 2}
        for mode in ("setup", "hold"):
            status = summary["corners"][mode]["status"]
            actual_exit = execution[mode]["exit_code"]
            if actual_exit != expected_exit_codes[status]:
                raise CrosscheckError(
                    f"{mode} marker status={status} requires exit code "
                    f"{expected_exit_codes[status]}, got {actual_exit}"
                )
        summary["execution"] = execution
        summary["inputs"] = input_metadata
        summary["sdc_adaptation"] = sdc_adaptation
        _write_json(summary_path, summary)
        summary = load_and_verify_summary(summary_path)
    except CrosscheckError as exc:
        summary = {
            "schema_version": SCHEMA_VERSION,
            "policy": {
                "threshold_ns": args.threshold_ns,
                "required_tool_version": args.require_tool_version,
                "sdc_roles": dict(SDC_ROLE_BY_MODE),
                "pt_sdc_roles": dict(PT_SDC_ROLE_BY_MODE),
                "sdc_adapter_schema_version": SDC_ADAPTER_SCHEMA_VERSION,
                "synopsys_lc_root": args.synopsys_lc_root,
                "max_violating_paths": args.max_violating_paths,
                "report_max_paths": args.report_max_paths,
                "pt_shell": policy_pt_shell,
                "approved_tcl_sha256": _approved_tcl_contract()["sha256"],
                "approved_tcl_bytes": _approved_tcl_contract()["bytes"],
            },
            "passed": False,
            "error": str(exc),
            "execution": execution,
            "inputs": input_metadata,
            "sdc_adaptation": sdc_adaptation,
        }
        _write_json(summary_path, summary)
        raise CrosscheckError(
            f"PrimeTime result evidence is incomplete or invalid; see {summary_path}: {exc}"
        ) from exc
    if summary["passed"] and all(execution[mode]["exit_code"] == 0 for mode in execution):
        return 0
    if any(execution[mode]["exit_code"] in {2, 124} for mode in execution):
        return 2
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="run setup/hold PrimeTime and parse evidence")
    run.add_argument("--top", required=True)
    run.add_argument("--netlist", type=Path, required=True)
    run.add_argument("--setup-sdc", type=Path)
    run.add_argument("--hold-sdc", type=Path)
    run.add_argument(
        "--sdc",
        type=Path,
        help=(
            "deprecated single-SDC option; rejected for Gold evidence (use "
            "--setup-sdc and --hold-sdc)"
        ),
    )
    run.add_argument("--setup-lib", type=Path, required=True)
    run.add_argument("--hold-lib", type=Path, required=True)
    run.add_argument("--setup-spef", type=Path, required=True)
    run.add_argument("--hold-spef", type=Path, required=True)
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--tcl", type=Path, default=DEFAULT_TCL)
    run.add_argument("--pt-shell", default=DEFAULT_PT_SHELL)
    run.add_argument(
        "--ssh-target",
        help="run pt_shell through this SSH host/config alias instead of locally",
    )
    run.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT)
    run.add_argument("--ssh-executable", default="ssh")
    run.add_argument("--rsync-executable", default="rsync")
    run.add_argument("--threshold-ns", type=float, default=DEFAULT_THRESHOLD_NS)
    run.add_argument(
        "--max-violating-paths", type=int, default=DEFAULT_MAX_VIOLATING_PATHS
    )
    run.add_argument("--report-max-paths", type=int, default=DEFAULT_REPORT_MAX_PATHS)
    run.add_argument("--timeout-seconds", type=int, default=7200)
    run.add_argument("--require-tool-version", default=DEFAULT_TOOL_VERSION)
    run.add_argument(
        "--synopsys-lc-root",
        default=DEFAULT_SYNOPSYS_LC_ROOT,
        help="explicit Library Compiler root exported to each clean PT process",
    )

    parse = subparsers.add_parser("parse", help="parse fetched PT reports on the host")
    parse.add_argument("--report-dir", type=Path, required=True)
    parse.add_argument("--output", type=Path, required=True)
    parse.add_argument("--expected-top")
    parse.add_argument("--threshold-ns", type=float, default=DEFAULT_THRESHOLD_NS)
    parse.add_argument("--require-tool-version", default=DEFAULT_TOOL_VERSION)
    parse.add_argument("--synopsys-lc-root", default=DEFAULT_SYNOPSYS_LC_ROOT)

    merge = subparsers.add_parser(
        "merge", help="merge verified PT evidence into a metrics JSON file"
    )
    merge.add_argument("--metrics", type=Path, required=True)
    merge.add_argument("--crosscheck", type=Path, required=True)
    merge.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "run":
            if args.max_violating_paths < 1:
                raise CrosscheckError("--max-violating-paths must be positive")
            if args.report_max_paths < 1:
                raise CrosscheckError("--report-max-paths must be positive")
            return run_crosscheck(args)
        if args.command == "parse":
            if args.threshold_ns < 0.0 or not math.isfinite(args.threshold_ns):
                raise CrosscheckError("--threshold-ns must be finite and non-negative")
            summary = build_summary(
                args.report_dir,
                summary_root=args.output.resolve().parent,
                expected_top=args.expected_top,
                expected_threshold_ns=args.threshold_ns,
                required_tool_version=args.require_tool_version,
                expected_lc_root=_validate_synopsys_lc_root(args.synopsys_lc_root),
            )
            _write_json(args.output, summary)
            print(
                f"PrimeTime crosscheck {'passed' if summary['passed'] else 'failed'}: "
                f"{args.output}"
            )
            return 0 if summary["passed"] else 1
        if args.command == "merge":
            merged = merge_metrics(args.metrics, args.crosscheck, args.output)
            passed = merged["checks"]["primetime_crosscheck_passed"]
            print(f"wrote metrics with PrimeTime crosscheck={passed}: {args.output}")
            return 0 if passed else 1
        raise CrosscheckError(f"unknown command: {args.command}")
    except CrosscheckError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

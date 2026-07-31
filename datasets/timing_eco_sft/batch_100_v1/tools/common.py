#!/usr/bin/env python3
"""Shared fail-closed helpers for the Batch-100 runner and finalizer."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^B1_CASE_\d{3}$")
SAFE_OBJECT_RE = re.compile(r"^[A-Za-z0-9_./\[\]$:+-]+$")
STATE_ORDER = (
    "PLANNED",
    "PROBE_ELIGIBLE",
    "BOUND",
    "CALIBRATING",
    "FROZEN",
    "REPLAYING",
    "VALIDATED",
    "FINALIZED",
)
FORBIDDEN_TCL = re.compile(
    r"(?im)^\s*(?:"
    r"ecoAddRepeater|addInst|addNet|ecoRoute|routeDesign|optDesign|"
    r"ccopt_design|clockDesign|create_clock|set_clock|set_false_path|"
    r"set_multicycle_path|set_disable_timing|deleteInst|deleteNet|"
    r"set_dont_touch|set_case_analysis|set_max_delay|set_min_delay"
    r")\b"
)
ECO_CHANGE_RE = re.compile(
    r"^\s*ecoChangeCell\s+-inst\s+\{([^{}\r\n]+)\}\s+"
    r"-cell\s+\{([^{}\r\n]+)\}\s*$"
)


class BatchError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative(value: str, label: str = "path") -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise BatchError(f"{label} is not a canonical relative path: {value!r}")
    return path


def atomic_write(path: Path, data: bytes, *, durable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            if durable:
                os.fsync(stream.fileno())
        os.replace(temporary, path)
        if durable:
            directory_fd = os.open(
                path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def atomic_json(path: Path, value: Any, *, durable: bool = False) -> None:
    atomic_write(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
        durable=durable,
    )


def read_json(path: Path, label: str | None = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BatchError(f"missing {label or 'JSON'}: {path}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BatchError(f"invalid {label or 'JSON'} {path}: {exc}") from exc


def require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BatchError(f"{label} must be an object")
    return value


def require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise BatchError(f"{label} must be an array")
    return value


def require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise BatchError(f"{label} must be Boolean")
    return value


def require_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BatchError(f"{label} must be numeric")
    result = float(value)
    if result != result or result in {float("inf"), float("-inf")}:
        raise BatchError(f"{label} must be finite")
    return result


def require_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise BatchError(f"{label} must be a lowercase SHA-256")
    return value


def require_real_file(path: Path, label: str, *, nonempty: bool = True) -> Path:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise BatchError(f"missing {label}: {path}") from exc
    if not stat.S_ISREG(mode):
        raise BatchError(f"{label} is not a regular file: {path}")
    if nonempty and path.stat().st_size == 0:
        raise BatchError(f"{label} is empty: {path}")
    return path


def require_real_directory(path: Path, label: str) -> Path:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise BatchError(f"missing {label}: {path}") from exc
    if not stat.S_ISDIR(mode):
        raise BatchError(f"{label} is not a real directory: {path}")
    return path


def tree_ledger(root: Path) -> list[dict[str, Any]]:
    """Return a deterministic no-symlink regular-file ledger."""

    require_real_directory(root, "artifact tree")
    rows: list[dict[str, Any]] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise BatchError(f"artifact tree contains a symlink: {path}")
            if stat.S_ISDIR(mode):
                stack.append(path)
            elif stat.S_ISREG(mode):
                rows.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "bytes": path.stat().st_size,
                        "sha256": sha256_file(path),
                    }
                )
            else:
                raise BatchError(f"artifact tree contains a special file: {path}")
    rows.sort(key=lambda row: row["path"])
    return rows


def tree_sha256(root: Path, *, exclude: Iterable[str] = ()) -> str:
    excluded = set(exclude)
    digest = hashlib.sha256()
    for row in tree_ledger(root):
        if row["path"] in excluded:
            continue
        digest.update(row["path"].encode())
        digest.update(b"\0")
        digest.update(str(row["bytes"]).encode())
        digest.update(b"\0")
        digest.update(row["sha256"].encode())
        digest.update(b"\n")
    return digest.hexdigest()


def safe_remove_task_leaf(task_root: Path, leaf: Path) -> None:
    """Remove exactly one real child directory without following symlinks."""

    require_real_directory(task_root, "task root")
    try:
        relative = leaf.relative_to(task_root)
    except ValueError as exc:
        raise BatchError(f"cleanup target is outside task root: {leaf}") from exc
    if len(relative.parts) != 1 or relative.parts[0] in {"", ".", ".."}:
        raise BatchError(f"cleanup target is not one direct task leaf: {leaf}")
    require_real_directory(leaf, "task leaf")
    shutil.rmtree(leaf)
    if leaf.exists() or leaf.is_symlink():
        raise BatchError(f"task leaf still exists after cleanup: {leaf}")


def parse_fix_tcl(text: str, expected_count: int) -> list[dict[str, str]]:
    if not text.endswith("\n"):
        raise BatchError("fix.tcl must end with a newline")
    if "\r" in text or "\x00" in text:
        raise BatchError("fix.tcl contains forbidden control bytes")
    if FORBIDDEN_TCL.search(text):
        raise BatchError("fix.tcl contains a forbidden ECO/routing/constraint command")
    operations: list[dict[str, str]] = []
    executable: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        executable.append(stripped)
        match = ECO_CHANGE_RE.fullmatch(line)
        if match:
            instance, cell = match.groups()
            if not SAFE_OBJECT_RE.fullmatch(instance) or not SAFE_OBJECT_RE.fullmatch(cell):
                raise BatchError(f"fix.tcl:{line_number}: unsafe object name")
            operations.append({"instance": instance, "cell": cell})
            continue
        if stripped not in {
            "setEcoMode -batchMode true",
            "setEcoMode -batchMode false",
            "refinePlace -eco true",
        }:
            raise BatchError(f"fix.tcl:{line_number}: command is outside the allowlist")
    if executable.count("setEcoMode -batchMode true") != 1:
        raise BatchError("fix.tcl must open exactly one ECO batch")
    if executable.count("setEcoMode -batchMode false") != 1:
        raise BatchError("fix.tcl must close exactly one ECO batch")
    if executable.count("refinePlace -eco true") != 1:
        raise BatchError("fix.tcl must run exactly one incremental legalization")
    if executable[-1] != "refinePlace -eco true":
        raise BatchError("refinePlace -eco true must be the last executable command")
    if len(operations) != expected_count:
        raise BatchError(
            f"fix.tcl has {len(operations)} resize operations, expected {expected_count}"
        )
    instances = [operation["instance"] for operation in operations]
    if len(set(instances)) != len(instances):
        raise BatchError("fix.tcl resizes an instance more than once")
    return operations


def same_number(left: Any, right: Any, tolerance: float, label: str) -> None:
    a = require_number(left, f"{label}.left")
    b = require_number(right, f"{label}.right")
    if abs(a - b) > tolerance + 1e-12:
        raise BatchError(f"{label} differs by {abs(a-b):.12g}, limit {tolerance}")

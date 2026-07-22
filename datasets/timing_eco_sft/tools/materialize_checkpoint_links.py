#!/usr/bin/env python3
"""Safely materialize fetched Innovus checkpoint baseline symlinks.

Innovus may save ``violating.enc.dat`` with absolute links back into the
guest-side baseline checkpoint.  Once a replay has been fetched (and possibly
removed from the guest), those links are not portable.  This tool replaces
only the narrowly-defined baseline links with verified regular files from the
host baseline bound by ``run_manifest.json``.

The operation is intentionally separate from replay execution and dataset
finalization.  Incomplete or failed replays are skipped without inspecting
their checkpoint.  A completed replay receives a deterministic
``checkpoint_link_materialization.json`` next to ``violating.enc.dat``.  A
second invocation verifies that report and every materialized byte instead of
rewriting it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, NamedTuple, Sequence


SCHEMA_VERSION = "timing_eco_checkpoint_link_materialization.v1"
RUN_SCHEMA_VERSION = "timing_eco_pilot_run.v1"
CHECKPOINT_NAME = "violating.enc.dat"
REPORT_NAME = "checkpoint_link_materialization.json"
PENDING_REPORT_NAME = ".checkpoint_link_materialization.pending.json"
CHECKSUM_MANIFEST_NAME = "baseline_checksums.sha256"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
SAFE_ID_RE = re.compile(r"[A-Za-z0-9._-]+")
REPLAY_RE = re.compile(r"replay_([1-9][0-9]*)")


class MaterializationError(RuntimeError):
    """Raised when provenance or filesystem safety checks fail."""


class Context(NamedTuple):
    run_dir: Path
    run_id: str
    guest_baseline_root: PurePosixPath
    baseline_source: Path
    baseline_source_text: str
    baseline_sha256: str
    checksum_path: Path
    checksum_sha256: str
    checksums: dict[str, str]
    case_ids: tuple[str, ...]


class LinkPlan(NamedTuple):
    destination: Path
    source: Path
    path: str
    guest_target: str
    baseline_relative: str
    sha256: str
    bytes: int


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _lstat(path: Path, label: str) -> os.stat_result:
    try:
        return path.lstat()
    except FileNotFoundError as exc:
        raise MaterializationError(f"missing {label}: {path}") from exc
    except OSError as exc:
        raise MaterializationError(f"cannot inspect {label} {path}: {exc}") from exc


def _require_real_directory(path: Path, label: str) -> None:
    mode = _lstat(path, label).st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise MaterializationError(f"{label} must be a non-symlink directory: {path}")


def _require_regular(path: Path, label: str) -> None:
    mode = _lstat(path, label).st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise MaterializationError(f"{label} must be a non-symlink regular file: {path}")


def _read_json(path: Path, label: str) -> Any:
    _require_regular(path, label)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MaterializationError(f"cannot parse {label} {path}: {exc}") from exc


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MaterializationError(f"{label} must be a JSON object")
    return value


def _canonical_host_absolute(value: Any, label: str) -> tuple[Path, str]:
    if not isinstance(value, str) or not value:
        raise MaterializationError(f"{label} must be a non-empty absolute path")
    path = Path(value)
    if not path.is_absolute() or str(path) != value or any(part in {".", ".."} for part in path.parts):
        raise MaterializationError(f"{label} must use a canonical absolute path: {value!r}")
    return path, value


def _canonical_guest_absolute(value: Any, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise MaterializationError(f"{label} must be a non-empty absolute POSIX path")
    path = PurePosixPath(value)
    if not path.is_absolute() or str(path) != value or value == "/":
        raise MaterializationError(f"{label} must use a canonical absolute POSIX path: {value!r}")
    if any(part in {".", ".."} for part in path.parts):
        raise MaterializationError(f"{label} contains an unsafe path component: {value!r}")
    return path


def _canonical_relative(value: str, label: str) -> PurePosixPath:
    if not value or "\\" in value or "\n" in value or "\r" in value:
        raise MaterializationError(f"{label} is not a safe POSIX relative path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(part in {".", ".."} for part in path.parts):
        raise MaterializationError(f"{label} is not a canonical POSIX relative path: {value!r}")
    return path


def _hash_open_regular(path: Path, label: str) -> tuple[str, int]:
    """Hash one regular file while refusing a symlink at the final component."""

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise MaterializationError(f"cannot open {label} {path}: {exc}") from exc
    digest = hashlib.sha256()
    size = 0
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise MaterializationError(f"{label} must be a regular file: {path}")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest(), size


def _assert_real_source_path(root: Path, relative: PurePosixPath) -> Path:
    """Reject symlinks and non-directories in every baseline source component."""

    _require_real_directory(root, "baseline source")
    current = root
    parts = relative.parts
    if not parts:
        raise MaterializationError("baseline source relative path must name a file")
    for component in parts[:-1]:
        current = current / component
        _require_real_directory(current, "baseline source directory")
    source = current / parts[-1]
    _require_regular(source, "baseline source file")
    return source


def _parse_checksums(path: Path, expected_sha256: str) -> dict[str, str]:
    _require_regular(path, "baseline checksum manifest")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise MaterializationError(f"cannot read baseline checksum manifest {path}: {exc}") from exc
    actual_sha256 = _sha256_bytes(raw)
    if actual_sha256 != expected_sha256:
        raise MaterializationError(
            f"baseline checksum manifest SHA256 mismatch: {actual_sha256} != {expected_sha256}"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MaterializationError("baseline checksum manifest is not UTF-8") from exc
    if not text or not text.endswith("\n"):
        raise MaterializationError("baseline checksum manifest must be non-empty and newline-terminated")
    checksums: dict[str, str] = {}
    for line_number, line in enumerate(text.splitlines(), 1):
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if match is None:
            raise MaterializationError(
                f"invalid baseline checksum manifest line {line_number}: {line!r}"
            )
        digest, relative = match.groups()
        _canonical_relative(relative, f"checksum path on line {line_number}")
        if relative in checksums:
            raise MaterializationError(f"duplicate baseline checksum path: {relative}")
        checksums[relative] = digest
    return checksums


def _load_context(run_dir: Path) -> Context:
    run_dir = run_dir.absolute()
    _require_real_directory(run_dir, "run directory")
    manifest_path = run_dir / "run_manifest.json"
    manifest = _mapping(_read_json(manifest_path, "run manifest"), "run manifest")
    if manifest.get("schema_version") != RUN_SCHEMA_VERSION:
        raise MaterializationError(f"unsupported run manifest schema: {manifest.get('schema_version')!r}")

    run_id = manifest.get("run_id")
    if not isinstance(run_id, str) or SAFE_ID_RE.fullmatch(run_id) is None:
        raise MaterializationError("run manifest has an unsafe run_id")
    if run_dir.name != run_id:
        raise MaterializationError(
            f"run directory basename {run_dir.name!r} does not match run_id {run_id!r}"
        )
    guest_root = _canonical_guest_absolute(manifest.get("guest_root"), "run manifest guest_root")
    guest_baseline_root = guest_root / run_id / "baseline" / "base.enc.dat"

    baseline = _mapping(manifest.get("baseline"), "run manifest baseline")
    baseline_source, baseline_source_text = _canonical_host_absolute(
        baseline.get("source"), "run manifest baseline.source"
    )
    _require_real_directory(baseline_source, "baseline source")
    baseline_sha256 = baseline.get("sha256")
    if not isinstance(baseline_sha256, str) or SHA256_RE.fullmatch(baseline_sha256) is None:
        raise MaterializationError("run manifest baseline.sha256 is invalid")
    checksum = _mapping(
        baseline.get("checksum_manifest"), "run manifest baseline.checksum_manifest"
    )
    if checksum.get("path") != CHECKSUM_MANIFEST_NAME:
        raise MaterializationError(
            f"checksum manifest path must be {CHECKSUM_MANIFEST_NAME!r}"
        )
    checksum_sha256 = checksum.get("sha256")
    if not isinstance(checksum_sha256, str) or SHA256_RE.fullmatch(checksum_sha256) is None:
        raise MaterializationError("run manifest checksum-manifest SHA256 is invalid")
    checksum_path = run_dir / CHECKSUM_MANIFEST_NAME
    checksums = _parse_checksums(checksum_path, checksum_sha256)

    raw_case_ids = manifest.get("case_ids")
    if not isinstance(raw_case_ids, list) or not raw_case_ids:
        raise MaterializationError("run manifest case_ids must be a non-empty list")
    case_ids: list[str] = []
    for case_id in raw_case_ids:
        if not isinstance(case_id, str) or SAFE_ID_RE.fullmatch(case_id) is None:
            raise MaterializationError(f"run manifest has an unsafe case ID: {case_id!r}")
        if case_id in case_ids:
            raise MaterializationError(f"run manifest has duplicate case ID: {case_id}")
        case_ids.append(case_id)

    return Context(
        run_dir=run_dir,
        run_id=run_id,
        guest_baseline_root=guest_baseline_root,
        baseline_source=baseline_source,
        baseline_source_text=baseline_source_text,
        baseline_sha256=baseline_sha256,
        checksum_path=checksum_path,
        checksum_sha256=checksum_sha256,
        checksums=checksums,
        case_ids=tuple(case_ids),
    )


def _select_cases(available: Sequence[str], requested: Sequence[str]) -> list[str]:
    if not requested or list(requested) == ["all"]:
        return list(available)
    if "all" in requested:
        raise MaterializationError("--case all cannot be combined with another selection")
    if len(set(requested)) != len(requested):
        raise MaterializationError("duplicate --case selection")
    unknown = sorted(set(requested) - set(available))
    if unknown:
        raise MaterializationError("unknown case selection: " + ", ".join(unknown))
    # Preserve run-manifest order, independent of command-line ordering.
    selected = set(requested)
    return [case_id for case_id in available if case_id in selected]


def _eligible_replays(context: Context, case_id: str) -> list[tuple[int, Path]]:
    case_dir = context.run_dir / "cases" / case_id
    _require_real_directory(case_dir, "case directory")
    runs_dir = case_dir / "runs"
    _require_real_directory(runs_dir, "runs directory")
    eligible: list[tuple[int, Path]] = []
    try:
        with os.scandir(runs_dir) as iterator:
            entries = sorted(iterator, key=lambda item: item.name)
    except OSError as exc:
        raise MaterializationError(f"cannot scan runs directory {runs_dir}: {exc}") from exc
    for entry in entries:
        match = REPLAY_RE.fullmatch(entry.name)
        if match is None:
            continue
        replay_dir = Path(entry.path)
        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            raise MaterializationError(f"replay path must be a non-symlink directory: {replay_dir}")
        status_path = replay_dir / "run_status.json"
        if not status_path.exists() and not status_path.is_symlink():
            # A prepared or currently running replay has not been durably fetched.
            continue
        status_value = _read_json(status_path, "run status")
        status = _mapping(status_value, f"run status {status_path}")
        replay_index = int(match.group(1))
        if status.get("replay") != replay_index:
            raise MaterializationError(f"run status replay index mismatch: {status_path}")
        if status.get("passed") is True and status.get("fetched") is True:
            if status.get("innovus_exit_code") != 0:
                raise MaterializationError(
                    f"passed replay has a nonzero Innovus exit code: {status_path}"
                )
            eligible.append((replay_index, replay_dir))
    return sorted(eligible, key=lambda item: item[0])


def _scan_checkpoint(checkpoint: Path) -> tuple[list[tuple[Path, str, str]], list[Path]]:
    """Return symlink records and regular files after a no-follow tree walk."""

    _require_real_directory(checkpoint, "violating checkpoint")
    links: list[tuple[Path, str, str]] = []
    regular_files: list[Path] = []
    stack = [checkpoint]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name, reverse=True)
        except OSError as exc:
            raise MaterializationError(f"cannot scan checkpoint directory {directory}: {exc}") from exc
        child_directories: list[Path] = []
        for entry in entries:
            path = Path(entry.path)
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                raise MaterializationError(f"cannot inspect checkpoint entry {path}: {exc}") from exc
            relative = path.relative_to(checkpoint).as_posix()
            _canonical_relative(relative, "checkpoint entry path")
            if stat.S_ISDIR(mode):
                child_directories.append(path)
            elif stat.S_ISLNK(mode):
                try:
                    target = os.readlink(path)
                except OSError as exc:
                    raise MaterializationError(f"cannot read checkpoint symlink {path}: {exc}") from exc
                links.append((path, relative, target))
            elif stat.S_ISREG(mode):
                regular_files.append(path)
            else:
                raise MaterializationError(
                    f"checkpoint contains a non-regular, non-directory entry: {path}"
                )
        # Entries were reverse-sorted; stack order below makes traversal stable.
        stack.extend(child_directories)
    links.sort(key=lambda item: item[1])
    regular_files.sort(key=lambda item: item.relative_to(checkpoint).as_posix())
    return links, regular_files


def _plan_link(
    context: Context, checkpoint: Path, path: Path, relative: str, target: str
) -> LinkPlan:
    target_path = _canonical_guest_absolute(target, f"checkpoint symlink target {path}")
    prefix = context.guest_baseline_root.parts
    if target_path.parts[: len(prefix)] != prefix or len(target_path.parts) <= len(prefix):
        raise MaterializationError(
            f"checkpoint symlink target is outside the bound guest baseline: {path} -> {target}"
        )
    target_relative = PurePosixPath(*target_path.parts[len(prefix) :])
    if target_relative.as_posix() != relative:
        raise MaterializationError(
            "checkpoint symlink path does not match its baseline-relative target: "
            f"{relative!r} != {target_relative.as_posix()!r}"
        )
    baseline_relative = target_relative.as_posix()
    checksum_relative = (PurePosixPath("base.enc.dat") / target_relative).as_posix()
    source = _assert_real_source_path(
        context.baseline_source, PurePosixPath(checksum_relative)
    )
    expected_sha256 = context.checksums.get(checksum_relative)
    if expected_sha256 is None:
        raise MaterializationError(
            f"checkpoint symlink source is absent from baseline checksums: {checksum_relative}"
        )
    actual_sha256, actual_bytes = _hash_open_regular(source, "baseline source file")
    if actual_sha256 != expected_sha256:
        raise MaterializationError(
            f"baseline source SHA256 mismatch for {checksum_relative}: "
            f"{actual_sha256} != {expected_sha256}"
        )
    if actual_bytes != source.stat(follow_symlinks=False).st_size:
        raise MaterializationError(f"baseline source size changed while hashing: {source}")
    return LinkPlan(
        destination=path,
        source=source,
        path=relative,
        guest_target=target,
        baseline_relative=baseline_relative,
        sha256=actual_sha256,
        bytes=actual_bytes,
    )


def _report(context: Context, case_id: str, replay_index: int, plans: Sequence[LinkPlan]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "replay": replay_index,
        "checkpoint": CHECKPOINT_NAME,
        "baseline": {
            "host_source": context.baseline_source_text,
            "bundle_sha256": context.baseline_sha256,
            "guest_checkpoint_root": str(context.guest_baseline_root),
            "checksum_manifest": {
                "path": CHECKSUM_MANIFEST_NAME,
                "sha256": context.checksum_sha256,
            },
        },
        "materialized_links": [
            {
                "path": plan.path,
                "guest_target": plan.guest_target,
                "baseline_relative": plan.baseline_relative,
                "sha256": plan.sha256,
                "bytes": plan.bytes,
            }
            for plan in plans
        ],
    }


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, data: bytes) -> None:
    descriptor, temporary_text = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_text)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _atomic_copy(plan: LinkPlan) -> None:
    try:
        if not plan.destination.is_symlink() or os.readlink(plan.destination) != plan.guest_target:
            raise MaterializationError(
                f"checkpoint symlink changed after validation: {plan.destination}"
            )
    except OSError as exc:
        raise MaterializationError(
            f"cannot revalidate checkpoint symlink {plan.destination}: {exc}"
        ) from exc

    descriptor, temporary_text = tempfile.mkstemp(
        prefix=f".{plan.destination.name}.materialize.", dir=plan.destination.parent
    )
    temporary = Path(temporary_text)
    source_descriptor: int | None = None
    digest = hashlib.sha256()
    copied = 0
    try:
        source_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            source_flags |= os.O_NOFOLLOW
        source_descriptor = os.open(plan.source, source_flags)
        source_stat = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_stat.st_mode):
            raise MaterializationError(f"baseline source ceased to be regular: {plan.source}")
        os.fchmod(descriptor, stat.S_IMODE(source_stat.st_mode))
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            copied += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise MaterializationError(f"short write while materializing {plan.destination}")
                view = view[written:]
        os.fsync(descriptor)
        actual_sha256 = digest.hexdigest()
        if actual_sha256 != plan.sha256 or copied != plan.bytes:
            raise MaterializationError(
                f"baseline source changed while copying {plan.baseline_relative}"
            )
        if not plan.destination.is_symlink() or os.readlink(plan.destination) != plan.guest_target:
            raise MaterializationError(
                f"checkpoint symlink changed before replacement: {plan.destination}"
            )
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, plan.destination)
        _fsync_directory(plan.destination.parent)
    except BaseException:
        try:
            if descriptor >= 0:
                os.close(descriptor)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        if source_descriptor is not None:
            os.close(source_descriptor)


def _load_report_plans(
    context: Context,
    case_id: str,
    replay_index: int,
    checkpoint: Path,
    report_path: Path,
) -> list[LinkPlan]:
    """Validate a final report or pending journal and rebuild its exact plan."""

    raw = _read_json(report_path, "checkpoint link materialization report")
    report = _mapping(raw, "checkpoint link materialization report")
    if report_path.read_bytes() != _json_bytes(raw):
        raise MaterializationError(f"report is not in deterministic canonical JSON form: {report_path}")
    expected_header = {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "replay": replay_index,
        "checkpoint": CHECKPOINT_NAME,
        "baseline": {
            "host_source": context.baseline_source_text,
            "bundle_sha256": context.baseline_sha256,
            "guest_checkpoint_root": str(context.guest_baseline_root),
            "checksum_manifest": {
                "path": CHECKSUM_MANIFEST_NAME,
                "sha256": context.checksum_sha256,
            },
        },
    }
    for key, expected in expected_header.items():
        if report.get(key) != expected:
            raise MaterializationError(f"report {key} does not match current provenance: {report_path}")
    if set(report) != {*expected_header, "materialized_links"}:
        raise MaterializationError(f"report has unexpected or missing top-level keys: {report_path}")

    entries = report.get("materialized_links")
    if not isinstance(entries, list) or not entries:
        raise MaterializationError(
            f"report materialized_links must be a non-empty list: {report_path}"
        )
    canonical_entries: list[dict[str, Any]] = []
    plans: list[LinkPlan] = []
    seen: set[str] = set()
    for index, entry_value in enumerate(entries):
        entry = _mapping(entry_value, f"report materialized_links[{index}]")
        if set(entry) != {"path", "guest_target", "baseline_relative", "sha256", "bytes"}:
            raise MaterializationError(f"report entry {index} has unexpected or missing keys")
        relative_value = entry.get("path")
        if not isinstance(relative_value, str):
            raise MaterializationError(f"report entry {index} path must be a string")
        relative = _canonical_relative(relative_value, f"report entry {index} path")
        if relative_value in seen:
            raise MaterializationError(f"duplicate report path: {relative_value}")
        seen.add(relative_value)
        expected_target = str(context.guest_baseline_root / relative)
        expected_baseline_relative = relative.as_posix()
        checksum_relative = (
            PurePosixPath("base.enc.dat") / relative
        ).as_posix()
        digest = entry.get("sha256")
        byte_count = entry.get("bytes")
        if entry.get("guest_target") != expected_target:
            raise MaterializationError(f"report guest_target mismatch for {relative_value}")
        if entry.get("baseline_relative") != expected_baseline_relative:
            raise MaterializationError(f"report baseline_relative mismatch for {relative_value}")
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise MaterializationError(f"report SHA256 is invalid for {relative_value}")
        if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
            raise MaterializationError(f"report byte count is invalid for {relative_value}")
        if context.checksums.get(checksum_relative) != digest:
            raise MaterializationError(f"report SHA256 is not bound by checksums for {relative_value}")

        source = _assert_real_source_path(
            context.baseline_source, PurePosixPath(checksum_relative)
        )
        source_sha256, source_bytes = _hash_open_regular(source, "baseline source file")
        if source_sha256 != digest or source_bytes != byte_count:
            raise MaterializationError(f"host baseline source changed for {relative_value}")
        plans.append(
            LinkPlan(
                destination=checkpoint.joinpath(*relative.parts),
                source=source,
                path=relative_value,
                guest_target=expected_target,
                baseline_relative=expected_baseline_relative,
                sha256=digest,
                bytes=byte_count,
            )
        )
        canonical_entries.append(dict(entry))
    if [entry["path"] for entry in canonical_entries] != sorted(seen):
        raise MaterializationError(f"report materialized_links are not sorted by path: {report_path}")
    if dict(report) != {**expected_header, "materialized_links": canonical_entries}:
        raise MaterializationError(f"report is not canonical: {report_path}")
    if dict(report) != _report(context, case_id, replay_index, plans):
        raise MaterializationError(f"report does not exactly reconstruct its copy plan: {report_path}")
    return plans


def _verify_materialized_file(plan: LinkPlan) -> None:
    _require_regular(plan.destination, "materialized checkpoint file")
    materialized_sha256, materialized_bytes = _hash_open_regular(
        plan.destination, "materialized checkpoint file"
    )
    if materialized_sha256 != plan.sha256 or materialized_bytes != plan.bytes:
        raise MaterializationError(
            f"materialized checkpoint file mismatch: {plan.destination}"
        )


def _validate_existing_report(
    context: Context,
    case_id: str,
    replay_index: int,
    checkpoint: Path,
    report_path: Path,
) -> Path:
    plans = _load_report_plans(
        context, case_id, replay_index, checkpoint, report_path
    )
    links, _ = _scan_checkpoint(checkpoint)
    if links:
        raise MaterializationError(
            f"reported checkpoint still contains a symlink: {links[0][0]}"
        )
    for plan in plans:
        _verify_materialized_file(plan)
    return report_path


def _resume_pending_report(
    context: Context,
    case_id: str,
    replay_index: int,
    replay_dir: Path,
    checkpoint: Path,
    pending_path: Path,
    report_path: Path,
) -> Path:
    """Resume an interrupted plan, then atomically promote its journal."""

    plans = _load_report_plans(
        context, case_id, replay_index, checkpoint, pending_path
    )
    links, _ = _scan_checkpoint(checkpoint)
    journal_paths = {plan.path for plan in plans}
    actual_links = {relative: (path, target) for path, relative, target in links}
    unexpected = sorted(set(actual_links) - journal_paths)
    if unexpected:
        raise MaterializationError(
            f"checkpoint has a symlink absent from pending journal: {unexpected[0]}"
        )

    remaining: list[LinkPlan] = []
    for plan in plans:
        actual_link = actual_links.get(plan.path)
        if actual_link is not None:
            _, target = actual_link
            if target != plan.guest_target:
                raise MaterializationError(
                    f"checkpoint symlink disagrees with pending journal: {plan.destination}"
                )
            remaining.append(plan)
        else:
            _verify_materialized_file(plan)

    for plan in remaining:
        _atomic_copy(plan)
    final_links, _ = _scan_checkpoint(checkpoint)
    if final_links:
        raise MaterializationError(
            f"checkpoint still contains a symlink after journal replay: {final_links[0][0]}"
        )
    for plan in plans:
        _verify_materialized_file(plan)
    if report_path.exists() or report_path.is_symlink():
        raise MaterializationError(
            f"final materialization report appeared during pending recovery: {report_path}"
        )
    os.replace(pending_path, report_path)
    _fsync_directory(replay_dir)
    return _validate_existing_report(
        context, case_id, replay_index, checkpoint, report_path
    )


def materialize_replay(
    context: Context, case_id: str, replay_index: int, replay_dir: Path
) -> Path:
    checkpoint = replay_dir / CHECKPOINT_NAME
    _require_real_directory(checkpoint, "violating checkpoint")
    report_path = replay_dir / REPORT_NAME
    pending_path = replay_dir / PENDING_REPORT_NAME
    report_present = report_path.exists() or report_path.is_symlink()
    pending_present = pending_path.exists() or pending_path.is_symlink()
    if report_present and pending_present:
        raise MaterializationError(
            f"both final report and pending journal exist for replay: {replay_dir}"
        )
    if report_present:
        return _validate_existing_report(
            context, case_id, replay_index, checkpoint, report_path
        )
    if pending_present:
        return _resume_pending_report(
            context,
            case_id,
            replay_index,
            replay_dir,
            checkpoint,
            pending_path,
            report_path,
        )

    links, _ = _scan_checkpoint(checkpoint)
    if not links:
        raise MaterializationError(
            "passed+fetched checkpoint has no auditable baseline symlink and no "
            f"materialization report: {checkpoint}"
        )
    plans = [
        _plan_link(context, checkpoint, path, relative, target)
        for path, relative, target in links
    ]
    payload = _report(context, case_id, replay_index, plans)
    # The durable journal is created before the first mutation.  A later
    # invocation can therefore distinguish already-copied files from links
    # still awaiting replacement and recover without losing provenance.
    _atomic_write(pending_path, _json_bytes(payload))
    return _resume_pending_report(
        context,
        case_id,
        replay_index,
        replay_dir,
        checkpoint,
        pending_path,
        report_path,
    )


def materialize_run(run_dir: Path, requested: Sequence[str] = ()) -> list[Path]:
    context = _load_context(run_dir)
    selected = _select_cases(context.case_ids, requested)
    reports: list[Path] = []
    for case_id in selected:
        for replay_index, replay_dir in _eligible_replays(context, case_id):
            reports.append(
                materialize_replay(context, case_id, replay_index, replay_dir)
            )
    return reports


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        metavar="ID|all",
        help="case ID to process; repeat it, or omit/use 'all' for all prepared cases",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        reports = materialize_run(args.run_dir, args.case)
        print(f"materialized or revalidated {len(reports)} passed+fetched replay checkpoint(s)")
    except (MaterializationError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

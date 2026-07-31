#!/usr/bin/env python3
"""Batch-local CLI for Mock LEF Batch-100.

The CLI owns catalog validation, immutable input binding, SQLite/sidecar state,
the one-time read-only probe, queue gating, artifact fetch, and finalization.
Innovus processes run only in isolated Firecracker guests.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import heapq
import itertools
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, deque
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import catalog
import checkpoint_archive
import evidence
import finalize_batch
import relaxed_v3
from common import (
    BatchError,
    SAFE_ID_RE,
    atomic_json,
    atomic_write,
    parse_fix_tcl,
    read_json,
    require_hash,
    require_real_directory,
    require_real_file,
    sha256_file,
    tree_sha256,
)
from state_store import StateStore


HERE = Path(__file__).resolve()
ROOT = HERE.parents[1]
REPO_ROOT = HERE.parents[4]
SPECS_PATH = Path(os.environ.get("B100_SPECS_PATH", ROOT / "case_specs.json"))
DEFAULT_BASELINE = (
    REPO_ROOT
    / "datasets"
    / "timing_eco_sft"
    / "pilot_10"
    / "work"
    / "baseline_qualified"
)
DEFAULT_RUN_DIR = ROOT / "work" / "batch100-v1"
GUEST_KEY = Path(
    os.environ.get("B100_GUEST_KEY", "/home/gkcy/.ssh/firecracker_qingteng_ed25519")
)
REMOTE_HOST = os.environ.get("B100_REMOTE_HOST", "chenyu@8.92.9.186")
REMOTE_STORAGE_ROOT = os.environ.get("B100_REMOTE_STORAGE_ROOT", "/data0/chenyu")
DIRECT_GUEST_SSH = os.environ.get("B100_DIRECT_GUEST_SSH") == "1"
# The ten-slot pool can expose up to 120 GiB at the guest /work 75% stop
# threshold (10 * 16 GiB * 75%).  Keep another 8 GiB on the host for COW and
# orchestration writes before admitting a new dispatch wave.
REMOTE_HOST_MIN_AVAILABLE_KIB = 128 * 1024 * 1024
GUEST_BASELINE = checkpoint_archive.GUEST_BASELINE_PREFIX
GUEST_RUN_ROOT = "/work/mock_lef_batch100_v1"
TOOLS = {
    "catalog": ROOT / "tools" / "catalog.py",
    "runner": ROOT / "tools" / "batch100.py",
    "state_store": ROOT / "tools" / "state_store.py",
    "common": ROOT / "tools" / "common.py",
    "checkpoint_archive": ROOT / "tools" / "checkpoint_archive.py",
    "probe": ROOT / "tools" / "probe.tcl",
    "calibrate": ROOT / "tools" / "calibrate.tcl",
    "runtime": ROOT / "tools" / "runtime.tcl",
    "evidence": ROOT / "tools" / "evidence.py",
    "finalizer": ROOT / "tools" / "finalize_batch.py",
    "relaxed_v3": ROOT / "tools" / "relaxed_v3.py",
}

RELAXED_V2_SCHEMA = "mock_lef_batch100.case_specs.relaxed_v2"
RELAXED_V3_SCHEMA = relaxed_v3.SCHEMA
RELAXED_SCHEMAS = {RELAXED_V2_SCHEMA, RELAXED_V3_SCHEMA}


class CalibrationCandidateRejected(BatchError):
    """A measured candidate miss, distinct from infrastructure failure."""


FROZEN_CLEANUP_SCHEMA = "mock_lef_batch100.frozen_cleanup.v1"
REPLAY_ASSIGNMENT_SCHEMA = "mock_lef_batch100.replay_assignment.v1"


def _validate_specs(specs: Mapping[str, Any]) -> None:
    """Validate the immutable catalog spec or an authorized relaxed contract.

    Relaxed contracts change construction difficulty only.  They retain every
    physical/replay acceptance gate and must not silently fall through the
    catalog validator.
    """

    schema = specs.get("schema_version")
    if schema == RELAXED_V3_SCHEMA:
        try:
            relaxed_v3.validate_specs(specs)
        except relaxed_v3.RelaxedV3Error as exc:
            raise BatchError(f"invalid relaxed-v3 specification: {exc}") from exc
        return
    if schema != RELAXED_V2_SCHEMA:
        catalog.validate_specs(dict(specs))
        return
    cases = specs.get("cases")
    if not isinstance(cases, list) or len(cases) != 100:
        raise BatchError("relaxed-v2 requires exactly 100 structured cases")
    expected_ids = [f"B1_CASE_{index:03d}" for index in range(1, 101)]
    if [case.get("id") for case in cases] != expected_ids:
        raise BatchError("relaxed-v2 case IDs are not B1_CASE_001..100 in order")
    relaxation = specs.get("relaxation")
    if not isinstance(relaxation, Mapping) or not relaxation.get("authorized_by_user"):
        raise BatchError("relaxed-v2 is missing explicit user authorization")
    required_gates = {
        "real_innovus",
        "exact_negative_endpoint_set",
        "setup_tns_zero_after_repair",
        "no_new_negative_endpoint",
        "drv_drc_connectivity_placement_no_regression",
        "dual_independent_replay",
    }
    if not required_gates <= set(relaxation.get("preserved_gates", ())):
        raise BatchError("relaxed-v2 weakens a preserved physical/replay gate")
    for case in cases:
        if (
            case.get("expected_modification_count") != 1
            or case.get("injection_profile") != "I0"
            or case.get("direct_inverse_allowed") is not True
            or case.get("target_endpoint_count") != 1
        ):
            raise BatchError(f"{case.get('id')}: invalid relaxed-v2 direct-inverse policy")
        severity = case.get("nominal_severity", {})
        ps = severity.get("ps")
        if ps not in {60, 80, 100} or severity.get("target_wns_ns") != -ps / 1000.0:
            raise BatchError(f"{case.get('id')}: invalid relaxed-v2 severity")


def _run_id(run_dir: Path) -> str:
    value = run_dir.name
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value) is None:
        raise BatchError(f"unsafe run directory leaf: {value!r}")
    return value


def _input_hashes() -> dict[str, str]:
    paths = {
        "catalog": catalog.CATALOG,
        "case_specs": SPECS_PATH,
        **{f"tool_{name}": path for name, path in TOOLS.items()},
    }
    return {name: sha256_file(require_real_file(path, name)) for name, path in paths.items()}


def _baseline_binding(path: Path) -> dict[str, Any]:
    require_real_directory(path, "baseline bundle")
    for name in (
        "base.enc",
        "base.enc.dat",
        "manifest.tcl",
        "restore.tcl",
        "qualification.json",
        "QUALIFIED_CANDIDATE",
    ):
        candidate = path / name
        if name.endswith(".dat"):
            require_real_directory(candidate, f"baseline {name}")
        else:
            require_real_file(candidate, f"baseline {name}")
    qualification = read_json(path / "qualification.json", "baseline qualification")
    if qualification.get("schema_version") != "smic40_baseline_qualification.v1":
        raise BatchError("unsupported baseline qualification schema")
    if qualification.get("status") != "QUALIFIED_CANDIDATE":
        raise BatchError("baseline is not QUALIFIED_CANDIDATE")
    if qualification.get("top") != "NV_NVDLA_CMAC_CORE_mac":
        raise BatchError("baseline top mismatch")
    timing = qualification.get("timing", {})
    setup = timing.get("setup", {})
    if float(setup.get("wns_ns", -1)) < 0 or float(setup.get("tns_ns", -1)) != 0:
        raise BatchError("baseline setup is not timing-clean")
    return {
        "source": str(path.resolve()),
        "entry_sha256": sha256_file(path / "base.enc"),
        "tree_sha256": tree_sha256(path),
        "qualification_sha256": sha256_file(path / "qualification.json"),
        "top": qualification["top"],
        "setup_view": qualification["analysis_views"]["setup"],
        "hold_view": qualification["analysis_views"]["hold"],
        "setup_wns_ns": setup["wns_ns"],
        "setup_tns_ns": setup["tns_ns"],
        "remote_path": GUEST_BASELINE,
    }


def _ssh_options(slot: int) -> list[str]:
    if slot not in range(10):
        raise BatchError(f"invalid slot: {slot}")
    options = [
        "-i",
        str(GUEST_KEY),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=8",
    ]
    if not DIRECT_GUEST_SSH:
        options.extend(
            [
                "-o",
                f"ProxyCommand=ssh -o BatchMode=yes -o ConnectTimeout=8 -W %h:%p {REMOTE_HOST}",
            ]
        )
    return options


def _guest(slot: int) -> str:
    return f"host@172.30.{slot}.10"


def _ssh(slot: int, command: str, *, timeout: int = 60, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ssh", *_ssh_options(slot), _guest(slot), command],
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        timeout=timeout,
    )


def _scp_to(slot: int, source: Path, destination: str) -> None:
    result = subprocess.run(
        ["scp", *_ssh_options(slot), str(source), f"{_guest(slot)}:{destination}"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode:
        raise BatchError(f"scp to slot{slot} failed: {result.stdout[-2000:]}")


def _scp_tree_to(slot: int, source: Path, destination: str) -> None:
    require_real_directory(source, "scp source tree")
    result = subprocess.run(
        ["scp", "-r", *_ssh_options(slot), str(source), f"{_guest(slot)}:{destination}"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode:
        raise BatchError(f"recursive scp to slot{slot} failed: {result.stdout[-2000:]}")


def _scp_from(slot: int, source: str, destination: Path, *, recursive: bool = False) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = ["scp", *_ssh_options(slot)]
    if recursive:
        command.append("-r")
    command.extend((f"{_guest(slot)}:{source}", str(destination)))
    result = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode:
        raise BatchError(f"scp from slot{slot} failed: {result.stdout[-2000:]}")


def _pack_guest_checkpoint(slot: int, inject_root: str) -> str:
    """Create one non-dereferencing tar in the exact injection leaf."""

    archive = f"{inject_root}/{checkpoint_archive.CHECKPOINT_ARCHIVE_NAME}"
    command = (
        f"cd {inject_root} && test ! -e {checkpoint_archive.CHECKPOINT_ARCHIVE_NAME} "
        f"&& tar -cf {checkpoint_archive.CHECKPOINT_ARCHIVE_NAME} -- "
        "violating.enc violating.enc.dat "
        f"&& sha256sum {checkpoint_archive.CHECKPOINT_ARCHIVE_NAME}"
    )
    result = _ssh(slot, command, timeout=1800)
    if result.returncode:
        raise BatchError(
            f"slot{slot}: checkpoint archive creation/hash failed: "
            f"{(result.stdout or '')[-2000:]}"
        )
    matches = re.findall(
        rf"(?m)^([0-9a-f]{{64}})  "
        rf"{re.escape(checkpoint_archive.CHECKPOINT_ARCHIVE_NAME)}\s*$",
        result.stdout or "",
    )
    if len(matches) != 1:
        raise BatchError(f"slot{slot}: ambiguous checkpoint archive SHA-256")
    return matches[0]


def _extract_guest_checkpoint_archive(
    slot: int, checkpoint_root: str, expected_sha256: str
) -> None:
    """Extract the exact locally preflighted archive on a CentOS guest.

    The guest image intentionally has no Python 3.  Safety comes from the
    local full-member preflight plus the SHA-256 equality check immediately
    before GNU tar sees those same bytes.
    """

    archive = f"{checkpoint_root}/{checkpoint_archive.CHECKPOINT_ARCHIVE_NAME}"
    destination = f"{checkpoint_root}/unpacked"
    command = (
        f"cd {checkpoint_root} "
        f"&& test ! -e {destination} && test ! -L {destination} "
        f"&& printf '%s  %s\\n' {expected_sha256} "
        f"{checkpoint_archive.CHECKPOINT_ARCHIVE_NAME} "
        f"| sha256sum -c - "
        f"&& mkdir {destination} "
        f"&& tar --no-same-owner --no-same-permissions "
        f"-xf {checkpoint_archive.CHECKPOINT_ARCHIVE_NAME} "
        f"-C {destination} "
        f"&& test -f {destination}/violating.enc "
        f"&& test -d {destination}/violating.enc.dat "
        f"&& printf '%s\\n' 'B100_CHECKPOINT_ARCHIVE_EXTRACT_PASS "
        f"{expected_sha256}'"
    )
    result = _ssh(slot, command, timeout=1800)
    marker = re.findall(
        rf"(?m)^B100_CHECKPOINT_ARCHIVE_EXTRACT_PASS "
        rf"{re.escape(expected_sha256)}\s*$",
        result.stdout or "",
    )
    if result.returncode or len(marker) != 1:
        raise BatchError(
            f"slot{slot}: checkpoint archive safe extraction/hash failed: "
            f"{(result.stdout or '')[-2000:]}"
        )


FROZEN_CHECKPOINT_SCHEMA = "mock_lef_batch100.frozen_checkpoint.v1"


def _frozen_checkpoint_paths(job: Path) -> tuple[Path, Path]:
    return job / "frozen_checkpoint", job / "frozen_checkpoint.ready.json"


def _frozen_checkpoint_ledger_sha256(payload: Mapping[str, Any]) -> str:
    ledger = {
        key: value
        for key, value in payload.items()
        if key != "freeze_ledger_sha256"
    }
    encoded = json.dumps(
        ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_frozen_checkpoint_ready(
    run_dir: Path,
    case_id: str,
    job: Path,
    *,
    plan_path: Path,
    binding_path: Path,
    config_path: Path,
) -> tuple[Path, dict[str, Any]]:
    """Atomically bind a complete, locally recoverable violating checkpoint."""

    frozen_root, ready = _frozen_checkpoint_paths(job)
    require_real_directory(frozen_root, f"{case_id} frozen checkpoint root")
    if ready.exists() or ready.is_symlink():
        raise BatchError(f"{case_id}: frozen checkpoint ready marker already exists")
    archive = require_real_file(
        frozen_root / checkpoint_archive.CHECKPOINT_ARCHIVE_NAME,
        f"{case_id} frozen checkpoint archive",
    )
    checkpoint = require_real_directory(
        frozen_root / "unpacked", f"{case_id} unpacked frozen checkpoint"
    )
    wrapper = require_real_file(
        checkpoint / "violating.enc", f"{case_id} frozen checkpoint wrapper"
    )
    data = require_real_directory(
        checkpoint / "violating.enc.dat", f"{case_id} frozen checkpoint data"
    )
    reports = require_real_directory(
        frozen_root / "injection_reports_fetch" / "reports",
        f"{case_id} frozen injection reports",
    )
    inject_log = require_real_file(
        frozen_root / "inject_innovus.log", f"{case_id} frozen injection log"
    )
    checkpoint_archive.inspect_checkpoint_archive(
        archive, baseline_prefix=GUEST_BASELINE
    )
    wrapper_hash = sha256_file(wrapper)
    tree_hash = checkpoint_archive.checkpoint_tree_sha256_v2(
        data, baseline_prefix=GUEST_BASELINE
    )
    payload: dict[str, Any] = {
        "schema_version": FROZEN_CHECKPOINT_SCHEMA,
        "case_id": case_id,
        "run_id": _run_id(run_dir),
        "archive_sha256": sha256_file(archive),
        "archive_bytes": archive.stat().st_size,
        "checkpoint_wrapper_sha256": wrapper_hash,
        "checkpoint_tree_sha256": tree_hash,
        "checkpoint_tree_hash_algorithm": (
            checkpoint_archive.CHECKPOINT_TREE_HASH_ALGORITHM
        ),
        "checkpoint_bundle_sha256": (
            checkpoint_archive.checkpoint_bundle_sha256_v1_from_hashes(
                wrapper_hash, tree_hash
            )
        ),
        "checkpoint_bundle_hash_algorithm": (
            checkpoint_archive.CHECKPOINT_BUNDLE_HASH_ALGORITHM
        ),
        "calibration_plan_sha256": sha256_file(
            require_real_file(plan_path, f"{case_id} calibration plan")
        ),
        "binding_sha256": sha256_file(
            require_real_file(binding_path, f"{case_id} binding")
        ),
        "runtime_config_sha256": sha256_file(
            require_real_file(config_path, f"{case_id} runtime config")
        ),
        "runtime_tcl_sha256": sha256_file(
            require_real_file(TOOLS["runtime"], "runtime Tcl")
        ),
        "runtime_common_sha256": sha256_file(
            require_real_file(TOOLS["common"], "runtime common module")
        ),
        "injection_log_sha256": sha256_file(inject_log),
        "injection_reports_tree_sha256": tree_sha256(reports),
    }
    payload["freeze_ledger_sha256"] = _frozen_checkpoint_ledger_sha256(payload)
    atomic_json(ready, payload, durable=True)
    return ready, payload


def _read_frozen_checkpoint(
    run_dir: Path,
    case_id: str,
    job: Path,
    *,
    frozen_root_override: Path | None = None,
    ready_override: Path | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    """Fail closed if any FROZEN checkpoint input or evidence has changed."""

    canonical_root, canonical_ready = _frozen_checkpoint_paths(job)
    frozen_root = frozen_root_override or canonical_root
    ready = ready_override or canonical_ready
    payload = read_json(
        require_real_file(ready, f"{case_id} frozen checkpoint ready marker"),
        f"{case_id} frozen checkpoint ready marker",
    )
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != FROZEN_CHECKPOINT_SCHEMA
        or payload.get("case_id") != case_id
        or payload.get("run_id") != _run_id(run_dir)
    ):
        raise BatchError(f"{case_id}: invalid frozen checkpoint marker identity")
    for key in (
        "archive_sha256",
        "checkpoint_wrapper_sha256",
        "checkpoint_tree_sha256",
        "checkpoint_bundle_sha256",
        "calibration_plan_sha256",
        "binding_sha256",
        "runtime_config_sha256",
        "runtime_tcl_sha256",
        "runtime_common_sha256",
        "injection_log_sha256",
        "injection_reports_tree_sha256",
        "freeze_ledger_sha256",
    ):
        require_hash(payload.get(key), f"{case_id}.frozen_checkpoint.{key}")
    if (
        payload.get("checkpoint_tree_hash_algorithm")
        != checkpoint_archive.CHECKPOINT_TREE_HASH_ALGORITHM
        or payload.get("checkpoint_bundle_hash_algorithm")
        != checkpoint_archive.CHECKPOINT_BUNDLE_HASH_ALGORITHM
        or payload["freeze_ledger_sha256"]
        != _frozen_checkpoint_ledger_sha256(payload)
    ):
        raise BatchError(f"{case_id}: invalid frozen checkpoint hash ledger")

    require_real_directory(frozen_root, f"{case_id} frozen checkpoint root")
    archive = require_real_file(
        frozen_root / checkpoint_archive.CHECKPOINT_ARCHIVE_NAME,
        f"{case_id} frozen checkpoint archive",
    )
    checkpoint = require_real_directory(
        frozen_root / "unpacked", f"{case_id} unpacked frozen checkpoint"
    )
    wrapper = require_real_file(
        checkpoint / "violating.enc", f"{case_id} frozen checkpoint wrapper"
    )
    data = require_real_directory(
        checkpoint / "violating.enc.dat", f"{case_id} frozen checkpoint data"
    )
    reports = require_real_directory(
        frozen_root / "injection_reports_fetch" / "reports",
        f"{case_id} frozen injection reports",
    )
    inject_log = require_real_file(
        frozen_root / "inject_innovus.log", f"{case_id} frozen injection log"
    )
    if (
        not isinstance(payload.get("archive_bytes"), int)
        or payload["archive_bytes"] < 0
        or archive.stat().st_size != payload["archive_bytes"]
        or sha256_file(archive) != payload["archive_sha256"]
    ):
        raise BatchError(f"{case_id}: frozen checkpoint archive changed")
    checkpoint_archive.inspect_checkpoint_archive(
        archive, baseline_prefix=GUEST_BASELINE
    )
    actual_wrapper_hash = sha256_file(wrapper)
    actual_tree_hash = checkpoint_archive.checkpoint_tree_sha256_v2(
        data, baseline_prefix=GUEST_BASELINE
    )
    if (
        actual_wrapper_hash != payload["checkpoint_wrapper_sha256"]
        or actual_tree_hash != payload["checkpoint_tree_sha256"]
        or checkpoint_archive.checkpoint_bundle_sha256_v1_from_hashes(
            actual_wrapper_hash, actual_tree_hash
        )
        != payload["checkpoint_bundle_sha256"]
        or sha256_file(job / "calibration_plan.json")
        != payload["calibration_plan_sha256"]
        or sha256_file(run_dir / "bindings" / f"{case_id}.json")
        != payload["binding_sha256"]
        or sha256_file(job / "runtime_config.tcl")
        != payload["runtime_config_sha256"]
        or sha256_file(TOOLS["runtime"]) != payload["runtime_tcl_sha256"]
        or sha256_file(TOOLS["common"]) != payload["runtime_common_sha256"]
        or sha256_file(inject_log) != payload["injection_log_sha256"]
        or tree_sha256(reports) != payload["injection_reports_tree_sha256"]
    ):
        raise BatchError(f"{case_id}: frozen checkpoint hash binding changed")
    return frozen_root, archive, payload


def _frozen_cleanup_paths(job: Path) -> tuple[Path, Path]:
    return (
        job / "frozen_checkpoint.cleanup",
        job / "frozen_checkpoint.cleanup.json",
    )


def _frozen_cleanup_ledger_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {
            key: value
            for key, value in payload.items()
            if key != "cleanup_ledger_sha256"
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _cleanup_local_frozen_checkpoint(
    run_dir: Path,
    case_id: str,
    job: Path,
    store: StateStore | None = None,
) -> None:
    """Crash-retryable cleanup of one exact hash-bound frozen checkpoint."""

    frozen_root, ready = _frozen_checkpoint_paths(job)
    tombstone, intent = _frozen_cleanup_paths(job)
    root_exists = frozen_root.exists() or frozen_root.is_symlink()
    ready_exists = ready.exists() or ready.is_symlink()
    intent_exists = intent.exists() or intent.is_symlink()
    tombstone_exists = tombstone.exists() or tombstone.is_symlink()
    if intent_exists:
        require_real_file(intent, f"{case_id} frozen cleanup intent")
    if tombstone_exists:
        require_real_directory(
            tombstone, f"{case_id} frozen cleanup tombstone"
        )

    if not intent_exists:
        if not root_exists and not ready_exists and not tombstone_exists:
            if store is not None:
                store.remove_artifacts(
                    case_id,
                    [
                        "frozen_checkpoint_archive",
                        "frozen_checkpoint_ready",
                    ],
                )
            return
        if not root_exists or not ready_exists or tombstone_exists:
            raise BatchError(
                f"{case_id}: incomplete local frozen checkpoint cleanup target"
            )
        _, _, frozen = _read_frozen_checkpoint(
            run_dir, case_id, job
        )
        cleanup = {
            "schema_version": FROZEN_CLEANUP_SCHEMA,
            "case_id": case_id,
            "run_id": _run_id(run_dir),
            "phase": "PREPARED",
            "ready_sha256": sha256_file(ready),
            "freeze_ledger_sha256": frozen["freeze_ledger_sha256"],
        }
        cleanup["cleanup_ledger_sha256"] = (
            _frozen_cleanup_ledger_sha256(cleanup)
        )
        atomic_json(intent, cleanup, durable=True)
    cleanup = read_json(
        require_real_file(intent, f"{case_id} frozen cleanup intent"),
        f"{case_id} frozen cleanup intent",
    )
    if (
        not isinstance(cleanup, dict)
        or cleanup.get("schema_version") != FROZEN_CLEANUP_SCHEMA
        or cleanup.get("case_id") != case_id
        or cleanup.get("run_id") != _run_id(run_dir)
        or cleanup.get("phase")
        not in {"PREPARED", "TOMBSTONED", "REMOVED"}
    ):
        raise BatchError(f"{case_id}: invalid frozen cleanup intent identity")
    for key in (
        "ready_sha256",
        "freeze_ledger_sha256",
        "cleanup_ledger_sha256",
    ):
        require_hash(cleanup.get(key), f"{case_id}.frozen_cleanup.{key}")
    if cleanup["cleanup_ledger_sha256"] != _frozen_cleanup_ledger_sha256(
        cleanup
    ):
        raise BatchError(f"{case_id}: frozen cleanup intent hash changed")
    if cleanup["phase"] == "REMOVED":
        if tombstone.exists() or tombstone.is_symlink():
            raise BatchError(
                f"{case_id}: removed frozen cleanup regained tombstone"
            )
        if (
            frozen_root.exists()
            or frozen_root.is_symlink()
            or ready.exists()
            or ready.is_symlink()
        ):
            raise BatchError(
                f"{case_id}: removed frozen cleanup regained input"
            )
        if store is not None:
            store.remove_artifacts(
                case_id,
                [
                    "frozen_checkpoint_archive",
                    "frozen_checkpoint_ready",
                ],
            )
        return

    if cleanup["phase"] == "PREPARED":
        inside_root = tombstone / frozen_root.name
        inside_ready = tombstone / ready.name
        canonical_root_exists = (
            frozen_root.exists() or frozen_root.is_symlink()
        )
        canonical_ready_exists = ready.exists() or ready.is_symlink()
        inside_root_exists = inside_root.exists() or inside_root.is_symlink()
        inside_ready_exists = (
            inside_ready.exists() or inside_ready.is_symlink()
        )
        if canonical_root_exists and inside_root_exists:
            raise BatchError(f"{case_id}: duplicate frozen cleanup root")
        if canonical_ready_exists and inside_ready_exists:
            raise BatchError(f"{case_id}: duplicate frozen cleanup marker")
        located_root = (
            frozen_root
            if canonical_root_exists
            else (inside_root if inside_root_exists else None)
        )
        located_ready = (
            ready
            if canonical_ready_exists
            else (inside_ready if inside_ready_exists else None)
        )
        if located_root is None or located_ready is None:
            raise BatchError(
                f"{case_id}: prepared frozen cleanup lost an input"
            )
        _, _, frozen = _read_frozen_checkpoint(
            run_dir,
            case_id,
            job,
            frozen_root_override=located_root,
            ready_override=located_ready,
        )
        if (
            sha256_file(located_ready) != cleanup["ready_sha256"]
            or frozen["freeze_ledger_sha256"]
            != cleanup["freeze_ledger_sha256"]
        ):
            raise BatchError(f"{case_id}: frozen cleanup target changed")

        if not (tombstone.exists() or tombstone.is_symlink()):
            tombstone.mkdir()
            _fsync_directory(job)
        if canonical_root_exists:
            os.rename(frozen_root, inside_root)
        if canonical_ready_exists:
            if sha256_file(ready) != cleanup["ready_sha256"]:
                raise BatchError(f"{case_id}: frozen ready marker changed")
            os.rename(ready, inside_ready)
        _fsync_directory(tombstone)
        _fsync_directory(job)
        if (
            frozen_root.exists()
            or frozen_root.is_symlink()
            or ready.exists()
            or ready.is_symlink()
            or not inside_root.exists()
            or inside_root.is_symlink()
            or not inside_ready.is_file()
            or inside_ready.is_symlink()
        ):
            raise BatchError(
                f"{case_id}: frozen cleanup tombstone is incomplete"
            )
        cleanup["phase"] = "TOMBSTONED"
        cleanup["cleanup_ledger_sha256"] = (
            _frozen_cleanup_ledger_sha256(cleanup)
        )
        atomic_json(intent, cleanup, durable=True)

    if (
        frozen_root.exists()
        or frozen_root.is_symlink()
        or ready.exists()
        or ready.is_symlink()
    ):
        raise BatchError(
            f"{case_id}: tombstoned cleanup regained a canonical input"
        )

    if tombstone.exists() or tombstone.is_symlink():
        require_real_directory(
            tombstone, f"{case_id} frozen cleanup tombstone"
        )
        shutil.rmtree(tombstone)
        _fsync_directory(job)
    if cleanup["phase"] != "REMOVED":
        cleanup["phase"] = "REMOVED"
        cleanup["cleanup_ledger_sha256"] = (
            _frozen_cleanup_ledger_sha256(cleanup)
        )
        atomic_json(intent, cleanup, durable=True)
    if store is not None:
        store.remove_artifacts(
            case_id,
            ["frozen_checkpoint_archive", "frozen_checkpoint_ready"],
        )


def _read_removed_frozen_cleanup(
    run_dir: Path, case_id: str, job: Path
) -> tuple[Path, dict[str, Any]]:
    """Validate durable proof that the exact frozen input was removed."""

    tombstone, intent = _frozen_cleanup_paths(job)
    if tombstone.exists() or tombstone.is_symlink():
        raise BatchError(f"{case_id}: frozen cleanup tombstone remains")
    frozen_root, ready = _frozen_checkpoint_paths(job)
    if (
        frozen_root.exists()
        or frozen_root.is_symlink()
        or ready.exists()
        or ready.is_symlink()
    ):
        raise BatchError(f"{case_id}: removed frozen cleanup regained input")
    payload = read_json(
        require_real_file(intent, f"{case_id} removed frozen cleanup proof"),
        f"{case_id} removed frozen cleanup proof",
    )
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != FROZEN_CLEANUP_SCHEMA
        or payload.get("case_id") != case_id
        or payload.get("run_id") != _run_id(run_dir)
        or payload.get("phase") != "REMOVED"
        or payload.get("cleanup_ledger_sha256")
        != _frozen_cleanup_ledger_sha256(payload)
    ):
        raise BatchError(f"{case_id}: invalid removed frozen cleanup proof")
    for key in (
        "ready_sha256",
        "freeze_ledger_sha256",
        "cleanup_ledger_sha256",
    ):
        require_hash(payload.get(key), f"{case_id}.frozen_cleanup.{key}")
    return intent, payload


def _finish_ready_frozen_checkpoint(
    run_dir: Path, store: StateStore, case_id: str
) -> str:
    """Promote one complete CALIBRATING freeze after idempotent leaf cleanup."""

    row = store.row(case_id)
    if row["state"] != "CALIBRATING":
        raise BatchError(
            f"{case_id}: frozen ready promotion requires CALIBRATING state"
        )
    job = require_real_directory(
        run_dir / "jobs" / case_id, f"{case_id} calibration job"
    )
    _, archive, frozen = _read_frozen_checkpoint(
        run_dir, case_id, job
    )
    plan_path = require_real_file(
        job / "calibration_plan.json", f"{case_id} calibration plan"
    )
    plan = read_json(plan_path, f"{case_id} calibration plan")
    binding_path = require_real_file(
        run_dir / "bindings" / f"{case_id}.json", f"{case_id} binding"
    )
    if (
        not isinstance(plan, dict)
        or plan.get("schema_version")
        != "mock_lef_batch100.calibration_plan.v1"
        or plan.get("case_id") != case_id
        or sha256_file(plan_path) != frozen["calibration_plan_sha256"]
        or sha256_file(binding_path) != frozen["binding_sha256"]
        or row.get("binding_sha256") != frozen["binding_sha256"]
    ):
        raise BatchError(f"{case_id}: frozen ready promotion identity changed")
    slot_name = row.get("slot")
    slot_match = re.fullmatch(r"slot([0-9])", str(slot_name or ""))
    if slot_match is None or plan.get("slot") != slot_name:
        raise BatchError(f"{case_id}: frozen ready calibration slot changed")
    slot = int(slot_match.group(1))
    injection_fingerprint = _operation_fingerprint(
        plan.get("injection_operations", [])
    )
    repair_fingerprint = _operation_fingerprint(
        plan.get("repair_operations", [])
    )
    ready = _frozen_checkpoint_paths(job)[1]
    store.register_artifact(
        case_id,
        "frozen_checkpoint_archive",
        str(archive.relative_to(run_dir)),
        frozen["archive_sha256"],
        frozen["archive_bytes"],
    )
    store.register_artifact(
        case_id,
        "frozen_checkpoint_ready",
        str(ready.relative_to(run_dir)),
        sha256_file(ready),
        ready.stat().st_size,
    )
    _remote_remove_case_leaf(slot, _run_id(run_dir), case_id)
    try:
        store.transition(
            case_id,
            "FROZEN",
            "hash-bound violating checkpoint cleanup/promote complete",
            slot=None,
            injection_sha256=injection_fingerprint,
            repair_sha256=repair_fingerprint,
        )
    except Exception:
        # SQLite can commit before an atomic sidecar refresh fails.  Preserve
        # the committed monotonic state and make its sidecar converge.
        if store.row(case_id)["state"] == "FROZEN":
            store.refresh_sidecar(case_id)
        else:
            raise
    return f"{case_id}: FROZEN slot{slot}"


def _decode(value: str) -> str:
    mapping = {"t": "\t", "n": "\n", "r": "\r", "\\": "\\"}
    output: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value) and value[index + 1] in mapping:
            output.append(mapping[value[index + 1]])
            index += 2
        else:
            output.append(value[index])
            index += 1
    return "".join(output)


def _tsv(path: Path) -> list[dict[str, str]]:
    require_real_file(path, path.name)
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.reader(stream, delimiter="\t", strict=True)
        try:
            header = [_decode(value) for value in next(reader)]
        except StopIteration as exc:
            raise BatchError(f"empty TSV: {path}") from exc
        if not header or len(set(header)) != len(header):
            raise BatchError(f"invalid TSV header: {path}")
        rows = []
        for line_number, raw in enumerate(reader, start=2):
            if len(raw) != len(header):
                raise BatchError(f"{path}:{line_number}: wrong column count")
            rows.append(dict(zip(header, (_decode(value) for value in raw), strict=True)))
    return rows


def _verify_probe(probe_dir: Path, baseline: Mapping[str, Any]) -> dict[str, Any]:
    metadata_rows = _tsv(probe_dir / "metadata.tsv")
    metadata = {row["key"]: row["value"] for row in metadata_rows}
    required = {
        "schema": "mock_lef_batch100.probe.v1",
        "purpose": "readonly_binding_probe_never_gold",
        "design_mutations": "0",
        "tool_version": "21.10-p004_1",
        "top": baseline["top"],
        "setup_view": baseline["setup_view"],
        "hold_view": baseline["hold_view"],
        "checkpoint": f"{GUEST_BASELINE}/base.enc",
    }
    for key, value in required.items():
        if metadata.get(key) != str(value):
            raise BatchError(f"probe metadata {key} mismatch")
    paths = _tsv(probe_dir / "paths.tsv")
    points = _tsv(probe_dir / "path_points.tsv")
    ladders = _tsv(probe_dir / "drive_ladders.tsv")
    reachability = _tsv(probe_dir / "cell_fanout_endpoints.tsv")
    if int(metadata.get("path_count", -1)) != len(paths):
        raise BatchError("probe path count mismatch")
    if not paths or not points or not ladders or not reachability:
        raise BatchError("probe evidence is incomplete")
    ranks = [int(row["stable_rank"]) for row in paths]
    if ranks != list(range(len(paths))):
        raise BatchError("probe stable ranks are not contiguous")
    return {
        "metadata": metadata,
        "paths": paths,
        "points": points,
        "ladders": ladders,
        "reachability": reachability,
        "tree_sha256": tree_sha256(probe_dir),
    }


def _ladder_map(rows: Sequence[Mapping[str, str]]) -> dict[str, list[tuple[float, str]]]:
    result: dict[str, list[tuple[float, str]]] = {}
    for row in rows:
        if row.get("equivalent") != "TRUE":
            continue
        try:
            drive = float(row["variant_drive"])
        except ValueError:
            continue
        result.setdefault(row["source_ref"], []).append((drive, row["variant_ref"]))
    for variants in result.values():
        variants.sort()
    return result


def _non_i0_repair_alternatives(
    cells: Sequence[Mapping[str, Any]],
    injection_instances: set[str],
    endpoints: set[str],
    count: int,
    *,
    limit: int = 6,
) -> list[list[dict[str, Any]]]:
    """Return bounded deterministic exact-count non-I0 repair alternatives.

    Every returned set covers all target paths represented by the probe, leaves
    at least one injected instance unrepaired, and contains at least one repair
    outside the injection set.  The alternatives differ by instance set, so a
    later batch-level fingerprint collision has a real fallback rather than a
    reordered spelling of the same repair.
    """

    if count <= 0 or limit <= 0:
        return []
    by_instance = {str(cell["instance"]): dict(cell) for cell in cells}
    if len(by_instance) != len(cells):
        raise BatchError("repair alternative input contains duplicate instances")
    usable = [
        cell
        for cell in by_instance.values()
        if cell.get("up_variants")
        and set(cell.get("up_estimates_by_endpoint_ns", {})) & endpoints
    ]
    injected = [
        cell for cell in usable if cell["instance"] in injection_instances
    ]
    outside = [
        cell for cell in usable if cell["instance"] not in injection_instances
    ]
    if not injected or not outside or len(usable) - 1 < count:
        return []

    def endpoint_coverage(cell: Mapping[str, Any]) -> set[str]:
        return set(cell.get("up_estimates_by_endpoint_ns", {})) & endpoints

    def strength(cell: Mapping[str, Any]) -> float:
        return sum(
            float(values[-1])
            for endpoint, values in cell.get(
                "up_estimates_by_endpoint_ns", {}
            ).items()
            if endpoint in endpoints and values
        )

    def delay(cell: Mapping[str, Any]) -> float:
        values = cell.get("delay_by_endpoint_ns", {}).values()
        return max((float(value) for value in values), default=0.0)

    static_key = lambda cell: (
        -len(endpoint_coverage(cell)),
        -strength(cell),
        -delay(cell),
        str(cell["instance"]),
    )
    outside.sort(key=static_key)
    # Preserving a weak injected cell usually costs the least closure margin.
    # Several choices are retained because a different omitted cell can create
    # a distinct exact-count repair fingerprint.
    preserved = sorted(
        injected,
        key=lambda cell: (
            strength(cell),
            delay(cell),
            str(cell["instance"]),
        ),
    )[:2]
    seed_pool = outside[: max(limit * 2, count + limit)]
    seed_groups: list[tuple[dict[str, Any], ...]] = [
        (cell,) for cell in seed_pool
    ]

    alternatives: dict[tuple[str, ...], tuple[float, list[dict[str, Any]]]] = {}
    for preserved_cell in preserved:
        preserved_instance = preserved_cell["instance"]
        for seeds in seed_groups:
            chosen = list(seeds)
            chosen_instances = {cell["instance"] for cell in chosen}
            covered = set().union(
                *(endpoint_coverage(cell) for cell in chosen)
            )
            while len(chosen) < count:
                remaining = [
                    cell
                    for cell in usable
                    if cell["instance"] != preserved_instance
                    and cell["instance"] not in chosen_instances
                ]
                if not remaining:
                    break
                selected = min(
                    remaining,
                    key=lambda cell: (
                        -len(endpoint_coverage(cell) - covered),
                        -len(endpoint_coverage(cell)),
                        -strength(cell),
                        -delay(cell),
                        str(cell["instance"]),
                    ),
                )
                chosen.append(selected)
                chosen_instances.add(selected["instance"])
                covered.update(endpoint_coverage(selected))
            if len(chosen) != count or not endpoints <= covered:
                continue
            if (
                not injection_instances - chosen_instances
                or not chosen_instances - injection_instances
            ):
                continue
            canonical = sorted(chosen, key=lambda cell: str(cell["instance"]))
            fingerprint = tuple(str(cell["instance"]) for cell in canonical)
            alternatives[fingerprint] = (
                -sum(strength(cell) for cell in canonical),
                canonical,
            )

    ordered = sorted(
        alternatives.values(),
        key=lambda item: (
            item[0],
            tuple(str(cell["instance"]) for cell in item[1]),
        ),
    )
    return [cells for _, cells in ordered[:limit]]


def _bind_relaxed_cases(
    probe: Mapping[str, Any], specs: Mapping[str, Any], run_dir: Path
) -> None:
    """Bind a relaxed card as one endpoint-local direct-inverse resize.

    This intentionally avoids the original combinatorial IA--ID planner.  The
    direct inverse puts the exact clean reference back, while strict Innovus
    calibration/replay still decides whether every concrete candidate is real.
    """

    points_by_rank: dict[int, list[Mapping[str, str]]] = {}
    for row in probe["points"]:
        points_by_rank.setdefault(int(row["stable_rank"]), []).append(row)
    ladders = _ladder_map(probe["ladders"])
    source_drive: dict[str, float] = {}
    for row in probe["ladders"]:
        try:
            source_drive.setdefault(row["source_ref"], float(row["source_drive"]))
        except ValueError:
            continue
    reachability = {
        row["instance"]: {
            endpoint for endpoint in row["endpoints"].split(",") if endpoint
        }
        for row in probe["reachability"]
    }
    by_group: dict[str, list[dict[str, Any]]] = {
        "exp": [], "pipeline": [], "top_tree": []
    }
    for path in probe["paths"]:
        try:
            cell_fraction = float(path["cell_delay_fraction"])
            net_fraction = float(path["net_delay_fraction"])
            rank = int(path["stable_rank"])
            baseline = float(path["slack_ns"])
        except (KeyError, ValueError):
            continue
        if cell_fraction < 0.70 or net_fraction > 0.30:
            continue
        endpoint = path["endpoint"]
        cells: list[dict[str, Any]] = []
        for point in points_by_rank.get(rank, []):
            instance = point.get("inst", "")
            if (
                point.get("delay_kind") != "cell"
                or not instance
                or point.get("sequential", "").lower() in {"1", "true", "yes"}
                or point.get("clock", "").lower() in {"1", "true", "yes"}
                or point.get("dont_touch", "").lower() in {"1", "true", "yes"}
                or reachability.get(instance) != {endpoint}
            ):
                continue
            reference = point["ref"]
            drive = source_drive.get(reference)
            if drive is None:
                continue
            lower = [pair for pair in ladders.get(reference, []) if pair[0] < drive]
            if not lower:
                continue
            # Nearest first is required by the calibrator's level-vector API.
            variants = list(reversed(lower))
            delay = float(point.get("delay_ns") or 0.0)
            estimates = [delay * (drive / variant_drive - 1.0) for variant_drive, _ in variants]
            if not estimates or max(estimates) <= 0.0:
                continue
            cells.append(
                {
                    "instance": instance,
                    "baseline_ref": reference,
                    "refs": [reference for _, reference in variants],
                    "estimates": estimates,
                    "point_index": int(point["point_index"]),
                }
            )
        if not cells:
            continue
        record = {
            "endpoint": endpoint,
            "rank": rank,
            "baseline": baseline,
            "cell_fraction": cell_fraction,
            "net_fraction": net_fraction,
            "cells": cells,
        }
        for group in path["hierarchy_groups"].split(","):
            if group in by_group:
                by_group[group].append(record)
    for rows in by_group.values():
        rows.sort(key=lambda row: (row["baseline"], row["endpoint"]))
    all_endpoints = sorted({row["endpoint"] for row in probe["paths"]})
    binding_root = run_dir / "bindings"
    binding_root.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    for spec in specs["cases"]:
        target_wns = float(spec["nominal_severity"]["target_wns_ns"])
        selected: dict[str, Any] | None = None
        selected_cell: dict[str, Any] | None = None
        for candidate in by_group[spec["hierarchy"]]:
            if candidate["endpoint"] in used:
                continue
            needed = candidate["baseline"] - target_wns
            # Prefer a real ladder that can plausibly reach the requested
            # negative slack; the full Innovus measurement remains decisive.
            viable = [
                cell for cell in candidate["cells"]
                if max(cell["estimates"]) >= needed * 0.55
            ]
            if not viable:
                continue
            selected = candidate
            selected_cell = min(
                viable,
                key=lambda cell: (
                    abs(max(cell["estimates"]) - needed),
                    -max(cell["estimates"]),
                    cell["instance"],
                ),
            )
            break
        if selected is None or selected_cell is None:
            raise BatchError(f"{spec['id']}: no endpoint-local relaxed binding")
        protected_count = int(spec["protected_endpoint_count"])
        protected = [
            endpoint for endpoint in all_endpoints
            if endpoint not in used and endpoint != selected["endpoint"]
        ][:protected_count]
        if len(protected) != protected_count:
            raise BatchError(f"{spec['id']}: insufficient relaxed protected endpoints")
        operation = {
            "instance": selected_cell["instance"],
            "baseline_ref": selected_cell["baseline_ref"],
            "new_ref": selected_cell["refs"][0],
            "candidate_refs_nearest_first": selected_cell["refs"],
            "candidate_estimated_slowdown_ns": selected_cell["estimates"],
            "candidate_estimated_slowdown_by_endpoint_ns": {
                selected["endpoint"]: selected_cell["estimates"]
            },
            "legal_down_refs_nearest_first": selected_cell["refs"],
            "legal_down_estimated_slowdown_ns": selected_cell["estimates"],
            "legal_down_estimated_slowdown_by_endpoint_ns": {
                selected["endpoint"]: selected_cell["estimates"]
            },
            "selected_legal_down_level": 0,
            "reachable_endpoints": [selected["endpoint"]],
            "point_index": selected_cell["point_index"],
        }
        repair = {
            "instance": selected_cell["instance"],
            "checkpoint_ref": selected_cell["refs"][0],
            "new_ref": selected_cell["baseline_ref"],
            "candidate_refs_nearest_first": [selected_cell["baseline_ref"]],
        }
        binding = {
            "schema_version": "mock_lef_batch100.binding.v1",
            "case_id": spec["id"],
            "probe_tree_sha256": probe["tree_sha256"],
            "target_endpoints": [selected["endpoint"]],
            "target_baseline_slacks_ns": {selected["endpoint"]: selected["baseline"]},
            "protected_endpoints": protected,
            "target_path_ranks": [selected["rank"]],
            "path_fractions": [{
                "endpoint": selected["endpoint"],
                "cell_delay_fraction": selected["cell_fraction"],
                "net_delay_fraction": selected["net_fraction"],
            }],
            "injection_operations": [operation],
            "repair_operations": [repair],
            "calibration_candidates": [{
                "candidate_index": 0,
                "injection_operations": [operation],
                "repair_operations": [repair],
            }],
            "planning_contract": {
                key: spec[key]
                for key in (
                    "shape", "strategy", "difficulty",
                    "expected_modification_count", "hierarchy",
                    "injection_profile", "guardrail_axis", "topology_tags",
                )
            },
        }
        atomic_json(binding_root / f"{spec['id']}.json", binding)
        used.add(selected["endpoint"])
        used.update(protected)


def _bind_cases(probe: Mapping[str, Any], specs: Mapping[str, Any], run_dir: Path) -> None:
    """Produce conservative, deterministic candidate bindings.

    Bindings are not validation.  Calibration must still reject and rebind any
    candidate that misses severity, topology, locality, or guardrails.
    """

    if specs.get("schema_version") in RELAXED_SCHEMAS:
        _bind_relaxed_cases(probe, specs, run_dir)
        return
    paths = probe["paths"]
    points_by_rank: dict[int, list[Mapping[str, str]]] = {}
    for row in probe["points"]:
        points_by_rank.setdefault(int(row["stable_rank"]), []).append(row)
    ladders = _ladder_map(probe["ladders"])
    source_drive: dict[str, float] = {}
    for row in probe["ladders"]:
        try:
            source_drive.setdefault(row["source_ref"], float(row["source_drive"]))
        except ValueError:
            continue
    eligible: dict[str, list[dict[str, Any]]] = {"exp": [], "pipeline": [], "top_tree": []}
    for row in paths:
        try:
            cell_fraction = float(row["cell_delay_fraction"])
            net_fraction = float(row["net_delay_fraction"])
        except ValueError:
            continue
        if cell_fraction < 0.70 or net_fraction > 0.30:
            continue
        rank = int(row["stable_rank"])
        cells: list[dict[str, Any]] = []
        seen: set[str] = set()
        for point in points_by_rank.get(rank, []):
            if point["delay_kind"] != "cell" or not point["inst"] or point["inst"] in seen:
                continue
            if point["sequential"].lower() in {"1", "true", "yes"}:
                continue
            if point["clock"].lower() in {"1", "true", "yes"}:
                continue
            if point["dont_touch"].lower() in {"1", "true", "yes"}:
                continue
            variants = ladders.get(point["ref"], [])
            source = source_drive.get(point["ref"])
            if source is None:
                continue
            lower = [item for item in variants if item[0] < source]
            upper = [item for item in variants if item[0] > source]
            if not lower or not upper:
                continue
            cells.append(
                {
                    "instance": point["inst"],
                    "baseline_ref": point["ref"],
                    "down_ref": lower[0][1],
                    "up_ref": upper[-1][1],
                    "down_variants": [
                        {"drive": drive, "ref": reference}
                        for drive, reference in reversed(lower)
                    ],
                    "source_drive": source,
                    "estimated_slowdown_ns": float(point["delay_ns"] or 0)
                    * (source / lower[0][0] - 1.0),
                    "up_variants": [
                        {"drive": drive, "ref": reference}
                        for drive, reference in upper
                    ],
                    "delay_ns": float(point["delay_ns"] or 0),
                    "point_index": int(point["point_index"]),
                }
            )
            seen.add(point["inst"])
        if len(cells) < 8:
            continue
        record = {
            "rank": rank,
            "endpoint": row["endpoint"],
            "beginpoint": row["beginpoint"],
            "baseline_slack_ns": float(row["slack_ns"]),
            "cell_delay_fraction": cell_fraction,
            "net_delay_fraction": net_fraction,
            "cells": cells,
            "cell_set": {cell["instance"] for cell in cells},
        }
        for group in row["hierarchy_groups"].split(","):
            if group in eligible:
                eligible[group].append(record)
    for group in eligible:
        eligible[group].sort(key=lambda item: (item["baseline_slack_ns"], item["endpoint"]))
    endpoint_baseline_slack: dict[str, float] = {}
    for row in paths:
        try:
            slack = float(row["slack_ns"])
        except ValueError:
            continue
        endpoint = row["endpoint"]
        endpoint_baseline_slack[endpoint] = min(
            endpoint_baseline_slack.get(endpoint, math.inf), slack
        )
    cell_endpoints: dict[str, set[str]] = {}
    for row in probe["reachability"]:
        endpoints = {
            endpoint for endpoint in row["endpoints"].split(",") if endpoint
        }
        if int(row["endpoint_count"]) != len(endpoints) or not endpoints:
            raise BatchError(
                f"probe reachability is malformed for {row['instance']}"
            )
        cell_endpoints[row["instance"]] = endpoints

    def merged_group_cells(
        group: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for target in group:
            endpoint = target["endpoint"]
            for source_cell in target["cells"]:
                instance = source_cell["instance"]
                cell = merged.setdefault(
                    instance,
                    {
                        **source_cell,
                        "delay_by_endpoint_ns": {},
                        "down_estimates_by_endpoint_ns": {},
                        "up_estimates_by_endpoint_ns": {},
                        "reachable_endpoints": sorted(
                            cell_endpoints.get(instance, set())
                        ),
                    },
                )
                delay = float(source_cell["delay_ns"])
                drive = float(source_cell["source_drive"])
                cell["delay_by_endpoint_ns"][endpoint] = delay
                cell["point_index"] = max(
                    int(cell.get("point_index", 0)),
                    int(source_cell.get("point_index", 0)),
                )
                cell["down_estimates_by_endpoint_ns"][endpoint] = [
                    delay * (drive / float(variant["drive"]) - 1.0)
                    for variant in source_cell["down_variants"]
                ]
                cell["up_estimates_by_endpoint_ns"][endpoint] = [
                    delay * (1.0 - drive / float(variant["drive"]))
                    for variant in source_cell["up_variants"]
                ]
        return list(merged.values())

    def operation_from_cell(
        cell: Mapping[str, Any], level: int | None = None
    ) -> dict[str, Any]:
        variants = cell["down_variants"]
        legal_references = [variant["ref"] for variant in variants]
        legal_by_endpoint = {
            endpoint: list(values)
            for endpoint, values in cell[
                "down_estimates_by_endpoint_ns"
            ].items()
        }
        legal_scalar = [
            max(
                (
                    float(values[index])
                    for values in legal_by_endpoint.values()
                    if index < len(values)
                ),
                default=0.0,
            )
            for index in range(len(legal_references))
        ]
        levels = range(len(variants)) if level is None else (level,)
        selected_levels = list(levels)
        references = [variants[index]["ref"] for index in selected_levels]
        by_endpoint = {
            endpoint: [values[index] for index in selected_levels]
            for endpoint, values in cell[
                "down_estimates_by_endpoint_ns"
            ].items()
        }
        scalar = [
            max(
                (
                    float(values[offset])
                    for values in by_endpoint.values()
                    if offset < len(values)
                ),
                default=0.0,
            )
            for offset in range(len(references))
        ]
        return {
            "instance": cell["instance"],
            "baseline_ref": cell["baseline_ref"],
            "new_ref": references[-1],
            "candidate_refs_nearest_first": references,
            "candidate_estimated_slowdown_ns": scalar,
            "candidate_estimated_slowdown_by_endpoint_ns": by_endpoint,
            # Calibration normally materializes one seed ref per operation.
            # Preserve the complete probe-qualified equivalent RVT down ladder
            # separately so an exact-set seed can be refined without rebinding.
            "legal_down_refs_nearest_first": legal_references,
            "legal_down_estimated_slowdown_ns": legal_scalar,
            "legal_down_estimated_slowdown_by_endpoint_ns": legal_by_endpoint,
            "selected_legal_down_level": selected_levels[-1],
            "reachable_endpoints": list(cell.get("reachable_endpoints", [])),
            "point_index": int(cell.get("point_index", 0)),
        }

    def predicted_score(
        operations: Sequence[Mapping[str, Any]],
        levels: Sequence[int],
        baseline_slacks: Mapping[str, float],
        target_wns: float,
    ) -> float:
        best = math.inf
        for scale_rank, scale in enumerate((3.0, 2.0, 4.0, 1.0)):
            predicted = []
            for endpoint, baseline_slack in baseline_slacks.items():
                slowdown = 0.0
                for operation, level in zip(operations, levels, strict=True):
                    values = operation[
                        "candidate_estimated_slowdown_by_endpoint_ns"
                    ].get(endpoint, ())
                    if level < len(values):
                        slowdown += float(values[level]) / scale
                predicted.append(float(baseline_slack) - slowdown)
            positive = [value for value in predicted if value >= 0.0]
            score = (
                100.0 * len(positive)
                + 10.0 * sum(positive)
                + abs(min(predicted) - target_wns)
                + 0.001 * scale_rank
            )
            best = min(best, score)
        return best

    used_targets: set[str] = set()
    used_protected: set[str] = set()
    binding_root = run_dir / "bindings"
    binding_root.mkdir(parents=True, exist_ok=True)
    binding_order = sorted(
        specs["cases"],
        key=lambda spec: (
            0
            if (
                spec["injection_profile"] == "I0"
                and spec["target_endpoint_count"] > 1
            )
            else (1 if spec["target_endpoint_count"] > 1 else 2),
            -int(spec["target_endpoint_count"]),
            -int(spec["nominal_severity"]["ps"]),
            -int(spec["expected_modification_count"]),
            -int(spec["protected_endpoint_count"]),
            spec["id"],
        ),
    )
    for spec in binding_order:
        pool = [
            item
            for item in eligible[spec["hierarchy"]]
            if item["endpoint"] not in used_targets and item["endpoint"] not in used_protected
        ]
        target_count = spec["target_endpoint_count"]
        count = spec["expected_modification_count"]

        def injection_cell_allowed(
            instance: str, group: Sequence[Mapping[str, Any]]
        ) -> bool:
            endpoints = {item["endpoint"] for item in group}
            reachable = cell_endpoints.get(instance, set())
            if not reachable or not reachable & endpoints:
                return False
            extras = reachable - endpoints
            if not extras:
                return True
            if spec["injection_profile"] == "I0" or len(extras) > 20:
                return False
            required = max(
                float(item["baseline_slack_ns"])
                - float(spec["nominal_severity"]["target_wns_ns"])
                for item in group
            )
            return all(
                endpoint_baseline_slack.get(endpoint, -math.inf)
                > required + 0.050
                for endpoint in extras
            )

        def group_cells(group: Sequence[Mapping[str, Any]]) -> tuple[set[str], set[str]]:
            all_cells = {
                cell["instance"] for item in group for cell in item["cells"]
            }
            local = {
                cell["instance"]
                for item in group
                for cell in item["cells"]
                if injection_cell_allowed(cell["instance"], group)
            }
            return all_cells, local

        def viable(group: Sequence[Mapping[str, Any]]) -> bool:
            all_cells, local = group_cells(group)
            if spec["injection_profile"] == "I0":
                return len(local) >= count
            return len(local) >= 2 and len(all_cells) >= count + 1

        def group_score(group: Sequence[Mapping[str, Any]]) -> tuple[Any, ...]:
            endpoints = {item["endpoint"] for item in group}
            baseline = {
                item["endpoint"]: float(item["baseline_slack_ns"]) for item in group
            }
            target_wns = float(spec["nominal_severity"]["target_wns_ns"])
            cells = [
                cell
                for cell in merged_group_cells(group)
                if injection_cell_allowed(cell["instance"], group)
            ]
            maximum_cells = count if spec["injection_profile"] == "I0" else 12
            missing = 0
            residual = 0.0
            optimistic_residual = 0.0
            for endpoint, slack in baseline.items():
                possible = sorted(
                    (
                        values[-1]
                        for cell in cells
                        for key, values in cell[
                            "down_estimates_by_endpoint_ns"
                        ].items()
                        if key == endpoint and values
                    ),
                    reverse=True,
                )[:maximum_cells]
                if not possible:
                    missing += 1
                optimistic_residual += max(
                    0.0, slack - target_wns - sum(possible)
                )
                residual += max(0.0, slack - target_wns - sum(possible) / 3.0)
            if spec["injection_profile"] == "I0":
                i0_distance = math.inf
                i0_negative_options = 0
                option_count = 0
                for subset in itertools.combinations(cells, count):
                    coverage = set().union(
                        *(
                            set(cell["reachable_endpoints"]) & endpoints
                            for cell in subset
                        )
                    )
                    if coverage != endpoints:
                        continue
                    operations = [
                        operation_from_cell(cell) for cell in subset
                    ]
                    option_count += math.prod(
                        len(operation["candidate_refs_nearest_first"])
                        for operation in operations
                    )
                    for levels in itertools.product(
                        *(
                            range(len(operation["candidate_refs_nearest_first"]))
                            for operation in operations
                        )
                    ):
                        # Use the optimistic cell-delay model only to choose
                        # a reachable group.  The later fixed-candidate
                        # shortlist explores conservative scale factors.
                        for scale in (1.0,):
                            predicted = []
                            for endpoint, slack in baseline.items():
                                slowdown = 0.0
                                for operation, level in zip(
                                    operations, levels, strict=True
                                ):
                                    values = operation[
                                        "candidate_estimated_slowdown_by_endpoint_ns"
                                    ].get(endpoint, ())
                                    if level < len(values):
                                        slowdown += float(values[level]) / scale
                                predicted.append(slack - slowdown)
                            if all(value < 0.0 for value in predicted):
                                i0_negative_options += 1
                                i0_distance = min(
                                    i0_distance,
                                    abs(min(predicted) - target_wns),
                                )
                return (
                    missing,
                    0 if i0_negative_options else 1,
                    optimistic_residual,
                    i0_distance,
                    -i0_negative_options,
                    -option_count,
                    max(baseline.values()),
                    tuple(sorted(endpoints)),
                )
            return (
                missing,
                residual,
                -min(len(cells), 12),
                max(baseline.values()),
                sum(baseline.values()),
                tuple(sorted(endpoints)),
            )

        group_options: list[list[dict[str, Any]]] = []
        if target_count == 1:
            group_options = [[candidate] for candidate in pool if viable([candidate])]
        else:
            seen_groups: set[tuple[str, ...]] = set()
            records_by_endpoint: dict[str, list[dict[str, Any]]] = {}
            for item in pool:
                records_by_endpoint.setdefault(item["endpoint"], []).append(item)
            # Prefer groups that exactly match an Innovus-observed fanout set.
            # This is essential for I0 multi-endpoint cards where one shared
            # direct-inverse cell must create exactly the planned endpoint set.
            for instance, reachable in sorted(cell_endpoints.items()):
                if len(reachable) != target_count or not reachable <= set(
                    records_by_endpoint
                ):
                    continue
                candidate: list[dict[str, Any]] = []
                for endpoint in sorted(reachable):
                    matching = [
                        item
                        for item in records_by_endpoint[endpoint]
                        if instance in item["cell_set"]
                    ]
                    if not matching:
                        candidate = []
                        break
                    candidate.append(
                        min(
                            matching,
                            key=lambda item: (
                                item["baseline_slack_ns"],
                                item["rank"],
                            ),
                        )
                    )
                key = tuple(sorted(reachable))
                if candidate and key not in seen_groups and viable(candidate):
                    seen_groups.add(key)
                    group_options.append(candidate)
            for anchor in pool:
                companions = [
                    item
                    for item in pool
                    if item is not anchor and anchor["cell_set"] & item["cell_set"]
                ]
                if len(companions) >= target_count - 1:
                    candidate = [anchor, *companions[: target_count - 1]]
                    key = tuple(sorted(item["endpoint"] for item in candidate))
                    if key not in seen_groups and viable(candidate):
                        seen_groups.add(key)
                        group_options.append(candidate)
        group_options.sort(key=group_score)

        def make_calibration_candidates(
            selected_group: Sequence[Mapping[str, Any]],
        ) -> list[dict[str, Any]]:
            endpoints = {item["endpoint"] for item in selected_group}
            baseline_slacks = {
                item["endpoint"]: float(item["baseline_slack_ns"])
                for item in selected_group
            }
            target_wns = float(spec["nominal_severity"]["target_wns_ns"])
            cells = merged_group_cells(selected_group)
            local = [
                cell
                for cell in cells
                if injection_cell_allowed(cell["instance"], selected_group)
            ]
            # Retain strong cells plus branch specialists, but keep subset
            # enumeration bounded and deterministic.
            ranked = sorted(
                local,
                key=lambda cell: (
                    -int(cell.get("point_index", 0)),
                    -sum(
                        values[-1]
                        for values in cell[
                            "down_estimates_by_endpoint_ns"
                        ].values()
                        if values
                    ),
                    -max(cell["delay_by_endpoint_ns"].values()),
                    cell["instance"],
                ),
            )
            injection_pool: list[dict[str, Any]] = []
            for endpoint in sorted(endpoints):
                specialists = sorted(
                    (
                        cell
                        for cell in local
                        if endpoint in cell["down_estimates_by_endpoint_ns"]
                    ),
                    key=lambda cell: (
                        -int(cell.get("point_index", 0)),
                        -cell["down_estimates_by_endpoint_ns"][endpoint][-1],
                        cell["instance"],
                    ),
                )[:3]
                for cell in specialists:
                    if cell not in injection_pool:
                        injection_pool.append(cell)
            for cell in ranked:
                if cell not in injection_pool:
                    injection_pool.append(cell)
                if len(injection_pool) >= 12:
                    break
            injection_pool = injection_pool[:12]
            if spec["injection_profile"] == "I0":
                sizes = (count,)
            else:
                minimum = 2 if len(injection_pool) >= 2 else 1
                maximum = min(12, len(injection_pool), len(cells) - 1)
                sizes = range(minimum, maximum + 1)
            subset_seeds: list[
                tuple[
                    float,
                    tuple[dict[str, Any], ...],
                ]
            ] = []
            for size in sizes:
                for subset in itertools.combinations(injection_pool, size):
                    injection_instances = {
                        cell["instance"] for cell in subset
                    }
                    coverage = set().union(
                        *(
                            set(cell["reachable_endpoints"]) & endpoints
                            for cell in subset
                        )
                    )
                    if coverage != endpoints:
                        continue
                    if spec["injection_profile"] != "I0":
                        repair_usable = [
                            cell
                            for cell in cells
                            if cell["up_variants"]
                            and set(cell["up_estimates_by_endpoint_ns"]) & endpoints
                        ]
                        repair_outside = {
                            cell["instance"]
                            for cell in repair_usable
                            if cell["instance"] not in injection_instances
                        }
                        repair_coverage = set().union(
                            *(
                                set(cell["up_estimates_by_endpoint_ns"]) & endpoints
                                for cell in repair_usable
                            )
                        )
                        if (
                            not repair_outside
                            or len(repair_usable) - 1 < count
                            or repair_coverage != endpoints
                        ):
                            continue
                    operations = [
                        operation_from_cell(cell) for cell in subset
                    ]
                    strongest = tuple(
                        len(operation["candidate_refs_nearest_first"]) - 1
                        for operation in operations
                    )
                    subset_seeds.append(
                        (
                            predicted_score(
                                operations,
                                strongest,
                                baseline_slacks,
                                target_wns,
                            ),
                            subset,
                        )
                    )
            subset_seeds.sort(
                key=lambda row: (
                    row[0],
                    len(row[1]),
                    tuple(cell["instance"] for cell in row[1]),
                )
            )
            subset_rows: list[
                tuple[
                    float,
                    tuple[dict[str, Any], ...],
                    list[dict[str, Any]],
                    tuple[int, ...],
                ]
            ] = []
            # Expanding every drive-level Cartesian product dominated bind
            # time.  Rank subsets cheaply at their strongest legal levels,
            # then expand only the most promising deterministic shortlist.
            viable_subset_count = 0
            for _, subset in subset_seeds:
                if spec["injection_profile"] == "I0":
                    repair_alternatives = [list(subset)]
                else:
                    repair_alternatives = _non_i0_repair_alternatives(
                        cells,
                        {cell["instance"] for cell in subset},
                        endpoints,
                        count,
                    )
                if not repair_alternatives:
                    continue
                viable_subset_count += 1
                operations = [operation_from_cell(cell) for cell in subset]
                vectors = _candidate_level_vectors(
                    operations,
                    max(baseline_slacks.values()) - target_wns,
                    limit=(
                        8 if spec["injection_profile"] == "I0" else 3
                    ),
                    baseline_slacks_ns=baseline_slacks,
                    target_wns_ns=target_wns,
                )
                for levels in vectors:
                    for repairs in repair_alternatives:
                        subset_rows.append(
                            (
                                predicted_score(
                                    operations,
                                    levels,
                                    baseline_slacks,
                                    target_wns,
                                ),
                                subset,
                                repairs,
                                levels,
                            )
                        )
                if viable_subset_count >= 64:
                    break
            subset_rows.sort(
                key=lambda row: (
                    row[0],
                    len(row[1]),
                    tuple(cell["instance"] for cell in row[1]),
                    row[3],
                )
            )
            if subset_rows:
                strongest = max(
                    subset_rows,
                    key=lambda row: sum(
                        operation_from_cell(cell)[
                            "candidate_estimated_slowdown_ns"
                        ][level]
                        for cell, level in zip(
                            row[1], row[3], strict=True
                        )
                    ),
                )
                subset_rows = [
                    subset_rows[0],
                    *(
                        [strongest]
                        if strongest is not subset_rows[0]
                        else []
                    ),
                    *(
                        row
                        for row in subset_rows[1:]
                        if row is not strongest
                    ),
                ]
            output: list[dict[str, Any]] = []
            seen_candidates: set[
                tuple[
                    tuple[tuple[str, str], ...],
                    tuple[tuple[str, str], ...],
                ]
            ] = set()
            for _, injection_cells, repair_cells, levels in subset_rows:
                injection_operations = [
                    operation_from_cell(cell, level)
                    for cell, level in zip(injection_cells, levels, strict=True)
                ]
                predicted_slacks = []
                for endpoint, baseline_slack in baseline_slacks.items():
                    slowdown = sum(
                        operation[
                            "candidate_estimated_slowdown_by_endpoint_ns"
                        ].get(endpoint, [0.0])[0]
                        for operation in injection_operations
                    )
                    predicted_slacks.append(baseline_slack - slowdown)
                tolerance = (
                    float(spec["nominal_severity"]["tolerance_ps"]) / 1000.0
                )
                # Estimates only rank candidates; Innovus remains the gate.
                # Still reject a binding that cannot even reach every target
                # or the severity window under the optimistic scale=1 model.
                if (
                    spec["injection_profile"] != "I0"
                    and (
                        any(slack >= 0.0 for slack in predicted_slacks)
                        or min(predicted_slacks) > target_wns + tolerance
                    )
                ):
                    continue
                injection_fingerprint = tuple(
                    sorted(
                        (
                            operation["instance"],
                            operation["candidate_refs_nearest_first"][0],
                        )
                        for operation in injection_operations
                    )
                )
                repair_fingerprint = tuple(
                    sorted(
                        (
                            cell["instance"],
                            cell["up_variants"][-1]["ref"]
                            if spec["injection_profile"] != "I0"
                            else cell["baseline_ref"],
                        )
                        for cell in repair_cells
                    )
                )
                candidate_fingerprint = (
                    injection_fingerprint,
                    repair_fingerprint,
                )
                if candidate_fingerprint in seen_candidates:
                    continue
                seen_candidates.add(candidate_fingerprint)
                injected_refs = {
                    operation["instance"]: operation[
                        "candidate_refs_nearest_first"
                    ][0]
                    for operation in injection_operations
                }
                repair_operations = []
                for cell in repair_cells:
                    if spec["injection_profile"] == "I0":
                        checkpoint_ref = injected_refs[cell["instance"]]
                        new_ref = cell["baseline_ref"]
                        candidates = [cell["baseline_ref"]]
                    else:
                        checkpoint_ref = injected_refs.get(
                            cell["instance"], cell["baseline_ref"]
                        )
                        new_ref = cell["up_variants"][-1]["ref"]
                        candidates = [
                            variant["ref"] for variant in cell["up_variants"]
                        ]
                    repair_operations.append(
                        {
                            "instance": cell["instance"],
                            "checkpoint_ref": checkpoint_ref,
                            "new_ref": new_ref,
                            "candidate_refs_nearest_first": candidates,
                        }
                    )
                output.append(
                    {
                        "candidate_index": len(output),
                        "injection_operations": injection_operations,
                        "repair_operations": repair_operations,
                    }
                )
                if len(output) >= 12:
                    break
            return output

        selected: list[dict[str, Any]] = []
        calibration_candidates: list[dict[str, Any]] = []
        protected: list[str] = []
        for group in group_options[:1024]:
            group_endpoints = {item["endpoint"] for item in group}
            protected_pool = [
                item["endpoint"]
                for item in pool
                if item["endpoint"] not in group_endpoints
                and item["endpoint"] not in used_targets
                and item["endpoint"] not in used_protected
            ]
            if len(protected_pool) < spec["protected_endpoint_count"]:
                continue
            candidates = make_calibration_candidates(group)
            if candidates:
                selected = list(group)
                calibration_candidates = candidates
                protected = protected_pool[: spec["protected_endpoint_count"]]
                break
        if len(selected) != target_count or not calibration_candidates:
            raise BatchError(
                f"{spec['id']}: no topology/severity-compatible target binding"
            )
        selected_endpoints = {target["endpoint"] for target in selected}
        protected_count = spec["protected_endpoint_count"]
        if len(protected) != protected_count:
            raise BatchError(f"{spec['id']}: insufficient protected endpoints")
        binding = {
            "schema_version": "mock_lef_batch100.binding.v1",
            "case_id": spec["id"],
            "probe_tree_sha256": probe["tree_sha256"],
            "target_endpoints": [item["endpoint"] for item in selected],
            "target_baseline_slacks_ns": {
                item["endpoint"]: item["baseline_slack_ns"] for item in selected
            },
            "protected_endpoints": protected,
            "target_path_ranks": [item["rank"] for item in selected],
            "path_fractions": [
                {
                    "endpoint": item["endpoint"],
                    "cell_delay_fraction": item["cell_delay_fraction"],
                    "net_delay_fraction": item["net_delay_fraction"],
                }
                for item in selected
            ],
            "injection_operations": calibration_candidates[0][
                "injection_operations"
            ],
            "repair_operations": calibration_candidates[0]["repair_operations"],
            "calibration_candidates": calibration_candidates,
            "planning_contract": {
                key: spec[key]
                for key in (
                    "shape",
                    "strategy",
                    "difficulty",
                    "expected_modification_count",
                    "hierarchy",
                    "injection_profile",
                    "guardrail_axis",
                    "topology_tags",
                )
            },
        }
        path = binding_root / f"{spec['id']}.json"
        atomic_json(path, binding)
        used_targets.update(binding["target_endpoints"])
        used_protected.update(protected)


def _verified_frozen_probe(
    run_dir: Path, baseline: Mapping[str, Any]
) -> dict[str, Any]:
    """Reload the immutable probe while preserving its pre-manifest hash."""

    probe_dir = require_real_directory(run_dir / "probe", "frozen probe")
    probe_manifest = read_json(
        probe_dir / "probe_manifest.json", "probe manifest"
    )
    if (
        probe_manifest.get("schema_version")
        != "mock_lef_batch100.probe_manifest.v1"
    ):
        raise BatchError("unsupported frozen probe manifest schema")
    frozen_hash = require_hash(
        probe_manifest.get("tree_sha256_before_manifest"),
        "probe_manifest.tree_sha256_before_manifest",
    )
    actual_hash = tree_sha256(
        probe_dir, exclude=("probe_manifest.json",)
    )
    if actual_hash != frozen_hash:
        raise BatchError("frozen probe content hash changed")
    verified = _verify_probe(probe_dir, baseline)
    expected = {
        "baseline_entry_sha256": baseline["entry_sha256"],
        "probe_tcl_sha256": sha256_file(TOOLS["probe"]),
        "path_count": len(verified["paths"]),
        "point_count": len(verified["points"]),
        "drive_ladder_rows": len(verified["ladders"]),
        "reachability_rows": len(verified["reachability"]),
    }
    for key, value in expected.items():
        if probe_manifest.get(key) != value:
            raise BatchError(f"frozen probe manifest {key} mismatch")
    # The manifest was added only after this hash was bound into every case.
    verified["tree_sha256"] = frozen_hash
    return verified


def _clear_remote_case_leaf_if_present(
    slot: int, run_id: str, case_id: str
) -> None:
    """Remove only one retry case leaf; absence is a successful no-op."""

    _remote_remove_case_leaf(slot, run_id, case_id)


def _clear_remote_retry_leaves(
    slots: Sequence[int], run_id: str, case_id: str
) -> None:
    if not slots:
        raise BatchError(f"{case_id}: no healthy slot for retry cleanup")
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=len(slots)) as executor:
        futures = {
            executor.submit(
                _clear_remote_case_leaf_if_present, slot, run_id, case_id
            ): slot
            for slot in slots
        }
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                failures.append(f"slot{futures[future]}: {exc}")
    if failures:
        raise BatchError(
            f"{case_id}: exact retry cleanup incomplete: "
            + "; ".join(sorted(failures))
        )


def _binding_endpoint_set(binding: Mapping[str, Any], label: str) -> set[str]:
    result: set[str] = set()
    for key in ("target_endpoints", "protected_endpoints"):
        values = binding.get(key)
        if not isinstance(values, list) or not all(
            isinstance(value, str) and value for value in values
        ):
            raise BatchError(f"{label}.{key} is malformed")
        if len(values) != len(set(values)):
            raise BatchError(f"{label}.{key} contains duplicates")
        result.update(values)
    return result


def _release_rejected_case_fingerprint_claims(
    run_dir: Path, case_id: str
) -> dict[str, list[str]]:
    """Release only provisional claims owned by a non-frozen rejected case."""

    removed: dict[str, list[str]] = {"injection": [], "repair": []}
    claims_path = run_dir / "fingerprint_claims.json"
    if not claims_path.exists():
        return removed
    lock_path = run_dir / ".fingerprint_claims.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        claims = read_json(claims_path, "fingerprint claims")
        if not isinstance(claims, dict):
            raise BatchError("fingerprint claims must be an object")
        for kind in ("injection", "repair"):
            owners = claims.get(kind)
            if not isinstance(owners, dict):
                raise BatchError(f"fingerprint claims {kind} is malformed")
            for fingerprint, owner in list(owners.items()):
                require_hash(fingerprint, f"{kind} fingerprint claim")
                if owner == case_id:
                    removed[kind].append(fingerprint)
                    del owners[fingerprint]
        if any(removed.values()):
            atomic_json(claims_path, claims, durable=True)
    for values in removed.values():
        values.sort()
    return removed


def _rebind_probe_eligible_case(
    run_dir: Path,
    store: StateStore,
    manifest: Mapping[str, Any],
    specs: Mapping[str, Any],
    case_id: str,
    available_slots: Sequence[int],
) -> None:
    """Rebind one rejected card without changing any other case artifact."""

    row = store.row(case_id)
    if row["state"] != "PROBE_ELIGIBLE":
        raise BatchError(
            f"{case_id}: rebind requires PROBE_ELIGIBLE, got {row['state']}"
        )
    by_id = {spec["id"]: spec for spec in specs["cases"]}
    if case_id not in by_id:
        raise BatchError(f"{case_id}: missing case spec during rebind")
    probe = _verified_frozen_probe(run_dir, manifest["baseline"])
    probe_hash = probe["tree_sha256"]
    binding_root = require_real_directory(
        run_dir / "bindings", "binding root"
    )
    binding_path = require_real_file(
        binding_root / f"{case_id}.json", f"{case_id} current binding"
    )
    old_binding_hash = sha256_file(binding_path)
    if row.get("binding_sha256") != old_binding_hash:
        raise BatchError(f"{case_id}: current binding hash changed before rebind")
    old_binding = read_json(binding_path, f"{case_id} current binding")
    if (
        old_binding.get("case_id") != case_id
        or old_binding.get("probe_tree_sha256") != probe_hash
    ):
        raise BatchError(f"{case_id}: current binding is not from frozen probe")

    excluded: set[str] = set()
    for other_spec in specs["cases"]:
        other_id = other_spec["id"]
        if other_id == case_id:
            continue
        other_row = store.row(other_id)
        other_path = require_real_file(
            binding_root / f"{other_id}.json", f"{other_id} binding"
        )
        if (
            other_row.get("binding_sha256") != sha256_file(other_path)
        ):
            raise BatchError(f"{other_id}: binding hash changed before rebind")
        other_binding = read_json(other_path, f"{other_id} binding")
        if (
            other_binding.get("case_id") != other_id
            or other_binding.get("probe_tree_sha256") != probe_hash
        ):
            raise BatchError(f"{other_id}: binding is not from frozen probe")
        excluded.update(
            _binding_endpoint_set(other_binding, f"{other_id} binding")
        )

    # Never cycle back to a target set that this card already exhausted.
    history_bindings = [binding_path]
    audit_case_root = run_dir / "rebind_audit" / case_id
    if audit_case_root.exists():
        require_real_directory(audit_case_root, f"{case_id} rebind audit")
        history_bindings.extend(
            sorted(audit_case_root.glob("attempt_*/binding.json"))
        )
    for history_path in history_bindings:
        require_real_file(history_path, f"{case_id} binding history")
        history = read_json(history_path, f"{case_id} binding history")
        excluded.update(
            _binding_endpoint_set(history, f"{case_id} binding history")
        )

    # Keep all path rows so reachability guardrails retain baseline slack for
    # excluded fanout endpoints.  Empty hierarchy membership makes only those
    # endpoints unavailable to the unchanged binder.
    rebound_paths = []
    for path_row in probe["paths"]:
        rebound = dict(path_row)
        if rebound["endpoint"] in excluded:
            rebound["hierarchy_groups"] = ""
        rebound_paths.append(rebound)
    rebound_probe = {**probe, "paths": rebound_paths}

    with tempfile.TemporaryDirectory(
        prefix=f".rebind-{case_id}-", dir=run_dir
    ) as temporary:
        temporary_root = Path(temporary)
        _bind_cases(
            rebound_probe, {"cases": [by_id[case_id]]}, temporary_root
        )
        new_binding_path = require_real_file(
            temporary_root / "bindings" / f"{case_id}.json",
            f"{case_id} rebound binding",
        )
        new_binding = read_json(
            new_binding_path, f"{case_id} rebound binding"
        )
        new_endpoints = _binding_endpoint_set(
            new_binding, f"{case_id} rebound binding"
        )
        if (
            new_binding.get("case_id") != case_id
            or new_binding.get("probe_tree_sha256") != probe_hash
            or new_endpoints & excluded
        ):
            raise BatchError(f"{case_id}: rebound binding violated isolation")

        _clear_remote_retry_leaves(
            available_slots, _run_id(run_dir), case_id
        )
        released_claims = _release_rejected_case_fingerprint_claims(
            run_dir, case_id
        )
        attempt = int(row["attempt"])
        audit_root = (
            run_dir
            / "rebind_audit"
            / case_id
            / f"attempt_{attempt:03d}"
        )
        if audit_root.exists():
            raise BatchError(f"{case_id}: rebind audit leaf already exists")
        audit_root.mkdir(parents=True)
        shutil.copy2(binding_path, audit_root / "binding.json")
        shutil.copy2(new_binding_path, audit_root / "new_binding.json")
        job = run_dir / "jobs" / case_id
        archived_job = job.exists()
        if archived_job:
            require_real_directory(job, f"{case_id} rejected job")
            shutil.move(str(job), str(audit_root / "job"))
        atomic_json(
            audit_root / "rebind_manifest.json",
            {
                "schema_version": "mock_lef_batch100.rebind_audit.v1",
                "case_id": case_id,
                "attempt": attempt,
                "probe_tree_sha256": probe_hash,
                "old_binding_sha256": old_binding_hash,
                "new_binding_sha256": sha256_file(new_binding_path),
                "excluded_endpoints": sorted(excluded),
                "archived_rejected_job": archived_job,
                "released_provisional_fingerprint_claims": released_claims,
                "remote_slots_cleaned": [
                    f"slot{slot}" for slot in sorted(available_slots)
                ],
            },
            durable=True,
        )
        atomic_json(binding_path, new_binding, durable=True)

    store.transition(
        case_id,
        "BOUND",
        "deterministic same-probe rebind after candidate rejection",
        slot=None,
        binding_sha256=sha256_file(binding_path),
        injection_sha256=None,
        repair_sha256=None,
    )


def cmd_catalog_check(_: argparse.Namespace) -> None:
    specs = read_json(SPECS_PATH, "case specs")
    _validate_specs(specs)
    print("catalog-check PASS: 100 cases and machine semantics match")


def cmd_init(args: argparse.Namespace) -> None:
    _validate_specs(read_json(SPECS_PATH, "case specs"))
    specs = read_json(SPECS_PATH)
    binding = _baseline_binding(args.baseline)
    StateStore(args.run_dir).initialize(
        [case["id"] for case in specs["cases"]], _input_hashes(), binding
    )
    atomic_json(
        args.run_dir / "run_manifest.json",
        {
            "schema_version": "mock_lef_batch100.run_manifest.v1",
            "run_id": _run_id(args.run_dir),
            "input_hashes": _input_hashes(),
            "baseline": binding,
            "remote": {
                "host": REMOTE_HOST,
                "slots": [f"slot{index}" for index in range(10)],
                "guest_run_root": GUEST_RUN_ROOT,
            },
        },
        durable=True,
    )
    print(f"init PASS: 100 PLANNED cases in {args.run_dir}")


def _verified_store(run_dir: Path) -> tuple[StateStore, dict[str, Any]]:
    store = StateStore(run_dir)
    if not store.path.is_file():
        raise BatchError(f"run is not initialized: {run_dir}")
    store.verify_inputs(_input_hashes())
    manifest = read_json(run_dir / "run_manifest.json", "run manifest")
    if manifest.get("input_hashes") != _input_hashes():
        raise BatchError("run manifest input hashes changed")
    return store, manifest


def cmd_probe(args: argparse.Namespace) -> None:
    store, manifest = _verified_store(args.run_dir)
    if any(row["state"] != "PLANNED" for row in store.rows()):
        raise BatchError("probe requires all cases to be PLANNED")
    run_id = _run_id(args.run_dir)
    remote_root = f"{GUEST_RUN_ROOT}/{run_id}"
    remote_probe = f"{remote_root}/probe"
    remote_script = f"{remote_root}/input/probe.tcl"
    baseline = manifest["baseline"]
    remote_hash = _ssh(
        0, f"sha256sum {GUEST_BASELINE}/base.enc", timeout=30
    )
    if remote_hash.returncode or re.search(
        rf"(?m)^{re.escape(baseline['entry_sha256'])}\s+",
        remote_hash.stdout or "",
    ) is None:
        raise BatchError("slot0 baseline entry hash does not match initialized input")
    mkdir = _ssh(
        0,
        f"test ! -e {remote_root} && mkdir -p {remote_root}/input",
        timeout=30,
    )
    if mkdir.returncode:
        raise BatchError(f"remote probe root is not fresh: {mkdir.stdout[-1000:]}")
    _scp_to(0, TOOLS["probe"], remote_script)
    command = (
        f"bash -lc 'cd {remote_root}/input && "
        f"B100_BASELINE_DIR={GUEST_BASELINE} "
        f"B100_PROBE_DIR={remote_probe} B100_MAX_PATHS=20000 "
        f"timeout 14400s innovus -no_gui -files probe.tcl -log probe.log'"
    )
    result = _ssh(0, command, timeout=14500)
    log_path = args.run_dir / "probe" / "host_ssh.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(result.stdout or "", encoding="utf-8")
    if result.returncode or "B100_PROBE_COMPLETE" not in (result.stdout or ""):
        raise BatchError(f"Innovus probe failed; see {log_path}")
    if re.search(r"(?m)^\*\*ERROR:", result.stdout or ""):
        raise BatchError(f"Innovus probe logged an error; see {log_path}")
    summaries = re.findall(
        r"(?m)^\*\*\* Message Summary: \d+ warning\(s\), (\d+) error\(s\)\s*$",
        result.stdout or "",
    )
    if not summaries or summaries[-1] != "0":
        raise BatchError(f"Innovus probe final error count is not zero; see {log_path}")
    local_parent = args.run_dir / "probe_fetch"
    if local_parent.exists():
        raise BatchError(f"local probe fetch path already exists: {local_parent}")
    local_parent.mkdir(parents=True)
    _scp_from(0, remote_probe, local_parent, recursive=True)
    fetched = local_parent / "probe"
    verified = _verify_probe(fetched, baseline)
    final_probe = args.run_dir / "probe"
    host_log = log_path.read_bytes()
    shutil.move(str(fetched), str(args.run_dir / ".probe.promote"))
    shutil.rmtree(local_parent)
    shutil.rmtree(final_probe)
    shutil.move(str(args.run_dir / ".probe.promote"), str(final_probe))
    (final_probe / "host_ssh.log").write_bytes(host_log)
    verified = _verify_probe(final_probe, baseline)
    atomic_json(
        final_probe / "probe_manifest.json",
        {
            "schema_version": "mock_lef_batch100.probe_manifest.v1",
            "tree_sha256_before_manifest": verified["tree_sha256"],
            "path_count": len(verified["paths"]),
            "point_count": len(verified["points"]),
            "drive_ladder_rows": len(verified["ladders"]),
            "reachability_rows": len(verified["reachability"]),
            "baseline_entry_sha256": baseline["entry_sha256"],
            "probe_tcl_sha256": _input_hashes()["tool_probe"],
        },
    )
    specs = read_json(SPECS_PATH)
    _bind_cases(verified, specs, args.run_dir)
    for case in specs["cases"]:
        store.transition(case["id"], "PROBE_ELIGIBLE", "fresh exact-baseline probe")
        binding_path = args.run_dir / "bindings" / f"{case['id']}.json"
        store.transition(
            case["id"],
            "BOUND",
            "deterministic machine binding",
            binding_sha256=sha256_file(binding_path),
        )
    print(f"probe PASS: {len(verified['paths'])} paths; 100 cases BOUND")


def _selection(specs: Mapping[str, Any], value: str) -> list[str]:
    canary = list(specs["canary_case_ids"])
    if value == "canary":
        return canary
    return [case["id"] for case in specs["cases"] if case["id"] not in set(canary)]


def _remote_host_available_kib() -> int:
    command = ["df", "-Pk", "--", REMOTE_STORAGE_ROOT]
    if not DIRECT_GUEST_SSH:
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            REMOTE_HOST,
            " ".join(command),
        ]
    result = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    if result.returncode:
        raise BatchError(
            "remote host storage check failed: "
            + (result.stdout or "").strip()[-1000:]
        )
    rows: list[list[str]] = []
    for line in (result.stdout or "").splitlines():
        fields = line.split()
        if (
            len(fields) >= 6
            and fields[1].isdigit()
            and fields[2].isdigit()
            and fields[3].isdigit()
            and fields[4].endswith("%")
        ):
            rows.append(fields)
    if len(rows) != 1:
        raise BatchError("remote host storage check returned malformed df evidence")
    total_kib = int(rows[0][1])
    available_kib = int(rows[0][3])
    if total_kib <= 0 or not 0 <= available_kib <= total_kib:
        raise BatchError("remote host storage check returned invalid capacity values")
    return available_kib


def _require_remote_host_capacity() -> int:
    available_kib = _remote_host_available_kib()
    if available_kib < REMOTE_HOST_MIN_AVAILABLE_KIB:
        raise BatchError(
            f"remote host {REMOTE_STORAGE_ROOT} has "
            f"{available_kib / (1024 * 1024):.1f} GiB available; "
            f"new dispatch requires at least "
            f"{REMOTE_HOST_MIN_AVAILABLE_KIB / (1024 * 1024):.0f} GiB"
        )
    return available_kib


def _verify_execution_slots(baseline: Mapping[str, Any]) -> list[int]:
    _require_remote_host_capacity()

    def check(slot: int) -> tuple[int, str | None]:
        result = _ssh(
            slot,
            f"sha256sum {GUEST_BASELINE}/base.enc && df -P /work | tail -1",
            timeout=30,
        )
        if result.returncode:
            return slot, "guest/baseline check failed"
        lines = (result.stdout or "").splitlines()
        if re.search(
            rf"(?m)^{re.escape(baseline['entry_sha256'])}\s+",
            result.stdout or "",
        ) is None:
            return slot, "baseline hash mismatch"
        if len(lines) < 2:
            return slot, "missing /work capacity evidence"
        fields = lines[-1].split()
        if len(fields) < 5 or not fields[-2].endswith("%"):
            return slot, "malformed /work capacity evidence"
        if int(fields[-2][:-1]) >= 75:
            return slot, "/work reached the 75% dispatch threshold"
        return slot, None

    available: list[int] = []
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(check, slot): slot for slot in range(10)}
        for future in as_completed(futures):
            slot = futures[future]
            try:
                _, error = future.result()
            except Exception as exc:
                failures.append(
                    f"slot{slot}: preflight exception "
                    f"{type(exc).__name__}: {exc}"
                )
                continue
            if error:
                failures.append(f"slot{slot}: {error}")
            else:
                available.append(slot)
    if failures:
        if not available:
            raise BatchError(
                "no execution slots are healthy: "
                + "; ".join(sorted(failures))
            )
        print("slot preflight degraded: " + "; ".join(sorted(failures)), file=sys.stderr)
    return sorted(available)


def _tcl_atom(value: str) -> str:
    if not value or any(character in value for character in "{}\\\r\n\x00"):
        raise BatchError(f"value cannot be represented as a data-only Tcl atom: {value!r}")
    return "{" + value + "}"


def _write_calibration_config(
    path: Path, spec: Mapping[str, Any], binding: Mapping[str, Any]
) -> None:
    lines = [
        f"set B100_CASE_ID {_tcl_atom(spec['id'])}",
        "set B100_TARGET_ENDPOINTS {"
        + " ".join(_tcl_atom(value) for value in binding["target_endpoints"])
        + "}",
        "set B100_PROTECTED_ENDPOINTS {"
        + " ".join(_tcl_atom(value) for value in binding["protected_endpoints"])
        + "}",
        f"set B100_NOMINAL_NS {float(spec['nominal_severity']['target_wns_ns']):.12g}",
        f"set B100_TOLERANCE_NS {float(spec['nominal_severity']['tolerance_ps']) / 1000.0:.12g}",
        "set B100_INJECTION_CANDIDATES {",
    ]
    for operation in binding["injection_operations"]:
        references = operation.get("candidate_refs_nearest_first")
        if not isinstance(references, list) or not references:
            raise BatchError(f"{spec['id']}: injection operation lacks candidate refs")
        lines.append(
            "  {"
            + " ".join(
                (
                    _tcl_atom(operation["instance"]),
                    _tcl_atom(operation["baseline_ref"]),
                    "{" + " ".join(_tcl_atom(value) for value in references) + "}",
                )
            )
            + "}"
        )
    lines.append("}")
    lines.append("set B100_REPAIR_OPERATIONS {")
    for operation in binding["repair_operations"]:
        lines.append(
            "  {"
            + " ".join(
                _tcl_atom(value)
                for value in (
                    operation["instance"],
                    operation["checkpoint_ref"],
                    operation["new_ref"],
                )
            )
            + "}"
        )
    lines.extend(("}", ""))
    atomic_write(path, "\n".join(lines).encode("utf-8"))


def _read_frozen_plan(path: Path, spec: Mapping[str, Any]) -> dict[str, Any]:
    rows = _tsv(path)
    expected_header = {"kind", "instance", "old_ref", "new_ref"}
    if rows and set(rows[0]) != expected_header:
        raise BatchError(f"{spec['id']}: malformed frozen plan")
    return _read_frozen_plan_from_rows(rows, spec)


def _read_frozen_plan_from_rows(
    rows: Sequence[Mapping[str, str]], spec: Mapping[str, Any]
) -> dict[str, Any]:
    injection = [dict(row) for row in rows if row["kind"] == "injection"]
    repair = [dict(row) for row in rows if row["kind"] == "repair"]
    if len(repair) != spec["expected_modification_count"]:
        raise BatchError(f"{spec['id']}: frozen repair count mismatch")
    if any(row["old_ref"] == row["new_ref"] for row in [*injection, *repair]):
        raise BatchError(f"{spec['id']}: frozen plan contains an ineffective resize")
    injection_instances = {row["instance"] for row in injection}
    repair_instances = {row["instance"] for row in repair}
    if spec["injection_profile"] == "I0":
        if injection_instances != repair_instances:
            raise BatchError(f"{spec['id']}: I0 frozen sets are not direct inverse")
    elif (
        injection_instances == repair_instances
        or not injection_instances - repair_instances
        or not repair_instances - injection_instances
    ):
        raise BatchError(f"{spec['id']}: non-I0 frozen sets violate non-inverse policy")
    return {"injection_operations": injection, "repair_operations": repair}


def _fix_bytes(repair: Sequence[Mapping[str, str]]) -> bytes:
    lines = ["setEcoMode -batchMode true"]
    lines.extend(
        f"ecoChangeCell -inst {{{row['instance']}}} -cell {{{row['new_ref']}}}"
        for row in repair
    )
    lines.extend(("setEcoMode -batchMode false", "refinePlace -eco true", ""))
    data = "\n".join(lines).encode("utf-8")
    parse_fix_tcl(data.decode("utf-8"), len(repair))
    return data


def _refinement_ladder(
    operation: Mapping[str, Any],
) -> tuple[list[str], list[float], dict[str, list[float]], int] | None:
    """Return validated hidden down-ladder metadata for one seed operation."""

    raw_refs = operation.get("legal_down_refs_nearest_first")
    raw_scalar = operation.get("legal_down_estimated_slowdown_ns")
    raw_by_endpoint = operation.get(
        "legal_down_estimated_slowdown_by_endpoint_ns"
    )
    raw_selected = operation.get("selected_legal_down_level")
    if (
        raw_refs is None
        and raw_scalar is None
        and raw_by_endpoint is None
        and raw_selected is None
    ):
        return None
    if (
        not isinstance(raw_refs, list)
        or not raw_refs
        or not all(isinstance(value, str) and value for value in raw_refs)
        or len(set(raw_refs)) != len(raw_refs)
        or not isinstance(raw_scalar, list)
        or len(raw_scalar) != len(raw_refs)
        or not isinstance(raw_by_endpoint, Mapping)
        or not isinstance(raw_selected, int)
        or isinstance(raw_selected, bool)
        or not 0 <= raw_selected < len(raw_refs)
    ):
        raise BatchError(
            f"{operation.get('instance', '<unknown>')}: malformed refinement ladder"
        )
    try:
        scalar = [float(value) for value in raw_scalar]
        by_endpoint = {
            str(endpoint): [float(value) for value in values]
            for endpoint, values in raw_by_endpoint.items()
        }
    except (TypeError, ValueError) as exc:
        raise BatchError(
            f"{operation.get('instance', '<unknown>')}: "
            "non-numeric refinement estimate"
        ) from exc
    if any(len(values) != len(raw_refs) for values in by_endpoint.values()):
        raise BatchError(
            f"{operation.get('instance', '<unknown>')}: "
            "refinement endpoint estimate length mismatch"
        )
    active = operation.get("candidate_refs_nearest_first")
    if (
        not isinstance(active, list)
        or len(active) != 1
        or active[0] != raw_refs[raw_selected]
        or operation.get("new_ref") != active[0]
        or active[0] == operation.get("baseline_ref")
    ):
        raise BatchError(
            f"{operation.get('instance', '<unknown>')}: "
            "refinement seed ref does not match its legal ladder"
        )
    return list(raw_refs), scalar, by_endpoint, raw_selected


def _refinement_context(
    measured_slacks: Mapping[str, float],
    target_endpoints: Sequence[str],
    target_wns_ns: float,
    tolerance_ns: float,
) -> tuple[int, tuple[str, ...]] | None:
    targets = tuple(target_endpoints)
    if (
        not targets
        or len(set(targets)) != len(targets)
        or tolerance_ns < 0.0
        or any(endpoint not in measured_slacks for endpoint in targets)
    ):
        raise BatchError("malformed adaptive refinement timing input")
    target_set = set(targets)
    negatives = {
        endpoint
        for endpoint, slack in measured_slacks.items()
        if float(slack) < 0.0
    }
    if negatives != target_set:
        return None
    measured = {
        endpoint: float(measured_slacks[endpoint]) for endpoint in targets
    }
    lower = target_wns_ns - tolerance_ns
    upper = target_wns_ns + tolerance_ns
    wns = min(measured.values())
    if lower - 1e-12 <= wns <= upper + 1e-12:
        return None
    if wns < lower:
        # A stronger (lower-index) ref is needed on every branch that is still
        # below the allowed WNS floor.
        endpoints = tuple(
            sorted(
                (
                    endpoint
                    for endpoint, slack in measured.items()
                    if slack < lower - 1e-12
                ),
                key=lambda endpoint: (measured[endpoint], endpoint),
            )
        )
        return -1, endpoints
    # When the exact target set is too shallow, only one branch must move
    # deeper to establish the requested WNS.  Start with the current worst
    # endpoint; real Innovus evidence remains the acceptance gate.
    endpoint = min(targets, key=lambda value: (measured[value], value))
    return 1, (endpoint,)


def _select_refinement_knobs(
    operations: Sequence[Mapping[str, Any]],
    measured_slacks: Mapping[str, float],
    target_endpoints: Sequence[str],
    target_wns_ns: float,
    tolerance_ns: float,
    *,
    limit: int = 4,
) -> tuple[tuple[int, str], ...]:
    """Select at most one deterministic, preferably private knob per branch."""

    context = _refinement_context(
        measured_slacks,
        target_endpoints,
        target_wns_ns,
        tolerance_ns,
    )
    if context is None or limit <= 0:
        return ()
    direction, endpoints = context
    target_set = set(target_endpoints)
    selected: list[tuple[int, str]] = []
    covered: set[str] = set()
    used_indices: set[int] = set()
    for endpoint in endpoints:
        if endpoint in covered or len(selected) >= limit:
            continue
        ranked: list[
            tuple[
                tuple[int, float, float, int, str, int],
                int,
                set[str],
            ]
        ] = []
        for index, operation in enumerate(operations):
            if index in used_indices:
                continue
            ladder = _refinement_ladder(operation)
            if ladder is None:
                continue
            refs, _, by_endpoint, seed_level = ladder
            values = by_endpoint.get(endpoint)
            if values is None:
                continue
            directional_levels = (
                range(seed_level - 1, -1, -1)
                if direction < 0
                else range(seed_level + 1, len(refs))
            )
            directional_levels = tuple(directional_levels)
            if not directional_levels:
                continue
            affected_targets = set(by_endpoint) & target_set
            raw_reachable = operation.get("reachable_endpoints")
            if isinstance(raw_reachable, list):
                reachable_targets = set(raw_reachable) & target_set
            else:
                reachable_targets = set(affected_targets)
            private = (
                affected_targets == {endpoint}
                and reachable_targets == {endpoint}
            )
            seed_estimate = float(values[seed_level])
            headroom = max(
                abs(seed_estimate - float(values[level]))
                for level in directional_levels
            )
            ranked.append(
                (
                    (
                        0 if private else 1,
                        -seed_estimate,
                        -headroom,
                        -int(operation.get("point_index", 0)),
                        str(operation.get("instance", "")),
                        index,
                    ),
                    index,
                    affected_targets,
                )
            )
        if not ranked:
            continue
        _, index, affected_targets = min(ranked, key=lambda row: row[0])
        selected.append((index, endpoint))
        used_indices.add(index)
        covered.update(affected_targets)
    return tuple(selected)


def _refinement_level_vectors(
    operations: Sequence[Mapping[str, Any]],
    measured_slacks: Mapping[str, float],
    target_endpoints: Sequence[str],
    target_wns_ns: float,
    tolerance_ns: float,
    *,
    limit: int = 32,
    levels_per_knob: int = 4,
) -> list[tuple[int, ...]]:
    """Rank bounded full-ladder refinements for fresh real-tool trials."""

    context = _refinement_context(
        measured_slacks,
        target_endpoints,
        target_wns_ns,
        tolerance_ns,
    )
    if context is None or limit <= 0 or levels_per_knob <= 0:
        return []
    direction, _ = context
    knobs = _select_refinement_knobs(
        operations,
        measured_slacks,
        target_endpoints,
        target_wns_ns,
        tolerance_ns,
    )
    if not knobs:
        return []
    ladders = [_refinement_ladder(operation) for operation in operations]
    if any(ladder is None for ladder in ladders):
        return []
    concrete_ladders = [
        ladder for ladder in ladders if ladder is not None
    ]
    seed_vector = tuple(ladder[3] for ladder in concrete_ladders)
    lower = target_wns_ns - tolerance_ns
    upper = target_wns_ns + tolerance_ns
    scales = (1.0, 2.0, 3.0, 4.0)

    def window_distance(value: float) -> float:
        if value < lower:
            return lower - value
        if value > upper:
            return value - upper
        return 0.0

    choices: list[list[int]] = []
    knob_indices: list[int] = []
    for operation_index, endpoint in knobs:
        _, _, by_endpoint, seed_level = concrete_ladders[operation_index]
        values = by_endpoint[endpoint]
        directional = list(
            range(seed_level - 1, -1, -1)
            if direction < 0
            else range(seed_level + 1, len(values))
        )

        def local_score(level: int) -> tuple[Any, ...]:
            alternatives = []
            delta = float(values[seed_level]) - float(values[level])
            for scale_rank, scale in enumerate(scales):
                predicted = float(measured_slacks[endpoint]) + delta / scale
                alternatives.append(
                    (
                        1 if predicted >= 0.0 else 0,
                        window_distance(predicted),
                        abs(predicted - target_wns_ns),
                        scale_rank,
                    )
                )
            return (
                min(alternatives),
                abs(level - seed_level),
                level,
            )

        directional.sort(key=local_score)
        choices.append(directional[:levels_per_knob])
        knob_indices.append(operation_index)
    if any(not values for values in choices):
        return []

    def vector_score(levels: tuple[int, ...]) -> tuple[Any, ...]:
        scale_scores = []
        for scale_rank, scale in enumerate(scales):
            predicted: list[float] = []
            for endpoint in target_endpoints:
                delta = 0.0
                for operation_index, level in zip(
                    knob_indices, levels, strict=True
                ):
                    _, _, by_endpoint, seed_level = concrete_ladders[
                        operation_index
                    ]
                    values = by_endpoint.get(endpoint)
                    if values is not None:
                        delta += (
                            float(values[seed_level]) - float(values[level])
                        ) / scale
                predicted.append(float(measured_slacks[endpoint]) + delta)
            wns = min(predicted)
            scale_scores.append(
                (
                    sum(value >= 0.0 for value in predicted),
                    window_distance(wns),
                    abs(wns - target_wns_ns),
                    scale_rank,
                )
            )
        full = list(seed_vector)
        for operation_index, level in zip(
            knob_indices, levels, strict=True
        ):
            full[operation_index] = level
        full_vector = tuple(full)
        return (
            min(scale_scores),
            sum(
                abs(level - seed_vector[index])
                for index, level in enumerate(full_vector)
            ),
            full_vector,
        )

    ranked: list[tuple[tuple[Any, ...], tuple[int, ...]]] = []
    seen: set[tuple[int, ...]] = set()
    for selected_levels in itertools.product(*choices):
        full = list(seed_vector)
        for operation_index, level in zip(
            knob_indices, selected_levels, strict=True
        ):
            full[operation_index] = level
        vector = tuple(full)
        if vector == seed_vector or vector in seen:
            continue
        seen.add(vector)
        ranked.append((vector_score(tuple(selected_levels)), vector))
    ranked.sort(key=lambda row: row[0])
    return [vector for _, vector in ranked[:limit]]


def _materialize_refinement_candidate(
    candidate: Mapping[str, Any], levels: Sequence[int]
) -> dict[str, Any]:
    """Return a candidate whose runtime refs exactly match one legal vector."""

    operations = candidate["injection_operations"]
    if len(operations) != len(levels):
        raise BatchError("refinement vector cardinality mismatch")
    injection: list[dict[str, Any]] = []
    actual_refs: dict[str, str] = {}
    for operation, level in zip(operations, levels, strict=True):
        ladder = _refinement_ladder(operation)
        if ladder is None:
            raise BatchError(
                f"{operation.get('instance', '<unknown>')}: "
                "refinement ladder is unavailable"
            )
        refs, scalar, by_endpoint, _ = ladder
        if (
            not isinstance(level, int)
            or isinstance(level, bool)
            or not 0 <= level < len(refs)
        ):
            raise BatchError("refinement vector contains an illegal level")
        selected = dict(operation)
        selected["new_ref"] = refs[level]
        selected["candidate_refs_nearest_first"] = [refs[level]]
        selected["candidate_estimated_slowdown_ns"] = [scalar[level]]
        selected["candidate_estimated_slowdown_by_endpoint_ns"] = {
            endpoint: [values[level]]
            for endpoint, values in by_endpoint.items()
        }
        selected["selected_legal_down_level"] = level
        injection.append(selected)
        actual_refs[selected["instance"]] = refs[level]
    repair = []
    for operation in candidate["repair_operations"]:
        selected = dict(operation)
        selected["checkpoint_ref"] = actual_refs.get(
            selected["instance"], selected["checkpoint_ref"]
        )
        repair.append(selected)
    return {
        **candidate,
        "injection_operations": injection,
        "repair_operations": repair,
    }


def _candidate_level_vectors(
    operations: Sequence[Mapping[str, Any]],
    required_slowdown_ns: float,
    limit: int = 100,
    *,
    baseline_slacks_ns: Mapping[str, float] | None = None,
    target_wns_ns: float | None = None,
) -> list[tuple[int, ...]]:
    estimates = [
        [float(value) for value in operation["candidate_estimated_slowdown_ns"]]
        for operation in operations
    ]
    if not estimates or any(not values for values in estimates):
        raise BatchError("injection candidate lacks estimated drive-level sensitivity")

    def score(levels: tuple[int, ...]) -> float:
        if baseline_slacks_ns is not None and target_wns_ns is not None:
            endpoints = sorted(baseline_slacks_ns)
            best = math.inf
            for scale_rank, scale in enumerate((3.0, 2.0, 4.0, 1.0)):
                predicted_slacks = []
                for endpoint in endpoints:
                    slowdown = 0.0
                    for index, level in enumerate(levels):
                        by_endpoint = operations[index].get(
                            "candidate_estimated_slowdown_by_endpoint_ns", {}
                        )
                        values = by_endpoint.get(endpoint, ())
                        if level < len(values):
                            slowdown += float(values[level]) / scale
                    predicted_slacks.append(
                        float(baseline_slacks_ns[endpoint]) - slowdown
                    )
                positive = [value for value in predicted_slacks if value >= 0.0]
                wns = min(predicted_slacks)
                # Missing a target is far worse than a small severity error.
                value = (
                    100.0 * len(positive)
                    + 10.0 * sum(positive)
                    + abs(wns - target_wns_ns)
                    + 0.001 * scale_rank
                )
                best = min(best, value)
            return best
        predicted = sum(
            estimates[index][level] for index, level in enumerate(levels)
        )
        # First-order drive-ratio estimates are intentionally conservative;
        # observed post-route sensitivity is commonly 25-100% of that bound.
        return min(
            abs(predicted - required_slowdown_ns * scale)
            for scale in (1.0, 2.0, 3.0, 4.0)
        )

    size = math.prod(len(values) for values in estimates)
    if size <= 20_000:
        vectors = list(itertools.product(*(range(len(values)) for values in estimates)))
        vectors.sort(key=lambda levels: (score(levels), levels))
        return vectors[:limit]

    share = required_slowdown_ns / len(estimates)
    start = tuple(
        min(range(len(values)), key=lambda level: abs(values[level] - share))
        for values in estimates
    )
    seeds = {
        start,
        tuple(0 for _ in estimates),
        tuple(len(values) - 1 for values in estimates),
    }
    heap = [(score(levels), levels) for levels in seeds]
    heapq.heapify(heap)
    seen = set(seeds)
    output: list[tuple[int, ...]] = []
    while heap and len(output) < limit:
        _, levels = heapq.heappop(heap)
        output.append(levels)
        for index, values in enumerate(estimates):
            for delta in (-1, 1):
                neighbor = list(levels)
                neighbor[index] += delta
                if not 0 <= neighbor[index] < len(values):
                    continue
                candidate = tuple(neighbor)
                if candidate in seen:
                    continue
                seen.add(candidate)
                heapq.heappush(heap, (score(candidate), candidate))
    return output


def _write_runtime_config(
    path: Path,
    case_id: str,
    binding: Mapping[str, Any],
    plan: Mapping[str, Any],
    nominal_ns: float,
    tolerance_ns: float,
) -> None:
    nominal_ns = float(nominal_ns)
    tolerance_ns = float(tolerance_ns)
    if (
        not math.isfinite(nominal_ns)
        or nominal_ns >= 0.0
        or not math.isfinite(tolerance_ns)
        or tolerance_ns < 0.0
    ):
        raise BatchError(f"{case_id}: invalid checkpoint portability severity")
    lines = [
        f"set B100_CASE_ID {_tcl_atom(case_id)}",
        "set B100_TARGET_ENDPOINTS {"
        + " ".join(_tcl_atom(value) for value in binding["target_endpoints"])
        + "}",
        "set B100_PROTECTED_ENDPOINTS {"
        + " ".join(_tcl_atom(value) for value in binding["protected_endpoints"])
        + "}",
        f"set B100_NOMINAL_NS {nominal_ns:.12g}",
        f"set B100_TOLERANCE_NS {tolerance_ns:.12g}",
        "set B100_INJECTION_OPERATIONS {",
    ]
    for operation in plan["injection_operations"]:
        lines.append(
            "  {ecoChangeCell "
            + _tcl_atom(operation["instance"])
            + " "
            + _tcl_atom(operation["new_ref"])
            + "}"
        )
    lines.append("}")
    lines.append("set B100_REPAIR_OPERATIONS {")
    for operation in plan["repair_operations"]:
        lines.append(
            "  {ecoChangeCell "
            + _tcl_atom(operation["instance"])
            + " "
            + _tcl_atom(operation["new_ref"])
            + "}"
        )
    lines.extend(("}", ""))
    atomic_write(path, "\n".join(lines).encode("utf-8"))


def _tool_process_ok(output: str, marker: str) -> bool:
    if marker not in output or re.search(r"(?m)^\*\*ERROR:", output):
        return False
    summaries = re.findall(
        r"(?m)^\*\*\* Message Summary: \d+ warning\(s\), (\d+) error\(s\)\s*$",
        output,
    )
    return bool(summaries) and summaries[-1] == "0"


def _replay_portability_marker_ok(output: str, case_id: str) -> bool:
    passes = re.findall(
        rf"(?m)^B100_CHECKPOINT_PORTABILITY_PASS {re.escape(case_id)} "
        r"wns_ns=\S+\s*$",
        output,
    )
    failure = re.search(
        rf"(?m)^B100_CHECKPOINT_PORTABILITY_FAIL {re.escape(case_id)}(?:\s|$)",
        output,
    )
    return len(passes) == 1 and failure is None


def _operation_fingerprint(operations: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(operations, sort_keys=True).encode()
    ).hexdigest()


def _claim_operation_fingerprints(
    run_dir: Path,
    case_id: str,
    injection: Sequence[Mapping[str, Any]],
    repair: Sequence[Mapping[str, Any]],
) -> tuple[str, str] | None:
    """Atomically reserve final operation fingerprints across case threads."""

    injection_hash = _operation_fingerprint(injection)
    repair_hash = _operation_fingerprint(repair)
    lock_path = run_dir / ".fingerprint_claims.lock"
    claims_path = run_dir / "fingerprint_claims.json"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        claims = (
            read_json(claims_path, "fingerprint claims")
            if claims_path.exists()
            else {"injection": {}, "repair": {}}
        )
        for kind, fingerprint in (
            ("injection", injection_hash),
            ("repair", repair_hash),
        ):
            owner = claims[kind].get(fingerprint)
            if owner is not None and owner != case_id:
                return None
        claims["injection"][injection_hash] = case_id
        claims["repair"][repair_hash] = case_id
        atomic_json(claims_path, claims, durable=True)
    return injection_hash, repair_hash


def _remote_remove_case_leaf(slot: int, run_id: str, case_id: str) -> None:
    if SAFE_ID_RE.fullmatch(case_id) is None:
        raise BatchError(f"unsafe cleanup case ID: {case_id}")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", run_id) is None:
        raise BatchError(f"unsafe cleanup run ID: {run_id!r}")
    run_root = f"{GUEST_RUN_ROOT}/{run_id}"
    leaf = f"{GUEST_RUN_ROOT}/{run_id}/tasks/{case_id}"
    parent = f"{GUEST_RUN_ROOT}/{run_id}/tasks"
    result = _ssh(
        slot,
        f"if test -L {GUEST_RUN_ROOT} || test -L {run_root} "
        f"|| test -L {parent}; then exit 40; "
        f"elif test ! -e {GUEST_RUN_ROOT} || test ! -e {run_root} "
        f"|| test ! -e {parent}; then true; "
        f"elif test ! -d {GUEST_RUN_ROOT} || test ! -d {run_root} "
        f"|| test ! -d {parent}; then exit 42; "
        f"elif test -L {leaf}; then exit 41; "
        f"elif test ! -e {leaf}; then true; "
        f"elif test ! -d {leaf}; then exit 43; "
        f"else rm -rf -- {leaf}; fi && "
        f"test ! -e {leaf} && test ! -L {leaf}",
        timeout=120,
    )
    if result.returncode:
        raise BatchError(f"{case_id}: exact remote task cleanup failed on slot{slot}")


def _replay_assignment_path(job: Path) -> Path:
    return job / "replay_assignment.ready.json"


def _replay_assignment_ledger_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {
            key: value
            for key, value in payload.items()
            if key != "assignment_ledger_sha256"
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_replay_assignment(
    run_dir: Path, case_id: str, job: Path
) -> tuple[Path, dict[str, Any]]:
    path = require_real_file(
        _replay_assignment_path(job), f"{case_id} replay assignment"
    )
    payload = read_json(path, f"{case_id} replay assignment")
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != REPLAY_ASSIGNMENT_SCHEMA
        or payload.get("case_id") != case_id
        or payload.get("run_id") != _run_id(run_dir)
    ):
        raise BatchError(f"{case_id}: invalid replay assignment identity")
    slots = payload.get("replay_slots")
    if (
        not isinstance(slots, list)
        or len(slots) != 2
        or len(set(slots)) != 2
        or not all(isinstance(slot, int) and slot in range(10) for slot in slots)
    ):
        raise BatchError(f"{case_id}: invalid replay assignment slots")
    for key in (
        "frozen_ready_sha256",
        "freeze_ledger_sha256",
        "archive_sha256",
        "calibration_plan_sha256",
        "binding_sha256",
        "runtime_config_sha256",
        "fix_sha256",
        "assignment_ledger_sha256",
    ):
        require_hash(payload.get(key), f"{case_id}.replay_assignment.{key}")
    if payload["assignment_ledger_sha256"] != (
        _replay_assignment_ledger_sha256(payload)
    ):
        raise BatchError(f"{case_id}: replay assignment ledger changed")
    _, _, frozen = _read_frozen_checkpoint(run_dir, case_id, job)
    current = {
        "frozen_ready_sha256": sha256_file(
            _frozen_checkpoint_paths(job)[1]
        ),
        "freeze_ledger_sha256": frozen["freeze_ledger_sha256"],
        "archive_sha256": frozen["archive_sha256"],
        "calibration_plan_sha256": sha256_file(
            job / "calibration_plan.json"
        ),
        "binding_sha256": sha256_file(
            run_dir / "bindings" / f"{case_id}.json"
        ),
        "runtime_config_sha256": sha256_file(job / "runtime_config.tcl"),
        "fix_sha256": sha256_file(job / "fix.tcl"),
    }
    if any(payload[key] != value for key, value in current.items()):
        raise BatchError(f"{case_id}: replay assignment input changed")
    return path, payload


def _write_or_reuse_replay_assignment(
    run_dir: Path,
    case_id: str,
    job: Path,
    slots: Sequence[int],
) -> tuple[Path, dict[str, Any]]:
    normalized = list(slots)
    if (
        len(normalized) != 2
        or len(set(normalized)) != 2
        or any(slot not in range(10) for slot in normalized)
    ):
        raise BatchError(f"{case_id}: replay assignment requires two slots")
    path = _replay_assignment_path(job)
    if path.exists() or path.is_symlink():
        existing_path, existing = _read_replay_assignment(
            run_dir, case_id, job
        )
        if existing["replay_slots"] != normalized:
            raise BatchError(f"{case_id}: replay assignment slots changed")
        return existing_path, existing
    _, _, frozen = _read_frozen_checkpoint(run_dir, case_id, job)
    payload = {
        "schema_version": REPLAY_ASSIGNMENT_SCHEMA,
        "case_id": case_id,
        "run_id": _run_id(run_dir),
        "replay_slots": normalized,
        "frozen_ready_sha256": sha256_file(
            _frozen_checkpoint_paths(job)[1]
        ),
        "freeze_ledger_sha256": frozen["freeze_ledger_sha256"],
        "archive_sha256": frozen["archive_sha256"],
        "calibration_plan_sha256": sha256_file(
            job / "calibration_plan.json"
        ),
        "binding_sha256": sha256_file(
            run_dir / "bindings" / f"{case_id}.json"
        ),
        "runtime_config_sha256": sha256_file(job / "runtime_config.tcl"),
        "fix_sha256": sha256_file(job / "fix.tcl"),
    }
    payload["assignment_ledger_sha256"] = (
        _replay_assignment_ledger_sha256(payload)
    )
    atomic_json(path, payload, durable=True)
    return _read_replay_assignment(run_dir, case_id, job)


def _case_staging_paths(run_dir: Path, case_id: str) -> tuple[Path, Path]:
    if SAFE_ID_RE.fullmatch(case_id) is None:
        raise BatchError(f"unsafe staging case ID: {case_id}")
    staging_root = run_dir / ".case_staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    require_real_directory(staging_root, "case staging root")
    return staging_root / case_id, staging_root / f"{case_id}.ready.json"


def _write_case_staging_ready(
    run_dir: Path, case_id: str, staging: Path, slots: Sequence[int]
) -> Path:
    expected_staging, ready = _case_staging_paths(run_dir, case_id)
    if staging != expected_staging:
        raise BatchError(f"{case_id}: non-canonical case staging path")
    require_real_directory(staging, f"{case_id} case staging")
    if ready.exists() or ready.is_symlink():
        raise BatchError(f"{case_id}: staging ready marker already exists")
    normalized_slots = sorted(slots)
    if (
        len(normalized_slots) != 2
        or len(set(normalized_slots)) != 2
        or any(slot not in range(10) for slot in normalized_slots)
    ):
        raise BatchError(f"{case_id}: staging requires two distinct replay slots")
    assignment_path, assignment = _read_replay_assignment(
        run_dir,
        case_id,
        require_real_directory(
            run_dir / "jobs" / case_id, f"{case_id} job"
        ),
    )
    if assignment["replay_slots"] != list(slots):
        raise BatchError(f"{case_id}: staging replay slots changed assignment")
    atomic_json(
        ready,
        {
            "schema_version": "mock_lef_batch100.case_staging_ready.v2",
            "case_id": case_id,
            "run_id": _run_id(run_dir),
            "replay_slots": list(slots),
            "replay_assignment_sha256": sha256_file(assignment_path),
            "artifact_tree_hash_algorithm": (
                finalize_batch.CASE_ARTIFACT_TREE_HASH_ALGORITHM
            ),
            "artifact_tree_sha256": finalize_batch.case_artifact_tree_sha256(
                staging
            ),
        },
        durable=True,
    )
    return ready


def _read_case_staging_ready(
    run_dir: Path, case_id: str
) -> tuple[Path, Path, dict[str, Any]]:
    staging, ready = _case_staging_paths(run_dir, case_id)
    payload = read_json(
        require_real_file(ready, f"{case_id} staging ready marker"),
        f"{case_id} staging ready marker",
    )
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version")
        != "mock_lef_batch100.case_staging_ready.v2"
        or payload.get("case_id") != case_id
        or payload.get("run_id") != _run_id(run_dir)
        or payload.get("artifact_tree_hash_algorithm")
        != finalize_batch.CASE_ARTIFACT_TREE_HASH_ALGORITHM
    ):
        raise BatchError(f"{case_id}: invalid staging ready marker identity")
    require_hash(
        payload.get("replay_assignment_sha256"),
        f"{case_id}.staging.replay_assignment_sha256",
    )
    slots = payload.get("replay_slots")
    if (
        not isinstance(slots, list)
        or len(slots) != 2
        or len(set(slots)) != 2
        or not all(isinstance(slot, int) and slot in range(10) for slot in slots)
    ):
        raise BatchError(f"{case_id}: invalid staging replay slots")
    require_hash(
        payload.get("artifact_tree_sha256"),
        f"{case_id}.staging.artifact_tree_sha256",
    )
    return staging, ready, payload


def _validate_staging_replay_assignment(
    run_dir: Path,
    case_id: str,
    payload: Mapping[str, Any],
    *,
    require_frozen_inputs: bool,
    allow_already_removed: bool = False,
) -> Path | None:
    job = require_real_directory(
        run_dir / "jobs" / case_id, f"{case_id} job"
    )
    path = _replay_assignment_path(job)
    if not path.exists() and not path.is_symlink():
        if allow_already_removed:
            return None
        raise BatchError(f"{case_id}: replay assignment is missing")
    require_real_file(path, f"{case_id} replay assignment")
    if sha256_file(path) != payload["replay_assignment_sha256"]:
        raise BatchError(f"{case_id}: staging replay assignment changed")
    frozen_root, frozen_ready = _frozen_checkpoint_paths(job)
    frozen_exists = frozen_root.exists() or frozen_root.is_symlink()
    frozen_ready_exists = frozen_ready.exists() or frozen_ready.is_symlink()
    if require_frozen_inputs and frozen_exists and frozen_ready_exists:
        _, assignment = _read_replay_assignment(run_dir, case_id, job)
    else:
        if require_frozen_inputs and frozen_exists != frozen_ready_exists:
            raise BatchError(
                f"{case_id}: incomplete frozen input during staging promotion"
            )
        assignment = read_json(path, f"{case_id} replay assignment")
        if (
            not isinstance(assignment, dict)
            or assignment.get("schema_version") != REPLAY_ASSIGNMENT_SCHEMA
            or assignment.get("case_id") != case_id
            or assignment.get("run_id") != _run_id(run_dir)
            or assignment.get("assignment_ledger_sha256")
            != _replay_assignment_ledger_sha256(assignment)
        ):
            raise BatchError(f"{case_id}: replay assignment marker changed")
        for key in (
            "frozen_ready_sha256",
            "freeze_ledger_sha256",
            "archive_sha256",
            "calibration_plan_sha256",
            "binding_sha256",
            "runtime_config_sha256",
            "fix_sha256",
            "assignment_ledger_sha256",
        ):
            require_hash(
                assignment.get(key),
                f"{case_id}.replay_assignment.{key}",
            )
        if require_frozen_inputs:
            _, removed = _read_removed_frozen_cleanup(
                run_dir, case_id, job
            )
            if (
                removed["ready_sha256"]
                != assignment["frozen_ready_sha256"]
                or removed["freeze_ledger_sha256"]
                != assignment["freeze_ledger_sha256"]
            ):
                raise BatchError(
                    f"{case_id}: removed frozen proof changed assignment"
                )
    if assignment.get("replay_slots") != payload["replay_slots"]:
        raise BatchError(f"{case_id}: staging replay slots changed assignment")
    return path


def _cleanup_replay_case_leaves(
    slots: Sequence[int], run_id: str, case_id: str
) -> None:
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=2) as cleanup_executor:
        cleanup_futures = {
            cleanup_executor.submit(
                _remote_remove_case_leaf, slot, run_id, case_id
            ): slot
            for slot in slots
        }
        for cleanup_future in as_completed(cleanup_futures):
            try:
                cleanup_future.result()
            except Exception as exc:
                failures.append(
                    f"slot{cleanup_futures[cleanup_future]}: {exc}"
                )
    if failures:
        raise BatchError(
            f"{case_id}: exact dual-slot cleanup incomplete: "
            + "; ".join(sorted(failures))
        )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _finish_ready_case_staging(
    run_dir: Path,
    store: StateStore,
    spec: Mapping[str, Any],
    acceptance: Mapping[str, Any],
) -> str:
    """Cleanup, atomically publish, then transition one complete staged case.

    A cleanup failure leaves the hash-bound staging tree and ready marker
    untouched in REPLAYING.  Re-running this helper is therefore cleanup-only:
    already absent remote leaves are successful no-ops.
    """

    case_id = spec["id"]
    if store.row(case_id)["state"] != "REPLAYING":
        raise BatchError(f"{case_id}: ready promotion requires REPLAYING state")
    staging, ready, payload = _read_case_staging_ready(run_dir, case_id)
    cases_root = require_real_directory(run_dir / "cases", "case output root")
    final_case = cases_root / case_id
    staging_exists = staging.exists() or staging.is_symlink()
    final_exists = final_case.exists() or final_case.is_symlink()
    if staging_exists and final_exists:
        raise BatchError(f"{case_id}: both staging and final case leaves exist")
    if not staging_exists and not final_exists:
        raise BatchError(f"{case_id}: ready marker has no case artifact tree")

    artifact_root = staging if staging_exists else final_case
    require_real_directory(artifact_root, f"{case_id} ready artifact tree")
    if (
        finalize_batch.case_artifact_tree_sha256(artifact_root)
        != payload["artifact_tree_sha256"]
    ):
        raise BatchError(f"{case_id}: ready artifact tree hash changed")
    finalize_batch.validate_case(artifact_root, spec, acceptance)
    _validate_staging_replay_assignment(
        run_dir,
        case_id,
        payload,
        require_frozen_inputs=staging_exists,
    )

    if staging_exists:
        _cleanup_replay_case_leaves(
            payload["replay_slots"], payload["run_id"], case_id
        )
        _cleanup_local_frozen_checkpoint(
            run_dir, case_id, run_dir / "jobs" / case_id, store
        )
        # The remote cleanup cannot mutate local artifacts, but this second
        # equality also proves the exact transient-checkpoint cleanup did not
        # mutate the already complete canonical staging tree.
        if (
            finalize_batch.case_artifact_tree_sha256(staging)
            != payload["artifact_tree_sha256"]
        ):
            raise BatchError(f"{case_id}: staging changed during remote cleanup")
        if final_case.exists() or final_case.is_symlink():
            raise BatchError(f"{case_id}: final case leaf appeared before promotion")
        os.rename(staging, final_case)
        _fsync_directory(cases_root)
        _fsync_directory(staging.parent)

    try:
        store.transition(
            case_id,
            "VALIDATED",
            "strict local dual-replay validation after exact remote cleanup",
        )
    except Exception:
        # Keep the fail-closed invariant if the state transaction itself did
        # not commit.  If it committed and only sidecar refresh failed, the
        # final case correctly belongs to VALIDATED and must not be rolled back.
        if (
            store.row(case_id)["state"] == "REPLAYING"
            and final_case.exists()
            and not staging.exists()
        ):
            os.rename(final_case, staging)
            _fsync_directory(cases_root)
            _fsync_directory(staging.parent)
        raise
    slots = _finish_validated_case_markers(
        run_dir, store, spec, acceptance
    )
    return f"{case_id}: VALIDATED slot{slots[0]}/slot{slots[1]}"


def _finish_validated_case_markers(
    run_dir: Path,
    store: StateStore,
    spec: Mapping[str, Any],
    acceptance: Mapping[str, Any],
) -> list[int]:
    """Converge post-commit marker/artifact work for one VALIDATED case."""

    case_id = spec["id"]
    if store.row(case_id)["state"] != "VALIDATED":
        raise BatchError(
            f"{case_id}: committed marker cleanup requires VALIDATED state"
        )
    staging, ready, payload = _read_case_staging_ready(run_dir, case_id)
    final_case = require_real_directory(
        run_dir / "cases" / case_id, f"{case_id} final case"
    )
    if staging.exists() or staging.is_symlink():
        raise BatchError(f"{case_id}: VALIDATED case still has staging tree")
    if (
        finalize_batch.case_artifact_tree_sha256(final_case)
        != payload["artifact_tree_sha256"]
    ):
        raise BatchError(f"{case_id}: committed case artifact tree changed")
    finalize_batch.validate_case(final_case, spec, acceptance)
    assignment = _validate_staging_replay_assignment(
        run_dir,
        case_id,
        payload,
        require_frozen_inputs=False,
        allow_already_removed=True,
    )
    retained_bytes = sum(
        path.stat().st_size
        for path in final_case.rglob("*")
        if not path.is_symlink() and path.is_file()
    )
    store.register_artifact(
        case_id,
        "validated_case_tree",
        str(final_case.relative_to(run_dir)),
        payload["artifact_tree_sha256"],
        retained_bytes,
    )
    store.remove_artifacts(
        case_id,
        ["frozen_checkpoint_archive", "frozen_checkpoint_ready"],
    )
    cleanup_tombstone, cleanup_intent = _frozen_cleanup_paths(
        run_dir / "jobs" / case_id
    )
    if cleanup_tombstone.exists() or cleanup_tombstone.is_symlink():
        raise BatchError(f"{case_id}: frozen cleanup tombstone remains")
    frozen_root, frozen_ready = _frozen_checkpoint_paths(
        run_dir / "jobs" / case_id
    )
    if (
        frozen_root.exists()
        or frozen_root.is_symlink()
        or frozen_ready.exists()
        or frozen_ready.is_symlink()
    ):
        raise BatchError(f"{case_id}: frozen checkpoint remains after validation")
    if cleanup_intent.exists() or cleanup_intent.is_symlink():
        _, removed = _read_removed_frozen_cleanup(
            run_dir, case_id, run_dir / "jobs" / case_id
        )
        if assignment is not None:
            assignment_payload = read_json(
                assignment, f"{case_id} replay assignment"
            )
            if (
                removed["ready_sha256"]
                != assignment_payload.get("frozen_ready_sha256")
                or removed["freeze_ledger_sha256"]
                != assignment_payload.get("freeze_ledger_sha256")
            ):
                raise BatchError(
                    f"{case_id}: cleanup proof changed before marker removal"
                )
    if assignment is not None:
        assignment.unlink()
        _fsync_directory(assignment.parent)
    if cleanup_intent.exists() or cleanup_intent.is_symlink():
        require_real_file(
            cleanup_intent, f"{case_id} removed frozen cleanup proof"
        ).unlink()
        _fsync_directory(cleanup_intent.parent)
    ready.unlink()
    _fsync_directory(ready.parent)
    store.refresh_sidecar(case_id)
    return list(payload["replay_slots"])


def _sensitivity(calibration: Path) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for path in sorted(calibration.glob("*_sensitivity/fetch/*/sensitivity.tsv")):
        for row in _tsv(path):
            result.setdefault(row["instance"], {})[row["endpoint"]] = float(
                row["delta_ns"]
            )
    if not result:
        raise BatchError("calibration has no repair sensitivity evidence")
    return result


def _functional_pairs(run_dir: Path) -> dict[tuple[str, str], bool]:
    result: dict[tuple[str, str], bool] = {}
    for row in _tsv(run_dir / "probe" / "drive_ladders.tsv"):
        if row["equivalent"] != "TRUE":
            continue
        source, variant = row["source_ref"], row["variant_ref"]
        result[(source, variant)] = True
        result[(variant, source)] = True
    return result


def _execute_one(
    run_dir: Path,
    store: StateStore,
    spec: Mapping[str, Any],
    slot_a: int,
    slot_b: int,
    functional_pairs: Mapping[tuple[str, str], bool],
) -> str:
    case_id = spec["id"]
    frozen_row = store.row(case_id)
    if frozen_row["state"] != "FROZEN":
        return f"{case_id}: skipped state={frozen_row['state']}"
    if slot_a == slot_b:
        raise BatchError(f"{case_id}: replay slots must differ")
    job = require_real_directory(run_dir / "jobs" / case_id, f"{case_id} job")
    plan_path = require_real_file(
        job / "calibration_plan.json", f"{case_id} calibration plan"
    )
    plan_sha256 = sha256_file(plan_path)
    plan = read_json(plan_path, f"{case_id} calibration plan")
    if (
        not isinstance(plan, dict)
        or plan.get("schema_version")
        != "mock_lef_batch100.calibration_plan.v1"
        or plan.get("case_id") != case_id
    ):
        raise BatchError(f"{case_id}: frozen calibration plan identity mismatch")
    injection_fingerprint = _operation_fingerprint(
        plan.get("injection_operations", [])
    )
    repair_fingerprint = _operation_fingerprint(
        plan.get("repair_operations", [])
    )
    if (
        require_hash(
            frozen_row.get("injection_sha256"),
            f"{case_id}.state.injection_sha256",
        )
        != injection_fingerprint
        or require_hash(
            frozen_row.get("repair_sha256"),
            f"{case_id}.state.repair_sha256",
        )
        != repair_fingerprint
    ):
        raise BatchError(f"{case_id}: frozen operation fingerprint changed before replay")
    binding_path = require_real_file(
        run_dir / "bindings" / f"{case_id}.json", f"{case_id} binding"
    )
    binding_hash = sha256_file(binding_path)
    if (
        require_hash(
            frozen_row.get("binding_sha256"),
            f"{case_id}.state.binding_sha256",
        )
        != binding_hash
        or plan.get("binding_sha256") != binding_hash
    ):
        raise BatchError(f"{case_id}: frozen binding changed before replay")
    binding = read_json(binding_path, f"{case_id} binding")
    config = require_real_file(
        job / "runtime_config.tcl", f"{case_id} frozen runtime config"
    )
    fix = require_real_file(job / "fix.tcl", f"{case_id} fix")
    if plan.get("fix_sha256") != sha256_file(fix):
        raise BatchError(f"{case_id}: frozen fix changed before replay")
    calibration = require_real_directory(
        job / "calibration", f"{case_id} calibration evidence"
    )
    if plan.get("calibration_tree_sha256") != tree_sha256(calibration):
        raise BatchError(f"{case_id}: frozen calibration evidence changed before replay")
    frozen_root, local_archive, frozen_checkpoint = _read_frozen_checkpoint(
        run_dir, case_id, job
    )
    if (
        frozen_checkpoint["calibration_plan_sha256"] != plan_sha256
        or frozen_checkpoint["binding_sha256"] != binding_hash
        or frozen_checkpoint["runtime_config_sha256"] != sha256_file(config)
    ):
        raise BatchError(f"{case_id}: frozen checkpoint identity changed before replay")
    local_inject = frozen_root / "unpacked"
    local_inject_reports = frozen_root / "injection_reports_fetch"
    inject_log = frozen_root / "inject_innovus.log"
    checkpoint_tree_hash = frozen_checkpoint["checkpoint_tree_sha256"]
    run_id = _run_id(run_dir)
    root_a = f"{GUEST_RUN_ROOT}/{run_id}/tasks/{case_id}"
    root_b = f"{GUEST_RUN_ROOT}/{run_id}/tasks/{case_id}"
    _write_or_reuse_replay_assignment(
        run_dir, case_id, job, (slot_a, slot_b)
    )
    # Record the replay phase before its first remote mutation.  A setup/SCP
    # interruption must remain visibly REPLAYING for explicit leaf inspection;
    # it must never masquerade as a clean FROZEN case that can be dispatched
    # onto different slots and orphan the partially created task leaf.
    store.transition(
        case_id,
        "REPLAYING",
        "two fresh replays from locally hash-bound violating checkpoint",
        slot=f"slot{slot_a},slot{slot_b}",
    )
    for slot, root in ((slot_a, root_a), (slot_b, root_b)):
        created = _ssh(
            slot,
            f"test ! -e {root} && mkdir -p {root}/input {root}/checkpoint",
            timeout=30,
        )
        if created.returncode:
            raise BatchError(
                f"{case_id}: execution task leaf is not fresh on slot{slot}"
            )
        _scp_to(slot, TOOLS["runtime"], f"{root}/input/runtime.tcl")
        _scp_to(slot, TOOLS["common"], f"{root}/input/common.py")
        _scp_to(slot, config, f"{root}/input/case_config.tcl")
        _scp_to(slot, fix, f"{root}/input/fix.tcl")
        _scp_to(
            slot,
            local_archive,
            f"{root}/checkpoint/{checkpoint_archive.CHECKPOINT_ARCHIVE_NAME}",
        )
        _extract_guest_checkpoint_archive(
            slot,
            f"{root}/checkpoint",
            frozen_checkpoint["archive_sha256"],
        )

    replay_specs = (
        (1, slot_a, root_a, f"{root_a}/checkpoint/unpacked/violating.enc"),
        (2, slot_b, root_b, f"{root_b}/checkpoint/unpacked/violating.enc"),
    )
    def run_replay(
        replay_spec: tuple[int, int, str, str],
    ) -> tuple[int, Path, Path, str, str]:
        index, slot, root, checkpoint = replay_spec
        command = (
            f"bash -lc 'cd {root}/input && B100_MODE=REPLAY "
            f"B100_TASK_DIR={root}/replay_{index} "
            f"B100_CASE_CONFIG={root}/input/case_config.tcl "
            f"B100_FIX_TCL={root}/input/fix.tcl B100_CHECKPOINT={checkpoint} "
            f"timeout 14400s innovus -no_gui -files runtime.tcl "
            f"-log replay_{index}.log'"
        )
        replayed = _ssh(slot, command, timeout=14500)
        log = job / f"replay_{index}_innovus.log"
        atomic_write(log, (replayed.stdout or "").encode("utf-8"))
        parent = job / f"replay_{index}_fetch"
        parent.mkdir()
        fetch_error: Exception | None = None
        try:
            _scp_from(
                slot, f"{root}/replay_{index}", parent, recursive=True
            )
        except Exception as exc:
            # A failed Innovus process often leaves the most useful reports
            # behind.  Preserve them before classifying stdout/return status.
            fetch_error = exc
        fetch_suffix = (
            f"; evidence fetch also failed: {fetch_error}"
            if fetch_error is not None
            else ""
        )
        portability_failed = re.search(
            rf"(?m)^B100_CHECKPOINT_PORTABILITY_FAIL "
            rf"{re.escape(case_id)}(?:\s|$)",
            replayed.stdout or "",
        )
        if portability_failed:
            raise BatchError(
                f"{case_id}: replay {index} failed the fresh-restore "
                f"checkpoint portability gate; see {log}{fetch_suffix}"
            )
        if replayed.returncode or not _tool_process_ok(
            replayed.stdout or "", f"B100_REPLAY_COMPLETE {case_id}"
        ):
            raise BatchError(
                f"{case_id}: replay {index} failed; see {log}{fetch_suffix}"
            )
        if not _replay_portability_marker_ok(replayed.stdout or "", case_id):
            raise BatchError(
                f"{case_id}: replay {index} lacks unambiguous fresh-restore "
                f"checkpoint portability evidence; see {log}{fetch_suffix}"
            )
        if fetch_error is not None:
            raise BatchError(
                f"{case_id}: replay {index} passed process gates but its "
                f"evidence fetch failed: {fetch_error}; see {log}"
            )
        fetched = parent / f"replay_{index}"
        reports = require_real_directory(
            fetched / "reports", f"{case_id} replay {index} reports"
        )
        process = re.findall(r"(?m)^B100_PROCESS_ID\s+(\d+)\s*$", replayed.stdout or "")
        if len(process) != 1:
            raise BatchError(f"{case_id}: replay {index} process ID is ambiguous")
        return slot, reports, log, process[0], f"replay_{index}"

    # Both replays are independent fresh Innovus processes restored from the
    # same frozen checkpoint.  Run them concurrently on distinct VMs.
    with ThreadPoolExecutor(max_workers=2) as replay_executor:
        replay_futures = {
            replay_executor.submit(run_replay, replay_spec): replay_spec[0]
            for replay_spec in replay_specs
        }
        replay_local: list[tuple[int, Path, Path, str, str]] = []
        replay_failures: list[str] = []
        for replay_future in as_completed(replay_futures):
            try:
                replay_local.append(replay_future.result())
            except Exception as exc:
                replay_failures.append(
                    f"replay {replay_futures[replay_future]}: {exc}"
                )
    if replay_failures:
        raise BatchError(
            f"{case_id}: dual replay failure(s) after both evidence fetches: "
            + " | ".join(sorted(replay_failures))
        )
    replay_local.sort(key=lambda item: item[4])

    cases_root = run_dir / "cases"
    cases_root.mkdir(parents=True, exist_ok=True)
    require_real_directory(cases_root, "case output root")
    case_root, ready_marker = _case_staging_paths(run_dir, case_id)
    final_case_root = cases_root / case_id
    if case_root.exists() or case_root.is_symlink():
        raise BatchError(f"{case_id}: case staging leaf already exists")
    if ready_marker.exists() or ready_marker.is_symlink():
        raise BatchError(f"{case_id}: case staging marker already exists")
    if final_case_root.exists() or final_case_root.is_symlink():
        raise BatchError(f"{case_id}: final case leaf already exists")
    case_root.mkdir()
    shutil.copy2(local_inject / "violating.enc", case_root / "violating.enc")
    shutil.copytree(
        local_inject / "violating.enc.dat",
        case_root / "violating.enc.dat",
        symlinks=True,
    )
    shutil.copy2(fix, case_root / "fix.tcl")
    evidence_root = case_root / "evidence"
    shutil.copytree(
        local_inject_reports / "reports",
        evidence_root / "injection" / "reports",
    )
    shutil.copy2(inject_log, evidence_root / "injection" / "innovus.log")
    if sha256_file(plan_path) != plan_sha256:
        raise BatchError(f"{case_id}: frozen calibration plan changed during execution")
    shutil.copy2(
        plan_path, evidence_root / "injection" / "calibration_plan.json"
    )
    if (
        sha256_file(evidence_root / "injection" / "calibration_plan.json")
        != plan_sha256
    ):
        raise BatchError(f"{case_id}: copied calibration plan hash mismatch")
    for index, (_, reports, log, _, name) in enumerate(replay_local, start=1):
        destination = evidence_root / name
        shutil.copytree(reports, destination / "reports")
        shutil.copy2(log, destination / "innovus.log")

    checkpoint_hash = sha256_file(case_root / "violating.enc")
    promoted_checkpoint_tree_hash = checkpoint_archive.checkpoint_tree_sha256_v2(
        case_root / "violating.enc.dat", baseline_prefix=GUEST_BASELINE
    )
    if (
        checkpoint_hash != frozen_checkpoint["checkpoint_wrapper_sha256"]
        or promoted_checkpoint_tree_hash != checkpoint_tree_hash
    ):
        raise BatchError(f"{case_id}: promoted checkpoint hash changed")
    checkpoint_bundle_hash = (
        checkpoint_archive.checkpoint_bundle_sha256_v1_from_hashes(
            checkpoint_hash, checkpoint_tree_hash
        )
    )
    if checkpoint_bundle_hash != frozen_checkpoint["checkpoint_bundle_sha256"]:
        raise BatchError(f"{case_id}: promoted checkpoint bundle hash changed")
    fix_hash = sha256_file(case_root / "fix.tcl")
    sensitivity = _sensitivity(
        job / "calibration" / plan["selected_calibration_evidence"]
    )
    replay_records = []
    for slot, reports, log, process, _ in replay_local:
        replay_records.append(
            evidence.replay_record(
                case_id=case_id,
                slot=f"slot{slot}",
                process_id=f"slot{slot}:pid{process}",
                log_path=log,
                reports=reports,
                targets=binding["target_endpoints"],
                protected=binding["protected_endpoints"],
                fix_sha256=fix_hash,
                checkpoint_sha256=checkpoint_hash,
                checkpoint_tree_sha256=checkpoint_tree_hash,
                checkpoint_tree_hash_algorithm=(
                    checkpoint_archive.CHECKPOINT_TREE_HASH_ALGORITHM
                ),
                checkpoint_bundle_sha256=checkpoint_bundle_hash,
                checkpoint_bundle_hash_algorithm=(
                    checkpoint_archive.CHECKPOINT_BUNDLE_HASH_ALGORITHM
                ),
                repair_operations=plan["repair_operations"],
                sensitivity=sensitivity,
                functional_pairs=functional_pairs,
            )
        )

    injection_fingerprint = _operation_fingerprint(
        plan["injection_operations"]
    )
    repair_fingerprint = _operation_fingerprint(plan["repair_operations"])
    validation = {
        "schema_version": "mock_lef_batch100.validation.v2",
        "case_id": case_id,
        "status": "VALIDATED",
        "validation_class": "validated_innovus_dual_replay",
        "signoff_qualified": False,
        "technology_classification": "mock_training_non_signoff",
        "fix_sha256": fix_hash,
        "violating_checkpoint_bundle_sha256": checkpoint_bundle_hash,
        "violating_checkpoint_bundle_hash_algorithm": (
            checkpoint_archive.CHECKPOINT_BUNDLE_HASH_ALGORITHM
        ),
        "violating_checkpoint_tree_hash_algorithm": (
            checkpoint_archive.CHECKPOINT_TREE_HASH_ALGORITHM
        ),
        "calibration_plan_sha256": plan_sha256,
        "injection_fingerprint_sha256": injection_fingerprint,
        "repair_fingerprint_sha256": repair_fingerprint,
        "hidden_injection_audit": {
            "injection_instances": sorted(
                row["instance"] for row in plan["injection_operations"]
            )
        },
        "injection_raw_evidence": evidence.raw_evidence_binding(
            evidence_root / "injection" / "innovus.log",
            evidence_root / "injection" / "reports",
        ),
        "replays": replay_records,
    }
    atomic_json(case_root / "validation.json", validation, durable=True)
    first = replay_records[0]
    metrics = {
        "schema_version": "mock_lef_batch100.metrics.v1",
        "case_id": case_id,
        "status": "PASS",
        "fix_sha256": fix_hash,
        "setup_wns_before_ns": first["before"]["setup_wns_ns"],
        "setup_wns_after_resize_ns": first["after_resize"]["setup_wns_ns"],
        "setup_wns_after_legalize_ns": first["after_legalize"]["setup_wns_ns"],
        "setup_tns_after_legalize_ns": first["after_legalize"]["setup_tns_ns"],
        "hold_before_ns": first["before"]["hold_wns_ns"],
        "hold_after_ns": first["after_legalize"]["hold_wns_ns"],
        "hold_gate_applied": False,
    }
    atomic_json(case_root / "metrics.json", metrics)
    observable = first["before"]
    instruction = (
        f"案例 {case_id} 的 violating checkpoint 在 {spec['hierarchy']} 层级观察到 "
        f"setup WNS={observable['setup_wns_ns']:.6f} ns、"
        f"TNS={observable['setup_tns_ns']:.6f} ns。负裕量 endpoint 恰为："
        + "、".join(binding["target_endpoints"])
        + "。目标 data path 的 cell/net delay (ns) 为："
        + "；".join(
            f"{endpoint}={values['cell_delay_ns']:.6f}/{values['net_delay_ns']:.6f}"
            for endpoint, values in observable["target_paths"].items()
        )
        + f"。DRV 计数：transition={observable['drv']['max_transition']}，"
        f"capacitance={observable['drv']['max_capacitance']}，"
        f"fanout={observable['drv']['max_fanout']}。请给出严格 {spec['expected_modification_count']} "
        "个不同实例的最小 RVT 组合逻辑 resize ECO，并在末尾仅执行 "
        "refinePlace -eco true；不得修改连接、约束、时钟或路由。\n"
    )
    atomic_write(case_root / "instruction.txt", instruction.encode("utf-8"))
    fix_text = fix.read_text(encoding="utf-8")
    answer = (
        "采用下列冻结的等价 RVT resize；目标在 legalization 前已闭合，"
        "随后仅做增量合法化。\n\n```tcl\n"
        + fix_text
        + "```\n"
    )
    atomic_write(case_root / "answer.txt", answer.encode("utf-8"))

    acceptance = read_json(SPECS_PATH)["acceptance"]
    finalize_batch.validate_case(case_root, spec, acceptance)
    _write_case_staging_ready(
        run_dir, case_id, case_root, (slot_a, slot_b)
    )
    return _finish_ready_case_staging(
        run_dir, store, spec, acceptance
    )


def _calibrate_one(
    run_dir: Path,
    store: StateStore,
    spec: Mapping[str, Any],
    slot: int,
) -> str:
    case_id = spec["id"]
    row = store.row(case_id)
    if row["state"] != "BOUND":
        return f"{case_id}: skipped state={row['state']}"
    attempt = int(row["attempt"]) + 1
    store.transition(
        case_id,
        "CALIBRATING",
        "fresh exact-baseline resize-only calibration",
        slot=f"slot{slot}",
        attempt=attempt,
    )
    job = run_dir / "jobs" / case_id
    if job.exists():
        raise BatchError(f"{case_id}: local job leaf already exists")
    job.mkdir(parents=True)
    binding_path = run_dir / "bindings" / f"{case_id}.json"
    binding = read_json(binding_path, f"{case_id} binding")
    run_id = _run_id(run_dir)
    remote_task = f"{GUEST_RUN_ROOT}/{run_id}/tasks/{case_id}"
    remote_input = f"{remote_task}/input"
    result = _ssh(
        slot,
        f"test ! -e {remote_task} && mkdir -p {remote_input}",
        timeout=30,
    )
    if result.returncode:
        raise BatchError(f"{case_id}: remote task leaf is not fresh: {result.stdout[-1000:]}")
    _scp_to(slot, TOOLS["calibrate"], f"{remote_input}/calibrate.tcl")
    calibration = job / "calibration"
    calibration.mkdir()

    process_index = 0
    current_candidate_dir = calibration

    def run_process(
        mode: str, levels: Sequence[int], *, repair_index: int | None = None
    ) -> Path:
        nonlocal process_index, current_candidate_dir
        process_index += 1
        leaf = f"{process_index:03d}_{mode.lower()}"
        remote_output = f"{remote_task}/calibration/{leaf}"
        extra = (
            f"B100_REPAIR_INDEX={repair_index} " if repair_index is not None else ""
        )
        command = (
            f"bash -lc 'cd {remote_input} && "
            f"B100_BASELINE_DIR={GUEST_BASELINE} "
            f"B100_CALIBRATION_DIR={remote_output} "
            f"B100_CASE_CONFIG={remote_input}/case_config.tcl "
            f"B100_CAL_MODE={mode} B100_LEVELS={','.join(map(str, levels))} "
            f"{extra}timeout 14400s innovus -no_gui -files calibrate.tcl "
            f"-log {leaf}.log'"
        )
        result = _ssh(slot, command, timeout=14500)
        local_leaf = current_candidate_dir / leaf
        local_leaf.mkdir()
        atomic_write(
            local_leaf / "innovus.log", (result.stdout or "").encode("utf-8")
        )
        fetched_parent = local_leaf / "fetch"
        fetched_parent.mkdir()
        fetch_error: Exception | None = None
        try:
            _scp_from(
                slot, remote_output, fetched_parent, recursive=True
            )
        except Exception as exc:
            # Fetch before classification so a failed process cannot strand
            # its reports in the guest and erase the root-cause evidence.
            fetch_error = exc
        marker = {
            "INJECTION": "B100_CALIBRATION_TRIAL_COMPLETE",
            "SENSITIVITY": "B100_CALIBRATION_SENSITIVITY_COMPLETE",
            "REPAIR": "B100_CALIBRATION_REPAIR_COMPLETE",
        }[mode]
        if (
            result.returncode
            or f"{marker} {case_id}" not in (result.stdout or "")
            or re.search(r"(?m)^\*\*ERROR:", result.stdout or "")
        ):
            raise BatchError(
                f"{case_id}: {mode.lower()} process {process_index} failed; "
                f"see {local_leaf / 'innovus.log'}"
                + (
                    f"; evidence fetch also failed: {fetch_error}"
                    if fetch_error is not None
                    else ""
                )
            )
        if fetch_error is not None:
            raise BatchError(
                f"{case_id}: {mode.lower()} process {process_index} passed "
                f"but evidence fetch failed: {fetch_error}; "
                f"see {local_leaf / 'innovus.log'}"
            )
        fetched = fetched_parent / leaf
        if not fetched.is_dir():
            raise BatchError(f"{case_id}: calibration process fetch is incomplete")
        return fetched

    def slacks(path: Path) -> dict[str, float]:
        rows = _tsv(path)
        result = {row["endpoint"]: float(row["slack_ns"]) for row in rows}
        if len(result) != len(rows) or not result:
            raise BatchError(f"{case_id}: malformed calibration slack evidence")
        return result

    target_set = set(binding["target_endpoints"])
    target_wns = float(spec["nominal_severity"]["target_wns_ns"])
    tolerance = float(spec["nominal_severity"]["tolerance_ps"]) / 1000.0
    candidates = binding.get("calibration_candidates") or [
        {
            "candidate_index": 0,
            "injection_operations": binding["injection_operations"],
            "repair_operations": binding["repair_operations"],
        }
    ]
    accepted_levels: list[int] | None = None
    accepted_wns: float | None = None
    accepted_candidate_index: int | None = None
    selected_candidate_dir: Path | None = None
    selected_config_path: Path | None = None
    accepted_refinement_vector: list[int] | None = None
    plan: dict[str, Any] | None = None
    rejection_reasons: list[str] = []
    for candidate_position, candidate in enumerate(candidates):
        candidate_binding = {
            **binding,
            "injection_operations": candidate["injection_operations"],
            "repair_operations": candidate["repair_operations"],
        }
        current_candidate_dir = calibration / f"candidate_{candidate_position:02d}"
        current_candidate_dir.mkdir()
        config_path = job / f"calibration_config_{candidate_position:02d}.tcl"
        _write_calibration_config(config_path, spec, candidate_binding)
        _scp_to(slot, config_path, f"{remote_input}/case_config.tcl")
        candidate_levels: list[int] | None = None
        candidate_wns: float | None = None
        candidate_refinement_vector: list[int] | None = None
        refinement_vectors: list[tuple[int, ...]] = []
        baseline_wns = min(
            float(value)
            for value in binding["target_baseline_slacks_ns"].values()
        )
        level_vectors = _candidate_level_vectors(
            candidate_binding["injection_operations"],
            baseline_wns - target_wns,
        )
        seed_runtime_vector = tuple(
            0 for _ in candidate_binding["injection_operations"]
        )
        level_vectors = [
            seed_runtime_vector,
            *(
                vector
                for vector in level_vectors
                if vector != seed_runtime_vector
            ),
        ]
        for vector in level_vectors:
            levels = list(vector)
            fetched = run_process("INJECTION", levels)
            measured = slacks(fetched / "injection_slacks.tsv")
            negatives = {
                endpoint for endpoint, slack in measured.items() if slack < 0.0
            }
            if negatives == target_set:
                wns = min(measured[endpoint] for endpoint in target_set)
                if abs(wns - target_wns) <= tolerance + 1e-12:
                    candidate_levels = list(levels)
                    candidate_wns = wns
                    break
                if vector == seed_runtime_vector:
                    refinement_vectors = _refinement_level_vectors(
                        candidate_binding["injection_operations"],
                        measured,
                        binding["target_endpoints"],
                        target_wns,
                        tolerance,
                    )
                    if refinement_vectors:
                        # The seed established the exact endpoint set, so stop
                        # static searching and retest bounded legal refinements.
                        break
        if candidate_levels is None and refinement_vectors:
            for refinement_index, legal_levels in enumerate(
                refinement_vectors
            ):
                refined_candidate = _materialize_refinement_candidate(
                    candidate, legal_levels
                )
                refined_binding = {
                    **binding,
                    "injection_operations": refined_candidate[
                        "injection_operations"
                    ],
                    "repair_operations": refined_candidate[
                        "repair_operations"
                    ],
                }
                refined_config_path = (
                    job
                    / (
                        f"calibration_config_{candidate_position:02d}_"
                        f"refinement_{refinement_index:03d}.tcl"
                    )
                )
                _write_calibration_config(
                    refined_config_path, spec, refined_binding
                )
                _scp_to(
                    slot,
                    refined_config_path,
                    f"{remote_input}/case_config.tcl",
                )
                levels = [0] * len(
                    refined_binding["injection_operations"]
                )
                fetched = run_process("INJECTION", levels)
                measured = slacks(fetched / "injection_slacks.tsv")
                negatives = {
                    endpoint
                    for endpoint, slack in measured.items()
                    if slack < 0.0
                }
                if negatives != target_set:
                    continue
                wns = min(measured[endpoint] for endpoint in target_set)
                if abs(wns - target_wns) > tolerance + 1e-12:
                    continue
                candidate_binding = refined_binding
                candidate_levels = levels
                candidate_wns = wns
                candidate_refinement_vector = list(legal_levels)
                config_path = refined_config_path
                break
        if candidate_levels is None:
            rejection_reasons.append(
                f"candidate_{candidate_position:02d}: "
                "exact-set/severity miss"
                + (
                    f" after {len(refinement_vectors)} deterministic refinements"
                    if refinement_vectors
                    else ""
                )
            )
            continue

        injection = []
        for operation, level in zip(
            candidate_binding["injection_operations"],
            candidate_levels,
            strict=True,
        ):
            injection.append(
                {
                    "kind": "injection",
                    "instance": operation["instance"],
                    "old_ref": operation["baseline_ref"],
                    "new_ref": operation["candidate_refs_nearest_first"][level],
                }
            )
        injection_by_instance = {
            row["instance"]: row["new_ref"] for row in injection
        }
        repair = []
        for operation in candidate_binding["repair_operations"]:
            repair.append(
                {
                    "kind": "repair",
                    "instance": operation["instance"],
                    "old_ref": injection_by_instance.get(
                        operation["instance"], operation["checkpoint_ref"]
                    ),
                    "new_ref": operation["new_ref"],
                }
            )
        candidate_plan = _read_frozen_plan_from_rows([*injection, *repair], spec)
        candidate_failed = False
        # A full exact-count repair closure is cheaper than N independent
        # sensitivity processes.  Reject a non-closing combination first,
        # then spend fresh processes proving every retained resize has >=1 ps
        # positive sensitivity.
        fetched = run_process("REPAIR", candidate_levels)
        for name in ("repair_before_refine.tsv", "repair_after_refine.tsv"):
            measured = slacks(fetched / name)
            negatives = [
                endpoint for endpoint, slack in measured.items() if slack < 0.0
            ]
            if negatives or any(
                measured.get(endpoint, -1.0) < 0.0 for endpoint in target_set
            ):
                rejection_reasons.append(
                    f"candidate_{candidate_position:02d}: closure miss at {name}"
                )
                candidate_failed = True
                break
        if candidate_failed:
            continue
        for index, operation in enumerate(repair):
            fetched = run_process(
                "SENSITIVITY", candidate_levels, repair_index=index
            )
            rows = _tsv(fetched / "sensitivity.tsv")
            if (
                {row["instance"] for row in rows} != {operation["instance"]}
                or max(float(row["delta_ns"]) for row in rows) < 0.001
            ):
                rejection_reasons.append(
                    f"candidate_{candidate_position:02d}: "
                    f"{operation['instance']} sensitivity miss"
                )
                candidate_failed = True
                break
        if candidate_failed:
            continue
        claimed = _claim_operation_fingerprints(
            run_dir,
            case_id,
            candidate_plan["injection_operations"],
            candidate_plan["repair_operations"],
        )
        if claimed is None:
            rejection_reasons.append(
                f"candidate_{candidate_position:02d}: "
                "batch operation fingerprint collision"
            )
            continue
        accepted_levels = candidate_levels
        accepted_wns = candidate_wns
        accepted_candidate_index = int(
            candidate.get("candidate_index", candidate_position)
        )
        selected_candidate_dir = current_candidate_dir
        selected_config_path = config_path
        accepted_refinement_vector = candidate_refinement_vector
        plan = candidate_plan
        break

    if (
        accepted_levels is None
        or accepted_candidate_index is None
        or selected_candidate_dir is None
        or selected_config_path is None
        or plan is None
    ):
        atomic_json(
            calibration / "rejections.json",
            {"case_id": case_id, "reasons": rejection_reasons},
            durable=True,
        )
        store.transition(
            case_id,
            "PROBE_ELIGIBLE",
            "candidate rejected by exact-set/severity calibration",
            slot=None,
        )
        raise CalibrationCandidateRejected(
            f"{case_id}: no endpoint-local calibration candidate met all gates"
        )

    atomic_json(
        calibration / "accepted_measurement.json",
        {
            "case_id": case_id,
            "candidate_index": accepted_candidate_index,
            "levels": accepted_levels,
            "refinement_legal_levels": accepted_refinement_vector,
            "wns_ns": accepted_wns,
            "process_count": process_index,
            "selected_evidence": str(selected_candidate_dir.relative_to(calibration)),
            "rejections": rejection_reasons,
        },
    )
    fix = _fix_bytes(plan["repair_operations"])
    fix_path = job / "fix.tcl"
    atomic_write(fix_path, fix)
    frozen = {
        "schema_version": "mock_lef_batch100.calibration_plan.v1",
        "case_id": case_id,
        "slot": f"slot{slot}",
        "attempt": attempt,
        "binding_sha256": sha256_file(binding_path),
        "calibration_tcl_sha256": sha256_file(TOOLS["calibrate"]),
        "config_sha256": sha256_file(selected_config_path),
        "selected_candidate_index": accepted_candidate_index,
        "selected_calibration_evidence": str(
            selected_candidate_dir.relative_to(calibration)
        ),
        "calibration_tree_sha256": tree_sha256(calibration),
        "fix_sha256": sha256_file(fix_path),
        **plan,
    }
    frozen_path = job / "calibration_plan.json"
    atomic_json(frozen_path, frozen, durable=True)

    # The calibration actor owns the complete freeze.  Materialize the exact
    # accepted injection once on this same slot, pull it home, and bind every
    # replay input before releasing the slot or publishing FROZEN.
    runtime_config = job / "runtime_config.tcl"
    _write_runtime_config(
        runtime_config,
        case_id,
        binding,
        plan,
        float(spec["nominal_severity"]["target_wns_ns"]),
        float(spec["nominal_severity"]["tolerance_ps"]) / 1000.0,
    )
    _scp_to(slot, TOOLS["runtime"], f"{remote_input}/runtime.tcl")
    _scp_to(slot, TOOLS["common"], f"{remote_input}/common.py")
    _scp_to(slot, runtime_config, f"{remote_input}/case_config.tcl")
    _scp_to(slot, fix_path, f"{remote_input}/fix.tcl")
    frozen_root, _ = _frozen_checkpoint_paths(job)
    frozen_root.mkdir()
    remote_freeze = f"{remote_task}/freeze"
    inject_command = (
        f"bash -lc 'cd {remote_input} && B100_MODE=INJECT "
        f"B100_TASK_DIR={remote_freeze} B100_BASELINE_DIR={GUEST_BASELINE} "
        f"B100_CASE_CONFIG={remote_input}/case_config.tcl "
        f"timeout 14400s innovus -no_gui -files runtime.tcl -log inject.log'"
    )
    injected = _ssh(slot, inject_command, timeout=14500)
    inject_log = frozen_root / "inject_innovus.log"
    atomic_write(inject_log, (injected.stdout or "").encode("utf-8"))
    injection_reports = frozen_root / "injection_reports_fetch"
    injection_reports.mkdir()
    injection_fetch_error: Exception | None = None
    try:
        _scp_from(
            slot,
            f"{remote_freeze}/reports",
            injection_reports,
            recursive=True,
        )
    except Exception as exc:
        injection_fetch_error = exc
    if injected.returncode or not _tool_process_ok(
        injected.stdout or "", f"B100_INJECTION_COMPLETE {case_id}"
    ):
        raise BatchError(
            f"{case_id}: injection/freeze failed; see {inject_log}"
            + (
                f"; evidence fetch also failed: {injection_fetch_error}"
                if injection_fetch_error is not None
                else ""
            )
        )
    if injection_fetch_error is not None:
        raise BatchError(
            f"{case_id}: injection/freeze passed but evidence fetch failed: "
            f"{injection_fetch_error}; see {inject_log}"
        )
    require_real_directory(
        injection_reports / "reports", f"{case_id} frozen injection reports"
    )

    remote_archive_hash = _pack_guest_checkpoint(slot, remote_freeze)
    local_archive = (
        frozen_root / checkpoint_archive.CHECKPOINT_ARCHIVE_NAME
    )
    _scp_from(
        slot,
        f"{remote_freeze}/{checkpoint_archive.CHECKPOINT_ARCHIVE_NAME}",
        local_archive,
    )
    if sha256_file(local_archive) != remote_archive_hash:
        raise BatchError(f"{case_id}: pulled frozen checkpoint archive hash mismatch")
    checkpoint_archive.safe_extract_checkpoint_archive(
        local_archive,
        frozen_root / "unpacked",
        baseline_prefix=GUEST_BASELINE,
        expected_sha256=remote_archive_hash,
    )
    _write_frozen_checkpoint_ready(
        run_dir,
        case_id,
        job,
        plan_path=frozen_path,
        binding_path=binding_path,
        config_path=runtime_config,
    )
    # This helper is also the resume path when exact remote cleanup fails after
    # the durable ready marker was published.
    return _finish_ready_frozen_checkpoint(run_dir, store, case_id)


def cmd_run(args: argparse.Namespace) -> None:
    store, manifest = _verified_store(args.run_dir)
    available_slots = _verify_execution_slots(manifest["baseline"])
    specs = read_json(SPECS_PATH)
    selected = _selection(specs, args.selection)
    if args.selection == "remaining":
        states = {row["case_id"]: row["state"] for row in store.rows()}
        not_validated = [case_id for case_id in specs["canary_case_ids"] if states[case_id] not in {"VALIDATED", "FINALIZED"}]
        if not_validated:
            raise BatchError(
                "remaining queue is gated until all canaries are VALIDATED: "
                + ", ".join(not_validated)
            )
    states = {case_id: store.row(case_id)["state"] for case_id in selected}
    interrupted = [
        case_id
        for case_id, state in states.items()
        if state in {"CALIBRATING", "REPLAYING"}
    ]
    if interrupted:
        raise BatchError(
            "run refuses ambiguous interrupted states; inspect/resume first: "
            + ", ".join(interrupted)
        )
    failures: list[str] = []
    rebound_now: set[str] = set()
    retry_blocked: set[str] = set()
    for case_id, state in states.items():
        if state != "PROBE_ELIGIBLE":
            continue
        try:
            _rebind_probe_eligible_case(
                args.run_dir,
                store,
                manifest,
                specs,
                case_id,
                available_slots,
            )
            rebound_now.add(case_id)
            print(f"{case_id}: rebound from immutable probe")
        except Exception as exc:
            failures.append(f"{case_id} rebind: {exc}")
    states = {case_id: store.row(case_id)["state"] for case_id in selected}

    # A prior process can stop after the local rebind but before calibration
    # dispatch.  Exact no-op cleanup makes that BOUND retry safe on any slot
    # that is healthy now, without touching another case leaf.
    for case_id, state in states.items():
        if (
            state == "BOUND"
            and case_id not in rebound_now
            and (args.run_dir / "rebind_audit" / case_id).is_dir()
        ):
            try:
                _clear_remote_retry_leaves(
                    available_slots, _run_id(args.run_dir), case_id
                )
            except Exception as exc:
                failures.append(f"{case_id} retry cleanup: {exc}")
                retry_blocked.add(case_id)

    runnable = [
        case_id
        for case_id, state in states.items()
        if state in {"BOUND", "FROZEN"} and case_id not in retry_blocked
    ]
    if not runnable:
        if failures:
            raise BatchError(
                f"{len(failures)} independent task(s) failed: "
                + " | ".join(failures)
            )
        raise BatchError(f"no runnable {args.selection} cases")
    by_id = {case["id"]: case for case in specs["cases"]}
    pending_calibration = deque(
        case_id for case_id in runnable if states[case_id] == "BOUND"
    )
    pending_replay: deque[str] = deque()
    replay_requirements: dict[str, tuple[int, int] | None] = {}
    for case_id in (
        value for value in runnable if states[value] == "FROZEN"
    ):
        assignment_path = _replay_assignment_path(
            args.run_dir / "jobs" / case_id
        )
        if assignment_path.exists() or assignment_path.is_symlink():
            try:
                _, assignment = _read_replay_assignment(
                    args.run_dir,
                    case_id,
                    require_real_directory(
                        args.run_dir / "jobs" / case_id,
                        f"{case_id} job",
                    ),
                )
                required = tuple(assignment["replay_slots"])
                if not set(required) <= set(available_slots):
                    raise BatchError(
                        "durably assigned replay slot is not healthy"
                    )
                replay_requirements[case_id] = required
            except Exception as exc:
                failures.append(
                    f"{case_id} replay assignment: {exc}"
                )
                continue
        else:
            replay_requirements[case_id] = None
        pending_replay.append(case_id)
    free_slots = set(available_slots)
    functional = _functional_pairs(args.run_dir)
    active: dict[Future[str], tuple[str, str, tuple[int, ...]]] = {}

    def submit_ready(executor: ThreadPoolExecutor) -> None:
        # Replays have priority and a crash-window assignment is immutable.
        # Scan past a temporarily blocked exact pair so unrelated cases keep
        # progressing instead of turning one busy assigned slot into a batch
        # barrier.
        while pending_replay:
            selected: tuple[str, tuple[int, int]] | None = None
            for _ in range(len(pending_replay)):
                case_id = pending_replay.popleft()
                required = replay_requirements[case_id]
                if required is None:
                    if len(free_slots) >= 2:
                        chosen = tuple(sorted(free_slots)[:2])
                        selected = (case_id, chosen)
                        break
                elif set(required) <= free_slots:
                    selected = (case_id, required)
                    break
                pending_replay.append(case_id)
            if selected is None:
                break
            case_id, (slot_a, slot_b) = selected
            free_slots.remove(slot_a)
            free_slots.remove(slot_b)
            future = executor.submit(
                _execute_one,
                args.run_dir,
                store,
                by_id[case_id],
                slot_a,
                slot_b,
                functional,
            )
            active[future] = ("replay", case_id, (slot_a, slot_b))

        # Preserve a currently free member of an immutable pair while its
        # other member is busy.  For an unassigned replay, preserve the lone
        # free slot only when the healthy pool can eventually supply two.
        reserved: set[int] = set()
        for case_id in pending_replay:
            required = replay_requirements[case_id]
            if required is not None:
                reserved.update(set(required) & free_slots)
        if (
            any(
                replay_requirements[case_id] is None
                for case_id in pending_replay
            )
            and len(free_slots) == 1
            and len(available_slots) >= 2
        ):
            reserved.update(free_slots)
        while pending_calibration and (free_slots - reserved):
            slot = min(free_slots - reserved)
            free_slots.remove(slot)
            case_id = pending_calibration.popleft()
            future = executor.submit(
                _calibrate_one,
                args.run_dir,
                store,
                by_id[case_id],
                slot,
            )
            active[future] = ("calibration", case_id, (slot,))

    with ThreadPoolExecutor(max_workers=len(available_slots)) as executor:
        while pending_calibration or pending_replay or active:
            submit_ready(executor)
            if not active:
                unresolved = [*pending_replay, *pending_calibration]
                if pending_replay and len(available_slots) == 1:
                    failures.append(
                        "awaiting a second healthy slot for strict dual replay: "
                        + ", ".join(unresolved)
                    )
                else:
                    failures.append(
                        "scheduler could not allocate healthy slots for: "
                        + ", ".join(unresolved)
                    )
                break
            completed, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in completed:
                stage, case_id, slots = active.pop(future)
                free_slots.update(slots)
                try:
                    message = future.result()
                    print(message)
                    if stage == "calibration":
                        if store.row(case_id)["state"] != "FROZEN":
                            raise BatchError(
                                f"{case_id}: calibration returned without FROZEN state"
                            )
                        replay_requirements[case_id] = None
                        pending_replay.append(case_id)
                except Exception as exc:
                    current = store.row(case_id)["state"]
                    if isinstance(exc, CalibrationCandidateRejected):
                        if (
                            stage != "calibration"
                            or current != "PROBE_ELIGIBLE"
                        ):
                            failures.append(
                                f"{case_id} calibration rejection state "
                                f"invariant: {current}"
                            )
                    failures.append(f"{case_id} {stage}: {exc}")

    if failures:
        raise BatchError(
            f"{len(failures)} independent task(s) failed: " + " | ".join(failures)
        )
    validated = [
        case_id
        for case_id in selected
        if store.row(case_id)["state"] in {"VALIDATED", "FINALIZED"}
    ]
    if len(validated) != len(selected):
        remaining = [
            case_id for case_id in selected if case_id not in set(validated)
        ]
        raise BatchError(
            f"{args.selection} run incomplete; unresolved: " + ", ".join(remaining)
        )
    print(f"run PASS: {len(validated)}/{len(selected)} {args.selection} cases validated")


def cmd_resume(args: argparse.Namespace) -> None:
    store, _ = _verified_store(args.run_dir)
    counts = Counter(row["state"] for row in store.rows())
    print("resume input-hash check PASS")
    rows = {row["case_id"]: row["state"] for row in store.rows()}
    specs = read_json(SPECS_PATH)
    state_machine = specs.get("state_machine", catalog.build_specs()["state_machine"])
    print(" ".join(f"{state}={counts.get(state, 0)}" for state in state_machine))
    if all(state == "PLANNED" for state in rows.values()):
        raise BatchError("resume requires the one-time probe before any case can run")
    by_id = {spec["id"]: spec for spec in specs["cases"]}
    recovery_failures: list[str] = []
    ambiguous_calibrations: list[str] = []
    for case_id, state in rows.items():
        if state != "CALIBRATING":
            continue
        _, frozen_ready = _frozen_checkpoint_paths(
            args.run_dir / "jobs" / case_id
        )
        if not frozen_ready.exists() or frozen_ready.is_symlink():
            ambiguous_calibrations.append(case_id)
            continue
        try:
            print(
                _finish_ready_frozen_checkpoint(
                    args.run_dir, store, case_id
                )
                + " (freeze promotion resume)"
            )
        except Exception as exc:
            recovery_failures.append(f"{case_id} freeze: {exc}")

    rows = {row["case_id"]: row["state"] for row in store.rows()}
    replaying = [
        case_id
        for case_id, state in rows.items()
        if state == "REPLAYING"
    ]
    ambiguous_replays: list[str] = []
    for case_id in replaying:
        _, ready = _case_staging_paths(args.run_dir, case_id)
        if not ready.exists() or ready.is_symlink():
            ambiguous_replays.append(case_id)
            continue
        try:
            print(
                _finish_ready_case_staging(
                    args.run_dir,
                    store,
                    by_id[case_id],
                    specs["acceptance"],
                )
                + " (cleanup-only resume)"
            )
        except Exception as exc:
            recovery_failures.append(f"{case_id}: {exc}")

    # A database commit can succeed before sidecar/marker cleanup.  Scan
    # VALIDATED cases for the durable ready marker and converge that window
    # without repeating remote cleanup.
    rows = {row["case_id"]: row["state"] for row in store.rows()}
    for case_id, state in rows.items():
        if state != "VALIDATED":
            continue
        _, ready = _case_staging_paths(args.run_dir, case_id)
        if not ready.exists() and not ready.is_symlink():
            continue
        if ready.is_symlink():
            recovery_failures.append(
                f"{case_id}: validated ready marker is a symlink"
            )
            continue
        try:
            slots = _finish_validated_case_markers(
                args.run_dir,
                store,
                by_id[case_id],
                specs["acceptance"],
            )
            print(
                f"{case_id}: VALIDATED slot{slots[0]}/slot{slots[1]} "
                "(post-commit marker resume)"
            )
        except Exception as exc:
            recovery_failures.append(f"{case_id} committed marker: {exc}")

    if ambiguous_calibrations or ambiguous_replays:
        raise BatchError(
            "resume stopped at interrupted tasks without a hash-bound complete "
            "ready marker; preserve/archive their exact local and remote leaves "
            "before an explicit retry: "
            + ", ".join(
                sorted(ambiguous_calibrations + ambiguous_replays)
            )
        )
    if recovery_failures:
        raise BatchError(
            "hash-bound recovery left cases in their committed safe states: "
            + " | ".join(recovery_failures)
        )
    rows = {row["case_id"]: row["state"] for row in store.rows()}
    canaries = specs["canary_case_ids"]
    if any(rows[case_id] not in {"VALIDATED", "FINALIZED"} for case_id in canaries):
        cmd_run(argparse.Namespace(run_dir=args.run_dir, selection="canary"))
        return
    remaining = [
        case["id"] for case in specs["cases"] if case["id"] not in set(canaries)
    ]
    if any(rows[case_id] not in {"VALIDATED", "FINALIZED"} for case_id in remaining):
        cmd_run(argparse.Namespace(run_dir=args.run_dir, selection="remaining"))
        return
    print("resume PASS: all 100 cases are already validated/finalized")


def cmd_status(args: argparse.Namespace) -> None:
    store, _ = _verified_store(args.run_dir)
    rows = store.rows()
    counts = Counter(row["state"] for row in rows)
    specs = read_json(SPECS_PATH, "case specs")
    for state in specs["state_machine"]:
        print(f"{state:14s} {counts.get(state, 0):3d}")
    active = [row for row in rows if row["state"] not in {"PLANNED", "FINALIZED"}]
    for row in active:
        print(
            f"{row['case_id']} {row['state']} slot={row['slot'] or '-'} "
            f"attempt={row['attempt']}"
        )


def cmd_fetch(args: argparse.Namespace) -> None:
    store, _ = _verified_store(args.run_dir)
    case_id = args.case_id
    if SAFE_ID_RE.fullmatch(case_id) is None:
        raise BatchError(f"unsafe case ID: {case_id!r}")
    row = store.row(case_id)
    local_case = args.run_dir / "cases" / case_id
    if local_case.is_dir():
        print(
            f"fetch PASS: {case_id} is already hash-checked locally at {local_case}"
        )
        return
    if not row["slot"]:
        raise BatchError(f"{case_id}: no assigned slot")
    slot_matches = re.fullmatch(
        r"slot([0-9])(?:,slot([0-9]))?", str(row["slot"])
    )
    if slot_matches is None:
        raise BatchError(f"{case_id}: malformed assigned slot set")
    slots = [
        int(value)
        for value in slot_matches.groups()
        if value is not None
    ]
    if len(slots) == 2:
        job = require_real_directory(
            args.run_dir / "jobs" / case_id, f"{case_id} job"
        )
        _, assignment = _read_replay_assignment(
            args.run_dir, case_id, job
        )
        if assignment["replay_slots"] != slots:
            raise BatchError(f"{case_id}: assigned slots changed")
    remote = f"{GUEST_RUN_ROOT}/{_run_id(args.run_dir)}/tasks/{case_id}"
    destination = args.run_dir / "fetched" / case_id
    if destination.exists() or destination.is_symlink():
        raise BatchError(f"fetch destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if len(slots) == 1:
        _scp_from(slots[0], remote, destination.parent, recursive=True)
        require_real_directory(destination, f"{case_id} fetched artifacts")
    else:
        destination.mkdir()
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            fetches = {
                executor.submit(
                    _scp_from,
                    slot,
                    remote,
                    destination / f"slot{slot}",
                    recursive=True,
                ): slot
                for slot in slots
            }
            for future in as_completed(fetches):
                try:
                    future.result()
                    require_real_directory(
                        destination / f"slot{fetches[future]}",
                        f"{case_id} slot{fetches[future]} fetched artifacts",
                    )
                except Exception as exc:
                    failures.append(f"slot{fetches[future]}: {exc}")
        if failures:
            raise BatchError(
                f"{case_id}: partial dual-slot fetch retained at "
                f"{destination}: " + " | ".join(sorted(failures))
            )
    print(
        f"fetch PASS: {case_id} from "
        + "/".join(f"slot{slot}" for slot in slots)
    )


def cmd_finalize(args: argparse.Namespace) -> None:
    store, _ = _verified_store(args.run_dir)
    states = {row["case_id"]: row["state"] for row in store.rows()}
    not_ready = [
        case_id
        for case_id, state in states.items()
        if state not in {"VALIDATED", "FINALIZED"}
    ]
    if not_ready:
        raise BatchError(
            "finalize requires every case to be VALIDATED or an already "
            f"strictly finalized case; {len(not_ready)} remain"
        )
    # Re-run the strict artifact finalizer even after a partially completed
    # state promotion.  Its writes are atomic and deterministic, so this
    # verifies the retained 100-case tree before converging any remaining
    # VALIDATED rows to FINALIZED.
    manifest = finalize_batch.finalize(args.run_dir)
    for case_id in sorted(states):
        if states[case_id] == "VALIDATED":
            store.transition(case_id, "FINALIZED", "strict batch finalizer")
    print(f"finalize PASS: {manifest['case_count']}/100 FINALIZED")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("catalog-check").set_defaults(func=cmd_catalog_check)
    init = sub.add_parser("init")
    init.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    init.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    init.set_defaults(func=cmd_init)
    probe = sub.add_parser("probe")
    probe.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    probe.set_defaults(func=cmd_probe)
    run = sub.add_parser("run")
    run.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    run.add_argument("--selection", choices=("canary", "remaining"), required=True)
    run.set_defaults(func=cmd_run)
    resume = sub.add_parser("resume")
    resume.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    resume.set_defaults(func=cmd_resume)
    status = sub.add_parser("status")
    status.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    status.set_defaults(func=cmd_status)
    fetch = sub.add_parser("fetch")
    fetch.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    fetch.add_argument("--case-id", required=True)
    fetch.set_defaults(func=cmd_fetch)
    final = sub.add_parser("finalize")
    final.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    final.set_defaults(func=cmd_finalize)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        args.func(args)
    except (
        BatchError,
        OSError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"batch100 {args.command} FAIL: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

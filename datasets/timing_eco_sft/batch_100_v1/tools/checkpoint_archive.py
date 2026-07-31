#!/usr/bin/env python3
"""Host-side fail-closed transport and hashing for Innovus checkpoints.

The production runner copies checkpoint tar files as single regular files.
This module deliberately does not use ``TarFile.extract*``: every member is
validated first and regular-file contents are copied without following links.
Guests receive only an archive that passed this host-side inspection, verify
the exact SHA-256, and extract it with their bundled GNU tar.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from common import BatchError, require_real_directory, require_real_file, sha256_file


CHECKPOINT_ARCHIVE_NAME = "violating.checkpoint.tar"
CHECKPOINT_TREE_HASH_ALGORITHM = "sha256-checkpoint-tree-v2"
CHECKPOINT_BUNDLE_HASH_ALGORITHM = "sha256-checkpoint-bundle-v1"
GUEST_BASELINE_PREFIX = (
    "/home/host/timing_eco_sft_pilot/baseline_qualified_portable_r1"
)
DEFAULT_MAX_MEMBERS = 100_000
DEFAULT_MAX_TOTAL_SIZE = 16 * 1024 * 1024 * 1024
DEFAULT_MAX_ARCHIVE_SIZE = 20 * 1024 * 1024 * 1024


def _archive_path(name: str, *, is_directory: bool) -> PurePosixPath:
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        raise BatchError(f"unsafe archive member name: {name!r}")
    raw = name[:-1] if is_directory and name.endswith("/") else name
    parts = raw.split("/")
    if (
        not raw
        or raw.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise BatchError(f"non-canonical archive member name: {name!r}")
    return PurePosixPath(*parts)


def _collapse_relative_link(parent: PurePosixPath, target: str) -> None:
    if not target or "\x00" in target or "\\" in target:
        raise BatchError(f"unsafe relative symlink target: {target!r}")
    depth = len(parent.parts)
    for part in target.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            depth -= 1
            if depth < 0:
                raise BatchError(f"relative symlink escapes checkpoint: {target!r}")
        else:
            depth += 1


def validate_symlink_target(
    member_path: PurePosixPath, target: str, baseline_prefix: str
) -> None:
    """Validate without resolving or dereferencing a symlink."""

    if not target or "\x00" in target or "\\" in target:
        raise BatchError(f"empty/invalid symlink target at {member_path}")
    if target.startswith("/"):
        target_parts = target.split("/")
        prefix = baseline_prefix.rstrip("/")
        prefix_parts = prefix.split("/")
        if (
            not prefix.startswith("/")
            or any(part in {"", ".", ".."} for part in target_parts[1:])
            or any(part in {"", ".", ".."} for part in prefix_parts[1:])
            or tuple(target_parts[1 : len(prefix_parts)])
            != tuple(prefix_parts[1:])
        ):
            raise BatchError(
                f"absolute symlink outside exact baseline prefix at "
                f"{member_path}: {target!r}"
            )
        return
    _collapse_relative_link(member_path.parent, target)


def _member_kind(member: tarfile.TarInfo) -> str:
    if member.isdir():
        return "directory"
    if member.isreg():
        return "file"
    if member.issym():
        return "symlink"
    if member.islnk():
        raise BatchError(f"hardlink forbidden in checkpoint archive: {member.name}")
    raise BatchError(f"special member forbidden in checkpoint archive: {member.name}")


def inspect_checkpoint_archive(
    archive: Path,
    *,
    baseline_prefix: str,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_total_size: int = DEFAULT_MAX_TOTAL_SIZE,
    max_archive_size: int = DEFAULT_MAX_ARCHIVE_SIZE,
) -> list[tuple[tarfile.TarInfo, PurePosixPath, str]]:
    """Validate a checkpoint archive completely before extraction."""

    require_real_file(archive, "checkpoint archive")
    if archive.stat().st_size > max_archive_size:
        raise BatchError("checkpoint archive exceeds compressed/on-disk size limit")
    if max_members <= 0 or max_total_size < 0:
        raise BatchError("invalid checkpoint archive safety limits")

    rows: list[tuple[tarfile.TarInfo, PurePosixPath, str]] = []
    kinds: dict[PurePosixPath, str] = {}
    total_size = 0
    try:
        with tarfile.open(archive, mode="r:") as stream:
            members = stream.getmembers()
    except (tarfile.TarError, OSError) as exc:
        raise BatchError(f"invalid uncompressed checkpoint tar: {exc}") from exc
    if len(members) > max_members:
        raise BatchError("checkpoint archive member-count limit exceeded")

    for member in members:
        kind = _member_kind(member)
        path = _archive_path(member.name, is_directory=kind == "directory")
        if path in kinds:
            raise BatchError(f"duplicate archive member: {path}")
        for parent in path.parents:
            if str(parent) == ".":
                continue
            if kinds.get(parent) not in {None, "directory"}:
                raise BatchError(f"archive member descends through non-directory: {path}")
        if kind != "directory" and any(
            known != path and path in known.parents for known in kinds
        ):
            raise BatchError(f"archive member conflicts with existing descendant: {path}")
        if kind == "file":
            if member.size < 0:
                raise BatchError(f"negative archive member size: {path}")
            total_size += member.size
            if total_size > max_total_size:
                raise BatchError("checkpoint archive expanded-size limit exceeded")
        elif kind == "symlink":
            validate_symlink_target(path, member.linkname, baseline_prefix)
        kinds[path] = kind
        rows.append((member, path, kind))

    required = {
        PurePosixPath("violating.enc"): "file",
        PurePosixPath("violating.enc.dat"): "directory",
    }
    for path, kind in required.items():
        if kinds.get(path) != kind:
            raise BatchError(f"checkpoint archive lacks required {kind}: {path}")
    allowed_roots = set(required)
    for path in kinds:
        if path not in allowed_roots and PurePosixPath("violating.enc.dat") not in path.parents:
            raise BatchError(f"unexpected top-level checkpoint archive member: {path}")
    if not any(PurePosixPath("violating.enc.dat") in path.parents for path in kinds):
        raise BatchError("checkpoint data tree is empty")
    binary_members = [
        path
        for path, kind in kinds.items()
        if (
            "vbin" in path.parts
            or (kind == "file" and path.name.endswith(".v.bin"))
        )
    ]
    if binary_members:
        raise BatchError(
            "binary Innovus netlist forbidden in portable checkpoint archive: "
            + ", ".join(path.as_posix() for path in sorted(binary_members))
        )
    ascii_netlists = [
        path
        for path, kind in kinds.items()
        if (
            kind == "file"
            and path.parent == PurePosixPath("violating.enc.dat")
            and path.name.endswith(".v.gz")
        )
    ]
    if len(ascii_netlists) != 1:
        raise BatchError(
            "portable checkpoint archive must contain exactly one top-level "
            "ASCII .v.gz netlist"
        )
    return rows


def safe_extract_checkpoint_archive(
    archive: Path,
    destination: Path,
    *,
    baseline_prefix: str,
    expected_sha256: str | None = None,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_total_size: int = DEFAULT_MAX_TOTAL_SIZE,
    max_archive_size: int = DEFAULT_MAX_ARCHIVE_SIZE,
) -> dict[str, Any]:
    """Preflight, then extract to a new directory without following links."""

    if expected_sha256 is not None and sha256_file(archive) != expected_sha256:
        raise BatchError("checkpoint archive SHA-256 mismatch")
    rows = inspect_checkpoint_archive(
        archive,
        baseline_prefix=baseline_prefix,
        max_members=max_members,
        max_total_size=max_total_size,
        max_archive_size=max_archive_size,
    )
    if destination.exists() or destination.is_symlink():
        raise BatchError(f"checkpoint extraction destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(mode=0o700)
    try:
        with tarfile.open(archive, mode="r:") as stream:
            # Directories first; validation established that none is a symlink.
            for _, relative, kind in sorted(rows, key=lambda row: len(row[1].parts)):
                if kind == "directory":
                    (destination / relative.as_posix()).mkdir(parents=True, exist_ok=False)
            for member, relative, kind in rows:
                target = destination / relative.as_posix()
                if kind == "directory":
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if kind == "symlink":
                    os.symlink(member.linkname, target)
                    continue
                source = stream.extractfile(member)
                if source is None:
                    raise BatchError(f"cannot read archive member: {relative}")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                descriptor = os.open(target, flags, 0o600)
                try:
                    with os.fdopen(descriptor, "wb") as output:
                        shutil.copyfileobj(source, output, 1024 * 1024)
                finally:
                    source.close()
                if target.stat().st_size != member.size:
                    raise BatchError(f"short extraction for archive member: {relative}")
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return {
        "archive_sha256": sha256_file(archive),
        "archive_bytes": archive.stat().st_size,
        "member_count": len(rows),
        "expanded_bytes": sum(member.size for member, _, kind in rows if kind == "file"),
    }


def checkpoint_tree_ledger_v2(
    root: Path, *, baseline_prefix: str
) -> list[dict[str, Any]]:
    """Describe directories, regular files and safe symlinks without dereference."""

    require_real_directory(root, "checkpoint data tree")
    rows: list[dict[str, Any]] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            mode = path.lstat().st_mode
            relative = path.relative_to(root).as_posix()
            if stat.S_ISDIR(mode):
                rows.append({"path": relative, "type": "directory"})
                stack.append(path)
            elif stat.S_ISREG(mode):
                rows.append(
                    {
                        "path": relative,
                        "type": "file",
                        "bytes": path.stat().st_size,
                        "sha256": sha256_file(path),
                    }
                )
            elif stat.S_ISLNK(mode):
                link = os.readlink(path)
                validate_symlink_target(PurePosixPath(relative), link, baseline_prefix)
                rows.append({"path": relative, "type": "symlink", "target": link})
            else:
                raise BatchError(f"checkpoint tree contains a special file: {path}")
    rows.sort(key=lambda row: row["path"])
    symlinks = {
        PurePosixPath(row["path"])
        for row in rows
        if row["type"] == "symlink"
    }
    for row in rows:
        path = PurePosixPath(row["path"])
        if any(parent in symlinks for parent in path.parents):
            raise BatchError(f"checkpoint entry descends through symlink: {path}")
    return rows


def checkpoint_tree_sha256_v2(root: Path, *, baseline_prefix: str) -> str:
    digest = hashlib.sha256()
    digest.update((CHECKPOINT_TREE_HASH_ALGORITHM + "\n").encode())
    for row in checkpoint_tree_ledger_v2(root, baseline_prefix=baseline_prefix):
        digest.update(row["path"].encode())
        digest.update(b"\0")
        digest.update(row["type"].encode())
        digest.update(b"\0")
        if row["type"] == "file":
            digest.update(str(row["bytes"]).encode())
            digest.update(b"\0")
            digest.update(row["sha256"].encode())
        elif row["type"] == "symlink":
            digest.update(row["target"].encode())
        digest.update(b"\n")
    return digest.hexdigest()


def checkpoint_bundle_sha256_v1_from_hashes(
    wrapper_sha256: str, tree_sha256: str
) -> str:
    """Bind the two retained checkpoint artifacts without a transport tar.

    The encoding is deliberately domain-separated and labels both components.
    Inputs are lower-case SHA-256 hex digests so the result can be recomputed
    from a finalized case directory on any host.
    """

    for value, label in (
        (wrapper_sha256, "violating.enc"),
        (tree_sha256, "violating.enc.dat"),
    ):
        if (
            not isinstance(value, str)
            or len(value) != 64
            or value.lower() != value
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise BatchError(f"{label} SHA-256 is not canonical lower-case hex")
    digest = hashlib.sha256()
    digest.update((CHECKPOINT_BUNDLE_HASH_ALGORITHM + "\n").encode())
    digest.update(b"violating.enc\0")
    digest.update(wrapper_sha256.encode())
    digest.update(b"\nviolating.enc.dat\0")
    digest.update(CHECKPOINT_TREE_HASH_ALGORITHM.encode())
    digest.update(b"\0")
    digest.update(tree_sha256.encode())
    digest.update(b"\n")
    return digest.hexdigest()


def checkpoint_bundle_sha256_v1(
    wrapper: Path, tree: Path, *, baseline_prefix: str
) -> str:
    """Hash the canonical retained ``violating.enc{,.dat}`` bundle."""

    require_real_file(wrapper, "checkpoint wrapper")
    tree_sha256 = checkpoint_tree_sha256_v2(
        tree, baseline_prefix=baseline_prefix
    )
    return checkpoint_bundle_sha256_v1_from_hashes(
        sha256_file(wrapper), tree_sha256
    )


def _main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=("verify-extract", "tree-hash", "bundle-hash")
    )
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--tree", type=Path)
    parser.add_argument("--wrapper", type=Path)
    parser.add_argument("--baseline-prefix", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify-extract":
            if args.archive is None or args.destination is None or args.expected_sha256 is None:
                raise BatchError("verify-extract requires archive, destination, and hash")
            result = safe_extract_checkpoint_archive(
                args.archive,
                args.destination,
                baseline_prefix=args.baseline_prefix,
                expected_sha256=args.expected_sha256,
            )
            print(
                "B100_CHECKPOINT_ARCHIVE_EXTRACT_PASS "
                f"{result['archive_sha256']} {result['member_count']} "
                f"{result['expanded_bytes']}"
            )
        elif args.command == "tree-hash":
            if args.tree is None:
                raise BatchError("tree-hash requires --tree")
            print(
                checkpoint_tree_sha256_v2(
                    args.tree, baseline_prefix=args.baseline_prefix
                )
            )
        else:
            if args.wrapper is None or args.tree is None:
                raise BatchError("bundle-hash requires --wrapper and --tree")
            print(
                checkpoint_bundle_sha256_v1(
                    args.wrapper,
                    args.tree,
                    baseline_prefix=args.baseline_prefix,
                )
            )
        return 0
    except (BatchError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())

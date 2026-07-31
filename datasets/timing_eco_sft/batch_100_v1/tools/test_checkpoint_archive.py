#!/usr/bin/env python3
"""Focused archive transport tests; no SSH or EDA process is started."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import batch100
import catalog
import checkpoint_archive
import finalize_batch
from common import BatchError, sha256_file
from test_batch100 import _fixture


BASELINE = batch100.GUEST_BASELINE


def _tar(path: Path, extra: list[tarfile.TarInfo] | None = None) -> None:
    with tarfile.open(path, "w") as stream:
        wrapper = tarfile.TarInfo("violating.enc")
        wrapper.size = 8
        stream.addfile(wrapper, io.BytesIO(b"wrapper\n"))
        data = tarfile.TarInfo("violating.enc.dat")
        data.type = tarfile.DIRTYPE
        stream.addfile(data)
        payload = tarfile.TarInfo("violating.enc.dat/top.v.gz")
        payload.size = 3
        stream.addfile(payload, io.BytesIO(b"db\n"))
        for member in extra or []:
            content = b"x" * member.size if member.isreg() else None
            stream.addfile(member, io.BytesIO(content) if content is not None else None)


class CheckpointArchiveTests(unittest.TestCase):
    def test_bundle_hash_is_recomputable_from_retained_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wrapper = root / "violating.enc"
            tree = root / "violating.enc.dat"
            tree.mkdir()
            wrapper.write_bytes(b"source violating.enc.dat/top.db\n")
            (tree / "top.db").write_bytes(b"db")
            wrapper_hash = sha256_file(wrapper)
            tree_hash = checkpoint_archive.checkpoint_tree_sha256_v2(
                tree, baseline_prefix=BASELINE
            )
            expected = (
                checkpoint_archive.checkpoint_bundle_sha256_v1_from_hashes(
                    wrapper_hash, tree_hash
                )
            )
            self.assertEqual(
                checkpoint_archive.checkpoint_bundle_sha256_v1(
                    wrapper, tree, baseline_prefix=BASELINE
                ),
                expected,
            )
            (tree / "top.db").write_bytes(b"changed")
            self.assertNotEqual(
                checkpoint_archive.checkpoint_bundle_sha256_v1(
                    wrapper, tree, baseline_prefix=BASELINE
                ),
                expected,
            )

    def test_roundtrip_preserves_links_and_v2_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "checkpoint.tar"
            relative = tarfile.TarInfo("violating.enc.dat/local.link")
            relative.type = tarfile.SYMTYPE
            relative.linkname = "top.v.gz"
            absolute = tarfile.TarInfo("violating.enc.dat/base.link")
            absolute.type = tarfile.SYMTYPE
            absolute.linkname = f"{BASELINE}/base.enc.dat/top.db"
            _tar(archive, [relative, absolute])
            digest = sha256_file(archive)

            first = root / "first"
            result = checkpoint_archive.safe_extract_checkpoint_archive(
                archive,
                first,
                baseline_prefix=BASELINE,
                expected_sha256=digest,
            )
            self.assertEqual(result["archive_sha256"], digest)
            self.assertTrue((first / "violating.enc.dat/local.link").is_symlink())
            self.assertEqual(
                os.readlink(first / "violating.enc.dat/base.link"),
                f"{BASELINE}/base.enc.dat/top.db",
            )
            first_hash = checkpoint_archive.checkpoint_tree_sha256_v2(
                first / "violating.enc.dat", baseline_prefix=BASELINE
            )

            # The exact same verified single file models the replay2 upload.
            second_archive = root / "replay2.tar"
            second_archive.write_bytes(archive.read_bytes())
            self.assertEqual(sha256_file(second_archive), digest)
            second = root / "second"
            checkpoint_archive.safe_extract_checkpoint_archive(
                second_archive,
                second,
                baseline_prefix=BASELINE,
                expected_sha256=digest,
            )
            self.assertEqual(
                checkpoint_archive.checkpoint_tree_sha256_v2(
                    second / "violating.enc.dat", baseline_prefix=BASELINE
                ),
                first_hash,
            )

    def test_rejects_hash_mismatch_and_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "checkpoint.tar"
            _tar(archive)
            with self.assertRaisesRegex(BatchError, "SHA-256 mismatch"):
                checkpoint_archive.safe_extract_checkpoint_archive(
                    archive,
                    root / "out",
                    baseline_prefix=BASELINE,
                    expected_sha256="0" * 64,
                )
            (root / "out").mkdir()
            with self.assertRaisesRegex(BatchError, "already exists"):
                checkpoint_archive.safe_extract_checkpoint_archive(
                    archive, root / "out", baseline_prefix=BASELINE
                )

    def test_rejects_unsafe_names_duplicates_and_conflicts(self) -> None:
        cases = []
        traversal = tarfile.TarInfo("../escape")
        traversal.size = 1
        cases.append((traversal, "non-canonical"))
        absolute = tarfile.TarInfo("/escape")
        absolute.size = 1
        cases.append((absolute, "non-canonical"))
        duplicate = tarfile.TarInfo("violating.enc.dat/top.v.gz")
        duplicate.size = 1
        cases.append((duplicate, "duplicate"))
        conflict = tarfile.TarInfo("violating.enc.dat/top.v.gz/child")
        conflict.size = 1
        cases.append((conflict, "descends through non-directory"))
        for member, message in cases:
            with self.subTest(member=member.name), tempfile.TemporaryDirectory() as temporary:
                archive = Path(temporary) / "checkpoint.tar"
                _tar(archive, [member])
                with self.assertRaisesRegex(BatchError, message):
                    checkpoint_archive.inspect_checkpoint_archive(
                        archive, baseline_prefix=BASELINE
                    )

    def test_rejects_hardlink_fifo_device_and_symlink_descendant(self) -> None:
        members = []
        hardlink = tarfile.TarInfo("violating.enc.dat/hard")
        hardlink.type = tarfile.LNKTYPE
        hardlink.linkname = "violating.enc.dat/top.db"
        members.append((hardlink, "hardlink"))
        fifo = tarfile.TarInfo("violating.enc.dat/fifo")
        fifo.type = tarfile.FIFOTYPE
        members.append((fifo, "special"))
        device = tarfile.TarInfo("violating.enc.dat/device")
        device.type = tarfile.CHRTYPE
        members.append((device, "special"))
        for member, message in members:
            with self.subTest(member=member.name), tempfile.TemporaryDirectory() as temporary:
                archive = Path(temporary) / "checkpoint.tar"
                _tar(archive, [member])
                with self.assertRaisesRegex(BatchError, message):
                    checkpoint_archive.inspect_checkpoint_archive(
                        archive, baseline_prefix=BASELINE
                    )

        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "checkpoint.tar"
            link = tarfile.TarInfo("violating.enc.dat/link")
            link.type = tarfile.SYMTYPE
            link.linkname = "top.db"
            child = tarfile.TarInfo("violating.enc.dat/link/child")
            child.size = 1
            _tar(archive, [link, child])
            with self.assertRaisesRegex(BatchError, "descends through non-directory"):
                checkpoint_archive.inspect_checkpoint_archive(
                    archive, baseline_prefix=BASELINE
                )

    def test_rejects_binary_or_missing_ascii_checkpoint_netlist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "binary.tar"
            vbin = tarfile.TarInfo("violating.enc.dat/vbin")
            vbin.type = tarfile.DIRTYPE
            payload = tarfile.TarInfo("violating.enc.dat/vbin/top.v.bin")
            payload.size = 3
            _tar(binary, [vbin, payload])
            with self.assertRaisesRegex(BatchError, "binary Innovus netlist"):
                checkpoint_archive.inspect_checkpoint_archive(
                    binary, baseline_prefix=BASELINE
                )

            duplicate_ascii = root / "duplicate-ascii.tar"
            second = tarfile.TarInfo("violating.enc.dat/other.v.gz")
            second.size = 3
            _tar(duplicate_ascii, [second])
            with self.assertRaisesRegex(BatchError, "exactly one"):
                checkpoint_archive.inspect_checkpoint_archive(
                    duplicate_ascii, baseline_prefix=BASELINE
                )

    def test_rejects_bad_symlink_targets(self) -> None:
        targets = (
            "/home/host/timing_eco_sft_pilot/baseline_qualified_portable_r10/x",
            "/etc/passwd",
            f"{BASELINE}/../escape",
            f"{BASELINE}/sub/../../escape",
            "../../../escape",
        )
        for target in targets:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                archive = Path(temporary) / "checkpoint.tar"
                link = tarfile.TarInfo("violating.enc.dat/link")
                link.type = tarfile.SYMTYPE
                link.linkname = target
                _tar(archive, [link])
                with self.assertRaises(BatchError):
                    checkpoint_archive.inspect_checkpoint_archive(
                        archive, baseline_prefix=BASELINE
                    )

    def test_limits_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "checkpoint.tar"
            extra = tarfile.TarInfo("violating.enc.dat/extra")
            extra.size = 4
            _tar(archive, [extra])
            with self.assertRaisesRegex(BatchError, "member-count"):
                checkpoint_archive.inspect_checkpoint_archive(
                    archive, baseline_prefix=BASELINE, max_members=3
                )
            with self.assertRaisesRegex(BatchError, "expanded-size"):
                checkpoint_archive.inspect_checkpoint_archive(
                    archive, baseline_prefix=BASELINE, max_total_size=4
                )
            with self.assertRaisesRegex(BatchError, "on-disk size"):
                checkpoint_archive.inspect_checkpoint_archive(
                    archive, baseline_prefix=BASELINE, max_archive_size=1
                )

    def test_tree_hash_v2_binds_symlink_target_and_rejects_special(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary) / "tree"
            tree.mkdir()
            (tree / "data").write_bytes(b"x")
            os.symlink("data", tree / "link")
            first = checkpoint_archive.checkpoint_tree_sha256_v2(
                tree, baseline_prefix=BASELINE
            )
            (tree / "link").unlink()
            os.symlink(f"{BASELINE}/data", tree / "link")
            second = checkpoint_archive.checkpoint_tree_sha256_v2(
                tree, baseline_prefix=BASELINE
            )
            self.assertNotEqual(first, second)
            (tree / "pipe").unlink(missing_ok=True)
            os.mkfifo(tree / "pipe")
            with self.assertRaisesRegex(BatchError, "special file"):
                checkpoint_archive.checkpoint_tree_sha256_v2(
                    tree, baseline_prefix=BASELINE
                )

    def test_guest_pack_is_tar_without_dereference_and_parses_one_hash(self) -> None:
        digest = hashlib.sha256(b"archive").hexdigest()
        completed = type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": (
                    f"{digest}  {checkpoint_archive.CHECKPOINT_ARCHIVE_NAME}\n"
                ),
            },
        )()
        with patch.object(batch100, "_ssh", return_value=completed) as ssh:
            self.assertEqual(batch100._pack_guest_checkpoint(3, "/exact/inject"), digest)
        command = ssh.call_args.args[1]
        self.assertIn("cd /exact/inject", command)
        self.assertIn("tar -cf", command)
        self.assertNotIn("-h", command)
        self.assertNotIn("--dereference", command)
        self.assertIn("sha256sum", command)

    def test_guest_extract_uses_verified_tar_without_python(self) -> None:
        digest = hashlib.sha256(b"archive").hexdigest()
        completed = type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": (
                    f"{checkpoint_archive.CHECKPOINT_ARCHIVE_NAME}: OK\n"
                    f"B100_CHECKPOINT_ARCHIVE_EXTRACT_PASS {digest}\n"
                ),
            },
        )()
        with patch.object(batch100, "_ssh", return_value=completed) as ssh:
            batch100._extract_guest_checkpoint_archive(
                4, "/exact/checkpoint", digest
            )
        command = ssh.call_args.args[1]
        self.assertNotIn("python", command)
        self.assertIn("sha256sum -c -", command)
        self.assertIn("tar --no-same-owner --no-same-permissions", command)
        self.assertIn("test ! -e /exact/checkpoint/unpacked", command)

        ambiguous = type(
            "Completed",
            (),
            {
                "returncode": 0,
                "stdout": (
                    f"B100_CHECKPOINT_ARCHIVE_EXTRACT_PASS {digest}\n"
                    f"B100_CHECKPOINT_ARCHIVE_EXTRACT_PASS {digest}\n"
                ),
            },
        )()
        with (
            patch.object(batch100, "_ssh", return_value=ambiguous),
            self.assertRaises(BatchError),
        ):
            batch100._extract_guest_checkpoint_archive(
                4, "/exact/checkpoint", digest
            )

    def test_production_path_has_no_recursive_checkpoint_scp(self) -> None:
        source = Path(batch100.__file__).read_text(encoding="utf-8")
        execution = source[source.index("def _execute_one(") : source.index("def _calibrate_one(")]
        calibration = source[source.index("def _calibrate_one(") : source.index("def cmd_run(")]
        self.assertNotIn("_scp_tree_to(", execution)
        self.assertNotIn("_pack_guest_checkpoint(", execution)
        self.assertIn("_extract_guest_checkpoint_archive(", execution)
        self.assertIn("_pack_guest_checkpoint(", calibration)
        self.assertIn("_write_frozen_checkpoint_ready(", calibration)

    def test_finalizer_requires_bundle_hash_schema_and_no_retained_tar(self) -> None:
        specs = __import__("json").loads(catalog.SPECS.read_text())
        spec = dict(specs["cases"][0])
        spec.update(
            target_endpoint_count=1,
            protected_endpoint_count=0,
            expected_modification_count=1,
        )
        spec["nominal_severity"] = {
            "ps": 60,
            "tolerance_ps": 10,
            "percent_of_clock": 0.75,
            "target_wns_ns": -0.06,
        }
        with tempfile.TemporaryDirectory() as temporary:
            case, validation = _fixture(Path(temporary), spec)
            finalize_batch.validate_case(case, spec, specs["acceptance"])

            original = validation["replays"][1][
                "violating_checkpoint_bundle_sha256"
            ]
            validation["replays"][1]["violating_checkpoint_bundle_sha256"] = "9" * 64
            (case / "validation.json").write_text(__import__("json").dumps(validation))
            with self.assertRaisesRegex(BatchError, "bundle hash mismatch"):
                finalize_batch.validate_case(case, spec, specs["acceptance"])

            validation["replays"][1]["violating_checkpoint_bundle_sha256"] = original
            (case / "validation.json").write_text(__import__("json").dumps(validation))
            (case / checkpoint_archive.CHECKPOINT_ARCHIVE_NAME).write_bytes(b"transient")
            with self.assertRaisesRegex(BatchError, "transport bundle retained"):
                finalize_batch.validate_case(case, spec, specs["acceptance"])

    def test_finalizer_rejects_duplicate_checkpoint_under_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            duplicate = root / "jobs/B1_CASE_001/inject/violating.enc.dat"
            duplicate.mkdir(parents=True)
            with self.assertRaisesRegex(BatchError, "non-canonical checkpoint retained"):
                finalize_batch._reject_checkpoint_residue(root)


if __name__ == "__main__":
    unittest.main()

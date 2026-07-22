#!/usr/bin/env python3
"""Tests for constrained fetched-checkpoint symlink materialization."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("materialize_checkpoint_links.py")
SPEC = importlib.util.spec_from_file_location(
    "timing_eco_materialize_checkpoint_links", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
materializer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(materializer)


class MaterializeCheckpointLinksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / "run-1"
        self.baseline = self.root / "baseline"
        self.guest_root = "/tmp/nvdla_sft"
        self.case_a = self.run / "cases" / "CASE_A"
        self.case_b = self.run / "cases" / "CASE_B"
        self.replay = self.case_a / "runs" / "replay_1"
        self.checkpoint = self.replay / "violating.enc.dat"

        (self.baseline / "base.enc.dat/libs").mkdir(parents=True)
        (self.baseline / "base.enc.dat/lef").mkdir(parents=True)
        (self.baseline / "base.enc.dat/libs/a.lib").write_bytes(b"alpha library\n")
        (self.baseline / "base.enc.dat/lef/tech.lef").write_bytes(b"technology lef\n")
        self.checkpoint.mkdir(parents=True)
        (self.checkpoint / "db.bin").write_bytes(b"checkpoint-owned\n")
        (self.case_b / "runs").mkdir(parents=True)
        self._write_status(self.replay, passed=True, fetched=True)
        self._write_checksums()
        self._write_manifest()
        self._set_link(
            "libs/a.lib",
            f"{self.guest_root}/run-1/baseline/base.enc.dat/libs/a.lib",
        )

    @staticmethod
    def _sha(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def _baseline_entries(self) -> dict[str, str]:
        entries: dict[str, str] = {}
        for path in sorted(self.baseline.rglob("*")):
            if path.is_file() and not path.is_symlink():
                relative = path.relative_to(self.baseline).as_posix()
                entries[relative] = self._sha(path.read_bytes())
        return entries

    def _write_checksums(self, entries: dict[str, str] | None = None) -> None:
        entries = self._baseline_entries() if entries is None else entries
        text = "".join(
            f"{digest}  {relative}\n" for relative, digest in sorted(entries.items())
        )
        self.run.mkdir(parents=True, exist_ok=True)
        (self.run / "baseline_checksums.sha256").write_text(text, encoding="utf-8")

    def _write_manifest(self, **updates: object) -> None:
        checksum = (self.run / "baseline_checksums.sha256").read_bytes()
        manifest: dict[str, object] = {
            "schema_version": "timing_eco_pilot_run.v1",
            "run_id": "run-1",
            "guest_root": self.guest_root,
            "case_ids": ["CASE_A", "CASE_B"],
            "baseline": {
                "source": str(self.baseline),
                "sha256": "a" * 64,
                "checksum_manifest": {
                    "path": "baseline_checksums.sha256",
                    "sha256": self._sha(checksum),
                },
            },
        }
        manifest.update(updates)
        self._write_json(self.run / "run_manifest.json", manifest)

    def _refresh_manifest_checksum(self) -> None:
        manifest = json.loads((self.run / "run_manifest.json").read_text(encoding="utf-8"))
        manifest["baseline"]["checksum_manifest"]["sha256"] = self._sha(
            (self.run / "baseline_checksums.sha256").read_bytes()
        )
        self._write_json(self.run / "run_manifest.json", manifest)

    def _write_status(
        self,
        replay: Path,
        *,
        passed: bool,
        fetched: bool,
        replay_index: int = 1,
        exit_code: int = 0,
    ) -> None:
        replay.mkdir(parents=True, exist_ok=True)
        self._write_json(
            replay / "run_status.json",
            {
                "replay": replay_index,
                "passed": passed,
                "fetched": fetched,
                "innovus_exit_code": exit_code,
            },
        )

    def _set_link(self, relative: str, target: str) -> Path:
        path = self.checkpoint / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() or path.is_symlink():
            path.unlink()
        os.symlink(target, path)
        return path

    def test_materializes_and_idempotently_revalidates(self) -> None:
        reports = materializer.materialize_run(self.run, ["CASE_A"])
        self.assertEqual(reports, [self.replay / materializer.REPORT_NAME])
        destination = self.checkpoint / "libs/a.lib"
        self.assertFalse(destination.is_symlink())
        self.assertTrue(stat.S_ISREG(destination.lstat().st_mode))
        self.assertEqual(destination.read_bytes(), b"alpha library\n")
        self.assertEqual(
            stat.S_IMODE(destination.stat().st_mode),
            stat.S_IMODE((self.baseline / "base.enc.dat/libs/a.lib").stat().st_mode),
        )

        report_path = reports[0]
        first_bytes = report_path.read_bytes()
        report = json.loads(first_bytes)
        self.assertEqual(
            report["schema_version"],
            "timing_eco_checkpoint_link_materialization.v1",
        )
        self.assertEqual(report["case_id"], "CASE_A")
        self.assertEqual(report["replay"], 1)
        self.assertEqual(report["checkpoint"], "violating.enc.dat")
        self.assertEqual(report["baseline"]["host_source"], str(self.baseline))
        self.assertEqual(
            report["baseline"]["guest_checkpoint_root"],
            f"{self.guest_root}/run-1/baseline/base.enc.dat",
        )
        self.assertEqual(
            report["materialized_links"],
            [
                {
                    "path": "libs/a.lib",
                    "guest_target": (
                        f"{self.guest_root}/run-1/baseline/base.enc.dat/libs/a.lib"
                    ),
                    "baseline_relative": "libs/a.lib",
                    "sha256": self._sha(b"alpha library\n"),
                    "bytes": len(b"alpha library\n"),
                }
            ],
        )
        self.assertEqual(first_bytes, materializer._json_bytes(report))

        self.assertEqual(
            materializer.materialize_run(self.run, ["CASE_A"]), reports
        )
        self.assertEqual(report_path.read_bytes(), first_bytes)

    def test_interrupted_copy_resumes_from_durable_pending_journal(self) -> None:
        self._set_link(
            "lef/tech.lef",
            f"{self.guest_root}/run-1/baseline/base.enc.dat/lef/tech.lef",
        )
        real_copy = materializer._atomic_copy
        copied_paths: list[str] = []

        def interrupt_after_first(plan: object) -> None:
            if copied_paths:
                raise RuntimeError("simulated process interruption")
            real_copy(plan)
            copied_paths.append(plan.path)

        with mock.patch.object(
            materializer, "_atomic_copy", side_effect=interrupt_after_first
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated process interruption"):
                materializer.materialize_run(self.run, ["CASE_A"])

        pending = self.replay / materializer.PENDING_REPORT_NAME
        final = self.replay / materializer.REPORT_NAME
        self.assertTrue(pending.is_file())
        self.assertFalse(final.exists())
        self.assertEqual(len(copied_paths), 1)
        copied = self.checkpoint / copied_paths[0]
        remaining_relative = (
            "libs/a.lib" if copied_paths[0] == "lef/tech.lef" else "lef/tech.lef"
        )
        remaining = self.checkpoint / remaining_relative
        self.assertTrue(copied.is_file())
        self.assertFalse(copied.is_symlink())
        self.assertTrue(remaining.is_symlink())

        self.assertEqual(
            materializer.materialize_run(self.run, ["CASE_A"]), [final]
        )
        self.assertFalse(pending.exists())
        self.assertTrue(final.is_file())
        self.assertFalse((self.checkpoint / "libs/a.lib").is_symlink())
        self.assertFalse((self.checkpoint / "lef/tech.lef").is_symlink())
        report = json.loads(final.read_text(encoding="utf-8"))
        self.assertEqual(
            [entry["path"] for entry in report["materialized_links"]],
            ["lef/tech.lef", "libs/a.lib"],
        )

    def test_no_links_and_no_report_is_rejected_fail_closed(self) -> None:
        (self.checkpoint / "libs/a.lib").unlink()
        with self.assertRaisesRegex(materializer.MaterializationError, "no auditable"):
            materializer.materialize_run(self.run, ["CASE_A"])

    def test_final_report_and_pending_journal_cannot_coexist(self) -> None:
        [report] = materializer.materialize_run(self.run, ["CASE_A"])
        pending = self.replay / materializer.PENDING_REPORT_NAME
        pending.write_bytes(report.read_bytes())
        with self.assertRaisesRegex(materializer.MaterializationError, "both final"):
            materializer.materialize_run(self.run, ["CASE_A"])

    def test_report_entries_are_sorted_by_checkpoint_relative_path(self) -> None:
        self._set_link(
            "lef/tech.lef",
            f"{self.guest_root}/run-1/baseline/base.enc.dat/lef/tech.lef",
        )
        [report_path] = materializer.materialize_run(self.run, ["CASE_A"])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(
            [entry["path"] for entry in report["materialized_links"]],
            ["lef/tech.lef", "libs/a.lib"],
        )

    def test_incomplete_replay_is_skipped_without_checkpoint_inspection(self) -> None:
        replay = self.case_b / "runs/replay_1"
        checkpoint = replay / "violating.enc.dat"
        checkpoint.mkdir(parents=True)
        os.symlink("../../relative-and-invalid", checkpoint / "bad")
        self.assertEqual(materializer.materialize_run(self.run, ["CASE_B"]), [])
        self.assertTrue((checkpoint / "bad").is_symlink())

    def test_failed_or_unfetched_replay_is_skipped(self) -> None:
        self._write_status(self.replay, passed=False, fetched=True, exit_code=1)
        self.assertEqual(materializer.materialize_run(self.run, ["CASE_A"]), [])
        self.assertTrue((self.checkpoint / "libs/a.lib").is_symlink())
        self._write_status(self.replay, passed=True, fetched=False)
        self.assertEqual(materializer.materialize_run(self.run, ["CASE_A"]), [])
        self.assertTrue((self.checkpoint / "libs/a.lib").is_symlink())

    def test_relative_guest_target_is_rejected_without_mutation(self) -> None:
        link = self._set_link("libs/a.lib", "../../base.enc.dat/libs/a.lib")
        with self.assertRaisesRegex(materializer.MaterializationError, "absolute"):
            materializer.materialize_run(self.run, ["CASE_A"])
        self.assertTrue(link.is_symlink())

    def test_target_outside_exact_guest_baseline_is_rejected(self) -> None:
        link = self._set_link(
            "libs/a.lib", f"{self.guest_root}/other-run/baseline/base.enc.dat/libs/a.lib"
        )
        with self.assertRaisesRegex(materializer.MaterializationError, "outside"):
            materializer.materialize_run(self.run, ["CASE_A"])
        self.assertTrue(link.is_symlink())

    def test_noncanonical_guest_target_is_rejected(self) -> None:
        link = self._set_link(
            "libs/a.lib",
            f"{self.guest_root}/run-1/baseline/base.enc.dat/libs/../libs/a.lib",
        )
        with self.assertRaisesRegex(materializer.MaterializationError, "canonical|unsafe"):
            materializer.materialize_run(self.run, ["CASE_A"])
        self.assertTrue(link.is_symlink())

    def test_target_must_match_checkpoint_relative_path_one_to_one(self) -> None:
        link = self._set_link(
            "libs/a.lib",
            f"{self.guest_root}/run-1/baseline/base.enc.dat/lef/tech.lef",
        )
        with self.assertRaisesRegex(materializer.MaterializationError, "does not match"):
            materializer.materialize_run(self.run, ["CASE_A"])
        self.assertTrue(link.is_symlink())

    def test_link_to_directory_is_rejected(self) -> None:
        link = self._set_link(
            "lef",
            f"{self.guest_root}/run-1/baseline/base.enc.dat/lef",
        )
        with self.assertRaises(materializer.MaterializationError):
            materializer.materialize_run(self.run, ["CASE_A"])
        self.assertTrue(link.is_symlink())

    def test_missing_checksum_entry_is_rejected(self) -> None:
        entries = self._baseline_entries()
        del entries["base.enc.dat/libs/a.lib"]
        self._write_checksums(entries)
        self._refresh_manifest_checksum()
        with self.assertRaisesRegex(materializer.MaterializationError, "absent from baseline checksums"):
            materializer.materialize_run(self.run, ["CASE_A"])

    def test_checksum_mismatch_is_rejected(self) -> None:
        entries = self._baseline_entries()
        entries["base.enc.dat/libs/a.lib"] = "0" * 64
        self._write_checksums(entries)
        self._refresh_manifest_checksum()
        with self.assertRaisesRegex(materializer.MaterializationError, "SHA256 mismatch"):
            materializer.materialize_run(self.run, ["CASE_A"])

    def test_checksum_manifest_digest_mismatch_is_rejected(self) -> None:
        with (self.run / "baseline_checksums.sha256").open("a", encoding="utf-8") as stream:
            stream.write(f"{'0' * 64}  extra\n")
        with self.assertRaisesRegex(materializer.MaterializationError, "manifest SHA256 mismatch"):
            materializer.materialize_run(self.run, ["CASE_A"])

    def test_dangling_host_source_is_rejected(self) -> None:
        (self.baseline / "base.enc.dat/libs/a.lib").unlink()
        with self.assertRaisesRegex(materializer.MaterializationError, "missing baseline source file"):
            materializer.materialize_run(self.run, ["CASE_A"])

    def test_symlinked_host_source_is_rejected(self) -> None:
        source = self.baseline / "base.enc.dat/libs/a.lib"
        source.unlink()
        os.symlink(self.baseline / "base.enc.dat/lef/tech.lef", source)
        with self.assertRaisesRegex(materializer.MaterializationError, "non-symlink regular file"):
            materializer.materialize_run(self.run, ["CASE_A"])

    def test_symlinked_host_source_parent_is_rejected(self) -> None:
        linked = self.baseline / "linked"
        os.symlink(self.baseline / "base.enc.dat/libs", linked)
        manifest = json.loads((self.run / "run_manifest.json").read_text(encoding="utf-8"))
        # Keep the fixture structurally valid while proving that baseline.source
        # itself may not be a symlink.
        manifest["baseline"]["source"] = str(linked)
        self._write_json(self.run / "run_manifest.json", manifest)
        with self.assertRaisesRegex(materializer.MaterializationError, "non-symlink directory"):
            materializer.materialize_run(self.run, ["CASE_A"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation requires POSIX")
    def test_checkpoint_special_file_is_rejected_before_any_copy(self) -> None:
        os.mkfifo(self.checkpoint / "pipe")
        link = self.checkpoint / "libs/a.lib"
        with self.assertRaisesRegex(materializer.MaterializationError, "non-regular"):
            materializer.materialize_run(self.run, ["CASE_A"])
        self.assertTrue(link.is_symlink())

    def test_any_invalid_link_prevents_all_link_mutations(self) -> None:
        valid = self.checkpoint / "libs/a.lib"
        invalid = self._set_link(
            "lef/tech.lef",
            f"{self.guest_root}/wrong/baseline/base.enc.dat/lef/tech.lef",
        )
        with self.assertRaises(materializer.MaterializationError):
            materializer.materialize_run(self.run, ["CASE_A"])
        self.assertTrue(valid.is_symlink())
        self.assertTrue(invalid.is_symlink())

    def test_tampered_materialized_file_fails_idempotent_revalidation(self) -> None:
        materializer.materialize_run(self.run, ["CASE_A"])
        (self.checkpoint / "libs/a.lib").write_bytes(b"tampered\n")
        with self.assertRaisesRegex(materializer.MaterializationError, "file mismatch"):
            materializer.materialize_run(self.run, ["CASE_A"])

    def test_tampered_or_noncanonical_report_is_rejected(self) -> None:
        [report] = materializer.materialize_run(self.run, ["CASE_A"])
        value = json.loads(report.read_text(encoding="utf-8"))
        value["baseline"]["bundle_sha256"] = "b" * 64
        self._write_json(report, value)
        with self.assertRaisesRegex(materializer.MaterializationError, "provenance"):
            materializer.materialize_run(self.run, ["CASE_A"])

    def test_report_symlink_is_rejected(self) -> None:
        report = self.replay / materializer.REPORT_NAME
        os.symlink(self.run / "run_manifest.json", report)
        with self.assertRaisesRegex(materializer.MaterializationError, "non-symlink regular file"):
            materializer.materialize_run(self.run, ["CASE_A"])

    def test_status_symlink_and_replay_index_mismatch_are_rejected(self) -> None:
        status = self.replay / "run_status.json"
        status.unlink()
        os.symlink(self.run / "run_manifest.json", status)
        with self.assertRaisesRegex(materializer.MaterializationError, "non-symlink regular file"):
            materializer.materialize_run(self.run, ["CASE_A"])
        status.unlink()
        self._write_status(self.replay, passed=True, fetched=True, replay_index=2)
        with self.assertRaisesRegex(materializer.MaterializationError, "index mismatch"):
            materializer.materialize_run(self.run, ["CASE_A"])

    def test_case_selection_is_strict_and_manifest_ordered(self) -> None:
        self.assertEqual(
            materializer._select_cases(["CASE_A", "CASE_B"], ["CASE_B", "CASE_A"]),
            ["CASE_A", "CASE_B"],
        )
        with self.assertRaisesRegex(materializer.MaterializationError, "cannot be combined"):
            materializer._select_cases(["CASE_A"], ["all", "CASE_A"])
        with self.assertRaisesRegex(materializer.MaterializationError, "duplicate"):
            materializer._select_cases(["CASE_A"], ["CASE_A", "CASE_A"])
        with self.assertRaisesRegex(materializer.MaterializationError, "unknown"):
            materializer._select_cases(["CASE_A"], ["CASE_X"])

    def test_main_reports_zero_for_selected_incomplete_case(self) -> None:
        self.assertEqual(
            materializer.main(["--run-dir", str(self.run), "--case", "CASE_B"]),
            0,
        )


if __name__ == "__main__":
    unittest.main()

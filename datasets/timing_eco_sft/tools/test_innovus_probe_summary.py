from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import innovus_probe_summary as probe  # noqa: E402


def _escape(value: object) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def _write_tsv(root: Path, name: str, rows: list[list[object]]) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow([_escape(value) for value in probe.SCHEMAS[name]])
        for row in rows:
            writer.writerow([_escape(value) for value in row])


def _refresh_manifest(root: Path) -> None:
    rows: list[list[object]] = []
    for name in probe.SCHEMAS:
        if name == "artifact_manifest.tsv":
            continue
        path = root / name
        with path.open("r", encoding="utf-8", newline="") as stream:
            row_count = sum(1 for _ in stream) - 1
        rows.append([name, f"test.{name}.v1", row_count, path.stat().st_size])
    _write_tsv(root, "artifact_manifest.tsv", rows)


def _fixture(
    root: Path,
    *,
    unsupported_help: bool = False,
    command_exists: object = "true",
) -> None:
    _write_tsv(
        root,
        "pre_restore_state.tsv",
        [["get_db_current_design", "OK", "", ""], ["dbGet_top_name", "OK", "0x0", ""]],
    )
    _write_tsv(
        root,
        "probe_metadata.tsv",
        [
            ["schema", "string", "innovus_readonly_probe.v1"],
            ["purpose", "string", "calibration_only_never_gold"],
            ["process_id", "integer", 123],
            ["tool_version", "string", "Innovus 21.10-p004_1"],
            ["baseline_dir", "path", "/guest/baseline"],
            ["top", "string", "design"],
            ["setup_view", "string", "functional_setup_ss"],
            ["hold_view", "string", "functional_hold_ff"],
            ["checkpoint", "path", "/guest/baseline/base.enc"],
            ["design_mutations", "integer", 0],
            ["gold_eligible", "boolean", "false"],
        ],
    )
    commands: list[list[object]] = []
    for command in sorted(probe.REQUIRED_COMMANDS):
        unsupported = unsupported_help and command == "rcOut"
        commands.append(
            [
                command,
                command_exists,
                "UNSUPPORTED" if unsupported else "SUPPORTED",
                "-view",
                "-view" if unsupported else "",
                f"raw/help/{command}.txt",
                "missing token" if unsupported else "",
            ]
        )
        help_path = root / "raw/help" / f"{command}.txt"
        help_path.parent.mkdir(parents=True, exist_ok=True)
        if not unsupported:
            help_path.write_text(f"help for {command} -view -spef -eco -postRoute -setup -hold -selectedTerms -incr\n", encoding="utf-8")
    _write_tsv(root, "command_probe.tsv", commands)

    sdc_rows: list[list[object]] = []
    for analysis, view in (("setup", "functional_setup_ss"), ("hold", "functional_hold_ff")):
        for sequence, timestamp in ((1, "10:00:00"), (2, "10:00:02")):
            relative = Path("raw/constraints") / f"{analysis}.{sequence}.sdc"
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"# Generated at 2026-07-20 {timestamp}\ncreate_clock -period 8 clk\n",
                encoding="utf-8",
            )
            sdc_rows.append([analysis, view, sequence, "OK", relative.as_posix(), path.stat().st_size, ""])
    _write_tsv(root, "sdc_write_probe.tsv", sdc_rows)

    _write_tsv(
        root,
        "candidate_paths.tsv",
        [
            [
                "late",
                "functional_setup_ss",
                "exp",
                0,
                "0.020",
                "u_exp/a/Q",
                "u_exp/b/D",
                "u_exp/c/Z",
                "u_exp/c",
                "BUF_X1M_A9TR40",
                "u_exp/n1",
                0,
            ],
            [
                "late",
                "functional_setup_ss",
                "exp",
                1,
                "0.030",
                "u_exp/d/Q",
                "u_exp/e/D",
                "u_exp/f/Z",
                "u_exp/f",
                "BUF_X1M_A9TR40",
                "u_exp/n2",
                1,
            ],
            [
                "early",
                "functional_hold_ff",
                "top_tree",
                0,
                "0.040",
                "a/Q",
                "b/D",
                "c/Z",
                "c",
                "BUF_X1M_A9TR40",
                "n3",
                0,
            ],
        ],
    )
    _write_tsv(root, "path_rejections.tsv", [])
    signature = "I:A|O:Z=A"
    _write_tsv(
        root,
        "lib_signatures.tsv",
        [
            ["BUF_X0P7M_A9TR40", "ss/BUF_X0P7M_A9TR40", "ss", "A", "Z=A", signature, "OK", ""],
            ["BUF_X0P7M_A9TR40", "ff/BUF_X0P7M_A9TR40", "ff", "A", "Z=A", signature, "OK", ""],
            ["BUF_X1M_A9TR40", "ss/BUF_X1M_A9TR40", "ss", "A", "Z=A", signature, "OK", ""],
            ["BUF_X1M_A9TR40", "ff/BUF_X1M_A9TR40", "ff", "A", "Z=A", signature, "OK", ""],
        ],
    )
    _write_tsv(
        root,
        "drive_families.tsv",
        [
            ["BUF_X1M_A9TR40", "1", "BUF", "M", "A9TR40", "BUF_X0P7M_A9TR40", "0.7", 2, "OK", "TRUE"],
            ["BUF_X1M_A9TR40", "1", "BUF", "M", "A9TR40", "BUF_X1M_A9TR40", "1", 2, "OK", "TRUE"],
        ],
    )
    _write_tsv(
        root,
        "selection_probe.tsv",
        [
            ["get_db_selected_full_name", "UNSUPPORTED", 0, "", "u_exp/b/D", "FALSE", "attribute absent"],
            ["get_db_selected_name", "OK", 1, "u_exp/b/D", "u_exp/b/D", "TRUE", ""],
            ["dbGet_selected_name", "OK", 1, "u_exp/b/D", "u_exp/b/D", "TRUE", ""],
        ],
    )
    report_rows: list[list[object]] = []
    for command, filename in (
        ("report_constraint", "report_constraint_all_violators.rpt"),
        ("verifyConnectivity", "verify_connectivity.rpt"),
        ("verify_drc", "verify_drc.rpt"),
    ):
        relative = Path("raw/reports") / filename
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if command == "verify_drc":
            path.write_text(
                "# Command: verify_drc -limit 1000000 -report verify_drc.rpt\n"
                "Total Violations : 0 Viols.\n",
                encoding="utf-8",
            )
        else:
            path.write_text(f"raw {command} format\n", encoding="utf-8")
        report_rows.append([command, "OK", relative.as_posix(), path.stat().st_size, ""])
    _write_tsv(root, "report_probe.tsv", report_rows)
    _write_tsv(root, "unsupported_apis.tsv", [])
    _refresh_manifest(root)
    (root / "probe.status").write_text(
        "SFT_INNOVUS_READONLY_PROBE_COMPLETE\n"
        "GOLD_ELIGIBLE=FALSE\n"
        "DESIGN_MUTATIONS=0\n",
        encoding="utf-8",
    )


class InnovusProbeSummaryTests(unittest.TestCase):
    def _rewrite_drc(self, root: Path, text: str) -> None:
        path = root / "raw/reports/verify_drc.rpt"
        path.write_text(text, encoding="utf-8")
        rows = []
        for row in probe._read_tsv(root, "report_probe.tsv"):
            values = [row[column] for column in probe.SCHEMAS["report_probe.tsv"]]
            if row["report_command"] == "verify_drc":
                values[3] = str(path.stat().st_size)
            rows.append(values)
        _write_tsv(root, "report_probe.tsv", rows)
        _refresh_manifest(root)

    def test_timestamp_only_sdc_differences_are_classified_without_gold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _fixture(root)
            summary = probe.summarize_probe(root)
            self.assertEqual(summary["verdict"], "PROBE_ONLY_NOT_GOLD")
            self.assertFalse(summary["gold_eligible"])
            self.assertTrue(summary["all_probed_apis_supported"])
            for item in summary["sdc_diff_analysis"]:
                self.assertFalse(item["raw_identical"])
                self.assertTrue(item["timestamp_header_only_difference"])

    def test_actual_tcl_numeric_command_exists_boolean_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _fixture(root, command_exists="1")
            summary = probe.summarize_probe(root)
            self.assertEqual(summary["verdict"], "PROBE_ONLY_NOT_GOLD")
            self.assertTrue(summary["all_probed_apis_supported"])

    def test_boolean_parser_rejects_non_boolean_numeric_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _fixture(root, command_exists="2")
            with self.assertRaisesRegex(probe.ProbeSummaryError, "not a typed Boolean"):
                probe.summarize_probe(root)

    def test_tcl_zero_is_the_false_boolean_not_a_truthy_integer(self) -> None:
        self.assertIs(probe._boolean("0", "test.boolean"), False)

    def test_drc_probe_requires_full_collection_limit_and_no_cutoff(self) -> None:
        cases = (
            (
                "# Command: verify_drc -report verify_drc.rpt\n"
                "Total Violations : 0 Viols.\n",
                "exactly one -limit 1000000",
            ),
            (
                "# Command: verify_drc -limit 1000 -report verify_drc.rpt\n"
                "Total Violations : 0 Viols.\n",
                "exactly one -limit 1000000",
            ),
            (
                "# Command: verify_drc -limit 1000000 -report verify_drc.rpt\n"
                "Violation limit reached; report truncated.\n"
                "Total Violations : 0 Viols.\n",
                "truncation signal",
            ),
            (
                "# Command: verify_drc -limit 1000000 -report verify_drc.rpt\n"
                "Total Violations : 1000000 Viols.\n",
                "reaches collection limit",
            ),
        )
        for report, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _fixture(root)
                self._rewrite_drc(root, report)
                with self.assertRaisesRegex(probe.ProbeSummaryError, message):
                    probe.summarize_probe(root)

    def test_non_timestamp_sdc_difference_is_not_normalized_away(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _fixture(root)
            path = root / "raw/constraints/setup.2.sdc"
            text = path.read_text(encoding="utf-8").replace("-period 8", "-period 9")
            path.write_text(text, encoding="utf-8")
            rows = []
            for row in probe._read_tsv(root, "sdc_write_probe.tsv"):
                rows.append([row[column] for column in probe.SCHEMAS["sdc_write_probe.tsv"]])
            for row in rows:
                if row[0] == "setup" and row[2] == "2":
                    row[5] = str(path.stat().st_size)
            _write_tsv(root, "sdc_write_probe.tsv", rows)
            _refresh_manifest(root)
            summary = probe.summarize_probe(root)
            setup = next(item for item in summary["sdc_diff_analysis"] if item["analysis"] == "setup")
            self.assertFalse(setup["normalized_timestamp_comments_identical"])
            self.assertFalse(setup["timestamp_header_only_difference"])

    def test_candidate_rank_order_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _fixture(root)
            rows = probe._read_tsv(root, "candidate_paths.tsv")
            rows[0]["stable_rank"], rows[1]["stable_rank"] = "1", "0"
            _write_tsv(
                root,
                "candidate_paths.tsv",
                [[row[column] for column in probe.SCHEMAS["candidate_paths.tsv"]] for row in rows],
            )
            _refresh_manifest(root)
            with self.assertRaisesRegex(probe.ProbeSummaryError, "not contiguous/in order"):
                probe.summarize_probe(root)

    def test_drive_flavor_change_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _fixture(root)
            rows = probe._read_tsv(root, "drive_families.tsv")
            rows[0]["variant_ref"] = "BUF_X0P7B_A9TR40"
            _write_tsv(
                root,
                "drive_families.tsv",
                [[row[column] for column in probe.SCHEMAS["drive_families.tsv"]] for row in rows],
            )
            _refresh_manifest(root)
            with self.assertRaisesRegex(probe.ProbeSummaryError, "changes exact stem/flavor/tail"):
                probe.summarize_probe(root)

    def test_unsupported_help_remains_probe_only_and_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _fixture(root, unsupported_help=True)
            summary = probe.summarize_probe(root)
            self.assertFalse(summary["all_probed_apis_supported"])
            self.assertIn("help:rcOut", summary["unsupported_or_incomplete"])
            self.assertEqual(summary["verdict"], "PROBE_ONLY_NOT_GOLD")


if __name__ == "__main__":
    unittest.main()

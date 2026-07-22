#!/usr/bin/env python3
"""Unit and static-contract tests for PrimeTime crosscheck support."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("primetime_crosscheck.py")
SPEC = importlib.util.spec_from_file_location("primetime_crosscheck", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
ptx = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ptx)


class PrimeTimeCrosscheckTest(unittest.TestCase):
    TOP = "NV_NVDLA_CMAC_CORE_mac"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(self._make_staging_removable)
        self.report_dir = self.root / "reports" / "primetime"
        self.report_dir.mkdir(parents=True)

    def _make_staging_removable(self) -> None:
        if not self.root.exists():
            return
        for path in self.root.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
            else:
                path.chmod(0o644)

    @staticmethod
    def _source_bytes(role: str, prefix: str = "real source bytes") -> bytes:
        if role == "tcl":
            return ptx.DEFAULT_TCL.read_bytes()
        if role == "setup_sdc":
            return (
                f"# {prefix} for {role}\r\n"
                "current_design NV_NVDLA_CMAC_CORE_mac\r\n"
                "set_clock_uncertainty 0.1 [all_clocks]\r\n"
            ).encode("utf-8")
        if role == "hold_sdc":
            return (
                f"# {prefix} for {role}\n"
                "current_design NV_NVDLA_CMAC_CORE_mac\n"
                "set_clock_uncertainty 0.2 [all_clocks]\n"
            ).encode("utf-8")
        return f"{prefix} for {role}\n".encode("utf-8")

    @staticmethod
    def _marker(
        mode: str,
        *,
        status: str = "PASS",
        wns: float = 0.02,
        tns: float = 0.0,
        violating_paths: int = 0,
    ) -> str:
        delay_type = "max" if mode == "setup" else "min"
        return (
            "PT_CROSSCHECK_V1 "
            f"status={status} mode={mode} delay_type={delay_type} "
            "top=NV_NVDLA_CMAC_CORE_mac "
            f"wns_ns={wns:.9f} tns_ns={tns:.9f} "
            f"violating_paths={violating_paths} threshold_ns=0.010000000 "
            "parasitics_read=1 propagated_clocks=1 clock_count=1 "
            "tool_version=R-2020.09-SP4 "
            "lc_root=/opt/synopsys/lc/R-2020.09-SP3\n"
        )

    def _write_corner(
        self,
        mode: str,
        *,
        status: str = "PASS",
        wns: float = 0.02,
        tns: float = 0.0,
        violating_paths: int = 0,
    ) -> None:
        (self.report_dir / f"{mode}.result").write_text(
            self._marker(
                mode,
                status=status,
                wns=wns,
                tns=tns,
                violating_paths=violating_paths,
            ),
            encoding="utf-8",
        )
        for role, pattern in ptx.REPORT_FILES.items():
            (self.report_dir / pattern.format(mode=mode)).write_text(
                f"real {mode} {role} report placeholder for parser unit test\n",
                encoding="utf-8",
            )

    def _add_provenance(
        self, summary: dict, *, tcl_bytes: bytes | None = None
    ) -> None:
        source_dir = self.root / "source_inputs"
        source_dir.mkdir(exist_ok=True)
        sources: dict[str, Path] = {}
        for role in ptx.INPUT_ROLES:
            source = source_dir / ptx.STAGED_INPUT_FILENAMES[role]
            source.write_bytes(
                tcl_bytes if role == "tcl" and tcl_bytes is not None else self._source_bytes(role)
            )
            sources[role] = source
        staged, metadata, adaptation = ptx._stage_input_bundle(
            sources, self.root, top=self.TOP
        )
        summary["inputs"] = metadata
        summary["sdc_adaptation"] = adaptation
        log_dir = self.root / "logs"
        log_dir.mkdir(exist_ok=True)
        summary["execution"] = {}
        for mode in ("setup", "hold"):
            log = log_dir / f"primetime_{mode}.log"
            log.write_text(f"PrimeTime {mode} completed\n", encoding="utf-8")
            summary["execution"][mode] = {
                "backend": "local",
                "mode": mode,
                "command": [
                    ptx.DEFAULT_PT_SHELL,
                    "-no_init",
                    "-f",
                    str(staged["tcl"]),
                ],
                "tcl_artifact": metadata["tcl"]["path"],
                "tcl_basename": staged["tcl"].name,
                "sdc_role": ptx.SDC_ROLE_BY_MODE[mode],
                "sdc_environment_variable": ptx.SDC_ENV_BY_MODE[mode],
                "sdc_artifact": metadata[ptx.SDC_ROLE_BY_MODE[mode]]["path"],
                "pt_sdc_artifact": adaptation[mode]["adapted_artifact"]["path"],
                "pt_sdc_sha256": adaptation[mode]["adapted_artifact"]["sha256"],
                "synopsys_lc_root": ptx.DEFAULT_SYNOPSYS_LC_ROOT,
                "home": str(self.root.resolve()),
                "working_directory": str(self.root.resolve()),
                "input_bindings": ptx._execution_input_bindings(
                    mode, metadata, adaptation
                ),
                "exit_code": 0,
                "timed_out": False,
                "log": ptx._artifact(log, self.root, f"{mode} log"),
            }

    def _remote_run_fixture(
        self, name: str
    ) -> tuple[types.SimpleNamespace, dict[str, Path]]:
        source_dir = self.root / f"{name}_sources"
        source_dir.mkdir()
        sources: dict[str, Path] = {}
        for role in ptx.INPUT_ROLES:
            source = source_dir / ptx.STAGED_INPUT_FILENAMES[role]
            source.write_bytes(self._source_bytes(role, "remote source bytes"))
            sources[role] = source
        ssh = source_dir / "fake_ssh"
        rsync = source_dir / "fake_rsync"
        ssh.write_text("fake ssh executable\n", encoding="utf-8")
        rsync.write_text("fake rsync executable\n", encoding="utf-8")
        args = types.SimpleNamespace(
            tcl=sources["tcl"],
            netlist=sources["netlist"],
            setup_sdc=sources["setup_sdc"],
            hold_sdc=sources["hold_sdc"],
            sdc=None,
            setup_lib=sources["setup_lib"],
            hold_lib=sources["hold_lib"],
            setup_spef=sources["setup_spef"],
            hold_spef=sources["hold_spef"],
            threshold_ns=0.010,
            timeout_seconds=60,
            require_tool_version="R-2020.09-SP4",
            pt_shell="/opt/synopsys/prime/R-2020.09-SP4/bin/pt_shell",
            output_dir=self.root / f"{name}_output",
            top="NV_NVDLA_CMAC_CORE_mac",
            max_violating_paths=100,
            report_max_paths=10,
            ssh_target="qingteng-fc",
            remote_root="/home/host/nvdla_timing_eco_sft/primetime",
            ssh_executable=str(ssh),
            rsync_executable=str(rsync),
            synopsys_lc_root=ptx.DEFAULT_SYNOPSYS_LC_ROOT,
        )
        return args, sources

    def _passing_summary(self) -> tuple[Path, dict]:
        self._write_corner("setup", wns=0.012)
        self._write_corner("hold", wns=0.015)
        summary = ptx.build_summary(
            self.report_dir,
            summary_root=self.root,
            expected_top="NV_NVDLA_CMAC_CORE_mac",
        )
        self._add_provenance(summary)
        path = self.root / "primetime_crosscheck.json"
        ptx._write_json(path, summary)
        return path, summary

    def test_build_and_verify_passing_summary(self) -> None:
        path, summary = self._passing_summary()
        self.assertTrue(summary["passed"])
        self.assertEqual("timing_eco_primetime_crosscheck.v4", summary["schema_version"])
        self.assertEqual("max", summary["corners"]["setup"]["delay_type"])
        self.assertEqual("min", summary["corners"]["hold"]["delay_type"])
        verified = ptx.load_and_verify_summary(path)
        self.assertTrue(verified["passed"])
        self.assertEqual(0.012, verified["corners"]["setup"]["wns_ns"])

    def test_verifier_pins_the_approved_tcl_bytes(self) -> None:
        self._write_corner("setup", wns=0.012)
        self._write_corner("hold", wns=0.015)
        summary = ptx.build_summary(
            self.report_dir,
            summary_root=self.root,
            expected_top=self.TOP,
        )
        self._add_provenance(summary, tcl_bytes=b"puts forged_pass\n")
        path = self.root / "unapproved_tcl.json"
        ptx._write_json(path, summary)
        with self.assertRaisesRegex(ptx.CrosscheckError, "approved DEFAULT_TCL"):
            ptx.load_and_verify_summary(path)

    def test_verifier_rejects_self_declared_approved_tcl_identity(self) -> None:
        path, summary = self._passing_summary()
        summary["policy"]["approved_tcl_sha256"] = "0" * 64
        ptx._write_json(path, summary)
        with self.assertRaisesRegex(ptx.CrosscheckError, "local DEFAULT_TCL"):
            ptx.load_and_verify_summary(path)

    def test_verifier_requires_exact_local_no_init_command(self) -> None:
        path, summary = self._passing_summary()
        summary["execution"]["setup"]["command"].remove("-no_init")
        ptx._write_json(path, summary)
        with self.assertRaisesRegex(ptx.CrosscheckError, "exact isolated"):
            ptx.load_and_verify_summary(path)

    def test_rejects_pre_dual_sdc_summary_schema(self) -> None:
        path, summary = self._passing_summary()
        for old_schema in (
            "timing_eco_primetime_crosscheck.v2",
            "timing_eco_primetime_crosscheck.v3",
        ):
            with self.subTest(schema=old_schema):
                summary["schema_version"] = old_schema
                ptx._write_json(path, summary)
                with self.assertRaisesRegex(ptx.CrosscheckError, "unsupported.*schema"):
                    ptx.load_and_verify_summary(path)

    def test_sdc_adapter_changes_only_the_exact_command_line(self) -> None:
        raw = (
            b"# Innovus output\r\n"
            b"set_units -time 1000ps\r\n"
            b"current_design NV_NVDLA_CMAC_CORE_mac\r\n"
            b"set_false_path -from [get_ports test_mode]\r\n"
            b"set_max_fanout 32  [get_designs {NV_NVDLA_CMAC_CORE_mac}]\r\n"
            b"set_max_transition 0.5  [get_designs {NV_NVDLA_CMAC_CORE_mac}]\r\n"
        )
        adapted, line_number, design_rule_lines = ptx._adapt_innovus_sdc_bytes(
            raw, self.TOP
        )
        self.assertEqual(3, line_number)
        self.assertEqual(
            {"set_max_fanout": 5, "set_max_transition": 6},
            design_rule_lines,
        )
        self.assertEqual(
            raw.replace(
                b"current_design NV_NVDLA_CMAC_CORE_mac\r\n",
                b"# PT_DIALECT_ADAPTER: current_design "
                b"NV_NVDLA_CMAC_CORE_mac\r\n",
            )
            .replace(
                b"[get_designs {NV_NVDLA_CMAC_CORE_mac}]",
                b"[current_design]",
            ),
            adapted,
        )
        self.assertEqual(raw.splitlines(keepends=True)[:2], adapted.splitlines(keepends=True)[:2])
        self.assertEqual(raw.splitlines(keepends=True)[3], adapted.splitlines(keepends=True)[3])

    def test_sdc_adapter_rejects_partial_or_unsupported_get_designs(self) -> None:
        invalid = {
            "partial_pair": (
                b"current_design NV_NVDLA_CMAC_CORE_mac\n"
                b"set_max_fanout 32  [get_designs {NV_NVDLA_CMAC_CORE_mac}]\n"
            ),
            "wrong_top": (
                b"current_design NV_NVDLA_CMAC_CORE_mac\n"
                b"set_max_fanout 32  [get_designs {OTHER_TOP}]\n"
                b"set_max_transition 0.5  [get_designs {OTHER_TOP}]\n"
            ),
            "unsupported_context": (
                b"current_design NV_NVDLA_CMAC_CORE_mac\n"
                b"set x [get_designs {NV_NVDLA_CMAC_CORE_mac}]\n"
            ),
            "nonexact_spacing": (
                b"current_design NV_NVDLA_CMAC_CORE_mac\n"
                b"set_max_fanout 32 [get_designs {NV_NVDLA_CMAC_CORE_mac}]\n"
                b"set_max_transition 0.5 [get_designs {NV_NVDLA_CMAC_CORE_mac}]\n"
            ),
        }
        for label, raw in invalid.items():
            with self.subTest(label=label):
                with self.assertRaises(ptx.CrosscheckError):
                    ptx._adapt_innovus_sdc_bytes(raw, self.TOP)

    def test_sdc_adapter_rejects_zero_multiple_mismatch_and_nonexact_lines(self) -> None:
        invalid = {
            "zero": b"create_clock -period 1 clk\n",
            "multiple": (
                b"current_design NV_NVDLA_CMAC_CORE_mac\n"
                b"current_design NV_NVDLA_CMAC_CORE_mac\n"
            ),
            "mismatch": b"current_design OTHER_TOP\n",
            "leading_space": b" current_design NV_NVDLA_CMAC_CORE_mac\n",
            "trailing_space": b"current_design NV_NVDLA_CMAC_CORE_mac \n",
            "brace": b"current_design {NV_NVDLA_CMAC_CORE_mac}\n",
        }
        for label, raw in invalid.items():
            with self.subTest(label=label):
                with self.assertRaises(ptx.CrosscheckError):
                    ptx._adapt_innovus_sdc_bytes(raw, self.TOP)

    def test_sdc_adapter_preserves_missing_final_newline(self) -> None:
        raw = b"set x 1\ncurrent_design NV_NVDLA_CMAC_CORE_mac"
        adapted, line_number, design_rule_lines = ptx._adapt_innovus_sdc_bytes(
            raw, self.TOP
        )
        self.assertEqual(2, line_number)
        self.assertEqual({}, design_rule_lines)
        self.assertFalse(adapted.endswith(b"\n"))
        self.assertEqual(
            b"set x 1\n# PT_DIALECT_ADAPTER: current_design "
            b"NV_NVDLA_CMAC_CORE_mac",
            adapted,
        )

    def test_rejects_forged_pass_marker(self) -> None:
        marker = self._marker("setup", status="PASS", wns=0.005)
        with self.assertRaisesRegex(ptx.CrosscheckError, "inconsistent"):
            ptx.parse_marker_line(marker, expected_mode="setup")

    def test_rejects_wrong_analysis_direction(self) -> None:
        marker = self._marker("setup").replace("delay_type=max", "delay_type=min")
        with self.assertRaisesRegex(ptx.CrosscheckError, "must use delay_type=max"):
            ptx.parse_marker_line(marker, expected_mode="setup")

    def test_rejects_marker_lc_root_mismatch_or_unsafe_path(self) -> None:
        with self.assertRaisesRegex(ptx.CrosscheckError, "does not match expected"):
            ptx.parse_marker_line(
                self._marker("setup").replace(
                    ptx.DEFAULT_SYNOPSYS_LC_ROOT, "/opt/synopsys/lc/OTHER"
                ),
                expected_mode="setup",
            )
        with self.assertRaisesRegex(ptx.CrosscheckError, "unsafe or empty"):
            ptx.parse_marker_line(
                self._marker("setup").replace(
                    ptx.DEFAULT_SYNOPSYS_LC_ROOT, "relative/lc"
                ),
                expected_mode="setup",
                expected_lc_root=None,
            )

    def test_rejects_tampered_report(self) -> None:
        path, _ = self._passing_summary()
        timing = self.report_dir / "setup_after_pt.rpt"
        timing.write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(ptx.CrosscheckError, "hash mismatch"):
            ptx.load_and_verify_summary(path)

    def test_rejects_fatal_error_in_hashed_log(self) -> None:
        path, summary = self._passing_summary()
        log = self.root / summary["execution"]["setup"]["log"]["path"]
        log.write_text("banner\nError: read_lib failed (DBR-011)\n", encoding="utf-8")
        summary["execution"]["setup"]["log"] = ptx._artifact(
            log, self.root, "setup log"
        )
        ptx._write_json(path, summary)
        with self.assertRaisesRegex(ptx.CrosscheckError, "fatal PrimeTime transcript"):
            ptx.load_and_verify_summary(path)

    def test_rejects_black_box_link_message_in_hashed_report(self) -> None:
        path, summary = self._passing_summary()
        report = self.report_dir / "setup_check_timing.rpt"
        report.write_text(
            "Information: Creating black box 'MISSING' (LNK-043)\n",
            encoding="utf-8",
        )
        summary["corners"]["setup"]["evidence"]["check_timing"] = ptx._artifact(
            report, self.root, "setup check_timing"
        )
        ptx._write_json(path, summary)
        with self.assertRaisesRegex(ptx.CrosscheckError, "fatal PrimeTime transcript"):
            ptx.load_and_verify_summary(path)

    def test_rejects_tampered_mode_specific_sdc(self) -> None:
        path, summary = self._passing_summary()
        hold_sdc = self.root / summary["inputs"]["hold_sdc"]["path"]
        hold_sdc.chmod(0o644)
        hold_sdc.write_text("tampered hold constraints\n", encoding="utf-8")
        with self.assertRaisesRegex(ptx.CrosscheckError, "hash mismatch"):
            ptx.load_and_verify_summary(path)

    def test_rejects_tampered_adapted_sdc_even_with_refreshed_hash(self) -> None:
        path, summary = self._passing_summary()
        item = summary["sdc_adaptation"]["setup"]["adapted_artifact"]
        adapted = self.root / item["path"]
        adapted.chmod(0o644)
        adapted.write_bytes(adapted.read_bytes() + b"# hidden constraint change\n")
        summary["sdc_adaptation"]["setup"]["adapted_artifact"] = ptx._artifact(
            adapted, self.root, "tampered adapted SDC"
        )
        summary["execution"]["setup"]["pt_sdc_sha256"] = ptx._sha256(adapted)
        summary["execution"]["setup"]["input_bindings"]["setup_pt_sdc"] = ptx._sha256(
            adapted
        )
        ptx._write_json(path, summary)
        with self.assertRaisesRegex(ptx.CrosscheckError, "deterministic bounded-line"):
            ptx.load_and_verify_summary(path)

    def test_rejects_json_metrics_not_bound_to_result_marker(self) -> None:
        path, summary = self._passing_summary()
        summary["corners"]["setup"]["wns_ns"] = 0.020
        ptx._write_json(path, summary)
        with self.assertRaisesRegex(ptx.CrosscheckError, "does not match its result marker"):
            ptx.load_and_verify_summary(path)

    def test_rejects_missing_staged_tcl_input(self) -> None:
        path, summary = self._passing_summary()
        del summary["inputs"]["tcl"]
        ptx._write_json(path, summary)
        with self.assertRaisesRegex(ptx.CrosscheckError, "inputs must contain exactly"):
            ptx.load_and_verify_summary(path)

    def test_rejects_broken_source_to_stage_hash_binding(self) -> None:
        path, summary = self._passing_summary()
        summary["inputs"]["netlist"]["source_sha256"] = "0" * 64
        ptx._write_json(path, summary)
        with self.assertRaisesRegex(ptx.CrosscheckError, "staged/source SHA256"):
            ptx.load_and_verify_summary(path)

    def test_rejects_execution_bound_to_different_spef(self) -> None:
        path, summary = self._passing_summary()
        summary["execution"]["setup"]["input_bindings"]["setup_spef"] = "0" * 64
        ptx._write_json(path, summary)
        with self.assertRaisesRegex(ptx.CrosscheckError, "input_bindings"):
            ptx.load_and_verify_summary(path)

    def test_rejects_setup_execution_bound_to_hold_sdc(self) -> None:
        path, summary = self._passing_summary()
        bindings = summary["execution"]["setup"]["input_bindings"]
        del bindings["setup_sdc"]
        bindings["hold_sdc"] = summary["inputs"]["hold_sdc"]["sha256"]
        ptx._write_json(path, summary)
        with self.assertRaisesRegex(ptx.CrosscheckError, "input_bindings"):
            ptx.load_and_verify_summary(path)

    def test_rejects_single_sdc_policy_tamper(self) -> None:
        path, summary = self._passing_summary()
        summary["policy"]["sdc_roles"]["hold"] = "setup_sdc"
        ptx._write_json(path, summary)
        with self.assertRaisesRegex(ptx.CrosscheckError, "policy.sdc_roles"):
            ptx.load_and_verify_summary(path)

    def test_rejects_setup_hold_top_mismatch_on_reload(self) -> None:
        path, summary = self._passing_summary()
        result = self.report_dir / "hold.result"
        result.write_text(
            result.read_text(encoding="utf-8").replace(
                "top=NV_NVDLA_CMAC_CORE_mac", "top=OTHER_TOP"
            ),
            encoding="utf-8",
        )
        summary["corners"]["hold"]["top"] = "OTHER_TOP"
        summary["corners"]["hold"]["evidence"]["result"] = ptx._artifact(
            result, self.root, "hold marker"
        )
        ptx._write_json(path, summary)
        with self.assertRaisesRegex(ptx.CrosscheckError, "marker top.*adaptation top"):
            ptx.load_and_verify_summary(path)

    def test_staging_is_content_addressed_and_read_only(self) -> None:
        source_dir = self.root / "originals"
        source_dir.mkdir()
        sources: dict[str, Path] = {}
        for role in ptx.INPUT_ROLES:
            source = source_dir / ptx.STAGED_INPUT_FILENAMES[role]
            source.write_bytes(self._source_bytes(role, "immutable bytes"))
            sources[role] = source
        staged, metadata, adaptation = ptx._stage_input_bundle(
            sources, self.root, top=self.TOP
        )
        original_netlist = staged["netlist"].read_bytes()
        sources["netlist"].write_text("later source mutation\n", encoding="utf-8")
        self.assertEqual(original_netlist, staged["netlist"].read_bytes())
        self.assertEqual(
            metadata["netlist"]["source_sha256"], metadata["netlist"]["sha256"]
        )
        self.assertNotEqual(staged["setup_sdc"], staged["hold_sdc"])
        self.assertEqual(
            "constraint_setup_after.sdc", staged["setup_sdc"].name
        )
        self.assertEqual("constraint_hold_after.sdc", staged["hold_sdc"].name)
        self.assertEqual(0, staged["setup_sdc"].stat().st_mode & 0o222)
        self.assertEqual(0, staged["hold_sdc"].stat().st_mode & 0o222)
        self.assertEqual(0, staged["setup_pt_sdc"].stat().st_mode & 0o222)
        self.assertEqual(
            adaptation["setup"]["adapted_artifact"]["sha256"],
            ptx._sha256(staged["setup_pt_sdc"]),
        )
        self.assertNotEqual(
            metadata["setup_sdc"]["sha256"],
            adaptation["setup"]["adapted_artifact"]["sha256"],
        )
        self.assertEqual(0, staged["netlist"].stat().st_mode & 0o222)
        self.assertEqual(0, staged["netlist"].parent.stat().st_mode & 0o222)

    def test_verifier_recomputes_content_addressed_bundle_name(self) -> None:
        path, summary = self._passing_summary()
        old_relative = Path(summary["inputs"]["tcl"]["path"]).parent
        old_bundle = self.root / old_relative
        forged_relative = old_relative.parent / ("primetime_" + "0" * 64)
        old_bundle.rename(self.root / forged_relative)
        for item in summary["inputs"].values():
            item["path"] = (forged_relative / Path(item["path"]).name).as_posix()
        for mode in ("setup", "hold"):
            adaptation = summary["sdc_adaptation"][mode]
            adaptation["source_artifact"] = summary["inputs"][
                adaptation["source_role"]
            ]["path"]
            artifact = adaptation["adapted_artifact"]
            artifact["path"] = (forged_relative / Path(artifact["path"]).name).as_posix()
        ptx._write_json(path, summary)
        with self.assertRaisesRegex(ptx.CrosscheckError, "not content-addressed"):
            ptx.load_and_verify_summary(path)

    def test_sanitized_environment_removes_all_pt_keys(self) -> None:
        cleaned = ptx._sanitized_environment(
            {
                "PATH": "/bin",
                "PT_CONFIG_TCL": "/tmp/stale.tcl",
                "PT_NETLIST": "/tmp/stale.v",
                "PT_ANY_FUTURE_SETTING": "stale",
            }
        )
        self.assertEqual({"PATH": "/bin"}, cleaned)

    def test_runner_rejects_legacy_or_missing_dual_sdc_before_execution(self) -> None:
        args, sources = self._remote_run_fixture("single_sdc_rejected")
        args.setup_sdc = None
        args.hold_sdc = None
        args.sdc = sources["setup_sdc"]
        with mock.patch.object(ptx.subprocess, "run") as run:
            with self.assertRaisesRegex(ptx.CrosscheckError, "not valid Gold evidence"):
                ptx.run_crosscheck(args)
        run.assert_not_called()

        args.sdc = None
        args.setup_sdc = sources["setup_sdc"]
        with mock.patch.object(ptx.subprocess, "run") as run:
            with self.assertRaisesRegex(ptx.CrosscheckError, "requires both"):
                ptx.run_crosscheck(args)
        run.assert_not_called()

    def test_runner_rejects_same_file_for_setup_and_hold_sdc(self) -> None:
        args, sources = self._remote_run_fixture("same_sdc_rejected")
        args.setup_sdc = sources["setup_sdc"]
        args.hold_sdc = sources["setup_sdc"]
        with mock.patch.object(ptx.subprocess, "run") as run:
            with self.assertRaisesRegex(ptx.CrosscheckError, "distinct source artifacts"):
                ptx.run_crosscheck(args)
        run.assert_not_called()

    def test_runner_rejects_unapproved_tcl_before_execution(self) -> None:
        args, sources = self._remote_run_fixture("unapproved_tcl_rejected")
        sources["tcl"].write_text("puts forged_pass\n", encoding="utf-8")
        with mock.patch.object(ptx.subprocess, "run") as run:
            with self.assertRaisesRegex(ptx.CrosscheckError, "approved DEFAULT_TCL"):
                ptx.run_crosscheck(args)
        run.assert_not_called()

    def test_runner_rejects_empty_or_unsafe_lc_root_before_execution(self) -> None:
        for index, value in enumerate(("", "relative/lc", "/opt/synopsys/../escape")):
            args, _ = self._remote_run_fixture(f"bad_lc_root_{index}")
            args.synopsys_lc_root = value
            with self.subTest(value=value), mock.patch.object(
                ptx.subprocess, "run"
            ) as run:
                with self.assertRaisesRegex(
                    ptx.CrosscheckError, "SYNOPSYS_LC_ROOT"
                ):
                    ptx.run_crosscheck(args)
            run.assert_not_called()

    def test_runner_uses_one_staged_bundle_for_both_clean_processes(self) -> None:
        source_dir = self.root / "run_sources"
        source_dir.mkdir()
        sources: dict[str, Path] = {}
        for role in ptx.INPUT_ROLES:
            source = source_dir / ptx.STAGED_INPUT_FILENAMES[role]
            source.write_bytes(self._source_bytes(role, "runner source bytes"))
            sources[role] = source
        executable = source_dir / "pt_shell"
        executable.write_text("fake PrimeTime executable\n", encoding="utf-8")
        output = self.root / "run_output"
        calls: list[tuple[list[str], dict[str, str], str, str]] = []

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            environment = dict(kwargs["env"])
            self.assertEqual(Path(environment["HOME"]), kwargs["cwd"])
            mode = environment["PT_MODE"]
            staged_netlist_text = Path(environment["PT_NETLIST"]).read_text(
                encoding="utf-8"
            )
            sdc_variable = ptx.SDC_ENV_BY_MODE[mode]
            staged_sdc_text = Path(environment[sdc_variable]).read_text(
                encoding="utf-8"
            )
            calls.append(
                (list(command), environment, staged_netlist_text, staged_sdc_text)
            )
            report_dir = Path(environment["PT_REPORT_DIR"])
            report_dir.mkdir(parents=True, exist_ok=True)
            (report_dir / f"{mode}.result").write_text(
                self._marker(mode), encoding="utf-8"
            )
            for role, pattern in ptx.REPORT_FILES.items():
                (report_dir / pattern.format(mode=mode)).write_text(
                    f"fake real-tool {mode} {role} evidence\n", encoding="utf-8"
                )
            if mode == "setup":
                sources["netlist"].write_text(
                    "source changed between PT processes\n", encoding="utf-8"
                )
            return subprocess.CompletedProcess(command, 0, stdout=f"{mode} completed\n")

        args = types.SimpleNamespace(
            tcl=sources["tcl"],
            netlist=sources["netlist"],
            setup_sdc=sources["setup_sdc"],
            hold_sdc=sources["hold_sdc"],
            sdc=None,
            setup_lib=sources["setup_lib"],
            hold_lib=sources["hold_lib"],
            setup_spef=sources["setup_spef"],
            hold_spef=sources["hold_spef"],
            threshold_ns=0.010,
            timeout_seconds=60,
            require_tool_version="R-2020.09-SP4",
            pt_shell=str(executable),
            output_dir=output,
            top="NV_NVDLA_CMAC_CORE_mac",
            max_violating_paths=100,
            report_max_paths=10,
            synopsys_lc_root=ptx.DEFAULT_SYNOPSYS_LC_ROOT,
        )
        with mock.patch.dict(
            os.environ,
            {"PT_CONFIG_TCL": "/tmp/stale.tcl", "PT_NETLIST": "/tmp/stale.v"},
        ), mock.patch.object(ptx.subprocess, "run", side_effect=fake_run):
            self.assertEqual(0, ptx.run_crosscheck(args))

        self.assertEqual(2, len(calls))
        setup_call, hold_call = calls
        self.assertEqual(setup_call[2], hold_call[2])
        self.assertIn("runner source bytes for netlist", setup_call[2])
        self.assertIn("runner source bytes for setup_sdc", setup_call[3])
        self.assertIn("runner source bytes for hold_sdc", hold_call[3])
        self.assertIn(
            "# PT_DIALECT_ADAPTER: current_design NV_NVDLA_CMAC_CORE_mac",
            setup_call[3],
        )
        self.assertNotRegex(setup_call[3], r"(?m)^current_design ")
        self.assertNotEqual(setup_call[3], hold_call[3])
        for command, environment, _, _ in calls:
            mode = environment["PT_MODE"]
            opposite_mode = "hold" if mode == "setup" else "setup"
            self.assertNotIn("PT_CONFIG_TCL", environment)
            self.assertNotIn("PT_SDC", environment)
            self.assertIn(ptx.SDC_ENV_BY_MODE[mode], environment)
            self.assertNotIn(ptx.SDC_ENV_BY_MODE[opposite_mode], environment)
            self.assertNotEqual("/tmp/stale.v", environment["PT_NETLIST"])
            self.assertIn("inputs/primetime_", environment["PT_NETLIST"])
            self.assertIn("inputs/primetime_", command[-1])
            self.assertEqual(["-no_init", "-f"], command[1:3])
            self.assertEqual(
                ptx.DEFAULT_SYNOPSYS_LC_ROOT, environment["SYNOPSYS_LC_ROOT"]
            )
        summary = ptx.load_and_verify_summary(output / "primetime_crosscheck.json")
        self.assertTrue(summary["passed"])
        self.assertEqual(ptx.SCHEMA_VERSION, summary["schema_version"])
        self.assertEqual(
            {
                "tcl",
                "netlist",
                "setup_sdc",
                "setup_pt_sdc",
                "setup_lib",
                "setup_spef",
            },
            set(summary["execution"]["setup"]["input_bindings"]),
        )
        self.assertEqual(
            {
                "tcl",
                "netlist",
                "hold_sdc",
                "hold_pt_sdc",
                "hold_lib",
                "hold_spef",
            },
            set(summary["execution"]["hold"]["input_bindings"]),
        )

    def test_remote_runner_uploads_once_quotes_and_fetches_evidence(self) -> None:
        args, _ = self._remote_run_fixture("remote_success")
        calls: list[list[str]] = []

        def fake_transport(
            command: list[str], **_: object
        ) -> subprocess.CompletedProcess:
            command = list(command)
            calls.append(command)
            if command[0] == args.rsync_executable:
                source = command[-2]
                destination = Path(command[-1].rstrip("/"))
                if ":" in source and source.endswith("/reports/primetime/"):
                    destination.mkdir(parents=True, exist_ok=True)
                    for mode in ("setup", "hold"):
                        (destination / f"{mode}.result").write_text(
                            self._marker(mode), encoding="utf-8"
                        )
                        for role, pattern in ptx.REPORT_FILES.items():
                            (destination / pattern.format(mode=mode)).write_text(
                                f"remote {mode} {role} evidence\n", encoding="utf-8"
                            )
                elif ":" in source and source.endswith("/logs/"):
                    destination.mkdir(parents=True, exist_ok=True)
                    for mode in ("setup", "hold"):
                        (destination / f"primetime_{mode}.log").write_text(
                            f"remote PrimeTime {mode} completed\n", encoding="utf-8"
                        )
                return subprocess.CompletedProcess(command, 0, stdout="rsync ok\n")
            return subprocess.CompletedProcess(command, 0, stdout="ssh ok\n")

        with mock.patch.object(
            ptx, "_new_remote_invocation_id", return_value="run_0123456789abcdef"
        ), mock.patch.object(ptx.subprocess, "run", side_effect=fake_transport):
            self.assertEqual(0, ptx.run_crosscheck(args))

        uploads = [
            command
            for command in calls
            if command[0] == args.rsync_executable and "--checksum" in command
        ]
        self.assertEqual(1, len(uploads))
        remote_pt_commands = [
            command
            for command in calls
            if command[0] == args.ssh_executable
            and ("PT_MODE=setup" in command[-1] or "PT_MODE=hold" in command[-1])
        ]
        self.assertEqual(2, len(remote_pt_commands))
        for command in remote_pt_commands:
            script = command[-1]
            mode = "setup" if "PT_MODE=setup" in script else "hold"
            opposite_mode = "hold" if mode == "setup" else "setup"
            self.assertIn("env -i", script)
            self.assertIn("HOME=", script)
            self.assertIn("/runs/run_0123456789abcdef/home", script)
            self.assertIn("/runs/run_0123456789abcdef/work", script)
            self.assertIn("-no_init -f", script)
            self.assertIn("test ! -e", script)
            self.assertNotIn("PT_CONFIG_TCL", script)
            self.assertNotIn("PT_SDC=", script)
            self.assertIn(ptx.SDC_ENV_BY_MODE[mode] + "=", script)
            self.assertNotIn(ptx.SDC_ENV_BY_MODE[opposite_mode] + "=", script)
            self.assertIn(
                ptx.PT_SDC_FILENAMES[ptx.PT_SDC_ROLE_BY_MODE[mode]], script
            )
            self.assertNotIn(
                ptx.STAGED_INPUT_FILENAMES[f"{mode}_sdc"], script
            )
            self.assertNotIn(
                ptx.PT_SDC_FILENAMES[ptx.PT_SDC_ROLE_BY_MODE[opposite_mode]],
                script,
            )
            self.assertIn(
                "SYNOPSYS_LC_ROOT=/opt/synopsys/lc/R-2020.09-SP3", script
            )
            self.assertNotRegex(script, r"(^|[ ;])rm([ ;]|$)")
        summary = ptx.load_and_verify_summary(
            args.output_dir / "primetime_crosscheck.json"
        )
        self.assertTrue(summary["passed"])
        self.assertEqual("ssh", summary["execution"]["setup"]["backend"])
        self.assertEqual(
            summary["execution"]["setup"]["remote_bundle"],
            summary["execution"]["hold"]["remote_bundle"],
        )
        self.assertEqual(
            remote_pt_commands[0], summary["execution"]["setup"]["command"]
        )
        tampered = json.loads(
            (args.output_dir / "primetime_crosscheck.json").read_text(encoding="utf-8")
        )
        tampered["execution"]["setup"]["command"][-1] = tampered["execution"][
            "setup"
        ]["command"][-1].replace("setup.spef", "other.spef")
        tampered_path = args.output_dir / "tampered_remote_command.json"
        ptx._write_json(tampered_path, tampered)
        with self.assertRaisesRegex(ptx.CrosscheckError, "exact isolated"):
            ptx.load_and_verify_summary(tampered_path)

        opposite = json.loads(
            (args.output_dir / "primetime_crosscheck.json").read_text(
                encoding="utf-8"
            )
        )
        bundle = opposite["execution"]["setup"]["remote_bundle"]
        opposite["execution"]["setup"]["command"][-1] += (
            " PT_HOLD_SDC="
            + bundle
            + "/"
            + ptx.STAGED_INPUT_FILENAMES["hold_sdc"]
        )
        opposite_path = args.output_dir / "tampered_opposite_sdc_command.json"
        ptx._write_json(opposite_path, opposite)
        with self.assertRaisesRegex(ptx.CrosscheckError, "exact isolated"):
            ptx.load_and_verify_summary(opposite_path)

        wrong_root = json.loads(
            (args.output_dir / "primetime_crosscheck.json").read_text(encoding="utf-8")
        )
        for mode in ("setup", "hold"):
            wrong_root["execution"][mode]["remote_root"] = "/home/host/other/root"
        wrong_root_path = args.output_dir / "tampered_remote_root.json"
        ptx._write_json(wrong_root_path, wrong_root)
        with self.assertRaisesRegex(ptx.CrosscheckError, "remote root/bundle"):
            ptx.load_and_verify_summary(wrong_root_path)

        wrong_limit = json.loads(
            (args.output_dir / "primetime_crosscheck.json").read_text(encoding="utf-8")
        )
        wrong_limit["policy"]["max_violating_paths"] = 1
        wrong_limit_path = args.output_dir / "tampered_remote_limit.json"
        ptx._write_json(wrong_limit_path, wrong_limit)
        with self.assertRaisesRegex(ptx.CrosscheckError, "exact isolated"):
            ptx.load_and_verify_summary(wrong_limit_path)

    def test_remote_shell_quotes_explicit_pt_values(self) -> None:
        top = "TOP'; touch /tmp/not_allowed #"
        bundle = "/home/host/safe/root/primetime_01234567"
        remote_inputs = {
            role: f"{bundle}/{ptx.STAGED_INPUT_FILENAMES[role]}"
            for role in ptx.INPUT_ROLES
        }
        remote_inputs.update(
            {
                role: f"{bundle}/{ptx.PT_SDC_FILENAMES[role]}"
                for role in ptx.PT_SDC_ROLE_BY_MODE.values()
            }
        )
        script = ptx._remote_pt_script(
            mode="setup",
            top=top,
            threshold_ns=0.010,
            max_violating_paths=100,
            report_max_paths=10,
            remote_pt_shell="/opt/synopsys/prime/bin/pt_shell",
            synopsys_lc_root=ptx.DEFAULT_SYNOPSYS_LC_ROOT,
            remote_inputs=remote_inputs,
            remote_report_dir="/home/host/safe/root/reports",
            remote_log_path="/home/host/safe/root/logs/setup.log",
            remote_home="/home/host/safe/root/home",
            remote_work_dir="/home/host/safe/root/work",
        )
        self.assertIn(f"PT_TOP={ptx.shlex.quote(top)}", script)
        self.assertIn("env -i", script)
        self.assertIn("HOME=/home/host/safe/root/home", script)
        self.assertIn("cd -- /home/host/safe/root/work", script)
        self.assertIn("-no_init -f", script)
        self.assertNotIn("PT_CONFIG_TCL", script)
        self.assertIn("PT_SETUP_SDC=", script)
        self.assertNotIn("PT_HOLD_SDC=", script)
        self.assertNotIn("PT_SDC=", script)
        self.assertIn("constraint_setup_after.pt.sdc", script)
        self.assertNotIn("constraint_setup_after.sdc", script)
        self.assertNotIn("constraint_hold_after.pt.sdc", script)
        self.assertIn(
            "SYNOPSYS_LC_ROOT=/opt/synopsys/lc/R-2020.09-SP3", script
        )

    def test_remote_runner_rejects_local_stale_evidence_without_transport(self) -> None:
        args, _ = self._remote_run_fixture("remote_local_stale")
        stale = args.output_dir / "reports" / "primetime" / "setup.result"
        stale.parent.mkdir(parents=True)
        stale.write_text("old evidence\n", encoding="utf-8")
        with mock.patch.object(ptx.subprocess, "run") as run:
            with self.assertRaisesRegex(ptx.CrosscheckError, "stale invocation evidence"):
                ptx.run_crosscheck(args)
        run.assert_not_called()

    def test_remote_runner_rejects_preexisting_remote_invocation(self) -> None:
        args, _ = self._remote_run_fixture("remote_preexisting")
        completed = subprocess.CompletedProcess(
            [args.ssh_executable], 73, stdout="pre-existing invocation\n"
        )
        with mock.patch.object(
            ptx, "_new_remote_invocation_id", return_value="run_0123456789abcdef"
        ), mock.patch.object(ptx.subprocess, "run", return_value=completed) as run:
            with self.assertRaisesRegex(ptx.CrosscheckError, "exit code 73"):
                ptx.run_crosscheck(args)
        self.assertEqual(1, run.call_count)

    def test_remote_runner_reports_upload_failure_without_delete(self) -> None:
        args, _ = self._remote_run_fixture("remote_upload_error")
        results = [
            subprocess.CompletedProcess([args.ssh_executable], 0, stdout="created\n"),
            subprocess.CompletedProcess([args.rsync_executable], 12, stdout="failed\n"),
        ]
        seen: list[list[str]] = []

        def fail_upload(command: list[str], **_: object) -> subprocess.CompletedProcess:
            seen.append(list(command))
            return results[len(seen) - 1]

        with mock.patch.object(
            ptx, "_new_remote_invocation_id", return_value="run_0123456789abcdef"
        ), mock.patch.object(ptx.subprocess, "run", side_effect=fail_upload):
            with self.assertRaisesRegex(ptx.CrosscheckError, "input upload.*exit code 12"):
                ptx.run_crosscheck(args)
        self.assertEqual(2, len(seen))
        self.assertTrue(all(" rm " not in " ".join(command) for command in seen))

    def test_remote_identifiers_and_roots_are_fail_closed(self) -> None:
        for value in ("/", "/tmp", "/home/host/../escape", "/home/host/run;touch"):
            with self.subTest(root=value):
                with self.assertRaises(ptx.CrosscheckError):
                    ptx._validate_remote_root(value)
        for value in ("../run", "run/child", "-run_12345678", "run;touch"):
            with self.subTest(identifier=value):
                with self.assertRaises(ptx.CrosscheckError):
                    ptx._validate_remote_identifier(value)
        for value in ("-oProxyCommand=bad", "host;touch", "user@host other"):
            with self.subTest(target=value):
                with self.assertRaises(ptx.CrosscheckError):
                    ptx._validate_ssh_target(value)

    def test_merge_metrics_derives_gold_boolean(self) -> None:
        summary_path, _ = self._passing_summary()
        metrics_path = self.root / "metrics.json"
        output_path = self.root / "metrics_with_pt.json"
        metrics = {
            "checks": {
                "constraint_hash_unchanged": True,
                "functional_audit_passed": True,
                "primetime_crosscheck_passed": False,
            }
        }
        metrics_path.write_text(json.dumps(metrics) + "\n", encoding="utf-8")
        merged = ptx.merge_metrics(metrics_path, summary_path, output_path)
        self.assertTrue(merged["checks"]["primetime_crosscheck_passed"])
        self.assertTrue(merged["crosschecks"]["primetime"]["passed"])
        self.assertEqual(merged, json.loads(output_path.read_text(encoding="utf-8")))

    def test_margin_failure_is_not_gold(self) -> None:
        self._write_corner("setup", status="FAIL", wns=0.005)
        self._write_corner("hold", wns=0.02)
        summary = ptx.build_summary(self.report_dir, summary_root=self.root)
        self.assertFalse(summary["passed"])
        self.assertEqual("FAIL", summary["corners"]["setup"]["status"])

    def test_tcl_is_complete_and_has_required_real_tool_commands(self) -> None:
        source = ptx.DEFAULT_TCL.read_text(encoding="utf-8")
        for command in (
            "read_lib $liberty",
            "set loaded_libs [get_libs *]",
            "set main_lib [get_object_name $loaded_libs]",
            "set_app_var link_path [list * $main_lib]",
            "set_app_var link_create_black_boxes false",
            "read_verilog $netlist",
            "if {![link_design $top]}",
            "set linked_design [current_design]",
            "get_object_name $linked_design",
            "read_sdc $sdc",
            "set_units -time ns",
            "set_propagated_clock $clocks",
            "read_parasitics -format SPEF $spef",
            "get_timing_paths",
            "report_timing",
            "report_annotated_parasitics",
            "-list_not_annotated -ignore_partially_annotated",
            "report_analysis_coverage",
            "-include unconstrained_endpoints",
            "report_units -nosplit",
            "PT_CROSSCHECK_ERROR",
            "PT_SETUP_SDC",
            "PT_HOLD_SDC",
            "setting $sdc_setting",
            "list PT_SDC $opposite_sdc_setting",
            "lc_root $lc_root",
            "::env(SYNOPSYS_LC_ROOT)",
            "global PT_CFG is forbidden",
            "PT_CONFIG_TCL is forbidden",
        ):
            self.assertIn(command, source)
        self.assertNotIn("current_design $top", source)
        self.assertNotIn("setting PT_SDC", source)
        self.assertNotIn("source $config", source)
        self.assertNotIn("REPORT_COMMAND_FAILED", source)
        try:
            import tkinter

            interpreter = tkinter.Tcl()
        except (ImportError, RuntimeError, OSError) as exc:
            self.skipTest(f"Tcl interpreter unavailable: {exc}")
        self.assertEqual(1, int(interpreter.call("info", "complete", source)))


if __name__ == "__main__":
    unittest.main()

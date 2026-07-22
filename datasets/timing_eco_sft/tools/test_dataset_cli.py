#!/usr/bin/env python3
"""Lightweight tests for dataset_cli.py."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("dataset_cli.py")
SPEC = importlib.util.spec_from_file_location("dataset_cli", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
dataset_cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dataset_cli)
ptx = dataset_cli._primetime_verifier()


class DatasetCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pilot = Path(self.temp.name) / "pilot_10"
        self.addCleanup(self._make_staging_removable)
        cases = [
            *( (f"SETUP_{index:03d}", "setup") for index in range(1, 5) ),
            *( (f"HOLD_{index:03d}", "hold") for index in range(1, 5) ),
            *( (f"MIXED_{index:03d}", "mixed") for index in range(1, 3) ),
        ]
        for case_id, case_type in cases:
            self._write_case(case_id, case_type)

    def _make_staging_removable(self) -> None:
        if not self.pilot.exists():
            return
        for path in self.pilot.rglob("*"):
            if path.is_symlink():
                continue
            path.chmod(0o755 if path.is_dir() else 0o644)

    @staticmethod
    def _sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _artifact(self, case: Path, relative: str) -> dict[str, object]:
        path = case / relative
        if path.is_dir():
            inventory = dataset_cli._tree_inventory(path, relative)
            return {
                "path": relative,
                "kind": "directory",
                "sha256": dataset_cli._inventory_sha256(inventory),
                "bytes": sum(int(item["bytes"]) for item in inventory),
                "files": len(inventory),
            }
        return {
            "path": relative,
            "sha256": self._sha(path),
            "bytes": path.stat().st_size,
        }

    def _refresh_artifact(self, case: Path, role: str) -> None:
        manifest_path = case / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        relative = manifest["artifacts"][role]["path"]
        manifest["artifacts"][role] = self._artifact(case, relative)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def _restage_pt_inputs(
        self, case: Path, replacements: dict[str, Path]
    ) -> dict[str, object]:
        """Rebuild a verifier-valid PT bundle while changing selected sources."""

        summary_path = case / "primetime_crosscheck.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        sources = {
            role: Path(str(summary["inputs"][role]["source_path"]))
            for role in ptx.INPUT_ROLES
        }
        sources.update(replacements)
        top = str(summary["sdc_adaptation"]["top"])
        staged, inputs, adaptation = ptx._stage_input_bundle(
            sources, case, top=top
        )
        summary["inputs"] = inputs
        summary["sdc_adaptation"] = adaptation
        home = str(case.resolve())
        for mode in ("setup", "hold"):
            execution = summary["execution"][mode]
            execution["command"] = [
                ptx.DEFAULT_PT_SHELL,
                "-no_init",
                "-f",
                str(staged["tcl"]),
            ]
            execution["tcl_artifact"] = inputs["tcl"]["path"]
            execution["tcl_basename"] = staged["tcl"].name
            execution["home"] = home
            execution["working_directory"] = home
            sdc_role = ptx.SDC_ROLE_BY_MODE[mode]
            execution["sdc_artifact"] = inputs[sdc_role]["path"]
            execution["pt_sdc_artifact"] = adaptation[mode]["adapted_artifact"][
                "path"
            ]
            execution["pt_sdc_sha256"] = adaptation[mode]["adapted_artifact"][
                "sha256"
            ]
            execution["input_bindings"] = ptx._execution_input_bindings(
                mode, inputs, adaptation
            )
        ptx._write_json(summary_path, summary)

        metrics_path = case / "metrics.json"
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        metrics["crosschecks"]["primetime"] = summary
        metrics_path.write_text(
            json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        manifest_path = case / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"]["primetime_crosscheck"] = self._artifact(
            case, "primetime_crosscheck.json"
        )
        manifest["artifacts"]["metrics"] = self._artifact(case, "metrics.json")
        for mode in ("setup", "hold"):
            role = f"pt_input_{mode}_sdc_adapted"
            manifest["artifacts"][role] = self._artifact(
                case, adaptation[mode]["adapted_artifact"]["path"]
            )
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return summary

    def _rewrite_pt_summary(self, case: Path, summary: dict[str, object]) -> None:
        """Keep the finalizer's embedded PT crosscheck bound to a tampered fixture."""

        summary_path = case / "primetime_crosscheck.json"
        ptx._write_json(summary_path, summary)
        metrics_path = case / "metrics.json"
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        metrics["crosschecks"]["primetime"] = summary
        metrics_path.write_text(
            json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self._refresh_artifact(case, "primetime_crosscheck")
        self._refresh_artifact(case, "metrics")

    def _write_case(self, case_id: str, case_type: str) -> None:
        case = self.pilot / "cases" / case_id
        reports = case / "reports"
        reports.mkdir(parents=True)
        repair_mode = "native" if case_id in {"SETUP_004", "HOLD_004"} else "surgical"
        design = "NV_NVDLA_CMAC_CORE_mac"
        baseline_sha = hashlib.sha256(b"qualified baseline").hexdigest()
        catalog_sha = hashlib.sha256(b"frozen catalog").hexdigest()
        checksum_sha = hashlib.sha256(b"baseline checksum manifest").hexdigest()
        setup_lib = b"library(setup) {}\n"
        hold_lib = b"library(hold) {}\n"
        source_artifacts = {
            "setup_liberty_ss": {
                "sha256": hashlib.sha256(setup_lib).hexdigest(),
                "bytes": len(setup_lib),
                "source_path": "/qualified/setup.lib",
            },
            "hold_liberty_ff": {
                "sha256": hashlib.sha256(hold_lib).hexdigest(),
                "bytes": len(hold_lib),
                "source_path": "/qualified/hold.lib",
            },
        }
        qualification = {
            "schema_version": dataset_cli.BASELINE_QUALIFICATION_SCHEMA_VERSION,
            "status": "QUALIFIED_CANDIDATE",
            "gold_status": False,
            "signoff_eligible": False,
            "technology_classification": "derived_non_signoff",
            "top": design,
            "source_artifacts": source_artifacts,
            "promotion_bindings": {"source_inputs": source_artifacts},
        }
        (case / "baseline_qualification.json").write_text(
            json.dumps(qualification, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        qualification_sha = self._sha(case / "baseline_qualification.json")
        baseline_provenance = {
            "baseline_sha256": baseline_sha,
            "catalog_sha256": catalog_sha,
            "checksum_manifest_sha256": checksum_sha,
            "qualification_sha256": qualification_sha,
            "qualification_status": "QUALIFIED_CANDIDATE",
            "technology_classification": "derived_non_signoff",
            "gold_status": False,
            "signoff_eligible": False,
            "source_artifacts": source_artifacts,
        }
        setup_wns = -0.04 if case_type in {"setup", "mixed"} else 0.05
        setup_tns = -0.08 if setup_wns < 0 else 0.0
        hold_wns = -0.03 if case_type in {"hold", "mixed"} else 0.04
        hold_tns = -0.06 if hold_wns < 0 else 0.0
        new_instance_role = "HOLD" if case_type == "hold" else "SETUP"
        new_instance_name = f"SFT_ECO_{case_id}_{new_instance_role}_1"
        if repair_mode == "native":
            fix = (
                f"optDesign -postRoute -{case_type} -selectedTerms "
                "reports/native_selected_terms.txt -incr\n"
                "refinePlace -eco true\n"
                "ecoRoute\n"
            )
        else:
            fix = (
                f"set target_pin [get_pins {case_id}/D]\n"
                "if {[sizeof_collection $target_pin] == 0} { error \"missing target\" }\n"
                "setEcoMode -batchMode true\n"
                "ecoAddRepeater -term $target_pin -cell DLY4_X1M_A9TR40 "
                f"-name {new_instance_name}\n"
                "setEcoMode -batchMode false\n"
                "refinePlace -preserveRouting\n"
                "ecoRoute\n"
            )
        inject = f"ecoChangeCell -inst {case_id}/U_BAD -cell BUF_X0P5M_A9TR40\n"
        (case / "inject.tcl").write_text(inject, encoding="utf-8")
        (case / "fix.tcl").write_text(fix, encoding="utf-8")
        (case / "replay.tcl").write_text("source fix.tcl\n", encoding="utf-8")
        (case / "instruction.txt").write_text(
            f"请修复 {case_id} 的 {case_type} timing violation；独立 replay harness 将复查 QoR。",
            encoding="utf-8",
        )
        (case / "answer.txt").write_text(
            "该违例应在数据路径上做局部 ECO，并限制新增单元数量。修复后重新检查两个时序方向及物理规则。\n\n"
            f"```tcl\n{fix}```\n",
            encoding="utf-8",
        )
        report_names = {
            "setup_before": "setup_before.rpt",
            "hold_before": "hold_before.rpt",
            "setup_after": "setup_after.rpt",
            "hold_after": "hold_after.rpt",
            "drv_after": "drv_after.rpt",
            "connectivity_after": "connectivity_after.rpt",
            "drc_before": "drc_before.rpt",
            "drc_after": "drc_after.rpt",
        }
        for name in report_names.values():
            (reports / name).write_text(f"{case_id} evidence\n", encoding="utf-8")

        for replay_index, replay_root in (
            (1, case),
            (2, case / "evidence" / "replay_2"),
        ):
            replay_reports = replay_root / "reports"
            replay_reports.mkdir(parents=True, exist_ok=True)
            replay_logs = replay_root / "logs"
            replay_logs.mkdir(parents=True, exist_ok=True)
            (replay_logs / "innovus.log").write_text(
                "Cadence Innovus(TM) Implementation System.\n"
                "Version:\tv21.10-p004_1, built Tue May 18 2021\n",
                encoding="utf-8",
            )
            (replay_logs / "host_ssh.log").write_text(
                f"host transport evidence for replay {replay_index}\n"
                f"SFT_CASE_PASSED {case_id}\n",
                encoding="utf-8",
            )
            guard = {
                "schema_version": dataset_cli.BASELINE_GUARD_SCHEMA_VERSION,
                "case_id": case_id,
                "minimum_wns_ns": 0.02,
                "baseline_sha256": baseline_sha,
                "catalog_sha256": catalog_sha,
                "qualification_sha256": qualification_sha,
                "checksum_manifest_sha256": checksum_sha,
                "qualification_status": "QUALIFIED_CANDIDATE",
                "technology_classification": "derived_non_signoff",
                "gold_status": False,
                "signoff_eligible": False,
                "setup": {"wns_ns": 0.03, "tns_ns": 0.0},
                "hold": {"wns_ns": 0.03, "tns_ns": 0.0},
                "passed": True,
            }
            (replay_reports / "baseline_guard.json").write_text(
                json.dumps(guard, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            physical = {
                "schema_version": dataset_cli.PHYSICAL_NO_REGRESSION_SCHEMA_VERSION,
                "case_id": case_id,
                "drv_before": {
                    "max_transition": 0,
                    "max_capacitance": 0,
                    "max_fanout": 0,
                },
                "drv_after": {
                    "max_transition": 0,
                    "max_capacitance": 0,
                    "max_fanout": 0,
                },
                "drc_before": {"total": 0, "categories": {}},
                "drc_after": {"total": 0, "categories": {}},
                "per_category_no_regression": True,
                "passed": True,
            }
            (replay_reports / "physical_no_regression.json").write_text(
                json.dumps(physical, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            binding = {
                key: baseline_provenance[key]
                for key in (
                    "baseline_sha256",
                    "catalog_sha256",
                    "qualification_sha256",
                    "checksum_manifest_sha256",
                    "qualification_status",
                    "technology_classification",
                )
            }
            injection_provenance = {
                "schema_version": dataset_cli.INJECTION_PROVENANCE_SCHEMA_VERSION,
                "case_id": case_id,
                "strategy": "fixture_injection",
                "calibration_status": "FROZEN",
                "status": "passed",
                "reason": "one frozen attempt passed",
                "baseline_binding": binding,
                "target_wns_ns": {
                    "setup": [-0.05, -0.02] if setup_wns < 0 else [0.0, 0.0],
                    "hold": [-0.04, -0.02] if hold_wns < 0 else [0.0, 0.0],
                },
                "attempts": [
                    {
                        "attempt": 1,
                        "parameter": "fixture=1",
                        "setup_wns_ns": setup_wns,
                        "setup_tns_ns": setup_tns,
                        "hold_wns_ns": hold_wns,
                        "hold_tns_ns": hold_tns,
                    }
                ],
            }
            (replay_reports / "injection_provenance.json").write_text(
                json.dumps(injection_provenance, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            run_status = {
                "replay": replay_index,
                "innovus_exit_code": 0,
                "fetched": True,
                "passed": True,
                "expected_marker": "SFT_CASE_PASSED",
                "marker_present": True,
                "baseline_sha256": baseline_sha,
                "catalog_sha256": catalog_sha,
                "baseline_qualification_sha256": qualification_sha,
                "baseline_checksum_manifest_sha256": checksum_sha,
                "baseline_status": "QUALIFIED_CANDIDATE",
                "guest_baseline_checksums_verified": True,
                "mode": "GOLD_REPLAY",
                "gold_eligible": True,
            }
            (replay_root / "run_status.json").write_text(
                json.dumps(run_status, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            (replay_root / "SFT_CASE_PASSED").write_text(
                f"{case_id} replay {replay_index} completed\n", encoding="utf-8"
            )
            (replay_root / "violating.enc").write_text(
                "if {[is_common_ui_mode]} {\n"
                "  read_db ./violating.enc.dat\n"
                "} else {\n"
                f"  restoreDesign ./violating.enc.dat {design}\n"
                "}\n",
                encoding="utf-8",
            )
            checkpoint = replay_root / "violating.enc.dat"
            checkpoint.mkdir(parents=True)
            (checkpoint / "db.bin").write_bytes(
                f"{case_id}-checkpoint-{replay_index}".encode("utf-8")
            )
            (replay_root / "before.v").write_text(
                f"// replay {replay_index}\nmodule {design}(input clk);\nendmodule\n",
                encoding="utf-8",
            )
            spef = (
                "*SPEF IEEE 1481-1998\n"
                f'*DESIGN "{design}"\n'
                "*D_NET fixture_net 0.001\n*CONN\n*END\n"
            )
            (replay_root / "setup_before.spef").write_text(spef, encoding="utf-8")
            (replay_root / "hold_before.spef").write_text(spef, encoding="utf-8")
            check_design = replay_reports / "check_design_after"
            check_design.mkdir()
            (check_design / f"{design}.main.htm.ascii").write_text(
                f"# Design: {design}\n"
                "# Command: checkDesign -all -outDir reports/check_design_after\n"
                "checkDesign -all -outDir completed\n",
                encoding="utf-8",
            )

        setup_constraint = (
            f"current_design {design}\n"
            "create_clock -period 2.4 [get_ports clk]\n"
        )
        hold_constraint = setup_constraint + "# hold-view snapshot\n"
        (reports / "constraint_before.sdc").write_text(setup_constraint, encoding="utf-8")
        (reports / "constraint_after.sdc").write_text(setup_constraint, encoding="utf-8")
        for stage in ("before", "after"):
            (reports / f"constraint_setup_{stage}.sdc").write_text(
                setup_constraint, encoding="utf-8"
            )
            (reports / f"constraint_hold_{stage}.sdc").write_text(
                hold_constraint, encoding="utf-8"
            )
        constraint_hashes = {
            "setup": self._sha(reports / "constraint_setup_before.sdc"),
            "hold": self._sha(reports / "constraint_hold_before.sdc"),
        }
        constraint_sha = constraint_hashes["setup"]
        audit = {
            "schema_version": dataset_cli.FUNCTIONAL_AUDIT_SCHEMA_VERSION,
            "case_id": case_id,
            "design": design,
            "repair_mode": repair_mode,
            "passed": True,
            "checks": {
                "fixed_netlist_written": True,
                "constraints_unchanged": {"passed": True},
                "no_illegal_eco": True,
                "connectivity_clean": True,
                "check_design_completed": True,
                "cell_budget_respected": True,
                "matching_corner_spef_written": True,
            },
        }
        (reports / "functional_audit.json").write_text(
            json.dumps(audit, indent=2) + "\n", encoding="utf-8"
        )
        if repair_mode == "native":
            for native_reports in (
                reports,
                case / "evidence" / "replay_2" / "reports",
            ):
                (native_reports / "native_cell_diff.tcl").write_text(
                    "# Exact native leaf-cell diff.\n"
                    f"set ::SFT_NATIVE_CELL_DIFF {{{{{case_id}/U1 BUF_X1 BUF_X2}}}}\n",
                    encoding="utf-8",
                )

        targets = []
        if case_type in {"setup", "mixed"}:
            for index in range(2):
                targets.append(
                    {
                        "role": "setup_primary",
                        "timing": "late",
                        "endpoint": f"{case_id}/SETUP_D{index}",
                        "beginpoint": f"{case_id}/SETUP_Q{index}",
                        "slack_ns": -0.04,
                        "net": f"{case_id}/setup_net_{index}",
                        "driver_pin": f"{case_id}/U_SETUP_DRV_{index}/Y",
                        "driver_inst": f"{case_id}/U_SETUP_DRV_{index}",
                        "driver_ref": "BUF_X1M_A9TR40",
                        "original_driver": {
                            "inst": f"{case_id}/U_SETUP_DRV_{index}",
                            "ref": "BUF_X1M_A9TR40",
                        },
                        "local_cells": [
                            {
                                "inst": f"{case_id}/U_SETUP_DRV_{index}",
                                "ref": "BUF_X1M_A9TR40",
                            }
                        ],
                    }
                )
        if case_type in {"hold", "mixed"}:
            for index in range(2):
                targets.append(
                    {
                        "role": "hold_primary",
                        "timing": "early",
                        "endpoint": f"{case_id}/HOLD_D{index}",
                        "beginpoint": f"{case_id}/HOLD_Q{index}",
                        "slack_ns": -0.03,
                        "net": f"{case_id}/hold_net_{index}",
                        "driver_pin": f"{case_id}/U_HOLD_DRV_{index}/Y",
                        "driver_inst": f"{case_id}/U_HOLD_DRV_{index}",
                        "driver_ref": "BUF_X1M_A9TR40",
                        "original_driver": {
                            "inst": f"{case_id}/U_HOLD_DRV_{index}",
                            "ref": "BUF_X1M_A9TR40",
                        },
                        "local_cells": [
                            {
                                "inst": f"{case_id}/U_HOLD_DRV_{index}",
                                "ref": "BUF_X1M_A9TR40",
                            }
                        ],
                    }
                )
        if repair_mode == "native":
            terms_text = "".join(f"{target['endpoint']}\n" for target in targets)
            for native_reports in (
                reports,
                case / "evidence" / "replay_2" / "reports",
            ):
                (native_reports / "native_selected_terms.txt").write_text(
                    terms_text, encoding="utf-8"
                )
        diagnostic = {
            "schema_version": dataset_cli.DIAGNOSTIC_CONTEXT_SCHEMA_VERSION,
            "case_id": case_id,
            "design": design,
            "max_eco_cells": 1,
            "targets": targets,
        }
        (reports / "diagnostic_context.json").write_text(
            json.dumps(diagnostic, indent=2) + "\n", encoding="utf-8"
        )
        locality = {
            "schema_version": dataset_cli.VIOLATION_LOCALITY_SCHEMA_VERSION,
            "case_id": case_id,
            "repair_mode": repair_mode,
        }
        for check, timing in (("setup", "late"), ("hold", "early")):
            endpoints = sorted(
                target["endpoint"] for target in targets if target["timing"] == timing
            )
            target_slacks = [
                float(target["slack_ns"])
                for target in targets
                if target["timing"] == timing
            ]
            clean_wns = 0.05 if check == "setup" else 0.04
            locality[check] = {
                "required": bool(endpoints),
                "wns_ns": min(target_slacks) if target_slacks else clean_wns,
                "tns_ns": sum(target_slacks) if target_slacks else 0.0,
                "expected_endpoint_count": len(endpoints),
                "selected_endpoint_count": len(endpoints),
                "violating_endpoint_count": len(endpoints),
                "selected_endpoints": endpoints,
                "selected_endpoint_slacks": sorted(
                    (
                        {
                            "endpoint": target["endpoint"],
                            "slack_ns": float(target["slack_ns"]),
                        }
                        for target in targets
                        if target["timing"] == timing
                    ),
                    key=lambda item: item["endpoint"],
                ),
                "violating_endpoints": endpoints,
                "cardinality_passed": True,
                "locality_passed": True,
                "coverage_passed": True,
                "opposite_headroom_passed": True,
                "selected_slack_consistency_passed": True,
            }
        locality["passed"] = True
        locality["reasons"] = []
        (reports / "violation_locality.json").write_text(
            json.dumps(locality, indent=2) + "\n", encoding="utf-8"
        )
        target_lines = []
        for target in targets:
            target_lines.append(
                f"role={target['role']}; timing={target['timing']}; "
                f"slack={target['slack_ns']:+.3f} ns; endpoint={target['endpoint']}; "
                f"beginpoint={target['beginpoint']}; net={target['net']}; "
                f"driver_pin={target['driver_pin']}; driver_inst={target['driver_inst']}; "
                f"driver_ref={target['driver_ref']}; "
                "local_cells=["
                + ",".join(
                    f"{cell['inst']}(ref={cell['ref']})"
                    for cell in target["local_cells"]
                )
                + "]"
            )
        (case / "instruction.txt").write_text(
            f"请修复 {case_id} 的 {case_type} timing violation；ECO cell 上限为 1。\n"
            f"新增实例命名 SFT_ECO_{case_id}_<SETUP|HOLD>_<ordinal>，ordinal 连续。\n"
            + "\n".join(target_lines)
            + "。"
            "独立 replay harness 将复查 QoR。",
            encoding="utf-8",
        )

        comparison = {
            "schema_version": dataset_cli.REPLAY_COMPARISON_SCHEMA_VERSION,
            "case_id": case_id,
            "status": "passed",
            "runs": 2,
            "deterministic": True,
            "constraint_sdc_sha256": constraint_sha,
            "constraint_sdc_sha256_by_mode": constraint_hashes,
            "concrete_fix_sha256": self._sha(case / "fix.tcl"),
            "diagnostic_context_sha256": self._sha(
                reports / "diagnostic_context.json"
            ),
            "violation_locality_sha256": self._sha(
                reports / "violation_locality.json"
            ),
            "baseline_provenance": baseline_provenance,
            "baseline_guard_sha256": [
                self._sha(reports / "baseline_guard.json"),
                self._sha(
                    case / "evidence/replay_2/reports/baseline_guard.json"
                ),
            ],
            "physical_no_regression_sha256": self._sha(
                reports / "physical_no_regression.json"
            ),
            "run_status_sha256": [
                self._sha(case / "run_status.json"),
                self._sha(case / "evidence/replay_2/run_status.json"),
            ],
            "injection_provenance_sha256": [
                self._sha(reports / "injection_provenance.json"),
                self._sha(
                    case / "evidence/replay_2/reports/injection_provenance.json"
                ),
            ],
            "violating_checkpoint_sha256": [
                self._sha(case / "violating.enc"),
                self._sha(case / "evidence/replay_2/violating.enc"),
            ],
            "violating_checkpoint_tree_sha256": [
                dataset_cli._tree_sha256(
                    case / "violating.enc.dat", "violating checkpoint"
                ),
                dataset_cli._tree_sha256(
                    case / "evidence/replay_2/violating.enc.dat",
                    "replay 2 violating checkpoint",
                ),
            ],
            "before_netlist_raw_sha256": [
                self._sha(case / "before.v"),
                self._sha(case / "evidence/replay_2/before.v"),
            ],
            "normalized_before_netlist_sha256": dataset_cli._normalized_verilog_sha256(
                case / "before.v", design, "before netlist"
            ),
            "setup_spef_before_sha256": [
                self._sha(case / "setup_before.spef"),
                self._sha(case / "evidence/replay_2/setup_before.spef"),
            ],
            "hold_spef_before_sha256": [
                self._sha(case / "hold_before.spef"),
                self._sha(case / "evidence/replay_2/hold_before.spef"),
            ],
            "check_design_primary_sha256": [
                self._sha(reports / f"check_design_after/{design}.main.htm.ascii"),
                self._sha(
                    case
                    / f"evidence/replay_2/reports/check_design_after/{design}.main.htm.ascii"
                ),
            ],
            "check_design_tree_sha256": [
                dataset_cli._tree_sha256(
                    reports / "check_design_after", "checkDesign"
                ),
                dataset_cli._tree_sha256(
                    case / "evidence/replay_2/reports/check_design_after",
                    "replay 2 checkDesign",
                ),
            ],
        }
        if repair_mode == "native":
            comparison["native_cell_diff_sha256"] = self._sha(
                reports / "native_cell_diff.tcl"
            )
            comparison["native_selected_terms_sha256"] = self._sha(
                reports / "native_selected_terms.txt"
            )
        (case / "replay_comparison.json").write_text(
            json.dumps(comparison, indent=2) + "\n", encoding="utf-8"
        )
        pt_report_dir = reports / "primetime"
        pt_report_dir.mkdir()
        for mode in ("setup", "hold"):
            delay_type = "max" if mode == "setup" else "min"
            (pt_report_dir / f"{mode}.result").write_text(
                "PT_CROSSCHECK_V1 "
                f"status=PASS mode={mode} delay_type={delay_type} top={design} "
                "wns_ns=0.020000000 tns_ns=0.000000000 violating_paths=0 "
                "threshold_ns=0.010000000 parasitics_read=1 "
                "propagated_clocks=1 clock_count=1 "
                "tool_version=R-2020.09-SP4 "
                f"lc_root={ptx.DEFAULT_SYNOPSYS_LC_ROOT}\n",
                encoding="utf-8",
            )
            for report_role, pattern in ptx.REPORT_FILES.items():
                (pt_report_dir / pattern.format(mode=mode)).write_text(
                    f"{case_id} PrimeTime {mode} {report_role} evidence\n",
                    encoding="utf-8",
                )

        pt_source_dir = case / "pt_fixture_sources"
        pt_source_dir.mkdir()
        pt_sources = {
            "tcl": pt_source_dir / ptx.STAGED_INPUT_FILENAMES["tcl"],
            "netlist": pt_source_dir / ptx.STAGED_INPUT_FILENAMES["netlist"],
            "setup_sdc": reports / "constraint_setup_after.sdc",
            "hold_sdc": reports / "constraint_hold_after.sdc",
            "setup_lib": pt_source_dir / ptx.STAGED_INPUT_FILENAMES["setup_lib"],
            "hold_lib": pt_source_dir / ptx.STAGED_INPUT_FILENAMES["hold_lib"],
            "setup_spef": pt_source_dir / ptx.STAGED_INPUT_FILENAMES["setup_spef"],
            "hold_spef": pt_source_dir / ptx.STAGED_INPUT_FILENAMES["hold_spef"],
        }
        pt_sources["tcl"].write_bytes(ptx.DEFAULT_TCL.read_bytes())
        pt_sources["netlist"].write_text(
            f"module {design}(input clk); endmodule\n", encoding="utf-8"
        )
        pt_sources["setup_lib"].write_bytes(setup_lib)
        pt_sources["hold_lib"].write_bytes(hold_lib)
        for mode in ("setup", "hold"):
            pt_sources[f"{mode}_spef"].write_text(
                "*SPEF IEEE 1481-1998\n"
                f'*DESIGN "{design}"\n'
                f"*D_NET {mode}_fixture 0.001\n*CONN\n*END\n",
                encoding="utf-8",
            )

        staged, pt_inputs, sdc_adaptation = ptx._stage_input_bundle(
            pt_sources, case, top=design
        )
        pt_summary = ptx.build_summary(
            pt_report_dir,
            summary_root=case,
            expected_top=design,
        )
        pt_summary["inputs"] = pt_inputs
        pt_summary["sdc_adaptation"] = sdc_adaptation
        pt_logs = case / "logs"
        pt_logs.mkdir(exist_ok=True)
        pt_summary["execution"] = {}
        pt_home = str(case.resolve())
        for mode in ("setup", "hold"):
            log = pt_logs / f"primetime_{mode}.log"
            log.write_text(f"PrimeTime {mode} completed\n", encoding="utf-8")
            pt_summary["execution"][mode] = {
                "backend": "local",
                "mode": mode,
                "command": [
                    ptx.DEFAULT_PT_SHELL,
                    "-no_init",
                    "-f",
                    str(staged["tcl"]),
                ],
                "tcl_artifact": pt_inputs["tcl"]["path"],
                "tcl_basename": staged["tcl"].name,
                "sdc_role": ptx.SDC_ROLE_BY_MODE[mode],
                "sdc_environment_variable": ptx.SDC_ENV_BY_MODE[mode],
                "sdc_artifact": pt_inputs[ptx.SDC_ROLE_BY_MODE[mode]]["path"],
                "pt_sdc_artifact": sdc_adaptation[mode]["adapted_artifact"]["path"],
                "pt_sdc_sha256": sdc_adaptation[mode]["adapted_artifact"]["sha256"],
                "synopsys_lc_root": ptx.DEFAULT_SYNOPSYS_LC_ROOT,
                "home": pt_home,
                "working_directory": pt_home,
                "input_bindings": ptx._execution_input_bindings(
                    mode, pt_inputs, sdc_adaptation
                ),
                "exit_code": 0,
                "timed_out": False,
                "log": ptx._artifact(log, case, f"{mode} log"),
            }
        ptx._write_json(case / "primetime_crosscheck.json", pt_summary)

        setup_wns = -0.04 if case_type in {"setup", "mixed"} else 0.05
        setup_tns = -0.08 if setup_wns < 0 else 0.0
        hold_wns = -0.03 if case_type in {"hold", "mixed"} else 0.04
        hold_tns = -0.06 if hold_wns < 0 else 0.0
        stage_common = {
            "drv": {
                "max_transition_violations": 0,
                "max_capacitance_violations": 0,
                "max_fanout_violations": 0,
            },
            "drc": {"total": 0, "categories": {}},
            "connectivity": {"violations": 0},
        }
        metrics = {
            "schema_version": dataset_cli.FINALIZER_SCHEMA_VERSION,
            "before": {
                "setup": {"wns_ns": setup_wns, "tns_ns": setup_tns},
                "hold": {"wns_ns": hold_wns, "tns_ns": hold_tns},
                **stage_common,
            },
            "after": {
                "setup": {"wns_ns": 0.02, "tns_ns": 0.0},
                "hold": {"wns_ns": 0.02, "tns_ns": 0.0},
                **stage_common,
            },
            "replay": {"status": "passed", "runs": 2, "deterministic": True},
            "checks": {
                "constraint_hash_unchanged": True,
                "functional_audit_passed": True,
                "primetime_crosscheck_passed": True,
            },
            "crosschecks": {
                "replay": comparison,
                "constraints": {
                    "sha256": constraint_sha,
                    "sha256_by_mode": constraint_hashes,
                },
                "functional_audit": {
                    "schema_version": dataset_cli.FUNCTIONAL_AUDIT_SCHEMA_VERSION,
                    "passed": True,
                },
                "primetime": pt_summary,
                "diagnostic_context": {
                    "schema_version": dataset_cli.DIAGNOSTIC_CONTEXT_SCHEMA_VERSION,
                    "sha256": self._sha(reports / "diagnostic_context.json"),
                },
                "violation_locality": {
                    "schema_version": dataset_cli.VIOLATION_LOCALITY_SCHEMA_VERSION,
                    "sha256": self._sha(reports / "violation_locality.json"),
                },
                "baseline_provenance": {
                    **baseline_provenance,
                    "baseline_guard_sha256": comparison["baseline_guard_sha256"],
                    "qualification_artifact_sha256": qualification_sha,
                },
                "physical_no_regression": {
                    "schema_version": dataset_cli.PHYSICAL_NO_REGRESSION_SCHEMA_VERSION,
                    "sha256": comparison["physical_no_regression_sha256"],
                },
            },
        }
        (case / "metrics.json").write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        artifact_paths = {
            "baseline_qualification": "baseline_qualification.json",
            "inject_tcl": "inject.tcl",
            "fix_tcl": "fix.tcl",
            "replay_tcl": "replay.tcl",
            "metrics": "metrics.json",
            **{role: f"reports/{name}" for role, name in report_names.items()},
            "run_log": "logs/innovus.log",
            "host_ssh_log": "logs/host_ssh.log",
            "constraint_before": "reports/constraint_before.sdc",
            "constraint_after": "reports/constraint_after.sdc",
            "constraint_setup_before": "reports/constraint_setup_before.sdc",
            "constraint_setup_after": "reports/constraint_setup_after.sdc",
            "constraint_hold_before": "reports/constraint_hold_before.sdc",
            "constraint_hold_after": "reports/constraint_hold_after.sdc",
            "functional_audit": "reports/functional_audit.json",
            "injection_provenance": "reports/injection_provenance.json",
            "baseline_guard": "reports/baseline_guard.json",
            "physical_no_regression": "reports/physical_no_regression.json",
            "run_status": "run_status.json",
            "success_marker": "SFT_CASE_PASSED",
            "violating_checkpoint": "violating.enc",
            "violating_checkpoint_data": "violating.enc.dat",
            "before_netlist": "before.v",
            "setup_spef_before": "setup_before.spef",
            "hold_spef_before": "hold_before.spef",
            "check_design_after": "reports/check_design_after",
            "diagnostic_context": "reports/diagnostic_context.json",
            "violation_locality": "reports/violation_locality.json",
            "primetime_crosscheck": "primetime_crosscheck.json",
            "pt_input_setup_sdc": "reports/constraint_setup_after.sdc",
            "pt_input_hold_sdc": "reports/constraint_hold_after.sdc",
            "pt_input_setup_sdc_adapted": sdc_adaptation["setup"][
                "adapted_artifact"
            ]["path"],
            "pt_input_hold_sdc_adapted": sdc_adaptation["hold"][
                "adapted_artifact"
            ]["path"],
            "replay_comparison": "replay_comparison.json",
            "instruction": "instruction.txt",
            "answer": "answer.txt",
            "replay_2_injection_provenance": (
                "evidence/replay_2/reports/injection_provenance.json"
            ),
            "replay_2_baseline_guard": (
                "evidence/replay_2/reports/baseline_guard.json"
            ),
            "replay_2_physical_no_regression": (
                "evidence/replay_2/reports/physical_no_regression.json"
            ),
            "replay_2_run_status": "evidence/replay_2/run_status.json",
            "replay_2_host_ssh_log": "evidence/replay_2/logs/host_ssh.log",
            "replay_2_success_marker": "evidence/replay_2/SFT_CASE_PASSED",
            "replay_2_violating_checkpoint": "evidence/replay_2/violating.enc",
            "replay_2_violating_checkpoint_data": "evidence/replay_2/violating.enc.dat",
            "replay_2_before_netlist": "evidence/replay_2/before.v",
            "replay_2_setup_spef_before": "evidence/replay_2/setup_before.spef",
            "replay_2_hold_spef_before": "evidence/replay_2/hold_before.spef",
            "replay_2_check_design_after": (
                "evidence/replay_2/reports/check_design_after"
            ),
        }
        if repair_mode == "native":
            artifact_paths["native_cell_diff"] = "reports/native_cell_diff.tcl"
            artifact_paths["native_selected_terms"] = (
                "reports/native_selected_terms.txt"
            )
            artifact_paths["replay_2_native_cell_diff"] = (
                "evidence/replay_2/reports/native_cell_diff.tcl"
            )
            artifact_paths["replay_2_native_selected_terms"] = (
                "evidence/replay_2/reports/native_selected_terms.txt"
            )
        artifacts = {
            role: self._artifact(case, relative) for role, relative in artifact_paths.items()
        }
        manifest = {
            "id": case_id,
            "type": case_type,
            "difficulty": "medium",
            "design": design,
            "tool_version": "Innovus 21.10-p004_1",
            "analysis_views": {"setup": "func_ss", "hold": "func_ff"},
            "repair_mode": repair_mode,
            "max_eco_cells": 1,
            "status": "gold",
            "finalizer_schema": dataset_cli.FINALIZER_SCHEMA_VERSION,
            "baseline_provenance": baseline_provenance,
            "artifacts": artifacts,
        }
        (case / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def test_export_and_validate_round_trip(self) -> None:
        output = self.pilot / "dataset.jsonl"
        records = dataset_cli.export_dataset(self.pilot, output)
        self.assertEqual(10, len(records))
        self.assertEqual(records, dataset_cli.validate_dataset(self.pilot, output))
        self.assertNotIn("ecoChangeCell -inst", output.read_text(encoding="utf-8"))

    def test_rejects_wrong_case_mix(self) -> None:
        manifest_path = self.pilot / "cases" / "MIXED_002" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["type"] = "hold"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(dataset_cli.DatasetError, "does not match type"):
            dataset_cli.build_records(self.pilot)

    def test_rejects_injection_leak(self) -> None:
        case = self.pilot / "cases" / "SETUP_001"
        inject = (case / "inject.tcl").read_text(encoding="utf-8")
        with (case / "instruction.txt").open("a", encoding="utf-8") as stream:
            stream.write("\n" + inject)
        self._refresh_artifact(case, "instruction")
        with self.assertRaisesRegex(dataset_cli.DatasetError, "inject.tcl content"):
            dataset_cli.build_records(self.pilot)

    def test_rejects_original_driver_provenance_leak(self) -> None:
        case = self.pilot / "cases" / "SETUP_001"
        with (case / "instruction.txt").open("a", encoding="utf-8") as stream:
            stream.write("\noriginal_driver_inst=SETUP_001/U_PRE_INJECTION")
        self._refresh_artifact(case, "instruction")
        with self.assertRaisesRegex(
            dataset_cli.DatasetError, "pre-injection original-driver provenance"
        ):
            dataset_cli.build_records(self.pilot)

    def test_rejects_stale_jsonl(self) -> None:
        output = self.pilot / "dataset.jsonl"
        dataset_cli.export_dataset(self.pilot, output)
        records = dataset_cli.read_dataset(output)
        records[0]["messages"][1]["content"] += " stale"
        output.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in records) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(dataset_cli.DatasetError, "not the canonical export"):
            dataset_cli.validate_dataset(self.pilot, output)

    def test_rejects_local_constraint_change_in_fix(self) -> None:
        case = self.pilot / "cases" / "SETUP_001"
        fix_path = case / "fix.tcl"
        old_fix = fix_path.read_text(encoding="utf-8")
        new_fix = old_fix + "set_max_delay 9.0 -to [get_pins SETUP_001/D]\n"
        fix_path.write_text(new_fix, encoding="utf-8")
        answer_path = case / "answer.txt"
        answer_path.write_text(
            answer_path.read_text(encoding="utf-8").replace(old_fix, new_fix),
            encoding="utf-8",
        )
        self._refresh_artifact(case, "fix_tcl")
        self._refresh_artifact(case, "answer")
        with self.assertRaisesRegex(dataset_cli.DatasetError, "set_max_delay"):
            dataset_cli.build_records(self.pilot)

    def test_rejects_non_finalizer_gold_manifest(self) -> None:
        path = self.pilot / "cases" / "SETUP_001" / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        for key, value, message in (
            ("status", "prepared", "status must be 'gold'"),
            ("finalizer_schema", "handwritten.v1", "finalizer_schema"),
        ):
            with self.subTest(key=key):
                original = manifest[key]
                manifest[key] = value
                path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaisesRegex(dataset_cli.DatasetError, message):
                    dataset_cli.build_records(self.pilot)
                manifest[key] = original
        path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_rejects_missing_or_path_only_strong_evidence(self) -> None:
        path = self.pilot / "cases" / "SETUP_001" / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        original_manifest = json.loads(path.read_text(encoding="utf-8"))
        del manifest["artifacts"]["functional_audit"]
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(dataset_cli.DatasetError, "functional_audit"):
            dataset_cli.build_records(self.pilot)

        manifest = original_manifest
        manifest["artifacts"]["constraint_before"] = "reports/constraint_before.sdc"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(dataset_cli.DatasetError, "finalizer artifact object"):
            dataset_cli.build_records(self.pilot)

    def test_rejects_unbound_strong_evidence(self) -> None:
        case = self.pilot / "cases" / "SETUP_001"
        comparison_path = case / "replay_comparison.json"
        comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
        comparison["constraint_sdc_sha256"] = "0" * 64
        comparison_path.write_text(json.dumps(comparison) + "\n", encoding="utf-8")
        self._refresh_artifact(case, "replay_comparison")
        with self.assertRaisesRegex(dataset_cli.DatasetError, "not bound to the SDC"):
            dataset_cli.build_records(self.pilot)

    def test_native_case_requires_typed_cell_diff(self) -> None:
        case = self.pilot / "cases" / "SETUP_004"
        manifest_path = case / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        del manifest["artifacts"]["native_cell_diff"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(dataset_cli.DatasetError, "native_cell_diff"):
            dataset_cli.build_records(self.pilot)

    def test_native_case_requires_selected_terms_artifacts(self) -> None:
        case = self.pilot / "cases" / "SETUP_004"
        manifest_path = case / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        del manifest["artifacts"]["native_selected_terms"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(dataset_cli.DatasetError, "native_selected_terms"):
            dataset_cli.build_records(self.pilot)

    def test_rejects_unexposed_existing_fix_instance(self) -> None:
        case = self.pilot / "cases" / "SETUP_001"
        context = dataset_cli._load_diagnostic_context(
            case / "reports/diagnostic_context.json",
            case_id="SETUP_001",
            design="NV_NVDLA_CMAC_CORE_mac",
            max_eco_cells=1,
        )
        with self.assertRaisesRegex(
            dataset_cli.DatasetError, "instances absent from diagnostic local_cells"
        ):
            dataset_cli._validate_fix_object_coverage(
                "ecoChangeCell -inst SETUP_001/U_HIDDEN -cell BUF_X2M_A9TR40\n",
                context,
                case_id="SETUP_001",
                case_type="setup",
                max_eco_cells=1,
            )

    def test_rejects_diagnostic_local_chain_that_leaks_injection(self) -> None:
        case = self.pilot / "cases" / "SETUP_001"
        path = case / "reports/diagnostic_context.json"
        context = json.loads(path.read_text(encoding="utf-8"))
        target = context["targets"][0]
        target["driver_inst"] = "SFT_ECO_INJECT_1"
        target["original_driver"]["inst"] = "SFT_ECO_INJECT_1"
        target["local_cells"][0]["inst"] = "SFT_ECO_INJECT_1"
        path.write_text(json.dumps(context) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(dataset_cli.DatasetError, "injection terminology"):
            dataset_cli._load_diagnostic_context(
                path,
                case_id="SETUP_001",
                design="NV_NVDLA_CMAC_CORE_mac",
                max_eco_cells=1,
            )

    def test_rejects_invalid_new_eco_naming_contract(self) -> None:
        case = self.pilot / "cases" / "SETUP_001"
        context = dataset_cli._load_diagnostic_context(
            case / "reports/diagnostic_context.json",
            case_id="SETUP_001",
            design="NV_NVDLA_CMAC_CORE_mac",
            max_eco_cells=1,
        )
        with self.assertRaisesRegex(dataset_cli.DatasetError, "declared convention"):
            dataset_cli._validate_fix_object_coverage(
                "ecoAddRepeater -term SETUP_001/SETUP_D0 "
                "-cell DLY4_X1M_A9TR40 -name SFT_ECO_WRONG_SETUP_1\n",
                context,
                case_id="SETUP_001",
                case_type="setup",
                max_eco_cells=1,
            )
        allowed = dataset_cli._validate_fix_object_coverage(
            "ecoAddRepeater -term SETUP_001/SETUP_D0 "
            "-cell DLY4_X1M_A9TR40 -name SFT_ECO_SETUP_001_SETUP_1\n",
            context,
            case_id="SETUP_001",
            case_type="setup",
            max_eco_cells=1,
        )
        with self.assertRaisesRegex(dataset_cli.DatasetError, "undeclared ECO instance"):
            dataset_cli._validate_answer_eco_names(
                "SFT_ECO_SETUP_001_SETUP_7",
                allowed,
                case_id="SETUP_001",
            )

        hierarchical = json.loads(json.dumps(context))
        hierarchical["targets"][0]["local_cells"].append(
            {
                "inst": "u_exp/SFT_ECO_SETUP_001_PATH_1",
                "ref": "DLY2_X4M_A9TR40",
            }
        )
        hierarchical_allowed = dataset_cli._validate_fix_object_coverage(
            "ecoChangeCell -inst u_exp/SFT_ECO_SETUP_001_PATH_1 "
            "-cell BUF_X0P7M_A9TR40\n",
            hierarchical,
            case_id="SETUP_001",
            case_type="setup",
            max_eco_cells=1,
        )
        dataset_cli._validate_answer_eco_names(
            "u_exp/SFT_ECO_SETUP_001_PATH_1",
            hierarchical_allowed,
            case_id="SETUP_001",
        )
        with self.assertRaisesRegex(dataset_cli.DatasetError, "undeclared ECO instance"):
            dataset_cli._validate_answer_eco_names(
                "u_other/SFT_ECO_SETUP_001_PATH_1",
                hierarchical_allowed,
                case_id="SETUP_001",
            )

    def test_rejects_tampered_checkpoint_tree_and_replay_status(self) -> None:
        case = self.pilot / "cases" / "SETUP_001"
        checkpoint = case / "evidence/replay_2/violating.enc.dat/db.bin"
        original_checkpoint = checkpoint.read_bytes()
        checkpoint.write_bytes(checkpoint.read_bytes() + b"tampered")
        with self.assertRaisesRegex(dataset_cli.DatasetError, "(?:byte count|SHA256) mismatch"):
            dataset_cli.build_records(self.pilot)

        checkpoint.write_bytes(original_checkpoint)
        status_path = case / "run_status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["catalog_sha256"] = "f" * 64
        status_path.write_text(json.dumps(status) + "\n", encoding="utf-8")
        self._refresh_artifact(case, "run_status")
        with self.assertRaisesRegex(dataset_cli.DatasetError, "run status catalog_sha256"):
            dataset_cli.build_records(self.pilot)

    def test_requires_both_stable_host_ssh_log_roles(self) -> None:
        case = self.pilot / "cases" / "SETUP_001"
        manifest_path = case / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for role in ("host_ssh_log", "replay_2_host_ssh_log"):
            with self.subTest(role=role):
                removed = manifest["artifacts"].pop(role)
                manifest_path.write_text(
                    json.dumps(manifest) + "\n", encoding="utf-8"
                )
                with self.assertRaisesRegex(dataset_cli.DatasetError, role):
                    dataset_cli.build_records(self.pilot)
                manifest["artifacts"][role] = removed
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    def test_requires_typed_pass_marker_in_host_log_not_in_innovus_log(self) -> None:
        case = self.pilot / "cases" / "SETUP_001"
        host_log = case / "logs/host_ssh.log"
        host_log.write_text("host transport completed\n", encoding="utf-8")
        innovus_log = case / "logs/innovus.log"
        with innovus_log.open("a", encoding="utf-8") as stream:
            stream.write("SFT_CASE_PASSED SETUP_001\n")
        self._refresh_artifact(case, "host_ssh_log")
        self._refresh_artifact(case, "run_log")
        with self.assertRaisesRegex(
            dataset_cli.DatasetError,
            "host SSH log must contain exactly one typed marker line",
        ):
            dataset_cli.build_records(self.pilot)

    def test_rejects_duplicate_typed_marker_in_replay_2_host_log(self) -> None:
        case = self.pilot / "cases" / "SETUP_001"
        host_log = case / "evidence/replay_2/logs/host_ssh.log"
        with host_log.open("a", encoding="utf-8") as stream:
            stream.write("SFT_CASE_PASSED SETUP_001\n")
        self._refresh_artifact(case, "replay_2_host_ssh_log")
        with self.assertRaisesRegex(
            dataset_cli.DatasetError,
            "host SSH log must contain exactly one typed marker line",
        ):
            dataset_cli.build_records(self.pilot)

    def test_rejects_host_log_role_not_bound_to_replay_log_path(self) -> None:
        case = self.pilot / "cases" / "SETUP_001"
        manifest_path = case / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"]["host_ssh_log"] = self._artifact(
            case, "logs/innovus.log"
        )
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(
            dataset_cli.DatasetError,
            "host_ssh_log is not bound to its canonical replay log path",
        ):
            dataset_cli.build_records(self.pilot)

    def test_rejects_primetime_liberty_not_bound_to_qualification(self) -> None:
        case = self.pilot / "cases" / "SETUP_001"
        alternate = case / "pt_fixture_sources/alternate_setup.lib"
        alternate.write_text("library(alternate_setup) {}\n", encoding="utf-8")
        self._restage_pt_inputs(
            case,
            {"setup_lib": alternate},
        )
        with self.assertRaisesRegex(dataset_cli.DatasetError, "not bound to setup_liberty_ss"):
            dataset_cli.build_records(self.pilot)

    def test_requires_stable_adapted_sdc_manifest_roles(self) -> None:
        case = self.pilot / "cases" / "SETUP_001"
        manifest_path = case / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        del manifest["artifacts"]["pt_input_setup_sdc_adapted"]
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(
            dataset_cli.DatasetError, "pt_input_setup_sdc_adapted"
        ):
            dataset_cli.build_records(self.pilot)

    def test_rejects_stable_adapted_role_not_bound_to_pt_summary(self) -> None:
        case = self.pilot / "cases" / "SETUP_001"
        manifest_path = case / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"]["pt_input_setup_sdc_adapted"] = self._artifact(
            case, "reports/constraint_setup_after.sdc"
        )
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(
            dataset_cli.DatasetError,
            "pt_input_setup_sdc_adapted path/hash/bytes",
        ):
            dataset_cli.build_records(self.pilot)

    def test_rejects_verifier_valid_raw_sdc_not_bound_to_replay_constraints(self) -> None:
        case = self.pilot / "cases" / "SETUP_001"
        alternate = case / "pt_fixture_sources/alternate_setup.sdc"
        alternate.write_text(
            "current_design NV_NVDLA_CMAC_CORE_mac\n"
            "create_clock -period 9.9 [get_ports clk]\n",
            encoding="utf-8",
        )
        self._restage_pt_inputs(case, {"setup_sdc": alternate})
        with self.assertRaisesRegex(
            dataset_cli.DatasetError,
            "raw setup SDC is not bound to replay-1 constraints",
        ):
            dataset_cli.build_records(self.pilot)

    def test_rejects_tampered_sdc_adapter_contract_via_pt_verifier(self) -> None:
        case = self.pilot / "cases" / "SETUP_001"
        summary_path = case / "primetime_crosscheck.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["sdc_adaptation"]["operation"] = "copy_without_exact_transform"
        self._rewrite_pt_summary(case, summary)
        with self.assertRaisesRegex(dataset_cli.DatasetError, "adapter operation"):
            dataset_cli.build_records(self.pilot)

    def test_rejects_nondeterministic_adapted_sdc_with_rehashed_evidence(self) -> None:
        case = self.pilot / "cases" / "SETUP_001"
        summary_path = case / "primetime_crosscheck.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        adapted_item = summary["sdc_adaptation"]["setup"]["adapted_artifact"]
        adapted_path = case / adapted_item["path"]
        adapted_path.chmod(0o644)
        adapted_path.write_bytes(adapted_path.read_bytes() + b"# extra mutation\n")
        adapted_item.update(
            {
                "sha256": self._sha(adapted_path),
                "bytes": adapted_path.stat().st_size,
            }
        )
        summary["execution"]["setup"]["pt_sdc_sha256"] = adapted_item["sha256"]
        summary["execution"]["setup"]["input_bindings"]["setup_pt_sdc"] = (
            adapted_item["sha256"]
        )
        self._rewrite_pt_summary(case, summary)
        self._refresh_artifact(case, "pt_input_setup_sdc_adapted")
        with self.assertRaisesRegex(
            dataset_cli.DatasetError, "deterministic bounded-line adaptation"
        ):
            dataset_cli.build_records(self.pilot)

    def test_rejects_execution_not_bound_to_actual_adapted_sdc(self) -> None:
        case = self.pilot / "cases" / "SETUP_001"
        summary_path = case / "primetime_crosscheck.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["execution"]["setup"]["pt_sdc_sha256"] = "0" * 64
        self._rewrite_pt_summary(case, summary)
        with self.assertRaisesRegex(dataset_cli.DatasetError, "adapted SDC binding"):
            dataset_cli.build_records(self.pilot)

    def test_rejects_lc_root_mismatch_across_policy_execution_and_corners(self) -> None:
        case = self.pilot / "cases" / "SETUP_001"
        summary_path = case / "primetime_crosscheck.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["policy"]["synopsys_lc_root"] = "/opt/synopsys/lc/OTHER"
        self._rewrite_pt_summary(case, summary)
        with self.assertRaisesRegex(dataset_cli.DatasetError, "does not match expected"):
            dataset_cli.build_records(self.pilot)

    def test_rejects_metrics_primetime_copy_that_differs_from_artifact(self) -> None:
        case = self.pilot / "cases" / "SETUP_001"
        metrics_path = case / "metrics.json"
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        metrics["crosschecks"]["primetime"]["policy"]["synopsys_lc_root"] = (
            "/opt/synopsys/lc/OTHER"
        )
        metrics_path.write_text(json.dumps(metrics) + "\n", encoding="utf-8")
        self._refresh_artifact(case, "metrics")
        with self.assertRaisesRegex(
            dataset_cli.DatasetError, "differs from its verified artifact"
        ):
            dataset_cli.build_records(self.pilot)


if __name__ == "__main__":
    unittest.main()

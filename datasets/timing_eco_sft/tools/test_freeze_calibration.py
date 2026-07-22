from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any


TOOLS = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "timing_eco_freeze_calibration_under_test", TOOLS / "freeze_calibration.py"
)
assert SPEC is not None and SPEC.loader is not None
freezer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(freezer)


class CalibrationFixture:
    def __init__(
        self,
        root: Path,
        case_id: str,
        *,
        clock_cell_count: int | None = None,
        include_repair_contract: bool = True,
        force_capture_strategy: bool = False,
        omit_probe_delay_reference: bool = False,
    ) -> None:
        self.root = root
        self.case_id = case_id
        self.input_catalog = root / "input_catalog.json"
        shutil.copy2(freezer.DEFAULT_CATALOG, self.input_catalog)
        self.catalog = json.loads(self.input_catalog.read_text(encoding="utf-8"))
        # The checked-in catalog is the final frozen pilot.  Freezer unit tests
        # need a deterministic pre-freeze stage, so normalize the fixture copy
        # (never the source catalog) before binding any calibration evidence.
        for catalog_case in self.catalog["cases"]:
            injection = catalog_case["injection"]
            injection["calibration_status"] = "PROBE_REQUIRED"
            if (
                injection["strategy"]
                in freezer.DATA_DELAY_INJECTION_STRATEGIES
            ):
                injection["setup_parameters"][
                    "delay_cell_reference"
                ] = "PROBE_REQUIRED"
            if (
                injection["strategy"]
                in freezer.CAPTURE_CLOCK_INJECTION_STRATEGIES
            ):
                injection["hold_parameters"][
                    "clock_cell_reference"
                ] = "PROBE_REQUIRED"
            catalog_case["repair"].pop("delay_cell_reference", None)
            catalog_case["repair"].pop("delay_cells_per_endpoint", None)
        self.case = next(case for case in self.catalog["cases"] if case["id"] == case_id)
        if omit_probe_delay_reference:
            self.case["injection"]["setup_parameters"].pop(
                "delay_cell_reference", None
            )
        if force_capture_strategy:
            if self.case["type"] != "hold":
                raise ValueError("force_capture_strategy fixture requires a hold case")
            self.case["injection"]["strategy"] = "local_capture_clock_delay"
            self.case["injection"]["setup_parameters"] = {}
            self.case["injection"]["hold_parameters"] = {
                "clock_cell_count": 1,
                "clock_cell_reference": "PROBE_REQUIRED",
            }
            early_selectors = [
                selector
                for selector in self.case["selectors"]
                if selector["timing"] == "early"
            ]
            if len(early_selectors) != 1:
                raise ValueError("force_capture_strategy fixture needs one early selector")
            early_selectors[0]["count"] = 1
        if clock_cell_count is not None:
            self.case["injection"]["hold_parameters"][
                "clock_cell_count"
            ] = clock_cell_count
        if (
            include_repair_contract
            and self.case["repair"]["strategy"]
            in freezer.run_pilot.HOLD_DELAY_REPAIR_STRATEGIES
        ):
            self.case["repair"].update(
                {
                    "delay_cell_reference": "DLY4_X1M_A9TR40",
                    "delay_cells_per_endpoint": 1,
                }
            )
        self._write(self.input_catalog, self.catalog)
        self.run = root / "calibration-run"
        self.case_root = self.run / "cases" / case_id
        self.replay = self.case_root / "runs" / "replay_1"
        self.reports = self.replay / "reports"
        self.reports.mkdir(parents=True)
        self._write_evidence()

    @staticmethod
    def _write(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _targets(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        ordinal = 0
        for selector in self.case["selectors"]:
            for _ in range(selector["count"]):
                ordinal += 1
                timing = selector["timing"]
                endpoint = f"u_{timing}_{ordinal}/D"
                result.append(
                    {
                        "role": selector["role"],
                        "timing": timing,
                        "endpoint": endpoint,
                        "beginpoint": f"u_launch_{ordinal}/Q",
                        "net": f"net_{timing}_{ordinal}",
                        "driver_pin": f"u_drv_{ordinal}/Y",
                        "driver_inst": f"u_drv_{ordinal}",
                        "driver_ref": "BUF_X1M_A9TR40",
                        "slack": "0.025",
                    }
                )
        return result

    def _measurement(self, check: str) -> tuple[float, float]:
        target = self.case["target_wns_ns"].get(check)
        if target is None:
            return 0.03, 0.0
        wns = (float(target[0]) + float(target[1])) / 2.0
        count = sum(
            int(selector["count"])
            for selector in self.case["selectors"]
            if selector["timing"] == ("late" if check == "setup" else "early")
        )
        return wns, wns * count

    @staticmethod
    def _dict_text(values: dict[str, Any]) -> str:
        return " ".join(f"{key} {value}" for key, value in values.items())

    def _injection(self, resolved: list[dict[str, Any]]) -> tuple[str, list[dict[str, str]], dict[tuple[str, str], str]]:
        injection = self.case["injection"]
        strategy = injection["strategy"]
        setup = {
            "drive_steps": injection["setup_parameters"].get("drive_steps", 0),
            "delay_cells": injection["setup_parameters"].get("delay_cells", 0),
        }
        hold = {
            "drive_steps": injection["hold_parameters"].get("drive_steps", 0),
            "clock_cell_count": injection["hold_parameters"].get("clock_cell_count", 0),
        }
        actions: list[dict[str, str]] = []
        current_refs: dict[tuple[str, str], str] = {}
        down = strategy in {"downsize_endpoint_driver", "mixed_downsize_and_speedup"}
        up = strategy in {"upsize_endpoint_driver", "mixed_downsize_and_speedup"}
        for target in resolved:
            key = (target["timing"], target["endpoint"])
            direction = None
            if target["timing"] == "late" and down:
                direction = "down"
            if target["timing"] == "early" and up:
                direction = "up"
            if direction:
                replacement = (
                    "BUF_X0P7M_A9TR40" if direction == "down" else "BUF_X2M_A9TR40"
                )
                actions.append(
                    {
                        "kind": "resize",
                        "inst": target["driver_inst"],
                        "from": target["driver_ref"],
                        "to": replacement,
                        "direction": direction,
                    }
                )
                current_refs[key] = replacement
            else:
                current_refs[key] = target["driver_ref"]

        delay_count = int(setup["delay_cells"])
        for target in resolved:
            if target["timing"] != "late":
                continue
            for index in range(1, delay_count + 1):
                actions.append(
                    {
                        "kind": "data_delay_injection",
                        "inst": f"SFT_ECO_{self.case_id}_PATH_{target['endpoint'].split('_')[-1].split('/')[0]}_{index}",
                        "cell": "DLY4_X1M_A9TR40",
                        "term": target["endpoint"],
                    }
                )
            if delay_count:
                current_refs[(target["timing"], target["endpoint"])] = "DLY4_X1M_A9TR40"

        clock_count = int(hold["clock_cell_count"])
        if clock_count:
            early_target = next(
                target for target in resolved if target["timing"] == "early"
            )
            capture_instance = early_target["endpoint"].rsplit("/", 1)[0]
            repeater_parent = (
                capture_instance.rsplit("/", 1)[0]
                if "/" in capture_instance
                else ""
            )
            for index in range(1, clock_count + 1):
                leaf = f"SFT_ECO_{self.case_id}_CLOCKPATH_{index}"
                actions.append(
                    {
                        "kind": "capture_clock_injection",
                        "inst": f"{repeater_parent}/{leaf}"
                        if repeater_parent
                        else leaf,
                        "cell": "DLYCLK_X1M_A9TR40",
                        "term": f"{capture_instance}/CK",
                    }
                )
        action_list = " ".join("{" + self._dict_text(item) + "}" for item in actions)
        parameter = (
            f"strategy {strategy} "
            f"setup {{{self._dict_text(setup)}}} "
            f"hold {{{self._dict_text(hold)}}} "
            f"actions {{{action_list}}}"
        )
        return parameter, actions, current_refs

    def _write_evidence(self) -> None:
        self.run.mkdir(exist_ok=True)
        prepared_catalog = self.run / "catalog.json"
        shutil.copy2(self.input_catalog, prepared_catalog)
        catalog_sha = self._sha(prepared_catalog)
        baseline_sha = "a" * 64
        qualification_sha = "b" * 64
        checksum_sha = "c" * 64
        binding = {
            "baseline_sha256": baseline_sha,
            "catalog_sha256": catalog_sha,
            "qualification_sha256": qualification_sha,
            "checksum_manifest_sha256": checksum_sha,
            "qualification_status": "QUALIFIED_CANDIDATE",
            "technology_classification": "derived_non_signoff",
        }
        run_manifest = {
            "schema_version": "timing_eco_pilot_run.v1",
            "run_id": "unit-calibration",
            "baseline": {
                "sha256": baseline_sha,
                "checksum_manifest": {"path": "baseline_checksums.sha256", "sha256": checksum_sha},
                "qualification": {
                    "sha256": qualification_sha,
                    "schema_version": "smic40_baseline_qualification.v1",
                    "status": "QUALIFIED_CANDIDATE",
                    "gold_status": False,
                    "signoff_eligible": False,
                    "technology_classification": "derived_non_signoff",
                },
            },
            "catalog": {"source": str(self.input_catalog), "sha256": catalog_sha},
            "case_ids": [self.case_id],
            "status": "NOT_GOLD_CALIBRATION_PREPARED",
            "gold_eligible": False,
        }
        self._write(self.run / "run_manifest.json", run_manifest)
        case_manifest = {
            "id": self.case_id,
            "type": self.case["type"],
            "difficulty": self.case["difficulty"],
            "design": self.catalog["design"]["top"],
            "tool_version": self.catalog["design"]["expected_tool_version"],
            "repair_mode": self.case["repair_mode"],
            "catalog_entry": self.case,
            "baseline_sha256": baseline_sha,
            "catalog_sha256": catalog_sha,
            "baseline_checksum_manifest_sha256": checksum_sha,
            "baseline_qualification": {
                "schema_version": "smic40_baseline_qualification.v1",
                "status": "QUALIFIED_CANDIDATE",
                "gold_status": False,
                "signoff_eligible": False,
                "technology_classification": "derived_non_signoff",
                "sha256": qualification_sha,
                "source_artifacts": {},
            },
            "status": "NOT_GOLD_CALIBRATION_PREPARED",
            "gold_eligible": False,
        }
        self._write(self.replay / "manifest.json", case_manifest)
        replay_status = {
            "replay": 1,
            "innovus_exit_code": 0,
            "fetched": True,
            "passed": True,
            "expected_marker": "NOT_GOLD_CALIBRATION",
            "marker_present": True,
            "baseline_sha256": baseline_sha,
            "catalog_sha256": catalog_sha,
            "baseline_qualification_sha256": qualification_sha,
            "baseline_checksum_manifest_sha256": checksum_sha,
            "baseline_status": "QUALIFIED_CANDIDATE",
            "guest_baseline_checksums_verified": True,
            "mode": "NOT_GOLD_CALIBRATION",
            "gold_eligible": False,
        }
        self._write(self.replay / "run_status.json", replay_status)
        (self.replay / "NOT_GOLD_CALIBRATION").write_text(
            f"{self.case_id} one-shot calibration completed; this is not a Gold replay\n",
            encoding="utf-8",
        )
        self._write(
            self.run / "run_status.json",
            {
                "run_id": "unit-calibration",
                "cases": {self.case_id: [replay_status]},
                "passed": True,
                "dry_run": False,
                "baseline_sha256": baseline_sha,
                "catalog_sha256": catalog_sha,
                "baseline_qualification_sha256": qualification_sha,
                "baseline_checksum_manifest_sha256": checksum_sha,
                "baseline_status": "QUALIFIED_CANDIDATE",
                "guest_baseline_checksums_verified": True,
                "mode": "NOT_GOLD_CALIBRATION",
                "gold_eligible": False,
            },
        )
        self._write(
            self.reports / "calibration_status.json",
            {
                "schema_version": "timing_eco_calibration_status.v1",
                "case_id": self.case_id,
                "status": "NOT_GOLD_CALIBRATION_COMPLETE",
                "gold_eligible": False,
                "fresh_replays_required_after_freeze": 2,
                "scope_validation": "passed",
                "passed": True,
            },
        )
        self._write(
            self.reports / "baseline_guard.json",
            {
                "schema_version": "timing_eco_baseline_guard.v1",
                "case_id": self.case_id,
                "minimum_wns_ns": self.catalog["design"]["acceptance"][
                    "baseline_wns_min_ns"
                ],
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
            },
        )

        resolved = self._targets()
        parameter, actions, current_refs = self._injection(resolved)
        setup_wns, setup_tns = self._measurement("setup")
        hold_wns, hold_tns = self._measurement("hold")
        timing_values = {
            "setup": (setup_wns, setup_tns, "late"),
            "hold": (hold_wns, hold_tns, "early"),
        }
        locality: dict[str, Any] = {
            "schema_version": "timing_eco_violation_locality.v1",
            "case_id": self.case_id,
            "repair_mode": self.case["repair_mode"],
        }
        diagnostic_targets: list[dict[str, Any]] = []
        for check, (wns, tns, timing) in timing_values.items():
            required = self.case["type"] in {check, "mixed"}
            selected = sorted(item["endpoint"] for item in resolved if item["timing"] == timing)
            violating = selected if required else []
            locality[check] = {
                "required": required,
                "wns_ns": wns,
                "tns_ns": tns,
                "expected_endpoint_count": len(selected),
                "selected_endpoint_count": len(selected),
                "violating_endpoint_count": len(violating),
                "selected_endpoints": selected,
                "selected_endpoint_slacks": [
                    {"endpoint": endpoint, "slack_ns": wns}
                    for endpoint in selected
                ],
                "violating_endpoints": violating,
                "cardinality_passed": True,
                "locality_passed": True,
                "coverage_passed": True,
                "opposite_headroom_passed": True,
                "selected_slack_consistency_passed": True,
            }
            if not required:
                # Runtime has no selected endpoints in the non-required mode.
                locality[check]["expected_endpoint_count"] = 0
                locality[check]["selected_endpoint_count"] = 0
                locality[check]["selected_endpoints"] = []
                locality[check]["selected_endpoint_slacks"] = []
        locality["passed"] = True
        locality["reasons"] = []
        self._write(self.reports / "violation_locality.json", locality)

        action_by_term: dict[str, list[dict[str, str]]] = {}
        for action in actions:
            if action["kind"] == "data_delay_injection":
                action_by_term.setdefault(action["term"], []).append(action)
        for target in resolved:
            check = "setup" if target["timing"] == "late" else "hold"
            if not locality[check]["required"]:
                continue
            injected = action_by_term.get(target["endpoint"], [])
            local_cells = [
                {"inst": action["inst"], "ref": action["cell"]}
                for action in reversed(injected)
            ]
            local_cells.append(
                {
                    "inst": target["driver_inst"],
                    "ref": (
                        target["driver_ref"]
                        if injected
                        else current_refs[(target["timing"], target["endpoint"])]
                    ),
                }
            )
            diagnostic_targets.append(
                {
                    "role": target["role"],
                    "timing": target["timing"],
                    "endpoint": target["endpoint"],
                    "beginpoint": target["beginpoint"],
                    "slack_ns": timing_values[check][0],
                    "net": target["net"],
                    "driver_pin": (
                        f"{local_cells[0]['inst']}/Y" if injected else target["driver_pin"]
                    ),
                    "driver_inst": local_cells[0]["inst"],
                    "driver_ref": local_cells[0]["ref"],
                    "original_driver": {
                        "inst": target["driver_inst"],
                        "ref": target["driver_ref"],
                    },
                    "local_cells": local_cells,
                }
            )
        self._write(
            self.reports / "diagnostic_context.json",
            {
                "schema_version": "timing_eco_diagnostic_context.v1",
                "case_id": self.case_id,
                "design": self.catalog["design"]["top"],
                "max_eco_cells": self.case["repair"]["max_eco_cells"],
                "targets": diagnostic_targets,
            },
        )
        target_words = []
        for target in resolved:
            target_words.append("{" + self._dict_text(target) + "}")
        (self.reports / "resolved_targets.tcl").write_text(
            "# Runtime-resolved design objects; generated by Innovus.\n"
            "set ::SFT_RESOLVED_TARGETS {" + " ".join(target_words) + "}\n",
            encoding="utf-8",
        )
        self._write(
            self.reports / "injection_provenance.json",
            {
                "schema_version": "timing_eco_injection_provenance.v1",
                "case_id": self.case_id,
                "strategy": self.case["injection"]["strategy"],
                "calibration_status": "PROBE_REQUIRED",
                "status": "calibration_candidate",
                "reason": "NOT_GOLD one-shot real-tool measurement",
                "baseline_binding": binding,
                "target_wns_ns": {
                    "setup": self.case["target_wns_ns"].get("setup", [0.0, 0.0]),
                    "hold": self.case["target_wns_ns"].get("hold", [0.0, 0.0]),
                },
                "attempts": [
                    {
                        "attempt": 1,
                        "parameter": parameter,
                        "setup_wns_ns": setup_wns,
                        "setup_tns_ns": setup_tns,
                        "hold_wns_ns": hold_wns,
                        "hold_tns_ns": hold_tns,
                    }
                ],
            },
        )

    def args(self, *, output: Path | None = None, in_place: bool = False) -> argparse.Namespace:
        return argparse.Namespace(
            case=self.case_id,
            calibration_case_dir=self.case_root,
            catalog=self.input_catalog,
            output=output,
            in_place=in_place,
        )


class FreezeCalibrationTests(unittest.TestCase):
    def test_complete_evidence_covers_all_ten_catalog_strategies(self) -> None:
        catalog = json.loads(freezer.DEFAULT_CATALOG.read_text(encoding="utf-8"))
        for case_id in catalog["case_order"]:
            with self.subTest(case_id=case_id), tempfile.TemporaryDirectory() as temporary:
                fixture = CalibrationFixture(Path(temporary), case_id)
                summary = freezer.freeze_case(fixture.args())
                written = json.loads(
                    Path(summary["output_catalog"]).read_text(encoding="utf-8")
                )
                case = next(case for case in written["cases"] if case["id"] == case_id)
                self.assertEqual("FROZEN", case["injection"]["calibration_status"])
                if (
                    case["injection"]["strategy"]
                    in freezer.DATA_DELAY_INJECTION_STRATEGIES
                ):
                    self.assertEqual(
                        "DLY4_X1M_A9TR40",
                        case["injection"]["setup_parameters"][
                            "delay_cell_reference"
                        ],
                    )
                    self.assertEqual(
                        "DLY4_X1M_A9TR40", summary["delay_cell_reference"]
                    )
                else:
                    self.assertNotIn(
                        "delay_cell_reference",
                        case["injection"]["setup_parameters"],
                    )
                    self.assertIsNone(summary["delay_cell_reference"])

    def test_default_writes_new_catalog_and_keeps_input_probe_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(Path(temporary), "SETUP_001")
            summary = freezer.freeze_case(fixture.args())
            output = Path(summary["output_catalog"])
            self.assertNotEqual(output, fixture.input_catalog)
            original = json.loads(fixture.input_catalog.read_text(encoding="utf-8"))
            frozen = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                "PROBE_REQUIRED",
                next(case for case in original["cases"] if case["id"] == "SETUP_001")["injection"]["calibration_status"],
            )
            self.assertEqual(
                "FROZEN",
                next(case for case in frozen["cases"] if case["id"] == "SETUP_001")["injection"]["calibration_status"],
            )
            self.assertFalse(summary["gold_replays_completed"])
            self.assertEqual(2, summary["fresh_gold_replays_required"])
            frozen_case = next(
                case for case in frozen["cases"] if case["id"] == "SETUP_001"
            )
            self.assertEqual(
                "DLY4_X1M_A9TR40",
                frozen_case["injection"]["setup_parameters"][
                    "delay_cell_reference"
                ],
            )

    def test_hold_repair_contract_must_be_declared_before_freezing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(
                Path(temporary),
                "HOLD_001",
                include_repair_contract=False,
            )
            with self.assertRaisesRegex(
                freezer.FreezeError, "case-local hold-repair"
            ):
                freezer.freeze_case(fixture.args())

    def test_old_prepared_catalog_and_probe_placeholder_freeze_without_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(
                Path(temporary),
                "SETUP_001",
                omit_probe_delay_reference=True,
            )
            # The prepared catalog intentionally retains the old, field-absent
            # format.  The current probe catalog may use the explicit
            # placeholder without changing the measured candidate.
            current = json.loads(fixture.input_catalog.read_text(encoding="utf-8"))
            case = next(
                item for item in current["cases"] if item["id"] == "SETUP_001"
            )
            case["injection"]["setup_parameters"][
                "delay_cell_reference"
            ] = "PROBE_REQUIRED"
            fixture._write(fixture.input_catalog, current)
            summary = freezer.freeze_case(fixture.args())
            frozen = json.loads(
                Path(summary["output_catalog"]).read_text(encoding="utf-8")
            )
            case = next(
                item for item in frozen["cases"] if item["id"] == "SETUP_001"
            )
            self.assertEqual(
                "DLY4_X1M_A9TR40",
                case["injection"]["setup_parameters"][
                    "delay_cell_reference"
                ],
            )

    def test_stage_catalog_normalizes_previously_frozen_delay_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(Path(temporary), "SETUP_003")
            current = json.loads(fixture.input_catalog.read_text(encoding="utf-8"))
            prior = next(
                item for item in current["cases"] if item["id"] == "SETUP_001"
            )
            prior["injection"]["calibration_status"] = "FROZEN"
            prior["injection"]["setup_parameters"][
                "delay_cell_reference"
            ] = "DLY2_X4M_A9TR40"
            fixture._write(fixture.input_catalog, current)
            summary = freezer.freeze_case(fixture.args())
            frozen = json.loads(
                Path(summary["output_catalog"]).read_text(encoding="utf-8")
            )
            prior = next(
                item for item in frozen["cases"] if item["id"] == "SETUP_001"
            )
            current_case = next(
                item for item in frozen["cases"] if item["id"] == "SETUP_003"
            )
            self.assertEqual(
                "DLY2_X4M_A9TR40",
                prior["injection"]["setup_parameters"][
                    "delay_cell_reference"
                ],
            )
            self.assertEqual(
                "DLY4_X1M_A9TR40",
                current_case["injection"]["setup_parameters"][
                    "delay_cell_reference"
                ],
            )

    def test_explicit_in_place_is_required_to_replace_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(Path(temporary), "SETUP_001")
            with self.assertRaisesRegex(freezer.FreezeError, "--in-place"):
                freezer.freeze_case(fixture.args(output=fixture.input_catalog))
            freezer.freeze_case(fixture.args(in_place=True))
            written = json.loads(fixture.input_catalog.read_text(encoding="utf-8"))
            case = next(case for case in written["cases"] if case["id"] == "SETUP_001")
            self.assertEqual("FROZEN", case["injection"]["calibration_status"])

    def test_rejects_scope_marker_that_says_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(Path(temporary), "SETUP_001")
            path = fixture.reports / "calibration_status.json"
            status = json.loads(path.read_text(encoding="utf-8"))
            status["scope_validation"] = "failed"
            status["scope_reason"] = "measured locality failed"
            fixture._write(path, status)
            with self.assertRaisesRegex(
                freezer.FreezeError, "passing calibration status|clean passing scope"
            ):
                freezer.freeze_case(fixture.args())

    def test_rejects_tampered_or_failing_baseline_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(Path(temporary), "SETUP_001")
            path = fixture.reports / "baseline_guard.json"
            guard = json.loads(path.read_text(encoding="utf-8"))
            guard["catalog_sha256"] = "e" * 64
            fixture._write(path, guard)
            with self.assertRaisesRegex(freezer.FreezeError, "baseline_guard.catalog"):
                freezer.freeze_case(fixture.args())

        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(Path(temporary), "SETUP_001")
            path = fixture.reports / "baseline_guard.json"
            guard = json.loads(path.read_text(encoding="utf-8"))
            guard["hold"]["wns_ns"] = -0.01
            fixture._write(path, guard)
            with self.assertRaisesRegex(freezer.FreezeError, "baseline guard hold"):
                freezer.freeze_case(fixture.args())

    def test_rejects_target_window_miss_even_with_passing_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(Path(temporary), "SETUP_001")
            provenance_path = fixture.reports / "injection_provenance.json"
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            provenance["attempts"][0]["setup_wns_ns"] = -0.2
            provenance["attempts"][0]["setup_tns_ns"] = -0.2
            fixture._write(provenance_path, provenance)
            locality_path = fixture.reports / "violation_locality.json"
            locality = json.loads(locality_path.read_text(encoding="utf-8"))
            locality["setup"]["wns_ns"] = -0.2
            locality["setup"]["tns_ns"] = -0.2
            locality["setup"]["selected_endpoint_slacks"][0]["slack_ns"] = -0.2
            fixture._write(locality_path, locality)
            context_path = fixture.reports / "diagnostic_context.json"
            context = json.loads(context_path.read_text(encoding="utf-8"))
            context["targets"][0]["slack_ns"] = -0.2
            fixture._write(context_path, context)
            with self.assertRaisesRegex(freezer.FreezeError, "outside"):
                freezer.freeze_case(fixture.args())

    def test_rejects_incomplete_or_inconsistent_selected_endpoint_slacks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(Path(temporary), "SETUP_001")
            locality_path = fixture.reports / "violation_locality.json"
            locality = json.loads(locality_path.read_text(encoding="utf-8"))
            del locality["setup"]["selected_endpoint_slacks"]
            fixture._write(locality_path, locality)
            with self.assertRaisesRegex(freezer.FreezeError, "must contain exactly"):
                freezer.freeze_case(fixture.args())

        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(Path(temporary), "SETUP_001")
            locality_path = fixture.reports / "violation_locality.json"
            locality = json.loads(locality_path.read_text(encoding="utf-8"))
            locality["setup"]["selected_endpoint_slacks"][0]["endpoint"] = "u_other/D"
            fixture._write(locality_path, locality)
            with self.assertRaisesRegex(freezer.FreezeError, "slack coverage"):
                freezer.freeze_case(fixture.args())

        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(Path(temporary), "SETUP_001")
            locality_path = fixture.reports / "violation_locality.json"
            locality = json.loads(locality_path.read_text(encoding="utf-8"))
            locality["setup"]["selected_endpoint_slacks"][0]["slack_ns"] = 0.01
            fixture._write(locality_path, locality)
            with self.assertRaisesRegex(freezer.FreezeError, "violating_endpoints"):
                freezer.freeze_case(fixture.args())

        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(Path(temporary), "SETUP_001")
            locality_path = fixture.reports / "violation_locality.json"
            locality = json.loads(locality_path.read_text(encoding="utf-8"))
            locality["setup"]["selected_endpoint_slacks"][0]["slack_ns"] -= 0.01
            fixture._write(locality_path, locality)
            with self.assertRaisesRegex(freezer.FreezeError, "reproduce WNS"):
                freezer.freeze_case(fixture.args())

        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(Path(temporary), "SETUP_001")
            context_path = fixture.reports / "diagnostic_context.json"
            context = json.loads(context_path.read_text(encoding="utf-8"))
            context["targets"][0]["slack_ns"] -= 0.01
            fixture._write(context_path, context)
            with self.assertRaisesRegex(freezer.FreezeError, "exact endpoint evidence"):
                freezer.freeze_case(fixture.args())

    def test_rejects_opposite_margin_and_catalog_hash_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(Path(temporary), "SETUP_001")
            provenance_path = fixture.reports / "injection_provenance.json"
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            provenance["attempts"][0]["hold_wns_ns"] = 0.001
            fixture._write(provenance_path, provenance)
            locality_path = fixture.reports / "violation_locality.json"
            locality = json.loads(locality_path.read_text(encoding="utf-8"))
            locality["hold"]["wns_ns"] = 0.001
            fixture._write(locality_path, locality)
            with self.assertRaisesRegex(freezer.FreezeError, "margin"):
                freezer.freeze_case(fixture.args())

        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(Path(temporary), "SETUP_001")
            provenance_path = fixture.reports / "injection_provenance.json"
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            provenance["baseline_binding"]["catalog_sha256"] = "d" * 64
            fixture._write(provenance_path, provenance)
            with self.assertRaisesRegex(freezer.FreezeError, "binding"):
                freezer.freeze_case(fixture.args())

    def test_rejects_resolved_target_or_action_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(Path(temporary), "SETUP_001")
            path = fixture.reports / "resolved_targets.tcl"
            path.write_text(path.read_text(encoding="utf-8").replace("u_late_1/D", "u_other/D"), encoding="utf-8")
            with self.assertRaisesRegex(freezer.FreezeError, "different endpoints"):
                freezer.freeze_case(fixture.args())

        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(Path(temporary), "SETUP_001")
            path = fixture.reports / "injection_provenance.json"
            provenance = json.loads(path.read_text(encoding="utf-8"))
            provenance["attempts"][0]["parameter"] = provenance["attempts"][0]["parameter"].replace(
                "DLY4_X1M_A9TR40", "DLY4_X2M_A9TR40"
            )
            fixture._write(path, provenance)
            with self.assertRaisesRegex(freezer.FreezeError, "disagrees|differ"):
                freezer.freeze_case(fixture.args())

    def test_rejects_inconsistent_or_unsafe_data_delay_references(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(Path(temporary), "SETUP_003")
            path = fixture.reports / "injection_provenance.json"
            provenance = json.loads(path.read_text(encoding="utf-8"))
            provenance["attempts"][0]["parameter"] = provenance["attempts"][0][
                "parameter"
            ].replace("DLY4_X1M_A9TR40", "DLY4_X2M_A9TR40", 1)
            fixture._write(path, provenance)
            with self.assertRaisesRegex(freezer.FreezeError, "one consistent"):
                freezer.freeze_case(fixture.args())

        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(Path(temporary), "SETUP_001")
            path = fixture.reports / "injection_provenance.json"
            provenance = json.loads(path.read_text(encoding="utf-8"))
            provenance["attempts"][0]["parameter"] = provenance["attempts"][0][
                "parameter"
            ].replace("DLY4_X1M_A9TR40", "DLY4-X1M_A9TR40")
            fixture._write(path, provenance)
            with self.assertRaisesRegex(freezer.FreezeError, "unsafe"):
                freezer.freeze_case(fixture.args())

    def test_capture_clock_cases_freeze_reference_only_from_measured_action(self) -> None:
        for case_id in ("HOLD_003", "MIXED_002"):
            with self.subTest(case_id=case_id), tempfile.TemporaryDirectory() as temporary:
                fixture = CalibrationFixture(Path(temporary), case_id)
                summary = freezer.freeze_case(fixture.args())
                frozen = json.loads(Path(summary["output_catalog"]).read_text(encoding="utf-8"))
                case = next(case for case in frozen["cases"] if case["id"] == case_id)
                self.assertEqual(
                    "DLYCLK_X1M_A9TR40",
                    case["injection"]["hold_parameters"]["clock_cell_reference"],
                )
                self.assertEqual("DLYCLK_X1M_A9TR40", summary["clock_cell_reference"])

    def test_arbitrary_case_capture_strategy_drives_normalization_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(
                Path(temporary), "HOLD_002", force_capture_strategy=True
            )
            # The current stage may already carry the exact measured reference
            # while the prepared calibration catalog still has the placeholder.
            current = json.loads(fixture.input_catalog.read_text(encoding="utf-8"))
            case = next(item for item in current["cases"] if item["id"] == "HOLD_002")
            case["injection"]["hold_parameters"][
                "clock_cell_reference"
            ] = "DLYCLK_X1M_A9TR40"
            fixture._write(fixture.input_catalog, current)

            summary = freezer.freeze_case(fixture.args())
            frozen = json.loads(
                Path(summary["output_catalog"]).read_text(encoding="utf-8")
            )
            case = next(item for item in frozen["cases"] if item["id"] == "HOLD_002")
            self.assertEqual("local_capture_clock_delay", case["injection"]["strategy"])
            self.assertEqual(
                "DLYCLK_X1M_A9TR40",
                case["injection"]["hold_parameters"]["clock_cell_reference"],
            )
            self.assertEqual("DLYCLK_X1M_A9TR40", summary["clock_cell_reference"])

    def test_capture_strategy_without_clock_evidence_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(
                Path(temporary), "HOLD_002", force_capture_strategy=True
            )
            path = fixture.reports / "injection_provenance.json"
            provenance = json.loads(path.read_text(encoding="utf-8"))
            provenance["attempts"][0]["parameter"] = provenance["attempts"][0][
                "parameter"
            ].replace("kind capture_clock_injection", "kind resize", 1)
            fixture._write(path, provenance)
            with self.assertRaisesRegex(
                freezer.FreezeError, "no measured capture-clock evidence"
            ):
                freezer.freeze_case(fixture.args())

    def test_multilevel_capture_clock_chain_freezes_one_measured_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(
                Path(temporary), "HOLD_003", clock_cell_count=3
            )
            summary = freezer.freeze_case(fixture.args())
            frozen = json.loads(
                Path(summary["output_catalog"]).read_text(encoding="utf-8")
            )
            case = next(
                item for item in frozen["cases"] if item["id"] == "HOLD_003"
            )
            self.assertEqual(
                3,
                case["injection"]["hold_parameters"]["clock_cell_count"],
            )
            self.assertEqual(
                "DLYCLK_X1M_A9TR40",
                case["injection"]["hold_parameters"]["clock_cell_reference"],
            )

    def test_rejects_inconsistent_capture_clock_chain_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(
                Path(temporary), "HOLD_003", clock_cell_count=3
            )
            path = fixture.reports / "injection_provenance.json"
            provenance = json.loads(path.read_text(encoding="utf-8"))
            provenance["attempts"][0]["parameter"] = provenance["attempts"][0][
                "parameter"
            ].replace("DLYCLK_X1M_A9TR40", "DLYCLK_X2M_A9TR40", 1)
            fixture._write(path, provenance)
            with self.assertRaisesRegex(freezer.FreezeError, "one consistent"):
                freezer.freeze_case(fixture.args())

        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(
                Path(temporary), "HOLD_003", clock_cell_count=3
            )
            path = fixture.reports / "injection_provenance.json"
            provenance = json.loads(path.read_text(encoding="utf-8"))
            provenance["attempts"][0]["parameter"] = provenance["attempts"][0][
                "parameter"
            ].replace("u_early_1/CK", "u_other/CK", 1)
            fixture._write(path, provenance)
            with self.assertRaisesRegex(freezer.FreezeError, "same clock pin"):
                freezer.freeze_case(fixture.args())

        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(
                Path(temporary), "HOLD_003", clock_cell_count=3
            )
            path = fixture.reports / "injection_provenance.json"
            provenance = json.loads(path.read_text(encoding="utf-8"))
            provenance["attempts"][0]["parameter"] = provenance["attempts"][0][
                "parameter"
            ].replace("DLYCLK_X1M_A9TR40", "DLYCLK-X1M-A9TR40")
            fixture._write(path, provenance)
            with self.assertRaisesRegex(freezer.FreezeError, "exact and safe"):
                freezer.freeze_case(fixture.args())

    def test_rejects_unrelated_catalog_mutation_and_existing_default_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(Path(temporary), "SETUP_001")
            changed = copy.deepcopy(fixture.catalog)
            changed["design"]["acceptance"]["baseline_wns_min_ns"] = 999.0
            fixture._write(fixture.input_catalog, changed)
            with self.assertRaisesRegex(freezer.FreezeError, "prepared calibration catalog"):
                freezer.freeze_case(fixture.args())

        with tempfile.TemporaryDirectory() as temporary:
            fixture = CalibrationFixture(Path(temporary), "SETUP_001")
            freezer.freeze_case(fixture.args())
            with self.assertRaisesRegex(freezer.FreezeError, "overwrite"):
                freezer.freeze_case(fixture.args())


if __name__ == "__main__":
    unittest.main()

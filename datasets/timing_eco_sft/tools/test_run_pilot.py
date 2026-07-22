"""Dependency-free tests for the pilot catalog and host-side renderer."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("run_pilot.py")
SPEC = importlib.util.spec_from_file_location("timing_eco_run_pilot", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
run_pilot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(run_pilot)


def _probe_catalog_raw(case_ids: set[str] | None = None) -> dict[str, object]:
    """Return an explicit pre-freeze copy of the checked-in final catalog."""

    raw = json.loads(run_pilot.CATALOG_PATH.read_text(encoding="utf-8"))
    selected = set(raw["case_order"]) if case_ids is None else set(case_ids)
    for case in raw["cases"]:
        if case["id"] not in selected:
            continue
        injection = case["injection"]
        injection["calibration_status"] = "PROBE_REQUIRED"
        if injection["strategy"] in run_pilot.DATA_DELAY_INJECTION_STRATEGIES:
            injection["setup_parameters"][
                "delay_cell_reference"
            ] = "PROBE_REQUIRED"
        if "clock_cell_count" in injection["hold_parameters"]:
            injection["hold_parameters"][
                "clock_cell_reference"
            ] = "PROBE_REQUIRED"
    return raw


class CatalogTests(unittest.TestCase):
    @staticmethod
    def _load_modified_catalog(raw: dict[str, object]) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "catalog.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            return run_pilot.load_catalog(path)

    def test_exact_case_and_repair_mix(self) -> None:
        catalog = run_pilot.load_catalog()
        self.assertEqual(catalog["case_order"], list(run_pilot.EXPECTED_CASE_IDS))
        self.assertEqual(
            {mode: sum(case["repair_mode"] == mode for case in catalog["cases"])
             for mode in ("surgical", "native")},
            {"surgical": 10, "native": 0},
        )

    def test_rendered_tcl_uses_runtime_selectors(self) -> None:
        catalog = run_pilot.load_catalog()
        rendered = run_pilot._render_case_config(catalog, catalog["cases"][0])
        self.assertIn("stable_rank 0", rendered)
        self.assertIn("include_globs", rendered)
        self.assertIn("calibration_status {FROZEN}", rendered)
        self.assertIn(
            "set ::SFT_FROZEN_DELAY_CELL {DLY2_X4M_A9TR40}", rendered
        )
        self.assertNotIn("pp_out_l0n06_1_d1_reg_14_", rendered)

    def test_selector_seeds_match_runtime_1000_path_probe(self) -> None:
        catalog = run_pilot.load_catalog()
        actual = {
            case["id"]: [
                (
                    selector["timing"],
                    selector["hierarchy_class"],
                    selector["stable_rank"],
                    selector["count"],
                )
                for selector in case["selectors"]
            ]
            for case in catalog["cases"]
        }
        self.assertEqual(
            actual,
            {
                "SETUP_001": [("late", "exp", 0, 1)],
                "SETUP_002": [("late", "exp", 15, 2)],
                "SETUP_003": [("late", "top_tree", 2, 2)],
                "SETUP_004": [("late", "top_tree", 10, 4)],
                "HOLD_001": [("early", "top_tree", 51, 1)],
                "HOLD_002": [("early", "top_tree", 487, 1)],
                "HOLD_003": [("early", "top_tree", 142, 1)],
                "HOLD_004": [("early", "top_tree", 233, 1)],
                "MIXED_001": [
                    ("late", "exp", 17, 2),
                    ("early", "top_tree", 319, 1),
                ],
                "MIXED_002": [
                    ("late", "top_tree", 4, 3),
                    ("early", "top_tree", 400, 1),
                ],
            },
        )
        self.assertFalse(
            any(
                selector["hierarchy_class"] == "multiplier"
                for case in catalog["cases"]
                for selector in case["selectors"]
            )
        )

    def test_selector_rank_intervals_must_not_overlap_within_timing_and_hierarchy(self) -> None:
        raw = json.loads(run_pilot.CATALOG_PATH.read_text(encoding="utf-8"))
        mixed = next(case for case in raw["cases"] if case["id"] == "MIXED_002")
        late = next(selector for selector in mixed["selectors"] if selector["timing"] == "late")
        late["stable_rank"] = 11
        with self.assertRaisesRegex(
            run_pilot.PilotError,
            r"overlapping deterministic selector intervals.*\[11, 14\).*\[10, 14\)",
        ):
            self._load_modified_catalog(raw)

    def test_adjacent_selector_rank_intervals_are_allowed(self) -> None:
        raw = json.loads(run_pilot.CATALOG_PATH.read_text(encoding="utf-8"))
        mixed = next(case for case in raw["cases"] if case["id"] == "MIXED_002")
        late = next(selector for selector in mixed["selectors"] if selector["timing"] == "late")
        late["stable_rank"] = 14
        loaded = self._load_modified_catalog(raw)
        loaded_mixed = next(
            case for case in loaded["cases"] if case["id"] == "MIXED_002"
        )
        self.assertEqual(14, loaded_mixed["selectors"][0]["stable_rank"])

    def test_top_tree_excludes_root_and_nested_arithmetic_trees(self) -> None:
        catalog = run_pilot.load_catalog()
        exclusions = set(
            catalog["design"]["hierarchy_classes"]["top_tree"]["exclude_globs"]
        )
        self.assertTrue(
            {"*/u_exp/*", "u_exp/*", "*/u_mul_*/*", "u_mul_*/*"}.issubset(
                exclusions
            )
        )

    def test_hold_004_uses_budgeted_surgical_data_delay_repair(self) -> None:
        catalog = run_pilot.load_catalog()
        case = next(case for case in catalog["cases"] if case["id"] == "HOLD_004")
        self.assertEqual(case["repair_mode"], "surgical")
        self.assertEqual(case["selectors"][0]["count"], 1)
        self.assertEqual(case["injection"]["strategy"], "local_capture_clock_delay")
        self.assertNotIn("uncertainty", case["injection"]["strategy"])
        self.assertEqual(
            case["repair"],
            {
                "strategy": "insert_data_delay",
                "max_eco_cells": 2,
                "delay_cell_reference": "DLY4_X0P5M_A9TR40",
                "delay_cells_per_endpoint": 2,
            },
        )

    def test_setup_001_uses_measured_delay_injection_fallback(self) -> None:
        catalog = run_pilot.load_catalog()
        case = next(case for case in catalog["cases"] if case["id"] == "SETUP_001")
        self.assertEqual(case["injection"]["strategy"], "insert_data_delay")
        self.assertEqual(
            case["injection"]["setup_parameters"],
            {
                "delay_cells": 1,
                "delay_cell_reference": "DLY2_X4M_A9TR40",
            },
        )
        self.assertEqual(case["repair"]["strategy"], "replace_delay_and_upsize")
        self.assertEqual(case["repair"]["max_eco_cells"], 2)

    def test_setup_004_uses_budgeted_surgical_repair(self) -> None:
        catalog = run_pilot.load_catalog()
        case = next(case for case in catalog["cases"] if case["id"] == "SETUP_004")
        self.assertEqual(case["repair_mode"], "surgical")
        self.assertEqual(case["selectors"][0]["count"], 4)
        self.assertEqual(case["injection"]["setup_parameters"]["delay_cells"], 2)
        self.assertEqual(case["repair"]["strategy"], "replace_delay_and_upsize")
        self.assertEqual(case["repair"]["max_eco_cells"], 12)

    def test_runtime_has_fail_closed_gold_evidence_contract(self) -> None:
        runtime = run_pilot.RUNTIME_TEMPLATE.read_text(encoding="utf-8")
        for marker in (
            "SFT_INJECTION_TARGET_VALIDATED",
            "injection_provenance.json",
            "concrete_fix.tcl",
            "constraint_${check}_${stage}.sdc",
            "diagnostic_context.json",
            "violation_locality.json",
            "physical_no_regression.json",
            "functional_audit.json",
            "cell_budget_respected",
            "native_cell_diff.tcl",
            "native_selected_terms.txt",
            "violating.enc",
            "before.v",
            "setup_before.spef",
            "hold_before.spef",
            "fixed.v",
            "setup_after.spef",
            "hold_after.spef",
        ):
            self.assertIn(marker, runtime)
        self.assertIn("NOT_GOLD_CALIBRATION", runtime)
        self.assertNotIn("max_attempts", runtime)
        self.assertNotIn('"max_transition_violations": 0', runtime)

    def test_runtime_drive_selection_is_exact_and_flavor_aware(self) -> None:
        runtime = run_pilot.RUNTIME_TEMPLATE.read_text(encoding="utf-8")
        for marker in (
            "flavor tail",
            "compare_drive_variants",
            "ambiguous drive level",
            "cannot resize $reference exactly",
            "validate_resolved_action_feasibility",
            "validate_resize_transition",
            "validate_equivalent_cells",
            "SFT_RESOLVED_ACTIONS_FEASIBLE",
        ):
            self.assertIn(marker, runtime)
        self.assertNotIn("if {$new_index < 0} { set new_index 0 }", runtime)
        self.assertNotIn("set candidate_endpoints {}", runtime)

    def test_injections_are_frozen_one_shot_directionally_separated_candidates(self) -> None:
        catalog = run_pilot.load_catalog()
        for case in catalog["cases"]:
            injection = case["injection"]
            self.assertEqual(injection["calibration_status"], "FROZEN")
            for legacy in ("max_attempts", "drive_steps", "delay_cells"):
                self.assertNotIn(legacy, injection)
            if case["type"] == "setup":
                self.assertTrue(injection["setup_parameters"])
                self.assertFalse(injection["hold_parameters"])
                self.assertGreaterEqual(injection["opposite_wns_min_ns"], 0.0)
            elif case["type"] == "hold":
                self.assertFalse(injection["setup_parameters"])
                self.assertTrue(injection["hold_parameters"])
                self.assertGreaterEqual(injection["opposite_wns_min_ns"], 0.0)
            else:
                self.assertTrue(injection["setup_parameters"])
                self.assertTrue(injection["hold_parameters"])
                self.assertNotEqual(
                    id(injection["setup_parameters"]), id(injection["hold_parameters"])
                )

    def test_capture_skew_cases_are_exactly_one_sink_and_clock_is_frozen(self) -> None:
        catalog = run_pilot.load_catalog()
        by_id = {case["id"]: case for case in catalog["cases"]}
        for case_id in ("HOLD_003", "MIXED_002"):
            case = by_id[case_id]
            hold_selectors = [
                selector for selector in case["selectors"] if selector["timing"] == "early"
            ]
            self.assertEqual(sum(item["count"] for item in hold_selectors), 1)
            parameters = case["injection"]["hold_parameters"]
            self.assertEqual(parameters["clock_cell_count"], 2)
            self.assertEqual(
                parameters["clock_cell_reference"], "DLYCLK8S8_X1B_A9TR40"
            )
        self.assertEqual(by_id["HOLD_003"]["repair"]["max_eco_cells"], 2)

    def test_capture_skew_supports_a_positive_multicell_chain_on_one_endpoint(self) -> None:
        raw = json.loads(run_pilot.CATALOG_PATH.read_text(encoding="utf-8"))
        case = next(item for item in raw["cases"] if item["id"] == "HOLD_003")
        case["injection"]["hold_parameters"].update(
            {
                "clock_cell_count": 3,
                "clock_cell_reference": "DLYCLK_X1M_A9TR40",
            }
        )
        case["injection"]["calibration_status"] = "FROZEN"
        case["repair"].update(
            {
                "delay_cell_reference": "DLY4_X1M_A9TR40",
                "delay_cells_per_endpoint": 1,
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "catalog.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            catalog = run_pilot.load_catalog(path)
        rendered = run_pilot._render_case_config(
            catalog,
            next(item for item in catalog["cases"] if item["id"] == "HOLD_003"),
        )
        self.assertIn("hold_clock_cell_count {3}", rendered)
        self.assertIn(
            "set ::SFT_FROZEN_CLOCK_CELL {DLYCLK_X1M_A9TR40}", rendered
        )

    def test_hold_repair_delay_contract_is_case_local_and_budgeted(self) -> None:
        base = json.loads(run_pilot.CATALOG_PATH.read_text(encoding="utf-8"))

        def validate(raw: dict[str, object]) -> dict[str, object]:
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "catalog.json"
                path.write_text(json.dumps(raw), encoding="utf-8")
                return run_pilot.load_catalog(path)

        # Legacy PROBE_REQUIRED entries may omit both fields.
        legacy_probe = _probe_catalog_raw({"HOLD_001"})
        case = next(
            item for item in legacy_probe["cases"] if item["id"] == "HOLD_001"
        )
        case["repair"].pop("delay_cell_reference")
        case["repair"].pop("delay_cells_per_endpoint")
        validate(legacy_probe)

        explicit_probe = json.loads(json.dumps(base))
        case = next(
            item for item in explicit_probe["cases"] if item["id"] == "HOLD_001"
        )
        case["injection"]["calibration_status"] = "PROBE_REQUIRED"
        case["injection"]["hold_parameters"][
            "clock_cell_reference"
        ] = "PROBE_REQUIRED"
        case["repair"].update(
            {
                "delay_cell_reference": "DLY4_X1M_A9TR40",
                "delay_cells_per_endpoint": 2,
            }
        )
        case["repair"]["max_eco_cells"] = 2
        validated = validate(explicit_probe)
        rendered = run_pilot._render_case_config(
            validated,
            next(item for item in validated["cases"] if item["id"] == "HOLD_001"),
        )
        self.assertIn("repair_delay_cells_per_endpoint {2}", rendered)
        self.assertIn(
            "set ::SFT_REPAIR_DELAY_CELL {DLY4_X1M_A9TR40}", rendered
        )

        frozen_missing = json.loads(json.dumps(base))
        case = next(
            item for item in frozen_missing["cases"] if item["id"] == "HOLD_001"
        )
        case["repair"].pop("delay_cell_reference")
        case["repair"].pop("delay_cells_per_endpoint")
        with self.assertRaisesRegex(run_pilot.PilotError, "FROZEN hold-delay"):
            validate(frozen_missing)

        incomplete = json.loads(json.dumps(base))
        case = next(item for item in incomplete["cases"] if item["id"] == "HOLD_001")
        case["repair"].pop("delay_cells_per_endpoint")
        with self.assertRaisesRegex(run_pilot.PilotError, "declared together"):
            validate(incomplete)

        placeholder = json.loads(json.dumps(base))
        case = next(item for item in placeholder["cases"] if item["id"] == "HOLD_001")
        case["repair"].update(
            {
                "delay_cell_reference": "PROBE_REQUIRED",
                "delay_cells_per_endpoint": 1,
            }
        )
        with self.assertRaisesRegex(run_pilot.PilotError, "exact safe"):
            validate(placeholder)

        unrelated = json.loads(json.dumps(base))
        case = next(item for item in unrelated["cases"] if item["id"] == "SETUP_001")
        case["repair"].update(
            {
                "delay_cell_reference": "DLY4_X1M_A9TR40",
                "delay_cells_per_endpoint": 1,
            }
        )
        with self.assertRaisesRegex(run_pilot.PilotError, "unsupported"):
            validate(unrelated)

        under_budget = json.loads(json.dumps(base))
        case = next(item for item in under_budget["cases"] if item["id"] == "HOLD_002")
        case["repair"].update(
            {
                "delay_cell_reference": "DLY4_X1M_A9TR40",
                "delay_cells_per_endpoint": 2,
            }
        )
        case["repair"]["max_eco_cells"] = 2
        with self.assertRaisesRegex(run_pilot.PilotError, "lower bound 3"):
            validate(under_budget)

    def test_mixed_render_uses_independent_setup_and_hold_parameters(self) -> None:
        catalog = run_pilot.load_catalog()
        mixed = next(case for case in catalog["cases"] if case["id"] == "MIXED_001")
        rendered = run_pilot._render_case_config(catalog, mixed)
        self.assertIn("setup_drive_steps {0}", rendered)
        self.assertIn("setup_delay_cells {2}", rendered)
        self.assertIn("hold_drive_steps {0}", rendered)
        self.assertIn("hold_clock_cell_count {2}", rendered)
        self.assertIn("set ::SFT_FROZEN_DELAY_CELL {DLY2_X4M_A9TR40}", rendered)
        self.assertIn("set ::SFT_FROZEN_CLOCK_CELL {DLYCLK8S6_X1B_A9TR40}", rendered)
        self.assertNotIn("\n    drive_steps ", rendered)
        self.assertNotIn("max_attempts", rendered)

    def test_catalog_rejects_legacy_shared_attempt_parameter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "catalog.json"
            raw = json.loads(run_pilot.CATALOG_PATH.read_text(encoding="utf-8"))
            raw["cases"][0]["injection"]["max_attempts"] = 3
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(run_pilot.PilotError, "legacy shared/search"):
                run_pilot.load_catalog(path)

    def test_catalog_rejects_frozen_placeholder_clock_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "catalog.json"
            raw = json.loads(run_pilot.CATALOG_PATH.read_text(encoding="utf-8"))
            case = next(item for item in raw["cases"] if item["id"] == "HOLD_003")
            case["injection"]["hold_parameters"][
                "clock_cell_reference"
            ] = "PROBE_REQUIRED"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(run_pilot.PilotError, "cannot be FROZEN"):
                run_pilot.load_catalog(path)

    def test_data_delay_reference_contract_is_case_local_and_backward_compatible(self) -> None:
        base = json.loads(run_pilot.CATALOG_PATH.read_text(encoding="utf-8"))

        def validate(raw: dict[str, object]) -> dict[str, object]:
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "catalog.json"
                path.write_text(json.dumps(raw), encoding="utf-8")
                return run_pilot.load_catalog(path)

        probe_placeholder = json.loads(json.dumps(base))
        probe_case = next(
            item for item in probe_placeholder["cases"] if item["id"] == "SETUP_001"
        )
        probe_case["injection"]["calibration_status"] = "PROBE_REQUIRED"
        probe_case["injection"]["setup_parameters"][
            "delay_cell_reference"
        ] = "PROBE_REQUIRED"
        validate(probe_placeholder)

        frozen = json.loads(json.dumps(base))
        frozen_case = next(item for item in frozen["cases"] if item["id"] == "SETUP_001")
        frozen_case["injection"]["calibration_status"] = "FROZEN"
        frozen_case["injection"]["setup_parameters"][
            "delay_cell_reference"
        ] = "DLY2_X4M_A9TR40"
        validated = validate(frozen)
        rendered = run_pilot._render_case_config(
            validated,
            next(item for item in validated["cases"] if item["id"] == "SETUP_001"),
        )
        self.assertIn(
            "set ::SFT_FROZEN_DELAY_CELL {DLY2_X4M_A9TR40}", rendered
        )

        for reference, message in (
            (None, "exact data-delay cell reference"),
            ("PROBE_REQUIRED", "exact data-delay cell reference"),
            ("DLY2/X4M_A9TR40", "unsafe characters"),
        ):
            with self.subTest(reference=reference):
                invalid = json.loads(json.dumps(base))
                case = next(
                    item for item in invalid["cases"] if item["id"] == "SETUP_001"
                )
                case["injection"]["calibration_status"] = "FROZEN"
                if reference is None:
                    case["injection"]["setup_parameters"].pop(
                        "delay_cell_reference"
                    )
                else:
                    case["injection"]["setup_parameters"][
                        "delay_cell_reference"
                    ] = reference
                with self.assertRaisesRegex(run_pilot.PilotError, message):
                    validate(invalid)

        probe_with_actual = json.loads(json.dumps(base))
        case = next(
            item for item in probe_with_actual["cases"] if item["id"] == "SETUP_001"
        )
        case["injection"]["calibration_status"] = "PROBE_REQUIRED"
        case["injection"]["setup_parameters"][
            "delay_cell_reference"
        ] = "DLY2_X4M_A9TR40"
        with self.assertRaisesRegex(run_pilot.PilotError, "must be omitted"):
            validate(probe_with_actual)

        non_delay = json.loads(json.dumps(base))
        case = next(item for item in non_delay["cases"] if item["id"] == "HOLD_001")
        case["injection"]["setup_parameters"][
            "delay_cell_reference"
        ] = "PROBE_REQUIRED"
        with self.assertRaisesRegex(run_pilot.PilotError, "must not declare"):
            validate(non_delay)

    def test_runtime_calibration_miss_preserves_structured_measurement(self) -> None:
        runtime = run_pilot.RUNTIME_TEMPLATE.read_text(encoding="utf-8")
        provenance = runtime.index("proc ::sft::write_injection_provenance")
        locality = runtime.index("proc ::sft::write_violation_locality")
        self.assertGreater(provenance, locality)
        for marker in (
            '\\"wns_ns\\"',
            '\\"tns_ns\\"',
            '\\"selected_endpoints\\"',
            '\\"selected_endpoint_slacks\\"',
            '\\"violating_endpoints\\"',
            '\\"locality_passed\\"',
            '\\"coverage_passed\\"',
            '\\"selected_slack_consistency_passed\\"',
            '\\"reasons\\"',
            "NOT_GOLD calibration scope/target observation failed",
        ):
            self.assertIn(marker, runtime)
        for marker in (
            "proc ::sft::exact_endpoint_slack",
            "-to $endpoint_pin",
            "lappend command -view $view",
            "exact endpoint report_timing returned",
        ):
            self.assertIn(marker, runtime)
        write_index = runtime.index("puts $stream {  \"schema_version\": \"timing_eco_violation_locality.v1\"")
        fail_index = runtime.index("::sft::fail [join $errors", write_index)
        self.assertLess(write_index, fail_index)

    def test_diagnostic_context_covers_existing_fix_objects_without_name_leak(self) -> None:
        runtime = run_pilot.RUNTIME_TEMPLATE.read_text(encoding="utf-8")
        for marker in (
            "diagnostic_local_cells",
            '\\"original_driver\\"',
            '\\"local_cells\\"',
            "post-injection data-driver chain exceeds $limit",
            "assert_diagnostic_instance_visible",
            "absent from diagnostic_context local_cells",
        ):
            self.assertIn(marker, runtime)
        self.assertIn("$insertion_cell PATH data_delay_injection", runtime)
        self.assertIn("$cell CLOCKPATH capture_clock_injection", runtime)
        self.assertNotIn("$delay_cell INJECT data_delay_injection", runtime)
        self.assertNotIn("$cell CLKINJECT capture_clock_injection", runtime)
        diagnostic_proc = runtime[
            runtime.index("proc ::sft::write_diagnostic_context") :
            runtime.index("proc ::sft::record_injection_trial")
        ]
        self.assertNotRegex(
            diagnostic_proc,
            r'(?i)\\"(?:injection|action|parameter|oracle|strategy)\\"',
        )


class TclRuntimeTests(unittest.TestCase):
    def test_pure_tcl_drive_and_feasibility_helpers(self) -> None:
        tclsh = os.environ.get("TCLSH") or shutil.which("tclsh")
        if not tclsh:
            self.skipTest("tclsh is unavailable")
        harness = Path(__file__).with_name("test_pilot_runtime.tcl")
        result = subprocess.run(
            [tclsh, str(harness), str(run_pilot.RUNTIME_TEMPLATE)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn("SFT_RUNTIME_UNIT_TESTS_PASSED", result.stdout)


class PrepareTests(unittest.TestCase):
    @staticmethod
    def _probe_catalog(
        root: Path,
        case_ids: set[str] | None = None,
        *,
        name: str = "probe_catalog.json",
    ) -> tuple[Path, dict[str, object]]:
        path = root / name
        path.write_text(
            json.dumps(_probe_catalog_raw(case_ids), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path, run_pilot.load_catalog(path)

    def _baseline(self, root: Path) -> Path:
        baseline = root / "baseline"
        baseline.mkdir()
        (baseline / "base.enc").write_text("# checkpoint loader\n", encoding="utf-8")
        (baseline / "base.enc.dat").mkdir()
        (baseline / "base.enc.dat" / "db.bin").write_bytes(b"fixture")
        (baseline / "manifest.tcl").write_text(
            "set ::SFT_BASE_TOP {NV_NVDLA_CMAC_CORE_mac}\n"
            "set ::SFT_SETUP_VIEW {test_setup}\n"
            "set ::SFT_HOLD_VIEW {test_hold}\n"
            "set ::SFT_BASE_CHECKPOINT {base.enc}\n",
            encoding="utf-8",
        )
        (baseline / "restore.tcl").write_text("# fixture restore\n", encoding="utf-8")
        (baseline / "setup.tcl").write_text("# fixture setup\n", encoding="utf-8")
        (baseline / "cds.lib").write_text("DEFINE work ./work\n", encoding="utf-8")
        (baseline / "qualification.json").write_text(
            json.dumps(
                {
                    "schema_version": "smic40_baseline_qualification.v1",
                    "status": "QUALIFIED_CANDIDATE",
                    "gold_status": False,
                    "signoff_eligible": False,
                    "technology_classification": "derived_non_signoff",
                    "top": "NV_NVDLA_CMAC_CORE_mac",
                    "analysis_views": {"setup": "test_setup", "hold": "test_hold"},
                    "timing": {
                        "setup": {"wns_ns": 0.03, "tns_ns": 0.0},
                        "hold": {"wns_ns": 0.03, "tns_ns": 0.0},
                    },
                    "drc": {"report_limit": 1000000},
                    "source_artifacts": {
                        "setup_liberty_ss": {
                            "sha256": "1" * 64,
                            "bytes": 100,
                            "source_path": "/qualified/setup.lib",
                        },
                        "hold_liberty_ff": {
                            "sha256": "2" * 64,
                            "bytes": 101,
                            "source_path": "/qualified/hold.lib",
                        },
                    },
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (baseline / "QUALIFIED_CANDIDATE").write_text(
            "status=QUALIFIED_CANDIDATE\n"
            "gold_status=false\n"
            "technology_classification=derived_non_signoff\n",
            encoding="utf-8",
        )
        return baseline

    @staticmethod
    def _run_args(run_dir: Path, *, replays: int = 1) -> argparse.Namespace:
        return argparse.Namespace(
            run_dir=run_dir,
            guest_root=None,
            ssh_bin="ssh",
            ssh_target="qingteng-fc",
            rsync_bin="rsync",
            dry_run=True,
            replays=replays,
            timeout_seconds=60,
            innovus_bin="innovus",
            fail_fast=False,
            allow_unfrozen_calibration=True,
            cleanup_passed_guest_replays=False,
        )

    def _prepare_single_probe_run(self, root: Path, run_id: str) -> Path:
        catalog_path, catalog = self._probe_catalog(root, {"SETUP_001"})
        return run_pilot.prepare_run(
            argparse.Namespace(
                baseline_bundle=self._baseline(root),
                work_root=root / "work",
                run_id=run_id,
                case=["SETUP_001"],
                guest_root="/home/host/nvdla_timing_eco_sft",
                catalog=catalog_path,
                allow_unfrozen_calibration=True,
            ),
            catalog,
        )

    @staticmethod
    def _write_calibration_marker(destination: Path, text: str | None = None) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "NOT_GOLD_CALIBRATION").write_text(
            text
            if text is not None
            else (
                "SETUP_001 one-shot calibration completed; "
                "this is not a Gold replay\n"
            ),
            encoding="utf-8",
        )

    def _run_with_mocked_remote(
        self,
        root: Path,
        *,
        run_id: str,
        cleanup_requested: bool,
        innovus_exit_code: int = 0,
        fetch_succeeded: bool = True,
        marker_text: str | None = (
            "SETUP_001 one-shot calibration completed; this is not a Gold replay\n"
        ),
        cleanup_exit_code: int = 0,
        materialization_error: str | None = None,
    ) -> tuple[Path, list[list[str]], list[dict[str, object]], Exception | None]:
        run_dir = self._prepare_single_probe_run(root, run_id)
        run_args = self._run_args(run_dir)
        run_args.dry_run = False
        run_args.cleanup_passed_guest_replays = cleanup_requested
        commands: list[list[str]] = []
        pre_cleanup_statuses: list[dict[str, object]] = []
        self.last_materialization_calls: list[dict[str, object]] = []
        local_replay = run_dir / "cases/SETUP_001/runs/replay_1"

        def fake_execute(
            command: list[str],
            *,
            dry_run: bool,
            capture_path: Path | None = None,
        ) -> subprocess.CompletedProcess[str]:
            self.assertFalse(dry_run)
            commands.append(list(command))
            command_text = " ".join(command)
            if "sft-replay-cleanup" in command_text:
                pre_cleanup_statuses.append(
                    run_pilot._load_json(local_replay / "run_status.json")
                )
                return subprocess.CompletedProcess(
                    command, cleanup_exit_code, "", ""
                )
            if capture_path is not None:
                capture_path.parent.mkdir(parents=True, exist_ok=True)
                capture_path.write_text("mock Innovus output\n", encoding="utf-8")
                return subprocess.CompletedProcess(
                    command, innovus_exit_code, "", ""
                )
            return subprocess.CompletedProcess(command, 0, "", "")

        def fake_fetch(
            rsync: str,
            target: str,
            guest_path: str,
            destination: Path,
            dry_run: bool,
        ) -> bool:
            self.assertFalse(dry_run)
            self.assertEqual(local_replay, destination)
            if marker_text is not None:
                self._write_calibration_marker(destination, marker_text)
            return fetch_succeeded

        def fake_materialize(
            actual_run_dir: Path,
            *,
            case_id: str,
            replay_index: int,
            replay_dir: Path,
        ) -> Path:
            self.assertEqual(run_dir, actual_run_dir)
            self.assertEqual("SETUP_001", case_id)
            self.assertEqual(1, replay_index)
            self.assertEqual(local_replay, replay_dir)
            self.last_materialization_calls.append(
                {
                    "run_dir": actual_run_dir,
                    "case_id": case_id,
                    "replay_index": replay_index,
                    "replay_dir": replay_dir,
                }
            )
            if materialization_error is not None:
                raise RuntimeError(materialization_error)
            report = replay_dir / "checkpoint_link_materialization.json"
            report.write_text(
                '{"schema_version":"timing_eco_checkpoint_link_materialization.v1"}\n',
                encoding="utf-8",
            )
            return report

        error: Exception | None = None
        with mock.patch.object(
            run_pilot, "_execute", side_effect=fake_execute
        ), mock.patch.object(
            run_pilot, "_fetch_from_guest", side_effect=fake_fetch
        ), mock.patch.object(
            run_pilot,
            "_materialize_fetched_checkpoint",
            side_effect=fake_materialize,
        ):
            try:
                run_pilot.run_remote(run_args)
            except Exception as exc:  # Asserted by each failure-oriented caller.
                error = exc
        return run_dir, commands, pre_cleanup_statuses, error

    def test_prepare_renders_all_cases_and_artifact_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = self._baseline(root)
            catalog_path, catalog = self._probe_catalog(root)
            args = argparse.Namespace(
                baseline_bundle=baseline,
                work_root=root / "work",
                run_id="unit-test",
                case=[],
                guest_root="/home/host/nvdla_timing_eco_sft",
                catalog=catalog_path,
                allow_unfrozen_calibration=True,
            )
            run_dir = run_pilot.prepare_run(args, catalog)
            case_dirs = sorted((run_dir / "cases").iterdir())
            self.assertEqual(len(case_dirs), 10)
            manifest = run_pilot._load_json(case_dirs[0] / "manifest.json")
            self.assertEqual(set(manifest["artifacts"]), set(run_pilot._artifact_map()))
            self.assertEqual(manifest["analysis_views"]["setup"], "test_setup")
            run_manifest = run_pilot._load_json(run_dir / "run_manifest.json")
            self.assertEqual(
                manifest["baseline_sha256"], run_manifest["baseline"]["sha256"]
            )
            self.assertEqual(
                manifest["catalog_sha256"], run_manifest["catalog"]["sha256"]
            )
            replay = (case_dirs[0] / "replay.tcl").read_text(encoding="utf-8")
            self.assertIn("source [file join $baseline_dir restore.tcl]", replay)
            self.assertIn("::sft::write_stage_reports before", replay)
            self.assertIn("::sft::write_stage_reports after", replay)
            self.assertIn("::sft::write_constraint_snapshot before", replay)
            self.assertIn("::sft::write_constraint_snapshot after", replay)
            self.assertIn("::sft::write_functional_audit", replay)
            self.assertEqual(
                manifest["artifacts"]["fix_tcl"], "reports/concrete_fix.tcl"
            )
            self.assertEqual(manifest["artifacts"]["fixed_netlist"], "fixed.v")
            for role in (
                "constraint_setup_before",
                "constraint_setup_after",
                "constraint_hold_before",
                "constraint_hold_after",
                "diagnostic_context",
                "violating_checkpoint",
                "violating_checkpoint_data",
                "before_netlist",
                "setup_spef_before",
                "hold_spef_before",
                "native_cell_diff",
                "native_selected_terms",
            ):
                self.assertIn(role, manifest["artifacts"])
            self.assertFalse(manifest["gold_eligible"])
            self.assertEqual(
                run_manifest["status"], "NOT_GOLD_CALIBRATION_PREPARED"
            )
            checksum = run_dir / "baseline_checksums.sha256"
            self.assertEqual(
                run_pilot._sha256_file(checksum),
                run_manifest["baseline"]["checksum_manifest"]["sha256"],
            )

    def test_prepare_rejects_unfrozen_cases_without_explicit_calibration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog_path, catalog = self._probe_catalog(root, {"SETUP_001"})
            args = argparse.Namespace(
                baseline_bundle=self._baseline(root),
                work_root=root / "work",
                run_id="must-be-explicit",
                case=["SETUP_001"],
                guest_root="/home/host/nvdla_timing_eco_sft",
                catalog=catalog_path,
                allow_unfrozen_calibration=False,
            )
            with self.assertRaisesRegex(run_pilot.PilotError, "NOT_GOLD bundle"):
                run_pilot.prepare_run(args, catalog)

    def test_legacy_qualification_without_source_libs_is_not_gold_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = self._baseline(root)
            qualification_path = baseline / "qualification.json"
            qualification = json.loads(qualification_path.read_text(encoding="utf-8"))
            qualification.pop("source_artifacts")
            qualification_path.write_text(
                json.dumps(qualification, sort_keys=True) + "\n", encoding="utf-8"
            )
            probe_catalog_path, probe_catalog = self._probe_catalog(
                root, {"SETUP_001"}
            )

            calibration_args = argparse.Namespace(
                baseline_bundle=baseline,
                work_root=root / "work",
                run_id="legacy-not-gold",
                case=["SETUP_001"],
                guest_root="/home/host/nvdla_timing_eco_sft",
                catalog=probe_catalog_path,
                allow_unfrozen_calibration=True,
            )
            calibration_run = run_pilot.prepare_run(
                calibration_args, probe_catalog
            )
            calibration_manifest = run_pilot._load_json(
                calibration_run / "run_manifest.json"
            )
            self.assertEqual(
                "NOT_GOLD_CALIBRATION_PREPARED", calibration_manifest["status"]
            )

            frozen_catalog_path = root / "frozen_catalog.json"
            frozen_catalog = json.loads(
                run_pilot.CATALOG_PATH.read_text(encoding="utf-8")
            )
            setup_001 = next(
                case for case in frozen_catalog["cases"] if case["id"] == "SETUP_001"
            )
            setup_001["injection"]["calibration_status"] = "FROZEN"
            setup_001["injection"]["setup_parameters"][
                "delay_cell_reference"
            ] = "DLY2_X4M_A9TR40"
            frozen_catalog_path.write_text(
                json.dumps(frozen_catalog) + "\n", encoding="utf-8"
            )
            gold_args = argparse.Namespace(
                baseline_bundle=baseline,
                work_root=root / "work",
                run_id="legacy-must-not-be-gold",
                case=["SETUP_001"],
                guest_root="/home/host/nvdla_timing_eco_sft",
                allow_unfrozen_calibration=False,
                catalog=frozen_catalog_path,
            )
            with self.assertRaisesRegex(
                run_pilot.PilotError,
                "Gold preparation requires qualified source artifacts",
            ):
                run_pilot.prepare_run(
                    gold_args, run_pilot.load_catalog(frozen_catalog_path)
                )

    def test_baseline_requires_typed_qualification_status_and_hashes_it(self) -> None:
        catalog = run_pilot.load_catalog()
        with tempfile.TemporaryDirectory() as temporary:
            baseline = self._baseline(Path(temporary))
            qualification_path = baseline / "qualification.json"
            qualification = json.loads(qualification_path.read_text(encoding="utf-8"))
            qualification["status"] = "UNQUALIFIED"
            qualification_path.write_text(json.dumps(qualification), encoding="utf-8")
            with self.assertRaisesRegex(run_pilot.PilotError, "qualification status"):
                run_pilot._validate_baseline(baseline, catalog)

        with tempfile.TemporaryDirectory() as temporary:
            baseline = self._baseline(Path(temporary))
            qualification_path = baseline / "qualification.json"
            qualification = json.loads(qualification_path.read_text(encoding="utf-8"))
            qualification["drc"].pop("report_limit")
            qualification_path.write_text(json.dumps(qualification), encoding="utf-8")
            with self.assertRaisesRegex(run_pilot.PilotError, "report_limit=1000000"):
                run_pilot._validate_baseline(baseline, catalog)

        with tempfile.TemporaryDirectory() as temporary:
            baseline = self._baseline(Path(temporary))
            (baseline / "QUALIFIED_CANDIDATE").write_text(
                "status=QUALIFIED_CANDIDATE\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(run_pilot.PilotError, "missing 'gold_status=false'"):
                run_pilot._validate_baseline(baseline, catalog)

    def test_baseline_requires_setup_cds_and_nonempty_checkpoint_files(self) -> None:
        catalog = run_pilot.load_catalog()
        for missing in ("setup.tcl", "cds.lib"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temporary:
                baseline = self._baseline(Path(temporary))
                (baseline / missing).unlink()
                with self.assertRaisesRegex(run_pilot.PilotError, missing):
                    run_pilot._validate_baseline(baseline, catalog)

        with tempfile.TemporaryDirectory() as temporary:
            baseline = self._baseline(Path(temporary))
            (baseline / "setup.tcl").write_bytes(b"")
            with self.assertRaisesRegex(run_pilot.PilotError, r"setup\.tcl \(empty file\)"):
                run_pilot._validate_baseline(baseline, catalog)

        with tempfile.TemporaryDirectory() as temporary:
            baseline = self._baseline(Path(temporary))
            checkpoint_file = baseline / "base.enc.dat" / "db.bin"
            checkpoint_file.write_bytes(b"")
            with self.assertRaisesRegex(run_pilot.PilotError, "no non-empty regular file"):
                run_pilot._validate_baseline(baseline, catalog)

        with tempfile.TemporaryDirectory() as temporary:
            baseline = self._baseline(Path(temporary))
            checkpoint_file = baseline / "base.enc.dat" / "db.bin"
            checkpoint_file.unlink()
            (baseline / "base.enc.dat").rmdir()
            (baseline / "base.enc.dat").write_text("not a directory\n", encoding="utf-8")
            with self.assertRaisesRegex(run_pilot.PilotError, "not a directory"):
                run_pilot._validate_baseline(baseline, catalog)

    def test_prepare_copies_and_hashes_the_requested_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = self._baseline(root)
            custom_catalog = root / "custom_catalog.json"
            raw = _probe_catalog_raw({"SETUP_001"})
            raw["dataset"] = "custom/catalog/source"
            custom_catalog.write_text(
                json.dumps(raw, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            args = argparse.Namespace(
                baseline_bundle=baseline,
                work_root=root / "work",
                run_id="custom-catalog",
                case=["SETUP_001"],
                guest_root="/home/host/nvdla_timing_eco_sft",
                catalog=custom_catalog,
                allow_unfrozen_calibration=True,
            )
            run_dir = run_pilot.prepare_run(args, run_pilot.load_catalog(custom_catalog))
            copied = run_dir / "catalog.json"
            self.assertEqual(custom_catalog.read_bytes(), copied.read_bytes())
            manifest = run_pilot._load_json(run_dir / "run_manifest.json")
            self.assertEqual(str(custom_catalog.resolve()), manifest["catalog"]["source"])
            self.assertEqual(run_pilot._sha256_file(copied), manifest["catalog"]["sha256"])

    def test_run_rejects_baseline_mutated_after_prepare_before_remote_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = self._baseline(root)
            catalog_path, catalog = self._probe_catalog(root, {"SETUP_001"})
            args = argparse.Namespace(
                baseline_bundle=baseline,
                work_root=root / "work",
                run_id="mutated-baseline",
                case=["SETUP_001"],
                guest_root="/home/host/nvdla_timing_eco_sft",
                catalog=catalog_path,
                allow_unfrozen_calibration=True,
            )
            run_dir = run_pilot.prepare_run(args, catalog)
            (baseline / "setup.tcl").write_text("# mutated\n", encoding="utf-8")
            with mock.patch.object(run_pilot, "_execute") as execute:
                with self.assertRaisesRegex(run_pilot.PilotError, "SHA256 changed"):
                    run_pilot.run_remote(self._run_args(run_dir))
                execute.assert_not_called()

    def test_runner_delegate_materializes_exact_candidate_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self._prepare_single_probe_run(
                Path(temporary), "normalizer-delegate"
            )
            replay = run_dir / "cases/SETUP_001/runs/replay_1"
            checkpoint = replay / "violating.enc.dat"
            checkpoint.mkdir(parents=True)
            target = (
                "/home/host/nvdla_timing_eco_sft/normalizer-delegate/"
                "baseline/base.enc.dat/db.bin"
            )
            os.symlink(target, checkpoint / "db.bin")

            report = run_pilot._materialize_fetched_checkpoint(
                run_dir,
                case_id="SETUP_001",
                replay_index=1,
                replay_dir=replay,
            )
            self.assertEqual(
                replay / "checkpoint_link_materialization.json", report
            )
            self.assertFalse((checkpoint / "db.bin").is_symlink())
            self.assertEqual(b"fixture", (checkpoint / "db.bin").read_bytes())
            payload = run_pilot._load_json(report)
            self.assertEqual(
                "timing_eco_checkpoint_link_materialization.v1",
                payload["schema_version"],
            )
            self.assertEqual("db.bin", payload["materialized_links"][0]["path"])

    def test_dry_run_uses_fresh_guest_dirs_delete_sync_and_cds_lib(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline = self._baseline(root)
            catalog_path, catalog = self._probe_catalog(root, {"SETUP_001"})
            prepare_args = argparse.Namespace(
                baseline_bundle=baseline,
                work_root=root / "work",
                run_id="fresh-guest",
                case=["SETUP_001"],
                guest_root="/home/host/nvdla_timing_eco_sft",
                catalog=catalog_path,
                allow_unfrozen_calibration=True,
            )
            run_dir = run_pilot.prepare_run(prepare_args, catalog)
            output = io.StringIO()
            with mock.patch.object(
                run_pilot, "_materialize_fetched_checkpoint"
            ) as materialize, contextlib.redirect_stdout(output):
                run_pilot.run_remote(self._run_args(run_dir))
            materialize.assert_not_called()
            commands = output.getvalue()
            guest_run = "/home/host/nvdla_timing_eco_sft/fresh-guest"
            guest_replay = f"{guest_run}/cases/SETUP_001/replay_1"
            self.assertIn(f"mkdir -- {guest_run}", commands)
            self.assertIn(f"mkdir -- {guest_replay}", commands)
            self.assertNotIn(f"mkdir -p -- {guest_replay}", commands)
            self.assertIn("rsync -az --delete", commands)
            self.assertIn("baseline_checksums.sha256", commands)
            self.assertIn("sha256sum --check --strict --quiet", commands)
            self.assertIn("SFT_ALLOW_UNFROZEN_CALIBRATION=1", commands)
            self.assertIn(
                f"-cds_lib_file {guest_run}/baseline/cds.lib", commands
            )
            run_manifest = run_pilot._load_json(run_dir / "run_manifest.json")
            replay_status = run_pilot._load_json(
                run_dir / "cases/SETUP_001/runs/replay_1/run_status.json"
            )
            self.assertEqual(
                run_manifest["baseline"]["sha256"],
                replay_status["baseline_sha256"],
            )
            self.assertEqual(replay_status["mode"], "NOT_GOLD_CALIBRATION")
            self.assertFalse(replay_status["gold_eligible"])
            self.assertFalse(replay_status["passed"])
            materialization = replay_status["checkpoint_link_materialization"]
            self.assertTrue(materialization["candidate"])
            self.assertFalse(materialization["required"])
            self.assertFalse(materialization["attempted"])
            self.assertIsNone(materialization["succeeded"])
            self.assertEqual("dry_run_only", materialization["state"])
            self.assertTrue(materialization["dry_run"])
            self.assertIsNone(materialization["report"])
            self.assertFalse(replay_status["guest_replay_cleanup"]["eligible"])
            root_status = run_pilot._load_json(run_dir / "run_status.json")
            self.assertFalse(root_status["passed"])
            self.assertTrue(root_status["dry_run_completed"])

    def test_guest_replay_cleanup_is_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir, commands, pre_cleanup, error = self._run_with_mocked_remote(
                Path(temporary),
                run_id="cleanup-default-off",
                cleanup_requested=False,
            )
            self.assertIsNone(error)
            self.assertFalse(
                any("sft-replay-cleanup" in " ".join(command) for command in commands)
            )
            self.assertEqual([], pre_cleanup)
            status = run_pilot._load_json(
                run_dir / "cases/SETUP_001/runs/replay_1/run_status.json"
            )
            self.assertEqual(1, len(self.last_materialization_calls))
            materialization = status["checkpoint_link_materialization"]
            self.assertTrue(materialization["candidate"])
            self.assertTrue(materialization["required"])
            self.assertTrue(materialization["attempted"])
            self.assertTrue(materialization["succeeded"])
            self.assertEqual("materialized_verified", materialization["state"])
            self.assertEqual(
                "checkpoint_link_materialization.json",
                materialization["report"],
            )
            self.assertRegex(materialization["report_sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(materialization["report_bytes"], 0)
            self.assertIsNone(materialization["error"])
            cleanup = status["guest_replay_cleanup"]
            self.assertEqual("not_requested", cleanup["state"])
            self.assertFalse(cleanup["requested"])
            self.assertTrue(cleanup["eligible"])
            self.assertFalse(cleanup["command_issued"])
            self.assertIsNone(cleanup["target"])

    def test_typed_success_marker_requires_exact_regular_file_bytes(self) -> None:
        expected = (
            b"SETUP_001 one-shot calibration completed; "
            b"this is not a Gold replay\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "NOT_GOLD_CALIBRATION"
            marker.write_bytes(expected)
            self.assertTrue(
                run_pilot._typed_success_marker_present(
                    marker,
                    case_id="SETUP_001",
                    calibration_mode=True,
                    dry_run=False,
                )
            )

            marker.write_bytes(expected[:-1] + b"\r\n")
            self.assertFalse(
                run_pilot._typed_success_marker_present(
                    marker,
                    case_id="SETUP_001",
                    calibration_mode=True,
                    dry_run=False,
                )
            )

            target = root / "real_marker"
            target.write_bytes(expected)
            marker.unlink()
            marker.symlink_to(target)
            self.assertFalse(
                run_pilot._typed_success_marker_present(
                    marker,
                    case_id="SETUP_001",
                    calibration_mode=True,
                    dry_run=False,
                )
            )

    def test_successful_cleanup_requires_durable_status_and_verifies_absence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir, commands, pre_cleanup, error = self._run_with_mocked_remote(
                Path(temporary),
                run_id="cleanup-success",
                cleanup_requested=True,
            )
            self.assertIsNone(error)
            cleanup_commands = [
                command
                for command in commands
                if "sft-replay-cleanup" in " ".join(command)
            ]
            self.assertEqual(1, len(cleanup_commands))
            cleanup_text = " ".join(cleanup_commands[0])
            target = (
                "/home/host/nvdla_timing_eco_sft/cleanup-success/"
                "cases/SETUP_001/replay_1"
            )
            self.assertIn(target, cleanup_text)
            self.assertIn("rm -rf", cleanup_text)
            self.assertIn("--one-file-system", cleanup_text)
            self.assertIn("find -P", cleanup_text)
            self.assertIn("-xdev -type d", cleanup_text)
            self.assertIn("chmod u+rwx", cleanup_text)
            self.assertIn('[ -e "$target" ]', cleanup_text)
            self.assertIn('[ -L "$target" ]', cleanup_text)
            self.assertIn('[ ! -d "$target" ]', cleanup_text)
            self.assertIn("exit 124", cleanup_text)
            self.assertIn("exit 125", cleanup_text)

            self.assertEqual(1, len(pre_cleanup))
            armed = pre_cleanup[0]
            self.assertTrue(armed["passed"])
            self.assertTrue(armed["marker_present"])
            self.assertTrue(armed["fetched"])
            self.assertEqual(1, len(self.last_materialization_calls))
            self.assertTrue(
                armed["checkpoint_link_materialization"]["succeeded"]
            )
            self.assertEqual(
                "materialized_verified",
                armed["checkpoint_link_materialization"]["state"],
            )
            armed_cleanup = armed["guest_replay_cleanup"]
            self.assertEqual("armed", armed_cleanup["state"])
            self.assertTrue(armed_cleanup["local_status_persisted_before_command"])
            self.assertFalse(armed_cleanup["command_issued"])

            status = run_pilot._load_json(
                run_dir / "cases/SETUP_001/runs/replay_1/run_status.json"
            )
            cleanup = status["guest_replay_cleanup"]
            self.assertEqual("deleted_verified", cleanup["state"])
            self.assertTrue(cleanup["command_issued"])
            self.assertTrue(cleanup["succeeded"])
            self.assertTrue(cleanup["verified_absent"])
            self.assertEqual(0, cleanup["returncode"])
            self.assertEqual(target, cleanup["target"])
            self.assertTrue(run_pilot._load_json(run_dir / "run_status.json")["passed"])

    def test_cleanup_script_handles_read_only_tree_without_following_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            outside_file = outside / "must_survive.txt"
            outside_file.write_text("outside\n", encoding="utf-8")
            outside.chmod(0o555)
            outside_file.chmod(0o444)

            replay = root / "replay_1"
            nested = replay / "reports/deep"
            nested.mkdir(parents=True)
            readonly_file = nested / "report.rpt"
            readonly_file.write_text("report\n", encoding="utf-8")
            (replay / "outside_link").symlink_to(outside, target_is_directory=True)
            readonly_file.chmod(0o444)
            nested.chmod(0o555)
            nested.parent.chmod(0o555)
            replay.chmod(0o555)

            result = subprocess.run(
                [
                    "sh",
                    "-c",
                    run_pilot.GUEST_REPLAY_CLEANUP_SCRIPT,
                    "sft-replay-cleanup",
                    str(replay),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            # Restore external fixture permissions even if an assertion below
            # fails, so TemporaryDirectory can always clean up safely.
            outside_mode = outside.stat().st_mode & 0o777
            outside_file_mode = outside_file.stat().st_mode & 0o777
            outside.chmod(0o755)
            outside_file.chmod(0o644)
            self.assertEqual(0, result.returncode, result.stdout)
            self.assertFalse(replay.exists())
            self.assertFalse(replay.is_symlink())
            self.assertTrue(outside_file.is_file())
            self.assertEqual(0o555, outside_mode)
            self.assertEqual(0o444, outside_file_mode)

    def test_cleanup_script_rejects_symlink_leaf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            witness = outside / "witness.txt"
            witness.write_text("must survive\n", encoding="utf-8")
            replay_link = root / "replay_1"
            replay_link.symlink_to(outside, target_is_directory=True)
            result = subprocess.run(
                [
                    "sh",
                    "-c",
                    run_pilot.GUEST_REPLAY_CLEANUP_SCRIPT,
                    "sft-replay-cleanup",
                    str(replay_link),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            self.assertEqual(124, result.returncode, result.stdout)
            self.assertTrue(replay_link.is_symlink())
            self.assertEqual("must survive\n", witness.read_text(encoding="utf-8"))

    def test_failed_replay_fetch_or_marker_never_triggers_cleanup(self) -> None:
        scenarios = (
            {
                "name": "innovus",
                "innovus_exit_code": 1,
                "fetch_succeeded": True,
                "marker_text": (
                    "SETUP_001 one-shot calibration completed; "
                    "this is not a Gold replay\n"
                ),
            },
            {
                "name": "fetch",
                "innovus_exit_code": 0,
                "fetch_succeeded": False,
                # Even an exact local marker cannot compensate for a failed fetch.
                "marker_text": (
                    "SETUP_001 one-shot calibration completed; "
                    "this is not a Gold replay\n"
                ),
            },
            {
                "name": "marker_missing",
                "innovus_exit_code": 0,
                "fetch_succeeded": True,
                "marker_text": None,
            },
            {
                "name": "marker_wrong_type",
                "innovus_exit_code": 0,
                "fetch_succeeded": True,
                "marker_text": "SETUP_001 replay completed\n",
            },
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario["name"]), tempfile.TemporaryDirectory() as temporary:
                run_dir, commands, pre_cleanup, error = self._run_with_mocked_remote(
                    Path(temporary),
                    run_id=f"cleanup-ineligible-{scenario['name']}",
                    cleanup_requested=True,
                    innovus_exit_code=scenario["innovus_exit_code"],
                    fetch_succeeded=scenario["fetch_succeeded"],
                    marker_text=scenario["marker_text"],
                )
                self.assertIsInstance(error, run_pilot.PilotError)
                self.assertFalse(
                    any(
                        "sft-replay-cleanup" in " ".join(command)
                        for command in commands
                    )
                )
                self.assertEqual([], pre_cleanup)
                self.assertEqual([], self.last_materialization_calls)
                status = run_pilot._load_json(
                    run_dir / "cases/SETUP_001/runs/replay_1/run_status.json"
                )
                materialization = status["checkpoint_link_materialization"]
                self.assertFalse(materialization["candidate"])
                self.assertFalse(materialization["required"])
                self.assertFalse(materialization["attempted"])
                self.assertIsNone(materialization["succeeded"])
                self.assertEqual("not_candidate", materialization["state"])
                self.assertIsNone(materialization["error"])
                cleanup = status["guest_replay_cleanup"]
                self.assertFalse(status["passed"])
                self.assertFalse(cleanup["eligible"])
                self.assertEqual("retained_not_eligible", cleanup["state"])
                self.assertFalse(cleanup["command_issued"])
                self.assertFalse(
                    run_pilot._load_json(run_dir / "run_status.json")["passed"]
                )

    def test_cleanup_failure_is_durable_and_fails_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir, commands, pre_cleanup, error = self._run_with_mocked_remote(
                Path(temporary),
                run_id="cleanup-failure",
                cleanup_requested=True,
                cleanup_exit_code=125,
            )
            self.assertIsInstance(error, run_pilot.PilotError)
            self.assertEqual(1, len(pre_cleanup))
            self.assertEqual(
                1,
                sum(
                    "sft-replay-cleanup" in " ".join(command)
                    for command in commands
                ),
            )
            status = run_pilot._load_json(
                run_dir / "cases/SETUP_001/runs/replay_1/run_status.json"
            )
            cleanup = status["guest_replay_cleanup"]
            self.assertTrue(status["passed"])
            self.assertEqual("failed", cleanup["state"])
            self.assertTrue(cleanup["command_issued"])
            self.assertFalse(cleanup["succeeded"])
            self.assertFalse(cleanup["verified_absent"])
            self.assertEqual(125, cleanup["returncode"])
            self.assertIn("absence verification failed", cleanup["error"])
            self.assertFalse(
                run_pilot._load_json(run_dir / "run_status.json")["passed"]
            )

    def test_materialization_failure_is_typed_and_forbids_guest_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir, commands, pre_cleanup, error = self._run_with_mocked_remote(
                Path(temporary),
                run_id="materialization-failure",
                cleanup_requested=True,
                materialization_error="baseline link checksum mismatch",
            )
            self.assertIsInstance(error, run_pilot.PilotError)
            self.assertEqual(1, len(self.last_materialization_calls))
            self.assertEqual([], pre_cleanup)
            self.assertFalse(
                any(
                    "sft-replay-cleanup" in " ".join(command)
                    for command in commands
                )
            )
            replay_status = run_pilot._load_json(
                run_dir / "cases/SETUP_001/runs/replay_1/run_status.json"
            )
            self.assertFalse(replay_status["passed"])
            materialization = replay_status["checkpoint_link_materialization"]
            self.assertTrue(materialization["candidate"])
            self.assertTrue(materialization["required"])
            self.assertTrue(materialization["attempted"])
            self.assertFalse(materialization["succeeded"])
            self.assertEqual("failed", materialization["state"])
            self.assertIsNone(materialization["report"])
            self.assertEqual(
                {
                    "code": "CHECKPOINT_LINK_MATERIALIZATION_FAILED",
                    "type": "RuntimeError",
                    "message": "baseline link checksum mismatch",
                },
                materialization["error"],
            )
            cleanup = replay_status["guest_replay_cleanup"]
            self.assertFalse(cleanup["eligible"])
            self.assertEqual("retained_not_eligible", cleanup["state"])
            self.assertFalse(cleanup["command_issued"])
            self.assertFalse(
                run_pilot._load_json(run_dir / "run_status.json")["passed"]
            )

    def test_cleanup_path_builder_rejects_unsafe_root_run_and_case(self) -> None:
        for unsafe_root in (
            "/",
            "relative/root",
            "//tmp/task",
            "/tmp//task",
            "/tmp/./task",
            "/tmp/../task",
        ):
            with self.subTest(root=unsafe_root), self.assertRaises(run_pilot.PilotError):
                run_pilot._guest_replay_path(
                    unsafe_root, "safe-run", "SETUP_001", 1
                )

        for unsafe_run in ("", ".", "..", "run/id", "run id"):
            with self.subTest(run=unsafe_run), self.assertRaises(run_pilot.PilotError):
                run_pilot._guest_replay_path(
                    "/tmp/task", unsafe_run, "SETUP_001", 1
                )

        for unsafe_case in ("", ".", "..", "SETUP/001", "SETUP 001"):
            with self.subTest(case=unsafe_case), self.assertRaises(run_pilot.PilotError):
                run_pilot._guest_replay_path(
                    "/tmp/task", "safe-run", unsafe_case, 1
                )

        self.assertEqual(
            "/tmp/task/safe-run/cases/SETUP_001/replay_2",
            run_pilot._guest_replay_path(
                "/tmp/task/", "safe-run", "SETUP_001", 2
            ),
        )

    def test_durable_json_fsyncs_file_and_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "status.json"
            real_fsync = os.fsync
            with mock.patch.object(
                run_pilot.os, "fsync", wraps=real_fsync
            ) as fsync:
                run_pilot._write_json(path, {"passed": True}, durable=True)
            self.assertEqual(2, fsync.call_count)
            self.assertEqual({"passed": True}, run_pilot._load_json(path))

    def test_run_rejects_unfrozen_bundle_without_calibration_before_remote_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            catalog_path, catalog = self._probe_catalog(root, {"SETUP_001"})
            prepare_args = argparse.Namespace(
                baseline_bundle=self._baseline(root),
                work_root=root / "work",
                run_id="reject-gold-run",
                case=["SETUP_001"],
                guest_root="/home/host/nvdla_timing_eco_sft",
                catalog=catalog_path,
                allow_unfrozen_calibration=True,
            )
            run_dir = run_pilot.prepare_run(prepare_args, catalog)
            run_args = self._run_args(run_dir)
            run_args.allow_unfrozen_calibration = False
            with mock.patch.object(run_pilot, "_execute") as execute:
                with self.assertRaisesRegex(run_pilot.PilotError, "PROBE_REQUIRED"):
                    run_pilot.run_remote(run_args)
                execute.assert_not_called()

    def test_gold_run_requires_two_fresh_replays(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            custom_catalog = root / "frozen_catalog.json"
            raw = json.loads(run_pilot.CATALOG_PATH.read_text(encoding="utf-8"))
            setup_001 = next(case for case in raw["cases"] if case["id"] == "SETUP_001")
            setup_001["injection"]["calibration_status"] = "FROZEN"
            setup_001["injection"]["setup_parameters"][
                "delay_cell_reference"
            ] = "DLY2_X4M_A9TR40"
            custom_catalog.write_text(json.dumps(raw), encoding="utf-8")
            prepare_args = argparse.Namespace(
                baseline_bundle=self._baseline(root),
                work_root=root / "work",
                run_id="frozen-two-replays",
                case=["SETUP_001"],
                guest_root="/home/host/nvdla_timing_eco_sft",
                allow_unfrozen_calibration=False,
                catalog=custom_catalog,
            )
            catalog = run_pilot.load_catalog(custom_catalog)
            run_dir = run_pilot.prepare_run(prepare_args, catalog)
            run_args = self._run_args(run_dir, replays=1)
            run_args.allow_unfrozen_calibration = False
            with mock.patch.object(run_pilot, "_execute") as execute:
                with self.assertRaisesRegex(run_pilot.PilotError, "exactly 2 fresh"):
                    run_pilot.run_remote(run_args)
                execute.assert_not_called()

    def test_calibration_replay_exits_after_injection_before_gold_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, probe_catalog = self._probe_catalog(
                Path(temporary), {"SETUP_001"}
            )
            replay = run_pilot._render_replay(probe_catalog["cases"][0])
        inject_index = replay.index("source [file join $::SFT_CASE_DIR inject.tcl]")
        marker_index = replay.index("::sft::write_calibration_marker")
        reports_index = replay.index("::sft::write_stage_reports before")
        constraint_index = replay.index("::sft::write_constraint_snapshot before")
        design_index = replay.index("::sft::write_violating_design")
        fix_index = replay.index("source [file join $::SFT_CASE_DIR fix.tcl]")
        self.assertLess(inject_index, marker_index)
        self.assertLess(marker_index, reports_index)
        self.assertLess(reports_index, constraint_index)
        self.assertLess(constraint_index, design_index)
        self.assertLess(design_index, fix_index)
        self.assertIn("exit 0", replay[marker_index:reports_index])
        self.assertIn("NOT_GOLD_CALIBRATION", replay)

    def test_dry_run_does_not_spawn_rsync(self) -> None:
        with mock.patch.object(run_pilot.subprocess, "run") as subprocess_run:
            run_pilot._sync_to_guest(
                "rsync", Path("."), "qingteng-fc", "/home/host/test", dry_run=True
            )
            subprocess_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests for the outcome-based RL/benchmark acceptance checker."""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("check_benchmark_pass.py")
SPEC = importlib.util.spec_from_file_location("check_benchmark_pass", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)

PILOT = Path(__file__).resolve().parents[1] / "pilot_10"
CRITERIA = PILOT / "benchmark_pass_criteria.json"
CASE_IDS = [
    "SETUP_001",
    "SETUP_002",
    "SETUP_003",
    "SETUP_004",
    "HOLD_001",
    "HOLD_002",
    "HOLD_003",
    "HOLD_004",
    "MIXED_001",
    "MIXED_002",
]


class BenchmarkPassTest(unittest.TestCase):
    def test_all_ten_canonical_cases_pass_official_profile(self) -> None:
        if not (PILOT / "cases" / CASE_IDS[0] / "manifest.json").is_file():
            self.skipTest("canonical Pilot-10 evidence is not present")
        for case_id in CASE_IDS:
            with self.subTest(case_id=case_id):
                case = PILOT / "cases" / case_id
                result = checker.evaluate_case(case, case, CRITERIA, "official")
                self.assertTrue(result["passed"], result["failed_gates"])
                self.assertTrue(result["official_benchmark_pass"])
                self.assertEqual(result["binary_reward"], 1.0)

    def test_rl_fast_is_feasible_but_not_official_pass(self) -> None:
        case = PILOT / "cases" / "SETUP_001"
        if not (case / "manifest.json").is_file():
            self.skipTest("canonical Pilot-10 evidence is not present")
        result = checker.evaluate_case(case, case, CRITERIA, "rl_fast")
        self.assertTrue(result["passed"])
        self.assertEqual(result["decision_label"], "RL_FEASIBLE")
        self.assertFalse(result["official_benchmark_pass"])

    def test_timing_gate_rejects_subthreshold_setup(self) -> None:
        criteria = json.loads(CRITERIA.read_text(encoding="utf-8"))
        metrics = {
            "after": {
                "setup": {"wns_ns": 0.009, "tns_ns": 0.0},
                "hold": {"wns_ns": 0.010, "tns_ns": 0.0},
            }
        }
        passed, _, failures = checker._timing_gate(
            metrics, criteria["innovus_timing"]
        )
        self.assertFalse(passed)
        self.assertTrue(any("setup WNS" in item for item in failures))

    def test_timing_gate_rejects_nonzero_tns(self) -> None:
        criteria = json.loads(CRITERIA.read_text(encoding="utf-8"))
        metrics = {
            "after": {
                "setup": {"wns_ns": 0.020, "tns_ns": -0.001},
                "hold": {"wns_ns": 0.020, "tns_ns": 0.0},
            }
        }
        passed, _, failures = checker._timing_gate(
            metrics, criteria["innovus_timing"]
        )
        self.assertFalse(passed)
        self.assertTrue(any("TNS" in item for item in failures))

    def test_drc_category_regression_fails_even_if_total_drops(self) -> None:
        metrics = {
            "before": {
                "drv": {
                    "max_transition_violations": 0,
                    "max_capacitance_violations": 0,
                    "max_fanout_violations": 10,
                },
                "drc": {"total": 10, "categories": {"SHORT": 5, "SPACING": 5}},
            },
            "after": {
                "drv": {
                    "max_transition_violations": 0,
                    "max_capacitance_violations": 0,
                    "max_fanout_violations": 10,
                },
                "drc": {"total": 9, "categories": {"SHORT": 6, "SPACING": 3}},
                "connectivity": {"violations": 0},
            },
        }
        passed, _, failures = checker._physical_gate(metrics)
        self.assertFalse(passed)
        self.assertTrue(any("SHORT regressed" in item for item in failures))

    def test_candidate_policy_rejects_hidden_instance_and_tcl_substitution(self) -> None:
        diagnostic = {
            "targets": [
                {
                    "endpoint": "target_reg/D",
                    "local_cells": [{"inst": "U_VISIBLE", "ref": "BUF_X1"}],
                }
            ]
        }
        candidate = """\
setEcoMode -batchMode true
ecoChangeCell -inst U_HIDDEN -cell BUF_X2
puts [exec touch hacked]
setEcoMode -batchMode false
refinePlace -eco true
ecoRoute -target
"""
        passed, _, failures = checker._fix_policy(
            candidate,
            case_id="SETUP_001",
            case_type="setup",
            budget=2,
            diagnostic=diagnostic,
        )
        self.assertFalse(passed)
        self.assertTrue(any("absent from diagnostic" in item for item in failures))
        self.assertTrue(any("substitution" in item for item in failures))

    def test_machine_criteria_match_catalog_thresholds(self) -> None:
        criteria = json.loads(CRITERIA.read_text(encoding="utf-8"))
        catalog = json.loads((PILOT / "catalog.json").read_text(encoding="utf-8"))
        acceptance = catalog["design"]["acceptance"]
        self.assertEqual(
            criteria["innovus_timing"]["setup"]["wns_min_ns"],
            acceptance["final_setup_wns_min_ns"],
        )
        self.assertEqual(
            criteria["innovus_timing"]["hold"]["wns_min_ns"],
            acceptance["final_hold_wns_min_ns"],
        )
        self.assertEqual(
            criteria["profiles"]["official"]["required_innovus_replays"],
            acceptance["replay_runs"],
        )


if __name__ == "__main__":
    unittest.main()

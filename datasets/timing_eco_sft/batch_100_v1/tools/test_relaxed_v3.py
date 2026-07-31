#!/usr/bin/env python3
"""Static contract tests for the planning-only relaxed-v3 cards."""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import relaxed_v3  # noqa: E402


class RelaxedV3ContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.specs = relaxed_v3.build_specs()

    def test_build_is_deterministic_and_valid(self) -> None:
        self.assertEqual(self.specs, relaxed_v3.build_specs())
        relaxed_v3.validate_specs(self.specs)

    def test_first_five_preserve_catalog_severity_ladder(self) -> None:
        actual = [
            (
                case["id"],
                case["nominal_severity"]["ps"],
                case["nominal_severity"]["tolerance_ps"],
            )
            for case in self.specs["cases"][:5]
        ]
        self.assertEqual(
            actual,
            [
                ("B1_CASE_001", 60, 10.0),
                ("B1_CASE_002", 80, 12.0),
                ("B1_CASE_003", 100, 15.0),
                ("B1_CASE_004", 120, 18.0),
                ("B1_CASE_005", 140, 21.0),
            ],
        )

    def test_only_construction_complexity_is_relaxed(self) -> None:
        base = relaxed_v3._read_mapping(relaxed_v3.BASE_SPECS)
        preserved = (
            "id",
            "shape",
            "strategy",
            "difficulty",
            "hierarchy",
            "protected_endpoint_count",
            "load_only_sink_count",
            "guardrail_axis",
            "catalog_evidence",
            "machine_semantics",
            "nominal_severity",
        )
        for source, case in zip(base["cases"], self.specs["cases"], strict=True):
            for key in preserved:
                self.assertEqual(case[key], source[key], (case["id"], key))
            self.assertEqual(case["expected_modification_count"], 1)
            self.assertEqual(case["target_endpoint_count"], 1)
            self.assertEqual(case["injection_profile"], "I0")
            self.assertIs(case["direct_inverse_allowed"], True)

    def test_measured_target_cannot_be_written_back(self) -> None:
        changed = copy.deepcopy(self.specs)
        changed["cases"][1]["nominal_severity"]["ps"] = 85
        with self.assertRaises(relaxed_v3.RelaxedV3Error):
            relaxed_v3.validate_specs(changed)

    def test_card_is_explicitly_planning_only(self) -> None:
        relaxation = self.specs["relaxation"]
        self.assertEqual(relaxation["current_phase"], "planning_specification_only")
        self.assertIs(relaxation["data_generation_performed"], False)
        for case in self.specs["cases"]:
            self.assertFalse(relaxed_v3.REALIZED_CASE_KEYS.intersection(case))

    def test_realized_data_is_rejected(self) -> None:
        changed = copy.deepcopy(self.specs)
        changed["cases"][0]["metrics"] = {"setup_wns_before_ns": -0.06}
        with self.assertRaises(relaxed_v3.RelaxedV3Error):
            relaxed_v3.validate_specs(changed)


if __name__ == "__main__":
    unittest.main()

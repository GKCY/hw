#!/usr/bin/env python3
"""Static safety/coverage tests for the Innovus runtime API help probe."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


PROBE = (
    Path(__file__).resolve().parents[1]
    / "pilot_10"
    / "templates"
    / "innovus_runtime_api_probe.tcl"
)


class InnovusRuntimeApiProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = PROBE.read_text(encoding="utf-8")

    def test_covers_every_runtime_sensitive_command(self) -> None:
        expected = {
            "ecoChangeCell": {"-inst", "-cell"},
            "ecoAddRepeater": {"-term", "-cell", "-name"},
            "setEcoMode": {"-batchMode"},
            "saveDesign": {"-rc"},
            "refinePlace": {"-eco"},
            "ecoRoute": set(),
            "rcOut": {"-spef", "-view"},
            "report_timing": {
                "-collection",
                "-late",
                "-early",
                "-view",
                "-max_paths",
                "-path_type",
                "-max_slack",
            },
            "optDesign": {
                "-postRoute",
                "-setup",
                "-hold",
                "-selectedTerms",
                "-incr",
            },
            "all_fanin": {"-to", "-only_cells"},
        }
        for command, tokens in expected.items():
            with self.subTest(command=command):
                self.assertRegex(
                    self.text,
                    rf"\[list\s+[a-z0-9_]+\s+{re.escape(command)}\s+"
                    rf"(?:\\\s*)?\{{([^}}]*)\}}",
                )
                for token in tokens:
                    self.assertIn(token, self.text)
        self.assertIn("-selectedTerms <fileName>", self.text)
        self.assertNotIn("construct the explicit selectedTerms scope", self.text)

    def test_never_executes_mutating_or_report_commands(self) -> None:
        unsafe = (
            "ecoChangeCell",
            "ecoAddRepeater",
            "setEcoMode",
            "saveDesign",
            "refinePlace",
            "ecoRoute",
            "rcOut",
            "report_timing",
            "optDesign",
            "selectPin",
            "deselectAll",
            "all_fanin",
        )
        executable_lines = [
            line
            for line in self.text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        for command in unsafe:
            with self.subTest(command=command):
                self.assertFalse(
                    any(
                        re.match(rf"^\s*(?:catch\s+\{{)?{re.escape(command)}\b", line)
                        for line in executable_lines
                    ),
                    f"probe appears to execute {command}",
                )
        self.assertNotIn("uplevel", self.text)
        self.assertNotRegex(self.text, r"(?m)^\s*eval\b")
        self.assertNotRegex(self.text, r"(?m)^\s*source\b")
        self.assertNotRegex(self.text, r"(?m)^\s*exit\b")

    def test_output_is_explicitly_non_gold_and_non_mutating(self) -> None:
        self.assertIn("GOLD_ELIGIBLE=FALSE", self.text)
        self.assertIn("DESIGN_MUTATIONS=0", self.text)
        self.assertIn("COMMANDS_EXECUTED=HELP_ONLY", self.text)
        self.assertIn("innovus_runtime_api_help_probe.v1", self.text)

    def test_refuses_to_overwrite_existing_evidence_directory(self) -> None:
        self.assertIn("SFT_RUNTIME_API_PROBE_DIR already exists", self.text)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import threading
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import catalog
import batch100
from common import BatchError, parse_fix_tcl, safe_remove_task_leaf, tree_sha256
import evidence
import finalize_batch
from state_store import StateStore


def _materialize_test_frozen_checkpoint(
    run_dir: Path, case_id: str
) -> tuple[Path, dict]:
    """Build a tiny real hash-bound frozen bundle around existing inputs."""

    job = run_dir / "jobs" / case_id
    job.mkdir(parents=True, exist_ok=True)
    bindings = run_dir / "bindings"
    bindings.mkdir(parents=True, exist_ok=True)
    binding = bindings / f"{case_id}.json"
    plan = job / "calibration_plan.json"
    config = job / "runtime_config.tcl"
    fix = job / "fix.tcl"
    if not binding.exists():
        binding.write_text(json.dumps({"case_id": case_id}))
    if not config.exists():
        config.write_text(f"set B100_CASE_ID {{{case_id}}}\n")
    if not fix.exists():
        fix.write_text("refinePlace -eco true\n")
    if not plan.exists():
        plan.write_text(
            json.dumps(
                {
                    "schema_version":
                        "mock_lef_batch100.calibration_plan.v1",
                    "case_id": case_id,
                    "slot": "slot0",
                    "binding_sha256": hashlib.sha256(
                        binding.read_bytes()
                    ).hexdigest(),
                    "injection_operations": [],
                    "repair_operations": [],
                }
            )
        )

    frozen, _ = batch100._frozen_checkpoint_paths(job)
    frozen.mkdir()
    unpacked = frozen / "unpacked"
    data = unpacked / "violating.enc.dat"
    data.mkdir(parents=True)
    (unpacked / "violating.enc").write_text("source checkpoint.dat\n")
    (data / "top.db").write_text("test checkpoint payload\n")
    (data / "design.v.gz").write_bytes(b"test ASCII netlist payload\n")
    archive = frozen / batch100.checkpoint_archive.CHECKPOINT_ARCHIVE_NAME
    with tarfile.open(archive, "w") as stream:
        stream.add(
            unpacked / "violating.enc",
            arcname="violating.enc",
            recursive=False,
        )
        stream.add(
            data,
            arcname="violating.enc.dat",
            recursive=True,
        )
    reports = frozen / "injection_reports_fetch" / "reports"
    reports.mkdir(parents=True)
    (reports / "mock.rpt").write_text("injection evidence\n")
    (frozen / "inject_innovus.log").write_text(
        f"B100_INJECTION_COMPLETE {case_id}\n"
    )
    return batch100._write_frozen_checkpoint_ready(
        run_dir,
        case_id,
        job,
        plan_path=plan,
        binding_path=binding,
        config_path=config,
    )


def _materialize_test_replay_assignment(
    run_dir: Path, case_id: str, slots: tuple[int, int] = (0, 1)
) -> Path:
    job = run_dir / "jobs" / case_id
    _materialize_test_frozen_checkpoint(run_dir, case_id)
    path, _ = batch100._write_or_reuse_replay_assignment(
        run_dir, case_id, job, slots
    )
    return path


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.specs = json.loads(catalog.SPECS.read_text())
        cls.cases = cls.specs["cases"]

    def test_catalog_is_exact(self) -> None:
        catalog.validate_specs(self.specs)
        self.assertEqual(
            [case["id"] for case in self.cases],
            [f"B1_CASE_{index:03d}" for index in range(1, 101)],
        )

    def test_distributions_and_semantic_overrides(self) -> None:
        self.assertEqual(
            Counter(case["shape"] for case in self.cases),
            {"single_cone": 55, "multi_endpoint": 25, "setup_drv_guardrail": 20},
        )
        self.assertEqual(
            Counter(case["strategy"] for case in self.cases),
            {"A": 35, "B": 35, "C": 20, "D": 10},
        )
        by_id = {case["id"]: case for case in self.cases}
        self.assertEqual(by_id["B1_CASE_004"]["load_only_sink_count"], 1)
        self.assertEqual(by_id["B1_CASE_009"]["protected_endpoint_count"], 1)
        self.assertEqual(by_id["B1_CASE_055"]["protected_endpoint_count"], 1)

    def test_severity_and_i0_contract(self) -> None:
        ranges = {"E": (40, 160), "M": (161, 400), "H": (401, 640)}
        actual_i0 = set()
        for case in self.cases:
            low, high = ranges[case["difficulty"]]
            self.assertGreaterEqual(case["nominal_severity"]["ps"], low)
            self.assertLessEqual(case["nominal_severity"]["ps"], high)
            if case["injection_profile"] == "I0":
                actual_i0.add(case["id"])
        self.assertEqual(actual_i0, catalog.I0_CASES)


class RelaxedV3RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.specs = json.loads(
            (batch100.ROOT / "case_specs_relaxed_v3.json").read_text()
        )

    def test_runtime_accepts_deterministic_v3_contract(self) -> None:
        batch100._validate_specs(self.specs)

    def test_runtime_rejects_v3_measured_target_writeback(self) -> None:
        changed = copy.deepcopy(self.specs)
        changed["cases"][1]["nominal_severity"]["ps"] = 85
        with self.assertRaises(BatchError):
            batch100._validate_specs(changed)

    def test_v3_uses_relaxed_binding_path(self) -> None:
        with patch.object(batch100, "_bind_relaxed_cases") as binder:
            batch100._bind_cases(
                {"paths": [], "points": [], "ladders": [], "reachability": []},
                {"schema_version": batch100.RELAXED_V3_SCHEMA, "cases": []},
                Path("/unused"),
            )
        binder.assert_called_once()


class RuntimePortabilityGateTests(unittest.TestCase):
    def test_runtime_config_freezes_severity_scalars(self) -> None:
        with tempfile.TemporaryDirectory(prefix="b100-runtime-config-") as temporary:
            config = Path(temporary) / "case_config.tcl"
            batch100._write_runtime_config(
                config,
                "B1_CASE_059",
                {
                    "target_endpoints": ["u/reg_0/D", "u/reg_1/D"],
                    "protected_endpoints": [],
                },
                {
                    "injection_operations": [
                        {"instance": "u/inject", "new_ref": "BUF_X0P5"}
                    ],
                    "repair_operations": [
                        {"instance": "u/repair", "new_ref": "BUF_X2"}
                    ],
                },
                -0.180,
                0.027,
            )
            text = config.read_text()
        self.assertIn("set B100_NOMINAL_NS -0.18\n", text)
        self.assertIn("set B100_TOLERANCE_NS 0.027\n", text)

    def test_runtime_config_rejects_invalid_gate_scalars(self) -> None:
        binding = {"target_endpoints": ["u/reg/D"], "protected_endpoints": []}
        plan = {"injection_operations": [], "repair_operations": []}
        with tempfile.TemporaryDirectory(prefix="b100-runtime-config-") as temporary:
            config = Path(temporary) / "case_config.tcl"
            for nominal, tolerance in (
                (float("nan"), 0.010),
                (0.0, 0.010),
                (-0.050, -0.001),
            ):
                with self.subTest(nominal=nominal, tolerance=tolerance):
                    with self.assertRaises(BatchError):
                        batch100._write_runtime_config(
                            config,
                            "B1_CASE_001",
                            binding,
                            plan,
                            nominal,
                            tolerance,
                        )

    def test_gate_precedes_replay_reports_fix_and_refine(self) -> None:
        text = (TOOLS / "runtime.tcl").read_text()
        replay = text[text.index('    } else {', text.index("proc ::b100::main")) :]
        restore = replay.index("uplevel #0 [list source $checkpoint]")
        gate = replay.index("::b100::checkpoint_portability_gate")
        before_reports = replay.index("::b100::report_stage before")
        fix = replay.index("source [file normalize [::b100::env B100_FIX_TCL]]")
        self.assertLess(restore, gate)
        self.assertLess(gate, before_reports)
        self.assertLess(before_reports, fix)

        gate_body = text[
            text.index("proc ::b100::checkpoint_portability_gate")
            : text.index("proc ::b100::report_stage")
        ]
        self.assertIn("if {$negatives ne $targets}", gate_body)
        self.assertIn("$wns < $lower - 1.0e-12", gate_body)
        self.assertNotIn("refinePlace", gate_body)
        self.assertNotIn("verifyConnectivity", gate_body)
        self.assertNotIn("verify_drc", gate_body)
        self.assertNotIn("checkPlace", gate_body)

    def test_replay_requires_one_unambiguous_portability_pass_marker(self) -> None:
        case_id = "B1_CASE_059"
        success = (
            f"B100_CHECKPOINT_PORTABILITY_PASS {case_id} wns_ns=-0.18825\n"
        )
        self.assertTrue(batch100._replay_portability_marker_ok(success, case_id))
        self.assertFalse(batch100._replay_portability_marker_ok("", case_id))
        self.assertFalse(
            batch100._replay_portability_marker_ok(success + success, case_id)
        )
        self.assertFalse(
            batch100._replay_portability_marker_ok(
                success
                + f"B100_CHECKPOINT_PORTABILITY_FAIL {case_id} "
                "reason=severity\n",
                case_id,
            )
        )

    def test_inject_and_replay_force_full_timing_before_evidence(self) -> None:
        text = (TOOLS / "runtime.tcl").read_text()
        main = text[
            text.index("proc ::b100::main {}")
            : text.index(
                "\n::b100::main\n",
                text.index("proc ::b100::main {}"),
            )
        ]
        inject = main[
            main.index('if {$mode eq "INJECT"}')
            : main.index("    } else {")
        ]
        ordered = (
            "::b100::resize $::B100_INJECTION_OPERATIONS",
            "refinePlace -eco true",
            "::b100::full_postroute_timing injection_freeze",
            "::b100::report_stage before",
            "::b100::save_portable_checkpoint",
        )
        positions = [inject.index(value) for value in ordered]
        self.assertEqual(positions, sorted(positions))

        resize_stage = text[
            text.index("proc ::b100::report_resize_stage")
            : text.index("proc ::b100::resize")
        ]
        self.assertLess(
            resize_stage.index(
                "::b100::full_postroute_timing after_resize"
            ),
            resize_stage.index("report_timing -late"),
        )
        replay = main[main.index("    } else {") :]
        self.assertLess(
            replay.index("source [file normalize [::b100::env B100_FIX_TCL]]"),
            replay.index(
                "::b100::full_postroute_timing after_legalize"
            ),
        )
        self.assertLess(
            replay.index(
                "::b100::full_postroute_timing after_legalize"
            ),
            replay.index("::b100::report_stage after_legalize"),
        )
        full_helper = text[
            text.index("proc ::b100::full_postroute_timing")
            : text.index("proc ::b100::assert_portable_checkpoint_shape")
        ]
        self.assertEqual(full_helper.count("timeDesign -postRoute"), 1)

    def test_portable_save_has_one_ascii_rc_path_and_restores_flag(self) -> None:
        text = (TOOLS / "runtime.tcl").read_text()
        helper = text[
            text.index("proc ::b100::save_portable_checkpoint")
            : text.index("proc ::b100::load_config")
        ]
        ordered = (
            "set ::enc_save_binary 0",
            "saveDesign $checkpoint -rc",
            "set ::enc_save_binary $original",
            "::b100::assert_portable_checkpoint_shape $checkpoint",
        )
        positions = [helper.index(value) for value in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(text.count("saveDesign $checkpoint -rc"), 1)
        lowered = text.lower()
        for forbidden in (
            "savenetlist",
            "write_netlist",
            "-verilog",
            "read_db",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, lowered)

    def test_calibration_forces_full_timing_before_every_sample(self) -> None:
        text = (TOOLS / "calibrate.tcl").read_text()
        main = text[
            text.index("proc ::b100_cal::main {}")
            : text.index(
                "\n::b100_cal::main\n",
                text.index("proc ::b100_cal::main {}"),
            )
        ]
        ordered_pairs = (
            (
                "::b100_cal::full_postroute_timing injection",
                "set before [::b100_cal::path_slacks]",
            ),
            (
                "::b100_cal::full_postroute_timing sensitivity",
                "set target_after [::b100_cal::target_values "
                "[::b100_cal::path_slacks]]",
            ),
            (
                "::b100_cal::full_postroute_timing repair_before_refine",
                "set before_refine [::b100_cal::path_slacks]",
            ),
            (
                "::b100_cal::full_postroute_timing repair_after_refine",
                "set after_refine [::b100_cal::path_slacks]",
            ),
        )
        for timing, sample in ordered_pairs:
            with self.subTest(sample=sample):
                self.assertLess(main.index(timing), main.index(sample))
        helper = text[
            text.index("proc ::b100_cal::full_postroute_timing")
            : text.index("proc ::b100_cal::materialize")
        ]
        self.assertEqual(helper.count("timeDesign -postRoute"), 1)


class RepairAlternativeTests(unittest.TestCase):
    @staticmethod
    def _cell(instance: str, strength: float, *endpoints: str) -> dict:
        return {
            "instance": instance,
            "up_variants": [{"ref": f"{instance}_UP"}],
            "up_estimates_by_endpoint_ns": {
                endpoint: [strength] for endpoint in endpoints
            },
            "delay_by_endpoint_ns": {
                endpoint: strength / 2.0 for endpoint in endpoints
            },
        }

    @staticmethod
    def _fingerprint(repairs: list[dict]) -> str:
        return batch100._operation_fingerprint(
            [
                {
                    "kind": "repair",
                    "instance": cell["instance"],
                    "old_ref": f"{cell['instance']}_BASE",
                    "new_ref": cell["up_variants"][-1]["ref"],
                }
                for cell in repairs
            ]
        )

    def test_known_cross_case_collision_pairs_have_claim_fallbacks(self) -> None:
        endpoints = {"u/target_a/D", "u/target_b/D"}
        cells = [
            self._cell("u/inject_a", 0.10, *endpoints),
            self._cell("u/inject_b", 0.09, *endpoints),
            self._cell("u/repair_a", 0.30, *endpoints),
            self._cell("u/repair_b", 0.28, *endpoints),
            self._cell("u/repair_c", 0.26, *endpoints),
            self._cell("u/repair_d", 0.24, *endpoints),
            self._cell("u/repair_e", 0.22, *endpoints),
        ]
        alternatives = batch100._non_i0_repair_alternatives(
            cells,
            {"u/inject_a", "u/inject_b"},
            endpoints,
            2,
            limit=6,
        )
        fingerprints = [self._fingerprint(repairs) for repairs in alternatives]
        self.assertGreaterEqual(len(fingerprints), 4)
        self.assertEqual(len(fingerprints), len(set(fingerprints)))
        for repairs in alternatives:
            repaired = {cell["instance"] for cell in repairs}
            self.assertEqual(len(repaired), 2)
            self.assertTrue(
                endpoints
                <= set().union(
                    *(
                        set(cell["up_estimates_by_endpoint_ns"])
                        for cell in repairs
                    )
                )
            )
            self.assertTrue({"u/inject_a", "u/inject_b"} - repaired)
            self.assertTrue(repaired - {"u/inject_a", "u/inject_b"})

        claimed: set[str] = set()
        for case_id in (
            "B1_CASE_006",
            "B1_CASE_082",
            "B1_CASE_098",
            "B1_CASE_100",
        ):
            available = next(
                fingerprint
                for fingerprint in fingerprints
                if fingerprint not in claimed
            )
            claimed.add(available)
            with self.subTest(case_id=case_id):
                self.assertIn(available, fingerprints)

    @staticmethod
    def _candidate_repair_fingerprint(candidate: dict) -> str:
        injected_refs = {
            operation["instance"]: operation["candidate_refs_nearest_first"][0]
            for operation in candidate["injection_operations"]
        }
        return batch100._operation_fingerprint(
            [
                {
                    "kind": "repair",
                    "instance": operation["instance"],
                    "old_ref": injected_refs.get(
                        operation["instance"], operation["checkpoint_ref"]
                    ),
                    "new_ref": operation["new_ref"],
                }
                for operation in candidate["repair_operations"]
            ]
        )

    def test_offline_full_probe_has_unique_repair_claim_assignment(self) -> None:
        source = os.environ.get("B100_OFFLINE_PROBE_RUN")
        if not source:
            self.skipTest("set B100_OFFLINE_PROBE_RUN for the full-probe binder audit")
        source_run = Path(source)
        manifest = json.loads((source_run / "run_manifest.json").read_text())
        probe = batch100._verify_probe(source_run / "probe", manifest["baseline"])
        specs = json.loads(catalog.SPECS.read_text())
        with tempfile.TemporaryDirectory(prefix="b100-offline-bind-") as temporary:
            run_dir = Path(temporary)
            batch100._bind_cases(probe, specs, run_dir)
            options: dict[str, set[str]] = {}
            for path in sorted((run_dir / "bindings").glob("*.json")):
                binding = json.loads(path.read_text())
                options[binding["case_id"]] = {
                    self._candidate_repair_fingerprint(candidate)
                    for candidate in binding["calibration_candidates"]
                }
        self.assertEqual(len(options), 100)

        def matching_size(selected: list[str]) -> int:
            owner: dict[str, str] = {}

            def augment(case_id: str, seen: set[str]) -> bool:
                for fingerprint in sorted(options[case_id]):
                    if fingerprint in seen:
                        continue
                    seen.add(fingerprint)
                    previous = owner.get(fingerprint)
                    if previous is None or augment(previous, seen):
                        owner[fingerprint] = case_id
                        return True
                return False

            return sum(
                augment(case_id, set())
                for case_id in sorted(
                    selected, key=lambda value: (len(options[value]), value)
                )
            )

        for pair in (
            ["B1_CASE_006", "B1_CASE_082"],
            ["B1_CASE_098", "B1_CASE_100"],
        ):
            with self.subTest(pair=pair):
                self.assertEqual(matching_size(pair), 2)
        self.assertEqual(matching_size(sorted(options)), 100)


class AdaptiveRefinementTests(unittest.TestCase):
    TARGETS = (
        "pp_nan_mts_d1_reg_10_/D",
        "pp_nan_mts_d1_reg_3_/D",
        "pp_nan_mts_d1_reg_5_/D",
        "pp_nan_mts_d1_reg_6_/D",
    )
    REFS = (
        "INV_X1M_A9TR40",
        "INV_X0P8M_A9TR40",
        "INV_X0P7M_A9TR40",
        "INV_X0P6M_A9TR40",
        "INV_X0P5M_A9TR40",
    )

    @classmethod
    def _operation(
        cls,
        index: int,
        endpoint: str,
        values: list[float],
        *,
        selected_level: int = 4,
    ) -> dict:
        reference = cls.REFS[selected_level]
        return {
            "instance": f"u_nan/refine_{index:02d}",
            "baseline_ref": "INV_X2M_A9TR40",
            "new_ref": reference,
            "candidate_refs_nearest_first": [reference],
            "candidate_estimated_slowdown_ns": [
                values[selected_level]
            ],
            "candidate_estimated_slowdown_by_endpoint_ns": {
                endpoint: [values[selected_level]]
            },
            "legal_down_refs_nearest_first": list(cls.REFS),
            "legal_down_estimated_slowdown_ns": list(values),
            "legal_down_estimated_slowdown_by_endpoint_ns": {
                endpoint: list(values)
            },
            "selected_legal_down_level": selected_level,
            "reachable_endpoints": [endpoint],
            "point_index": 100 - index,
        }

    @classmethod
    def _case059_operations(cls) -> list[dict]:
        target_10, target_3, target_5, target_6 = cls.TARGETS
        weak = [0.04, 0.08, 0.12, 0.18, 0.25]
        medium = [0.04, 0.09, 0.14, 0.21, 0.30]
        operations = [
            cls._operation(0, target_10, medium),
            cls._operation(
                1, target_10, [0.08, 0.1794, 0.2357, 0.298, 0.40]
            ),
            cls._operation(2, target_10, weak),
            cls._operation(
                3, target_3, [0.05, 0.0931, 0.1628, 0.25, 0.40]
            ),
            cls._operation(4, target_3, medium),
            cls._operation(5, target_3, weak),
            cls._operation(6, target_5, medium),
            cls._operation(7, target_5, weak),
            cls._operation(8, target_5, weak),
            cls._operation(9, target_6, medium),
            cls._operation(
                10,
                target_6,
                [0.1097, 0.179801, 0.26, 0.33, 0.40],
            ),
            cls._operation(11, target_6, weak),
        ]
        # A stronger shared candidate must not outrank a private branch knob.
        shared = [0.08, 0.16, 0.28, 0.42, 0.60]
        operations[0]["candidate_estimated_slowdown_ns"] = [shared[-1]]
        operations[0][
            "candidate_estimated_slowdown_by_endpoint_ns"
        ] = {
            target_10: [shared[-1]],
            target_3: [shared[-1]],
        }
        operations[0]["legal_down_estimated_slowdown_ns"] = list(shared)
        operations[0][
            "legal_down_estimated_slowdown_by_endpoint_ns"
        ] = {
            target_10: list(shared),
            target_3: list(shared),
        }
        operations[0]["reachable_endpoints"] = [target_10, target_3]
        return operations

    @classmethod
    def _seed_slacks(cls) -> dict[str, float]:
        target_10, target_3, target_5, target_6 = cls.TARGETS
        return {
            target_10: -0.379920,
            target_3: -0.407500,
            target_5: -0.103518,
            target_6: -0.408449,
        }

    def test_case059_private_knobs_generate_measured_v4_v9_neighbors(
        self,
    ) -> None:
        operations = self._case059_operations()
        measured = self._seed_slacks()
        knobs = batch100._select_refinement_knobs(
            operations,
            measured,
            self.TARGETS,
            -0.180,
            0.027,
        )
        self.assertEqual(
            {index for index, _ in knobs},
            {1, 3, 10},
        )
        self.assertTrue(
            all(
                set(
                    operations[index][
                        "legal_down_estimated_slowdown_by_endpoint_ns"
                    ]
                )
                == {endpoint}
                for index, endpoint in knobs
            )
        )

        vectors = batch100._refinement_level_vectors(
            operations,
            measured,
            self.TARGETS,
            -0.180,
            0.027,
            limit=32,
        )
        seed = [
            operation["selected_legal_down_level"]
            for operation in operations
        ]
        variant_4 = list(seed)
        variant_4[1] = 1
        variant_4[3] = 2
        variant_4[10] = 1
        variant_9 = list(seed)
        variant_9[1] = 1
        variant_9[3] = 1
        variant_9[10] = 1
        self.assertIn(tuple(variant_4), vectors)
        self.assertIn(tuple(variant_9), vectors)
        self.assertEqual(
            vectors,
            batch100._refinement_level_vectors(
                operations,
                dict(reversed(list(measured.items()))),
                self.TARGETS,
                -0.180,
                0.027,
                limit=32,
            ),
        )

        materialized_4 = batch100._materialize_refinement_candidate(
            {
                "injection_operations": operations,
                "repair_operations": [],
            },
            variant_4,
        )
        refs_4 = [
            operation["candidate_refs_nearest_first"][0]
            for operation in materialized_4["injection_operations"]
        ]
        self.assertEqual(refs_4[1], "INV_X0P8M_A9TR40")
        self.assertEqual(refs_4[3], "INV_X0P7M_A9TR40")
        self.assertEqual(refs_4[10], "INV_X0P8M_A9TR40")

    def test_refinement_requires_exact_set_and_severity_miss(self) -> None:
        operations = self._case059_operations()
        in_window = self._seed_slacks()
        in_window[self.TARGETS[3]] = -0.188250
        in_window[self.TARGETS[1]] = -0.170300
        in_window[self.TARGETS[0]] = -0.159320
        self.assertEqual(
            batch100._refinement_level_vectors(
                operations,
                in_window,
                self.TARGETS,
                -0.180,
                0.027,
            ),
            [],
        )
        extra_negative = self._seed_slacks()
        extra_negative["u_nan/unplanned_reg/D"] = -0.001
        target_positive = self._seed_slacks()
        target_positive[self.TARGETS[0]] = 0.001
        for measured in (extra_negative, target_positive):
            with self.subTest(measured=measured):
                self.assertEqual(
                    batch100._refinement_level_vectors(
                        operations,
                        measured,
                        self.TARGETS,
                        -0.180,
                        0.027,
                    ),
                    [],
                )

    def test_too_shallow_seed_moves_deeper_and_i0_stays_direct_inverse(
        self,
    ) -> None:
        target = "u_nan/target_reg/D"
        operation = self._operation(
            0,
            target,
            [0.05, 0.10, 0.18, 0.28, 0.40],
            selected_level=1,
        )
        vectors = batch100._refinement_level_vectors(
            [operation],
            {target: -0.100},
            [target],
            -0.180,
            0.027,
        )
        self.assertTrue(vectors)
        self.assertTrue(all(vector[0] > 1 for vector in vectors))

        candidate = {
            "injection_operations": [operation],
            "repair_operations": [
                {
                    "instance": operation["instance"],
                    "checkpoint_ref": operation["new_ref"],
                    "new_ref": operation["baseline_ref"],
                }
            ],
        }
        materialized = batch100._materialize_refinement_candidate(
            candidate, vectors[0]
        )
        injection = materialized["injection_operations"][0]
        repair = materialized["repair_operations"][0]
        self.assertEqual(repair["checkpoint_ref"], injection["new_ref"])
        plan = batch100._read_frozen_plan_from_rows(
            [
                {
                    "kind": "injection",
                    "instance": injection["instance"],
                    "old_ref": injection["baseline_ref"],
                    "new_ref": injection["new_ref"],
                },
                {
                    "kind": "repair",
                    "instance": repair["instance"],
                    "old_ref": repair["checkpoint_ref"],
                    "new_ref": repair["new_ref"],
                },
            ],
            {
                "id": "B1_CASE_TEST",
                "expected_modification_count": 1,
                "injection_profile": "I0",
            },
        )
        self.assertEqual(
            plan["repair_operations"][0]["new_ref"],
            injection["baseline_ref"],
        )

    def test_calibrator_retests_refinement_in_a_fresh_process(self) -> None:
        case_id = "B1_CASE_001"
        target = "u_nan/target_reg/D"
        operation = self._operation(
            0,
            target,
            [0.05, 0.12, 0.20, 0.30, 0.40],
        )
        candidate = {
            "candidate_index": 0,
            "injection_operations": [operation],
            "repair_operations": [
                {
                    "instance": operation["instance"],
                    "checkpoint_ref": operation["new_ref"],
                    "new_ref": operation["baseline_ref"],
                    "candidate_refs_nearest_first": [
                        operation["baseline_ref"]
                    ],
                }
            ],
        }
        binding = {
            "target_endpoints": [target],
            "target_baseline_slacks_ns": {target: 0.10},
            "protected_endpoints": [],
            "injection_operations": candidate["injection_operations"],
            "repair_operations": candidate["repair_operations"],
            "calibration_candidates": [candidate],
        }
        spec = {
            "id": case_id,
            "expected_modification_count": 1,
            "injection_profile": "I0",
            "nominal_severity": {
                "target_wns_ns": -0.180,
                "tolerance_ps": 27,
            },
        }
        store = _FakeStore({case_id: "BOUND"})
        innovus_commands: list[str] = []

        def ssh(slot, command, **kwargs):
            del slot, kwargs
            if command.startswith("test ! -e "):
                return SimpleNamespace(returncode=0, stdout="")
            if "B100_CAL_MODE=INJECTION" in command:
                marker = "B100_CALIBRATION_TRIAL_COMPLETE"
            elif "B100_CAL_MODE=REPAIR" in command:
                marker = "B100_CALIBRATION_REPAIR_COMPLETE"
            elif "B100_CAL_MODE=SENSITIVITY" in command:
                marker = "B100_CALIBRATION_SENSITIVITY_COMPLETE"
            elif "B100_MODE=INJECT" in command:
                return SimpleNamespace(
                    returncode=0,
                    stdout=(
                        f"B100_INJECTION_COMPLETE {case_id}\n"
                        "*** Message Summary: 0 warning(s), 0 error(s)\n"
                    ),
                )
            else:
                raise AssertionError(f"unexpected remote command: {command}")
            innovus_commands.append(command)
            return SimpleNamespace(
                returncode=0,
                stdout=f"{marker} {case_id}\n",
            )

        def scp_from(slot, source, destination, *, recursive=False):
            del slot, recursive
            if source.endswith("/freeze/reports"):
                reports = destination / "reports"
                reports.mkdir()
                (reports / "mock.rpt").write_text("freeze evidence\n")
                return
            if source.endswith(
                batch100.checkpoint_archive.CHECKPOINT_ARCHIVE_NAME
            ):
                destination.write_bytes(b"archive")
                return
            leaf = source.rsplit("/", 1)[-1]
            fetched = destination / leaf
            fetched.mkdir()
            if leaf.endswith("_injection"):
                injection_count = sum(
                    "B100_CAL_MODE=INJECTION" in command
                    for command in innovus_commands
                )
                slack = -0.300 if injection_count == 1 else -0.180
                (fetched / "injection_slacks.tsv").write_text(
                    f"endpoint\tslack_ns\n{target}\t{slack}\n"
                )
            elif leaf.endswith("_repair"):
                for name in (
                    "repair_before_refine.tsv",
                    "repair_after_refine.tsv",
                ):
                    (fetched / name).write_text(
                        f"endpoint\tslack_ns\n{target}\t0.010\n"
                    )
            elif leaf.endswith("_sensitivity"):
                (fetched / "sensitivity.tsv").write_text(
                    "instance\tendpoint\tdelta_ns\n"
                    f"{operation['instance']}\t{target}\t0.010\n"
                )

        with tempfile.TemporaryDirectory(
            prefix="b100-refinement-calibrator-"
        ) as temporary:
            run_dir = Path(temporary)
            (run_dir / "bindings").mkdir()
            (run_dir / "bindings" / f"{case_id}.json").write_text(
                json.dumps(binding)
            )
            with (
                patch.object(batch100, "_ssh", side_effect=ssh),
                patch.object(batch100, "_scp_to"),
                patch.object(batch100, "_scp_from", side_effect=scp_from),
                patch.object(
                    batch100,
                    "_claim_operation_fingerprints",
                    return_value=("a" * 64, "b" * 64),
                ),
                patch.object(
                    batch100,
                    "_pack_guest_checkpoint",
                    return_value=hashlib.sha256(b"archive").hexdigest(),
                ),
                patch.object(
                    batch100.checkpoint_archive,
                    "safe_extract_checkpoint_archive",
                    side_effect=lambda archive, destination, **kwargs: (
                        destination.mkdir()
                    ),
                ),
                patch.object(
                    batch100,
                    "_write_frozen_checkpoint_ready",
                    return_value=(
                        Path("frozen_checkpoint.ready.json"),
                        {},
                    ),
                ),
                patch.object(
                    batch100,
                    "_finish_ready_frozen_checkpoint",
                    side_effect=lambda run, actual_store, actual_case: (
                        actual_store.transition(
                            actual_case,
                            "FROZEN",
                            "test freeze",
                            slot=None,
                        )
                        or f"{actual_case}: FROZEN slot0"
                    ),
                ),
                patch.object(batch100, "_remote_remove_case_leaf"),
            ):
                result = batch100._calibrate_one(
                    run_dir, store, spec, slot=0
                )

            self.assertIn("FROZEN slot0", result)
            self.assertEqual(store.row(case_id)["state"], "FROZEN")
            self.assertEqual(len(innovus_commands), 4)
            self.assertEqual(
                sum(
                    "B100_CAL_MODE=INJECTION" in command
                    for command in innovus_commands
                ),
                2,
            )
            self.assertTrue(
                all(
                    f"B100_BASELINE_DIR={batch100.GUEST_BASELINE}" in command
                    for command in innovus_commands
                )
            )
            plan = json.loads(
                (
                    run_dir
                    / "jobs"
                    / case_id
                    / "calibration_plan.json"
                ).read_text()
            )
            self.assertNotEqual(
                plan["injection_operations"][0]["new_ref"],
                operation["new_ref"],
            )
            self.assertEqual(
                plan["repair_operations"][0]["old_ref"],
                plan["injection_operations"][0]["new_ref"],
            )
            self.assertEqual(
                plan["repair_operations"][0]["new_ref"],
                operation["baseline_ref"],
            )


class EvidenceParserTests(unittest.TestCase):
    @staticmethod
    def _connectivity_report(directory: Path, name: str, summary: str) -> Path:
        path = directory / name
        path.write_text(f"Begin Summary\n{summary}End Summary\n")
        return path

    def test_connectivity_count_innovus_2110_nonzero_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = self._connectivity_report(
                Path(temporary),
                "connectivity.rpt",
                "16 Problem(s) (IMPVFC-96): category A\n"
                "15 Problem(s) (IMPVFC-97): category B\n"
                "16 Problem(s) (IMPVFC-98): category C\n"
                "47 total info(s) created.\n",
            )
            self.assertEqual(evidence.connectivity_count(report), 47)

    def test_connectivity_count_rejects_mismatched_total(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = self._connectivity_report(
                Path(temporary),
                "connectivity.rpt",
                "16 Problem(s) (IMPVFC-96): category A\n"
                "15 Problem(s) (IMPVFC-97): category B\n"
                "16 Problem(s) (IMPVFC-98): category C\n"
                "48 total info(s) created.\n",
            )
            with self.assertRaises(BatchError):
                evidence.connectivity_count(report)

    def test_connectivity_count_no_problems_is_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = self._connectivity_report(
                Path(temporary),
                "connectivity.rpt",
                "Found no problems or warnings.\n",
            )
            self.assertEqual(evidence.connectivity_count(report), 0)


class TclAndFilesystemTests(unittest.TestCase):
    def test_tcl_allowlist(self) -> None:
        good = (
            "setEcoMode -batchMode true\n"
            "ecoChangeCell -inst {u/a} -cell {NAND2_X2M_A9TR40}\n"
            "setEcoMode -batchMode false\n"
            "refinePlace -eco true\n"
        )
        self.assertEqual(parse_fix_tcl(good, 1)[0]["instance"], "u/a")
        for command in (
            "ecoRoute -target",
            "ecoAddRepeater -term {u/a/Y} -cell {BUF_X1M_A9TR40}",
            "create_clock -period 9 clk",
            "optDesign -postRoute -setup",
        ):
            with self.subTest(command=command), self.assertRaises(BatchError):
                parse_fix_tcl(good.replace("refinePlace -eco true", command), 1)

    def test_tree_hash_and_safe_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tree = root / "tree"
            tree.mkdir()
            (tree / "a").write_bytes(b"a")
            first = tree_sha256(tree)
            (tree / "a").write_bytes(b"b")
            self.assertNotEqual(first, tree_sha256(tree))
            tasks = root / "tasks"
            leaf = tasks / "B1_CASE_001"
            leaf.mkdir(parents=True)
            (leaf / "x").write_text("x")
            safe_remove_task_leaf(tasks, leaf)
            self.assertFalse(leaf.exists())
            outside = root / "outside"
            outside.mkdir()
            with self.assertRaises(BatchError):
                safe_remove_task_leaf(tasks, outside)


class StateTests(unittest.TestCase):
    def test_wal_transitions_sidecars_and_hash_guard(self) -> None:
        hashes = {"catalog": "a" * 64, "runtime": "b" * 64}
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            store = StateStore(run)
            store.initialize(["B1_CASE_001"], hashes, {"entry_sha256": "c" * 64})
            self.assertEqual(store.row("B1_CASE_001")["state"], "PLANNED")
            store.transition("B1_CASE_001", "PROBE_ELIGIBLE", "probe")
            store.transition(
                "B1_CASE_001", "BOUND", "bind", binding_sha256="d" * 64
            )
            sidecar = json.loads((run / "state/B1_CASE_001.json").read_text())
            self.assertEqual(sidecar["case"]["state"], "BOUND")
            store.verify_inputs(hashes)
            with self.assertRaises(BatchError):
                store.verify_inputs({**hashes, "runtime": "e" * 64})
            with self.assertRaises(BatchError):
                store.transition("B1_CASE_001", "VALIDATED", "skip")

    def test_artifact_mutations_immediately_refresh_atomic_sidecar(self) -> None:
        hashes = {"catalog": "a" * 64}
        case_id = "B1_CASE_001"
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            store = StateStore(run)
            store.initialize(
                [case_id], hashes, {"entry_sha256": "b" * 64}
            )
            store.register_artifact(
                case_id,
                "frozen_checkpoint_archive",
                "jobs/B1_CASE_001/frozen_checkpoint/archive.tar",
                "c" * 64,
                123,
            )
            sidecar = json.loads(
                (run / "state" / f"{case_id}.json").read_text()
            )
            self.assertEqual(
                [row["logical_name"] for row in sidecar["artifacts"]],
                ["frozen_checkpoint_archive"],
            )
            self.assertEqual(
                store.remove_artifacts(
                    case_id,
                    [
                        "frozen_checkpoint_archive",
                        "frozen_checkpoint_archive",
                    ],
                ),
                1,
            )
            sidecar = json.loads(
                (run / "state" / f"{case_id}.json").read_text()
            )
            self.assertEqual(sidecar["artifacts"], [])
            self.assertEqual(
                store.remove_artifacts(
                    case_id, ["frozen_checkpoint_archive"]
                ),
                0,
            )
            with self.assertRaisesRegex(
                BatchError, "non-empty strings"
            ):
                store.remove_artifacts(case_id, ["valid", 7])  # type: ignore[list-item]


def _stage_before(target: str) -> dict:
    return {
        "setup_wns_ns": -0.060,
        "setup_tns_ns": -0.060,
        "hold_wns_ns": 0.050,
        "hold_tns_ns": 0.0,
        "negative_endpoints": [target],
        "target_slacks_ns": {target: -0.060},
        "target_paths": {
            target: {
                "cell_delay_ns": 7.0,
                "net_delay_ns": 1.0,
                "slack_ns": -0.060,
            }
        },
        "drv": {"max_transition": 1, "max_capacitance": 2, "max_fanout": 3},
        "drc_count": 10,
        "connectivity_violations": 0,
        "constraint_sha256": "1" * 64,
        "pin_net_connectivity_sha256": "2" * 64,
        "instance_set_sha256": "3" * 64,
        "topology_sha256": "4" * 64,
    }


def _stage_after(target: str) -> dict:
    return {
        "setup_wns_ns": 0.010,
        "setup_tns_ns": 0.0,
        "hold_wns_ns": 0.049,
        "hold_tns_ns": 0.0,
        "negative_endpoints": [],
        "target_slacks_ns": {target: 0.010},
        "drv": {"max_transition": 1, "max_capacitance": 2, "max_fanout": 3},
        "drc_count": 10,
        "connectivity_violations": 0,
        "constraint_sha256": "1" * 64,
        "pin_net_connectivity_sha256": "2" * 64,
        "instance_set_sha256": "3" * 64,
        "topology_sha256": "4" * 64,
        "routing_mutations": 0,
    }


class _FakeStore:
    """Small thread-safe state store used to exercise the real scheduler."""

    def __init__(
        self,
        states: dict[str, str],
        row_fields: dict[str, dict] | None = None,
    ) -> None:
        self._states = dict(states)
        self._row_fields = copy.deepcopy(row_fields or {})
        self._lock = threading.Lock()
        self.transitions: list[tuple[str, str]] = []
        self.artifacts: dict[str, dict[str, dict]] = {}
        self.sidecar_refreshes: list[str] = []

    def row(self, case_id: str) -> dict:
        with self._lock:
            return {
                "case_id": case_id,
                "state": self._states[case_id],
                "attempt": 0,
                **self._row_fields.get(case_id, {}),
            }

    def rows(self) -> list[dict]:
        with self._lock:
            return [
                {"case_id": case_id, "state": state}
                for case_id, state in self._states.items()
            ]

    def transition(self, case_id: str, state: str, reason: str, **updates) -> None:
        del reason
        with self._lock:
            self._states[case_id] = state
            self._row_fields.setdefault(case_id, {}).update(updates)
            self.transitions.append((case_id, state))

    def register_artifact(
        self,
        case_id: str,
        logical_name: str,
        relative_path: str,
        sha256: str,
        size: int,
    ) -> None:
        with self._lock:
            self.artifacts.setdefault(case_id, {})[logical_name] = {
                "relative_path": relative_path,
                "sha256": sha256,
                "bytes": size,
            }

    def remove_artifacts(
        self, case_id: str, logical_names: list[str] | tuple[str, ...]
    ) -> int:
        with self._lock:
            case_artifacts = self.artifacts.setdefault(case_id, {})
            removed = 0
            for name in set(logical_names):
                if case_artifacts.pop(name, None) is not None:
                    removed += 1
            return removed

    def refresh_sidecar(self, case_id: str) -> None:
        with self._lock:
            self.sidecar_refreshes.append(case_id)


class _SlotTracker:
    """Records scheduler allocations and detects concurrent slot reuse."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active: set[int] = set()
        self.collisions: list[tuple[str, tuple[int, ...]]] = []
        self.allocations: list[tuple[str, str, tuple[int, ...]]] = []

    def acquire(self, stage: str, case_id: str, slots: tuple[int, ...]) -> None:
        with self._lock:
            if self.active.intersection(slots):
                self.collisions.append((case_id, slots))
            self.active.update(slots)
            self.allocations.append((stage, case_id, slots))

    def release(self, slots: tuple[int, ...]) -> None:
        with self._lock:
            self.active.difference_update(slots)


def _scheduler_specs(case_ids: list[str]) -> dict:
    return {
        "canary_case_ids": case_ids,
        "cases": [{"id": case_id} for case_id in case_ids],
    }


class RemoteHostCapacityTests(unittest.TestCase):
    @staticmethod
    def _df_result(available_kib: int, *, returncode: int = 0):
        return SimpleNamespace(
            returncode=returncode,
            stdout=(
                "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                f"/dev/mapper/data 3844548592 3695345448 {available_kib} "
                "96% /data0\n"
            ),
        )

    def test_current_and_boundary_capacity_are_admitted(self) -> None:
        current_available_kib = 149_203_144
        with patch.object(
            batch100.subprocess,
            "run",
            return_value=self._df_result(current_available_kib),
        ):
            self.assertEqual(
                batch100._require_remote_host_capacity(),
                current_available_kib,
            )
        with patch.object(
            batch100.subprocess,
            "run",
            return_value=self._df_result(
                batch100.REMOTE_HOST_MIN_AVAILABLE_KIB
            ),
        ):
            self.assertEqual(
                batch100._require_remote_host_capacity(),
                batch100.REMOTE_HOST_MIN_AVAILABLE_KIB,
            )

    def test_below_threshold_refuses_dispatch_before_guest_checks(self) -> None:
        with (
            patch.object(
                batch100,
                "_remote_host_available_kib",
                return_value=batch100.REMOTE_HOST_MIN_AVAILABLE_KIB - 1,
            ),
            patch.object(batch100, "_ssh") as guest_ssh,
        ):
            with self.assertRaisesRegex(BatchError, "requires at least 128 GiB"):
                batch100._verify_execution_slots({"entry_sha256": "a" * 64})
        guest_ssh.assert_not_called()

    def test_malformed_or_failed_df_evidence_is_rejected(self) -> None:
        for result in (
            SimpleNamespace(returncode=0, stdout="not df output\n"),
            SimpleNamespace(returncode=255, stdout="ssh unavailable\n"),
        ):
            with self.subTest(returncode=result.returncode), patch.object(
                batch100.subprocess, "run", return_value=result
            ), self.assertRaises(BatchError):
                batch100._remote_host_available_kib()


class DynamicSchedulerTests(unittest.TestCase):
    def test_execution_slot_preflight_checks_all_ten_slots_concurrently(
        self,
    ) -> None:
        baseline_hash = "a" * 64
        all_slots_entered = threading.Barrier(10)
        checked_slots: list[int] = []
        checked_lock = threading.Lock()

        def ssh(slot, command, *, timeout=60, capture=True):
            del command, timeout, capture
            try:
                all_slots_entered.wait(timeout=2)
            except threading.BrokenBarrierError as exc:
                raise AssertionError(
                    "slot preflight did not execute all ten checks concurrently"
                ) from exc
            with checked_lock:
                checked_slots.append(slot)
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    f"{baseline_hash}  /baseline/base.enc\n"
                    "/dev/vda 100000 20000 80000 20% /work\n"
                ),
            )

        with (
            patch.object(
                batch100,
                "_remote_host_available_kib",
                return_value=batch100.REMOTE_HOST_MIN_AVAILABLE_KIB,
            ),
            patch.object(batch100, "_ssh", side_effect=ssh),
        ):
            available = batch100._verify_execution_slots(
                {"entry_sha256": baseline_hash}
            )

        self.assertEqual(available, list(range(10)))
        self.assertEqual(sorted(checked_slots), list(range(10)))

    def test_execution_slot_preflight_isolates_one_slot_exception(self) -> None:
        baseline_hash = "a" * 64

        def ssh(slot, command, **kwargs):
            del command, kwargs
            if slot == 4:
                raise TimeoutError("deliberate slot timeout")
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    f"{baseline_hash}  /baseline/base.enc\n"
                    "/dev/vda 100000 20000 80000 20% /work\n"
                ),
            )

        with (
            patch.object(
                batch100,
                "_remote_host_available_kib",
                return_value=batch100.REMOTE_HOST_MIN_AVAILABLE_KIB,
            ),
            patch.object(batch100, "_ssh", side_effect=ssh),
            redirect_stderr(io.StringIO()) as errors,
        ):
            available = batch100._verify_execution_slots(
                {"entry_sha256": baseline_hash}
            )
        self.assertEqual(available, [0, 1, 2, 3, 5, 6, 7, 8, 9])
        self.assertIn("slot4: preflight exception", errors.getvalue())

    def _scheduler_patches(
        self,
        store: _FakeStore,
        specs: dict,
        slots: list[int],
        calibrate,
        execute,
    ):
        original_read_json = batch100.read_json

        def read_json(path, label=None):
            if Path(path) == batch100.SPECS_PATH:
                return specs
            return original_read_json(path, label)

        return patch.multiple(
            batch100,
            _verified_store=lambda run_dir: (store, {"baseline": {}}),
            _verify_execution_slots=lambda baseline: slots,
            read_json=read_json,
            _functional_pairs=lambda run_dir: {},
            _calibrate_one=calibrate,
            _execute_one=execute,
        )

    def test_failure_isolated_and_replay_starts_before_last_calibration_finishes(
        self,
    ) -> None:
        case_a, case_b, case_c = (
            "B1_CASE_001",
            "B1_CASE_002",
            "B1_CASE_003",
        )
        specs = _scheduler_specs([case_a, case_b, case_c])
        store = _FakeStore({case_id: "BOUND" for case_id in specs["canary_case_ids"]})
        tracker = _SlotTracker()
        all_calibrations_started = threading.Barrier(3)
        replay_a_started = threading.Event()
        replay_preceded_last_calibration = []

        def calibrate(run_dir, actual_store, spec, slot):
            del run_dir
            case_id = spec["id"]
            slots = (slot,)
            tracker.acquire("calibration", case_id, slots)
            actual_store.transition(case_id, "CALIBRATING", "test")
            try:
                all_calibrations_started.wait(timeout=2)
                if case_id == case_b:
                    actual_store.transition(
                        case_id, "PROBE_ELIGIBLE", "measured rejection"
                    )
                    raise batch100.CalibrationCandidateRejected(
                        "deliberate candidate rejection"
                    )
                if case_id == case_c:
                    replay_preceded_last_calibration.append(
                        replay_a_started.wait(timeout=3)
                    )
                actual_store.transition(case_id, "FROZEN", "test")
                return f"{case_id}: FROZEN"
            finally:
                tracker.release(slots)

        def execute(run_dir, actual_store, spec, slot_a, slot_b, functional):
            del run_dir, functional
            case_id = spec["id"]
            slots = (slot_a, slot_b)
            tracker.acquire("replay", case_id, slots)
            try:
                actual_store.transition(case_id, "REPLAYING", "test")
                if case_id == case_a:
                    replay_a_started.set()
                actual_store.transition(case_id, "VALIDATED", "test")
                return f"{case_id}: VALIDATED"
            finally:
                tracker.release(slots)

        args = SimpleNamespace(run_dir=Path("/tmp/b100-scheduler-test"), selection="canary")
        with self._scheduler_patches(
            store, specs, [0, 1, 2], calibrate, execute
        ), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(
                BatchError, "B1_CASE_002 calibration.*deliberate candidate rejection"
            ):
                batch100.cmd_run(args)

        self.assertEqual(store.row(case_a)["state"], "VALIDATED")
        self.assertEqual(store.row(case_b)["state"], "PROBE_ELIGIBLE")
        self.assertEqual(store.row(case_c)["state"], "VALIDATED")
        self.assertEqual(replay_preceded_last_calibration, [True])
        self.assertFalse(tracker.collisions)
        replay_allocations = [
            slots for stage, _, slots in tracker.allocations if stage == "replay"
        ]
        self.assertTrue(replay_allocations)
        self.assertTrue(all(len(slots) == 2 for slots in replay_allocations))

    def test_two_slots_make_progress_without_overlap_or_split_allocation(self) -> None:
        case_ids = ["B1_CASE_001", "B1_CASE_002"]
        specs = _scheduler_specs(case_ids)
        store = _FakeStore({case_id: "BOUND" for case_id in case_ids})
        tracker = _SlotTracker()
        both_calibrations_started = threading.Barrier(2)

        def calibrate(run_dir, actual_store, spec, slot):
            del run_dir
            case_id = spec["id"]
            slots = (slot,)
            tracker.acquire("calibration", case_id, slots)
            actual_store.transition(case_id, "CALIBRATING", "test")
            try:
                both_calibrations_started.wait(timeout=2)
                actual_store.transition(case_id, "FROZEN", "test")
                return f"{case_id}: FROZEN"
            finally:
                tracker.release(slots)

        def execute(run_dir, actual_store, spec, slot_a, slot_b, functional):
            del run_dir, functional
            case_id = spec["id"]
            slots = (slot_a, slot_b)
            tracker.acquire("replay", case_id, slots)
            try:
                actual_store.transition(case_id, "REPLAYING", "test")
                actual_store.transition(case_id, "VALIDATED", "test")
                return f"{case_id}: VALIDATED"
            finally:
                tracker.release(slots)

        args = SimpleNamespace(run_dir=Path("/tmp/b100-two-slot-test"), selection="canary")
        with self._scheduler_patches(
            store, specs, [0, 1], calibrate, execute
        ), redirect_stdout(io.StringIO()):
            batch100.cmd_run(args)

        self.assertTrue(
            all(store.row(case_id)["state"] == "VALIDATED" for case_id in case_ids)
        )
        self.assertFalse(tracker.collisions)
        replay_allocations = [
            slots for stage, _, slots in tracker.allocations if stage == "replay"
        ]
        self.assertEqual(len(replay_allocations), 2)
        self.assertTrue(all(set(slots) == {0, 1} for slots in replay_allocations))

    def test_one_healthy_slot_calibrates_cases_but_defers_dual_replay(
        self,
    ) -> None:
        case_ids = ["B1_CASE_001", "B1_CASE_002"]
        specs = _scheduler_specs(case_ids)
        store = _FakeStore({case_id: "BOUND" for case_id in case_ids})
        calibration_slots: list[int] = []
        replay_calls: list[tuple[int, int]] = []

        def calibrate(run_dir, actual_store, spec, slot):
            del run_dir
            calibration_slots.append(slot)
            actual_store.transition(spec["id"], "CALIBRATING", "test")
            actual_store.transition(spec["id"], "FROZEN", "test")
            return f"{spec['id']}: FROZEN"

        def execute(run_dir, actual_store, spec, slot_a, slot_b, functional):
            del run_dir, actual_store, spec, functional
            replay_calls.append((slot_a, slot_b))
            raise AssertionError("strict replay must not run on one slot")

        with self._scheduler_patches(
            store, specs, [7], calibrate, execute
        ), redirect_stdout(io.StringIO()), self.assertRaisesRegex(
            BatchError, "awaiting a second healthy slot"
        ):
            batch100.cmd_run(
                SimpleNamespace(
                    run_dir=Path("/tmp/b100-one-slot-test"),
                    selection="canary",
                )
            )
        self.assertEqual(calibration_slots, [7, 7])
        self.assertEqual(replay_calls, [])
        self.assertTrue(
            all(store.row(case_id)["state"] == "FROZEN" for case_id in case_ids)
        )

    def test_infrastructure_failure_stays_calibrating_for_fail_closed_resume(
        self,
    ) -> None:
        case_id = "B1_CASE_001"
        specs = _scheduler_specs([case_id])
        store = _FakeStore({case_id: "BOUND"})

        def calibrate(run_dir, actual_store, spec, slot):
            del run_dir, slot
            actual_store.transition(spec["id"], "CALIBRATING", "test")
            raise RuntimeError("transport disappeared")

        with self._scheduler_patches(
            store,
            specs,
            [0],
            calibrate,
            lambda *args: None,
        ), redirect_stdout(io.StringIO()), self.assertRaisesRegex(
            BatchError, "transport disappeared"
        ):
            batch100.cmd_run(
                SimpleNamespace(
                    run_dir=Path("/tmp/b100-infra-failure-test"),
                    selection="canary",
                )
            )
        self.assertEqual(store.row(case_id)["state"], "CALIBRATING")

    def test_frozen_crash_window_reuses_exact_durable_replay_slots(self) -> None:
        case_id = "B1_CASE_001"
        specs = _scheduler_specs([case_id])
        store = _FakeStore({case_id: "FROZEN"})
        received: list[tuple[int, int]] = []
        with tempfile.TemporaryDirectory(
            prefix="b100-assigned-scheduler-"
        ) as temporary:
            run_dir = Path(temporary) / "run"
            _materialize_test_replay_assignment(
                run_dir, case_id, (2, 3)
            )

            def execute(
                actual_run,
                actual_store,
                spec,
                slot_a,
                slot_b,
                functional,
            ):
                del actual_run, functional
                received.append((slot_a, slot_b))
                actual_store.transition(spec["id"], "REPLAYING", "test")
                actual_store.transition(spec["id"], "VALIDATED", "test")
                return f"{spec['id']}: VALIDATED"

            with self._scheduler_patches(
                store,
                specs,
                [0, 1, 2, 3],
                lambda *args: None,
                execute,
            ), redirect_stdout(io.StringIO()):
                batch100.cmd_run(
                    SimpleNamespace(
                        run_dir=run_dir, selection="canary"
                    )
                )
        self.assertEqual(received, [(2, 3)])

    def test_execute_one_runs_the_two_fresh_replays_concurrently(self) -> None:
        case_id = "B1_CASE_001"
        target = "u/reg/D"
        replay_barrier = threading.Barrier(2)
        concurrent_replays = []

        with tempfile.TemporaryDirectory(prefix="b100-execute-test-") as temporary:
            run_dir = Path(temporary) / "run"
            job = run_dir / "jobs" / case_id
            job.mkdir(parents=True)
            (run_dir / "bindings").mkdir()
            (job / "calibration" / "selected").mkdir(parents=True)
            (job / "fix.tcl").write_text(
                "ecoChangeCell -inst {u/repair} -cell {BUF_X2}\n"
                "refinePlace -eco true\n"
            )
            (job / "runtime_config.tcl").write_text(
                f"set B100_CASE_ID {{{case_id}}}\n"
            )
            injection_operations = [
                {"instance": "u/inject", "new_ref": "BUF_X0"}
            ]
            repair_operations = [
                {"instance": "u/repair", "new_ref": "BUF_X2"}
            ]
            (run_dir / "bindings" / f"{case_id}.json").write_text(
                json.dumps(
                    {
                        "target_endpoints": [target],
                        "protected_endpoints": [],
                    }
                )
            )
            binding_hash = hashlib.sha256(
                (run_dir / "bindings" / f"{case_id}.json").read_bytes()
            ).hexdigest()
            fix_hash = hashlib.sha256(
                (job / "fix.tcl").read_bytes()
            ).hexdigest()
            plan = {
                "schema_version": "mock_lef_batch100.calibration_plan.v1",
                "case_id": case_id,
                "selected_calibration_evidence": "selected",
                "binding_sha256": binding_hash,
                "calibration_tree_sha256": tree_sha256(job / "calibration"),
                "fix_sha256": fix_hash,
                "injection_operations": injection_operations,
                "repair_operations": repair_operations,
            }
            (job / "calibration_plan.json").write_text(json.dumps(plan))
            _materialize_test_frozen_checkpoint(run_dir, case_id)
            store = _FakeStore(
                {case_id: "FROZEN"},
                {
                    case_id: {
                        "binding_sha256": binding_hash,
                        "injection_sha256": batch100._operation_fingerprint(
                            injection_operations
                        ),
                        "repair_sha256": batch100._operation_fingerprint(
                            repair_operations
                        ),
                    }
                },
            )
            with store._lock:
                store._row_fields[case_id]["injection_sha256"] = "0" * 64
            with (
                patch.object(batch100, "_ssh") as remote,
                self.assertRaisesRegex(
                    BatchError, "operation fingerprint changed before replay"
                ),
            ):
                batch100._execute_one(
                    run_dir, store, {"id": case_id}, 0, 1, {}
                )
            remote.assert_not_called()
            with store._lock:
                store._row_fields[case_id]["injection_sha256"] = (
                    batch100._operation_fingerprint(injection_operations)
                )

            def ssh(slot, command, **kwargs):
                del kwargs
                self.assertEqual(store.row(case_id)["state"], "REPLAYING")
                if "B100_MODE=INJECT" in command:
                    output = (
                        f"B100_INJECTION_COMPLETE {case_id}\n"
                        "*** Message Summary: 0 warning(s), 0 error(s)\n"
                    )
                elif "B100_MODE=REPLAY" in command:
                    try:
                        replay_barrier.wait(timeout=2)
                        concurrent_replays.append(slot)
                    except threading.BrokenBarrierError as exc:
                        raise AssertionError(
                            "the two replay processes did not overlap"
                        ) from exc
                    output = (
                        f"B100_CHECKPOINT_PORTABILITY_PASS {case_id} "
                        "wns_ns=-0.05\n"
                        f"B100_REPLAY_COMPLETE {case_id}\n"
                        f"B100_PROCESS_ID {1000 + slot}\n"
                        "*** Message Summary: 0 warning(s), 0 error(s)\n"
                    )
                else:
                    output = ""
                return SimpleNamespace(returncode=0, stdout=output)

            def scp_from(slot, source, destination, *, recursive=False):
                del slot, recursive
                if source.endswith(".tar"):
                    destination.write_bytes(b"archive")
                    return
                if source.endswith("/reports"):
                    (destination / "reports").mkdir(parents=True)
                    (destination / "reports" / "mock.rpt").write_text("report\n")
                    return
                leaf = source.rsplit("/", 1)[-1]
                fetched = destination / leaf
                (fetched / "reports").mkdir(parents=True)
                (fetched / "reports" / "mock.rpt").write_text("report\n")
                if leaf == "inject":
                    (fetched / "violating.enc").write_text("checkpoint\n")
                    (fetched / "violating.enc.dat").mkdir()
                    (fetched / "violating.enc.dat" / "top.db").write_text("db\n")

            before = _stage_before(target)
            after = _stage_after(target)

            def replay_record(**kwargs):
                return {
                    "slot": kwargs["slot"],
                    "before": copy.deepcopy(before),
                    "after_resize": copy.deepcopy(after),
                    "after_legalize": copy.deepcopy(after),
                }

            spec = {
                "id": case_id,
                "hierarchy": "top",
                "expected_modification_count": 1,
                "nominal_severity": {
                    "target_wns_ns": -0.05,
                    "tolerance_ps": 10.0,
                },
            }
            with (
                patch.object(batch100, "_ssh", side_effect=ssh),
                patch.object(
                    batch100,
                    "_pack_guest_checkpoint",
                    return_value=hashlib.sha256(b"archive").hexdigest(),
                ),
                patch.object(batch100, "_extract_guest_checkpoint_archive"),
                patch.object(
                    batch100.checkpoint_archive,
                    "safe_extract_checkpoint_archive",
                    side_effect=lambda archive, destination, **kwargs: (
                        destination.mkdir(),
                        (destination / "violating.enc").write_text("checkpoint\n"),
                        (destination / "violating.enc.dat").mkdir(),
                        (destination / "violating.enc.dat/top.db").write_text("db\n"),
                    ),
                ),
                patch.object(batch100, "_scp_to"),
                patch.object(batch100, "_scp_tree_to"),
                patch.object(batch100, "_scp_from", side_effect=scp_from),
                patch.object(
                    batch100,
                    "_write_runtime_config",
                    side_effect=lambda path, *args: path.write_text("config\n"),
                ),
                patch.object(batch100, "_sensitivity", return_value={}),
                patch.object(batch100.evidence, "replay_record", side_effect=replay_record),
                patch.object(batch100.finalize_batch, "validate_case"),
                patch.object(batch100, "_remote_remove_case_leaf"),
            ):
                result = batch100._execute_one(
                    run_dir, store, spec, 0, 1, {}
                )
                final_case_was_published = (
                    run_dir / "cases" / case_id
                ).is_dir()
                staging_was_removed = not (
                    run_dir / ".case_staging" / case_id
                ).exists()
                ready_was_removed = not (
                    run_dir
                    / ".case_staging"
                    / f"{case_id}.ready.json"
                ).exists()

        self.assertEqual(set(concurrent_replays), {0, 1})
        self.assertEqual(store.row(case_id)["state"], "VALIDATED")
        self.assertIn("VALIDATED slot0/slot1", result)
        self.assertTrue(final_case_was_published)
        self.assertTrue(staging_was_removed)
        self.assertTrue(ready_was_removed)


class ActorRecoveryTests(unittest.TestCase):
    def test_replay_assignment_is_hash_bound_and_slot_immutable(self) -> None:
        case_id = "B1_CASE_001"
        with tempfile.TemporaryDirectory(
            prefix="b100-replay-assignment-"
        ) as temporary:
            run_dir = Path(temporary) / "run"
            path = _materialize_test_replay_assignment(
                run_dir, case_id, (2, 7)
            )
            original = path.read_bytes()
            reused, payload = batch100._write_or_reuse_replay_assignment(
                run_dir,
                case_id,
                run_dir / "jobs" / case_id,
                (2, 7),
            )
            self.assertEqual(reused, path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(payload["replay_slots"], [2, 7])
            with self.assertRaisesRegex(
                BatchError, "assignment slots changed"
            ):
                batch100._write_or_reuse_replay_assignment(
                    run_dir,
                    case_id,
                    run_dir / "jobs" / case_id,
                    (1, 2),
                )
            (run_dir / "jobs" / case_id / "fix.tcl").write_text(
                "changed\n"
            )
            with self.assertRaisesRegex(
                BatchError, "assignment input changed"
            ):
                batch100._read_replay_assignment(
                    run_dir, case_id, run_dir / "jobs" / case_id
                )

    def test_frozen_cleanup_resumes_after_partial_tombstone_rmtree(
        self,
    ) -> None:
        case_id = "B1_CASE_001"
        store = _FakeStore({case_id: "REPLAYING"})
        store.artifacts[case_id] = {
            "frozen_checkpoint_archive": {},
            "frozen_checkpoint_ready": {},
        }
        with tempfile.TemporaryDirectory(
            prefix="b100-frozen-tombstone-"
        ) as temporary:
            run_dir = Path(temporary) / "run"
            _materialize_test_frozen_checkpoint(run_dir, case_id)
            job = run_dir / "jobs" / case_id

            def partial_rmtree(path):
                payload = (
                    Path(path)
                    / "frozen_checkpoint"
                    / "unpacked"
                    / "violating.enc.dat"
                    / "top.db"
                )
                payload.unlink()
                raise OSError("deliberate rmtree interruption")

            with (
                patch.object(
                    batch100.shutil,
                    "rmtree",
                    side_effect=partial_rmtree,
                ),
                self.assertRaisesRegex(
                    OSError, "deliberate rmtree interruption"
                ),
            ):
                batch100._cleanup_local_frozen_checkpoint(
                    run_dir, case_id, job, store
                )
            intent = job / "frozen_checkpoint.cleanup.json"
            self.assertEqual(
                json.loads(intent.read_text())["phase"], "TOMBSTONED"
            )
            batch100._cleanup_local_frozen_checkpoint(
                run_dir, case_id, job, store
            )
            _, removed = batch100._read_removed_frozen_cleanup(
                run_dir, case_id, job
            )
            self.assertEqual(removed["phase"], "REMOVED")
            self.assertFalse((job / "frozen_checkpoint").exists())
            self.assertFalse(
                (job / "frozen_checkpoint.ready.json").exists()
            )
            self.assertEqual(store.artifacts[case_id], {})

    def test_completed_cleanup_never_deletes_a_regained_tombstone(
        self,
    ) -> None:
        case_id = "B1_CASE_001"
        with tempfile.TemporaryDirectory(
            prefix="b100-frozen-cleanup-safety-"
        ) as temporary:
            run_dir = Path(temporary) / "run"
            _materialize_test_frozen_checkpoint(run_dir, case_id)
            job = run_dir / "jobs" / case_id
            batch100._cleanup_local_frozen_checkpoint(
                run_dir, case_id, job
            )
            tombstone, _ = batch100._frozen_cleanup_paths(job)
            tombstone.mkdir()
            retained = tombstone / "unrelated-after-cleanup.txt"
            retained.write_text("must not be deleted\n")
            with self.assertRaisesRegex(
                BatchError, "regained tombstone"
            ):
                batch100._cleanup_local_frozen_checkpoint(
                    run_dir, case_id, job
                )
            self.assertEqual(
                retained.read_text(), "must not be deleted\n"
            )

    def test_staging_promotion_recovers_after_frozen_cleanup_window(
        self,
    ) -> None:
        case_id = "B1_CASE_001"
        spec = {"id": case_id}
        store = _FakeStore({case_id: "REPLAYING"})
        with tempfile.TemporaryDirectory(
            prefix="b100-post-cleanup-promotion-"
        ) as temporary:
            run_dir = Path(temporary) / "run"
            (run_dir / "cases").mkdir(parents=True)
            _materialize_test_replay_assignment(run_dir, case_id)
            staging, _ = batch100._case_staging_paths(
                run_dir, case_id
            )
            staging.mkdir()
            (staging / "artifact.txt").write_text("validated\n")
            ready = batch100._write_case_staging_ready(
                run_dir, case_id, staging, (0, 1)
            )
            batch100._cleanup_local_frozen_checkpoint(
                run_dir,
                case_id,
                run_dir / "jobs" / case_id,
                store,
            )
            self.assertTrue(
                (
                    run_dir
                    / "jobs"
                    / case_id
                    / "frozen_checkpoint.cleanup.json"
                ).is_file()
            )
            with (
                patch.object(batch100.finalize_batch, "validate_case"),
                patch.object(batch100, "_remote_remove_case_leaf"),
            ):
                result = batch100._finish_ready_case_staging(
                    run_dir, store, spec, {}
                )
            self.assertIn("VALIDATED slot0/slot1", result)
            self.assertFalse(staging.exists())
            self.assertFalse(ready.exists())
            self.assertFalse(
                (
                    run_dir
                    / "jobs"
                    / case_id
                    / "frozen_checkpoint.cleanup.json"
                ).exists()
            )

    def test_resume_promotes_complete_calibrating_freeze(self) -> None:
        case_id = "B1_CASE_001"
        specs = {
            "cases": [{"id": case_id}],
            "canary_case_ids": [case_id],
            "acceptance": {},
        }
        with tempfile.TemporaryDirectory(
            prefix="b100-freeze-resume-"
        ) as temporary:
            run_dir = Path(temporary) / "run"
            _materialize_test_frozen_checkpoint(run_dir, case_id)
            binding = run_dir / "bindings" / f"{case_id}.json"
            store = _FakeStore(
                {case_id: "CALIBRATING"},
                {
                    case_id: {
                        "slot": "slot0",
                        "binding_sha256": hashlib.sha256(
                            binding.read_bytes()
                        ).hexdigest(),
                    }
                },
            )
            dispatched: list[str] = []
            original_read_json = batch100.read_json

            def read_json(path, label=None):
                if Path(path) == batch100.SPECS_PATH:
                    return specs
                return original_read_json(path, label)

            with (
                patch.object(
                    batch100,
                    "_verified_store",
                    return_value=(store, {}),
                ),
                patch.object(batch100, "read_json", side_effect=read_json),
                patch.object(batch100, "_remote_remove_case_leaf"),
                patch.object(
                    batch100,
                    "cmd_run",
                    side_effect=lambda args: dispatched.append(
                        args.selection
                    ),
                ),
                redirect_stdout(io.StringIO()) as output,
            ):
                batch100.cmd_resume(
                    SimpleNamespace(run_dir=run_dir)
                )
            self.assertEqual(store.row(case_id)["state"], "FROZEN")
            self.assertEqual(dispatched, ["canary"])
            self.assertIn("freeze promotion resume", output.getvalue())
            self.assertEqual(
                set(store.artifacts[case_id]),
                {
                    "frozen_checkpoint_archive",
                    "frozen_checkpoint_ready",
                },
            )


class FailureEvidenceTests(unittest.TestCase):
    def test_calibration_fetches_partial_evidence_before_classification(
        self,
    ) -> None:
        case_id = "B1_CASE_001"
        target = "u/target_reg/D"
        operation = {
            "instance": "u/inject",
            "baseline_ref": "BUF_X2",
            "new_ref": "BUF_X1",
            "candidate_refs_nearest_first": ["BUF_X1"],
            "candidate_estimated_slowdown_ns": [0.2],
            "candidate_estimated_slowdown_by_endpoint_ns": {
                target: [0.2]
            },
        }
        binding = {
            "target_endpoints": [target],
            "target_baseline_slacks_ns": {target: 0.05},
            "protected_endpoints": [],
            "injection_operations": [operation],
            "repair_operations": [
                {
                    "instance": "u/inject",
                    "checkpoint_ref": "BUF_X1",
                    "new_ref": "BUF_X2",
                }
            ],
            "calibration_candidates": [
                {
                    "candidate_index": 0,
                    "injection_operations": [operation],
                    "repair_operations": [
                        {
                            "instance": "u/inject",
                            "checkpoint_ref": "BUF_X1",
                            "new_ref": "BUF_X2",
                        }
                    ],
                }
            ],
        }
        spec = {
            "id": case_id,
            "expected_modification_count": 1,
            "injection_profile": "I0",
            "nominal_severity": {
                "target_wns_ns": -0.1,
                "tolerance_ps": 10,
            },
        }
        store = _FakeStore({case_id: "BOUND"})
        order: list[str] = []

        def ssh(slot, command, **kwargs):
            del slot, kwargs
            if command.startswith("test ! -e "):
                return SimpleNamespace(returncode=0, stdout="")
            order.append("process")
            return SimpleNamespace(
                returncode=1,
                stdout="**ERROR: deliberate calibration failure\n",
            )

        def scp_from(slot, source, destination, *, recursive=False):
            del slot, recursive
            order.append("fetch")
            fetched = destination / source.rsplit("/", 1)[-1]
            fetched.mkdir()
            (fetched / "partial.rpt").write_text(
                "partial failure evidence\n"
            )

        with tempfile.TemporaryDirectory(
            prefix="b100-calibration-failure-evidence-"
        ) as temporary:
            run_dir = Path(temporary) / "run"
            (run_dir / "bindings").mkdir(parents=True)
            (run_dir / "bindings" / f"{case_id}.json").write_text(
                json.dumps(binding)
            )
            with (
                patch.object(batch100, "_ssh", side_effect=ssh),
                patch.object(batch100, "_scp_to"),
                patch.object(
                    batch100, "_scp_from", side_effect=scp_from
                ),
                self.assertRaisesRegex(
                    BatchError, "injection process 1 failed"
                ),
            ):
                batch100._calibrate_one(
                    run_dir, store, spec, slot=0
                )
            job = run_dir / "jobs" / case_id
            self.assertEqual(order, ["process", "fetch"])
            self.assertTrue(
                (
                    job
                    / "calibration"
                    / "candidate_00"
                    / "001_injection"
                    / "innovus.log"
                ).is_file()
            )
            self.assertTrue(
                (
                    job
                    / "calibration"
                    / "candidate_00"
                    / "001_injection"
                    / "fetch"
                    / "001_injection"
                    / "partial.rpt"
                ).is_file()
            )
            self.assertEqual(
                store.row(case_id)["state"], "CALIBRATING"
            )

    def test_dual_replay_collects_both_failure_evidence_sets(self) -> None:
        case_id = "B1_CASE_001"
        target = "u/reg/D"
        barrier = threading.Barrier(2)
        fetched_slots: list[int] = []
        with tempfile.TemporaryDirectory(
            prefix="b100-replay-failure-evidence-"
        ) as temporary:
            run_dir = Path(temporary) / "run"
            job = run_dir / "jobs" / case_id
            (job / "calibration").mkdir(parents=True)
            (job / "calibration" / "measurement.txt").write_text("ok\n")
            (job / "fix.tcl").write_text(
                "ecoChangeCell -inst {u/repair} -cell {BUF_X2}\n"
                "refinePlace -eco true\n"
            )
            (job / "runtime_config.tcl").write_text(
                f"set B100_CASE_ID {{{case_id}}}\n"
            )
            (run_dir / "bindings").mkdir()
            binding_path = run_dir / "bindings" / f"{case_id}.json"
            binding_path.write_text(
                json.dumps(
                    {
                        "target_endpoints": [target],
                        "protected_endpoints": [],
                    }
                )
            )
            injection = [
                {
                    "instance": "u/inject",
                    "old_ref": "BUF_X2",
                    "new_ref": "BUF_X1",
                }
            ]
            repair = [
                {
                    "instance": "u/repair",
                    "old_ref": "BUF_X1",
                    "new_ref": "BUF_X2",
                }
            ]
            plan = {
                "schema_version":
                    "mock_lef_batch100.calibration_plan.v1",
                "case_id": case_id,
                "slot": "slot0",
                "binding_sha256": hashlib.sha256(
                    binding_path.read_bytes()
                ).hexdigest(),
                "calibration_tree_sha256": tree_sha256(
                    job / "calibration"
                ),
                "fix_sha256": hashlib.sha256(
                    (job / "fix.tcl").read_bytes()
                ).hexdigest(),
                "injection_operations": injection,
                "repair_operations": repair,
            }
            (job / "calibration_plan.json").write_text(
                json.dumps(plan)
            )
            _materialize_test_frozen_checkpoint(run_dir, case_id)
            store = _FakeStore(
                {case_id: "FROZEN"},
                {
                    case_id: {
                        "binding_sha256": plan["binding_sha256"],
                        "injection_sha256":
                            batch100._operation_fingerprint(injection),
                        "repair_sha256":
                            batch100._operation_fingerprint(repair),
                    }
                },
            )

            def ssh(slot, command, **kwargs):
                del kwargs
                if "B100_MODE=REPLAY" in command:
                    barrier.wait(timeout=2)
                    return SimpleNamespace(
                        returncode=1,
                        stdout=(
                            f"**ERROR: replay failed on slot{slot}\n"
                        ),
                    )
                return SimpleNamespace(returncode=0, stdout="")

            def scp_from(
                slot, source, destination, *, recursive=False
            ):
                del recursive
                fetched_slots.append(slot)
                leaf = source.rsplit("/", 1)[-1]
                fetched = destination / leaf
                fetched.mkdir()
                (fetched / "partial.rpt").write_text(
                    f"slot{slot} failure evidence\n"
                )

            with (
                patch.object(batch100, "_ssh", side_effect=ssh),
                patch.object(batch100, "_scp_to"),
                patch.object(
                    batch100, "_extract_guest_checkpoint_archive"
                ),
                patch.object(
                    batch100, "_scp_from", side_effect=scp_from
                ),
                self.assertRaisesRegex(
                    BatchError,
                    "dual replay failure.*replay 1.*replay 2",
                ),
            ):
                batch100._execute_one(
                    run_dir,
                    store,
                    {
                        "id": case_id,
                        "expected_modification_count": 1,
                    },
                    0,
                    1,
                    {},
                )
            self.assertEqual(set(fetched_slots), {0, 1})
            self.assertTrue(
                (job / "replay_1_fetch" / "replay_1").is_dir()
            )
            self.assertTrue(
                (job / "replay_2_fetch" / "replay_2").is_dir()
            )
            self.assertEqual(
                store.row(case_id)["state"], "REPLAYING"
            )


class CasePromotionRecoveryTests(unittest.TestCase):
    @staticmethod
    def _ready_case(run_dir: Path, case_id: str) -> tuple[Path, Path]:
        (run_dir / "cases").mkdir(parents=True)
        _materialize_test_replay_assignment(run_dir, case_id)
        staging, _ = batch100._case_staging_paths(run_dir, case_id)
        staging.mkdir()
        (staging / "artifact.txt").write_text("validated artifact\n")
        ready = batch100._write_case_staging_ready(
            run_dir, case_id, staging, (0, 1)
        )
        return staging, ready

    def test_partial_cleanup_failure_is_retryable_and_never_promotes(self) -> None:
        case_id = "B1_CASE_001"
        spec = {"id": case_id}
        store = _FakeStore({case_id: "REPLAYING"})
        first_calls: list[int] = []
        retry_calls: list[int] = []
        with tempfile.TemporaryDirectory(
            prefix="b100-promotion-recovery-"
        ) as temporary:
            run_dir = Path(temporary) / "run"
            staging, ready = self._ready_case(run_dir, case_id)
            final_case = run_dir / "cases" / case_id

            def partial_cleanup(slot, run_id, actual_case_id):
                self.assertEqual(run_id, "run")
                self.assertEqual(actual_case_id, case_id)
                first_calls.append(slot)
                if slot == 1:
                    raise BatchError("deliberate slot1 cleanup failure")

            with (
                patch.object(
                    batch100.finalize_batch, "validate_case"
                ) as validate,
                patch.object(
                    batch100,
                    "_remote_remove_case_leaf",
                    side_effect=partial_cleanup,
                ),
            ):
                with self.assertRaisesRegex(
                    BatchError, "dual-slot cleanup incomplete.*slot1"
                ):
                    batch100._finish_ready_case_staging(
                        run_dir, store, spec, {}
                    )

            self.assertEqual(set(first_calls), {0, 1})
            self.assertEqual(store.row(case_id)["state"], "REPLAYING")
            self.assertTrue(staging.is_dir())
            self.assertTrue(ready.is_file())
            self.assertFalse(final_case.exists())
            validate.assert_called_once_with(staging, spec, {})

            def successful_cleanup(slot, run_id, actual_case_id):
                self.assertTrue(staging.is_dir())
                self.assertFalse(final_case.exists())
                self.assertEqual((run_id, actual_case_id), ("run", case_id))
                retry_calls.append(slot)

            original_transition = store.transition

            def transition_after_promotion(actual_case_id, state, reason, **updates):
                self.assertEqual(set(retry_calls), {0, 1})
                self.assertTrue(final_case.is_dir())
                self.assertFalse(staging.exists())
                original_transition(
                    actual_case_id, state, reason, **updates
                )

            store.transition = transition_after_promotion
            with (
                patch.object(batch100.finalize_batch, "validate_case"),
                patch.object(
                    batch100,
                    "_remote_remove_case_leaf",
                    side_effect=successful_cleanup,
                ),
            ):
                result = batch100._finish_ready_case_staging(
                    run_dir, store, spec, {}
                )

            self.assertEqual(set(retry_calls), {0, 1})
            self.assertEqual(store.row(case_id)["state"], "VALIDATED")
            self.assertTrue(final_case.is_dir())
            self.assertFalse(staging.exists())
            self.assertFalse(ready.exists())
            self.assertIn("VALIDATED slot0/slot1", result)

    def test_ready_hash_accepts_and_binds_safe_checkpoint_symlinks(self) -> None:
        case_id = "B1_CASE_001"
        with tempfile.TemporaryDirectory(
            prefix="b100-promotion-symlink-"
        ) as temporary:
            run_dir = Path(temporary) / "run"
            (run_dir / "cases").mkdir(parents=True)
            _materialize_test_replay_assignment(run_dir, case_id)
            staging, _ = batch100._case_staging_paths(run_dir, case_id)
            checkpoint = staging / "violating.enc.dat"
            checkpoint.mkdir(parents=True)
            (staging / "violating.enc").write_text("checkpoint\n")
            link = checkpoint / "lib.link"
            first_target = (
                f"{batch100.GUEST_BASELINE}/base.enc.dat/lib_a.lib"
            )
            second_target = (
                f"{batch100.GUEST_BASELINE}/base.enc.dat/lib_b.lib"
            )
            os.symlink(first_target, link)
            ready = batch100._write_case_staging_ready(
                run_dir, case_id, staging, (0, 1)
            )
            _, _, payload = batch100._read_case_staging_ready(
                run_dir, case_id
            )
            self.assertEqual(
                payload["artifact_tree_hash_algorithm"],
                finalize_batch.CASE_ARTIFACT_TREE_HASH_ALGORITHM,
            )
            link.unlink()
            os.symlink(second_target, link)
            store = _FakeStore({case_id: "REPLAYING"})
            with (
                patch.object(batch100.finalize_batch, "validate_case"),
                patch.object(batch100, "_remote_remove_case_leaf") as cleanup,
                self.assertRaisesRegex(
                    BatchError, "ready artifact tree hash changed"
                ),
            ):
                batch100._finish_ready_case_staging(
                    run_dir, store, {"id": case_id}, {}
                )
            cleanup.assert_not_called()
            self.assertTrue(staging.is_dir())
            self.assertTrue(ready.is_file())

    def test_validation_failure_keeps_hidden_staging_and_skips_cleanup(self) -> None:
        case_id = "B1_CASE_001"
        spec = {"id": case_id}
        store = _FakeStore({case_id: "REPLAYING"})
        with tempfile.TemporaryDirectory(
            prefix="b100-promotion-validation-"
        ) as temporary:
            run_dir = Path(temporary) / "run"
            staging, ready = self._ready_case(run_dir, case_id)
            with (
                patch.object(
                    batch100.finalize_batch,
                    "validate_case",
                    side_effect=BatchError("invalid staged evidence"),
                ),
                patch.object(
                    batch100, "_remote_remove_case_leaf"
                ) as cleanup,
            ):
                with self.assertRaisesRegex(
                    BatchError, "invalid staged evidence"
                ):
                    batch100._finish_ready_case_staging(
                        run_dir, store, spec, {}
                    )
            cleanup.assert_not_called()
            self.assertEqual(store.row(case_id)["state"], "REPLAYING")
            self.assertTrue(staging.is_dir())
            self.assertTrue(ready.is_file())
            self.assertFalse((run_dir / "cases" / case_id).exists())

    def test_resume_completes_only_hash_bound_ready_staging(self) -> None:
        case_id = "B1_CASE_001"
        spec = {"id": case_id}
        specs = {
            "cases": [spec],
            "canary_case_ids": [case_id],
            "acceptance": {},
        }
        store = _FakeStore({case_id: "REPLAYING"})
        with tempfile.TemporaryDirectory(
            prefix="b100-cleanup-resume-"
        ) as temporary:
            run_dir = Path(temporary) / "run"
            self._ready_case(run_dir, case_id)
            original_read_json = batch100.read_json

            def read_json(path, label=None):
                if Path(path) == batch100.SPECS_PATH:
                    return specs
                return original_read_json(path, label)

            with (
                patch.object(
                    batch100,
                    "_verified_store",
                    return_value=(store, {}),
                ),
                patch.object(batch100, "read_json", side_effect=read_json),
                patch.object(batch100.finalize_batch, "validate_case"),
                patch.object(batch100, "_remote_remove_case_leaf"),
                redirect_stdout(io.StringIO()) as output,
            ):
                batch100.cmd_resume(SimpleNamespace(run_dir=run_dir))

            self.assertEqual(store.row(case_id)["state"], "VALIDATED")
            self.assertTrue((run_dir / "cases" / case_id).is_dir())
            self.assertIn("cleanup-only resume", output.getvalue())

    def test_remote_cleanup_is_idempotent_and_guards_symlink_ancestors(self) -> None:
        commands: list[str] = []

        def ssh(slot, command, **kwargs):
            self.assertEqual(slot, 2)
            self.assertEqual(kwargs["timeout"], 120)
            commands.append(command)
            return SimpleNamespace(returncode=0, stdout="")

        with patch.object(batch100, "_ssh", side_effect=ssh):
            batch100._remote_remove_case_leaf(
                2, "safe-run", "B1_CASE_001"
            )

        self.assertEqual(len(commands), 1)
        command = commands[0]
        self.assertIn(
            f"test -L {batch100.GUEST_RUN_ROOT}/safe-run/tasks", command
        )
        self.assertIn(
            f"test ! -e {batch100.GUEST_RUN_ROOT}/safe-run/tasks", command
        )
        self.assertIn(
            f"test -L {batch100.GUEST_RUN_ROOT}/safe-run/tasks/B1_CASE_001",
            command,
        )
        self.assertIn(
            f"rm -rf -- {batch100.GUEST_RUN_ROOT}/safe-run/tasks/B1_CASE_001",
            command,
        )
        with self.assertRaisesRegex(BatchError, "unsafe cleanup run ID"):
            batch100._remote_remove_case_leaf(
                2, "../unsafe", "B1_CASE_001"
            )


class FinalizeRecoveryTests(unittest.TestCase):
    def test_partial_finalized_state_is_revalidated_and_converged(self) -> None:
        already_finalized = "B1_CASE_001"
        pending = "B1_CASE_002"
        store = _FakeStore(
            {already_finalized: "FINALIZED", pending: "VALIDATED"}
        )
        args = SimpleNamespace(run_dir=Path("/tmp/b100-finalize-recovery"))
        with (
            patch.object(
                batch100, "_verified_store", return_value=(store, {})
            ),
            patch.object(
                batch100.finalize_batch,
                "finalize",
                return_value={"case_count": 100},
            ) as strict_finalize,
            redirect_stdout(io.StringIO()) as output,
        ):
            batch100.cmd_finalize(args)

        strict_finalize.assert_called_once_with(args.run_dir)
        self.assertEqual(store.row(already_finalized)["state"], "FINALIZED")
        self.assertEqual(store.row(pending)["state"], "FINALIZED")
        self.assertEqual(store.transitions, [(pending, "FINALIZED")])
        self.assertIn("100/100 FINALIZED", output.getvalue())

    def test_nonvalidated_state_still_blocks_finalization(self) -> None:
        store = _FakeStore(
            {"B1_CASE_001": "FINALIZED", "B1_CASE_002": "REPLAYING"}
        )
        args = SimpleNamespace(run_dir=Path("/tmp/b100-finalize-blocked"))
        with (
            patch.object(
                batch100, "_verified_store", return_value=(store, {})
            ),
            patch.object(batch100.finalize_batch, "finalize") as strict_finalize,
            self.assertRaisesRegex(BatchError, "1 remain"),
        ):
            batch100.cmd_finalize(args)
        strict_finalize.assert_not_called()


class RebindRecoveryTests(unittest.TestCase):
    def test_frozen_probe_rebind_view_rejects_content_changes(self) -> None:
        baseline_hash = "a" * 64
        with tempfile.TemporaryDirectory(prefix="b100-probe-rebind-") as temporary:
            run_dir = Path(temporary)
            probe_dir = run_dir / "probe"
            probe_dir.mkdir()
            payload = probe_dir / "payload.tsv"
            payload.write_text("frozen\n")
            frozen_hash = tree_sha256(probe_dir)
            batch100.atomic_json(
                probe_dir / "probe_manifest.json",
                {
                    "schema_version": "mock_lef_batch100.probe_manifest.v1",
                    "tree_sha256_before_manifest": frozen_hash,
                    "baseline_entry_sha256": baseline_hash,
                    "probe_tcl_sha256": hashlib.sha256(
                        batch100.TOOLS["probe"].read_bytes()
                    ).hexdigest(),
                    "path_count": 1,
                    "point_count": 1,
                    "drive_ladder_rows": 1,
                    "reachability_rows": 1,
                },
            )
            verified = {
                "paths": [{}],
                "points": [{}],
                "ladders": [{}],
                "reachability": [{}],
                "tree_sha256": "b" * 64,
            }
            with patch.object(
                batch100, "_verify_probe", return_value=verified
            ):
                result = batch100._verified_frozen_probe(
                    run_dir, {"entry_sha256": baseline_hash}
                )
                self.assertEqual(result["tree_sha256"], frozen_hash)
                payload.write_text("tampered\n")
                with self.assertRaisesRegex(
                    BatchError, "frozen probe content hash changed"
                ):
                    batch100._verified_frozen_probe(
                        run_dir, {"entry_sha256": baseline_hash}
                    )

    def test_rebind_archives_only_rejected_case_and_preserves_other_case(
        self,
    ) -> None:
        rejected = "B1_CASE_001"
        untouched = "B1_CASE_002"
        probe_hash = "9" * 64
        baseline = {"entry_sha256": "8" * 64}
        specs = {"cases": [{"id": rejected}, {"id": untouched}]}

        with tempfile.TemporaryDirectory(prefix="b100-case-rebind-") as temporary:
            run_dir = Path(temporary) / "run"
            store = StateStore(run_dir)
            store.initialize(
                [rejected, untouched],
                {"runner": "1" * 64},
                baseline,
            )
            binding_root = run_dir / "bindings"
            binding_root.mkdir()
            old_binding = {
                "case_id": rejected,
                "probe_tree_sha256": probe_hash,
                "target_endpoints": ["old_target"],
                "protected_endpoints": ["old_protected"],
            }
            other_binding = {
                "case_id": untouched,
                "probe_tree_sha256": probe_hash,
                "target_endpoints": ["other_target"],
                "protected_endpoints": ["other_protected"],
            }
            batch100.atomic_json(
                binding_root / f"{rejected}.json", old_binding
            )
            batch100.atomic_json(
                binding_root / f"{untouched}.json", other_binding
            )
            for case_id in (rejected, untouched):
                store.transition(case_id, "PROBE_ELIGIBLE", "test probe")
                store.transition(
                    case_id,
                    "BOUND",
                    "test bind",
                    binding_sha256=hashlib.sha256(
                        (binding_root / f"{case_id}.json").read_bytes()
                    ).hexdigest(),
                )
            store.transition(
                rejected, "CALIBRATING", "test failure", attempt=1, slot="slot0"
            )
            store.transition(
                rejected, "PROBE_ELIGIBLE", "candidate exhausted", slot=None
            )

            rejected_job = run_dir / "jobs" / rejected
            other_job = run_dir / "jobs" / untouched
            rejected_job.mkdir(parents=True)
            other_job.mkdir(parents=True)
            (rejected_job / "rejection.log").write_text("keep this evidence\n")
            (other_job / "keep.log").write_text("do not touch\n")
            other_case = run_dir / "cases" / untouched
            other_case.mkdir(parents=True)
            (other_case / "artifact").write_text("validated artifact\n")
            rejected_injection_claim = "a" * 64
            rejected_repair_claim = "b" * 64
            other_injection_claim = "c" * 64
            other_repair_claim = "d" * 64
            batch100.atomic_json(
                run_dir / "fingerprint_claims.json",
                {
                    "injection": {
                        rejected_injection_claim: rejected,
                        other_injection_claim: untouched,
                    },
                    "repair": {
                        rejected_repair_claim: rejected,
                        other_repair_claim: untouched,
                    },
                },
            )
            other_binding_before = (
                binding_root / f"{untouched}.json"
            ).read_bytes()
            other_job_before = tree_sha256(other_job)
            other_case_before = tree_sha256(other_case)

            probe = {
                "tree_sha256": probe_hash,
                "paths": [
                    {"endpoint": endpoint, "hierarchy_groups": "exp"}
                    for endpoint in (
                        "old_target",
                        "old_protected",
                        "other_target",
                        "other_protected",
                        "new_target",
                        "new_protected",
                    )
                ],
                "points": [],
                "ladders": [],
                "reachability": [],
            }
            cleanup_calls = []

            def bind_single(filtered_probe, single_specs, destination):
                self.assertEqual(single_specs["cases"], [{"id": rejected}])
                selectable = {
                    row["endpoint"]
                    for row in filtered_probe["paths"]
                    if row["hierarchy_groups"]
                }
                self.assertEqual(
                    selectable, {"new_target", "new_protected"}
                )
                output = destination / "bindings"
                output.mkdir()
                batch100.atomic_json(
                    output / f"{rejected}.json",
                    {
                        "case_id": rejected,
                        "probe_tree_sha256": probe_hash,
                        "target_endpoints": ["new_target"],
                        "protected_endpoints": ["new_protected"],
                    },
                )

            with (
                patch.object(
                    batch100, "_verified_frozen_probe", return_value=probe
                ),
                patch.object(batch100, "_bind_cases", side_effect=bind_single),
                patch.object(
                    batch100,
                    "_clear_remote_retry_leaves",
                    side_effect=lambda slots, run_id, case_id: cleanup_calls.append(
                        (tuple(slots), run_id, case_id)
                    ),
                ),
            ):
                batch100._rebind_probe_eligible_case(
                    run_dir,
                    store,
                    {"baseline": baseline},
                    specs,
                    rejected,
                    [0, 1],
                )

            rebound = json.loads(
                (binding_root / f"{rejected}.json").read_text()
            )
            self.assertEqual(rebound["target_endpoints"], ["new_target"])
            self.assertEqual(store.row(rejected)["state"], "BOUND")
            self.assertEqual(
                store.row(rejected)["binding_sha256"],
                hashlib.sha256(
                    (binding_root / f"{rejected}.json").read_bytes()
                ).hexdigest(),
            )
            audit = (
                run_dir
                / "rebind_audit"
                / rejected
                / "attempt_001"
            )
            self.assertEqual(
                json.loads((audit / "binding.json").read_text()), old_binding
            )
            self.assertTrue((audit / "job" / "rejection.log").is_file())
            self.assertFalse(rejected_job.exists())
            self.assertEqual(
                (binding_root / f"{untouched}.json").read_bytes(),
                other_binding_before,
            )
            self.assertEqual(tree_sha256(other_job), other_job_before)
            self.assertEqual(tree_sha256(other_case), other_case_before)
            self.assertEqual(cleanup_calls, [((0, 1), "run", rejected)])
            claims = json.loads(
                (run_dir / "fingerprint_claims.json").read_text()
            )
            self.assertEqual(
                claims,
                {
                    "injection": {other_injection_claim: untouched},
                    "repair": {other_repair_claim: untouched},
                },
            )
            rebind_manifest = json.loads(
                (audit / "rebind_manifest.json").read_text()
            )
            self.assertEqual(
                rebind_manifest["released_provisional_fingerprint_claims"],
                {
                    "injection": [rejected_injection_claim],
                    "repair": [rejected_repair_claim],
                },
            )

    def test_run_rebinds_probe_eligible_case_and_resume_dispatches_it(self) -> None:
        case_id = "B1_CASE_001"
        specs = _scheduler_specs([case_id])
        store = _FakeStore({case_id: "PROBE_ELIGIBLE"})
        rebind_calls = []

        def rebind(run_dir, actual_store, manifest, actual_specs, actual_case, slots):
            del run_dir, manifest
            self.assertIs(actual_specs, specs)
            rebind_calls.append((actual_case, tuple(slots)))
            actual_store.transition(actual_case, "BOUND", "test")

        def calibrate(run_dir, actual_store, spec, slot):
            del run_dir
            actual_store.transition(spec["id"], "CALIBRATING", "test")
            actual_store.transition(spec["id"], "FROZEN", "test")
            return f"{spec['id']}: FROZEN slot{slot}"

        def execute(run_dir, actual_store, spec, slot_a, slot_b, functional):
            del run_dir, functional
            actual_store.transition(spec["id"], "REPLAYING", "test")
            actual_store.transition(spec["id"], "VALIDATED", "test")
            return f"{spec['id']}: VALIDATED slot{slot_a}/slot{slot_b}"

        args = SimpleNamespace(
            run_dir=Path("/tmp/b100-rebind-scheduler-test"),
            selection="canary",
        )
        scheduler_patches = patch.multiple(
            batch100,
            _verified_store=lambda run_dir: (store, {"baseline": {}}),
            _verify_execution_slots=lambda baseline: [0, 1],
            read_json=lambda path: specs,
            _functional_pairs=lambda run_dir: {},
            _rebind_probe_eligible_case=rebind,
            _calibrate_one=calibrate,
            _execute_one=execute,
        )
        with scheduler_patches, redirect_stdout(io.StringIO()):
            batch100.cmd_run(args)
        self.assertEqual(rebind_calls, [(case_id, (0, 1))])
        self.assertEqual(store.row(case_id)["state"], "VALIDATED")

        resumed_store = _FakeStore({case_id: "PROBE_ELIGIBLE"})
        dispatched = []
        with (
            patch.object(
                batch100,
                "_verified_store",
                return_value=(resumed_store, {"baseline": {}}),
            ),
            patch.object(batch100, "read_json", return_value=specs),
            patch.object(
                batch100,
                "cmd_run",
                side_effect=lambda resume_args: dispatched.append(
                    resume_args.selection
                ),
            ),
            redirect_stdout(io.StringIO()),
        ):
            batch100.cmd_resume(
                SimpleNamespace(run_dir=Path("/tmp/b100-resume-test"))
            )
        self.assertEqual(dispatched, ["canary"])

    def test_retry_cleanup_failure_blocks_case_before_calibration(self) -> None:
        case_id = "B1_CASE_001"
        specs = _scheduler_specs([case_id])
        store = _FakeStore({case_id: "BOUND"})
        calibration_calls = []
        with tempfile.TemporaryDirectory(prefix="b100-retry-block-") as temporary:
            run_dir = Path(temporary) / "run"
            (run_dir / "rebind_audit" / case_id).mkdir(parents=True)
            with (
                patch.object(
                    batch100,
                    "_verified_store",
                    return_value=(store, {"baseline": {}}),
                ),
                patch.object(
                    batch100, "_verify_execution_slots", return_value=[0, 1]
                ),
                patch.object(batch100, "read_json", return_value=specs),
                patch.object(
                    batch100,
                    "_clear_remote_retry_leaves",
                    side_effect=BatchError("slot cleanup refused"),
                ),
                patch.object(
                    batch100,
                    "_calibrate_one",
                    side_effect=lambda *args: calibration_calls.append(args),
                ),
                redirect_stdout(io.StringIO()),
            ):
                with self.assertRaisesRegex(
                    BatchError, "retry cleanup.*slot cleanup refused"
                ):
                    batch100.cmd_run(
                        SimpleNamespace(
                            run_dir=run_dir, selection="canary"
                        )
                    )
        self.assertEqual(calibration_calls, [])
        self.assertEqual(store.row(case_id)["state"], "BOUND")


def _timing_report(slacks: dict[str, float]) -> str:
    blocks = ["Cadence Innovus 21.10-p004_1"]
    for index, (endpoint, slack) in enumerate(slacks.items(), start=1):
        blocks.extend(
            [
                f"Path {index}:",
                f"Endpoint: {endpoint}",
                f"Slack Time {slack:.9f}",
            ]
        )
    return "\n".join(blocks) + "\n"


def _write_raw_replay_reports(
    reports: Path, target: str, old_ref: str, new_ref: str
) -> None:
    reports.mkdir(parents=True)
    full_before = {target: -0.060}
    full_before.update(
        {
            f"u/dummy_reg_{index:04d}/D": 0.200 + index / 1_000_000.0
            for index in range(evidence.EXPECTED_PORTABILITY_ENDPOINTS - 1)
        }
    )
    full_after = dict(full_before)
    full_after[target] = 0.010
    (reports / "checkpoint_portability_slacks.tsv").write_text(
        "endpoint\tslack_ns\n"
        + "".join(f"{name}\t{slack:.9f}\n" for name, slack in full_before.items())
    )
    for stage, setup_slacks, hold_slack in (
        ("before", full_before, 0.050),
        ("after_legalize", full_after, 0.049),
    ):
        (reports / f"setup_{stage}.rpt").write_text(_timing_report(setup_slacks))
        (reports / f"target_setup_{stage}.rpt").write_text(
            _timing_report({target: setup_slacks[target]})
        )
        (reports / f"hold_{stage}.rpt").write_text(
            _timing_report({target: hold_slack})
        )
        (reports / f"drv_{stage}.rpt").write_text(
            "Check type : max_transition\nNo violations\n"
            "Check type : max_capacitance\nNo violations\n"
            "Check type : max_fanout\nNo violations\n"
        )
        (reports / f"connectivity_{stage}.rpt").write_text(
            "Begin Summary\nFound no problems or warnings\nEnd Summary\n"
        )
        (reports / f"drc_{stage}.rpt").write_text(
            "No DRC violations were found\n"
        )
        (reports / f"placement_{stage}.rpt").write_text(
            "Placement check complete: 0 errors\n"
        )
        ref = old_ref if stage == "before" else new_ref
        (reports / f"cells_{stage}.tsv").write_text(
            f"instance\tref\nu/repair\t{ref}\n"
        )
        (reports / f"pin_net_{stage}.tsv").write_text(
            "pin\tnets\nu/repair/A\tn1\nu/repair/ZN\tn2\n"
        )
    (reports / "constraint_before.sdc").write_text("create_clock -period 8 clk\n")
    (reports / "constraint_after.sdc").write_text("create_clock -period 8 clk\n")
    (reports / "target_paths_before.tsv").write_text(
        "endpoint\tcell_delay_ns\tnet_delay_ns\tslack_ns\n"
        f"{target}\t7.0\t1.0\t-0.060\n"
    )
    (reports / "setup_after_resize.rpt").write_text(_timing_report(full_after))
    (reports / "target_setup_after_resize.rpt").write_text(
        _timing_report({target: 0.010})
    )


def _fixture(root: Path, spec: dict) -> tuple[Path, dict]:
    case = root / spec["id"]
    (case / "violating.enc.dat").mkdir(parents=True)
    (case / "violating.enc").write_text("source violating.enc.dat/top.db\n")
    (case / "violating.enc.dat/top.db").write_bytes(b"db")
    instance = "u/repair"
    new_ref = "NAND2_X2M_A9TR40"
    old_ref = (
        "NAND2_X0P7M_A9TR40"
        if spec["injection_profile"] == "I0"
        else "NAND2_X1M_A9TR40"
    )
    fix = (
        "setEcoMode -batchMode true\n"
        f"ecoChangeCell -inst {{{instance}}} -cell {{{new_ref}}}\n"
        "setEcoMode -batchMode false\n"
        "refinePlace -eco true\n"
    )
    (case / "fix.tcl").write_text(fix)
    (case / "instruction.txt").write_text("请修复可观察的 setup 违例。\n")
    (case / "answer.txt").write_text(f"诊断后采用等价 RVT resize。\n\n```tcl\n{fix}```\n")
    target = "u/reg/D"
    checkpoint_hash = hashlib.sha256((case / "violating.enc").read_bytes()).hexdigest()
    checkpoint_tree = batch100.checkpoint_archive.checkpoint_tree_sha256_v2(
        case / "violating.enc.dat", baseline_prefix=batch100.GUEST_BASELINE
    )
    checkpoint_bundle_hash = (
        batch100.checkpoint_archive.checkpoint_bundle_sha256_v1_from_hashes(
            checkpoint_hash,
            checkpoint_tree,
        )
    )
    fix_hash = hashlib.sha256(fix.encode()).hexdigest()
    evidence_root = case / "evidence"
    injection = evidence_root / "injection"
    (injection / "reports").mkdir(parents=True)
    (injection / "reports" / "injection_marker.rpt").write_text("injection\n")
    (injection / "innovus.log").write_text("Cadence Innovus 21.10-p004_1\n")
    if spec["injection_profile"] == "I0":
        injection_operations = [
            {
                "kind": "injection",
                "instance": instance,
                "old_ref": new_ref,
                "new_ref": old_ref,
            }
        ]
    else:
        injection_operations = [
            {
                "kind": "injection",
                "instance": "u/inject",
                "old_ref": "NOR2_X1M_A9TR40",
                "new_ref": "NOR2_X0P7M_A9TR40",
            }
        ]
    repair_operations = [
        {
            "kind": "repair",
            "instance": instance,
            "old_ref": old_ref,
            "new_ref": new_ref,
        }
    ]
    calibration_plan = {
        "schema_version": "mock_lef_batch100.calibration_plan.v1",
        "case_id": spec["id"],
        "slot": "slot0",
        "attempt": 1,
        "binding_sha256": "1" * 64,
        "calibration_tcl_sha256": "2" * 64,
        "config_sha256": "3" * 64,
        "selected_candidate_index": 0,
        "selected_calibration_evidence": "candidate_00/process_001",
        "calibration_tree_sha256": "4" * 64,
        "fix_sha256": fix_hash,
        "injection_operations": injection_operations,
        "repair_operations": repair_operations,
    }
    plan_path = injection / "calibration_plan.json"
    plan_path.write_text(json.dumps(calibration_plan))
    plan_sha256 = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    replay_records = []
    for index in (1, 2):
        replay_root = evidence_root / f"replay_{index}"
        replay_log = replay_root / "innovus.log"
        replay_log.parent.mkdir(parents=True)
        replay_log.write_text(
            f"Cadence Innovus 21.10-p004_1\nB100_PROCESS_ID {index}00\n"
        )
        reports = replay_root / "reports"
        _write_raw_replay_reports(reports, target, old_ref, new_ref)
        replay_records.append(
            evidence.replay_record(
                case_id=spec["id"],
                slot=f"slot{index - 1}",
                process_id=f"slot{index - 1}:pid{index}00",
                log_path=replay_log,
                reports=reports,
                targets=[target],
                protected=[],
                fix_sha256=fix_hash,
                checkpoint_sha256=checkpoint_hash,
                checkpoint_tree_sha256=checkpoint_tree,
                checkpoint_tree_hash_algorithm=(
                    batch100.checkpoint_archive.CHECKPOINT_TREE_HASH_ALGORITHM
                ),
                checkpoint_bundle_sha256=checkpoint_bundle_hash,
                checkpoint_bundle_hash_algorithm=(
                    batch100.checkpoint_archive.CHECKPOINT_BUNDLE_HASH_ALGORITHM
                ),
                repair_operations=[
                    {"instance": instance, "new_ref": new_ref}
                ],
                sensitivity={instance: {target: 0.002}},
                functional_pairs={(old_ref, new_ref): True},
            )
        )
    injected = [operation["instance"] for operation in injection_operations]
    validation = {
        "schema_version": "mock_lef_batch100.validation.v2",
        "case_id": spec["id"],
        "status": "VALIDATED",
        "validation_class": "validated_innovus_dual_replay",
        "signoff_qualified": False,
        "technology_classification": "mock_training_non_signoff",
        "fix_sha256": fix_hash,
        "violating_checkpoint_tree_hash_algorithm": (
            batch100.checkpoint_archive.CHECKPOINT_TREE_HASH_ALGORITHM
        ),
        "violating_checkpoint_bundle_sha256": checkpoint_bundle_hash,
        "violating_checkpoint_bundle_hash_algorithm": (
            batch100.checkpoint_archive.CHECKPOINT_BUNDLE_HASH_ALGORITHM
        ),
        "calibration_plan_sha256": plan_sha256,
        "hidden_injection_audit": {"injection_instances": injected},
        "injection_raw_evidence": evidence.raw_evidence_binding(
            injection / "innovus.log", injection / "reports"
        ),
        "injection_fingerprint_sha256": batch100._operation_fingerprint(
            injection_operations
        ),
        "repair_fingerprint_sha256": batch100._operation_fingerprint(
            repair_operations
        ),
        "replays": replay_records,
    }
    (case / "validation.json").write_text(json.dumps(validation))
    first = replay_records[0]
    (case / "metrics.json").write_text(
        json.dumps(
            {
                "schema_version": "mock_lef_batch100.metrics.v1",
                "case_id": spec["id"],
                "status": "PASS",
                "fix_sha256": fix_hash,
                "setup_wns_before_ns": first["before"]["setup_wns_ns"],
                "setup_wns_after_resize_ns": first["after_resize"]["setup_wns_ns"],
                "setup_wns_after_legalize_ns": first["after_legalize"][
                    "setup_wns_ns"
                ],
                "setup_tns_after_legalize_ns": first["after_legalize"][
                    "setup_tns_ns"
                ],
                "hold_before_ns": first["before"]["hold_wns_ns"],
                "hold_after_ns": first["after_legalize"]["hold_wns_ns"],
                "hold_gate_applied": False,
            }
        )
    )
    return case, validation


class FinalizerNegativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.specs = json.loads(catalog.SPECS.read_text())
        cls.acceptance = cls.specs["acceptance"]

    def _mutated(self, mutation, *, case_index: int = 0) -> None:
        spec = copy.deepcopy(self.specs["cases"][case_index])
        # Keep the fixture compact while testing finalizer semantics.
        spec["target_endpoint_count"] = 1
        spec["protected_endpoint_count"] = 0
        spec["expected_modification_count"] = 1
        spec["nominal_severity"] = {
            "ps": 60,
            "tolerance_ps": 10,
            "percent_of_clock": 0.75,
            "target_wns_ns": -0.06,
        }
        with tempfile.TemporaryDirectory() as temporary:
            case, validation = _fixture(Path(temporary), spec)
            mutation(case, validation, spec)
            (case / "validation.json").write_text(json.dumps(validation))
            with self.assertRaises(BatchError):
                finalize_batch.validate_case(case, spec, self.acceptance)

    @staticmethod
    def _rewrite_plan(case: Path, validation: dict, mutation) -> None:
        path = case / "evidence" / "injection" / "calibration_plan.json"
        plan = json.loads(path.read_text())
        mutation(plan)
        path.write_text(json.dumps(plan))
        validation["calibration_plan_sha256"] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        validation["injection_fingerprint_sha256"] = (
            batch100._operation_fingerprint(plan["injection_operations"])
        )
        validation["repair_fingerprint_sha256"] = (
            batch100._operation_fingerprint(plan["repair_operations"])
        )
        validation["hidden_injection_audit"]["injection_instances"] = [
            operation["instance"] for operation in plan["injection_operations"]
        ]

    def test_accepts_complete_fixture(self) -> None:
        spec = copy.deepcopy(self.specs["cases"][0])
        with tempfile.TemporaryDirectory() as temporary:
            case, _ = _fixture(Path(temporary), spec)
            summary = finalize_batch.validate_case(case, spec, self.acceptance)
            self.assertEqual(summary["case_id"], spec["id"])
            manifest = finalize_batch._case_manifest(case, summary)
            for name in (
                "calibration_plan_sha256",
                "injection_fingerprint_sha256",
                "repair_fingerprint_sha256",
            ):
                self.assertEqual(manifest[name], summary[name])

    def test_rejects_missing_replay_and_hold_gate(self) -> None:
        self._mutated(lambda c, v, s: v["replays"].pop())
        self._mutated(lambda c, v, s: v["replays"][0].update(hold_gate_applied=True))

    def test_rejects_checkpoint_bundle_hash_or_algorithm_mismatch(self) -> None:
        self._mutated(
            lambda c, v, s: v.update(
                violating_checkpoint_bundle_sha256="0" * 64
            )
        )
        self._mutated(
            lambda c, v, s: v.update(
                violating_checkpoint_bundle_hash_algorithm="sha256-tar-v0"
            )
        )
        self._mutated(
            lambda c, v, s: v["replays"][0].update(
                violating_checkpoint_bundle_sha256="1" * 64
            )
        )
        self._mutated(
            lambda c, v, s: v["replays"][0].update(
                violating_checkpoint_bundle_hash_algorithm="sha256-tar-v0"
            )
        )

    def test_rejects_metrics_schema_hold_gate_and_numeric_mismatch(self) -> None:
        def mutate_metrics(case, key, value):
            path = case / "metrics.json"
            metrics = json.loads(path.read_text())
            metrics[key] = value
            path.write_text(json.dumps(metrics))

        self._mutated(
            lambda c, v, s: mutate_metrics(c, "schema_version", "metrics.v0")
        )
        self._mutated(
            lambda c, v, s: mutate_metrics(c, "hold_gate_applied", True)
        )
        for name in (
            "setup_wns_before_ns",
            "setup_wns_after_resize_ns",
            "setup_wns_after_legalize_ns",
            "setup_tns_after_legalize_ns",
            "hold_before_ns",
            "hold_after_ns",
        ):
            self._mutated(
                lambda c, v, s, metric=name: mutate_metrics(c, metric, 123.0)
            )

    def test_rejects_nested_checkpoint_and_transport_residue(self) -> None:
        def nested_file(case, relative):
            path = case / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"residue")

        self._mutated(
            lambda c, v, s: nested_file(c, "evidence/replay_1/after.enc")
        )
        self._mutated(
            lambda c, v, s: (
                c / "evidence" / "replay_2" / "fixed.enc.dat"
            ).mkdir()
        )
        self._mutated(
            lambda c, v, s: nested_file(c, "evidence/injection/transport.tar")
        )
        self._mutated(
            lambda c, v, s: nested_file(c, "evidence/replay_1/transport.tgz")
        )

    def test_rejects_missing_or_tampered_raw_evidence(self) -> None:
        self._mutated(
            lambda case, validation, spec: (
                case / "evidence" / "injection" / "innovus.log"
            ).unlink()
        )
        self._mutated(
            lambda case, validation, spec: (
                case / "evidence" / "replay_1" / "innovus.log"
            ).unlink()
        )
        self._mutated(
            lambda case, validation, spec: (
                case
                / "evidence"
                / "replay_2"
                / "reports"
                / "setup_after_resize.rpt"
            ).unlink()
        )

        def tamper_report(case, validation, spec):
            replay_root = case / "evidence" / "replay_1"
            path = (
                case
                / "evidence"
                / "replay_1"
                / "reports"
                / "setup_before.rpt"
            )
            path.write_text(path.read_text().replace("-0.060000000", "-0.058000000"))
            validation["replays"][0]["raw_evidence"] = evidence.raw_evidence_binding(
                replay_root / "innovus.log", replay_root / "reports"
            )

        self._mutated(tamper_report)

    def test_rejects_missing_tampered_plan_and_fingerprint_claims(self) -> None:
        self._mutated(
            lambda case, validation, spec: (
                case / "evidence" / "injection" / "calibration_plan.json"
            ).unlink()
        )

        def tamper_plan_without_rebinding(case, validation, spec):
            path = (
                case / "evidence" / "injection" / "calibration_plan.json"
            )
            plan = json.loads(path.read_text())
            plan["slot"] = "slot9"
            path.write_text(json.dumps(plan))

        self._mutated(tamper_plan_without_rebinding)
        self._mutated(
            lambda case, validation, spec: validation.update(
                injection_fingerprint_sha256="0" * 64
            )
        )
        self._mutated(
            lambda case, validation, spec: validation.update(
                repair_fingerprint_sha256="0" * 64
            )
        )

    def test_rejects_plan_ref_fix_and_replay_diff_tampering(self) -> None:
        def break_i0_inverse(case, validation, spec):
            self._rewrite_plan(
                case,
                validation,
                lambda plan: plan["injection_operations"][0].update(
                    old_ref="NAND2_X4M_A9TR40"
                ),
            )

        self._mutated(break_i0_inverse)

        def make_iaid_ref_inverse(case, validation, spec):
            def mutate(plan):
                repair = plan["repair_operations"][0]
                plan["injection_operations"][0].update(
                    old_ref=repair["new_ref"],
                    new_ref=repair["old_ref"],
                )

            self._rewrite_plan(case, validation, mutate)

        self._mutated(make_iaid_ref_inverse, case_index=1)

        def diverge_fix_from_plan(case, validation, spec):
            self._rewrite_plan(
                case,
                validation,
                lambda plan: plan["repair_operations"][0].update(
                    new_ref="NAND2_X4M_A9TR40"
                ),
            )

        self._mutated(diverge_fix_from_plan, case_index=1)

        def diverge_replay_old_ref_from_plan(case, validation, spec):
            self._rewrite_plan(
                case,
                validation,
                lambda plan: plan["repair_operations"][0].update(
                    old_ref="NAND2_X1P4M_A9TR40"
                ),
            )

        self._mutated(diverge_replay_old_ref_from_plan, case_index=1)

    def test_rejects_iaid_set_policy_from_frozen_plan(self) -> None:
        def remove_repair_outside_injection(case, validation, spec):
            def mutate(plan):
                plan["injection_operations"][0]["instance"] = "u/repair"

            self._rewrite_plan(case, validation, mutate)

        self._mutated(remove_repair_outside_injection, case_index=1)

        def restore_every_injected_instance(case, validation, spec):
            def mutate(plan):
                injection = plan["injection_operations"][0]
                injection["instance"] = "u/repair"
                injection["old_ref"] = plan["repair_operations"][0]["new_ref"]
                injection["new_ref"] = plan["repair_operations"][0]["old_ref"]

            self._rewrite_plan(case, validation, mutate)

        self._mutated(restore_every_injected_instance, case_index=1)

    def test_rejects_tampered_full_slack_ledger(self) -> None:
        def tamper(case, validation, spec):
            ledger = validation["replays"][0]["checkpoint_portability_slacks"]
            ledger["slacks_ns"]["u/reg/D"] = -0.058

        self._mutated(tamper)

    def test_rejects_extra_endpoint_and_drv_drc_regression(self) -> None:
        def extra(case, validation, spec):
            validation["replays"][0]["before"]["negative_endpoints"].append("u/extra/D")
        self._mutated(extra)
        self._mutated(
            lambda c, v, s: v["replays"][0]["after_legalize"]["drv"].update(
                max_transition=2
            )
        )
        self._mutated(
            lambda c, v, s: v["replays"][0]["after_legalize"].update(drc_count=11)
        )

    def test_rejects_filler_resize_direct_inverse_and_constraint_change(self) -> None:
        self._mutated(
            lambda c, v, s: v["replays"][0]["cell_diff"][0].update(
                old_ref=v["replays"][0]["cell_diff"][0]["new_ref"]
            )
        )
        self._mutated(
            lambda c, v, s: v["hidden_injection_audit"].update(
                injection_instances=["u/repair"]
            ),
            case_index=1,
        )
        self._mutated(
            lambda c, v, s: v["replays"][0]["after_legalize"].update(
                constraint_sha256="8" * 64
            )
        )

    def test_rejects_topology_equivalence_forbidden_tcl_and_checkpoint_residue(self) -> None:
        self._mutated(
            lambda c, v, s: v["replays"][0]["after_legalize"].update(
                topology_sha256="8" * 64
            )
        )
        def forbidden(case, validation, spec):
            (case / "fix.tcl").write_text(
                (case / "fix.tcl").read_text().replace(
                    "refinePlace -eco true", "ecoRoute -target"
                )
            )
        self._mutated(forbidden)
        self._mutated(
            lambda c, v, s: v["replays"][0].update(
                functional_equivalence_pass=False
            )
        )
        def residue(case, validation, spec):
            (case / "fixed.enc").write_text("bad")
        self._mutated(residue)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import re
import unittest
from pathlib import Path


RUNTIME = (
    Path(__file__).resolve().parents[1]
    / "pilot_10"
    / "templates"
    / "pilot_runtime.tcl"
)


class PilotRuntimeInnovusContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = RUNTIME.read_text(encoding="utf-8")

    def test_concrete_fix_closes_batch_before_physical_closeout(self) -> None:
        body = re.search(
            r"proc ::sft::write_concrete_fix \{\} \{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(body)
        text = body.group("body")
        self.assertIn('if {$::SFT_CASE(repair_mode) eq "surgical"}', text)
        enter = text.index("puts $stream [list setEcoMode -batchMode true]")
        exit_batch = text.index("puts $stream [list setEcoMode -batchMode false]")
        refine = text.index("puts $stream [list refinePlace -eco true]")
        route = text.index("puts $stream [list ecoRoute -target]")
        self.assertLess(enter, exit_batch)
        self.assertLess(exit_batch, refine)
        self.assertLess(refine, route)

    def test_paired_inverter_fix_uses_one_minimal_leq_window(self) -> None:
        body = re.search(
            r"proc ::sft::write_concrete_fix \{\} \{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(body)
        text = body.group("body")
        batch_enter = text.index(
            "puts $stream [list setEcoMode -batchMode true]"
        )
        leq_disable = text.index(
            "puts $stream [list setEcoMode -LEQCheck false]"
        )
        actions = text.index("foreach command $repair_commands")
        batch_exit = text.index(
            "puts $stream [list setEcoMode -batchMode false]"
        )
        leq_restore = text.index(
            "puts $stream [list setEcoMode -LEQCheck true]"
        )
        refine = text.index("puts $stream [list refinePlace -eco true]")
        ordered = [
            batch_enter,
            leq_disable,
            actions,
            batch_exit,
            leq_restore,
            refine,
        ]
        self.assertEqual(ordered, sorted(ordered))
        self.assertEqual(len(ordered), len(set(ordered)))
        self.assertEqual(text.count("setEcoMode -LEQCheck false"), 1)
        self.assertEqual(text.count("setEcoMode -LEQCheck true"), 1)
        self.assertIn(
            "::sft::validate_paired_inverter_functional_proofs", text
        )

    def test_paired_inverter_fix_uses_probe_proven_local_drc_closeout(self) -> None:
        body = re.search(
            r"proc ::sft::write_concrete_fix \{\} \{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(body)
        text = body.group("body")
        refine = text.index("puts $stream [list refinePlace -eco true]")
        target = text.index("puts $stream [list ecoRoute -target]")
        disable = text.rindex(
            "puts $stream [list setNanoRouteMode -routeWithTimingDriven false]"
        )
        fix_drc = text.index(
            "puts $stream [list ecoRoute -fix_drc "
            "{907.30 263.40 914.90 270.50}]"
        )
        restore = text.rindex(
            "puts $stream [list setNanoRouteMode -routeWithTimingDriven true]"
        )
        self.assertEqual([refine, target, disable, fix_drc, restore], sorted(
            [refine, target, disable, fix_drc, restore]
        ))
        self.assertEqual(text.count("puts $stream [list ecoRoute"), 2)
        self.assertEqual(text.count("-routeWithTimingDriven false"), 1)
        self.assertEqual(text.count("-routeWithTimingDriven true"), 1)
        self.assertGreaterEqual(
            text.count("if {[::sft::uses_paired_inverter_repair]}"), 3
        )

    def test_hold002_fix_uses_probe_proven_fixed_repeater_location(self) -> None:
        planner = re.search(
            r"proc ::sft::plan_add_repeater \{term reference tag reason\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        writer = re.search(
            r"proc ::sft::write_concrete_fix \{\} \{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(planner)
        self.assertIsNotNone(writer)
        planner_text = planner.group("body")
        for marker in (
            "::sft::uses_hold002_fixed_repeater_location",
            'set location {898.00 248.08}',
            "lappend command -loc $location",
            "dict set operation loc $location",
            'if {$term ne "mac_out_nan_reg/D"',
            '$reference ne "DLY4_X0P5M_A9TR40"',
        ):
            self.assertIn(marker, planner_text)
        writer_text = writer.group("body")
        self.assertIn(
            "::sft::validate_hold002_fixed_repeater_target", writer_text
        )
        self.assertNotIn("uses_hold002_nontiming_route", self.text)
        self.assertNotIn("validate_hold002_nontiming_route_target", self.text)

    def test_concrete_fix_policy_requires_one_exact_ordered_closeout(self) -> None:
        body = re.search(
            r"proc ::sft::validate_concrete_fix_policy \{path\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(body)
        text = body.group("body")
        for marker in (
            "set refine_commands 0",
            "incr refine_commands",
            '[lindex $line 1] ne "-eco"',
            '[lindex $line 2] ne "true"',
            "set route_commands 0",
            "incr route_commands",
            "set target_route_commands 0",
            "incr target_route_commands",
            "set fix_drc_route_commands 0",
            "incr fix_drc_route_commands",
            "if {$refine_commands != 1}",
            "$last_action_index < $refine_index",
            "$refine_index < $target_route_index",
        ):
            self.assertIn(marker, text)

    def test_concrete_fix_policy_enforces_exact_case_local_leq_window(self) -> None:
        body = re.search(
            r"proc ::sft::validate_concrete_fix_policy \{path\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(body)
        text = body.group("body")
        for marker in (
            '[llength $line] != 3 || [lindex $line 2] ni {true false}',
            '$option eq "-LEQCheck"',
            "if {![::sft::uses_paired_inverter_repair]}",
            "$leq_modes ne {false true}",
            "$batch_enter_index < $leq_disable_index",
            "$leq_disable_index < $first_action_index",
            "$last_action_index < $batch_exit_index",
            "$batch_exit_index < $leq_restore_index",
            "$leq_restore_index < $refine_index",
            'elseif {[llength $leq_modes] != 0}',
            'non-paired repair must not change LEQCheck',
        ):
            self.assertIn(marker, text)
        self.assertIn(
            "[::sft::uses_paired_inverter_repair] && !$leq_disabled",
            text,
        )

    def test_concrete_fix_policy_accepts_only_probe_proven_paired_drc_window(self) -> None:
        body = re.search(
            r"proc ::sft::validate_concrete_fix_policy \{path\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(body)
        text = body.group("body")
        for marker in (
            "set timing_driven_modes {}",
            '[lindex $line 1] ne "-routeWithTimingDriven"',
            "if {![::sft::uses_paired_inverter_repair]}",
            "set paired_drc_fix_bbox {907.30 263.40 914.90 270.50}",
            '[lindex $line 1] eq "-target"',
            '[lindex $line 1] eq "-fix_drc"',
            '[lindex $line 2] eq $paired_drc_fix_bbox',
            "$route_commands != 2",
            "$target_route_commands != 1",
            "$fix_drc_route_commands != 1",
            "$timing_driven_modes ne {false true}",
            "$target_route_index != $refine_index + 1",
            "$timing_driven_disable_index != $target_route_index + 1",
            "$fix_drc_route_index != $timing_driven_disable_index + 1",
            "$timing_driven_restore_index != $fix_drc_route_index + 1",
            "paired-inverter target route must retain timing-driven routing",
            "paired-inverter local DRC repair must execute inside its routeWithTimingDriven-disabled window",
            "ordinary repair requires one target route and must not use fix_drc or change routeWithTimingDriven",
        ):
            self.assertIn(marker, text)
        self.assertIn(
            "[llength $timing_driven_modes] != 0",
            text,
        )

    def test_hold002_fixed_location_profile_is_case_and_target_isolated(self) -> None:
        selector = re.search(
            r"proc ::sft::uses_hold002_fixed_repeater_location \{\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        target = re.search(
            r"proc ::sft::validate_hold002_fixed_repeater_target \{\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(selector)
        self.assertIsNotNone(target)
        selector_text = selector.group("body")
        for marker in (
            '$::SFT_CASE(id) eq "HOLD_002"',
            '$::SFT_CASE(type) eq "hold"',
            '$::SFT_CASE(repair_mode) eq "surgical"',
            '$::SFT_CASE(injection_strategy) eq "local_capture_clock_delay"',
            '$::SFT_CASE(repair_strategy) eq "insert_delay_and_downsize"',
            '$::SFT_FROZEN_CLOCK_CELL eq "DLYCLK8S6_X1B_A9TR40"',
            '$::SFT_REPAIR_DELAY_CELL eq "DLY4_X0P5M_A9TR40"',
            '[dict get $selector stable_rank] == 487',
        ):
            self.assertIn(marker, selector_text)
        target_text = target.group("body")
        for marker in (
            "endpoint mac_out_nan_reg/D",
            "net FE_OFN16864_pp_nan_pvld_d2_0",
            "driver_pin FE_OFC8281_pp_nan_pvld_d2_0/Y",
            "driver_inst FE_OFC8281_pp_nan_pvld_d2_0",
            "driver_ref BUF_X1B_A9TR40",
            "target fingerprint mismatch",
        ):
            self.assertIn(marker, target_text)
        policy = re.search(
            r"proc ::sft::validate_concrete_fix_policy \{path\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(policy)
        policy_text = policy.group("body")
        for marker in (
            "set hold002_fixed_repeater_commands 0",
            "incr hold002_fixed_repeater_commands",
            "-name SFT_ECO_HOLD_002_HOLD_1",
            "-loc {898.00 248.08}",
            "$hold002_fixed_repeater_commands != 1",
            "exactly one probe-proven fixed-location repeater",
        ):
            self.assertIn(marker, policy_text)

    def test_hold003_fixed_locations_are_case_target_and_action_isolated(self) -> None:
        selector = re.search(
            r"proc ::sft::uses_hold003_fixed_repeater_locations \{\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        target = re.search(
            r"proc ::sft::validate_hold003_fixed_repeater_target \{\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(selector)
        self.assertIsNotNone(target)
        selector_text = selector.group("body")
        for marker in (
            '$::SFT_CASE(id) eq "HOLD_003"',
            '$::SFT_CASE(type) eq "hold"',
            '$::SFT_CASE(difficulty) eq "hard"',
            '$::SFT_CASE(repair_strategy) eq "insert_data_delay"',
            '$::SFT_CASE(max_eco_cells) == 2',
            '$::SFT_CASE(repair_delay_cells_per_endpoint) == 2',
            '$::SFT_FROZEN_CLOCK_CELL eq "DLYCLK8S8_X1B_A9TR40"',
            '[dict get $selector stable_rank] == 142',
        ):
            self.assertIn(marker, selector_text)
        target_text = target.group("body")
        for marker in (
            "endpoint pp_nan_mts_d2_reg_8_/D",
            "net n3086",
            "driver_pin U28144/Y",
            "driver_inst U28144",
            "driver_ref OAI21_X0P5M_A9TR40",
            "beginpoint pp_nan_mts_d1_reg_8_/CK",
            "target fingerprint mismatch",
        ):
            self.assertIn(marker, target_text)

        planner = re.search(
            r"proc ::sft::plan_add_repeater \{term reference tag reason\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(planner)
        planner_text = planner.group("body")
        for marker in (
            "::sft::uses_hold003_fixed_repeater_locations",
            "SFT_ECO_HOLD_003_HOLD_1 {914.15 263.20}",
            "SFT_ECO_HOLD_003_HOLD_2 {915.86 263.20}",
            'if {$term ne "pp_nan_mts_d2_reg_8_/D"',
            "lappend command -loc $location",
            "dict set operation loc $location",
        ):
            self.assertIn(marker, planner_text)

        policy = re.search(
            r"proc ::sft::validate_concrete_fix_policy \{path\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(policy)
        policy_text = policy.group("body")
        for marker in (
            "set hold003_fixed_repeater_names {}",
            "lappend hold003_fixed_repeater_names $name",
            "SFT_ECO_HOLD_003_HOLD_1 {914.15 263.20}",
            "SFT_ECO_HOLD_003_HOLD_2 {915.86 263.20}",
            "[lsort -dictionary $hold003_fixed_repeater_names] ne",
            "exactly its two probe-proven fixed-location repeaters",
        ):
            self.assertIn(marker, policy_text)

    def test_mixed001_strong_restore_is_case_target_and_action_isolated(self) -> None:
        selector = re.search(
            r"proc ::sft::uses_mixed001_strong_restore_buffer \{\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        target = re.search(
            r"proc ::sft::validate_mixed001_strong_restore_targets \{\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        chooser = re.search(
            r"proc ::sft::coordinated_restore_buffer \{\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(selector)
        self.assertIsNotNone(target)
        self.assertIsNotNone(chooser)
        selector_text = selector.group("body")
        for marker in (
            '$::SFT_CASE(id) eq "MIXED_001"',
            '$::SFT_CASE(type) eq "mixed"',
            '$::SFT_CASE(repair_strategy) eq "coordinated_setup_hold"',
            '$::SFT_FROZEN_DELAY_CELL eq "DLY2_X4M_A9TR40"',
            '$::SFT_FROZEN_CLOCK_CELL eq "DLYCLK8S6_X1B_A9TR40"',
            '[dict get $setup_selector stable_rank] == 17',
            '[dict get $hold_selector stable_rank] == 319',
        ):
            self.assertIn(marker, selector_text)
        target_text = target.group("body")
        for marker in (
            "endpoint u_exp/exp_sft_31_reg_0_/D",
            "driver_inst u_exp/U4457",
            "endpoint u_exp/exp_sft_23_reg_0_/D",
            "driver_inst u_exp/U4528",
            "endpoint pp_exp_d2_reg_2_/D",
            "driver_inst U26729",
            "target fingerprint mismatch",
        ):
            self.assertIn(marker, target_text)
        chooser_text = chooser.group("body")
        self.assertIn("::sft::validate_mixed001_strong_restore_targets", chooser_text)
        self.assertIn("set buffer BUF_X2M_A9TR40", chooser_text)
        self.assertIn("BUF_X2M_A9TR40", chooser_text)
        self.assertIn(
            "[::sft::coordinated_restore_buffer]",
            re.search(
                r"proc ::sft::write_concrete_fix \{\} \{(?P<body>.*?)\n\}",
                self.text,
                re.DOTALL,
            ).group("body"),
        )

        policy = re.search(
            r"proc ::sft::validate_concrete_fix_policy \{path\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(policy)
        policy_text = policy.group("body")
        for marker in (
            "set mixed001_strong_restore_commands 0",
            "incr mixed001_strong_restore_commands",
            "u_exp/SFT_ECO_MIXED_001_PATH_4",
            'ne "BUF_X2M_A9TR40"',
            "$mixed001_strong_restore_commands != 4",
            "exactly four strong restore-buffer replacements",
        ):
            self.assertIn(marker, policy_text)

    def test_native_fix_uses_exact_terms_file_and_never_gui_selection_or_batch(self) -> None:
        planner = re.search(
            r"proc ::sft::plan_native_selected_terms \{mode\} \{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(planner)
        text = planner.group("body")
        self.assertIn("set relative_path [file join reports native_selected_terms.txt]", text)
        self.assertIn(
            "[lsort -command ::sft::compare_diagnostic_targets", text
        )
        self.assertIn("puts -nonewline $stream $expected_text", text)
        self.assertIn("-selectedTerms $relative_path -incr", text)
        self.assertNotIn("selectPin", text)
        self.assertNotIn("deselectAll", text)
        self.assertNotIn("setEcoMode", text)
        self.assertNotIn("proc ::sft::assert_native_selection", self.text)

    def test_native_locality_unions_pre_and_post_optimization_fanin(self) -> None:
        planner = re.search(
            r"proc ::sft::plan_native_selected_terms \{mode\} \{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        validator = re.search(
            r"proc ::sft::validate_native_cell_budget \{\} \{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(planner)
        self.assertIsNotNone(validator)
        self.assertIn(
            "set native_allowed_before [::sft::native_selected_fanin_cells]",
            planner.group("body"),
        )
        text = validator.group("body")
        self.assertIn("set allowed_after [::sft::native_selected_fanin_cells]", text)
        self.assertIn(
            "[concat $native_allowed_before $allowed_after]", text
        )
        self.assertIn("selected_fanin_before_after_union", text)

    def test_calibration_validates_repair_feasibility_before_return(self) -> None:
        body = re.search(
            r"proc ::sft::validate_resolved_action_feasibility \{\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(body)
        text = body.group("body")
        self.assertLess(
            text.index("set strategy $::SFT_CASE(repair_strategy)"),
            text.index("if {[::sft::calibration_only]}")
        )
        self.assertIn("SFT_CALIBRATION_INJECTION_AND_REPAIR_ACTIONS_FEASIBLE", text)

    def test_injection_closeout_matches_targeted_repair_routing(self) -> None:
        body = re.search(
            r"proc ::sft::legalize_and_route \{\} \{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(body)
        text = body.group("body")
        self.assertIn("setEcoMode -batchMode false", text)
        self.assertNotIn("setEcoMode -batchMode true", text)
        self.assertIn("if {[catch {ecoRoute -target} message]}", text)
        self.assertNotIn("catch {ecoRoute} message", text)
        self.assertLess(
            text.index("setEcoMode -batchMode false"),
            text.index("refinePlace -eco true"),
        )
        self.assertLess(
            text.index("refinePlace -eco true"),
            text.index("ecoRoute -target"),
        )

    def test_zero_step_setup_profile_skips_driver_mutations(self) -> None:
        for proc_name in {
            "validate_resolved_action_feasibility",
            "write_concrete_fix",
        }:
            body = re.search(
                rf"proc ::sft::{proc_name} \{{\}} \{{(?P<body>.*?)\n\}}",
                self.text,
                re.DOTALL,
            )
            self.assertIsNotNone(body)
            text = body.group("body")
            self.assertIn("if {$driver_steps > 0}", text)

    def test_setup_004_paired_inverter_profile_is_case_isolated(self) -> None:
        selector = re.search(
            r"proc ::sft::uses_paired_inverter_repair \{\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        body = re.search(
            r"proc ::sft::replace_delay_and_upsize_profile \{\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(selector)
        self.assertIsNotNone(body)
        selector_text = selector.group("body")
        for marker in (
            "if {![info exists ::SFT_CASE($key)]} { return 0 }",
            "if {![info exists ::SFT_FROZEN_DELAY_CELL]} { return 0 }",
            '$::SFT_CASE(id) eq "SETUP_004"',
            '$::SFT_CASE(type) eq "setup"',
            '$::SFT_CASE(repair_mode) eq "surgical"',
            '$::SFT_CASE(injection_strategy) eq "insert_data_delay"',
            '$::SFT_CASE(calibration_status) eq "FROZEN"',
            '$::SFT_FROZEN_DELAY_CELL eq "DLY4_X4M_A9TR40"',
            "$::SFT_CASE(setup_delay_cells) == 2",
            '$::SFT_CASE(repair_strategy) eq "replace_delay_and_upsize"',
        ):
            self.assertIn(marker, selector_text)
        text = body.group("body")
        self.assertIn("[::sft::uses_paired_inverter_repair]", text)
        self.assertIn("$paired_setup_inverter", text)
        isolated = text.index("[::sft::uses_paired_inverter_repair]")
        zero_profile = text.index(
            "return [dict create driver_steps 0 buffer $paired_setup_inverter]"
        )
        fallback = text.index(
            "return [dict create driver_steps 3 buffer $strong_setup_buffer]"
        )
        self.assertLess(isolated, zero_profile)
        self.assertLess(zero_profile, fallback)

    def test_paired_inverter_planner_requires_exclusive_serial_topology(self) -> None:
        topology = re.search(
            r"proc ::sft::exclusive_injected_serial_pair \{term instances\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        planner = re.search(
            r"proc ::sft::plan_paired_inverting_delay_replacements "
            r"\{inverter\} \{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(topology)
        self.assertIsNotNone(planner)
        topology_text = topology.group("body")
        for marker in (
            "exactly two unique injected cells",
            "[::sft::net_driver_instance $term]",
            "[::sft::single_input_pin $downstream]",
            "[llength $pins] != 2",
            "get_ports -quiet -of_objects $internal_net",
            "exclusive two-pin topology is required",
        ):
            self.assertIn(marker, topology_text)
        planner_text = planner.group("body")
        for marker in (
            "::sft::exclusive_injected_serial_pair",
            "::sft::assert_noninverting_repeater",
            "::sft::assert_inverting_repeater",
            "proof paired_double_inversion",
            "composed_function noninverting",
            "exclusive_internal_net true",
        ):
            self.assertIn(marker, planner_text)

    def test_paired_inverter_audit_binds_each_swap_to_one_topology_proof(self) -> None:
        validator = re.search(
            r"proc ::sft::validate_paired_inverter_functional_proofs \{\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(validator)
        text = validator.group("body")
        for marker in (
            "[::sft::uses_paired_inverter_repair]",
            '[dict get $operation kind] ne "ecoChangeCell"',
            "{inst from to reason proof pair_term pair_mate}",
            '[dict get $operation proof] ne "paired_double_inversion"',
            "dict lappend operations_by_term $term $operation",
            "[lsort [dict keys $operations_by_term]] ne $expected_terms",
            "dict lappend proofs_by_term [dict get $proof term] $proof",
            "[lsort [dict keys $proofs_by_term]] ne $expected_terms",
            "[llength $operations] != 2 || [llength $proofs] != 1",
            "[lsort [list $instance $mate]] ne $instances",
            "$proof_instances ne $instances",
            '[dict get $proof composed_function] ne "noninverting"',
            '[dict get $proof exclusive_internal_net] ne "true"',
        ):
            self.assertIn(marker, text)

        audit = re.search(
            r"proc ::sft::write_functional_audit \{\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(audit)
        self.assertIn(
            "::sft::validate_paired_inverter_functional_proofs",
            audit.group("body"),
        )

    def test_inverter_proof_accepts_only_explicit_unary_not_forms(self) -> None:
        proof = re.search(
            r"proc ::sft::assert_inverting_repeater \{reference context\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(proof)
        text = proof.group("body")
        self.assertIn('$stripped ni [list "!$input" "~$input"]', text)
        self.assertIn("is not proven inverting", text)

    def test_checkpoint_retains_rc_and_fanin_uses_2110_syntax(self) -> None:
        self.assertIn("saveDesign $checkpoint -rc", self.text)
        self.assertIn("all_fanin -to $pin -only_cells", self.text)
        self.assertNotIn("all_fanin -to $pin -flat", self.text)

    def test_gold_drc_reports_use_and_validate_the_fixed_full_audit_limit(self) -> None:
        self.assertIn("variable drc_report_limit 1000000", self.text)
        self.assertIn("verify_drc -limit 1000000 -report $drc_path", self.text)
        self.assertIn("exactly one -limit $drc_report_limit", self.text)
        self.assertIn("early-termination/truncation signal", self.text)
        self.assertIn("reaches collection limit $drc_report_limit", self.text)

    def test_probe_drc_report_uses_the_same_fixed_limit(self) -> None:
        collector = (
            RUNTIME.parent / "innovus_probe_collect.tcl"
        ).read_text(encoding="utf-8")
        self.assertIn("variable drc_report_limit 1000000", collector)
        self.assertIn("verify_drc -limit 1000000 -report", collector)

    def test_sdc_snapshot_normalizes_only_one_exact_generated_on_header(self) -> None:
        normalizer = re.search(
            r"proc ::sft::normalize_write_sdc_generated_on \{path\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(normalizer)
        text = normalizer.group("body")
        self.assertIn(
            r"^#  Generated on:[ \t]+[^ \t\r][^\r]*$",
            text,
        )
        self.assertIn("if {$header_count != 1}", text)
        self.assertIn("SFT_NORMALIZED_VOLATILE_METADATA", text)
        self.assertIn('-translation binary -encoding binary', text)

        snapshot = re.search(
            r"proc ::sft::write_constraint_snapshot \{stage\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(snapshot)
        body = snapshot.group("body")
        write = body.index("write_sdc -view $view $path")
        nonempty = body.index("![::sft::nonempty_file $path]")
        normalize = body.index("::sft::normalize_write_sdc_generated_on $path")
        self.assertLess(write, nonempty)
        self.assertLess(nonempty, normalize)

    def test_functional_audit_describes_exact_header_normalization(self) -> None:
        self.assertIn(
            "each write_sdc file had exactly one '#  Generated on:' line "
            "replaced by the fixed SFT marker",
            self.text,
        )
        self.assertIn(
            "identical in every remaining raw byte",
            self.text,
        )

    def test_before_timing_metrics_reuse_only_the_injection_measurement(self) -> None:
        injector = re.search(
            r"proc ::sft::apply_injection \{\} \{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(injector)
        text = injector.group("body")
        measurement = text.index("set measurement [::sft::injection_measurement]")
        setup_cache = text.index("set stage_metrics(before,setup)")
        hold_cache = text.index("set stage_metrics(before,hold)")
        provenance = text.index("::sft::record_injection_trial")
        locality = text.index("::sft::write_violation_locality $measurement")
        self.assertLess(measurement, setup_cache)
        self.assertLess(setup_cache, hold_cache)
        self.assertLess(hold_cache, provenance)
        self.assertLess(provenance, locality)

        updater = re.search(
            r"proc ::sft::update_stage_timing_metrics \{stage\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(updater)
        text = updater.group("body")
        self.assertIn('if {$stage eq "before"}', text)
        self.assertIn("before timing metric cache is incomplete", text)
        self.assertIn("[::sft::timing_summary late]", text)
        self.assertIn("[::sft::timing_summary early]", text)
        self.assertLess(text.index('if {$stage eq "before"}'), text.index("timing_summary late"))

    def test_stage_reports_remain_full_and_after_is_never_cached(self) -> None:
        writer = re.search(
            r"proc ::sft::write_stage_reports \{stage\} \{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(writer)
        text = writer.group("body")
        setup_report = text.index("::sft::command_to_file $setup_path $setup_command")
        hold_report = text.index("::sft::command_to_file $hold_path $hold_command")
        metrics = text.index("::sft::update_stage_timing_metrics $stage")
        self.assertLess(setup_report, hold_report)
        self.assertLess(hold_report, metrics)
        self.assertIn("report_timing -late -max_paths 200", text)
        self.assertIn("report_timing -early -max_paths 200", text)

        updater = re.search(
            r"proc ::sft::update_stage_timing_metrics \{stage\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(updater)
        text = updater.group("body")
        before_block = text.index('if {$stage eq "before"}')
        after_measurement = text.index("set stage_metrics($stage,setup)")
        self.assertLess(before_block, after_measurement)
        self.assertNotIn('if {$stage eq "after"}', text)

    def test_locality_reuses_only_complete_negative_endpoint_evidence(self) -> None:
        locality = re.search(
            r"proc ::sft::write_violation_locality \{measurement\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(locality)
        text = locality.group("body")
        negative = text.index("set negative [::sft::negative_endpoint_slacks $mode]")
        selected = text.index(
            "set selected_slacks [::sft::selected_endpoint_slacks $mode $selected $negative]"
        )
        self.assertLess(negative, selected)

        selector = re.search(
            r"proc ::sft::selected_endpoint_slacks "
            r"\{mode endpoints \{negative \{\}\}\} \{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(selector)
        text = selector.group("body")
        self.assertIn("if {[dict exists $negative $endpoint]}", text)
        self.assertIn("set slack [dict get $negative $endpoint]", text)
        self.assertIn("set slack [::sft::exact_endpoint_slack $mode $endpoint]", text)
        self.assertIn("cached negative endpoint slack is invalid", text)

    def test_constant_net_filter_uses_the_innovus_enum_fail_closed(self) -> None:
        self.assertIn("proc ::sft::net_is_constant {net}", self.text)
        self.assertIn("get_db $net .constant", self.text)
        self.assertIn(
            '$classification ni {no_constant none false 0}', self.text
        )
        self.assertIn("cannot determine constant classification", self.text)
        self.assertNotIn(
            "{is_power is_ground is_constant constant}", self.text
        )

    def test_boolean_alias_resolution_is_tri_state_and_stops_on_false(self) -> None:
        self.assertIn(
            "proc ::sft::boolean_property_result {object names}", self.text
        )
        self.assertIn(
            "return [dict create supported 1 name $name value 0 raw $value]",
            self.text,
        )
        self.assertIn(
            "return [dict create supported 0 name $name value \"\" raw $value]",
            self.text,
        )
        self.assertNotIn("proc ::sft::any_true_property", self.text)

    def test_runtime_checks_independent_net_classifiers_fail_closed(self) -> None:
        for aliases in (
            "{is_power power}",
            "{is_ground ground}",
            "{is_clock is_clock_net clock}",
        ):
            self.assertIn(aliases, self.text)
        self.assertIn("proc ::sft::boolean_value_or_fail", self.text)
        self.assertIn("cannot determine $label", self.text)
        self.assertNotIn("{is_power is_ground is_clock}", self.text)

    def test_capture_clock_discovery_requires_explicit_boolean_evidence(self) -> None:
        body = re.search(
            r"proc ::sft::clock_pin_for_endpoint \{endpoint\} \{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(body)
        text = body.group("body")
        self.assertIn("boolean_property_result $pin {is_clock_pin clock}", text)
        self.assertIn("if {![dict get $result supported]}", text)
        self.assertNotIn("regexp", text)
        self.assertIn("explicitly classified capture clock pin", text)

    def test_data_delay_injection_uses_case_frozen_reference_only_for_gold(self) -> None:
        chooser = re.search(
            r"proc ::sft::injection_delay_cell \{\} \{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(chooser)
        text = chooser.group("body")
        self.assertIn('$::SFT_CASE(calibration_status) eq "FROZEN"', text)
        self.assertIn("set reference $::SFT_FROZEN_DELAY_CELL", text)
        self.assertIn("::sft::lib_cell_object $reference", text)
        self.assertIn("^[A-Za-z0-9_.]+$", text)
        self.assertIn(
            "[file tail [::sft::object_name $object]] ne $reference", text
        )
        self.assertIn("return $reference", text)
        self.assertIn("::sft::first_existing_cell $::SFT_DELAY_CELLS", text)
        self.assertLess(
            text.index("return $reference"),
            text.index("::sft::first_existing_cell $::SFT_DELAY_CELLS"),
        )

        injector = re.search(
            r"proc ::sft::inject_data_delay \{mode count\} \{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(injector)
        self.assertIn(
            "set delay_cell [::sft::injection_delay_cell]",
            injector.group("body"),
        )
        self.assertIn(
            "dict set change scaffold_cell $insertion_cell",
            injector.group("body"),
        )
        self.assertIn(
            "dict set change cell $delay_cell",
            injector.group("body"),
        )

        # Hold repair has a separate, case-local frozen reference and count;
        # it never reuses the setup-injection choice implicitly.
        repair = re.search(
            r"proc ::sft::plan_hold_delays \{\} \{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(repair)
        self.assertIn("set delay [::sft::repair_delay_cell]", repair.group("body"))
        self.assertIn(
            "set count $::SFT_CASE(repair_delay_cells_per_endpoint)",
            repair.group("body"),
        )
        self.assertNotIn("SFT_FROZEN_DELAY_CELL", repair.group("body"))

        chooser = re.search(
            r"proc ::sft::repair_delay_cell \{\} \{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(chooser)
        chooser_text = chooser.group("body")
        self.assertIn("set reference $::SFT_REPAIR_DELAY_CELL", chooser_text)
        self.assertIn('$::SFT_CASE(calibration_status) eq "FROZEN"', chooser_text)
        self.assertLess(
            chooser_text.index("return $reference"),
            chooser_text.index("::sft::first_existing_cell $::SFT_DELAY_CELLS"),
        )

    def test_capture_clock_injection_builds_a_counted_chain_on_one_endpoint(self) -> None:
        injector = re.search(
            r"proc ::sft::inject_capture_skew \{mode count\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(injector)
        text = injector.group("body")
        self.assertIn("[llength $selected] != 1", text)
        self.assertIn("$count < 1", text)
        self.assertIn(
            "for {set index 0} {$index < $count} {incr index}", text
        )
        self.assertIn(
            "$clock_pin $cell CLOCKPATH capture_clock_injection", text
        )
        self.assertIn(
            "::sft::inject_capture_skew early $::SFT_CASE(hold_clock_cell_count)",
            self.text,
        )

    def test_inserted_repeater_is_bound_to_exact_hierarchical_db_name(self) -> None:
        resolver = re.search(
            r"proc ::sft::resolve_added_repeater \{target requested_name "
            r"requested_cell expected_name\} \{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(resolver)
        text = resolver.group("body")
        self.assertIn("exact_cells_named $expected_name", text)
        self.assertIn("[llength $objects] != 1", text)
        self.assertIn("[file tail $actual_name] ne $requested_name", text)
        self.assertIn("$actual_name ne $expected_name", text)
        self.assertIn("$actual_cell ne $requested_cell", text)

        add = re.search(
            r"proc ::sft::add_repeater_to_term \{target cell tag change_kind\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(add)
        text = add.group("body")
        self.assertIn(
            "ecoAddRepeater -term $target -cell $cell -name $name", text
        )
        self.assertIn("set actual_name [::sft::resolve_added_repeater", text)
        self.assertIn("inst $actual_name", text)
        self.assertNotIn("-name $actual_name", text)
        self.assertIn("get_cells -quiet [list $full_name]", self.text)
        self.assertNotIn("cells_with_leaf_name", self.text)

    def test_repeater_scope_comes_from_target_owning_cell(self) -> None:
        body = re.search(
            r"proc ::sft::repeater_parent_for_term \{target\} "
            r"\{(?P<body>.*?)\n\}",
            self.text,
            re.DOTALL,
        )
        self.assertIsNotNone(body)
        text = body.group("body")
        self.assertIn("get_cells -quiet -of_objects $pin", text)
        self.assertIn("set parent [file dirname $owner]", text)
        self.assertIn("[llength $cells] != 1", text)

    def test_drv_parser_accepts_only_canonical_innovus_pipe_tables(self) -> None:
        self.assertIn("Pin\\s+Name", self.text)
        self.assertIn("pipe-table DRV violator row has non-negative slack", self.text)
        self.assertIn(
            "pipe-table DRV row appears without exactly one preceding canonical header",
            self.text,
        )
        self.assertIn(
            "ambiguously mixes whitespace and pipe tables", self.text
        )


if __name__ == "__main__":
    unittest.main()

# Pure-Tcl unit tests for drive-family and action-feasibility helpers.
# Innovus database commands used by these helpers are replaced with small,
# deterministic Liberty-object mocks; no EDA tool or design database is used.

if {[llength $argv] != 1} {
    error "usage: tclsh test_pilot_runtime.tcl /path/to/pilot_runtime.tcl"
}
source [lindex $argv 0]

proc assert_equal {actual expected label} {
    if {$actual ne $expected} {
        error "$label: expected=<$expected> actual=<$actual>"
    }
}

proc assert_error_match {script pattern label} {
    if {![catch {uplevel 1 $script} message]} {
        error "$label: expected an error matching $pattern"
    }
    if {![string match $pattern $message]} {
        error "$label: unexpected error: $message"
    }
}

proc write_binary_test_file {path data} {
    set stream [open $path w]
    fconfigure $stream -translation binary -encoding binary
    puts -nonewline $stream $data
    close $stream
}

# write_sdc's generation timestamp is the sole permitted volatile SDC byte
# range.  The normalizer must preserve all other bytes and line endings, and
# reject missing, duplicate, or merely similar header spellings.
set sdc_test_path [file join /tmp "sft_runtime_sdc_[pid].sdc"]
set sdc_input "header\r\n#  Generated on:      Mon Jul 20 12:00:00 2026\r\nset x {two  spaces}\r\n"
write_binary_test_file $sdc_test_path $sdc_input
::sft::normalize_write_sdc_generated_on $sdc_test_path
set sdc_expected "header\r\n#  Generated on:      SFT_NORMALIZED_VOLATILE_METADATA\r\nset x {two  spaces}\r\n"
assert_equal [::sft::read_file $sdc_test_path true] $sdc_expected \
    "SDC normalizer changes only the exact volatile header and preserves CRLF bytes"

set sdc_missing "# Generated on: Mon Jul 20 12:00:00 2026\nset x 1\n"
write_binary_test_file $sdc_test_path $sdc_missing
assert_error_match {
    ::sft::normalize_write_sdc_generated_on $sdc_test_path
} {*has 0 exact '#  Generated on:' headers; expected exactly one*} \
    "near-match SDC header does not broaden normalization"
assert_equal [::sft::read_file $sdc_test_path true] $sdc_missing \
    "missing exact SDC header fails before changing the file"

set sdc_duplicate "#  Generated on: first\n#  Generated on: second\nset x 1\n"
write_binary_test_file $sdc_test_path $sdc_duplicate
assert_error_match {
    ::sft::normalize_write_sdc_generated_on $sdc_test_path
} {*has 2 exact '#  Generated on:' headers; expected exactly one*} \
    "duplicate exact SDC headers fail closed"
assert_equal [::sft::read_file $sdc_test_path true] $sdc_duplicate \
    "duplicate SDC headers fail before changing the file"
file delete -force $sdc_test_path

set ::mock_references {}
set ::bad_functions {}
set ::pin_interfaces {}
set ::net_db_attributes {}
set ::boolean_property_values {}
set ::boolean_property_queries {}
set ::mock_cell_references {}
set ::mock_timing_properties {}

proc reset_mock_caches {} {
    set ::sft::reference_variant_cache {}
    set ::sft::lib_function_cache {}
}

proc get_lib_cells {args} {
    return $::mock_references
}

proc get_lib_pins {args} {
    set cell [lindex $args end]
    set reference [file tail $cell]
    if {[dict exists $::pin_interfaces $reference]} {
        set result {}
        foreach pin [dict get $::pin_interfaces $reference] {
            lappend result "$cell/$pin"
        }
        return $result
    }
    return [list "$cell/A" "$cell/Y"]
}

proc get_property {object property} {
    lappend ::boolean_property_queries [list get_property $object $property]
    set boolean_key [list $object $property]
    if {[dict exists $::mock_timing_properties $boolean_key]} {
        return [dict get $::mock_timing_properties $boolean_key]
    }
    if {[dict exists $::boolean_property_values $boolean_key]} {
        return [dict get $::boolean_property_values $boolean_key]
    }
    if {$property in {ref_name base_name cell_name} &&
        [dict exists $::mock_cell_references $object]} {
        return [dict get $::mock_cell_references $object]
    }
    if {$property in {full_name name}} {
        return $object
    }
    set pin [file tail $object]
    if {$property in {direction pin_direction}} {
        if {$pin in {A I}} { return input }
        if {$pin in {Y Z O}} { return output }
    }
    if {$property in {function logic_function} && $pin in {Y Z O}} {
        set reference [file tail [file dirname $object]]
        if {[dict exists $::bad_functions $reference]} {
            return [dict get $::bad_functions $reference]
        }
        return A
    }
    return ""
}

proc get_db {object attribute} {
    lappend ::boolean_property_queries [list get_db $object $attribute]
    set key [list $object $attribute]
    if {[dict exists $::net_db_attributes $key]} {
        return [dict get $::net_db_attributes $key]
    }
    error "unsupported mock get_db property $attribute for $object"
}

set ::boolean_property_values [dict create \
    [list data_pin is_clock_pin] false \
    [list data_pin clock] true]
set ::boolean_property_queries {}
set false_result [::sft::boolean_property_result data_pin {is_clock_pin clock}]
assert_equal [dict get $false_result supported] 1 \
    "explicit false Boolean property is supported"
assert_equal [dict get $false_result value] 0 \
    "explicit false Boolean property keeps its false value"
assert_equal [dict get $false_result name] is_clock_pin \
    "primary Boolean alias is authoritative"
assert_equal $::boolean_property_queries \
    {{get_property data_pin is_clock_pin}} \
    "explicit false stops before legacy fallback queries"

set ::boolean_property_values [dict create [list clock_pin is_clock_pin] true]
set ::boolean_property_queries {}
set true_result [::sft::boolean_property_result clock_pin {is_clock_pin clock}]
assert_equal [dict get $true_result supported] 1 \
    "explicit true Boolean property is supported"
assert_equal [dict get $true_result value] 1 \
    "explicit true Boolean property keeps its true value"
assert_equal $::boolean_property_queries \
    {{get_property clock_pin is_clock_pin}} \
    "explicit true stops before legacy fallback queries"

set ::boolean_property_values {}
set ::net_db_attributes [dict create [list fallback_pin .is_clock_pin] false]
set ::boolean_property_queries {}
set db_result [::sft::boolean_property_result fallback_pin {is_clock_pin clock}]
assert_equal [dict get $db_result supported] 1 \
    "get_db fallback can supply a supported Boolean"
assert_equal [dict get $db_result value] 0 \
    "get_db false stops the alias search"
assert_equal $::boolean_property_queries \
    {{get_property fallback_pin is_clock_pin} {get_db fallback_pin .is_clock_pin}} \
    "get_db false stops before querying a legacy alias"

set ::boolean_property_values [dict create [list ambiguous_pin is_clock_pin] maybe]
set ::boolean_property_queries {}
set ambiguous_result [::sft::boolean_property_result ambiguous_pin {is_clock_pin clock}]
assert_equal [dict get $ambiguous_result supported] 0 \
    "a readable non-Boolean value is unsupported"
assert_equal $::boolean_property_queries \
    {{get_property ambiguous_pin is_clock_pin}} \
    "a readable non-Boolean value does not fall through"
assert_error_match {
    ::sft::boolean_value_or_fail ambiguous_pin {is_clock_pin clock} \
        {endpoint clock-pin classification}
} {*cannot determine endpoint clock-pin classification*} \
    "unsupported Boolean safety evidence fails closed"

set ::boolean_property_values {}
set ::net_db_attributes {}
set ::boolean_property_queries {}
assert_error_match {
    ::sft::boolean_value_or_fail unknown_pin {is_clock_pin clock} \
        {endpoint clock-pin classification}
} {*cannot determine endpoint clock-pin classification*} \
    "missing Boolean safety evidence fails closed"

set ::net_db_attributes [dict create [list data_net .constant] no_constant]
assert_equal [::sft::net_is_constant data_net] 0 \
    "no_constant enum identifies a normal data net"
foreach false_value {none false 0} {
    set ::net_db_attributes [dict create [list data_net .constant] $false_value]
    assert_equal [::sft::net_is_constant data_net] 0 \
        "$false_value enum is an explicit non-constant value"
}
set ::net_db_attributes [dict create [list tie_net .constant] constant_1]
assert_equal [::sft::net_is_constant tie_net] 1 \
    "any other non-empty enum identifies a constant net"
set ::net_db_attributes {}
assert_error_match {
    ::sft::net_is_constant unknown_net
} {*cannot determine constant classification*} \
    "unsupported constant enum fails closed"
set ::net_db_attributes [dict create [list empty_net .constant] ""]
assert_error_match {
    ::sft::net_is_constant empty_net
} {*cannot determine constant classification*} \
    "empty constant enum fails closed"

assert_equal [::sft::parse_drive BUF_X1M_A9TR40] \
    {BUF 1 M A9TR40} "M flavor parsing"
assert_equal [::sft::parse_drive BUF_X0P5B_A9TR40] \
    {BUF 0.5 B A9TR40} "B flavor parsing"

set ::mock_references {
    BUF_X2B_A9TR40
    BUF_X2M_A9TR40
    BUF_X1P0M_A9TR40
    BUF_X0P5M_A9TR40
    BUF_X1M_A9TR40
    BUF_X1M_A9TR40
}
reset_mock_caches
assert_equal [::sft::available_references BUF_X0P5M_A9TR40] \
    {{0.5 BUF_X0P5M_A9TR40} {1 BUF_X1M_A9TR40} {1.0 BUF_X1P0M_A9TR40} {2 BUF_X2M_A9TR40}} \
    "numeric/full-reference ordering, exact dedupe, and flavor isolation"
assert_error_match {
    ::sft::replacement_for_reference BUF_X0P5M_A9TR40 up 1
} {*ambiguous drive level 1*} "equal-drive references fail closed"

set ::mock_references {
    BUF_X2B_A9TR40
    BUF_X2M_A9TR40
    BUF_X0P5M_A9TR40
    BUF_X1M_A9TR40
}
reset_mock_caches
assert_equal [::sft::replacement_for_reference BUF_X0P5M_A9TR40 up 2] \
    BUF_X2M_A9TR40 "exact two-level upsize"
assert_error_match {
    ::sft::replacement_for_reference BUF_X0P5M_A9TR40 up 3
} {*cannot resize*exactly up by 3*} "upsize is not clamped"
assert_error_match {
    ::sft::replacement_for_reference BUF_X0P5M_A9TR40 down 1
} {*cannot resize*exactly down by 1*} "downsize is not clamped"
assert_error_match {
    ::sft::validate_equivalent_cells BUF_X1M_A9TR40 BUF_X2B_A9TR40 test
} {*changes cell family*} "drive flavor is part of the family key"

set ::SFT_BUFFER_CELLS {BUF_X0P7M_A9TR40 BUF_X1M_A9TR40}
set ::SFT_FROZEN_DELAY_CELL DLY2_X4M_A9TR40
array set ::SFT_CASE {setup_delay_cells 1}
set ::mock_references {
    BUF_X0P7M_A9TR40
    BUF_X1M_A9TR40
    BUF_X4M_A9TR40
}
reset_mock_caches
set weak_setup_profile [::sft::replace_delay_and_upsize_profile]
assert_equal [dict get $weak_setup_profile driver_steps] 1 \
    "one injected delay cell keeps the measured weak driver profile"
assert_equal [dict get $weak_setup_profile buffer] BUF_X0P7M_A9TR40 \
    "one injected delay cell keeps the first configured buffer"

set ::SFT_CASE(setup_delay_cells) 2
set strong_setup_profile [::sft::replace_delay_and_upsize_profile]
assert_equal [dict get $strong_setup_profile driver_steps] 0 \
    "two injected delay cells keep the endpoint driver unchanged"
assert_equal [dict get $strong_setup_profile buffer] BUF_X4M_A9TR40 \
    "two injected delay cells use the measured equal-area buffer"

set ::SFT_CASE(repair_strategy) replace_delay_and_upsize
assert_equal [::sft::uses_equal_area_injection_scaffold \
    late 2 DLY2_X4M_A9TR40] 1 \
    "the SETUP_002 profile uses a routed equal-area injection scaffold"
assert_equal [::sft::uses_equal_area_injection_scaffold \
    late 1 DLY2_X4M_A9TR40] 0 \
    "a single-delay setup profile does not use the scaffold"
assert_equal [::sft::uses_equal_area_injection_scaffold \
    late 2 DLY4_X2M_A9TR40] 0 \
    "a non-equal-area delay cell does not use the scaffold"
set ::SFT_CASE(repair_strategy) coordinated_setup_hold
assert_equal [::sft::uses_equal_area_injection_scaffold \
    late 2 DLY2_X4M_A9TR40] 0 \
    "mixed timing repair retains its independently calibrated injection flow"
set ::SFT_CASE(repair_strategy) replace_delay_and_upsize

set ::mock_references {BUF_X0P7M_A9TR40 BUF_X1M_A9TR40 BUF_X2M_A9TR40}
reset_mock_caches
assert_error_match {
    ::sft::replace_delay_and_upsize_profile
} {*cannot resolve Liberty cell BUF_X4M_A9TR40*} \
    "the equal-area setup profile fails closed when its exact buffer is missing"

set ::SFT_FROZEN_DELAY_CELL DLY4_X2M_A9TR40
set ::mock_references {BUF_X0P7M_A9TR40 BUF_X1M_A9TR40 BUF_X2M_A9TR40}
reset_mock_caches
set wider_delay_profile [::sft::replace_delay_and_upsize_profile]
assert_equal [dict get $wider_delay_profile driver_steps] 3 \
    "DLY4_X2M retains the measured three-level driver profile"
assert_equal [dict get $wider_delay_profile buffer] BUF_X2M_A9TR40 \
    "DLY4_X2M retains the measured strong buffer"

set ::SFT_FROZEN_DELAY_CELL DLY4_X4M_A9TR40
set ::SFT_CASE(id) SETUP_004
set ::bad_functions [dict create INV_X9B_A9TR40 {(~A)}]
set ::mock_references {
    BUF_X0P7M_A9TR40
    BUF_X1M_A9TR40
    BUF_X2M_A9TR40
    INV_X9B_A9TR40
}
reset_mock_caches
set widest_delay_profile [::sft::replace_delay_and_upsize_profile]
assert_equal [dict get $widest_delay_profile driver_steps] 0 \
    "SETUP_004 paired-inverter repair leaves endpoint drivers unchanged"
assert_equal [dict get $widest_delay_profile buffer] INV_X9B_A9TR40 \
    "SETUP_004 uses the exact-footprint paired inverter"
assert_equal [::sft::uses_paired_inverter_repair] 1 \
    "the paired-inverter repair is enabled for SETUP_004"

set ::SFT_CASE(id) FUTURE_SETUP_CASE
set future_widest_delay_profile [::sft::replace_delay_and_upsize_profile]
assert_equal [dict get $future_widest_delay_profile driver_steps] 3 \
    "the paired-inverter profile is isolated to SETUP_004"
assert_equal [dict get $future_widest_delay_profile buffer] BUF_X2M_A9TR40 \
    "future DLY4_X4M cases retain the default strong buffer"
assert_equal [::sft::uses_paired_inverter_repair] 0 \
    "future cases do not inherit the paired-inverter repair"

set ::SFT_CASE(id) SETUP_004
set ::mock_references {BUF_X0P7M_A9TR40 BUF_X1M_A9TR40 BUF_X2M_A9TR40}
reset_mock_caches
assert_error_match {
    ::sft::replace_delay_and_upsize_profile
} {*cannot resolve Liberty cell INV_X9B_A9TR40*} \
    "the paired-inverter profile fails closed when its exact cell is missing"

set ::SFT_CASE(id) FUTURE_SETUP_CASE
set ::SFT_FROZEN_DELAY_CELL DLY4_X2M_A9TR40
set ::mock_references {BUF_X0P7M_A9TR40 BUF_X1M_A9TR40 BUF_X4M_A9TR40}
reset_mock_caches
assert_error_match {
    ::sft::replace_delay_and_upsize_profile
} {*cannot resolve Liberty cell BUF_X2M_A9TR40*} \
    "the strong wider-delay profile fails closed when its exact buffer is missing"
set ::bad_functions {}
set ::SFT_CASE(setup_delay_cells) 0
assert_error_match {
    ::sft::replace_delay_and_upsize_profile
} {*setup_delay_cells must be a positive integer*} \
    "the setup repair profile rejects a missing delay-cell basis"

# Keep exact multi-level resize coverage; steps count available family levels
# rather than numeric X.
set ::mock_references {
    BUF_X2M_A9TR40
    OAI211_X0P5M_A9TR40 OAI211_X0P7M_A9TR40 OAI211_X1M_A9TR40
    OAI211_X1P4M_A9TR40 OAI211_X2M_A9TR40 OAI211_X3M_A9TR40 OAI211_X4M_A9TR40
    OAI21_X0P5M_A9TR40 OAI21_X0P7M_A9TR40 OAI21_X1M_A9TR40
    OAI21_X1P4M_A9TR40 OAI21_X2M_A9TR40 OAI21_X3M_A9TR40 OAI21_X4M_A9TR40
    NAND2_X0P5B_A9TR40 NAND2_X0P7B_A9TR40 NAND2_X1B_A9TR40
    NAND2_X1P4B_A9TR40 NAND2_X2B_A9TR40 NAND2_X3B_A9TR40 NAND2_X4B_A9TR40
    NAND3_X0P5A_A9TR40 NAND3_X0P7A_A9TR40 NAND3_X1A_A9TR40
    NAND3_X1P4A_A9TR40 NAND3_X2A_A9TR40 NAND3_X3A_A9TR40 NAND3_X4A_A9TR40
}
reset_mock_caches
foreach mapping {
    {OAI211_X1M_A9TR40 OAI211_X3M_A9TR40}
    {OAI21_X1M_A9TR40 OAI21_X3M_A9TR40}
    {NAND2_X1B_A9TR40 NAND2_X3B_A9TR40}
    {NAND3_X0P7A_A9TR40 NAND3_X2A_A9TR40}
    {NAND3_X1A_A9TR40 NAND3_X3A_A9TR40}
} {
    lassign $mapping original expected
    assert_equal [::sft::replacement_for_reference $original up 3] $expected \
        "frozen setup family has its exact three-level replacement"
}

set ::mock_references {BUF_X1M_A9TR40 BUF_X2M_A9TR40}
set ::bad_functions [dict create BUF_X2M_A9TR40 !A]
reset_mock_caches
assert_error_match {
    ::sft::validate_resize_transition BUF_X1M_A9TR40 up 1 test
} {*changes Liberty Boolean function*} "resize validates Liberty functions"
set ::bad_functions {}

set ::mock_references {DLY4_X1M_A9TR40 BUF_X1M_A9TR40}
set ::pin_interfaces [dict create \
    DLY4_X1M_A9TR40 {A Y} \
    BUF_X1M_A9TR40 {A Z}]
reset_mock_caches
assert_error_match {
    ::sft::assert_pin_compatible_noninverting_replacement \
        DLY4_X1M_A9TR40 BUF_X1M_A9TR40 test
} {*changes the Liberty pin interface/function*} "DLY to BUF requires exact pin mapping"
set ::pin_interfaces {}

array set ::SFT_CASE {
    calibration_status FROZEN
    injection_strategy downsize_endpoint_driver
    setup_drive_steps 1
    hold_drive_steps 0
    repair_strategy upsize_driver_and_buffer
}
set ::SFT_FROZEN_DELAY_CELL DLY2_X4M_A9TR40
set ::sft::targets [list [dict create \
    endpoint u_capture/D timing late driver_ref BUF_X1M_A9TR40]]
set ::mock_references {
    BUF_X0P5M_A9TR40
    BUF_X1M_A9TR40
    BUF_X2M_A9TR40
}
reset_mock_caches
::sft::validate_resolved_action_feasibility

set ::mock_references {BUF_X0P5M_A9TR40 BUF_X1M_A9TR40}
reset_mock_caches
assert_error_match {
    ::sft::validate_resolved_action_feasibility
} {*cannot resize*exactly up by 1*} "planned repair upsize must exist"

# NOT_GOLD calibration is allowed to skip mutation/replay, but it must still
# reject a candidate whose planned Gold repair is structurally impossible.
set ::SFT_CASE(calibration_status) PROBE_REQUIRED
set ::env(SFT_ALLOW_UNFROZEN_CALIBRATION) 1
assert_error_match {
    ::sft::validate_resolved_action_feasibility
} {*cannot resize*exactly up by 1*} \
    "calibration validates planned repair before returning"
unset ::env(SFT_ALLOW_UNFROZEN_CALIBRATION)
set ::SFT_CASE(calibration_status) FROZEN

array set ::SFT_CASE {
    calibration_status FROZEN
    injection_strategy insert_data_delay
    setup_delay_cells 2
    setup_drive_steps 0
    hold_drive_steps 0
    repair_strategy replace_delay_and_upsize
}
set ::sft::targets [list [dict create \
    endpoint u_capture/D timing late driver_ref OAI211_X1M_A9TR40]]
set ::mock_references {
    BUF_X4M_A9TR40
    OAI211_X0P5M_A9TR40 OAI211_X0P7M_A9TR40 OAI211_X1M_A9TR40
    OAI211_X1P4M_A9TR40 OAI211_X2M_A9TR40
}
reset_mock_caches
::sft::validate_resolved_action_feasibility

set ::SFT_FROZEN_DELAY_CELL DLY4_X2M_A9TR40
set ::mock_references {
    BUF_X2M_A9TR40
    OAI211_X0P5M_A9TR40 OAI211_X0P7M_A9TR40 OAI211_X1M_A9TR40
    OAI211_X1P4M_A9TR40 OAI211_X2M_A9TR40
}
reset_mock_caches
assert_error_match {
    ::sft::validate_resolved_action_feasibility
} {*cannot resize*exactly up by 3*} \
    "strong wider-delay setup feasibility rejects a missing three-level replacement"

set drc_test_path [file join /tmp "sft_runtime_drc_[pid].rpt"]
proc write_drc_test_report {path command body total} {
    set stream [open $path w]
    puts $stream "# Command: $command"
    if {$body ne ""} { puts $stream $body }
    puts $stream "Total Violations : $total Viols."
    close $stream
}

write_drc_test_report $drc_test_path \
    {verify_drc -limit 1000000 -report drc.rpt} \
    {SHORT: ( Metal Short ) Regular Wire of Net a & Net b ( M1 )} 1
set parsed_drc [::sft::parse_drc_counts $drc_test_path]
assert_equal [dict get $parsed_drc total] 1 "full-limit DRC total"
assert_equal [dict get $parsed_drc categories SHORT] 1 "full-limit DRC category"

write_drc_test_report $drc_test_path \
    {verify_drc -report drc.rpt} "" 0
assert_error_match {
    ::sft::parse_drc_counts $drc_test_path
} {*exactly one -limit 1000000*} "DRC parser requires the fixed high limit"

write_drc_test_report $drc_test_path \
    {verify_drc -limit 1000000 -report drc.rpt} \
    {Violation limit reached; report truncated.} 0
assert_error_match {
    ::sft::parse_drc_counts $drc_test_path
} {*early-termination/truncation signal*} "DRC parser rejects cutoff markers"

write_drc_test_report $drc_test_path \
    {verify_drc -limit 1000000 -report drc.rpt} "" 1000000
assert_error_match {
    ::sft::parse_drc_counts $drc_test_path
} {*reaches collection limit 1000000*} "DRC parser rejects totals at the cutoff"
file delete -force $drc_test_path

set drv_test_path [file join /tmp "sft_runtime_drv_[pid].rpt"]
proc write_drv_test_report {path body} {
    set stream [open $path w]
    puts $stream $body
    close $stream
}

# Actual Innovus 21.10 report_constraint excerpts use this five-column pipe
# table.  Exercise all three DRV types rather than special-casing fanout.
write_drv_test_report $drv_test_path {
Check type : max_transition
---------------------------
     +--------------------------------------------------------------------------------------+
     | Pin Name               | Required | Actual | Slack  | View                |
     |------------------------+----------+--------+--------+---------------------|
     | u_data_a/Y             | 0.120    | 0.141  | -0.021 | functional_setup_ss |
Check type : max_capacitance
---------------------------
     | Pin Name               | Required | Actual | Slack  | View                |
     | u_data_b/Y             | 0.080    | 0.095  | -0.015 | functional_setup_ss |
     | u_data_c/Y             | 0.100    | 0.106  | -0.006 | functional_setup_ss |
Check type : max_fanout
---------------------------
     | Pin Name                       | Required | Actual  | Slack    | View                |
     | FE_DBTC102_nvdla_core_rstn/Y   | 32.000   | 253.000 | -221.000 | functional_setup_ss |
}
set parsed_drv [::sft::parse_drv_counts $drv_test_path]
assert_equal [dict get $parsed_drv max_transition] 1 \
    "Innovus pipe-table max-transition count"
assert_equal [dict get $parsed_drv max_capacitance] 2 \
    "Innovus pipe-table max-capacitance count"
assert_equal [dict get $parsed_drv max_fanout] 1 \
    "Innovus pipe-table max-fanout count"

write_drv_test_report $drv_test_path {
Check type : max_transition
No Violations found
Check type : max_capacitance
No Violations found
Check type : max_fanout
| orphan_driver/Y | 32.000 | 75.000 | -43.000 | functional_setup_ss |
}
assert_error_match {
    ::sft::parse_drv_counts $drv_test_path
} {*pipe-table DRV row appears without exactly one preceding canonical header*} \
    "pipe rows without the canonical header fail closed"

write_drv_test_report $drv_test_path {
Check type : max_transition
No Violations found
Check type : max_capacitance
No Violations found
Check type : max_fanout
| Pin Name | Required | Actual | Slack | View |
| u_driver/Y | 32.000 | 32.000 | 0.000 | functional_setup_ss |
}
assert_error_match {
    ::sft::parse_drv_counts $drv_test_path
} {*pipe-table DRV violator row has non-negative slack*} \
    "pipe-table violator rows require negative slack"
file delete -force $drv_test_path

# Innovus 21.10 keeps ecoAddRepeater -name hierarchy-free, then creates the
# cell in the sink cell's parent hierarchy.  These database mocks reproduce
# the observed SETUP_001 behavior:
#   -term u_exp/exp_reg/D -name SFT_... -> u_exp/SFT_...
set ::mock_pin_owners [dict create \
    u_exp/exp_reg/D u_exp/exp_reg \
    u_cap/cap_reg/CK u_cap/cap_reg \
    u_nonnegative/D u_nonnegative]
set ::mock_cell_references [dict create \
    u_exp/exp_reg DFF_X1M_A9TR40 \
    u_cap/cap_reg DFF_X1M_A9TR40 \
    u_nonnegative DFF_X1M_A9TR40]
set ::mock_eco_add_mode normal
set ::mock_eco_add_commands {}

proc get_pins {args} {
    set requested [lindex $args end]
    set result {}
    foreach pin $requested {
        if {[dict exists $::mock_pin_owners $pin]} {
            lappend result $pin
        }
    }
    return $result
}

set ::mock_report_timing_result {}
set ::mock_report_timing_commands {}
proc report_timing {args} {
    lappend ::mock_report_timing_commands $args
    return $::mock_report_timing_result
}

set ::SFT_SETUP_VIEW functional_setup_ss
set ::SFT_HOLD_VIEW functional_hold_ff
set ::mock_timing_properties [dict create \
    [list exact_late_path capturing_point] u_exp/exp_reg/D \
    [list exact_late_path slack] -0.031 \
    [list exact_early_path capturing_point] u_cap/cap_reg/CK \
    [list exact_early_path slack] 0.014]
set ::mock_report_timing_result {exact_late_path}
set ::mock_report_timing_commands {}
assert_equal [::sft::exact_endpoint_slack late u_exp/exp_reg/D] -0.031 \
    "exact selected setup endpoint slack"
assert_equal [lindex $::mock_report_timing_commands 0] \
    {-collection -max_paths 1 -path_type full_clock -to u_exp/exp_reg/D -late -view functional_setup_ss} \
    "setup endpoint query binds one exact pin and setup view"
set ::mock_report_timing_result {exact_early_path}
set ::mock_report_timing_commands {}
assert_equal [::sft::exact_endpoint_slack early u_cap/cap_reg/CK] 0.014 \
    "exact selected hold endpoint slack is retained even when non-negative"
assert_equal [lindex $::mock_report_timing_commands 0] \
    {-collection -max_paths 1 -path_type full_clock -to u_cap/cap_reg/CK -early -view functional_hold_ff} \
    "hold endpoint query binds one exact pin and hold view"

set ::mock_report_timing_result {exact_late_path duplicate_path}
assert_error_match {
    ::sft::exact_endpoint_slack late u_exp/exp_reg/D
} {*returned 2 paths*} "ambiguous exact endpoint path query fails closed"
set ::mock_report_timing_result {wrong_endpoint_path}
dict set ::mock_timing_properties [list wrong_endpoint_path capturing_point] u_other/D
dict set ::mock_timing_properties [list wrong_endpoint_path slack] -0.031
assert_error_match {
    ::sft::exact_endpoint_slack late u_exp/exp_reg/D
} {*returned u_other/D for requested*} "endpoint aliasing fails closed"
set ::mock_report_timing_result {nonfinite_path}
dict set ::mock_timing_properties [list nonfinite_path capturing_point] u_exp/exp_reg/D
dict set ::mock_timing_properties [list nonfinite_path slack] NaN
assert_error_match {
    ::sft::exact_endpoint_slack late u_exp/exp_reg/D
} {*slack is not finite*} "non-finite exact endpoint slack fails closed"

# violation_locality first collects the complete negative-endpoint dictionary.
# A selected endpoint already present there reuses its worst real slack; only a
# missing (normally non-violating) endpoint incurs an exact endpoint query.
dict set ::mock_timing_properties [list exact_nonnegative_path capturing_point] \
    u_nonnegative/D
dict set ::mock_timing_properties [list exact_nonnegative_path slack] 0.019
set ::mock_report_timing_result {exact_nonnegative_path}
set ::mock_report_timing_commands {}
set selected_slacks [::sft::selected_endpoint_slacks late \
    {u_exp/exp_reg/D u_nonnegative/D} \
    [dict create u_exp/exp_reg/D -0.031]]
assert_equal [dict get $selected_slacks u_exp/exp_reg/D] -0.031 \
    "negative selected endpoint reuses the global worst slack"
assert_equal [dict get $selected_slacks u_nonnegative/D] 0.019 \
    "negative-dictionary miss retains an exact endpoint slack"
assert_equal [llength $::mock_report_timing_commands] 1 \
    "only a negative-dictionary miss issues an exact endpoint query"
assert_equal [lindex $::mock_report_timing_commands 0] \
    {-collection -max_paths 1 -path_type full_clock -to u_nonnegative/D -late -view functional_setup_ss} \
    "fallback exact query remains bound to the selected pin and view"

set ::mock_report_timing_commands {}
set selected_slacks [::sft::selected_endpoint_slacks late \
    {u_exp/exp_reg/D} [dict create u_exp/exp_reg/D -0.044]]
assert_equal $selected_slacks {u_exp/exp_reg/D -0.044} \
    "fully cached negative selected endpoints preserve their real slack"
assert_equal [llength $::mock_report_timing_commands] 0 \
    "fully cached negative selected endpoints issue no duplicate query"
assert_error_match {
    ::sft::selected_endpoint_slacks late {u_exp/exp_reg/D} \
        [dict create u_exp/exp_reg/D 0.0]
} {*cached negative endpoint slack is invalid*} \
    "a non-negative value cannot masquerade as negative-path cache evidence"

# Before-stage WNS/TNS may reuse the injection measurement from the identical
# database state.  A partial/invalid cache fails closed, while after-stage
# timing is always remeasured and overwrites any pre-existing array values.
rename ::sft::timing_summary ::sft::timing_summary_real
set ::mock_timing_summary_values [dict create \
    late {-0.201 -0.378} early {0.050 0.0}]
set ::mock_timing_summary_calls {}
proc ::sft::timing_summary {mode} {
    lappend ::mock_timing_summary_calls $mode
    return [dict get $::mock_timing_summary_values $mode]
}

array unset ::sft::stage_metrics
set ::sft::stage_metrics(before,setup) {-0.097 -0.182}
set ::sft::stage_metrics(before,hold) {0.050 0.0}
::sft::update_stage_timing_metrics before
assert_equal $::mock_timing_summary_calls {} \
    "complete before metric cache avoids duplicate collection queries"
assert_equal $::sft::stage_metrics(before,setup) {-0.097 -0.182} \
    "before setup cache is reused without changing values"
assert_equal $::sft::stage_metrics(before,hold) {0.050 0.0} \
    "before hold cache is reused without changing values"

array unset ::sft::stage_metrics
set ::sft::stage_metrics(before,setup) {-0.097 -0.182}
set ::mock_timing_summary_calls {}
assert_error_match {
    ::sft::update_stage_timing_metrics before
} {*before timing metric cache is incomplete*} \
    "partial before metric cache fails closed"
assert_equal $::mock_timing_summary_calls {} \
    "partial cache cannot mix cached and fresh timing summaries"

array unset ::sft::stage_metrics
set ::sft::stage_metrics(before,setup) {NaN 0.0}
set ::sft::stage_metrics(before,hold) {0.050 0.0}
assert_error_match {
    ::sft::update_stage_timing_metrics before
} {*cached wns is not finite*} \
    "invalid before metric cache fails closed"

array unset ::sft::stage_metrics
set ::sft::stage_metrics(after,setup) {9.0 0.0}
set ::sft::stage_metrics(after,hold) {9.0 0.0}
set ::mock_timing_summary_calls {}
::sft::update_stage_timing_metrics after
assert_equal $::mock_timing_summary_calls {late early} \
    "after stage always performs fresh setup and hold measurement"
assert_equal $::sft::stage_metrics(after,setup) {-0.201 -0.378} \
    "after setup replaces stale array state with fresh measurement"
assert_equal $::sft::stage_metrics(after,hold) {0.050 0.0} \
    "after hold replaces stale array state with fresh measurement"

rename ::sft::timing_summary {}
rename ::sft::timing_summary_real ::sft::timing_summary

proc get_cells {args} {
    set of_index [lsearch -exact $args -of_objects]
    if {$of_index >= 0} {
        set result {}
        foreach object [lindex $args [expr {$of_index + 1}]] {
            if {[dict exists $::mock_pin_owners $object]} {
                lappend result [dict get $::mock_pin_owners $object]
            }
        }
        return $result
    }
    set hierarchical_index [lsearch -exact $args -hierarchical]
    if {$hierarchical_index >= 0} {
        set leaf [lindex $args [expr {$hierarchical_index + 1}]]
        set result {}
        foreach instance [dict keys $::mock_cell_references] {
            if {[file tail $instance] eq $leaf} {
                lappend result $instance
            }
        }
        return [lsort $result]
    }
    set result {}
    foreach instance [lindex $args end] {
        if {[dict exists $::mock_cell_references $instance]} {
            lappend result $instance
            if {$::mock_eco_add_mode eq "duplicate" &&
                [string match "SFT_ECO_*" [file tail $instance]]} {
                lappend result $instance
            }
        }
    }
    return $result
}

proc ecoAddRepeater {args} {
    lappend ::mock_eco_add_commands $args
    array set option $args
    set owner [dict get $::mock_pin_owners $option(-term)]
    set parent [file dirname $owner]
    set expected [expr {$parent eq "." ?
        $option(-name) : "${parent}/$option(-name)"}]
    switch -- $::mock_eco_add_mode {
        normal {
            dict set ::mock_cell_references $expected $option(-cell)
        }
        wrong_scope {
            dict set ::mock_cell_references "u_wrong/$option(-name)" $option(-cell)
        }
        duplicate {
            dict set ::mock_cell_references $expected $option(-cell)
            dict set ::mock_cell_references "u_duplicate/$option(-name)" $option(-cell)
        }
        wrong_reference {
            dict set ::mock_cell_references $expected BUF_X1M_A9TR40
        }
        default { error "unsupported mock ecoAddRepeater mode $::mock_eco_add_mode" }
    }
}

array set ::SFT_CASE {
    id SETUP_001
    max_eco_cells 8
    calibration_status PROBE_REQUIRED
}
set ::SFT_ECO_PREFIX SFT_ECO_
set ::SFT_BUFFER_CELLS {BUF_X1M_A9TR40}
set ::mock_references {DLY4_X1M_A9TR40 BUF_X1M_A9TR40}
set ::pin_interfaces [dict create \
    DLY4_X1M_A9TR40 {A Y} \
    BUF_X1M_A9TR40 {A Y}]
set ::sft::changes {}
set ::sft::eco_count 0
reset_mock_caches

set ::SFT_DELAY_CELLS {DLY4_X1M_A9TR40 DLY2_X4M_A9TR40}
set ::SFT_FROZEN_DELAY_CELL {}
assert_equal [::sft::injection_delay_cell] DLY4_X1M_A9TR40 \
    "legacy probe config without a delay reference uses global candidates"
set ::SFT_DELAY_CELLS {DLY2_X4M_A9TR40 DLY4_X1M_A9TR40}
set ::SFT_FROZEN_DELAY_CELL PROBE_REQUIRED
assert_equal [::sft::injection_delay_cell] DLY2_X4M_A9TR40 \
    "probe placeholder follows the current global candidate order"
set ::SFT_CASE(calibration_status) FROZEN
set ::SFT_FROZEN_DELAY_CELL DLY4_X1M_A9TR40
assert_equal [::sft::injection_delay_cell] DLY4_X1M_A9TR40 \
    "frozen injection ignores a later global candidate reorder"
set ::SFT_FROZEN_DELAY_CELL PROBE_REQUIRED
assert_error_match {
    ::sft::injection_delay_cell
} {*frozen data-delay injection has no exact cell reference*} \
    "frozen injection rejects the probe placeholder"
set ::SFT_CASE(calibration_status) PROBE_REQUIRED
set ::SFT_FROZEN_DELAY_CELL {}

set path_1 [::sft::add_repeater_to_term \
    u_exp/exp_reg/D DLY4_X1M_A9TR40 PATH data_delay_injection]
set path_2 [::sft::add_repeater_to_term \
    u_exp/exp_reg/D DLY4_X1M_A9TR40 PATH data_delay_injection]
set clock_path [::sft::add_repeater_to_term \
    u_cap/cap_reg/CK DLY4_X1M_A9TR40 CLOCKPATH capture_clock_injection]
assert_equal $path_1 u_exp/SFT_ECO_SETUP_001_PATH_1 \
    "first inserted repeater resolves to its exact hierarchical full name"
assert_equal $path_2 u_exp/SFT_ECO_SETUP_001_PATH_2 \
    "successive repeater resolves independently in the same hierarchy"
assert_equal $clock_path u_cap/SFT_ECO_SETUP_001_CLOCKPATH_3 \
    "capture-clock repeater uses the capture sink hierarchy"
assert_equal [dict get [lindex $::sft::changes 0] inst] $path_1 \
    "injection action records the first actual full name"
assert_equal [dict get [lindex $::sft::changes 1] inst] $path_2 \
    "injection action records the successive actual full name"
assert_equal [dict get [lindex $::sft::changes 2] inst] $clock_path \
    "capture-clock action records its actual full name"
foreach command $::mock_eco_add_commands {
    set name [lindex $command [expr {[lsearch -exact $command -name] + 1}]]
    assert_equal [file tail $name] $name \
        "ecoAddRepeater -name remains a legal hierarchy-free leaf"
}

# The serial-chain audit must compare DB-traced full names with the full names
# recorded by add_repeater_to_term, not with the leaf -name arguments.
set ::mock_chain_drivers [dict create \
    u_exp/exp_reg/D $path_2 \
    ${path_2}/A $path_1 \
    ${path_1}/A u_exp/original_driver \
    u_cap/cap_reg/CK $clock_path \
    ${clock_path}/A u_cap/original_clock_driver]
proc ::sft::net_driver_instance {sink_pin_name} {
    if {![dict exists $::mock_chain_drivers $sink_pin_name]} {
        ::sft::fail "mock chain has no driver for $sink_pin_name"
    }
    return [dict get $::mock_chain_drivers $sink_pin_name]
}
proc ::sft::single_input_pin {instance} {
    return "${instance}/A"
}
::sft::validate_injected_repeater_chains

# Replacement repairs consume changes.inst.  Confirm the emitted ecoChangeCell
# commands use the resolved hierarchy, while only ecoAddRepeater -name remains
# a leaf argument.
set ::sft::diagnostic_existing_instances [list $path_1 $path_2]
set ::sft::repair_commands {}
set ::sft::repair_operations {}
set ::sft::functional_proofs {}
::sft::plan_replace_injected_delays BUF_X1M_A9TR40
assert_equal [lindex $::sft::repair_commands 0] \
    [list ecoChangeCell -inst $path_1 -cell BUF_X1M_A9TR40] \
    "first replacement repair uses actual full instance name"
assert_equal [lindex $::sft::repair_commands 1] \
    [list ecoChangeCell -inst $path_2 -cell BUF_X1M_A9TR40] \
    "successive replacement repair uses actual full instance name"

set ::mock_references {DLY4_X1M_A9TR40 BUF_X1M_A9TR40 BUF_X2M_A9TR40}
set ::sft::repair_commands {}
set ::sft::repair_operations {}
set ::sft::functional_proofs {}
reset_mock_caches
::sft::plan_replace_injected_delays BUF_X2M_A9TR40
assert_equal [lindex $::sft::repair_commands 0] \
    [list ecoChangeCell -inst $path_1 -cell BUF_X2M_A9TR40] \
    "strong replacement profile reaches the emitted hierarchical command"

proc reset_repeater_resolution_mock {} {
    set ::mock_cell_references [dict create \
        u_exp/exp_reg DFF_X1M_A9TR40 \
        u_cap/cap_reg DFF_X1M_A9TR40]
    set ::mock_eco_add_commands {}
    set ::sft::changes {}
    set ::sft::eco_count 0
    set ::sft::functional_proofs {}
}

reset_repeater_resolution_mock
set ::mock_eco_add_mode duplicate
assert_error_match {
    ::sft::add_repeater_to_term \
        u_exp/exp_reg/D DLY4_X1M_A9TR40 PATH data_delay_injection
} {*resolved to 2 instances at expected exact name*} \
    "ambiguous inserted leaf names fail closed"

reset_repeater_resolution_mock
set ::mock_eco_add_mode wrong_scope
assert_error_match {
    ::sft::add_repeater_to_term \
        u_exp/exp_reg/D DLY4_X1M_A9TR40 PATH data_delay_injection
} {*resolved to 0 instances at expected exact name u_exp/*} \
    "an inserted instance outside the target hierarchy fails closed"

reset_repeater_resolution_mock
set ::mock_eco_add_mode wrong_reference
assert_error_match {
    ::sft::add_repeater_to_term \
        u_exp/exp_reg/D DLY4_X1M_A9TR40 PATH data_delay_injection
} {*with reference BUF_X1M_A9TR40*requested DLY4_X1M_A9TR40*} \
    "an inserted instance with the wrong cell reference fails closed"

reset_repeater_resolution_mock
set ::mock_eco_add_mode normal
set ::SFT_ECO_PREFIX bad/name_
assert_error_match {
    ::sft::add_repeater_to_term \
        u_exp/exp_reg/D DLY4_X1M_A9TR40 PATH data_delay_injection
} {*must be one legal hierarchy-free leaf name*} \
    "hierarchical ecoAddRepeater -name arguments are rejected before insertion"
set ::SFT_ECO_PREFIX SFT_ECO_

# A capture-clock candidate may be a calibrated multi-cell serial chain, but
# it must remain bound to one selected early endpoint and one exact clock pin.
reset_repeater_resolution_mock
set ::sft::targets [list [dict create \
    endpoint u_cap/cap_reg/D timing early driver_ref BUF_X1M_A9TR40]]
proc ::sft::discover_clock_delay_cell {} { return DLY4_X1M_A9TR40 }
proc ::sft::clock_pin_for_endpoint {endpoint} {
    if {$endpoint ne "u_cap/cap_reg/D"} { error "unexpected endpoint $endpoint" }
    return u_cap/cap_reg/CK
}
::sft::inject_capture_skew early 2
assert_equal [llength $::sft::changes] 2 \
    "capture-clock injection records every cell in a multi-level chain"
set clock_chain_1 [dict get [lindex $::sft::changes 0] inst]
set clock_chain_2 [dict get [lindex $::sft::changes 1] inst]
foreach action $::sft::changes {
    assert_equal [dict get $action kind] capture_clock_injection \
        "capture-clock provenance kind"
    assert_equal [dict get $action cell] DLY4_X1M_A9TR40 \
        "capture-clock chain uses one exact reference"
    assert_equal [dict get $action term] u_cap/cap_reg/CK \
        "capture-clock chain uses one exact term"
}
set ::mock_chain_drivers [dict create \
    u_cap/cap_reg/CK $clock_chain_2 \
    ${clock_chain_2}/A $clock_chain_1 \
    ${clock_chain_1}/A u_cap/original_clock_driver]
::sft::validate_injected_repeater_chains
assert_error_match {
    ::sft::inject_capture_skew early 0
} {*count must be a positive integer*} \
    "capture-clock chain count is positive"
set ::sft::targets [concat $::sft::targets [list [dict create \
    endpoint u_cap/second_reg/D timing early driver_ref BUF_X1M_A9TR40]]]
assert_error_match {
    ::sft::inject_capture_skew early 1
} {*requires exactly one early endpoint*} \
    "capture-clock chain rejects multiple early endpoints"

# Hold repairs use their own exact case-local reference and repeat count.  A
# frozen replay cannot silently fall back to the global candidate ordering.
array set ::SFT_CASE {
    id HOLD_UT
    calibration_status FROZEN
    max_eco_cells 4
    repair_delay_cells_per_endpoint 2
}
set ::SFT_REPAIR_DELAY_CELL DLY4_X1M_A9TR40
set ::SFT_DELAY_CELLS {DLY2_X4M_A9TR40 DLY4_X1M_A9TR40}
set ::mock_references {
    DLY2_X4M_A9TR40
    DLY4_X1M_A9TR40
}
set ::sft::targets [list \
    [dict create endpoint u_hold_1/D timing early] \
    [dict create endpoint u_hold_2/D timing early]]
set ::sft::repair_commands {}
set ::sft::repair_operations {}
set ::sft::functional_proofs {}
set ::sft::eco_count 0
::sft::plan_hold_delays
assert_equal [llength $::sft::repair_operations] 4 \
    "two frozen hold-delay cells are planned per early endpoint"
foreach operation $::sft::repair_operations {
    assert_equal [dict get $operation cell] DLY4_X1M_A9TR40 \
        "hold repair ignores reordered global delay candidates"
}
set ::SFT_REPAIR_DELAY_CELL {}
assert_error_match {
    ::sft::repair_delay_cell
} {*frozen hold-delay repair has no exact cell reference*} \
    "frozen hold repair cannot fall back to global delay candidates"

# Exported repair scripts have one canonical physical closeout.  The hidden
# injection flow now uses the same targeted-routing policy so the physical
# before/after comparison is symmetric.
set closeout_test_path [file join /tmp "sft_runtime_closeout_[pid].tcl"]
proc write_closeout_test_fix {path body} {
    set stream [open $path w]
    puts -nonewline $stream $body
    close $stream
}
set surgical_fix {
setEcoMode -batchMode true
ecoChangeCell -inst u_drv -cell BUF_X2M_A9TR40
setEcoMode -batchMode false
refinePlace -eco true
ecoRoute -target
}
array set ::SFT_CASE {repair_mode surgical type setup}
write_closeout_test_fix $closeout_test_path [string trimleft $surgical_fix "\n"]
::sft::validate_concrete_fix_policy $closeout_test_path

write_closeout_test_fix $closeout_test_path [string map \
    {"ecoRoute -target" "ecoRoute"} [string trimleft $surgical_fix "\n"]]
assert_error_match {
    ::sft::validate_concrete_fix_policy $closeout_test_path
} {*must be exactly ecoRoute -target*} "plain ecoRoute repair closeout is rejected"

write_closeout_test_fix $closeout_test_path [string map \
    {"ecoRoute -target" "ecoRoute -target -timingDriven"} \
    [string trimleft $surgical_fix "\n"]]
assert_error_match {
    ::sft::validate_concrete_fix_policy $closeout_test_path
} {*must be exactly ecoRoute -target*} "ecoRoute closeout arguments are exact"

write_closeout_test_fix $closeout_test_path \
    "[string trimleft $surgical_fix "\n"]ecoRoute -target\n"
assert_error_match {
    ::sft::validate_concrete_fix_policy $closeout_test_path
} {*requires exactly one ecoRoute -target*} "duplicate ecoRoute closeout is rejected"

write_closeout_test_fix $closeout_test_path [string map \
    {"refinePlace -eco true" "refinePlace -eco true -preserveRouting"} \
    [string trimleft $surgical_fix "\n"]]
assert_error_match {
    ::sft::validate_concrete_fix_policy $closeout_test_path
} {*must be exactly refinePlace -eco true*} "refinePlace closeout arguments are exact"

write_closeout_test_fix $closeout_test_path [string map \
    {"refinePlace -eco true" \
     "refinePlace -eco true\nrefinePlace -eco true"} \
    [string trimleft $surgical_fix "\n"]]
assert_error_match {
    ::sft::validate_concrete_fix_policy $closeout_test_path
} {*requires exactly one refinePlace -eco true*} \
    "duplicate refinePlace closeout is rejected"

write_closeout_test_fix $closeout_test_path [string map \
    {"refinePlace -eco true\necoRoute -target" \
     "ecoRoute -target\nrefinePlace -eco true"} \
    [string trimleft $surgical_fix "\n"]]
assert_error_match {
    ::sft::validate_concrete_fix_policy $closeout_test_path
} {*must follow every ECO action in refinePlace then ecoRoute order*} \
    "physical closeout order is fixed"

set native_fix {
optDesign -postRoute -setup -selectedTerms reports/native_selected_terms.txt -incr
refinePlace -eco true
ecoRoute -target
}
array set ::SFT_CASE {repair_mode native type setup}
write_closeout_test_fix $closeout_test_path [string trimleft $native_fix "\n"]
::sft::validate_concrete_fix_policy $closeout_test_path
file delete -force $closeout_test_path

puts SFT_RUNTIME_UNIT_TESTS_PASSED

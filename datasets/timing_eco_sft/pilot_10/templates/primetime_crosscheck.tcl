# PrimeTime post-ECO timing crosscheck, one analysis corner per invocation.
#
# Required settings are supplied only as explicit environment variables by
# the hash-binding runner:
#   PT_TOP, PT_NETLIST, PT_LIB, PT_SPEF, PT_MODE, PT_REPORT_DIR,
#   SYNOPSYS_LC_ROOT, plus exactly one mode-specific constraint:
#   PT_SETUP_SDC when PT_MODE=setup or PT_HOLD_SDC when PT_MODE=hold.
# Optional settings:
#   PT_MIN_SLACK_NS (0.010), PT_MAX_VIOLATING_PATHS (100000),
#   PT_REPORT_MAX_PATHS (100)
#
# Run this script twice for a case: setup uses the SS library/SPEF and
# PT_MODE=setup; hold uses the FF library/SPEF and PT_MODE=hold.

namespace eval ::ptx {
    variable marker_prefix "PT_CROSSCHECK_V1"
    variable mode "unknown"
    variable delay_type "unknown"
    variable report_dir "."
}

proc ::ptx::setting {name {default "__PTX_REQUIRED__"}} {
    if {[info exists ::env($name)]} {
        set value $::env($name)
    } elseif {$default ne "__PTX_REQUIRED__"} {
        set value $default
    } else {
        error "required setting $name is not defined"
    }
    if {[string trim $value] eq ""} {
        error "setting $name must not be empty"
    }
    return $value
}

proc ::ptx::setting_exists {name} {
    return [info exists ::env($name)]
}

proc ::ptx::require_readable_file {path label} {
    if {![file isfile $path]} {
        error "$label is not a regular file: $path"
    }
    if {![file readable $path]} {
        error "$label is not readable: $path"
    }
    return [file normalize $path]
}

proc ::ptx::require_nonnegative_integer {value label} {
    if {![string is integer -strict $value] || $value < 1} {
        error "$label must be a positive integer, got: $value"
    }
    return $value
}

proc ::ptx::require_number {value label} {
    if {![string is double -strict $value]} {
        error "$label must be numeric, got: $value"
    }
    return [expr {double($value)}]
}

proc ::ptx::safe_token {value} {
    set token [string trim $value]
    regsub -all {[^A-Za-z0-9_.:+/-]} $token {_} token
    if {$token eq ""} {
        return "unknown"
    }
    return $token
}

proc ::ptx::emit_marker {fields} {
    variable marker_prefix
    variable report_dir
    variable mode

    set tokens [list $marker_prefix]
    foreach {key value} $fields {
        lappend tokens "${key}=[::ptx::safe_token $value]"
    }
    set line [join $tokens " "]
    puts $line
    flush stdout

    file mkdir $report_dir
    set result_file [file join $report_dir "${mode}.result"]
    set stream [open $result_file w]
    puts $stream $line
    close $stream
}

proc ::ptx::emit_error_marker {} {
    variable mode
    variable delay_type
    variable report_dir

    if {[info exists ::env(PT_MODE)]} {
        set candidate [string tolower [string trim $::env(PT_MODE)]]
        if {$candidate in {setup hold}} {
            set mode $candidate
            set delay_type [expr {$mode eq "setup" ? "max" : "min"}]
        }
    }
    if {[info exists ::env(PT_REPORT_DIR)] && [string trim $::env(PT_REPORT_DIR)] ne ""} {
        set report_dir $::env(PT_REPORT_DIR)
    }
    ::ptx::emit_marker [list \
        status ERROR \
        mode $mode \
        delay_type $delay_type \
        reason execution_error]
}

proc ::ptx::redirect_report {path command_body} {
    # Report generation is part of the evidence contract.  Let any command
    # error abort the crosscheck instead of creating a plausible-looking file.
    redirect -file $path $command_body
}

proc ::ptx::timing_metrics {delay_type max_violating_paths} {
    set worst [get_timing_paths \
        -delay_type $delay_type \
        -max_paths 1 \
        -nworst 1]
    if {[sizeof_collection $worst] != 1} {
        error "PrimeTime returned no constrained $delay_type timing path"
    }
    set wns [::ptx::require_number [get_attribute $worst slack] "worst slack"]

    set violating [get_timing_paths \
        -delay_type $delay_type \
        -slack_lesser_than 0.0 \
        -max_paths $max_violating_paths \
        -nworst 1]
    set violating_paths [sizeof_collection $violating]
    if {$violating_paths >= $max_violating_paths} {
        error "violating-path enumeration reached PT_MAX_VIOLATING_PATHS=$max_violating_paths"
    }

    set tns 0.0
    foreach_in_collection path $violating {
        set slack [::ptx::require_number [get_attribute $path slack] "path slack"]
        if {$slack < 0.0} {
            set tns [expr {$tns + $slack}]
        }
    }
    return [list $wns $tns $violating_paths]
}

proc ::ptx::main {} {
    variable mode
    variable delay_type
    variable report_dir

    if {[info exists ::PT_CFG] || [array exists ::PT_CFG]} {
        error "global PT_CFG is forbidden in the isolated Gold crosscheck"
    }
    if {[info exists ::env(PT_CONFIG_TCL)]} {
        error "PT_CONFIG_TCL is forbidden in the isolated Gold crosscheck"
    }

    set mode [string tolower [::ptx::setting PT_MODE]]
    if {$mode ni {setup hold}} {
        error "PT_MODE must be setup or hold, got: $mode"
    }
    set delay_type [expr {$mode eq "setup" ? "max" : "min"}]
    set sdc_setting [expr {$mode eq "setup" ? "PT_SETUP_SDC" : "PT_HOLD_SDC"}]
    set opposite_sdc_setting [expr {
        $mode eq "setup" ? "PT_HOLD_SDC" : "PT_SETUP_SDC"
    }]
    foreach forbidden_setting [list PT_SDC $opposite_sdc_setting] {
        if {[::ptx::setting_exists $forbidden_setting]} {
            error "$forbidden_setting must not be defined for a $mode invocation"
        }
    }

    set top [::ptx::setting PT_TOP]
    if {![info exists ::env(SYNOPSYS_LC_ROOT)] ||
        [string trim $::env(SYNOPSYS_LC_ROOT)] eq ""} {
        error "SYNOPSYS_LC_ROOT must be explicitly defined and non-empty"
    }
    set lc_root [string trim $::env(SYNOPSYS_LC_ROOT)]
    set netlist [::ptx::require_readable_file [::ptx::setting PT_NETLIST] "gate netlist"]
    set liberty [::ptx::require_readable_file [::ptx::setting PT_LIB] "Liberty library"]
    set sdc [::ptx::require_readable_file [::ptx::setting $sdc_setting] "$mode SDC"]
    set spef [::ptx::require_readable_file [::ptx::setting PT_SPEF] "SPEF"]

    set report_dir [::ptx::setting PT_REPORT_DIR]
    file mkdir $report_dir
    set report_dir [file normalize $report_dir]
    set threshold [::ptx::require_number \
        [::ptx::setting PT_MIN_SLACK_NS 0.010] "PT_MIN_SLACK_NS"]
    if {$threshold < 0.0} {
        error "PT_MIN_SLACK_NS must be non-negative"
    }
    set max_violating_paths [::ptx::require_nonnegative_integer \
        [::ptx::setting PT_MAX_VIOLATING_PATHS 100000] \
        "PT_MAX_VIOLATING_PATHS"]
    set report_max_paths [::ptx::require_nonnegative_integer \
        [::ptx::setting PT_REPORT_MAX_PATHS 100] \
        "PT_REPORT_MAX_PATHS"]

    # Load exactly one mode-specific SDC, one PVT library, and one matching
    # SPEF per invocation.  Setup and hold constraints are separate evidence;
    # a setup-view SDC must never be silently reused for the hold run.
    read_lib $liberty
    set loaded_libs [get_libs *]
    if {[sizeof_collection $loaded_libs] != 1} {
        error "expected exactly one loaded Liberty library"
    }
    set main_lib [get_object_name $loaded_libs]
    set_app_var link_path [list * $main_lib]
    set_app_var link_create_black_boxes false
    read_verilog $netlist
    if {![link_design $top]} {
        error "PrimeTime failed to link requested top $top"
    }
    set linked_design [current_design]
    if {[sizeof_collection $linked_design] != 1} {
        error "PrimeTime has no unique current design after link_design $top"
    }
    set linked_top [get_object_name $linked_design]
    if {$linked_top ne $top} {
        error "PrimeTime current design $linked_top does not match requested top $top"
    }
    read_sdc $sdc
    # All machine-readable slack fields and PT_MIN_SLACK_NS are expressed in
    # nanoseconds.  Do not inherit a Liberty/SDC display unit implicitly.
    set_units -time ns

    set clocks [get_clocks *]
    set clock_count [sizeof_collection $clocks]
    if {$clock_count < 1} {
        error "no clocks exist after read_sdc"
    }
    # This is a post-route crosscheck.  Timing the CTS netlist with ideal
    # clocks would not be comparable to Innovus post-route setup/hold views.
    set_propagated_clock $clocks
    read_parasitics -format SPEF $spef
    update_timing

    set timing_report [file join $report_dir "${mode}_after_pt.rpt"]
    set check_report [file join $report_dir "${mode}_check_timing.rpt"]
    set global_report [file join $report_dir "${mode}_global_timing.rpt"]
    set parasitic_report [file join $report_dir "${mode}_annotated_parasitics.rpt"]
    set unannotated_report [file join $report_dir "${mode}_unannotated_parasitics.rpt"]
    set coverage_report [file join $report_dir "${mode}_analysis_coverage.rpt"]
    set unconstrained_report [file join $report_dir "${mode}_unconstrained_endpoints.rpt"]
    set units_report [file join $report_dir "${mode}_units.rpt"]

    ::ptx::redirect_report $timing_report [list report_timing \
        -delay_type $delay_type \
        -path_type full_clock_expanded \
        -input_pins \
        -nets \
        -transition_time \
        -capacitance \
        -significant_digits 6 \
        -max_paths $report_max_paths \
        -nworst 1]
    ::ptx::redirect_report $check_report [list check_timing -verbose]
    ::ptx::redirect_report $global_report [list report_global_timing]
    ::ptx::redirect_report $parasitic_report [list report_annotated_parasitics -check]
    ::ptx::redirect_report $unannotated_report [list report_annotated_parasitics \
        -check -list_not_annotated -ignore_partially_annotated]
    ::ptx::redirect_report $coverage_report [list report_analysis_coverage \
        -status_details [list untested violated met]]
    ::ptx::redirect_report $unconstrained_report [list check_timing \
        -verbose -include unconstrained_endpoints]
    ::ptx::redirect_report $units_report [list report_units -nosplit]

    lassign [::ptx::timing_metrics $delay_type $max_violating_paths] \
        wns tns violating_paths
    set epsilon 1.0e-9
    set passed [expr {
        $wns >= $threshold &&
        abs($tns) <= $epsilon &&
        $violating_paths == 0
    }]
    set status [expr {$passed ? "PASS" : "FAIL"}]

    set tool_version unknown
    if {[info exists ::sh_product_version]} {
        set tool_version $::sh_product_version
    } else {
        catch {set tool_version [get_app_var sh_product_version]}
    }
    ::ptx::emit_marker [list \
        status $status \
        mode $mode \
        delay_type $delay_type \
        top $top \
        wns_ns [format %.9f $wns] \
        tns_ns [format %.9f $tns] \
        violating_paths $violating_paths \
        threshold_ns [format %.9f $threshold] \
        parasitics_read 1 \
        propagated_clocks 1 \
        clock_count $clock_count \
        tool_version $tool_version \
        lc_root $lc_root]

    return [expr {$passed ? 0 : 3}]
}

set ::ptx_exit_code 2
if {[catch {set ::ptx_exit_code [::ptx::main]} message options]} {
    catch {::ptx::emit_error_marker}
    puts stderr "PT_CROSSCHECK_ERROR: $message"
    if {[dict exists $options -errorinfo]} {
        puts stderr [dict get $options -errorinfo]
    }
    exit 2
}
exit $::ptx_exit_code

# One-process/one-trial hidden calibrator for Mock LEF Batch-100.
#
# The Python queue launches a fresh Innovus process for every candidate.  This
# file deliberately restores the qualified baseline exactly once; Innovus
# rejects repeated restoreDesign in a live process as unreliable.

namespace eval ::b100_cal {
    variable out ""
}

proc ::b100_cal::env {name {default ""}} {
    if {[info exists ::env($name)] && [string trim $::env($name)] ne ""} {
        return $::env($name)
    }
    if {$default ne ""} { return $default }
    error "missing required environment variable $name"
}

proc ::b100_cal::resize {operations} {
    setEcoMode -batchMode true
    foreach operation $operations {
        lassign $operation instance reference
        ecoChangeCell -inst $instance -cell $reference
    }
    setEcoMode -batchMode false
}

proc ::b100_cal::full_postroute_timing {stage} {
    if {[llength [info commands timeDesign]] != 1} {
        error "timeDesign unavailable for durable calibration timing at $stage"
    }
    puts "B100_CALIBRATION_FULL_TIMING_BEGIN $::B100_CASE_ID stage=$stage"
    timeDesign -postRoute
    puts "B100_CALIBRATION_FULL_TIMING_END $::B100_CASE_ID stage=$stage"
}

proc ::b100_cal::materialize {} {
    set raw [split [::b100_cal::env B100_LEVELS] ,]
    if {[llength $raw] != [llength $::B100_INJECTION_CANDIDATES]} {
        error "B100_LEVELS cardinality mismatch"
    }
    set operations {}
    foreach level $raw candidate $::B100_INJECTION_CANDIDATES {
        if {![string is integer -strict $level] || $level < 0} {
            error "invalid injection candidate level $level"
        }
        lassign $candidate instance baseline references
        if {$level >= [llength $references]} {
            error "candidate level $level is out of range for $instance"
        }
        lappend operations [list $instance [lindex $references $level]]
    }
    return $operations
}

proc ::b100_cal::path_slacks {} {
    set result [dict create]
    set paths [report_timing -collection -late -view $::SFT_SETUP_VIEW \
        -path_type full_clock -max_paths 20000 -nworst 1]
    foreach_in_collection path $paths {
        set endpoint_object [get_db $path .capturing_point]
        set endpoint [get_db $endpoint_object .name]
        set slack [expr {double([get_db $path .slack])}]
        if {![dict exists $result $endpoint] || $slack < [dict get $result $endpoint]} {
            dict set result $endpoint $slack
        }
    }
    return $result
}

proc ::b100_cal::write_slacks {path slacks} {
    set stream [open $path w]
    puts $stream "endpoint\tslack_ns"
    dict for {endpoint slack} $slacks {
        puts $stream "$endpoint\t$slack"
    }
    close $stream
}

proc ::b100_cal::target_values {slacks} {
    set result [dict create]
    foreach endpoint $::B100_TARGET_ENDPOINTS {
        if {![dict exists $slacks $endpoint]} {
            error "timing collection omitted target endpoint $endpoint"
        }
        dict set result $endpoint [dict get $slacks $endpoint]
    }
    return $result
}

proc ::b100_cal::main {} {
    variable out
    set mode [string toupper [::b100_cal::env B100_CAL_MODE]]
    if {$mode ni {INJECTION SENSITIVITY REPAIR}} {
        error "B100_CAL_MODE must be INJECTION, SENSITIVITY, or REPAIR"
    }
    set out [file normalize [::b100_cal::env B100_CALIBRATION_DIR]]
    if {[file exists $out]} { error "calibration leaf already exists: $out" }
    file mkdir $out
    set ::env(TMPDIR) [file join $out tmp]
    file mkdir $::env(TMPDIR)
    uplevel #0 [list source [file normalize [::b100_cal::env B100_CASE_CONFIG]]]
    foreach required {
        B100_CASE_ID B100_TARGET_ENDPOINTS B100_PROTECTED_ENDPOINTS
        B100_NOMINAL_NS B100_TOLERANCE_NS B100_INJECTION_CANDIDATES
        B100_REPAIR_OPERATIONS
    } {
        if {![info exists ::$required]} { error "case config lacks ::$required" }
    }

    # Exactly one restore per process.
    source [file join [::b100_cal::env B100_BASELINE_DIR] restore.tcl]
    setMultiCpuUsage -localCpu 4
    set_analysis_view -setup [list $::SFT_SETUP_VIEW] -hold [list $::SFT_HOLD_VIEW]
    set injection [::b100_cal::materialize]
    ::b100_cal::resize $injection
    refinePlace -eco true
    ::b100_cal::full_postroute_timing injection
    set before [::b100_cal::path_slacks]
    ::b100_cal::write_slacks [file join $out injection_slacks.tsv] $before

    if {$mode eq "INJECTION"} {
        puts "B100_CALIBRATION_TRIAL_COMPLETE $::B100_CASE_ID"
        exit 0
    }
    if {$mode eq "SENSITIVITY"} {
        set index [::b100_cal::env B100_REPAIR_INDEX]
        if {![string is integer -strict $index] || $index < 0 ||
            $index >= [llength $::B100_REPAIR_OPERATIONS]} {
            error "invalid B100_REPAIR_INDEX"
        }
        lassign [lindex $::B100_REPAIR_OPERATIONS $index] instance old_ref new_ref
        set target_before [::b100_cal::target_values $before]
        ::b100_cal::resize [list [list $instance $new_ref]]
        ::b100_cal::full_postroute_timing sensitivity
        set target_after [::b100_cal::target_values [::b100_cal::path_slacks]]
        set stream [open [file join $out sensitivity.tsv] w]
        puts $stream "instance\tendpoint\tdelta_ns"
        foreach endpoint $::B100_TARGET_ENDPOINTS {
            puts $stream "$instance\t$endpoint\t[expr {
                [dict get $target_after $endpoint] - [dict get $target_before $endpoint]
            }]"
        }
        close $stream
        puts "B100_CALIBRATION_SENSITIVITY_COMPLETE $::B100_CASE_ID index=$index"
        exit 0
    }

    set repairs {}
    foreach operation $::B100_REPAIR_OPERATIONS {
        lappend repairs [list [lindex $operation 0] [lindex $operation 2]]
    }
    ::b100_cal::resize $repairs
    ::b100_cal::full_postroute_timing repair_before_refine
    set before_refine [::b100_cal::path_slacks]
    ::b100_cal::write_slacks [file join $out repair_before_refine.tsv] $before_refine
    refinePlace -eco true
    ::b100_cal::full_postroute_timing repair_after_refine
    set after_refine [::b100_cal::path_slacks]
    ::b100_cal::write_slacks [file join $out repair_after_refine.tsv] $after_refine
    puts "B100_CALIBRATION_REPAIR_COMPLETE $::B100_CASE_ID"
    exit 0
}

::b100_cal::main

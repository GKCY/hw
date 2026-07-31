# Frozen Batch-100 calibration/replay runtime.
#
# Required environment:
#   B100_MODE          INJECT or REPLAY
#   B100_TASK_DIR      new task leaf
#   B100_BASELINE_DIR  exact qualified baseline (INJECT only)
#   B100_CASE_CONFIG   frozen Tcl data file
#   B100_FIX_TCL       canonical fix (REPLAY only)
#   B100_CHECKPOINT    violating.enc wrapper (REPLAY only)
#
# The case config is generated from structured binding JSON and may contain
# data assignments only.  This runtime never performs routing optimization.

namespace eval ::b100 {
    variable task ""
    variable reports ""
}
set ::SFT_SETUP_VIEW functional_setup_ss
set ::SFT_HOLD_VIEW functional_hold_ff

proc ::b100::env {name {default ""}} {
    if {[info exists ::env($name)] && [string trim $::env($name)] ne ""} {
        return $::env($name)
    }
    if {$default ne ""} { return $default }
    error "missing required environment variable $name"
}

proc ::b100::path_slacks {} {
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

proc ::b100::checkpoint_portability_fail {reason details} {
    puts "B100_CHECKPOINT_PORTABILITY_FAIL $::B100_CASE_ID reason=$reason $details"
    error "checkpoint portability gate failed for $::B100_CASE_ID: $reason"
}

proc ::b100::checkpoint_portability_gate {} {
    variable reports
    set slacks [::b100::path_slacks]
    set stream [open [file join $reports checkpoint_portability_slacks.tsv] w]
    puts $stream "endpoint\tslack_ns"
    foreach endpoint [lsort -dictionary [dict keys $slacks]] {
        puts $stream "$endpoint\t[dict get $slacks $endpoint]"
    }
    close $stream

    set targets [lsort -dictionary -unique $::B100_TARGET_ENDPOINTS]
    if {[llength $targets] != [llength $::B100_TARGET_ENDPOINTS] ||
        ![llength $targets]} {
        ::b100::checkpoint_portability_fail malformed_targets \
            "target_count=[llength $::B100_TARGET_ENDPOINTS]"
    }
    set negatives {}
    dict for {endpoint slack} $slacks {
        if {$slack < 0.0} { lappend negatives $endpoint }
    }
    set negatives [lsort -dictionary $negatives]
    if {$negatives ne $targets} {
        ::b100::checkpoint_portability_fail negative_endpoint_set \
            "expected_count=[llength $targets] actual_count=[llength $negatives]"
    }

    set wns [dict get $slacks [lindex $targets 0]]
    foreach endpoint [lrange $targets 1 end] {
        set slack [dict get $slacks $endpoint]
        if {$slack < $wns} { set wns $slack }
    }
    set nominal [expr {double($::B100_NOMINAL_NS)}]
    set tolerance [expr {double($::B100_TOLERANCE_NS)}]
    set lower [expr {$nominal - $tolerance}]
    set upper [expr {$nominal + $tolerance}]
    if {$wns < $lower - 1.0e-12 || $wns > $upper + 1.0e-12} {
        ::b100::checkpoint_portability_fail severity \
            "wns_ns=$wns lower_ns=$lower upper_ns=$upper"
    }
    puts "B100_CHECKPOINT_PORTABILITY_PASS $::B100_CASE_ID wns_ns=$wns"
}

proc ::b100::report_stage {stage} {
    variable reports
    report_timing -late -view $::SFT_SETUP_VIEW -path_type full_clock \
        -net -max_paths 20000 -nworst 1 > [file join $reports setup_${stage}.rpt]
    if {[llength $::B100_TARGET_ENDPOINTS]} {
        report_timing -late -view $::SFT_SETUP_VIEW -path_type full_clock \
            -net -to [get_pins -quiet $::B100_TARGET_ENDPOINTS] \
            -max_paths [llength $::B100_TARGET_ENDPOINTS] -nworst 1 \
            > [file join $reports target_setup_${stage}.rpt]
    }
    report_timing -early -view $::SFT_HOLD_VIEW -path_type full_clock \
        -net -max_paths 20000 -nworst 1 > [file join $reports hold_${stage}.rpt]
    report_constraint -all_violators > [file join $reports drv_${stage}.rpt]
    verifyConnectivity -type all -report [file join $reports connectivity_${stage}.rpt]
    verify_drc -limit 1000000 -report [file join $reports drc_${stage}.rpt]
    checkPlace > [file join $reports placement_${stage}.rpt]
    ::b100::write_cell_snapshot $stage
    ::b100::write_connectivity_snapshot $stage
    ::b100::write_target_path_snapshot $stage
}

proc ::b100::db {object attributes {default ""}} {
    if {$object eq ""} { return $default }
    foreach attribute $attributes {
        if {![catch {get_db $object .$attribute} value] && $value ne ""} {
            return $value
        }
    }
    return $default
}

proc ::b100::write_cell_snapshot {stage} {
    variable reports
    set rows {}
    foreach_in_collection cell [get_cells -hierarchical *] {
        set name [get_object_name $cell]
        set reference [get_property $cell ref_name]
        if {$reference eq ""} { set reference [get_property $cell base_name] }
        lappend rows [list $name [file tail [lindex $reference 0]]]
    }
    set stream [open [file join $reports cells_${stage}.tsv] w]
    puts $stream "instance\tref"
    foreach row [lsort -dictionary -index 0 $rows] {
        puts $stream "[lindex $row 0]\t[lindex $row 1]"
    }
    close $stream
}

proc ::b100::write_connectivity_snapshot {stage} {
    variable reports
    set rows {}
    foreach_in_collection pin [get_pins -hierarchical *] {
        set pin_name [get_object_name $pin]
        set net_names {}
        foreach_in_collection net [get_nets -quiet -of_objects $pin] {
            lappend net_names [get_object_name $net]
        }
        lappend rows [list $pin_name [join [lsort -dictionary $net_names] ,]]
    }
    set stream [open [file join $reports pin_net_${stage}.tsv] w]
    puts $stream "pin\tnets"
    foreach row [lsort -dictionary -index 0 $rows] {
        puts $stream "[lindex $row 0]\t[lindex $row 1]"
    }
    close $stream
}

proc ::b100::write_target_path_snapshot {stage} {
    variable reports
    set stream [open [file join $reports target_paths_${stage}.tsv] w]
    puts $stream "endpoint\tcell_delay_ns\tnet_delay_ns\tslack_ns"
    foreach endpoint $::B100_TARGET_ENDPOINTS {
        set collection [report_timing -collection -late -view $::SFT_SETUP_VIEW \
            -path_type full_clock -to [get_pins -quiet [list $endpoint]] \
            -max_paths 1 -nworst 1]
        set found FALSE
        foreach_in_collection path $collection {
            set cell_delay 0.0
            set net_delay 0.0
            set index 0
            foreach point [::b100::db $path {timing_points} ""] {
                set pin [::b100::db $point {pin} ""]
                set pin_name [::b100::db $pin {name} ""]
                set direction ""
                set sequential FALSE
                if {[string first "/" $pin_name] >= 0} {
                    set direction [string tolower [lindex [::b100::db $pin {direction} ""] 0]]
                    set inst [::b100::db $pin {inst instance} ""]
                    set base [::b100::db $inst {base_cell cell} ""]
                    set sequential [string tolower [::b100::db $base {is_sequential sequential} FALSE]]
                }
                set delay [expr {double([::b100::db $point {delay} 0.0])}]
                if {$direction in {out output} && $sequential ni {1 true yes}} {
                    set cell_delay [expr {$cell_delay + $delay}]
                } elseif {$direction in {in input} && $index > 1} {
                    set net_delay [expr {$net_delay + $delay}]
                }
                incr index
            }
            puts $stream "$endpoint\t$cell_delay\t$net_delay\t[get_db $path .slack]"
            set found TRUE
            break
        }
        if {!$found} {
            close $stream
            error "no late timing path collected for target $endpoint at $stage"
        }
    }
    close $stream
}

proc ::b100::before_refine {command operation} {
    if {$operation ne "enter"} { return }
    ::b100::report_resize_stage
}

proc ::b100::report_resize_stage {} {
    variable reports
    ::b100::full_postroute_timing after_resize
    report_timing -late -view $::SFT_SETUP_VIEW -path_type full_clock \
        -net -max_paths 20000 -nworst 1 > [file join $reports setup_after_resize.rpt]
    report_timing -late -view $::SFT_SETUP_VIEW -path_type full_clock \
        -net -to [get_pins -quiet $::B100_TARGET_ENDPOINTS] \
        -max_paths [llength $::B100_TARGET_ENDPOINTS] -nworst 1 \
        > [file join $reports target_setup_after_resize.rpt]
    ::b100::write_target_path_snapshot after_resize
}

proc ::b100::resize {operations} {
    setEcoMode -batchMode true
    foreach operation $operations {
        if {[llength $operation] != 3 || [lindex $operation 0] ne "ecoChangeCell"} {
            error "case config contains a non-resize operation"
        }
        lassign $operation ignored instance reference
        ecoChangeCell -inst $instance -cell $reference
    }
    setEcoMode -batchMode false
}

proc ::b100::full_postroute_timing {stage} {
    if {[llength [info commands timeDesign]] != 1} {
        error "timeDesign unavailable for durable post-route timing at $stage"
    }
    puts "B100_FULL_TIMING_BEGIN $::B100_CASE_ID stage=$stage"
    timeDesign -postRoute
    puts "B100_FULL_TIMING_END $::B100_CASE_ID stage=$stage"
}

proc ::b100::assert_portable_checkpoint_shape {checkpoint} {
    if {![file isfile $checkpoint]} {
        error "portable checkpoint wrapper is missing: $checkpoint"
    }
    set tree "${checkpoint}.dat"
    if {![file isdirectory $tree]} {
        error "portable checkpoint data tree is missing: $tree"
    }
    set ascii_netlists [glob -nocomplain -types f \
        [file join $tree *.v.gz]]
    if {[llength $ascii_netlists] != 1} {
        error "portable checkpoint must contain exactly one top-level ASCII .v.gz netlist"
    }
    if {[file exists [file join $tree vbin]]} {
        error "portable checkpoint unexpectedly contains a binary vbin tree"
    }
}

proc ::b100::save_portable_checkpoint {checkpoint} {
    if {![info exists ::enc_save_binary]} {
        error "Innovus portable-save flag ::enc_save_binary is unavailable"
    }
    set original $::enc_save_binary
    if {![string is boolean -strict $original] || !$original} {
        error "unexpected ::enc_save_binary before portable save: $original"
    }
    puts "B100_PORTABLE_SAVE_BEGIN $::B100_CASE_ID enc_save_binary=0"
    set save_code [catch {
        set ::enc_save_binary 0
        saveDesign $checkpoint -rc
    } save_message save_options]
    set restore_code [catch {
        set ::enc_save_binary $original
    } restore_message restore_options]
    if {$restore_code} {
        return -options $restore_options $restore_message
    }
    if {$save_code} {
        return -options $save_options $save_message
    }
    if {$::enc_save_binary != $original} {
        error "::enc_save_binary was not restored after portable save"
    }
    ::b100::assert_portable_checkpoint_shape $checkpoint
    puts "B100_PORTABLE_SAVE_END $::B100_CASE_ID enc_save_binary=$::enc_save_binary"
    return $save_message
}

proc ::b100::load_config {} {
    set config [file normalize [::b100::env B100_CASE_CONFIG]]
    set before [lsort [info globals B100_*]]
    uplevel #0 [list source $config]
    set after [lsort [info globals B100_*]]
    foreach required {
        B100_CASE_ID B100_TARGET_ENDPOINTS B100_PROTECTED_ENDPOINTS
        B100_NOMINAL_NS B100_TOLERANCE_NS
        B100_INJECTION_OPERATIONS B100_REPAIR_OPERATIONS
    } {
        if {![info exists ::$required]} { error "case config lacks ::$required" }
    }
    foreach name $after {
        if {[lsearch -exact $before $name] < 0 && $name ni {
            B100_CASE_ID B100_TARGET_ENDPOINTS B100_PROTECTED_ENDPOINTS
            B100_NOMINAL_NS B100_TOLERANCE_NS
            B100_INJECTION_OPERATIONS B100_REPAIR_OPERATIONS
        }} {
            error "case config created unexpected global $name"
        }
    }
}

proc ::b100::main {} {
    variable task
    variable reports
    set mode [string toupper [::b100::env B100_MODE]]
    if {$mode ni {INJECT REPLAY}} { error "B100_MODE must be INJECT or REPLAY" }
    puts "B100_PROCESS_ID [pid]"
    set task [file normalize [::b100::env B100_TASK_DIR]]
    if {[file exists $task]} { error "task leaf already exists: $task" }
    file mkdir $task
    set reports [file join $task reports]
    file mkdir $reports
    set ::env(TMPDIR) [file join $task tmp]
    file mkdir $::env(TMPDIR)
    ::b100::load_config

    if {$mode eq "INJECT"} {
        set baseline [file normalize [::b100::env B100_BASELINE_DIR]]
        source [file join $baseline restore.tcl]
        setMultiCpuUsage -localCpu 4
        set_analysis_view -setup [list $::SFT_SETUP_VIEW] -hold [list $::SFT_HOLD_VIEW]
        ::b100::resize $::B100_INJECTION_OPERATIONS
        refinePlace -eco true
        ::b100::full_postroute_timing injection_freeze
        ::b100::report_stage before
        write_sdc -view $::SFT_SETUP_VIEW [file join $reports constraint_before.sdc]
        ::b100::save_portable_checkpoint [file join $task violating.enc]
        puts "B100_INJECTION_COMPLETE $::B100_CASE_ID"
    } else {
        set checkpoint [file normalize [::b100::env B100_CHECKPOINT]]
        uplevel #0 [list source $checkpoint]
        setMultiCpuUsage -localCpu 4
        set_analysis_view -setup [list $::SFT_SETUP_VIEW] -hold [list $::SFT_HOLD_VIEW]
        # Fail before applying the fix, incremental legalization, or any of the
        # expensive physical evidence commands if the frozen checkpoint did
        # not reproduce the calibrated violation in this fresh process.
        ::b100::checkpoint_portability_gate
        ::b100::report_stage before
        write_sdc -view $::SFT_SETUP_VIEW [file join $reports constraint_before.sdc]
        trace add execution refinePlace enter ::b100::before_refine
        source [file normalize [::b100::env B100_FIX_TCL]]
        trace remove execution refinePlace enter ::b100::before_refine
        ::b100::full_postroute_timing after_legalize
        ::b100::report_stage after_legalize
        write_sdc -view $::SFT_SETUP_VIEW [file join $reports constraint_after.sdc]
        puts "B100_REPLAY_COMPLETE $::B100_CASE_ID"
    }
    exit 0
}

::b100::main

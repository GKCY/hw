# Non-authoritative single-process screening for supervised Batch-100 work.
#
# Promising rows must still pass the ordinary fresh-process calibrator.  This
# script only avoids repeated Innovus startup while measuring discrete RVT
# alternatives from the frozen probe.

proc b100_screen_resize {instance reference} {
    setEcoMode -batchMode true
    ecoChangeCell -inst $instance -cell $reference
    setEcoMode -batchMode false
    refinePlace -eco true
}

proc b100_screen_slacks {} {
    set result [dict create]
    set paths [report_timing -collection -late -view $::SFT_SETUP_VIEW \
        -path_type full_clock -max_paths 20000 -nworst 1]
    foreach_in_collection path $paths {
        set endpoint [get_db [get_db $path .capturing_point] .name]
        set slack [expr {double([get_db $path .slack])}]
        if {![dict exists $result $endpoint] || $slack < [dict get $result $endpoint]} {
            dict set result $endpoint $slack
        }
    }
    return $result
}

if {![info exists ::env(B100_SCREEN_CONFIG)] ||
    ![info exists ::env(B100_SCREEN_OUT)] ||
    ![info exists ::env(B100_BASELINE_DIR)]} {
    error "manual screen environment is incomplete"
}
source [file normalize $::env(B100_SCREEN_CONFIG)]
if {![info exists ::B100_SCREEN_CANDIDATES] || ![llength $::B100_SCREEN_CANDIDATES]} {
    error "manual screen config has no candidates"
}
source [file join [file normalize $::env(B100_BASELINE_DIR)] restore.tcl]
setMultiCpuUsage -localCpu 4
set_analysis_view -setup [list $::SFT_SETUP_VIEW] -hold [list $::SFT_HOLD_VIEW]

set stream [open [file normalize $::env(B100_SCREEN_OUT)] w]
puts $stream "candidate_index\tendpoint\tinstance\tbaseline_ref\tnew_ref\ttarget_slack_ns\tnegative_count\tnegative_endpoints"
foreach candidate $::B100_SCREEN_CANDIDATES {
    lassign $candidate index endpoint instance baseline_ref new_ref
    b100_screen_resize $instance $new_ref
    timeDesign -postRoute
    set slacks [b100_screen_slacks]
    if {![dict exists $slacks $endpoint]} {
        error "screen omitted target endpoint $endpoint"
    }
    set negatives {}
    dict for {name slack} $slacks {
        if {$slack < 0.0} { lappend negatives $name }
    }
    puts $stream "$index\t$endpoint\t$instance\t$baseline_ref\t$new_ref\t[dict get $slacks $endpoint]\t[llength $negatives]\t[join [lsort $negatives] ,]"
    flush $stream
    b100_screen_resize $instance $baseline_ref
}
close $stream
puts "B100_MANUAL_SCREEN_COMPLETE"
exit 0

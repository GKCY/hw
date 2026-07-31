# Fast, non-authoritative electrical ranking for supervised Batch-100 work.
#
# This intentionally omits refinePlace and all physical/global acceptance
# checks.  A promising row must be rerun by manual_screen_resize.tcl and then
# pass the ordinary fresh-process calibrator plus two independent replays.

proc b100_raw_change {instance reference} {
    setEcoMode -batchMode true
    ecoChangeCell -inst $instance -cell $reference
    setEcoMode -batchMode false
}

proc b100_raw_target_slack {endpoint} {
    set paths [report_timing -collection -late -view $::SFT_SETUP_VIEW \
        -to $endpoint -path_type full_clock -max_paths 1 -nworst 1]
    if {[sizeof_collection $paths] != 1} {
        error "raw screen omitted target endpoint $endpoint"
    }
    set path [index_collection $paths 0]
    return [expr {double([get_db $path .slack])}]
}

if {![info exists ::env(B100_SCREEN_CONFIG)] ||
    ![info exists ::env(B100_SCREEN_OUT)] ||
    ![info exists ::env(B100_BASELINE_DIR)]} {
    error "manual raw screen environment is incomplete"
}
source [file normalize $::env(B100_SCREEN_CONFIG)]
if {![info exists ::B100_SCREEN_CANDIDATES] || ![llength $::B100_SCREEN_CANDIDATES]} {
    error "manual raw screen config has no candidates"
}
source [file join [file normalize $::env(B100_BASELINE_DIR)] restore.tcl]
setMultiCpuUsage -localCpu 4
set_analysis_view -setup [list $::SFT_SETUP_VIEW] -hold [list $::SFT_HOLD_VIEW]
setEcoMode -refinePlace false

set stream [open [file normalize $::env(B100_SCREEN_OUT)] w]
puts $stream "candidate_index\tendpoint\tinstance\tbaseline_ref\tnew_ref\ttarget_slack_ns\tnegative_count\tnegative_endpoints"
foreach candidate $::B100_SCREEN_CANDIDATES {
    lassign $candidate index endpoint instance baseline_ref new_ref
    b100_raw_change $instance $new_ref
    set slack [b100_raw_target_slack $endpoint]
    puts $stream "$index\t$endpoint\t$instance\t$baseline_ref\t$new_ref\t$slack\t-1\t"
    flush $stream
    b100_raw_change $instance $baseline_ref
}
close $stream
puts "B100_MANUAL_SCREEN_COMPLETE"
exit 0

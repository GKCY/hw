# Read-only Innovus 21.10 timing-path API probe used while developing the
# batch-local collector.  It restores the exact qualified checkpoint and
# records which timing-point attributes this installed build exposes.

if {![info exists ::env(B100_BASELINE_DIR)]} {
    error "B100_BASELINE_DIR is required"
}
if {![info exists ::env(B100_API_PROBE_OUT)]} {
    error "B100_API_PROBE_OUT is required"
}

set baseline_dir [file normalize $::env(B100_BASELINE_DIR)]
set output [file normalize $::env(B100_API_PROBE_OUT)]
if {[file exists $output]} {
    error "refusing to overwrite $output"
}
file mkdir [file dirname $output]

source [file join $baseline_dir restore.tcl]
set paths [report_timing -collection -late -view $::SFT_SETUP_VIEW \
    -path_type full_clock -max_paths 1]
set path [index_collection $paths 0]

set stream [open $output w]
puts $stream "PATH=[get_object_name $path]"
foreach property {
    slack launching_point capturing_point timing_points points
    arrival arrival_time required required_time transition
} {
    if {[catch {get_db $path .$property} value]} {
        puts $stream "PATH_PROPERTY\t$property\tERROR\t$value"
    } else {
        puts $stream "PATH_PROPERTY\t$property\tOK\t$value"
    }
}

set points ""
foreach property {timing_points points} {
    if {![catch {get_db $path .$property} candidate] &&
        [llength $candidate] > 0} {
        set points $candidate
        puts $stream "POINT_SOURCE=$property"
        break
    }
}
set index 0
foreach point $points {
    puts $stream "POINT_BEGIN=$index"
    foreach property {
        name full_name pin pin_name object cell inst instance
        arrival arrival_time delay cell_delay net_delay transition slew
        capacitance load rise_fall edge
    } {
        if {[catch {get_db $point .$property} value]} {
            puts $stream "POINT_PROPERTY\t$index\t$property\tERROR\t$value"
        } else {
            puts $stream "POINT_PROPERTY\t$index\t$property\tOK\t$value"
        }
    }
    incr index
}
puts $stream "POINT_COUNT=$index"
close $stream
puts "B100_PATH_API_PROBE_COMPLETE"
exit 0

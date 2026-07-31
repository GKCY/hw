# Batch-100 read-only Innovus probe.
#
# Required:
#   B100_BASELINE_DIR   exact qualified portable baseline bundle
#   B100_PROBE_DIR      new output directory on /work
# Optional:
#   B100_MAX_PATHS      late paths to collect (default 20000)
#
# This script never mutates or saves the design.

namespace eval ::b100_probe {
    variable out ""
    variable seen_refs {}
    variable seen_cells [dict create]
}

proc ::b100_probe::env {name {default ""}} {
    if {[info exists ::env($name)] && [string trim $::env($name)] ne ""} {
        return $::env($name)
    }
    if {$default ne ""} { return $default }
    error "missing required environment variable $name"
}

proc ::b100_probe::escape {value} {
    return [string map [list "\\" "\\\\" "\t" "\\t" "\n" "\\n" "\r" "\\r"] $value]
}

proc ::b100_probe::row {stream fields} {
    set encoded {}
    foreach field $fields { lappend encoded [::b100_probe::escape $field] }
    puts $stream [join $encoded "\t"]
}

proc ::b100_probe::db {object properties {default ""}} {
    foreach property $properties {
        if {![catch {get_db $object .$property} value] && [string trim $value] ne ""} {
            return $value
        }
    }
    return $default
}

proc ::b100_probe::name {object} {
    if {$object eq ""} { return "" }
    if {![catch {get_db $object .name} value] && [string trim $value] ne ""} {
        set value [lindex $value 0]
        regsub "^$::SFT_BASE_TOP/" $value "" value
        return $value
    }
    return ""
}

proc ::b100_probe::groups {endpoint} {
    set groups {}
    if {[regexp {(^|/)u_exp/} $endpoint]} { lappend groups exp }
    if {[string match *pp_out* $endpoint] || [string match *dat_out* $endpoint]} {
        lappend groups pipeline
    }
    if {![regexp {(^|/)u_exp/} $endpoint] &&
        ![regexp {(^|/)u_mul_[^/]*/} $endpoint]} {
        lappend groups top_tree
    }
    return $groups
}

proc ::b100_probe::edge {point} {
    set value [::b100_probe::db $point {transition_type transition edge rise_fall} ""]
    return [string tolower [lindex $value 0]]
}

proc ::b100_probe::pin_detail {point} {
    set pin [::b100_probe::db $point {pin} ""]
    if {$pin eq ""} {
        return [dict create pin "" inst "" ref "" direction "" sequential UNKNOWN \
            clock UNKNOWN dont_touch UNKNOWN net "" net_cap "" max_cap ""]
    }
    # Timing-point pins are opaque handles in this Innovus build.  Its .name
    # property is common to both pins and ports, while hport/port objects do not
    # expose inst, net, clock, or max-cap attributes.  In this flattened top,
    # every instance pin is canonicalized as instance/pin and top ports have no
    # slash, so classify from the already-safe canonical name.
    set pin_name [::b100_probe::name $pin]
    if {[string first "/" $pin_name] < 0} {
        return [dict create pin $pin_name inst "" ref "" \
            direction [string tolower [lindex [::b100_probe::db $pin {direction} ""] 0]] \
            sequential UNKNOWN clock UNKNOWN dont_touch UNKNOWN net "" \
            net_cap "" max_cap ""]
    }
    set inst [::b100_probe::db $pin {inst instance} ""]
    set base [::b100_probe::db $inst {base_cell cell} ""]
    set net [::b100_probe::db $pin {net} ""]
    return [dict create \
        pin $pin_name \
        inst [::b100_probe::name $inst] \
        ref [file tail [::b100_probe::name $base]] \
        direction [string tolower [lindex [::b100_probe::db $pin {direction} ""] 0]] \
        sequential [::b100_probe::db $base {is_sequential sequential} UNKNOWN] \
        clock [::b100_probe::db $pin {is_clock clock} UNKNOWN] \
        dont_touch [::b100_probe::db $inst {dont_touch is_dont_touch} UNKNOWN] \
        net [::b100_probe::name $net] \
        net_cap "" \
        max_cap [::b100_probe::db $pin {max_capacitance} ""]]
}

proc ::b100_probe::collect_paths {maximum} {
    variable out
    variable seen_refs
    variable seen_cells
    set path_stream [open [file join $out paths.tsv] w]
    set point_stream [open [file join $out path_points.tsv] w]
    ::b100_probe::row $path_stream {
        stable_rank slack_ns beginpoint endpoint hierarchy_groups point_count
        data_cell_delay_ns data_net_delay_ns cell_delay_fraction net_delay_fraction
    }
    ::b100_probe::row $point_stream {
        stable_rank point_index pin inst ref direction edge delay_ns slew_ns
        delay_kind sequential clock dont_touch net net_capacitance max_capacitance
    }
    set command [list report_timing -collection -late -view $::SFT_SETUP_VIEW \
        -path_type full_clock -max_paths $maximum]
    if {[catch {uplevel #0 $command} collection]} {
        close $path_stream
        close $point_stream
        error "late timing collection failed: $collection"
    }
    set rank 0
    foreach_in_collection path $collection {
        set slack [::b100_probe::db $path {slack path_slack} ""]
        set beginpoint [::b100_probe::name [::b100_probe::db $path {launching_point beginpoint} ""]]
        set endpoint [::b100_probe::name [::b100_probe::db $path {capturing_point endpoint} ""]]
        set points [::b100_probe::db $path {timing_points} ""]
        set cell_delay 0.0
        set net_delay 0.0
        set index 0
        foreach point $points {
            set detail [::b100_probe::pin_detail $point]
            set delay [::b100_probe::db $point {delay} 0.0]
            set slew [::b100_probe::db $point {slew} ""]
            set direction [dict get $detail direction]
            set sequential [string tolower [dict get $detail sequential]]
            set kind other
            if {$direction in {out output} && $sequential ni {1 true yes}} {
                set kind cell
                set cell_delay [expr {$cell_delay + double($delay)}]
                set reference [dict get $detail ref]
                if {$reference ne "" && [lsearch -exact $seen_refs $reference] < 0} {
                    lappend seen_refs $reference
                }
                set instance [dict get $detail inst]
                if {$instance ne ""} {
                    dict set seen_cells $instance [dict create \
                        output_pin [dict get $detail pin] ref $reference]
                }
            } elseif {$direction in {in input} && $index > 1} {
                set kind net
                set net_delay [expr {$net_delay + double($delay)}]
            }
            ::b100_probe::row $point_stream [list \
                $rank $index [dict get $detail pin] [dict get $detail inst] \
                [dict get $detail ref] $direction [::b100_probe::edge $point] \
                $delay $slew $kind $sequential [dict get $detail clock] \
                [dict get $detail dont_touch] [dict get $detail net] \
                [dict get $detail net_cap] [dict get $detail max_cap]]
            incr index
        }
        set total [expr {$cell_delay + $net_delay}]
        set cell_fraction [expr {$total > 0.0 ? $cell_delay / $total : -1.0}]
        set net_fraction [expr {$total > 0.0 ? $net_delay / $total : -1.0}]
        ::b100_probe::row $path_stream [list \
            $rank $slack $beginpoint $endpoint [join [::b100_probe::groups $endpoint] ,] \
            $index $cell_delay $net_delay $cell_fraction $net_fraction]
        incr rank
    }
    close $path_stream
    close $point_stream
    return $rank
}

proc ::b100_probe::collect_reachability {} {
    variable out
    variable seen_cells
    set stream [open [file join $out cell_fanout_endpoints.tsv] w]
    ::b100_probe::row $stream {
        instance output_pin ref endpoint_count endpoints
    }
    foreach instance [lsort -dictionary [dict keys $seen_cells]] {
        set cell [dict get $seen_cells $instance]
        set output_pin [dict get $cell output_pin]
        set start [get_pins -quiet [list $output_pin]]
        if {[sizeof_collection $start] != 1} {
            close $stream
            error "cannot resolve unique output pin for reachability: $output_pin"
        }
        if {[catch {all_fanout -from $start -endpoints_only} collection]} {
            close $stream
            error "all_fanout failed for $output_pin: $collection"
        }
        set endpoints {}
        foreach_in_collection endpoint $collection {
            lappend endpoints [get_object_name $endpoint]
        }
        set endpoints [lsort -dictionary -unique $endpoints]
        ::b100_probe::row $stream [list \
            $instance $output_pin [dict get $cell ref] \
            [llength $endpoints] [join $endpoints ,]]
    }
    close $stream
}

proc ::b100_probe::drive {reference} {
    if {![regexp {^(.*)_X([0-9]+(?:P[0-9]+)?)([A-Z]*)_(.+)$} \
        $reference -> stem token flavor tail]} {
        return {}
    }
    return [list $stem [string map {P .} $token] $flavor $tail]
}

proc ::b100_probe::lib_signature {cell} {
    set inputs {}
    set outputs {}
    if {[catch {get_lib_pins -quiet -of_objects $cell} pins]} { return UNSUPPORTED }
    foreach_in_collection pin $pins {
        set name [file tail [::b100_probe::name $pin]]
        set direction [string tolower [lindex [::b100_probe::db $pin {direction} ""] 0]]
        if {$direction in {in input}} {
            lappend inputs $name
        } elseif {$direction in {out output}} {
            set function [::b100_probe::db $pin {function logic_function} UNSUPPORTED]
            set function [string map [list " " "" "\t" "" "\n" ""] $function]
            lappend outputs "$name=$function"
        }
    }
    if {![llength $inputs] || ![llength $outputs]} { return UNSUPPORTED }
    return "I:[join [lsort -unique $inputs] ,]|O:[join [lsort -unique $outputs] {;}]"
}

proc ::b100_probe::collect_ladders {} {
    variable out
    variable seen_refs
    set stream [open [file join $out drive_ladders.tsv] w]
    ::b100_probe::row $stream {
        source_ref source_drive stem flavor tail variant_ref variant_drive
        source_signature variant_signature equivalent active_copy_count
    }
    foreach source [lsort -unique $seen_refs] {
        set parsed [::b100_probe::drive $source]
        if {[llength $parsed] != 4} { continue }
        lassign $parsed stem source_drive flavor tail
        set source_cells [get_lib_cells -quiet "*/$source"]
        set source_signature ""
        set source_count 0
        foreach_in_collection source_cell $source_cells {
            incr source_count
            set signature [::b100_probe::lib_signature $source_cell]
            if {$source_signature eq ""} { set source_signature $signature }
            if {$signature ne $source_signature} { set source_signature COPY_MISMATCH }
        }
        set variants [get_lib_cells -quiet "*/${stem}_X*_${tail}"]
        set seen_variants {}
        foreach_in_collection variant $variants {
            set variant_ref [file tail [::b100_probe::name $variant]]
            if {[lsearch -exact $seen_variants $variant_ref] >= 0} { continue }
            lappend seen_variants $variant_ref
            set variant_parsed [::b100_probe::drive $variant_ref]
            if {[llength $variant_parsed] != 4 ||
                [lindex $variant_parsed 0] ne $stem ||
                [lindex $variant_parsed 2] ne $flavor ||
                [lindex $variant_parsed 3] ne $tail} {
                continue
            }
            set variant_signature [::b100_probe::lib_signature $variant]
            set equivalent [expr {$source_signature ne "UNSUPPORTED" &&
                $source_signature ne "COPY_MISMATCH" &&
                $source_signature eq $variant_signature ? "TRUE" : "FALSE"}]
            ::b100_probe::row $stream [list \
                $source $source_drive $stem $flavor $tail $variant_ref \
                [lindex $variant_parsed 1] $source_signature $variant_signature \
                $equivalent $source_count]
        }
    }
    close $stream
}

proc ::b100_probe::metadata {path_count {capture_reports 1}} {
    variable out
    set stream [open [file join $out metadata.tsv] w]
    ::b100_probe::row $stream {key value}
    foreach pair [list \
        [list schema mock_lef_batch100.probe.v1] \
        [list purpose readonly_binding_probe_never_gold] \
        [list design_mutations 0] \
        [list process_id [pid]] \
        [list tool_version [getVersion]] \
        [list top $::SFT_BASE_TOP] \
        [list setup_view $::SFT_SETUP_VIEW] \
        [list hold_view $::SFT_HOLD_VIEW] \
        [list checkpoint $::SFT_BASE_CHECKPOINT] \
        [list path_count $path_count]] {
        ::b100_probe::row $stream $pair
    }
    close $stream
    if {$capture_reports} {
        report_timing -late -view $::SFT_SETUP_VIEW -path_type full_clock \
            -max_paths 200 -nworst 1 > [file join $out baseline_setup.rpt]
        report_timing -early -view $::SFT_HOLD_VIEW -path_type full_clock \
            -max_paths 200 -nworst 1 > [file join $out baseline_hold.rpt]
        report_constraint -all_violators > [file join $out baseline_drv.rpt]
        write_sdc -view $::SFT_SETUP_VIEW [file join $out baseline_setup.sdc]
        write_sdc -view $::SFT_HOLD_VIEW [file join $out baseline_hold.sdc]
    }
}

proc ::b100_probe::main {} {
    variable out
    set baseline [file normalize [::b100_probe::env B100_BASELINE_DIR]]
    set out [file normalize [::b100_probe::env B100_PROBE_DIR]]
    set maximum [::b100_probe::env B100_MAX_PATHS 20000]
    if {![string is integer -strict $maximum] || $maximum < 100 || $maximum > 20000} {
        error "B100_MAX_PATHS must be an integer from 100 through 20000"
    }
    if {[file exists $out]} { error "refusing to overwrite probe directory $out" }
    file mkdir $out
    foreach required {manifest.tcl restore.tcl qualification.json QUALIFIED_CANDIDATE} {
        if {![file isfile [file join $baseline $required]]} {
            error "baseline lacks $required"
        }
    }
    source [file join $baseline restore.tcl]
    setMultiCpuUsage -localCpu 4
    set_analysis_view -setup [list $::SFT_SETUP_VIEW] -hold [list $::SFT_HOLD_VIEW]
    set count [::b100_probe::collect_paths $maximum]
    ::b100_probe::collect_reachability
    ::b100_probe::collect_ladders
    set capture_reports 1
    if {[info exists ::env(B100_PROBE_SMOKE_FAST)] &&
        [string is true -strict $::env(B100_PROBE_SMOKE_FAST)]} {
        set capture_reports 0
    }
    ::b100_probe::metadata $count $capture_reports
    puts "B100_PROBE_COMPLETE paths=$count mutations=0"
    exit 0
}

::b100_probe::main

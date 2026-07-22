# Innovus 21.10 read-only calibration/probe collector.
#
# Required environment:
#   SFT_BASELINE_DIR  immutable baseline bundle containing manifest.tcl and
#                     restore.tcl
#   SFT_PROBE_DIR     new/empty output directory for raw and typed evidence
# Optional environment:
#   SFT_PROBE_MAX_PATHS  timing paths per analysis (default: 10000)
#
# Run this file with a fresh `innovus -no_gui` process.  It restores the
# supplied post-route checkpoint, writes reports, and briefly exercises only
# the GUI selection state.  It never changes cells, routes, constraints, or
# placement and never saves a design.  Its output is calibration evidence,
# never a Gold/SFT sample.

namespace eval ::sft_probe {
    variable schema "innovus_readonly_probe.v1"
    variable out_dir ""
    variable baseline_dir ""
    variable unsupported {}
    variable artifacts {}
    variable candidates {}
    variable rejection_rows {}
    variable safe_endpoint ""
    variable library_rows {}
    variable family_rows {}
    variable reference_evidence {}
    variable drc_report_limit 1000000
}

proc ::sft_probe::require_env {name} {
    if {![info exists ::env($name)] || [string trim $::env($name)] eq ""} {
        error "required environment variable $name is not set"
    }
    return $::env($name)
}

proc ::sft_probe::write_text {path text} {
    file mkdir [file dirname $path]
    set stream [open $path w]
    puts -nonewline $stream $text
    close $stream
}

proc ::sft_probe::read_text {path} {
    if {![file isfile $path]} { return "" }
    set stream [open $path r]
    set value [read $stream]
    close $stream
    return $value
}

proc ::sft_probe::tsv_escape {value} {
    return [string map [list "\\" "\\\\" "\t" "\\t" "\n" "\\n" "\r" "\\r"] $value]
}

proc ::sft_probe::write_tsv {relative headers rows schema_name} {
    variable out_dir
    variable artifacts
    set path [file join $out_dir $relative]
    file mkdir [file dirname $path]
    set stream [open $path w]
    set escaped_headers {}
    foreach value $headers { lappend escaped_headers [::sft_probe::tsv_escape $value] }
    puts $stream [join $escaped_headers "\t"]
    foreach row $rows {
        if {[llength $row] != [llength $headers]} {
            close $stream
            error "$relative row has [llength $row] fields, expected [llength $headers]"
        }
        set escaped {}
        foreach value $row { lappend escaped [::sft_probe::tsv_escape $value] }
        puts $stream [join $escaped "\t"]
    }
    close $stream
    lappend artifacts [list $relative $schema_name [llength $rows] [file size $path]]
}

proc ::sft_probe::mark_unsupported {capability detail} {
    variable unsupported
    set pair [list $capability $detail]
    if {[lsearch -exact $unsupported $pair] < 0} { lappend unsupported $pair }
    # Keep the diagnostic machine-readable and on one line without attempting
    # to build a string-map list inside a nested quoted substitution.  The
    # original values remain losslessly preserved in `unsupported` and the TSV
    # evidence; only this stderr rendering folds whitespace.
    set log_capability [regsub -all {[[:space:]]+} [string trim $capability] {_}]
    set log_detail [regsub -all {[[:space:]]+} [string trim $detail] {_}]
    puts stderr [format {SFT_PROBE_UNSUPPORTED capability=%s detail=%s} \
        $log_capability $log_detail]
}

proc ::sft_probe::items {collection} {
    if {[llength [info commands sizeof_collection]] &&
        ![catch {sizeof_collection $collection}]} {
        set result {}
        foreach_in_collection object $collection { lappend result $object }
        return $result
    }
    return $collection
}

proc ::sft_probe::object_name {object} {
    foreach command [list \
        [list get_object_name $object] \
        [list get_property $object full_name] \
        [list get_property $object name] \
        [list get_db $object .full_name] \
        [list get_db $object .name]] {
        if {![catch {uplevel #0 $command} value] && [string trim $value] ne ""} {
            return [lindex $value 0]
        }
    }
    return $object
}

proc ::sft_probe::property_result {object names} {
    foreach name $names {
        foreach command [list \
            [list get_property $object $name] \
            [list get_db $object .$name]] {
            if {![catch {uplevel #0 $command} value] && [string trim $value] ne ""} {
                return [dict create supported 1 name $name value $value]
            }
        }
    }
    return [dict create supported 0 name "" value ""]
}

proc ::sft_probe::boolean_result {object names} {
    set result [::sft_probe::property_result $object $names]
    if {![dict get $result supported]} { return $result }
    set value [string tolower [lindex [dict get $result value] 0]]
    if {$value in {1 true yes on}} {
        dict set result value 1
    } elseif {$value in {0 false no off}} {
        dict set result value 0
    } else {
        dict set result supported 0
        dict set result value ""
    }
    return $result
}

proc ::sft_probe::assert_fresh_process {} {
    variable out_dir
    set rows {}
    set loaded 0
    foreach spec [list \
        [list get_db_current_design [list get_db current_design .name]] \
        [list dbGet_top_name [list dbGet top.name]]] {
        lassign $spec label command
        if {[catch {uplevel #0 $command} value]} {
            lappend rows [list $label ERROR "" $value]
            continue
        }
        set normalized [string trim $value]
        lappend rows [list $label OK $normalized ""]
        if {$normalized ne "" && $normalized ni {0 0x0 NULL null none}} { set loaded 1 }
    }
    ::sft_probe::write_tsv pre_restore_state.tsv \
        {query status value error} $rows pre_restore_state.v1
    if {$loaded} {
        error "a design was already loaded; collector requires a fresh Innovus process"
    }
}

proc ::sft_probe::tool_version {} {
    foreach command [list [list getVersion] [list version] [list get_db program_version]] {
        if {![catch {uplevel #0 $command} value] && [string trim $value] ne ""} {
            return [string trim $value]
        }
    }
    return "UNAVAILABLE"
}

proc ::sft_probe::restore_baseline {} {
    variable baseline_dir
    foreach required {manifest.tcl restore.tcl} {
        set path [file join $baseline_dir $required]
        if {![file isfile $path] || ![file readable $path]} {
            error "baseline file is missing or unreadable: $path"
        }
    }
    source [file join $baseline_dir manifest.tcl]
    foreach name {SFT_BASE_TOP SFT_SETUP_VIEW SFT_HOLD_VIEW SFT_BASE_CHECKPOINT} {
        if {![info exists ::$name] || [string trim [set ::$name]] eq ""} {
            error "baseline manifest does not define ::$name"
        }
    }
    source [file join $baseline_dir restore.tcl]
    set_analysis_view -setup [list $::SFT_SETUP_VIEW] -hold [list $::SFT_HOLD_VIEW]
}

proc ::sft_probe::capture_help {} {
    variable out_dir
    set specs [list \
        [list write_sdc {-view}] \
        [list rcOut {-spef -view}] \
        [list refinePlace {-eco}] \
        [list ecoRoute {}] \
        [list optDesign {-postRoute -setup -hold -selectedTerms -incr}]]
    set rows {}
    foreach spec $specs {
        lassign $spec command required_tokens
        set relative [file join raw help "${command}.txt"]
        set path [file join $out_dir $relative]
        file mkdir [file dirname $path]
        # Normalize Tcl's numeric Boolean result before writing the typed TSV.
        # The summary reader remains backward-compatible with existing 0/1
        # evidence produced by earlier collector revisions.
        set exists [expr {[llength [info commands $command]] > 0 ? "TRUE" : "FALSE"}]
        set status UNSUPPORTED
        set missing $required_tokens
        set detail "command is absent"
        if {$exists} {
            set body [list help $command]
            if {[catch {redirect -file $path $body} message]} {
                set detail $message
                ::sft_probe::write_text "${path}.error.txt" "$message\n"
            } elseif {![file isfile $path] || [file size $path] == 0} {
                set detail "help produced no output"
            } else {
                set text [::sft_probe::read_text $path]
                set missing {}
                foreach token $required_tokens {
                    if {[string first [string tolower $token] [string tolower $text]] < 0} {
                        lappend missing $token
                    }
                }
                if {![llength $missing]} {
                    set status SUPPORTED
                    set detail ""
                } else {
                    set detail "help lacks required tokens"
                }
            }
        }
        if {$status ne "SUPPORTED"} {
            ::sft_probe::mark_unsupported "help:$command" "$detail missing=$missing"
        }
        lappend rows [list $command $exists $status [join $required_tokens ,] \
            [join $missing ,] $relative $detail]
    }
    ::sft_probe::write_tsv command_probe.tsv \
        {command command_exists help_status required_tokens missing_tokens raw_help_file detail} \
        $rows command_probe.v1
}

proc ::sft_probe::write_sdc_pair {label view} {
    variable out_dir
    set rows {}
    for {set sequence 1} {$sequence <= 2} {incr sequence} {
        set relative [file join raw constraints "${label}.${sequence}.sdc"]
        set path [file join $out_dir $relative]
        file mkdir [file dirname $path]
        set status OK
        set detail ""
        if {[catch {write_sdc -view $view $path} message]} {
            set status UNSUPPORTED
            set detail $message
            ::sft_probe::write_text "${path}.error.txt" "$message\n"
            ::sft_probe::mark_unsupported "write_sdc:$label" $message
        } elseif {![file isfile $path] || [file size $path] == 0} {
            set status UNSUPPORTED
            set detail "write_sdc returned without a nonempty file"
            ::sft_probe::mark_unsupported "write_sdc:$label" $detail
        }
        set bytes 0
        if {[file isfile $path]} { set bytes [file size $path] }
        lappend rows [list $label $view $sequence $status $relative $bytes $detail]
        if {$sequence == 1} { after 1100 }
    }
    return $rows
}

proc ::sft_probe::capture_sdcs {} {
    set rows [concat \
        [::sft_probe::write_sdc_pair setup $::SFT_SETUP_VIEW] \
        [::sft_probe::write_sdc_pair hold $::SFT_HOLD_VIEW]]
    ::sft_probe::write_tsv sdc_write_probe.tsv \
        {analysis view sequence status relative_file bytes detail} \
        $rows sdc_write_probe.v1
}

proc ::sft_probe::path_field {path kind} {
    if {$kind eq "endpoint"} {
        set names {capturing_point endpoint endpoint_pin}
    } elseif {$kind eq "beginpoint"} {
        set names {launching_point beginpoint startpoint startpoint_pin}
    } elseif {$kind eq "slack"} {
        set names {slack path_slack}
    } else {
        error "unknown path field $kind"
    }
    set result [::sft_probe::property_result $path $names]
    if {![dict get $result supported]} { return "" }
    if {$kind eq "slack"} { return [lindex [dict get $result value] 0] }
    return [::sft_probe::object_name [lindex [dict get $result value] 0]]
}

proc ::sft_probe::reject {mode index slack beginpoint endpoint reason detail} {
    variable rejection_rows
    lappend rejection_rows [list $mode $index $slack $beginpoint $endpoint $reason $detail]
    return [dict create eligible 0 reason $reason detail $detail]
}

proc ::sft_probe::false_boolean_or_reject {mode index slack beginpoint endpoint object names label} {
    set result [::sft_probe::boolean_result $object $names]
    if {![dict get $result supported]} {
        ::sft_probe::mark_unsupported "path_filter:$label" "no readable Boolean property in $names"
        return [::sft_probe::reject $mode $index $slack $beginpoint $endpoint \
            "${label}_filter_unsupported" "properties=$names"]
    }
    if {[dict get $result value]} {
        return [::sft_probe::reject $mode $index $slack $beginpoint $endpoint $label \
            "property=[dict get $result name]"]
    }
    return [dict create eligible 1]
}

# Innovus 21.10 models `.constant` as an enum, not a Boolean.  Its explicit
# normal-data value is `no_constant`; `none`, `false`, and `0` are the other
# explicit false renderings accepted by this fail-closed parser.  Any other
# non-empty enum is a constant classification.  Missing/empty/unsupported
# evidence is rejected rather than silently treating the net as safe.
proc ::sft_probe::not_constant_or_reject {mode index slack beginpoint endpoint net} {
    set result [::sft_probe::property_result $net {constant}]
    if {![dict get $result supported]} {
        ::sft_probe::mark_unsupported path_filter:constant_net \
            "no readable enum property: constant"
        return [::sft_probe::reject $mode $index $slack $beginpoint $endpoint \
            constant_net_filter_unsupported "property=constant"]
    }
    set raw_value [string trim [lindex [dict get $result value] 0]]
    set classification [string tolower $raw_value]
    if {$classification in {no_constant none false 0}} {
        return [dict create eligible 1]
    }
    return [::sft_probe::reject $mode $index $slack $beginpoint $endpoint \
        constant_net "property=[dict get $result name] value=$raw_value"]
}

proc ::sft_probe::driver_info {mode index slack beginpoint endpoint} {
    if {[catch {get_pins -quiet [list $endpoint]} endpoint_collection]} {
        return [::sft_probe::reject $mode $index $slack $beginpoint $endpoint endpoint_query_error $endpoint_collection]
    }
    set endpoint_pins [::sft_probe::items $endpoint_collection]
    if {[llength $endpoint_pins] != 1} {
        return [::sft_probe::reject $mode $index $slack $beginpoint $endpoint endpoint_not_unique \
            "count=[llength $endpoint_pins]"]
    }
    set endpoint_pin [lindex $endpoint_pins 0]
    foreach check [list \
        [list $endpoint_pin {is_clock_pin clock} clock_endpoint]] {
        lassign $check object names label
        set result [::sft_probe::false_boolean_or_reject $mode $index $slack $beginpoint $endpoint $object $names $label]
        if {![dict get $result eligible]} { return $result }
    }
    if {[catch {get_nets -quiet -of_objects $endpoint_pin} net_collection]} {
        return [::sft_probe::reject $mode $index $slack $beginpoint $endpoint net_query_error $net_collection]
    }
    set nets [::sft_probe::items $net_collection]
    if {[llength $nets] != 1} {
        return [::sft_probe::reject $mode $index $slack $beginpoint $endpoint net_not_unique "count=[llength $nets]"]
    }
    set net [lindex $nets 0]
    foreach check [list \
        [list $net {is_power power} power_net] \
        [list $net {is_ground ground} ground_net]] {
        lassign $check object names label
        set result [::sft_probe::false_boolean_or_reject $mode $index $slack $beginpoint $endpoint $object $names $label]
        if {![dict get $result eligible]} { return $result }
    }
    set constant_result [::sft_probe::not_constant_or_reject \
        $mode $index $slack $beginpoint $endpoint $net]
    if {![dict get $constant_result eligible]} { return $constant_result }
    set clock_result [::sft_probe::false_boolean_or_reject \
        $mode $index $slack $beginpoint $endpoint $net \
        {is_clock is_clock_net clock} clock_net]
    if {![dict get $clock_result eligible]} { return $clock_result }
    if {[catch {get_pins -quiet -of_objects $net} net_pin_collection]} {
        return [::sft_probe::reject $mode $index $slack $beginpoint $endpoint net_pin_query_error $net_pin_collection]
    }
    set drivers {}
    foreach pin [::sft_probe::items $net_pin_collection] {
        set direction [::sft_probe::property_result $pin {direction pin_direction}]
        if {![dict get $direction supported]} {
            ::sft_probe::mark_unsupported path_filter:driver_direction \
                "no direction property for [::sft_probe::object_name $pin]"
            return [::sft_probe::reject $mode $index $slack $beginpoint $endpoint \
                driver_direction_unsupported [::sft_probe::object_name $pin]]
        }
        set value [string tolower [lindex [dict get $direction value] 0]]
        if {$value in {out output inout}} { lappend drivers $pin }
    }
    if {[llength $drivers] != 1} {
        return [::sft_probe::reject $mode $index $slack $beginpoint $endpoint multidriver \
            "driver_count=[llength $drivers]"]
    }
    set driver_pin [lindex $drivers 0]
    set driver_clock [::sft_probe::false_boolean_or_reject $mode $index $slack $beginpoint $endpoint \
        $driver_pin {is_clock_pin clock} clock_driver]
    if {![dict get $driver_clock eligible]} { return $driver_clock }
    if {[catch {get_cells -quiet -of_objects $driver_pin} cell_collection]} {
        return [::sft_probe::reject $mode $index $slack $beginpoint $endpoint driver_cell_query_error $cell_collection]
    }
    set cells [::sft_probe::items $cell_collection]
    if {[llength $cells] != 1} {
        return [::sft_probe::reject $mode $index $slack $beginpoint $endpoint driver_not_leaf \
            "cell_count=[llength $cells]"]
    }
    set cell [lindex $cells 0]
    set dont_touch [::sft_probe::false_boolean_or_reject $mode $index $slack $beginpoint $endpoint \
        $cell {dont_touch is_dont_touch} dont_touch_driver]
    if {![dict get $dont_touch eligible]} { return $dont_touch }
    set reference [::sft_probe::property_result $cell {ref_name base_name cell_name}]
    if {![dict get $reference supported]} {
        return [::sft_probe::reject $mode $index $slack $beginpoint $endpoint driver_reference_unsupported \
            [::sft_probe::object_name $cell]]
    }
    return [dict create eligible 1 \
        driver_pin [::sft_probe::object_name $driver_pin] \
        driver_inst [::sft_probe::object_name $cell] \
        driver_ref [file tail [lindex [dict get $reference value] 0]] \
        net [::sft_probe::object_name $net]]
}

proc ::sft_probe::hierarchy_groups {endpoint driver_inst} {
    set text "$endpoint $driver_inst"
    set result {}
    set is_exp [expr {[regexp {(^|/)u_exp/} $endpoint] || [regexp {(^|/)u_exp/} $driver_inst]}]
    set is_multiplier [expr {[regexp {(^|/)u_mul_[^/]*/} $endpoint] || [regexp {(^|/)u_mul_[^/]*/} $driver_inst]}]
    if {$is_exp} { lappend result exp }
    if {$is_multiplier} { lappend result multiplier }
    if {[string match *pp_out* $text] || [string match *dat_out* $text]} { lappend result pipeline }
    if {!$is_exp && !$is_multiplier} { lappend result top_tree }
    return $result
}

proc ::sft_probe::compare_records {left right} {
    set left_slack [dict get $left slack]
    set right_slack [dict get $right slack]
    if {$left_slack < $right_slack} { return -1 }
    if {$left_slack > $right_slack} { return 1 }
    foreach key {endpoint beginpoint driver_pin driver_inst driver_ref net} {
        set order [string compare [dict get $left $key] [dict get $right $key]]
        if {$order != 0} { return $order }
    }
    return 0
}

proc ::sft_probe::timing_paths {mode max_paths} {
    set view [expr {$mode eq "late" ? $::SFT_SETUP_VIEW : $::SFT_HOLD_VIEW}]
    set command [list report_timing -collection -max_paths $max_paths -path_type full_clock]
    lappend command [expr {$mode eq "late" ? "-late" : "-early"}]
    lappend command -view $view
    if {[catch {uplevel #0 $command} paths]} {
        ::sft_probe::mark_unsupported "timing_paths:$mode" $paths
        return {}
    }
    return [::sft_probe::items $paths]
}

proc ::sft_probe::collect_paths {max_paths} {
    variable candidates
    variable safe_endpoint
    variable rejection_rows
    set eligible_by_mode {}
    foreach mode {late early} {
        set paths [::sft_probe::timing_paths $mode $max_paths]
        if {[llength $paths] >= $max_paths} {
            ::sft_probe::mark_unsupported "timing_paths:${mode}:truncated" \
                "returned at least SFT_PROBE_MAX_PATHS=$max_paths"
        }
        set eligible {}
        set index 0
        foreach path $paths {
            set slack [::sft_probe::path_field $path slack]
            set beginpoint [::sft_probe::path_field $path beginpoint]
            set endpoint [::sft_probe::path_field $path endpoint]
            if {$slack eq "" || ![string is double -strict $slack]} {
                ::sft_probe::reject $mode $index $slack $beginpoint $endpoint slack_unsupported ""
                incr index
                continue
            }
            if {$beginpoint eq "" || $endpoint eq ""} {
                ::sft_probe::reject $mode $index $slack $beginpoint $endpoint path_endpoint_unsupported ""
                incr index
                continue
            }
            set driver [::sft_probe::driver_info $mode $index $slack $beginpoint $endpoint]
            if {[dict get $driver eligible]} {
                dict unset driver eligible
                dict set driver slack [expr {double($slack)}]
                dict set driver beginpoint $beginpoint
                dict set driver endpoint $endpoint
                dict set driver mode $mode
                dict set driver source_index $index
                dict set driver groups [::sft_probe::hierarchy_groups $endpoint [dict get $driver driver_inst]]
                lappend eligible $driver
            }
            incr index
        }
        dict set eligible_by_mode $mode [lsort -command ::sft_probe::compare_records $eligible]
    }

    set rows {}
    foreach mode {late early} {
        set records [dict get $eligible_by_mode $mode]
        foreach group {exp multiplier pipeline top_tree} {
            set rank 0
            set used_endpoints {}
            set used_drivers {}
            set used_nets {}
            foreach record $records {
                if {[lsearch -exact [dict get $record groups] $group] < 0} { continue }
                set endpoint [dict get $record endpoint]
                set driver [dict get $record driver_inst]
                set net [dict get $record net]
                if {[lsearch -exact $used_endpoints $endpoint] >= 0 ||
                    [lsearch -exact $used_drivers $driver] >= 0 ||
                    [lsearch -exact $used_nets $net] >= 0} {
                    continue
                }
                lappend used_endpoints $endpoint
                lappend used_drivers $driver
                lappend used_nets $net
                set view [expr {$mode eq "late" ? $::SFT_SETUP_VIEW : $::SFT_HOLD_VIEW}]
                lappend rows [list $mode $view $group $rank \
                    [format %.12g [dict get $record slack]] \
                    [dict get $record beginpoint] $endpoint \
                    [dict get $record driver_pin] $driver [dict get $record driver_ref] $net \
                    [dict get $record source_index]]
                dict set record hierarchy_group $group
                dict set record stable_rank $rank
                dict set record view $view
                lappend candidates $record
                if {$safe_endpoint eq "" && $mode eq "late"} { set safe_endpoint $endpoint }
                incr rank
            }
        }
    }
    if {$safe_endpoint eq "" && [llength $candidates]} {
        set safe_endpoint [dict get [lindex $candidates 0] endpoint]
    }
    ::sft_probe::write_tsv candidate_paths.tsv \
        {analysis view hierarchy_group stable_rank slack_ns beginpoint endpoint driver_pin driver_inst driver_ref net source_path_index} \
        $rows candidate_paths.v1
    ::sft_probe::write_tsv path_rejections.tsv \
        {analysis source_path_index slack_ns beginpoint endpoint reason detail} \
        $rejection_rows path_rejections.v1

    variable out_dir
    set stream [open [file join $out_dir calibration_candidates.tcl] w]
    puts $stream "# Typed, read-only Innovus calibration candidates; never Gold."
    puts $stream [list set ::SFT_PROBE_SCHEMA candidate_paths.v1]
    puts $stream [list set ::SFT_PROBE_CANDIDATES $candidates]
    close $stream
}

proc ::sft_probe::parse_drive {reference} {
    set name [file tail $reference]
    if {![regexp {^(.*)_X([0-9]+(P[0-9]+)?)([A-Z]*)_(.+)$} \
        $name -> stem token ignored flavor tail]} {
        return {}
    }
    return [list $stem [string map {P .} $token] $flavor $tail]
}

proc ::sft_probe::lib_cells {query} {
    if {[catch {get_lib_cells -quiet $query} collection]} { return {} }
    return [::sft_probe::items $collection]
}

proc ::sft_probe::exact_lib_copies {reference} {
    set result {}
    set seen {}
    foreach query [list "*/$reference" $reference] {
        foreach cell [::sft_probe::lib_cells $query] {
            set name [::sft_probe::object_name $cell]
            if {[file tail $name] ne $reference || [lsearch -exact $seen $name] >= 0} { continue }
            lappend seen $name
            lappend result $cell
        }
    }
    return $result
}

proc ::sft_probe::lib_pins {cell} {
    if {![catch {get_lib_pins -quiet -of_objects $cell} collection]} {
        set pins [::sft_probe::items $collection]
        if {[llength $pins]} { return [dict create status OK pins $pins detail ""] }
    }
    if {![catch {get_db $cell .lib_pins} collection]} {
        set pins [::sft_probe::items $collection]
        if {[llength $pins]} { return [dict create status OK pins $pins detail "get_db fallback"] }
    }
    return [dict create status UNSUPPORTED pins {} detail "cannot enumerate lib pins"]
}

proc ::sft_probe::normalized_function {value} {
    return [string map [list " " "" "\t" "" "\n" "" "\r" ""] $value]
}

proc ::sft_probe::signature_for_copy {reference cell} {
    set pins_result [::sft_probe::lib_pins $cell]
    if {[dict get $pins_result status] ne "OK"} { return $pins_result }
    set inputs {}
    set outputs {}
    foreach pin [dict get $pins_result pins] {
        set direction [::sft_probe::property_result $pin {direction pin_direction}]
        if {![dict get $direction supported]} {
            return [dict create status UNSUPPORTED pins {} detail \
                "missing direction on [::sft_probe::object_name $pin]"]
        }
        set direction_value [string tolower [lindex [dict get $direction value] 0]]
        set pin_name [file tail [::sft_probe::object_name $pin]]
        if {$direction_value in {in input}} {
            lappend inputs $pin_name
        } elseif {$direction_value in {out output}} {
            set function [::sft_probe::property_result $pin {function logic_function}]
            if {![dict get $function supported]} {
                return [dict create status UNSUPPORTED pins {} detail \
                    "missing Boolean function on $reference/$pin_name"]
            }
            lappend outputs "$pin_name=[::sft_probe::normalized_function [lindex [dict get $function value] 0]]"
        }
    }
    set inputs [lsort -unique $inputs]
    set outputs [lsort -unique $outputs]
    if {![llength $inputs] || ![llength $outputs]} {
        return [dict create status UNSUPPORTED pins {} detail "empty input or output signature"]
    }
    set input_text [join $inputs ,]
    set output_text [join $outputs {;}]
    return [dict create status OK inputs $input_text outputs $output_text \
        signature "I:$input_text|O:$output_text" detail ""]
}

proc ::sft_probe::collect_reference_evidence {references} {
    variable library_rows
    variable reference_evidence
    foreach reference [lsort -unique $references] {
        set copies [::sft_probe::exact_lib_copies $reference]
        set signatures {}
        set all_ok 1
        if {![llength $copies]} { set all_ok 0 }
        foreach cell $copies {
            set full_name [::sft_probe::object_name $cell]
            set library [file dirname $full_name]
            if {$library eq "."} { set library "<unqualified>" }
            set result [::sft_probe::signature_for_copy $reference $cell]
            set status [dict get $result status]
            set inputs ""
            set outputs ""
            set signature ""
            if {$status eq "OK"} {
                set inputs [dict get $result inputs]
                set outputs [dict get $result outputs]
                set signature [dict get $result signature]
                lappend signatures $signature
            } else {
                set all_ok 0
                ::sft_probe::mark_unsupported "lib_signature:$reference" [dict get $result detail]
            }
            lappend library_rows [list $reference $full_name $library $inputs $outputs \
                $signature $status [dict get $result detail]]
        }
        set signatures [lsort -unique $signatures]
        if {![llength $copies]} {
            set status NO_ACTIVE_COPY
            set canonical ""
            ::sft_probe::mark_unsupported "lib_signature:$reference" "no active library copy"
        } elseif {!$all_ok} {
            set status UNSUPPORTED
            set canonical ""
        } elseif {[llength $signatures] != 1} {
            set status COPY_MISMATCH
            set canonical ""
            ::sft_probe::mark_unsupported "lib_signature:$reference" \
                "active library copies disagree on Boolean signature"
        } else {
            set status OK
            set canonical [lindex $signatures 0]
        }
        dict set reference_evidence $reference [dict create status $status canonical $canonical \
            copies [llength $copies]]
    }
}

proc ::sft_probe::collect_drive_families {} {
    variable candidates
    variable family_rows
    variable library_rows
    variable reference_evidence
    variable out_dir
    set sources {}
    foreach candidate $candidates { lappend sources [dict get $candidate driver_ref] }
    set sources [lsort -unique $sources]
    set family_variants {}
    set all_references $sources
    foreach source $sources {
        set parsed [::sft_probe::parse_drive $source]
        if {[llength $parsed] != 4} {
            dict set family_variants $source {}
            ::sft_probe::mark_unsupported "drive_family:$source" "reference does not match exact drive grammar"
            continue
        }
        lassign $parsed stem source_drive flavor tail
        set pattern "${stem}_X*_${tail}"
        set variants {}
        foreach query [list "*/$pattern" $pattern] {
            foreach cell [::sft_probe::lib_cells $query] {
                set reference [file tail [::sft_probe::object_name $cell]]
                set candidate [::sft_probe::parse_drive $reference]
                if {[llength $candidate] != 4} { continue }
                if {[lindex $candidate 0] eq $stem && [lindex $candidate 2] eq $flavor &&
                    [lindex $candidate 3] eq $tail && [lsearch -exact $variants $reference] < 0} {
                    lappend variants $reference
                }
            }
        }
        set variants [lsort -dictionary -unique $variants]
        dict set family_variants $source $variants
        set all_references [concat $all_references $variants]
    }
    ::sft_probe::collect_reference_evidence [lsort -unique $all_references]
    foreach source $sources {
        set parsed [::sft_probe::parse_drive $source]
        if {[llength $parsed] != 4} {
            lappend family_rows [list $source "" "" "" "" "" "" 0 \
                UNSUPPORTED_PATTERN UNKNOWN]
            continue
        }
        lassign $parsed stem source_drive flavor tail
        set source_info [dict get $reference_evidence $source]
        set variants [dict get $family_variants $source]
        if {![llength $variants]} {
            lappend family_rows [list $source $source_drive $stem $flavor $tail "" "" 0 \
                NO_VARIANTS UNKNOWN]
            ::sft_probe::mark_unsupported "drive_family:$source" "no exact-flavor variants"
            continue
        }
        foreach variant $variants {
            set variant_parsed [::sft_probe::parse_drive $variant]
            set variant_drive [lindex $variant_parsed 1]
            set info [dict get $reference_evidence $variant]
            set signature_status [dict get $info status]
            if {[dict get $source_info status] ne "OK" || $signature_status ne "OK"} {
                set equivalent UNKNOWN
            } elseif {[dict get $source_info canonical] eq [dict get $info canonical]} {
                set equivalent TRUE
            } else {
                set equivalent FALSE
                ::sft_probe::mark_unsupported "drive_family:$source" \
                    "Boolean mismatch for variant $variant"
            }
            lappend family_rows [list $source $source_drive $stem $flavor $tail $variant \
                $variant_drive [dict get $info copies] $signature_status $equivalent]
        }
    }
    ::sft_probe::write_tsv lib_signatures.tsv \
        {reference lib_cell library input_pins output_functions signature status detail} \
        $library_rows lib_signatures.v1
    ::sft_probe::write_tsv drive_families.tsv \
        {source_ref source_drive stem flavor tail variant_ref variant_drive active_copy_count signature_status equivalent_to_source} \
        $family_rows drive_families.v1
    set stream [open [file join $out_dir drive_families.tcl] w]
    puts $stream "# Exact flavor-preserving drive families; read-only probe evidence."
    puts $stream [list set ::SFT_PROBE_DRIVE_FAMILY_SCHEMA drive_families.v1]
    puts $stream [list set ::SFT_PROBE_DRIVE_FAMILIES $family_rows]
    close $stream
}

proc ::sft_probe::query_selection {label command expected} {
    if {[catch {uplevel #0 $command} values]} {
        return [list $label UNSUPPORTED 0 "" $expected FALSE $values]
    }
    set names {}
    foreach value $values { lappend names [::sft_probe::object_name $value] }
    set names [lsort -unique $names]
    set exact [expr {[llength $names] == 1 && [lindex $names 0] eq $expected ? "TRUE" : "FALSE"}]
    return [list $label OK [llength $names] [join $names {;}] $expected $exact ""]
}

proc ::sft_probe::capture_selection {} {
    variable safe_endpoint
    set rows {}
    if {$safe_endpoint eq ""} {
        ::sft_probe::mark_unsupported selection "no safe eligible timing endpoint"
        foreach label {get_db_selected_full_name get_db_selected_name dbGet_selected_name} {
            lappend rows [list $label UNSUPPORTED 0 "" "" FALSE "no safe endpoint"]
        }
    } else {
        catch {deselectAll}
        if {[catch {selectPin $safe_endpoint} message]} {
            ::sft_probe::mark_unsupported selection $message
            foreach label {get_db_selected_full_name get_db_selected_name dbGet_selected_name} {
                lappend rows [list $label UNSUPPORTED 0 "" $safe_endpoint FALSE $message]
            }
        } else {
            lappend rows [::sft_probe::query_selection get_db_selected_full_name \
                [list get_db selected .full_name] $safe_endpoint]
            lappend rows [::sft_probe::query_selection get_db_selected_name \
                [list get_db selected .name] $safe_endpoint]
            lappend rows [::sft_probe::query_selection dbGet_selected_name \
                [list dbGet selected.name] $safe_endpoint]
        }
        catch {deselectAll}
        set matched 0
        foreach row $rows { if {[lindex $row 5] eq "TRUE"} { set matched 1 } }
        if {!$matched} {
            ::sft_probe::mark_unsupported selection \
                "none of the three Innovus queries returned exactly the selected endpoint"
        }
    }
    ::sft_probe::write_tsv selection_probe.tsv \
        {query status result_count values expected exact_match error} \
        $rows selection_probe.v1
}

proc ::sft_probe::capture_report_command {name relative command} {
    variable out_dir
    set path [file join $out_dir $relative]
    file mkdir [file dirname $path]
    if {[catch {uplevel #0 [concat $command [list > $path]]} message]} {
        ::sft_probe::write_text "${path}.error.txt" "$message\n"
        return [list $name UNSUPPORTED $relative 0 $message]
    }
    if {![file isfile $path] || [file size $path] == 0} {
        return [list $name UNSUPPORTED $relative 0 "command produced no report"]
    }
    return [list $name OK $relative [file size $path] ""]
}

proc ::sft_probe::capture_reports {} {
    variable out_dir
    set rows {}
    set result [::sft_probe::capture_report_command report_constraint raw/reports/report_constraint_all_violators.rpt \
        [list report_constraint -all_violators]]
    lappend rows $result
    if {[lindex $result 1] ne "OK"} { ::sft_probe::mark_unsupported report_constraint [lindex $result 4] }

    foreach spec [list \
        [list verifyConnectivity raw/reports/verify_connectivity.rpt \
            [list verifyConnectivity -type all -report [file join $out_dir raw reports verify_connectivity.rpt]]] \
        [list verify_drc raw/reports/verify_drc.rpt \
            [list verify_drc -limit 1000000 -report [file join $out_dir raw reports verify_drc.rpt]]]] {
        lassign $spec name relative command
        set path [file join $out_dir $relative]
        file mkdir [file dirname $path]
        if {[catch {uplevel #0 $command} message]} {
            ::sft_probe::write_text "${path}.error.txt" "$message\n"
            set result [list $name UNSUPPORTED $relative 0 $message]
        } elseif {![file isfile $path] || [file size $path] == 0} {
            set result [list $name UNSUPPORTED $relative 0 "command produced no report"]
        } else {
            set result [list $name OK $relative [file size $path] ""]
        }
        lappend rows $result
        if {[lindex $result 1] ne "OK"} { ::sft_probe::mark_unsupported $name [lindex $result 4] }
    }
    ::sft_probe::write_tsv report_probe.tsv \
        {report_command status relative_file bytes detail} $rows report_probe.v1
}

proc ::sft_probe::write_metadata {} {
    variable baseline_dir
    set rows [list \
        [list schema string innovus_readonly_probe.v1] \
        [list purpose string calibration_only_never_gold] \
        [list process_id integer [pid]] \
        [list tool_version string [::sft_probe::tool_version]] \
        [list baseline_dir path $baseline_dir] \
        [list top string $::SFT_BASE_TOP] \
        [list setup_view string $::SFT_SETUP_VIEW] \
        [list hold_view string $::SFT_HOLD_VIEW] \
        [list checkpoint path $::SFT_BASE_CHECKPOINT] \
        [list design_mutations integer 0] \
        [list gold_eligible boolean false]]
    ::sft_probe::write_tsv probe_metadata.tsv {key type value} $rows probe_metadata.v1
}

proc ::sft_probe::finalize {} {
    variable out_dir
    variable unsupported
    variable artifacts
    variable schema
    set overall [expr {[llength $unsupported] ? "COMPLETE_WITH_UNSUPPORTED_APIS" : "COMPLETE"}]
    ::sft_probe::write_tsv unsupported_apis.tsv \
        {capability detail} $unsupported unsupported_apis.v1
    set rows {}
    foreach artifact $artifacts { lappend rows $artifact }
    ::sft_probe::write_tsv artifact_manifest.tsv \
        {relative_file schema row_count bytes} $rows artifact_manifest.v1

    set stream [open [file join $out_dir probe_manifest.tcl] w]
    puts $stream "# Probe-only typed manifest.  This file can never authorize Gold data."
    puts $stream [list set ::SFT_PROBE_SCHEMA $schema]
    puts $stream [list set ::SFT_PROBE_STATUS $overall]
    puts $stream [list set ::SFT_PROBE_GOLD_ELIGIBLE false]
    puts $stream [list set ::SFT_PROBE_DESIGN_MUTATIONS 0]
    puts $stream [list set ::SFT_PROBE_UNSUPPORTED $unsupported]
    close $stream

    ::sft_probe::write_text [file join $out_dir probe.status] \
        "SFT_INNOVUS_READONLY_PROBE_$overall\nGOLD_ELIGIBLE=FALSE\nDESIGN_MUTATIONS=0\n"
    puts "SFT_INNOVUS_READONLY_PROBE_$overall"
    puts "SFT_PROBE_GOLD_ELIGIBLE FALSE"
}

proc ::sft_probe::main {} {
    variable out_dir
    variable baseline_dir
    set baseline_dir [file normalize [::sft_probe::require_env SFT_BASELINE_DIR]]
    set requested_out [::sft_probe::require_env SFT_PROBE_DIR]
    if {[file exists $requested_out] && ![file isdirectory $requested_out]} {
        error "SFT_PROBE_DIR exists and is not a directory: $requested_out"
    }
    file mkdir $requested_out
    set out_dir [file normalize $requested_out]
    set existing [glob -nocomplain -directory $out_dir *]
    if {[llength $existing]} {
        error "SFT_PROBE_DIR must be empty to preserve one-run provenance: $out_dir"
    }
    ::sft_probe::assert_fresh_process
    ::sft_probe::restore_baseline
    ::sft_probe::write_metadata
    ::sft_probe::capture_help
    ::sft_probe::capture_sdcs
    set max_paths 10000
    if {[info exists ::env(SFT_PROBE_MAX_PATHS)]} { set max_paths $::env(SFT_PROBE_MAX_PATHS) }
    if {![string is integer -strict $max_paths] || $max_paths < 100} {
        error "SFT_PROBE_MAX_PATHS must be an integer >= 100"
    }
    ::sft_probe::collect_paths $max_paths
    ::sft_probe::collect_drive_families
    ::sft_probe::capture_selection
    ::sft_probe::capture_reports
    ::sft_probe::finalize
}

if {[catch {::sft_probe::main} message options]} {
    if {[info exists ::sft_probe::out_dir] && $::sft_probe::out_dir ne "" &&
        [file isdirectory $::sft_probe::out_dir]} {
        set error_info $message
        if {[dict exists $options -errorinfo]} { set error_info [dict get $options -errorinfo] }
        ::sft_probe::write_text [file join $::sft_probe::out_dir probe.status] \
            "SFT_INNOVUS_READONLY_PROBE_FAILED\nGOLD_ELIGIBLE=FALSE\nDESIGN_MUTATIONS=UNKNOWN\n"
        ::sft_probe::write_text [file join $::sft_probe::out_dir probe_error.txt] "$error_info\n"
    }
    puts stderr "SFT_INNOVUS_READONLY_PROBE_FAILED: $message"
    if {[dict exists $options -errorinfo]} { puts stderr [dict get $options -errorinfo] }
    exit 1
}
exit 0

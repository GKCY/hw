# Runtime support for the Innovus timing-ECO pilot.
#
# This file deliberately resolves timing objects from the restored database.
# Catalog entries select structural classes and stable ranks; no generated task
# contains a fabricated instance, pin, or net name.

namespace eval ::sft {
    # Innovus verify_drc defaults to 1000 errors.  Gold evidence always uses
    # this fixed high cutoff and rejects a report that reaches it.
    variable drc_report_limit 1000000
    variable targets {}
    variable changes {}
    variable eco_count 0
    variable injection_trials {}
    variable repair_commands {}
    variable repair_operations {}
    variable functional_proofs {}
    variable lib_function_cache {}
    variable reference_variant_cache {}
    variable native_before_cells {}
    variable native_allowed_before {}
    variable native_changed_cells 0
    variable stage_metrics
    variable stage_drv
    variable stage_drc
    variable injection_violation_slacks {}
    variable injection_scope {}
    variable diagnostic_existing_instances {}
    variable injection_pre_routed 0
    # Frozen setup-only cases with two serial injected delay cells use measured
    # repair profiles.  Keep these cells out of the global buffer candidate
    # order so coordinated setup/hold cases retain their calibrated weak-buffer
    # behavior.  DLY2_X4M has an equal-area BUF_X4M replacement.  SETUP_004's
    # two serial DLY4_X4M cells use two equal-footprint INV_X9B cells: their
    # composed function remains non-inverting and the exclusive internal net
    # is proven before planning the swaps.  This avoids the neighboring route
    # churn caused by shrinking each ten-site delay cell to a four-site BUF.
    # DLY4_X2M retains its measured stronger driver profile.
    variable equal_area_setup_buffer BUF_X4M_A9TR40
    variable strong_setup_buffer BUF_X2M_A9TR40
    variable paired_setup_inverter INV_X9B_A9TR40
}

proc ::sft::fail {message} {
    error "SFT: $message"
}

proc ::sft::items {collection} {
    set result {}
    if {[llength [info commands sizeof_collection]] &&
        ![catch {sizeof_collection $collection}]} {
        foreach_in_collection object $collection {
            lappend result $object
        }
        return $result
    }
    return $collection
}

proc ::sft::object_name {object} {
    foreach command [list \
        [list get_object_name $object] \
        [list get_property $object full_name] \
        [list get_property $object name] \
        [list get_db $object .name]] {
        if {![catch {uplevel #0 $command} value] && $value ne ""} {
            return [lindex $value 0]
        }
    }
    return $object
}

proc ::sft::property {object names} {
    foreach name $names {
        if {![catch {get_property $object $name} value] && $value ne ""} {
            return $value
        }
        if {![catch {get_db $object .$name} value] && $value ne ""} {
            return $value
        }
    }
    return ""
}

# Resolve one Boolean property from an ordered alias list.  The first readable,
# non-empty property is authoritative: an explicit false is a supported result
# and must not fall through to a legacy alias.  In particular, Innovus 21.10
# exposes `is_clock_pin=false` on ordinary instance pins but rejects the legacy
# `clock` attribute with IMPDBTCL-248.
#
# The result is deliberately tri-state: supported true, supported false, or
# unsupported.  A readable non-Boolean value is unsupported rather than an
# invitation to try another alias, matching the read-only probe's
# `boolean_result` contract.
proc ::sft::boolean_property_result {object names} {
    foreach name $names {
        foreach command [list \
            [list get_property $object $name] \
            [list get_db $object .$name]] {
            if {[catch {uplevel #0 $command} value] ||
                [string trim $value] eq ""} {
                continue
            }
            set normalized [string tolower [string trim [lindex $value 0]]]
            if {$normalized in {true 1 yes on}} {
                return [dict create supported 1 name $name value 1 raw $value]
            }
            if {$normalized in {false 0 no off}} {
                return [dict create supported 1 name $name value 0 raw $value]
            }
            return [dict create supported 0 name $name value "" raw $value]
        }
    }
    return [dict create supported 0 name "" value "" raw ""]
}

# Safety predicates must never turn missing schema evidence into permission to
# edit the design.  Callers retain their specific rejection message for a true
# classification, while this helper rejects unsupported/ambiguous evidence.
proc ::sft::boolean_value_or_fail {object names label} {
    set result [::sft::boolean_property_result $object $names]
    if {![dict get $result supported]} {
        set detail "properties=$names"
        if {[dict get $result name] ne ""} {
            append detail " first_readable=[dict get $result name] raw=[dict get $result raw]"
        }
        ::sft::fail "cannot determine $label for [::sft::object_name $object] ($detail)"
    }
    return [dict get $result value]
}

# Innovus 21.10 exposes a net's constant classification as an enum at
# `get_db $net .constant`; it does not expose `.is_constant`.  A normal data
# net is reported as `no_constant`.  Innovus may also render a false enum as
# `none`, `false`, or `0`; treat only those explicit false values as safe.
# Every other non-empty value is a constant classification, and an unreadable
# property fails closed.
proc ::sft::net_is_constant {net} {
    set value ""
    if {[catch {get_db $net .constant} value] || [string trim $value] eq ""} {
        ::sft::fail "cannot determine constant classification for net [::sft::object_name $net]"
    }
    set classification [string tolower [string trim [lindex $value 0]]]
    return [expr {$classification ni {no_constant none false 0}}]
}

proc ::sft::view_for {mode} {
    if {$mode eq "late" && [info exists ::SFT_SETUP_VIEW]} {
        return $::SFT_SETUP_VIEW
    }
    if {$mode eq "early" && [info exists ::SFT_HOLD_VIEW]} {
        return $::SFT_HOLD_VIEW
    }
    return ""
}

proc ::sft::timing_paths {mode max_paths} {
    set command [list report_timing -collection -max_paths $max_paths -path_type full_clock]
    lappend command [expr {$mode eq "late" ? "-late" : "-early"}]
    set view [::sft::view_for $mode]
    if {$view ne ""} {
        lappend command -view $view
    }
    if {[catch {uplevel #0 $command} paths]} {
        ::sft::fail "report_timing -collection failed for $mode: $paths"
    }
    return [::sft::items $paths]
}

proc ::sft::violating_timing_paths {mode max_paths} {
    set command [list report_timing -collection -max_paths $max_paths -max_slack 0.0 -path_type full_clock]
    lappend command [expr {$mode eq "late" ? "-late" : "-early"}]
    set view [::sft::view_for $mode]
    if {$view ne ""} { lappend command -view $view }
    if {[catch {uplevel #0 $command} paths]} {
        ::sft::fail "violating report_timing -collection failed for $mode: $paths"
    }
    return [::sft::items $paths]
}

proc ::sft::path_field {path kind} {
    if {$kind eq "endpoint"} {
        set names {capturing_point endpoint endpoint_pin}
    } elseif {$kind eq "beginpoint"} {
        set names {launching_point beginpoint startpoint startpoint_pin}
    } elseif {$kind eq "slack"} {
        set names {slack path_slack}
    } else {
        ::sft::fail "unknown timing-path field $kind"
    }
    set value [::sft::property $path $names]
    if {$value eq ""} {
        return ""
    }
    if {$kind eq "slack"} {
        return [lindex $value 0]
    }
    return [::sft::object_name [lindex $value 0]]
}

proc ::sft::matches_globs {text include_globs exclude_globs} {
    set included 0
    foreach pattern $include_globs {
        if {[string match $pattern $text]} {
            set included 1
            break
        }
    }
    if {!$included} {
        return 0
    }
    foreach pattern $exclude_globs {
        if {[string match $pattern $text]} {
            return 0
        }
    }
    return 1
}

proc ::sft::pin_collection {pin_name} {
    set pins [get_pins -quiet [list $pin_name]]
    if {[llength [::sft::items $pins]] != 1} {
        ::sft::fail "expected one pin named $pin_name"
    }
    return $pins
}

proc ::sft::driver_info {endpoint} {
    set endpoint_pin [::sft::pin_collection $endpoint]
    set endpoint_pin_object [lindex [::sft::items $endpoint_pin] 0]
    if {[::sft::boolean_value_or_fail $endpoint_pin_object \
        {is_clock_pin clock} "endpoint clock-pin classification"]} {
        ::sft::fail "endpoint $endpoint is a clock pin"
    }
    set nets [::sft::items [get_nets -quiet -of_objects $endpoint_pin]]
    if {[llength $nets] != 1} {
        ::sft::fail "endpoint $endpoint does not have exactly one data net"
    }
    set net [lindex $nets 0]
    foreach check [list \
        [list {is_power power} power] \
        [list {is_ground ground} ground] \
        [list {is_clock is_clock_net clock} clock]] {
        lassign $check names label
        if {[::sft::boolean_value_or_fail $net $names "$label-net classification"]} {
            ::sft::fail "endpoint $endpoint is driven by a power/ground/clock net"
        }
    }
    if {[::sft::net_is_constant $net]} {
        ::sft::fail "endpoint $endpoint is driven by a constant net"
    }
    set drivers {}
    foreach pin [::sft::items [get_pins -quiet -of_objects $net]] {
        set direction [string tolower [::sft::property $pin {direction pin_direction}]]
        if {$direction eq "out" || $direction eq "output" || $direction eq "inout"} {
            lappend drivers $pin
        }
    }
    if {[llength $drivers] != 1} {
        ::sft::fail "net [::sft::object_name $net] has [llength $drivers] drivers"
    }
    set driver_pin [lindex $drivers 0]
    if {[::sft::boolean_value_or_fail $driver_pin \
        {is_clock_pin clock} "driver clock-pin classification"]} {
        ::sft::fail "driver [::sft::object_name $driver_pin] is a clock pin"
    }
    set cells [::sft::items [get_cells -quiet -of_objects $driver_pin]]
    if {[llength $cells] != 1} {
        ::sft::fail "driver [::sft::object_name $driver_pin] is not a leaf-cell pin"
    }
    set cell [lindex $cells 0]
    if {[::sft::boolean_value_or_fail $cell \
        {dont_touch is_dont_touch} "driver dont-touch classification"]} {
        ::sft::fail "driver [::sft::object_name $cell] is dont-touch"
    }
    set reference [::sft::property $cell {ref_name base_name cell_name}]
    if {$reference eq ""} {
        ::sft::fail "cannot determine reference cell for [::sft::object_name $cell]"
    }
    return [dict create \
        endpoint $endpoint \
        net [::sft::object_name $net] \
        driver_pin [::sft::object_name $driver_pin] \
        driver_inst [::sft::object_name $cell] \
        driver_ref [file tail [lindex $reference 0]]]
}

proc ::sft::compare_targets {left right} {
    set left_slack [dict get $left slack]
    set right_slack [dict get $right slack]
    if {$left_slack < $right_slack} { return -1 }
    if {$left_slack > $right_slack} { return 1 }
    foreach key {endpoint beginpoint driver_inst net} {
        set order [string compare [dict get $left $key] [dict get $right $key]]
        if {$order != 0} { return $order }
    }
    return 0
}

proc ::sft::injection_resize_requirement {strategy mode} {
    switch -- $strategy {
        downsize_endpoint_driver {
            if {$mode eq "late"} { return [list down $::SFT_CASE(setup_drive_steps)] }
        }
        upsize_endpoint_driver {
            if {$mode eq "early"} { return [list up $::SFT_CASE(hold_drive_steps)] }
        }
        mixed_downsize_and_speedup {
            if {$mode eq "late"} { return [list down $::SFT_CASE(setup_drive_steps)] }
            if {$mode eq "early"} { return [list up $::SFT_CASE(hold_drive_steps)] }
        }
    }
    return {}
}

proc ::sft::write_resolved_targets {} {
    variable targets
    set path [file join $::SFT_REPORT_DIR resolved_targets.tcl]
    set stream [open $path w]
    puts $stream "# Runtime-resolved design objects; generated by Innovus."
    puts $stream [list set ::SFT_RESOLVED_TARGETS $targets]
    close $stream
}

proc ::sft::resolve_targets {} {
    variable targets
    set targets {}
    set used_endpoints {}
    set used_drivers {}
    set used_nets {}
    foreach selector $::SFT_SELECTORS {
        set role [dict get $selector role]
        set mode [dict get $selector timing]
        set include_globs [dict get $selector include_globs]
        set exclude_globs [dict get $selector exclude_globs]
        set rank [dict get $selector stable_rank]
        set count [dict get $selector count]
        set candidates {}
        set unique_endpoints {}
        set unique_drivers {}
        set unique_nets {}
        set feasibility_rejections 0
        set last_feasibility_error ""
        foreach path [::sft::timing_paths $mode 1000] {
            set endpoint [::sft::path_field $path endpoint]
            set beginpoint [::sft::path_field $path beginpoint]
            if {$endpoint eq "" || $beginpoint eq ""} {
                continue
            }
            if {[lsearch -exact $used_endpoints $endpoint] >= 0} {
                continue
            }
            if {[catch {::sft::driver_info $endpoint} driver]} {
                continue
            }
            set driver_inst [dict get $driver driver_inst]
            set net_name [dict get $driver net]
            # The hierarchy anchor follows the actual object being modified;
            # beginpoint-only matches are intentionally excluded.
            set searchable "$endpoint $driver_inst"
            if {![::sft::matches_globs $searchable $include_globs $exclude_globs]} {
                continue
            }
            if {[lsearch -exact $used_drivers $driver_inst] >= 0 ||
                [lsearch -exact $used_nets $net_name] >= 0} {
                continue
            }
            set resize_requirement [::sft::injection_resize_requirement \
                $::SFT_CASE(injection_strategy) $mode]
            if {[llength $resize_requirement]} {
                lassign $resize_requirement direction steps
                if {[catch {::sft::validate_resize_transition \
                    [dict get $driver driver_ref] $direction $steps \
                    "injection target $endpoint"} replacement]} {
                    incr feasibility_rejections
                    set last_feasibility_error $replacement
                    continue
                }
                dict set driver injection_replacement $replacement
            }
            dict set driver role $role
            dict set driver timing $mode
            dict set driver beginpoint $beginpoint
            dict set driver slack [::sft::path_field $path slack]
            lappend candidates $driver
        }
        set candidates [lsort -command ::sft::compare_targets $candidates]
        set unique_candidates {}
        foreach candidate $candidates {
            set endpoint [dict get $candidate endpoint]
            set driver_inst [dict get $candidate driver_inst]
            set net_name [dict get $candidate net]
            if {[lsearch -exact $unique_endpoints $endpoint] >= 0 ||
                [lsearch -exact $unique_drivers $driver_inst] >= 0 ||
                [lsearch -exact $unique_nets $net_name] >= 0} {
                continue
            }
            lappend unique_candidates $candidate
            lappend unique_endpoints $endpoint
            lappend unique_drivers $driver_inst
            lappend unique_nets $net_name
        }
        set candidates $unique_candidates
        set chosen [lrange $candidates $rank [expr {$rank + $count - 1}]]
        if {[llength $chosen] != $count} {
            ::sft::fail "selector $role requested $count paths at rank $rank but found [llength $candidates] candidates; resize_feasibility_rejections=$feasibility_rejections last_error=$last_feasibility_error"
        }
        foreach target $chosen {
            lappend targets $target
            lappend used_endpoints [dict get $target endpoint]
            lappend used_drivers [dict get $target driver_inst]
            lappend used_nets [dict get $target net]
        }
    }
    set late_count [llength [::sft::targets_for_timing late]]
    set early_count [llength [::sft::targets_for_timing early]]
    if {$late_count != $::SFT_CASE(setup_target_count) ||
        $early_count != $::SFT_CASE(hold_target_count)} {
        ::sft::fail "resolved target cardinality regressed: setup=$late_count/$::SFT_CASE(setup_target_count) hold=$early_count/$::SFT_CASE(hold_target_count)"
    }
    ::sft::validate_resolved_action_feasibility
    ::sft::write_resolved_targets
    puts "SFT_RESOLVED_TARGET_COUNT [llength $targets]"
}

proc ::sft::parse_drive {reference} {
    set name [file tail $reference]
    if {![regexp {^(.*)_X([0-9]+(P[0-9]+)?)([A-Z]*)_(.+)$} \
        $name -> stem token ignored flavor tail]} {
        return {}
    }
    return [list $stem [string map {P .} $token] $flavor $tail]
}

proc ::sft::compare_drive_variants {left right} {
    set left_drive [lindex $left 0]
    set right_drive [lindex $right 0]
    if {$left_drive < $right_drive} { return -1 }
    if {$left_drive > $right_drive} { return 1 }
    return [string compare [lindex $left 1] [lindex $right 1]]
}

proc ::sft::lib_cell_object {reference} {
    foreach query [list "*/$reference" $reference] {
        if {![catch {get_lib_cells -quiet $query} cells]} {
            set objects [::sft::items $cells]
            if {[llength $objects] == 1} {
                return [lindex $objects 0]
            }
            if {[llength $objects] > 1} {
                # MMMC commonly returns one copy per library.  Accept only when
                # all copies have the requested base name; the function check
                # below still compares the actual Liberty pin functions.
                set exact {}
                foreach object $objects {
                    if {[file tail [::sft::object_name $object]] eq $reference} {
                        lappend exact $object
                    }
                }
                if {[llength $exact]} {
                    return [lindex $exact 0]
                }
            }
        }
    }
    ::sft::fail "cannot resolve Liberty cell $reference"
}

proc ::sft::lib_pins_for_cell {cell} {
    if {![catch {get_lib_pins -quiet -of_objects $cell} pins]} {
        set objects [::sft::items $pins]
        if {[llength $objects]} {
            return $objects
        }
    }
    # Common-UI fallback.  This path must be probed on Innovus 21.10 because
    # library object schemas vary across ISR releases; failure is deliberate.
    if {![catch {get_db $cell .lib_pins} pins]} {
        set objects [::sft::items $pins]
        if {[llength $objects]} {
            return $objects
        }
    }
    ::sft::fail "cannot enumerate Liberty pins for [::sft::object_name $cell] (probe get_lib_pins/get_db syntax in Innovus 21.10)"
}

proc ::sft::normalized_function {value} {
    return [string map [list " " "" "\t" "" "\n" "" "\r" ""] $value]
}

proc ::sft::lib_function_evidence {reference} {
    variable lib_function_cache
    if {[dict exists $lib_function_cache $reference]} {
        return [dict get $lib_function_cache $reference]
    }
    set cell [::sft::lib_cell_object $reference]
    set inputs {}
    set outputs {}
    foreach pin [::sft::lib_pins_for_cell $cell] {
        set direction [string tolower [lindex [::sft::property $pin {direction pin_direction}] 0]]
        set pin_name [file tail [::sft::object_name $pin]]
        if {$direction in {in input}} {
            lappend inputs $pin_name
        } elseif {$direction in {out output}} {
            set function [lindex [::sft::property $pin {function logic_function}] 0]
            if {$function eq ""} {
                ::sft::fail "Liberty output $reference/$pin_name has no readable Boolean function"
            }
            lappend outputs [list $pin_name [::sft::normalized_function $function]]
        }
    }
    if {![llength $outputs]} {
        ::sft::fail "Liberty cell $reference has no readable output function"
    }
    set evidence [dict create reference $reference inputs [lsort $inputs] outputs [lsort $outputs]]
    dict set lib_function_cache $reference $evidence
    return $evidence
}

proc ::sft::validate_equivalent_cells {from_reference to_reference context} {
    set from_parsed [::sft::parse_drive $from_reference]
    set to_parsed [::sft::parse_drive $to_reference]
    if {[llength $from_parsed] != 4 || [llength $to_parsed] != 4 ||
        [lindex $from_parsed 0] ne [lindex $to_parsed 0] ||
        [lindex $from_parsed 2] ne [lindex $to_parsed 2] ||
        [lindex $from_parsed 3] ne [lindex $to_parsed 3]} {
        ::sft::fail "$context changes cell family: $from_reference -> $to_reference"
    }
    set from_evidence [::sft::lib_function_evidence $from_reference]
    set to_evidence [::sft::lib_function_evidence $to_reference]
    if {[dict get $from_evidence inputs] ne [dict get $to_evidence inputs] ||
        [dict get $from_evidence outputs] ne [dict get $to_evidence outputs]} {
        ::sft::fail "$context changes Liberty Boolean function: $from_reference -> $to_reference"
    }
    return [dict create inputs [dict get $from_evidence inputs] \
        outputs [dict get $from_evidence outputs]]
}

proc ::sft::assert_equivalent_cells {from_reference to_reference context} {
    variable functional_proofs
    set signature [::sft::validate_equivalent_cells $from_reference $to_reference $context]
    lappend functional_proofs [dict create kind equivalent_resize context $context \
        from $from_reference to $to_reference signature [dict get $signature outputs]]
}

proc ::sft::assert_noninverting_repeater {reference context} {
    variable functional_proofs
    set evidence [::sft::lib_function_evidence $reference]
    set inputs [dict get $evidence inputs]
    set outputs [dict get $evidence outputs]
    if {[llength $inputs] != 1 || [llength $outputs] != 1} {
        ::sft::fail "$context requires a one-input/one-output non-inverting cell; got $reference"
    }
    set input [lindex $inputs 0]
    set function [lindex [lindex $outputs 0] 1]
    # Liberty buffer functions are normally A, (A), or (((A))).  Reject every
    # operator rather than assuming a BUF/DLY name proves logical equivalence.
    set stripped [string map [list "(" "" ")" ""] $function]
    if {$stripped ne $input} {
        ::sft::fail "$context cell $reference is not proven non-inverting (function=$function input=$input)"
    }
    lappend functional_proofs [dict create kind noninverting_repeater context $context \
        cell $reference input $input function $function]
}

proc ::sft::assert_inverting_repeater {reference context} {
    variable functional_proofs
    set evidence [::sft::lib_function_evidence $reference]
    set inputs [dict get $evidence inputs]
    set outputs [dict get $evidence outputs]
    if {[llength $inputs] != 1 || [llength $outputs] != 1} {
        ::sft::fail "$context requires a one-input/one-output inverter; got $reference"
    }
    set input [lindex $inputs 0]
    set function [lindex [lindex $outputs 0] 1]
    set stripped [string map [list "(" "" ")" ""] $function]
    if {$stripped ni [list "!$input" "~$input"]} {
        ::sft::fail "$context cell $reference is not proven inverting (function=$function input=$input)"
    }
    lappend functional_proofs [dict create kind inverting_repeater context $context \
        cell $reference input $input function $function]
}

proc ::sft::assert_pin_compatible_noninverting_replacement {from_reference to_reference context} {
    variable functional_proofs
    set from_evidence [::sft::lib_function_evidence $from_reference]
    set to_evidence [::sft::lib_function_evidence $to_reference]
    ::sft::assert_noninverting_repeater $from_reference "$context source"
    ::sft::assert_noninverting_repeater $to_reference "$context replacement"
    if {[dict get $from_evidence inputs] ne [dict get $to_evidence inputs] ||
        [dict get $from_evidence outputs] ne [dict get $to_evidence outputs]} {
        ::sft::fail "$context changes the Liberty pin interface/function: $from_reference -> $to_reference"
    }
    lappend functional_proofs [dict create kind pin_compatible_noninverting_replacement \
        context $context from $from_reference to $to_reference \
        inputs [dict get $from_evidence inputs] outputs [dict get $from_evidence outputs]]
}

proc ::sft::available_references {current_reference} {
    variable reference_variant_cache
    set parsed [::sft::parse_drive $current_reference]
    if {[llength $parsed] != 4} {
        return {}
    }
    lassign $parsed stem current_drive flavor tail
    set family_key [list $stem $flavor $tail]
    if {[dict exists $reference_variant_cache $family_key]} {
        return [dict get $reference_variant_cache $family_key]
    }
    set pattern "${stem}_X*_${tail}"
    set cells {}
    set seen_references {}
    foreach query [list "*/$pattern" $pattern] {
        if {![catch {get_lib_cells -quiet $query} found]} {
            foreach cell [::sft::items $found] {
                set reference [file tail [::sft::object_name $cell]]
                set candidate [::sft::parse_drive $reference]
                if {[llength $candidate] == 4 &&
                    [lindex $candidate 0] eq $stem &&
                    [lindex $candidate 2] eq $flavor &&
                    [lindex $candidate 3] eq $tail &&
                    [lsearch -exact $seen_references $reference] < 0} {
                    lappend cells [list [lindex $candidate 1] $reference]
                    lappend seen_references $reference
                }
            }
        }
        if {[llength $cells]} {
            break
        }
    }
    set cells [lsort -command ::sft::compare_drive_variants $cells]
    dict set reference_variant_cache $family_key $cells
    return $cells
}

proc ::sft::resize_instance {instance direction steps} {
    variable changes
    set objects [::sft::items [get_cells -quiet [list $instance]]]
    if {[llength $objects] != 1} {
        ::sft::fail "cannot resolve resize instance $instance"
    }
    set current [file tail [lindex [::sft::property [lindex $objects 0] {ref_name base_name cell_name}] 0]]
    set replacement [::sft::replacement_for_reference $current $direction $steps]
    ::sft::assert_equivalent_cells $current $replacement "resize $instance"
    ecoChangeCell -inst $instance -cell $replacement
    lappend changes [dict create kind resize inst $instance from $current to $replacement direction $direction]
    puts "SFT_ECO_RESIZE $instance $current $replacement"
}

proc ::sft::lib_cell_exists {reference} {
    foreach query [list "*/$reference" $reference] {
        if {![catch {get_lib_cells -quiet $query} cells] && [llength [::sft::items $cells]]} {
            return 1
        }
    }
    return 0
}

proc ::sft::first_existing_cell {references} {
    foreach reference $references {
        if {[::sft::lib_cell_exists $reference]} {
            return $reference
        }
    }
    ::sft::fail "none of the requested library cells exists: $references"
}

proc ::sft::next_eco_name {tag} {
    variable eco_count
    incr eco_count
    return "${::SFT_ECO_PREFIX}${::SFT_CASE(id)}_${tag}_${eco_count}"
}

# Innovus interprets ecoAddRepeater -name as a leaf instance name.  When the
# sink term is below a hierarchy, the created database object is placed in the
# sink cell's parent hierarchy.  For example, inserting leaf name
# SFT_ECO_SETUP_001_PATH_1 on u_exp/reg/D creates the cell
# u_exp/SFT_ECO_SETUP_001_PATH_1.  Keep the command argument hierarchy-free,
# but bind every later audit/repair step to the exact database full_name.
proc ::sft::repeater_parent_for_term {target} {
    set pin [::sft::pin_collection $target]
    set cells [::sft::items [get_cells -quiet -of_objects $pin]]
    if {[llength $cells] != 1} {
        ::sft::fail "cannot resolve one owning cell for repeater term $target"
    }
    set owner [::sft::object_name [lindex $cells 0]]
    if {$owner eq "" || [regexp {[[:cntrl:]]} $owner]} {
        ::sft::fail "repeater term $target has an invalid owning-cell name"
    }
    set parent [file dirname $owner]
    if {$parent eq "."} {
        return ""
    }
    return $parent
}

proc ::sft::validate_repeater_leaf_name {leaf_name} {
    if {$leaf_name eq "" || [file tail $leaf_name] ne $leaf_name ||
        [regexp {[/\\]} $leaf_name] ||
        [regexp {[[:space:][:cntrl:]]} $leaf_name]} {
        ::sft::fail "ecoAddRepeater -name must be one legal hierarchy-free leaf name: $leaf_name"
    }
}

proc ::sft::exact_cells_named {full_name} {
    if {[catch {get_cells -quiet [list $full_name]} cells]} {
        ::sft::fail "cannot query exact cell name $full_name: $cells"
    }
    return [::sft::items $cells]
}

proc ::sft::resolve_added_repeater {target requested_name requested_cell expected_name} {
    set objects [::sft::exact_cells_named $expected_name]
    if {[llength $objects] != 1} {
        ::sft::fail "ecoAddRepeater -name $requested_name resolved to [llength $objects] instances at expected exact name $expected_name after insertion"
    }
    set object [lindex $objects 0]
    set actual_name [::sft::object_name $object]
    if {[file tail $actual_name] ne $requested_name || $actual_name ne $expected_name} {
        ::sft::fail "ecoAddRepeater -name $requested_name created unexpected instance $actual_name; expected $expected_name for term $target"
    }
    set actual_cell [::sft::property $object {ref_name base_name cell_name}]
    if {$actual_cell eq ""} {
        ::sft::fail "cannot read reference of inserted repeater $actual_name"
    }
    set actual_cell [file tail [lindex $actual_cell 0]]
    set requested_cell [file tail $requested_cell]
    if {$actual_cell ne $requested_cell} {
        ::sft::fail "ecoAddRepeater inserted $actual_name with reference $actual_cell; requested $requested_cell"
    }
    return $actual_name
}

proc ::sft::add_repeater_to_term {target cell tag change_kind} {
    variable changes
    variable eco_count
    if {[string match "*_repair" $change_kind] && $eco_count >= $::SFT_CASE(max_eco_cells)} {
        ::sft::fail "case exceeded max_eco_cells=$::SFT_CASE(max_eco_cells)"
    }
    ::sft::assert_noninverting_repeater $cell "$change_kind on $target"
    set name [::sft::next_eco_name $tag]
    ::sft::validate_repeater_leaf_name $name
    set parent [::sft::repeater_parent_for_term $target]
    set expected_name [expr {$parent eq "" ? $name : "${parent}/${name}"}]
    set existing [::sft::exact_cells_named $expected_name]
    if {[llength $existing]} {
        ::sft::fail "ecoAddRepeater expected instance $expected_name already exists"
    }
    ecoAddRepeater -term $target -cell $cell -name $name
    set actual_name [::sft::resolve_added_repeater $target $name $cell $expected_name]
    lappend changes [dict create kind $change_kind inst $actual_name cell $cell term $target]
    puts "SFT_ECO_ADD_REPEATER $actual_name $cell $target"
    return $actual_name
}

proc ::sft::clock_pin_for_endpoint {endpoint} {
    set endpoint_pin [::sft::pin_collection $endpoint]
    set cells [::sft::items [get_cells -quiet -of_objects $endpoint_pin]]
    if {[llength $cells] != 1} {
        ::sft::fail "cannot resolve capture cell for $endpoint"
    }
    set instance [::sft::object_name [lindex $cells 0]]
    set supported 0
    foreach pin [::sft::items [get_pins -quiet "${instance}/*"]] {
        set name [::sft::object_name $pin]
        set result [::sft::boolean_property_result $pin {is_clock_pin clock}]
        if {![dict get $result supported]} {
            continue
        }
        incr supported
        if {[dict get $result value]} {
            return $name
        }
    }
    if {$supported == 0} {
        ::sft::fail "cannot determine capture clock-pin classification for $endpoint"
    }
    ::sft::fail "cannot find an explicitly classified capture clock pin for $endpoint"
}

proc ::sft::discover_clock_delay_cell {} {
    set references {}
    foreach pattern $::SFT_CLOCK_CELL_GLOBS {
        foreach query [list "*/$pattern" $pattern] {
            if {![catch {get_lib_cells -quiet $query} cells]} {
                foreach object [::sft::items $cells] {
                    lappend references [file tail [::sft::object_name $object]]
                }
            }
        }
    }
    set references [lsort -dictionary -unique $references]
    if {![llength $references]} {
        ::sft::fail "no clock delay/buffer cell matched $::SFT_CLOCK_CELL_GLOBS"
    }
    if {$::SFT_FROZEN_CLOCK_CELL eq "PROBE_REQUIRED"} {
        set candidate [lindex $references 0]
        puts stderr "SFT_PROBE_REQUIRED clock candidates are sorted but not frozen; candidate=$candidate choices=$references"
        return $candidate
    }
    if {$::SFT_FROZEN_CLOCK_CELL eq "" ||
        [lsearch -exact $references $::SFT_FROZEN_CLOCK_CELL] < 0} {
        ::sft::fail "frozen clock cell $::SFT_FROZEN_CLOCK_CELL is absent from sorted candidates $references"
    }
    puts "SFT_FROZEN_CLOCK_CELL_VALIDATED $::SFT_FROZEN_CLOCK_CELL"
    return $::SFT_FROZEN_CLOCK_CELL
}

proc ::sft::targets_for_timing {mode} {
    variable targets
    set selected {}
    foreach target $targets {
        if {[dict get $target timing] eq $mode} {
            lappend selected $target
        }
    }
    return $selected
}

proc ::sft::inject_resize {mode direction steps} {
    foreach target [::sft::targets_for_timing $mode] {
        ::sft::resize_instance [dict get $target driver_inst] $direction $steps
    }
}

proc ::sft::injection_delay_cell {} {
    set reference $::SFT_FROZEN_DELAY_CELL
    if {$::SFT_CASE(calibration_status) eq "FROZEN"} {
        if {$reference eq "" || $reference eq "PROBE_REQUIRED"} {
            ::sft::fail "frozen data-delay injection has no exact cell reference"
        }
        if {![regexp {^[A-Za-z0-9_.]+$} $reference]} {
            ::sft::fail "frozen data-delay injection cell reference is unsafe: $reference"
        }
        # Do not consult the ordered global candidates here.  A Gold replay is
        # bound to the exact cell measured for this case, even if a later
        # calibration stage reorders ::SFT_DELAY_CELLS for other cases.
        set object [::sft::lib_cell_object $reference]
        if {[file tail [::sft::object_name $object]] ne $reference} {
            ::sft::fail "frozen data-delay injection resolved a non-exact Liberty cell for $reference"
        }
        puts "SFT_FROZEN_DELAY_CELL_VALIDATED $reference"
        return $reference
    }
    if {$reference ne "" && $reference ne "PROBE_REQUIRED"} {
        ::sft::fail "unfrozen data-delay reference must be empty or PROBE_REQUIRED"
    }
    set candidate [::sft::first_existing_cell $::SFT_DELAY_CELLS]
    puts stderr "SFT_PROBE_REQUIRED data-delay candidates are not case-frozen; candidate=$candidate choices=$::SFT_DELAY_CELLS"
    return $candidate
}

proc ::sft::inject_data_delay {mode count} {
    variable changes
    variable equal_area_setup_buffer
    variable injection_pre_routed
    set delay_cell [::sft::injection_delay_cell]
    set scaffolded [::sft::uses_equal_area_injection_scaffold $mode $count $delay_cell]
    set insertion_cell [expr {$scaffolded ? $equal_area_setup_buffer : $delay_cell}]
    if {$scaffolded} {
        set scaffold_object [::sft::lib_cell_object $insertion_cell]
        if {[file tail [::sft::object_name $scaffold_object]] ne $insertion_cell} {
            ::sft::fail "cannot resolve exact equal-area injection scaffold $insertion_cell"
        }
    }
    set change_start [llength $changes]
    foreach target [::sft::targets_for_timing $mode] {
        for {set index 0} {$index < $count} {incr index} {
            ::sft::add_repeater_to_term [dict get $target endpoint] \
                $insertion_cell PATH data_delay_injection
        }
    }
    if {!$scaffolded} {
        return
    }

    # Establish a routed, physically legal buffer scaffold before creating the
    # timing violation.  The final scaffold-to-delay swap and the Gold
    # delay-to-buffer repair then dirty the same four instances/five nets,
    # avoiding route-state asymmetry from the initial ecoAddRepeater topology.
    ::sft::legalize_and_route
    setEcoMode -batchMode true
    for {set index $change_start} {$index < [llength $changes]} {incr index} {
        set change [lindex $changes $index]
        set instance [dict get $change inst]
        ::sft::assert_pin_compatible_noninverting_replacement \
            $insertion_cell $delay_cell "equal-area injection scaffold $instance"
        ecoChangeCell -inst $instance -cell $delay_cell
        dict set change scaffold_cell $insertion_cell
        dict set change cell $delay_cell
        lset changes $index $change
    }
    setEcoMode -batchMode false
    ::sft::legalize_and_route
    set injection_pre_routed 1
    puts "SFT_EQUAL_AREA_INJECTION_SCAFFOLD_ROUTED cell=$insertion_cell delay=$delay_cell instances=[expr {[llength $changes] - $change_start}]"
}

proc ::sft::uses_equal_area_injection_scaffold {mode count delay_cell} {
    return [expr {
        $mode eq "late" &&
        $count >= 2 &&
        $delay_cell eq "DLY2_X4M_A9TR40" &&
        $::SFT_CASE(repair_strategy) eq "replace_delay_and_upsize"
    }]
}

proc ::sft::uses_paired_inverter_repair {} {
    # This exception disables Innovus' per-cell LEQ guard, so bind it to the
    # complete frozen SETUP_004 repair profile rather than to a cell/count
    # coincidence that a future case could accidentally inherit.
    foreach key {
        id type repair_mode injection_strategy calibration_status
        setup_delay_cells repair_strategy
    } {
        if {![info exists ::SFT_CASE($key)]} { return 0 }
    }
    if {![info exists ::SFT_FROZEN_DELAY_CELL]} { return 0 }
    return [expr {
        $::SFT_CASE(id) eq "SETUP_004" &&
        $::SFT_CASE(type) eq "setup" &&
        $::SFT_CASE(repair_mode) eq "surgical" &&
        $::SFT_CASE(injection_strategy) eq "insert_data_delay" &&
        $::SFT_CASE(calibration_status) eq "FROZEN" &&
        $::SFT_FROZEN_DELAY_CELL eq "DLY4_X4M_A9TR40" &&
        $::SFT_CASE(setup_delay_cells) == 2 &&
        $::SFT_CASE(repair_strategy) eq "replace_delay_and_upsize"
    }]
}

proc ::sft::uses_hold002_fixed_repeater_location {} {
    # Real Innovus 21.10 placement/routing probes showed that HOLD_002 needs
    # its repair delay at one specific legal near-sink site.  The default
    # placement creates a new SHORT at u_nan/U77, while {898.00 248.08}
    # preserves every full-chip DRC category and positive setup/hold slack.
    # Bind the placement exception to the complete frozen case profile so a
    # future case cannot inherit it from a coincidental strategy match.
    foreach key {
        id type difficulty repair_mode injection_strategy calibration_status
        repair_strategy max_eco_cells repair_delay_cells_per_endpoint
        hold_clock_cell_count hold_target_count setup_target_count
    } {
        if {![info exists ::SFT_CASE($key)]} { return 0 }
    }
    foreach global {::SFT_FROZEN_CLOCK_CELL ::SFT_REPAIR_DELAY_CELL ::SFT_SELECTORS} {
        if {![info exists $global]} { return 0 }
    }
    if {[llength $::SFT_SELECTORS] != 1} { return 0 }
    set selector [lindex $::SFT_SELECTORS 0]
    foreach key {role timing hierarchy_class stable_rank count} {
        if {![dict exists $selector $key]} { return 0 }
    }
    return [expr {
        $::SFT_CASE(id) eq "HOLD_002" &&
        $::SFT_CASE(type) eq "hold" &&
        $::SFT_CASE(difficulty) eq "medium" &&
        $::SFT_CASE(repair_mode) eq "surgical" &&
        $::SFT_CASE(injection_strategy) eq "local_capture_clock_delay" &&
        $::SFT_CASE(calibration_status) eq "FROZEN" &&
        $::SFT_CASE(repair_strategy) eq "insert_delay_and_downsize" &&
        $::SFT_CASE(max_eco_cells) == 4 &&
        $::SFT_CASE(repair_delay_cells_per_endpoint) == 1 &&
        $::SFT_CASE(hold_clock_cell_count) == 2 &&
        $::SFT_CASE(hold_target_count) == 1 &&
        $::SFT_CASE(setup_target_count) == 0 &&
        $::SFT_FROZEN_CLOCK_CELL eq "DLYCLK8S6_X1B_A9TR40" &&
        $::SFT_REPAIR_DELAY_CELL eq "DLY4_X0P5M_A9TR40" &&
        [dict get $selector role] eq "hold_primary" &&
        [dict get $selector timing] eq "early" &&
        [dict get $selector hierarchy_class] eq "top_tree" &&
        [dict get $selector stable_rank] == 487 &&
        [dict get $selector count] == 1
    }]
}

proc ::sft::validate_hold002_fixed_repeater_target {} {
    variable targets
    if {![::sft::uses_hold002_fixed_repeater_location]} {
        ::sft::fail "HOLD_002 fixed repeater location requested without its frozen case fingerprint"
    }
    if {[llength $targets] != 1} {
        ::sft::fail "HOLD_002 fixed repeater location requires exactly one resolved target"
    }
    set target [lindex $targets 0]
    set expected [dict create \
        endpoint mac_out_nan_reg/D \
        net FE_OFN16864_pp_nan_pvld_d2_0 \
        driver_pin FE_OFC8281_pp_nan_pvld_d2_0/Y \
        driver_inst FE_OFC8281_pp_nan_pvld_d2_0 \
        driver_ref BUF_X1B_A9TR40 \
        role hold_primary \
        timing early]
    foreach key [dict keys $expected] {
        if {![dict exists $target $key] ||
            [dict get $target $key] ne [dict get $expected $key]} {
            ::sft::fail "HOLD_002 fixed repeater target fingerprint mismatch at $key"
        }
    }
}

proc ::sft::uses_hold003_fixed_repeater_locations {} {
    # Real Innovus 21.10 placement/routing probes isolated HOLD_003's physical
    # failure to the default repeater placement.  The two adjacent legal sites
    # below leave every existing instance stationary, preserve positive
    # setup/hold slack, keep connectivity clean, and reproduce every full-chip
    # DRC category exactly.  Bind this exception to the complete frozen case
    # and selector fingerprint so no future case can inherit the probe-derived
    # locations.
    foreach key {
        id type difficulty repair_mode injection_strategy calibration_status
        repair_strategy max_eco_cells repair_delay_cells_per_endpoint
        hold_clock_cell_count hold_target_count setup_target_count
    } {
        if {![info exists ::SFT_CASE($key)]} { return 0 }
    }
    foreach global {::SFT_FROZEN_CLOCK_CELL ::SFT_REPAIR_DELAY_CELL ::SFT_SELECTORS} {
        if {![info exists $global]} { return 0 }
    }
    if {[llength $::SFT_SELECTORS] != 1} { return 0 }
    set selector [lindex $::SFT_SELECTORS 0]
    foreach key {role timing hierarchy_class stable_rank count} {
        if {![dict exists $selector $key]} { return 0 }
    }
    return [expr {
        $::SFT_CASE(id) eq "HOLD_003" &&
        $::SFT_CASE(type) eq "hold" &&
        $::SFT_CASE(difficulty) eq "hard" &&
        $::SFT_CASE(repair_mode) eq "surgical" &&
        $::SFT_CASE(injection_strategy) eq "local_capture_clock_delay" &&
        $::SFT_CASE(calibration_status) eq "FROZEN" &&
        $::SFT_CASE(repair_strategy) eq "insert_data_delay" &&
        $::SFT_CASE(max_eco_cells) == 2 &&
        $::SFT_CASE(repair_delay_cells_per_endpoint) == 2 &&
        $::SFT_CASE(hold_clock_cell_count) == 2 &&
        $::SFT_CASE(hold_target_count) == 1 &&
        $::SFT_CASE(setup_target_count) == 0 &&
        $::SFT_FROZEN_CLOCK_CELL eq "DLYCLK8S8_X1B_A9TR40" &&
        $::SFT_REPAIR_DELAY_CELL eq "DLY4_X0P5M_A9TR40" &&
        [dict get $selector role] eq "hold_primary" &&
        [dict get $selector timing] eq "early" &&
        [dict get $selector hierarchy_class] eq "top_tree" &&
        [dict get $selector stable_rank] == 142 &&
        [dict get $selector count] == 1
    }]
}

proc ::sft::validate_hold003_fixed_repeater_target {} {
    variable targets
    if {![::sft::uses_hold003_fixed_repeater_locations]} {
        ::sft::fail "HOLD_003 fixed repeater locations requested without their frozen case fingerprint"
    }
    if {[llength $targets] != 1} {
        ::sft::fail "HOLD_003 fixed repeater locations require exactly one resolved target"
    }
    set target [lindex $targets 0]
    set expected [dict create \
        endpoint pp_nan_mts_d2_reg_8_/D \
        net n3086 \
        driver_pin U28144/Y \
        driver_inst U28144 \
        driver_ref OAI21_X0P5M_A9TR40 \
        role hold_primary \
        timing early \
        beginpoint pp_nan_mts_d1_reg_8_/CK]
    foreach key [dict keys $expected] {
        if {![dict exists $target $key] ||
            [dict get $target $key] ne [dict get $expected $key]} {
            ::sft::fail "HOLD_003 fixed repeater target fingerprint mismatch at $key"
        }
    }
}

proc ::sft::uses_mixed001_strong_restore_buffer {} {
    # Two real Innovus 21.10 replays left MIXED_001 at setup WNS=-0.015 ns
    # and TNS=-0.029 ns when the injected delay chain was restored with
    # BUF_X0P7M_A9TR40.  A follow-up BUF_X1M_A9TR40 probe improved WNS to
    # -0.002 ns but still missed the +0.010 ns Gold threshold.  Bind the
    # same-family BUF_X2M repair to the complete frozen case and selector
    # fingerprint; the generic catalog preference remains unchanged.
    foreach key {
        id type difficulty repair_mode injection_strategy calibration_status
        repair_strategy max_eco_cells repair_delay_cells_per_endpoint
        hold_clock_cell_count setup_delay_cells hold_target_count
        setup_target_count
    } {
        if {![info exists ::SFT_CASE($key)]} { return 0 }
    }
    foreach global {
        ::SFT_FROZEN_DELAY_CELL ::SFT_REPAIR_DELAY_CELL
        ::SFT_FROZEN_CLOCK_CELL ::SFT_BUFFER_CELLS ::SFT_SELECTORS
    } {
        if {![info exists $global]} { return 0 }
    }
    if {$::SFT_BUFFER_CELLS ne {BUF_X0P7M_A9TR40 BUF_X1M_A9TR40} ||
        [llength $::SFT_SELECTORS] != 2} {
        return 0
    }
    set setup_selector [lindex $::SFT_SELECTORS 0]
    set hold_selector [lindex $::SFT_SELECTORS 1]
    foreach key {role timing hierarchy_class stable_rank count} {
        if {![dict exists $setup_selector $key] ||
            ![dict exists $hold_selector $key]} {
            return 0
        }
    }
    return [expr {
        $::SFT_CASE(id) eq "MIXED_001" &&
        $::SFT_CASE(type) eq "mixed" &&
        $::SFT_CASE(difficulty) eq "medium" &&
        $::SFT_CASE(repair_mode) eq "surgical" &&
        $::SFT_CASE(injection_strategy) eq "mixed_data_delay_and_capture_skew" &&
        $::SFT_CASE(calibration_status) eq "FROZEN" &&
        $::SFT_CASE(repair_strategy) eq "coordinated_setup_hold" &&
        $::SFT_CASE(max_eco_cells) == 6 &&
        $::SFT_CASE(repair_delay_cells_per_endpoint) == 1 &&
        $::SFT_CASE(hold_clock_cell_count) == 2 &&
        $::SFT_CASE(setup_delay_cells) == 2 &&
        $::SFT_CASE(hold_target_count) == 1 &&
        $::SFT_CASE(setup_target_count) == 2 &&
        $::SFT_FROZEN_DELAY_CELL eq "DLY2_X4M_A9TR40" &&
        $::SFT_REPAIR_DELAY_CELL eq "DLY4_X0P5M_A9TR40" &&
        $::SFT_FROZEN_CLOCK_CELL eq "DLYCLK8S6_X1B_A9TR40" &&
        [dict get $setup_selector role] eq "setup_primary" &&
        [dict get $setup_selector timing] eq "late" &&
        [dict get $setup_selector hierarchy_class] eq "exp" &&
        [dict get $setup_selector stable_rank] == 17 &&
        [dict get $setup_selector count] == 2 &&
        [dict get $hold_selector role] eq "hold_primary" &&
        [dict get $hold_selector timing] eq "early" &&
        [dict get $hold_selector hierarchy_class] eq "top_tree" &&
        [dict get $hold_selector stable_rank] == 319 &&
        [dict get $hold_selector count] == 1
    }]
}

proc ::sft::validate_mixed001_strong_restore_targets {} {
    variable targets
    if {![::sft::uses_mixed001_strong_restore_buffer]} {
        ::sft::fail "MIXED_001 strong restore buffer requested without its frozen case fingerprint"
    }
    set expected [list \
        [dict create endpoint u_exp/exp_sft_31_reg_0_/D net u_exp/n1808 \
            driver_pin u_exp/U4457/Y driver_inst u_exp/U4457 \
            driver_ref OAI211_X1M_A9TR40 role setup_primary timing late \
            beginpoint u_exp/cfg_is_fp16_d1_reg_0_/CK] \
        [dict create endpoint u_exp/exp_sft_23_reg_0_/D net u_exp/n1840 \
            driver_pin u_exp/U4528/Y driver_inst u_exp/U4528 \
            driver_ref OAI211_X1M_A9TR40 role setup_primary timing late \
            beginpoint u_exp/cfg_is_fp16_d1_reg_0_/CK] \
        [dict create endpoint pp_exp_d2_reg_2_/D net n15263 \
            driver_pin U26729/Y driver_inst U26729 \
            driver_ref OA22_X0P5M_A9TR40 role hold_primary timing early \
            beginpoint pp_exp_d1_reg_2_/CK]]
    if {[llength $targets] != [llength $expected]} {
        ::sft::fail "MIXED_001 strong restore requires exactly three resolved targets"
    }
    foreach actual $targets reference $expected {
        foreach key [dict keys $reference] {
            if {![dict exists $actual $key] ||
                [dict get $actual $key] ne [dict get $reference $key]} {
                ::sft::fail "MIXED_001 strong restore target fingerprint mismatch at $key"
            }
        }
    }
}

proc ::sft::coordinated_restore_buffer {} {
    if {![::sft::uses_mixed001_strong_restore_buffer]} {
        return [::sft::first_existing_cell $::SFT_BUFFER_CELLS]
    }
    ::sft::validate_mixed001_strong_restore_targets
    set buffer BUF_X2M_A9TR40
    set object [::sft::lib_cell_object $buffer]
    if {[file tail [::sft::object_name $object]] ne "BUF_X2M_A9TR40"} {
        ::sft::fail "cannot resolve exact MIXED_001 strong restore buffer BUF_X2M_A9TR40"
    }
    return $buffer
}

proc ::sft::inject_capture_skew {mode count} {
    if {![string is integer -strict $count] || $count < 1} {
        ::sft::fail "capture-skew injection count must be a positive integer"
    }
    set cell [::sft::discover_clock_delay_cell]
    set selected [::sft::targets_for_timing $mode]
    if {[llength $selected] != 1} {
        ::sft::fail "capture-skew injection requires exactly one $mode endpoint; got [llength $selected]"
    }
    set first [lindex $selected 0]
    set clock_pin [::sft::clock_pin_for_endpoint [dict get $first endpoint]]
    for {set index 0} {$index < $count} {incr index} {
        ::sft::add_repeater_to_term $clock_pin $cell CLOCKPATH capture_clock_injection
    }
}

proc ::sft::net_driver_instance {sink_pin_name} {
    set sink [::sft::pin_collection $sink_pin_name]
    set nets [::sft::items [get_nets -quiet -of_objects $sink]]
    if {[llength $nets] != 1} {
        ::sft::fail "cannot trace one net into $sink_pin_name"
    }
    set drivers {}
    foreach pin [::sft::items [get_pins -quiet -of_objects [lindex $nets 0]]] {
        set direction [string tolower [::sft::property $pin {direction pin_direction}]]
        if {$direction in {out output inout}} { lappend drivers $pin }
    }
    if {[llength $drivers] != 1} {
        ::sft::fail "cannot trace one driver into $sink_pin_name"
    }
    set cells [::sft::items [get_cells -quiet -of_objects [lindex $drivers 0]]]
    if {[llength $cells] != 1} {
        ::sft::fail "driver of $sink_pin_name is not one leaf instance"
    }
    return [::sft::object_name [lindex $cells 0]]
}

proc ::sft::single_input_pin {instance} {
    set cells [::sft::items [get_cells -quiet [list $instance]]]
    if {[llength $cells] != 1} { ::sft::fail "cannot resolve inserted repeater $instance" }
    set inputs {}
    foreach pin [::sft::items [get_pins -quiet -of_objects [lindex $cells 0]]] {
        set direction [string tolower [::sft::property $pin {direction pin_direction}]]
        if {$direction in {in input}} { lappend inputs [::sft::object_name $pin] }
    }
    if {[llength $inputs] != 1} {
        ::sft::fail "inserted repeater $instance does not expose exactly one data input"
    }
    return [lindex $inputs 0]
}

proc ::sft::validate_injected_repeater_chains {} {
    variable changes
    set terms {}
    foreach change $changes {
        if {[dict get $change kind] in {
            data_delay_injection capture_clock_injection
        }} {
            dict lappend terms [dict get $change term] [dict get $change inst]
        }
    }
    dict for {term expected} $terms {
        set remaining [lsort -unique $expected]
        set current [::sft::net_driver_instance $term]
        set visited {}
        while {[lsearch -exact $remaining $current] >= 0} {
            if {[lsearch -exact $visited $current] >= 0} {
                ::sft::fail "cycle detected while tracing injected repeater chain at $term"
            }
            lappend visited $current
            set input [::sft::single_input_pin $current]
            set current [::sft::net_driver_instance $input]
        }
        if {[lsort -unique $visited] ne $remaining} {
            puts stderr "SFT_PROBE_REQUIRED successive ecoAddRepeater -term did not form one auditable serial chain"
            ::sft::fail "injected repeater chain mismatch at $term: expected=$remaining traced=[lsort -unique $visited]"
        }
    }
    if {[dict size $terms]} {
        puts "SFT_INJECTION_CHAINS_VALIDATED terms=[dict size $terms]"
    }
}

proc ::sft::json_quote {value} {
    set escaped [string map [list \\ \\\\ \" \\\" \n \\n \r \\r \t \\t] $value]
    return "\"$escaped\""
}

proc ::sft::injection_measurement {} {
    lassign [::sft::timing_summary late] setup_wns setup_tns
    lassign [::sft::timing_summary early] hold_wns hold_tns
    return [dict create setup_wns $setup_wns setup_tns $setup_tns \
        hold_wns $hold_wns hold_tns $hold_tns]
}

proc ::sft::provenance_environment {name} {
    if {![info exists ::env($name)] || $::env($name) eq ""} {
        ::sft::fail "required provenance environment variable $name is missing"
    }
    return $::env($name)
}

proc ::sft::provenance_sha256 {name} {
    set value [::sft::provenance_environment $name]
    if {![regexp {^[0-9a-f]{64}$} $value]} {
        ::sft::fail "provenance environment variable $name is not a lowercase SHA256"
    }
    return $value
}

proc ::sft::validate_baseline_guard {} {
    set measurement [::sft::injection_measurement]
    foreach check {setup hold} {
        set wns [dict get $measurement ${check}_wns]
        set tns [dict get $measurement ${check}_tns]
        if {$wns < $::SFT_CASE(baseline_wns_min) || $tns < -1.0e-12} {
            ::sft::fail "immutable baseline fails $check guard: WNS=$wns TNS=$tns required WNS>=$::SFT_CASE(baseline_wns_min), TNS=0"
        }
    }
    set baseline_sha [::sft::provenance_sha256 SFT_BASELINE_SHA256]
    set catalog_sha [::sft::provenance_sha256 SFT_CATALOG_SHA256]
    set qualification_sha [::sft::provenance_sha256 SFT_BASELINE_QUALIFICATION_SHA256]
    set checksums_sha [::sft::provenance_sha256 SFT_BASELINE_CHECKSUMS_SHA256]
    set qualification_status [::sft::provenance_environment SFT_BASELINE_STATUS]
    set technology [::sft::provenance_environment SFT_TECHNOLOGY_CLASSIFICATION]
    if {$qualification_status ne "QUALIFIED_CANDIDATE" ||
        $technology ne "derived_non_signoff"} {
        ::sft::fail "baseline provenance is not the qualified derived_non_signoff adapter: status=$qualification_status technology=$technology"
    }
    set path [file join $::SFT_REPORT_DIR baseline_guard.json]
    set stream [open $path w]
    puts $stream "{"
    puts $stream {  "schema_version": "timing_eco_baseline_guard.v1",}
    puts $stream "  \"case_id\": [::sft::json_quote $::SFT_CASE(id)],"
    puts $stream "  \"minimum_wns_ns\": $::SFT_CASE(baseline_wns_min),"
    puts $stream "  \"baseline_sha256\": [::sft::json_quote $baseline_sha],"
    puts $stream "  \"catalog_sha256\": [::sft::json_quote $catalog_sha],"
    puts $stream "  \"qualification_sha256\": [::sft::json_quote $qualification_sha],"
    puts $stream "  \"checksum_manifest_sha256\": [::sft::json_quote $checksums_sha],"
    puts $stream "  \"qualification_status\": [::sft::json_quote $qualification_status],"
    puts $stream "  \"technology_classification\": [::sft::json_quote $technology],"
    puts $stream {  "gold_status": false,}
    puts $stream {  "signoff_eligible": false,}
    puts $stream "  \"setup\": {\"wns_ns\": [dict get $measurement setup_wns], \"tns_ns\": [dict get $measurement setup_tns]},"
    puts $stream "  \"hold\": {\"wns_ns\": [dict get $measurement hold_wns], \"tns_ns\": [dict get $measurement hold_tns]},"
    puts $stream {  "passed": true}
    puts $stream "}"
    close $stream
    puts "SFT_BASELINE_GUARD_VALIDATED setup_wns=[dict get $measurement setup_wns] hold_wns=[dict get $measurement hold_wns]"
}

proc ::sft::measurement_in_target {measurement} {
    set required {}
    if {$::SFT_CASE(type) in {setup mixed}} { lappend required setup }
    if {$::SFT_CASE(type) in {hold mixed}} { lappend required hold }
    foreach check $required {
        set wns [dict get $measurement ${check}_wns]
        set low $::SFT_CASE(${check}_wns_low)
        set high $::SFT_CASE(${check}_wns_high)
        if {$wns < $low || $wns > $high} {
            return 0
        }
    }
    return 1
}

proc ::sft::json_string_array {values} {
    set result "\["
    set separator ""
    foreach value $values {
        append result $separator [::sft::json_quote $value]
        set separator ", "
    }
    append result "\]"
    return $result
}

# Measure a selected endpoint independently of the global violation query.
# The exact pin collection, timing direction, and MMMC view are all explicit:
# a non-violating selected endpoint must still leave a finite post-injection
# slack in violation_locality.json so a failed calibration remains auditable.
proc ::sft::exact_endpoint_slack {mode endpoint} {
    if {$mode ni {late early}} {
        ::sft::fail "unsupported exact endpoint timing mode $mode"
    }
    set endpoint_pin [::sft::pin_collection $endpoint]
    set command [list report_timing -collection -max_paths 1 \
        -path_type full_clock -to $endpoint_pin]
    lappend command [expr {$mode eq "late" ? "-late" : "-early"}]
    set view [::sft::view_for $mode]
    if {$view eq ""} {
        ::sft::fail "exact endpoint slack has no analysis view for $mode/$endpoint"
    }
    lappend command -view $view
    if {[catch {uplevel #0 $command} paths]} {
        ::sft::fail "exact endpoint report_timing failed for $mode/$endpoint: $paths"
    }
    set paths [::sft::items $paths]
    if {[llength $paths] != 1} {
        ::sft::fail "exact endpoint report_timing returned [llength $paths] paths for $mode/$endpoint"
    }
    set path [lindex $paths 0]
    set reported_endpoint [::sft::path_field $path endpoint]
    if {$reported_endpoint ne $endpoint} {
        ::sft::fail "exact endpoint report_timing returned $reported_endpoint for requested $mode/$endpoint"
    }
    set slack [::sft::path_field $path slack]
    if {![string is double -strict $slack] || [regexp -nocase {nan|inf} $slack]} {
        ::sft::fail "exact endpoint slack is not finite for $mode/$endpoint: $slack"
    }
    return $slack
}

proc ::sft::selected_endpoint_slacks {mode endpoints {negative {}}} {
    set result {}
    foreach endpoint $endpoints {
        if {[dict exists $result $endpoint]} {
            ::sft::fail "duplicate selected endpoint while measuring $mode slack: $endpoint"
        }
        if {[dict exists $negative $endpoint]} {
            # negative_endpoint_slacks already queried every violating path in
            # this mode/view and retained the worst slack for each endpoint.
            # Reuse that real post-injection value instead of issuing an
            # equivalent exact-endpoint report_timing query a second time.
            set slack [dict get $negative $endpoint]
            if {![string is double -strict $slack] ||
                [regexp -nocase {nan|inf} $slack] || $slack >= 0.0} {
                ::sft::fail "cached negative endpoint slack is invalid for $mode/$endpoint: $slack"
            }
        } else {
            # A selected endpoint can legitimately be non-violating in the
            # opposite timing direction.  It still needs one exact, finite
            # measurement so violation_locality.json remains complete and a
            # missing negative-path record can never be treated as clean.
            set slack [::sft::exact_endpoint_slack $mode $endpoint]
        }
        dict set result $endpoint $slack
    }
    if {[dict size $result] != [llength $endpoints]} {
        ::sft::fail "$mode selected endpoint slack coverage is incomplete"
    }
    return $result
}

proc ::sft::json_endpoint_slacks {endpoints slacks} {
    set result "\["
    set separator ""
    foreach endpoint $endpoints {
        if {![dict exists $slacks $endpoint]} {
            ::sft::fail "cannot serialize missing selected endpoint slack for $endpoint"
        }
        append result $separator \
            "{\"endpoint\": [::sft::json_quote $endpoint], \"slack_ns\": [dict get $slacks $endpoint]}"
        set separator ", "
    }
    append result "\]"
    return $result
}

proc ::sft::negative_endpoint_slacks {mode} {
    set paths [::sft::violating_timing_paths $mode 10000]
    if {[llength $paths] >= 10000} {
        ::sft::fail "$mode violation-locality audit is truncated at 10000 paths"
    }
    set result {}
    foreach path $paths {
        set slack [::sft::path_field $path slack]
        set endpoint [::sft::path_field $path endpoint]
        if {$endpoint eq "" || $slack eq "" || $slack >= 0.0} { continue }
        if {![dict exists $result $endpoint] || $slack < [dict get $result $endpoint]} {
            dict set result $endpoint $slack
        }
    }
    return $result
}

proc ::sft::write_violation_locality {measurement} {
    variable injection_violation_slacks
    variable injection_scope
    set injection_violation_slacks {}
    set injection_scope {}
    set errors {}
    foreach check {setup hold} mode {late early} {
        set selected {}
        foreach target [::sft::targets_for_timing $mode] {
            lappend selected [dict get $target endpoint]
        }
        set selected [lsort -unique $selected]
        set expected_count $::SFT_CASE(${check}_target_count)
        set cardinality_passed [expr {[llength $selected] == $expected_count}]
        if {!$cardinality_passed} {
            lappend errors "$check selected cluster count regressed: expected=$expected_count actual=[llength $selected]"
        }
        set negative [::sft::negative_endpoint_slacks $mode]
        set selected_slacks [::sft::selected_endpoint_slacks $mode $selected $negative]
        set violating [lsort [dict keys $negative]]
        set required [expr {$::SFT_CASE(type) eq $check || $::SFT_CASE(type) eq "mixed"}]
        set locality_passed 1
        set coverage_passed 1
        set headroom_passed 1
        set slack_consistency_passed 1
        set selected_negative {}
        set selected_wns 1.0e30
        set selected_tns 0.0
        foreach endpoint $selected {
            set slack [dict get $selected_slacks $endpoint]
            if {$slack < $selected_wns} { set selected_wns $slack }
            if {$slack < 0.0} {
                lappend selected_negative $endpoint
                set selected_tns [expr {$selected_tns + $slack}]
            }
            if {[dict exists $negative $endpoint]} {
                if {$slack >= 0.0 ||
                    abs($slack - [dict get $negative $endpoint]) > 1.0e-6} {
                    set slack_consistency_passed 0
                }
            } elseif {$slack < 0.0} {
                set slack_consistency_passed 0
            }
        }
        set selected_negative [lsort $selected_negative]
        if {$required} {
            foreach endpoint $violating {
                if {[lsearch -exact $selected $endpoint] < 0} {
                    set locality_passed 0
                }
            }
            set coverage_passed [expr {$violating eq $selected &&
                [llength $violating] == $expected_count && $cardinality_passed}]
            if {!$locality_passed || !$coverage_passed} {
                lappend errors "$check violation locality/coverage failed: selected=$selected violating=$violating expected_count=$expected_count"
            }
            set measured_wns [dict get $measurement ${check}_wns]
            set measured_tns [dict get $measurement ${check}_tns]
            if {![llength $selected] || $selected_negative ne $violating ||
                abs($selected_wns - $measured_wns) > 1.0e-6 ||
                abs($selected_tns - $measured_tns) > 1.0e-6} {
                set slack_consistency_passed 0
            }
        } else {
            set wns [dict get $measurement ${check}_wns]
            set tns [dict get $measurement ${check}_tns]
            if {[llength $violating] || $wns < $::SFT_CASE(opposite_wns_min) ||
                $tns < -1.0e-12} {
                set headroom_passed 0
                set locality_passed [expr {![llength $violating]}]
                set coverage_passed $locality_passed
                lappend errors "$::SFT_CASE(type)-only injection polluted opposite $check timing: WNS=$wns TNS=$tns violations=$violating required WNS>=$::SFT_CASE(opposite_wns_min), TNS=0"
            }
        }
        if {!$slack_consistency_passed} {
            lappend errors "$check selected endpoint slack evidence disagrees with negative endpoints or WNS/TNS: selected_slacks=$selected_slacks selected_negative=$selected_negative violating=$violating WNS=[dict get $measurement ${check}_wns] TNS=[dict get $measurement ${check}_tns]"
        }
        foreach endpoint $violating {
            dict set injection_violation_slacks [list $mode $endpoint] [dict get $negative $endpoint]
        }
        dict set injection_scope $check [dict create \
            required $required selected $selected violating $violating \
            expected_count $expected_count wns [dict get $measurement ${check}_wns] \
            tns [dict get $measurement ${check}_tns] \
            cardinality_passed $cardinality_passed locality_passed $locality_passed \
            coverage_passed $coverage_passed headroom_passed $headroom_passed]
        dict set injection_scope $check selected_slacks $selected_slacks
        dict set injection_scope $check slack_consistency_passed $slack_consistency_passed
    }
    set path [file join $::SFT_REPORT_DIR violation_locality.json]
    set stream [open $path w]
    puts $stream "{"
    puts $stream {  "schema_version": "timing_eco_violation_locality.v1",}
    puts $stream "  \"case_id\": [::sft::json_quote $::SFT_CASE(id)],"
    puts $stream "  \"repair_mode\": [::sft::json_quote $::SFT_CASE(repair_mode)],"
    foreach check {setup hold} {
        set item [dict get $injection_scope $check]
        set comma ","
        set required_json [expr {[dict get $item required] ? "true" : "false"}]
        set cardinality_json [expr {[dict get $item cardinality_passed] ? "true" : "false"}]
        set locality_json [expr {[dict get $item locality_passed] ? "true" : "false"}]
        set coverage_json [expr {[dict get $item coverage_passed] ? "true" : "false"}]
        set headroom_json [expr {[dict get $item headroom_passed] ? "true" : "false"}]
        set slack_consistency_json [expr {[dict get $item slack_consistency_passed] ? "true" : "false"}]
        puts $stream "  \"$check\": {\"required\": $required_json, \"wns_ns\": [dict get $item wns], \"tns_ns\": [dict get $item tns], \"expected_endpoint_count\": [dict get $item expected_count], \"selected_endpoint_count\": [llength [dict get $item selected]], \"violating_endpoint_count\": [llength [dict get $item violating]], \"selected_endpoints\": [::sft::json_string_array [dict get $item selected]], \"selected_endpoint_slacks\": [::sft::json_endpoint_slacks [dict get $item selected] [dict get $item selected_slacks]], \"violating_endpoints\": [::sft::json_string_array [dict get $item violating]], \"cardinality_passed\": $cardinality_json, \"locality_passed\": $locality_json, \"coverage_passed\": $coverage_json, \"opposite_headroom_passed\": $headroom_json, \"selected_slack_consistency_passed\": $slack_consistency_json}$comma"
    }
    set passed_json [expr {![llength $errors] ? "true" : "false"}]
    puts $stream "  \"passed\": $passed_json,"
    puts $stream "  \"reasons\": [::sft::json_string_array $errors]"
    puts $stream "}"
    close $stream
    if {[llength $errors]} {
        ::sft::fail [join $errors {; }]
    }
    puts "SFT_VIOLATION_LOCALITY_VALIDATED setup=$::SFT_CASE(setup_target_count) hold=$::SFT_CASE(hold_target_count)"
}

proc ::sft::diagnostic_string {value field} {
    if {$value eq "" || [regexp {[[:cntrl:]]} $value]} {
        ::sft::fail "diagnostic target $field is empty or contains a control character"
    }
    return $value
}

proc ::sft::compare_diagnostic_targets {left right} {
    foreach key {role timing endpoint beginpoint net driver_pin driver_inst driver_ref} {
        set order [string compare [dict get $left $key] [dict get $right $key]]
        if {$order != 0} { return $order }
    }
    return 0
}

proc ::sft::diagnostic_local_cells {target current_driver} {
    set original_inst [dict get $target driver_inst]
    set current_inst [dict get $current_driver driver_inst]
    set result {}
    set visited {}
    set limit 8
    for {set depth 0} {$depth < $limit} {incr depth} {
        if {[lsearch -exact $visited $current_inst] >= 0} {
            ::sft::fail "cycle in post-injection data-driver chain for [dict get $target endpoint] at $current_inst"
        }
        lappend visited $current_inst
        set current_ref [::sft::current_reference $current_inst]
        lappend result [dict create \
            inst [::sft::diagnostic_string $current_inst local_cells.inst] \
            ref [::sft::diagnostic_string $current_ref local_cells.ref]]
        if {$current_inst eq $original_inst} {
            return $result
        }
        # Every injected data-delay stage must be a one-input repeater. Walking
        # its unique input net is a current-DB observation and does not rely on
        # the hidden injection action list.
        if {[catch {::sft::single_input_pin $current_inst} input_pin]} {
            ::sft::fail "cannot walk diagnostic data-driver chain through $current_inst before reaching original driver $original_inst: $input_pin"
        }
        if {[catch {::sft::net_driver_instance $input_pin} upstream]} {
            ::sft::fail "cannot resolve upstream diagnostic driver from $current_inst/$input_pin: $upstream"
        }
        set current_inst $upstream
    }
    ::sft::fail "post-injection data-driver chain exceeds $limit cells before original driver $original_inst"
}

proc ::sft::json_local_cells {cells} {
    set result "\["
    set separator ""
    foreach cell $cells {
        append result $separator "{\"inst\": " \
            [::sft::json_quote [dict get $cell inst]] ", \"ref\": " \
            [::sft::json_quote [dict get $cell ref]] "}"
        set separator ", "
    }
    append result "\]"
    return $result
}

proc ::sft::write_diagnostic_context {} {
    variable targets
    variable injection_violation_slacks
    variable diagnostic_existing_instances
    set diagnostic_existing_instances {}
    set records {}
    set seen {}
    foreach target $targets {
        set mode [dict get $target timing]
        set endpoint [dict get $target endpoint]
        set key [list $mode $endpoint]
        if {![dict exists $injection_violation_slacks $key]} {
            ::sft::fail "diagnostic target $mode/$endpoint has no proven negative slack"
        }
        if {[dict exists $seen $key]} {
            ::sft::fail "diagnostic context contains duplicate target $mode/$endpoint"
        }
        dict set seen $key 1
        # Re-resolve the repair-facing data driver after injection and ECO
        # route. Delay insertion changes the endpoint's immediate driver/net;
        # emitting the restore-time driver would make the visible task
        # inconsistent with the concrete repair object.
        if {[catch {::sft::driver_info $endpoint} current_driver]} {
            ::sft::fail "cannot re-resolve post-injection diagnostic driver for $endpoint: $current_driver"
        }
        set record {}
        foreach field {role timing endpoint beginpoint} {
            dict set record $field [::sft::diagnostic_string [dict get $target $field] $field]
        }
        foreach field {net driver_pin driver_inst driver_ref} {
            dict set record $field [::sft::diagnostic_string [dict get $current_driver $field] $field]
        }
        dict set record original_driver [dict create \
            inst [::sft::diagnostic_string [dict get $target driver_inst] original_driver.inst] \
            ref [::sft::diagnostic_string [dict get $target driver_ref] original_driver.ref]]
        set local_cells [::sft::diagnostic_local_cells $target $current_driver]
        if {![llength $local_cells] ||
            [dict get [lindex $local_cells end] inst] ne [dict get $target driver_inst]} {
            ::sft::fail "diagnostic local_cells does not terminate at original driver for $endpoint"
        }
        dict set record local_cells $local_cells
        foreach cell $local_cells {
            lappend diagnostic_existing_instances [dict get $cell inst]
        }
        set slack [dict get $injection_violation_slacks $key]
        if {![string is double -strict $slack] || [regexp -nocase {nan|inf} $slack]} {
            ::sft::fail "diagnostic target slack is not finite for $mode/$endpoint: $slack"
        }
        dict set record slack_ns $slack
        lappend records $record
    }
    set records [lsort -command ::sft::compare_diagnostic_targets $records]
    set diagnostic_existing_instances [lsort -unique $diagnostic_existing_instances]
    if {![llength $records]} { ::sft::fail "diagnostic context has no targets" }
    set path [file join $::SFT_REPORT_DIR diagnostic_context.json]
    set stream [open $path w]
    puts $stream "{"
    puts $stream {  "schema_version": "timing_eco_diagnostic_context.v1",}
    puts $stream "  \"case_id\": [::sft::json_quote $::SFT_CASE(id)],"
    puts $stream "  \"design\": [::sft::json_quote $::SFT_BASE_TOP],"
    puts $stream "  \"max_eco_cells\": $::SFT_CASE(max_eco_cells),"
    puts $stream {  "targets": [}
    set last [expr {[llength $records] - 1}]
    for {set index 0} {$index <= $last} {incr index} {
        set record [lindex $records $index]
        set comma [expr {$index == $last ? "" : ","}]
        set original [dict get $record original_driver]
        puts $stream "    {\"role\": [::sft::json_quote [dict get $record role]], \"timing\": [::sft::json_quote [dict get $record timing]], \"endpoint\": [::sft::json_quote [dict get $record endpoint]], \"beginpoint\": [::sft::json_quote [dict get $record beginpoint]], \"slack_ns\": [dict get $record slack_ns], \"net\": [::sft::json_quote [dict get $record net]], \"driver_pin\": [::sft::json_quote [dict get $record driver_pin]], \"driver_inst\": [::sft::json_quote [dict get $record driver_inst]], \"driver_ref\": [::sft::json_quote [dict get $record driver_ref]], \"original_driver\": {\"inst\": [::sft::json_quote [dict get $original inst]], \"ref\": [::sft::json_quote [dict get $original ref]]}, \"local_cells\": [::sft::json_local_cells [dict get $record local_cells]]}$comma"
    }
    puts $stream "  \]"
    puts $stream "}"
    close $stream
}

proc ::sft::record_injection_trial {attempt parameter measurement} {
    variable injection_trials
    lappend injection_trials [dict merge [dict create attempt $attempt parameter $parameter] $measurement]
    puts "SFT_INJECTION_TRIAL $attempt $parameter setup_wns=[dict get $measurement setup_wns] hold_wns=[dict get $measurement hold_wns]"
}

proc ::sft::write_injection_provenance {status reason} {
    variable injection_trials
    set path [file join $::SFT_REPORT_DIR injection_provenance.json]
    set stream [open $path w]
    puts $stream "{"
    puts $stream "  \"schema_version\": \"timing_eco_injection_provenance.v1\","
    puts $stream "  \"case_id\": [::sft::json_quote $::SFT_CASE(id)],"
    puts $stream "  \"strategy\": [::sft::json_quote $::SFT_CASE(injection_strategy)],"
    puts $stream "  \"calibration_status\": [::sft::json_quote $::SFT_CASE(calibration_status)],"
    puts $stream "  \"status\": [::sft::json_quote $status],"
    puts $stream "  \"reason\": [::sft::json_quote $reason],"
    puts $stream "  \"baseline_binding\": {\"baseline_sha256\": [::sft::json_quote [::sft::provenance_sha256 SFT_BASELINE_SHA256]], \"catalog_sha256\": [::sft::json_quote [::sft::provenance_sha256 SFT_CATALOG_SHA256]], \"qualification_sha256\": [::sft::json_quote [::sft::provenance_sha256 SFT_BASELINE_QUALIFICATION_SHA256]], \"checksum_manifest_sha256\": [::sft::json_quote [::sft::provenance_sha256 SFT_BASELINE_CHECKSUMS_SHA256]], \"qualification_status\": [::sft::json_quote [::sft::provenance_environment SFT_BASELINE_STATUS]], \"technology_classification\": [::sft::json_quote [::sft::provenance_environment SFT_TECHNOLOGY_CLASSIFICATION]]},"
    puts $stream "  \"target_wns_ns\": {\"setup\": \[$::SFT_CASE(setup_wns_low), $::SFT_CASE(setup_wns_high)\], \"hold\": \[$::SFT_CASE(hold_wns_low), $::SFT_CASE(hold_wns_high)\]},"
    puts $stream "  \"attempts\": \["
    set last [expr {[llength $injection_trials] - 1}]
    for {set index 0} {$index <= $last} {incr index} {
        set trial [lindex $injection_trials $index]
        set comma [expr {$index == $last ? "" : ","}]
        puts $stream "    {\"attempt\": [dict get $trial attempt], \"parameter\": [::sft::json_quote [dict get $trial parameter]], \"setup_wns_ns\": [dict get $trial setup_wns], \"setup_tns_ns\": [dict get $trial setup_tns], \"hold_wns_ns\": [dict get $trial hold_wns], \"hold_tns_ns\": [dict get $trial hold_tns]}$comma"
    }
    puts $stream "  \]"
    puts $stream "}"
    close $stream
}

proc ::sft::apply_frozen_injection {strategy} {
    variable changes
    set change_start [llength $changes]
    switch -- $strategy {
        downsize_endpoint_driver {
            ::sft::inject_resize late down $::SFT_CASE(setup_drive_steps)
        }
        insert_data_delay {
            ::sft::inject_data_delay late $::SFT_CASE(setup_delay_cells)
        }
        upsize_endpoint_driver {
            ::sft::inject_resize early up $::SFT_CASE(hold_drive_steps)
        }
        local_capture_clock_delay {
            ::sft::inject_capture_skew early $::SFT_CASE(hold_clock_cell_count)
        }
        mixed_downsize_and_speedup {
            ::sft::inject_resize late down $::SFT_CASE(setup_drive_steps)
            ::sft::inject_resize early up $::SFT_CASE(hold_drive_steps)
        }
        mixed_data_delay_and_capture_skew {
            ::sft::inject_data_delay late $::SFT_CASE(setup_delay_cells)
            ::sft::inject_capture_skew early $::SFT_CASE(hold_clock_cell_count)
        }
        default { ::sft::fail "unsupported injection strategy $strategy" }
    }
    set applied [lrange $changes $change_start end]
    return [list strategy $strategy \
        setup [list drive_steps $::SFT_CASE(setup_drive_steps) delay_cells $::SFT_CASE(setup_delay_cells)] \
        hold [list drive_steps $::SFT_CASE(hold_drive_steps) clock_cell_count $::SFT_CASE(hold_clock_cell_count)] \
        actions $applied]
}

proc ::sft::calibration_only {} {
    return [expr {$::SFT_CASE(calibration_status) ne "FROZEN" &&
        [info exists ::env(SFT_ALLOW_UNFROZEN_CALIBRATION)] &&
        $::env(SFT_ALLOW_UNFROZEN_CALIBRATION) eq "1"}]
}

proc ::sft::apply_injection {} {
    variable injection_trials
    variable changes
    variable eco_count
    variable stage_metrics
    variable injection_pre_routed
    set injection_trials {}
    set changes {}
    set eco_count 0
    set injection_pre_routed 0
    unset -nocomplain stage_metrics(before,setup) stage_metrics(before,hold)
    set strategy $::SFT_CASE(injection_strategy)
    set parameter [::sft::apply_frozen_injection $strategy]
    if {!$injection_pre_routed} {
        ::sft::legalize_and_route
    }
    ::sft::validate_injected_repeater_chains
    set measurement [::sft::injection_measurement]
    # The remaining injection audit steps are read-only with explicit MMMC
    # views.  Cache this setup/hold summary for the immediately following
    # before stage, while retaining the full report_timing evidence files.
    set stage_metrics(before,setup) [list \
        [dict get $measurement setup_wns] [dict get $measurement setup_tns]]
    set stage_metrics(before,hold) [list \
        [dict get $measurement hold_wns] [dict get $measurement hold_tns]]
    ::sft::record_injection_trial 1 $parameter $measurement
    if {[catch {
        ::sft::write_violation_locality $measurement
        ::sft::write_diagnostic_context
    } scope_error]} {
        if {[::sft::calibration_only]} {
            set ::sft::calibration_scope_error $scope_error
            ::sft::write_injection_provenance calibration_candidate \
                "NOT_GOLD calibration scope/target observation failed: $scope_error"
            puts stderr "SFT_NOT_GOLD_CALIBRATION_SCOPE $scope_error"
            return
        } else {
            ::sft::write_injection_provenance failed $scope_error
            ::sft::fail $scope_error
        }
    }
    if {$::SFT_CASE(calibration_status) ne "FROZEN"} {
        if {[::sft::calibration_only]} {
            ::sft::write_injection_provenance calibration_candidate \
                "NOT_GOLD one-shot real-tool measurement; freeze parameters in catalog and rerun twice from fresh baselines"
            return
        }
        ::sft::write_injection_provenance failed \
            "PROBE_REQUIRED candidate was invoked without explicit NOT_GOLD calibration mode"
        ::sft::fail "injection candidate is PROBE_REQUIRED and cannot become Gold"
    }
    if {![::sft::measurement_in_target $measurement]} {
        ::sft::write_injection_provenance failed \
            "the single frozen injection did not hit every catalog WNS interval"
        ::sft::fail "frozen structural injection missed the catalog WNS interval"
    }
    ::sft::write_injection_provenance passed \
        "one frozen injection hit every target, preserved opposite timing headroom, and passed endpoint locality/coverage"
    puts "SFT_INJECTION_TARGET_VALIDATED attempts=1"
}

proc ::sft::write_calibration_marker {} {
    if {![::sft::calibration_only]} {
        ::sft::fail "write_calibration_marker is only valid for an unfrozen calibration replay"
    }
    set path [file join $::SFT_REPORT_DIR calibration_status.json]
    set stream [open $path w]
    puts $stream "{"
    puts $stream {  "schema_version": "timing_eco_calibration_status.v1",}
    puts $stream "  \"case_id\": [::sft::json_quote $::SFT_CASE(id)],"
    puts $stream {  "status": "NOT_GOLD_CALIBRATION_COMPLETE",}
    puts $stream {  "gold_eligible": false,}
    puts $stream {  "fresh_replays_required_after_freeze": 2,}
    if {[info exists ::sft::calibration_scope_error]} {
        puts $stream "  \"scope_validation\": \"failed\","
        puts $stream "  \"scope_reason\": [::sft::json_quote $::sft::calibration_scope_error],"
    } else {
        puts $stream "  \"scope_validation\": \"passed\","
    }
    puts $stream {  "passed": true}
    puts $stream "}"
    close $stream
    set marker [open [file join $::SFT_CASE_DIR NOT_GOLD_CALIBRATION] w]
    puts $marker "$::SFT_CASE(id) one-shot calibration completed; this is not a Gold replay"
    close $marker
}

proc ::sft::current_reference {instance} {
    set objects [::sft::items [get_cells -quiet [list $instance]]]
    if {[llength $objects] != 1} {
        ::sft::fail "cannot resolve instance $instance while generating concrete_fix.tcl"
    }
    set reference [::sft::property [lindex $objects 0] {ref_name base_name cell_name}]
    if {$reference eq ""} {
        ::sft::fail "cannot read current reference for $instance"
    }
    return [file tail [lindex $reference 0]]
}

proc ::sft::replacement_for_reference {reference direction steps} {
    if {$direction ni {up down}} {
        ::sft::fail "invalid resize direction $direction for $reference"
    }
    if {![string is integer -strict $steps] || $steps < 1} {
        ::sft::fail "resize steps must be a positive integer for $reference"
    }
    set parsed [::sft::parse_drive $reference]
    set variants [::sft::available_references $reference]
    if {[llength $parsed] != 4 || [llength $variants] < 2} {
        ::sft::fail "no drive-equivalent variants for $reference"
    }
    set current_drive [lindex $parsed 1]
    set exact_reference [file tail $reference]
    set reference_found 0
    set drive_levels {}
    foreach variant $variants {
        set drive [lindex $variant 0]
        if {[lindex $variant 1] eq $exact_reference} {
            set reference_found 1
        }
        if {![llength $drive_levels] || [lindex $drive_levels end] != $drive} {
            lappend drive_levels $drive
        }
    }
    if {!$reference_found} {
        ::sft::fail "$reference is absent from its drive-equivalent family"
    }
    set current_index -1
    for {set index 0} {$index < [llength $drive_levels]} {incr index} {
        if {[lindex $drive_levels $index] == $current_drive} {
            set current_index $index
            break
        }
    }
    if {$current_index < 0} {
        ::sft::fail "drive $current_drive for $reference is absent from its family levels"
    }
    set delta [expr {$direction eq "up" ? $steps : -$steps}]
    set new_index [expr {$current_index + $delta}]
    if {$new_index < 0 || $new_index >= [llength $drive_levels]} {
        ::sft::fail "cannot resize $reference exactly $direction by $steps drive level(s); available levels=$drive_levels"
    }
    set target_drive [lindex $drive_levels $new_index]
    set replacements {}
    foreach variant $variants {
        if {[lindex $variant 0] == $target_drive} {
            lappend replacements [lindex $variant 1]
        }
    }
    if {[llength $replacements] != 1} {
        ::sft::fail "ambiguous drive level $target_drive for $reference; references=$replacements"
    }
    return [lindex $replacements 0]
}

proc ::sft::validate_resize_transition {reference direction steps context} {
    set replacement [::sft::replacement_for_reference $reference $direction $steps]
    ::sft::validate_equivalent_cells $reference $replacement $context
    return $replacement
}

proc ::sft::injected_reference_for_target {target} {
    set original [dict get $target driver_ref]
    set requirement [::sft::injection_resize_requirement \
        $::SFT_CASE(injection_strategy) [dict get $target timing]]
    if {![llength $requirement]} {
        return $original
    }
    lassign $requirement direction steps
    return [::sft::validate_resize_transition $original $direction $steps \
        "resolved injection for [dict get $target endpoint]"]
}

proc ::sft::validate_planned_restore {target context} {
    set original [dict get $target driver_ref]
    set injected [::sft::injected_reference_for_target $target]
    if {$injected eq $original} {
        ::sft::fail "$context expects a resized injection on [dict get $target endpoint]"
    }
    ::sft::validate_equivalent_cells $injected $original $context
}

proc ::sft::validate_planned_resize {target direction steps context} {
    set original [dict get $target driver_ref]
    set injected [::sft::injected_reference_for_target $target]
    set replacement [::sft::replacement_for_reference $original $direction $steps]
    ::sft::validate_equivalent_cells $injected $replacement $context
    return $replacement
}

proc ::sft::required_timing_targets {mode context} {
    set selected [::sft::targets_for_timing $mode]
    if {![llength $selected]} {
        ::sft::fail "$context has no $mode target"
    }
    return $selected
}

proc ::sft::replace_delay_and_upsize_profile {} {
    variable equal_area_setup_buffer
    variable strong_setup_buffer
    variable paired_setup_inverter
    set count $::SFT_CASE(setup_delay_cells)
    if {![string is integer -strict $count] || $count < 1} {
        ::sft::fail "setup_delay_cells must be a positive integer for replace_delay_and_upsize"
    }
    if {$count >= 2} {
        if {[info exists ::SFT_FROZEN_DELAY_CELL] &&
            $::SFT_FROZEN_DELAY_CELL eq "DLY2_X4M_A9TR40"} {
            set object [::sft::lib_cell_object $equal_area_setup_buffer]
            if {[file tail [::sft::object_name $object]] ne $equal_area_setup_buffer} {
                ::sft::fail "cannot resolve exact equal-area setup buffer $equal_area_setup_buffer"
            }
            return [dict create driver_steps 0 buffer $equal_area_setup_buffer]
        }
        if {[::sft::uses_paired_inverter_repair]} {
            set object [::sft::lib_cell_object $paired_setup_inverter]
            if {[file tail [::sft::object_name $object]] ne $paired_setup_inverter} {
                ::sft::fail "cannot resolve exact paired setup inverter $paired_setup_inverter"
            }
            ::sft::assert_inverting_repeater $paired_setup_inverter \
                "SETUP_004 paired-footprint repair"
            return [dict create driver_steps 0 buffer $paired_setup_inverter]
        }
        set object [::sft::lib_cell_object $strong_setup_buffer]
        if {[file tail [::sft::object_name $object]] ne $strong_setup_buffer} {
            ::sft::fail "cannot resolve exact strong setup buffer $strong_setup_buffer"
        }
        return [dict create driver_steps 3 buffer $strong_setup_buffer]
    }
    return [dict create driver_steps 1 \
        buffer [::sft::first_existing_cell $::SFT_BUFFER_CELLS]]
}

proc ::sft::validate_resolved_action_feasibility {} {
    variable targets
    set resize_modes {}
    switch -- $::SFT_CASE(injection_strategy) {
        downsize_endpoint_driver { set resize_modes {late} }
        upsize_endpoint_driver { set resize_modes {early} }
        mixed_downsize_and_speedup { set resize_modes {late early} }
    }
    foreach mode $resize_modes {
        ::sft::required_timing_targets $mode \
            "injection strategy $::SFT_CASE(injection_strategy)"
    }
    foreach target $targets {
        set requirement [::sft::injection_resize_requirement \
            $::SFT_CASE(injection_strategy) [dict get $target timing]]
        if {[llength $requirement]} {
            lassign $requirement direction steps
            ::sft::validate_resize_transition [dict get $target driver_ref] \
                $direction $steps "resolved injection for [dict get $target endpoint]"
        }
    }

    set strategy $::SFT_CASE(repair_strategy)
    switch -- $strategy {
        upsize_endpoint_driver {
            foreach target [::sft::required_timing_targets late $strategy] {
                ::sft::validate_planned_restore $target "planned setup restore"
            }
        }
        upsize_driver_and_buffer {
            set late_targets [::sft::required_timing_targets late $strategy]
            ::sft::validate_planned_resize [lindex $late_targets 0] up 1 \
                "planned setup upsize"
            foreach target [lrange $late_targets 1 end] {
                ::sft::validate_planned_restore $target "planned setup restore"
            }
        }
        replace_delay_and_upsize {
            set profile [::sft::replace_delay_and_upsize_profile]
            set driver_steps [dict get $profile driver_steps]
            set late_targets [::sft::required_timing_targets late $strategy]
            if {$driver_steps > 0} {
                foreach target $late_targets {
                    ::sft::validate_planned_resize $target up $driver_steps \
                        "planned setup upsize"
                }
            }
        }
        insert_delay_and_downsize {
            foreach target [::sft::required_timing_targets early $strategy] {
                ::sft::validate_planned_resize $target down 1 "planned hold downsize"
            }
        }
        setup_then_hold {
            foreach target [::sft::required_timing_targets late $strategy] {
                ::sft::validate_planned_restore $target "planned mixed setup restore"
            }
        }
    }
    if {[::sft::calibration_only]} {
        puts "SFT_CALIBRATION_INJECTION_AND_REPAIR_ACTIONS_FEASIBLE targets=[llength $targets] strategy=$strategy"
        return
    }
    puts "SFT_RESOLVED_ACTIONS_FEASIBLE targets=[llength $targets] strategy=$strategy"
}

proc ::sft::original_reference {instance} {
    variable changes
    foreach change $changes {
        if {[dict get $change kind] eq "resize" && [dict get $change inst] eq $instance} {
            return [dict get $change from]
        }
    }
    return [::sft::current_reference $instance]
}

proc ::sft::target_timing_for_instance {instance} {
    variable targets
    foreach target $targets {
        if {[dict get $target driver_inst] eq $instance} {
            return [dict get $target timing]
        }
    }
    return ""
}

proc ::sft::append_repair_command {command operation} {
    variable repair_commands
    variable repair_operations
    lappend repair_commands $command
    if {$operation ne ""} {
        lappend repair_operations $operation
    }
}

proc ::sft::check_repair_budget {} {
    variable repair_operations
    set physical 0
    foreach operation $repair_operations {
        if {[dict get $operation kind] in {ecoChangeCell ecoAddRepeater}} {
            incr physical
        }
    }
    if {$physical >= $::SFT_CASE(max_eco_cells)} {
        ::sft::fail "concrete repair exceeds max_eco_cells=$::SFT_CASE(max_eco_cells)"
    }
}

proc ::sft::assert_diagnostic_instance_visible {instance reason} {
    variable diagnostic_existing_instances
    if {[lsearch -exact $diagnostic_existing_instances $instance] < 0} {
        ::sft::fail "$reason references existing instance $instance that is absent from diagnostic_context local_cells"
    }
}

proc ::sft::plan_change_cell {instance replacement reason} {
    ::sft::assert_diagnostic_instance_visible $instance $reason
    set current [::sft::current_reference $instance]
    if {$current eq $replacement} {
        ::sft::fail "$reason would leave $instance unchanged at $current"
    }
    ::sft::assert_equivalent_cells $current $replacement "$reason $instance"
    ::sft::check_repair_budget
    ::sft::append_repair_command [list ecoChangeCell -inst $instance -cell $replacement] \
        [dict create kind ecoChangeCell inst $instance from $current to $replacement reason $reason proof liberty_boolean_equivalent]
}

proc ::sft::plan_noninverting_change_cell {instance replacement reason} {
    ::sft::assert_diagnostic_instance_visible $instance $reason
    set current [::sft::current_reference $instance]
    ::sft::assert_pin_compatible_noninverting_replacement \
        $current $replacement "$reason $instance"
    ::sft::check_repair_budget
    ::sft::append_repair_command [list ecoChangeCell -inst $instance -cell $replacement] \
        [dict create kind ecoChangeCell inst $instance from $current to $replacement reason $reason proof both_cells_noninverting]
}

proc ::sft::plan_add_repeater {term reference tag reason} {
    variable eco_count
    ::sft::assert_noninverting_repeater $reference "$reason on $term"
    ::sft::check_repair_budget
    set name [::sft::next_eco_name $tag]
    set command [list ecoAddRepeater -term $term -cell $reference -name $name]
    set operation [dict create \
        kind ecoAddRepeater inst $name cell $reference term $term \
        reason $reason proof liberty_noninverting]
    if {[::sft::uses_hold002_fixed_repeater_location]} {
        if {$term ne "mac_out_nan_reg/D" ||
            $reference ne "DLY4_X0P5M_A9TR40" ||
            $tag ne "HOLD" || $reason ne "hold_data_delay"} {
            ::sft::fail "HOLD_002 fixed repeater location reached by an unexpected repair action"
        }
        set location {898.00 248.08}
        lappend command -loc $location
        dict set operation loc $location
    }
    if {[::sft::uses_hold003_fixed_repeater_locations]} {
        if {$term ne "pp_nan_mts_d2_reg_8_/D" ||
            $reference ne "DLY4_X0P5M_A9TR40" ||
            $tag ne "HOLD" || $reason ne "hold_data_delay"} {
            ::sft::fail "HOLD_003 fixed repeater locations reached by an unexpected repair action"
        }
        set locations [dict create \
            SFT_ECO_HOLD_003_HOLD_1 {914.15 263.20} \
            SFT_ECO_HOLD_003_HOLD_2 {915.86 263.20}]
        if {![dict exists $locations $name]} {
            ::sft::fail "HOLD_003 fixed repeater locations reached by unexpected ECO name $name"
        }
        set location [dict get $locations $name]
        lappend command -loc $location
        dict set operation loc $location
    }
    ::sft::append_repair_command $command $operation
}

proc ::sft::plan_restore_resizes {timing_filter} {
    variable changes
    set planned {}
    foreach change $changes {
        if {[dict get $change kind] ne "resize"} { continue }
        set instance [dict get $change inst]
        if {[lsearch -exact $planned $instance] >= 0} { continue }
        set timing [::sft::target_timing_for_instance $instance]
        if {$timing_filter eq "all" || $timing eq $timing_filter} {
            ::sft::plan_change_cell $instance [dict get $change from] restore_injected_resize
            lappend planned $instance
        }
    }
}

proc ::sft::plan_replace_injected_delays {buffer} {
    variable changes
    foreach change $changes {
        if {[dict get $change kind] eq "data_delay_injection"} {
            ::sft::plan_noninverting_change_cell [dict get $change inst] $buffer replace_injected_delay
        }
    }
}

proc ::sft::exclusive_injected_serial_pair {term instances} {
    set expected [lsort -unique $instances]
    if {[llength $instances] != 2 || [llength $expected] != 2} {
        ::sft::fail "paired-inverter repair requires exactly two unique injected cells at $term; got $instances"
    }
    set downstream [::sft::net_driver_instance $term]
    if {[lsearch -exact $expected $downstream] < 0} {
        ::sft::fail "paired-inverter repair cannot trace a selected downstream cell at $term; got $downstream expected=$expected"
    }
    set downstream_input [::sft::single_input_pin $downstream]
    set input_collection [::sft::pin_collection $downstream_input]
    set nets [::sft::items [get_nets -quiet -of_objects $input_collection]]
    if {[llength $nets] != 1} {
        ::sft::fail "paired-inverter repair cannot resolve one internal net at $downstream_input"
    }
    set internal_net [lindex $nets 0]
    set upstream [::sft::net_driver_instance $downstream_input]
    if {$upstream eq $downstream || [lsearch -exact $expected $upstream] < 0} {
        ::sft::fail "paired-inverter repair cannot trace the selected upstream cell at $term; got $upstream expected=$expected"
    }
    set pins [::sft::items [get_pins -quiet -of_objects $internal_net]]
    if {[llength $pins] != 2} {
        ::sft::fail "paired-inverter internal net [::sft::object_name $internal_net] has [llength $pins] pins; exclusive two-pin topology is required"
    }
    set output_pin ""
    set input_pin ""
    foreach pin $pins {
        set direction [string tolower [lindex [::sft::property $pin {direction pin_direction}] 0]]
        set cells [::sft::items [get_cells -quiet -of_objects $pin]]
        if {[llength $cells] != 1} {
            ::sft::fail "paired-inverter internal pin [::sft::object_name $pin] is not owned by one leaf cell"
        }
        set owner [::sft::object_name [lindex $cells 0]]
        if {$direction in {out output}} {
            if {$output_pin ne "" || $owner ne $upstream} {
                ::sft::fail "paired-inverter internal net has an unexpected output pin [::sft::object_name $pin]"
            }
            set output_pin [::sft::object_name $pin]
        } elseif {$direction in {in input}} {
            if {$input_pin ne "" || $owner ne $downstream ||
                [::sft::object_name $pin] ne $downstream_input} {
                ::sft::fail "paired-inverter internal net has an unexpected input pin [::sft::object_name $pin]"
            }
            set input_pin [::sft::object_name $pin]
        } else {
            ::sft::fail "paired-inverter internal net has unsupported pin direction $direction"
        }
    }
    if {$output_pin eq "" || $input_pin eq ""} {
        ::sft::fail "paired-inverter internal net lacks one upstream output and one downstream input"
    }
    if {[catch {get_ports -quiet -of_objects $internal_net} ports]} {
        ::sft::fail "cannot prove paired-inverter internal net is port-free: $ports"
    }
    if {[llength [::sft::items $ports]] != 0} {
        ::sft::fail "paired-inverter internal net [::sft::object_name $internal_net] reaches a top-level port"
    }
    return [dict create term $term upstream $upstream downstream $downstream \
        internal_net [::sft::object_name $internal_net] \
        output_pin $output_pin input_pin $input_pin]
}

proc ::sft::plan_paired_inverting_delay_replacements {inverter} {
    variable changes
    variable functional_proofs
    set by_term {}
    foreach change $changes {
        if {[dict get $change kind] eq "data_delay_injection"} {
            dict lappend by_term [dict get $change term] [dict get $change inst]
        }
    }
    set selected_terms {}
    foreach target [::sft::targets_for_timing late] {
        lappend selected_terms [dict get $target endpoint]
    }
    set selected_terms [lsort -unique $selected_terms]
    if {[lsort [dict keys $by_term]] ne $selected_terms} {
        ::sft::fail "paired-inverter repair target coverage differs from injected chains"
    }
    foreach term $selected_terms {
        set topology [::sft::exclusive_injected_serial_pair $term [dict get $by_term $term]]
        set upstream [dict get $topology upstream]
        set downstream [dict get $topology downstream]
        foreach instance [list $upstream $downstream] {
            ::sft::assert_diagnostic_instance_visible $instance replace_paired_injected_delay
            set current [::sft::current_reference $instance]
            if {$current ne $::SFT_FROZEN_DELAY_CELL} {
                ::sft::fail "paired-inverter repair expected $instance at $::SFT_FROZEN_DELAY_CELL; got $current"
            }
            set source [::sft::lib_function_evidence $current]
            set replacement [::sft::lib_function_evidence $inverter]
            ::sft::assert_noninverting_repeater $current \
                "paired-inverter source $instance"
            ::sft::assert_inverting_repeater $inverter \
                "paired-inverter replacement $instance"
            set source_outputs {}
            foreach output [dict get $source outputs] { lappend source_outputs [lindex $output 0] }
            set replacement_outputs {}
            foreach output [dict get $replacement outputs] { lappend replacement_outputs [lindex $output 0] }
            if {[dict get $source inputs] ne [dict get $replacement inputs] ||
                $source_outputs ne $replacement_outputs} {
                ::sft::fail "paired-inverter replacement changes the pin interface: $current -> $inverter"
            }
            ::sft::check_repair_budget
            set mate [expr {$instance eq $upstream ? $downstream : $upstream}]
            ::sft::append_repair_command \
                [list ecoChangeCell -inst $instance -cell $inverter] \
                [dict create kind ecoChangeCell inst $instance from $current to $inverter \
                    reason replace_paired_injected_delay proof paired_double_inversion \
                    pair_term $term pair_mate $mate]
        }
        lappend functional_proofs [dict create kind paired_double_inversion \
            term $term upstream $upstream downstream $downstream \
            internal_net [dict get $topology internal_net] inverter $inverter \
            composed_function noninverting exclusive_internal_net true]
    }
}

proc ::sft::validate_paired_inverter_functional_proofs {} {
    variable repair_operations
    variable functional_proofs
    variable paired_setup_inverter
    if {![::sft::uses_paired_inverter_repair]} {
        ::sft::fail "paired-inverter functional proof validation escaped SETUP_004"
    }

    set operations_by_term {}
    foreach operation $repair_operations {
        if {![dict exists $operation kind] ||
            [dict get $operation kind] ne "ecoChangeCell"} {
            ::sft::fail "paired-inverter repair contains a non-cell operation: $operation"
        }
        foreach field {inst from to reason proof pair_term pair_mate} {
            if {![dict exists $operation $field]} {
                ::sft::fail "paired-inverter operation lacks $field evidence: $operation"
            }
        }
        if {[dict get $operation from] ne $::SFT_FROZEN_DELAY_CELL ||
            [dict get $operation to] ne $paired_setup_inverter ||
            [dict get $operation reason] ne "replace_paired_injected_delay" ||
            [dict get $operation proof] ne "paired_double_inversion"} {
            ::sft::fail "paired-inverter operation is not the frozen proven swap: $operation"
        }
        set term [dict get $operation pair_term]
        if {$term eq ""} {
            ::sft::fail "paired-inverter operation has an empty pair_term: $operation"
        }
        dict lappend operations_by_term $term $operation
    }

    set expected_terms {}
    foreach target [::sft::targets_for_timing late] {
        lappend expected_terms [dict get $target endpoint]
    }
    set expected_terms [lsort -unique $expected_terms]
    if {![llength $expected_terms] ||
        [lsort [dict keys $operations_by_term]] ne $expected_terms} {
        ::sft::fail "paired-inverter operation terms differ from selected setup endpoints"
    }

    set proofs_by_term {}
    foreach proof $functional_proofs {
        if {![dict exists $proof kind] ||
            [dict get $proof kind] ne "paired_double_inversion"} {
            continue
        }
        foreach field {
            term upstream downstream internal_net inverter
            composed_function exclusive_internal_net
        } {
            if {![dict exists $proof $field]} {
                ::sft::fail "paired-inverter topology proof lacks $field evidence: $proof"
            }
        }
        if {[dict get $proof inverter] ne $paired_setup_inverter ||
            [dict get $proof composed_function] ne "noninverting" ||
            [dict get $proof exclusive_internal_net] ne "true" ||
            [dict get $proof internal_net] eq ""} {
            ::sft::fail "paired-inverter topology proof is incomplete: $proof"
        }
        dict lappend proofs_by_term [dict get $proof term] $proof
    }
    if {[lsort [dict keys $proofs_by_term]] ne $expected_terms} {
        ::sft::fail "paired-inverter topology proofs differ from selected setup endpoints"
    }

    foreach term $expected_terms {
        set operations [dict get $operations_by_term $term]
        set proofs [dict get $proofs_by_term $term]
        if {[llength $operations] != 2 || [llength $proofs] != 1} {
            ::sft::fail "paired-inverter endpoint $term requires two swaps and one topology proof"
        }
        set instances {}
        foreach operation $operations {
            lappend instances [dict get $operation inst]
        }
        set instances [lsort -unique $instances]
        if {[llength $instances] != 2} {
            ::sft::fail "paired-inverter endpoint $term does not have two unique instances"
        }
        foreach operation $operations {
            set instance [dict get $operation inst]
            set mate [dict get $operation pair_mate]
            if {$mate eq $instance || [lsort [list $instance $mate]] ne $instances} {
                ::sft::fail "paired-inverter mate binding is inconsistent at $term: $operation"
            }
        }
        set proof [lindex $proofs 0]
        set proof_instances [lsort -unique [list \
            [dict get $proof upstream] [dict get $proof downstream]]]
        if {$proof_instances ne $instances} {
            ::sft::fail "paired-inverter topology proof does not bind both swaps at $term"
        }
    }
}

proc ::sft::repair_delay_cell {} {
    set reference $::SFT_REPAIR_DELAY_CELL
    if {$::SFT_CASE(calibration_status) eq "FROZEN"} {
        if {$reference eq "" || $reference eq "PROBE_REQUIRED"} {
            ::sft::fail "frozen hold-delay repair has no exact cell reference"
        }
        if {![regexp {^[A-Za-z0-9_.]+$} $reference]} {
            ::sft::fail "frozen hold-delay repair cell reference is unsafe: $reference"
        }
        set object [::sft::lib_cell_object $reference]
        if {[file tail [::sft::object_name $object]] ne $reference} {
            ::sft::fail "frozen hold-delay repair resolved a non-exact Liberty cell for $reference"
        }
        puts "SFT_REPAIR_DELAY_CELL_VALIDATED $reference"
        return $reference
    }
    if {$reference ne ""} {
        if {$reference eq "PROBE_REQUIRED" ||
            ![regexp {^[A-Za-z0-9_.]+$} $reference]} {
            ::sft::fail "unfrozen hold-delay repair cell reference is not exact and safe: $reference"
        }
        set object [::sft::lib_cell_object $reference]
        if {[file tail [::sft::object_name $object]] ne $reference} {
            ::sft::fail "unfrozen hold-delay repair resolved a non-exact Liberty cell for $reference"
        }
        return $reference
    }
    set candidate [::sft::first_existing_cell $::SFT_DELAY_CELLS]
    puts stderr "SFT_PROBE_REQUIRED hold-delay repair is not case-frozen; candidate=$candidate choices=$::SFT_DELAY_CELLS"
    return $candidate
}

proc ::sft::plan_hold_delays {} {
    set count $::SFT_CASE(repair_delay_cells_per_endpoint)
    if {![string is integer -strict $count] || $count < 1} {
        ::sft::fail "hold-delay repair cells_per_endpoint must be a positive integer"
    }
    set delay [::sft::repair_delay_cell]
    foreach target [::sft::targets_for_timing early] {
        for {set index 0} {$index < $count} {incr index} {
            ::sft::plan_add_repeater [dict get $target endpoint] $delay HOLD hold_data_delay
        }
    }
}

proc ::sft::plan_resize_from_original {instance direction steps reason} {
    ::sft::assert_diagnostic_instance_visible $instance $reason
    set original [::sft::original_reference $instance]
    set replacement [::sft::replacement_for_reference $original $direction $steps]
    set current [::sft::current_reference $instance]
    if {$current eq $replacement} {
        ::sft::fail "$reason selected the already-instantiated reference $replacement"
    }
    ::sft::assert_equivalent_cells $current $replacement "$reason $instance"
    ::sft::check_repair_budget
    ::sft::append_repair_command [list ecoChangeCell -inst $instance -cell $replacement] \
        [dict create kind ecoChangeCell inst $instance from $current to $replacement reason $reason proof liberty_boolean_equivalent]
}

proc ::sft::cell_reference_snapshot {} {
    set snapshot {}
    if {[catch {get_cells -hierarchical *} cells]} {
        ::sft::fail "cannot enumerate cells for native ECO budget: $cells"
    }
    foreach cell [::sft::items $cells] {
        set reference [::sft::property $cell {ref_name base_name cell_name}]
        if {$reference eq ""} { continue }
        dict set snapshot [::sft::object_name $cell] [file tail [lindex $reference 0]]
    }
    if {![dict size $snapshot]} {
        ::sft::fail "native ECO budget snapshot contains no leaf cells"
    }
    return $snapshot
}

proc ::sft::native_selected_fanin_cells {} {
    set allowed {}
    set mode [expr {$::SFT_CASE(type) eq "setup" ? "late" : "early"}]
    foreach target [::sft::targets_for_timing $mode] {
        set endpoint [dict get $target endpoint]
        set pin [::sft::pin_collection $endpoint]
        if {[catch {all_fanin -to $pin -only_cells} cone]} {
            puts stderr "SFT_PROBE_REQUIRED verify Innovus 21.10 all_fanin -to <pin> -only_cells for native changed-cell locality"
            ::sft::fail "cannot prove native changed-cell locality for $endpoint: $cone"
        }
        foreach cell [::sft::items $cone] {
            lappend allowed [::sft::object_name $cell]
        }
        foreach cell [::sft::items [get_cells -quiet -of_objects $pin]] {
            lappend allowed [::sft::object_name $cell]
        }
    }
    set allowed [lsort -unique $allowed]
    if {![llength $allowed]} {
        ::sft::fail "native selectedTerms fanin audit produced an empty cell set"
    }
    return $allowed
}

proc ::sft::validate_native_cell_budget {} {
    variable native_before_cells
    variable native_allowed_before
    variable native_changed_cells
    if {$::SFT_CASE(repair_mode) ne "native"} { return }
    set after [::sft::cell_reference_snapshot]
    set changed {}
    foreach name [lsort -unique [concat [dict keys $native_before_cells] [dict keys $after]]] {
        set before_ref "<absent>"
        set after_ref "<absent>"
        if {[dict exists $native_before_cells $name]} { set before_ref [dict get $native_before_cells $name] }
        if {[dict exists $after $name]} { set after_ref [dict get $after $name] }
        if {$before_ref ne $after_ref} {
            lappend changed [list $name $before_ref $after_ref]
        }
    }
    set native_changed_cells [llength $changed]
    set path [file join $::SFT_REPORT_DIR native_cell_diff.tcl]
    set stream [open $path w]
    puts $stream "# Exact leaf-cell reference diff across native selectedTerms optimization."
    puts $stream [list set ::SFT_NATIVE_CELL_DIFF $changed]
    close $stream
    if {$native_changed_cells > $::SFT_CASE(max_eco_cells)} {
        ::sft::fail "native optDesign changed $native_changed_cells cells; budget is $::SFT_CASE(max_eco_cells)"
    }
    set allowed_after [::sft::native_selected_fanin_cells]
    set allowed [lsort -unique [concat $native_allowed_before $allowed_after]]
    set outside {}
    foreach item $changed {
        set name [lindex $item 0]
        if {[lsearch -exact $allowed $name] < 0} { lappend outside $name }
    }
    if {[llength $outside]} {
        ::sft::fail "native selectedTerms changed cells outside selected endpoint fanin: [lsort -unique $outside]"
    }
    puts "SFT_NATIVE_CELL_BUDGET_VALIDATED changed=$native_changed_cells limit=$::SFT_CASE(max_eco_cells) locality=selected_fanin_before_after_union"
}

proc ::sft::plan_native_selected_terms {mode} {
    variable native_before_cells
    variable native_allowed_before
    set endpoints {}
    # Match diagnostic_context.json ordering exactly so the standalone term
    # file is byte-bindable to the repair-facing endpoint evidence.
    set selected_targets [lsort -command ::sft::compare_diagnostic_targets \
        [::sft::targets_for_timing $mode]]
    foreach target $selected_targets {
        lappend endpoints [dict get $target endpoint]
    }
    if {![llength $endpoints] || [llength $endpoints] != [llength [lsort -unique $endpoints]]} {
        ::sft::fail "native selectedTerms endpoints must be nonempty and unique: $endpoints"
    }
    set relative_path [file join reports native_selected_terms.txt]
    set path [file join $::SFT_CASE_DIR $relative_path]
    set expected_text [join $endpoints "\n"]
    append expected_text "\n"
    set stream [open $path w]
    puts -nonewline $stream $expected_text
    close $stream
    if {[::sft::read_file $path] ne $expected_text} {
        ::sft::fail "native selectedTerms file differs from resolved endpoint list: $path"
    }
    set native_allowed_before [::sft::native_selected_fanin_cells]
    set native_before_cells [::sft::cell_reference_snapshot]
    if {$mode eq "late"} {
        ::sft::append_repair_command [list optDesign -postRoute -setup -selectedTerms $relative_path -incr] \
            [dict create kind nativeOpt mode setup scope explicit_selected_terms_file terms_file $relative_path proof innovus_timing_optimizer]
    } else {
        ::sft::append_repair_command [list optDesign -postRoute -hold -selectedTerms $relative_path -incr] \
            [dict create kind nativeOpt mode hold scope explicit_selected_terms_file terms_file $relative_path proof innovus_timing_optimizer]
    }
    puts "SFT_NATIVE_SELECTED_TERMS_WRITTEN $path count=[llength $endpoints]"
}

proc ::sft::write_concrete_fix {} {
    variable repair_commands
    variable repair_operations
    variable eco_count
    set repair_commands {}
    set repair_operations {}
    # Repair names and limits are independent of hidden injection names.
    set eco_count 0
    set strategy $::SFT_CASE(repair_strategy)
    switch -- $strategy {
        upsize_endpoint_driver {
            ::sft::plan_restore_resizes late
        }
        upsize_driver_and_buffer {
            set late_targets [::sft::targets_for_timing late]
            set first [lindex $late_targets 0]
            ::sft::plan_resize_from_original [dict get $first driver_inst] up 1 setup_driver_upsize
            foreach target [lrange $late_targets 1 end] {
                set instance [dict get $target driver_inst]
                ::sft::plan_change_cell $instance [::sft::original_reference $instance] restore_injected_resize
            }
            set buffered [lindex $late_targets end]
            set buffer [::sft::first_existing_cell $::SFT_BUFFER_CELLS]
            ::sft::plan_add_repeater [dict get $buffered endpoint] $buffer SETUP setup_data_buffer
        }
        replace_delay_and_upsize {
            set profile [::sft::replace_delay_and_upsize_profile]
            if {[::sft::uses_paired_inverter_repair]} {
                ::sft::plan_paired_inverting_delay_replacements \
                    [dict get $profile buffer]
            } else {
                ::sft::plan_replace_injected_delays [dict get $profile buffer]
            }
            set driver_steps [dict get $profile driver_steps]
            if {$driver_steps > 0} {
                foreach target [::sft::targets_for_timing late] {
                    ::sft::plan_resize_from_original [dict get $target driver_inst] \
                        up $driver_steps setup_driver_upsize
                }
            }
        }
        native_selected_terms_setup { ::sft::plan_native_selected_terms late }
        insert_data_delay { ::sft::plan_hold_delays }
        insert_delay_and_downsize {
            ::sft::plan_hold_delays
            foreach target [::sft::targets_for_timing early] {
                ::sft::plan_resize_from_original [dict get $target driver_inst] down 1 hold_driver_downsize
            }
        }
        native_selected_terms_hold { ::sft::plan_native_selected_terms early }
        setup_then_hold {
            ::sft::plan_restore_resizes late
            ::sft::plan_hold_delays
        }
        coordinated_setup_hold {
            ::sft::plan_replace_injected_delays \
                [::sft::coordinated_restore_buffer]
            ::sft::plan_hold_delays
        }
        default { ::sft::fail "unsupported repair strategy $strategy" }
    }
    if {![llength $repair_operations]} {
        ::sft::fail "repair strategy $strategy generated no concrete operations"
    }
    if {[::sft::uses_paired_inverter_repair]} {
        # Validate the pairwise functional evidence before emitting a script
        # that temporarily disables Innovus' per-cell LEQ check.
        ::sft::validate_paired_inverter_functional_proofs
    }
    if {[::sft::uses_hold002_fixed_repeater_location]} {
        ::sft::validate_hold002_fixed_repeater_target
    }
    if {[::sft::uses_hold003_fixed_repeater_locations]} {
        ::sft::validate_hold003_fixed_repeater_target
    }
    if {[::sft::uses_mixed001_strong_restore_buffer]} {
        ::sft::validate_mixed001_strong_restore_targets
    }
    set path [file join $::SFT_REPORT_DIR concrete_fix.tcl]
    set stream [open $path w]
    puts $stream "# Concrete Gold timing ECO for $::SFT_CASE(id)."
    puts $stream "# Deterministically generated from reports/resolved_targets.tcl; no constraints are modified."
    if {$::SFT_CASE(repair_mode) eq "surgical"} {
        puts $stream [list setEcoMode -batchMode true]
        if {[::sft::uses_paired_inverter_repair]} {
            # Innovus LEQ is intentionally per-cell and cannot recognize that
            # two exclusive serial inversions compose to the original buffer
            # function.  Disable it only inside this topology-proven pair
            # window; the concrete-fix policy requires immediate restoration.
            puts $stream [list setEcoMode -LEQCheck false]
        }
    }
    foreach command $repair_commands {
        puts $stream $command
    }
    # Commit surgical netlist edits before placement and routing.  Innovus
    # 21.10 documents -batchMode as an Enter/Exit boundary for ecoChangeCell
    # and ecoAddRepeater; native optDesign must execute outside that batch.
    if {$::SFT_CASE(repair_mode) eq "surgical"} {
        puts $stream [list setEcoMode -batchMode false]
        if {[::sft::uses_paired_inverter_repair]} {
            puts $stream [list setEcoMode -LEQCheck true]
        }
    }
    puts $stream [list refinePlace -eco true]
    # Every case uses the ordinary timing-driven dirty-net closeout.  The
    # fingerprint-bound HOLD_002 physical exception is encoded on its
    # ecoAddRepeater -loc option, not by changing a global router mode.
    puts $stream [list ecoRoute -target]
    if {[::sft::uses_paired_inverter_repair]} {
        # A real Innovus probe proved that the original timing-driven target
        # route followed by one non-timing-driven local DRC repair preserves
        # every full-chip DRC category.  Bind the exact repair box to the
        # fully fingerprinted SETUP_004 exception and restore the route mode
        # immediately afterward.
        puts $stream [list setNanoRouteMode -routeWithTimingDriven false]
        puts $stream [list ecoRoute -fix_drc {907.30 263.40 914.90 270.50}]
        puts $stream [list setNanoRouteMode -routeWithTimingDriven true]
    }
    close $stream
    puts "SFT_CONCRETE_FIX_WRITTEN $path operations=[llength $repair_operations]"
    return $path
}

proc ::sft::legalize_and_route {} {
    if {[catch {setEcoMode -batchMode false} message]} {
        ::sft::fail "cannot exit ECO batch mode before legalization: $message"
    }
    if {[catch {refinePlace -eco true} message]} {
        ::sft::fail "incremental placement failed: $message"
    }
    # Use the same dirty-net-only routing policy for violation injection and
    # repair.  This keeps the before/after physical-quality comparison
    # symmetric and prevents unrelated full-chip DRC category churn.
    if {[catch {ecoRoute -target} message]} {
        ::sft::fail "ECO routing failed: $message"
    }
}

proc ::sft::command_to_file {path command} {
    file mkdir [file dirname $path]
    if {[catch {uplevel #0 [concat $command [list > $path]]} message]} {
        ::sft::fail "report command failed for $path: $message"
    }
}

proc ::sft::timing_summary {mode} {
    set violating [::sft::violating_timing_paths $mode 10000]
    if {![llength $violating]} {
        set paths [::sft::timing_paths $mode 1]
        if {![llength $paths]} {
            ::sft::fail "no timing paths were returned for $mode"
        }
        set worst [::sft::path_field [lindex $paths 0] slack]
        if {$worst eq ""} {
            ::sft::fail "worst timing path has no readable slack for $mode"
        }
        return [list $worst 0.0]
    }
    if {[llength $violating] >= 10000} {
        ::sft::fail "$mode TNS collection is truncated at 10000 violating paths"
    }
    set worst 1.0e30
    set total 0.0
    set seen {}
    foreach path $violating {
        set endpoint [::sft::path_field $path endpoint]
        set slack [::sft::path_field $path slack]
        if {$endpoint eq "" || $slack eq "" || [lsearch -exact $seen $endpoint] >= 0} {
            continue
        }
        lappend seen $endpoint
        if {$slack < $worst} { set worst $slack }
        if {$slack < 0.0} { set total [expr {$total + $slack}] }
    }
    return [list $worst $total]
}

proc ::sft::parse_drv_counts {path} {
    set wanted {max_transition max_capacitance max_fanout}
    set seen {}
    set rows {}
    set pipe_rows {}
    set pipe_headers {}
    set verbose {}
    set zero {}
    foreach check $wanted {
        dict set rows $check 0
        dict set pipe_rows $check 0
        dict set pipe_headers $check 0
        dict set verbose $check 0
        dict set zero $check 0
    }
    set current ""
    foreach line [split [::sft::read_file $path] "\n"] {
        if {[regexp -nocase {^\s*Check\s+type\s*:\s*([a-z_]+)\s*$} $line -> heading]} {
            set heading [string tolower $heading]
            if {[lsearch -exact $wanted $heading] >= 0} {
                if {[dict exists $seen $heading]} {
                    ::sft::fail "duplicate DRV section $heading in $path"
                }
                dict set seen $heading 1
                set current $heading
            } else {
                set current ""
            }
            continue
        }
        if {$current eq ""} { continue }
        # Innovus 21.10 renders report_constraint -all_violators as a
        # pipe-delimited table.  Require its canonical five-column header so
        # an unrelated numeric pipe row cannot silently become DRV evidence.
        if {[regexp -nocase {^\s*\|\s*Pin\s+Name\s*\|\s*Required\s*\|\s*Actual\s*\|\s*Slack\s*\|\s*View\s*\|\s*$} $line]} {
            dict incr pipe_headers $current
            continue
        }
        if {[regexp -nocase {^\s*\|\s*[^|[:space:]][^|]*\|\s*[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?\s*\|\s*[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?\s*\|\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*\|\s*[^|[:space:]][^|]*\|\s*$} $line -> slack]} {
            if {[dict get $pipe_headers $current] != 1} {
                ::sft::fail "$current pipe-table DRV row appears without exactly one preceding canonical header"
            }
            if {$slack >= -1.0e-12} {
                ::sft::fail "$current pipe-table DRV violator row has non-negative slack $slack"
            }
            dict incr pipe_rows $current
            continue
        }
        if {[regexp -nocase {^\s*\S+(?:\s+[rf])?\s+[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?\s+[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?\s+([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*$} $line -> slack]} {
            if {[regexp {^\s*[-+|]} $line]} { continue }
            if {$slack >= -1.0e-12} {
                ::sft::fail "$current DRV violator row has non-negative slack $slack"
            }
            dict incr rows $current
        }
        if {[regexp -nocase {slack\s*:\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*\(\s*VIOLATED\s*\)} $line -> slack]} {
            if {$slack >= -1.0e-12} {
                ::sft::fail "$current verbose DRV row has non-negative slack $slack"
            }
            dict incr verbose $current
        }
        if {[regexp -nocase {No\s+(?:paths?|violations?)\s+(?:were\s+)?found|No\s+\w*\s*violations?} $line]} {
            dict set zero $current 1
        }
    }
    set counts {}
    foreach check $wanted {
        if {![dict exists $seen $check]} {
            puts stderr "SFT_PROBE_REQUIRED report_constraint -all_violators lacks a uniquely parseable $check section"
            ::sft::fail "DRV evidence is incomplete for $check in $path"
        }
        set table_count [dict get $rows $check]
        set pipe_count [dict get $pipe_rows $check]
        set pipe_header_count [dict get $pipe_headers $check]
        set verbose_count [dict get $verbose $check]
        set zero_marker [dict get $zero $check]
        if {$pipe_header_count > 1} {
            ::sft::fail "$check DRV section has duplicate pipe-table headers"
        }
        if {$pipe_count && $pipe_header_count != 1} {
            ::sft::fail "$check pipe-table DRV rows lack exactly one canonical header"
        }
        if {$table_count && ($pipe_count || $pipe_header_count)} {
            ::sft::fail "$check DRV section ambiguously mixes whitespace and pipe tables"
        }
        if {$pipe_count} {
            set table_count $pipe_count
        }
        if {$zero_marker && ($table_count || $verbose_count)} {
            ::sft::fail "$check DRV section contains both violator rows and a zero marker"
        }
        if {$table_count && $verbose_count && $table_count != $verbose_count} {
            ::sft::fail "$check DRV count is ambiguous: table=$table_count verbose=$verbose_count"
        }
        if {$table_count} {
            dict set counts $check $table_count
        } elseif {$verbose_count} {
            dict set counts $check $verbose_count
        } elseif {$zero_marker} {
            dict set counts $check 0
        } else {
            puts stderr "SFT_PROBE_REQUIRED parse Innovus 21.10 $check report_constraint section"
            ::sft::fail "$check DRV section has neither rows nor an explicit zero marker"
        }
    }
    return $counts
}

proc ::sft::parse_drc_counts {path} {
    variable drc_report_limit
    set text [::sft::read_file $path]
    set command_headers {}
    set total ""
    set categories {}
    foreach line [split $text "\n"] {
        if {[regexp -nocase {^\s*#\s*Command:\s*(.+)$} $line -> value]} {
            lappend command_headers [string trim $value]
        }
        if {[regexp -nocase {^\s*Total\s+Violations\s*:\s*([0-9]+)\s+Viols?\.\s*$} $line -> value]} {
            if {$total ne ""} { ::sft::fail "duplicate DRC total in $path" }
            set total $value
        }
        if {[regexp {^\s*([A-Z][A-Z0-9_.-]*):\s+\(} $line -> category]} {
            dict incr categories $category
        }
    }
    if {[llength $command_headers] != 1} {
        ::sft::fail "DRC evidence must contain exactly one Innovus Command header in $path"
    }
    set command [lindex $command_headers 0]
    if {![regexp -nocase {(^|[[:space:]])verify_drc([[:space:]]|$)} $command]} {
        ::sft::fail "DRC evidence Command header is not verify_drc in $path"
    }
    if {[catch {set command_words [lrange $command 0 end]}]} {
        ::sft::fail "DRC evidence has a malformed Command header in $path"
    }
    set command_limits {}
    for {set index 0} {$index < [llength $command_words]} {incr index} {
        if {[string equal -nocase [lindex $command_words $index] -limit]} {
            if {$index + 1 >= [llength $command_words]} {
                ::sft::fail "verify_drc Command has no value after -limit in $path"
            }
            lappend command_limits [lindex $command_words [expr {$index + 1}]]
        }
    }
    if {[llength $command_limits] != 1 ||
        ![string is integer -strict [lindex $command_limits 0]] ||
        [lindex $command_limits 0] != $drc_report_limit} {
        ::sft::fail "verify_drc Command must contain exactly one -limit $drc_report_limit in $path"
    }
    set truncation_patterns {
        {truncat[a-z]*}
        {(limit|maximum).{0,96}(reach[a-z]*|exceed[a-z]*|hit|stopp?[a-z]*|terminat[a-z]*)}
        {(reach[a-z]*|exceed[a-z]*|hit).{0,96}(limit|maximum)}
        {(stopp?[a-z]*|terminat[a-z]*).{0,96}(errors?|violations?)}
        {(only|first)\s+[0-9]+\s+(errors?|violations?).{0,96}(report[a-z]*|show[a-z]*|list[a-z]*)}
    }
    foreach line [split $text "\n"] {
        if {[regexp {^\s*#} $line]} { continue }
        if {[regexp -nocase {not.{0,24}(reach[a-z]*|exceed[a-z]*)} $line]} { continue }
        foreach pattern $truncation_patterns {
            if {[regexp -nocase $pattern $line]} {
                ::sft::fail "DRC report contains an early-termination/truncation signal in $path: [string trim $line]"
            }
        }
    }
    if {$total eq ""} {
        puts stderr "SFT_PROBE_REQUIRED verify Innovus 21.10 verify_drc typed total/category format"
        ::sft::fail "DRC evidence lacks a unique Total Violations marker in $path"
    }
    if {$total >= $drc_report_limit} {
        ::sft::fail "DRC total $total reaches collection limit $drc_report_limit in $path; completeness is unproven"
    }
    set listed 0
    dict for {category count} $categories { incr listed $count }
    if {$listed != $total} {
        ::sft::fail "DRC category records do not reconcile to total in $path: categories=$listed total=$total"
    }
    return [dict create total $total categories $categories]
}

proc ::sft::json_number_object {values} {
    set result "{"
    set separator ""
    foreach key [lsort [dict keys $values]] {
        append result $separator [::sft::json_quote $key] ": " [dict get $values $key]
        set separator ", "
    }
    append result "}"
    return $result
}

proc ::sft::validate_physical_no_regression {} {
    variable stage_drv
    variable stage_drc
    foreach stage {before after} {
        if {![info exists stage_drv($stage)] || ![info exists stage_drc($stage)]} {
            ::sft::fail "physical no-regression audit is missing $stage evidence"
        }
    }
    foreach check {max_transition max_capacitance max_fanout} {
        set before [dict get $stage_drv(before) $check]
        set after [dict get $stage_drv(after) $check]
        if {$after > $before} {
            ::sft::fail "$check DRV regressed: before=$before after=$after"
        }
    }
    set before_categories [dict get $stage_drc(before) categories]
    set after_categories [dict get $stage_drc(after) categories]
    foreach category [lsort -unique [concat [dict keys $before_categories] [dict keys $after_categories]]] {
        set before 0
        set after 0
        if {[dict exists $before_categories $category]} { set before [dict get $before_categories $category] }
        if {[dict exists $after_categories $category]} { set after [dict get $after_categories $category] }
        if {$after > $before} {
            ::sft::fail "DRC category $category regressed: before=$before after=$after"
        }
    }
    if {[dict get $stage_drc(after) total] > [dict get $stage_drc(before) total]} {
        ::sft::fail "total DRC regressed"
    }
    set path [file join $::SFT_REPORT_DIR physical_no_regression.json]
    set stream [open $path w]
    puts $stream "{"
    puts $stream {  "schema_version": "timing_eco_physical_no_regression.v1",}
    puts $stream "  \"case_id\": [::sft::json_quote $::SFT_CASE(id)],"
    puts $stream "  \"drv_before\": [::sft::json_number_object $stage_drv(before)],"
    puts $stream "  \"drv_after\": [::sft::json_number_object $stage_drv(after)],"
    puts $stream "  \"drc_before\": {\"total\": [dict get $stage_drc(before) total], \"categories\": [::sft::json_number_object $before_categories]},"
    puts $stream "  \"drc_after\": {\"total\": [dict get $stage_drc(after) total], \"categories\": [::sft::json_number_object $after_categories]},"
    puts $stream {  "per_category_no_regression": true,}
    puts $stream {  "passed": true}
    puts $stream "}"
    close $stream
}

proc ::sft::validated_timing_metric_pair {value label} {
    if {[llength $value] != 2} {
        ::sft::fail "$label timing metric cache must contain exactly WNS and TNS"
    }
    lassign $value wns tns
    foreach field {wns tns} number [list $wns $tns] {
        if {![string is double -strict $number] ||
            [regexp -nocase {nan|inf} $number]} {
            ::sft::fail "$label cached $field is not finite: $number"
        }
    }
    return [list $wns $tns]
}

proc ::sft::update_stage_timing_metrics {stage} {
    variable stage_metrics
    if {$stage ni {before after}} {
        ::sft::fail "unsupported timing report stage $stage"
    }
    if {$stage eq "before"} {
        set setup_cached [info exists stage_metrics(before,setup)]
        set hold_cached [info exists stage_metrics(before,hold)]
        if {$setup_cached != $hold_cached} {
            ::sft::fail "before timing metric cache is incomplete"
        }
        if {$setup_cached} {
            foreach check {setup hold} {
                set stage_metrics(before,$check) [::sft::validated_timing_metric_pair \
                    $stage_metrics(before,$check) "before $check"]
            }
            return
        }
    }
    # The after stage is always measured from the repaired database.  Even a
    # pre-existing array value must be overwritten rather than reused.
    set stage_metrics($stage,setup) [::sft::validated_timing_metric_pair \
        [::sft::timing_summary late] "$stage setup"]
    set stage_metrics($stage,hold) [::sft::validated_timing_metric_pair \
        [::sft::timing_summary early] "$stage hold"]
}

proc ::sft::write_stage_reports {stage} {
    variable stage_metrics
    variable stage_drv
    variable stage_drc
    set setup_path [file join $::SFT_REPORT_DIR setup_${stage}.rpt]
    set hold_path [file join $::SFT_REPORT_DIR hold_${stage}.rpt]
    set setup_command [list report_timing -late -max_paths 200 -view $::SFT_SETUP_VIEW]
    set hold_command [list report_timing -early -max_paths 200 -view $::SFT_HOLD_VIEW]
    ::sft::command_to_file $setup_path $setup_command
    ::sft::command_to_file $hold_path $hold_command
    # Full timing reports above remain mandatory evidence.  Only the duplicate
    # collection query used to derive the before WNS/TNS pair may be reused.
    ::sft::update_stage_timing_metrics $stage

    set drv_path [file join $::SFT_REPORT_DIR drv_${stage}.rpt]
    ::sft::command_to_file $drv_path [list report_constraint -all_violators]
    set stage_drv($stage) [::sft::parse_drv_counts $drv_path]
    set connectivity_path [file join $::SFT_REPORT_DIR connectivity_${stage}.rpt]
    if {[catch {verifyConnectivity -type all -report $connectivity_path} message]} {
        ::sft::fail "verifyConnectivity failed: $message"
    }
    if {![::sft::nonempty_file $connectivity_path]} {
        ::sft::fail "verifyConnectivity produced no report for $stage"
    }
    if {![::sft::connectivity_report_is_clean $connectivity_path]} {
        ::sft::fail "$stage connectivity report is not explicitly clean: $connectivity_path"
    }
    set drc_path [file join $::SFT_REPORT_DIR drc_${stage}.rpt]
    if {[catch {verify_drc -limit 1000000 -report $drc_path} message]} {
        ::sft::fail "verify_drc failed: $message"
    }
    set stage_drc($stage) [::sft::parse_drc_counts $drc_path]
}

proc ::sft::nonempty_file {path} {
    return [expr {[file isfile $path] && [file size $path] > 0}]
}

proc ::sft::nonempty_tree {path} {
    if {![file isdirectory $path]} { return 0 }
    foreach candidate [glob -nocomplain -types f -directory $path *] {
        if {[file size $candidate] > 0} { return 1 }
    }
    foreach directory [glob -nocomplain -types d -directory $path *] {
        if {[::sft::nonempty_tree $directory]} { return 1 }
    }
    return 0
}

proc ::sft::read_file {path {binary false}} {
    if {![file isfile $path]} {
        ::sft::fail "missing evidence file $path"
    }
    set stream [open $path r]
    if {$binary} {
        fconfigure $stream -translation binary -encoding binary
    }
    set data [read $stream]
    close $stream
    return $data
}

# Innovus write_sdc stamps every export with one volatile generation-time
# comment.  Normalize exactly that known header line before any byte-level
# constraint comparison; every other byte, including line endings, remains
# evidence.  Missing or duplicate headers are an unknown format and therefore
# fail closed instead of broadening the comparison.
proc ::sft::normalize_write_sdc_generated_on {path} {
    set data [::sft::read_file $path true]
    set normalized_lines {}
    set header_count 0
    foreach raw_line [split $data "\n"] {
        set line $raw_line
        set line_ending ""
        if {[string length $line] > 0 && [string index $line end] eq "\r"} {
            set line [string range $line 0 end-1]
            set line_ending "\r"
        }
        if {[regexp {^#  Generated on:[ \t]+[^ \t\r][^\r]*$} $line]} {
            incr header_count
            set line {#  Generated on:      SFT_NORMALIZED_VOLATILE_METADATA}
        }
        lappend normalized_lines "${line}${line_ending}"
    }
    if {$header_count != 1} {
        ::sft::fail "write_sdc snapshot $path has $header_count exact '#  Generated on:' headers; expected exactly one"
    }
    set stream [open $path w]
    fconfigure $stream -translation binary -encoding binary
    puts -nonewline $stream [join $normalized_lines "\n"]
    close $stream
}

proc ::sft::write_constraint_snapshot {stage} {
    foreach check {setup hold} view [list $::SFT_SETUP_VIEW $::SFT_HOLD_VIEW] {
        set path [file join $::SFT_REPORT_DIR constraint_${check}_${stage}.sdc]
        if {[catch {write_sdc -view $view $path} message]} {
            puts stderr "SFT_PROBE_REQUIRED verify Innovus 21.10 write_sdc -view <analysis_view> <file> syntax"
            ::sft::fail "write_sdc failed for $check/$stage: $message"
        }
        if {![::sft::nonempty_file $path]} {
            ::sft::fail "write_sdc produced an empty $check/$stage constraint snapshot"
        }
        ::sft::normalize_write_sdc_generated_on $path
    }
    # Compatibility alias only. Gold evidence and PT bind the two mode-specific
    # files above; this alias is never used to claim cross-view equality.
    set legacy [file join $::SFT_REPORT_DIR constraint_${stage}.sdc]
    file copy -- [file join $::SFT_REPORT_DIR constraint_setup_${stage}.sdc] $legacy
    if {![::sft::nonempty_file $legacy]} {
        ::sft::fail "failed to create setup-view compatibility SDC for $stage"
    }
    return [list \
        [file join $::SFT_REPORT_DIR constraint_setup_${stage}.sdc] \
        [file join $::SFT_REPORT_DIR constraint_hold_${stage}.sdc]]
}

proc ::sft::constraints_are_byte_identical {} {
    foreach check {setup hold} {
        set before [file join $::SFT_REPORT_DIR constraint_${check}_before.sdc]
        set after [file join $::SFT_REPORT_DIR constraint_${check}_after.sdc]
        if {[::sft::read_file $before true] ne [::sft::read_file $after true]} {
            return 0
        }
    }
    return 1
}

proc ::sft::connectivity_report_is_clean {path} {
    set text [::sft::read_file $path]
    set counts {}
    if {[regexp -nocase {Found\s+no\s+problems\s+or\s+warnings\.} $text]} {
        lappend counts 0
    }
    if {[regexp -nocase {Found\s+([0-9]+)\s+problems?(?:\s+(?:and|,)\s+([0-9]+)\s+warnings?)?} $text -> problems warnings]} {
        if {$warnings eq ""} { set warnings 0 }
        lappend counts [expr {$problems + $warnings}]
    }
    if {[regexp -nocase {Verification\s+Complete\s*:\s*([0-9]+)\s+Viols?\.?(?:\s+([0-9]+)\s+Wrngs?\.?)?} $text -> violations warnings]} {
        if {$warnings eq ""} { set warnings 0 }
        lappend counts [expr {$violations + $warnings}]
    }
    set counts [lsort -integer -unique $counts]
    return [expr {[llength $counts] == 1 && [lindex $counts 0] == 0}]
}

proc ::sft::validate_final_timing {} {
    variable stage_metrics
    lassign $stage_metrics(after,setup) setup_wns setup_tns
    lassign $stage_metrics(after,hold) hold_wns hold_tns
    if {$setup_wns < $::SFT_CASE(final_setup_wns_min) || $setup_tns < -1.0e-12} {
        ::sft::fail "final setup guard failed: WNS=$setup_wns TNS=$setup_tns required WNS>=$::SFT_CASE(final_setup_wns_min), TNS=0"
    }
    if {$hold_wns < $::SFT_CASE(final_hold_wns_min) || $hold_tns < -1.0e-12} {
        ::sft::fail "final hold guard failed: WNS=$hold_wns TNS=$hold_tns required WNS>=$::SFT_CASE(final_hold_wns_min), TNS=0"
    }
    ::sft::validate_physical_no_regression
    puts "SFT_FINAL_TIMING_VALIDATED setup_wns=$setup_wns hold_wns=$hold_wns"
}

proc ::sft::write_violating_design {} {
    set checkpoint [file join $::SFT_CASE_DIR violating.enc]
    if {[catch {saveDesign $checkpoint -rc} message]} {
        puts stderr "SFT_PROBE_REQUIRED verify Innovus 21.10 saveDesign <checkpoint> -rc syntax and self-contained .dat output"
        ::sft::fail "violating checkpoint export failed: $message"
    }
    set checkpoint_data "${checkpoint}.dat"
    if {![::sft::nonempty_file $checkpoint] || ![::sft::nonempty_tree $checkpoint_data]} {
        ::sft::fail "saveDesign did not produce a self-contained violating.enc plus violating.enc.dat checkpoint"
    }
    set netlist [file join $::SFT_CASE_DIR before.v]
    if {[catch {saveNetlist $netlist} message]} {
        ::sft::fail "before-ECO saveNetlist failed: $message"
    }
    foreach check {setup hold} view [list $::SFT_SETUP_VIEW $::SFT_HOLD_VIEW] {
        set spef [file join $::SFT_CASE_DIR ${check}_before.spef]
        if {[catch {rcOut -spef $spef -view $view} message]} {
            puts stderr "SFT_PROBE_REQUIRED verify Innovus 21.10 rcOut -spef <file> -view <analysis_view> syntax"
            ::sft::fail "$check before-ECO SPEF export failed: $message"
        }
        if {![::sft::nonempty_file $spef]} {
            ::sft::fail "$check before-ECO SPEF is missing or empty"
        }
    }
    if {![::sft::nonempty_file $netlist]} {
        ::sft::fail "before-ECO gate netlist is missing or empty"
    }
    puts "SFT_VIOLATING_DESIGN_EXPORTED checkpoint=$checkpoint"
}

proc ::sft::write_exported_design {} {
    set netlist [file join $::SFT_CASE_DIR fixed.v]
    if {[catch {saveNetlist $netlist} message]} {
        ::sft::fail "saveNetlist failed: $message"
    }
    set setup_spef [file join $::SFT_CASE_DIR setup_after.spef]
    if {[catch {rcOut -spef $setup_spef -view $::SFT_SETUP_VIEW} message]} {
        puts stderr "SFT_PROBE_REQUIRED verify Innovus 21.10 rcOut -spef <file> -view <analysis_view> syntax"
        ::sft::fail "setup SPEF export failed: $message"
    }
    set hold_spef [file join $::SFT_CASE_DIR hold_after.spef]
    if {[catch {rcOut -spef $hold_spef -view $::SFT_HOLD_VIEW} message]} {
        ::sft::fail "hold SPEF export failed: $message"
    }
    foreach path [list $netlist $setup_spef $hold_spef] {
        if {![::sft::nonempty_file $path]} {
            ::sft::fail "design export is missing or empty: $path"
        }
    }
}

proc ::sft::write_check_design_evidence {} {
    set out_dir [file join $::SFT_REPORT_DIR check_design_after]
    if {[catch {checkDesign -all -outDir $out_dir} message]} {
        ::sft::fail "checkDesign -all failed: $message"
    }
    set evidence [glob -nocomplain -directory $out_dir *]
    if {![llength $evidence]} {
        ::sft::fail "checkDesign completed without producing evidence under $out_dir"
    }
    return $out_dir
}

proc ::sft::validate_concrete_fix_policy {path} {
    if {[::sft::uses_hold002_fixed_repeater_location]} {
        ::sft::validate_hold002_fixed_repeater_target
    }
    if {[::sft::uses_hold003_fixed_repeater_locations]} {
        ::sft::validate_hold003_fixed_repeater_target
    }
    if {[::sft::uses_mixed001_strong_restore_buffer]} {
        ::sft::validate_mixed001_strong_restore_targets
    }
    set allowed {setEcoMode setNanoRouteMode ecoChangeCell ecoAddRepeater optDesign refinePlace ecoRoute}
    set seen_action 0
    set command_index 0
    set last_action_index -1
    set first_action_index -1
    set batch_modes {}
    set batch_open 0
    set batch_enter_index -1
    set batch_exit_index -1
    set leq_modes {}
    set leq_disabled 0
    set leq_disable_index -1
    set leq_restore_index -1
    set timing_driven_modes {}
    set timing_driven_disabled 0
    set timing_driven_disable_index -1
    set timing_driven_restore_index -1
    set native_commands 0
    set refine_commands 0
    set refine_index -1
    set route_commands 0
    set target_route_commands 0
    set target_route_index -1
    set fix_drc_route_commands 0
    set fix_drc_route_index -1
    set hold002_fixed_repeater_commands 0
    set hold003_fixed_repeater_names {}
    set mixed001_strong_restore_commands 0
    set paired_drc_fix_bbox {907.30 263.40 914.90 270.50}
    foreach raw_line [split [::sft::read_file $path] "\n"] {
        set line [string trim $raw_line]
        if {$line eq "" || [string match "#*" $line]} { continue }
        if {[catch {set command [lindex $line 0]}]} {
            ::sft::fail "concrete fix has malformed Tcl: $line"
        }
        incr command_index
        if {[lsearch -exact $allowed $command] < 0} {
            ::sft::fail "concrete fix contains non-whitelisted command $command"
        }
        if {$command in {ecoChangeCell ecoAddRepeater optDesign}} {
            set seen_action 1
            if {$first_action_index < 0} { set first_action_index $command_index }
            set last_action_index $command_index
        }
        if {$command eq "setEcoMode"} {
            if {[llength $line] != 3 || [lindex $line 2] ni {true false}} {
                ::sft::fail "concrete fix has malformed setEcoMode command"
            }
            set option [lindex $line 1]
            set mode [lindex $line 2]
            if {$option eq "-batchMode"} {
                lappend batch_modes $mode
                set batch_open [expr {$mode eq "true"}]
                if {$mode eq "true"} {
                    set batch_enter_index $command_index
                } else {
                    set batch_exit_index $command_index
                }
            } elseif {$option eq "-LEQCheck"} {
                if {![::sft::uses_paired_inverter_repair]} {
                    ::sft::fail "concrete fix may change LEQCheck only for the topology-proven paired-inverter repair"
                }
                lappend leq_modes $mode
                set leq_disabled [expr {$mode eq "false"}]
                if {$mode eq "false"} {
                    set leq_disable_index $command_index
                } else {
                    set leq_restore_index $command_index
                }
            } else {
                ::sft::fail "concrete fix has non-whitelisted setEcoMode option $option"
            }
        }
        if {$command eq "setNanoRouteMode"} {
            if {[llength $line] != 3 ||
                [lindex $line 1] ne "-routeWithTimingDriven" ||
                [lindex $line 2] ni {true false}} {
                ::sft::fail "concrete fix may set only exact -routeWithTimingDriven true/false NanoRoute modes"
            }
            if {![::sft::uses_paired_inverter_repair]} {
                ::sft::fail "concrete fix may change routeWithTimingDriven only for a fingerprint-bound physical repair"
            }
            set mode [lindex $line 2]
            lappend timing_driven_modes $mode
            set timing_driven_disabled [expr {$mode eq "false"}]
            if {$mode eq "false"} {
                set timing_driven_disable_index $command_index
            } else {
                set timing_driven_restore_index $command_index
            }
        }
        if {$command in {ecoChangeCell ecoAddRepeater} && !$batch_open} {
            ::sft::fail "surgical ECO action appears outside setEcoMode batch boundary"
        }
        if {$command eq "ecoAddRepeater" &&
            [::sft::uses_hold002_fixed_repeater_location]} {
            incr hold002_fixed_repeater_commands
            set expected [list ecoAddRepeater \
                -term mac_out_nan_reg/D \
                -cell DLY4_X0P5M_A9TR40 \
                -name SFT_ECO_HOLD_002_HOLD_1 \
                -loc {898.00 248.08}]
            if {$line ne $expected} {
                ::sft::fail "HOLD_002 repair repeater must use its exact probe-proven term, cell, name, and fixed location"
            }
        }
        if {$command eq "ecoAddRepeater" &&
            [::sft::uses_hold003_fixed_repeater_locations]} {
            if {[llength $line] != 9 || [lindex $line 1] ne "-term" ||
                [lindex $line 2] ne "pp_nan_mts_d2_reg_8_/D" ||
                [lindex $line 3] ne "-cell" ||
                [lindex $line 4] ne "DLY4_X0P5M_A9TR40" ||
                [lindex $line 5] ne "-name" ||
                [lindex $line 7] ne "-loc"} {
                ::sft::fail "HOLD_003 repair repeaters must use the exact probe-proven term, cell, names, and fixed locations"
            }
            set name [lindex $line 6]
            set expected_locations [dict create \
                SFT_ECO_HOLD_003_HOLD_1 {914.15 263.20} \
                SFT_ECO_HOLD_003_HOLD_2 {915.86 263.20}]
            if {![dict exists $expected_locations $name] ||
                [lindex $line 8] ne [dict get $expected_locations $name]} {
                ::sft::fail "HOLD_003 repair repeater $name does not use its exact probe-proven fixed location"
            }
            lappend hold003_fixed_repeater_names $name
        }
        if {$command eq "ecoChangeCell" &&
            [::sft::uses_mixed001_strong_restore_buffer]} {
            incr mixed001_strong_restore_commands
            set expected_instances [list \
                u_exp/SFT_ECO_MIXED_001_PATH_1 \
                u_exp/SFT_ECO_MIXED_001_PATH_2 \
                u_exp/SFT_ECO_MIXED_001_PATH_3 \
                u_exp/SFT_ECO_MIXED_001_PATH_4]
            if {[llength $line] != 5 || [lindex $line 1] ne "-inst" ||
                [lsearch -exact $expected_instances [lindex $line 2]] < 0 ||
                [lindex $line 3] ne "-cell" ||
                [lindex $line 4] ne "BUF_X2M_A9TR40"} {
                ::sft::fail "MIXED_001 delay restoration must use four exact injected instances and BUF_X2M_A9TR40"
            }
        }
        if {$command in {ecoChangeCell ecoAddRepeater} &&
            [::sft::uses_paired_inverter_repair] && !$leq_disabled} {
            ::sft::fail "paired-inverter ECO action appears outside its LEQCheck-disabled proof window"
        }
        if {$command in {refinePlace ecoRoute} && $batch_open} {
            ::sft::fail "physical closeout appears before setEcoMode batch exit"
        }
        if {$command eq "refinePlace"} {
            incr refine_commands
            if {[llength $line] != 3 || [lindex $line 1] ne "-eco" ||
                [lindex $line 2] ne "true"} {
                ::sft::fail "repair closeout must be exactly refinePlace -eco true"
            }
            set refine_index $command_index
        }
        if {$command eq "ecoRoute"} {
            incr route_commands
            if {[llength $line] == 2 && [lindex $line 1] eq "-target"} {
                incr target_route_commands
                if {[::sft::uses_paired_inverter_repair] &&
                    $timing_driven_disabled} {
                    ::sft::fail "paired-inverter target route must retain timing-driven routing"
                }
                set target_route_index $command_index
            } elseif {[::sft::uses_paired_inverter_repair] &&
                      [llength $line] == 3 &&
                      [lindex $line 1] eq "-fix_drc" &&
                      [lindex $line 2] eq $paired_drc_fix_bbox} {
                incr fix_drc_route_commands
                if {!$timing_driven_disabled} {
                    ::sft::fail "paired-inverter local DRC repair must execute inside its routeWithTimingDriven-disabled window"
                }
                set fix_drc_route_index $command_index
            } else {
                ::sft::fail "repair closeout contains a non-whitelisted ecoRoute command or DRC box"
            }
        }
        if {$command eq "optDesign"} {
            incr native_commands
            set selected_index [lsearch -exact $line -selectedTerms]
            if {$selected_index < 0 || [lsearch -exact $line -postRoute] < 0 ||
                $selected_index + 1 >= [llength $line]} {
                ::sft::fail "native optDesign is not restricted to a post-route selectedTerms file"
            }
            set terms_file [lindex $line [expr {$selected_index + 1}]]
            set expected_terms_file [file join reports native_selected_terms.txt]
            if {$terms_file ne $expected_terms_file || [string match "-*" $terms_file]} {
                ::sft::fail "native optDesign selectedTerms file must be $expected_terms_file"
            }
            set expected_mode [expr {$::SFT_CASE(type) eq "setup" ? "-setup" : "-hold"}]
            set opposite_mode [expr {$expected_mode eq "-setup" ? "-hold" : "-setup"}]
            if {[lsearch -exact $line $expected_mode] < 0 ||
                [lsearch -exact $line $opposite_mode] >= 0 ||
                [lsearch -exact $line -incr] < 0} {
                ::sft::fail "native optDesign must select only $expected_mode and include -incr"
            }
        }
    }
    if {!$seen_action} {
        ::sft::fail "concrete fix has no ECO action"
    }
    if {$::SFT_CASE(repair_mode) eq "native"} {
        if {$native_commands != 1 || [llength $batch_modes] != 0 ||
            [llength $leq_modes] != 0 ||
            [llength $timing_driven_modes] != 0} {
            ::sft::fail "native concrete fix requires one optDesign and no ECO or NanoRoute mode commands"
        }
    } elseif {$native_commands != 0 || $batch_modes ne {true false}} {
        ::sft::fail "surgical concrete fix requires one setEcoMode enter/exit pair and no optDesign"
    }
    if {[::sft::uses_paired_inverter_repair]} {
        if {$leq_modes ne {false true} ||
            !($batch_enter_index < $leq_disable_index &&
              $leq_disable_index < $first_action_index &&
              $last_action_index < $batch_exit_index &&
              $batch_exit_index < $leq_restore_index &&
              $leq_restore_index < $refine_index)} {
            ::sft::fail "paired-inverter repair requires one ordered LEQCheck false/true window inside batch ECO and before closeout"
        }
    } elseif {[llength $leq_modes] != 0} {
        ::sft::fail "non-paired repair must not change LEQCheck"
    }
    if {[::sft::uses_hold002_fixed_repeater_location] &&
        $hold002_fixed_repeater_commands != 1} {
        ::sft::fail "HOLD_002 repair requires exactly one probe-proven fixed-location repeater"
    }
    if {[::sft::uses_hold003_fixed_repeater_locations] &&
        [lsort -dictionary $hold003_fixed_repeater_names] ne
            {SFT_ECO_HOLD_003_HOLD_1 SFT_ECO_HOLD_003_HOLD_2}} {
        ::sft::fail "HOLD_003 repair requires exactly its two probe-proven fixed-location repeaters"
    }
    if {[::sft::uses_mixed001_strong_restore_buffer] &&
        $mixed001_strong_restore_commands != 4} {
        ::sft::fail "MIXED_001 repair requires exactly four strong restore-buffer replacements"
    }
    if {[::sft::uses_paired_inverter_repair]} {
        if {$route_commands != 2 || $target_route_commands != 1 ||
            $fix_drc_route_commands != 1 ||
            $timing_driven_modes ne {false true} ||
            $target_route_index != $refine_index + 1 ||
            $timing_driven_disable_index != $target_route_index + 1 ||
            $fix_drc_route_index != $timing_driven_disable_index + 1 ||
            $timing_driven_restore_index != $fix_drc_route_index + 1} {
            ::sft::fail "paired-inverter closeout must be exact adjacent refinePlace, target route, routeWithTimingDriven false, proven-box fix_drc, routeWithTimingDriven true"
        }
    } elseif {[llength $timing_driven_modes] != 0 ||
              $route_commands != 1 || $target_route_commands != 1 ||
              $fix_drc_route_commands != 0} {
        ::sft::fail "ordinary repair requires one target route and must not use fix_drc or change routeWithTimingDriven"
    }
    if {$refine_commands != 1} {
        ::sft::fail "repair closeout requires exactly one refinePlace -eco true command"
    }
    if {!($last_action_index < $refine_index &&
          $refine_index < $target_route_index)} {
        ::sft::fail "repair closeout must follow every ECO action in refinePlace then ecoRoute order"
    }
    set text [::sft::read_file $path]
    foreach forbidden {set_false_path set_multicycle_path set_disable_timing set_clock_uncertainty set_clock_latency set_input_delay set_output_delay set_max_delay set_min_delay reset_path create_clock set_analysis_view set_interactive_constraint_modes source ::sft::} {
        if {[regexp -nocase "(^|\\n)\\s*[string map {* \\*} $forbidden]" $text]} {
            ::sft::fail "concrete fix contains forbidden abstraction/constraint command $forbidden"
        }
    }
}

proc ::sft::write_functional_audit {} {
    variable repair_operations
    variable functional_proofs
    variable native_changed_cells
    set concrete [file join $::SFT_REPORT_DIR concrete_fix.tcl]
    ::sft::validate_concrete_fix_policy $concrete
    if {[::sft::uses_paired_inverter_repair]} {
        ::sft::validate_paired_inverter_functional_proofs
    }
    if {![::sft::constraints_are_byte_identical]} {
        ::sft::fail "setup-view or hold-view before/after SDC differs byte-for-byte after exact Generated-on header normalization; repair is not Gold"
    }
    foreach path [list \
        [file join $::SFT_CASE_DIR before.v] \
        [file join $::SFT_CASE_DIR setup_before.spef] \
        [file join $::SFT_CASE_DIR hold_before.spef] \
        [file join $::SFT_CASE_DIR fixed.v] \
        [file join $::SFT_CASE_DIR setup_after.spef] \
        [file join $::SFT_CASE_DIR hold_after.spef] \
        [file join $::SFT_REPORT_DIR physical_no_regression.json] \
        [file join $::SFT_REPORT_DIR violation_locality.json] \
        [file join $::SFT_REPORT_DIR diagnostic_context.json]] {
        if {![::sft::nonempty_file $path]} {
            ::sft::fail "functional audit is missing export $path"
        }
    }
    if {$::SFT_CASE(repair_mode) eq "native" &&
        ![::sft::nonempty_file [file join $::SFT_REPORT_DIR native_selected_terms.txt]]} {
        ::sft::fail "functional audit is missing native selectedTerms file"
    }
    set connectivity [file join $::SFT_REPORT_DIR connectivity_after.rpt]
    if {![::sft::connectivity_report_is_clean $connectivity]} {
        ::sft::fail "functional audit cannot prove clean connectivity from $connectivity"
    }
    set check_design_dir [::sft::write_check_design_evidence]
    set manual 0
    set native 0
    foreach operation $repair_operations {
        set kind [dict get $operation kind]
        if {$kind in {ecoChangeCell ecoAddRepeater}} {
            incr manual
            if {![dict exists $operation proof]} {
                ::sft::fail "manual ECO operation lacks a Liberty function proof: $operation"
            }
            set proof [dict get $operation proof]
            if {[::sft::uses_paired_inverter_repair] &&
                $proof ne "paired_double_inversion"} {
                ::sft::fail "paired-inverter LEQ window contains an operation without paired-double-inversion proof: $operation"
            }
            if {![::sft::uses_paired_inverter_repair] &&
                $proof eq "paired_double_inversion"} {
                ::sft::fail "paired-double-inversion proof escaped its case-isolated repair"
            }
        } elseif {$kind eq "nativeOpt"} {
            incr native
        }
    }
    if {$::SFT_CASE(repair_mode) eq "surgical" && $manual == 0} {
        ::sft::fail "surgical case has no manual ECO operation"
    }
    if {$::SFT_CASE(repair_mode) eq "native" && $native != 1} {
        ::sft::fail "native case must contain exactly one selectedTerms optDesign operation"
    }
    if {$::SFT_CASE(repair_mode) eq "surgical" && ![llength $functional_proofs]} {
        ::sft::fail "no Liberty Boolean-function evidence was collected"
    }

    set path [file join $::SFT_REPORT_DIR functional_audit.json]
    set stream [open $path w]
    puts $stream "{"
    puts $stream {  "schema_version": "timing_eco_functional_audit.v1",}
    puts $stream "  \"case_id\": [::sft::json_quote $::SFT_CASE(id)],"
    puts $stream "  \"design\": [::sft::json_quote $::SFT_BASE_TOP],"
    puts $stream {  "passed": true,}
    puts $stream "  \"repair_mode\": [::sft::json_quote $::SFT_CASE(repair_mode)],"
    puts $stream "  \"checks\": {"
    puts $stream {    "fixed_netlist_written": {"passed": true, "evidence": "fixed.v exists and is non-empty"},}
    puts $stream {    "constraints_unchanged": {"passed": true, "evidence": "each write_sdc file had exactly one '#  Generated on:' line replaced by the fixed SFT marker; setup-view and hold-view before/after files are independently identical in every remaining raw byte"},}
    puts $stream {    "no_illegal_eco": {"passed": true, "evidence": "concrete_fix.tcl passed the command whitelist and constraint blacklist"},}
    puts $stream {    "liberty_function_preserved": {"passed": true, "evidence": "manual replacements use equal Liberty functions, proven non-inverting cells, or exclusive serial inverter pairs whose composed function is non-inverting"},}
    puts $stream {    "connectivity_clean": {"passed": true, "evidence": "connectivity_after.rpt reports zero problems"},}
    puts $stream {    "physical_no_regression": {"passed": true, "evidence": "before/after DRV and every DRC category passed typed no-regression comparison"},}
    puts $stream {    "violation_locality_complete": {"passed": true, "evidence": "every negative-slack endpoint equals the selected target set and cluster cardinality is preserved"},}
    puts $stream {    "check_design_completed": {"passed": true, "evidence": "checkDesign -all produced reports/check_design_after evidence"},}
    puts $stream {    "cell_budget_respected": {"passed": true, "evidence": "manual operation count or native before/after leaf-cell diff is within max_eco_cells"},}
    puts $stream {    "violating_design_preserved": {"passed": true, "evidence": "violating.enc/.dat, before.v, setup_before.spef, and hold_before.spef are non-empty"},}
    puts $stream {    "matching_corner_spef_written": {"passed": true, "evidence": "before/after setup and hold SPEFs are non-empty"}}
    puts $stream "  },"
    puts $stream "  \"evidence\": {\"repair_operations\": [llength $repair_operations], \"manual_operations\": $manual, \"native_operations\": $native, \"native_changed_cells\": $native_changed_cells, \"max_eco_cells\": $::SFT_CASE(max_eco_cells), \"liberty_function_proofs\": [llength $functional_proofs], \"check_design_dir\": [::sft::json_quote [file tail $check_design_dir]]},"
    puts $stream "  \"operations\": \["
    set last [expr {[llength $repair_operations] - 1}]
    for {set index 0} {$index <= $last} {incr index} {
        set comma [expr {$index == $last ? "" : ","}]
        puts $stream "    [::sft::json_quote [lindex $repair_operations $index]]$comma"
    }
    puts $stream "  \]"
    puts $stream "}"
    close $stream
}

proc ::sft::write_provisional_metrics {} {
    variable stage_metrics
    lassign $stage_metrics(before,setup) before_setup_wns before_setup_tns
    lassign $stage_metrics(before,hold) before_hold_wns before_hold_tns
    lassign $stage_metrics(after,setup) after_setup_wns after_setup_tns
    lassign $stage_metrics(after,hold) after_hold_wns after_hold_tns
    set path [file join $::SFT_CASE_DIR metrics.json]
    set stream [open $path w]
    puts $stream "{"
    puts $stream {  "status": "provisional; physical-report parsing, replay comparison, and PrimeTime are pending",}
    puts $stream "  \"before\": {\"setup\": {\"wns_ns\": $before_setup_wns, \"tns_ns\": $before_setup_tns}, \"hold\": {\"wns_ns\": $before_hold_wns, \"tns_ns\": $before_hold_tns}, \"drv\": {\"status\": \"unparsed\", \"report\": \"reports/drv_before.rpt\"}, \"drc\": {\"status\": \"unparsed\", \"report\": \"reports/drc_before.rpt\"}, \"connectivity\": {\"status\": \"unparsed\", \"report\": \"reports/connectivity_before.rpt\"}},"
    puts $stream "  \"after\": {\"setup\": {\"wns_ns\": $after_setup_wns, \"tns_ns\": $after_setup_tns}, \"hold\": {\"wns_ns\": $after_hold_wns, \"tns_ns\": $after_hold_tns}, \"drv\": {\"status\": \"unparsed\", \"report\": \"reports/drv_after.rpt\"}, \"drc\": {\"status\": \"unparsed\", \"report\": \"reports/drc_after.rpt\"}, \"connectivity\": {\"status\": \"unparsed\", \"report\": \"reports/connectivity_after.rpt\"}},"
    puts $stream {  "replay": {"status": "pending", "runs": 1, "deterministic": false},}
    puts $stream {  "checks": {"constraint_hash_unchanged": true, "functional_audit_passed": true, "primetime_crosscheck_passed": false}}
    puts $stream "}"
    close $stream
}

proc ::sft::write_success_marker {} {
    set stream [open [file join $::SFT_CASE_DIR SFT_CASE_PASSED] w]
    puts $stream "$::SFT_CASE(id) replay completed"
    close $stream
}

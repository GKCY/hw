# Innovus 21.10 timing-ECO runtime API help probe.
#
# Required environment:
#   SFT_RUNTIME_API_PROBE_DIR  output directory; it must not already exist
#
# This file is safe to source in an empty or already-restored Innovus process.
# It captures command-help text only.  It deliberately does not run any ECO,
# placement, routing, save/export, timing-report, or GUI-selection command.
# Therefore a SUPPORTED row proves only that this Innovus build advertises the
# requested command-line tokens; it is not behavioral or Gold evidence.

namespace eval ::sft_runtime_api_probe {
    variable schema "innovus_runtime_api_help_probe.v1"
    variable out_dir ""
    variable rows {}
    variable artifacts {}
    variable unsupported_count 0
}

proc ::sft_runtime_api_probe::require_env {name} {
    if {![info exists ::env($name)] || [string trim $::env($name)] eq ""} {
        error "required environment variable $name is not set"
    }
    return [file normalize $::env($name)]
}

proc ::sft_runtime_api_probe::write_text {path value} {
    file mkdir [file dirname $path]
    set stream [open $path w]
    puts -nonewline $stream $value
    close $stream
}

proc ::sft_runtime_api_probe::read_text {path} {
    set stream [open $path r]
    set value [read $stream]
    close $stream
    return $value
}

proc ::sft_runtime_api_probe::tsv_escape {value} {
    return [string map [list "\\" "\\\\" "\t" "\\t" "\n" "\\n" "\r" "\\r"] $value]
}

proc ::sft_runtime_api_probe::write_tsv {path headers rows} {
    set stream [open $path w]
    set encoded_headers {}
    foreach field $headers {
        lappend encoded_headers [::sft_runtime_api_probe::tsv_escape $field]
    }
    puts $stream [join $encoded_headers "\t"]
    foreach row $rows {
        if {[llength $row] != [llength $headers]} {
            close $stream
            error "[file tail $path] row has [llength $row] fields; expected [llength $headers]"
        }
        set encoded {}
        foreach field $row {
            lappend encoded [::sft_runtime_api_probe::tsv_escape $field]
        }
        puts $stream [join $encoded "\t"]
    }
    close $stream
}

proc ::sft_runtime_api_probe::tool_version {} {
    foreach command {getVersion version} {
        if {[llength [info commands $command]] == 0} { continue }
        if {![catch [list $command] value] && [string trim $value] ne ""} {
            return [string trim $value]
        }
    }
    if {[llength [info commands get_db]] &&
        ![catch {get_db program_version} value] && [string trim $value] ne ""} {
        return [string trim $value]
    }
    return "UNAVAILABLE"
}

proc ::sft_runtime_api_probe::design_state {} {
    # Read-only metadata only.  A loaded design is neither required nor used.
    if {[llength [info commands get_db]] &&
        ![catch {get_db current_design .name} value] && [string trim $value] ne ""} {
        return "LOADED:[string trim $value]"
    }
    if {[llength [info commands dbGet]] &&
        ![catch {dbGet top.name} value] &&
        [string trim $value] ni {"" 0 0x0 NULL null none}} {
        return "LOADED:[string trim $value]"
    }
    return "NO_DESIGN_OR_QUERY_UNAVAILABLE"
}

proc ::sft_runtime_api_probe::capture_one {id command required_tokens example purpose} {
    variable out_dir
    variable rows
    variable artifacts
    variable unsupported_count

    set relative [file join raw help "${id}.txt"]
    set path [file join $out_dir $relative]
    file mkdir [file dirname $path]
    set exists [expr {[llength [info commands $command]] > 0}]
    set status "UNSUPPORTED"
    set missing $required_tokens
    set detail "command is absent"

    if {$exists} {
        # The only dynamically evaluated command in this collector is help.
        set help_command [list help $command]
        if {[catch {redirect -file $path $help_command} message]} {
            set detail "help failed: $message"
            ::sft_runtime_api_probe::write_text "${path}.error.txt" "$message\n"
        } elseif {![file isfile $path] || [file size $path] == 0} {
            set detail "help produced no output"
        } else {
            set help_text [string tolower [::sft_runtime_api_probe::read_text $path]]
            set missing {}
            foreach token $required_tokens {
                if {[string first [string tolower $token] $help_text] < 0} {
                    lappend missing $token
                }
            }
            if {[llength $missing] == 0} {
                set status "SUPPORTED"
                set detail ""
            } else {
                set detail "help lacks required tokens"
            }
            lappend artifacts [list $relative [file size $path]]
        }
    }

    if {$status ne "SUPPORTED"} { incr unsupported_count }
    lappend rows [list $id $command $exists $status [join $required_tokens ,] \
        [join $missing ,] $example $purpose $relative $detail]
}

proc ::sft_runtime_api_probe::capture_contracts {} {
    # Each record is: stable id, command, tokens that must all occur in help,
    # intended runtime form (never executed here), and why the form matters.
    set specs [list \
        [list eco_change_cell ecoChangeCell {-inst -cell} \
            {ecoChangeCell -inst <instance> -cell <lib_cell>} \
            {surgical drive-strength replacement}] \
        [list eco_add_repeater ecoAddRepeater {-term -cell -name} \
            {ecoAddRepeater -term <sink_pin> -cell <lib_cell> -name <new_instance>} \
            {deterministically named data-path delay or buffer insertion}] \
        [list eco_batch_mode setEcoMode {-batchMode} \
            {setEcoMode -batchMode <true|false>} \
            {explicit ECO command batching boundary}] \
        [list save_design_with_rc saveDesign {-rc} \
            {saveDesign -rc <checkpoint.enc>} \
            {checkpoint form that advertises retained extracted RC}] \
        [list refine_place_eco refinePlace {-eco} \
            {refinePlace -eco true} \
            {incremental legalization after ECO edits}] \
        [list eco_route ecoRoute {-target} \
            {ecoRoute -target} \
            {incremental routing after legalization}] \
        [list rc_out_view rcOut {-spef -view} \
            {rcOut -spef <output.spef> -view <analysis_view>} \
            {view-specific post-ECO parasitic export}] \
        [list timing_collection report_timing \
            {-collection -late -early -view -max_paths -path_type -max_slack} \
            {report_timing -collection -late|-early -view <view> -max_paths <n> -path_type full_clock -max_slack <ns>} \
            {collection-valued setup and hold path discovery}] \
        [list native_selected_terms optDesign \
            {-postRoute -setup -hold -selectedTerms -incr} \
            {optDesign -postRoute -setup|-hold -selectedTerms <fileName> -incr} \
            {native timing optimization restricted by an explicit term-list file}] \
        [list native_fanin_scope all_fanin {-to -only_cells} \
            {all_fanin -to <endpoint_pin> -only_cells} \
            {audit native changed-cell locality}]]

    foreach spec $specs {
        lassign $spec id command required_tokens example purpose
        ::sft_runtime_api_probe::capture_one \
            $id $command $required_tokens $example $purpose
    }
}

proc ::sft_runtime_api_probe::finalize {} {
    variable schema
    variable out_dir
    variable rows
    variable artifacts
    variable unsupported_count

    ::sft_runtime_api_probe::write_tsv \
        [file join $out_dir command_probe.tsv] \
        {contract_id command command_exists help_status required_tokens missing_tokens intended_form purpose raw_help_file detail} \
        $rows
    ::sft_runtime_api_probe::write_tsv \
        [file join $out_dir artifact_manifest.tsv] \
        {relative_file bytes} $artifacts

    set metadata [list \
        [list schema $schema] \
        [list purpose runtime_api_help_only_never_gold] \
        [list tool_version [::sft_runtime_api_probe::tool_version]] \
        [list process_id [pid]] \
        [list design_state [::sft_runtime_api_probe::design_state]] \
        [list design_mutations 0] \
        [list commands_executed help_only] \
        [list gold_eligible false] \
        [list unsupported_contract_count $unsupported_count]]
    ::sft_runtime_api_probe::write_tsv \
        [file join $out_dir metadata.tsv] {key value} $metadata

    set marker "SFT_INNOVUS_RUNTIME_API_HELP_PROBE_COMPLETE\n"
    append marker "GOLD_ELIGIBLE=FALSE\n"
    append marker "DESIGN_MUTATIONS=0\n"
    append marker "COMMANDS_EXECUTED=HELP_ONLY\n"
    append marker "UNSUPPORTED_CONTRACTS=$unsupported_count\n"
    ::sft_runtime_api_probe::write_text [file join $out_dir probe.status] $marker
    puts -nonewline $marker
}

proc ::sft_runtime_api_probe::main {} {
    variable out_dir
    set out_dir [::sft_runtime_api_probe::require_env SFT_RUNTIME_API_PROBE_DIR]
    if {[file exists $out_dir]} {
        error "SFT_RUNTIME_API_PROBE_DIR already exists; refusing to overwrite evidence: $out_dir"
    }
    file mkdir $out_dir
    ::sft_runtime_api_probe::capture_contracts
    ::sft_runtime_api_probe::finalize
}

::sft_runtime_api_probe::main

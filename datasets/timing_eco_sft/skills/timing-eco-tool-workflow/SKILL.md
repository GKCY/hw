---
name: timing-eco-tool-workflow
description: Run reliable timing-ECO diagnosis and repair with Cadence Innovus 21.10 and Synopsys PrimeTime R-2020.09-SP4. Use for post-route setup, hold, or mixed timing cases where an agent must inspect a frozen checkpoint, exercise candidate ECOs, avoid Tcl/API and process-control mistakes, and emit a small replayable fix.tcl.
---

# Timing ECO Tool Workflow

Work directly in the EDA VM. Treat the case task as authoritative for the top
name, analysis views, timing evidence, allowed objects, ECO budget, naming
rules, and acceptance thresholds. Use exact objects already supplied by the
task.

## Separate diagnostics from the deliverable

Use scratch Tcl files and reports for restore, queries, timing analysis, and
experiments. Keep the final `fix.tcl` literal and minimal: it must contain only
the policy-allowed ECO actions followed by incremental placement and routing.

## Use the Innovus 21.10 command forms exactly

- Restore a checkpoint with `restoreDesign <checkpoint> <top>`.
- Activate both views together:
  `set_analysis_view -setup <setup_view> -hold <hold_view>`.
- Report setup with
  `report_timing -late -view <setup_view> -max_paths <n>`.
- Report hold with
  `report_timing -early -view <hold_view> -max_paths <n>`.
- Replace a cell with
  `ecoChangeCell -inst <instance> -cell <lib_cell>`.
- Insert a repeater with
  `ecoAddRepeater -term <sink_pin> -cell <lib_cell> -name <new_instance>`;
  add `-loc {<x> <y>}` only when justified and allowed.
- Enter and leave ECO batching with
  `setEcoMode -batchMode true` and `setEcoMode -batchMode false`.
- Legalize and route with `refinePlace -eco true` followed by
  `ecoRoute -target`.

When uncertain about an option, use `help <command>` without restoring the
design. Once a checkpoint is loaded, keep design-dependent command discovery,
queries, and validation in that same Innovus process and restored state. Fold
the proven command form into the next candidate script instead of paying for
another restore only to probe report syntax. Use full documented option names.

## Make every diagnostic process terminate

An error in a Tcl file passed with `innovus -no_gui -files` can stop file
processing before a trailing `exit` and leave Innovus waiting interactively.
Wrap the entire scratch body in `catch` so success and failure both exit:

```tcl
set script_status [catch {
    restoreDesign /case/violating.enc <top>
    set_analysis_view -setup <setup_view> -hold <hold_view>
    # Diagnostic or candidate experiment.
} script_message script_options]
if {$script_status} {
    puts stderr "EDA_SCRIPT_ERROR: $script_message"
    puts stderr [dict get $script_options -errorinfo]
}
exit $script_status
```

Run initial syntax/help probes with short timeouts. Use roughly 5–10 minutes
for a first restore/timing experiment and extend a timeout only after the log
shows useful EDA progress. After each process, inspect its exit status and log
before changing the script. Terminate a process promptly after its log shows a
known command error.

## Query conservatively

Prefer stable collection commands such as `get_cells -quiet`,
`get_pins -quiet`, and `get_lib_cells -quiet`. Check that an exact query
resolves the expected number of objects before applying an ECO.

For a necessary `dbGet` or `get_db` query, inspect its command help and prove
the complete query in a small read-only probe first.

## Iterate from a fresh state

For every candidate:

1. Start a fresh Innovus process and restore the frozen checkpoint.
2. Activate both setup and hold views.
3. Capture before timing when useful.
4. Source the scratch copy of the candidate repair.
5. Report setup with `-late` and hold with `-early`.
6. Require the candidate to preserve passing slack in both directions.
7. Check the log for command errors, unresolved objects, placement/routing
   failures, and new physical violations.

Begin each experiment from a fresh restore so an earlier command failure or
accumulated ECO state cannot contaminate comparisons.

## Check DRV with the active MMMC views

After activating the case's setup and hold analysis views, report each
electrical rule against the setup view with the Innovus 21.10 form:

```tcl
foreach drv_type {max_transition max_capacitance max_fanout} {
    report_constraint \
        -drv_violation_type $drv_type \
        -view <setup_view> \
        -all_violators \
        -verbose
}
```

Compare the counts in all three reports with the fresh-state baseline. Treat
`No Violations found` as zero; a nonzero baseline count is not a candidate
failure unless the candidate increases it. Each violation row identifies its
analysis view. In this Innovus release these electrical design rules are
reported against the active setup/late view even though both setup and hold
views remain activated for the surrounding timing validation.

## Check DRC and connectivity with report commands

Use the version-proven Innovus 21.10 report commands instead of querying
internal marker objects.  Define a writable report directory below the
workspace, then collect both reports from the current restored state:

```tcl
set report_dir /workspace/reports
file mkdir $report_dir

verify_drc \
    -limit 1000000 \
    -report [file join $report_dir drc_before.rpt]
verifyConnectivity \
    -type all \
    -report [file join $report_dir connectivity_before.rpt]
```

After applying, legalizing, and routing the candidate in the same Innovus
process, run the identical commands with `after` report names:

```tcl
verify_drc \
    -limit 1000000 \
    -report [file join $report_dir drc_after.rpt]
verifyConnectivity \
    -type all \
    -report [file join $report_dir connectivity_after.rpt]
```

Do not omit the DRC limit: Innovus otherwise defaults to a bounded report that
can stop before all violations are collected.  Require the report's total to
remain below the limit and compare every DRC category in the union of the
before and after reports; each after count must be no greater than its before
count.  Require the after connectivity report to contain zero violations.
Check that every requested report exists and is non-empty before trusting it.

Do not substitute `get_markers`, `get_db markers`, or guessed marker
attributes for these report commands.  Those internal object interfaces are
not portable across Innovus versions and are unnecessary for outcome
validation.  If either report command itself is uncertain in a different tool
version, prove its syntax with `help` before restoring the design.

## Consolidate final validation

After the candidate has converged, perform one final validation from one fresh
Innovus process and one restore. In that same post-ECO state, report overall
setup and hold timing, the three DRV classes, DRC, and connectivity. Save those
reports before exiting. A prior candidate experiment can guide convergence,
but it does not justify repeating separate `verify1` and `final_verify` passes
when the candidate and restored state are unchanged.

## Choose a small functional repair

- For setup, remove unnecessary data delay or use a faster, functionally
  compatible local cell; consider a conservative same-function drive-strength
  change only on task-authorized local cells.
- For hold, add task-authorized non-inverting data delay at the specified
  endpoint. Use deterministic instance names exactly as required by the task.
- For mixed cases, combine the minimum setup and hold actions within the
  shared budget, then recheck both directions.
- Preserve Boolean function and pin compatibility. Select a replacement only
  after proving that compatibility.

Focus the search on the diagnostic targets and local-cell list supplied in the
task.

## Emit the final repair

A typical surgical `fix.tcl` has this shape, with only justified actions:

```tcl
setEcoMode -batchMode true
ecoChangeCell -inst <authorized_instance> -cell <compatible_cell>
# or: ecoAddRepeater -term <authorized_endpoint> -cell <delay_cell> -name <required_name>
setEcoMode -batchMode false
refinePlace -eco true
ecoRoute -target
```

Write the best tested candidate to `/workspace/fix.tcl` before finishing.
Keep diagnostic scripts and reports in scratch space so `fix.tcl` remains the
small, replayable deliverable.

## Use PrimeTime as a bounded final cross-check

First obtain a candidate that passes both Innovus views. Start PrimeTime only
when a matching post-ECO netlist, mode-specific SDC, Liberty library, and SPEF
already exist from the same candidate state.

Refresh extracted parasitics after the ECO, then export the inputs once from
that state with the Innovus 21.10 forms. A post-route ECO can leave in-memory
parasitics dirty; `extractRC` clears that state before `rcOut`.

```tcl
setExtractRCMode -engine postRoute -effortLevel medium
extractRC
saveNetlist <post_eco_netlist>
write_sdc -view <setup_view> <setup_sdc>
write_sdc -view <hold_view> <hold_sdc>
rcOut -spef <setup_spef> -view <setup_view>
rcOut -spef <hold_spef> -view <hold_view>
```

Run one isolated PrimeTime process per corner:

- Setup: setup Liberty, setup SDC, setup SPEF, and `-delay_type max`.
- Hold: hold Liberty, hold SDC, hold SPEF, and `-delay_type min`.

One setup invocation and one hold invocation are sufficient. Treat PrimeTime
as confirmation of an Innovus-converged candidate rather than as an
open-ended command-discovery loop.

### Adapt an Innovus SDC for PrimeTime

Create a separate PT-specific SDC while preserving the original Innovus SDC.
Apply only these bounded dialect translations:

1. Turn the generated `current_design <top>` line into a comment. The PT
   script makes the requested top current with `link_design`.
2. Express design-scoped `set_max_fanout` and `set_max_transition`
   constraints with `[current_design]`.
3. Preserve all other constraint lines unchanged.

Keep setup and hold SDCs separate; load only the SDC matching the current
Liberty/SPEF corner.

### Link and validate the requested top explicitly

Use this sequence before timing:

```tcl
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
    error "failed to link requested top $top"
}
set linked_top [get_object_name [current_design]]
if {$linked_top ne $top} {
    error "linked top $linked_top does not match $top"
}

read_sdc $pt_sdc
set_units -time ns
set clocks [get_clocks *]
if {[sizeof_collection $clocks] < 1} {
    error "no clocks exist after read_sdc"
}
set_propagated_clock $clocks
read_parasitics -format SPEF $spef
update_timing
```

For a collection count, use the collection directly, for example
`sizeof_collection [all_registers]`. Check the top, clocks, and a constrained
worst path before generating large reports:

```tcl
set worst [get_timing_paths \
    -delay_type $delay_type \
    -max_paths 1 \
    -nworst 1]
if {[sizeof_collection $worst] != 1} {
    error "no constrained $delay_type timing path"
}
report_timing \
    -delay_type $delay_type \
    -max_paths 5 \
    -nworst 1
```

### Make PrimeTime exit on every path

Put the complete PT flow in one procedure and catch it at top level. Make the
Tcl script itself return a status and exit; use an OS timeout only as a safety
net.

```tcl
proc run_pt {} {
    # Read one corner, link the exact top, load its SDC and SPEF, then report.
    return 0
}

set pt_status 2
if {[catch {set pt_status [run_pt]} pt_message pt_options]} {
    puts stderr "PT_SCRIPT_ERROR: $pt_message"
    if {[dict exists $pt_options -errorinfo]} {
        puts stderr [dict get $pt_options -errorinfo]
    }
}
exit $pt_status
```

Use a short timeout for the first complete PT run. If its preflight cannot
establish the exact linked top, at least one clock, annotated parasitics, and
one constrained timing path, retain the Innovus-tested candidate and finish
instead of expanding the PT experiment.

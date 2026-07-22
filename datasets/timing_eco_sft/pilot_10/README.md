# Pilot-10 case orchestration

`catalog.json` is the deterministic definition of the four setup, four hold,
and two mixed cases.  All ten cases use budgeted object-level surgical ECOs.
The final catalog contains no native-optimization case.  Catalog
selectors contain only structural classes and stable ranks; exact pins, nets,
instances, and replacement cells are resolved after restoring the post-route
database and are written to `reports/resolved_targets.tcl`.

Every hold-bearing case uses a local capture-clock-delay injection and does not
alter global clock uncertainty.  `HOLD_004` repairs its one selected data
endpoint with exactly two `DLY4_X0P5M_A9TR40` cells under a two-cell budget.
Every injection attempt records measured
setup/hold WNS/TNS in
`reports/injection_provenance.json`.  Only a frozen first attempt may become
Gold: if a later cumulative discovery attempt reaches the catalog window, the
replay deliberately fails and asks for a fresh run with the discovered action
count frozen in the catalog.
Before resolving any target, replay measures the untouched checkpoint and
requires setup and hold WNS of at least +20 ps with zero TNS; the measurement
is saved as `reports/baseline_guard.json`.  A case therefore cannot hide a
pre-existing baseline violation.

After a one-shot NOT_GOLD run, use `tools/freeze_calibration.py` to validate
the complete fetched evidence before changing a case to `FROZEN`.  The tool
does not run EDA and does not treat the NOT_GOLD marker as a pass by itself.
It defaults to a new `catalog.frozen-<CASE>.json`; replacing the input requires
the explicit `--in-place` option.  Capture-clock references for `HOLD_003` and
`MIXED_002`, plus case-local setup data-delay references, are taken only from
the measured injection actions.  Frozen data-delay injection therefore stays
bound to the measured cell even if later calibration stages reorder the global
delay candidates.  Capture-clock calibration may freeze a positive multi-cell
serial chain, but all measured actions must use the same safe reference and
the same clock pin on exactly one selected early endpoint.

Surgical repairs that insert hold data delay have a separate case-local
contract: `repair.delay_cell_reference` and
`repair.delay_cells_per_endpoint`.  A `PROBE_REQUIRED` case may omit the pair
for compatibility, but every `FROZEN` case must provide an exact safe Liberty
reference and a positive count.  Gold never falls back to the global delay
candidate order.  The catalog validator also proves that `max_eco_cells` is at
least the statically determined repair-operation count.  See `CALIBRATION.md`
for the command and all gates.

The runner consumes the benchmark baseline contract:

```text
<baseline>/base.enc
<baseline>/base.enc.dat/
<baseline>/manifest.tcl
<baseline>/restore.tcl
```

For Gold preparation, `qualification.json` must also carry
`source_artifacts.setup_liberty_ss` and
`source_artifacts.hold_liberty_ff`, each with the qualified source file's
SHA256, positive byte count, and absolute path.  Older qualification bundles
without those entries remain usable only for explicit NOT_GOLD calibration.

`manifest.tcl` defines `::SFT_BASE_TOP`, `::SFT_SETUP_VIEW`,
`::SFT_HOLD_VIEW`, and `::SFT_BASE_CHECKPOINT`.  `restore.tcl` must restore the
checkpoint and activate both views.

## Prepare and inspect

From the repository root:

```sh
python3 datasets/timing_eco_sft/tools/run_pilot.py list

python3 datasets/timing_eco_sft/tools/run_pilot.py prepare \
  --baseline-bundle pnr/innovus/smic40/build_benchmark/baseline_qualified \
  --run-id pilot10-review-01
```

Preparation verifies and hashes the baseline, then renders independent
`case_config.tcl`, `inject.tcl`, `fix.tcl`, and `replay.tcl` files under the
ignored `work/<run-id>/` directory.  It copies the exact baseline
qualification into each case and binds the baseline tree, catalog,
qualification, and checksum-manifest hashes into the prepared manifest.  It
does not contact the VM.

## Run in qingteng-fc

The Firecracker VM must already be running and reachable through the verified
`qingteng-fc` SSH alias.  The runner never starts, stops, or reconfigures it.

```sh
python3 datasets/timing_eco_sft/tools/run_pilot.py run \
  --run-dir datasets/timing_eco_sft/pilot_10/work/pilot10-review-01
```

The default guest root is `/home/host/nvdla_timing_eco_sft`.  The baseline is
synced once per run, while every case/replay gets a fresh guest directory and
Innovus process.  Two replays are run by default.  Results are fetched after
each process even on failure and stored below each case's `runs/replay_N/`.
Use `--dry-run` to print commands without contacting the guest, and use the
`fetch` subcommand to retry an interrupted artifact transfer.

Every replay retains two distinct transcripts: `logs/innovus.log` is the
authoritative Innovus version/tool transcript, while `logs/host_ssh.log` is
the host-side SSH stdout capture.  Gold requires each host log to be non-empty
and to contain exactly one full line `SFT_CASE_PASSED <ID>`; putting the marker
only in the Innovus log is insufficient.

Generated `metrics.json` is intentionally marked provisional.  A case must not
be copied into `pilot_10/cases/` or exported as Gold until report parsing,
two-replay comparison, functional/constraint audit, and PrimeTime cross-check
replace those provisional fields with passing evidence.

Each successful replay additionally emits the concrete Gold answer at
`reports/concrete_fix.tcl`, independent active-constraint snapshots
`constraint_setup_before.sdc`, `constraint_setup_after.sdc`,
`constraint_hold_before.sdc`, and `constraint_hold_after.sdc` below
`reports/`, plus `reports/functional_audit.json`.  Before/after bytes must
match within each view; setup and hold are intentionally separate artifacts
and need not match each other.  The PrimeTime v4 crosscheck retains those raw
Innovus SDCs unchanged, then deterministically prefixes only the unique exact
`current_design <top>` line with `# PT_DIALECT_ADAPTER: ` in separate
setup/hold `.pt.sdc` artifacts.  Each process uses only its mode-specific
adapted file, and execution evidence binds both that file and its raw source.
The staged Tcl must be byte-identical to the approved checked-in template;
every invocation uses `-no_init` with an isolated `HOME` and working directory,
and the v4 policy binds the exact executable, command, path-count limits, and
Tcl digest/size.  Typed results, reports, and process logs are rejected if
they contain fatal PrimeTime transcript markers even when a PASS marker is
present.
Legacy
`constraint_before.sdc`/`constraint_after.sdc` setup-view aliases are not
sufficient for Gold crosscheck evidence.  The fixed gate netlist and
matching-corner parasitics are replay-root files `fixed.v`,
`setup_after.spef`, and `hold_after.spef`.  The concrete fix contains only
actual design objects and Innovus ECO commands; it includes incremental
placement and ECO routing and never sources runtime helper procedures.
The replay also preserves `reports/baseline_guard.json`,
`reports/physical_no_regression.json`, the violating `violating.enc` wrapper
and complete `violating.enc.dat/` tree, `before.v`, both before-ECO SPEFs, and
the full `reports/check_design_after/` tree.  Gold finalization hashes and
compares those artifacts across both replays; copying only the pass marker or
top-level reports is insufficient.
The injection measurement and the immediately following before stage share the
same routed database state and explicit MMMC views.  The runtime therefore
reuses that measured WNS/TNS pair for its internal before-stage summary, while
still generating both complete `setup_before.rpt` and `hold_before.rpt` files;
the finalizer independently parses those reports.  Post-repair after-stage
timing is always freshly measured.  Violation-locality collection similarly
reuses each selected endpoint's worst slack from the complete post-injection
negative-path query, but performs an exact pin/view query for every selected
endpoint absent from that dictionary, preserving fail-closed coverage.
The generic harness retains fail-closed native-operation evidence support, but
the final ten-case catalog does not exercise it.  In a real Innovus 21.10 Gold
qualification attempt, the `HOLD_004` selected-terms hold optimization changed
three leaf cells outside the selected endpoint's before/after fanin union.
Adding `-targeted` was then rejected with `IMPOPT-7277` because it was
incompatible with the incremental/mode flow.  That candidate was therefore
discarded instead of weakening locality: `HOLD_004` is surgical and neither
`native_selected_terms.txt` nor `native_cell_diff.tcl` is required for it.

## Gold promotion sequence

After every case has passed one fresh NOT_GOLD calibration attempt and its
complete action has been manually frozen in `catalog.json`, prepare and run a
new bundle without the calibration escape hatch:

```sh
PILOT="$PWD/datasets/timing_eco_sft/pilot_10"
TOOLS="$PWD/datasets/timing_eco_sft/tools"
BASE="$PWD/pnr/innovus/smic40/build_benchmark/baseline_qualified"
WORK="$PILOT/work"
RUN_ID=pilot10-gold-01

python3 "$TOOLS/run_pilot.py" prepare \
  --baseline-bundle "$BASE" \
  --work-root "$WORK" \
  --run-id "$RUN_ID" \
  --case all

python3 "$TOOLS/run_pilot.py" run \
  --run-dir "$WORK/$RUN_ID" \
  --replays 2
```

Stage the exact qualified SS and FF Liberty files on the host and verify their
SHA256 and byte counts against `qualification.json`.  Then run one PrimeTime
crosscheck per case.  `--output-dir` is the prepared case root, not replay 1;
the fixed netlist, SDCs, and SPEFs all come from replay 1:

```sh
RUN="$WORK/$RUN_ID"
CASE=SETUP_001
CASE_ROOT="$RUN/cases/$CASE"
R1="$CASE_ROOT/runs/replay_1"

python3 "$TOOLS/primetime_crosscheck.py" run \
  --ssh-target qingteng-fc \
  --remote-root /home/host/nvdla_timing_eco_sft/primetime \
  --pt-shell /opt/synopsys/prime/R-2020.09-SP4/bin/pt_shell \
  --top NV_NVDLA_CMAC_CORE_mac \
  --netlist "$R1/fixed.v" \
  --setup-sdc "$R1/reports/constraint_setup_after.sdc" \
  --hold-sdc "$R1/reports/constraint_hold_after.sdc" \
  --setup-lib "$RUN/pt_libs/setup.lib" \
  --hold-lib "$RUN/pt_libs/hold.lib" \
  --setup-spef "$R1/setup_after.spef" \
  --hold-spef "$R1/hold_after.spef" \
  --output-dir "$CASE_ROOT" \
  --threshold-ns 0.010 \
  --require-tool-version R-2020.09-SP4 \
  --synopsys-lc-root /opt/synopsys/lc/R-2020.09-SP3 \
  --timeout-seconds 7200
```

`SYNOPSYS_LC_ROOT` is passed explicitly to each clean PT process and is
recorded identically in v4 policy, setup/hold execution records, and typed
corner markers.  Repeat that command for all ten IDs.  The `run` subcommand
already emits the self-contained v4 summary, so no additional `merge` is
needed.  Finalizer v2 publishes stable
`pt_input_setup_sdc_adapted`/`pt_input_hold_sdc_adapted` manifest roles for
the copied `.pt.sdc` files, in addition to stable raw SDC roles.  The dataset
validator reruns the PT verifier, binds raw SDC bytes to replay-1 constraints,
and requires the adapted roles' path/hash/bytes to equal the deterministic v4
metadata.  Validate all evidence before materializing canonical cases, then
export and revalidate the ten-line JSONL:

```sh
python3 "$TOOLS/finalize_pilot.py" \
  --run-dir "$RUN" --output-root "$PILOT" --case all --validate-only
python3 "$TOOLS/finalize_pilot.py" \
  --run-dir "$RUN" --output-root "$PILOT" --case all
python3 "$TOOLS/dataset_cli.py" validate \
  --pilot-root "$PILOT" --source-only
python3 "$TOOLS/dataset_cli.py" export \
  --pilot-root "$PILOT" --output "$PILOT/dataset.jsonl"
python3 "$TOOLS/dataset_cli.py" validate \
  --pilot-root "$PILOT" --output "$PILOT/dataset.jsonl"
wc -l "$PILOT/dataset.jsonl"
```

## Innovus 21.10 runtime API help probe

Before calibration, capture the exact command help from the same Innovus ISR
that will execute the cases.  Use a new output directory on every run:

```sh
SFT_RUNTIME_API_PROBE_DIR=/absolute/path/to/api-probe-21.10 \
  innovus -no_gui -execute \
  'source /absolute/path/to/innovus_runtime_api_probe.tcl; exit'
```

The source file is
`templates/innovus_runtime_api_probe.tcl`.  It is safe with or without a
loaded design: it only invokes `help` plus read-only version/design-state
queries.  It never executes ECO, timing-report, selection, placement, routing,
save, or export commands.  `command_probe.tsv` records the expected runtime
form and missing help tokens; `raw/help/*.txt` preserves the unmodified tool
output.  In particular it checks the `-name` form of `ecoAddRepeater`, the
`-rc` form of `saveDesign`, both setup/hold `report_timing -collection` forms,
and the generic native (not selected by the final catalog)
`optDesign ... -selectedTerms <fileName> -incr` surface.

This artifact is syntax-discovery evidence only.  Even when every row is
`SUPPORTED`, `probe.status` remains `GOLD_ELIGIBLE=FALSE` and
`DESIGN_MUTATIONS=0`.  Command behavior, batch-boundary ordering, checkpoint
RC retention, repeated-repeater topology, and selected-term containment still
require isolated real-design calibration and two fresh replays.

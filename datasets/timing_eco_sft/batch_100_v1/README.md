# Mock LEF Batch-100 v1

This directory contains a fail-closed generator and validator for
`B1_CASE_001` through `B1_CASE_100`.  `CASE_CATALOG.md` remains the immutable
planning baseline; machine state and measured conclusions live under the
ignored `work/` directory and in finalized case artifacts.

The batch is non-signoff training data.  The only design mutations allowed in
an answer are functionally equivalent combinational RVT `ecoChangeCell`
operations followed by `refinePlace -eco true`.  Buffer insertion, constraint
or clock ECO, sequential/clock-gating/macro edits, routing optimization, and
`ecoRoute` are rejected.

## Relaxed v3 contract and first validated slice

`RELAXED_V3.md` defines a construction contract whose machine-readable card
contains planning inputs only.  It preserves
each original card's severity and tolerance while reducing construction to one
endpoint-local resize with an allowed direct-inverse repair.  Build and check
its machine-readable card set without starting EDA or generating case data:

```sh
python3 datasets/timing_eco_sft/batch_100_v1/tools/relaxed_v3.py build
python3 datasets/timing_eco_sft/batch_100_v1/tools/relaxed_v3.py check
```

The separately authorized run `work/batch100-relaxed-v3-r1` contains validated
real-Innovus artifacts for `B1_CASE_002` through `B1_CASE_005`.  It is a
partial run and cannot be finalized as a 100-case dataset.

The v3 runtime path was explicitly enabled for that independent run; existing
relaxed-v2 evidence was left unchanged.  The consolidated `B1_CASE_001`–`005`
results and their differing run provenance are documented in
`CASES_001_005_VALIDATION_REPORT.md`.  Pilot-style, case-by-case reports are
available under `case_reports/cases/`.

## Commands

From the repository root:

```sh
python3 datasets/timing_eco_sft/batch_100_v1/tools/batch100.py catalog-check

python3 datasets/timing_eco_sft/batch_100_v1/tools/batch100.py init \
  --run-dir datasets/timing_eco_sft/batch_100_v1/work/batch100-v1

python3 datasets/timing_eco_sft/batch_100_v1/tools/batch100.py probe \
  --run-dir datasets/timing_eco_sft/batch_100_v1/work/batch100-v1

python3 datasets/timing_eco_sft/batch_100_v1/tools/batch100.py run \
  --selection canary \
  --run-dir datasets/timing_eco_sft/batch_100_v1/work/batch100-v1

python3 datasets/timing_eco_sft/batch_100_v1/tools/batch100.py run \
  --selection remaining \
  --run-dir datasets/timing_eco_sft/batch_100_v1/work/batch100-v1

python3 datasets/timing_eco_sft/batch_100_v1/tools/batch100.py resume \
  --run-dir datasets/timing_eco_sft/batch_100_v1/work/batch100-v1

python3 datasets/timing_eco_sft/batch_100_v1/tools/batch100.py status \
  --run-dir datasets/timing_eco_sft/batch_100_v1/work/batch100-v1

python3 datasets/timing_eco_sft/batch_100_v1/tools/batch100.py fetch \
  --case-id B1_CASE_001 \
  --run-dir datasets/timing_eco_sft/batch_100_v1/work/batch100-v1

python3 datasets/timing_eco_sft/batch_100_v1/tools/batch100.py finalize \
  --run-dir datasets/timing_eco_sft/batch_100_v1/work/batch100-v1
```

`init` binds the catalog, structured case specs, baseline tree and every
runtime/finalizer source by SHA-256.  `resume` refuses an in-place continuation
when any bound input changes.  `probe` runs once from a fresh Innovus process,
collects at most 20,000 late paths with point-level delay/slew and
function-equivalent RVT ladders, then produces candidate bindings.  A binding
is never treated as validation: calibration must freeze exact injection and
repair objects before replay.

The state machine is:

```text
PLANNED -> PROBE_ELIGIBLE -> BOUND -> CALIBRATING -> FROZEN
        -> REPLAYING -> VALIDATED -> FINALIZED
```

All transitions are written both to SQLite in WAL mode and to one durable
atomic sidecar per case.  The remaining 88 cases cannot run until all 12
canaries are `VALIDATED`.

## Finalization boundary

The strict finalizer requires two fresh Innovus processes on different slots,
identical checkpoint/fix/constraint/topology/cell-diff evidence, timing values
within 1 ps, exact target and protected endpoint sets, closure before and after
legalization, no routing mutations, no DRV/DRC/connectivity regression, and
complete functional/RVT/pin-signature audits.  Hold is recorded with
`hold_gate_applied=false`.

Only after 100/100 cases pass does it atomically write `dataset.jsonl` and the
batch `manifest.json`.  It rejects any retained fixed/after/replay checkpoint.

## Tests

```sh
python3 -m unittest -v \
  datasets/timing_eco_sft/batch_100_v1/tools/test_batch100.py
```

The suite covers the catalog distribution and overrides, severity and I0
contracts, Tcl policy, state recovery and input hashes, safe cleanup, artifact
tree hashing, and negative finalizer gates.

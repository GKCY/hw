# Timing ECO SFT pilot dataset

`pilot_10/cases/<ID>/` is the source of truth.  The checked-in
`pilot_10/dataset.jsonl` is a deterministic, validated export; do not edit it
by hand.

## Export and validation

Run from the repository root:

```sh
python3 datasets/timing_eco_sft/tools/dataset_cli.py export
python3 datasets/timing_eco_sft/tools/dataset_cli.py validate
```

To check case sources before `dataset.jsonl` exists:

```sh
python3 datasets/timing_eco_sft/tools/dataset_cli.py validate --source-only
```

Both commands accept `--pilot-root` and `--output`.  The exporter writes UTF-8
JSONL atomically and sorts records by case ID.  The validator rebuilds every
record from its source files and rejects a stale or manually changed JSONL.

## Case contract

Each case directory contains `manifest.json`, `instruction.txt`, `answer.txt`,
`metrics.json`, `inject.tcl`, `fix.tcl`, `replay.tcl`, timing/physical reports,
and the Innovus run log.  The basic timing/report portion of
`manifest.json` looks like this:

```json
{
  "id": "SETUP_001",
  "type": "setup",
  "difficulty": "easy",
  "design": "NV_NVDLA_CMAC_CORE_mac",
  "tool_version": "Innovus 21.10-p004_1",
  "analysis_views": {"setup": "functional_ss", "hold": "functional_ff"},
  "artifacts": {
    "inject_tcl": "inject.tcl",
    "fix_tcl": "fix.tcl",
    "replay_tcl": "replay.tcl",
    "metrics": "metrics.json",
    "setup_before": "reports/setup_before.rpt",
    "hold_before": "reports/hold_before.rpt",
    "setup_after": "reports/setup_after.rpt",
    "hold_after": "reports/hold_after.rpt",
    "drv_after": "reports/drv_after.rpt",
    "connectivity_after": "reports/connectivity_after.rpt",
    "drc_before": "reports/drc_before.rpt",
    "drc_after": "reports/drc_after.rpt",
    "run_log": "logs/innovus.log",
    "host_ssh_log": "logs/host_ssh.log"
  }
}
```

Artifact values may instead be `{"path": ..., "sha256": ...}` objects.
Declared checksums are verified; the canonical JSONL records the computed
SHA256 and byte count but never embeds artifact contents.

Gold manifests also declare the baseline qualification,
injection/baseline/physical audits, both host-captured SSH logs, run-status
and success markers, the
violating checkpoint (wrapper plus `.enc.dat/` tree), before-ECO netlist and
per-corner SPEFs, checkDesign output tree, replay comparison, PrimeTime
crosscheck, and the corresponding replay-2 evidence.  Directory artifacts
carry a deterministic, symlink-free file inventory and tree hash.  Validation
recomputes every declared file and directory hash, including roles not used to
compose the training messages.

Native cases additionally declare `reports/native_selected_terms.txt` and
`reports/native_cell_diff.tcl` from both replays.  The term-list bytes must be
identical across replays, contain exactly one resolved endpoint per LF-terminated
line, and match the diagnostic context in order; the concrete `optDesign`
command must name that file explicitly.

`metrics.json` uses `before` and `after` objects.  Each contains
`setup.{wns_ns,tns_ns}`, `hold.{wns_ns,tns_ns}`, three non-negative counts in
`drv`, `drc.{total,categories}`, and `connectivity.violations`.  It also
contains:

```json
{
  "replay": {"status": "passed", "runs": 2, "deterministic": true},
  "checks": {
    "constraint_hash_unchanged": true,
    "functional_audit_passed": true,
    "primetime_crosscheck_passed": true
  }
}
```

The Gold gate requires the requested before-ECO WNS/TNS to be negative,
post-ECO setup and hold WNS to be at least `+0.010 ns`, both TNS values to be
zero, no DRV/DRC regression, no remaining connectivity violation, and two
deterministic passing replays.  It requires exactly ten cases with a
4 setup / 4 hold / 2 mixed distribution: `SETUP_001` through `SETUP_004`,
`HOLD_001` through `HOLD_004`, and `MIXED_001` through `MIXED_002`.

## RL and benchmark acceptance

The authoritative outcome-based acceptance contract is
`pilot_10/benchmark_pass_criteria.json`, with a human-readable definition in
`pilot_10/BENCHMARK_PASS_CRITERIA.md`.  A candidate is not compared with the
Gold Tcl.  The trusted harness applies candidate `fix.tcl` to the frozen
violating checkpoint and `tools/check_benchmark_pass.py` evaluates the start
fingerprint, Tcl policy and cell budget, both timing directions, per-category
physical no-regression, connectivity, constraints, functional/checkDesign
evidence, replay determinism, PrimeTime, and artifact hashes.

Use `--profile rl_fast` for a one-replay training-loop feasibility reward.  It
does not count as an official pass.  `--profile official` additionally requires
two deterministic Innovus replays and independent PrimeTime max/min checks and
is the only profile that may emit `BENCHMARK_PASS`.

`answer.txt` contains two to four diagnosis sentences followed by exactly one
`tcl` fence.  That fence must match `fix.tcl` after line-ending and trailing
whitespace normalization.  The exporter prevents `inject.tcl` content from appearing in
any training message, rejects pre-injection `original_driver_inst`/
`original_driver_ref` provenance fields, and rejects constraint-relaxation
commands in the Gold fix.  Those original-driver fields may remain in the
hashed diagnostic evidence for chain-integrity audits, but are not prompt
content.

Run the dependency-free unit tests with:

```sh
python3 -m unittest datasets/timing_eco_sft/tools/test_dataset_cli.py
```

## Innovus replay execution and guest cleanup

`run_pilot.py run` keeps every guest replay directory by default.  This is the
preferred diagnostic mode because a failed Innovus process, failed artifact
fetch, or missing/wrong typed success marker leaves the remote evidence in
place:

```sh
python3 datasets/timing_eco_sft/tools/run_pilot.py run \
  --run-dir RUN \
  --replays 2 \
  --fail-fast
```

When guest space is constrained, cleanup can be enabled explicitly:

```sh
python3 datasets/timing_eco_sft/tools/run_pilot.py run \
  --run-dir RUN \
  --replays 2 \
  --fail-fast \
  --cleanup-passed-guest-replays
```

The opt-in cleanup is fail-closed.  It runs only after Innovus exits zero,
`rsync` fetch succeeds, the freshly created local replay contains the exact
case- and mode-typed success marker, and an atomic per-replay
`run_status.json` has been flushed with `fsync` together with its parent
directory.  It deletes only
`<guest-root>/<run-id>/cases/<case-id>/replay_<N>` and then tests that neither
the path nor a dangling symlink remains.  To handle staged bundles containing
`0555` directories and `0444` files, it first grants owner write/search only
to real directories inside that exact leaf, using a non-symlink-following,
single-filesystem `find`; deletion is also confined to one filesystem.  A
symlink/non-directory leaf is rejected.  Unsafe/noncanonical roots, run IDs,
case IDs, and replay indices are rejected before any remote cleanup command.
An ineligible replay is never touched.  A permission, deletion, or
absence-check failure fails the overall run and is recorded in both the local
replay status and the top-level run status; already fetched local evidence is
retained.

## PrimeTime crosscheck

`pilot_10/templates/primetime_crosscheck.tcl` analyzes one corner per fresh
PrimeTime process.  Setup must use the setup-view SDC, SS Liberty, and
setup-view SPEF; hold must use the independently exported hold-view SDC, FF
Liberty, and hold-view SPEF.  Keeping those runs separate prevents a
constraint, library, or parasitic corner from leaking into the other
analysis.  Before either process starts, the runner verifies that the selected
Tcl is byte-identical to the checked-in `DEFAULT_TCL`, then copies it with the
netlist, both SDCs, libraries, and SPEFs into a content-addressed read-only directory
below `CASE/inputs/`.  The raw Innovus SDCs remain byte-identical to the replay
artifacts.  For PrimeTime only, the v4 runner creates one deterministic
`.pt.sdc` per mode by prefixing the unique exact `current_design <top>` line
with `# PT_DIALECT_ADAPTER: `; every other byte and line ending is preserved.
Each process binds only its mode-specific adapted SDC, while the summary binds
that file back to its raw source by path, SHA256, byte count, and transformed
line number.  Inherited `PT_*` variables are removed so a stale
`PT_CONFIG_TCL` cannot override them.  Every PrimeTime child uses `-no_init`.
Local runs set both `HOME` and the working directory to the case output root;
remote runs use invocation-private `home/` and `work/` directories.  The v4
summary records and verifies the exact executable, command, isolation paths,
path-count limits, and approved Tcl identity.
The runner must be invoked where `pt_shell` and all input paths are visible
(local mode):

```sh
python3 datasets/timing_eco_sft/tools/primetime_crosscheck.py run \
  --top NV_NVDLA_CMAC_CORE_mac \
  --netlist CASE/fixed.v \
  --setup-sdc CASE/reports/constraint_setup_after.sdc \
  --hold-sdc CASE/reports/constraint_hold_after.sdc \
  --setup-lib TECH/ss.lib \
  --hold-lib TECH/ff.lib \
  --setup-spef CASE/setup.spef \
  --hold-spef CASE/hold.spef \
  --synopsys-lc-root /opt/synopsys/lc/R-2020.09-SP3 \
  --output-dir CASE
```

The deprecated single-file `--sdc` option is retained only to produce an
actionable error; it cannot produce Gold evidence.  The two SDC arguments
must name distinct source artifacts (their bytes may legitimately be equal).

If the EDA guest does not provide Python, run the same command on the host and
add an SSH target.  Input paths and `--output-dir` are then host paths, while
`--pt-shell` is resolved on the guest:

```sh
python3 datasets/timing_eco_sft/tools/primetime_crosscheck.py run \
  --ssh-target qingteng-fc \
  --remote-root /home/host/nvdla_timing_eco_sft/primetime \
  --top NV_NVDLA_CMAC_CORE_mac \
  --netlist CASE/fixed.v \
  --setup-sdc CASE/reports/constraint_setup_after.sdc \
  --hold-sdc CASE/reports/constraint_hold_after.sdc \
  --setup-lib TECH/ss.lib \
  --hold-lib TECH/ff.lib \
  --setup-spef CASE/setup.spef \
  --hold-spef CASE/hold.spef \
  --synopsys-lc-root /opt/synopsys/lc/R-2020.09-SP3 \
  --output-dir CASE
```

Remote mode uploads exactly one content-addressed bundle with `rsync`, creates
a new random invocation directory without deleting or reusing an existing
one, runs setup and hold in separate SSH/PT processes, and fetches their
reports and logs before building the same v4 summary.  The remote PT child is
started with `env -i`, a small runtime/license allowlist, explicit `PT_*`
values, and the exact `SYNOPSYS_LC_ROOT` selected by
`--synopsys-lc-root`.  `--ssh-executable` and `--rsync-executable` can select
alternate local clients; SSH config should carry ports and identity settings.

The default executable and required version are
`/opt/synopsys/prime/R-2020.09-SP4/bin/pt_shell` and `R-2020.09-SP4`.
Successful analysis produces `reports/primetime/setup_after_pt.rpt`,
`hold_after_pt.rpt`, check/global/parasitic/coverage/unit reports, typed
`setup.result` and `hold.result` markers, logs, staged-input hashes, and
`primetime_crosscheck.json`.  The policy, both execution records, and both
typed result markers must all name the same safe absolute
`SYNOPSYS_LC_ROOT`; a mismatch fails verification.  Exit status is
0 only when both corners have WNS at least `+0.010 ns`, zero TNS, no violating
paths, propagated post-CTS clocks, readable SPEF input, and a consistent
PrimeTime version.  Timing-margin failure returns 1; execution, timeout,
missing evidence, or parse failure returns 2.
Every typed result, report, and process log is also scanned fail-closed for
fatal transcript evidence (`Error:`, `PT_CROSSCHECK_ERROR`, and the selected
fatal `LNK-003`/`LNK-005`/`LNK-043` diagnostics); a PASS marker cannot
override one of those diagnostics.

Fetched reports can instead be parsed on the host for diagnosis, provided the
output JSON is placed above the `reports/` directory so all evidence paths
remain case-relative:

```sh
python3 datasets/timing_eco_sft/tools/primetime_crosscheck.py parse \
  --report-dir CASE/reports/primetime \
  --expected-top NV_NVDLA_CMAC_CORE_mac \
  --synopsys-lc-root /opt/synopsys/lc/R-2020.09-SP3 \
  --output CASE/primetime_crosscheck.json
```

The report-only `parse` result intentionally lacks execution and staged-input
provenance and therefore cannot be merged as Gold.  `merge` accepts only the
self-contained summary produced by `run`; it verifies every report/log and
staged input, source-to-stage hashes, the exact raw-to-adapted SDC transform,
per-mode raw and adapted input bindings, LC-root consistency, and the shared
Tcl before recomputing the Gold boolean.

Run its host-side tests with:

```sh
python3 -m unittest datasets/timing_eco_sft/tools/test_primetime_crosscheck.py
```

## Gold finalization

Do not promote the provisional `metrics.json` emitted inside Innovus.  After
two fresh-database replays and the PrimeTime run have been fetched, rebuild
and gate the canonical cases on the host:

```sh
# Evidence checks only; writes nothing.
python3 datasets/timing_eco_sft/tools/finalize_pilot.py \
  --run-dir datasets/timing_eco_sft/pilot_10/work/pilot10-review-01 \
  --validate-only

python3 datasets/timing_eco_sft/tools/finalize_pilot.py \
  --run-dir datasets/timing_eco_sft/pilot_10/work/pilot10-review-01 \
  --output-root datasets/timing_eco_sft/pilot_10
```

Each `runs/replay_1` and `runs/replay_2` must contain real Innovus timing,
DRV, DRC, and connectivity reports for both before/after stages, plus
`reports/concrete_fix.tcl`, the four mode-specific
`constraint_setup_before.sdc`, `constraint_setup_after.sdc`,
`constraint_hold_before.sdc`, and `constraint_hold_after.sdc` snapshots,
`functional_audit.json`, `physical_no_regression.json`, `baseline_guard.json`,
`resolved_targets.tcl`, `injection_provenance.json`, `violating.enc` with its
non-empty `violating.enc.dat/` directory, `before.v`, setup/hold before-ECO
SPEFs, `fixed.v`, setup/hold after-ECO SPEFs, the complete
`reports/check_design_after/` directory, the Innovus log, run status, and the
typed success marker.  Each replay must also retain `logs/host_ssh.log`, the
host-side capture of SSH stdout.  Innovus version evidence remains bound to
`logs/innovus.log`, while the host log must contain exactly one full line
`SFT_CASE_PASSED <ID>` and no other `SFT_CASE_PASSED` text. Hidden provenance
must use schema
`timing_eco_injection_provenance.v1`, pass on one frozen attempt, land the
requested check inside its WNS interval, and agree with the before reports;
it is copied as evidence but never used to compose the training messages.
A functional audit uses schema `timing_eco_functional_audit.v1`,
names the case, sets `passed` to true, and has a non-empty `checks` object in
which every check passes.  The core checks are `fixed_netlist_written`,
`constraints_unchanged`, `no_illegal_eco`, `connectivity_clean`,
`check_design_completed`, `cell_budget_respected`, and
`matching_corner_spef_written`.

Gold preparation additionally requires `qualification.json` to name the
qualified SS setup and FF hold Liberty source artifacts with SHA256, byte
count, and absolute source path.  The finalizer binds that qualification hash,
the immutable baseline tree hash, catalog hash, and GNU checksum-manifest hash
through the prepared manifest, both `run_status.json` files,
`baseline_guard.json`, and `injection_provenance.json`.  A success marker by
itself is never sufficient.  The violating checkpoint wrapper must restore
its sibling data directory; before netlist/SPEFs must be structurally
non-empty; and the primary checkDesign ASCII report must contain all five
critical categories with explicit zero counts.

The finalizer does not infer zero from an empty or unknown report.  It parses
the real `report_timing`, `report_constraint -all_violators`,
`verifyConnectivity`, and `verify_drc` formats; each zero must have an
explicit tool marker.  Every DRC report must prove the fixed
`verify_drc -limit 1000000` invocation in its unique command header.  A known
cutoff/truncation marker or a total at that limit fails closed before
before/after comparison.  It requires two report sets with identical path/object
identity and physical counts; WNS/TNS may differ by at most 1 ps and each run
must independently pass every acceptance gate. Concrete fixes and resolved
targets are byte-identical. For each setup/hold SDC export, the runtime requires
exactly one literal `#  Generated on:` header and replaces only that complete
line with a fixed marker immediately after `write_sdc`; missing or duplicate
headers fail closed. Before/after SDCs must then be byte-identical, so no other
line or byte is ignored. Fixed netlists use
a normalized hash that removes volatile generated comments and canonicalizes
whitespace; both raw hashes remain in the comparison evidence, while replay
1's raw bytes are bound to the PrimeTime input.
The concrete fix must use resolved design objects, contain an ECO action plus
`refinePlace`/`ecoRoute`, and may not relax constraints.

`primetime_crosscheck.json` stays at the prepared case root.  Its typed result
markers and every declared report/log hash are reverified, and its input
hashes must bind to replay 1's `fixed.v`, setup-view and hold-view post-ECO
raw SDCs, and matching setup/hold SPEFs.  A v4 summary that omits either raw
SDC, its deterministic adapted SDC, an adapted execution binding, or the
common LC root is rejected.  Its setup/hold Liberty inputs must also
match the exact qualified SS/FF source SHA256 and byte counts recorded in the
baseline qualification; substituting another library with a compatible name
is rejected.  The finalizer-v2 canonical manifest exposes stable
`pt_input_setup_sdc_adapted` and `pt_input_hold_sdc_adapted` roles whose
path/hash/bytes must exactly match the verified v4 adaptation metadata; the
stable raw SDC roles remain bound to replay 1's post-ECO constraint snapshots.
The same manifest exposes `host_ssh_log` and `replay_2_host_ssh_log`; each is
non-empty, hash-bound to its canonical replay path, and independently checked
for the exact typed marker.
`dataset_cli.py` reruns the same PT verifier and checks these canonical
bindings before export.  Run the PrimeTime command with the prepared case root as
`--output-dir` so the summary lands where the finalizer expects it.  Only after
all gates pass does the finalizer atomically create
`pilot_10/cases/<ID>` with canonical metrics, a Chinese evidence-only
instruction, and an answer whose sole Tcl fence exactly contains
`fix.tcl`. The prepared runtime's generic fix materializer is retained as
`replay_fix_materializer.tcl`, and canonical `replay.tcl` is redirected to it;
this keeps fresh replay proof collection working while `fix.tcl` remains the
standalone concrete training answer. Existing canonical case directories are
never overwritten.

Run the finalizer tests with:

```sh
python3 -m unittest datasets/timing_eco_sft/tools/test_finalize_pilot.py
```

Run the complete host-side regression suite with:

```sh
python3 -m unittest discover -s datasets/timing_eco_sft/tools -p 'test_*.py'
```

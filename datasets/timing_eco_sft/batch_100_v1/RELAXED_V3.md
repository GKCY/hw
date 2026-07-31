# Relaxed v3 construction contract

`CASE_CATALOG.md` remains the immutable planning baseline.  Relaxed v3 is a
separate, non-signoff construction contract for the same 100 case IDs.  It is
introduced because a uniform severity target is not compatible with the
discrete drive-strength ladders available in the frozen RVT collateral.

The machine-readable planning specification is
`case_specs_relaxed_v3.json`.  It is deterministically materialized from
`case_specs.json` by `tools/relaxed_v3.py`.

## Preserved card identity

For every case, relaxed v3 preserves the original:

- ID, shape, strategy, difficulty label, hierarchy and topology labels;
- guardrail axis, protected-endpoint count and load-only-sink count;
- catalog evidence and machine topology semantics;
- per-card nominal severity and tolerance from `case_specs.json`.

The first five cards therefore retain the intended severity curriculum:

| Case | Target WNS | Tolerance |
|---|---:|---:|
| `B1_CASE_001` | -60 ps | +/-10 ps |
| `B1_CASE_002` | -80 ps | +/-12 ps |
| `B1_CASE_003` | -100 ps | +/-15 ps |
| `B1_CASE_004` | -120 ps | +/-18 ps |
| `B1_CASE_005` | -140 ps | +/-21 ps |

## Relaxed construction complexity

Each effective card:

- has exactly one target endpoint;
- uses exactly one legal, functionally equivalent combinational RVT resize;
- uses injection profile `I0`;
- permits the repair to be the exact inverse of the injected resize.

The original modification count, injection profile, direct-inverse policy,
severity and target cardinality remain recorded in each case's `relaxation`
object for auditability.

## Gates that are not relaxed

Construction must still use real Innovus evidence and must preserve:

- exact negative-endpoint matching after injection;
- target setup closure and zero setup TNS after repair;
- no new negative setup endpoint;
- DRV, DRC, connectivity, constraint and topology invariants;
- legal placement and the cell/net-delay admission thresholds;
- two independent replay checks with the existing numeric tolerance.

An unreachable discrete target is rejected as infeasible.  Its tolerance must
not be widened after observing a candidate, and its measured slack must not be
copied back into the card as a new target.

All eventual artifacts must identify this contract as
`mock_training_relaxed_v3_non_signoff`.  They must not be represented as
original-catalog signoff data or mixed with relaxed-v2 artifacts.

## Current phase

The machine-readable card remains a planning input: measured targets are not
written back into `case_specs_relaxed_v3.json`, and realized evidence is kept
outside the card.  The user subsequently authorized a first construction
slice.  `work/batch100-relaxed-v3-r1` now contains real Innovus artifacts for
`B1_CASE_002` through `B1_CASE_005`; all four are `VALIDATED` after two
independent replay slots:

| Case | Measured violating WNS | Replay slots |
|---|---:|---|
| `B1_CASE_002` | -84.9323 ps | `slot1`, `slot2` |
| `B1_CASE_003` | -96.7464 ps | `slot0`, `slot3` |
| `B1_CASE_004` | -124.089 ps | `slot1`, `slot3` |
| `B1_CASE_005` | -135.839 ps | `slot0`, `slot2` |

This is a validated partial construction run, not a finalized 100-case dataset
and not signoff data.  The one-field migration from the legacy generic
non-signoff label to the v3-specific technology classification is hash-audited
in `work/batch100-relaxed-v3-r1/relaxed_v3_reclassification_audit.json`.

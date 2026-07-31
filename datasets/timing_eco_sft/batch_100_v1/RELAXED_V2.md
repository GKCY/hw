# Relaxed v2 construction contract

`CASE_CATALOG.md` remains the immutable planning baseline.  The user authorized
this separate effective specification because the original hidden construction
profiles made batch completion impractical.

The effective machine specification is `case_specs_relaxed_v2.json`.

- Keep all 100 IDs, their shape, hierarchy, topology labels, protected-endpoint
  rules, and real-Innovus evidence requirements.  Each effective card has one
  target endpoint; the original target cardinality is retained in its
  `relaxation` record.
- Use one legal, functionally equivalent RVT resize per case.
- Permit the repair to be the exact inverse of the injected resize.
- Target 60 ps for original E cards, 80 ps for original M cards, and 100 ps
  for original H cards, each with a 10 ps tolerance.
- Do not relax exact negative-endpoint matching, physical checks,
  connectivity/topology invariants, or the two independent replay checks.

Every produced artifact must identify this contract as
`mock_training_relaxed_v2_non_signoff`; it must not be represented as original
catalog signoff data.

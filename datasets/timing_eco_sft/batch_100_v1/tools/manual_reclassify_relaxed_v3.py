#!/usr/bin/env python3
"""Normalize validated relaxed-v3 artifact metadata without changing evidence.

The original replay publisher emitted the generic non-signoff classification.
This operator changes only validation.technology_classification, revalidates
the case, refreshes the canonical artifact-tree ledger, and records an audit.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path


TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import batch100  # noqa: E402
import finalize_batch  # noqa: E402
from common import BatchError, read_json  # noqa: E402


GENERIC = "mock_training_non_signoff"
RELAXED_V3 = "mock_training_relaxed_v3_non_signoff"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--case-id", action="append", required=True)
    args = parser.parse_args()

    store, _ = batch100._verified_store(args.run_dir)
    specs = read_json(batch100.SPECS_PATH, "relaxed-v3 case specs")
    if specs.get("schema_version") != batch100.RELAXED_V3_SCHEMA:
        raise BatchError("metadata normalization requires relaxed-v3 specs")
    by_id = {case["id"]: case for case in specs["cases"]}
    case_ids = list(dict.fromkeys(args.case_id))
    if len(case_ids) != len(args.case_id):
        raise BatchError("duplicate case ID")
    audit_path = args.run_dir / "relaxed_v3_reclassification_audit.json"
    if audit_path.exists() or audit_path.is_symlink():
        raise BatchError(f"audit already exists: {audit_path}")

    records = []
    for case_id in case_ids:
        try:
            spec = by_id[case_id]
        except KeyError as exc:
            raise BatchError(f"unknown case ID: {case_id}") from exc
        if store.row(case_id)["state"] != "VALIDATED":
            raise BatchError(f"{case_id}: normalization requires VALIDATED state")
        case_root = args.run_dir / "cases" / case_id
        validation_path = case_root / "validation.json"
        original_bytes = validation_path.read_bytes()
        validation = read_json(validation_path, f"{case_id} validation")
        if validation.get("technology_classification") != GENERIC:
            raise BatchError(
                f"{case_id}: unexpected source technology classification"
            )
        state = read_json(
            args.run_dir / "state" / f"{case_id}.json",
            f"{case_id} state sidecar",
        )
        artifacts = {
            artifact["logical_name"]: artifact
            for artifact in state.get("artifacts", [])
        }
        try:
            recorded = artifacts["validated_case_tree"]
        except KeyError as exc:
            raise BatchError(f"{case_id}: validated tree ledger is missing") from exc
        old_tree = finalize_batch.case_artifact_tree_sha256(case_root)
        if recorded["sha256"] != old_tree:
            raise BatchError(f"{case_id}: pre-normalization tree ledger mismatch")
        # Prove the complete published artifact with the strict validator
        # before changing the one legacy classification field.
        finalize_batch.validate_case(case_root, spec, specs["acceptance"])

        validation["technology_classification"] = RELAXED_V3
        try:
            batch100.atomic_json(validation_path, validation, durable=True)
            new_tree = finalize_batch.case_artifact_tree_sha256(case_root)
            retained_bytes = sum(
                path.stat().st_size
                for path in case_root.rglob("*")
                if not path.is_symlink() and path.is_file()
            )
            store.register_artifact(
                case_id,
                "validated_case_tree",
                str(case_root.relative_to(args.run_dir)),
                new_tree,
                retained_bytes,
            )
        except Exception:
            batch100.atomic_write(validation_path, original_bytes, durable=True)
            store.register_artifact(
                case_id,
                "validated_case_tree",
                recorded["relative_path"],
                old_tree,
                int(recorded["bytes"]),
            )
            raise
        records.append(
            {
                "case_id": case_id,
                "field": "validation.technology_classification",
                "old_value": GENERIC,
                "new_value": RELAXED_V3,
                "old_validation_sha256": _sha256_bytes(original_bytes),
                "new_validation_sha256": batch100.sha256_file(validation_path),
                "old_case_tree_sha256": old_tree,
                "new_case_tree_sha256": new_tree,
            }
        )

    batch100.atomic_json(
        audit_path,
        {
            "schema_version": "mock_lef_batch100.relaxed_v3_reclassification.v1",
            "run_id": batch100._run_id(args.run_dir),
            "changed_field_only": "validation.technology_classification",
            "records": records,
        },
        durable=True,
    )
    print(
        f"RELAXED_V3_RECLASSIFY_PASS cases={','.join(case_ids)} "
        f"audit={audit_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

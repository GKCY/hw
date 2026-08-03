#!/usr/bin/env python3
"""Hash-audited presentation-only migration to oracle-safe Instruction v2."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Mapping

import evidence
import finalize_batch
import instruction
from common import (
    BatchError,
    atomic_json,
    atomic_write,
    read_json,
    require_mapping,
    require_real_directory,
    require_real_file,
    sha256_file,
)
from state_store import StateStore


AUDIT_SCHEMA = "mock_lef_batch100.instruction_v2_migration.v1"


def _specs(path: Path) -> dict[str, Mapping[str, Any]]:
    payload = require_mapping(read_json(path, "case specs"), "case specs")
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise BatchError("case specs lacks cases array")
    result: dict[str, Mapping[str, Any]] = {}
    for value in cases:
        case = require_mapping(value, "case spec")
        case_id = case.get("id")
        if not isinstance(case_id, str) or case_id in result:
            raise BatchError("case specs contains malformed/duplicate ID")
        result[case_id] = case
    return result


def _retained_bytes(root: Path) -> int:
    return sum(
        path.stat().st_size
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    )


def migrate(
    run_dir: Path,
    specs_path: Path,
    case_ids: list[str],
    *,
    audit_file: str = "instruction_v2_migration_audit.json",
) -> dict[str, Any]:
    run_dir = require_real_directory(run_dir, "run directory")
    by_id = _specs(require_real_file(specs_path, "case specs"))
    if not case_ids or len(set(case_ids)) != len(case_ids):
        raise BatchError("migration requires unique case IDs")
    store = StateStore(run_dir)
    if (
        not audit_file.endswith(".json")
        or "/" in audit_file
        or "\\" in audit_file
        or audit_file in {".json", "..json"}
    ):
        raise BatchError("migration audit file must be a plain JSON filename")
    audit_path = run_dir / audit_file
    if audit_path.exists() or audit_path.is_symlink():
        raise BatchError(f"refusing to overwrite migration audit: {audit_path}")

    prepared: list[dict[str, Any]] = []
    for case_id in case_ids:
        spec = by_id.get(case_id)
        if spec is None:
            raise BatchError(f"case ID is absent from selected specs: {case_id}")
        if store.row(case_id)["state"] != "VALIDATED":
            raise BatchError(f"{case_id}: instruction migration requires VALIDATED state")
        case_root = require_real_directory(
            run_dir / "cases" / case_id, f"{case_id} case tree"
        )
        validation = require_mapping(
            read_json(case_root / "validation.json", "validation"),
            f"{case_id}.validation",
        )
        replays = validation.get("replays")
        if not isinstance(replays, list) or len(replays) != 2:
            raise BatchError(f"{case_id}: migration requires two replay records")
        observable = require_mapping(replays[0].get("before"), f"{case_id}.before")
        report_paths = [
            require_real_file(
                case_root
                / "evidence"
                / f"replay_{index}"
                / "reports"
                / "target_setup_before.rpt",
                f"{case_id} replay {index} target report",
            )
            for index in (1, 2)
        ]
        path_evidence = [
            evidence.observable_target_path_evidence(path) for path in report_paths
        ]
        evidence.compare_observable_path_evidence(path_evidence[0], path_evidence[1])
        new_instruction = instruction.render_instruction(
            case_id=case_id,
            expected_modification_count=spec["expected_modification_count"],
            observable=observable,
            path_evidence=path_evidence[0],
        )
        fix_text = require_real_file(case_root / "fix.tcl", "fix Tcl").read_text(
            encoding="utf-8"
        )
        new_answer = instruction.render_answer(fix_text)
        instruction_path = require_real_file(
            case_root / "instruction.txt", "instruction"
        )
        answer_path = require_real_file(case_root / "answer.txt", "answer")
        old_tree_hash = finalize_batch.case_artifact_tree_sha256(case_root)
        sidecar = require_mapping(
            read_json(run_dir / "state" / f"{case_id}.json", "state sidecar"),
            f"{case_id}.state",
        )
        artifacts = sidecar.get("artifacts")
        if not isinstance(artifacts, list):
            raise BatchError(f"{case_id}: state sidecar lacks artifacts")
        artifact = next(
            (
                item
                for item in artifacts
                if isinstance(item, Mapping)
                and item.get("logical_name") == "validated_case_tree"
            ),
            None,
        )
        if artifact is None or artifact.get("sha256") != old_tree_hash:
            raise BatchError(f"{case_id}: current case tree is not state-bound")
        prepared.append(
            {
                "case_id": case_id,
                "case_root": case_root,
                "instruction_path": instruction_path,
                "answer_path": answer_path,
                "old_instruction_sha256": sha256_file(instruction_path),
                "old_answer_sha256": sha256_file(answer_path),
                "old_case_tree_sha256": old_tree_hash,
                "new_instruction": new_instruction,
                "new_answer": new_answer,
                "observable_report_sha256": [sha256_file(path) for path in report_paths],
            }
        )

    records: list[dict[str, Any]] = []
    for row in prepared:
        atomic_write(
            row["instruction_path"],
            row["new_instruction"].encode("utf-8"),
            durable=True,
        )
        atomic_write(
            row["answer_path"],
            row["new_answer"].encode("utf-8"),
            durable=True,
        )
        new_tree_hash = finalize_batch.case_artifact_tree_sha256(row["case_root"])
        store.register_artifact(
            row["case_id"],
            "validated_case_tree",
            str(row["case_root"].relative_to(run_dir)),
            new_tree_hash,
            _retained_bytes(row["case_root"]),
        )
        records.append(
            {
                "case_id": row["case_id"],
                "changed_fields_only": ["instruction.txt", "answer.txt"],
                "source_evidence": "dual_replay_violating_checkpoint_reports",
                "source_report_sha256": row["observable_report_sha256"],
                "old_instruction_sha256": row["old_instruction_sha256"],
                "new_instruction_sha256": sha256_file(row["instruction_path"]),
                "old_answer_sha256": row["old_answer_sha256"],
                "new_answer_sha256": sha256_file(row["answer_path"]),
                "old_case_tree_sha256": row["old_case_tree_sha256"],
                "new_case_tree_sha256": new_tree_hash,
            }
        )

    audit = {
        "schema_version": AUDIT_SCHEMA,
        "run_id": run_dir.name,
        "policy": "observable-evidence-only; no construction plan, binding, or fix input to prompt renderer",
        "records": records,
    }
    atomic_json(audit_path, audit, durable=True)
    return audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--specs", type=Path, required=True)
    parser.add_argument("--case-id", action="append", required=True)
    parser.add_argument(
        "--audit-file", default="instruction_v2_migration_audit.json"
    )
    args = parser.parse_args()
    audit = migrate(
        args.run_dir,
        args.specs,
        args.case_id,
        audit_file=args.audit_file,
    )
    print(
        f"instruction-v2 migration PASS: {len(audit['records'])} case(s); "
        f"run={audit['run_id']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

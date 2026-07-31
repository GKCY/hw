#!/usr/bin/env python3
"""Build and validate the planning-only relaxed-v3 Batch-100 specification."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping


HERE = Path(__file__).resolve()
ROOT = HERE.parents[1]
CATALOG = ROOT / "CASE_CATALOG.md"
BASE_SPECS = ROOT / "case_specs.json"
V2_CONTRACT = ROOT / "RELAXED_V2.md"
V2_SPECS = ROOT / "case_specs_relaxed_v2.json"
V3_CONTRACT = ROOT / "RELAXED_V3.md"
V3_SPECS = ROOT / "case_specs_relaxed_v3.json"

SCHEMA = "mock_lef_batch100.case_specs.relaxed_v3"
CLASSIFICATION = "mock_training_relaxed_v3_non_signoff"
PLANNING_STATUS = "relaxed_v3_unvalidated"
TOPOLOGY_TAG = "relaxed_v3_direct_inverse"

PRESERVED_GATES = [
    "real_innovus",
    "exact_negative_endpoint_set",
    "setup_tns_zero_after_repair",
    "no_new_negative_endpoint",
    "drv_drc_connectivity_placement_no_regression",
    "dual_independent_replay",
]
REALIZED_CASE_KEYS = {
    "binding",
    "calibration",
    "checkpoint",
    "measurement",
    "metrics",
    "replay",
    "validation",
}


class RelaxedV3Error(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_mapping(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RelaxedV3Error(f"cannot read JSON mapping {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RelaxedV3Error(f"expected JSON object: {path}")
    return value


def _original_relaxation(case: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "difficulty": case["difficulty"],
        "expected_modification_count": case["expected_modification_count"],
        "injection_profile": case["injection_profile"],
        "direct_inverse_allowed": case["direct_inverse_allowed"],
        "nominal_severity": copy.deepcopy(case["nominal_severity"]),
        "target_endpoint_count": case["target_endpoint_count"],
    }


def build_specs() -> dict[str, Any]:
    """Materialize v3 without importing any measured work-tree evidence."""

    base = _read_mapping(BASE_SPECS)
    cases = base.get("cases")
    if not isinstance(cases, list) or len(cases) != 100:
        raise RelaxedV3Error("base specification must contain exactly 100 cases")

    specs = copy.deepcopy(base)
    specs["schema_version"] = SCHEMA
    specs["source_catalog"] = {
        **copy.deepcopy(base["source_catalog"]),
        "baseline_case_specs_sha256": _sha256(BASE_SPECS),
        "predecessor_contract": {
            "path": V2_CONTRACT.name,
            "sha256": _sha256(V2_CONTRACT),
        },
        "predecessor_case_specs": {
            "path": V2_SPECS.name,
            "sha256": _sha256(V2_SPECS),
        },
    }
    specs["design"]["technology_classification"] = CLASSIFICATION

    relaxed_cases: list[dict[str, Any]] = []
    for source in cases:
        case = copy.deepcopy(source)
        case["planning_status"] = PLANNING_STATUS
        case["expected_modification_count"] = 1
        case["target_endpoint_count"] = 1
        case["injection_profile"] = "I0"
        case["direct_inverse_allowed"] = True
        case["topology_tags"] = [
            tag for tag in source["topology_tags"] if not tag.startswith("relaxed_v")
        ] + [TOPOLOGY_TAG]
        case["relaxation"] = _original_relaxation(source)
        relaxed_cases.append(case)
    specs["cases"] = relaxed_cases
    specs["relaxation"] = {
        "authorized_by_user": True,
        "scope": "construction_complexity_only",
        "current_phase": "planning_specification_only",
        "data_generation_performed": False,
        "preserved_gates": list(PRESERVED_GATES),
        "changes": {
            "direct_inverse_allowed_for_all_profiles": True,
            "expected_modification_count": 1,
            "injection_profile": "I0",
            "target_endpoint_count": 1,
            "nominal_severity_policy": "preserve_each_original_case",
            "severity_tolerance_policy": "preserve_each_original_case",
            "preserve_original_contract_in_case_relaxation": True,
        },
    }
    return specs


def validate_specs(specs: Mapping[str, Any]) -> None:
    """Fail closed if a checked-in v3 card differs from its declared source."""

    expected = build_specs()
    if dict(specs) != expected:
        raise RelaxedV3Error(
            "relaxed-v3 specs differ from deterministic case_specs.json materialization"
        )
    cases = specs["cases"]
    expected_ids = [f"B1_CASE_{index:03d}" for index in range(1, 101)]
    if [case["id"] for case in cases] != expected_ids:
        raise RelaxedV3Error("case IDs are not exactly B1_CASE_001..100 in order")
    if specs["relaxation"]["data_generation_performed"] is not False:
        raise RelaxedV3Error("planning card must not claim that data was generated")
    for case in cases:
        realized = REALIZED_CASE_KEYS.intersection(case)
        if realized:
            raise RelaxedV3Error(
                f"{case['id']}: planning card contains realized data keys "
                f"{sorted(realized)}"
            )
        if (
            case["planning_status"] != PLANNING_STATUS
            or case["expected_modification_count"] != 1
            or case["target_endpoint_count"] != 1
            or case["injection_profile"] != "I0"
            or case["direct_inverse_allowed"] is not True
            or case["topology_tags"].count(TOPOLOGY_TAG) != 1
        ):
            raise RelaxedV3Error(f"{case['id']}: invalid relaxed-v3 construction card")


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _summary(specs: Mapping[str, Any]) -> str:
    first_five = ", ".join(
        f"{case['id']}={case['nominal_severity']['ps']}ps"
        for case in specs["cases"][:5]
    )
    return (
        f"RELAXED_V3_OK cases={len(specs['cases'])} "
        f"classification={CLASSIFICATION} first_five=[{first_five}] "
        "data_generated=false"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--output", type=Path, default=V3_SPECS)
    build_parser.add_argument("--force", action="store_true")
    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--input", type=Path, default=V3_SPECS)
    args = parser.parse_args()

    try:
        if args.command == "build":
            if (args.output.exists() or args.output.is_symlink()) and not args.force:
                raise RelaxedV3Error(
                    f"output already exists; use --force to replace it: {args.output}"
                )
            specs = build_specs()
            validate_specs(specs)
            _atomic_write_json(args.output, specs)
        else:
            specs = _read_mapping(args.input)
            validate_specs(specs)
    except RelaxedV3Error as exc:
        print(f"RELAXED_V3_ERROR: {exc}", file=sys.stderr)
        return 1
    print(_summary(specs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

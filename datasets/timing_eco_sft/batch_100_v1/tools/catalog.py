#!/usr/bin/env python3
"""Materialize and validate the Batch-100 planning catalog.

Natural-language parsing is deliberately confined to this developer utility.
The runtime consumes only the checked-in ``case_specs.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


HERE = Path(__file__).resolve()
ROOT = HERE.parents[1]
CATALOG = ROOT / "CASE_CATALOG.md"
SPECS = ROOT / "case_specs.json"

CANARY = (
    "B1_CASE_001",
    "B1_CASE_033",
    "B1_CASE_051",
    "B1_CASE_055",
    "B1_CASE_059",
    "B1_CASE_070",
    "B1_CASE_077",
    "B1_CASE_079",
    "B1_CASE_082",
    "B1_CASE_091",
    "B1_CASE_096",
    "B1_CASE_100",
)
I0_CASES = {
    "B1_CASE_001",
    "B1_CASE_005",
    "B1_CASE_014",
    "B1_CASE_025",
    "B1_CASE_036",
    "B1_CASE_056",
    "B1_CASE_064",
    "B1_CASE_081",
    "B1_CASE_086",
    "B1_CASE_093",
}
SHAPE_NAMES = {
    "单关键锥": "single_cone",
    "多 endpoint": "multi_endpoint",
    "Setup/DRV guardrail": "setup_drv_guardrail",
}
PROTECTED_ENDPOINTS = {
    "B1_CASE_009": 1,
    "B1_CASE_043": 1,
    "B1_CASE_044": 1,
    "B1_CASE_045": 2,
    "B1_CASE_046": 1,
    "B1_CASE_047": 2,
    "B1_CASE_050": 1,
    "B1_CASE_051": 2,
    "B1_CASE_053": 2,
    "B1_CASE_055": 1,
    "B1_CASE_057": 1,
    "B1_CASE_065": 1,
    "B1_CASE_074": 1,
    "B1_CASE_075": 2,
    "B1_CASE_081": 1,
    "B1_CASE_082": 1,
    "B1_CASE_083": 1,
    "B1_CASE_086": 1,
    "B1_CASE_087": 1,
    "B1_CASE_088": 1,
    "B1_CASE_093": 1,
    "B1_CASE_094": 1,
    "B1_CASE_097": 1,
    "B1_CASE_098": 1,
}
LOAD_ONLY_SINKS = {"B1_CASE_004": 1}
EXPECTED_CROSS = {
    ("single_cone", "A", "E"): 13,
    ("single_cone", "A", "M"): 7,
    ("single_cone", "A", "H"): 2,
    ("single_cone", "B", "E"): 10,
    ("single_cone", "B", "M"): 9,
    ("single_cone", "B", "H"): 1,
    ("single_cone", "C", "E"): 2,
    ("single_cone", "C", "M"): 6,
    ("single_cone", "C", "H"): 2,
    ("single_cone", "D", "E"): 0,
    ("single_cone", "D", "M"): 2,
    ("single_cone", "D", "H"): 1,
    ("multi_endpoint", "A", "E"): 3,
    ("multi_endpoint", "A", "M"): 4,
    ("multi_endpoint", "A", "H"): 1,
    ("multi_endpoint", "B", "E"): 2,
    ("multi_endpoint", "B", "M"): 4,
    ("multi_endpoint", "B", "H"): 2,
    ("multi_endpoint", "C", "E"): 1,
    ("multi_endpoint", "C", "M"): 3,
    ("multi_endpoint", "C", "H"): 2,
    ("multi_endpoint", "D", "E"): 0,
    ("multi_endpoint", "D", "M"): 2,
    ("multi_endpoint", "D", "H"): 1,
    ("setup_drv_guardrail", "A", "E"): 2,
    ("setup_drv_guardrail", "A", "M"): 3,
    ("setup_drv_guardrail", "A", "H"): 0,
    ("setup_drv_guardrail", "B", "E"): 1,
    ("setup_drv_guardrail", "B", "M"): 4,
    ("setup_drv_guardrail", "B", "H"): 2,
    ("setup_drv_guardrail", "C", "E"): 1,
    ("setup_drv_guardrail", "C", "M"): 2,
    ("setup_drv_guardrail", "C", "H"): 1,
    ("setup_drv_guardrail", "D", "E"): 0,
    ("setup_drv_guardrail", "D", "M"): 4,
    ("setup_drv_guardrail", "D", "H"): 0,
}


class CatalogError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _topology_tags(text: str, shape: str, strategy: str) -> list[str]:
    tags = {shape, f"strategy_{strategy.lower()}"}
    vocabulary = {
        "capture_near": ("capture 近端", "capture 前", "末级", "末端"),
        "launch_near": ("launch 近端", "launch 后"),
        "reconvergence": ("reconverge", "reconvergence", "汇合"),
        "fork": ("分叉", "分支", "旁支", "旁路"),
        "shared_driver": ("共享驱动", "公共瓶颈", "共享高负载"),
        "slew_chain": ("slew-chain", "slew chain", "链首", "深链"),
        "high_load": ("高负载", "负载"),
        "input_cap_tradeoff": ("输入电容", "pin cap", "capacitance"),
        "transition_guardrail": ("max transition", "transition 限值"),
        "capacitance_guardrail": ("max capacitance", "capacitance 限值"),
        "protected_setup": ("非目标 endpoint", "非目标 setup", "非目标路径"),
        "multi_input_arc": ("多输入", "input pin", "慢输入"),
    }
    lower = text.lower()
    for tag, needles in vocabulary.items():
        if any(needle.lower() in lower for needle in needles):
            tags.add(tag)
    return sorted(tags)


def _blocks(text: str) -> Iterable[tuple[str, str, str, str]]:
    pattern = re.compile(
        r"^#### (B1_CASE_\d{3})\n\n"
        r"- 元数据：(.+?)\n"
        r"- 场景：(.+?)\n"
        r"- 安全解与检查：(.+?)$",
        re.MULTILINE,
    )
    yield from (match.groups() for match in pattern.finditer(text))


def parse_catalog() -> list[dict[str, Any]]:
    text = CATALOG.read_text(encoding="utf-8")
    blocks = list(_blocks(text))
    if len(blocks) != 100:
        raise CatalogError(f"expected 100 case cards, found {len(blocks)}")
    cases: list[dict[str, Any]] = []
    for case_id, metadata, scenario, safe_solution in blocks:
        parts = metadata.removesuffix("。").split("；")
        if len(parts) < 10 or parts[0] != "`planned_unvalidated`":
            raise CatalogError(f"{case_id}: malformed metadata line")
        shape_text = parts[1]
        if shape_text not in SHAPE_NAMES:
            raise CatalogError(f"{case_id}: unknown shape {shape_text!r}")
        strategy_match = re.fullmatch(r"策略 ([A-D])", parts[2])
        count_match = re.fullmatch(r"预计 ([1-6]) 个有效修改", parts[4])
        hierarchy_match = re.fullmatch(r"候选层级 `(exp|pipeline|top_tree)`", parts[5])
        endpoint_match = re.fullmatch(r"(\d+) 个 ?(?:目标 )?endpoint", parts[6])
        injection_match = re.search(r"隐藏构造：(I0|I[ABCD])", metadata)
        inverse_match = re.search(r"直接逆操作：(允许|不允许)", metadata)
        if not all(
            (strategy_match, count_match, hierarchy_match, endpoint_match,
             injection_match, inverse_match)
        ):
            raise CatalogError(f"{case_id}: cannot parse structured metadata")
        difficulty = parts[3]
        if difficulty not in {"E", "M", "H"}:
            raise CatalogError(f"{case_id}: invalid difficulty {difficulty!r}")
        severities = [
            (float(percent), int(ps))
            for percent, ps in re.findall(
                r"(\d+(?:\.\d+)?)%\s*/\s*(\d+) ps", scenario
            )
        ]
        if not severities:
            raise CatalogError(f"{case_id}: no nominal severity")
        percent, severity_ps = max(severities, key=lambda pair: pair[1])
        expected_ps = percent * 80.0
        if abs(expected_ps - severity_ps) > 0.001:
            raise CatalogError(
                f"{case_id}: {percent}% of 8 ns is not {severity_ps} ps"
            )
        shape = SHAPE_NAMES[shape_text]
        strategy = strategy_match.group(1)
        guardrail_axis = "none"
        guardrail = re.search(r"guardrail 轴：([^；]+)", metadata)
        if guardrail:
            raw = guardrail.group(1)
            guardrail_axis = {
                "防止恶化非目标 setup endpoint": "protected_setup_endpoint",
                "max transition 风险": "max_transition",
                "max capacitance 风险": "max_capacitance",
            }.get(raw, raw)
        case = {
            "id": case_id,
            "planning_status": "planned_unvalidated",
            "shape": shape,
            "strategy": strategy,
            "difficulty": difficulty,
            "expected_modification_count": int(count_match.group(1)),
            "hierarchy": hierarchy_match.group(1),
            "target_endpoint_count": int(endpoint_match.group(1)),
            "protected_endpoint_count": PROTECTED_ENDPOINTS.get(case_id, 0),
            "load_only_sink_count": LOAD_ONLY_SINKS.get(case_id, 0),
            "topology_tags": _topology_tags(
                f"{metadata} {scenario} {safe_solution}", shape, strategy
            ),
            "nominal_severity": {
                "percent_of_clock": percent,
                "ps": severity_ps,
                "target_wns_ns": -severity_ps / 1000.0,
                "tolerance_ps": max(10.0, severity_ps * 0.15),
            },
            "injection_profile": injection_match.group(1),
            "direct_inverse_allowed": inverse_match.group(1) == "允许",
            "guardrail_axis": guardrail_axis,
            "catalog_evidence": {
                "metadata": metadata,
                "scenario": scenario,
                "safe_solution": safe_solution,
            },
            "machine_semantics": {
                "target_endpoint_set_exact": True,
                "protected_endpoint_baseline_drop_max_ps": 1.0,
                "topology_must_remain_unchanged": True,
            },
        }
        if case_id == "B1_CASE_004":
            case["machine_semantics"]["side_branch"] = (
                "one load-only sink; it is not an endpoint"
            )
        if case_id in {"B1_CASE_009", "B1_CASE_055"}:
            case["machine_semantics"]["side_branch"] = (
                "exactly one positive-slack protected non-target endpoint"
            )
        cases.append(case)
    return cases


def build_specs() -> dict[str, Any]:
    return {
        "schema_version": "mock_lef_batch100.case_specs.v1",
        "source_catalog": {
            "path": "CASE_CATALOG.md",
            "sha256": sha256(CATALOG),
            "status": "planning_baseline_never_signoff",
        },
        "design": {
            "top": "NV_NVDLA_CMAC_CORE_mac",
            "tool": "Cadence Innovus",
            "tool_version": "21.10-p004_1",
            "setup_view": "functional_setup_ss",
            "hold_view": "functional_hold_ff",
            "clock_period_ns": 8.0,
            "technology_classification": "mock_training_non_signoff",
            "signoff_qualified": False,
        },
        "policy": {
            "allowed_commands": ["ecoChangeCell", "refinePlace -eco true"],
            "forbidden_commands": [
                "ecoAddRepeater",
                "addInst",
                "addNet",
                "ecoRoute",
                "routeDesign",
                "optDesign",
                "ccopt_design",
                "clockDesign",
                "set_clock",
                "create_clock",
                "set_false_path",
                "set_multicycle_path",
                "set_disable_timing",
            ],
            "allowed_cells": "functionally equivalent combinational RVT only",
            "forbidden_object_classes": [
                "sequential",
                "clock_tree",
                "clock_gating",
                "macro",
                "dont_touch",
            ],
            "hold_gate_applied": False,
        },
        "acceptance": {
            "cell_delay_fraction_min": 0.70,
            "net_delay_fraction_max": 0.30,
            "repair_sensitivity_min_ps": 1.0,
            "replay_numeric_tolerance_ps": 1.0,
            "protected_slack_drop_max_ps": 1.0,
            "setup_wns_min_ns": 0.0,
            "setup_tns_ns": 0.0,
            "drv_must_not_regress": True,
            "drc_must_not_regress": True,
            "connectivity_must_not_regress": True,
            "constraint_hash_must_match": True,
            "placement_must_be_legal": True,
            "hold_is_observation_only": True,
        },
        "state_machine": [
            "PLANNED",
            "PROBE_ELIGIBLE",
            "BOUND",
            "CALIBRATING",
            "FROZEN",
            "REPLAYING",
            "VALIDATED",
            "FINALIZED",
        ],
        "canary_case_ids": list(CANARY),
        "cases": parse_catalog(),
    }


def _distribution(cases: Iterable[dict[str, Any]]) -> Counter[tuple[str, str, str]]:
    return Counter((c["shape"], c["strategy"], c["difficulty"]) for c in cases)


def validate_specs(specs: dict[str, Any]) -> None:
    expected = build_specs()
    if specs != expected:
        raise CatalogError(
            "case_specs.json differs from the deterministic catalog materialization"
        )
    cases = specs["cases"]
    ids = [case["id"] for case in cases]
    expected_ids = [f"B1_CASE_{index:03d}" for index in range(1, 101)]
    if ids != expected_ids:
        raise CatalogError("case IDs are not exactly B1_CASE_001..100 in order")
    if _distribution(cases) != Counter(EXPECTED_CROSS):
        raise CatalogError("shape/strategy/difficulty distribution differs from catalog")
    if Counter(c["shape"] for c in cases) != {
        "single_cone": 55,
        "multi_endpoint": 25,
        "setup_drv_guardrail": 20,
    }:
        raise CatalogError("shape totals differ from 55/25/20")
    if Counter(c["strategy"] for c in cases) != {"A": 35, "B": 35, "C": 20, "D": 10}:
        raise CatalogError("strategy totals differ from 35/35/20/10")
    if Counter(c["difficulty"] for c in cases) != {"E": 35, "M": 50, "H": 15}:
        raise CatalogError("difficulty totals differ from 35/50/15")
    actual_i0 = {c["id"] for c in cases if c["injection_profile"] == "I0"}
    if actual_i0 != I0_CASES:
        raise CatalogError(f"I0 set mismatch: {sorted(actual_i0 ^ I0_CASES)}")
    ranges = {"E": range(1, 3), "M": range(2, 5), "H": range(4, 7)}
    for case in cases:
        if case["expected_modification_count"] not in ranges[case["difficulty"]]:
            raise CatalogError(f"{case['id']}: modification count outside difficulty")
        ps = case["nominal_severity"]["ps"]
        if not (
            (case["difficulty"] == "E" and 40 <= ps <= 160)
            or (case["difficulty"] == "M" and 160 < ps <= 400)
            or (case["difficulty"] == "H" and 400 < ps <= 640)
        ):
            raise CatalogError(f"{case['id']}: severity outside difficulty band")
        if (case["injection_profile"] == "I0") != case["direct_inverse_allowed"]:
            raise CatalogError(f"{case['id']}: direct-inverse/I0 mismatch")
    by_id = {case["id"]: case for case in cases}
    if by_id["B1_CASE_004"]["load_only_sink_count"] != 1:
        raise CatalogError("004 must have exactly one load-only, non-endpoint sink")
    for case_id in ("B1_CASE_009", "B1_CASE_055"):
        if by_id[case_id]["protected_endpoint_count"] != 1:
            raise CatalogError(f"{case_id}: must have one protected endpoint")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("write-specs", "check"))
    args = parser.parse_args(argv)
    try:
        if args.command == "write-specs":
            SPECS.write_text(
                json.dumps(build_specs(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"wrote {SPECS}")
        else:
            validate_specs(json.loads(SPECS.read_text(encoding="utf-8")))
            print("catalog-check PASS: 100 deterministic case specs")
    except (CatalogError, OSError, json.JSONDecodeError) as exc:
        print(f"catalog-check FAIL: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

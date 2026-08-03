#!/usr/bin/env python3
"""Render evidence-complete Batch-100 prompts without hidden construction data."""

from __future__ import annotations

import math
import re
from typing import Any, Mapping

from common import BatchError, SAFE_ID_RE, require_mapping, require_number


SYSTEM_PROMPT = (
    "你是一名资深 Cadence Innovus 21.10 物理设计工程师。请仅依据用户消息中"
    "列出的 violating checkpoint 可观察证据诊断 setup 违例，并给出最小化、"
    "可重放的 RVT 组合逻辑 resize ECO。除注释和空行外，可执行命令严格限于一对 "
    "setEcoMode -batchMode true/false、题面规定数量的 ecoChangeCell，以及末尾唯一一次 "
    "refinePlace -eco true；不得修改约束、时钟、顺序单元、clock-gating、macro、连接或路由。"
)

_FORBIDDEN_DISCLOSURE = re.compile(
    r"(?ix)(?:"
    r"\binject(?:ion|ed|ing)?\b|\bhidden\b|\boracle\b|\bgold\b|"
    r"\bcalibration(?:_plan)?\b|\bbinding\b|"
    r"\bbaseline[_ -]?ref\b|\bcheckpoint[_ -]?ref\b|"
    r"\bdirect[_ -]?inverse\b|\bexact[_ -]?inverse\b|"
    r"\b(?:injection|repair)_operations\b|"
    r"注入|隐藏|金标|答案实例|直接逆操作|精确逆操作"
    r")"
)


def assert_oracle_safe(text: str) -> None:
    """Reject construction-side vocabulary from model-visible text."""

    match = _FORBIDDEN_DISCLOSURE.search(text)
    if match is not None:
        raise BatchError(
            "model-visible text contains construction-side disclosure marker: "
            f"{match.group(0)!r}"
        )


def _finite(value: Any, label: str) -> float:
    number = require_number(value, label)
    if not math.isfinite(number):
        raise BatchError(f"{label} must be finite")
    return number


def render_instruction(
    *,
    case_id: str,
    expected_modification_count: int,
    observable: Mapping[str, Any],
    path_evidence: Mapping[str, Any],
) -> str:
    """Build a prompt from observable replay-before evidence only.

    Deliberately do not add binding, construction-plan, fix, baseline-reference,
    or repair-reference parameters to this API.  Keeping those objects outside
    the renderer makes accidental answer leakage harder than a text blacklist
    alone.
    """

    if SAFE_ID_RE.fullmatch(case_id) is None:
        raise BatchError(f"unsafe case ID for instruction: {case_id!r}")
    if (
        not isinstance(expected_modification_count, int)
        or isinstance(expected_modification_count, bool)
        or expected_modification_count < 1
    ):
        raise BatchError("instruction modification count must be a positive integer")

    design = path_evidence.get("design")
    view = path_evidence.get("analysis_view")
    paths = require_mapping(path_evidence.get("paths"), "path_evidence.paths")
    negatives = observable.get("negative_endpoints")
    target_paths = require_mapping(observable.get("target_paths"), "target_paths")
    if not isinstance(design, str) or not design:
        raise BatchError("path evidence lacks a design name")
    if not isinstance(view, str) or not view:
        raise BatchError("path evidence lacks an analysis view")
    if (
        not isinstance(negatives, list)
        or not negatives
        or any(not isinstance(item, str) or not item for item in negatives)
        or set(paths) != set(negatives)
        or set(target_paths) != set(negatives)
    ):
        raise BatchError("observable target evidence does not match negative endpoints")

    setup_wns = _finite(observable.get("setup_wns_ns"), "setup WNS")
    setup_tns = _finite(observable.get("setup_tns_ns"), "setup TNS")
    hold_wns = _finite(observable.get("hold_wns_ns"), "hold WNS")
    hold_tns = _finite(observable.get("hold_tns_ns"), "hold TNS")
    drv = require_mapping(observable.get("drv"), "observable.drv")
    drc = observable.get("drc_count")
    connectivity = observable.get("connectivity_violations")
    if not isinstance(drc, int) or isinstance(drc, bool) or drc < 0:
        raise BatchError("observable DRC count must be a nonnegative integer")
    if (
        not isinstance(connectivity, int)
        or isinstance(connectivity, bool)
        or connectivity < 0
    ):
        raise BatchError("observable connectivity count must be a nonnegative integer")
    for key in ("max_transition", "max_capacitance", "max_fanout"):
        value = drv.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise BatchError(f"observable DRV {key} must be a nonnegative integer")

    lines = [
        f"请在随样本提供的 {design} 已布线 post-route violating checkpoint 上完成 {case_id} setup timing ECO。",
        (
            f"当前 Innovus MMMC setup 视图 {view} 的 WNS/TNS="
            f"{setup_wns:+.6f}/{setup_tns:+.6f} ns；负裕量 endpoint 精确集合为："
            + "、".join(negatives)
            + "。"
        ),
        (
            "当前 hold 仅作为观察证据记录，WNS/TNS="
            f"{hold_wns:+.6f}/{hold_tns:+.6f} ns。"
        ),
        (
            "当前完整物理检查计数："
            f"max_transition={drv['max_transition']}、"
            f"max_capacitance={drv['max_capacitance']}、"
            f"max_fanout={drv['max_fanout']}、DRC={drc}、"
            f"connectivity={connectivity}。"
        ),
        "以下局部路径证据均直接取自该 violating checkpoint；每个列表按报告中的正 point delay 从大到小截取，这些条目仅为报告排序结果，不构成实例或目标 ref 推荐：",
    ]

    for index, endpoint in enumerate(negatives, start=1):
        detail = require_mapping(paths[endpoint], f"path_evidence[{endpoint}]")
        aggregate = require_mapping(target_paths[endpoint], f"target_paths[{endpoint}]")
        beginpoint = detail.get("beginpoint")
        cells = detail.get("cells")
        if not isinstance(beginpoint, str) or not beginpoint:
            raise BatchError(f"path evidence lacks beginpoint for {endpoint}")
        if not isinstance(cells, list) or len(cells) < expected_modification_count:
            raise BatchError(f"insufficient observable path cells for {endpoint}")
        lines.append(
            f"目标 {index}: beginpoint={beginpoint}; endpoint={endpoint}; "
            f"slack={_finite(aggregate.get('slack_ns'), 'target slack'):+.6f} ns; "
            f"data_cell_delay={_finite(aggregate.get('cell_delay_ns'), 'cell delay'):.6f} ns; "
            f"data_net_delay={_finite(aggregate.get('net_delay_ns'), 'net delay'):.6f} ns。"
        )
        for cell_index, cell_value in enumerate(cells, start=1):
            cell = require_mapping(cell_value, f"path cell {cell_index}")
            instance = cell.get("instance")
            pin = cell.get("pin")
            reference = cell.get("current_ref")
            if not all(isinstance(value, str) and value for value in (instance, pin, reference)):
                raise BatchError(f"malformed observable path cell {cell_index}")
            lines.append(
                f"  {cell_index}. inst={instance}; pin={pin}; current_ref={reference}; "
                f"reported_point_delay={_finite(cell.get('delay_ns'), 'point delay'):.6f} ns"
            )

    count_word = "一个" if expected_modification_count == 1 else f"{expected_modification_count} 个"
    lines.extend(
        [
            (
                f"请从上述可观察路径实例中选择恰好{count_word}不同实例，每个实例只执行一次 "
                "ecoChangeCell，并仅替换为逻辑功能、pin signature 与 RVT 家族等价的 drive-strength ref；"
                "这里的最小化指修改实例数固定为题面预算，目标 ref 必须根据当前可观察证据与等价性、闭合要求选择。"
            ),
            (
                "修复后必须满足：setup WNS>=0、TNS=0、无负裕量 endpoint；"
                "max_transition/max_capacitance/max_fanout、DRC 和 connectivity 均不得比当前计数增加；"
                "placement 必须合法，约束、时钟、实例集合、pin-net 拓扑和路由不得改变。"
            ),
            (
                "输出一个 Tcl 代码块。除注释和空行外，命令顺序必须是："
                "setEcoMode -batchMode true；规定数量的 ecoChangeCell -inst {...} -cell {...}；"
                "setEcoMode -batchMode false；最后且仅最后执行一次 refinePlace -eco true。"
            ),
        ]
    )
    text = "\n".join(lines) + "\n"
    assert_oracle_safe(text)
    return text


def render_answer(fix_text: str) -> str:
    """Wrap the validated Tcl without construction-side commentary."""

    if not isinstance(fix_text, str) or not fix_text.endswith("\n"):
        raise BatchError("answer fix Tcl must be newline-terminated text")
    return (
        "根据题面中的 violating-checkpoint 路径证据，执行下列等价 RVT resize，"
        "随后进行增量合法化。\n\n"
        f"```tcl\n{fix_text}```\n"
    )

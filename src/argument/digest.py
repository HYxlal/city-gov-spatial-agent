"""结果摘要（digest）计算。

用途有两个，不能混为一谈：

1. **复算一致性**：同一 Operation 在相同输入下重跑，digest 必须相同。
   若不相同的来源不是 SQL 本身，而是 LLM 改写结论、实时地图接口抖动、
   缓存命中/失效差异——这正是该指标要抓的东西。
2. **What-if 敏感性**：参数改变后 digest 必然改变，此时看的不是"一致"，
   而是结论是否发生翻转。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return str(value)


def canonical_json(payload: Any) -> str:
    """稳定序列化：键排序、紧凑分隔符、保留中文。"""
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    )


def compute_digest(payload: Any) -> str:
    """返回 ``sha256:<hex>`` 形式的结果摘要。"""
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def digest_of_evidence(raw_metrics: dict[str, Any], params: dict[str, Any]) -> str:
    """证据摘要：由结果数据与执行参数共同决定。"""
    return compute_digest({"metrics": raw_metrics, "params": params})

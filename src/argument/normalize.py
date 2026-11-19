"""实体与文本规范化。

Claim 中的实体名与 Evidence 中的原始数值要做比对，但两者来源不同：
前者由 LLM 从自然语言中抽取，后者是数据库/接口返回值。直接字符串相等
匹配会误杀（全半角、空白、大小写、"朝阳区" vs "朝阳"），因此统一规范化
后再做双向包含判断。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

#: 全角字符 -> 半角（含全角字母数字与常用标点）
_FULLWIDTH_MAP = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}
_FULLWIDTH_MAP[0x3000] = 0x20

_WS_RE = re.compile(r"\s+")

#: 实体比对的最小长度。过短的字符串（如单字、单字符编码）不参与包含匹配，
#: 否则 "区" 这类字符会让任意地名互相匹配。
MIN_ENTITY_LEN = 2


def normalize_text(text: str) -> str:
    """全角转半角、压缩空白、转小写。"""
    folded = str(text).translate(_FULLWIDTH_MAP)
    return _WS_RE.sub("", folded).strip().lower()


def collect_scalars(data: Any, *, skip_keys: bool = True) -> tuple[set[str], list[float]]:
    """递归收集数据中的字符串与数值。

    与"把数值拍平"的做法不同，这里只在需要做实体存在性判断时使用，
    且 ``skip_keys=True`` 默认不收集 dict 的键名——否则 "count" 这类
    字段名会被当成实体，造成误通过。
    """
    strings: set[str] = set()
    numbers: list[float] = []

    if isinstance(data, dict):
        for key, value in data.items():
            if not skip_keys:
                strings.add(normalize_text(key))
            sub_strings, sub_numbers = collect_scalars(value, skip_keys=skip_keys)
            strings.update(sub_strings)
            numbers.extend(sub_numbers)
    elif isinstance(data, (list, tuple, set)):
        for item in data:
            sub_strings, sub_numbers = collect_scalars(item, skip_keys=skip_keys)
            strings.update(sub_strings)
            numbers.extend(sub_numbers)
    elif isinstance(data, bool):
        pass  # bool 是 int 的子类，必须排除，否则 True 会变成 1.0
    elif isinstance(data, (int, float)):
        numbers.append(float(data))
    elif isinstance(data, str):
        strings.add(normalize_text(data))

    return strings, numbers


def entity_present(entity: str, candidates: Iterable[str]) -> bool:
    """判断实体是否出现在候选字符串集合中（双向包含）。

    "朝阳区" 可匹配原始值 "朝阳"，反之亦然；H3 索引大小写不同也能匹配。
    """
    target = normalize_text(entity)
    if len(target) < MIN_ENTITY_LEN:
        return False
    return any(target in cand or cand in target for cand in candidates if cand)

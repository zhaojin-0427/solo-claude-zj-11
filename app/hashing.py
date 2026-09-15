"""输入规范化哈希（SHA-256）。

忽略可选的 request_id，按 JSON 排序键序列化，保证同一培养方案/课程/规则
内容无论字段书写顺序如何都得到同一哈希。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import PreevaluationRequest


def canonical_payload(req: PreevaluationRequest) -> dict[str, Any]:
    data = req.model_dump(mode="json")
    data.pop("request_id", None)
    return data


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    )


def input_hash(req: PreevaluationRequest) -> str:
    payload = canonical_payload(req)
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}:{digest}"

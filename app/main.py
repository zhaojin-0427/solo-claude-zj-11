"""交换学分预审 API。

- ``POST /api/preevaluations``：提交培养方案/已修/拟选校外课与规则，搜索最优组合，
  内容哈希命中时复用不可变版本，不重新计算。
- ``GET  /api/preevaluations/{version_id}``：读取历史预审版本。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .engine.search import search
from .hashing import canonical_json, canonical_payload, input_hash
from .models import PreevaluationRequest, PreevaluationResult
from .storage import VersionStore

app = FastAPI(
    title="交换学分预审 API",
    version="1.0.0",
    description=(
        "组合校外课程并预判返校学分认定：支持一对多/多对一抵扣、必修优先、"
        "学分换算上下限、学习成果覆盖、先修链、排课冲突、校内最低修读与课业负担上限。"
    ),
)

_store: Optional[VersionStore] = None


def get_store() -> VersionStore:
    global _store
    if _store is None:
        _store = VersionStore()
    return _store


@app.exception_handler(RequestValidationError)
async def _validation_handler(request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"error": "输入校验失败",
                 "details": jsonable_encoder(exc.errors())},
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/preevaluations", response_model=PreevaluationResult,
          response_model_exclude_none=False)
def create_preevaluation(req: PreevaluationRequest,
                         store: VersionStore = Depends(get_store)
                         ) -> dict:
    digest = input_hash(req)
    existing = store.get_by_hash(digest)
    if existing is not None:
        result = dict(existing["result"])
        result["reused"] = True
        return result

    best, evaluated, truncated = search(req)
    now = datetime.now(timezone.utc).isoformat()

    # 落盘内容不含自引用的 version_id；读出时再补齐，保证每行只 INSERT 一次
    body: dict = {
        "input_hash": digest,
        "best_plan": best.model_dump(mode="json") if best else None,
        "plans_evaluated": evaluated,
        "enumeration_truncated": truncated,
        "locked_must_include": list(req.must_include),
        "created_at": now,
        "reused": False,
    }
    payload_json = canonical_json(canonical_payload(req))
    # 并发兜底：另一线程/进程可能已写入同哈希
    version_id, created = store.put_if_absent(
        digest, payload_json, canonical_json(body), now)
    if not created:
        existing = store.get_by_hash(digest)
        assert existing is not None
        result = dict(existing["result"])
        result["reused"] = True
        return result
    body["version_id"] = version_id
    return body


@app.get("/api/preevaluations/{version_id}",
         response_model=PreevaluationResult)
def get_preevaluation(version_id: int,
                      store: VersionStore = Depends(get_store)) -> dict:
    row = store.get_by_version(version_id)
    if row is None:
        raise HTTPException(status_code=404, detail="预审版本不存在")
    result = dict(row["result"])
    result["reused"] = True
    return result

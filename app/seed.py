"""样例数据装载：读取 examples/catalog.json 建立 vessel-guan-07 基线。

基线只登记到"两份草稿并存、frag-09 被同时占用"；确认、否决、失效、重组
由 tools/demo_replay.py 现场驱动，以演示完整决策链。

seed 是幂等的：库中已有同名残片/假设时跳过，重复执行不会报错。
"""
from __future__ import annotations

import json
from pathlib import Path

from .errors import ConflictError
from .service import AssemblyService

CATALOG_PATH = Path(__file__).resolve().parents[1] / "examples" / "catalog.json"


def load_catalog(path: str | Path | None = None) -> dict:
    return json.loads(Path(path or CATALOG_PATH).read_text(encoding="utf-8"))


def _ignore_duplicate(fn) -> bool:
    try:
        fn()
    except ConflictError:
        return False
    return True


def seed_baseline(service: AssemblyService, catalog: dict | None = None) -> dict:
    """灌入基线数据并创建两份竞争草稿，返回执行/跳过计数。"""
    catalog = catalog or load_catalog()
    group_id = catalog["group_id"]
    applied = skipped = 0

    def run(fn) -> None:
        nonlocal applied, skipped
        if _ignore_duplicate(fn):
            applied += 1
        else:
            skipped += 1

    for expert in catalog["experts"]:
        run(lambda e=expert: service.register_expert(e))
    for fragment in catalog["fragments"]:
        run(lambda f=fragment: service.register_fragment(f))
    for candidate in catalog["candidates"]:
        run(lambda c=candidate: service.propose_candidate(c))
    for evidence in catalog["evidence"]:
        run(lambda e=evidence: service.record_evidence(e))
    for sig in catalog["signatures"]:
        run(lambda s=sig: service.observe_signature(s))
    for hyp in catalog["hypotheses"]:
        run(lambda h=hyp: service.create_hypothesis({
            "hypothesis_id": h["hypothesis_id"], "group_id": group_id,
            "fragment_ids": h["fragment_ids"],
            "candidate_ids": h["candidate_ids"],
            "created_by": h["created_by"], "note": h.get("note", ""),
        }))
    return {"applied": applied, "skipped": skipped}

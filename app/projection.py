"""事件重放：把 append-only 事件流折叠成当前读模型，并重建关系图。

重放是确定性的——给定同一批事件，任何时刻都能得到相同快照，
决策回放接口也复用这里的折叠逻辑按 seq 推进。
"""
from __future__ import annotations

from typing import Any, Callable

#: 假设"活跃编辑"状态，修订只允许在这些状态下进行
EDITABLE_STATES = {"draft", "under_review"}


class Projection:
    def __init__(self) -> None:
        self.experts: dict[str, dict] = {}
        self.fragments: dict[str, dict] = {}
        self.evidence: dict[str, dict] = {}
        self.candidates: dict[str, dict] = {}
        self.hypotheses: dict[str, dict] = {}
        self.reviews: list[dict] = []
        # 关系图边：(src, relation, dst) -> 首次建立该关系的事件 seq
        self.relations: dict[tuple[str, str, str], int] = {}
        self.last_seq = 0

    def apply(self, event: dict[str, Any]) -> None:
        seq = event["seq"]
        self.last_seq = seq
        etype = event["event_type"]
        p = event["payload"]
        handler = self._handlers().get(etype)
        if handler:
            handler(event, p)

    def _handlers(self) -> dict[str, Callable]:
        return {
            "expert_registered": self._expert_registered,
            "fragment_registered": self._fragment_registered,
            "evidence_recorded": self._evidence_recorded,
            "candidate_proposed": self._candidate_proposed,
            "edge_signature_observed": self._edge_signature_observed,
            "hypothesis_created": self._hypothesis_created,
            "hypothesis_revised": self._hypothesis_revised,
            "hypothesis_submitted": self._hypothesis_submitted,
            "review_cast": self._review_cast,
            "hypothesis_confirmed": self._hypothesis_confirmed,
            "hypothesis_invalidated": self._hypothesis_invalidated,
            "hypothesis_withdrawn": self._hypothesis_withdrawn,
        }

    # ---- handlers -------------------------------------------------------
    def _expert_registered(self, e: dict, p: dict) -> None:
        self.experts[p["expert_id"]] = {
            "expert_id": p["expert_id"], "name": p["name"], "role": p["role"],
        }

    def _fragment_registered(self, e: dict, p: dict) -> None:
        features = {f["feature_id"]: f for f in p["features"]}
        self.fragments[p["fragment_id"]] = {
            "fragment_id": p["fragment_id"],
            "group_id": p["group_id"],
            "dimensions": p["dimensions"],
            "features": features,
            "registered_by": p["registered_by"],
        }
        for fid in features:
            self.relations[
                (f"fragment:{p['fragment_id']}", "describes",
                 f"edge_feature:{p['fragment_id']}:{fid}")
            ] = e["seq"]

    def _evidence_recorded(self, e: dict, p: dict) -> None:
        self.evidence[p["evidence_id"]] = {
            "evidence_id": p["evidence_id"],
            "group_id": p["group_id"],
            "kind": p["kind"],
            "summary": p["summary"],
            "recorded_by": p["recorded_by"],
            "target": p["target"],
            "stance": p["stance"],
            "seq": e["seq"],
        }
        rel = p["stance"]  # supports / contradicts，均为契约关系类型
        self.relations[(
            f"evidence:{p['evidence_id']}", rel,
            f"{p['target']['type']}:{p['target']['id']}",
        )] = e["seq"]

    def _candidate_proposed(self, e: dict, p: dict) -> None:
        self.candidates[p["candidate_id"]] = {
            "candidate_id": p["candidate_id"],
            "group_id": p["group_id"],
            "left": p["left"],
            "right": p["right"],
            "score": p["score"],
            "rotation": p["rotation"],
            "signatures": dict(p.get("signatures", {})),
            "proposed_by": p["proposed_by"],
            "state": "proposed",
            "seq": e["seq"],
        }
        for side in (p["left"], p["right"]):
            self.relations[(
                f"candidate:{p['candidate_id']}", "joins",
                f"edge_feature:{side['fragment_id']}:{side['feature_id']}",
            )] = e["seq"]

    def _edge_signature_observed(self, e: dict, p: dict) -> None:
        cand = self.candidates.get(p["candidate_id"])
        if cand:
            cand["signatures"][p["signature"]] = {
                "value": p["value"], "observed_by": p["observed_by"],
            }
        if p.get("evidence_id"):
            ev = self.evidence.get(p["evidence_id"])
            already_linked = (
                ev is not None
                and ev["target"]["type"] == "candidate"
                and ev["target"]["id"] == p["candidate_id"]
                and ev["stance"] == "supports"
            )
            if not already_linked:
                # 签名观测自带证据编号、但没有独立 evidence_recorded 时，补一条支持边
                self.relations[(
                    f"evidence:{p['evidence_id']}",
                    "supports", f"candidate:{p['candidate_id']}",
                )] = e["seq"]

    def _hypothesis_created(self, e: dict, p: dict) -> None:
        self._put_hypothesis(p, state="draft", seq=e["seq"])

    def _hypothesis_revised(self, e: dict, p: dict) -> None:
        hyp = self.hypotheses[p["hypothesis_id"]]
        # 修订产生新版本内容，但旧版本结论已在事件流中永久保留
        hyp["fragment_ids"] = list(p["fragment_ids"])
        hyp["candidate_ids"] = list(p["candidate_ids"])
        hyp["version"] = p["version"]
        hyp["revision_note"] = p.get("revision_note", "")
        hyp["updated_seq"] = e["seq"]

    def _hypothesis_submitted(self, e: dict, p: dict) -> None:
        hyp = self.hypotheses[p["hypothesis_id"]]
        hyp["state"] = "under_review"
        hyp["submitted_seq"] = e["seq"]

    def _review_cast(self, e: dict, p: dict) -> None:
        self.reviews.append({
            "review_id": p["review_id"],
            "hypothesis_id": p["hypothesis_id"],
            "version": p["version"],
            "expert_id": p["expert_id"],
            "decision": p["decision"],
            "comment": p.get("comment", ""),
            "dedup_key": p.get("dedup_key"),
            "seq": e["seq"],
        })
        self.relations[(
            f"review:{p['review_id']}", "reviews",
            f"hypothesis:{p['hypothesis_id']}@v{p['version']}",
        )] = e["seq"]

    def _hypothesis_confirmed(self, e: dict, p: dict) -> None:
        hyp = self.hypotheses[p["hypothesis_id"]]
        hyp["state"] = "confirmed"
        hyp["confirmed_at"] = e["created_at"]
        hyp["confirmed_by"] = p["confirmed_by"]
        hyp["confirmed_seq"] = e["seq"]
        for cid in hyp["candidate_ids"]:
            cand = self.candidates.get(cid)
            if cand:
                cand["state"] = "recommended"
                self.relations[(
                    f"hypothesis:{hyp['hypothesis_id']}", "contains",
                    f"candidate:{cid}",
                )] = e["seq"]
        for fid in hyp["fragment_ids"]:
            self.relations[(
                f"hypothesis:{hyp['hypothesis_id']}", "contains",
                f"fragment:{fid}",
            )] = e["seq"]

    def _hypothesis_invalidated(self, e: dict, p: dict) -> None:
        hyp = self.hypotheses[p["hypothesis_id"]]
        prior_state = hyp["state"]
        hyp["state"] = "invalidated"
        hyp["invalidated_at"] = e["created_at"]
        hyp["invalidated_by"] = p["invalidated_by"]
        hyp["invalidation_reason"] = p["reason"]
        hyp["trigger_evidence"] = p.get("evidence_id")
        hyp["invalidated_seq"] = e["seq"]
        for cid in hyp["candidate_ids"]:
            cand = self.candidates.get(cid)
            if cand and cand["state"] == "recommended":
                cand["state"] = "proposed"
        if p.get("superseded_by"):
            self.relations[(
                f"hypothesis:{p['superseded_by']}", "supersedes",
                f"hypothesis:{hyp['hypothesis_id']}",
            )] = e["seq"]

    def _hypothesis_withdrawn(self, e: dict, p: dict) -> None:
        hyp = self.hypotheses[p["hypothesis_id"]]
        hyp["state"] = "withdrawn"
        hyp["withdrawn_seq"] = e["seq"]

    def _put_hypothesis(self, p: dict, *, state: str, seq: int) -> None:
        self.hypotheses[p["hypothesis_id"]] = {
            "hypothesis_id": p["hypothesis_id"],
            "group_id": p["group_id"],
            "version": p["version"],
            "fragment_ids": list(p["fragment_ids"]),
            "candidate_ids": list(p["candidate_ids"]),
            "state": state,
            "created_seq": seq,
            "updated_seq": seq,
            "revision_note": "",
        }

    # ---- 查询辅助 -------------------------------------------------------
    def reviews_for(self, hypothesis_id: str, version: int | None = None) -> list[dict]:
        return [
            r for r in self.reviews
            if r["hypothesis_id"] == hypothesis_id
            and (version is None or r["version"] == version)
        ]

    def as_dict(self) -> dict:
        return {
            "experts": self.experts,
            "fragments": self.fragments,
            "evidence": self.evidence,
            "candidates": self.candidates,
            "hypotheses": self.hypotheses,
            "reviews": self.reviews,
            "relations": [
                {"from": s, "relation": rel, "to": d, "since_seq": seq}
                for (s, rel, d), seq in sorted(self.relations.items(), key=lambda x: x[1])
            ],
            "last_seq": self.last_seq,
        }


def replay(events: list[dict]) -> Projection:
    proj = Projection()
    for event in sorted(events, key=lambda e: e["seq"]):
        proj.apply(event)
    return proj

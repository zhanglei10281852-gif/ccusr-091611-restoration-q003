"""应用服务：命令处理 + 读模型查询。

每个命令都在锁内"全量重放 → 校验 → 追加事件"，数据量为单批评审项目，
简单且天然一致；真正的并发安全网是事件表上的两个唯一约束。
"""
from __future__ import annotations

import threading
from typing import Any

from . import domain
from .contract import EXPERT_ROLES, REVIEW_DECISIONS, ROLE_PERMS
from .errors import (
    ConflictError,
    GateError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .projection import Projection, replay
from .store import ConcurrentVersionError, EventStore


class AssemblyService:
    def __init__(self, store: EventStore):
        self.store = store
        self._lock = threading.RLock()

    # ================================================================ 内部
    def _project(self) -> Projection:
        return replay(self.store.all_events())

    def _require_expert(self, proj: Projection, expert_id: str) -> dict:
        expert = proj.experts.get(expert_id)
        if not expert:
            raise NotFoundError(f"专家 {expert_id} 未登记")
        return expert

    def _require_hypothesis(self, proj: Projection, hyp_id: str) -> dict:
        hyp = proj.hypotheses.get(hyp_id)
        if not hyp:
            raise NotFoundError(f"假设 {hyp_id} 不存在")
        return hyp

    def _append(self, event_type: str, agg_type: str, agg_id: str, actor: str,
                payload: dict, *, version: int | None = None,
                dedup_key: str | None = None) -> tuple[dict, bool]:
        try:
            return self.store.append(
                event_type, agg_type, agg_id, actor, payload,
                version=version, dedup_key=dedup_key,
            )
        except ConcurrentVersionError:
            # 重新读取服务端最新版本，给第二位修复师可操作的冲突信息
            current = self._project().hypotheses.get(agg_id)
            raise ConflictError(
                f"假设 {agg_id} 已被其他人修订（版本冲突），请基于最新版本重新调整",
                details={
                    "hypothesis_id": agg_id,
                    "server_version": current["version"] if current else None,
                    "your_version": version - 1 if version else None,
                },
            )

    # ================================================================ 命令
    def register_expert(self, payload: dict) -> dict:
        domain.require_fields(payload, ("expert_id", "name", "role"), "专家登记")
        if payload["role"] not in EXPERT_ROLES:
            raise ValidationError(
                f"角色 {payload['role']} 不合法，允许: {sorted(EXPERT_ROLES)}"
            )
        with self._lock:
            proj = self._project()
            if payload["expert_id"] in proj.experts:
                raise ConflictError(f"专家 {payload['expert_id']} 已登记")
            event, _ = self.store.append(
                "expert_registered", "expert", payload["expert_id"],
                payload["name"], payload,
            )
        return {"event_seq": event["seq"], "expert": payload}

    def register_fragment(self, payload: dict) -> dict:
        domain.validate_fragment(payload)
        with self._lock:
            proj = self._project()
            if payload["fragment_id"] in proj.fragments:
                raise ConflictError(f"残片 {payload['fragment_id']} 已登记")
            event, _ = self.store.append(
                "fragment_registered", "fragment", payload["fragment_id"],
                payload["registered_by"], payload,
            )
        return {"event_seq": event["seq"], "fragment_id": payload["fragment_id"]}

    def record_evidence(self, payload: dict) -> dict:
        domain.validate_evidence(payload)
        with self._lock:
            proj = self._project()
            if payload["evidence_id"] in proj.evidence:
                raise ConflictError(f"证据 {payload['evidence_id']} 已存在")
            ttype, tid = payload["target"]["type"], payload["target"]["id"]
            if ttype == "candidate" and tid not in proj.candidates:
                raise NotFoundError(f"证据指向的候选邻接 {tid} 不存在")
            if ttype == "hypothesis" and tid not in proj.hypotheses:
                raise NotFoundError(f"证据指向的假设 {tid} 不存在")
            event, _ = self.store.append(
                "evidence_recorded", "evidence", payload["evidence_id"],
                payload["recorded_by"], payload,
            )
        return {"event_seq": event["seq"], "evidence_id": payload["evidence_id"]}

    def propose_candidate(self, payload: dict) -> dict:
        with self._lock:
            proj = self._project()
            domain.validate_candidate(payload, proj.fragments)
            if payload["candidate_id"] in proj.candidates:
                raise ConflictError(f"候选邻接 {payload['candidate_id']} 已存在")
            event, _ = self.store.append(
                "candidate_proposed", "candidate", payload["candidate_id"],
                payload["proposed_by"], payload,
            )
        return {"event_seq": event["seq"], "candidate_id": payload["candidate_id"]}

    def observe_signature(self, payload: dict) -> dict:
        domain.require_fields(
            payload,
            ("candidate_id", "signature", "value", "observed_by"),
            "断面签名观测",
        )
        with self._lock:
            proj = self._project()
            cand = proj.candidates.get(payload["candidate_id"])
            if not cand:
                raise NotFoundError(f"候选邻接 {payload['candidate_id']} 不存在")
            if payload["signature"] in cand["signatures"]:
                # 签名观测是不可变证据；复测若推翻旧值应登记 contradicts 证据，
                # 而不是覆盖，否则评审会上无法解释结论为何改变
                raise ConflictError(
                    f"候选 {payload['candidate_id']} 的签名 {payload['signature']} "
                    "已观测，不能重复登记覆盖",
                )
            event, _ = self.store.append(
                "edge_signature_observed", "candidate", payload["candidate_id"],
                payload["observed_by"], payload,
            )
        return {"event_seq": event["seq"]}

    def create_hypothesis(self, payload: dict) -> dict:
        domain.require_fields(
            payload, ("hypothesis_id", "group_id", "fragment_ids",
                      "candidate_ids", "created_by"),
            "组合假设",
        )
        if not payload["candidate_ids"]:
            raise ValidationError("组合假设至少包含一条候选邻接")
        with self._lock:
            proj = self._project()
            if payload["hypothesis_id"] in proj.hypotheses:
                raise ConflictError(f"假设 {payload['hypothesis_id']} 已存在")
            for cid in payload["candidate_ids"]:
                if cid not in proj.candidates:
                    raise NotFoundError(f"候选邻接 {cid} 不存在")
            event, _ = self._append(
                "hypothesis_created",
                "hypothesis", payload["hypothesis_id"],
                payload["created_by"],
                {
                    "hypothesis_id": payload["hypothesis_id"],
                    "group_id": payload["group_id"],
                    "version": 1,
                    "fragment_ids": list(payload["fragment_ids"]),
                    "candidate_ids": list(payload["candidate_ids"]),
                    "note": payload.get("note", ""),
                },
                version=1,
            )
        return {"event_seq": event["seq"], "hypothesis_id": payload["hypothesis_id"],
                "version": 1}

    def revise_hypothesis(self, payload: dict) -> dict:
        """两位修复师并发调整同一假设：expected_version 失配即 409。"""
        domain.require_fields(
            payload,
            ("hypothesis_id", "expected_version", "fragment_ids",
             "candidate_ids", "revised_by", "revision_note"),
            "假设修订",
        )
        with self._lock:
            proj = self._project()
            hyp = self._require_hypothesis(proj, payload["hypothesis_id"])
            if hyp["state"] not in ("draft", "under_review"):
                raise ConflictError(
                    f"状态 {hyp['state']} 的假设不可修订；新证据请走失效流程，"
                    "旧结论会保留在历史中",
                )
            if payload["expected_version"] != hyp["version"]:
                raise ConflictError(
                    f"修订基于的版本 {payload['expected_version']} 已过期，"
                    f"服务端当前为 v{hyp['version']}",
                    details={
                        "hypothesis_id": hyp["hypothesis_id"],
                        "server_version": hyp["version"],
                        "your_version": payload["expected_version"],
                    },
                )
            for cid in payload["candidate_ids"]:
                if cid not in proj.candidates:
                    raise NotFoundError(f"候选邻接 {cid} 不存在")
            new_version = hyp["version"] + 1
            event, _ = self._append(
                "hypothesis_revised", "hypothesis", hyp["hypothesis_id"],
                payload["revised_by"],
                {
                    "hypothesis_id": hyp["hypothesis_id"],
                    "group_id": hyp["group_id"],
                    "version": new_version,
                    "fragment_ids": list(payload["fragment_ids"]),
                    "candidate_ids": list(payload["candidate_ids"]),
                    "revision_note": payload["revision_note"],
                },
                version=new_version,
            )
        return {"event_seq": event["seq"], "hypothesis_id": hyp["hypothesis_id"],
                "version": new_version}

    def submit_for_review(self, hyp_id: str, actor: str) -> dict:
        with self._lock:
            proj = self._project()
            hyp = self._require_hypothesis(proj, hyp_id)
            self._require_expert(proj, actor)
            domain.ensure_transition(hyp["state"], "under_review")
            event, _ = self.store.append(
                "hypothesis_submitted", "hypothesis", hyp_id, actor,
                {"hypothesis_id": hyp_id, "version": hyp["version"]},
            )
        return {"event_seq": event["seq"], "state": "under_review"}

    def cast_review(self, payload: dict) -> dict:
        """离线意见重传：client_request_id 幂等键保证一人一票、绝不重复计票。"""
        domain.require_fields(
            payload,
            ("review_id", "hypothesis_id", "expert_id", "decision",
             "client_request_id"),
            "专家意见",
        )
        if payload["decision"] not in REVIEW_DECISIONS:
            raise ValidationError(
                f"decision 必须是 {sorted(REVIEW_DECISIONS)} 之一"
            )
        dedup_key = f"review:{payload['client_request_id']}"
        with self._lock:
            proj = self._project()
            hyp = self._require_hypothesis(proj, payload["hypothesis_id"])
            expert = self._require_expert(proj, payload["expert_id"])
            if hyp["state"] != "under_review":
                raise ConflictError(
                    f"假设当前状态为 {hyp['state']}，只有评审中版本可以接收意见",
                )
            if payload.get("version") is not None and payload["version"] != hyp["version"]:
                raise ConflictError(
                    f"意见针对 v{payload['version']}，当前评审版本为 v{hyp['version']}",
                    details={"server_version": hyp["version"],
                             "your_version": payload["version"]},
                )
            body = {
                "review_id": payload["review_id"],
                "hypothesis_id": hyp["hypothesis_id"],
                "version": hyp["version"],
                "expert_id": expert["expert_id"],
                "decision": payload["decision"],
                "comment": payload.get("comment", ""),
                "dedup_key": dedup_key,
            }
            event, reused = self.store.append(
                "review_cast", "review", payload["review_id"],
                expert["expert_id"], body, dedup_key=dedup_key,
            )
            if reused:
                prior = event["payload"]
                semantic = ("hypothesis_id", "expert_id", "decision", "comment")
                if any(prior.get(k) != body.get(k) for k in semantic):
                    raise ConflictError(
                        "幂等键已被使用但提交内容与原意见不一致，拒绝重放",
                        details={"client_request_id": payload["client_request_id"]},
                    )
            proj = self._project()
            tally = domain.tally_reviews(
                proj.reviews_for(hyp["hypothesis_id"], hyp["version"]),
                proj.experts,
            )
        return {
            "event_seq": event["seq"], "replayed": reused,
            "review_id": body["review_id"], "tally": tally,
        }

    def confirm_hypothesis(self, hyp_id: str, actor: str) -> dict:
        with self._lock:
            proj = self._project()
            hyp = self._require_hypothesis(proj, hyp_id)
            expert = self._require_expert(proj, actor)
            domain.assert_can_confirm(expert["role"], ROLE_PERMS)
            domain.ensure_transition(hyp["state"], "confirmed")

            tally = domain.tally_reviews(
                proj.reviews_for(hyp_id, hyp["version"]), proj.experts
            )
            if not tally["quorum"]:
                raise PermissionDenied(
                    "评审共识不足：至少 2 票赞成（含首席修复师）且无反对票",
                    details={"tally": tally},
                )

            report = domain.evaluate_gates(
                hyp, fragments=proj.fragments, candidates=proj.candidates,
                hypotheses=proj.hypotheses, evidence=proj.evidence,
            )
            if not report["passed"]:
                raise GateError(
                    f"假设 {hyp_id} 未通过确认闸门，不能仅凭算法分值确认",
                    details={"gate_report": report,
                             "violations": domain.blocking_violations(report)},
                )

            event, _ = self.store.append(
                "hypothesis_confirmed", "hypothesis", hyp_id, actor,
                {"hypothesis_id": hyp_id, "version": hyp["version"],
                 "confirmed_by": actor},
            )
        return {"event_seq": event["seq"], "hypothesis_id": hyp_id,
                "state": "confirmed", "gate_report": report, "tally": tally}

    def invalidate_hypothesis(self, payload: dict) -> dict:
        """新证据使已确认组合失效：追加失效事件，确认事实永久保留。"""
        domain.require_fields(
            payload, ("hypothesis_id", "invalidated_by", "reason"), "失效宣告"
        )
        with self._lock:
            proj = self._project()
            hyp = self._require_hypothesis(proj, payload["hypothesis_id"])
            expert = self._require_expert(proj, payload["invalidated_by"])
            domain.assert_can_invalidate(expert["role"], ROLE_PERMS)
            domain.ensure_transition(hyp["state"], "invalidated")
            evidence_id = payload.get("evidence_id")
            if evidence_id and evidence_id not in proj.evidence:
                raise NotFoundError(f"触发失效的证据 {evidence_id} 不存在")
            if payload.get("superseded_by") and payload["superseded_by"] not in proj.hypotheses:
                raise NotFoundError(
                    f"接替假设 {payload['superseded_by']} 不存在"
                )
            event, _ = self.store.append(
                "hypothesis_invalidated", "hypothesis", hyp["hypothesis_id"],
                expert["expert_id"],
                {
                    "hypothesis_id": hyp["hypothesis_id"],
                    "invalidated_by": expert["expert_id"],
                    "reason": payload["reason"],
                    "evidence_id": evidence_id,
                    "superseded_by": payload.get("superseded_by"),
                },
            )
        return {"event_seq": event["seq"], "hypothesis_id": hyp["hypothesis_id"],
                "state": "invalidated"}

    def withdraw_hypothesis(self, hyp_id: str, actor: str) -> dict:
        with self._lock:
            proj = self._project()
            hyp = self._require_hypothesis(proj, hyp_id)
            self._require_expert(proj, actor)
            domain.ensure_transition(hyp["state"], "withdrawn")
            event, _ = self.store.append(
                "hypothesis_withdrawn", "hypothesis", hyp_id, actor,
                {"hypothesis_id": hyp_id, "reason": "撤回"},
            )
        return {"event_seq": event["seq"], "state": "withdrawn"}

    # ================================================================ 查询
    def snapshot(self) -> dict:
        with self._lock:
            return self._project().as_dict()

    def hypothesis_detail(self, hyp_id: str) -> dict:
        with self._lock:
            proj = self._project()
            hyp = self._require_hypothesis(proj, hyp_id)
            report = domain.evaluate_gates(
                hyp, fragments=proj.fragments, candidates=proj.candidates,
                hypotheses=proj.hypotheses, evidence=proj.evidence,
            )
            reviews = proj.reviews_for(hyp_id)
            tally = domain.tally_reviews(
                proj.reviews_for(hyp_id, hyp["version"]), proj.experts
            )
            return {
                "hypothesis": hyp,
                "candidates": {cid: proj.candidates.get(cid)
                               for cid in hyp["candidate_ids"]},
                "gate_report": report,
                "reviews": reviews,
                "tally_on_current_version": tally,
                "timeline": self._timeline(proj, group_id=hyp["group_id"],
                                           hyp_id=hyp_id),
            }

    def feasibility(self, group_id: str | None = None) -> dict:
        """当前可行组合与冲突路径：对每个活跃假设跑闸门，并标出互斥对。"""
        with self._lock:
            proj = self._project()
            hyps = [
                h for h in proj.hypotheses.values()
                if group_id is None or h["group_id"] == group_id
            ]
            items = []
            for hyp in sorted(hyps, key=lambda h: h["hypothesis_id"]):
                report = domain.evaluate_gates(
                    hyp, fragments=proj.fragments, candidates=proj.candidates,
                    hypotheses=proj.hypotheses, evidence=proj.evidence,
                )
                items.append({
                    "hypothesis_id": hyp["hypothesis_id"],
                    "group_id": hyp["group_id"],
                    "version": hyp["version"],
                    "state": hyp["state"],
                    "gates_passed": report["passed"],
                    "blocking_conflicts": domain.blocking_violations(report),
                })
            # 互斥关系：共享残片、且都还占着/想占残片的假设对
            conflicts = []
            active = [h for h in hyps if h["state"] in
                      ("draft", "under_review", "confirmed")]
            for i, a in enumerate(active):
                for b in active[i + 1:]:
                    shared = sorted(set(a["fragment_ids"]) & set(b["fragment_ids"]))
                    if shared:
                        conflicts.append({
                            "pair": [a["hypothesis_id"], b["hypothesis_id"]],
                            "states": [a["state"], b["state"]],
                            "shared_fragments": shared,
                            "resolvable_while": (
                                "neither confirmed"
                                if "confirmed" not in (a["state"], b["state"])
                                else "one already confirmed — 另一组合无法通过排他闸门"
                            ),
                        })
            return {"group_id": group_id, "combinations": items,
                    "mutual_exclusion": conflicts}

    def replay_group(self, group_id: str) -> dict:
        """重放某一器物（组）从候选提出到拆解重组的全部决策。"""
        with self._lock:
            proj = self._project()
            groups = {f["group_id"] for f in proj.fragments.values()}
            groups |= {c["group_id"] for c in proj.candidates.values()}
            groups |= {h["group_id"] for h in proj.hypotheses.values()}
            if group_id not in groups:
                raise NotFoundError(f"器物组 {group_id} 不存在")
            return {"group_id": group_id, "timeline": self._timeline(proj, group_id)}

    # ---- 时间线构造 -----------------------------------------------------
    def _timeline(self, proj: Projection, group_id: str,
                  hyp_id: str | None = None) -> list[dict]:
        related_candidates: set[str] = set()
        related_evidence: set[str] = set()
        if hyp_id is not None:
            hyp = proj.hypotheses[hyp_id]
            related_candidates.update(hyp["candidate_ids"])
            for eid, ev in proj.evidence.items():
                t = ev["target"]
                if (t["type"] == "hypothesis" and t["id"] == hyp_id) or (
                    t["type"] == "candidate" and t["id"] in related_candidates
                ):
                    related_evidence.add(eid)

        steps: list[dict] = []
        for event in self.store.all_events():
            p = event["payload"]
            etype = event["event_type"]
            keep = True
            if hyp_id is not None:
                keep = (
                    p.get("hypothesis_id") == hyp_id
                    or p.get("candidate_id") in related_candidates
                    or p.get("evidence_id") in related_evidence
                    or (etype == "edge_signature_observed"
                        and p.get("candidate_id") in related_candidates)
                    or (etype == "review_cast" and p.get("hypothesis_id") == hyp_id)
                )
            else:
                gid = p.get("group_id")
                if etype == "edge_signature_observed":
                    gid = proj.candidates.get(p.get("candidate_id"), {}).get("group_id")
                elif etype == "review_cast":
                    gid = proj.hypotheses.get(p.get("hypothesis_id"), {}).get("group_id")
                elif etype in ("hypothesis_confirmed", "hypothesis_invalidated",
                               "hypothesis_withdrawn", "hypothesis_submitted"):
                    gid = proj.hypotheses.get(p.get("hypothesis_id"), {}).get("group_id")
                keep = gid == group_id
            if keep:
                steps.append({
                    "seq": event["seq"],
                    "at": event["created_at"],
                    "event_type": etype,
                    "actor": event["actor"],
                    "payload": p,
                })
        return steps

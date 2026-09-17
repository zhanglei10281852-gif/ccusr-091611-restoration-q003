"""拼接假设协作图的核心服务。

职责:
- 维护残片、断面特征、候选邻接、组合假设、证据、专家意见组成的关系图;
- 所有决策以追加事件的方式记录,历史结论只失效、不删除;
- 确认组合前执行守卫检查(排他占用、方向一致性、必需签名);
- 版本冲突显式暴露,离线意见重传幂等去重;
- 提供当前可行组合、冲突路径与器物决策重放三类查询接口。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from itertools import combinations
from pathlib import Path

from .guards import (
    GuardReport,
    SignaturePolicy,
    check_fragment_exclusivity,
    check_orientation_consistency,
    check_required_signatures,
)
from .model import (
    ACTIVE_STATES,
    ADJUSTABLE_STATES,
    CONFIRMABLE_STATES,
    CandidateEdge,
    Evidence,
    EvidenceKind,
    Expert,
    Fragment,
    Hypothesis,
    HypothesisState,
    Review,
    Stance,
    split_endpoint,
)


class UnknownEntityError(KeyError):
    """引用了图中不存在的实体。"""


class VersionConflictError(Exception):
    """基于过期版本的并发修改,冲突显式暴露给调用方。"""

    def __init__(self, hypothesis_id: str, expected: int, current: int):
        super().__init__(
            f"假设 {hypothesis_id} 版本冲突: 基于 v{expected} 修改,当前已是 v{current}"
        )
        self.hypothesis_id = hypothesis_id
        self.expected = expected
        self.current = current


class InvalidTransitionError(Exception):
    """不允许的状态迁移。"""


class GuardRejectionError(Exception):
    """确认请求被守卫驳回;驳回原因已记入事件日志。"""

    def __init__(self, report: GuardReport):
        super().__init__(f"假设 {report.hypothesis_id} 确认被驳回: {'; '.join(report.failures)}")
        self.report = report


@dataclass(frozen=True)
class Event:
    """追加式决策事件;replay 接口按器物过滤后按 seq 重放。"""

    seq: int
    kind: str
    actor: str
    hypothesis_id: str | None
    vessel_ids: tuple
    details: dict
    ts: float

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "actor": self.actor,
            "hypothesis_id": self.hypothesis_id,
            "vessel_ids": list(self.vessel_ids),
            "details": self.details,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Event":
        return cls(
            seq=int(data["seq"]),
            kind=data["kind"],
            actor=data["actor"],
            hypothesis_id=data.get("hypothesis_id"),
            vessel_ids=tuple(data.get("vessel_ids", ())),
            details=dict(data.get("details", {})),
            ts=float(data.get("ts", 0.0)),
        )


class AssemblyService:
    def __init__(
        self,
        policy: SignaturePolicy | None = None,
        score_range: tuple = (0.0, 1.0),
        clock=time.time,
    ):
        self._policy = policy or SignaturePolicy()
        self._score_range = score_range
        self._clock = clock
        self._fragments: dict = {}
        self._experts: dict = {}
        self._hypotheses: dict = {}
        self._reviews: list = []  # 按到达顺序保存,同一专家取最新立场
        self._review_ids: set = set()
        self._evidence: dict = {}
        self._relations: list = []  # (源节点, 关系, 目标节点),只增不删
        self._events: list = []
        self._seq = 0

    # ------------------------------------------------------------------ 登记

    def register_fragment(self, fragment: Fragment) -> None:
        if fragment.fragment_id in self._fragments:
            raise ValueError(f"残片 {fragment.fragment_id} 已登记")
        self._fragments[fragment.fragment_id] = fragment
        frag_node = f"fragment:{fragment.fragment_id}"
        for edge_id in fragment.edges:
            self._add_relation(f"edge_feature:{fragment.fragment_id}:{edge_id}", "describes", frag_node)
        self._record(
            "fragment_registered",
            actor="system",
            hypothesis_id=None,
            vessel_ids=(fragment.vessel_id,),
            details={"fragment_id": fragment.fragment_id},
        )

    def register_expert(self, expert: Expert) -> None:
        if expert.expert_id in self._experts:
            raise ValueError(f"专家 {expert.expert_id} 已登记")
        self._experts[expert.expert_id] = expert
        self._record(
            "expert_registered",
            actor="system",
            hypothesis_id=None,
            vessel_ids=(),
            details={"expert_id": expert.expert_id, "roles": list(expert.roles)},
        )

    # ------------------------------------------------------------ 假设生命周期

    def propose_hypothesis(
        self,
        hypothesis_id: str,
        fragment_ids,
        candidate_edges,
        created_by: str,
        supersedes: str | None = None,
    ) -> Hypothesis:
        if hypothesis_id in self._hypotheses:
            raise ValueError(f"假设 {hypothesis_id} 已存在")
        if supersedes is not None and supersedes not in self._hypotheses:
            raise UnknownEntityError(f"被替代的假设不存在: {supersedes}")
        edges = tuple(candidate_edges)
        self._validate_payload(fragment_ids, edges)
        hyp = Hypothesis(
            hypothesis_id=hypothesis_id,
            version=1,
            fragment_ids=tuple(fragment_ids),
            candidate_edges=edges,
            state=HypothesisState.DRAFT,
            created_by=created_by,
            supersedes=supersedes,
        )
        self._hypotheses[hypothesis_id] = hyp
        hyp_node = f"hypothesis:{hypothesis_id}"
        for frag_id in hyp.fragment_ids:
            self._add_relation(hyp_node, "contains", f"fragment:{frag_id}")
        if supersedes:
            self._add_relation(hyp_node, "supersedes", f"hypothesis:{supersedes}")
        self._record(
            "hypothesis_proposed",
            actor=created_by,
            hypothesis_id=hypothesis_id,
            vessel_ids=self._vessels_of(hyp),
            details={
                "version": 1,
                "fragment_ids": list(hyp.fragment_ids),
                "candidate_edges": [e.to_dict() for e in edges],
                "supersedes": supersedes,
            },
        )
        return hyp

    def submit_for_review(self, hypothesis_id: str, expected_version: int) -> Hypothesis:
        hyp = self._get(hypothesis_id)
        self._check_version(hyp, expected_version)
        if hyp.state is not HypothesisState.DRAFT:
            raise InvalidTransitionError(f"假设 {hypothesis_id} 当前状态 {hyp.state.value},不能送审")
        return self._transition(hyp, HypothesisState.UNDER_REVIEW, actor="system", kind="submitted_for_review")

    def adjust_hypothesis(
        self,
        hypothesis_id: str,
        expected_version: int,
        actor: str,
        fragment_ids=None,
        candidate_edges=None,
    ) -> Hypothesis:
        """调整假设内容。版本号不匹配时抛 VersionConflictError,冲突显式暴露。

        调整会递增版本号;此前版本上的签名针对旧版本,不再计入确认守卫。
        """
        hyp = self._get(hypothesis_id)
        if hyp.version != expected_version:
            self._record(
                "version_conflict",
                actor=actor,
                hypothesis_id=hypothesis_id,
                vessel_ids=self._vessels_of(hyp),
                details={
                    "operation": "adjust",
                    "expected_version": expected_version,
                    "current_version": hyp.version,
                },
            )
            raise VersionConflictError(hypothesis_id, expected_version, hyp.version)
        if hyp.state not in ADJUSTABLE_STATES:
            raise InvalidTransitionError(
                f"假设 {hypothesis_id} 当前状态 {hyp.state.value},不能调整;"
                "已确认组合只能由新证据失效后再提出替代假设"
            )
        new_fragments = tuple(fragment_ids) if fragment_ids is not None else hyp.fragment_ids
        new_edges = tuple(candidate_edges) if candidate_edges is not None else hyp.candidate_edges
        self._validate_payload(new_fragments, new_edges)
        updated = replace(
            hyp,
            version=hyp.version + 1,
            fragment_ids=new_fragments,
            candidate_edges=new_edges,
        )
        self._hypotheses[hypothesis_id] = updated
        hyp_node = f"hypothesis:{hypothesis_id}"
        for frag_id in new_fragments:
            rel = (hyp_node, "contains", f"fragment:{frag_id}")
            if rel not in self._relations:
                self._relations.append(rel)
        self._record(
            "hypothesis_adjusted",
            actor=actor,
            hypothesis_id=hypothesis_id,
            vessel_ids=self._vessels_of(updated),
            details={
                "from_version": hyp.version,
                "to_version": updated.version,
                "fragment_ids": list(new_fragments),
                "candidate_edges": [e.to_dict() for e in new_edges],
            },
        )
        return updated

    def withdraw(self, hypothesis_id: str, expected_version: int, actor: str) -> Hypothesis:
        hyp = self._get(hypothesis_id)
        self._check_version(hyp, expected_version)
        if hyp.state not in ADJUSTABLE_STATES:
            raise InvalidTransitionError(f"假设 {hypothesis_id} 当前状态 {hyp.state.value},不能撤回")
        return self._transition(hyp, HypothesisState.WITHDRAWN, actor=actor, kind="hypothesis_withdrawn")

    # ------------------------------------------------------------------ 评审

    def add_review(self, review: Review) -> tuple:
        """登记专家意见,返回 (评审, 是否新计入)。

        review_id 由提交方生成:离线重传、网络重试都会命中同一 id,
        只记录一条"重复忽略"事件,不会重复计票。
        """
        if review.expert_id not in self._experts:
            raise UnknownEntityError(f"专家未登记: {review.expert_id}")
        hyp = self._get(review.hypothesis_id)
        if review.review_id in self._review_ids:
            existing = next(r for r in self._reviews if r.review_id == review.review_id)
            self._record(
                "review_duplicate_ignored",
                actor=review.expert_id,
                hypothesis_id=review.hypothesis_id,
                vessel_ids=self._vessels_of(hyp),
                details={"review_id": review.review_id},
            )
            return existing, False
        self._reviews.append(review)
        self._review_ids.add(review.review_id)
        relation = "supports" if review.stance is Stance.ENDORSE else "contradicts"
        self._add_relation(f"review:{review.review_id}", relation, f"hypothesis:{hyp.hypothesis_id}")
        self._record(
            "review_recorded",
            actor=review.expert_id,
            hypothesis_id=hyp.hypothesis_id,
            vessel_ids=self._vessels_of(hyp),
            details={
                "review_id": review.review_id,
                "version": review.version,
                "stance": review.stance.value,
                "comment": review.comment,
                "submitted_offline": review.submitted_offline,
                "stale": review.version != hyp.version,
            },
        )
        return review, True

    # ------------------------------------------------------------------ 证据

    def add_evidence(self, evidence: Evidence) -> tuple:
        """登记证据,返回 (失效的假设 id 列表, 是否新证据)。

        contradicts 类证据指向已确认组合时使其失效;旧结论与全部历史保留在图中。
        """
        if evidence.evidence_id in self._evidence:
            return [], False
        hyp = self._get(evidence.hypothesis_id)
        self._evidence[evidence.evidence_id] = evidence
        relation = "supports" if evidence.kind is EvidenceKind.SUPPORTS else "contradicts"
        self._add_relation(f"evidence:{evidence.evidence_id}", relation, f"hypothesis:{hyp.hypothesis_id}")
        self._record(
            "evidence_recorded",
            actor=evidence.source or "system",
            hypothesis_id=hyp.hypothesis_id,
            vessel_ids=self._vessels_of(hyp),
            details={
                "evidence_id": evidence.evidence_id,
                "kind": evidence.kind.value,
                "description": evidence.description,
            },
        )
        invalidated = []
        if evidence.kind is EvidenceKind.CONTRADICTS and hyp.state is HypothesisState.CONFIRMED:
            updated = replace(hyp, state=HypothesisState.INVALIDATED, invalidated_by=evidence.evidence_id)
            self._hypotheses[hyp.hypothesis_id] = updated
            invalidated.append(hyp.hypothesis_id)
            self._record(
                "hypothesis_invalidated",
                actor=evidence.source or "system",
                hypothesis_id=hyp.hypothesis_id,
                vessel_ids=self._vessels_of(updated),
                details={
                    "evidence_id": evidence.evidence_id,
                    "description": evidence.description,
                    "invalidated_version": hyp.version,
                },
            )
        return invalidated, True

    # ------------------------------------------------------------------ 确认

    def evaluate_guards(self, hypothesis_id: str) -> GuardReport:
        hyp = self._get(hypothesis_id)
        checks = {
            "fragment_exclusivity": check_fragment_exclusivity(hyp, list(self._hypotheses.values())),
            "orientation_consistency": check_orientation_consistency(hyp),
            "required_signatures": check_required_signatures(hyp, self._reviews, self._experts, self._policy),
        }
        return GuardReport(hypothesis_id=hyp.hypothesis_id, version=hyp.version, checks=checks)

    def confirm(self, hypothesis_id: str, expected_version: int, decided_by: str) -> Hypothesis:
        """确认组合。守卫不通过时记录驳回事件并抛 GuardRejectionError。"""
        hyp = self._get(hypothesis_id)
        if hyp.version != expected_version:
            self._record(
                "version_conflict",
                actor=decided_by,
                hypothesis_id=hypothesis_id,
                vessel_ids=self._vessels_of(hyp),
                details={
                    "operation": "confirm",
                    "expected_version": expected_version,
                    "current_version": hyp.version,
                },
            )
            raise VersionConflictError(hypothesis_id, expected_version, hyp.version)
        if hyp.state not in CONFIRMABLE_STATES:
            raise InvalidTransitionError(f"假设 {hypothesis_id} 当前状态 {hyp.state.value},不能确认")
        report = self.evaluate_guards(hypothesis_id)
        if not report.ok:
            self._record(
                "confirmation_rejected",
                actor=decided_by,
                hypothesis_id=hypothesis_id,
                vessel_ids=self._vessels_of(hyp),
                details={"version": hyp.version, "failures": list(report.failures)},
            )
            raise GuardRejectionError(report)
        updated = replace(hyp, state=HypothesisState.CONFIRMED)
        self._hypotheses[hypothesis_id] = updated
        endorsers = self._current_endorsers(updated)
        self._record(
            "hypothesis_confirmed",
            actor=decided_by,
            hypothesis_id=hypothesis_id,
            vessel_ids=self._vessels_of(updated),
            details={"version": updated.version, "endorsers": endorsers},
        )
        return updated

    # ------------------------------------------------------------------ 查询

    def current_feasible(self) -> dict:
        """当前可行组合:已确认的、现在可通过守卫的、以及被守卫拦下的。"""
        confirmed, confirmable, blocked = [], [], []
        for hyp in self._hypotheses.values():
            summary = self._summary(hyp)
            if hyp.state is HypothesisState.CONFIRMED:
                confirmed.append(summary)
            elif hyp.state in (HypothesisState.DRAFT, HypothesisState.UNDER_REVIEW):
                report = self.evaluate_guards(hyp.hypothesis_id)
                if report.ok:
                    confirmable.append(summary)
                else:
                    blocked.append({**summary, "failures": list(report.failures)})
        return {"confirmed": confirmed, "confirmable": confirmable, "blocked": blocked}

    def conflict_paths(self) -> list:
        """活跃假设之间因争夺同一残片形成的冲突路径。"""
        active = [h for h in self._hypotheses.values() if h.state in ACTIVE_STATES]
        frag_to_hyps: dict = {}
        for hyp in active:
            for frag_id in hyp.fragment_ids:
                frag_to_hyps.setdefault(frag_id, []).append(hyp.hypothesis_id)
        contested = {f: sorted(ids) for f, ids in frag_to_hyps.items() if len(ids) > 1}
        if not contested:
            return []

        adjacency: dict = {}
        for frag_id, hyp_ids in contested.items():
            for hid in hyp_ids:
                adjacency.setdefault(frag_id, set()).add(hid)
                adjacency.setdefault(hid, set()).add(frag_id)

        hyp_ids = sorted({hid for ids in contested.values() for hid in ids})
        components, seen = [], set()
        for hid in hyp_ids:
            if hid in seen:
                continue
            stack, comp = [hid], set()
            while stack:
                node = stack.pop()
                if node in comp:
                    continue
                comp.add(node)
                seen.add(node)
                stack.extend(adjacency.get(node, ()))
            components.append(comp)

        result = []
        for comp in components:
            comp_hyps = sorted(n for n in comp if n in self._hypotheses)
            comp_frags = sorted(n for n in comp if n not in self._hypotheses)
            paths = [
                {"from": a, "to": b, "via": self._shortest_path(adjacency, a, b)}
                for a, b in combinations(comp_hyps, 2)
            ]
            result.append(
                {
                    "hypotheses": comp_hyps,
                    "contested_fragments": comp_frags,
                    "paths": paths,
                }
            )
        return result

    def replay(self, vessel_id: str) -> list:
        """重放某器物从候选提出到拆解重组的全部决策事件。"""
        return [
            {
                "seq": event.seq,
                "kind": event.kind,
                "hypothesis_id": event.hypothesis_id,
                "actor": event.actor,
                "summary": _summarize_event(event, self._experts),
                "details": event.details,
            }
            for event in self._events
            if vessel_id in event.vessel_ids
        ]

    def relations(self, node_id: str | None = None) -> list:
        """关系图中的边;可按节点过滤(源或目标)。"""
        if node_id is None:
            return list(self._relations)
        return [r for r in self._relations if r[0] == node_id or r[2] == node_id]

    def get_hypothesis(self, hypothesis_id: str) -> Hypothesis:
        return self._get(hypothesis_id)

    @property
    def events(self) -> list:
        return list(self._events)

    # ------------------------------------------------------------------ 持久化

    def to_snapshot(self) -> dict:
        return {
            "policy": self._policy.to_dict(),
            "score_range": list(self._score_range),
            "fragments": [f.to_dict() for f in self._fragments.values()],
            "experts": [e.to_dict() for e in self._experts.values()],
            "hypotheses": [h.to_dict() for h in self._hypotheses.values()],
            "reviews": [r.to_dict() for r in self._reviews],
            "evidence": [e.to_dict() for e in self._evidence.values()],
            "relations": [list(r) for r in self._relations],
            "events": [e.to_dict() for e in self._events],
            "seq": self._seq,
        }

    @classmethod
    def from_snapshot(cls, data: dict, clock=time.time) -> "AssemblyService":
        service = cls(
            policy=SignaturePolicy.from_dict(data["policy"]),
            score_range=tuple(data["score_range"]),
            clock=clock,
        )
        for frag_data in data["fragments"]:
            frag = Fragment.from_dict(frag_data)
            service._fragments[frag.fragment_id] = frag
        for exp_data in data["experts"]:
            expert = Expert.from_dict(exp_data)
            service._experts[expert.expert_id] = expert
        for hyp_data in data["hypotheses"]:
            hyp = Hypothesis.from_dict(hyp_data)
            service._hypotheses[hyp.hypothesis_id] = hyp
        for review_data in data["reviews"]:
            review = Review.from_dict(review_data)
            service._reviews.append(review)
            service._review_ids.add(review.review_id)
        for ev_data in data["evidence"]:
            evidence = Evidence.from_dict(ev_data)
            service._evidence[evidence.evidence_id] = evidence
        service._relations = [tuple(r) for r in data["relations"]]
        service._events = [Event.from_dict(e) for e in data["events"]]
        service._seq = int(data["seq"])
        return service

    def save(self, path) -> None:
        Path(path).write_text(
            json.dumps(self.to_snapshot(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @classmethod
    def load(cls, path) -> "AssemblyService":
        return cls.from_snapshot(json.loads(Path(path).read_text(encoding="utf-8")))

    # ------------------------------------------------------------------ 内部

    def _get(self, hypothesis_id: str) -> Hypothesis:
        try:
            return self._hypotheses[hypothesis_id]
        except KeyError:
            raise UnknownEntityError(f"假设不存在: {hypothesis_id}") from None

    def _check_version(self, hyp: Hypothesis, expected_version: int) -> None:
        if hyp.version != expected_version:
            raise VersionConflictError(hyp.hypothesis_id, expected_version, hyp.version)

    def _validate_payload(self, fragment_ids, candidate_edges) -> None:
        fragment_ids = tuple(fragment_ids)
        if not fragment_ids:
            raise ValueError("假设至少包含一块残片")
        if len(set(fragment_ids)) != len(fragment_ids):
            raise ValueError(f"假设内残片重复: {fragment_ids}")
        for frag_id in fragment_ids:
            if frag_id not in self._fragments:
                raise UnknownEntityError(f"残片未登记: {frag_id}")
        low, high = self._score_range
        in_scope = set(fragment_ids)
        for edge in candidate_edges:
            if not low <= edge.score <= high:
                raise ValueError(
                    f"候选边分值 {edge.score} 超出契约范围 [{low}, {high}]"
                )
            for endpoint in (edge.left, edge.right):
                frag_id, edge_id = split_endpoint(endpoint)
                if frag_id not in in_scope:
                    raise ValueError(f"候选边端点 {endpoint} 不在假设残片集合内")
                fragment = self._fragments[frag_id]
                if edge_id not in fragment.edges:
                    raise UnknownEntityError(f"残片 {frag_id} 没有断面边 {edge_id}")

    def _transition(self, hyp: Hypothesis, state: HypothesisState, actor: str, kind: str) -> Hypothesis:
        updated = replace(hyp, state=state)
        self._hypotheses[hyp.hypothesis_id] = updated
        self._record(
            kind,
            actor=actor,
            hypothesis_id=hyp.hypothesis_id,
            vessel_ids=self._vessels_of(updated),
            details={"version": updated.version},
        )
        return updated

    def _vessels_of(self, hyp: Hypothesis) -> tuple:
        seen = []
        for frag_id in hyp.fragment_ids:
            fragment = self._fragments.get(frag_id)
            if fragment and fragment.vessel_id not in seen:
                seen.append(fragment.vessel_id)
        return tuple(seen)

    def _current_endorsers(self, hyp: Hypothesis) -> list:
        latest: dict = {}
        for review in self._reviews:
            if review.hypothesis_id == hyp.hypothesis_id and review.version == hyp.version:
                latest[review.expert_id] = review
        return sorted(eid for eid, r in latest.items() if r.stance is Stance.ENDORSE)

    def _summary(self, hyp: Hypothesis) -> dict:
        return {
            "hypothesis_id": hyp.hypothesis_id,
            "version": hyp.version,
            "state": hyp.state.value,
            "fragment_ids": list(hyp.fragment_ids),
            "vessel_ids": list(self._vessels_of(hyp)),
        }

    def _add_relation(self, src: str, relation: str, dst: str) -> None:
        rel = (src, relation, dst)
        if rel not in self._relations:
            self._relations.append(rel)

    def _record(self, kind: str, actor: str, hypothesis_id, vessel_ids, details) -> None:
        self._seq += 1
        self._events.append(
            Event(
                seq=self._seq,
                kind=kind,
                actor=actor,
                hypothesis_id=hypothesis_id,
                vessel_ids=tuple(vessel_ids),
                details=details,
                ts=self._clock(),
            )
        )

    @staticmethod
    def _shortest_path(adjacency: dict, start: str, goal: str) -> list:
        queue = [(start, [start])]
        visited = {start}
        while queue:
            node, path = queue.pop(0)
            if node == goal:
                return path
            for nxt in sorted(adjacency.get(node, ())):
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append((nxt, path + [nxt]))
        return path


def _summarize_event(event: Event, experts: dict) -> str:
    """把事件翻译成评审会上可直接宣读的一句话。"""
    d = event.details
    hid = event.hypothesis_id
    actor = _actor_label(event.actor, experts)
    if event.kind == "fragment_registered":
        return f"残片 {d['fragment_id']} 登记入图"
    if event.kind == "expert_registered":
        return f"专家 {actor} 登记,角色: {', '.join(d['roles'])}"
    if event.kind == "hypothesis_proposed":
        edges = "、".join(
            f"{e['left']}↔{e['right']}(线索分值 {e['score']})" for e in d["candidate_edges"]
        )
        note = f",替代 {d['supersedes']}" if d.get("supersedes") else ""
        return (
            f"{actor} 提出假设 {hid} v{d['version']}: 残片 {', '.join(d['fragment_ids'])}"
            f"{';候选邻接 ' + edges if edges else ''}{note}(算法分值仅为线索,不作定论)"
        )
    if event.kind == "submitted_for_review":
        return f"假设 {hid} v{d['version']} 送审"
    if event.kind == "hypothesis_adjusted":
        return (
            f"{actor} 将 {hid} 从 v{d['from_version']} 调整为 v{d['to_version']};"
            "旧版本上的签名随之失效,需重新评审"
        )
    if event.kind == "review_recorded":
        stance = "赞成" if d["stance"] == Stance.ENDORSE.value else "否决"
        stale = "(针对旧版本,不计入当前签名)" if d.get("stale") else ""
        offline = "(离线补传)" if d.get("submitted_offline") else ""
        return f"{actor} 对 {hid} v{d['version']} 投{stance}{offline}{stale}: {d['comment']}"
    if event.kind == "review_duplicate_ignored":
        return f"评审 {d['review_id']} 为重复提交(离线重传),已忽略,不重复计票"
    if event.kind == "evidence_recorded":
        kind = "支持" if d["kind"] == EvidenceKind.SUPPORTS.value else "反驳"
        return f"登记证据 {d['evidence_id']}({kind} {hid}): {d['description']}"
    if event.kind == "hypothesis_confirmed":
        endorsers = "、".join(_actor_label(e, experts) for e in d["endorsers"])
        return f"假设 {hid} v{d['version']} 经 {endorsers} 签名确认"
    if event.kind == "confirmation_rejected":
        return f"假设 {hid} v{d['version']} 确认被守卫驳回: {'; '.join(d['failures'])}"
    if event.kind == "hypothesis_invalidated":
        return (
            f"假设 {hid} (v{d['invalidated_version']}) 因证据 {d['evidence_id']} 失效: "
            f"{d['description']}(历史结论保留,残片占用释放)"
        )
    if event.kind == "hypothesis_withdrawn":
        return f"{actor} 撤回假设 {hid} v{d['version']}"
    if event.kind == "version_conflict":
        op = "调整" if d["operation"] == "adjust" else "确认"
        return (
            f"{actor} 基于 v{d['expected_version']} {op} {hid} 时遇到版本冲突: "
            f"当前已是 v{d['current_version']},操作被拒绝"
        )
    return f"{event.kind}: {d}"


def _actor_label(actor: str, experts: dict) -> str:
    expert = experts.get(actor)
    return f"{expert.name}({actor})" if expert else actor

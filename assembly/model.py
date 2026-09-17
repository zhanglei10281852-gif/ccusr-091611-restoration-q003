"""陶片拼接假设协作图的核心数据模型。

算法匹配分值只代表线索强度,任何组合结论都必须经过专家签名与守卫检查;
所有结论以事件形式追加保存,历史结论只失效、不删除。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class HypothesisState(str, Enum):
    DRAFT = "draft"
    UNDER_REVIEW = "under_review"
    CONFIRMED = "confirmed"
    INVALIDATED = "invalidated"
    WITHDRAWN = "withdrawn"


# 仍处于竞争中的状态;失效或撤回的假设会释放其残片占用
ACTIVE_STATES = frozenset(
    {HypothesisState.DRAFT, HypothesisState.UNDER_REVIEW, HypothesisState.CONFIRMED}
)

# 允许直接提出确认请求的状态
CONFIRMABLE_STATES = frozenset({HypothesisState.DRAFT, HypothesisState.UNDER_REVIEW})

# 允许调整内容的状态;已确认组合只能被新证据失效,再提出替代假设
ADJUSTABLE_STATES = frozenset({HypothesisState.DRAFT, HypothesisState.UNDER_REVIEW})


class Stance(str, Enum):
    ENDORSE = "endorse"
    REJECT = "reject"


class EvidenceKind(str, Enum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"


def split_endpoint(endpoint: str) -> tuple[str, str]:
    """把 ``frag-01:e2`` 形式的候选边端点拆成 (残片 id, 边 id)。"""
    frag, sep, edge = endpoint.partition(":")
    if not sep or not frag or not edge:
        raise ValueError(f"非法候选边端点: {endpoint!r}, 期望形如 'frag-01:e2'")
    return frag, edge


@dataclass(frozen=True)
class EdgeFeature:
    """残片某条断面的三维特征摘要。"""

    edge_id: str
    orientation_deg: float
    curvature: float
    roughness: float
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "orientation_deg": self.orientation_deg,
            "curvature": self.curvature,
            "roughness": self.roughness,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, edge_id: str, data: dict) -> "EdgeFeature":
        return cls(
            edge_id=edge_id,
            orientation_deg=float(data["orientation_deg"]),
            curvature=float(data["curvature"]),
            roughness=float(data["roughness"]),
            summary=data.get("summary", ""),
        )


@dataclass(frozen=True)
class Fragment:
    fragment_id: str
    vessel_id: str
    dimensions: dict
    edges: dict  # edge_id -> EdgeFeature

    def to_dict(self) -> dict:
        return {
            "fragment_id": self.fragment_id,
            "vessel_id": self.vessel_id,
            "dimensions": dict(self.dimensions),
            "edges": {eid: ef.to_dict() for eid, ef in self.edges.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Fragment":
        return cls(
            fragment_id=data["fragment_id"],
            vessel_id=data["vessel_id"],
            dimensions=dict(data["dimensions"]),
            edges={
                eid: EdgeFeature.from_dict(eid, ef) for eid, ef in data["edges"].items()
            },
        )


@dataclass(frozen=True)
class CandidateEdge:
    """候选邻接。score 是算法给出的线索强度,不是定论。"""

    left: str  # "frag-01:e2"
    right: str  # "frag-09:e4"
    score: float
    rotation_deg: float = 0.0  # 对齐时 right 残片相对 left 残片的旋转角

    def endpoints(self) -> tuple[tuple[str, str], tuple[str, str]]:
        return split_endpoint(self.left), split_endpoint(self.right)

    def to_dict(self) -> dict:
        return {
            "left": self.left,
            "right": self.right,
            "score": self.score,
            "rotation_deg": self.rotation_deg,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CandidateEdge":
        return cls(
            left=data["left"],
            right=data["right"],
            score=float(data["score"]),
            rotation_deg=float(data.get("rotation_deg", 0.0)),
        )


@dataclass(frozen=True)
class Expert:
    expert_id: str
    name: str
    roles: tuple

    def to_dict(self) -> dict:
        return {"expert_id": self.expert_id, "name": self.name, "roles": list(self.roles)}

    @classmethod
    def from_dict(cls, data: dict) -> "Expert":
        return cls(
            expert_id=data["expert_id"],
            name=data["name"],
            roles=tuple(data["roles"]),
        )


@dataclass(frozen=True)
class Review:
    """专家意见。review_id 由提交方生成,离线重传时凭它去重,不会重复计票。"""

    review_id: str
    hypothesis_id: str
    version: int  # 评审针对的假设版本;版本过期后不计入当前签名
    expert_id: str
    stance: Stance
    comment: str = ""
    submitted_offline: bool = False

    def to_dict(self) -> dict:
        return {
            "review_id": self.review_id,
            "hypothesis_id": self.hypothesis_id,
            "version": self.version,
            "expert_id": self.expert_id,
            "stance": self.stance.value,
            "comment": self.comment,
            "submitted_offline": self.submitted_offline,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Review":
        return cls(
            review_id=data["review_id"],
            hypothesis_id=data["hypothesis_id"],
            version=int(data["version"]),
            expert_id=data["expert_id"],
            stance=Stance(data["stance"]),
            comment=data.get("comment", ""),
            submitted_offline=bool(data.get("submitted_offline", False)),
        )


@dataclass(frozen=True)
class Evidence:
    """断面证据。contradicts 类证据可以使已确认组合失效,但旧结论保留在图中。"""

    evidence_id: str
    hypothesis_id: str
    kind: EvidenceKind
    description: str
    source: str = ""

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "hypothesis_id": self.hypothesis_id,
            "kind": self.kind.value,
            "description": self.description,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Evidence":
        return cls(
            evidence_id=data["evidence_id"],
            hypothesis_id=data["hypothesis_id"],
            kind=EvidenceKind(data["kind"]),
            description=data["description"],
            source=data.get("source", ""),
        )


@dataclass(frozen=True)
class Hypothesis:
    hypothesis_id: str
    version: int
    fragment_ids: tuple
    candidate_edges: tuple  # tuple[CandidateEdge, ...]
    state: HypothesisState
    created_by: str
    supersedes: str | None = None
    invalidated_by: str | None = None  # 使其失效的证据 id

    def to_dict(self) -> dict:
        return {
            "hypothesis_id": self.hypothesis_id,
            "version": self.version,
            "fragment_ids": list(self.fragment_ids),
            "candidate_edges": [e.to_dict() for e in self.candidate_edges],
            "state": self.state.value,
            "created_by": self.created_by,
            "supersedes": self.supersedes,
            "invalidated_by": self.invalidated_by,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Hypothesis":
        return cls(
            hypothesis_id=data["hypothesis_id"],
            version=int(data["version"]),
            fragment_ids=tuple(data["fragment_ids"]),
            candidate_edges=tuple(CandidateEdge.from_dict(e) for e in data["candidate_edges"]),
            state=HypothesisState(data["state"]),
            created_by=data["created_by"],
            supersedes=data.get("supersedes"),
            invalidated_by=data.get("invalidated_by"),
        )

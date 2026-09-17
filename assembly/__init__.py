"""陶片拼接假设协作图。

把残片、断面特征、候选邻接、组合假设和专家意见组织成可回溯的关系图。
算法分值只是线索强度;确认组合必须通过守卫检查与专家签名。
"""
from .guards import GuardReport, SignaturePolicy
from .model import (
    CandidateEdge,
    EdgeFeature,
    Evidence,
    EvidenceKind,
    Expert,
    Fragment,
    Hypothesis,
    HypothesisState,
    Review,
    Stance,
)
from .service import (
    AssemblyService,
    Event,
    GuardRejectionError,
    InvalidTransitionError,
    UnknownEntityError,
    VersionConflictError,
)

__all__ = [
    "AssemblyService",
    "CandidateEdge",
    "EdgeFeature",
    "Event",
    "Evidence",
    "EvidenceKind",
    "Expert",
    "Fragment",
    "GuardRejectionError",
    "GuardReport",
    "Hypothesis",
    "HypothesisState",
    "InvalidTransitionError",
    "Review",
    "SignaturePolicy",
    "Stance",
    "UnknownEntityError",
    "VersionConflictError",
]

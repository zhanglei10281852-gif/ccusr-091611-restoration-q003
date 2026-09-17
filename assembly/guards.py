"""组合确认前的守卫检查:残片排他占用、方向一致性、必需签名。

守卫只回答"当前能否确认",不参考算法分值高低——分值只是线索。
"""
from __future__ import annotations

from dataclasses import dataclass

from .model import Expert, Hypothesis, HypothesisState, Review, Stance


@dataclass(frozen=True)
class SignaturePolicy:
    """确认一个组合所需的签名策略。"""

    required_roles: tuple = ("lead_restorer", "conservation_scientist")
    min_endorsers: int = 2
    veto_on_reject: bool = True  # 当前版本存在有效否决时禁止确认

    def to_dict(self) -> dict:
        return {
            "required_roles": list(self.required_roles),
            "min_endorsers": self.min_endorsers,
            "veto_on_reject": self.veto_on_reject,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SignaturePolicy":
        return cls(
            required_roles=tuple(data["required_roles"]),
            min_endorsers=int(data["min_endorsers"]),
            veto_on_reject=bool(data["veto_on_reject"]),
        )


@dataclass(frozen=True)
class GuardReport:
    """一次守卫评估的结果;failures 为空才允许确认。"""

    hypothesis_id: str
    version: int
    checks: dict  # 检查名 -> 未通过原因元组(空表示通过)

    @property
    def ok(self) -> bool:
        return all(not problems for problems in self.checks.values())

    @property
    def failures(self) -> tuple:
        return tuple(p for problems in self.checks.values() for p in problems)


def check_fragment_exclusivity(
    hypothesis: Hypothesis, all_hypotheses: list
) -> tuple:
    """同一残片不得被另一个已确认组合占用。"""
    problems = []
    for other in all_hypotheses:
        if other.hypothesis_id == hypothesis.hypothesis_id:
            continue
        if other.state is not HypothesisState.CONFIRMED:
            continue
        shared = sorted(set(hypothesis.fragment_ids) & set(other.fragment_ids))
        if shared:
            problems.append(
                f"残片 {', '.join(shared)} 已被已确认组合 "
                f"{other.hypothesis_id} 独占占用"
            )
    return tuple(problems)


class _RotationUnionFind:
    """带旋转势差的并查集,检验一组候选边的方向约束是否自洽。

    每条候选边给出约束: orientation(right) = orientation(left) + rotation (mod 360)。
    若约束环闭合后旋转角不自洽,说明这些边不可能同时成立。
    """

    def __init__(self) -> None:
        self._parent: dict = {}
        self._pot: dict = {}  # 节点到根节点的旋转角

    def _find(self, x: str) -> tuple:
        if x not in self._parent:
            self._parent[x] = x
            self._pot[x] = 0.0
            return x, 0.0
        if self._parent[x] == x:
            return x, 0.0
        root, pot_parent = self._find(self._parent[x])
        self._pot[x] = (self._pot[x] + pot_parent) % 360.0
        self._parent[x] = root
        return root, self._pot[x]

    def add_constraint(self, a: str, b: str, rotation_ab: float, tolerance_deg: float) -> bool:
        """加入约束 orientation(b) = orientation(a) + rotation_ab,返回是否自洽。"""
        root_a, pot_a = self._find(a)
        root_b, pot_b = self._find(b)
        if root_a == root_b:
            # 已有约束意味着 orientation(b) - orientation(a) = pot_a - pot_b
            delta = (pot_a - pot_b - rotation_ab + 180.0) % 360.0 - 180.0
            return abs(delta) <= tolerance_deg
        # 合并:令 root_a 挂到 root_b 下,使新约束成立
        self._parent[root_a] = root_b
        self._pot[root_a] = (rotation_ab - pot_a + pot_b) % 360.0
        return True


def check_orientation_consistency(hypothesis: Hypothesis, tolerance_deg: float = 1.0) -> tuple:
    """同一假设内所有候选边的方向约束必须可同时满足。"""
    problems = []
    used_edges: dict = {}
    uf = _RotationUnionFind()
    for edge in hypothesis.candidate_edges:
        (frag_l, _), (frag_r, _) = edge.endpoints()
        if frag_l == frag_r:
            problems.append(
                f"候选边 {edge.left} ↔ {edge.right} 连接同一残片,方向不可能自洽"
            )
            continue
        for endpoint in (edge.left, edge.right):
            if endpoint in used_edges:
                problems.append(
                    f"残片边 {endpoint} 被候选边 {used_edges[endpoint]} 与 "
                    f"{edge.left} ↔ {edge.right} 重复使用"
                )
            else:
                used_edges[endpoint] = f"{edge.left} ↔ {edge.right}"
        if not uf.add_constraint(frag_l, frag_r, edge.rotation_deg, tolerance_deg):
            problems.append(
                f"候选边 {edge.left} ↔ {edge.right} 要求相对旋转 "
                f"{edge.rotation_deg}°,与已有方向约束矛盾"
            )
    return tuple(problems)


def check_required_signatures(
    hypothesis: Hypothesis,
    reviews: list,
    experts: dict,
    policy: SignaturePolicy,
) -> tuple:
    """确认需要当前版本上的足够签名;同一专家以其最新立场计票。"""
    latest: dict = {}  # expert_id -> 该专家针对当前版本的最新评审
    for review in reviews:
        if review.hypothesis_id != hypothesis.hypothesis_id:
            continue
        if review.version != hypothesis.version:
            continue
        latest[review.expert_id] = review

    endorsers = sorted(eid for eid, r in latest.items() if r.stance is Stance.ENDORSE)
    rejecters = sorted(eid for eid, r in latest.items() if r.stance is Stance.REJECT)

    problems = []
    if policy.veto_on_reject and rejecters:
        names = ", ".join(_expert_label(experts, eid) for eid in rejecters)
        problems.append(f"专家 {names} 对当前版本投了否决")
    if len(endorsers) < policy.min_endorsers:
        problems.append(
            f"当前版本有效赞成签名 {len(endorsers)} 个,不足要求的 {policy.min_endorsers} 个"
        )
    covered = set()
    for eid in endorsers:
        expert = experts.get(eid)
        if expert:
            covered.update(expert.roles)
    missing = [role for role in policy.required_roles if role not in covered]
    if missing:
        problems.append(f"缺少必需角色的签名: {', '.join(missing)}")
    return tuple(problems)


def _expert_label(experts: dict, expert_id: str) -> str:
    expert: Expert | None = experts.get(expert_id)
    return f"{expert.name}({expert_id})" if expert else expert_id

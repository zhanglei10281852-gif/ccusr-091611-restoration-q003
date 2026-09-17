#!/usr/bin/env python3
"""拼接假设协作图命令行接口。

用法:
    python tools/assembly_cli.py demo [--store PATH]   运行演示场景并保存状态
    python tools/assembly_cli.py feasible [--store PATH]    当前可行组合
    python tools/assembly_cli.py conflicts [--store PATH]   冲突路径
    python tools/assembly_cli.py replay VESSEL [--store PATH]  重放器物决策史
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assembly import (  # noqa: E402
    AssemblyService,
    CandidateEdge,
    EdgeFeature,
    Evidence,
    EvidenceKind,
    Expert,
    Fragment,
    GuardRejectionError,
    Review,
    SignaturePolicy,
    Stance,
    VersionConflictError,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STORE = ROOT / "build" / "demo_store.json"


def load_examples() -> tuple:
    contract = json.loads((ROOT / "domain" / "contract.json").read_text(encoding="utf-8"))
    fragments = [Fragment.from_dict(d) for d in json.loads((ROOT / "examples" / "fragments.json").read_text(encoding="utf-8"))]
    experts = [Expert.from_dict(d) for d in json.loads((ROOT / "examples" / "experts.json").read_text(encoding="utf-8"))]
    hypotheses = json.loads((ROOT / "examples" / "hypotheses.json").read_text(encoding="utf-8"))
    policy = SignaturePolicy.from_dict(contract["signature_policy"])
    return policy, fragments, experts, hypotheses


def build_demo() -> AssemblyService:
    """重演一次完整的协作过程:竞争、确认、证据推翻、冲突、重组。"""
    policy, fragments, experts, hypotheses = load_examples()
    svc = AssemblyService(policy=policy, score_range=tuple([0.0, 1.0]))
    for frag in fragments:
        svc.register_fragment(frag)
    for expert in experts:
        svc.register_expert(expert)

    # 1. 两组都很有说服力的方案同时被提出,frag-09 被双方占用
    for item in hypotheses:
        svc.propose_hypothesis(
            item["hypothesis_id"],
            item["fragment_ids"],
            [CandidateEdge.from_dict(e) for e in item["candidate_edges"]],
            created_by="exp-01",
        )
        svc.submit_for_review(item["hypothesis_id"], expected_version=1)

    # 2. hyp-21 获得签名;林岚的评审离线补传,重传一次不应重复计票
    svc.add_review(Review("rev-001", "hyp-21", 1, "exp-01", Stance.ENDORSE, "断面云母片走向连续", submitted_offline=True))
    svc.add_review(Review("rev-002", "hyp-21", 1, "exp-02", Stance.ENDORSE, "胎土成分一致"))
    svc.add_review(Review("rev-001", "hyp-21", 1, "exp-01", Stance.ENDORSE, "断面云母片走向连续", submitted_offline=True))
    svc.confirm("hyp-21", expected_version=1, decided_by="exp-01")

    # 3. hyp-22 也想确认,但 frag-09 已被独占;周邈同时留下断面否决依据
    svc.add_review(Review("rev-003", "hyp-22", 1, "exp-01", Stance.ENDORSE, "分值可观,建议拼接"))
    svc.add_review(Review("rev-004", "hyp-22", 1, "exp-02", Stance.ENDORSE, "备选方案"))
    try:
        svc.confirm("hyp-22", expected_version=1, decided_by="exp-01")
    except GuardRejectionError:
        pass
    svc.add_review(Review("rev-005", "hyp-22", 1, "exp-03", Stance.REJECT,
                          "frag-09:e1 与 frag-14:e3 断面氧化层理方向相反,暂缓"))

    # 4. 新证据推翻已确认的 hyp-21;旧结论保留在图中
    svc.add_evidence(Evidence(
        "ev-501", "hyp-21", EvidenceKind.CONTRADICTS,
        "显微CT显示 frag-01:e2 与 frag-09:e4 断面胎土成分突变,非同一器物",
        source="lab-ct",
    ))

    # 5. 两位修复师同时基于 v1 调整 hyp-22:一人成功,另一人看到版本冲突
    svc.adjust_hypothesis("hyp-22", expected_version=1, actor="exp-01",
                          candidate_edges=[CandidateEdge("frag-09:e1", "frag-14:e3", 0.77, 91.0)])
    try:
        svc.adjust_hypothesis("hyp-22", expected_version=1, actor="exp-03",
                              candidate_edges=[CandidateEdge("frag-09:e1", "frag-14:e3", 0.77, 95.0)])
    except VersionConflictError:
        pass

    # 6. v2 上重新签名后确认 hyp-22(frag-09 已随 hyp-21 失效而释放)
    svc.add_review(Review("rev-006", "hyp-22", 2, "exp-01", Stance.ENDORSE, "旋转角修正后断面吻合"))
    svc.add_review(Review("rev-007", "hyp-22", 2, "exp-02", Stance.ENDORSE, "成分与层理均支持"))
    svc.confirm("hyp-22", expected_version=2, decided_by="exp-03")

    # 7. 针对被推翻的 hyp-21 提出替代组合并确认
    svc.propose_hypothesis("hyp-23", ["frag-01", "frag-07"],
                           [CandidateEdge("frag-01:e1", "frag-07:e1", 0.68, 2.0)],
                           created_by="exp-03", supersedes="hyp-21")
    svc.submit_for_review("hyp-23", expected_version=1)
    svc.add_review(Review("rev-008", "hyp-23", 1, "exp-01", Stance.ENDORSE, "层理连续"))
    svc.add_review(Review("rev-009", "hyp-23", 1, "exp-02", Stance.ENDORSE, "胎土成分与出土层位一致"))
    svc.confirm("hyp-23", expected_version=1, decided_by="exp-03")

    # 8. 周邈提出新的竞争假设,与两个已确认组合争夺残片,形成冲突路径
    svc.propose_hypothesis("hyp-24", ["frag-09", "frag-07"],
                           [CandidateEdge("frag-09:e2", "frag-07:e4", 0.71, 88.0)],
                           created_by="exp-03")

    # 9. vessel-B 的独立小组合,用于对照 replay 的器物过滤
    svc.propose_hypothesis("hyp-31", ["frag-20", "frag-21"],
                           [CandidateEdge("frag-20:e1", "frag-21:e1", 0.9, 180.0)],
                           created_by="exp-01")
    svc.submit_for_review("hyp-31", expected_version=1)
    svc.add_review(Review("rev-010", "hyp-31", 1, "exp-01", Stance.ENDORSE, "薄胎吻合"))
    svc.add_review(Review("rev-011", "hyp-31", 1, "exp-02", Stance.ENDORSE, "成分一致"))
    svc.confirm("hyp-31", expected_version=1, decided_by="exp-01")
    return svc


def print_feasible(svc: AssemblyService) -> None:
    report = svc.current_feasible()
    print("== 当前可行组合 ==")
    for item in report["confirmed"]:
        print(f"  [已确认] {item['hypothesis_id']} v{item['version']}: {', '.join(item['fragment_ids'])}")
    for item in report["confirmable"]:
        print(f"  [可确认] {item['hypothesis_id']} v{item['version']}: {', '.join(item['fragment_ids'])}")
    for item in report["blocked"]:
        print(f"  [被拦截] {item['hypothesis_id']} v{item['version']} ({item['state']}): {', '.join(item['fragment_ids'])}")
        for failure in item["failures"]:
            print(f"      - {failure}")


def print_conflicts(svc: AssemblyService) -> None:
    components = svc.conflict_paths()
    print("== 冲突路径 ==")
    if not components:
        print("  当前没有残片争夺")
    for comp in components:
        print(f"  争夺残片: {', '.join(comp['contested_fragments'])}")
        for path in comp["paths"]:
            print(f"    {' -> '.join(path['via'])}")


def print_replay(svc: AssemblyService, vessel_id: str) -> None:
    print(f"== {vessel_id} 决策重放 ==")
    for entry in svc.replay(vessel_id):
        print(f"  #{entry['seq']:02d} {entry['summary']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["demo", "feasible", "conflicts", "replay"])
    parser.add_argument("vessel", nargs="?", help="replay 的器物 id")
    parser.add_argument("--store", default=str(DEFAULT_STORE), help="状态文件路径")
    args = parser.parse_args()

    if args.command == "demo":
        svc = build_demo()
        store = Path(args.store)
        store.parent.mkdir(parents=True, exist_ok=True)
        svc.save(store)
        print(f"演示场景已写入 {store}\n")
        print_feasible(svc)
        print()
        print_conflicts(svc)
        print()
        print_replay(svc, "vessel-A")
        return

    svc = AssemblyService.load(args.store)
    if args.command == "feasible":
        print_feasible(svc)
    elif args.command == "conflicts":
        print_conflicts(svc)
    elif args.command == "replay":
        if not args.vessel:
            parser.error("replay 需要器物 id,例如: replay vessel-A")
        print_replay(svc, args.vessel)


if __name__ == "__main__":
    main()

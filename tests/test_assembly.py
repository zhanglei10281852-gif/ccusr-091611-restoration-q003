"""拼接假设协作图的行为测试,逐条对应需求:

- 算法分值不是定论(确认只看守卫与签名)
- 确认前检查残片排他占用、方向一致性、必需签名
- 新证据使已确认组合失效,但旧结论不删除
- 并发调整暴露版本冲突
- 离线意见重传不重复计票
- 接口给出当前可行组合、冲突路径与器物决策重放
"""
import json
import tempfile
import unittest
from pathlib import Path

from assembly import (
    AssemblyService,
    CandidateEdge,
    EdgeFeature,
    Evidence,
    EvidenceKind,
    Expert,
    Fragment,
    GuardRejectionError,
    HypothesisState,
    InvalidTransitionError,
    Review,
    SignaturePolicy,
    Stance,
    UnknownEntityError,
    VersionConflictError,
)

ROOT = Path(__file__).resolve().parents[1]


def make_fragment(frag_id, vessel="vessel-A", edges=("e1", "e2", "e3", "e4")):
    return Fragment(
        fragment_id=frag_id,
        vessel_id=vessel,
        dimensions={"length_mm": 50.0, "width_mm": 30.0, "thickness_mm": 6.0},
        edges={
            eid: EdgeFeature(eid, orientation_deg=0.0, curvature=0.03, roughness=0.4)
            for eid in edges
        },
    )


def endorse(review_id, hyp_id, version, expert):
    return Review(review_id, hyp_id, version, expert, Stance.ENDORSE, "同意")


class AssemblyTestCase(unittest.TestCase):
    def setUp(self):
        self._time = [1000.0]
        self.svc = AssemblyService(clock=lambda: self._time[0])
        for fid in ("frag-01", "frag-07", "frag-09", "frag-14"):
            self.svc.register_fragment(make_fragment(fid))
        self.svc.register_fragment(make_fragment("frag-20", vessel="vessel-B", edges=("e1", "e2")))
        self.svc.register_fragment(make_fragment("frag-21", vessel="vessel-B", edges=("e1", "e2")))
        self.svc.register_expert(Expert("exp-01", "林岚", ("lead_restorer",)))
        self.svc.register_expert(Expert("exp-02", "赵其", ("conservation_scientist",)))
        self.svc.register_expert(Expert("exp-03", "周邈", ("archaeologist", "lead_restorer")))

    def propose(self, hyp_id="hyp-1", fragments=("frag-01", "frag-09"),
                edges=None, **kwargs):
        edges = edges if edges is not None else [
            CandidateEdge("frag-01:e2", "frag-09:e4", 0.82, 178.5)
        ]
        return self.svc.propose_hypothesis(hyp_id, list(fragments), edges,
                                           created_by="exp-01", **kwargs)

    def sign_and_confirm(self, hyp_id, version=1):
        self.svc.add_review(endorse(f"r-{hyp_id}-a", hyp_id, version, "exp-01"))
        self.svc.add_review(endorse(f"r-{hyp_id}-b", hyp_id, version, "exp-02"))
        return self.svc.confirm(hyp_id, expected_version=version, decided_by="exp-01")


class TestScoreIsNotVerdict(AssemblyTestCase):
    def test_high_score_cannot_bypass_signatures(self):
        self.propose(edges=[CandidateEdge("frag-01:e2", "frag-09:e4", 0.99, 178.5)])
        with self.assertRaises(GuardRejectionError) as ctx:
            self.svc.confirm("hyp-1", expected_version=1, decided_by="exp-01")
        self.assertTrue(any("签名" in f for f in ctx.exception.report.failures))

    def test_low_score_can_confirm_with_full_signatures(self):
        self.propose(edges=[CandidateEdge("frag-01:e2", "frag-09:e4", 0.30, 178.5)])
        hyp = self.sign_and_confirm("hyp-1")
        self.assertIs(hyp.state, HypothesisState.CONFIRMED)


class TestConfirmGuards(AssemblyTestCase):
    def test_fragment_exclusivity(self):
        self.propose("hyp-1")
        self.sign_and_confirm("hyp-1")
        self.propose("hyp-2", fragments=("frag-09", "frag-14"),
                     edges=[CandidateEdge("frag-09:e1", "frag-14:e3", 0.77, 93.0)])
        self.svc.add_review(endorse("r-2a", "hyp-2", 1, "exp-01"))
        self.svc.add_review(endorse("r-2b", "hyp-2", 1, "exp-02"))
        with self.assertRaises(GuardRejectionError) as ctx:
            self.svc.confirm("hyp-2", expected_version=1, decided_by="exp-01")
        self.assertTrue(any("frag-09" in f and "hyp-1" in f for f in ctx.exception.report.failures))
        # 驳回原因(含断面证据讨论)进入事件日志,可回溯
        rejected = [e for e in self.svc.events if e.kind == "confirmation_rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertTrue(any("frag-09" in f for f in rejected[0].details["failures"]))

    def test_orientation_cycle_conflict(self):
        edges = [
            CandidateEdge("frag-01:e1", "frag-09:e1", 0.8, 90.0),
            CandidateEdge("frag-09:e2", "frag-14:e1", 0.8, 90.0),
            CandidateEdge("frag-14:e2", "frag-01:e2", 0.8, 90.0),  # 环闭合应需 270°,矛盾
        ]
        self.propose("hyp-1", fragments=("frag-01", "frag-09", "frag-14"), edges=edges)
        self.svc.add_review(endorse("r-a", "hyp-1", 1, "exp-01"))
        self.svc.add_review(endorse("r-b", "hyp-1", 1, "exp-02"))
        with self.assertRaises(GuardRejectionError) as ctx:
            self.svc.confirm("hyp-1", expected_version=1, decided_by="exp-01")
        self.assertTrue(any("方向约束矛盾" in f for f in ctx.exception.report.failures))

    def test_orientation_consistent_cycle_passes(self):
        edges = [
            CandidateEdge("frag-01:e1", "frag-09:e1", 0.8, 90.0),
            CandidateEdge("frag-09:e2", "frag-14:e1", 0.8, 90.0),
            CandidateEdge("frag-14:e2", "frag-01:e2", 0.8, 180.0),  # 90+90+180 = 360,自洽
        ]
        self.propose("hyp-1", fragments=("frag-01", "frag-09", "frag-14"), edges=edges)
        hyp = self.sign_and_confirm("hyp-1")
        self.assertIs(hyp.state, HypothesisState.CONFIRMED)

    def test_orientation_duplicate_edge_use(self):
        edges = [
            CandidateEdge("frag-01:e1", "frag-09:e1", 0.8, 90.0),
            CandidateEdge("frag-01:e1", "frag-14:e1", 0.8, 90.0),  # 同一断面边重复使用
        ]
        self.propose("hyp-1", fragments=("frag-01", "frag-09", "frag-14"), edges=edges)
        self.svc.add_review(endorse("r-a", "hyp-1", 1, "exp-01"))
        self.svc.add_review(endorse("r-b", "hyp-1", 1, "exp-02"))
        with self.assertRaises(GuardRejectionError) as ctx:
            self.svc.confirm("hyp-1", expected_version=1, decided_by="exp-01")
        self.assertTrue(any("重复使用" in f for f in ctx.exception.report.failures))

    def test_required_roles_must_sign(self):
        self.propose("hyp-1")
        # 两位修复师,但缺少 conservation_scientist 角色
        self.svc.add_review(endorse("r-a", "hyp-1", 1, "exp-01"))
        self.svc.add_review(endorse("r-b", "hyp-1", 1, "exp-03"))
        with self.assertRaises(GuardRejectionError) as ctx:
            self.svc.confirm("hyp-1", expected_version=1, decided_by="exp-01")
        self.assertTrue(any("conservation_scientist" in f for f in ctx.exception.report.failures))

    def test_reject_vetoes_current_version(self):
        self.propose("hyp-1")
        self.svc.add_review(endorse("r-a", "hyp-1", 1, "exp-01"))
        self.svc.add_review(endorse("r-b", "hyp-1", 1, "exp-02"))
        self.svc.add_review(Review("r-c", "hyp-1", 1, "exp-03", Stance.REJECT, "断面釉层不连续"))
        with self.assertRaises(GuardRejectionError) as ctx:
            self.svc.confirm("hyp-1", expected_version=1, decided_by="exp-01")
        self.assertTrue(any("否决" in f for f in ctx.exception.report.failures))

    def test_confirmed_hypothesis_cannot_be_adjusted(self):
        self.propose("hyp-1")
        self.sign_and_confirm("hyp-1")
        with self.assertRaises(InvalidTransitionError):
            self.svc.adjust_hypothesis("hyp-1", expected_version=1, actor="exp-01",
                                       fragment_ids=["frag-01", "frag-14"])


class TestVersionConflict(AssemblyTestCase):
    def test_concurrent_adjust_exposes_conflict(self):
        self.propose("hyp-1")
        self.svc.adjust_hypothesis("hyp-1", expected_version=1, actor="exp-01",
                                   candidate_edges=[CandidateEdge("frag-01:e2", "frag-09:e4", 0.82, 180.0)])
        with self.assertRaises(VersionConflictError) as ctx:
            self.svc.adjust_hypothesis("hyp-1", expected_version=1, actor="exp-03",
                                       candidate_edges=[CandidateEdge("frag-01:e2", "frag-09:e4", 0.82, 175.0)])
        self.assertEqual((ctx.exception.expected, ctx.exception.current), (1, 2))
        # 冲突本身也记入决策史,评审会上可见
        conflicts = [e for e in self.svc.events if e.kind == "version_conflict"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].actor, "exp-03")
        self.assertEqual(conflicts[0].details["current_version"], 2)

    def test_confirm_with_stale_version_conflicts(self):
        self.propose("hyp-1")
        self.svc.adjust_hypothesis("hyp-1", expected_version=1, actor="exp-01",
                                   candidate_edges=[CandidateEdge("frag-01:e2", "frag-09:e4", 0.82, 180.0)])
        with self.assertRaises(VersionConflictError):
            self.svc.confirm("hyp-1", expected_version=1, decided_by="exp-01")

    def test_adjust_resets_signature_base(self):
        self.propose("hyp-1")
        self.svc.add_review(endorse("r-a", "hyp-1", 1, "exp-01"))
        self.svc.add_review(endorse("r-b", "hyp-1", 1, "exp-02"))
        self.svc.adjust_hypothesis("hyp-1", expected_version=1, actor="exp-01",
                                   candidate_edges=[CandidateEdge("frag-01:e2", "frag-09:e4", 0.82, 180.0)])
        # v1 的签名不计入 v2
        with self.assertRaises(GuardRejectionError) as ctx:
            self.svc.confirm("hyp-1", expected_version=2, decided_by="exp-01")
        self.assertTrue(any("签名" in f for f in ctx.exception.report.failures))
        self.sign_and_confirm("hyp-1", version=2)


class TestOfflineReviewIdempotency(AssemblyTestCase):
    def test_retransmission_not_double_counted(self):
        self.propose("hyp-1")
        review = Review("rev-offline-1", "hyp-1", 1, "exp-01", Stance.ENDORSE,
                        "断面吻合", submitted_offline=True)
        _, created_first = self.svc.add_review(review)
        _, created_second = self.svc.add_review(review)
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        duplicates = [e for e in self.svc.events if e.kind == "review_duplicate_ignored"]
        self.assertEqual(len(duplicates), 1)
        # 只计一票:还需另一位专家才能满足 min_endorsers=2
        with self.assertRaises(GuardRejectionError):
            self.svc.confirm("hyp-1", expected_version=1, decided_by="exp-02")
        self.svc.add_review(endorse("rev-2", "hyp-1", 1, "exp-02"))
        hyp = self.svc.confirm("hyp-1", expected_version=1, decided_by="exp-02")
        self.assertIs(hyp.state, HypothesisState.CONFIRMED)

    def test_unknown_expert_rejected(self):
        self.propose("hyp-1")
        with self.assertRaises(UnknownEntityError):
            self.svc.add_review(endorse("r-x", "hyp-1", 1, "ghost"))


class TestEvidenceInvalidation(AssemblyTestCase):
    def test_contradicting_evidence_invalidates_but_history_kept(self):
        self.propose("hyp-1")
        self.sign_and_confirm("hyp-1")
        invalidated, _ = self.svc.add_evidence(Evidence(
            "ev-1", "hyp-1", EvidenceKind.CONTRADICTS,
            "显微CT显示断面胎土成分突变", source="lab-ct"))
        self.assertEqual(invalidated, ["hyp-1"])
        hyp = self.svc.get_hypothesis("hyp-1")
        self.assertIs(hyp.state, HypothesisState.INVALIDATED)
        self.assertEqual(hyp.invalidated_by, "ev-1")
        # 旧结论未被删除:确认与失效事件都留在日志里
        kinds = [e.kind for e in self.svc.events]
        self.assertIn("hypothesis_confirmed", kinds)
        self.assertIn("hypothesis_invalidated", kinds)
        # 关系图中证据与假设的 contradicts 边保留
        self.assertIn(("evidence:ev-1", "contradicts", "hypothesis:hyp-1"),
                      self.svc.relations())

    def test_invalidation_releases_fragments(self):
        self.propose("hyp-1")
        self.sign_and_confirm("hyp-1")
        self.svc.add_evidence(Evidence("ev-1", "hyp-1", EvidenceKind.CONTRADICTS, "成分突变"))
        self.propose("hyp-2", fragments=("frag-09", "frag-14"),
                     edges=[CandidateEdge("frag-09:e1", "frag-14:e3", 0.77, 93.0)])
        hyp = self.sign_and_confirm("hyp-2")
        self.assertIs(hyp.state, HypothesisState.CONFIRMED)

    def test_supporting_evidence_keeps_confirmation(self):
        self.propose("hyp-1")
        self.sign_and_confirm("hyp-1")
        invalidated, _ = self.svc.add_evidence(Evidence(
            "ev-2", "hyp-1", EvidenceKind.SUPPORTS, "出土层位一致"))
        self.assertEqual(invalidated, [])
        self.assertIs(self.svc.get_hypothesis("hyp-1").state, HypothesisState.CONFIRMED)


class TestQueries(AssemblyTestCase):
    def build_competition(self):
        """hyp-1 已确认占用 frag-09,hyp-2 在审也想要 frag-09。"""
        self.propose("hyp-1")
        self.sign_and_confirm("hyp-1")
        self.propose("hyp-2", fragments=("frag-09", "frag-14"),
                     edges=[CandidateEdge("frag-09:e1", "frag-14:e3", 0.77, 93.0)])

    def test_current_feasible(self):
        self.build_competition()
        report = self.svc.current_feasible()
        self.assertEqual([h["hypothesis_id"] for h in report["confirmed"]], ["hyp-1"])
        self.assertEqual(report["confirmable"], [])
        blocked = {h["hypothesis_id"]: h for h in report["blocked"]}
        self.assertIn("hyp-2", blocked)
        self.assertTrue(any("frag-09" in f for f in blocked["hyp-2"]["failures"]))

    def test_conflict_paths(self):
        self.build_competition()
        self.propose("hyp-3", fragments=("frag-14", "frag-07"),
                     edges=[CandidateEdge("frag-14:e1", "frag-07:e1", 0.6, 2.0)])
        components = self.svc.conflict_paths()
        self.assertEqual(len(components), 1)
        comp = components[0]
        self.assertEqual(comp["contested_fragments"], ["frag-09", "frag-14"])
        paths = {(p["from"], p["to"]): p["via"] for p in comp["paths"]}
        self.assertEqual(paths[("hyp-1", "hyp-2")], ["hyp-1", "frag-09", "hyp-2"])
        # 跨两块残片的传递冲突路径
        self.assertEqual(paths[("hyp-1", "hyp-3")],
                         ["hyp-1", "frag-09", "hyp-2", "frag-14", "hyp-3"])

    def test_no_conflict_when_all_resolved(self):
        self.propose("hyp-1")
        self.sign_and_confirm("hyp-1")
        self.assertEqual(self.svc.conflict_paths(), [])

    def test_replay_vessel_decision_trail(self):
        self.build_competition()
        self.svc.add_evidence(Evidence("ev-1", "hyp-1", EvidenceKind.CONTRADICTS, "成分突变"))
        self.svc.propose_hypothesis("hyp-9", ["frag-01", "frag-07"],
                                    [CandidateEdge("frag-01:e1", "frag-07:e1", 0.6, 2.0)],
                                    created_by="exp-03", supersedes="hyp-1")
        # vessel-B 的噪声不应出现在 vessel-A 的重放里
        self.svc.propose_hypothesis("hyp-b", ["frag-20", "frag-21"],
                                    [CandidateEdge("frag-20:e1", "frag-21:e1", 0.9, 180.0)],
                                    created_by="exp-01")
        trail = self.svc.replay("vessel-A")
        kinds = [entry["kind"] for entry in trail]
        self.assertIn("hypothesis_proposed", kinds)
        self.assertIn("hypothesis_confirmed", kinds)
        self.assertIn("hypothesis_invalidated", kinds)
        self.assertNotIn("hyp-b", {entry["hypothesis_id"] for entry in trail})
        # 评审会能讲清是哪条证据改变了结论
        inv = next(e for e in trail if e["kind"] == "hypothesis_invalidated")
        self.assertEqual(inv["details"]["evidence_id"], "ev-1")
        self.assertIn("成分突变", inv["summary"])
        seqs = [entry["seq"] for entry in trail]
        self.assertEqual(seqs, sorted(seqs))


class TestPersistence(AssemblyTestCase):
    def test_snapshot_round_trip(self):
        self.propose("hyp-1")
        self.sign_and_confirm("hyp-1")
        self.svc.add_evidence(Evidence("ev-1", "hyp-1", EvidenceKind.CONTRADICTS, "成分突变"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.json"
            self.svc.save(path)
            loaded = AssemblyService.load(path)
        self.assertEqual(loaded.replay("vessel-A"), self.svc.replay("vessel-A"))
        self.assertEqual(loaded.current_feasible(), self.svc.current_feasible())
        self.assertEqual(loaded.relations(), self.svc.relations())
        # 加载后幂等键仍然有效
        review = Review("rev-x", "hyp-1", 1, "exp-01", Stance.ENDORSE, "迟到意见")
        loaded.add_review(review)
        _, created = loaded.add_review(review)
        self.assertFalse(created)


class TestExamplesAndContract(unittest.TestCase):
    def test_examples_load_into_service(self):
        contract = json.loads((ROOT / "domain" / "contract.json").read_text(encoding="utf-8"))
        svc = AssemblyService(
            policy=SignaturePolicy.from_dict(contract["signature_policy"]),
            score_range=tuple(contract["score_range"]),
        )
        for data in json.loads((ROOT / "examples" / "fragments.json").read_text(encoding="utf-8")):
            svc.register_fragment(Fragment(
                fragment_id=data["fragment_id"],
                vessel_id=data["vessel_id"],
                dimensions=data["dimensions"],
                edges={eid: EdgeFeature.from_dict(eid, ef) for eid, ef in data["edges"].items()},
            ))
        for data in json.loads((ROOT / "examples" / "experts.json").read_text(encoding="utf-8")):
            svc.register_expert(Expert(data["expert_id"], data["name"], tuple(data["roles"])))
        for item in json.loads((ROOT / "examples" / "hypotheses.json").read_text(encoding="utf-8")):
            hyp = svc.propose_hypothesis(
                item["hypothesis_id"], item["fragment_ids"],
                [CandidateEdge.from_dict(e) for e in item["candidate_edges"]],
                created_by="exp-01")
            self.assertEqual(hyp.version, 1)
        # 两组方案争夺 frag-09,冲突路径立即可见
        components = svc.conflict_paths()
        self.assertEqual(len(components), 1)
        self.assertEqual(components[0]["contested_fragments"], ["frag-09"])

    def test_score_out_of_range_rejected(self):
        svc = AssemblyService()
        svc.register_fragment(make_fragment("frag-01"))
        svc.register_fragment(make_fragment("frag-09"))
        with self.assertRaises(ValueError):
            svc.propose_hypothesis("hyp-bad", ["frag-01", "frag-09"],
                                   [CandidateEdge("frag-01:e1", "frag-09:e1", 1.5, 0.0)],
                                   created_by="exp-01")


if __name__ == "__main__":
    unittest.main()

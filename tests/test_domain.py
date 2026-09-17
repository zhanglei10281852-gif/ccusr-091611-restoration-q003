"""纯领域闸门测试：方向一致性、必需签名、排他占用、对立证据、结构。"""
from __future__ import annotations

import unittest

from app import domain


def frag(fid, features=("e1",)):
    return {"fragment_id": fid, "features": {f: {"feature_id": f} for f in features}}


def cand(cid, a, b, rotation=0, signatures=("curve_profile", "cross_section")):
    return {
        "candidate_id": cid, "group_id": "g1",
        "left": {"fragment_id": a[0], "feature_id": a[1]},
        "right": {"fragment_id": b[0], "feature_id": b[1]},
        "score": 0.9, "rotation": rotation,
        "signatures": {s: {"value": 1} for s in signatures},
    }


def hyp(hid, fids, cids, state="under_review"):
    return {"hypothesis_id": hid, "group_id": "g1", "state": state,
            "fragment_ids": fids, "candidate_ids": cids, "version": 1}


class GateTests(unittest.TestCase):
    def run_gates(self, h, fragments, candidates, hypotheses=None, evidence=None):
        return domain.evaluate_gates(
            h, fragments={f["fragment_id"]: f for f in fragments},
            candidates={c["candidate_id"]: c for c in candidates},
            hypotheses=hypotheses or {h["hypothesis_id"]: h},
            evidence=evidence or {},
        )

    def test_consistent_orientation_three_fragment_ring(self):
        # A-0-B-90-C；C 经 270° 边回到 A（反向约束 -270 ≡ 90），方向闭合一致
        fragments = [frag("A", ("e1", "e2")), frag("B", ("e1", "e2")),
                     frag("C", ("e1", "e2"))]
        candidates = [
            cand("c1", ("A", "e1"), ("B", "e1"), 0),
            cand("c2", ("B", "e2"), ("C", "e1"), 90),
            cand("c3", ("C", "e2"), ("A", "e2"), 270),
        ]
        h = hyp("h1", ["A", "B", "C"], ["c1", "c2", "c3"])
        report = self.run_gates(h, fragments, candidates)
        orient = next(c for c in report["checks"] if c["gate"] == "orientation")
        self.assertTrue(orient["passed"], orient["violations"])
        self.assertTrue(report["passed"])

    def test_contradictory_orientation_path(self):
        # 两条路径对 C 给出不同方向
        fragments = [frag("A", ("e1", "e2")), frag("B", ("e1",)),
                     frag("C", ("e1", "e2")), frag("D", ("e1",))]
        candidates = [
            cand("c1", ("A", "e1"), ("B", "e1"), 0),
            cand("c2", ("B", "e1"), ("C", "e1"), 90),
            cand("c3", ("A", "e2"), ("D", "e1"), 0),
            cand("c4", ("D", "e1"), ("C", "e2"), 180),
        ]
        h = hyp("h1", ["A", "B", "C", "D"], ["c1", "c2", "c3", "c4"])
        report = self.run_gates(h, fragments, candidates)
        orient = next(c for c in report["checks"] if c["gate"] == "orientation")
        self.assertFalse(orient["passed"])
        self.assertIn("方向约束冲突", orient["violations"][0]["message"])

    def test_missing_required_signature_blocks_high_score(self):
        fragments = [frag("A"), frag("B")]
        candidates = [cand("c1", ("A", "e1"), ("B", "e1"),
                           signatures=("curve_profile",))]  # 0.9 分也没用
        h = hyp("h1", ["A", "B"], ["c1"])
        report = self.run_gates(h, fragments, candidates)
        sig = next(c for c in report["checks"] if c["gate"] == "required_signatures")
        self.assertFalse(sig["passed"])
        self.assertEqual(sig["violations"][0]["missing"], ["cross_section"])

    def test_exclusive_occupancy_conflict_path(self):
        fragments = [frag("A"), frag("B"), frag("C")]
        candidates = [cand("c1", ("A", "e1"), ("B", "e1")),
                      cand("c2", ("B", "e1"), ("C", "e1"))]
        h1 = hyp("h1", ["A", "B"], ["c1"], state="confirmed")
        h2 = hyp("h2", ["B", "C"], ["c2"], state="under_review")
        report = self.run_gates(
            h2, fragments, candidates,
            hypotheses={h1["hypothesis_id"]: h1, h2["hypothesis_id"]: h2},
        )
        occ = next(c for c in report["checks"] if c["gate"] == "exclusive_occupancy")
        self.assertFalse(occ["passed"])
        v = occ["violations"][0]
        self.assertEqual(v["fragment_id"], "B")
        self.assertEqual(v["occupied_by"][0]["hypothesis_id"], "h1")
        self.assertTrue(any("h1" in node for node in v["path"]))

    def test_invalidated_hypothesis_does_not_occupy(self):
        fragments = [frag("A"), frag("B")]
        candidates = [cand("c1", ("A", "e1"), ("B", "e1"))]
        h1 = hyp("h1", ["A", "B"], ["c1"], state="invalidated")
        h2 = hyp("h2", ["A", "B"], ["c1"])
        report = self.run_gates(
            h2, fragments, candidates,
            hypotheses={h1["hypothesis_id"]: h1, h2["hypothesis_id"]: h2},
        )
        occ = next(c for c in report["checks"] if c["gate"] == "exclusive_occupancy")
        self.assertTrue(occ["passed"])

    def test_contradicting_evidence_blocks(self):
        fragments = [frag("A"), frag("B")]
        candidates = [cand("c1", ("A", "e1"), ("B", "e1"))]
        h = hyp("h1", ["A", "B"], ["c1"])
        evidence = {"ev-1": {"target": {"type": "candidate", "id": "c1"},
                             "stance": "contradicts"}}
        report = self.run_gates(h, fragments, candidates, evidence=evidence)
        ev = next(c for c in report["checks"] if c["gate"] == "evidence")
        self.assertFalse(ev["passed"])
        self.assertEqual(ev["violations"][0]["evidence_ids"], ["ev-1"])

    def test_structure_rejects_undeclared_fragment_and_reused_feature(self):
        fragments = [frag("A", ("e1",)), frag("B", ("e1", "e2")), frag("C", ("e1",))]
        candidates = [
            cand("c1", ("A", "e1"), ("B", "e1")),
            cand("c2", ("B", "e1"), ("C", "e1")),  # B:e1 被重复使用
        ]
        h = hyp("h1", ["A", "B"], ["c1", "c2"])  # C 未申报
        report = self.run_gates(h, fragments, candidates)
        struct = next(c for c in report["checks"] if c["gate"] == "structure")
        self.assertFalse(struct["passed"])
        messages = " ".join(v["message"] for v in struct["violations"])
        self.assertIn("未申报残片 C", messages)
        self.assertIn("重复占用", messages)


class TallyTests(unittest.TestCase):
    def test_same_expert_latest_review_wins(self):
        experts = {"zhao": {"role": "lead_restorer"}, "lin": {"role": "materials_analyst"}}
        reviews = [
            {"seq": 1, "expert_id": "lin", "decision": "request_changes"},
            {"seq": 2, "expert_id": "lin", "decision": "approve"},
            {"seq": 3, "expert_id": "zhao", "decision": "approve"},
        ]
        tally = domain.tally_reviews(reviews, experts)
        self.assertEqual(tally["votes"], {"approve": 2, "request_changes": 0, "reject": 0})
        self.assertTrue(tally["quorum"])

    def test_quorum_needs_lead(self):
        experts = {"chen": {"role": "restorer"}, "lin": {"role": "materials_analyst"}}
        reviews = [
            {"seq": 1, "expert_id": "chen", "decision": "approve"},
            {"seq": 2, "expert_id": "lin", "decision": "approve"},
        ]
        self.assertFalse(domain.tally_reviews(reviews, experts)["quorum"])

    def test_quorum_fails_on_reject(self):
        experts = {"zhao": {"role": "lead_restorer"}, "lin": {"role": "materials_analyst"}}
        reviews = [
            {"seq": 1, "expert_id": "zhao", "decision": "approve"},
            {"seq": 2, "expert_id": "lin", "decision": "reject"},
        ]
        self.assertFalse(domain.tally_reviews(reviews, experts)["quorum"])


class TransitionTests(unittest.TestCase):
    def test_invalidated_is_terminal(self):
        with self.assertRaises(domain.ConflictError):
            domain.ensure_transition("invalidated", "confirmed")

    def test_legal_transitions(self):
        domain.ensure_transition("draft", "under_review")
        domain.ensure_transition("confirmed", "invalidated")


if __name__ == "__main__":
    unittest.main()

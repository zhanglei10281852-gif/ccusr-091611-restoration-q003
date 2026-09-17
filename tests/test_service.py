"""服务层测试：并发修订、幂等重传、确认闸门、失效保史、持久化重放。"""
from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.errors import ConflictError, GateError, PermissionDenied
from app.seed import seed_baseline
from app.service import AssemblyService
from app.store import EventStore


def fresh_service(path: str = ":memory:") -> AssemblyService:
    return AssemblyService(EventStore(path))


class BaselineFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = fresh_service()
        seed_baseline(self.svc)

    def approve(self, hyp_id: str) -> None:
        self.svc.submit_for_review(hyp_id, "chen")
        for rid, expert, cid in [
            (f"{rid_base}-{hyp_id}", expert, f"client-{expert}-{hyp_id}")
            for rid_base, expert in (("rl", "lin"), ("rc", "chen"), ("rz", "zhao"))
        ]:
            self.svc.cast_review({
                "review_id": rid, "hypothesis_id": hyp_id, "expert_id": expert,
                "decision": "approve", "comment": "同意", "client_request_id": cid,
            })


class ConcurrentRevisionTests(BaselineFixture):
    def test_second_concurrent_revision_gets_conflict(self):
        payload = {
            "hypothesis_id": "hyp-22", "expected_version": 1,
            "fragment_ids": ["frag-09", "frag-14"], "candidate_ids": ["cand-22"],
            "revision_note": "调整",
        }
        ok = self.svc.revise_hypothesis({**payload, "revised_by": "chen"})
        self.assertEqual(ok["version"], 2)
        with self.assertRaises(ConflictError) as ctx:
            self.svc.revise_hypothesis({**payload, "revised_by": "zhao"})
        self.assertEqual(ctx.exception.details["server_version"], 2)
        self.assertEqual(ctx.exception.details["your_version"], 1)

    def test_parallel_revisions_exactly_one_wins(self):
        """两条线程同时基于 v1 修订：恰有一条成功，另一条 409。"""
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def revise(actor: str) -> None:
            barrier.wait()
            try:
                self.svc.revise_hypothesis({
                    "hypothesis_id": "hyp-22", "expected_version": 1,
                    "fragment_ids": ["frag-09", "frag-14"],
                    "candidate_ids": ["cand-22"],
                    "revised_by": actor, "revision_note": f"{actor} 的并发调整",
                })
                outcomes.append("ok")
            except ConflictError:
                outcomes.append("conflict")

        threads = [threading.Thread(target=revise, args=(a,))
                   for a in ("chen", "zhao")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(outcomes), ["conflict", "ok"])
        self.assertEqual(self.svc.hypothesis_detail("hyp-22")["hypothesis"]["version"], 2)


class IdempotentReviewTests(BaselineFixture):
    def test_offline_retransmit_not_double_counted(self):
        self.svc.submit_for_review("hyp-21", "chen")
        vote = {
            "review_id": "rev-lin-x", "hypothesis_id": "hyp-21",
            "expert_id": "lin", "decision": "approve", "comment": "离线意见",
            "client_request_id": "device-a/lin/42",
        }
        first = self.svc.cast_review(dict(vote))
        second = self.svc.cast_review(dict(vote))
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(second["tally"]["votes"]["approve"], 1)
        # 事件流中只有一条 review_cast
        reviews = [e for e in self.svc.store.all_events()
                   if e["event_type"] == "review_cast"]
        self.assertEqual(len(reviews), 1)

    def test_same_idempotency_key_different_body_rejected(self):
        self.svc.submit_for_review("hyp-21", "chen")
        self.svc.cast_review({
            "review_id": "rev-a", "hypothesis_id": "hyp-21", "expert_id": "lin",
            "decision": "approve", "client_request_id": "k/1",
        })
        with self.assertRaises(ConflictError):
            self.svc.cast_review({
                "review_id": "rev-a", "hypothesis_id": "hyp-21", "expert_id": "lin",
                "decision": "reject",  # 同一幂等键却改了结论
                "client_request_id": "k/1",
            })

    def test_review_against_stale_version_rejected(self):
        self.svc.submit_for_review("hyp-22", "chen")
        self.svc.revise_hypothesis({
            "hypothesis_id": "hyp-22", "expected_version": 1,
            "fragment_ids": ["frag-09", "frag-14"], "candidate_ids": ["cand-22"],
            "revised_by": "chen", "revision_note": "v2",
        })
        with self.assertRaises(ConflictError):
            self.svc.cast_review({
                "review_id": "rev-stale", "hypothesis_id": "hyp-22",
                "expert_id": "lin", "decision": "approve", "version": 1,
                "client_request_id": "stale/1",
            })


class ConfirmationFlowTests(BaselineFixture):
    def test_high_score_without_signature_cannot_confirm(self):
        self.svc.submit_for_review("hyp-22", "chen")
        for rid, expert, cid in [
            ("rl22", "lin", "c-lin-22"), ("rc22", "chen", "c-chen-22"),
            ("rz22", "zhao", "c-zhao-22"),
        ]:
            self.svc.cast_review({
                "review_id": rid, "hypothesis_id": "hyp-22", "expert_id": expert,
                "decision": "approve", "client_request_id": cid,
            })
        with self.assertRaises(GateError) as ctx:
            self.svc.confirm_hypothesis("hyp-22", "zhao")
        gates = {c["gate"]: c for c in ctx.exception.details["gate_report"]["checks"]}
        self.assertFalse(gates["required_signatures"]["passed"])

    def test_non_lead_cannot_confirm(self):
        self.approve("hyp-21")
        with self.assertRaises(PermissionDenied):
            self.svc.confirm_hypothesis("hyp-21", "chen")

    def test_quorum_enforced(self):
        self.svc.submit_for_review("hyp-21", "chen")
        self.svc.cast_review({
            "review_id": "only", "hypothesis_id": "hyp-21", "expert_id": "zhao",
            "decision": "approve", "client_request_id": "solo/1",
        })
        with self.assertRaises(PermissionDenied):
            self.svc.confirm_hypothesis("hyp-21", "zhao")

    def test_full_confirmation_then_exclusive_block(self):
        self.approve("hyp-21")
        result = self.svc.confirm_hypothesis("hyp-21", "zhao")
        self.assertTrue(all(c["passed"] for c in result["gate_report"]["checks"]))

        feas = self.svc.feasibility("vessel-guan-07")
        pair = next(m for m in feas["mutual_exclusion"]
                    if set(m["pair"]) == {"hyp-21", "hyp-22"})
        self.assertEqual(pair["shared_fragments"], ["frag-09"])
        h22 = next(c for c in feas["combinations"] if c["hypothesis_id"] == "hyp-22")
        self.assertFalse(h22["gates_passed"])
        self.assertTrue(
            any(v["gate"] == "exclusive_occupancy" for v in h22["blocking_conflicts"])
        )


class InvalidationHistoryTests(BaselineFixture):
    def test_invalidation_releases_occupancy_but_keeps_history(self):
        self.approve("hyp-21")
        self.svc.confirm_hypothesis("hyp-21", "zhao")

        # 新对立证据 → 失效
        self.svc.record_evidence({
            "evidence_id": "ev-new", "group_id": "vessel-guan-07",
            "kind": "microscopy", "summary": "烧成温度不同",
            "recorded_by": "lin",
            "target": {"type": "candidate", "id": "cand-21"},
            "stance": "contradicts",
        })
        out = self.svc.invalidate_hypothesis({
            "hypothesis_id": "hyp-21", "invalidated_by": "zhao",
            "reason": "显微证据否定", "evidence_id": "ev-new",
        })
        self.assertEqual(out["state"], "invalidated")

        detail = self.svc.hypothesis_detail("hyp-21")
        self.assertEqual(detail["hypothesis"]["state"], "invalidated")
        # 旧结论永久保留
        self.assertEqual(detail["hypothesis"]["confirmed_by"], "zhao")
        self.assertIsNotNone(detail["hypothesis"].get("confirmed_seq"))
        self.assertEqual(detail["hypothesis"]["trigger_evidence"], "ev-new")
        self.assertTrue(detail["reviews"])  # 旧意见仍在

        # 事件流完整：created → submitted → reviews → confirmed → invalidated
        types = [e["event_type"] for e in self.svc.store.all_events()
                 if e["payload"].get("hypothesis_id") == "hyp-21"
                 or e["event_type"] in ("review_cast",)
                 and e["payload"].get("hypothesis_id") == "hyp-21"]
        self.assertIn("hypothesis_confirmed", types)
        self.assertIn("hypothesis_invalidated", types)

        # 失效是终态：不能再修订或重新确认
        with self.assertRaises(ConflictError):
            self.svc.revise_hypothesis({
                "hypothesis_id": "hyp-21", "expected_version": 1,
                "fragment_ids": ["frag-01", "frag-09"], "candidate_ids": ["cand-21"],
                "revised_by": "chen", "revision_note": "试图复活",
            })

        # 排他占用释放：另一草案不再因 occupancy 被拦（仍可能因证据/签名被拦）
        feas = self.svc.feasibility("vessel-guan-07")
        h22 = next(c for c in feas["combinations"] if c["hypothesis_id"] == "hyp-22")
        self.assertFalse(
            any(v["gate"] == "exclusive_occupancy" for v in h22["blocking_conflicts"])
        )

    def test_non_lead_cannot_invalidate(self):
        self.approve("hyp-21")
        self.svc.confirm_hypothesis("hyp-21", "zhao")
        with self.assertRaises(PermissionDenied):
            self.svc.invalidate_hypothesis({
                "hypothesis_id": "hyp-21", "invalidated_by": "chen",
                "reason": "越权",
            })


class ReplayTests(BaselineFixture):
    def test_replay_group_covers_full_lifecycle(self):
        timeline = self.svc.replay_group("vessel-guan-07")["timeline"]
        types = [s["event_type"] for s in timeline]
        self.assertIn("candidate_proposed", types)
        self.assertEqual(types.count("candidate_proposed"), 2)
        self.assertEqual(timeline, sorted(timeline, key=lambda s: s["seq"]))


class PersistenceTests(unittest.TestCase):
    def test_seed_is_idempotent(self):
        svc = fresh_service()
        first = seed_baseline(svc)
        second = seed_baseline(svc)
        self.assertEqual(first["applied"], 16)
        self.assertEqual(second, {"applied": 0, "skipped": 16})
        # 签名观测也不能重复登记
        with self.assertRaises(ConflictError):
            svc.observe_signature({
                "candidate_id": "cand-21", "signature": "cross_section",
                "value": {}, "observed_by": "lin",
            })

    def test_events_survive_reopen(self):
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "graph.db")
            svc = fresh_service(db)
            seed_baseline(svc)
            seq_before = svc.snapshot()["last_seq"]
            svc.store.close()

            svc2 = AssemblyService(EventStore(db))
            snap = svc2.snapshot()
            self.assertEqual(snap["last_seq"], seq_before)
            self.assertIn("frag-09", snap["fragments"])
            self.assertIn("hyp-22", snap["hypotheses"])
            svc2.store.close()


if __name__ == "__main__":
    unittest.main()

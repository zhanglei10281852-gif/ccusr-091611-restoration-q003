"""HTTP 接口测试：真实起 ThreadingHTTPServer，走 urllib 请求。"""
from __future__ import annotations

import json
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.api import make_handler
from app.seed import seed_baseline
from app.service import AssemblyService
from app.store import EventStore


class ApiFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.service = AssemblyService(EventStore(":memory:"))
        seed_baseline(self.service)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service))
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        import threading
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def request(self, method: str, path: str, body: dict | None = None,
                headers: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = Request(self.base + path, data=data, method=method, headers={
            "Content-Type": "application/json", **(headers or {})})
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def approve(self, hyp_id: str) -> None:
        self.request("POST", f"/hypotheses/{hyp_id}/submit", {"actor": "chen"})
        for rid, expert, cid in [
            (f"hl-{expert}", expert, f"http-{expert}-{hyp_id}")
            for expert in ("lin", "chen", "zhao")
        ]:
            status, _ = self.request("POST", f"/hypotheses/{hyp_id}/reviews", {
                "review_id": rid, "expert_id": expert, "decision": "approve",
                "comment": "同意", "client_request_id": cid})
            self.assertEqual(status, 200)


class HttpFlowTests(ApiFixture):
    def test_health_and_snapshot(self):
        status, body = self.request("GET", "/health")
        self.assertEqual((status, body["status"]), (200, "ok"))
        status, snap = self.request("GET", "/snapshot")
        self.assertEqual(status, 200)
        self.assertIn("frag-09", snap["fragments"])
        self.assertTrue(snap["relations"])

    def test_feasibility_reports_mutex_path(self):
        status, body = self.request("GET", "/feasibility?group_id=vessel-guan-07")
        self.assertEqual(status, 200)
        pair = next(m for m in body["mutual_exclusion"]
                    if set(m["pair"]) == {"hyp-21", "hyp-22"})
        self.assertEqual(pair["shared_fragments"], ["frag-09"])

    def test_graph_endpoint_returns_relations(self):
        status, body = self.request("GET", "/graph")
        self.assertEqual(status, 200)
        kinds = {r["relation"] for r in body["relations"]}
        self.assertIn("joins", kinds)
        self.assertIn("describes", kinds)

    def test_confirm_gate_failure_is_422(self):
        # hyp-22 缺必需签名：即使评审通过也 422
        self.approve("hyp-22")
        status, body = self.request(
            "POST", "/hypotheses/hyp-22/confirm", {"actor": "zhao"})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "confirmation_gate_failed")
        gates = {c["gate"]: c for c in body["details"]["gate_report"]["checks"]}
        self.assertFalse(gates["required_signatures"]["passed"])

    def test_review_retransmit_is_idempotent(self):
        self.request("POST", "/hypotheses/hyp-21/submit", {"actor": "chen"})
        vote = {"review_id": "rv1", "expert_id": "lin", "decision": "approve",
                "comment": "离线", "client_request_id": "net/lin/1"}
        s1, b1 = self.request("POST", "/hypotheses/hyp-21/reviews", vote)
        s2, b2 = self.request("POST", "/hypotheses/hyp-21/reviews", vote)
        self.assertEqual((s1, b1["replayed"]), (200, False))
        self.assertEqual((s2, b2["replayed"]), (200, True))
        self.assertEqual(b2["tally"]["votes"]["approve"], 1)

    def test_stale_revision_conflict_409(self):
        self.request("POST", "/hypotheses/hyp-22/revisions", {
            "expected_version": 1,
            "fragment_ids": ["frag-09", "frag-14"], "candidate_ids": ["cand-22"],
            "revised_by": "chen", "revision_note": "第一次",
        })
        status, body = self.request("POST", "/hypotheses/hyp-22/revisions", {
            "expected_version": 1,
            "fragment_ids": ["frag-09", "frag-14"], "candidate_ids": ["cand-22"],
            "revised_by": "zhao", "revision_note": "并发的第二次",
        })
        self.assertEqual(status, 409)
        self.assertEqual(body["details"]["server_version"], 2)

    def test_full_confirm_then_invalidate_and_replay(self):
        self.approve("hyp-21")
        status, body = self.request(
            "POST", "/hypotheses/hyp-21/confirm", {"actor": "zhao"})
        self.assertEqual(status, 200)

        # 对立证据 + 失效
        self.request("POST", "/evidence", {
            "evidence_id": "ev-x", "group_id": "vessel-guan-07",
            "kind": "microscopy", "summary": "烧成温度不同",
            "recorded_by": "lin",
            "target": {"type": "candidate", "id": "cand-21"},
            "stance": "contradicts",
        })
        status, body = self.request("POST", "/hypotheses/hyp-21/invalidate", {
            "invalidated_by": "zhao", "reason": "显微证据否定", "evidence_id": "ev-x"})
        self.assertEqual(status, 200)

        status, detail = self.request("GET", "/hypotheses/hyp-21")
        self.assertEqual(status, 200)
        self.assertEqual(detail["hypothesis"]["state"], "invalidated")
        self.assertEqual(detail["hypothesis"]["confirmed_by"], "zhao")
        self.assertTrue(detail["reviews"])

        status, replay = self.request("GET", "/groups/vessel-guan-07/replay")
        self.assertEqual(status, 200)
        types = [s["event_type"] for s in replay["timeline"]]
        self.assertIn("hypothesis_confirmed", types)
        self.assertIn("hypothesis_invalidated", types)
        self.assertEqual(types, sorted(types, key=lambda t: 0))  # seq 有序
        seqs = [s["seq"] for s in replay["timeline"]]
        self.assertEqual(seqs, sorted(seqs))

    def test_validation_error_400_and_404(self):
        status, body = self.request("POST", "/candidates", {
            "candidate_id": "bad", "group_id": "vessel-guan-07",
            "left": {"fragment_id": "frag-01", "feature_id": "e2"},
            "right": {"fragment_id": "frag-09", "feature_id": "e4"},
            "score": 1.7, "rotation": 0, "proposed_by": "chen"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "validation_error")

        status, body = self.request("GET", "/hypotheses/missing")
        self.assertEqual(status, 404)


class HttpMutationTests(ApiFixture):
    def test_register_fragment_and_propose_candidate(self):
        status, body = self.request("POST", "/fragments", {
            "fragment_id": "frag-20", "group_id": "vessel-guan-07",
            "registered_by": "chen",
            "dimensions": {"length_mm": 50, "width_mm": 40, "thickness_mm": 5},
            "features": [{"feature_id": "e1", "kind": "break"}]})
        self.assertEqual(status, 200)
        self.assertEqual(body["fragment_id"], "frag-20")


if __name__ == "__main__":
    unittest.main()

"""HTTP JSON 接口（标准库 http.server）。

动作类接口可用 X-Expert-Id 请求头表明操作者，命令体中显式给出的操作者优先。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .errors import DomainError
from .service import AssemblyService
from .store import EventStore


def make_handler(service: AssemblyService):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CeramicGraph/1.0"

        def log_message(self, fmt, *args):  # 静音默认日志，演示脚本自己打印
            pass

        # ---- 基础收发 ----------------------------------------------------
        def _send(self, status: int, body: dict | list) -> None:
            data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise DomainError(f"请求体不是合法 JSON: {exc}")
            if not isinstance(data, dict):
                raise DomainError("请求体必须是 JSON 对象")
            return data

        def _actor(self, body: dict, key: str) -> str:
            return body.get(key) or self.headers.get("X-Expert-Id", "")

        def _run(self, fn):
            try:
                result = fn()
            except DomainError as exc:
                self._send(exc.status, exc.to_dict())
            except Exception as exc:  # noqa: BLE001 — 接口层兜底
                self._send(500, {"error": "internal_error", "message": str(exc)})
            else:
                self._send(200, result if result is not None else {"ok": True})

        # ---- 路由 --------------------------------------------------------
        def do_GET(self):  # noqa: N802
            parts = urlsplit(self.path)
            qs = parse_qs(parts.query)
            route = parts.path.strip("/").split("/")

            def go():
                if parts.path == "/health":
                    return {"status": "ok"}
                if parts.path == "/snapshot":
                    return service.snapshot()
                if parts.path == "/graph":
                    snap = service.snapshot()
                    return {"relations": snap["relations"], "last_seq": snap["last_seq"]}
                if parts.path == "/feasibility":
                    return service.feasibility(qs.get("group_id", [None])[0])
                if len(route) == 3 and route[0] == "groups" and route[2] == "replay":
                    return service.replay_group(route[1])
                if len(route) == 2 and route[0] == "hypotheses":
                    return service.hypothesis_detail(route[1])
                if parts.path == "/events":
                    after = int(qs.get("after_seq", ["0"])[0])
                    return {"events": service.store.all_events(after_seq=after)}
                from .errors import NotFoundError
                raise NotFoundError(f"无此路径: {parts.path}")

            self._run(go)

        def do_POST(self):  # noqa: N802
            route = urlsplit(self.path).path.strip("/").split("/")

            def go():
                body = self._body()
                if route == ["experts"]:
                    return service.register_expert(body)
                if route == ["fragments"]:
                    return service.register_fragment(body)
                if route == ["evidence"]:
                    return service.record_evidence(body)
                if route == ["candidates"]:
                    return service.propose_candidate(body)
                if len(route) == 3 and route[0] == "candidates" and route[2] == "signatures":
                    body.setdefault("candidate_id", route[1])
                    return service.observe_signature(body)
                if route == ["hypotheses"]:
                    return service.create_hypothesis(body)
                if len(route) == 3 and route[0] == "hypotheses" and route[2] == "revisions":
                    body.setdefault("hypothesis_id", route[1])
                    return service.revise_hypothesis(body)
                if len(route) == 3 and route[0] == "hypotheses" and route[2] == "submit":
                    return service.submit_for_review(route[1], self._actor(body, "actor"))
                if len(route) == 3 and route[0] == "hypotheses" and route[2] == "reviews":
                    body.setdefault("hypothesis_id", route[1])
                    return service.cast_review(body)
                if len(route) == 3 and route[0] == "hypotheses" and route[2] == "confirm":
                    return service.confirm_hypothesis(route[1], self._actor(body, "actor"))
                if len(route) == 3 and route[0] == "hypotheses" and route[2] == "invalidate":
                    body.setdefault("hypothesis_id", route[1])
                    return service.invalidate_hypothesis(body)
                if len(route) == 3 and route[0] == "hypotheses" and route[2] == "withdraw":
                    return service.withdraw_hypothesis(route[1], self._actor(body, "actor"))
                from .errors import NotFoundError
                raise NotFoundError(f"无此路径: /{'/'.join(route)}")

            self._run(go)

    return Handler


def build_server(db_path: str = ":memory:", host: str = "127.0.0.1",
                 port: int = 8080) -> tuple[ThreadingHTTPServer, AssemblyService]:
    store = EventStore(db_path)
    service = AssemblyService(store)
    httpd = ThreadingHTTPServer((host, port), make_handler(service))
    return httpd, service

"""python -m app [--db data/graph.db] [--port 8080] [--seed]

--seed 时灌入 vessel-guan-07 基线（专家/残片/候选/两份竞争草稿）。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .seed import seed_baseline
from .service import AssemblyService
from .store import EventStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="陶片拼接假设协作图服务")
    parser.add_argument("--db", default=os.environ.get("CERAMIC_DB", "data/graph.db"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--seed", action="store_true", help="灌入演示基线数据")
    args = parser.parse_args(argv)

    if args.db != ":memory:":
        Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    store = EventStore(args.db)
    service = AssemblyService(store)
    if args.seed:
        result = seed_baseline(service)
        print(f"基线数据就绪（新增 {result['applied']} 个事件，"
              f"已存在跳过 {result['skipped']} 个）: {args.db}")

    from http.server import ThreadingHTTPServer
    from .api import make_handler
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(service))
    print(f"服务监听 http://{args.host}:{args.port}  (Ctrl+C 停止)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止")
    finally:
        httpd.server_close()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

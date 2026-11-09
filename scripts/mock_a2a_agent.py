"""本地假 A2A Agent（开发联调用独立进程，默认 127.0.0.1:9100）。

启动后注册到 agent-hub：

.. code-block:: bash

    curl -X POST http://127.0.0.1:8000/api/v1/agents \\
      -H "Authorization: Bearer <token>" \\
      -H "Content-Type: application/json" \\
      -d '{"url": "http://127.0.0.1:9100"}'

dev/test 环境默认 ``A2A_ALLOW_LOOPBACK=true``（生产默认 false，禁止注册回环地址）；
也可用 ``A2A_ALLOWED_HOSTS=127.0.0.1`` 显式白名单。

触发场景：task 文本包含 success / slow / fail / input_required / auth_required / streaming。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn

from tests.fixtures.fake_a2a_agent import FakeA2aAgent


def main() -> None:
    parser = argparse.ArgumentParser(description="本地假 A2A Agent")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--name", default="Fake Agent")
    parser.add_argument("--slug", default="fake")
    parser.add_argument("--streaming", action="store_true", help="Agent Card 声明支持流式")
    parser.add_argument("--slow-seconds", type=float, default=0.0, help="slow 场景延迟")
    parser.add_argument("--requires-approval", action="store_true", help="声明写操作需审批")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"
    agent = FakeA2aAgent(
        name=args.name,
        slug=args.slug,
        url=url,
        streaming=args.streaming,
        slow_seconds=args.slow_seconds,
        requires_approval=args.requires_approval,
    )
    env = os.environ.get("ENV", "dev")
    print(f"[mock-a2a] 假 Agent 已启动：{url}（ENV={env}）")
    print(f"[mock-a2a] Agent Card：GET {url}/.well-known/agent-card.json")
    print(f'[mock-a2a] 注册命令：POST /api/v1/agents {{"url": "{url}"}}')
    uvicorn.run(agent.app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

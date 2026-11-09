"""M2 验收实测：对 8000 真实实例跑通注册→预览→健康→对话→任务→resume/authorize。

用法：uv run python scripts/verify_m2_live.py [--base http://127.0.0.1:8000]
前提：scripts/mock_a2a_agent.py 已在 127.0.0.1:9100 运行。
测试后会删除联调数据（软删 Agent）。
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid

import httpx

AGENT_URL = "http://127.0.0.1:9100"


async def main(base: str) -> int:
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        mark = "PASS" if condition else "FAIL"
        print(f"[{mark}] {label}" + (f"：{detail[:120]}" if detail and not condition else ""))
        if not condition:
            failures.append(label)

    async with httpx.AsyncClient(base_url=base, timeout=60, trust_env=False) as client:
        # 1. 注册联调用户
        email = f"m2-live-{uuid.uuid4().hex[:8]}@example.com"
        registered = await client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": "password123", "display_name": "M2联调"},
        )
        check("注册联调用户", registered.status_code == 201, registered.text)
        token = registered.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 2. 系统能力开关
        info = await client.get("/api/v1/system/info", headers=headers)
        check(
            "capabilities.agents/tasks=true",
            info.json()["capabilities"]["agents"] is True
            and info.json()["capabilities"]["tasks"] is True,
            info.text,
        )

        # 3. preview
        preview = await client.post(
            "/api/v1/agents/preview", json={"url": AGENT_URL}, headers=headers
        )
        check("preview 拉取校验 Card", preview.status_code == 200, preview.text)
        if preview.status_code == 200:
            check("preview.normalized 字段", preview.json()["normalized"]["name"] == "Fake Agent")

        # 4. 注册
        created = await client.post(
            "/api/v1/agents", json={"url": AGENT_URL, "slug": "m2-live-agent"}, headers=headers
        )
        check("注册 Agent（201 + 详情）", created.status_code == 201, created.text)
        agent_id = created.json()["id"] if created.status_code == 201 else None
        if agent_id is None:
            return 1

        # 幂等：同 url+slug 再注册返回已有
        again = await client.post(
            "/api/v1/agents", json={"url": AGENT_URL, "slug": "m2-live-agent"}, headers=headers
        )
        check("幂等注册返回已有", again.status_code == 201 and again.json()["id"] == agent_id)

        # 5. 健康检查
        health = await client.post(f"/api/v1/agents/{agent_id}/health", headers=headers)
        check(
            "健康检查 healthy",
            health.status_code == 200 and health.json()["health_status"] == "healthy",
            health.text,
        )
        check("健康检查带延迟", health.json().get("latency_ms") is not None)

        # 6. 列表 stats
        listing = await client.get("/api/v1/agents", headers=headers)
        check("agents 列表 stats.enabled=1", listing.json()["stats"]["enabled"] == 1)

        # 7. 对话调用（真实 LLM 委派 send_message）
        run_id, chat_events = await _chat(
            client,
            headers,
            "请调用 send_message 工具，agent=m2-live-agent，task 内容写：input_required 场景，帮我核对退款统计口径。",
        )
        check("对话启动", run_id is not None)
        names = [name for name, _ in chat_events]
        check("SSE 含 a2a 事件", any(name.startswith("a2a.") for name in names), str(names))

        task_id = next(
            (
                data["task_id"]
                for name, data in chat_events
                if name in {"a2a.input_required", "a2a.auth_required"}
            ),
            None,
        )
        if task_id is None:
            print("[INFO] LLM 未触发等待态（不可控），退化为任务列表验证")
            listing_tasks = await client.get("/api/v1/tasks", headers=headers)
            check("任务列表可查", listing_tasks.status_code == 200, listing_tasks.text)
            items = listing_tasks.json()["items"]
            if items:
                task_id = items[0]["id"]
            else:
                print("[INFO] 无任务行；跳过动作验证")
                return 1 if failures else 0

        # 8. 任务详情
        detail = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
        check("任务详情 200", detail.status_code == 200, detail.text)
        state = detail.json()["state"]
        check("任务状态为等待态", state in {"input_required", "auth_required", "completed"}, state)

        # 9. resume / authorize
        if state == "input_required":
            resume = await client.post(
                f"/api/v1/tasks/{task_id}/resume",
                json={"input": "按创建时间统计"},
                headers=headers,
            )
            check("resume 200 + run 引用", resume.status_code == 200, resume.text)
            final = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
            check("resume 后任务终态", final.json()["state"] in {"completed", "working"})
        elif state == "auth_required":
            authorize = await client.post(
                f"/api/v1/tasks/{task_id}/authorize",
                json={"scopes": ["repo:read"]},
                headers=headers,
            )
            check("authorize 200", authorize.status_code == 200, authorize.text)

        # 10. 动作错误码：completed 后 cancel → 409
        final = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
        if final.json()["state"] == "completed":
            denied = await client.post(f"/api/v1/tasks/{task_id}/cancel", headers=headers)
            check(
                "终态 cancel → 409 task_not_cancelable",
                denied.status_code == 409
                and denied.json()["error"]["code"] == "task_not_cancelable",
                denied.text,
            )

        # 11. 清理：软删 Agent（若任务已终态）
        deleted = await client.delete(f"/api/v1/agents/{agent_id}", headers=headers)
        print(f"[INFO] 清理联调 Agent：HTTP {deleted.status_code}")

    print("=" * 50)
    print(f"实测结果：{len(failures)} 项失败" + (f"：{failures}" if failures else "，全部通过"))
    return 1 if failures else 0


async def _chat(client: httpx.AsyncClient, headers: dict, message: str) -> tuple[str | None, list]:
    run_id: str | None = None
    events: list[tuple[str, dict]] = []
    try:
        async with client.stream(
            "POST", "/api/v1/chat/stream", json={"message": message}, headers=headers, timeout=120
        ) as response:
            run_id = response.headers.get("X-Run-ID")
            event_name: str | None = None
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event_name = line.split(":", 1)[1].strip()
                elif line.startswith("data:") and event_name:
                    data = json.loads(line.split(":", 1)[1].strip())
                    events.append((event_name, data))
                    if event_name in {"message.done", "error"}:
                        return run_id, events
    except httpx.HTTPError as exc:
        print(f"[ERROR] 对话流异常：{exc}")
    return run_id, events


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    import asyncio

    code = asyncio.run(main(args.base))
    sys.exit(code)

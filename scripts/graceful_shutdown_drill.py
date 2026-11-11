"""优雅停机演练：两个 uvicorn 实例，先摘流量再 SIGTERM。

验证目标：
- 正在流的 SSE 在 SIGTERM 后仍收到 ``message.done``（排水窗口生效）；
- 排水期间另一个实例持续服务，请求 0 失败；
- 实例重启后能继续服务。

    DATABASE_URL=... CHECKPOINT_DATABASE_URL=... \
    .venv/bin/python scripts/graceful_shutdown_drill.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

TERMINAL = {"message.done", "error"}


def _child_env(name: str, base_env: dict[str, str]) -> dict[str, str]:
    root = Path(f".tmp/drill-{name}")
    return {
        **base_env,
        "ENV": "dev",
        "LLM_PROVIDER": "fake",
        "FAKE_LLM_DELAY_MS": "100",
        "RATE_LIMIT_ENABLED": "false",
        "CLEANUP_ENABLED": "false",
        "STARTUP_RECOVERY_ENABLED": "false",
        "RECOVERY_INTERVAL_SECONDS": "0",
        "OTEL_ENABLED": "false",
        "SHUTDOWN_GRACE_SECONDS": "30",
        "DATA_DIR": str(root / "data"),
        "ATTACHMENT_DIR": str(root / "attachments"),
        "KNOWLEDGE_DIR": str(root / "knowledge"),
    }


async def _wait_ready(client: httpx.AsyncClient, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if (await client.get("/readyz")).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.2)
    return False


async def _collect_run(
    base_url: str,
    token: str,
    message: str,
    *,
    first_delta: asyncio.Event | None = None,
) -> list[str]:
    events: list[str] = []
    async with httpx.AsyncClient(
        base_url=base_url, timeout=60, trust_env=False
    ) as client:
        async with client.stream(
            "POST",
            "/api/v1/chat/stream",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": message},
        ) as response:
            if response.status_code != 200:
                await response.aread()
                return [f"http:{response.status_code}"]
            event_name: str | None = None
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event_name = line.split(":", 1)[1].strip()
                elif line.startswith("data:") and event_name:
                    events.append(event_name)
                    if event_name == "message.delta" and first_delta is not None:
                        first_delta.set()
                    if event_name in TERMINAL:
                        break
    return events


def _start(name: str, port: int, base_env: dict[str, str]) -> tuple[subprocess.Popen, Path]:
    log_path = Path(f".tmp/drill-{name}.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            # 与生产镜像一致：先由 uvicorn 等待在跑连接，再进入 lifespan 排水。
            "--timeout-graceful-shutdown",
            "90",
        ],
        env=_child_env(name, base_env),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    return process, log_path


def _terminate(process: subprocess.Popen, timeout: float = 45.0) -> int:
    if process.poll() is not None:
        return int(process.returncode or 0)
    process.send_signal(signal.SIGTERM)
    try:
        return int(process.wait(timeout=timeout))
    except subprocess.TimeoutExpired:
        process.kill()
        return int(process.wait(timeout=5))


async def _register(base_url: str) -> str:
    async with httpx.AsyncClient(base_url=base_url, timeout=15, trust_env=False) as client:
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": f"drain-{int(time.time() * 1000)}@example.com",
                "password": "password123",
                "display_name": "排水演练",
            },
        )
        if response.status_code != 201:
            raise RuntimeError(f"注册失败：{response.status_code} {response.text[:200]}")
        return str(response.json()["access_token"])


async def _run(args: argparse.Namespace) -> int:
    original = os.environ.get("DATABASE_URL", "")
    if not original:
        raise SystemExit("需要 DATABASE_URL（建议指向 agent_hub_test）")
    checkpoint = os.environ.get("CHECKPOINT_DATABASE_URL") or original.replace(
        "postgresql+psycopg://", "postgresql://"
    )
    base_env = {**os.environ, "DATABASE_URL": original, "CHECKPOINT_DATABASE_URL": checkpoint}
    a, b = _start("a", args.port_a, base_env), _start("b", args.port_b, base_env)
    passed = True
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{args.port_a}", timeout=5, trust_env=False
        ) as client:
            if not await _wait_ready(client):
                raise RuntimeError("实例 A 未就绪")
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{args.port_b}", timeout=5, trust_env=False
        ) as client:
            if not await _wait_ready(client):
                raise RuntimeError("实例 B 未就绪")
        token = await _register(f"http://127.0.0.1:{args.port_a}")

        for round_index in range(2):
            current = a if round_index == 0 else b
            current_port = args.port_a if round_index == 0 else args.port_b
            other_port = args.port_b if round_index == 0 else args.port_a
            first = asyncio.Event()
            live = asyncio.create_task(
                _collect_run(
                    f"http://127.0.0.1:{current_port}",
                    token,
                    f"排水轮次 {round_index}",
                    first_delta=first,
                )
            )
            await asyncio.wait_for(first.wait(), timeout=20)
            exit_code = await asyncio.to_thread(_terminate, current[0], 45)
            other_ok = 0
            for index in range(3):
                events = await _collect_run(
                    f"http://127.0.0.1:{other_port}", token, f"另一实例 {round_index}-{index}"
                )
                other_ok += int("message.done" in events)
            live_events = await asyncio.wait_for(live, timeout=30)
            live_ok = int("message.done" in live_events)
            print(
                f"round {round_index}: 在跑 SSE done={live_ok}，另一实例成功={other_ok}/3，"
                f"被停实例 exit={exit_code}"
            )
            passed &= live_ok == 1 and other_ok == 3
            if exit_code != 0:
                print(f"  提示：{current[1]} 退出码 {exit_code}（被 SIGTERM 终止属预期）")
            process, _ = _start("a" if round_index == 0 else "b", current_port, base_env)
            if round_index == 0:
                a = (process, a[1])
            else:
                b = (process, b[1])
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{current_port}", timeout=5, trust_env=False
            ) as client:
                passed &= await _wait_ready(client)
    finally:
        for process, _log in (a, b):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    print(f"\n结果：{'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="优雅停机演练")
    parser.add_argument("--port-a", type=int, default=8041)
    parser.add_argument("--port-b", type=int, default=8042)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()

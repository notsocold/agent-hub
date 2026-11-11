"""数据库短时不可用演练（本地 TCP 代理，不停止/启动任何容器）。

流程：代理在中间转发 5432 流量 → 启动 API → 关代理（模拟 DB 不可用）→
``/readyz`` 503、业务请求 503 → 开代理 → ``/readyz`` 200、聊天恢复；API 进程不重启。

    DATABASE_URL=postgresql+psycopg://user:pass@localhost:54321/agent_hub_test \
    CHECKPOINT_DATABASE_URL=postgresql://user:pass@localhost:54321/agent_hub_test \
    .venv/bin/python scripts/db_outage_drill.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

TARGET_RE = re.compile(r"@([^@/]+)/")


class TcpProxy:
    def __init__(self, target_host: str, target_port: int) -> None:
        self.target_host = target_host
        self.target_port = target_port
        self.port = 0
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self, port: int = 0) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", port)
        self.port = int(self._server.sockets[0].getsockname()[1])
        return self.port

    async def stop(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
        # 先断开正在转发的连接，再等待 server 关闭；否则 wait_closed 可能
        # 一直等 handler 里的 pipe 结束。
        writers = list(self._writers)
        for writer in writers:
            writer.close()
        if writers:
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(writer.wait_closed() for writer in writers),
                        return_exceptions=True,
                    ),
                    timeout=2,
                )
            except TimeoutError:
                pass
        self._writers.clear()
        if server is not None:
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=2)
            except TimeoutError:
                pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                self.target_host, self.target_port
            )
            self._writers.add(upstream_writer)
            await asyncio.gather(
                self._pipe(reader, upstream_writer),
                self._pipe(upstream_reader, writer),
            )
        except Exception:
            pass
        finally:
            for item in (writer, upstream_writer):
                if item is not None:
                    self._writers.discard(item)
                    item.close()

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except Exception:
            pass


def _split_db_url(url: str) -> tuple[str, int, str]:
    parts = urlsplit(url.replace("+psycopg", ""))
    if not parts.hostname:
        raise SystemExit(f"无法解析 DATABASE_URL：{url}")
    return parts.hostname, parts.port or 5432, url


def _proxy_url(url: str, port: int) -> str:
    return TARGET_RE.sub(f"@127.0.0.1:{port}/", url, count=1)


def _with_connect_timeout(url: str, seconds: int = 3) -> str:
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}connect_timeout={seconds}"


async def _wait_status(client: httpx.AsyncClient, path: str, expected: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = await client.get(path)
            if response.status_code == expected:
                return True
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.3)
    return False


async def _run(args: argparse.Namespace) -> int:
    original = args.database_url or os.environ.get("DATABASE_URL", "")
    if not original:
        raise SystemExit("需要 DATABASE_URL（建议指向 agent_hub_test）")
    host, port, _ = _split_db_url(original)
    proxy = TcpProxy(host, port)
    proxy_port = await proxy.start()

    api_port = args.api_port
    child_env = {
        **os.environ,
        "DATABASE_URL": _with_connect_timeout(_proxy_url(original, proxy_port)),
        "CHECKPOINT_DATABASE_URL": _with_connect_timeout(
            _proxy_url(original.replace("postgresql+psycopg://", "postgresql://"), proxy_port)
        ),
        "ENV": "dev",
        "LLM_PROVIDER": "fake",
        "RATE_LIMIT_ENABLED": "false",
        "CLEANUP_ENABLED": "false",
        "STARTUP_RECOVERY_ENABLED": "false",
        "OTEL_ENABLED": "false",
    }
    log_path = Path(".tmp/db-outage-drill.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(api_port),
        ],
        env=child_env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{api_port}"
    passed = True
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=5, trust_env=False) as client:
            if not await _wait_status(client, "/readyz", 200, 30):
                raise RuntimeError("API 未在 30s 内就绪")
            registered = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": f"outage-{int(time.time())}@example.com",
                    "password": "password123",
                    "display_name": "故障演练",
                },
            )
            if registered.status_code != 201:
                raise RuntimeError(f"注册失败：{registered.status_code} {registered.text[:200]}")
            token = registered.json()["access_token"]
            headers = {"Authorization": f"Bearer {token}"}

            await proxy.stop()
            ready_down = await _wait_status(client, "/readyz", 503, 20)
            outage_response = await client.get("/api/v1/conversations", headers=headers)
            print(f"故障中 /readyz 503: {ready_down}")
            print(
                f"故障中业务请求: HTTP {outage_response.status_code} "
                f"{outage_response.text[:120]}"
            )
            passed &= ready_down and outage_response.status_code == 503

            await proxy.start(proxy_port)
            ready_up = await _wait_status(client, "/readyz", 200, 30)
            recovered = await client.post(
                "/api/v1/chat/stream",
                headers=headers,
                json={"message": "数据库恢复后你好"},
            )
            recovered_text = recovered.text
            print(f"恢复后 /readyz 200: {ready_up}")
            print(f"恢复后聊天: HTTP {recovered.status_code} 包含 message.done={('message.done' in recovered_text)}")
            passed &= ready_up and recovered.status_code == 200 and "message.done" in recovered_text
            passed &= process.poll() is None
            print(f"API 进程未重启: {process.poll() is None}")
    finally:
        await proxy.stop()
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log_file.close()
    print(f"\n结果：{'PASS' if passed else 'FAIL'}（服务日志 {log_path}）")
    return 0 if passed else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="数据库短时不可用演练")
    parser.add_argument("--database-url", default="")
    parser.add_argument("--api-port", type=int, default=8030)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()

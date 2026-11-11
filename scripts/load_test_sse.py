"""本机 50 并发 SSE 压测。

前置：服务端 `RATE_LIMIT_ENABLED=false`、`LLM_PROVIDER=fake`，并使用独立测试库/会话。
脚本只读服务端契约，不修改服务端配置。

    .venv/bin/python scripts/load_test_sse.py --concurrency 5 --rounds 1
    .venv/bin/python scripts/load_test_sse.py --concurrency 50 --rounds 1 \\
        --json-out .tmp/m6-load.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx


@dataclass
class Result:
    ok: bool
    status_code: int
    first_token_ms: float | None = None
    duration_ms: float | None = None
    error: str = ""


@dataclass
class Stats:
    results: list[Result] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def ok(self) -> int:
        return sum(1 for item in self.results if item.ok)

    @property
    def error_rate(self) -> float:
        return 0.0 if not self.total else (self.total - self.ok) / self.total

    def percentile(self, values: list[float], p: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, round((p / 100) * (len(ordered) - 1))))
        return ordered[index]

    def summary(self) -> dict[str, Any]:
        first = [item.first_token_ms for item in self.results if item.first_token_ms is not None]
        duration = [item.duration_ms for item in self.results if item.duration_ms is not None]
        statuses: dict[str, int] = {}
        for item in self.results:
            statuses[str(item.status_code)] = statuses.get(str(item.status_code), 0) + 1
        return {
            "total": self.total,
            "ok": self.ok,
            "error": self.total - self.ok,
            "error_rate": round(self.error_rate, 4),
            "status_codes": statuses,
            "first_token_ms": {
                "p50": self.percentile(first, 50),
                "p95": self.percentile(first, 95),
                "p99": self.percentile(first, 99),
                "mean": round(statistics.fmean(first), 1) if first else None,
            },
            "duration_ms": {
                "p50": self.percentile(duration, 50),
                "p95": self.percentile(duration, 95),
                "p99": self.percentile(duration, 99),
                "mean": round(statistics.fmean(duration), 1) if duration else None,
            },
        }


async def _register(client: httpx.AsyncClient, index: int) -> str:
    suffix = uuid.uuid4().hex[:10]
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"load-{index}-{suffix}@example.com",
            "password": "password123",
            "display_name": f"压测用户{index}",
        },
    )
    if response.status_code != 201:
        raise RuntimeError(f"注册失败 HTTP {response.status_code}: {response.text[:200]}")
    return str(response.json()["access_token"])


async def _one_chat(
    client: httpx.AsyncClient,
    *,
    token: str,
    index: int,
    round_index: int,
) -> Result:
    started = time.monotonic()
    first_token_ms: float | None = None
    terminal = False
    error = ""
    status_code = 0
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "message": f"压测消息 {index}-{round_index}",
        "client_message_id": f"load-{index}-{round_index}-{uuid.uuid4().hex[:8]}",
    }
    try:
        async with client.stream(
            "POST", "/api/v1/chat/stream", json=payload, headers=headers, timeout=120
        ) as response:
            status_code = response.status_code
            if status_code != 200:
                await response.aread()
                return Result(False, status_code, error=response.text[:200])
            event_name: str | None = None
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event_name = line.split(":", 1)[1].strip()
                elif line.startswith("data:") and event_name:
                    data = json.loads(line.split(":", 1)[1].strip())
                    if event_name in {"message.delta", "reasoning.delta"} and first_token_ms is None:
                        first_token_ms = (time.monotonic() - started) * 1000
                    if event_name in {"message.done", "error"}:
                        terminal = True
                        if event_name == "error":
                            error = str(data.get("message") or data.get("code") or "error")[:200]
                        break
    except Exception as exc:
        return Result(False, status_code or 0, error=str(exc)[:200])
    duration_ms = (time.monotonic() - started) * 1000
    return Result(
        terminal and status_code == 200 and not error,
        status_code or 200,
        first_token_ms,
        duration_ms,
        error,
    )


async def _run(args: argparse.Namespace) -> int:
    start = time.monotonic()
    limits = httpx.Limits(max_connections=args.concurrency * 2, max_keepalive_connections=args.concurrency)
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        timeout=args.timeout,
        limits=limits,
        trust_env=False,
    ) as client:
        tokens = await asyncio.gather(
            *[_register(client, index) for index in range(args.concurrency)]
        )
        stats = Stats()
        tasks: list[asyncio.Task[Result]] = []
        for index, token in enumerate(tokens):
            for round_index in range(args.rounds):
                tasks.append(
                    asyncio.create_task(
                        _one_chat(client, token=token, index=index, round_index=round_index)
                    )
                )
        for result in await asyncio.gather(*tasks):
            stats.results.append(result)

    elapsed = round(time.monotonic() - start, 2)
    summary = stats.summary()
    summary["concurrency"] = args.concurrency
    summary["rounds"] = args.rounds
    summary["elapsed_seconds"] = elapsed
    summary["generated_at"] = datetime.now(UTC).isoformat()

    print("\n== M6 SSE 压测 ==")
    for key in ("total", "ok", "error", "error_rate", "status_codes"):
        print(f"{key}: {summary[key]}")
    print(f"first_token_ms: {summary['first_token_ms']}")
    print(f"duration_ms: {summary['duration_ms']}")
    print(f"elapsed_seconds: {elapsed}")
    failures = [item for item in stats.results if not item.ok][:5]
    for item in failures:
        print(f"FAIL status={item.status_code} error={item.error}")

    if args.json_out:
        target = Path(args.json_out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"已写入 {target}")
    return 0 if summary["error_rate"] < 0.01 else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="agent-hub SSE 压测")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--json-out", default="")
    parser.add_argument("--self-check", action="store_true", help="只校验参数，不访问网络")
    args = parser.parse_args()
    if args.concurrency < 1 or args.rounds < 1:
        raise SystemExit("--concurrency / --rounds 必须 >= 1")
    if args.self_check:
        print(
            f"base_url={args.base_url} concurrency={args.concurrency} rounds={args.rounds} "
            f"timeout={args.timeout}"
        )
        return
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()

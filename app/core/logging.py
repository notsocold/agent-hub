"""日志配置与请求上下文。

M1 先用标准库 logging 输出可读日志；结构化日志在 M6 切换。
"""

from __future__ import annotations

import logging
import sys

from app.core.errors import request_id_ctx


class RequestIdFilter(logging.Filter):
    """把当前请求 ID 注入日志记录，便于串联一次请求。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = request_id_ctx.get() or "-"
        return True


def configure_logging(debug: bool = False) -> None:
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(logging.DEBUG if debug else logging.INFO)
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    handler.addFilter(RequestIdFilter())
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if debug else logging.INFO)

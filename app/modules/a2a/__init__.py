"""A2A 出站：Agent Card 拉取校验与 JSON-RPC 客户端。

M3 入站 Server 另建 server 子模块；本包当前只承载出站能力。
"""

from app.modules.a2a.card import (
    CardFetchResult,
    fetch_agent_card,
    normalize_card,
    validate_card_url_same_origin,
)
from app.modules.a2a.client import (
    A2aClient,
    A2aError,
    A2aUpdate,
    clear_test_transports,
    register_test_transport,
)

__all__ = [
    "A2aClient",
    "A2aError",
    "A2aUpdate",
    "CardFetchResult",
    "clear_test_transports",
    "fetch_agent_card",
    "normalize_card",
    "register_test_transport",
    "validate_card_url_same_origin",
]

"""导出 OpenAPI 契约到 docs/openapi.json，供前端生成类型/核对契约。"""

from __future__ import annotations

import json
from pathlib import Path

from app.main import app


def main() -> None:
    target = Path("docs/openapi.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"已导出 {target}")


if __name__ == "__main__":
    main()

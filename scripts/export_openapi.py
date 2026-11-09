"""导出 OpenAPI 契约到 docs/openapi.json，供前端生成类型/核对契约。

用法（二选一，脚本会自行把项目根加入 sys.path）：

    uv run python scripts/export_openapi.py
    .venv/bin/python scripts/export_openapi.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 直接以 ``python scripts/export_openapi.py`` 运行时，sys.path[0] 是 scripts/，
# 需要显式补上项目根，否则 ``import app`` 失败。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import app


def main() -> None:
    target = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"已导出 {target}")


if __name__ == "__main__":
    main()

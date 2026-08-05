"""路由层。**只做校验与编排调用，不写业务逻辑。**

全部路由挂在 `server.base_path` 前缀下（默认 `/poe_agent`），包括 `/docs`、
`/openapi.json` 与静态前端。用显式前缀而非 FastAPI 的 `root_path`：后者假定
反向代理会剥掉前缀，本机直连时路径对不上。
"""

from __future__ import annotations


def normalize_base_path(raw: str | None) -> str:
    """归一为 `/xxx` 或空串。

    容忍用户在 config.yaml 里写 `poe_agent`、`/poe_agent/`、`//poe_agent` 等形式——
    前缀写错的表现是「所有路由 404」，排查起来很费时间，不值得为省几行代码换来。
    """
    s = (raw or "").strip().strip("/")
    return f"/{s}" if s else ""

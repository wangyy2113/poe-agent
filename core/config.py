"""双层配置：.env 管密钥（pydantic-settings），config.yaml 管声明式行为。

跨平台约定：路径一律用 pathlib，不硬编码分隔符；文本读取显式 UTF-8。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings

from core.model_policy import DEEPSEEK_MODEL, ModelPolicyError, require_model

# 仓库根目录 = 本文件的上一级。配置里的相对路径都相对它解析，
# 这样无论从哪个目录启动，data/ 与 logs/ 都落在仓库内。
REPO_ROOT = Path(__file__).resolve().parent.parent


class Secrets(BaseSettings):
    """来自 .env 的密钥与环境配置。必填项无默认值，缺失即启动失败。"""

    # ── LLM ──
    llm_base_url: str
    llm_api_key: str = ""
    llm_api_keys: str = ""  # 多 Key：逗号分隔，所有凭证只服务同一个模型
    llm_model: str = DEEPSEEK_MODEL

    # ── Legacy values accepted for migration; no remote adapter consumes them ──
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    rerank_base_url: str = ""
    rerank_api_key: str = ""
    rerank_model: str = ""

    # ── 鉴权 ──
    admin_token: str = ""
    api_keys: str = ""

    # ── GGG 官方角色 API（OAuth scope: account:characters）──
    poe_oauth_token: str = ""
    poe_oauth_user_agent: str = ""

    model_config = {
        "env_file": (".env", ".env.local"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
        "case_sensitive": False,
        "hide_input_in_errors": True,
    }

    def credential_specs(self) -> list[tuple[str, str]]:
        """Resolve credentials without permitting per-key model routing.

        The old ``key:model`` format is accepted only when it names the pinned
        model. Invalid entries fail before any network call, without echoing keys.
        """
        model = require_model(self.llm_model)
        raw = self.llm_api_keys.strip() or self.llm_api_key.strip()
        specs: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            key, sep, bound = item.partition(":")
            key = key.strip()
            if sep:
                require_model(bound)
            if not key:
                raise ModelPolicyError("LLM 凭证条目为空；请检查本机配置")
            if key not in seen:
                seen.add(key)
                specs.append((key, model))
        return specs


@dataclass
class Config:
    """config.yaml 的内容 + 解析好的绝对路径。"""

    raw: dict[str, Any] = field(default_factory=dict)

    # ── 取值 helpers ──
    def get(self, path: str, default: Any = None) -> Any:
        """按 'a.b.c' 取值。"""
        node: Any = self.raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path_of(self, key: str, default: str) -> Path:
        """把 paths.* 解析为绝对路径（相对仓库根）。"""
        value = str(self.get(f"paths.{key}", default))
        p = Path(value)
        return p if p.is_absolute() else REPO_ROOT / p

    # ── 常用项 ──
    @property
    def db_path(self) -> Path:
        return self.path_of("db", "data/poe.db")

    @property
    def backup_dir(self) -> Path:
        """SQLite 一致性快照目录；与 HTTP 缓存一样属于可重建外的运行时数据。"""
        return self.path_of("backups", "data/backups")

    @property
    def cache_dir(self) -> Path:
        return self.path_of("http_cache", "data/http_cache")

    @property
    def logs_dir(self) -> Path:
        return self.path_of("logs", "logs")

    @property
    def source_priority(self) -> dict[str, int]:
        return dict(self.get("sources.priority", {}) or {})

    def priority_of(self, source: str) -> int:
        """未登记的来源按最低优先级处理，避免被误当高优先级采信。"""
        return int(self.source_priority.get(source, 99))

    def resolve_model(self, alias: str) -> str:
        """Compatibility entry point; arbitrary aliases no longer route models."""
        return require_model(alias)


def load_config(path: Path | None = None) -> Config:
    cfg_path = path or REPO_ROOT / "config.yaml"
    if not cfg_path.is_file():
        return Config(raw={})
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"config.yaml 顶层必须是映射，实际为 {type(data).__name__}")
    return Config(raw=data)


@lru_cache
def get_config() -> Config:
    return load_config()


@lru_cache
def get_secrets() -> Secrets:
    # pydantic-settings 的 env_file 相对当前工作目录解析，
    # 这里显式指向仓库根，避免从别处启动时读不到 .env。
    os.environ.setdefault("PYTHONUTF8", "1")
    return Secrets(_env_file=(REPO_ROOT / ".env", REPO_ROOT / ".env.local"))  # type: ignore[call-arg]


def mask(secret: str) -> str:
    """脱敏标签，用于日志。完整密钥不得出现在任何输出中。"""
    if not secret:
        return "<empty>"
    return f"{secret[:7]}…{secret[-4:]}" if len(secret) > 14 else f"{secret[:4]}…"

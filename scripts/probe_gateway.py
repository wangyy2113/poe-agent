#!/usr/bin/env python3
"""Verify the configured DeepSeek gateway using only this project's credentials.

Use .env / .env.local or LLM_* environment variables. No CLI-tool settings,
sibling projects, model discovery fallback or alternative providers are read.
The gateway and credentials are never printed. No arguments can select a model
or send a configured credential to an alternative host.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import get_secrets  # noqa: E402
from core.llm import LLMClient  # noqa: E402
from core.model_policy import AUXILIARY_MODEL_REASON, DEEPSEEK_MODEL  # noqa: E402
from scripts import verify_llm  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        secrets = get_secrets()
        secrets.credential_specs()  # Reject legacy routing before any network request.
        client = LLMClient()
        print(f"模型策略：仅 {DEEPSEEK_MODEL}")
        print(f"网关已配置；凭证数量：{len(client.credential_status())}")
        print(AUXILIARY_MODEL_REASON)
        verify_llm.online(client)
    except Exception as exc:
        # Upstream exceptions can contain request bodies, keys and endpoint names.
        print(f"网关验证失败：{type(exc).__name__}；请检查本机配置", file=sys.stderr)
        return 1
    return 0 if verify_llm.FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""计划或执行知识库增量同步，并输出事实级版本 diff。

默认只预览，不联网、不写事实表：

    python scripts/sync_knowledge.py
    python scripts/sync_knowledge.py --apply
    python scripts/sync_knowledge.py --apply --categories currency,maps --force
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from kb import pipeline  # noqa: E402
from kb.sync import (  # noqa: E402
    CATEGORY_SPECS,
    DEFAULT_STALE_AFTER_HOURS,
    SyncAlreadyRunning,
    build_plan,
    run_sync,
    validate_categories,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="POE1-Agent 知识库自动同步与版本 diff")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="执行计划；不加时只预览，不联网、不修改事实表",
    )
    parser.add_argument(
        "--categories",
        help="显式同步逗号分隔的分类；不填时按过期时间自动选择",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="显式同步所有自动化安全分类（不含唯一装备复合链）",
    )
    parser.add_argument("--force", action="store_true", help="忽略 HTTP 缓存重新抓取")
    parser.add_argument(
        "--stale-after-hours",
        type=float,
        help="覆盖 config.yaml 的自动同步过期小时数",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    return parser


def _configured_categories(cfg) -> tuple[str, ...]:  # noqa: ANN001
    raw = cfg.get("knowledge_sync.categories", [spec.name for spec in CATEGORY_SPECS])
    if not isinstance(raw, list):
        raise ValueError("knowledge_sync.categories 必须是列表")
    return validate_categories(str(item) for item in raw)


def _explicit_categories(args: argparse.Namespace, configured: tuple[str, ...]) -> tuple[str, ...] | None:
    if args.all and args.categories:
        raise ValueError("--all 与 --categories 不能同时使用")
    if args.all:
        return configured
    if args.categories is None:
        return None
    return validate_categories(args.categories.split(","))


def _print_plan(plan, *, as_json: bool) -> None:  # noqa: ANN001
    payload = {"mode": "plan", "items": [item.as_dict() for item in plan]}
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    print("知识库同步计划（预览不会联网或修改事实表）")
    for item in plan:
        age = "从未成功" if item.age_hours is None else f"{item.age_hours:.1f} 小时"
        marker = "DUE" if item.due else "OK "
        print(f"  [{marker}] {item.category:<12} {age:<14} {item.reason}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cfg = get_config()
    setup_logging(cfg.logs_dir, str(cfg.get("logging.level", "INFO")))
    conn = dbmod.init_db(cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0))
    try:
        configured = _configured_categories(cfg)
        explicit = _explicit_categories(args, configured)
        stale_hours = (
            args.stale_after_hours
            if args.stale_after_hours is not None
            else float(cfg.get("knowledge_sync.stale_after_hours", DEFAULT_STALE_AFTER_HOURS))
        )
        plan = build_plan(
            conn,
            categories=explicit or configured,
            stale_after_hours=stale_hours,
        )
        if not args.apply:
            _print_plan(plan, as_json=args.json)
            return 0

        selected = explicit or tuple(item.category for item in plan if item.due)
        if not selected:
            result = {"status": "up_to_date", "summary": {"planned": 0}, "diffs": []}
        else:
            result = run_sync(
                conn,
                selected,
                force=args.force,
                mode="explicit" if explicit is not None else "auto",
                refresh_coverage=pipeline.run_coverage,
            )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["status"] in {"succeeded", "up_to_date"} else 1
    except (ValueError, SyncAlreadyRunning) as exc:
        print(f"[中止] {exc}", file=sys.stderr)
        return 2
    finally:
        dbmod.close_db()


if __name__ == "__main__":
    raise SystemExit(main())

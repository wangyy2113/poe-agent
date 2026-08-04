"""日志配置。

跨平台要点：
  - 文件名按日期后缀直接命名（app-YYYY-MM-DD.log），**不做 rename 轮转**。
    TimedRotatingFileHandler 靠 rename 实现，Windows 上文件被杀软或索引器
    占用时会抛 WinError 32 导致日志中断；直接换文件名从机制上避开。
  - handler 显式 encoding="utf-8"，否则 Windows 默认 cp936 会破坏中文。
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _prune_old_logs(logs_dir: Path, retention_days: int) -> None:
    """按文件名日期清理过期日志。用文件名而非 mtime，避免复制文件后误判。"""
    if retention_days <= 0:
        return
    cutoff = date.today() - timedelta(days=retention_days)
    for path in logs_dir.glob("app-*.log"):
        stamp = path.stem.removeprefix("app-")
        try:
            if datetime.strptime(stamp, "%Y-%m-%d").date() < cutoff:
                path.unlink()
        except (ValueError, OSError):
            # 文件名不合规或删除失败（Windows 上可能被占用）都不影响主流程
            continue


def setup_logging(logs_dir: Path, level: str = "INFO", retention_days: int = 14) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    _prune_old_logs(logs_dir, retention_days)

    # Windows 控制台默认 cp936，中文会乱码或抛 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (AttributeError, ValueError):
                pass

    log_file = logs_dir / f"app-{date.today().isoformat()}.log"
    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # 幂等：重复调用不叠加 handler
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(file_handler)
    root.addHandler(console)

    # 第三方库降噪
    for noisy in ("httpx", "httpcore", "urllib3", "jieba"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

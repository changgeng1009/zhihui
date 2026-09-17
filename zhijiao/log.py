"""统一日志（Unified Logging）。

* 一个进程级 logger 树，格式统一：``HH:MM:SS | LEVEL | backend | message``；
* 所有 token 经 :func:`mask` 掩码后才允许落盘；
* :class:`RouteTrace` 记录一次路由决策的全部尝试，便于回答
  "这个请求最后到底走了哪个项目、为什么没走别的"；
* 可选 JSONL 落盘到 ``<state_dir>/logs/zhijiao-YYYY-MM-DD.jsonl``，
  便于后续用脚本分析路由分布。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = [
    "mask",
    "get_logger",
    "configure_logging",
    "default_jsonl_path",
    "RouteTrace",
    "RouteAttempt",
    "emit_route",
]

_LOGGER_NAME = "zhijiao"
_CONFIGURED = False
_LOCK = threading.Lock()
_JSONL_PATH: Optional[Path] = None

_FMT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATEFMT = "%H:%M:%S"


def mask(value: Any, *, keep_head: int = 6, keep_tail: int = 4) -> str:
    """把敏感值掩码成 ``abcdef...cdef`` 形式。

    短值（<= keep_head + keep_tail）一律显示为 ``***``。
    """
    if value is None:
        return ""
    s = str(value)
    if not s:
        return ""
    if len(s) <= keep_head + keep_tail:
        return "***"
    return f"{s[:keep_head]}...{s[-keep_tail:]}"


def configure_logging(
    level: Optional[str] = None,
    *,
    jsonl_path: Optional[Path] = None,
    force: bool = False,
) -> logging.Logger:
    """初始化统一 logger（幂等）。

    ``level`` 为 ``None`` 时读环境变量 ``ZJ_LOG_LEVEL``（默认 INFO）——
    刻意不 import :mod:`zhijiao.config`，避免 config ↔ log 循环依赖。
    推荐用法是让宿主调一次 :func:`zhijiao.setup`，由它把配置里的
    ``log_level`` / ``log_jsonl`` 传进来。
    """
    global _CONFIGURED, _JSONL_PATH
    if level is None:
        level = os.environ.get("ZJ_LOG_LEVEL", "INFO")
    with _LOCK:
        if _CONFIGURED and not force:
            logger = logging.getLogger(_LOGGER_NAME)
            logger.setLevel(getattr(logging, level.upper(), logging.INFO))
            return logger

        logger = logging.getLogger(_LOGGER_NAME)
        logger.setLevel(getattr(logging, level.upper(), logging.INFO))
        logger.propagate = False
        for h in list(logger.handlers):
            logger.removeHandler(h)

        sh = logging.StreamHandler(stream=sys.stderr)
        sh.setFormatter(logging.Formatter(_FMT, datefmt=_DATEFMT))
        logger.addHandler(sh)

        _JSONL_PATH = jsonl_path
        _CONFIGURED = True
        return logger


def get_logger(name: str = "") -> logging.Logger:
    """取得统一 logger 的子 logger。"""
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(f"{_LOGGER_NAME}.{name}" if name else _LOGGER_NAME)


def default_jsonl_path(log_dir: Path) -> Path:
    """按天切分的路由日志文件路径。"""
    day = datetime.now().strftime("%Y-%m-%d")
    return Path(log_dir) / f"zhijiao-{day}.jsonl"


# --------------------------------------------------------------------------- #
# 路由轨迹
# --------------------------------------------------------------------------- #
@dataclass
class RouteAttempt:
    """一次 Backend 尝试。"""

    backend: str
    ok: bool
    ms: float = 0.0
    code: str = ""
    message: str = ""
    skipped: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "ok": self.ok,
            "skipped": self.skipped,
            "ms": round(self.ms, 1),
            "code": self.code,
            "message": self.message,
        }


@dataclass
class RouteTrace:
    """一次 ``Router.invoke`` 的完整轨迹。"""

    capability: str
    attempts: List[RouteAttempt] = field(default_factory=list)
    chosen: Optional[str] = None
    total_ms: float = 0.0
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # ------------------------------------------------------------------ #
    def record(
        self,
        backend: str,
        *,
        ok: bool,
        ms: float = 0.0,
        code: str = "",
        message: str = "",
        skipped: bool = False,
    ) -> RouteAttempt:
        a = RouteAttempt(backend=backend, ok=ok, ms=ms, code=code, message=message, skipped=skipped)
        self.attempts.append(a)
        return a

    @property
    def fallback_count(self) -> int:
        """降级次数 = 失败/跳过的尝试次数。"""
        return sum(1 for a in self.attempts if not a.ok)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "capability": self.capability,
            "chosen": self.chosen,
            "fallback_count": self.fallback_count,
            "total_ms": round(self.total_ms, 1),
            "started_at": self.started_at,
            "attempts": [a.to_dict() for a in self.attempts],
        }

    def summary(self) -> str:
        chain = " -> ".join(
            f"{a.backend}{'' if a.ok else '(x)'}" for a in self.attempts
        ) or "(空)"
        return f"{self.capability}: {chain} => {self.chosen or 'FAILED'} ({self.total_ms:.0f}ms)"


def emit_route(trace: RouteTrace, *, logger: Optional[logging.Logger] = None) -> None:
    """把路由轨迹打到统一日志，并按配置写入 JSONL。"""
    log = logger or get_logger("router")
    if trace.chosen:
        log.info(trace.summary())
    else:
        log.warning(trace.summary())

    path = _JSONL_PATH
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(trace.to_dict(), ensure_ascii=False) + "\n")
    except OSError:  # pragma: no cover - 日志失败不应影响主流程
        log.debug("写入路由 JSONL 失败: %s", path)

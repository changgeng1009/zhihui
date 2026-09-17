"""统一配置（Unified Config）。

优先级：**环境变量 > config.json > 内置默认值**。

环境变量前缀统一为 ``ZJ_``，例如::

    ZJ_UPSTREAM_ROOT=D:/CodexWork/智慧职教刷课/upstreams
    ZJ_LOG_LEVEL=DEBUG
    ZJ_STATE_DIR=D:/CodexWork/智慧职教刷课/state
    ZJ_REQUEST_TIMEOUT=10
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import ConfigError

__all__ = ["Settings", "get_settings", "reset_settings", "PROJECT_ROOT"]

#: 项目根目录（``zhijiao/`` 的上一级）
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

_ENV_PREFIX = "ZJ_"

_DEFAULTS: Dict[str, Any] = {
    "upstream_root": str(PROJECT_ROOT / "upstreams"),
    "state_dir": str(PROJECT_ROOT / "state"),
    "log_dir": str(PROJECT_ROOT / "state" / "logs"),
    #: Playwright 浏览器内核目录（默认留空 => 用项目内 browsers/，配合 PLAYWRIGHT_BROWSERS_PATH）
    "browsers_dir": str(PROJECT_ROOT / "browsers"),
    "log_level": "INFO",
    "log_jsonl": True,
    "request_timeout": 10,
    # 各上游目录名（允许改名而不影响 Adapter）
    "upstream_dirs": {
        "icve_toolkit": "ICVE_Toolkit",
        "zjy_toolkit": "ZJY-Toolkit",
        "mooc_work_answer": "mooc-work-answer",
        "ocsjs": "ocsjs",
    },
    # 路由：全局是否允许降级（测试/调试可关）
    "allow_fallback": True,
    # OCS 浏览器兜底
    "ocs": {
        "script_path": "",  # 本地已构建的 userscript 路径（可选）
        "script_url": "https://docs.ocsjs.com/",  # 官方分发/文档
        "enabled": True,
    },
    # 题库目录（透传给 ICVE_Toolkit 的 ZjyClient）
    "question_bank_dir": "",
}


def _coerce(value: Any, like: Any) -> Any:
    """按默认值的类型把字符串环境变量转回来。"""
    if isinstance(like, bool):
        return str(value).strip().lower() in ("1", "true", "yes", "y", "on")
    if isinstance(like, int) and not isinstance(like, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return like
    if isinstance(like, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return like
    if isinstance(like, dict):
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except ValueError:
                return like
            return parsed if isinstance(parsed, dict) else like
    return value


@dataclass
class Settings:
    """统一层运行时配置。"""

    upstream_root: Path
    state_dir: Path
    log_dir: Path
    browsers_dir: Path
    log_level: str = "INFO"
    log_jsonl: bool = True
    request_timeout: int = 10
    upstream_dirs: Dict[str, str] = field(default_factory=dict)
    allow_fallback: bool = True
    ocs: Dict[str, Any] = field(default_factory=dict)
    question_bank_dir: str = ""

    # ------------------------------------------------------------------ #
    def upstream_path(self, backend_id: str) -> Path:
        """返回某个 Backend 对应的上游目录。"""
        name = self.upstream_dirs.get(backend_id)
        if not name:
            raise ConfigError(
                f"未配置 Backend {backend_id!r} 的上游目录",
                details={"known": sorted(self.upstream_dirs)},
            )
        return self.upstream_root / name

    def apply_env(self) -> None:
        """把"必须由环境变量/解释器开关传递"的设置落下来（幂等）。

        两项：

        1. ``PLAYWRIGHT_BROWSERS_PATH``
           上游 ``slider_auto.py`` 直接调 playwright，而 playwright 只认这个环境变量
           来找浏览器内核。本项目把内核放在 **项目内** ``browsers/``，
           所以必须在这里把路径告诉它 —— 这样项目才是自包含的，
           不需要用户在任何机器上单独装一次 Chromium。

        2. ``sys.pycache_prefix``
           ``import`` 上游模块时 Python 会顺手在**上游目录里**写 ``__pycache__/*.pyc``，
           污染"上游只读区"（阶段 1 完整性测试会红）。把它重定向到项目内
           ``state/pycache/``，整个仓库里就不会出现任何 ``__pycache__``。

        两项都用 ``setdefault`` 语义：显式设过者优先，不覆盖使用者的选择。
        """
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(self.browsers_dir))

        import sys as _sys

        prefix = str(self.state_dir / "pycache")
        if getattr(_sys, "pycache_prefix", None) in (None, ""):
            _sys.pycache_prefix = prefix

    def ensure_dirs(self) -> None:
        """确保运行期目录存在，并应用环境变量。"""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.browsers_dir.mkdir(parents=True, exist_ok=True)
        if self.log_jsonl:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        self.apply_env()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "upstream_root": str(self.upstream_root),
            "state_dir": str(self.state_dir),
            "log_dir": str(self.log_dir),
            "browsers_dir": str(self.browsers_dir),
            "log_level": self.log_level,
            "log_jsonl": self.log_jsonl,
            "request_timeout": self.request_timeout,
            "upstream_dirs": dict(self.upstream_dirs),
            "allow_fallback": self.allow_fallback,
            "ocs": dict(self.ocs),
            "question_bank_dir": self.question_bank_dir,
        }


_CACHED: Optional[Settings] = None


def _load_file(path: Optional[Path]) -> Dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ConfigError(f"配置文件解析失败: {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件顶层必须是对象: {path}")
    return raw


def _load_env() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, default in _DEFAULTS.items():
        env_key = _ENV_PREFIX + key.upper()
        if env_key in os.environ:
            out[key] = _coerce(os.environ[env_key], default)
    # 便捷项：ZJ_CONFIG_FILE 指定配置文件路径
    return out


def get_settings(*, config_file: Optional[str] = None, reload: bool = False) -> Settings:
    """读取（并缓存）统一配置。"""
    global _CACHED
    if _CACHED is not None and not reload and config_file is None:
        return _CACHED

    merged: Dict[str, Any] = dict(_DEFAULTS)

    # ① config.json
    cfg_path = Path(config_file) if config_file else (
        Path(os.environ.get("ZJ_CONFIG_FILE", "")) if os.environ.get("ZJ_CONFIG_FILE") else PROJECT_ROOT / "config.json"
    )
    file_cfg = _load_file(cfg_path)
    if file_cfg:
        # 嵌套的 ocs 段落做浅合并，避免整段被覆盖丢键
        if isinstance(file_cfg.get("ocs"), dict):
            ocs = dict(_DEFAULTS["ocs"])
            ocs.update(file_cfg["ocs"])
            file_cfg = {**file_cfg, "ocs": ocs}
        if isinstance(file_cfg.get("upstream_dirs"), dict):
            dirs = dict(_DEFAULTS["upstream_dirs"])
            dirs.update(file_cfg["upstream_dirs"])
            file_cfg = {**file_cfg, "upstream_dirs": dirs}
        merged.update(file_cfg)

    # ② 环境变量
    env_cfg = _load_env()
    if isinstance(env_cfg.get("ocs"), dict):
        merged["ocs"] = {**merged["ocs"], **env_cfg["ocs"]}
        env_cfg = {k: v for k, v in env_cfg.items() if k != "ocs"}
    merged.update(env_cfg)

    settings = Settings(
        upstream_root=Path(str(merged["upstream_root"])).expanduser().resolve(),
        state_dir=Path(str(merged["state_dir"])).expanduser().resolve(),
        log_dir=Path(str(merged["log_dir"])).expanduser().resolve(),
        browsers_dir=Path(str(merged["browsers_dir"])).expanduser().resolve(),
        log_level=str(merged["log_level"]).upper(),
        log_jsonl=bool(merged["log_jsonl"]),
        request_timeout=int(merged["request_timeout"]),
        upstream_dirs=dict(merged["upstream_dirs"]),
        allow_fallback=bool(merged["allow_fallback"]),
        ocs=dict(merged["ocs"]),
        question_bank_dir=str(merged["question_bank_dir"] or ""),
    )

    if not settings.upstream_root.is_dir():
        raise ConfigError(
            f"upstream_root 不存在: {settings.upstream_root}",
            details={"hint": "设置 ZJ_UPSTREAM_ROOT 指向四个上游 clone 的父目录"},
        )

    if config_file is None:
        _CACHED = settings
    return settings


def reset_settings() -> None:
    """清空配置缓存（测试用）。"""
    global _CACHED
    _CACHED = None


def upstream_dirs_list(settings: Optional[Settings] = None) -> List[str]:
    s = settings or get_settings()
    return sorted(s.upstream_dirs)

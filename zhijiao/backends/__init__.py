"""Backend 注册表。

统一在这里声明"本项目接入了哪些上游"。新增一个上游 = 加一个 Adapter 文件
+ 在 ``REGISTRY`` 里加一行，其余（路由、日志、错误、数据格式）自动生效。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Type

from ..config import Settings, get_settings
from ..log import get_logger
from .base import (
    ALL_CAPABILITIES,
    BackendCheck,
    BackendInfo,
    BackendKind,
    BaseBackend,
    Capability,
    wrap_upstream,
)
from .icve_toolkit import IcveToolkitBackend
from .loader import UpstreamLoader, get_loader
from .mooc_work_answer import MoocWorkAnswerBackend
from .ocsjs import OcsjsBackend
from .zjy_toolkit import ZjyToolkitBackend

__all__ = [
    "REGISTRY",
    "Capability",
    "BackendKind",
    "BackendInfo",
    "BaseBackend",
    "BackendCheck",
    "ALL_CAPABILITIES",
    "wrap_upstream",
    "build_backends",
    "get_backend",
    "backend_infos",
    "loader_for",
]

_log = get_logger("backends")

#: Backend id -> Adapter 类
REGISTRY: Dict[str, Type[BaseBackend]] = {
    IcveToolkitBackend.info.id: IcveToolkitBackend,
    MoocWorkAnswerBackend.info.id: MoocWorkAnswerBackend,
    ZjyToolkitBackend.info.id: ZjyToolkitBackend,
    OcsjsBackend.info.id: OcsjsBackend,
}

_BUILT: Optional[Dict[str, BaseBackend]] = None


def build_backends(
    settings: Optional[Settings] = None,
    loader: Optional[UpstreamLoader] = None,
    *,
    force: bool = False,
) -> Dict[str, BaseBackend]:
    """构造（并缓存）全部 Backend 实例。"""
    global _BUILT
    if _BUILT is not None and not force:
        return _BUILT

    st = settings or get_settings()
    ld = loader or get_loader(st)
    built: Dict[str, BaseBackend] = {}
    for bid, cls in REGISTRY.items():
        try:
            built[bid] = cls(settings=st, loader=ld)
        except Exception as e:  # pragma: no cover - 单个 Adapter 构造失败不应拖垮全局
            _log.error("构造 Backend %s 失败：%s", bid, e)
    _BUILT = built
    return built


def get_backend(
    backend_id: str,
    settings: Optional[Settings] = None,
    *,
    force: bool = False,
) -> BaseBackend:
    """按 id 取 Backend 实例。"""
    from ..errors import ConfigError

    backends = build_backends(settings, force=force)
    if backend_id not in backends:
        raise ConfigError(
            f"未知 Backend id: {backend_id!r}",
            details={"known": sorted(backends)},
        )
    return backends[backend_id]


def backend_infos(settings: Optional[Settings] = None) -> List[Dict[str, Any]]:
    """所有 Backend 的静态元信息（不含运行期探测）。"""
    return [b.info.to_dict() for b in build_backends(settings).values()]


def loader_for(settings: Optional[Settings] = None) -> UpstreamLoader:
    return get_loader(settings)


def reset_backends() -> None:
    """清空 Backend 实例缓存（测试用）。"""
    global _BUILT
    _BUILT = None

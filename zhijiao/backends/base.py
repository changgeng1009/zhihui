"""Backend 协议（Backend Protocol）。

每个上游项目被包装成一个 **Backend**，实现本模块定义的统一接口。
统一层之上（Router / Tool Layer）只认这个接口，永不直接触碰上游对象。

契约
----
1. ``info.capabilities`` 声明该后端**真正支持**的能力；
2. 能力名与 ``Capability`` 枚举值同名的方法即实现（``list_courses`` / ``get_attendance`` ...）；
3. 不支持的能力一律抛 :class:`CapabilityNotSupported`（可降级），**不要返回空列表冒充成功**；
4. 任何上游裸异常必须转成 :mod:`zhijiao.errors` 里的统一异常；
5. Backend **无状态**：每次调用接收 ``session``（:class:`~zhijiao.session.LoginState`），
   自身不缓存凭据（客户端实例可按 session 指纹缓存，见各 Adapter）。
"""

from __future__ import annotations

import abc
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Dict, FrozenSet, Optional

from ..errors import CapabilityNotSupported, ZhijiaoError
from ..log import get_logger
from ..session import LoginState

__all__ = [
    "Capability",
    "BackendKind",
    "BackendInfo",
    "BaseBackend",
    "BackendCheck",
    "ALL_CAPABILITIES",
    "wrap_upstream",
]

_log = get_logger("backend")


class Capability(str, Enum):
    """本项目对外承诺的 7 个统一能力。"""

    LIST_COURSES = "list_courses"
    GET_COURSE_DETAIL = "get_course_detail"
    GET_COURSE_PROGRESS = "get_course_progress"
    LIST_UNFINISHED_TASKS = "list_unfinished_tasks"
    START_LEARNING = "start_learning"
    GET_ATTENDANCE = "get_attendance"
    GET_RESULTS = "get_results"

    @classmethod
    def parse(cls, raw: Any) -> "Capability":
        if isinstance(raw, Capability):
            return raw
        try:
            return cls(str(raw))
        except ValueError as e:
            known = ", ".join(c.value for c in cls)
            raise ZhijiaoError(
                f"未知能力 {raw!r}，可用：{known}", code="ZJ-5002",
            ) from e


ALL_CAPABILITIES: FrozenSet[Capability] = frozenset(Capability)


class BackendKind(str, Enum):
    """后端形态。"""

    API = "api"           # 直连官方 HTTP API（进程内加载上游）
    REMOTE = "remote"     # 远端托管服务（HTTP 适配）
    BROWSER = "browser"   # 浏览器端执行（用户脚本兜底）


@dataclass(frozen=True)
class BackendInfo:
    """后端的静态元信息。"""

    id: str
    name: str
    kind: BackendKind
    license: str
    upstream_dir: str
    capabilities: FrozenSet[Capability]
    notes: str = ""
    #: 是否允许在无凭据时参与路由（浏览器兜底需要）
    credential_optional: bool = False
    #: 上游仓库地址，便于追溯
    repo: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind.value,
            "license": self.license,
            "upstream_dir": self.upstream_dir,
            "capabilities": sorted(c.value for c in self.capabilities),
            "notes": self.notes,
            "credential_optional": self.credential_optional,
            "repo": self.repo,
        }


class BaseBackend(abc.ABC):
    """所有 Adapter 的基类。"""

    #: 子类必须定义
    info: ClassVar[BackendInfo]

    def __init__(self, settings: Any = None) -> None:
        if settings is None:
            from ..config import get_settings

            settings = get_settings()
        self.settings = settings
        # 客户端缓存：session 指纹 -> 上游 client 实例
        self._clients: Dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    # 能力声明
    # ------------------------------------------------------------------ #
    @property
    def id(self) -> str:
        return self.info.id

    def supports(self, capability: Capability) -> bool:
        return capability in self.info.capabilities

    def supported_capabilities(self) -> FrozenSet[Capability]:
        return self.info.capabilities

    # ------------------------------------------------------------------ #
    # 可用性
    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        """后端当前是否可参与路由。默认 True，子类按需覆盖。"""
        return True

    def unavailable_reason(self) -> str:
        """不可用原因（``available`` 为 False 时才有意义）。"""
        return ""

    def check(self) -> "BackendCheck":
        """返回一个完整的可用性/能力快照，供 ``Router.health()`` 使用。"""
        try:
            ok = bool(self.available)
            return BackendCheck(
                backend=self.id,
                available=ok,
                reason="" if ok else self.unavailable_reason(),
                capabilities=sorted(c.value for c in self.info.capabilities),
            )
        except Exception as e:  # pragma: no cover - 防御
            return BackendCheck(
                backend=self.id, available=False, reason=f"{type(e).__name__}: {e}",
                capabilities=sorted(c.value for c in self.info.capabilities),
            )

    # ------------------------------------------------------------------ #
    # 调用入口
    # ------------------------------------------------------------------ #
    def call(
        self,
        capability: Capability,
        *,
        session: Optional[LoginState] = None,
        **kwargs: Any,
    ) -> Any:
        """按能力名分派到同名方法。"""
        capability = Capability.parse(capability)
        if not self.supports(capability):
            raise CapabilityNotSupported(
                f"{self.info.name} 不支持能力 {capability.value}",
                backend=self.id,
                details={"supported": sorted(c.value for c in self.info.capabilities)},
            )
        handler = getattr(self, capability.value, None)
        if handler is None or not callable(handler):
            raise CapabilityNotSupported(
                f"{self.info.name} 声明支持 {capability.value} 但未实现该方法",
                backend=self.id,
            )
        _log.debug("→ %s.%s", self.id, capability.value)
        return handler(session=session, **kwargs)

    # ------------------------------------------------------------------ #
    # 子类工具
    # ------------------------------------------------------------------ #
    def _check_available(self) -> None:
        """在校验可用性后放行，否则抛可降级异常。"""
        from ..errors import BackendUnavailable

        if not self.available:
            raise BackendUnavailable(
                f"{self.info.name} 不可用：{self.unavailable_reason()}",
                backend=self.id,
                details={"upstream_dir": self.info.upstream_dir},
            )

    def _require_credential(self, session: Optional[LoginState]) -> LoginState:
        """需要凭据的能力统一在此校验。"""
        from ..errors import AuthError

        if session is None or not session.is_valid:
            raise AuthError(
                f"{self.info.name} 需要登录凭据",
                backend=self.id,
                details={"hint": "提供 ZJ_SSO_TOKEN 或 state/session.json"},
            )
        return session

    # ------------------------------------------------------------------ #
    # 共享助手
    # ------------------------------------------------------------------ #
    def _resolve_course(
        self,
        *,
        session: Optional[LoginState],
        course: Optional[Any] = None,
        course_id: str = "",
        course_info_id: str = "",
        class_id: str = "",
        course_type: Optional[Any] = None,
        **_: Any,
    ) -> Any:
        """把"课程定位参数"统一解析成一个 :class:`~zhijiao.contracts.Course`。

        接受三种入参形态（上层 Tool Layer 与 Agent 都可能用不同写法）：

        1. 直接传 ``course=Course(...)``   —— 最省事，零网络；
        2. 传 ``course_id`` (+ ``course_info_id`` / ``class_id`` / ``course_type``)
           —— 在 :meth:`list_courses` 结果里按 key 匹配；
        3. 都不传 —— 抛错，避免"猜一个课程"这种危险行为。

        本方法是所有需要课程的 Backend 的**公共前置**，避免每个 Adapter 各写一遍。
        """
        from ..contracts import Course, CourseType, normalize_course_type
        from ..errors import ZhijiaoError

        if isinstance(course, Course):
            return course

        if course is not None and isinstance(course, dict):
            # 容忍"上游原始 course dict"直接透传
            return Course(
                course_id=str(course.get("courseId") or course.get("course_id") or ""),
                course_info_id=str(course.get("courseInfoId") or course.get("course_info_id") or ""),
                class_id=str(course.get("classId") or course.get("class_id") or ""),
                name=str(course.get("courseName") or course.get("course_name") or course.get("name") or ""),
                course_type=normalize_course_type(
                    course.get("_courseType") or course.get("courseType") or course_type
                ),
                backend=self.id,
                raw=dict(course),
            )

        if not course_id:
            raise ZhijiaoError(
                "需要课程定位信息：请传 course=Course(...) 或至少 course_id",
                code="ZJ-1001",
                backend=self.id,
                details={"hint": "先 list_courses 拿到 course_id 与 course_info_id"},
            )

        wanted_type = normalize_course_type(course_type) if course_type else CourseType.UNKNOWN
        for c in self.list_courses(session=session):
            if c.course_id != course_id:
                continue
            if course_info_id and c.course_info_id and c.course_info_id != course_info_id:
                continue
            if class_id and c.class_id and c.class_id != class_id:
                continue
            if wanted_type is not CourseType.UNKNOWN and c.course_type is not wanted_type:
                continue
            return c

        raise ZhijiaoError(
            f"在课程列表中找不到 course_id={course_id!r}",
            code="ZJ-1001",
            backend=self.id,
            details={"course_info_id": course_info_id, "class_id": class_id},
        )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} id={self.id} available={self.available}>"


@dataclass
class BackendCheck:
    """一次健康检查结果。"""

    backend: str
    available: bool
    reason: str = ""
    capabilities: list = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "available": self.available,
            "reason": self.reason,
            "capabilities": list(self.capabilities),
            **({"extra": self.extra} if self.extra else {}),
        }


# --------------------------------------------------------------------------- #
# 上游异常归一化
# --------------------------------------------------------------------------- #
_TRANSIENT_NAMES = {
    "Timeout",
    "ConnectTimeout",
    "ReadTimeout",
    "ConnectionError",
    "ConnectionResetError",
    "NewConnectionError",
    "MaxRetryError",
    "RemoteDisconnected",
    "IncompleteRead",
    "ChunkedEncodingError",
    "SSLError",
}


@contextmanager
def wrap_upstream(backend_id: str, what: str = ""):
    """把上游裸异常统一转成 :mod:`zhijiao.errors` 里的异常。

    * 网络类 -> :class:`UpstreamTransientError`（可降级）
    * 统一层异常 -> 原样放行（比如 Adapter 主动抛的 ``AuthError``）
    * 其他 -> :class:`UpstreamError`（可降级）

    用法::

        with wrap_upstream("icve_toolkit", "list_courses"):
            raw = client.get_my_courses()
    """
    from ..errors import UpstreamError, UpstreamTransientError, ZhijiaoError

    try:
        yield
    except ZhijiaoError:
        raise
    except BaseException as e:  # noqa: BLE001 - 刻意兜住所有上游异常
        name = type(e).__name__
        label = f"{backend_id}.{what}" if what else backend_id
        if name in _TRANSIENT_NAMES or _is_requests_transient(e):
            raise UpstreamTransientError(
                f"{label} 网络异常：{name}: {e}", backend=backend_id, cause=e,
            ) from e
        raise UpstreamError(
            f"{label} 上游异常：{name}: {e}", backend=backend_id, cause=e,
        ) from e


def _is_requests_transient(e: BaseException) -> bool:
    """requests 的异常层级里带 ``Timeout`` / ``Connection`` 都算瞬时。"""
    for klass in type(e).__mro__:
        n = klass.__name__
        if n in ("Timeout", "ConnectionError", "RequestException"):
            return n != "RequestException" or True
    return False


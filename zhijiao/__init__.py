"""智慧职教统一工具层（zhijiao）。

把四个开源项目整合成**一层**统一能力，对外只暴露 7 个 Tool::

    Agent
      → Unified Tool Layer   (zhijiao/tools.py)      7 个稳定能力
      → Router               (zhijiao/router.py)     自动选项目 + 降级
      → Adapter × 4          (zhijiao/backends/*)    零改造包装上游
      → ICVE_Toolkit / ZJY-Toolkit / mooc-work-answer / OCS

设计约束（对应任务要求）
------------------------
* 不重写、不大改上游核心代码 —— ``upstreams/`` 是只读 git clone，
  由 ``tests/test_stage1_upstream_intact.py`` 断言其 ``git status`` 始终干净；
* 优先保留原项目作为独立 Backend / Adapter —— 每个上游一个 Adapter 文件；
* 只在外部增加一层 Router + Tool Layer —— ``zhijiao/`` 是唯一自写代码；
* 自动判断调用哪个项目 —— ``ROUTING_TABLE`` + 能力/可用性探测；
* 统一登录状态 / 配置 / 日志 / 错误 / 数据格式 —— ``session`` / ``config`` /
  ``log`` / ``errors`` / ``contracts``；
* 重复功能不重新实现 —— 路由表把每个能力收敛到**单一主 Backend**；
* API 优先，OCS 兜底 —— 路由表末位固定 ``ocsjs``，且它只产出交接计划；
* 不复制大量源码 —— ``zhijiao/`` 内零上游代码拷贝。

快速开始::

    import zhijiao

    zhijiao.health()                       # 四个后端的可用性
    zhijiao.describe_tools()               # 每个能力会走哪条链

    print(zhijiao.list_courses())
    print(zhijiao.get_course_progress(course_id="..."))
    print(zhijiao.start_learning(course_id="...", mode="progress"))

    zhijiao.call_tool("list_courses")      # 字典化统一信封（给 Agent 用）
"""

from __future__ import annotations

from typing import Any, Optional

from .api import (
    LOGIN_CHANNELS,
    call_tool,
    describe_tools,
    health,
    list_tools,
    login,
    logout,
    make_tools,
)
from .backends import (
    ALL_CAPABILITIES,
    BackendInfo,
    BackendKind,
    BaseBackend,
    Capability,
    backend_infos,
    build_backends,
    get_backend,
)
from .config import Settings, get_settings, reset_settings
from .contracts import (
    AttendanceRecord,
    BrowserHandoff,
    Course,
    CourseDetail,
    CourseProgress,
    CourseType,
    ExamKind,
    ExamResult,
    LearningMode,
    LearningReport,
    SignStatus,
    TaskNode,
)
from .errors import (
    AuthError,
    BackendUnavailable,
    CapabilityNotSupported,
    ConfigError,
    CredentialRejected,
    NotSupported,
    RouteExhausted,
    UpstreamError,
    UpstreamTransientError,
    ZhijiaoError,
)
from .log import RouteTrace, configure_logging, get_logger
from .router import ROUTING_TABLE, Router, RouteResult, default_router
from .session import LoginState, SessionStore, load_state, require_state
from .tools import TOOL_NAMES, Tools, is_handoff

__version__ = "0.1.0"

# --------------------------------------------------------------------------- #
# 模块级便捷函数：用进程级共享 Tools 实例，省去手动构造
# --------------------------------------------------------------------------- #
_TOOLS: Optional[Tools] = None


def tools(
    settings: Optional[Settings] = None,
    session: Optional[LoginState] = None,
    *,
    force: bool = False,
) -> Tools:
    """取进程级共享的 :class:`Tools` 实例。"""
    global _TOOLS
    if _TOOLS is None or force or session is not None:
        _TOOLS = make_tools(settings, session)
    return _TOOLS


def setup(settings: Optional[Settings] = None) -> Settings:
    """**推荐在宿主启动时调用一次**：装载配置 → 建运行期目录 → 初始化统一日志。

    统一层其余部分都是惰性初始化的，不调 ``setup()`` 也能用；
    但调用它才能让 ``config.json`` / 环境变量里的 ``log_level``、``log_jsonl``
    真正生效（路由日志同时落到 ``<log_dir>/zhijiao-YYYY-MM-DD.jsonl``）。

    :return: 生效的 :class:`Settings`
    """
    from .log import configure_logging, default_jsonl_path

    st = settings or get_settings()
    st.ensure_dirs()
    configure_logging(
        st.log_level,
        jsonl_path=default_jsonl_path(st.log_dir) if st.log_jsonl else None,
        force=True,
    )
    return st


def reset_all() -> None:
    """清空全部进程级缓存（测试用）：配置 / 加载器 / Backend / 路由 / Tools。

    已导入的上游模块保留在 ``sys.modules`` —— 这是刻意的，
    因为上游存在函数内懒导入，卸载会破坏它们。
    """
    global _TOOLS
    from .backends import reset_backends
    from .backends.loader import reset_loader
    from .router import reset_router

    _TOOLS = None
    reset_settings()
    reset_loader()
    reset_backends()
    reset_router()


def __getattr__(name: str) -> Any:
    """让 7 个能力可以直接 ``zhijiao.list_courses(...)`` 调用。"""
    if name in TOOL_NAMES:
        def _tool(*args: Any, **kwargs: Any) -> Any:
            return getattr(tools(), name)(*args, **kwargs)

        _tool.__name__ = name
        _tool.__doc__ = f"统一能力 {name}（等价于 zhijiao.tools().{name}）"
        return _tool
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "__version__",
    # 门面
    "Tools",
    "tools",
    "setup",
    "reset_all",
    "call_tool",
    "describe_tools",
    "health",
    "list_tools",
    "make_tools",
    "is_handoff",
    "TOOL_NAMES",
    # 路由
    "Router",
    "RouteResult",
    "ROUTING_TABLE",
    "default_router",
    # Backend
    "Capability",
    "BackendKind",
    "BackendInfo",
    "BaseBackend",
    "ALL_CAPABILITIES",
    "build_backends",
    "get_backend",
    "backend_infos",
    # 契约
    "Course",
    "CourseType",
    "CourseDetail",
    "CourseProgress",
    "TaskNode",
    "AttendanceRecord",
    "SignStatus",
    "ExamResult",
    "ExamKind",
    "LearningReport",
    "LearningMode",
    "BrowserHandoff",
    # 错误
    "ZhijiaoError",
    "ConfigError",
    "AuthError",
    "CredentialRejected",
    "BackendUnavailable",
    "CapabilityNotSupported",
    "UpstreamError",
    "UpstreamTransientError",
    "RouteExhausted",
    "NotSupported",
    # 会话 / 配置 / 日志
    "LoginState",
    "SessionStore",
    "load_state",
    "require_state",
    "Settings",
    "get_settings",
    "reset_settings",
    "configure_logging",
    "get_logger",
    "RouteTrace",
]

"""字典化门面 —— 给 Agent / 其他语言宿主用的稳定接口。

统一信封格式（**唯一**需要理解的结构）::

    {
      "ok": true,
      "data": ...,
      "error": null,
      "route": {"capability": "list_courses", "chosen": "icve_toolkit",
                "fallback_count": 0, "total_ms": 412.3,
                "attempts": [{"backend": "icve_toolkit", "ok": true, ...}]}
    }

失败时 ``ok=false``、``error`` 为 :meth:`zhijiao.errors.ZhijiaoError.to_dict`，
``data`` 为 ``null``，``route`` 依然保留 —— 这样调用方永远能看到"试过哪些项目"。

用法::

    from zhijiao import call_tool
    r = call_tool("list_courses")
    r = call_tool("get_course_progress", course_id="abc")
    r = call_tool("start_learning", course_id="abc", mode="progress")
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from .config import Settings, get_settings
from .contracts import BrowserHandoff
from .errors import ZhijiaoError, to_envelope
from .log import get_logger
from .router import default_router
from .session import LoginState
from .tools import TOOL_NAMES, Tools, is_handoff

__all__ = [
    "call_tool",
    "describe_tools",
    "health",
    "list_tools",
    "make_tools",
    "login",
    "logout",
    "LOGIN_CHANNELS",
]

_log = get_logger("api")


def make_tools(
    settings: Optional[Settings] = None,
    session: Optional[LoginState] = None,
) -> Tools:
    st = settings or get_settings()
    return Tools(settings=st, router=default_router(st, force=False), session=session)


def list_tools() -> List[str]:
    """返回对外承诺的能力名列表。"""
    return list(TOOL_NAMES)


def call_tool(
    name: str,
    *,
    session: Optional[LoginState] = None,
    settings: Optional[Settings] = None,
    tools: Optional[Tools] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """按名调用统一能力，返回统一信封。"""
    if name not in TOOL_NAMES:
        return to_envelope(
            ok=False,
            error=ZhijiaoError(
                f"未知能力 {name!r}",
                code="ZJ-5002",
                details={"available": list(TOOL_NAMES)},
            ),
        )

    t = tools or make_tools(settings, session)
    # include_raw 是本门面的输出选项，不能透传给 Tool（Tool 没有这个形参）
    include_raw = bool(kwargs.pop("include_raw", False))
    try:
        result = getattr(t, name)(session=session, **kwargs)
    except ZhijiaoError as e:
        return to_envelope(ok=False, error=e, route=_trace_dict(t))
    except Exception as e:  # noqa: BLE001 - 兜底成统一信封，绝不把裸异常抛给宿主
        _log.exception("能力 %s 抛出未归一化异常", name)
        return to_envelope(ok=False, error=e, route=_trace_dict(t))

    return to_envelope(ok=True, data=_plain(result, include_raw), route=_trace_dict(t))


def _plain(value: Any, include_raw: bool) -> Any:
    if isinstance(value, BrowserHandoff):
        return value.to_dict()
    if isinstance(value, list):
        return [
            v.to_dict(include_raw=include_raw) if hasattr(v, "to_dict") else v  # type: ignore[call-arg]
            for v in value
        ]
    if hasattr(value, "to_dict"):
        return value.to_dict(include_raw=include_raw)  # type: ignore[call-arg]
    return value


def _trace_dict(t: Tools) -> Optional[Dict[str, Any]]:
    return t.last_trace.to_dict() if t.last_trace is not None else None


def describe_tools(
    settings: Optional[Settings] = None,
) -> Dict[str, Any]:
    """路由画像：每个能力会走哪条 Backend 链、每个 Backend 当前状态。"""
    st = settings or get_settings()
    t = make_tools(st)
    info = t.describe()
    info["tools"] = list(TOOL_NAMES)
    return info


def health(settings: Optional[Settings] = None) -> List[Dict[str, Any]]:
    """所有 Backend 的可用性快照（不发网络请求）。"""
    return [
        c.to_dict()
        for c in make_tools(settings or get_settings()).router.health()
    ]


# --------------------------------------------------------------------------- #
# 统一登录（不属于对外 7 个能力，是取得统一登录态的三条通道）
# --------------------------------------------------------------------------- #
#: 登录通道
#: * ``password`` —— 账密 + 上游全自动滑块（需 playwright + Chromium），最省事
#: * ``edge``     —— **新起一个独立 Edge**（项目内独立 profile），你手动登录；
#:                   与系统里正在运行的任何 Edge 互不相干，支持官方页面的扫码/短信
#: * ``browser``  —— 用系统默认浏览器打开登录页（可能挤进你正在用的浏览器）
#: * ``auto``     —— 先试 password，依赖缺失或失败再退到 edge
LOGIN_CHANNELS = ("password", "edge", "browser", "auto")


def _record_login(
    settings: "Settings",
    *,
    channel: str,
    ok: bool,
    error: Optional[BaseException] = None,
    state: Optional[LoginState] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """把一次登录尝试落到 ``<log_dir>/login-YYYY-MM-DD.jsonl``。

    为什么要有这个：登录失败时终端输出一闪而过，事后完全无从排查
    （2026-09-17 实测：用户登录失败，``state/`` 里没有任何痕迹，
    只能靠"上游目录多了个校准文件"反推它跑过滑块）。
    有了这份日志，才能回答"它到底走到哪一步、抛了什么错"。

    敏感字段一律走 :func:`zhijiao.log.mask` 掩码；**绝不含密码**。
    落盘失败只降级成 debug 日志，绝不影响登录本身。
    """
    import json
    from datetime import datetime, timezone

    from .log import mask

    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        rec: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "channel": channel,
            "ok": ok,
            "error_code": getattr(error, "code", None) if error is not None else None,
            "error_type": type(error).__name__ if error is not None else None,
            "error_message": str(error) if error is not None else None,
            "username": (state.username if state is not None else None) or None,
            "nick_name": (state.nick_name if state is not None else None) or None,
            "stu_id": (state.stu_id if state is not None else None) or None,
            "school": (state.school if state is not None else None) or None,
            "sso_token": mask(state.sso_token) if state is not None and state.sso_token else None,
        }
        if extra:
            rec.update(extra)
        path = settings.log_dir / f"login-{datetime.now():%Y-%m-%d}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:  # pragma: no cover - 日志失败不影响登录
        _log.debug("写入登录日志失败", exc_info=True)
    except Exception:  # noqa: BLE001 - 同上，日志绝不反噬主流程
        _log.debug("构造登录日志失败", exc_info=True)


def login(
    username: Optional[str] = None,
    password: Optional[str] = None,
    *,
    channel: str = "auto",
    timeout: int = 300,
    preferred_port: Optional[int] = None,
    max_attempts: int = 2,
    headless: bool = False,
    settings: Optional[Settings] = None,
    persist: bool = True,
) -> LoginState:
    """取得统一登录态（SSO Token）并按需落盘。

    四个上游共用同一个 SSO Token，所以**登录一次，所有后端都能用**。

    :param channel: ``password`` | ``browser`` | ``auto``（默认先自动、失败退人工）
    :param persist: 成功后写入 ``<state_dir>/session.json``（权限 0600）
    :raises CredentialRejected: 账密错误或回调超时（**不重试、不降级**）
    :raises BackendUnavailable: 依赖缺失（如未装 playwright）
    """
    from .backends import build_backends
    from .errors import BackendUnavailable, CredentialRejected, ConfigError
    from .session import SessionStore

    if channel not in LOGIN_CHANNELS:
        raise ConfigError(
            f"未知登录通道 {channel!r}", details={"available": list(LOGIN_CHANNELS)}
        )

    st_settings = settings or get_settings()
    backends = build_backends(st_settings)

    _BROWSER_HINT = (
        "改用 zhijiao.login(channel='browser') 走人工浏览器回调登录 —— "
        "该通道只需要 lxml，不需要 Chromium 与 playwright"
    )

    def _enrich(exc: BackendUnavailable) -> BackendUnavailable:
        """给"自动登录不可用"补上可操作建议，别让使用者只看到一句缺依赖。"""
        exc.details.setdefault("hint", _BROWSER_HINT)
        return exc

    def _try_password() -> Optional[LoginState]:
        if not (username and password):
            return None
        b = backends.get("icve_toolkit")
        if b is None or not b.available:
            return None
        try:
            return b.login_with_password(  # type: ignore[attr-defined]
                username, password, max_attempts=max_attempts, headless=headless,
            )
        except BackendUnavailable as e:
            raise _enrich(e) from e

    def _try_browser() -> LoginState:
        b = backends.get("mooc_work_answer")
        if b is None or not b.available:
            raise BackendUnavailable(
                "mooc-work-answer 后端不可用，无法走浏览器回调登录",
                backend="mooc_work_answer",
            )
        return b.login_via_browser_callback(  # type: ignore[attr-defined]
            timeout=timeout, preferred_port=preferred_port,
        )

    def _try_edge() -> LoginState:
        b = backends.get("mooc_work_answer")
        if b is None or not b.available:
            raise BackendUnavailable(
                "mooc-work-answer 后端不可用，无法走独立 Edge 登录",
                backend="mooc_work_answer",
            )
        return b.login_via_edge_browser(  # type: ignore[attr-defined]
            timeout=timeout, preferred_port=preferred_port,
        )

    state: Optional[LoginState] = None
    _err: Optional[BaseException] = None
    try:
        if channel == "password":
            if not (username and password):
                raise ConfigError("channel='password' 需要 username 与 password")
            state = _try_password()
            if state is None:
                raise BackendUnavailable(
                    "自动滑块登录不可用（ICVE_Toolkit 后端不在岗或缺依赖）",
                    backend="icve_toolkit",
                    details={"hint": _BROWSER_HINT},
                )
        elif channel == "edge":
            state = _try_edge()
        elif channel == "browser":
            state = _try_browser()
        else:  # auto
            try:
                state = _try_password()
            except BackendUnavailable as e:
                _log.warning("自动滑块登录不可用（%s），退到独立 Edge 通道", e)
                state = _try_edge()
            except CredentialRejected:
                # 账密被拒 => 重试/换通道都没意义（上游明确说明且会触发风控）
                raise
            if state is None:
                _log.info("未提供账密，走独立 Edge 通道")
                state = _try_edge()
    except BaseException as e:  # noqa: BLE001 - 任何结局都要留下日志再抛出
        _err = e
        raise
    finally:
        _record_login(
            st_settings,
            channel=channel,
            ok=(_err is None and state is not None),
            error=_err,
            state=state,
        )

    if persist:
        SessionStore(st_settings).save(state)
    return state


def logout(settings: Optional[Settings] = None) -> bool:
    """清除落盘登录态（``state/session.json``）。"""
    from .session import SessionStore

    return SessionStore(settings or get_settings()).clear()

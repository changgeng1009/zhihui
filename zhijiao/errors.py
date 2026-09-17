"""统一错误（Unified Error Tree）。

所有 Backend 抛出的异常都必须能收敛到本模块的异常树，并带一个**稳定的错误码**。
Router 依据异常类型决定"继续降级"还是"立即失败"。

错误码分区
----------
* ``ZJ-1xxx`` 配置/环境
* ``ZJ-2xxx`` 鉴权/凭据（**不降级**）
* ``ZJ-3xxx`` 后端可用性/能力（**降级**）
* ``ZJ-4xxx`` 上游返回/网络（**降级**）
* ``ZJ-5xxx`` 路由收敛结果
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

__all__ = [
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
    "retryable",
    "to_envelope",
]


class ZhijiaoError(Exception):
    """所有统一层异常的根。

    :param message: 人类可读消息
    :param code: 稳定错误码
    :param backend: 抛错的后端 id（可空）
    :param details: 附加上下文（入参、上游响应片段等）

    ``retryable`` 是**由错误码分区推导**的（见 :attr:`retryable`），
    子类可以显式覆盖。这样 ``ZhijiaoError("...", code="ZJ-4001")`` 也会被正确
    当作可降级 —— 避免"码说可重试、标志说不可重试"的自相矛盾。
    """

    code: str = "ZJ-0000"
    http_status: int = 500
    #: 允许降级的错误码前缀分区
    _RETRYABLE_PREFIXES = ("ZJ-3", "ZJ-4")

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        backend: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.backend = backend
        self.details: Dict[str, Any] = details or {}
        if cause is not None:
            self.__cause__ = cause

    # ------------------------------------------------------------------ #
    @property
    def retryable(self) -> bool:
        """是否允许路由器降级到下一个 Backend。

        规则（对应模块 docstring 的错误码分区）：

        * ``ZJ-1xxx`` 配置 / ``ZJ-2xxx`` 鉴权 / ``ZJ-5xxx`` 路由收敛 → **不降级**
          （换后端也没用，或者已经收敛完了）
        * ``ZJ-3xxx`` 后端可用性 / ``ZJ-4xxx`` 上游返回 → **降级**

        子类若需要例外，直接在类体里写 ``retryable = True/False`` 覆盖本属性。
        """
        return self.code.startswith(self._RETRYABLE_PREFIXES)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "type": type(self).__name__,
            "message": self.message,
            "backend": self.backend,
            "retryable": self.retryable,
            "details": self.details,
        }

    def __str__(self) -> str:  # pragma: no cover
        who = f"[{self.backend}] " if self.backend else ""
        return f"{self.code} {who}{self.message}"


# --------------------------------------------------------------------------- #
# ZJ-1xxx 配置 / 环境
# --------------------------------------------------------------------------- #
class ConfigError(ZhijiaoError):
    """配置缺失或非法（例如未设置上游根目录）。"""

    code = "ZJ-1001"
    http_status = 500
    retryable = False


# --------------------------------------------------------------------------- #
# ZJ-2xxx 鉴权 / 凭据 —— 不降级
# --------------------------------------------------------------------------- #
class AuthError(ZhijiaoError):
    """未登录 / 凭据失效。换 Backend 也无济于事，必须重新登录。"""

    code = "ZJ-2001"
    http_status = 401
    retryable = False


class CredentialRejected(AuthError):
    """账密被拒绝。

    ICVE 上游 README 明确：账号不存在/密码错误等账密类拒绝不重试（重试无意义），
    且同 IP 高频尝试会触发风控惩罚。因此本异常**永不重试、永不降级**。
    """

    code = "ZJ-2002"
    http_status = 401
    retryable = False


# --------------------------------------------------------------------------- #
# ZJ-3xxx 后端可用性 / 能力 —— 降级
# --------------------------------------------------------------------------- #
class BackendUnavailable(ZhijiaoError):
    """后端不可用：缺依赖、缺凭据、或本身是未实现的占位。"""

    code = "ZJ-3001"
    http_status = 503
    retryable = True


class CapabilityNotSupported(ZhijiaoError):
    """后端存在但不支持该能力（例如 ZJY-Toolkit 无 ``list_courses``）。"""

    code = "ZJ-3002"
    http_status = 501
    retryable = True


# --------------------------------------------------------------------------- #
# ZJ-4xxx 上游返回 / 网络 —— 降级
# --------------------------------------------------------------------------- #
class UpstreamError(ZhijiaoError):
    """上游返回了非预期结构，或解析失败。"""

    code = "ZJ-4001"
    http_status = 502
    retryable = True


class UpstreamTransientError(UpstreamError):
    """网络超时、连接重置等可重试的瞬时错误。"""

    code = "ZJ-4002"
    http_status = 504
    retryable = True


# --------------------------------------------------------------------------- #
# ZJ-5xxx 路由收敛结果
# --------------------------------------------------------------------------- #
class RouteExhausted(ZhijiaoError):
    """路由表内所有 Backend 都失败。

    ``details['attempts']`` 里保留每一次尝试的 backend / code / message，
    便于一眼看出"为什么最后没成功"。
    """

    code = "ZJ-5001"
    http_status = 502
    retryable = False

    def __init__(
        self,
        capability: str,
        attempts: List[Dict[str, Any]],
        *,
        cause: Optional[BaseException] = None,
    ) -> None:
        summary = "; ".join(
            "{}({}) {}".format(a.get("backend"), a.get("code"), a.get("message"))
            for a in attempts
        ) or "无可用后端"
        super().__init__(
            f"能力 {capability} 的所有后端均失败：{summary}",
            details={"capability": capability, "attempts": attempts},
            cause=cause,
        )
        self.capability = capability
        self.attempts = attempts


class NotSupported(ZhijiaoError):
    """所有已知 Backend 都不支持该能力。"""

    code = "ZJ-5002"
    http_status = 501
    retryable = False


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def retryable(exc: BaseException) -> bool:
    """该异常是否允许路由器降级到下一个 Backend。"""
    if isinstance(exc, ZhijiaoError):
        return exc.retryable
    # 非统一层异常（上游裸异常）：保守起见视为可降级，交由下一个 Backend 试
    return True


def to_envelope(
    *,
    ok: bool,
    data: Any = None,
    error: Optional[BaseException] = None,
    route: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """统一对外信封。

    ``{"ok": bool, "data": ..., "error": {...}|None, "route": {...}}``

    这是 Agent 侧唯一需要理解的结构。
    """
    env: Dict[str, Any] = {
        "ok": ok,
        "data": data,
        "error": None,
        "route": route,
    }
    if error is not None:
        env["error"] = (
            error.to_dict() if isinstance(error, ZhijiaoError)
            else {
                "ok": False,
                "code": "ZJ-0000",
                "type": type(error).__name__,
                "message": str(error),
                "backend": None,
                "retryable": True,
                "details": {},
            }
        )
    return env

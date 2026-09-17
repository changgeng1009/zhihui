"""能力路由器（Router）。

职责：**给定一个能力，自动决定调用哪个上游项目**，并在失败时按优先级降级。

决策依据（三者缺一不可）
------------------------
1. **声明顺序**：``ROUTING_TABLE[capability]`` 给出候选 Backend 的优先级（见 `docs/TOOL_BACKEND_MAP.md`）；
2. **能力支持**：``backend.supports(cap)`` —— 不支持的直接跳过（如 ZJY 不参与任何实际调用）；
3. **运行期可用**：``backend.available`` —— 缺依赖 / 缺源码 / 未授权的一律跳过。

降级规则
--------
* 可降级：``BackendUnavailable`` / ``CapabilityNotSupported`` /
  ``UpstreamTransientError`` / ``UpstreamError`` / 上游裸异常；
* **不降级**：``AuthError`` / ``CredentialRejected`` / ``ConfigError`` ——
  换个后端也一样缺凭据或配置，继续试只会浪费时间并增加风控风险。

每一次尝试（含跳过）都会写进 :class:`~zhijiao.log.RouteTrace`，
可用 ``verbose=True`` 或 ``last_trace()`` 拿到，回答"为什么走了这个项目"。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .backends import (
    ALL_CAPABILITIES,
    BackendCheck,
    BaseBackend,
    Capability,
    build_backends,
)
from .config import Settings, get_settings
from .errors import ConfigError, RouteExhausted, ZhijiaoError, retryable
from .log import RouteTrace, emit_route, get_logger
from .session import LoginState

__all__ = ["ROUTING_TABLE", "Router", "RouteResult", "default_router"]

_log = get_logger("router")

#: 能力 -> 候选 Backend 顺序（主选 / 次选 / 兜底）。
#: 放在**模块级常量**里，是为了让"路由策略"可以被单测直接断言，
#: 也方便将来改成从配置文件读取。
ROUTING_TABLE: Dict[Capability, List[str]] = {
    # 三域聚合课程列表 —— ICVE 的 get_my_courses 是唯一一次拉全的
    Capability.LIST_COURSES: ["icve_toolkit", "mooc_work_answer", "ocsjs"],
    # 课程结构 —— ICVE 的递归课程树解析最完整
    Capability.GET_COURSE_DETAIL: ["icve_toolkit", "mooc_work_answer", "ocsjs"],
    Capability.GET_COURSE_PROGRESS: ["icve_toolkit", "mooc_work_answer", "ocsjs"],
    # 未完成任务 —— ICVE 的 include_completed=False 语义天然吻合
    Capability.LIST_UNFINISHED_TASKS: ["icve_toolkit", "mooc_work_answer", "ocsjs"],
    # 刷课 —— ICVE 的 run_speed_course 覆盖三分支心跳，绝不可替代
    Capability.START_LEARNING: ["icve_toolkit", "mooc_work_answer", "ocsjs"],
    # 签到 / 考勤 —— 只有 ICVE 有；ZJY 是唯一可能补位的（当前不可用）
    Capability.GET_ATTENDANCE: ["icve_toolkit", "zjy_toolkit", "ocsjs"],
    # 成绩 —— ICVE 有逐次作业/考试列表
    Capability.GET_RESULTS: ["icve_toolkit", "mooc_work_answer", "ocsjs"],
}


@dataclass
class RouteResult:
    """一次路由的返回值 + 轨迹。"""

    value: Any
    backend: str
    capability: Capability
    trace: RouteTrace = field(repr=False, default=None)  # type: ignore[assignment]

    def to_dict(self, *, include_raw: bool = False) -> Dict[str, Any]:
        v = self.value
        if hasattr(v, "to_dict"):
            v = v.to_dict(include_raw=include_raw)  # type: ignore[call-arg]
        elif isinstance(v, list):
            v = [
                x.to_dict(include_raw=include_raw) if hasattr(x, "to_dict") else x  # type: ignore[call-arg]
                for x in v
            ]
        return {"backend": self.backend, "capability": self.capability.value, "data": v}


class Router:
    """统一能力路由器。"""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        backends: Optional[Dict[str, BaseBackend]] = None,
        routing_table: Optional[Dict[Capability, List[str]]] = None,
        *,
        session: Optional[LoginState] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.backends: Dict[str, BaseBackend] = (
            backends if backends is not None else build_backends(self.settings)
        )
        self.table: Dict[Capability, List[str]] = dict(routing_table or ROUTING_TABLE)
        self.session = session
        self._last_trace: Optional[RouteTrace] = None

    # ------------------------------------------------------------------ #
    # 计划
    # ------------------------------------------------------------------ #
    def route(self, capability: Any) -> List[str]:
        """返回该能力的**声明**候选顺序（不做可用性过滤）。"""
        cap = Capability.parse(capability)
        ids = self.table.get(cap)
        if ids is None:
            if cap in ALL_CAPABILITIES:
                raise ConfigError(f"路由表缺少能力 {cap.value} 的配置")
            raise ConfigError(f"未知能力 {cap.value}")
        return list(ids)

    def plan(self, capability: Any) -> List[str]:
        """返回**候选链**：声明顺序 ∩ 已注册 ∩ 支持该能力。

        ⚠️ 这里**刻意不判断可用性**。可用性在 :meth:`invoke` 的循环内逐项检查
        并写进 :class:`RouteTrace`（``skipped`` + 原因），
        这样"``zjy_toolkit`` 因为没有源码被跳过"这类关键信息不会被静默过滤掉。
        需要"当前确实能用"的视图请用 :meth:`attemptable`。
        """
        cap = Capability.parse(capability)
        return [
            bid
            for bid in self.route(cap)
            if bid in self.backends and self.backends[bid].supports(cap)
        ]

    def attemptable(self, capability: Any) -> List[str]:
        """候选链中**当前确实可用**的部分（不跑网络，供预检 / 展示用）。"""
        cap = Capability.parse(capability)
        return [bid for bid in self.plan(cap) if self.backends[bid].available]

    def last_trace(self) -> Optional[RouteTrace]:
        return self._last_trace

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #
    def invoke(
        self,
        capability: Any,
        *,
        session: Optional[LoginState] = None,
        backend: Optional[str] = None,
        no_fallback: Optional[bool] = None,
        verbose: bool = False,
        **kwargs: Any,
    ) -> RouteResult:
        """按优先级链调用，返回 :class:`RouteResult`（含轨迹）。

        :param backend: 强制指定某个 Backend（默认同时关闭降级，便于定位问题）
        :param no_fallback: 关闭降级；显式传 ``False`` 可与 ``backend`` 组合使用
        :param verbose: 打印轨迹摘要
        :raises RouteExhausted: 全部候选失败
        """
        cap = Capability.parse(capability)
        st = session if session is not None else self.session

        if backend is not None:
            # 指定后端默认同时关闭降级（便于定位问题）；
            # 显式传 no_fallback=False 时表示"优先用它，失败后按常规链继续"。
            if no_fallback is None:
                no_fallback = True
            if no_fallback:
                candidates = [backend]
            else:
                candidates = [backend] + [b for b in self.plan(cap) if b != backend]
        else:
            candidates = self.plan(cap)
            if no_fallback is None:
                no_fallback = not self.settings.allow_fallback

        if not candidates:
            raise ConfigError(
                f"没有可用的候选后端支持能力 {cap.value}",
                details={"table": self.route(cap), "registered": sorted(self.backends)},
            )

        trace = RouteTrace(capability=cap.value)
        t0 = time.monotonic()
        last_error: Optional[BaseException] = None

        for bid in candidates:
            b = self.backends.get(bid)
            if b is None:
                trace.record(bid, ok=False, skipped=True, code="ZJ-3001",
                             message="未注册的 Backend id")
                if no_fallback:
                    break
                continue

            if not b.supports(cap):
                trace.record(bid, ok=False, skipped=True, code="ZJ-3002",
                             message="声明不支持该能力")
                if no_fallback:
                    break
                continue

            if not b.available:
                reason = b.unavailable_reason() or "不可用"
                trace.record(bid, ok=False, skipped=True, code="ZJ-3001", message=reason)
                last_error = ZhijiaoError(
                    f"{b.info.name} 不可用：{reason}",
                    code="ZJ-3001", backend=bid, details={"skipped": True},
                )
                if no_fallback:
                    break
                continue

            t1 = time.monotonic()
            try:
                value = b.call(cap, session=st, **kwargs)
            except BaseException as e:  # noqa: BLE001 - 需要统一收敛判定
                ms = (time.monotonic() - t1) * 1000
                code = getattr(e, "code", "ZJ-0000") if isinstance(e, ZhijiaoError) else "ZJ-0000"
                trace.record(bid, ok=False, ms=ms, code=code, message=str(e))
                last_error = e
                if not retryable(e):
                    _log.error("能力 %s 在 %s 上遭遇不可降级错误：%s", cap.value, bid, e)
                    trace.total_ms = (time.monotonic() - t0) * 1000
                    self._finish(trace, verbose)
                    raise
                _log.warning("能力 %s 在 %s 上失败，尝试降级：%s", cap.value, bid, e)
                if no_fallback:
                    break
                continue

            ms = (time.monotonic() - t1) * 1000
            trace.record(bid, ok=True, ms=ms)
            trace.chosen = bid
            trace.total_ms = (time.monotonic() - t0) * 1000
            self._finish(trace, verbose)
            return RouteResult(value=value, backend=bid, capability=cap, trace=trace)

        trace.total_ms = (time.monotonic() - t0) * 1000
        self._finish(trace, verbose)

        if isinstance(last_error, ZhijiaoError) and not last_error.retryable:
            raise last_error
        raise RouteExhausted(cap.value, [a.to_dict() for a in trace.attempts], cause=last_error)

    def invoke_value(self, capability: Any, **kwargs: Any) -> Any:
        """同 :meth:`invoke`，但直接返回数据（丢弃轨迹）。"""
        return self.invoke(capability, **kwargs).value

    # ------------------------------------------------------------------ #
    def _finish(self, trace: RouteTrace, verbose: bool) -> None:
        """收尾：记录轨迹并落到统一日志（含可选 JSONL）。"""
        self._last_trace = trace
        emit_route(trace, logger=_log)
        if verbose:
            for a in trace.attempts:
                _log.info(
                    "  %s %s %s %s",
                    "OK   " if a.ok else ("SKIP " if a.skipped else "FAIL "),
                    a.backend, f"{a.ms:.0f}ms", a.message,
                )

    # ------------------------------------------------------------------ #
    # 健康检查
    # ------------------------------------------------------------------ #
    def health(self) -> List[BackendCheck]:
        """主动探活全部 Backend（不做网络请求）。"""
        checks: List[BackendCheck] = []
        for bid in sorted(self.backends):
            checks.append(self.backends[bid].check())
        return checks

    def describe(self) -> Dict[str, Any]:
        """输出完整路由画像：每个能力会走哪条链、每个后端状态如何。

        三层视图，层层收窄：

        * ``declared``   —— 路由表里声明的顺序（策略意图）；
        * ``candidates`` —— 去掉未注册 / 声明不支持的后端；
        * ``available``  —— 再去掉当前不可用的（真正会被尝试的）。
        """
        return {
            "capabilities": {
                cap.value: {
                    "declared": self.route(cap),
                    "candidates": self.plan(cap),
                    "available": self.attemptable(cap),
                }
                for cap in Capability
            },
            "backends": [c.to_dict() for c in self.health()],
        }


_DEFAULT: Optional[Router] = None


def default_router(
    settings: Optional[Settings] = None,
    *,
    force: bool = False,
) -> Router:
    """进程级默认路由器。"""
    global _DEFAULT
    if _DEFAULT is None or force:
        _DEFAULT = Router(settings or get_settings())
    return _DEFAULT


def reset_router() -> None:
    """丢弃进程级默认路由器（测试用）。"""
    global _DEFAULT
    _DEFAULT = None

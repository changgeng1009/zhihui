"""统一 Tool Layer —— 对外唯一门面。

对上层（Agent / CLI / 任何宿主）只暴露这 7 个能力，签名稳定、返回统一 DTO、
错误统一异常。**这一层不含任何抓取逻辑**，只做三件事：

1. 归一化入参（允许 ``course=Course(...)`` 或 ``course_id=...`` 两种写法）；
2. 通过 :class:`~zhijiao.router.Router` 自动选后端并降级；
3. 记录最后一次路由轨迹，便于回答"这个结果是从哪个项目来的"。

| 能力 | 返回 |
|---|---|
| ``list_courses`` | ``list[Course]`` |
| ``get_course_detail`` | ``CourseDetail`` |
| ``get_course_progress`` | ``CourseProgress`` |
| ``list_unfinished_tasks`` | ``list[TaskNode]`` |
| ``start_learning`` | ``LearningReport`` |
| ``get_attendance`` | ``list[AttendanceRecord]`` |
| ``get_results`` | ``list[ExamResult]`` |

当 API 后端全部不可用时，若兜底后端是 ``ocsjs``，返回的是
:class:`~zhijiao.contracts.BrowserHandoff`（浏览器交接计划）而不是数据 ——
用 ``is_handoff(result)`` 判断即可。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Union

from .backends import Capability
from .config import Settings, get_settings
from .contracts import (
    AttendanceRecord,
    BrowserHandoff,
    Course,
    CourseDetail,
    CourseProgress,
    CourseType,
    ExamResult,
    LearningMode,
    LearningReport,
    TaskNode,
)
from .errors import ZhijiaoError
from .log import RouteTrace, get_logger
from .router import Router, RouteResult, default_router
from .session import LoginState, load_state

__all__ = ["Tools", "is_handoff", "TOOL_NAMES"]

_log = get_logger("tools")

TOOL_NAMES = (
    "list_courses",
    "get_course_detail",
    "get_course_progress",
    "list_unfinished_tasks",
    "start_learning",
    "get_attendance",
    "get_results",
)


def is_handoff(value: Any) -> bool:
    """结果是否为浏览器交接计划（即 API 路线全挂、由 OCS 兜底）。"""
    if isinstance(value, BrowserHandoff):
        return True
    if isinstance(value, list) and value:
        return isinstance(value[0], BrowserHandoff)
    return False


class Tools:
    """7 个统一能力的实现门面。"""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        router: Optional[Router] = None,
        session: Optional[LoginState] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.router = router or default_router(self.settings)
        self.session = session
        self.last_trace: Optional[RouteTrace] = None

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _st(self, session: Optional[LoginState]) -> LoginState:
        """统一登录态：显式传入 > 构造时注入 > 环境变量 / 落盘文件。"""
        if session is not None and session.is_valid:
            return session
        if self.session is not None and self.session.is_valid:
            return self.session
        st = load_state(settings=self.settings)
        if st.is_valid:
            self.session = st
        return st

    def _run(
        self,
        capability: Capability,
        *,
        session: Optional[LoginState],
        backend: Optional[str],
        **kwargs: Any,
    ) -> RouteResult:
        st = self._st(session)
        # OCS 兜底不需要凭据，所以即使 st 为空也让它有机会接手；
        # 需要凭据的后端会自己抛 AuthError（不可降级，直接暴露）。
        try:
            res = self.router.invoke(capability, session=st, backend=backend, **kwargs)
        except ZhijiaoError:
            # 失败路径也要把轨迹带出来 —— 否则错误信封里的 route 是空的，
            # 使用者就看不到"到底试过哪些项目、各自为什么失败"。
            self.last_trace = self.router.last_trace()
            raise
        self.last_trace = res.trace
        if st.is_valid and self.session is None:
            self.session = st
        return res

    @staticmethod
    def _course_kwargs(
        course: Optional[Union[Course, Dict[str, Any]]] = None,
        course_id: Optional[str] = None,
        course_info_id: Optional[str] = None,
        class_id: Optional[str] = None,
        course_type: Optional[Any] = None,
    ) -> Dict[str, Any]:
        kw: Dict[str, Any] = {}
        if isinstance(course, (Course, dict)):
            kw["course"] = course
        if course_id:
            kw["course_id"] = course_id
        if course_info_id:
            kw["course_info_id"] = course_info_id
        if class_id:
            kw["class_id"] = class_id
        if course_type is not None:
            kw["course_type"] = course_type
        return kw

    # ------------------------------------------------------------------ #
    # ① list_courses
    # ------------------------------------------------------------------ #
    def list_courses(
        self,
        *,
        course_type: Optional[Any] = None,
        session: Optional[LoginState] = None,
        backend: Optional[str] = None,
    ) -> List[Course]:
        """列出当前账号的课程（SPOC / MOOC / 资源库三域）。"""
        kw: Dict[str, Any] = {}
        if course_type is not None:
            kw["course_type"] = course_type
        res = self._run(Capability.LIST_COURSES, session=session, backend=backend, **kw)
        return _as_list(res.value)

    # ------------------------------------------------------------------ #
    # ② get_course_detail
    # ------------------------------------------------------------------ #
    def get_course_detail(
        self,
        course: Optional[Union[Course, Dict[str, Any]]] = None,
        *,
        course_id: Optional[str] = None,
        course_info_id: Optional[str] = None,
        class_id: Optional[str] = None,
        course_type: Optional[Any] = None,
        include_completed: bool = True,
        session: Optional[LoginState] = None,
        backend: Optional[str] = None,
    ) -> CourseDetail:
        """获取课程结构（章节 + 课件节点）。"""
        res = self._run(
            Capability.GET_COURSE_DETAIL,
            session=session,
            backend=backend,
            include_completed=include_completed,
            **self._course_kwargs(course, course_id, course_info_id, class_id, course_type),
        )
        return _expect(
            res.value,
            CourseDetail,
            CourseDetail(course=_as_course(course), backend=res.backend),
        )

    # ------------------------------------------------------------------ #
    # ③ get_course_progress
    # ------------------------------------------------------------------ #
    def get_course_progress(
        self,
        course: Optional[Union[Course, Dict[str, Any]]] = None,
        *,
        course_id: Optional[str] = None,
        course_info_id: Optional[str] = None,
        class_id: Optional[str] = None,
        course_type: Optional[Any] = None,
        session: Optional[LoginState] = None,
        backend: Optional[str] = None,
    ) -> CourseProgress:
        """获取课程进度聚合（总数 / 已完成 / 百分比 / 按类型分布）。"""
        res = self._run(
            Capability.GET_COURSE_PROGRESS,
            session=session,
            backend=backend,
            **self._course_kwargs(course, course_id, course_info_id, class_id, course_type),
        )
        return _expect(
            res.value,
            CourseProgress,
            CourseProgress(course=_as_course(course), backend=res.backend),
        )

    # ------------------------------------------------------------------ #
    # ④ list_unfinished_tasks
    # ------------------------------------------------------------------ #
    def list_unfinished_tasks(
        self,
        course: Optional[Union[Course, Dict[str, Any]]] = None,
        *,
        course_id: Optional[str] = None,
        course_info_id: Optional[str] = None,
        class_id: Optional[str] = None,
        course_type: Optional[Any] = None,
        limit: Optional[int] = None,
        session: Optional[LoginState] = None,
        backend: Optional[str] = None,
    ) -> List[TaskNode]:
        """列出课程下未完成的任务（课件单元）。"""
        kw = self._course_kwargs(course, course_id, course_info_id, class_id, course_type)
        if limit:
            kw["limit"] = limit
        res = self._run(Capability.LIST_UNFINISHED_TASKS, session=session, backend=backend, **kw)
        return _as_list(res.value)

    # ------------------------------------------------------------------ #
    # ⑤ start_learning
    # ------------------------------------------------------------------ #
    def start_learning(
        self,
        course: Optional[Union[Course, Dict[str, Any]]] = None,
        *,
        mode: Union[str, LearningMode] = LearningMode.PROGRESS,
        course_id: Optional[str] = None,
        course_info_id: Optional[str] = None,
        class_id: Optional[str] = None,
        course_type: Optional[Any] = None,
        session: Optional[LoginState] = None,
        backend: Optional[str] = None,
        **options: Any,
    ) -> LearningReport:
        """开始学习 / 刷课。

        ``mode``: ``progress`` | ``discussion`` | ``answer`` | ``all``

        ``options`` 会原样透传给后端（如 ICVE 的 ``simulate_real=True``、
        mooc-work-answer 的 ``skip_keywords="#课程A"``）。
        """
        kw = self._course_kwargs(course, course_id, course_info_id, class_id, course_type)
        kw["mode"] = mode
        kw.update(options)
        res = self._run(Capability.START_LEARNING, session=session, backend=backend, **kw)
        return _expect(
            res.value,
            LearningReport,
            LearningReport(course=_as_course(course), backend=res.backend),
        )

    # ------------------------------------------------------------------ #
    # ⑥ get_attendance
    # ------------------------------------------------------------------ #
    def get_attendance(
        self,
        course: Optional[Union[Course, Dict[str, Any]]] = None,
        *,
        course_id: Optional[str] = None,
        course_info_id: Optional[str] = None,
        class_id: Optional[str] = None,
        course_type: Optional[Any] = None,
        limit: Optional[int] = None,
        session: Optional[LoginState] = None,
        backend: Optional[str] = None,
    ) -> List[AttendanceRecord]:
        """获取课程签到 / 考勤记录。"""
        kw = self._course_kwargs(course, course_id, course_info_id, class_id, course_type)
        if limit:
            kw["limit"] = limit
        res = self._run(Capability.GET_ATTENDANCE, session=session, backend=backend, **kw)
        return _as_list(res.value)

    # ------------------------------------------------------------------ #
    # ⑦ get_results
    # ------------------------------------------------------------------ #
    def get_results(
        self,
        course: Optional[Union[Course, Dict[str, Any]]] = None,
        *,
        course_id: Optional[str] = None,
        course_info_id: Optional[str] = None,
        class_id: Optional[str] = None,
        course_type: Optional[Any] = None,
        session: Optional[LoginState] = None,
        backend: Optional[str] = None,
    ) -> List[ExamResult]:
        """获取课程成绩（作业 / 考试 / 测验）。"""
        res = self._run(
            Capability.GET_RESULTS,
            session=session,
            backend=backend,
            **self._course_kwargs(course, course_id, course_info_id, class_id, course_type),
        )
        return _as_list(res.value)

    # ------------------------------------------------------------------ #
    # 便捷方法（基于上面 7 个能力组合，非新能力）
    # ------------------------------------------------------------------ #
    def find_course(
        self,
        *,
        course_id: Optional[str] = None,
        name: Optional[str] = None,
        course_type: Optional[Any] = None,
        courses: Optional[Sequence[Course]] = None,
        session: Optional[LoginState] = None,
    ) -> Course:
        """按 ``course_id`` 精确匹配或 ``name`` 子串匹配定位课程。"""
        from .errors import ZhijiaoError as _E

        pool = list(courses) if courses is not None else self.list_courses(
            course_type=course_type, session=session
        )
        if course_id:
            for c in pool:
                if c.course_id == course_id:
                    return c
        if name:
            hits = [c for c in pool if name in (c.name or "")]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise _E(
                    f"课程名 {name!r} 匹配到 {len(hits)} 门，请用 course_id 指定",
                    code="ZJ-1001",
                    details={"matches": [{"course_id": c.course_id, "name": c.name} for c in hits]},
                )
        raise _E(
            "找不到课程：请提供 course_id 或唯一的 name",
            code="ZJ-1001",
            details={"scanned": len(pool), "course_type": getattr(course_type, "value", course_type)},
        )

    def overview(self, *, session: Optional[LoginState] = None) -> List[Dict[str, Any]]:
        """一次性给出"课程 + 进度 + 待办数"的概览（组合已有能力，零额外请求模式）。"""
        out: List[Dict[str, Any]] = []
        for c in self.list_courses(session=session):
            try:
                prog = self.get_course_progress(course=c, session=session)
                pending = len(self.list_unfinished_tasks(course=c, session=session))
                row = prog.to_dict()
                row["pending"] = pending
                out.append(row)
            except ZhijiaoError as e:
                out.append(
                    {
                        "course": c.to_dict(),
                        "error": e.to_dict(),
                    }
                )
        return out

    # ------------------------------------------------------------------ #
    def describe(self) -> Dict[str, Any]:
        """路由画像（每个能力走哪条链、每个后端状态）。"""
        return self.router.describe()


# --------------------------------------------------------------------------- #
def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    # 浏览器兜底的交接计划：包成单元素列表，便于 is_handoff() 统一识别
    return [value]


def _as_course(value: Any) -> Course:
    if isinstance(value, Course):
        return value
    if isinstance(value, dict):
        return Course(
            course_id=str(value.get("courseId") or value.get("course_id") or ""),
            course_info_id=str(value.get("courseInfoId") or value.get("course_info_id") or ""),
            class_id=str(value.get("classId") or value.get("class_id") or ""),
            name=str(value.get("courseName") or value.get("name") or ""),
            course_type=value.get("course_type") or CourseType.UNKNOWN,
        )
    return Course(course_id="", course_info_id="", name="")


def _expect(value: Any, dto_cls: type, fallback: Any) -> Any:
    """返回期望的 DTO；若后端给的是浏览器交接计划则**原样透传**（不能吞掉）。"""
    if isinstance(value, BrowserHandoff):
        return value
    if isinstance(value, dto_cls):
        return value
    return fallback

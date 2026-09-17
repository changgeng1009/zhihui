"""Adapter: ``atvkh/ICVE_Toolkit`` —— API 主力后端。

**为什么它是主力**（依据见 ``docs/TOOL_BACKEND_MAP.md``）

* 它的 ``ZjyClient`` 天然覆盖三域（主域 zjy2 / AI 域 / 资源库域），
  且 ``get_my_courses()`` 已经做了三域聚合去重 —— 统一层不需要重写合并逻辑；
* 它是四个上游里**唯一**提供签到/考勤能力的项目（``sign.py``）；
* 刷课心跳三种模式的差异（SPOC AES 并发 / MOOC 6API 探测 / 资源库明文串行）
  全部已在上游实现，属于"绝对不能重写"的部分。

**零改造**：本 Adapter 只 import 上游模块并调用其公开函数，从未修改上游文件。
"""

from __future__ import annotations

import importlib
import time
from typing import Any, Dict, List, Optional

from ..contracts import (
    AttendanceRecord,
    Course,
    CourseDetail,
    CourseProgress,
    CourseType,
    ExamKind,
    ExamResult,
    LearningMode,
    LearningReport,
    TaskNode,
    normalize_course_type,
    normalize_learning_mode,
    normalize_progress,
    normalize_sign_status,
)
from ..errors import BackendUnavailable, CredentialRejected, ZhijiaoError
from ..log import get_logger
from ..session import LoginState
from .base import BackendInfo, BackendKind, BaseBackend, Capability, wrap_upstream
from .loader import UpstreamLoader

__all__ = ["IcveToolkitBackend"]

_log = get_logger("backend.icve_toolkit")

#: 上游 ``LearningMode`` -> ``speed_course.run_speed_course(speed_type=...)``
#: 注意上游的分支判断是 ``["all","exam"]``，所以"仅答题"要传 ``exam``。
_SPEED_TYPE: Dict[LearningMode, str] = {
    LearningMode.PROGRESS: "progress",
    LearningMode.DISCUSSION: "discussion",
    LearningMode.ANSWER: "exam",
    LearningMode.ALL: "all",
}

_EXAM_KIND: Dict[str, ExamKind] = {
    "作业": ExamKind.HOMEWORK,
    "考试": ExamKind.EXAM,
    "测验": ExamKind.QUIZ,
    "homework": ExamKind.HOMEWORK,
    "exam": ExamKind.EXAM,
    "quiz": ExamKind.QUIZ,
}


class IcveToolkitBackend(BaseBackend):
    """``ICVE_Toolkit`` 的进程内适配器。"""

    info = BackendInfo(
        id="icve_toolkit",
        name="ICVE_Toolkit（智慧职教课程自动完成工具）",
        kind=BackendKind.API,
        license="PolyForm Noncommercial 1.0.0（禁止商用）",
        upstream_dir="ICVE_Toolkit",
        repo="https://github.com/atvkh/ICVE_Toolkit",
        capabilities=frozenset(
            {
                Capability.LIST_COURSES,
                Capability.GET_COURSE_DETAIL,
                Capability.GET_COURSE_PROGRESS,
                Capability.LIST_UNFINISHED_TASKS,
                Capability.START_LEARNING,
                Capability.GET_ATTENDANCE,
                Capability.GET_RESULTS,
            }
        ),
        notes="三域聚合客户端 ZjyClient；唯一提供签到/考勤的本地后端",
    )

    #: 上游模块（按需加载）
    _MODULES = ("zjy_client", "speed_course", "sign", "answer")

    def __init__(self, settings: Any = None, loader: Optional[UpstreamLoader] = None) -> None:
        super().__init__(settings)
        from .loader import get_loader

        self.loader = loader or get_loader(settings)
        self._avail: Optional[bool] = None
        self._reason = ""
        self._zc_cls: Any = None
        self._runtime_writes_contained = False

    # ------------------------------------------------------------------ #
    # 可用性
    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        if self._avail is None:
            self._avail = self._probe()
        return self._avail

    def unavailable_reason(self) -> str:
        if self._avail is None:
            self.available
        return self._reason

    def _probe(self) -> bool:
        """离线探测：目录 + 依赖 + 能 import 上游核心模块。"""
        try:
            d = self.loader.upstream_dir(self.info.id)
        except ZhijiaoError as e:
            self._reason = str(e)
            return False
        if not (d / "zjy_client.py").is_file():
            self._reason = f"缺少 zjy_client.py（{d}）"
            return False
        for mod in ("requests", "Crypto"):
            try:
                importlib.import_module(mod)
            except ImportError:
                self._reason = (
                    f"缺少依赖 {mod!r}，无法加载 ICVE_Toolkit 核心客户端；"
                    "请 pip install requests pycryptodome"
                )
                return False
        try:
            self._zc_cls = getattr(self.loader.load(self.info.id, "zjy_client"), "ZjyClient")
        except ZhijiaoError as e:
            self._reason = f"加载 zjy_client 失败：{e}"
            return False
        return True

    def check(self):
        c = super().check()
        c.extra["upstream_commit"] = self.loader.commit(self.info.id)
        return c

    # ------------------------------------------------------------------ #
    # 客户端构造（按凭据缓存）
    # ------------------------------------------------------------------ #
    def _client(self, session: Optional[LoginState]) -> Any:
        st = self._require_credential(session)
        self._check_available()

        key = st.sso_token or st.token
        cached = self._clients.get(key)
        if cached is not None:
            return cached

        client = self._zc_cls(
            token=st.token or None,
            sso_token=st.sso_token or None,
            question_bank_dir=self.settings.question_bank_dir or None,
        )

        # 只有 SSO Token 时，用上游的 passLogin 无感换取主域 Bearer
        if not client.token:
            with wrap_upstream(self.info.id, "refresh_token_from_sso"):
                ok = client.refresh_token_from_sso()
            if not ok:
                raise CredentialRejected(
                    "SSO Token 无法换取主域 Bearer（可能已过期）",
                    backend=self.info.id,
                    details={"hint": "重新登录或更新 ZJ_SSO_TOKEN"},
                )

        # 回填身份信息到统一登录态（不落盘密码）
        if getattr(client, "user_info", None):
            st.nick_name = st.nick_name or str(client.user_info.get("nickName", "") or "")
            st.school = st.school or str(client.user_info.get("schoolName", "") or "")
        st.stu_id = st.stu_id or str(getattr(client, "stu_id", "") or "")

        self._clients[key] = client
        _log.debug(
            "ZjyClient 就绪 user=%s stu_id=%s",
            st.nick_name or st.username or "?",
            st.stu_id or "?",
        )
        return client

    # ------------------------------------------------------------------ #
    # ① list_courses
    # ------------------------------------------------------------------ #
    def list_courses(
        self,
        *,
        session: Optional[LoginState] = None,
        course_type: Optional[Any] = None,
        **_: Any,
    ) -> List[Course]:
        client = self._client(session)
        with wrap_upstream(self.info.id, "get_my_courses"):
            raw = client.get_my_courses() or []

        wanted = normalize_course_type(course_type) if course_type else CourseType.UNKNOWN
        out: List[Course] = []
        for r in raw:
            if not isinstance(r, dict):
                continue
            c = self._to_course(r)
            if not c.course_id:
                continue
            if wanted is not CourseType.UNKNOWN and c.course_type is not wanted:
                continue
            out.append(c)
        _log.info("list_courses: %d 门（过滤后）", len(out))
        return out

    def _to_course(self, raw: Dict[str, Any]) -> Course:
        return Course(
            course_id=str(raw.get("courseId") or raw.get("id") or ""),
            course_info_id=str(raw.get("courseInfoId") or ""),
            class_id=str(raw.get("classId") or ""),
            name=str(raw.get("courseName") or raw.get("courseTitle") or raw.get("name") or ""),
            course_type=normalize_course_type(raw.get("_courseType")),
            backend=self.info.id,
            raw=dict(raw),
        )

    # ------------------------------------------------------------------ #
    # ② get_course_detail
    # ------------------------------------------------------------------ #
    def get_course_detail(
        self,
        *,
        session: Optional[LoginState] = None,
        include_completed: bool = True,
        **kw: Any,
    ) -> CourseDetail:
        course = self._resolve_course(session=session, **kw)
        cells = self._fetch_cells(session, course, include_completed=include_completed)
        ctype = self._ctype_of(course)
        nodes = [self._to_task(c, ctype) for c in cells]
        return CourseDetail(course=course, nodes=nodes, backend=self.info.id)

    # ------------------------------------------------------------------ #
    # ③ get_course_progress
    # ------------------------------------------------------------------ #
    def get_course_progress(
        self,
        *,
        session: Optional[LoginState] = None,
        **kw: Any,
    ) -> CourseProgress:
        course = self._resolve_course(session=session, **kw)

        # 资源库域（RESOURCE）：上游扫描分支把每片叶子的 _speed 硬编码为 0
        # （zjy_client.get_course_cells 的 RESOURCE 分支），按叶子平均会永远得 0%。
        # 服务端在 zyk 课程列表里直接给了权威进度字段 studySpeed，优先取它。
        if course.course_type == CourseType.RESOURCE and course.raw.get("studySpeed") is not None:
            from ..contracts import normalize_progress

            return CourseProgress(
                course=course,
                percent=normalize_progress(course.raw.get("studySpeed")),
                backend=self.info.id,
            )

        detail = self.get_course_detail(session=session, include_completed=True, course=course)
        return CourseProgress.from_nodes(detail.course, detail.nodes, backend=self.info.id)

    # ------------------------------------------------------------------ #
    # ④ list_unfinished_tasks
    # ------------------------------------------------------------------ #
    def list_unfinished_tasks(
        self,
        *,
        session: Optional[LoginState] = None,
        limit: Optional[int] = None,
        **kw: Any,
    ) -> List[TaskNode]:
        # 上游 get_course_cells(include_completed=False) 的语义**本身就是**
        # "未完成课件"（源码内 _skipped_speed_count 专门跳过 _speed >= 100），
        # 因此这里直接复用，不做二次过滤。
        detail = self.get_course_detail(session=session, include_completed=False, **kw)
        nodes = [n for n in detail.nodes if not n.finished]
        return nodes[:limit] if limit else nodes

    # ------------------------------------------------------------------ #
    # ⑤ start_learning
    # ------------------------------------------------------------------ #
    def start_learning(
        self,
        *,
        session: Optional[LoginState] = None,
        mode: Any = LearningMode.PROGRESS,
        simulate_real: bool = False,
        **kw: Any,
    ) -> LearningReport:
        course = self._resolve_course(session=session, **kw)
        client = self._client(session)
        lm = normalize_learning_mode(mode)

        # 上游 run_speed_course 返回 None 且内部 try/except 吞异常，
        # 因此用"前后未完成任务数差值"推导一份**诚实的**执行报告。
        pending_before = self._count_pending(client, course)
        started = time.monotonic()
        upstream_error = ""
        try:
            sc = self.loader.load(self.info.id, "speed_course")
            with wrap_upstream(self.info.id, "run_speed_course"):
                sc.run_speed_course(
                    client,
                    course.raw,
                    speed_type=_SPEED_TYPE[lm],
                    simulate_real=simulate_real,
                )
        except ZhijiaoError as e:
            upstream_error = f"{getattr(e, 'code', 'ZJ-0000')} {e}"
            _log.warning("run_speed_course 抛错：%s", e)
        elapsed = time.monotonic() - started

        pending_after = self._count_pending(client, course)
        succeeded = max(pending_before - pending_after, 0)
        failed = pending_after if pending_before else 0

        return LearningReport(
            course=course,
            mode=lm,
            attempted=pending_before,
            succeeded=succeeded,
            failed=failed,
            elapsed_s=elapsed,
            errors=[upstream_error] if upstream_error else [],
            details={
                "source": "progress_diff",
                "pending_before": pending_before,
                "pending_after": pending_after,
                "simulate_real": simulate_real,
                "speed_type": _SPEED_TYPE[lm],
                "note": (
                    "ICVE_Toolkit 的 run_speed_course 无返回值且内部吞异常，"
                    "本报告由执行前后未完成任务数差值推导；"
                    "精确到单个课件的结果请对比 list_unfinished_tasks 前后差异。"
                ),
            },
            backend=self.info.id,
        )

    # ------------------------------------------------------------------ #
    # ⑥ get_attendance
    # ------------------------------------------------------------------ #
    def get_attendance(
        self,
        *,
        session: Optional[LoginState] = None,
        limit: Optional[int] = None,
        **kw: Any,
    ) -> List[AttendanceRecord]:
        course = self._resolve_course(session=session, **kw)
        client = self._client(session)
        sign_mod = self.loader.load(self.info.id, "sign")
        with wrap_upstream(self.info.id, "get_signs"):
            raw = sign_mod.get_signs(
                client, course.class_id, course.course_info_id, course.course_id
            ) or []
        out = [self._to_attendance(r, course) for r in raw if isinstance(r, dict)]
        return out[:limit] if limit else out

    def _to_attendance(self, raw: Dict[str, Any], course: Course) -> AttendanceRecord:
        return AttendanceRecord(
            sign_id=str(raw.get("signId") or raw.get("id") or ""),
            course_id=course.course_id,
            title=str(raw.get("teachTitle") or ""),
            sign_type=str(raw.get("signType") or raw.get("gesture") or ""),
            status=normalize_sign_status(raw.get("myStatus")),
            time=str(
                raw.get("mySignTime")
                or raw.get("startTime")
                or raw.get("teachDate")
                or ""
            ),
            class_id=course.class_id,
            course_info_id=course.course_info_id,
            backend=self.info.id,
            raw=dict(raw),
        )

    # ------------------------------------------------------------------ #
    # ⑦ get_results
    # ------------------------------------------------------------------ #
    def get_results(
        self,
        *,
        session: Optional[LoginState] = None,
        **kw: Any,
    ) -> List[ExamResult]:
        course = self._resolve_course(session=session, **kw)
        client = self._client(session)
        answer_mod = self.loader.load(self.info.id, "answer")
        ctype = self._ctype_of(course)
        with wrap_upstream(self.info.id, "get_course_exams_list"):
            raw = answer_mod.get_course_exams_list(
                client, course.class_id, course.course_info_id, course.course_id, ctype
            ) or []
        return [self._to_exam(r, course) for r in raw if isinstance(r, dict)]

    def _to_exam(self, raw: Dict[str, Any], course: Course) -> ExamResult:
        kind_raw = str(raw.get("type") or "").strip()
        return ExamResult(
            exam_id=str(raw.get("examId") or raw.get("id") or ""),
            title=str(raw.get("title") or ""),
            kind=_EXAM_KIND.get(kind_raw, _EXAM_KIND.get(kind_raw.lower(), ExamKind.UNKNOWN)),
            score=_parse_score(raw.get("score")),
            submitted=bool(raw.get("submit", False)),
            status=kind_raw,
            course_id=course.course_id,
            backend=self.info.id,
            raw=dict(raw),
        )

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _ctype_of(course: Course) -> str:
        """统一 CourseType -> 上游 ctype 字符串（上游认 SPOC/MOOC/RESOURCE）。"""
        ct = course.course_type
        if ct in (CourseType.SPOC, CourseType.MOOC, CourseType.RESOURCE):
            return ct.value
        return "SPOC"

    def _fetch_cells(
        self,
        session: Optional[LoginState],
        course: Course,
        *,
        include_completed: bool,
    ) -> List[Dict[str, Any]]:
        client = self._client(session)
        ctype = self._ctype_of(course)
        with wrap_upstream(self.info.id, "get_course_cells"):
            cells = client.get_course_cells(
                course.course_info_id,
                course.class_id,
                course.course_id,
                include_completed=include_completed,
                ctype=ctype,
            )
        return [c for c in (cells or []) if isinstance(c, dict)]

    def _to_task(self, raw: Dict[str, Any], ctype: str) -> TaskNode:
        raw_speed = raw.get("_speed")
        if raw_speed is None and self._zc_cls is not None:
            try:
                raw_speed = self._zc_cls._parse_cell_speed(raw, ctype)
            except Exception:  # pragma: no cover - 上游改了签名也不该炸
                raw_speed = 0
        progress = normalize_progress(raw_speed)
        return TaskNode(
            id=str(raw.get("id") or ""),
            name=str(raw.get("name") or ""),
            file_type=str(raw.get("fileType") or ""),
            progress=progress,
            finished=progress >= 100.0,
            is_leaf=True,
            url=str(raw.get("fileUrl") or ""),
            duration_s=_parse_duration(raw),
            parent_id=str(raw.get("parentId") or ""),
            is_container=False,
            backend=self.info.id,
            raw=dict(raw),
        )

    def _count_pending(self, client: Any, course: Course) -> int:
        """直接复用已构造的 client 统计未完成任务数（不重复校验凭据）。"""
        ctype = self._ctype_of(course)
        try:
            with wrap_upstream(self.info.id, "get_course_cells"):
                raw = client.get_course_cells(
                    course.course_info_id,
                    course.class_id,
                    course.course_id,
                    include_completed=False,
                    ctype=ctype,
                )
            return sum(1 for r in (raw or []) if isinstance(r, dict))
        except ZhijiaoError as e:
            _log.warning("统计未完成任务失败：%s", e)
            return 0

    # ------------------------------------------------------------------ #
    # 运行期写入收容（"零改造上游"的唯一例外，见 docstring）
    # ------------------------------------------------------------------ #
    def _contain_runtime_writes(self) -> None:
        """把上游"写在自己目录旁"的运行期文件重定向到项目内 ``state/``。

        背景：``slider_auto._calib_path()`` 用 ``Path(__file__).parent`` 定位，
        源码运行时就是 ``upstreams/ICVE_Toolkit/slider_calib.json`` ——
        一次真实的滑块登录就会污染"上游只读区"，让阶段 1 的
        逐文件 sha256 完整性测试变红（2026-09-17 实测发生）。

        做法：**运行期**替换这一个路径函数，让校准数据落到
        ``<state_dir>/slider_calib.json``。

        为什么这不算违反"零改造上游"纪律：

        * 上游**文件一个字节都没动**（阶段 1 依然全绿）；
        * 只改了"数据写到哪里"，**完全不碰**缺口识别、轨迹生成、
          token 捕获这些业务逻辑；
        * 这是 Adapter 的本职：收容自己所依赖组件的副作用。

        即便如此，它仍是唯一一处对上游对象的运行期替换，
        因此单独用 ``test_stage7_containment.py`` 钉住，并在这里明说。
        """
        if self._runtime_writes_contained:
            return
        mod = self.loader.load(self.info.id, "slider_auto")

        state_dir = self.settings.state_dir
        target = state_dir / "slider_calib.json"

        def _contained_calib_path() -> Any:
            target.parent.mkdir(parents=True, exist_ok=True)
            return target

        _contained_calib_path.__doc__ = (
            "zhijiao 收容版：把滑块校准数据指到项目内 state/，不写上游目录。"
        )
        if callable(getattr(mod, "_calib_path", None)):
            mod._calib_path = _contained_calib_path
            _log.debug("slider_auto._calib_path 已重定向 -> %s", target)

        self._runtime_writes_contained = True

    # ------------------------------------------------------------------ #
    # 额外能力：用账密换取 SSO Token（可选，依赖 playwright）
    # ------------------------------------------------------------------ #
    def login_with_password(
        self,
        username: str,
        password: str,
        *,
        max_attempts: int = 2,
        headless: bool = False,
    ) -> LoginState:
        """调用上游全自动滑块登录，返回统一 :class:`LoginState`。

        **不属于对外 7 个能力**，是统一登录态的可选获取通道。
        需要 playwright + opencv + numpy + scipy + pillow（Chromium 内核约 700MB，
        本项目的约定是放在项目内 ``browsers/``，见 ``Settings.browsers_dir``）。
        不带这些依赖时抛 :class:`BackendUnavailable`，并建议改用
        ``channel='browser'`` 的人工回调通道。
        """
        self._check_available()
        # 让 playwright 认出项目内的浏览器内核目录（幂等，显式设过则不覆盖）
        self.settings.ensure_dirs()
        # 把上游会写到它自己目录旁的运行期文件重定向到项目内 state/
        self._contain_runtime_writes()
        try:
            slider = self.loader.load(self.info.id, "slider_auto")
        except ZhijiaoError as e:
            raise BackendUnavailable(
                f"滑块登录模块加载失败：{e}",
                backend=self.info.id,
                details={"hint": "pip install playwright opencv-python numpy scipy pillow"},
            ) from e

        missing = slider.missing_packages()
        if missing:
            raise BackendUnavailable(
                "全自动滑块登录缺少依赖：" + ", ".join(missing),
                backend=self.info.id,
                details={
                    "hint": "pip install -r upstreams/ICVE_Toolkit/requirements.txt && playwright install chromium"
                },
            )

        with wrap_upstream(self.info.id, "obtain_sso_token"):
            sso = slider.obtain_sso_token(username, password, max_attempts=max_attempts, headless=headless)

        if not sso:
            raise CredentialRejected(
                "自动登录失败：滑块未通过或账密被拒",
                backend=self.info.id,
                details={
                    "hint": "上游明确说明账密类拒绝不重试；同 IP 高频尝试会触发风控，请勿反复重试",
                },
            )
        return LoginState(sso_token=sso, username=username)


# --------------------------------------------------------------------------- #
# 字段解析助手
# --------------------------------------------------------------------------- #
def _parse_score(raw: Any) -> Optional[float]:
    """上游用 ``"-"`` 表示"还没有成绩"。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s in ("-", "None", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_duration(raw: Dict[str, Any]) -> Optional[int]:
    """尽力从上游课件 dict 里取出时长（秒），取不到返回 None。"""
    for key in ("duration", "_duration", "totalTime", "totalSeconds", "videoTime"):
        v = raw.get(key)
        if v in (None, "", "-"):
            continue
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    return None

"""Adapter: ``11273/mooc-work-answer`` —— API 次选后端。

**为什么它是次选而不是主力**（依据见 ``docs/TOOL_BACKEND_MAP.md``）

* 它有四个上游里最干净的抽象 ``BaseAPIClient``（多 base_url 的 get/post/put），
  但**没有任何签到/考勤能力** —— 而这正是 ``get_attendance`` 唯一缺不得的东西；
* 它的刷课入口 ``AIMoocHandler`` / ``ZYKMoocHandler`` 在 ``__init__`` 里就调用
  ``start_courses()``，粒度是**整个账号**而不是单门课程；
* 它的 SPOC（zjy2 主域）不支持（其 README 指职教云需用另一个独立仓库）。

因此它精确地承担三件事：

1. AI 域（``ai.icve.com.cn``）与资源库域（``zyk.icve.com.cn``）的**结构化查询**；
2. ICVE_Toolkit 失败时的 ``list_courses`` / ``get_course_detail`` / 进度类降级；
3. ``start_learning`` 的降级（账号级语义，见下文 ``scope``）。

**零改造**：只调用 ``AIMoocApi`` / ``ZYKMoocApi`` / ``AIMoocHandler`` / ``ZYKMoocHandler``
的公开方法，从未修改上游文件。

许可证提醒
----------
上游**没有 LICENSE 文件**，法律上默认"保留所有权利"，仅限个人本地学习研究。
详见 ``LICENSES_AND_COMPLIANCE.md``。
"""

from __future__ import annotations

import importlib
import time
from typing import Any, Dict, List, Optional

from ..contracts import (
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
)
from ..errors import (
    BackendUnavailable,
    CapabilityNotSupported,
    CredentialRejected,
    UpstreamError,
    ZhijiaoError,
)
from ..log import get_logger, mask as _mask
from ..session import LoginState
from .base import BackendInfo, BackendKind, BaseBackend, Capability, wrap_upstream
from .loader import UpstreamLoader

__all__ = ["MoocWorkAnswerBackend"]

_log = get_logger("backend.mooc_work_answer")

#: 与 ICVE 侧保持一致的域名 -> 统一课程类型映射
_DOMAIN_TYPE = {
    "mooc": CourseType.MOOC,
    "resource": CourseType.RESOURCE,
}


class MoocWorkAnswerBackend(BaseBackend):
    """``mooc-work-answer`` 的进程内适配器。"""

    info = BackendInfo(
        id="mooc_work_answer",
        name="mooc-work-answer（智慧职教助手）",
        kind=BackendKind.API,
        license="无 LICENSE 文件（默认保留所有权利，仅限个人学习研究）",
        upstream_dir="mooc-work-answer",
        repo="https://github.com/11273/mooc-work-answer",
        capabilities=frozenset(
            {
                Capability.LIST_COURSES,
                Capability.GET_COURSE_DETAIL,
                Capability.GET_COURSE_PROGRESS,
                Capability.LIST_UNFINISHED_TASKS,
                Capability.START_LEARNING,
                Capability.GET_RESULTS,
                # 注意：刻意**不含** GET_ATTENDANCE —— 上游没有签到模块
            }
        ),
        notes="BaseAPIClient 抽象最干净；覆盖 AI 域与资源库域；无签到；刷课为账号级",
    )

    def __init__(self, settings: Any = None, loader: Optional[UpstreamLoader] = None) -> None:
        super().__init__(settings)
        from .loader import get_loader

        self.loader = loader or get_loader(settings)
        self._avail: Optional[bool] = None
        self._reason = ""

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
        try:
            d = self.loader.upstream_dir(self.info.id)
        except ZhijiaoError as e:
            self._reason = str(e)
            return False
        if not (d / "AIMoocMain" / "api.py").is_file() and not (d / "ZYKMoocMain" / "api.py").is_file():
            self._reason = f"缺少 AIMoocMain/api.py 与 ZYKMoocMain/api.py（{d}）"
            return False
        for mod in ("requests", "Crypto"):
            try:
                importlib.import_module(mod)
            except ImportError:
                self._reason = (
                    f"缺少依赖 {mod!r}；请 pip install requests pycryptodome lxml"
                )
                return False
        try:
            self.loader.load(self.info.id, "ZYKMoocMain.api")
        except ZhijiaoError as e:
            self._reason = f"加载 ZYKMoocMain.api 失败：{e}"
            return False
        return True

    def check(self):
        c = super().check()
        c.extra["upstream_commit"] = self.loader.commit(self.info.id)
        return c

    # ------------------------------------------------------------------ #
    # 客户端（按域 + 凭据缓存）
    # ------------------------------------------------------------------ #
    def _client(self, session: Optional[LoginState], domain: str) -> Any:
        """``domain``: ``"mooc"``（AI 域）或 ``"resource"``（资源库域）。"""
        st = self._require_credential(session)
        self._check_available()

        key = f"{domain}:{st.credential}"
        cached = self._clients.get(key)
        if cached is not None:
            return cached

        if domain == "mooc":
            mod = self.loader.load(self.info.id, "AIMoocMain.api")
            client = mod.AIMoocApi(token=st.credential)
        elif domain == "resource":
            mod = self.loader.load(self.info.id, "ZYKMoocMain.api")
            client = mod.ZYKMoocApi(token=st.credential)
        else:  # pragma: no cover - 内部调用不会走到
            raise CapabilityNotSupported(f"未知域 {domain!r}", backend=self.info.id)

        if not getattr(client, "access_token", None):
            raise UpstreamError(
                f"{self.info.name} 在 {domain} 域换取 access_token 失败（SSO Token 可能已过期）",
                backend=self.info.id,
                details={"domain": domain},
            )
        self._clients[key] = client
        return client

    @staticmethod
    def _domain_of(course_type: CourseType) -> Optional[str]:
        return _DOMAIN_TYPE.get(course_type.value.lower()) if isinstance(course_type, CourseType) else None

    def _domains_for(self, course_type: Any) -> List[str]:
        """根据请求的类型决定要访问哪些域。未知则两个都试。"""
        ct = normalize_course_type(course_type) if course_type else CourseType.UNKNOWN
        if ct is CourseType.MOOC:
            return ["mooc"]
        if ct is CourseType.RESOURCE:
            return ["resource"]
        if ct is CourseType.SPOC:
            # 上游不覆盖 zjy2 主域 SPOC
            return []
        return ["mooc", "resource"]

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
        domains = self._domains_for(course_type)
        if not domains:
            raise CapabilityNotSupported(
                f"{self.info.name} 不覆盖 SPOC（zjy2 主域）课程；"
                "职教云 SPOC 请使用 ICVE_Toolkit 后端",
                backend=self.info.id,
                details={"course_type": "SPOC"},
            )

        out: List[Course] = []
        errors: List[ZhijiaoError] = []
        for domain in domains:
            try:
                out.extend(self._list_domain(session, domain))
            except ZhijiaoError as e:
                errors.append(e)
                _log.warning("list_courses 域 %s 失败：%s", domain, e)

        if not out and errors:
            raise errors[0]
        return out

    def _list_domain(self, session: Optional[LoginState], domain: str) -> List[Course]:
        client = self._client(session, domain)
        with wrap_upstream(self.info.id, f"my_course_list[{domain}]"):
            payload = client.my_course_list(page_num=1, page_size=9999, flag=1) \
                if domain == "resource" else client.my_course_list(page_num=1, page_size=9999)
        rows = _rows(payload)
        ct = _DOMAIN_TYPE[domain]
        return [c for c in (self._to_course(r, ct) for r in rows if isinstance(r, dict)) if c.course_id]

    def _to_course(self, raw: Dict[str, Any], ct: CourseType) -> Course:
        # 两个域的 courseInfoId 字段名不同：
        #   资源库域  -> courseInfoId
        #   AI 域     -> id
        course_info_id = raw.get("courseInfoId") or (raw.get("id") if ct is CourseType.MOOC else "") or ""
        course_id = raw.get("courseId") or raw.get("id") or ""
        return Course(
            course_id=str(course_id),
            course_info_id=str(course_info_id),
            class_id=str(raw.get("classId") or ""),
            name=str(raw.get("courseName") or raw.get("name") or ""),
            course_type=ct,
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
        domain = self._domain_of(course.course_type)
        if domain is None:
            raise CapabilityNotSupported(
                f"{self.info.name} 不支持课程类型 {course.course_type.value}",
                backend=self.info.id,
            )
        nodes = self._fetch_nodes(session, course, domain)
        if not include_completed:
            nodes = [n for n in nodes if not n.finished]
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
        detail = self.get_course_detail(session=session, include_completed=True, **kw)
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
        skip_keywords: str = "",
        **kw: Any,
    ) -> LearningReport:
        """委托给上游自己的刷课处理器。

        ⚠️ **语义差异（重要）**：上游 ``AIMoocHandler`` / ``ZYKMoocHandler`` 在
        ``__init__`` 中即调用 ``start_courses()``，粒度是**整个账号**，
        没有"只刷某一门课"的入口。因此本方法：

        * 只支持 ``mode=progress`` / ``all``（进度与讨论）；
        * 报告里 ``details['scope'] = 'account'``，``attempted`` 为**该课程**的待办数，
          ``succeeded`` 为执行后减少量（与 ICVE 侧同样是差值推导）；
        * 不想被刷的课请用 ``skip_keywords``（上游 ``jump_content`` 格式：
          以 ``#`` 分隔，如 ``"#课程A#课程B"``）。
        """
        course = self._resolve_course(session=session, **kw)
        lm = normalize_learning_mode(mode)
        if lm not in (LearningMode.PROGRESS, LearningMode.ALL):
            raise CapabilityNotSupported(
                f"{self.info.name} 的刷课入口只支持 progress/all；"
                f"mode={lm.value} 请使用 ICVE_Toolkit 后端",
                backend=self.info.id,
            )

        domain = self._domain_of(course.course_type)
        if domain is None:
            raise CapabilityNotSupported(
                f"{self.info.name} 不支持课程类型 {course.course_type.value}",
                backend=self.info.id,
            )
        st = self._require_credential(session)

        pending_before = self._count_pending(session, course, domain)
        upstream_error = ""
        try:
            if domain == "resource":
                mod = self.loader.load(self.info.id, "ZYKMoocMain.main")
                handler = mod.ZYKMoocHandler
                handler(jump_content=skip_keywords, token=st.credential)
            else:
                mod = self.loader.load(self.info.id, "AIMoocMain.main")
                handler = mod.AIMoocHandler
                handler(jump_content=skip_keywords, token=st.credential)
        except ZhijiaoError as e:
            upstream_error = f"{getattr(e, 'code', 'ZJ-0000')} {e}"
            _log.warning("上游刷课处理器抛错：%s", e)
        except Exception as e:  # 上游内部 try/except 之外的意外
            upstream_error = f"{type(e).__name__}: {e}"
            _log.warning("上游刷课处理器异常：%s", e)

        pending_after = self._count_pending(session, course, domain)

        return LearningReport(
            course=course,
            mode=lm,
            attempted=pending_before,
            succeeded=max(pending_before - pending_after, 0),
            failed=pending_after if pending_before else 0,
            elapsed_s=0.0,
            errors=[upstream_error] if upstream_error else [],
            details={
                "source": "progress_diff",
                "scope": "account",
                "warning": (
                    "mooc-work-answer 的刷课入口是账号级的，"
                    "执行时会遍历账号下所有课程；如需排除请传 skip_keywords"
                ),
                "domain": domain,
                "pending_before": pending_before,
                "pending_after": pending_after,
                "skip_keywords": skip_keywords,
            },
            backend=self.info.id,
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
        """⚠️ 语义说明：上游只有**课程级最终成绩**（``finalScore``），
        没有逐次作业/考试的明细接口。因此本方法返回的是"课程总成绩"记录，
        ``status`` 字段标注为 ``course_final_score`` 以示区分。

        需要逐次作业/考试的分数明细，请使用 ICVE_Toolkit 后端。
        """
        course = self._resolve_course(session=session, **kw)
        domain = self._domain_of(course.course_type)
        if domain is None:
            raise CapabilityNotSupported(
                f"{self.info.name} 不支持课程类型 {course.course_type.value}",
                backend=self.info.id,
            )
        raw = course.raw or {}
        score = _first_number(raw.get("finalScore"), raw.get("score"), raw.get("totalScore"))
        return [
            ExamResult(
                exam_id=f"{course.course_id}:final",
                title=f"{course.name}（课程总成绩）",
                kind=ExamKind.UNKNOWN,
                score=score,
                submitted=score is not None,
                status="course_final_score",
                course_id=course.course_id,
                backend=self.info.id,
                raw={"finalScore": raw.get("finalScore"), "studySpeed": raw.get("studySpeed")},
            )
        ]

    # ------------------------------------------------------------------ #
    # 课程树
    # ------------------------------------------------------------------ #
    def _fetch_nodes(
        self,
        session: Optional[LoginState],
        course: Course,
        domain: str,
    ) -> List[TaskNode]:
        client = self._client(session, domain)
        if domain == "resource":
            with wrap_upstream(self.info.id, "study_design_list[resource]"):
                design = client.study_design_list(course.course_info_id)
            return self._walk(client, course, _as_list(design), domain)
        # AI 域：先拿设计列表，再递归 getCellList；已完成集合来自 study_record_list
        with wrap_upstream(self.info.id, "study_design_list[mooc]"):
            design = client.study_design_list(course.course_info_id, course.course_id)
        with wrap_upstream(self.info.id, "study_record_list[mooc]"):
            records = client.study_record_list(course.course_info_id, course.course_id)
        self._ai_completed = _flatten_ids(records)
        return self._walk(client, course, _as_list(design), domain)

    def _walk(
        self,
        client: Any,
        course: Course,
        design: List[Dict[str, Any]],
        domain: str,
    ) -> List[TaskNode]:
        """把设计列表展开成叶子节点。

        这里只做**结构遍历 + 字段改名**，不含任何抓取/加解密逻辑；
        AI 域的第二层调用的是上游自己的 ``get_cell_list``。
        """
        out: List[TaskNode] = []
        for node in design:
            if not isinstance(node, dict):
                continue
            children = node.get("children")
            if not children and domain == "mooc":
                # AI 域的 design 列表层不直接带 children，需再拉一层
                with wrap_upstream(self.info.id, "get_cell_list[mooc]"):
                    children = client.get_cell_list(
                        course.course_info_id, course.course_id, node.get("id")
                    )
            if isinstance(children, list) and children:
                for child in children:
                    if isinstance(child, dict):
                        out.append(self._to_task(child, domain))
            else:
                out.append(self._to_task(node, domain))
        return [n for n in out if n.id]

    def _to_task(self, raw: Dict[str, Any], domain: str) -> TaskNode:
        if domain == "resource":
            ssr = raw.get("studentStudyRecord") or {}
            progress = normalize_progress(ssr.get("speed")) if isinstance(ssr, dict) else 0.0
        else:
            completed = getattr(self, "_ai_completed", set())
            progress = 100.0 if str(raw.get("id") or "") in completed else 0.0
        ftype = str(raw.get("fileType") or "")
        return TaskNode(
            id=str(raw.get("id") or ""),
            name=str(raw.get("name") or ""),
            file_type=ftype,
            progress=progress,
            finished=progress >= 100.0,
            is_leaf=True,
            url=str(raw.get("fileUrl") or ""),
            parent_id=str(raw.get("parentId") or ""),
            backend=self.info.id,
            raw=dict(raw),
        )

    def _count_pending(
        self,
        session: Optional[LoginState],
        course: Course,
        domain: str,
    ) -> int:
        try:
            return len(self.list_unfinished_tasks(session=session, course=course))
        except ZhijiaoError as e:
            _log.warning("统计未完成任务失败：%s", e)
            return 0

    # ------------------------------------------------------------------ #
    # 额外能力：OAuth 本地回调登录（可选，无需 playwright）
    # ------------------------------------------------------------------ #
    def login_via_browser_callback(
        self,
        *,
        timeout: int = 300,
        preferred_port: Optional[int] = None,
    ) -> LoginState:
        """人工浏览器登录：起本地 HTTP 服务接收 SSO token 回调。

        调用上游 ``NewMoocMain/oauth_login.py`` 的 ``oauth_login()``。
        **不属于对外 7 个能力**，是统一登录态的第二条获取通道：

        * 相比 ICVE_Toolkit 的自动滑块：**不需要 playwright**，但需要人工在浏览器点一下；
        * 失败/超时抛 :class:`CredentialRejected`（不可降级）。

        :return: 含 ``sso_token`` 的统一 :class:`LoginState`
        """
        self._check_available()
        try:
            mod = self.loader.load(self.info.id, "NewMoocMain.oauth_login")
        except ZhijiaoError as e:
            raise BackendUnavailable(
                f"OAuth 登录模块加载失败：{e}",
                backend=self.info.id,
                details={"hint": "pip install -r requirements.txt（需要 lxml）"},
            ) from e

        with wrap_upstream(self.info.id, "oauth_login"):
            token = mod.oauth_login(timeout=timeout, preferred_port=preferred_port)

        if not token:
            raise CredentialRejected(
                "OAuth 回调登录未取得 SSO Token（超时或已取消）",
                backend=self.info.id,
                details={"timeout": timeout, "preferred_port": preferred_port},
            )
        _log.info("OAuth 回调登录成功，已取得 SSO Token（%s）", _mask(token))
        return LoginState(sso_token=str(token))

    # ------------------------------------------------------------------ #
    # 额外能力：独立 Edge 浏览器登录（推荐的人工通道）
    # ------------------------------------------------------------------ #
    #: Edge 独立 profile 的目录名（在项目内 state/ 下）
    EDGE_PROFILE_DIRNAME = "edge-profile"

    def login_via_edge_browser(
        self,
        *,
        timeout: int = 600,
        preferred_port: Optional[int] = None,
        headless: bool = False,
    ) -> LoginState:
        """**新起一个独立的 Edge** 完成人工登录，token 经本地回调取回。

        为什么要有这条通道：``login_via_browser_callback`` 用 ``webbrowser.open``，
        会把登录页塞进系统默认浏览器——如果那个浏览器正被别的东西占用/使用，
        就不方便（2026-09-17 用户实测：他的 Edge 正被另一个会话使用）。

        隔离保证（这是本方法存在的意义）：

        * 用 playwright 的 ``launch_persistent_context(channel="msedge")`` 起一个
          **全新的 Edge 进程**，``--user-data-dir`` 指向**项目内** ``state/edge-profile/``；
        * 独立 profile = 独立进程 = **与你正在运行的任何 Edge 互不相干**，
          既不读也不写它们的配置，更不会把标签页塞进去；
        * 全程**不调用** ``webbrowser.open``；
        * Edge 的缓存/配置全部落在项目内 ``state/edge-profile/``，不散到项目外。

        复用上游：本地回调服务器与 token 捕获**原样复用**上游
        ``NewMoocMain/oauth_login.py`` 的 ``OAuthLoginHandler``（零重写），
        只把"打开浏览器"这一步换成可控的 Edge。

        登录页是官方 SSO（``sso.icve.com.cn/sso/auth``），页面上官方提供什么方式
        （账号密码 / 短信 / **扫码**）都能用，拿到的都是同一个 SSO Token。
        """
        self._check_available()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise BackendUnavailable(
                "需要 playwright 才能驱动独立 Edge",
                backend=self.info.id,
                details={"hint": "pip install playwright（本项目 .venv 已装）"},
            ) from e

        try:
            mod = self.loader.load(self.info.id, "NewMoocMain.oauth_login")
        except ZhijiaoError as e:
            raise BackendUnavailable(
                f"OAuth 登录模块加载失败：{e}",
                backend=self.info.id,
                details={"hint": "pip install -r requirements.txt（需要 lxml）"},
            ) from e

        profile_dir = self.settings.state_dir / self.EDGE_PROFILE_DIRNAME
        profile_dir.mkdir(parents=True, exist_ok=True)

        handler = mod.OAuthLoginHandler(port=preferred_port)
        if not handler.start_server():
            raise UpstreamError(
                "本地回调服务启动失败（端口可能被占用）",
                backend=self.info.id,
            )
        login_url = (
            "https://sso.icve.com.cn/sso/auth?mode=simple&source=2"
            f"&redirect=http://localhost:{handler.port}/login"
        )
        _log.info("独立 Edge 已启动，请在窗口内完成登录（官方 SSO 页，支持扫码/短信）")
        print(f"  ↳ 登录页：{login_url}")

        token: Optional[str] = None
        try:
            with sync_playwright() as p:
                try:
                    ctx = p.chromium.launch_persistent_context(
                        user_data_dir=str(profile_dir),
                        channel="msedge",           # 系统安装的 Edge，非下载的 Chromium
                        headless=headless,
                        args=["--no-first-run", "--no-default-browser-check"],
                    )
                except Exception as e:
                    raise BackendUnavailable(
                        f"无法启动独立 Edge：{type(e).__name__}: {e}",
                        backend=self.info.id,
                        details={"hint": "确认本机装有 Microsoft Edge；profile 目录需可写"},
                    ) from e

                closed = {"flag": False}
                ctx.on("close", lambda: closed.update(flag=True))

                try:
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
                except Exception as e:
                    ctx.close()
                    raise UpstreamError(
                        f"打开 SSO 登录页失败：{type(e).__name__}: {e}",
                        backend=self.info.id,
                    ) from e

                deadline = time.monotonic() + timeout
                try:
                    while token is None:
                        token = getattr(handler, "token", None)
                        if token:
                            break
                        if closed["flag"]:
                            break                      # 用户手动关掉了窗口
                        if time.monotonic() > deadline:
                            break                      # 超时
                        time.sleep(0.5)
                finally:
                    try:
                        ctx.close()
                    except Exception:  # pragma: no cover
                        pass
        finally:
            try:
                handler.stop_server()
            except Exception:  # pragma: no cover
                pass

        if not token:
            raise CredentialRejected(
                "独立 Edge 登录未取得 SSO Token（超时 / 已关闭窗口 / 未完成登录）",
                backend=self.info.id,
                details={
                    "timeout": timeout,
                    "profile_dir": str(profile_dir),
                    "login_url": login_url,
                },
            )
        _log.info("独立 Edge 登录成功，已取得 SSO Token（%s）", _mask(token))
        return LoginState(sso_token=str(token))


# --------------------------------------------------------------------------- #
# 上游载荷解析助手
# --------------------------------------------------------------------------- #
def _rows(payload: Any) -> List[Dict[str, Any]]:
    """从上游 ``parse_response`` 的产物里取出列表。

    上游 ``BaseAPIClient.get`` 会先剥一层 ``data``，分页接口通常剩 ``{rows, total}``，
    但不同域/版本可能是 list / rows / records / list。这里做宽容提取。
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("rows", "records", "list", "items", "data"):
        v = payload.get(key)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
        if isinstance(v, dict):
            inner = _rows(v)
            if inner:
                return inner
    # 单对象且带课程字段 -> 当作只有一门课
    if any("course" in str(k).lower() for k in payload):
        return [payload]
    return []


def _as_list(payload: Any) -> List[Dict[str, Any]]:
    """课程树接口：上游直接返回 list，但宽容处理 ``{data/rows: [...]}``。"""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    return _rows(payload)


def _flatten_ids(payload: Any) -> set:
    """把 ``study_record_list`` 的产物摊平成 id 集合。

    上游该接口返回"已完成课件"的记录，形态在不同版本下有 list[dict] /
    list[str] / dict 三种可能，这里统一成 id 字符串集合。
    """
    ids: set = set()
    items = payload if isinstance(payload, list) else _rows(payload)
    for item in items:
        if isinstance(item, str):
            ids.add(item)
        elif isinstance(item, dict):
            for key in ("id", "sourceId", "coursewareId", "cellId", "itemId"):
                v = item.get(key)
                if v not in (None, ""):
                    ids.add(str(v))
    return ids


def _first_number(*values: Any) -> Optional[float]:
    for v in values:
        if v in (None, "", "-"):
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None

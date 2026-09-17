"""Adapter: ``ocsjs/ocsjs`` —— 浏览器端兜底（OCS，MIT）。

定位
----
OCS 是**浏览器用户脚本**（油猴 / 脚本猫），不是可 import 的 Python 库；
而且要 ``pnpm install && pnpm build`` 才能产出可用脚本。

因此本 Adapter **不实现任何抓取逻辑**，而是在所有 API 后端都不可用时，
产出一份 :class:`~zhijiao.contracts.BrowserHandoff` **交接计划**：

* 目标 URL（按课程类型选域名）
* 应启用的 OCS Project（``zjy`` 职教云 / ``icve`` 智慧职教）
* 场景（作业 / 考试 / 刷课 / 签到）
* 课程定位参数（``courseId`` / ``courseInfoId`` / ``classId``）
* 上游脚本构建产物路径（若已 ``pnpm build``）或官方分发地址
* 给使用者的操作步骤

为什么不做"Python 复刻 OCS 选择器"
---------------------------------
那才是真正的"重写上游"。OCS 的价值恰恰是**真浏览器里执行**——
能处理 iframe、跨域、播放器事件这些 API 路线难以覆盖的情形。
把它降级成 Python 复刻，既违反"不重写"，也丢掉了它唯一的优势。

许可
----
MIT（``Copyright (c) 2022 enncy``）—— 四个上游里唯一明确允许二次分发的。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..contracts import BrowserHandoff, Course, CourseType, normalize_course_type
from ..errors import ZhijiaoError
from ..log import get_logger
from ..session import LoginState
from .base import BackendInfo, BackendKind, BaseBackend, Capability
from .loader import UpstreamLoader

__all__ = ["OcsjsBackend"]

_log = get_logger("backend.ocsjs")

#: 统一 CourseType -> (域名, OCS Project 名)
#: Project 名取自上游 ``packages/scripts/src/projects/{zjy,icve}.ts`` 的 Project.create
_TARGET: Dict[CourseType, Dict[str, str]] = {
    CourseType.SPOC: {
        "url": "https://zjy2.icve.com.cn/",
        "project": "zjy",
        "project_name": "职教云",
        "domain": "zjy2.icve.com.cn",
    },
    CourseType.RESOURCE: {
        "url": "https://zyk.icve.com.cn/",
        "project": "zjy",
        "project_name": "职教云",
        "domain": "zyk.icve.com.cn",
    },
    CourseType.MOOC: {
        "url": "https://ai.icve.com.cn/",
        "project": "icve",
        "project_name": "智慧职教",
        "domain": "ai.icve.com.cn",
    },
    CourseType.UNKNOWN: {
        "url": "https://zjy2.icve.com.cn/",
        "project": "zjy",
        "project_name": "职教云",
        "domain": "zjy2.icve.com.cn",
    },
}

#: 能力 -> (场景标签, 一句话说明)
_SCENARIO: Dict[Capability, Dict[str, str]] = {
    Capability.LIST_COURSES: {
        "scenario": "course_list",
        "desc": "在浏览器里查看「我的课程」列表，OCS 面板会读取并展示课程与进度",
    },
    Capability.GET_COURSE_DETAIL: {
        "scenario": "course_structure",
        "desc": "打开课程学习页面，OCS 面板会解析章节与课件结构",
    },
    Capability.GET_COURSE_PROGRESS: {
        "scenario": "progress",
        "desc": "打开课程学习页面，OCS 面板上直接显示各章节完成进度",
    },
    Capability.LIST_UNFINISHED_TASKS: {
        "scenario": "unfinished",
        "desc": "打开课程学习页面并启用「未完成优先」，OCS 会跳过已完成项",
    },
    Capability.START_LEARNING: {
        "scenario": "study",
        "desc": "在真浏览器里播放视频 / 翻页课件，这是 OCS 最擅长的场景",
    },
    Capability.GET_ATTENDANCE: {
        "scenario": "sign",
        "desc": "打开课堂签到页面，OCS 面板显示签到状态并可执行签到",
    },
    Capability.GET_RESULTS: {
        "scenario": "exam",
        "desc": "打开作业 / 考试页面，OCS 会读取题目与成绩",
    },
}


class OcsjsBackend(BaseBackend):
    """OCS 浏览器兜底：产出交接计划，不抓数据。"""

    info = BackendInfo(
        id="ocsjs",
        name="OCS 网课助手（浏览器用户脚本）",
        kind=BackendKind.BROWSER,
        license="MIT License (c) 2022 enncy",
        upstream_dir="ocsjs",
        repo="https://github.com/ocsjs/ocsjs",
        capabilities=frozenset(Capability),
        credential_optional=True,
        notes="浏览器端兜底：输出 handoff 计划而非执行抓取",
    )

    def __init__(self, settings: Any = None, loader: Optional[UpstreamLoader] = None) -> None:
        super().__init__(settings)
        from .loader import get_loader

        self.loader = loader or get_loader(settings)

    # ------------------------------------------------------------------ #
    # 可用性：只要有配置开关打开就算可用（不需要凭据）
    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        try:
            return bool(self.settings.ocs.get("enabled", True)) and self._assets_root() is not None
        except ZhijiaoError:
            return False

    def unavailable_reason(self) -> str:
        if not self.settings.ocs.get("enabled", True):
            return "OCS 兜底已在配置中关闭（ocs.enabled=false）"
        if self._assets_root() is None:
            return "找不到 ocsjs 上游目录，无法生成交接计划"
        return ""

    def _assets_root(self):
        try:
            d = self.loader.upstream_dir(self.info.id)
        except ZhijiaoError:
            return None
        return d if d.is_dir() else None

    def check(self):
        c = super().check()
        root = self._assets_root()
        c.extra["upstream_commit"] = self.loader.commit(self.info.id) if root else ""
        c.extra["built_script"] = self._built_script() or ""
        c.extra["credential_required"] = False
        return c

    def _built_script(self) -> str:
        """探测已构建的 userscript（``pnpm build`` 的产物）。"""
        root = self._assets_root()
        if root is None:
            return ""
        configured = str(self.settings.ocs.get("script_path") or "")
        if configured:
            return configured
        import glob

        for pattern in ("dist/*.user.js", "packages/*/dist/*.user.js", "dist/**/*.user.js"):
            hits = glob.glob(str(root / pattern), recursive=True)
            if hits:
                return hits[0]
        return ""

    # ------------------------------------------------------------------ #
    # 七个能力：全部产出 handoff
    # ------------------------------------------------------------------ #
    def _handoff(
        self,
        capability: Capability,
        *,
        reason: str,
        course: Optional[Course] = None,
        course_type: Any = None,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> BrowserHandoff:
        ct = course.course_type if course else (
            normalize_course_type(course_type) if course_type else CourseType.UNKNOWN
        )
        target = _TARGET.get(ct, _TARGET[CourseType.UNKNOWN])
        meta = _SCENARIO[capability]

        params: Dict[str, Any] = {}
        if course is not None:
            params.update(
                {
                    "courseId": course.course_id,
                    "courseInfoId": course.course_info_id,
                    "classId": course.class_id,
                    "courseName": course.name,
                    "courseType": course.course_type.value,
                }
            )
        if extra_params:
            params.update(extra_params)

        built = self._built_script()
        script_url = str(self.settings.ocs.get("script_url") or "")

        steps = [
            "① 在浏览器安装用户脚本管理器（Tampermonkey / 脚本猫 ScriptCat）",
            (
                f"② 安装 OCS 脚本：{'本地已构建产物 ' + built if built else '从官方渠道获取（' + script_url + '）'}"
            ),
            f"③ 打开 {target['url']} 并登录智慧职教账号",
            f"④ 进入对应页面；OCS 面板会自动识别 Project「{target['project_name']}」",
            f"⑤ {meta['desc']}",
        ]
        if params.get("courseId"):
            steps.append(
                f"⑥ 定位课程：{params.get('courseName') or ''} "
                f"(courseId={params['courseId']}, courseInfoId={params.get('courseInfoId')})"
            )

        return BrowserHandoff(
            capability=capability.value,
            reason=reason,
            target_url=target["url"],
            ocs_projects=[target["project"]],
            scenario=meta["scenario"],
            params=params,
            script_path=built,
            script_url="" if built else script_url,
            steps=steps,
        )

    def _reason_for(self, capability: Capability) -> str:
        return (
            f"{self.info.name} 作为浏览器兜底接管 {capability.value}："
            "API 路线不可用（或该能力本地后端不支持），"
            "改由真浏览器中的 OCS 用户脚本执行"
        )

    # ①
    def list_courses(self, *, session: Optional[LoginState] = None, course_type: Any = None, **_: Any):
        return self._handoff(
            Capability.LIST_COURSES, reason=self._reason_for(Capability.LIST_COURSES),
            course_type=course_type,
        )

    # ②
    def get_course_detail(self, *, session=None, course: Any = None, **kw: Any):
        return self._handoff(
            Capability.GET_COURSE_DETAIL, reason=self._reason_for(Capability.GET_COURSE_DETAIL),
            course=course if isinstance(course, Course) else None,
            course_type=kw.get("course_type"),
        )

    # ③
    def get_course_progress(self, *, session=None, course: Any = None, **kw: Any):
        return self._handoff(
            Capability.GET_COURSE_PROGRESS, reason=self._reason_for(Capability.GET_COURSE_PROGRESS),
            course=course if isinstance(course, Course) else None,
            course_type=kw.get("course_type"),
        )

    # ④
    def list_unfinished_tasks(self, *, session=None, course: Any = None, **kw: Any):
        return self._handoff(
            Capability.LIST_UNFINISHED_TASKS,
            reason=self._reason_for(Capability.LIST_UNFINISHED_TASKS),
            course=course if isinstance(course, Course) else None,
            course_type=kw.get("course_type"),
        )

    # ⑤
    def start_learning(self, *, session=None, course: Any = None, mode: Any = None, **kw: Any):
        return self._handoff(
            Capability.START_LEARNING, reason=self._reason_for(Capability.START_LEARNING),
            course=course if isinstance(course, Course) else None,
            course_type=kw.get("course_type"),
            extra_params={"mode": getattr(mode, "value", mode)},
        )

    # ⑥
    def get_attendance(self, *, session=None, course: Any = None, **kw: Any):
        return self._handoff(
            Capability.GET_ATTENDANCE, reason=self._reason_for(Capability.GET_ATTENDANCE),
            course=course if isinstance(course, Course) else None,
            course_type=kw.get("course_type"),
        )

    # ⑦
    def get_results(self, *, session=None, course: Any = None, **kw: Any):
        return self._handoff(
            Capability.GET_RESULTS, reason=self._reason_for(Capability.GET_RESULTS),
            course=course if isinstance(course, Course) else None,
            course_type=kw.get("course_type"),
        )

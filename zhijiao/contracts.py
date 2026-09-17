"""统一数据格式（Unified Data Contracts）。

本模块定义本项目对外**唯一**的数据形状。所有 Backend 的原始返回都必须先
标准化成这里的 DTO，任何 Backend 的私有字段名都不允许泄漏到统一层之上。

设计要点
--------
* 纯 ``dataclasses`` + ``Enum``，无第三方依赖；
* 每个 DTO 都提供 ``to_dict()``，且默认**不外泄** ``raw``（上游原始响应），
  需要调试时用 ``to_dict(include_raw=True)``；
* 提供 ``normalize_*`` 系列函数，把三个上游千奇百怪的字段名收敛到统一枚举。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

__all__ = [
    "CourseType",
    "SignStatus",
    "LearningMode",
    "ExamKind",
    "Course",
    "TaskNode",
    "CourseDetail",
    "CourseProgress",
    "AttendanceRecord",
    "ExamResult",
    "LearningReport",
    "BrowserHandoff",
    "normalize_course_type",
    "normalize_sign_status",
    "normalize_learning_mode",
    "normalize_progress",
]


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #
class CourseType(str, Enum):
    """课程所属域。

    对应上游的三套域名：

    * ``SPOC``     -> ``zjy2.icve.com.cn`` 主域 SPOC 课程
    * ``MOOC``     -> ``ai.icve.com.cn``   AI 域 MOOC 课程
    * ``RESOURCE`` -> ``zyk.icve.com.cn``  资源库课程
    """

    SPOC = "SPOC"
    MOOC = "MOOC"
    RESOURCE = "RESOURCE"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def parse(cls, raw: Any) -> "CourseType":
        return normalize_course_type(raw)


class SignStatus(str, Enum):
    """签到状态。

    直接采用 ICVE 上游的状态码语义（``sign.py`` / README 均有记载）：

    ===== ========
    码    含义
    ===== ========
    0     未签到
    1     已签到
    2     迟到
    3     请假
    4     病假
    5     事假
    ===== ========
    """

    UNSIGNED = "UNSIGNED"
    SIGNED = "SIGNED"
    LATE = "LATE"
    LEAVE = "LEAVE"
    SICK = "SICK"
    ABSENT = "ABSENT"
    UNKNOWN = "UNKNOWN"

    @property
    def text(self) -> str:
        return {
            SignStatus.UNSIGNED: "未签到",
            SignStatus.SIGNED: "已签到",
            SignStatus.LATE: "迟到",
            SignStatus.LEAVE: "请假",
            SignStatus.SICK: "病假",
            SignStatus.ABSENT: "事假",
            SignStatus.UNKNOWN: "未知",
        }[self]

    @classmethod
    def parse(cls, raw: Any) -> "SignStatus":
        return normalize_sign_status(raw)


class LearningMode(str, Enum):
    """学习/刷课模式，映射到 ICVE 上游的 ``speed_type``。"""

    PROGRESS = "progress"      # 仅进度
    DISCUSSION = "discussion"  # 仅讨论
    ANSWER = "answer"          # 仅答题
    ALL = "all"                # 全部

    @classmethod
    def parse(cls, raw: Any) -> "LearningMode":
        return normalize_learning_mode(raw)


class ExamKind(str, Enum):
    """作业 / 考试 / 测验。"""

    HOMEWORK = "HOMEWORK"
    EXAM = "EXAM"
    QUIZ = "QUIZ"
    UNKNOWN = "UNKNOWN"


# --------------------------------------------------------------------------- #
# 标准化辅助
# --------------------------------------------------------------------------- #
_COURSE_TYPE_ALIASES: Dict[str, CourseType] = {
    # 上游 ICVE_Toolkit 的 _courseType
    "spoc": CourseType.SPOC,
    "mooc": CourseType.MOOC,
    "resource": CourseType.RESOURCE,
    "zyk": CourseType.RESOURCE,
    # 中文 / 其他上游写法
    "资源库": CourseType.RESOURCE,
    "职教云": CourseType.SPOC,
    "智慧职教": CourseType.MOOC,
    # 域名线索
    "zjy2": CourseType.SPOC,
    "zjy2.icve.com.cn": CourseType.SPOC,
    "ai": CourseType.MOOC,
    "ai.icve.com.cn": CourseType.MOOC,
    "zyk.icve.com.cn": CourseType.RESOURCE,
    # 数字型（资源库 flag / 课程类型码）
    "0": CourseType.SPOC,
    "1": CourseType.MOOC,
    "2": CourseType.RESOURCE,
    "3": CourseType.SPOC,
}

_SIGN_STATUS_ALIASES: Dict[str, SignStatus] = {
    "0": SignStatus.UNSIGNED,
    "1": SignStatus.SIGNED,
    "2": SignStatus.LATE,
    "3": SignStatus.LEAVE,
    "4": SignStatus.SICK,
    "5": SignStatus.ABSENT,
    "未签到": SignStatus.UNSIGNED,
    "已签到": SignStatus.SIGNED,
    "迟到": SignStatus.LATE,
    "请假": SignStatus.LEAVE,
    "病假": SignStatus.SICK,
    "事假": SignStatus.ABSENT,
    "unsigned": SignStatus.UNSIGNED,
    "signed": SignStatus.SIGNED,
    "late": SignStatus.LATE,
    "leave": SignStatus.LEAVE,
    "sick": SignStatus.SICK,
    "absent": SignStatus.ABSENT,
}

_LEARNING_MODE_ALIASES: Dict[str, LearningMode] = {
    "progress": LearningMode.PROGRESS,
    "讨论": LearningMode.DISCUSSION,
    "discussion": LearningMode.DISCUSSION,
    "答题": LearningMode.ANSWER,
    "answer": LearningMode.ANSWER,
    "all": LearningMode.ALL,
    "全部": LearningMode.ALL,
}


def _alias_lookup(raw: Any, table: Dict[str, Any], default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return default
    if isinstance(raw, str):
        key = raw.strip()
        hit = table.get(key)
        if hit is None:
            hit = table.get(key.lower())
        return hit if hit is not None else default
    if isinstance(raw, (int, float)):
        # 上游常把状态码当数字返回：``myStatus: 1`` / ``signResultType: 0``
        hit = table.get(str(raw))
        if hit is None:
            try:
                hit = table.get(str(int(raw)))
            except (TypeError, ValueError):
                hit = None
        return hit if hit is not None else default
    return default


def normalize_course_type(raw: Any) -> CourseType:
    """把任意上游的课程类型写法收敛到 :class:`CourseType`。"""
    if isinstance(raw, CourseType):
        return raw
    return _alias_lookup(raw, _COURSE_TYPE_ALIASES, CourseType.UNKNOWN)


def normalize_sign_status(raw: Any) -> SignStatus:
    """把任意上游的签到状态写法收敛到 :class:`SignStatus`。"""
    if isinstance(raw, SignStatus):
        return raw
    return _alias_lookup(raw, _SIGN_STATUS_ALIASES, SignStatus.UNKNOWN)


def normalize_learning_mode(raw: Any) -> LearningMode:
    """把任意上游的学习模式写法收敛到 :class:`LearningMode`。"""
    if isinstance(raw, LearningMode):
        return raw
    return _alias_lookup(raw, _LEARNING_MODE_ALIASES, LearningMode.PROGRESS)


def normalize_progress(raw: Any) -> float:
    """把上游的进度字段（``"100"`` / ``"100%"`` / ``100.0`` / ``None``）收敛为 0–100 的 float。

    ⚠️ **不做 0–1 归一化猜测**。上游（ICVE ``speed`` / mooc ``studentStudyRecord.speed``）
    一律是 0–100 的百分比；若把 ``speed=1`` 当作"归一化的 1.0"再乘 100，
    一门刚刚开始的课会被误判成 **100% 已完成**，进而被
    ``list_unfinished_tasks`` 跳过。宁可低估完成度，也不能谎报完成。
    越界值只做钳制。
    """
    if raw is None:
        return 0.0
    if isinstance(raw, bool):
        return 0.0
    if isinstance(raw, (int, float)):
        return _clamp(float(raw))
    if isinstance(raw, str):
        s = raw.strip().rstrip("%").strip()
        if not s:
            return 0.0
        try:
            return _clamp(float(s))
        except ValueError:
            return 0.0
    return 0.0


def _clamp(v: float) -> float:
    if v < 0:
        return 0.0
    if v > 100:
        return 100.0
    return v


# --------------------------------------------------------------------------- #
# DTO 基类
# --------------------------------------------------------------------------- #
class _Dto:
    """统一序列化基类。"""

    def to_dict(self, *, include_raw: bool = False) -> Dict[str, Any]:
        return _to_plain(self, include_raw=include_raw)

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return f"<{type(self).__name__} {self.to_dict()}>"


def _to_plain(obj: Any, *, include_raw: bool) -> Any:
    """递归把 dataclass / Enum / 容器转成 JSON 友好结构。"""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        out: Dict[str, Any] = {}
        for f in dataclasses.fields(obj):
            if f.name == "raw" and not include_raw:
                continue
            out[f.name] = _to_plain(getattr(obj, f.name), include_raw=include_raw)
        return out
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (list, tuple, set)):
        return [_to_plain(x, include_raw=include_raw) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _to_plain(v, include_raw=include_raw) for k, v in obj.items()}
    return obj


# --------------------------------------------------------------------------- #
# DTO 定义
# --------------------------------------------------------------------------- #
@dataclass
class Course(_Dto):
    """一门课程。三域统一表示。"""

    course_id: str
    course_info_id: str
    name: str
    course_type: CourseType = CourseType.UNKNOWN
    class_id: str = ""
    backend: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        # 让 DTO 自愈：无论哪个 Adapter 传来什么写法，落到这里都是统一枚举
        self.course_type = normalize_course_type(self.course_type)
        self.course_id = str(self.course_id or "")
        self.course_info_id = str(self.course_info_id or "")
        self.class_id = str(self.class_id or "")
        self.name = str(self.name or "")

    @property
    def key(self) -> str:
        """稳定去重键（与 ICVE 上游 ``get_my_courses`` 的 key 一致）。"""
        return f"{self.course_id}|{self.course_info_id}|{self.class_id}"


@dataclass
class TaskNode(_Dto):
    """课程树中的一个**叶子**课件单元（可学习的任务）。

    容器节点不单独建模，直接由 ``CourseDetail.nodes`` 的扁平列表承担。
    """

    id: str
    name: str
    file_type: str = ""
    progress: float = 0.0
    finished: bool = False
    is_leaf: bool = True
    url: str = ""
    duration_s: Optional[int] = None
    parent_id: str = ""
    is_container: bool = False
    children: List["TaskNode"] = field(default_factory=list)
    backend: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.progress = normalize_progress(self.progress)
        # 100% 必然意味着已完成；反向不强制改写 progress，避免谎报完成度
        self.finished = bool(self.finished) or self.progress >= 100.0
        self.id = str(self.id or "")
        self.name = str(self.name or "")
        self.file_type = str(self.file_type or "")

    def flatten(self, *, leaves_only: bool = False) -> List["TaskNode"]:
        """把树摊平。"""
        out: List[TaskNode] = []
        if not (leaves_only and self.is_container):
            out.append(self)
        for child in self.children:
            out.extend(child.flatten(leaves_only=leaves_only))
        return out


@dataclass
class CourseDetail(_Dto):
    """课程详情：课程元信息 + 扁平化任务节点 + 统计。"""

    course: Course
    nodes: List[TaskNode] = field(default_factory=list)
    backend: str = ""

    @property
    def total(self) -> int:
        return len(self.nodes)

    @property
    def finished_count(self) -> int:
        return sum(1 for n in self.nodes if n.finished)

    def to_dict(self, *, include_raw: bool = False) -> Dict[str, Any]:
        return {
            "course": self.course.to_dict(include_raw=include_raw),
            "nodes": [n.to_dict(include_raw=include_raw) for n in self.nodes],
            "total": self.total,
            "finished": self.finished_count,
            "backend": self.backend,
        }


@dataclass
class CourseProgress(_Dto):
    """课程进度聚合。"""

    course: Course
    total: int = 0
    finished: int = 0
    percent: float = 0.0
    by_file_type: Dict[str, Dict[str, float]] = field(default_factory=dict)
    backend: str = ""

    @classmethod
    def from_nodes(cls, course: Course, nodes: List[TaskNode], backend: str = "") -> "CourseProgress":
        total = len(nodes)
        finished = sum(1 for n in nodes if n.finished)
        buckets: Dict[str, Dict[str, float]] = {}
        for n in nodes:
            key = n.file_type or "(空)"
            b = buckets.setdefault(key, {"total": 0, "finished": 0})
            b["total"] += 1
            if n.finished:
                b["finished"] += 1
        return cls(
            course=course,
            total=total,
            finished=finished,
            percent=round(finished * 100.0 / total, 2) if total else 0.0,
            by_file_type=buckets,
            backend=backend,
        )

    def to_dict(self, *, include_raw: bool = False) -> Dict[str, Any]:
        return {
            "course": self.course.to_dict(include_raw=include_raw),
            "total": self.total,
            "finished": self.finished,
            "percent": self.percent,
            "by_file_type": self.by_file_type,
            "backend": self.backend,
        }


@dataclass
class AttendanceRecord(_Dto):
    """一条签到（考勤）记录。"""

    sign_id: str
    course_id: str = ""
    title: str = ""
    sign_type: str = ""          # 普通签到 / 手势签到 / 二维码签到
    status: SignStatus = SignStatus.UNKNOWN
    time: str = ""
    class_id: str = ""
    course_info_id: str = ""
    backend: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.status = normalize_sign_status(self.status)
        self.sign_id = str(self.sign_id or "")

    @property
    def status_text(self) -> str:
        return self.status.text


@dataclass
class ExamResult(_Dto):
    """一次作业 / 考试 / 测验的成绩。"""

    exam_id: str
    title: str = ""
    kind: ExamKind = ExamKind.UNKNOWN
    score: Optional[float] = None
    submitted: bool = False
    status: str = ""
    course_id: str = ""
    backend: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.exam_id = str(self.exam_id or "")
        self.title = str(self.title or "")

    @property
    def score_text(self) -> str:
        return "-" if self.score is None else (str(int(self.score)) if float(self.score).is_integer() else str(self.score))


@dataclass
class LearningReport(_Dto):
    """一次 start_learning 的执行结果。"""

    course: Course
    mode: LearningMode = LearningMode.PROGRESS
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    elapsed_s: float = 0.0
    errors: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)
    backend: str = ""

    def __post_init__(self) -> None:
        # Adapter 可能直接把调用方的字符串 mode 透传进来
        self.mode = normalize_learning_mode(self.mode)

    def to_dict(self, *, include_raw: bool = False) -> Dict[str, Any]:
        return {
            "course": self.course.to_dict(include_raw=include_raw),
            "mode": self.mode.value,
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "elapsed_s": round(self.elapsed_s, 2),
            "errors": list(self.errors),
            "details": self.details,
            "backend": self.backend,
        }


@dataclass
class BrowserHandoff(_Dto):
    """浏览器端兜底计划。

    当所有 API Backend 都不可用时，OCS Adapter 不"实现"抓取，
    而是产出这份计划，交由使用者在浏览器中用 OCS 用户脚本执行。
    """

    capability: str
    reason: str
    target_url: str = ""
    ocs_projects: List[str] = field(default_factory=list)
    scenario: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    script_path: str = ""
    script_url: str = ""
    steps: List[str] = field(default_factory=list)
    backend: str = "ocsjs"

    def to_dict(self, *, include_raw: bool = False) -> Dict[str, Any]:
        return {
            "capability": self.capability,
            "reason": self.reason,
            "target_url": self.target_url,
            "ocs_projects": list(self.ocs_projects),
            "scenario": self.scenario,
            "params": dict(self.params),
            "script_path": self.script_path,
            "script_url": self.script_url,
            "steps": list(self.steps),
            "backend": self.backend,
        }

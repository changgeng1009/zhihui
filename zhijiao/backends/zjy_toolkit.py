"""Adapter: ``atvkh/ZJY-Toolkit`` —— 远端占位后端（刻意不可用）。

事实核查（见 ``docs/UPSTREAM_ANALYSIS.md`` §2）
-----------------------------------------------

.. code-block:: text

    $ git ls-files      # 仓库里只有 1 个文件
    README.md
    $ git branch -r
    origin/main

``ZJY-Toolkit`` **不含任何源码**。它是一个部署在 ``https://study.atvkh.xyz`` 的
托管 SaaS（后端 Python + FastAPI、前端 Vanilla JS、以 CDK 激活码授权）。

因此它**物理上不可能**被本地加载为 Backend。本类存在的价值有三个：

1. **保留升级插槽**：若作者开放 API 或使用者获得授权自建部署，
   只需在本类里实现 HTTP 调用并把 ``available`` 改为 True，路由表无需改动；
2. **能力对照表**：它在 README 里声明的能力（尤其「24H 云端签到监听」
   「出勤统计」「成绩查询」）恰好指出了本地三个后端的能力缺口，
   路由表把它排在 ``get_attendance`` 的第二位就是为了这个补位意图；
3. **透明降级**：路由轨迹里会明确记录
   ``zjy_toolkit: skipped (远端未授权/未实现)``，
   而不是静默消失——使用者一眼能看到"这里本来还有一个后端"。

⚠️ **不逆向其私有接口**：调用需 CDK 授权且无公开 API 契约，
擅自逆向会引入不稳定的第三方依赖与合规风险，本项目明确不做。
"""

from __future__ import annotations

from typing import Any, Optional

from ..contracts import CourseType
from ..errors import BackendUnavailable
from ..log import get_logger
from .base import BackendInfo, BackendKind, BaseBackend, Capability
from .loader import UpstreamLoader

__all__ = ["ZjyToolkitBackend"]

_log = get_logger("backend.zjy_toolkit")

_UNAVAILABLE_REASON = (
    "ZJY-Toolkit 仓库仅含 README.md（无源码），实为托管服务 study.atvkh.xyz，"
    "需 CDK 授权且无公开 API 契约 —— 作为远端插槽占用，不参与本地调用"
)


class ZjyToolkitBackend(BaseBackend):
    """远端占位后端：永远 ``available=False``。"""

    info = BackendInfo(
        id="zjy_toolkit",
        name="ZJY-Toolkit / StudySpace（学无止境云空间）",
        kind=BackendKind.REMOTE,
        license="无 LICENSE 文件（README 标注 Educational）",
        upstream_dir="ZJY-Toolkit",
        repo="https://github.com/atvkh/ZJY-Toolkit",
        # 声明"若接入后能干什么"，路由表据此把它放进 get_attendance 的第二位；
        # 但 available=False，所以实际永远不会被真正调用。
        capabilities=frozenset(
            {
                Capability.LIST_COURSES,
                Capability.GET_COURSE_PROGRESS,
                Capability.START_LEARNING,
                Capability.GET_ATTENDANCE,
                Capability.GET_RESULTS,
            }
        ),
        notes="远端 SaaS 占位（无源码）；能力声明用于对照与未来接入",
    )

    def __init__(self, settings: Any = None, loader: Optional[UpstreamLoader] = None) -> None:
        super().__init__(settings)
        from .loader import get_loader

        self.loader = loader or get_loader(settings)

    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        return False

    def unavailable_reason(self) -> str:
        return _UNAVAILABLE_REASON

    def check(self):
        c = super().check()
        c.extra["has_source"] = False
        c.extra["endpoint"] = "https://study.atvkh.xyz"
        return c

    def call(self, capability, *, session=None, **kwargs):  # type: ignore[override]
        # 统一在这里拦下，保证任何能力都给出同一个明确原因，
        # 而不会因为"方法没实现"而报出容易误判的 CapabilityNotSupported。
        raise BackendUnavailable(
            _UNAVAILABLE_REASON,
            backend=self.info.id,
            details={
                "endpoint": "https://study.atvkh.xyz",
                "has_source": False,
                "hint": "如需启用：获得作者授权或自建部署后，在 zjy_toolkit.py 中实现 HTTP 调用并将 available 置为 True",
            },
        )

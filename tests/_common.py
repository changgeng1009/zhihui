"""测试公共设施：环境隔离、上游完整性清单、假 Backend。

设计原则
--------
* **全离线**：不访问真实账号、不向线上接口发请求。
* **不污染仓库**：state / log 全部落在临时目录；上游目录只读。
* **可重复**：每次 ``bootstrap()`` 都清空配置/加载器/Backend/路由缓存。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional

ROOT = Path(__file__).resolve().parent.parent
UPSTREAMS = ROOT / "upstreams"
MANIFEST = Path(__file__).resolve().parent / "upstream_manifest.json"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 四个上游的目录名（与 zhijiao/config.py 的默认 upstream_dirs 一致）
UPSTREAM_IDS = ("icve_toolkit", "zjy_toolkit", "mooc_work_answer", "ocsjs")
UPSTREAM_DIRS = {
    "icve_toolkit": "ICVE_Toolkit",
    "zjy_toolkit": "ZJY-Toolkit",
    "mooc_work_answer": "mooc-work-answer",
    "ocsjs": "ocsjs",
}

_STATE_DIR: Optional[Path] = None


# --------------------------------------------------------------------------- #
# 环境
# --------------------------------------------------------------------------- #
def state_dir() -> Path:
    global _STATE_DIR
    if _STATE_DIR is None:
        _STATE_DIR = Path(tempfile.mkdtemp(prefix="zhijiao-test-"))
    return _STATE_DIR


def bootstrap(*, log_level: str = "WARNING"):
    """配置测试环境并清空进程级缓存，返回 Settings。"""
    import zhijiao
    from zhijiao.config import get_settings

    os.environ["ZJ_UPSTREAM_ROOT"] = str(UPSTREAMS)
    os.environ["ZJ_STATE_DIR"] = str(state_dir())
    os.environ["ZJ_LOG_DIR"] = str(state_dir() / "logs")
    os.environ["ZJ_LOG_LEVEL"] = log_level
    os.environ["ZJ_LOG_JSONL"] = "false"
    os.environ["ZJ_ALLOW_FALLBACK"] = "true"
    # 绝不能因为开发机上有真实凭据而让测试"看起来通过"
    for k in ("ZJ_SSO_TOKEN", "ZJ_TOKEN", "ZJ_USERNAME"):
        os.environ.pop(k, None)

    zhijiao.reset_all()
    st = get_settings()
    st.ensure_dirs()
    return st


def fake_session(token: str = "test-token"):
    from zhijiao.session import LoginState

    return LoginState(sso_token=token, username="tester", nick_name="测试用户", stu_id="10001")


# --------------------------------------------------------------------------- #
# 上游完整性清单
# --------------------------------------------------------------------------- #
def compute_manifest() -> Dict[str, Any]:
    """对 ``upstreams/`` 下所有非 `.git` 文件做 sha256 快照。"""
    out: Dict[str, Any] = {}
    for bid, dirname in UPSTREAM_DIRS.items():
        base = UPSTREAMS / dirname
        files: Dict[str, str] = {}
        if base.is_dir():
            for p in sorted(base.rglob("*")):
                if not p.is_file():
                    continue
                rel = p.relative_to(base).as_posix()
                if rel.startswith(".git/") or rel == ".git":
                    continue
                files[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        out[bid] = {"dir": dirname, "count": len(files), "files": files}
    return out


def write_manifest() -> Path:
    MANIFEST.write_text(
        json.dumps(compute_manifest(), ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8",
    )
    return MANIFEST


def load_manifest() -> Dict[str, Any]:
    if not MANIFEST.is_file():
        return {}
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def diff_manifest() -> Dict[str, Any]:
    """对比当前上游与基线清单，返回差异（空 dict 表示完全未改动）。"""
    base = load_manifest()
    now = compute_manifest()
    diff: Dict[str, Any] = {"missing_upstream": [], "added": {}, "removed": {}, "modified": {}}
    if not base:
        diff["missing_upstream"].append("(无基线清单)")
        return diff

    for bid, dirname in UPSTREAM_DIRS.items():
        if not (UPSTREAMS / dirname).is_dir():
            diff["missing_upstream"].append(bid)
            continue
        b = set(base.get(bid, {}).get("files", {}))
        n = set(now.get(bid, {}).get("files", {}))
        bmap = base.get(bid, {}).get("files", {})
        nmap = now.get(bid, {}).get("files", {})
        if n - b:
            diff["added"][bid] = sorted(n - b)
        if b - n:
            diff["removed"][bid] = sorted(b - n)
        changed = sorted(r for r in (b & n) if bmap[r] != nmap[r])
        if changed:
            diff["modified"][bid] = changed
    return diff


def diff_is_empty(diff: Dict[str, Any]) -> bool:
    return not (
        diff.get("missing_upstream")
        or diff.get("added")
        or diff.get("removed")
        or diff.get("modified")
    )


def diff_summary(diff: Dict[str, Any]) -> str:
    parts: List[str] = []
    if diff.get("missing_upstream"):
        parts.append(f"缺失上游: {diff['missing_upstream']}")
    for kind in ("added", "removed", "modified"):
        for bid, files in (diff.get(kind) or {}).items():
            shown = ", ".join(files[:5]) + ("..." if len(files) > 5 else "")
            parts.append(f"{kind}[{bid}]: {shown}")
    return "; ".join(parts) or "(无差异)"


# --------------------------------------------------------------------------- #
# 假 Backend
# --------------------------------------------------------------------------- #
def make_fake_backend(
    backend_id: str,
    *,
    capabilities: Optional[FrozenSet[Any]] = None,
    available: bool = True,
    reason: str = "",
    result: Any = None,
    error: Optional[BaseException] = None,
    calls: Optional[List[Dict[str, Any]]] = None,
):
    """构造一个可编程的假 Backend，用于验证路由决策。"""
    from zhijiao.backends import ALL_CAPABILITIES, BackendInfo, BackendKind, BaseBackend

    caps = capabilities if capabilities is not None else ALL_CAPABILITIES

    class _Fake(BaseBackend):
        info = BackendInfo(
            id=backend_id,
            name=f"Fake {backend_id}",
            kind=BackendKind.API,
            license="test",
            upstream_dir=backend_id,
            capabilities=frozenset(caps),
        )

        @property
        def available(self) -> bool:
            return available

        def unavailable_reason(self) -> str:
            return reason or "fake unavailable"

        def call(self, capability, *, session=None, **kwargs):
            if calls is not None:
                calls.append({"backend": backend_id, "capability": capability.value, **kwargs})
            if error is not None:
                raise error
            if callable(result):
                return result(capability, session=session, **kwargs)
            return result

    return _Fake()


def router_with(*backends, routing_table=None):
    """用假 Backend 构造 Router。

    默认按传入顺序，为**每个能力**都生成一条覆盖全部假 Backend 的路由链 ——
    这样测试断言的是"优先级 + 降级"机制本身，而不是真实后端的能力差异。
    """
    from zhijiao.backends import Capability
    from zhijiao.router import Router

    st = bootstrap()
    table = routing_table
    if table is None:
        ids = [b.info.id for b in backends]
        table = {cap: list(ids) for cap in Capability}
    return Router(
        settings=st,
        backends={b.info.id: b for b in backends},
        routing_table=table,
    )

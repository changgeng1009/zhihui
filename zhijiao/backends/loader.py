"""上游隔离加载器（Upstream Loader）。

上游模块都是"顶层模块式导入"（ICVE 的 ``from utils import log``、
mooc 的 ``from MoocMain.log import Logger``），所以必须把**上游根目录**放进 ``sys.path``
才能 import。

本加载器的纪律
--------------
* 只做两件事：把上游目录插入 ``sys.path``；用 ``importlib`` 导入并缓存模块。
* **绝不**修改上游文件、绝不 monkey-patch 上游函数。
* 模块只导入一次（缓存），重复调用零成本。
* 检测跨上游的同名顶层模块冲突，冲突时抛出带明确指引的 :class:`ConfigError`。
* 导入期间把工作目录切到日志目录，接住上游"import 时在 cwd 建日志文件"的副作用
  （如 mooc-work-answer 的 ``MoocMain/log.py``）。这样仓库根目录保持干净，
  且**依然没有改动上游代码**。
"""

from __future__ import annotations

import importlib
import os
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..config import Settings, get_settings
from ..errors import BackendUnavailable, ConfigError
from ..log import get_logger

__all__ = ["UpstreamLoader", "get_loader"]

_log = get_logger("loader")

#: 进程级锁：``sys.path`` 与 cwd 都是进程全局状态
_LOCK = threading.RLock()

#: module 名 -> 来源上游目录（用于冲突检测）
_MODULE_ORIGIN: Dict[str, str] = {}


class UpstreamLoader:
    """按 Backend id 加载其上游模块。"""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self._modules: Dict[Tuple[str, str], Any] = {}

    # ------------------------------------------------------------------ #
    # 路径
    # ------------------------------------------------------------------ #
    def upstream_dir(self, backend_id: str) -> Path:
        """Backend id -> 上游目录（可能不存在，由调用方判断）。"""
        return self.settings.upstream_path(backend_id)

    def require_dir(self, backend_id: str) -> Path:
        """上游目录必须存在，否则抛可降级的 :class:`BackendUnavailable`。"""
        d = self.upstream_dir(backend_id)
        if not d.is_dir():
            raise BackendUnavailable(
                f"上游目录不存在：{d}",
                backend=backend_id,
                details={"hint": f"git clone <repo> {d}"},
            )
        return d

    def commit(self, backend_id: str) -> str:
        """读取上游当前 commit（纯 Python 解析 ``.git``，不依赖 git 可执行文件）。"""
        try:
            return _read_git_head(self.upstream_dir(backend_id))
        except OSError:  # pragma: no cover
            return ""

    # ------------------------------------------------------------------ #
    # 导入
    # ------------------------------------------------------------------ #
    def load(self, backend_id: str, module_name: str) -> Any:
        """导入 ``<upstream_dir>/<module_name>``。

        :raises BackendUnavailable: 上游目录/模块文件不存在，或依赖缺失
        """
        key = (backend_id, module_name)
        if key in self._modules:
            return self._modules[key]

        upstream_dir = self.require_dir(backend_id)

        # 顶层模块 -> 需要文件存在性检查，给报错更好的提示
        if "." not in module_name:
            candidate = upstream_dir / f"{module_name}.py"
            pkg_candidate = upstream_dir / module_name / "__init__.py"
            if not candidate.is_file() and not pkg_candidate.is_dir():
                raise BackendUnavailable(
                    f"上游 {backend_id} 中找不到模块 {module_name!r}",
                    backend=backend_id,
                    details={"dir": str(upstream_dir)},
                )

        with _LOCK:
            _conflict = _check_conflict(module_name, upstream_dir)
            if _conflict:
                raise ConfigError(
                    f"模块名冲突：{module_name!r} 已被 {_conflict} 占用，"
                    f"无法再为 {upstream_dir} 加载。",
                    details={
                        "module": module_name,
                        "owner": _conflict,
                        "requested_by": str(upstream_dir),
                        "hint": "给该上游 Adapter 改成带包名的导入路径，或隔离到独立进程",
                    },
                )

            added = _ensure_sys_path(upstream_dir)
            try:
                with _workdir(self.settings.log_dir if self.settings.log_jsonl else self.settings.state_dir):
                    module = importlib.import_module(module_name)
            except ImportError as e:
                raise BackendUnavailable(
                    f"导入 {backend_id}.{module_name} 缺少依赖：{e}",
                    backend=backend_id,
                    details={"module": module_name, "hint": "见 requirements.txt"},
                    cause=e,
                ) from e
            # 注意：**不移除** sys.path 条目。
            # 上游存在函数内懒导入（如 speed_course.py: `from answer import ...`），
            # 只有路径常驻才能让这些调用在运行期成功。
            # 若本次是"重新插入到最前"，记录一下便于诊断。
            if not added:
                _log.debug("上游路径已在 sys.path（已前置）: %s", upstream_dir)

            _MODULE_ORIGIN.setdefault(module_name, str(upstream_dir))
            self._modules[key] = module
            _log.debug("已加载上游模块 %s.%s <- %s", backend_id, module_name, upstream_dir)
            return module

    def load_many(self, backend_id: str, module_names: List[str]) -> Dict[str, Any]:
        return {name: self.load(backend_id, name) for name in module_names}

    def loaded_modules(self) -> List[str]:
        return sorted(f"{bid}:{mod}" for bid, mod in self._modules)

    def reset(self) -> None:
        """仅清缓存（不卸载 sys.modules），测试用。"""
        self._modules.clear()


# --------------------------------------------------------------------------- #
# sys.path 帮助函数
# --------------------------------------------------------------------------- #
def _ensure_sys_path(path: Path) -> bool:
    """把 ``path`` 插到 ``sys.path`` 最前，返回是否为本次新增。"""
    s = str(path)
    if s in sys.path:
        sys.path.remove(s)
        sys.path.insert(0, s)
        return False
    sys.path.insert(0, s)
    return True


def _remove_sys_path(path: Path) -> None:
    s = str(path)
    while s in sys.path:
        sys.path.remove(s)


def _check_conflict(module_name: str, wanted_dir: Path) -> Optional[str]:
    """若同名的**顶层模块**已被另一个上游目录提供，返回那个目录。"""
    origin = _MODULE_ORIGIN.get(module_name)
    if origin and Path(origin) != wanted_dir:
        return origin

    existing = sys.modules.get(module_name)
    if existing is None:
        return None
    exist_file = getattr(existing, "__file__", None)
    if not exist_file:
        return None
    try:
        exist_path = Path(exist_file).resolve()
    except OSError:  # pragma: no cover
        return None
    # 同目录下的同一个模块（重复调用）不算冲突
    try:
        exist_path.relative_to(wanted_dir.resolve())
        return None
    except ValueError:
        pass
    # 标准库/已安装包也不算冲突
    if _is_stdlib_or_site(exist_path):
        return None
    return str(exist_path.parent)


def _is_stdlib_or_site(path: Path) -> bool:
    parts = {p.lower() for p in path.parts}
    if "site-packages" in parts or "lib" in parts and "python3" in str(path).lower():
        return True
    try:
        import sysconfig

        stdlib = Path(sysconfig.get_paths().get("stdlib", "")).resolve()
        path.relative_to(stdlib)
        return True
    except (ValueError, OSError):
        return False


@contextmanager
def _workdir(path: Path) -> Iterator[None]:
    """临时切换 cwd（进程全局，用锁保护）。"""
    with _LOCK:
        old = os.getcwd()
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.chdir(path)
        except OSError:  # pragma: no cover - 切不过去就用原 cwd
            yield
            return
        try:
            yield
        finally:
            try:
                os.chdir(old)
            except OSError:  # pragma: no cover
                pass


def _read_git_head(repo: Path) -> str:
    """纯 Python 读取 git HEAD commit。"""
    git = repo / ".git"
    if git.is_file():
        # worktree/submodule：.git 是文件，形如 "gitdir: /path"
        try:
            content = git.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        if not content.startswith("gitdir:"):
            return ""
        git = Path(content.split(":", 1)[1].strip())
    head = git / "HEAD"
    if not head.is_file():
        return ""
    try:
        content = head.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not content.startswith("ref:"):
        return content
    ref = content.split(":", 1)[1].strip()
    ref_file = git / ref
    if ref_file.is_file():
        try:
            return ref_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    packed = git / "packed-refs"
    if packed.is_file():
        try:
            for line in packed.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("^"):
                    continue
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0]
        except OSError:
            return ""
    return ""


_LOADER: Optional[UpstreamLoader] = None


def get_loader(settings: Optional[Settings] = None) -> UpstreamLoader:
    """取得进程级共享加载器。"""
    global _LOADER
    if _LOADER is None or (settings is not None and settings is not _LOADER.settings):
        _LOADER = UpstreamLoader(settings)
    return _LOADER


def reset_loader() -> None:
    """丢弃进程级加载器（测试用）。

    注意：**不会**从 ``sys.modules`` 卸载已导入的上游模块 ——
    卸载会让上游的函数内懒导入失效，也会让 ``_MODULE_ORIGIN`` 的冲突检测失去意义。
    """
    global _LOADER
    _LOADER = None

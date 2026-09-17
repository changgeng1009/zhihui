"""统一登录状态（Unified Login State）。

核心洞察（见 ``docs/UPSTREAM_ANALYSIS.md`` §1.3）
--------------------------------------------------
四个上游虽然各自实现登录，但**凭据源头是同一个**：

.. code-block:: text

    sso.icve.com.cn  ── /data/userLogin ──>  SSO Token（单一凭据源）
                            │
                            │  各域自取：GET {domain}/auth/passLogin?token=<SSO Token>
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
        zjy2 Bearer    ai X-AI-Token   zyk Bearer

因此统一层只需要持久化 **一个 SSO Token**，各 Adapter 用它构造自己的客户端并派生本域 Bearer。
这样"统一登录状态"不需要把四个项目的登录流程缝合成一个，而是**收敛到一个凭据**。

凭据来源优先级
--------------
1. 代码显式传入（``LoginState(...)``）
2. 环境变量：``ZJ_SSO_TOKEN`` / ``ZJ_TOKEN`` / ``ZJ_USERNAME``
3. 落盘文件：``<state_dir>/session.json``
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from .config import Settings, get_settings
from .errors import AuthError
from .log import get_logger, mask

__all__ = ["LoginState", "SessionStore", "load_state", "require_state"]

_log = get_logger("session")

_SESSION_FILE = "session.json"


@dataclass
class LoginState:
    """一次登录会话的凭据与身份信息。

    ``sso_token`` 是主凭据；``token``（zjy2 域 Bearer）是可选加速项，
    有则免一次 ``passLogin``。**默认不保存明文密码。**
    """

    sso_token: str = ""
    token: str = ""
    username: str = ""
    nick_name: str = ""
    stu_id: str = ""
    school: str = ""
    obtained_at: float = field(default_factory=time.time)
    #: 各域派生出的 Bearer 缓存（运行期用，不进落盘文件）
    domain_tokens: Dict[str, str] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ #
    @property
    def is_valid(self) -> bool:
        """是否具备至少一个可用凭据。"""
        return bool(self.sso_token or self.token)

    @property
    def credential(self) -> str:
        """交给上游 ``passLogin`` 用的凭据（优先 SSO Token）。"""
        return self.sso_token or self.token

    def require(self) -> "LoginState":
        """没有凭据时抛 :class:`AuthError`。"""
        if not self.is_valid:
            raise AuthError(
                "未登录：请先提供 SSO Token（ZJ_SSO_TOKEN / state/session.json）",
                details={"hint": "见 README「登录」一节"},
            )
        return self

    def masked(self) -> Dict[str, Any]:
        """用于日志的安全视图。"""
        return {
            "username": self.username,
            "nick_name": self.nick_name,
            "stu_id": self.stu_id,
            "school": self.school,
            "sso_token": mask(self.sso_token),
            "token": mask(self.token),
            "obtained_at": self.obtained_at,
        }

    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        return {
            "sso_token": self.sso_token,
            "token": self.token,
            "username": self.username,
            "nick_name": self.nick_name,
            "stu_id": self.stu_id,
            "school": self.school,
            "obtained_at": self.obtained_at,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "LoginState":
        return cls(
            sso_token=str(raw.get("sso_token") or ""),
            token=str(raw.get("token") or ""),
            username=str(raw.get("username") or ""),
            nick_name=str(raw.get("nick_name") or ""),
            stu_id=str(raw.get("stu_id") or ""),
            school=str(raw.get("school") or ""),
            obtained_at=float(raw.get("obtained_at") or time.time()),
        )

    @classmethod
    def from_env(cls, environ: Optional[Dict[str, str]] = None) -> Optional["LoginState"]:
        env = environ if environ is not None else os.environ
        sso = env.get("ZJ_SSO_TOKEN", "").strip()
        tok = env.get("ZJ_TOKEN", "").strip()
        if not (sso or tok):
            return None
        return cls(
            sso_token=sso,
            token=tok,
            username=env.get("ZJ_USERNAME", "").strip(),
        )


class SessionStore:
    """凭据的落盘存储（``<state_dir>/session.json``，权限 0600）。"""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self.path: Path = self.settings.state_dir / _SESSION_FILE

    # ------------------------------------------------------------------ #
    def load(self) -> Optional[LoginState]:
        """从磁盘读取登录态；不存在或损坏返回 ``None``。"""
        if not self.path.is_file():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            _log.warning("读取 %s 失败：%s", self.path, e)
            return None
        if not isinstance(raw, dict):
            return None
        state = LoginState.from_dict(raw)
        return state if state.is_valid else None

    def save(self, state: LoginState) -> Path:
        """写入登录态。"""
        self.settings.ensure_dirs()
        self.path.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:  # POSIX only；Windows 忽略
            os.chmod(self.path, 0o600)
        except OSError:  # pragma: no cover
            pass
        _log.info("登录态已保存: %s (%s)", self.path, state.masked())
        return self.path

    def clear(self) -> bool:
        """删除落盘登录态。"""
        if self.path.is_file():
            self.path.unlink()
            _log.info("登录态已清除: %s", self.path)
            return True
        return False


def load_state(
    *,
    explicit: Optional[LoginState] = None,
    settings: Optional[Settings] = None,
) -> LoginState:
    """按优先级装载登录态：显式传入 > 环境变量 > 落盘文件。

    找不到任何凭据时返回**空** LoginState（不抛错），
    由具体 Tool 调用前通过 :meth:`LoginState.require` 决定是否强制。
    """
    if explicit is not None and explicit.is_valid:
        return explicit

    from_env = LoginState.from_env()
    if from_env is not None:
        _log.debug("登录态来源：环境变量 (%s)", from_env.masked())
        return from_env

    store = SessionStore(settings)
    from_file = store.load()
    if from_file is not None:
        _log.debug("登录态来源：%s (%s)", store.path, from_file.masked())
        return from_file

    return explicit or LoginState()


def require_state(
    *,
    explicit: Optional[LoginState] = None,
    settings: Optional[Settings] = None,
) -> LoginState:
    """同 :func:`load_state`，但无凭据时抛 :class:`AuthError`。"""
    return load_state(explicit=explicit, settings=settings).require()

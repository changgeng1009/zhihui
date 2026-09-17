"""阶段 6 测试：统一登录通道（账密自动滑块 / 浏览器回调）。

**全程离线**：所有会真的启动 Chromium 或打开浏览器的地方一律打桩，
只验证**通道选择、参数传递、错误归一化、登录态落盘**这些统一层自己的逻辑。
另有一条"运行时真可用"的测试（真的启动一次项目内 Chromium），
它不联网，只证明内核能跑起来 —— 缺内核时自动跳过。
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from typing import Any, Dict, List

from _common import bootstrap, fake_session

from zhijiao.api import LOGIN_CHANNELS, login, logout
from zhijiao.backends import build_backends
from zhijiao.config import get_settings
from zhijiao.errors import BackendUnavailable, ConfigError, CredentialRejected
from zhijiao.session import LoginState, SessionStore


class _Patch:
    """临时替换对象属性，退出时还原。"""

    def __init__(self, test: unittest.TestCase, obj: Any, name: str, value: Any):
        self.obj, self.name, self.value = obj, name, value
        self.had = hasattr(obj, name)
        self.old = getattr(obj, name, None)
        if not self.had:
            test.addCleanup(lambda: delattr(obj, name))
        else:
            test.addCleanup(setattr, obj, name, self.old)
        setattr(obj, name, value)


class TestLogoutAndStore(unittest.TestCase):
    """登录态持久化（不含任何网络）。"""

    @classmethod
    def setUpClass(cls):
        cls.st = bootstrap()

    def test_channels_declared(self):
        self.assertEqual(
            set(LOGIN_CHANNELS),
            {"password", "edge", "browser", "auto"},
            "四条登录通道：账密 / 独立Edge / 默认浏览器 / 自动降级",
        )

    def test_save_load_clear_roundtrip(self):
        store = SessionStore(self.st)
        store.clear()
        self.assertIsNone(store.load(), "清空后不应还能读到登录态")

        st = LoginState(sso_token="unit-test-sso", username="u1", nick_name="测试")
        path = store.save(st)
        self.assertTrue(path.is_file())

        got = store.load()
        self.assertIsNotNone(got)
        self.assertEqual(got.sso_token, "unit-test-sso")
        self.assertEqual(got.username, "u1")

        self.assertTrue(store.clear())
        self.assertIsNone(store.load())

    def test_stored_file_hides_nothing_sensitive_but_is_protected(self):
        """落盘只存 token 与身份，不含密码；且必须落在 state_dir 内。"""
        store = SessionStore(self.st)
        store.save(LoginState(sso_token="abc", username="u1"))
        content = store.path.read_text(encoding="utf-8")
        self.assertIn("sso_token", content)
        self.assertNotIn("password", content)
        self.assertEqual(store.path, self.st.state_dir / "session.json")

    def test_default_paths_all_live_inside_project(self):
        """不带任何 ZJ_* 覆盖时的默认路径，必须全部落在项目内。"""
        from zhijiao.config import PROJECT_ROOT, get_settings

        saved = os.environ.pop("ZJ_STATE_DIR", None)
        saved_log = os.environ.pop("ZJ_LOG_DIR", None)
        saved_br = os.environ.pop("ZJ_BROWSERS_DIR", None)
        try:
            st = get_settings(reload=True)
            root = str(PROJECT_ROOT)
            self.assertEqual(st.state_dir, PROJECT_ROOT / "state")
            self.assertEqual(st.log_dir, PROJECT_ROOT / "state" / "logs")
            self.assertEqual(st.browsers_dir, PROJECT_ROOT / "browsers")
            self.assertEqual(st.upstream_root, PROJECT_ROOT / "upstreams")
            for label, p in (
                ("state_dir", st.state_dir),
                ("log_dir", st.log_dir),
                ("browsers_dir", st.browsers_dir),
                ("upstream_root", st.upstream_root),
            ):
                with self.subTest(path=label):
                    self.assertTrue(
                        str(p).startswith(root), f"{label} 跑到了项目外：{p}"
                    )
        finally:
            for key, val in (("ZJ_STATE_DIR", saved), ("ZJ_LOG_DIR", saved_log),
                             ("ZJ_BROWSERS_DIR", saved_br)):
                if val is not None:
                    os.environ[key] = val
            get_settings(reload=True)

    def test_logout_helper(self):
        SessionStore(self.st).save(LoginState(sso_token="x"))
        self.assertTrue(logout(self.st))
        self.assertIsNone(SessionStore(self.st).load())


class TestLoginChannelDispatch(unittest.TestCase):
    """`login()` 的通道选择逻辑（后端方法全部打桩）。"""

    @classmethod
    def setUpClass(cls):
        cls.st = bootstrap()

    def setUp(self):
        bootstrap()
        self.backends = build_backends(force=True)
        SessionStore(self.st).clear()

    # ---- 入参校验 ---------------------------------------------------- #
    def test_unknown_channel_raises_config_error(self):
        with self.assertRaises(ConfigError) as ctx:
            login("u", "p", channel="sms")
        self.assertEqual(ctx.exception.code, "ZJ-1001")
        self.assertIn("browser", ctx.exception.details["available"])

    def test_password_channel_requires_credentials(self):
        with self.assertRaises(ConfigError) as ctx:
            login(channel="password")
        self.assertEqual(ctx.exception.code, "ZJ-1001")
        self.assertIn("username", ctx.exception.message)

    # ---- password 通道 ----------------------------------------------- #
    def test_password_channel_returns_and_persists(self):
        icve = self.backends["icve_toolkit"]
        seen: Dict[str, Any] = {}

        def fake_login(username, password, *, max_attempts=2, headless=False):
            seen.update(username=username, password=password, max_attempts=max_attempts)
            return LoginState(sso_token="tok-from-slider", username=username)

        _Patch(self, icve, "login_with_password", fake_login)
        st = login("学号", "密码", channel="password", settings=self.st, persist=True)

        self.assertEqual(st.sso_token, "tok-from-slider")
        self.assertEqual(seen["username"], "学号")
        self.assertEqual(seen["max_attempts"], 2)
        # 已落盘
        self.assertEqual(SessionStore(self.st).load().sso_token, "tok-from-slider")

    def test_password_channel_without_persist_writes_nothing(self):
        icve = self.backends["icve_toolkit"]
        _Patch(self, icve, "login_with_password",
               lambda u, p, **k: LoginState(sso_token="tok-x", username=u))
        login("u", "p", channel="password", settings=self.st, persist=False)
        self.assertIsNone(SessionStore(self.st).load())

    def test_password_channel_unavailable_raises_with_hint(self):
        icve = self.backends["icve_toolkit"]
        _Patch(self, icve, "login_with_password",
               lambda *a, **k: (_ for _ in ()).throw(BackendUnavailable("缺 playwright")))
        with self.assertRaises(BackendUnavailable) as ctx:
            login("u", "p", channel="password", settings=self.st)
        self.assertIn("browser", str(ctx.exception.details))

    def test_credential_rejected_is_not_swallowed(self):
        """账密被拒 → 不降级、不换通道（上游明确说明重试会触发风控）。"""
        icve = self.backends["icve_toolkit"]
        mwa = self.backends["mooc_work_answer"]
        browser_called: List[bool] = []

        _Patch(self, icve, "login_with_password",
               lambda *a, **k: (_ for _ in ()).throw(CredentialRejected("账密错误")))
        _Patch(self, mwa, "login_via_browser_callback",
               lambda **k: browser_called.append(True) or LoginState(sso_token="nope"))

        with self.assertRaises(CredentialRejected):
            login("u", "bad", channel="auto", settings=self.st)
        self.assertEqual(browser_called, [], "账密被拒后不应再去试浏览器通道")

    # ---- browser 通道 ------------------------------------------------ #
    def test_browser_channel(self):
        mwa = self.backends["mooc_work_answer"]
        seen: Dict[str, Any] = {}

        def fake_browser(*, timeout=300, preferred_port=None):
            seen.update(timeout=timeout, preferred_port=preferred_port)
            return LoginState(sso_token="tok-from-browser")

        _Patch(self, mwa, "login_via_browser_callback", fake_browser)
        st = login(channel="browser", timeout=42, preferred_port=18080,
                   settings=self.st, persist=False)
        self.assertEqual(st.sso_token, "tok-from-browser")
        self.assertEqual(seen["timeout"], 42)
        self.assertEqual(seen["preferred_port"], 18080)

    # ---- auto 通道 --------------------------------------------------- #
    def test_auto_prefers_password(self):
        icve = self.backends["icve_toolkit"]
        mwa = self.backends["mooc_work_answer"]
        used: List[str] = []

        _Patch(self, icve, "login_with_password",
               lambda u, p, **k: used.append("password") or LoginState(sso_token="tok-p"))
        _Patch(self, mwa, "login_via_browser_callback",
               lambda **k: used.append("browser") or LoginState(sso_token="tok-b"))

        st = login("u", "p", channel="auto", settings=self.st, persist=False)
        self.assertEqual(st.sso_token, "tok-p")
        self.assertEqual(used, ["password"], "auto 应优先账密，成功就不碰浏览器")

    def test_auto_falls_back_to_edge_when_deps_missing(self):
        """依赖缺失时 auto 应退到**独立 Edge**，而不是系统默认浏览器。

        ⚠️ 这里必须把 edge 方法也打桩 —— 否则 auto 会走真实
        ``login_via_edge_browser``，在测试机上真的弹出 Edge 窗口。
        """
        icve = self.backends["icve_toolkit"]
        mwa = self.backends["mooc_work_answer"]
        used: List[str] = []

        _Patch(self, icve, "login_with_password",
               lambda *a, **k: (_ for _ in ()).throw(BackendUnavailable("缺 playwright")))
        _Patch(self, mwa, "login_via_edge_browser",
               lambda **k: used.append("edge") or LoginState(sso_token="tok-edge"))
        _Patch(self, mwa, "login_via_browser_callback",
               lambda **k: used.append("browser") or LoginState(sso_token="tok-b"))

        st = login("u", "p", channel="auto", settings=self.st, persist=False)
        self.assertEqual(st.sso_token, "tok-edge")
        self.assertEqual(used, ["edge"], "auto 应退到独立 Edge，而不是系统默认浏览器")

    def test_auto_without_credentials_goes_edge(self):
        """未提供账密时 auto 走独立 Edge（必须打桩，否则测试会弹出真 Edge）。"""
        mwa = self.backends["mooc_work_answer"]
        _Patch(self, mwa, "login_via_edge_browser",
               lambda **k: LoginState(sso_token="tok-e"))
        st = login(channel="auto", settings=self.st, persist=False)
        self.assertEqual(st.sso_token, "tok-e")


class TestAdapterLoginMethods(unittest.TestCase):
    """两个 Adapter 的登录方法本体（上游函数打桩）。"""

    @classmethod
    def setUpClass(cls):
        cls.st = bootstrap()
        cls.backends = build_backends(force=True)

    def test_icve_password_login_returns_login_state(self):
        icve = self.backends["icve_toolkit"]
        slider = icve.loader.load("icve_toolkit", "slider_auto")
        seen: Dict[str, Any] = {}

        def fake_obtain(user, pwd, max_attempts=2, headless=False):
            seen.update(user=user, pwd=pwd, max_attempts=max_attempts, headless=headless)
            return "sso-from-slider"

        _Patch(self, slider, "missing_packages", lambda: [])
        _Patch(self, slider, "obtain_sso_token", fake_obtain)

        st = icve.login_with_password("stu001", "pw", max_attempts=1, headless=True)
        self.assertIsInstance(st, LoginState)
        self.assertEqual(st.sso_token, "sso-from-slider")
        self.assertEqual(st.username, "stu001")
        self.assertEqual(seen, {"user": "stu001", "pwd": "pw", "max_attempts": 1, "headless": True})

    def test_icve_password_login_missing_deps_gives_actionable_error(self):
        icve = self.backends["icve_toolkit"]
        slider = icve.loader.load("icve_toolkit", "slider_auto")
        _Patch(self, slider, "missing_packages", lambda: ["playwright", "scipy"])

        with self.assertRaises(BackendUnavailable) as ctx:
            icve.login_with_password("u", "p")
        e = ctx.exception
        self.assertEqual(e.code, "ZJ-3001")
        self.assertTrue(e.retryable, "依赖缺失应可降级到别的登录通道")
        self.assertIn("playwright", e.message)
        self.assertIn("pip install", e.details["hint"])

    def test_icve_password_login_failure_raises_credential_rejected(self):
        icve = self.backends["icve_toolkit"]
        slider = icve.loader.load("icve_toolkit", "slider_auto")
        _Patch(self, slider, "missing_packages", lambda: [])
        _Patch(self, slider, "obtain_sso_token", lambda *a, **k: None)

        with self.assertRaises(CredentialRejected) as ctx:
            icve.login_with_password("u", "wrong")
        self.assertEqual(ctx.exception.code, "ZJ-2002")
        self.assertFalse(ctx.exception.retryable, "账密/滑块失败不应重试（触发风控）")
        self.assertIn("风控", str(ctx.exception.details))

    def test_mwa_browser_callback_returns_login_state(self):
        mwa = self.backends["mooc_work_answer"]
        mod = mwa.loader.load("mooc_work_answer", "NewMoocMain.oauth_login")
        seen: Dict[str, Any] = {}

        def fake_oauth(*, timeout=300, preferred_port=None):
            seen.update(timeout=timeout, preferred_port=preferred_port)
            return "sso-from-oauth"

        _Patch(self, mod, "oauth_login", fake_oauth)
        st = mwa.login_via_browser_callback(timeout=77, preferred_port=9999)
        self.assertEqual(st.sso_token, "sso-from-oauth")
        self.assertEqual(seen, {"timeout": 77, "preferred_port": 9999})

    def test_mwa_browser_callback_timeout_raises_credential_rejected(self):
        mwa = self.backends["mooc_work_answer"]
        mod = mwa.loader.load("mooc_work_answer", "NewMoocMain.oauth_login")
        _Patch(self, mod, "oauth_login", lambda **k: None)

        with self.assertRaises(CredentialRejected) as ctx:
            mwa.login_via_browser_callback(timeout=1)
        self.assertEqual(ctx.exception.code, "ZJ-2002")
        self.assertEqual(ctx.exception.details["timeout"], 1)

    def test_login_methods_are_not_among_the_seven_capabilities(self):
        """登录是"取得统一登录态"的通道，**不是**对外 7 个能力之一。"""
        from zhijiao.backends import Capability

        for bid in ("icve_toolkit", "mooc_work_answer"):
            caps = {c.value for c in self.backends[bid].supported_capabilities()}
            self.assertNotIn("login", caps)
        self.assertNotIn("login_with_password", {c.value for c in Capability})
        self.assertNotIn("login_via_browser_callback", {c.value for c in Capability})


class TestBrowserRuntimeInProject(unittest.TestCase):
    """Chromium 内核必须落在项目内，且真的能启动（离线）。"""

    @classmethod
    def setUpClass(cls):
        cls.st = bootstrap()
        cls.st.apply_env()

    def test_browsers_dir_is_inside_project(self):
        root = Path(__file__).resolve().parent.parent
        browsers = self.st.browsers_dir
        self.assertEqual(browsers, root / "browsers")
        self.assertTrue(
            str(browsers).startswith(str(root)),
            f"浏览器目录跑到了项目外：{browsers}",
        )

    def test_apply_env_points_playwright_at_project_dir(self):
        self.st.apply_env()
        self.assertEqual(Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]), self.st.browsers_dir)

    def test_apply_env_does_not_override_explicit_setting(self):
        """使用者显式设过 PLAYWRIGHT_BROWSERS_PATH 时应尊重它（setdefault 语义）。"""
        old = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        try:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = r"C:\custom\browsers"
            self.st.apply_env()
            self.assertEqual(os.environ["PLAYWRIGHT_BROWSERS_PATH"], r"C:\custom\browsers")
        finally:
            if old is None:
                os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
            else:
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = old

    def test_no_browser_artifacts_outside_project(self):
        """用户目录里不应再有 playwright 的浏览器缓存（已整体搬进项目）。"""
        outside = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
        self.assertFalse(
            outside.exists(),
            f"项目外仍存在浏览器缓存：{outside}（应已移入项目 browsers/）",
        )

    def test_upstream_reports_deps_ready(self):
        icve = build_backends(self.st)["icve_toolkit"]
        if not icve.available:
            self.skipTest(f"ICVE_Toolkit 后端不在岗：{icve.unavailable_reason()}")
        slider = icve.loader.load("icve_toolkit", "slider_auto")
        self.assertEqual(
            slider.missing_packages(), [],
            "账密自动登录的依赖不齐，zhijiao.login(channel='password') 会失败",
        )
        self.assertTrue(slider.deps_ready())

    @unittest.skipUnless(
        (Path(__file__).resolve().parent.parent / "browsers").is_dir(),
        "browsers/ 不存在（未下载 Chromium），跳过真实启动测试",
    )
    def test_chromium_actually_launches_from_project_dir(self):
        """真启动一次 Chromium —— 不联网，只证明项目内内核可用。"""
        self.st.apply_env()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("未安装 playwright")

        with sync_playwright() as p:
            exe = p.chromium.executable_path
            self.assertIn(str(self.st.browsers_dir), exe, f"内核不在项目内：{exe}")
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content("<h1 id='t'>ok</h1>")
                self.assertEqual(page.inner_text("#t"), "ok")
                self.assertTrue(browser.version)
            finally:
                browser.close()


class _FakeEdgePage:
    def __init__(self):
        self.url = None

    def goto(self, url, **kw):
        self.url = url


class _FakeEdgeChromium:
    def __init__(self, ctx):
        self._ctx = ctx
        self.launch_kwargs = None

    def launch_persistent_context(self, **kw):
        err = getattr(self._ctx, "launch_error", None)
        if err is not None:
            raise err
        return self._ctx


class _FakeEdgePlaywright:
    def __init__(self, ctx):
        self.chromium = _FakeEdgeChromium(ctx)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install_fake_playwright(test, ctx):
    """把 playwright.sync_api.sync_playwright 换成可控假件（结束后还原）。"""
    import sys
    import types

    fake_mod = types.ModuleType("playwright.sync_api")
    fake_mod.sync_playwright = lambda: _FakeEdgePlaywright(ctx)

    saved_api = sys.modules.get("playwright.sync_api")
    saved_pkg = sys.modules.get("playwright")

    def _restore():
        if saved_api is None:
            sys.modules.pop("playwright.sync_api", None)
        else:
            sys.modules["playwright.sync_api"] = saved_api
        if saved_pkg is None:
            sys.modules.pop("playwright", None)
        else:
            sys.modules["playwright"] = saved_pkg

    sys.modules["playwright.sync_api"] = fake_mod
    test.addCleanup(_restore)
    return ctx


class _FakeOAuthModule:
    """可用的假 OAuthLoginHandler：start_server 后可定时回调 token / 触发关闭。"""

    def __init__(self, *, token_after=None, fire_close_after=None):
        import threading
        import types

        outer = self
        instances = []

        class _Handler:
            def __init__(self, port=None):
                self.port = port or 18999
                self.token = None
                self.started = False
                self.stopped = False
                instances.append(self)

            def start_server(self, retry_count=3):
                self.started = True
                if token_after is not None:
                    t = threading.Timer(
                        token_after, lambda: setattr(self, "token", "tok-edge")
                    )
                    t.daemon = True
                    t.start()
                return True

            def stop_server(self, *a):
                self.stopped = True

        self.instances = instances
        self.module = types.ModuleType("NewMoocMain.oauth_login")
        self.module.OAuthLoginHandler = _Handler
        outer._Handler = _Handler


def _stub_oauth_module(test, mwa, fake):
    """只把 oauth_login 那个模块换成假件，其余委托给真实 loader。

    注意必须先捕获原始 ``load`` 再 patch，否则 lambda 调自己会无限递归。
    """
    original_load = mwa.loader.load

    def _load(bid, name, **k):
        if name.endswith("oauth_login"):
            return fake.module
        return original_load(bid, name, **k)

    _Patch(test, mwa.loader, "load", _load)


class TestEdgeChannel(unittest.TestCase):
    """独立 Edge 通道：新起独立 profile 的 Edge，绝不碰系统默认浏览器。"""

    @classmethod
    def setUpClass(cls):
        cls.st = bootstrap()
        cls.backends = build_backends(force=True)

    def test_channel_dispatches_to_edge_method(self):
        from zhijiao.session import LoginState

        mwa = self.backends["mooc_work_answer"]
        seen = {}

        def fake_edge(**kw):
            seen.update(kw)
            return LoginState(sso_token="tok-edge")

        _Patch(self, mwa, "login_via_edge_browser", fake_edge)
        st = login(channel="edge", timeout=123, preferred_port=19000,
                   settings=self.st, persist=False)
        self.assertEqual(st.sso_token, "tok-edge")
        self.assertEqual(seen["timeout"], 123)
        self.assertEqual(seen["preferred_port"], 19000)

    def test_edge_channel_uses_isolated_profile_and_never_webbrowser(self):
        """关键安全断言：独立 profile 在项目内；全程不调用 webbrowser.open。"""
        import webbrowser
        from pathlib import Path

        mwa = self.backends["mooc_work_answer"]
        fake = _FakeOAuthModule(token_after=0.2)
        _stub_oauth_module(self, mwa, fake)

        ctx = _FakeEdgePage()  # 复用 page 假件当 ctx 容器（有 pages 属性语义即可）

        class _Ctx:
            pages = [_FakeEdgePage()]

            def on(self, ev, cb):
                pass

            def new_page(self):
                return _FakeEdgePage()

            def close(self):
                pass

        _install_fake_playwright(self, _Ctx())

        opened = []
        _Patch(self, webbrowser, "open", lambda *a, **k: opened.append(a) or True)

        st = mwa.login_via_edge_browser(timeout=15, preferred_port=19001)
        self.assertEqual(st.sso_token, "tok-edge")
        self.assertEqual(opened, [], "独立 Edge 通道绝不允许调用 webbrowser.open")

        h = fake.instances[-1]
        self.assertTrue(h.started)
        self.assertTrue(h.stopped, "结束时应关闭本地回调服务")
        # 独立 profile 必须落在项目内 state/
        self.assertEqual(
            Path(str(mwa.settings.state_dir)) / "edge-profile",
            self.st.state_dir / "edge-profile",
        )

    def test_edge_channel_launch_failure_is_actionable(self):
        mwa = self.backends["mooc_work_answer"]
        fake = _FakeOAuthModule()
        _stub_oauth_module(self, mwa, fake)

        class _Boom:
            #: 让 _FakeEdgeChromium.launch_persistent_context 抛错，模拟"没有 Edge"
            launch_error = RuntimeError("msedge not found")
            pages: list = []

            def __init__(self):
                self.chromium = self

            def on(self, ev, cb):
                pass

            def new_page(self):
                return _FakeEdgePage()

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        _install_fake_playwright(self, _Boom())
        with self.assertRaises(BackendUnavailable) as ctx:
            mwa.login_via_edge_browser(timeout=5)
        self.assertIn("Edge", ctx.exception.message)
        self.assertEqual(ctx.exception.code, "ZJ-3001")

    def test_edge_channel_user_closes_window_without_login(self):
        """用户直接关掉 Edge 窗口 → 视为取消，抛 CredentialRejected。"""
        import threading

        mwa = self.backends["mooc_work_answer"]
        fake = _FakeOAuthModule(token_after=None)
        _stub_oauth_module(self, mwa, fake)

        class _ClosingCtx:
            def __init__(self):
                self.pages = []
                self._cbs = []
                t = threading.Timer(0.4, lambda: [cb() for cb in list(self._cbs)])
                t.daemon = True
                t.start()

            def on(self, ev, cb):
                self._cbs.append(cb)

            def new_page(self):
                return _FakeEdgePage()

            def close(self):
                pass

        _install_fake_playwright(self, _ClosingCtx())
        with self.assertRaises(CredentialRejected):
            mwa.login_via_edge_browser(timeout=20)

    def test_edge_is_in_login_channels_but_not_a_capability(self):
        from zhijiao.api import LOGIN_CHANNELS
        from zhijiao.backends import Capability

        self.assertIn("edge", LOGIN_CHANNELS)
        self.assertNotIn("edge", {c.value for c in Capability})

    def test_auto_falls_back_to_edge_not_default_browser(self):
        """auto 降级应优先独立 Edge，而不是可能占用中的系统默认浏览器。"""
        from zhijiao.errors import BackendUnavailable

        mwa = self.backends["mooc_work_answer"]
        icve = self.backends["icve_toolkit"]
        used = []
        fake = _FakeOAuthModule()

        _Patch(self, icve, "login_with_password",
               lambda *a, **k: (_ for _ in ()).throw(BackendUnavailable("缺 playwright")))
        _Patch(self, mwa, "login_via_edge_browser",
               lambda **k: used.append("edge") or LoginState(sso_token="tok-edge"))
        _Patch(self, mwa, "login_via_browser_callback",
               lambda **k: used.append("default-browser") or LoginState(sso_token="tok-b"))

        st = login("u", "p", channel="auto", settings=self.st, persist=False)
        self.assertEqual(st.sso_token, "tok-edge")
        self.assertEqual(used, ["edge"], "auto 应退到独立 Edge，而不是系统默认浏览器")


if __name__ == "__main__":
    unittest.main(verbosity=2)

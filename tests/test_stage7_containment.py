"""阶段 7 测试：运行期副作用收容（保证"上游只读区"永远干净）。

背景（2026-09-17 实测事故）：用户做了一次真实的滑块登录，
上游 ``slider_auto._calib_path()`` 把 ``slider_calib.json`` 写进了
``upstreams/ICVE_Toolkit/``，``import`` 又写了 ``__pycache__/*.pyc`` ——
**上游只读区被污染，阶段 1 完整性测试变红**。

本阶段验证收容措施：

1. ``sys.pycache_prefix`` 已指向项目内 ``state/pycache``，
   上游目录里**永远不会再出现** ``__pycache__``；
2. ``slider_auto._calib_path()`` 已被重定向到 ``state/slider_calib.json``；
3. 通过上游自己的 ``_calib_path()`` 写文件，文件落在 ``state/``，上游目录不变；
4. 走一遍"打桩的账密登录"之后，上游逐文件 sha256 仍然与基线完全一致。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

from _common import (
    UPSTREAMS,
    UPSTREAM_DIRS,
    bootstrap,
    diff_is_empty,
    diff_manifest,
    diff_summary,
)

from zhijiao.backends import build_backends
from zhijiao.errors import CredentialRejected
from zhijiao.session import LoginState


class TestPycacheRedirect(unittest.TestCase):
    """``import`` 上游不应再往上游目录写 ``__pycache__``。"""

    @classmethod
    def setUpClass(cls):
        cls.st = bootstrap()

    def test_pycache_prefix_points_inside_project(self):
        prefix = getattr(sys, "pycache_prefix", None)
        self.assertIsNotNone(prefix, "sys.pycache_prefix 未设置，上游目录会被写 __pycache__")
        self.assertTrue(
            str(prefix).startswith(str(self.st.state_dir)),
            f"pycache_prefix 不在项目 state/ 内：{prefix}",
        )
        self.assertNotIn("upstreams", str(prefix))

    def test_importing_upstream_does_not_create_pycache_in_upstream_dir(self):
        icve = build_backends(self.st)["icve_toolkit"]
        self.assertTrue(icve.available, icve.unavailable_reason())
        # 强制 import 一批上游模块
        for mod in ("zjy_client", "speed_course", "sign", "answer", "slider_auto"):
            icve.loader.load("icve_toolkit", mod)
        for bid, dirname in UPSTREAM_DIRS.items():
            with self.subTest(upstream=bid):
                pycache = UPSTREAMS / dirname / "__pycache__"
                self.assertFalse(
                    pycache.exists(),
                    f"上游目录出现 __pycache__（应被 sys.pycache_prefix 重定向）：{pycache}",
                )


class TestSliderCalibRedirect(unittest.TestCase):
    """滑块校准数据必须落到 state/，不能写进上游目录。"""

    @classmethod
    def setUpClass(cls):
        cls.st = bootstrap()
        cls.icve = build_backends(force=True)["icve_toolkit"]

    def test_calib_path_redirected_into_state(self):
        self.icve._contain_runtime_writes()
        slider = self.icve.loader.load("icve_toolkit", "slider_auto")
        got = Path(str(slider._calib_path()))
        self.assertTrue(
            str(got).startswith(str(self.st.state_dir)),
            f"校准文件仍在项目 state/ 之外：{got}",
        )
        self.assertNotIn("upstreams", str(got))
        self.assertEqual(got.name, "slider_calib.json")

    def test_writing_via_upstream_helper_lands_in_state(self):
        """用**上游自己的** _calib_path() 写文件，文件必须出现在 state/。"""
        self.icve._contain_runtime_writes()
        slider = self.icve.loader.load("icve_toolkit", "slider_auto")
        p = Path(str(slider._calib_path()))
        try:
            p.write_text('{"probe": true}', encoding="utf-8")
            self.assertTrue(p.is_file(), "写入未落到 state/")
            self.assertNotIn("upstreams", str(p))
        finally:
            if p.is_file():
                p.unlink()

    def test_upstream_dir_has_no_runtime_artifacts(self):
        for bid, dirname in UPSTREAM_DIRS.items():
            base = UPSTREAMS / dirname
            for pat in ("slider_calib.json", "accounts.json", "__pycache__", "*.log"):
                hits = list(base.rglob(pat))
                with self.subTest(upstream=bid, pattern=pat):
                    self.assertEqual(
                        hits, [], f"上游目录出现运行期产物 {pat}: {[str(h) for h in hits]}"
                    )


class TestLoginAttemptKeepsUpstreamClean(unittest.TestCase):
    """完整走一遍"打桩的账密登录"，上游 sha256 仍必须与基线一致。"""

    @classmethod
    def setUpClass(cls):
        cls.st = bootstrap()
        cls.backends = build_backends(force=True)

    def test_full_login_attempt_leaves_upstream_untouched(self):
        from zhijiao.api import login

        icve = self.backends["icve_toolkit"]
        slider = icve.loader.load("icve_toolkit", "slider_auto")

        # 打桩：不真启动 Chromium。先让"滑块未通过"（返回 None），
        # 这正是 2026-09-17 用户真实遇到的那条失败路径。
        old_obtain = slider.obtain_sso_token
        slider.obtain_sso_token = lambda *a, **k: None
        try:
            with self.assertRaises(CredentialRejected):
                login("probe-user", "probe-pwd", channel="password",
                      settings=self.st, persist=False)
        finally:
            slider.obtain_sso_token = old_obtain

        diff = diff_manifest()
        self.assertTrue(
            diff_is_empty(diff),
            "一次登录尝试之后上游文件被改动：" + diff_summary(diff),
        )

    def test_login_attempt_is_recorded_for_diagnosis(self):
        """登录尝试必须留下 JSONL 日志，否则事后无从排查。"""
        import json

        from zhijiao.api import login

        icve = self.backends["icve_toolkit"]
        slider = icve.loader.load("icve_toolkit", "slider_auto")
        old_obtain = slider.obtain_sso_token
        slider.obtain_sso_token = lambda *a, **k: "sso-probe-token"
        try:
            login("probe-user", "probe-pwd", channel="password",
                  settings=self.st, persist=False)
        finally:
            slider.obtain_sso_token = old_obtain

        day = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
        path = self.st.log_dir / f"login-{day}.jsonl"
        self.assertTrue(path.is_file(), f"登录日志不存在：{path}")
        recs = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertTrue(recs, "登录日志是空的")
        last = recs[-1]
        self.assertTrue(last["ok"])
        self.assertEqual(last["channel"], "password")
        self.assertEqual(last["username"], "probe-user")
        # token 必须掩码，绝不能明文落盘
        self.assertNotIn("sso-probe-token", json.dumps(recs, ensure_ascii=False))
        self.assertIn("...", last["sso_token"])
        # 任何记录里都不该出现密码
        self.assertNotIn("probe-pwd", json.dumps(recs, ensure_ascii=False))


class TestUpstreamStillCleanAfterAll(unittest.TestCase):
    """本阶段真的 import 并调用了上游 —— 最后再整体复核一次。"""

    def test_manifest_clean(self):
        bootstrap()
        diff = diff_manifest()
        self.assertTrue(diff_is_empty(diff), diff_summary(diff))


if __name__ == "__main__":
    unittest.main(verbosity=2)

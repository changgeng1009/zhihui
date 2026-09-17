"""阶段 3 测试：Adapter 契约（真正 import 上游，断言"能力没被破坏"）。

这是最关键的回归守卫：
* **真的**把上游模块 import 进来（不是 mock），验证 Adapter 依赖的每个方法/签名仍然存在；
* 验证 Adapter 与 Backend 协议的符合性；
* 验证占位后端（ZJY）与非签到后端（mooc-work-answer）如实声明自身能力；
* 验证 OCS 兜底产出的是"交接计划"而不是假数据；
* 最后**复算上游清单**，证明这一路的 import 没有碰过上游任何文件。

上游一旦改签名/删方法，本测试立刻变红 —— 这就是"方便以后单独升级某一个上游项目"的保障。
"""

from __future__ import annotations

import inspect
import unittest
from typing import Any, Callable, Iterable

from _common import UPSTREAMS, UPSTREAM_DIRS, bootstrap, diff_is_empty, diff_manifest, diff_summary

from zhijiao.backends import (
    ALL_CAPABILITIES,
    Capability,
    build_backends,
    backend_infos,
)
from zhijiao.errors import BackendUnavailable, CapabilityNotSupported, ZhijiaoError
from zhijiao.session import LoginState

#: 测试用的假凭据：只用于构造客户端对象，**不会**产生网络成功
FAKE = LoginState(sso_token="unit-test-sso-token", username="unit_test")


def assert_params(test: unittest.TestCase, func: Callable, names: Iterable[str], label: str = ""):
    """断言函数签名里包含这些形参名（容忍上游新增可选参数）。"""
    sig = inspect.signature(func)
    have = set(sig.parameters)
    missing = [n for n in names if n not in have]
    test.assertEqual(
        missing, [],
        f"{label or getattr(func, '__qualname__', func)} 缺少形参 {missing}；"
        f"实际为 {sorted(have)}",
    )


class TestIcveToolkitAdapter(unittest.TestCase):
    """主力后端：上游契约逐条钉住。"""

    @classmethod
    def setUpClass(cls):
        bootstrap()
        cls.backends = build_backends(force=True)
        cls.b = cls.backends["icve_toolkit"]

    def test_available_and_capabilities(self):
        self.assertTrue(self.b.available, f"不可用原因：{self.b.unavailable_reason()}")
        self.assertEqual(self.b.supported_capabilities(), ALL_CAPABILITIES)
        # 四个上游里唯一覆盖全部 7 个能力

    def test_declared_capabilities_are_implemented(self):
        for cap in ALL_CAPABILITIES:
            with self.subTest(capability=cap.value):
                self.assertTrue(callable(getattr(self.b, cap.value, None)))

    def test_upstream_commit_recorded(self):
        commit = self.b.loader.commit("icve_toolkit")
        self.assertRegex(commit, r"^[0-9a-f]{7,40}$", "应能读到上游 commit 以便追溯版本")

    # ---- 逐个上游契约 -------------------------------------------------- #
    def test_zjy_client_contract(self):
        mod = self.b.loader.load("icve_toolkit", "zjy_client")
        self.assertTrue(hasattr(mod, "ZjyClient"))
        cls = mod.ZjyClient

        assert_params(self, cls.__init__, ["token", "sso_token", "question_bank_dir"], "ZjyClient.__init__")
        # get_my_courses: 三域聚合课程列表
        self.assertTrue(callable(getattr(cls, "get_my_courses", None)))
        assert_params(self, cls.get_my_courses, [], "get_my_courses")
        # get_course_cells: 课程树叶子节点（含 include_completed / ctype）
        assert_params(
            self, cls.get_course_cells,
            ["course_info_id", "class_id", "course_id", "include_completed", "ctype"],
            "ZjyClient.get_course_cells",
        )
        # _parse_cell_speed: 进度解析（静态方法）
        assert_params(self, cls._parse_cell_speed, ["r", "ctype"], "ZjyClient._parse_cell_speed")
        # 鉴权相关
        for name in ("apply_token", "refresh_token_from_sso", "auth_ai_domain", "auth_zyk_domain",
                     "generate_aes_key", "aes_encrypt"):
            with self.subTest(method=name):
                self.assertTrue(callable(getattr(cls, name, None)), f"ZjyClient.{name} 消失")
        # 成绩/试卷相关（get_results 依赖）
        for name in ("zyk_get_exam_list", "zyk_get_exam_paper", "zyk_get_homework_answers"):
            with self.subTest(method=name):
                self.assertTrue(callable(getattr(cls, name, None)), f"ZjyClient.{name} 消失")
        # 三域常量
        for const in ("BASE_URL", "AI_BASE_URL", "ZYK_BASE_URL", "SSO_BASE"):
            with self.subTest(const=const):
                self.assertTrue(hasattr(mod, const), f"域名常量 {const} 消失")

    def test_speed_course_contract(self):
        mod = self.b.loader.load("icve_toolkit", "speed_course")
        assert_params(
            self, mod.run_speed_course,
            ["client", "course", "speed_type", "simulate_real"],
            "speed_course.run_speed_course",
        )

    def test_sign_contract(self):
        mod = self.b.loader.load("icve_toolkit", "sign")
        assert_params(
            self, mod.get_signs,
            ["client", "class_id", "course_info_id", "course_id"],
            "sign.get_signs",
        )
        for name in ("batch_sign", "one_click_sign", "do_sign_action", "convert_gesture"):
            with self.subTest(func=name):
                self.assertTrue(callable(getattr(mod, name, None)), f"sign.{name} 消失")

    def test_answer_contract(self):
        mod = self.b.loader.load("icve_toolkit", "answer")
        assert_params(
            self, mod.get_course_exams_list,
            ["client", "class_id", "course_info_id", "course_id", "ctype"],
            "answer.get_course_exams_list",
        )

    def test_slider_login_optional_path(self):
        mod = self.b.loader.load("icve_toolkit", "slider_auto")
        assert_params(
            self, mod.obtain_sso_token,
            ["user", "pwd", "max_attempts", "headless"],
            "slider_auto.obtain_sso_token",
        )
        self.assertTrue(callable(getattr(mod, "missing_packages", None)))

    def test_lazy_import_inside_speed_course_still_works(self):
        """上游 ``speed_course`` 有函数内懒导入 ``from answer import ...``。

        这要求上游目录**常驻 sys.path**。若 loader 在 import 后移除路径，
        这里会 ImportError —— 正是这个测试守住了那个坑。
        """
        self.b.loader.load("icve_toolkit", "speed_course")
        src = inspect.getsource(
            self.b.loader.load("icve_toolkit", "speed_course").run_speed_course
        )
        self.assertIn("_brush_progress", src)
        # 直接验证答案模块可用（懒导入的目标）
        self.assertTrue(callable(getattr(self.b.loader.load("icve_toolkit", "answer"),
                                        "get_course_exams_list", None)))


class TestMoocWorkAnswerAdapter(unittest.TestCase):
    """次选后端：抽象最干净，但**没有签到能力**。"""

    @classmethod
    def setUpClass(cls):
        bootstrap()
        cls.b = build_backends(force=True)["mooc_work_answer"]

    def test_available(self):
        self.assertTrue(self.b.available, f"不可用原因：{self.b.unavailable_reason()}")

    def test_does_not_claim_attendance(self):
        self.assertNotIn(
            Capability.GET_ATTENDANCE, self.b.supported_capabilities(),
            "mooc-work-answer 没有签到模块，绝不能声明支持 get_attendance",
        )
        # 除非上游真的加进签到 —— 那说明它升级了，本项目应同步更新路由表
        src = (UPSTREAMS / "mooc-work-answer")
        sign_like = [p.name for p in src.rglob("*sign*.py")]
        if sign_like:
            self.fail(f"上游新增了疑似签到模块 {sign_like}，请复核路由表与能力声明")

    def test_capabilities_are_implemented(self):
        for cap in self.b.supported_capabilities():
            with self.subTest(capability=cap.value):
                self.assertTrue(callable(getattr(self.b, cap.value, None)))

    def test_base_api_client_contract(self):
        mod = self.b.loader.load("mooc_work_answer", "base.api_client")
        self.assertTrue(hasattr(mod, "BaseAPIClient"))
        for name in ("get", "post", "put", "login", "set_auth_token"):
            with self.subTest(method=name):
                self.assertTrue(callable(getattr(mod.BaseAPIClient, name, None)))

    def test_zyk_api_contract(self):
        mod = self.b.loader.load("mooc_work_answer", "ZYKMoocMain.api")
        cls = mod.ZYKMoocApi
        assert_params(self, cls.__init__, ["username", "password", "token"], "ZYKMoocApi.__init__")
        assert_params(self, cls.my_course_list, ["page_num", "page_size", "flag"], "ZYKMoocApi.my_course_list")
        assert_params(self, cls.study_design_list, ["course_info_id"], "ZYKMoocApi.study_design_list")
        assert_params(self, cls.course_content, ["source_id"], "ZYKMoocApi.course_content")
        assert_params(
            self, cls.study_record,
            ["course_info_id", "parent_id", "study_time", "source_id",
             "actual_num", "last_num", "total_num"],
            "ZYKMoocApi.study_record",
        )
        self.assertTrue(hasattr(mod, "ZYK_BASE_URL"))

    def test_ai_api_contract(self):
        mod = self.b.loader.load("mooc_work_answer", "AIMoocMain.api")
        cls = mod.AIMoocApi
        assert_params(self, cls.my_course_list, ["page_num", "page_size", "query_type", "flag"],
                      "AIMoocApi.my_course_list")
        assert_params(self, cls.study_design_list, ["course_info_id", "course_id"],
                      "AIMoocApi.study_design_list")
        assert_params(self, cls.study_record_list, ["course_info_id", "course_id"],
                      "AIMoocApi.study_record_list")
        assert_params(self, cls.get_cell_list, ["course_info_id", "course_id", "parent_id"],
                      "AIMoocApi.get_cell_list")
        self.assertTrue(hasattr(mod, "BASE_URL"))

    def test_handlers_exist_and_are_account_scoped(self):
        """上游刷课入口在 ``__init__`` 里就调 start_courses()，粒度是**账号级**。

        Adapter 的 ``start_learning`` 文档与 ``details['scope']`` 依赖这一事实，
        这里把它钉住；上游若改为按课程，需要同步更新文档与实现。
        """
        ai = self.b.loader.load("mooc_work_answer", "AIMoocMain.main")
        zyk = self.b.loader.load("mooc_work_answer", "ZYKMoocMain.main")
        for cls in (ai.AIMoocHandler, zyk.ZYKMoocHandler):
            with self.subTest(handler=cls.__name__):
                self.assertTrue(callable(getattr(cls, "start_courses", None)))
                self.assertTrue(callable(getattr(cls, "process_course", None)))
        ai_src = inspect.getsource(ai.AIMoocHandler.__init__)
        self.assertIn("start_courses", ai_src, "上游刷课入口语义变了，请复核 start_learning 的 scope 说明")


class TestZjyToolkitPlaceholder(unittest.TestCase):
    """远端占位：必须始终不可用，且给出可读原因。"""

    @classmethod
    def setUpClass(cls):
        bootstrap()
        cls.b = build_backends(force=True)["zjy_toolkit"]

    def test_always_unavailable(self):
        self.assertFalse(self.b.available)

    def test_reason_explains_why(self):
        reason = self.b.unavailable_reason()
        self.assertIn("README", reason)
        self.assertTrue(any(k in reason for k in ("无源码", "study.atvkh.xyz")))

    def test_call_raises_backend_unavailable(self):
        for cap in self.b.supported_capabilities():
            with self.subTest(capability=cap.value):
                with self.assertRaises(BackendUnavailable) as ctx:
                    self.b.call(cap, session=FAKE)
                self.assertEqual(ctx.exception.code, "ZJ-3001")
                self.assertTrue(ctx.exception.retryable, "不可用必须可降级，路由器才能跳过它")

    def test_check_reports_no_source(self):
        c = self.b.check()
        self.assertFalse(c.available)
        self.assertFalse(c.extra["has_source"])
        self.assertEqual(c.extra["endpoint"], "https://study.atvkh.xyz")

    def test_only_used_for_attendance_parity(self):
        """它声明了签到能力 —— 这是路由表把它排进 get_attendance 的依据。"""
        self.assertIn(Capability.GET_ATTENDANCE, self.b.supported_capabilities())


class TestOcsjsBrowserFallback(unittest.TestCase):
    """浏览器兜底：产出交接计划，不抓数据。"""

    @classmethod
    def setUpClass(cls):
        bootstrap()
        cls.b = build_backends(force=True)["ocsjs"]

    def test_available_without_credential(self):
        self.assertTrue(self.b.available, f"不可用原因：{self.b.unavailable_reason()}")
        self.assertTrue(self.b.info.credential_optional)

    def test_does_not_require_login(self):
        # 传入空的 session 也应当能产出计划
        h = self.b.call(Capability.START_LEARNING, session=LoginState())
        self.assertEqual(h.capability, "start_learning")

    def test_upstream_project_files_exist(self):
        """交接计划里的 Project 名必须真的存在于上游，不能凭空捏造。"""
        d = UPSTREAMS / UPSTREAM_DIRS["ocsjs"]
        for rel, needle in (
            ("packages/scripts/src/projects/zjy.ts", "'职教云'"),
            ("packages/scripts/src/projects/icve.ts", "'智慧职教'"),
        ):
            with self.subTest(file=rel):
                p = d / rel
                self.assertTrue(p.is_file(), f"找不到上游 {rel}")
                text = p.read_text(encoding="utf-8", errors="replace")
                self.assertIn(needle, text, f"{rel} 里找不到 Project 名 {needle}")

    def test_project_names_match_upstream(self):
        for ct, want_project in (
            (None, "zjy"),
            ("SPOC", "zjy"),
            ("RESOURCE", "zjy"),
            ("MOOC", "icve"),
        ):
            with self.subTest(course_type=ct):
                h = self.b.list_courses(course_type=ct)
                self.assertEqual(h.ocs_projects, [want_project])

    def test_handoff_for_every_capability(self):
        for cap in Capability:
            with self.subTest(capability=cap.value):
                h = self.b.call(cap, session=LoginState())
                self.assertEqual(h.capability, cap.value)
                self.assertTrue(h.target_url.startswith("https://"))
                self.assertTrue(h.steps, "交接计划必须给操作步骤")
                self.assertTrue(h.reason)

    def test_handoff_carries_course_locator(self):
        from zhijiao.contracts import Course, CourseType

        c = Course(course_id="C9", course_info_id="I9", class_id="K9",
                   name="测试课", course_type=CourseType.MOOC)
        h = self.b.get_course_progress(course=c)
        self.assertEqual(h.params["courseId"], "C9")
        self.assertEqual(h.params["courseInfoId"], "I9")
        self.assertEqual(h.target_url, "https://ai.icve.com.cn/")
        # 步骤里应能定位到课程
        self.assertTrue(any("C9" in s for s in h.steps))

    def test_scenario_mapping(self):
        expect = {
            Capability.START_LEARNING: "study",
            Capability.GET_ATTENDANCE: "sign",
            Capability.GET_RESULTS: "exam",
            Capability.GET_COURSE_PROGRESS: "progress",
        }
        for cap, want in expect.items():
            with self.subTest(capability=cap.value):
                self.assertEqual(self.b.call(cap, session=LoginState()).scenario, want)


class TestAdapterProtocolConformance(unittest.TestCase):
    """所有 Adapter 都必须符合 Backend 协议。"""

    @classmethod
    def setUpClass(cls):
        bootstrap()
        cls.backends = build_backends(force=True)

    def test_registry_covers_four_upstreams(self):
        self.assertEqual(
            set(self.backends),
            {"icve_toolkit", "zjy_toolkit", "mooc_work_answer", "ocsjs"},
        )

    def test_infos_declare_license_and_repo(self):
        for info in backend_infos():
            with self.subTest(backend=info["id"]):
                self.assertTrue(info["license"], "必须声明许可证（合规要求）")
                self.assertTrue(info["repo"].startswith("https://github.com/"))
                self.assertTrue(info["capabilities"])

    def test_capabilities_implemented_or_call_overridden(self):
        from zhijiao.backends.base import BaseBackend

        for bid, b in self.backends.items():
            overrides_call = type(b).call is not BaseBackend.call
            for cap in b.supported_capabilities():
                with self.subTest(backend=bid, capability=cap.value):
                    self.assertTrue(
                        callable(getattr(b, cap.value, None)) or overrides_call,
                        f"{bid} 声明支持 {cap.value} 但既无同名方法也未覆写 call()",
                    )

    def test_unsupported_capability_raises_not_supported(self):
        b = self.backends["mooc_work_answer"]
        with self.assertRaises(CapabilityNotSupported) as ctx:
            b.call(Capability.GET_ATTENDANCE, session=FAKE)
        self.assertEqual(ctx.exception.code, "ZJ-3002")
        self.assertTrue(ctx.exception.retryable)

    def test_missing_credential_raises_auth_error_and_is_not_retryable(self):
        b = self.backends["icve_toolkit"]
        with self.assertRaises(ZhijiaoError) as ctx:
            b.call(Capability.LIST_COURSES, session=LoginState())
        self.assertEqual(ctx.exception.code, "ZJ-2001")
        self.assertFalse(ctx.exception.retryable, "缺凭据不应触发降级（换后端也一样）")

    def test_upstream_files_still_untouched_after_import(self):
        """本测试类真的 import 了上游模块 —— 证明这不会碰任何上游文件。"""
        diff = diff_manifest()
        self.assertTrue(diff_is_empty(diff), "import 上游后文件被改动：" + diff_summary(diff))


if __name__ == "__main__":
    unittest.main(verbosity=2)

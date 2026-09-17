"""阶段 5 测试：统一 Tool Layer 端到端（7 个能力 + 统一信封）。

用假 Backend 驱动完整链路：``Tools`` → ``Router`` → ``Backend``，
验证入参归一化、返回 DTO 形状、错误信封、兜底透传、以及便捷组合方法。

最后一组用**真实** Backend 实例跑"无凭据"路径，验证错误链路是统一且可读的，
（仍然不发任何网络请求 —— 因为缺凭据时会在构造客户端之前就抛 AuthError）。
"""

from __future__ import annotations

import unittest

from _common import bootstrap, fake_session, make_fake_backend, router_with

from zhijiao.api import call_tool, describe_tools, health, list_tools, make_tools
from zhijiao.backends import ALL_CAPABILITIES, Capability
from zhijiao.contracts import (
    AttendanceRecord,
    BrowserHandoff,
    Course,
    CourseDetail,
    CourseProgress,
    CourseType,
    ExamKind,
    ExamResult,
    LearningMode,
    LearningReport,
    SignStatus,
    TaskNode,
)
from zhijiao.errors import AuthError, RouteExhausted, ZhijiaoError
from zhijiao.session import LoginState
from zhijiao.tools import TOOL_NAMES, Tools, is_handoff


def _course():
    return Course(course_id="C1", course_info_id="I1", class_id="K1",
                  name="测试课程", course_type=CourseType.SPOC, backend="fake_a")


def _nodes():
    return [
        TaskNode(id="n1", name="视频1", file_type="video", progress=100, finished=True),
        TaskNode(id="n2", name="视频2", file_type="video", progress=30, finished=False),
        TaskNode(id="n3", name="课件3", file_type="ppt", progress=0, finished=False),
    ]


class _FakeBackendDriver:
    """一个"会算账"的假后端：按能力返回形状正确的 DTO。"""

    def __init__(self, backend_id="fake_a", error=None):
        self.calls = []
        self.backend = make_fake_backend(
            backend_id, result=self._respond, error=error, calls=self.calls
        )

    def _respond(self, capability, *, session=None, **kw):
        cap = Capability.parse(capability)
        course = kw.get("course") if isinstance(kw.get("course"), Course) else _course()
        if cap is Capability.LIST_COURSES:
            return [_course(), Course(course_id="C2", course_info_id="I2", name="另一门课",
                                      course_type=CourseType.MOOC, backend="fake_a")]
        if cap is Capability.GET_COURSE_DETAIL:
            return CourseDetail(course=course, nodes=_nodes(), backend="fake_a")
        if cap is Capability.GET_COURSE_PROGRESS:
            return CourseProgress.from_nodes(course, _nodes(), backend="fake_a")
        if cap is Capability.LIST_UNFINISHED_TASKS:
            out = [n for n in _nodes() if not n.finished]
            return out[: kw["limit"]] if kw.get("limit") else out
        if cap is Capability.START_LEARNING:
            return LearningReport(course=course, mode=kw.get("mode", LearningMode.PROGRESS),
                                  attempted=2, succeeded=2, failed=0, backend="fake_a")
        if cap is Capability.GET_ATTENDANCE:
            return [AttendanceRecord(sign_id="s1", title="第1次课", sign_type="普通签到",
                                     status=SignStatus.SIGNED, time="2026-09-01 08:00",
                                     backend="fake_a"),
                    AttendanceRecord(sign_id="s2", title="第2次课", status=SignStatus.UNSIGNED,
                                     backend="fake_a")]
        if cap is Capability.GET_RESULTS:
            return [ExamResult(exam_id="e1", title="期中考试", kind=ExamKind.EXAM, score=88.5,
                               submitted=True, backend="fake_a")]
        return None


class TestToolLayerHappyPath(unittest.TestCase):
    """7 个能力在假后端上的正常路径。"""

    @classmethod
    def setUpClass(cls):
        bootstrap()

    def setUp(self):
        self.driver = _FakeBackendDriver()
        self.tools = Tools(router=router_with(self.driver.backend), session=fake_session())

    def test_tool_names_are_the_seven_required(self):
        self.assertEqual(
            set(TOOL_NAMES),
            {"list_courses", "get_course_detail", "get_course_progress",
             "list_unfinished_tasks", "start_learning", "get_attendance", "get_results"},
        )
        self.assertEqual(set(list_tools()), set(TOOL_NAMES))

    def test_list_courses(self):
        got = self.tools.list_courses()
        self.assertEqual(len(got), 2)
        self.assertIsInstance(got[0], Course)
        self.assertEqual(got[0].course_type, CourseType.SPOC)
        self.assertEqual(self.tools.last_trace.chosen, "fake_a")

    def test_get_course_detail(self):
        d = self.tools.get_course_detail(course=_course())
        self.assertIsInstance(d, CourseDetail)
        self.assertEqual(d.total, 3)
        self.assertEqual(d.finished_count, 1)

    def test_get_course_progress(self):
        p = self.tools.get_course_progress(course_id="C1")
        self.assertIsInstance(p, CourseProgress)
        self.assertEqual(p.total, 3)
        self.assertEqual(p.finished, 1)
        self.assertAlmostEqual(p.percent, 33.33, places=1)
        self.assertIn("video", p.by_file_type)

    def test_list_unfinished_tasks(self):
        got = self.tools.list_unfinished_tasks(course_id="C1")
        self.assertEqual([n.id for n in got], ["n2", "n3"])
        self.assertTrue(all(not n.finished for n in got))

    def test_list_unfinished_tasks_with_limit(self):
        got = self.tools.list_unfinished_tasks(course_id="C1", limit=1)
        self.assertEqual([n.id for n in got], ["n2"])

    def test_start_learning(self):
        rep = self.tools.start_learning(course_id="C1", mode="progress")
        self.assertIsInstance(rep, LearningReport)
        self.assertEqual(rep.attempted, 2)
        self.assertEqual(rep.succeeded, 2)
        self.assertEqual(rep.mode, LearningMode.PROGRESS)
        self.assertEqual(rep.to_dict()["mode"], "progress")

    def test_start_learning_normalizes_string_mode(self):
        self.tools.start_learning(course_id="C1", mode="answer")
        call = self.driver.calls[-1]
        self.assertEqual(call["mode"], "answer")

    def test_start_learning_passes_through_extra_options(self):
        self.tools.start_learning(course_id="C1", simulate_real=True)
        self.assertTrue(self.driver.calls[-1]["simulate_real"])

    def test_get_attendance(self):
        got = self.tools.get_attendance(course_id="C1")
        self.assertEqual(len(got), 2)
        self.assertIs(got[0].status, SignStatus.SIGNED)
        self.assertEqual(got[0].status_text, "已签到")
        self.assertIs(got[1].status, SignStatus.UNSIGNED)

    def test_get_results(self):
        got = self.tools.get_results(course_id="C1")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].score, 88.5)
        self.assertEqual(got[0].score_text, "88.5")
        self.assertIs(got[0].kind, ExamKind.EXAM)

    def test_course_locator_variants(self):
        """``course=``、``course_id=`` 两种写法都要可用，且透传一致。"""
        self.tools.get_course_progress(course=_course())
        self.assertEqual(self.driver.calls[-1]["course"].course_id, "C1")
        self.tools.get_course_progress(course_id="C1")
        self.assertEqual(self.driver.calls[-1]["course_id"], "C1")
        self.tools.get_course_progress(course_id="C1", course_info_id="I1", class_id="K1")
        self.assertEqual(self.driver.calls[-1]["course_info_id"], "I1")

    def test_trace_available_after_each_call(self):
        self.tools.list_courses()
        t = self.tools.last_trace
        self.assertEqual(t.capability, "list_courses")
        self.assertEqual(t.attempts[0].backend, "fake_a")


class TestToolLayerFallback(unittest.TestCase):
    """Tool 层看到的降级与兜底。"""

    @classmethod
    def setUpClass(cls):
        bootstrap()

    def test_falls_back_to_second_backend(self):
        first = _FakeBackendDriver("fake_a", error=ZhijiaoError("a 挂了", code="ZJ-4001",
                                                                backend="fake_a"))
        second = _FakeBackendDriver("fake_b")
        tools = Tools(router=router_with(first.backend, second.backend), session=fake_session())
        got = tools.list_courses()
        self.assertEqual(len(got), 2)
        self.assertEqual(tools.last_trace.chosen, "fake_b")
        self.assertEqual(tools.last_trace.fallback_count, 1)
        self.assertEqual([c["backend"] for c in first.calls], ["fake_a"])

    def test_auth_error_surfaces_immediately(self):
        a = make_fake_backend("a", error=AuthError("未登录", backend="a"))
        b = _FakeBackendDriver("fake_b")
        tools = Tools(router=router_with(a, b.backend), session=fake_session())
        with self.assertRaises(AuthError):
            tools.list_courses()
        self.assertEqual(b.calls, [])

    def test_browser_handoff_passthrough_for_list(self):
        """API 全挂 -> OCS 交接计划，且不被包装成假数据。"""
        a = make_fake_backend("a", error=ZhijiaoError("a 挂", code="ZJ-4001", backend="a"))
        handoff = BrowserHandoff(capability="list_courses", reason="API 不可用",
                                 target_url="https://zjy2.icve.com.cn/", ocs_projects=["zjy"],
                                 steps=["① 装脚本"])
        c = make_fake_backend("c", result=handoff)
        tools = Tools(router=router_with(a, c), session=LoginState())
        got = tools.list_courses()
        self.assertTrue(is_handoff(got))
        self.assertEqual(got[0].target_url, "https://zjy2.icve.com.cn/")
        self.assertEqual(tools.last_trace.chosen, "c")

    def test_handoff_passthrough_for_single_dto_tools(self):
        """单值返回的能力（progress / detail / learning）也必须透传交接计划。"""
        handoff = BrowserHandoff(capability="get_course_progress", reason="API 不可用",
                                 target_url="https://ai.icve.com.cn/", ocs_projects=["icve"])
        a = make_fake_backend("a", error=ZhijiaoError("a 挂", code="ZJ-4001", backend="a"))
        c = make_fake_backend("c", result=handoff)
        tools = Tools(router=router_with(a, c), session=LoginState())
        for method in ("get_course_progress", "get_course_detail"):
            with self.subTest(tool=method):
                got = getattr(tools, method)(course_id="C1")
                self.assertIsInstance(got, BrowserHandoff, f"{method} 吞掉了交接计划")

    def test_all_failed_raises_route_exhausted(self):
        a = make_fake_backend("a", error=ZhijiaoError("a 挂", code="ZJ-4001", backend="a"))
        b = make_fake_backend("b", error=ZhijiaoError("b 挂", code="ZJ-4001", backend="b"))
        tools = Tools(router=router_with(a, b), session=fake_session())
        with self.assertRaises(RouteExhausted) as ctx:
            tools.get_results(course_id="C1")
        self.assertEqual(len(ctx.exception.details["attempts"]), 2)


class TestConvenienceMethods(unittest.TestCase):
    """基于 7 个能力的组合方法（不算新能力）。"""

    @classmethod
    def setUpClass(cls):
        bootstrap()

    def setUp(self):
        self.driver = _FakeBackendDriver()
        self.tools = Tools(router=router_with(self.driver.backend), session=fake_session())

    def test_find_course_by_id(self):
        c = self.tools.find_course(course_id="C2")
        self.assertEqual(c.name, "另一门课")

    def test_find_course_by_unique_name(self):
        c = self.tools.find_course(name="测试课程")
        self.assertEqual(c.course_id, "C1")

    def test_find_course_ambiguous_name_raises(self):
        with self.assertRaises(ZhijiaoError) as ctx:
            self.tools.find_course(name="课")  # 两门课名都含"课"
        self.assertEqual(ctx.exception.details["matches"].__len__(), 2)

    def test_find_course_not_found_raises(self):
        with self.assertRaises(ZhijiaoError):
            self.tools.find_course(course_id="不存在")

    def test_overview_aggregates(self):
        rows = self.tools.overview()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["total"], 3)
        self.assertEqual(rows[0]["pending"], 2)
        self.assertIn("by_file_type", rows[0])


class TestApiEnvelope(unittest.TestCase):
    """``call_tool`` 统一信封 —— Agent 侧唯一需要理解的结构。"""

    @classmethod
    def setUpClass(cls):
        bootstrap()

    def test_list_tools(self):
        self.assertEqual(set(list_tools()), set(TOOL_NAMES))

    def test_ok_envelope_shape(self):
        d = _FakeBackendDriver()
        tools = Tools(router=router_with(d.backend), session=fake_session())
        env = call_tool("list_courses", tools=tools)
        self.assertTrue(env["ok"])
        self.assertIsNone(env["error"])
        self.assertEqual(len(env["data"]), 2)
        self.assertEqual(env["data"][0]["course_id"], "C1")
        self.assertNotIn("raw", env["data"][0], "信封默认不带 raw")
        self.assertEqual(env["route"]["chosen"], "fake_a")
        self.assertEqual(env["route"]["capability"], "list_courses")

    def test_ok_envelope_can_include_raw(self):
        d = _FakeBackendDriver()
        tools = Tools(router=router_with(d.backend), session=fake_session())
        env = call_tool("list_courses", tools=tools, include_raw=True)
        self.assertTrue(env["ok"])
        self.assertIn("raw", env["data"][0])

    def test_error_envelope_for_unknown_tool(self):
        env = call_tool("no_such_tool")
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "ZJ-5002")
        self.assertIn("list_courses", env["error"]["details"]["available"])

    def test_error_envelope_carries_route(self):
        d2 = _FakeBackendDriver("fake_b")
        a = make_fake_backend("a", error=ZhijiaoError("a 挂", code="ZJ-4001", backend="a"))
        tools = Tools(router=router_with(a, d2.backend), session=fake_session())
        env = call_tool("get_results", tools=tools, course_id="C1")
        self.assertTrue(env["ok"])
        self.assertEqual(env["route"]["fallback_count"], 1)
        self.assertEqual(env["route"]["attempts"][0]["code"], "ZJ-4001")

    def test_error_envelope_on_exhaustion(self):
        a = make_fake_backend("a", error=ZhijiaoError("a 挂", code="ZJ-4001", backend="a"))
        tools = Tools(router=router_with(a), session=fake_session())
        env = call_tool("get_results", tools=tools, course_id="C1")
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "ZJ-5001")
        self.assertEqual(len(env["error"]["details"]["attempts"]), 1)
        self.assertIsNotNone(env["route"])

    def test_unexpected_exception_is_wrapped_not_leaked(self):
        """宿主永远不该收到裸异常。"""
        def boom(capability, *, session=None, **kw):
            raise KeyError("上游崩了")

        a = make_fake_backend("a", result=boom)
        tools = Tools(router=router_with(a), session=fake_session())
        env = call_tool("list_courses", tools=tools)
        # KeyError 属于上游裸异常 -> Router 视作可降级 -> 无次选 -> RouteExhausted
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "ZJ-5001")
        self.assertIn("上游崩了", str(env["error"]["details"]))

    def test_handoff_is_json_friendly(self):
        handoff = BrowserHandoff(capability="start_learning", reason="API 不可用",
                                 target_url="https://zjy2.icve.com.cn/", ocs_projects=["zjy"],
                                 steps=["① 装脚本"])
        a = make_fake_backend("a", error=ZhijiaoError("a 挂", code="ZJ-4001", backend="a"))
        c = make_fake_backend("c", result=handoff)
        tools = Tools(router=router_with(a, c), session=LoginState())
        env = call_tool("start_learning", tools=tools, course_id="C1", mode="progress")
        self.assertTrue(env["ok"])
        self.assertIsInstance(env["data"], dict)
        self.assertEqual(env["data"]["capability"], "start_learning")
        import json

        json.dumps(env, ensure_ascii=False)  # 必须可 JSON 序列化


class TestRealBackendsWithoutCredential(unittest.TestCase):
    """真实 Backend + 无凭据：错误链路必须统一、可读，且**不发网络请求**。"""

    @classmethod
    def setUpClass(cls):
        cls.st = bootstrap()
        cls.tools = make_tools(settings=cls.st)

    def test_no_credential_gives_auth_error_envelope(self):
        env = call_tool("list_courses", tools=self.tools, session=LoginState())
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "ZJ-2001")
        self.assertEqual(env["error"]["type"], "AuthError")
        self.assertFalse(env["error"]["retryable"])
        blob = env["error"]["message"] + str(env["error"]["details"])
        self.assertIn("ZJ_SSO_TOKEN", blob, "错误提示必须告诉使用者怎么提供凭据")

    def test_auth_error_is_not_retried_on_other_backends(self):
        """缺凭据时不该去试 mooc-work-answer / ocsjs —— 换后端也一样没凭据。"""
        env = call_tool("list_courses", tools=self.tools, session=LoginState())
        attempts = env["route"]["attempts"]
        self.assertEqual([a["backend"] for a in attempts], ["icve_toolkit"])
        self.assertEqual(env["route"]["chosen"], None)

    def test_attendance_reports_skipped_placeholder_in_trace(self):
        """zjy_toolkit 在真实路由链里被跳过时，原因必须出现在轨迹中（透明降级）。

        全程离线：用**真实的** ZJY 占位后端 + **真实的** OCS 兜底后端，
        验证"声明支持但当前不可用"不会被静默吞掉，而是带原因落入轨迹。
        """
        from zhijiao.backends import build_backends
        from zhijiao.router import Router

        real = build_backends(force=True)
        r = Router(
            bootstrap(),
            backends={"zjy_toolkit": real["zjy_toolkit"], "ocsjs": real["ocsjs"]},
            routing_table={Capability.GET_ATTENDANCE: ["zjy_toolkit", "ocsjs"]},
        )
        tools = Tools(router=r, session=LoginState())
        got = tools.get_attendance(course_id="C1", course_info_id="I1", class_id="K1")

        self.assertTrue(is_handoff(got), "API 全不可用时应由 OCS 交出浏览器计划")
        trace = tools.last_trace
        self.assertEqual([x.backend for x in trace.attempts], ["zjy_toolkit", "ocsjs"])
        skipped = trace.attempts[0]
        self.assertTrue(skipped.skipped)
        self.assertEqual(skipped.code, "ZJ-3001")
        self.assertIn("README", skipped.message)
        self.assertEqual(trace.chosen, "ocsjs")
        self.assertEqual(got[0].ocs_projects, ["zjy"])

    def test_attendance_needs_credential_too(self):
        env = call_tool("get_attendance", tools=self.tools, session=LoginState(),
                        course_id="C1")
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["code"], "ZJ-2001")

    def test_describe_tools_reports_route_chains(self):
        d = describe_tools(self.st)
        self.assertEqual(set(d["tools"]), set(TOOL_NAMES))
        for cap in Capability:
            with self.subTest(capability=cap.value):
                self.assertIn(cap.value, d["capabilities"])
                self.assertEqual(d["capabilities"][cap.value]["declared"][-1], "ocsjs")

    def test_health_snapshot(self):
        rows = health(self.st)
        by_id = {r["backend"]: r for r in rows}
        self.assertEqual(len(rows), 4)
        self.assertTrue(by_id["icve_toolkit"]["available"])
        self.assertFalse(by_id["zjy_toolkit"]["available"])
        self.assertIn("reason", by_id["zjy_toolkit"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

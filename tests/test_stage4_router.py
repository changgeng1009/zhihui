"""阶段 4 测试：路由决策（「自动判断当前任务应该调用哪个项目」）。

用可编程的假 Backend 隔离验证**机制本身**，不依赖真凭据：
* 优先级顺序、能力过滤、可用性过滤；
* 降级链：可降级错误继续、不可降级错误立即中止；
* 强制指定后端 + 关闭降级；
* 全部失败时 ``RouteExhausted`` 携带每一次尝试的明细；
* 路由轨迹（``RouteTrace``）如实记录被跳过的后端与原因；
* 真实路由表的结构断言。
"""

from __future__ import annotations

import unittest

from _common import bootstrap, fake_session, make_fake_backend, router_with

from zhijiao.backends import ALL_CAPABILITIES, Capability
from zhijiao.errors import (
    AuthError,
    BackendUnavailable,
    CapabilityNotSupported,
    CredentialRejected,
    ConfigError,
    RouteExhausted,
    UpstreamError,
    UpstreamTransientError,
)
from zhijiao.log import RouteTrace
from zhijiao.router import ROUTING_TABLE, Router
from zhijiao.session import LoginState


class TestRoutingTableShape(unittest.TestCase):
    """路由表本身的结构契约。"""

    @classmethod
    def setUpClass(cls):
        bootstrap()

    def test_covers_all_capabilities(self):
        self.assertEqual(set(ROUTING_TABLE), set(Capability))

    def test_every_capability_ends_with_ocsjs(self):
        """「API 不可用时，可将 OCS 作为浏览器端兜底方案」—— 末位必须是 ocsjs。"""
        for cap, chain in ROUTING_TABLE.items():
            with self.subTest(capability=cap.value):
                self.assertEqual(chain[-1], "ocsjs")

    def test_attendance_route_includes_zjy_parity_slot(self):
        self.assertEqual(
            ROUTING_TABLE[Capability.GET_ATTENDANCE],
            ["icve_toolkit", "zjy_toolkit", "ocsjs"],
        )

    def test_icve_is_primary_for_every_capability(self):
        """主选全部落在 ICVE_Toolkit —— 它是唯一覆盖全能力的本地后端。"""
        for cap, chain in ROUTING_TABLE.items():
            with self.subTest(capability=cap.value):
                self.assertEqual(chain[0], "icve_toolkit")

    def test_no_duplicates(self):
        for cap, chain in ROUTING_TABLE.items():
            with self.subTest(capability=cap.value):
                self.assertEqual(len(chain), len(set(chain)))


class TestRouterBasics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bootstrap()

    def test_route_returns_declared_order(self):
        r = Router(bootstrap(), backends={})
        self.assertEqual(r.route(Capability.LIST_COURSES), ROUTING_TABLE[Capability.LIST_COURSES])
        for cap in Capability:
            with self.subTest(capability=cap.value):
                self.assertEqual(r.route(cap), ROUTING_TABLE[cap])

    def test_plan_filters_unregistered_and_unsupported(self):
        a = make_fake_backend("a", capabilities=frozenset({Capability.LIST_COURSES}))
        b = make_fake_backend("b", capabilities=frozenset({Capability.GET_RESULTS}))
        r = router_with(a, b)
        self.assertEqual(r.plan(Capability.LIST_COURSES), ["a"])
        self.assertEqual(r.plan(Capability.GET_RESULTS), ["b"])

    def test_first_candidate_is_used(self):
        calls = []
        a = make_fake_backend("a", result=[1], calls=calls)
        b = make_fake_backend("b", result=[2], calls=calls)
        r = router_with(a, b)
        res = r.invoke(Capability.LIST_COURSES, session=fake_session())
        self.assertEqual(res.value, [1])
        self.assertEqual(res.backend, "a")
        self.assertEqual([c["backend"] for c in calls], ["a"], "成功后不应再调用次选")

    def test_invoke_value_shortcut(self):
        a = make_fake_backend("a", result="X")
        r = router_with(a)
        self.assertEqual(r.invoke_value(Capability.LIST_COURSES, session=fake_session()), "X")


class TestFallbackBehaviour(unittest.TestCase):
    """降级链 —— 本项目「自动选项目」的核心。"""

    @classmethod
    def setUpClass(cls):
        bootstrap()

    def _two(self, first_error):
        calls = []
        a = make_fake_backend("a", error=first_error, calls=calls)
        b = make_fake_backend("b", result="B-result", calls=calls)
        return router_with(a, b), calls

    def test_backend_unavailable_falls_back(self):
        r, calls = self._two(BackendUnavailable("a 挂了", backend="a"))
        res = r.invoke(Capability.LIST_COURSES, session=fake_session())
        self.assertEqual(res.value, "B-result")
        self.assertEqual(res.backend, "b")
        self.assertEqual([c["backend"] for c in calls], ["a", "b"])
        self.assertEqual(res.trace.fallback_count, 1)

    def test_transient_upstream_error_falls_back(self):
        r, _ = self._two(UpstreamTransientError("超时", backend="a"))
        self.assertEqual(r.invoke(Capability.LIST_COURSES, session=fake_session()).backend, "b")

    def test_upstream_error_falls_back(self):
        r, _ = self._two(UpstreamError("结构变了", backend="a"))
        self.assertEqual(r.invoke(Capability.LIST_COURSES, session=fake_session()).backend, "b")

    def test_bare_exception_falls_back(self):
        """上游裸异常也要被当作可降级（保守策略）。"""
        r, _ = self._two(RuntimeError("上游没归一化的异常"))
        self.assertEqual(r.invoke(Capability.LIST_COURSES, session=fake_session()).backend, "b")

    def test_auth_error_does_not_fall_back(self):
        """凭据问题换后端也没用 —— 必须立即抛出，且次选不能被调用。"""
        r, calls = self._two(AuthError("未登录", backend="a"))
        with self.assertRaises(AuthError):
            r.invoke(Capability.LIST_COURSES, session=fake_session())
        self.assertEqual([c["backend"] for c in calls], ["a"], "AuthError 后不应尝试次选")

    def test_credential_rejected_does_not_fall_back(self):
        r, calls = self._two(CredentialRejected("账密错误", backend="a"))
        with self.assertRaises(CredentialRejected):
            r.invoke(Capability.LIST_COURSES, session=fake_session())
        self.assertEqual([c["backend"] for c in calls], ["a"])

    def test_config_error_does_not_fall_back(self):
        r, calls = self._two(ConfigError("配置坏了"))
        with self.assertRaises(ConfigError):
            r.invoke(Capability.LIST_COURSES, session=fake_session())
        self.assertEqual([c["backend"] for c in calls], ["a"])

    def test_unavailable_backend_is_skipped_without_call(self):
        calls = []
        a = make_fake_backend("a", available=False, reason="缺依赖", calls=calls, result="never")
        b = make_fake_backend("b", result="B", calls=calls)
        r = router_with(a, b)
        res = r.invoke(Capability.LIST_COURSES, session=fake_session())
        self.assertEqual(res.backend, "b")
        self.assertEqual(calls, [{"backend": "b", "capability": "list_courses"}])
        skipped = [x for x in res.trace.attempts if x.skipped]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0].backend, "a")
        self.assertIn("缺依赖", skipped[0].message)

    def test_three_level_fallback_chain(self):
        """主选失败 -> 次选失败 -> 兜底成功（模拟真实 ocsjs 接管）。"""
        calls = []
        a = make_fake_backend("a", error=BackendUnavailable("a 无源码"), calls=calls)
        b = make_fake_backend("b", error=UpstreamError("b 上游变了"), calls=calls)
        c = make_fake_backend("c", result="HANDOFF", calls=calls)
        r = router_with(a, b, c)
        res = r.invoke(Capability.START_LEARNING, session=LoginState())
        self.assertEqual(res.value, "HANDOFF")
        self.assertEqual(res.backend, "c")
        self.assertEqual(res.trace.fallback_count, 2)
        self.assertEqual(len(res.trace.attempts), 3)


class TestExhaustionAndPin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bootstrap()

    def test_all_failed_raises_route_exhausted_with_details(self):
        a = make_fake_backend("a", error=BackendUnavailable("a 不可用"))
        b = make_fake_backend("b", error=BackendUnavailable("b 不可用"))
        r = router_with(a, b)
        with self.assertRaises(RouteExhausted) as ctx:
            r.invoke(Capability.LIST_COURSES, session=fake_session())
        e = ctx.exception
        self.assertEqual(e.code, "ZJ-5001")
        self.assertFalse(e.retryable)
        attempts = e.details["attempts"]
        self.assertEqual([x["backend"] for x in attempts], ["a", "b"])
        self.assertTrue(all(not x["ok"] for x in attempts))
        for x in attempts:
            self.assertIn("code", x)
            self.assertIn("message", x)

    def test_exhausted_message_lists_every_attempt(self):
        a = make_fake_backend("a", error=BackendUnavailable("原因A"))
        b = make_fake_backend("b", error=UpstreamError("原因B"))
        r = router_with(a, b)
        with self.assertRaises(RouteExhausted) as ctx:
            r.invoke(Capability.GET_RESULTS, session=fake_session())
        msg = str(ctx.exception)
        self.assertIn("原因A", msg)
        self.assertIn("原因B", msg)

    def test_capability_not_supported_degrades(self):
        a = make_fake_backend("a", capabilities=frozenset())  # 什么都不支持
        b = make_fake_backend("b", result="B")
        r = router_with(a, b)
        self.assertEqual(r.invoke(Capability.LIST_COURSES, session=fake_session()).backend, "b")

    def test_pin_backend_disables_fallback_by_default(self):
        calls = []
        a = make_fake_backend("a", error=BackendUnavailable("a 挂了"), calls=calls)
        b = make_fake_backend("b", result="B", calls=calls)
        r = router_with(a, b)
        with self.assertRaises(RouteExhausted):
            r.invoke(Capability.LIST_COURSES, session=fake_session(), backend="a")
        self.assertEqual([c["backend"] for c in calls], ["a"])

    def test_pin_backend_with_explicit_fallback(self):
        a = make_fake_backend("a", error=BackendUnavailable("a 挂了"))
        b = make_fake_backend("b", result="B")
        r = router_with(a, b)
        res = r.invoke(
            Capability.LIST_COURSES, session=fake_session(),
            backend="a", no_fallback=False,
        )
        self.assertEqual(res.backend, "b")

    def test_empty_plan_raises_config_error(self):
        a = make_fake_backend("a", capabilities=frozenset())
        r = router_with(a)
        with self.assertRaises(ConfigError):
            r.invoke(Capability.LIST_COURSES, session=fake_session())

    def test_global_fallback_switch(self):
        st = bootstrap()
        st.allow_fallback = False
        a = make_fake_backend("a", error=BackendUnavailable("a 挂了"))
        b = make_fake_backend("b", result="B")
        r = router_with(a, b)
        r.settings = st
        try:
            with self.assertRaises(RouteExhausted):
                r.invoke(Capability.LIST_COURSES, session=fake_session())
        finally:
            st.allow_fallback = True  # 复原，避免影响其他测试


class TestRouteTrace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bootstrap()

    def test_trace_records_chain_and_is_serializable(self):
        a = make_fake_backend("a", error=BackendUnavailable("a 不可用"))
        b = make_fake_backend("b", result="B")
        r = router_with(a, b)
        r.invoke(Capability.LIST_COURSES, session=fake_session())
        t = r.last_trace()
        self.assertIsInstance(t, RouteTrace)
        self.assertEqual(t.capability, "list_courses")
        self.assertEqual(t.chosen, "b")
        self.assertEqual([x.backend for x in t.attempts], ["a", "b"])
        d = t.to_dict()
        self.assertEqual(d["capability"], "list_courses")
        self.assertEqual(d["chosen"], "b")
        self.assertEqual(d["fallback_count"], 1)
        self.assertIn("total_ms", d)
        self.assertEqual(len(d["attempts"]), 2)
        self.assertIn("->", t.summary())

    def test_trace_summary_marks_failures(self):
        a = make_fake_backend("a", error=BackendUnavailable("x"))
        b = make_fake_backend("b", result="B")
        r = router_with(a, b)
        r.invoke(Capability.LIST_COURSES, session=fake_session())
        self.assertIn("a(x)", r.last_trace().summary())
        self.assertIn("b", r.last_trace().summary())


class TestRealRouterAgainstUpstreams(unittest.TestCase):
    """用**真实** Backend 实例验证路由表与能力声明的吻合度（不发网络请求）。"""

    @classmethod
    def setUpClass(cls):
        st = bootstrap()
        cls.r = Router(st)
        cls.health = {c.backend: c for c in cls.r.health()}

    def test_health_covers_four_backends(self):
        self.assertEqual(
            set(self.health),
            {"icve_toolkit", "zjy_toolkit", "mooc_work_answer", "ocsjs"},
        )

    def test_expected_availability(self):
        self.assertTrue(self.health["icve_toolkit"].available)
        self.assertTrue(self.health["mooc_work_answer"].available)
        self.assertTrue(self.health["ocsjs"].available)
        self.assertFalse(self.health["zjy_toolkit"].available)

    def test_plan_is_candidates_without_availability_filtering(self):
        """``plan`` 是候选链（声明 ∩ 支持），**刻意不含**可用性判断。

        可用性在 invoke 循环里逐项检查并记入轨迹 —— 这样
        "zjy_toolkit 因为无源码被跳过" 才会出现在轨迹里，而不是静默消失。
        """
        self.assertEqual(
            self.r.plan(Capability.GET_ATTENDANCE),
            ["icve_toolkit", "zjy_toolkit", "ocsjs"],
        )
        self.assertEqual(
            self.r.plan(Capability.LIST_COURSES),
            ["icve_toolkit", "mooc_work_answer", "ocsjs"],
        )

    def test_attemptable_excludes_unavailable(self):
        """``attemptable`` 才是"真正会被尝试"的链 —— zjy 被正确剔除。"""
        self.assertEqual(
            self.r.attemptable(Capability.GET_ATTENDANCE),
            ["icve_toolkit", "ocsjs"],
            "zjy 声明支持但不可用，不应进入可尝试链",
        )
        self.assertEqual(
            self.r.attemptable(Capability.LIST_COURSES),
            ["icve_toolkit", "mooc_work_answer", "ocsjs"],
        )

    def test_describe_exposes_three_narrowing_views(self):
        d = self.r.describe()
        att = d["capabilities"][Capability.GET_ATTENDANCE.value]
        self.assertEqual(att["declared"], ["icve_toolkit", "zjy_toolkit", "ocsjs"])
        self.assertEqual(att["candidates"], ["icve_toolkit", "zjy_toolkit", "ocsjs"])
        self.assertEqual(att["available"], ["icve_toolkit", "ocsjs"])
        self.assertEqual(len(d["backends"]), 4)
        ids = {b["backend"] for b in d["backends"]}
        self.assertEqual(ids, {"icve_toolkit", "zjy_toolkit", "mooc_work_answer", "ocsjs"})

    def test_all_capabilities_have_at_least_one_attemptable_backend(self):
        for cap in Capability:
            with self.subTest(capability=cap.value):
                self.assertTrue(
                    self.r.attemptable(cap),
                    f"能力 {cap.value} 没有任何真正可用的后端",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)

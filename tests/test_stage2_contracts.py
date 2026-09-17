"""阶段 2 测试：统一数据格式与标准化。

覆盖：
* 三域课程类型/签到状态/学习模式/进度的**字段名收敛**（上游写法千奇百怪）；
* DTO 的 ``to_dict`` 默认不外泄 ``raw``，显式要求时才带；
* 进度聚合的算术正确性；
* 浏览器交接计划的序列化。
"""

from __future__ import annotations

import unittest

from _common import bootstrap

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
    normalize_course_type,
    normalize_learning_mode,
    normalize_progress,
    normalize_sign_status,
)


class TestNormalizers(unittest.TestCase):
    """上游字段 -> 统一枚举。"""

    def test_course_type_from_upstream_markers(self):
        # ICVE_Toolkit 的 _courseType
        self.assertIs(normalize_course_type("SPOC"), CourseType.SPOC)
        self.assertIs(normalize_course_type("MOOC"), CourseType.MOOC)
        self.assertIs(normalize_course_type("RESOURCE"), CourseType.RESOURCE)
        # 大写/小写/中文/域名/数字
        self.assertIs(normalize_course_type("spoc"), CourseType.SPOC)
        self.assertIs(normalize_course_type("资源库"), CourseType.RESOURCE)
        self.assertIs(normalize_course_type("zyk"), CourseType.RESOURCE)
        self.assertIs(normalize_course_type("ai.icve.com.cn"), CourseType.MOOC)
        self.assertIs(normalize_course_type("zjy2.icve.com.cn"), CourseType.SPOC)
        # 未知/空
        self.assertIs(normalize_course_type(None), CourseType.UNKNOWN)
        self.assertIs(normalize_course_type("莫名其妙"), CourseType.UNKNOWN)
        self.assertIs(normalize_course_type(123), CourseType.UNKNOWN)

    def test_sign_status_covers_upstream_0_to_5(self):
        # ICVE README 的状态码表：0未签到 1已签到 2迟到 3请假 4病假 5事假
        expected = {
            0: SignStatus.UNSIGNED, 1: SignStatus.SIGNED, 2: SignStatus.LATE,
            3: SignStatus.LEAVE, 4: SignStatus.SICK, 5: SignStatus.ABSENT,
        }
        for code, want in expected.items():
            with self.subTest(code=code):
                self.assertIs(normalize_sign_status(code), want)
                self.assertIs(normalize_sign_status(str(code)), want)
        # 中文与英文别名
        self.assertIs(normalize_sign_status("迟到"), SignStatus.LATE)
        self.assertIs(normalize_sign_status("SIGNED"), SignStatus.SIGNED)
        self.assertIs(normalize_sign_status(None), SignStatus.UNKNOWN)
        self.assertIs(normalize_sign_status("9"), SignStatus.UNKNOWN)

    def test_sign_status_text_is_chinese(self):
        self.assertEqual(SignStatus.SIGNED.text, "已签到")
        self.assertEqual(SignStatus.ABSENT.text, "事假")
        self.assertEqual(SignStatus.UNKNOWN.text, "未知")

    def test_learning_mode_aliases(self):
        self.assertIs(normalize_learning_mode("progress"), LearningMode.PROGRESS)
        self.assertIs(normalize_learning_mode("答题"), LearningMode.ANSWER)
        self.assertIs(normalize_learning_mode("discussion"), LearningMode.DISCUSSION)
        self.assertIs(normalize_learning_mode("全部"), LearningMode.ALL)
        self.assertIs(normalize_learning_mode("不存在"), LearningMode.PROGRESS)  # 默认值

    def test_progress_variants(self):
        cases = [
            (100, 100.0), ("100", 100.0), ("100%", 100.0), (0, 0.0),
            (None, 0.0), ("", 0.0), ("-", 0.0), (55.5, 55.5),
            ("55.5%", 55.5),
            (120, 100.0), (-3, 0.0),        # 越界钳制
            (True, 0.0),                    # bool 不算数字
        ]
        for raw, want in cases:
            with self.subTest(raw=raw):
                self.assertAlmostEqual(normalize_progress(raw), want, places=3)

    def test_progress_never_inflates_small_values(self):
        """回归守卫：绝对不能把 0–1 当成"归一化写法"再乘 100。

        上游 ``speed`` 一律是 0–100。若把小值放大，``speed=1``（刚 1%）会被算成
        100%，这门课会被 ``list_unfinished_tasks`` 当成已完成而跳过 —— 静默丢任务。
        """
        for raw in (0.01, 0.1, 0.5, 0.9, 1, 1.0, "1", "0.5"):
            with self.subTest(raw=raw):
                got = normalize_progress(raw)
                self.assertLess(got, 100.0, f"{raw!r} 被错误放大成 {got}")
                self.assertAlmostEqual(got, float(raw), places=3)


class TestDtoSerialization(unittest.TestCase):
    """DTO 序列化契约。"""

    @classmethod
    def setUpClass(cls):
        bootstrap()

    def _course(self):
        return Course(
            course_id="C1", course_info_id="I1", class_id="K1",
            name="测试课程", course_type=CourseType.SPOC,
            backend="icve_toolkit", raw={"secret": "raw-payload"},
        )

    def test_raw_hidden_by_default(self):
        d = self._course().to_dict()
        self.assertNotIn("raw", d, "默认输出不应外泄上游原始 payload")
        self.assertEqual(d["course_type"], "SPOC", "枚举应序列化为字符串值")
        self.assertEqual(d["name"], "测试课程")

    def test_raw_included_on_request(self):
        d = self._course().to_dict(include_raw=True)
        self.assertIn("raw", d)
        self.assertEqual(d["raw"]["secret"], "raw-payload")

    def test_course_key_is_dedup_key(self):
        self.assertEqual(self._course().key, "C1|I1|K1")

    def test_enum_serializes_to_value(self):
        rec = AttendanceRecord(sign_id="s1", status=SignStatus.LATE)
        d = rec.to_dict()
        self.assertEqual(d["status"], "LATE")
        self.assertEqual(rec.status_text, "迟到")

    def test_exam_score_text_handles_missing(self):
        self.assertEqual(ExamResult(exam_id="e1", score=None).score_text, "-")
        self.assertEqual(ExamResult(exam_id="e1", score=88.0).score_text, "88")
        self.assertEqual(ExamResult(exam_id="e1", score=88.5).score_text, "88.5")

    def test_attendance_default_hides_raw(self):
        rec = AttendanceRecord(sign_id="s1", raw={"a": 1})
        self.assertNotIn("raw", rec.to_dict())
        self.assertIn("raw", rec.to_dict(include_raw=True))


class TestProgressAggregation(unittest.TestCase):
    """进度聚合是统一层的纯计算职责。"""

    def test_counts_and_percent(self):
        course = Course(course_id="C1", course_info_id="I1", name="X")
        nodes = [
            TaskNode(id="1", name="a", file_type="video", progress=100, finished=True),
            TaskNode(id="2", name="b", file_type="video", progress=100, finished=True),
            TaskNode(id="3", name="c", file_type="ppt", progress=50, finished=False),
            TaskNode(id="4", name="d", file_type="ppt", progress=0, finished=False),
        ]
        p = CourseProgress.from_nodes(course, nodes, backend="icve_toolkit")
        self.assertEqual(p.total, 4)
        self.assertEqual(p.finished, 2)
        self.assertAlmostEqual(p.percent, 50.0)
        self.assertEqual(p.by_file_type["video"], {"total": 2, "finished": 2})
        self.assertEqual(p.by_file_type["ppt"], {"total": 2, "finished": 0})
        d = p.to_dict()
        self.assertEqual(d["percent"], 50.0)
        self.assertNotIn("raw", d)

    def test_empty_course_does_not_divide_by_zero(self):
        p = CourseProgress.from_nodes(Course(course_id="C", course_info_id="I", name="X"), [])
        self.assertEqual(p.total, 0)
        self.assertAlmostEqual(p.percent, 0.0)

    def test_blank_file_type_bucketed(self):
        course = Course(course_id="C", course_info_id="I", name="X")
        p = CourseProgress.from_nodes(course, [TaskNode(id="1", name="a")])
        self.assertIn("(空)", p.by_file_type)


class TestTaskNodeTree(unittest.TestCase):
    def test_flatten_leaves_only(self):
        leaf1 = TaskNode(id="1", name="l1")
        leaf2 = TaskNode(id="2", name="l2")
        root = TaskNode(id="r", name="root", is_container=True, children=[leaf1, leaf2])
        self.assertEqual(len(root.flatten()), 3)
        self.assertEqual([n.id for n in root.flatten(leaves_only=True)], ["1", "2"])

    def test_course_detail_to_dict_has_stats(self):
        course = Course(course_id="C", course_info_id="I", name="X")
        detail = CourseDetail(
            course=course,
            nodes=[TaskNode(id="1", name="a", progress=100, finished=True), TaskNode(id="2", name="b")],
            backend="icve_toolkit",
        )
        d = detail.to_dict()
        self.assertEqual(d["total"], 2)
        self.assertEqual(d["finished"], 1)
        self.assertEqual(d["backend"], "icve_toolkit")


class TestDtoSelfHealing(unittest.TestCase):
    """DTO 应在 ``__post_init__`` 自愈 —— 无论哪个 Adapter 传来什么写法。"""

    @classmethod
    def setUpClass(cls):
        bootstrap()

    def test_course_normalizes_type_and_str_fields(self):
        c = Course(course_id=1, course_info_id=None, name=None, course_type="resource")
        self.assertIs(c.course_type, CourseType.RESOURCE)
        self.assertEqual(c.course_id, "1")
        self.assertEqual(c.course_info_id, "")
        self.assertEqual(c.name, "")

    def test_task_node_normalizes_progress_and_finished(self):
        n = TaskNode(id="1", name="a", progress="100%")
        self.assertAlmostEqual(n.progress, 100.0)
        self.assertTrue(n.finished, "100% 必然意味着已完成")

    def test_task_node_keeps_explicit_finished_without_inflating_progress(self):
        n = TaskNode(id="1", name="a", progress=30, finished=True)
        self.assertTrue(n.finished)
        self.assertAlmostEqual(n.progress, 30.0, msg="不应为了自洽而把进度改成 100")

    def test_task_node_small_progress_stays_unfinished(self):
        n = TaskNode(id="1", name="a", progress=1)
        self.assertFalse(n.finished, "1% 不能被当成 100%")

    def test_attendance_normalizes_numeric_status(self):
        rec = AttendanceRecord(sign_id="s1", status=1)   # 上游常返回数字
        self.assertIs(rec.status, SignStatus.SIGNED)
        self.assertEqual(rec.status_text, "已签到")

    def test_learning_report_normalizes_string_mode(self):
        rep = LearningReport(course=Course(course_id="C", course_info_id="I", name="X"),
                             mode="discussion")
        self.assertIs(rep.mode, LearningMode.DISCUSSION)
        self.assertEqual(rep.to_dict()["mode"], "discussion")


class TestBrowserHandoff(unittest.TestCase):
    def test_serialization(self):
        h = BrowserHandoff(
            capability="start_learning",
            reason="API 不可用",
            target_url="https://zjy2.icve.com.cn/",
            ocs_projects=["zjy"],
            scenario="study",
            params={"courseId": "C1"},
            steps=["① 装用户脚本", "② 打开页面"],
        )
        d = h.to_dict()
        self.assertEqual(d["capability"], "start_learning")
        self.assertEqual(d["ocs_projects"], ["zjy"])
        self.assertEqual(len(d["steps"]), 2)
        self.assertNotIn("raw", d)


if __name__ == "__main__":
    unittest.main(verbosity=2)

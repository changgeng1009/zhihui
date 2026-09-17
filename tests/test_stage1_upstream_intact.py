"""阶段 1 测试：上游完整性（「不重写、不大改原项目核心代码」的硬性守卫）。

判据不是"我觉得没改"，而是**逐文件 sha256 比对基线清单**：

1. 四个上游目录存在且非空；
2. 每个上游的许可证文件情况与记录一致；
3. ``upstreams/`` 下**所有非 .git 文件**的 sha256 与 ``tests/upstream_manifest.json`` 完全一致。

第 3 条是关键：任何对上游文件的增/删/改都会让本测试变红。
基线清单由 ``python run_tests.py --snapshot`` 生成。
另外附带一个"尽力而为"的 ``git status --porcelain`` 检查（git 不可用则跳过，不算失败）。
"""

from __future__ import annotations

import shutil
import subprocess
import unittest

from _common import (
    UPSTREAM_DIRS,
    UPSTREAMS,
    diff_is_empty,
    diff_manifest,
    diff_summary,
    load_manifest,
)


class TestUpstreamIntact(unittest.TestCase):
    """上游必须始终是原样 clone。"""

    @classmethod
    def setUpClass(cls):
        from _common import bootstrap

        bootstrap()

    # ------------------------------------------------------------------ #
    def test_all_upstream_dirs_exist(self):
        for bid, dirname in UPSTREAM_DIRS.items():
            with self.subTest(upstream=bid):
                d = UPSTREAMS / dirname
                self.assertTrue(d.is_dir(), f"上游目录缺失: {d}")
                self.assertTrue(any(d.iterdir()), f"上游目录为空: {d}")

    def test_manifest_baseline_exists(self):
        m = load_manifest()
        self.assertTrue(m, "缺少基线清单，请先运行: python run_tests.py --snapshot")
        for bid in UPSTREAM_DIRS:
            with self.subTest(upstream=bid):
                self.assertIn(bid, m)
                self.assertGreater(m[bid]["count"], 0)

    def test_no_upstream_file_modified(self):
        diff = diff_manifest()
        self.assertTrue(
            diff_is_empty(diff),
            "上游文件被改动（违反『不重写、不大改原项目核心代码』）：" + diff_summary(diff),
        )

    def test_license_files_present_as_recorded(self):
        """许可证文件的存在性必须与 `LICENSES_AND_COMPLIANCE.md` 的记载一致。"""
        expectations = {
            "icve_toolkit": True,   # PolyForm Noncommercial 1.0.0
            "zjy_toolkit": False,   # 仅 README
            "mooc_work_answer": False,  # 无 LICENSE 文件
            "ocsjs": True,          # MIT
        }
        for bid, should_exist in expectations.items():
            with self.subTest(upstream=bid):
                d = UPSTREAMS / UPSTREAM_DIRS[bid]
                found = list(d.glob("LICENSE*")) + list(d.glob("COPYING*"))
                self.assertEqual(
                    bool(found), should_exist,
                    f"{bid} 的许可证文件情况与合规文档不符（found={[p.name for p in found]}）",
                )

    def test_zjy_toolkit_really_has_no_source(self):
        """ZJY-Toolkit 只有 README —— 这是它被实现为『远端占位』的事实依据。"""
        d = UPSTREAMS / UPSTREAM_DIRS["zjy_toolkit"]
        py_files = [p for p in d.rglob("*.py") if ".git" not in p.parts]
        js_files = [p for p in d.rglob("*.js") if ".git" not in p.parts]
        self.assertEqual(py_files, [], f"ZJY-Toolkit 出现了 Python 源码: {py_files}")
        self.assertEqual(js_files, [], f"ZJY-Toolkit 出现了 JS 源码: {js_files}")

    def test_git_worktree_clean_if_git_available(self):
        """尽力而为：如果 git 可用，四个上游的 working tree 必须干净。"""
        git = shutil.which("git")
        if not git:
            self.skipTest("git 不在 PATH 上，跳过（已由 sha256 清单覆盖）")
        dirty = {}
        for bid, dirname in UPSTREAM_DIRS.items():
            d = UPSTREAMS / dirname
            if not (d / ".git").exists():
                continue
            try:
                r = subprocess.run(
                    [git, "-C", str(d), "status", "--porcelain"],
                    capture_output=True, text=True, timeout=20,
                )
            except (OSError, subprocess.SubprocessError) as e:  # pragma: no cover
                self.skipTest(f"调用 git 失败: {e}")
            if r.stdout.strip():
                dirty[bid] = r.stdout.strip().splitlines()[:5]
        self.assertEqual(dirty, {}, f"上游 working tree 不干净: {dirty}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

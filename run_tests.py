#!/usr/bin/env python
"""一键运行全部阶段测试。

用法::

    python run_tests.py                # 跑全部阶段
    python run_tests.py --stage 3      # 只跑阶段 3
    python run_tests.py --verbose      # 打印每个用例
    python run_tests.py --snapshot     # 重建上游完整性基线（仅在确认上游确为原样 clone 后使用）

阶段划分（与任务要求的"每完成一个阶段都运行测试"对应）

======  ================================================================
阶段    验证内容
======  ================================================================
1       上游完整性：四个仓库逐文件 sha256 与基线一致（未被改动）
2       统一数据格式：字段名收敛、DTO 序列化、进度聚合
3       Adapter 契约：真正 import 上游并断言方法/签名未变
4       路由决策：优先级、降级、不可降级错误、轨迹
5       Tool Layer 端到端：7 个能力 + 统一信封 + 兜底透传
======  ================================================================

全部测试**离线可跑**：不访问真实账号、不向线上接口发请求、不写入上游目录。
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TESTS = ROOT / "tests"

STAGES = {
    1: ("上游完整性", "test_stage1_upstream_intact"),
    2: ("统一数据格式", "test_stage2_contracts"),
    3: ("Adapter 契约", "test_stage3_backends_import"),
    4: ("路由决策", "test_stage4_router"),
    5: ("Tool Layer 端到端", "test_stage5_tools_e2e"),
    6: ("登录通道", "test_stage6_login"),
    7: ("副作用收容", "test_stage7_containment"),
}


def reexec_with_project_venv() -> None:
    """若项目内 `.venv` 存在且当前不是它，就用它重新执行本脚本。

    这样无论用哪个 Python 调用（系统 python / 沙箱托管 venv / 项目 .venv），
    行为一致 —— 避免"用错解释器导致依赖找不到"这类环境问题。
    """
    import subprocess
    import sys as _sys

    if os.environ.get("ZJ_REEXEC") == "1":
        return
    venv_py = ROOT / ".venv" / "Scripts" / "python.exe"
    if not venv_py.is_file():
        return
    try:
        if Path(_sys.executable).resolve() == venv_py.resolve():
            return
    except OSError:
        return
    env = dict(os.environ, ZJ_REEXEC="1")
    raise SystemExit(
        subprocess.call([str(venv_py), str(Path(__file__).resolve()), *_sys.argv[1:]], env=env)
    )


def snapshot() -> int:
    sys.path.insert(0, str(TESTS))
    from _common import compute_manifest, write_manifest  # noqa: E402

    m = compute_manifest()
    if not any(v["count"] for v in m.values()):
        print("✗ upstreams/ 里没有文件，无法生成基线。请先 clone 四个上游。")
        return 1
    path = write_manifest()
    print(f"✓ 基线已写入 {path}")
    for bid, v in sorted(m.items()):
        print(f"  {bid:<20} {v['dir']:<20} {v['count']:>5} 个文件")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="智慧职教统一层分阶段测试")
    ap.add_argument("--stage", type=int, choices=sorted(STAGES), help="只跑指定阶段")
    ap.add_argument("--verbose", "-v", action="store_true", help="逐用例输出")
    ap.add_argument("--snapshot", action="store_true", help="重建上游完整性基线")
    args = ap.parse_args()

    if args.snapshot:
        reexec_with_project_venv()
        return snapshot()

    reexec_with_project_venv()

    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    # 让 `import _common` 可用：top-level dir 设为 tests/
    sys.path.insert(0, str(TESTS))

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    stages = [args.stage] if args.stage else sorted(STAGES)
    missing = []
    for stage in stages:
        label, module = STAGES[stage]
        try:
            suite.addTests(loader.loadTestsFromName(module))
        except Exception as e:  # noqa: BLE001
            missing.append((stage, label, module, e))

    print("=" * 74)
    print("智慧职教统一工具层 · 分阶段测试")
    print("=" * 74)
    for stage in stages:
        label, module = STAGES[stage]
        print(f"  阶段 {stage}  {label:<18} {module}")
    print("-" * 74)

    if missing:
        for stage, label, module, e in missing:
            print(f"✗ 阶段 {stage} ({label}) 无法加载 {module}: {e}")
        return 2

    verbosity = 2 if args.verbose else 1
    # 统一 logger 默认写到 stderr，会与 unittest 进度交错；测试期静音它
    from zhijiao.log import configure_logging

    configure_logging("CRITICAL", jsonl_path=None, force=True)

    runner = unittest.TextTestRunner(verbosity=verbosity, stream=sys.stdout)
    with redirect_stderr(io.StringIO()):
        result = runner.run(suite)

    print("-" * 74)
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    skipped = len(result.skipped)
    if result.wasSuccessful():
        print(f"✓ 全部通过：{total} 个用例（跳过 {skipped} 个）")
        print("  上游未被改动 · 契约完好 · 路由与降级符合预期")
        return 0
    print(f"✗ 失败：{failed} / {total} 个用例（跳过 {skipped} 个）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

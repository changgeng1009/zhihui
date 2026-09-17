#!/usr/bin/env python
"""智慧职教统一工具层 —— 演示 / 自检脚本（**全程离线**）。

用途：不开真实账号，就能看到四件事：

1. 四个 Backend 的可用性（谁在岗、谁为什么不在岗）；
2. 每个统一能力会走哪条链（三层视图：declared / candidates / available）；
3. 路由与降级机制的真实行为（用假 Backend 编排失败，看它怎么逐级下沉到 OCS 兜底）；
4. Agent 侧统一信封 ``call_tool`` 的返回格式。

用法::

    python demo.py                 # 全部
    python demo.py --health        # 只看后端可用性
    python demo.py --routes        # 只看路由表
    python demo.py --demo          # 只看降级演示

真实联调（需要凭据，会访问线上接口）::

    set ZJ_SSO_TOKEN=<你的 SSO Token>
    python -c "import zhijiao; print(zhijiao.health()); print(zhijiao.list_courses())"
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 演示默认把统一日志压到 ERROR，避免与演示输出交错；
# 想看完整路由日志：set ZJ_LOG_LEVEL=INFO，日志同时落在 state/logs/。
os.environ.setdefault("ZJ_LOG_LEVEL", "ERROR")


def reexec_with_project_venv() -> None:
    """若项目内 `.venv` 存在且当前不是它，就用它重新执行本脚本。"""
    import subprocess

    if os.environ.get("ZJ_REEXEC") == "1":
        return
    venv_py = ROOT / ".venv" / "Scripts" / "python.exe"
    if not venv_py.is_file():
        return
    try:
        if Path(sys.executable).resolve() == venv_py.resolve():
            return
    except OSError:
        return
    env = dict(os.environ, ZJ_REEXEC="1")
    raise SystemExit(
        subprocess.call([str(venv_py), str(Path(__file__).resolve()), *sys.argv[1:]], env=env)
    )


def hr(title: str = "") -> None:
    print("\n" + "=" * 74)
    if title:
        print(title)
        print("=" * 74)


def show_health() -> None:
    hr("① 后端可用性（不跑网络，只看目录/依赖/能力声明）")
    import zhijiao

    rows = zhijiao.health()
    for r in rows:
        mark = "✓ 在岗" if r["available"] else "✗ 不在岗"
        print(f"\n  {mark}  {r['backend']}")
        print(f"       能力({len(r['capabilities'])}): {', '.join(r['capabilities']) or '(无)'}")
        if r.get("reason"):
            print(f"       原因: {r['reason']}")
        extra = {k: v for k, v in (r.get("extra") or {}).items() if v}
        if extra:
            for k, v in extra.items():
                print(f"       {k}: {v}")


def show_routes() -> None:
    hr("② 统一能力 → 项目 路由表（三层视图）")
    import zhijiao

    d = zhijiao.describe_tools()
    print("\n  「declared」= 路由表声明  「candidates」= 去掉不支持  「available」= 当前真能用")
    print(f"\n  {'能力':<22} {'declared':<40} {'available'}")
    print("  " + "-" * 72)
    for cap in zhijiao.Capability:
        row = d["capabilities"][cap.value]
        dec = " > ".join(row["declared"])
        ava = " > ".join(row["available"]) or "(无)"
        print(f"  {cap.value:<22} {dec:<40} {ava}")

    print("\n  后端元信息：")
    for info in zhijiao.backend_infos():
        print(f"\n    {info['id']}  [{info['kind']}]")
        print(f"      名称: {info['name']}")
        print(f"      许可: {info['license']}")
        print(f"      仓库: {info['repo']}")
        if info.get("notes"):
            print(f"      备注: {info['notes']}")


def show_fallback_demo() -> None:
    """用假 Backend 演示主选失败 → 次选失败 → OCS 兜底。"""
    hr("③ 路由与降级演示（假 Backend，离线）")
    from zhijiao.backends import ALL_CAPABILITIES, BackendInfo, BackendKind, BaseBackend, Capability
    from zhijiao.contracts import BrowserHandoff, Course, CourseProgress, TaskNode
    from zhijiao.errors import BackendUnavailable, UpstreamError
    from zhijiao.router import Router
    from zhijiao.session import LoginState
    from zhijiao.tools import Tools

    def fake(bid: str, *, error=None, result=None, available=True, reason=""):
        class _F(BaseBackend):
            info = BackendInfo(
                id=bid, name=f"假后端 {bid}", kind=BackendKind.API, license="demo",
                upstream_dir=bid, capabilities=frozenset(ALL_CAPABILITIES),
            )

            @property
            def available(self):  # noqa: D102
                return available

            def unavailable_reason(self):  # noqa: D102
                return reason

            def call(self, capability, *, session=None, **kw):  # noqa: D102
                if error is not None:
                    raise error
                # 直接给值 / 给一个按能力计算的函数，两种都支持
                return result(capability, session=session, **kw) if callable(result) else result

        return _F()

    course = Course(course_id="C-1", course_info_id="I-1", class_id="K-1",
                    name="示例课程", course_type="SPOC")

    scenarios = [
        (
            "场景 A：主选正常",
            fake("icve_toolkit", result=lambda c, **k: CourseProgress.from_nodes(
                course,
                [TaskNode(id="1", name="视频", file_type="video", progress=100),
                 TaskNode(id="2", name="课件", file_type="ppt", progress=40)],
                backend="icve_toolkit",
            )),
            fake("mooc_work_answer", result="不该被用到"),
            fake("ocsjs", result="不该被用到"),
        ),
        (
            "场景 B：主选挂了(可降级) → 次选接手",
            fake("icve_toolkit", error=UpstreamError("上游接口结构变了", backend="icve_toolkit")),
            fake("mooc_work_answer", result=lambda c, **k: CourseProgress.from_nodes(
                course, [TaskNode(id="1", name="视频", file_type="video", progress=100)],
                backend="mooc_work_answer")),
            fake("ocsjs", result="不该被用到"),
        ),
        (
            "场景 C：两个 API 后端都挂了 → OCS 浏览器兜底",
            fake("icve_toolkit", error=UpstreamError("上游接口结构变了", backend="icve_toolkit")),
            fake("mooc_work_answer", error=BackendUnavailable("缺依赖", backend="mooc_work_answer")),
            fake("ocsjs", result=lambda c, **k: BrowserHandoff(
                capability="get_course_progress",
                reason="API 路线不可用，交由真浏览器中的 OCS 执行",
                target_url="https://zjy2.icve.com.cn/", ocs_projects=["zjy"],
                scenario="progress", params={"courseId": "C-1"},
                steps=["① 装用户脚本管理器", "② 安装 OCS 脚本", "③ 打开课程页"],
            )),
        ),
    ]

    for title, a, b, c in scenarios:
        table = {cap: ["icve_toolkit", "mooc_work_answer", "ocsjs"] for cap in Capability}
        router = Router(backends={x.info.id: x for x in (a, b, c)}, routing_table=table)
        tools = Tools(router=router, session=LoginState(sso_token="demo"))

        print(f"\n  {title}")
        print("  " + "-" * 70)
        got = tools.get_course_progress(course=course)
        for at in tools.last_trace.attempts:
            if at.skipped:
                state = "SKIP"
            elif at.ok:
                state = "OK  "
            else:
                state = "FAIL"
            print(f"     {state}  {at.backend:<20} {at.code:<9} {at.message or '—'}")
        print(f"     → 最终由 {tools.last_trace.chosen} 返回:")
        if hasattr(got, "to_dict"):
            print(f"       {got.to_dict()}")
        else:
            print(f"       {got.to_dict() if hasattr(got, 'to_dict') else got}")


def show_envelope() -> None:
    hr("④ Agent 侧统一信封（call_tool 的返回格式）")
    from zhijiao.api import call_tool
    from zhijiao.backends import ALL_CAPABILITIES, BackendInfo, BackendKind, BaseBackend, Capability
    from zhijiao.errors import AuthError
    from zhijiao.router import Router
    from zhijiao.session import LoginState

    class _Deny(BaseBackend):
        info = BackendInfo(id="icve_toolkit", name="假后端（故意缺凭据）",
                           kind=BackendKind.API, license="demo", upstream_dir="x",
                           capabilities=frozenset(ALL_CAPABILITIES))

        def call(self, capability, *, session=None, **kw):
            raise AuthError("需要登录凭据", backend="icve_toolkit",
                            details={"hint": "提供 ZJ_SSO_TOKEN"})

    table = {cap: ["icve_toolkit"] for cap in Capability}
    router = Router(backends={"icve_toolkit": _Deny()}, routing_table=table)
    env = call_tool("list_courses", tools=__import__("zhijiao.tools", fromlist=["Tools"]).Tools(
        router=router, session=LoginState()))

    import json

    print("\n  失败时：ok=false，error 带稳定错误码，route 保留完整尝试轨迹")
    print(json.dumps(env, ensure_ascii=False, indent=2))
    print("\n  关键点：route 即使失败也非空 —— 永远能看到「试过谁、为什么失败」。")


def show_login() -> None:
    """登录通道就绪状态（不实际登录、不联网）。"""
    hr("⑤ 统一登录：通道与就绪状态")
    import os as _os

    import zhijiao
    from zhijiao.api import LOGIN_CHANNELS
    from zhijiao.backends import build_backends
    from zhijiao.session import SessionStore, LoginState

    st = zhijiao.get_settings()
    backends = build_backends(st)

    print("\n  四个上游**共用同一个凭据源**：sso.icve.com.cn 的 SSO Token。")
    print("  所以只需要登录一次，所有后端各自换本域 Bearer。\n")

    print(f"  通道（channel）：{', '.join(LOGIN_CHANNELS)}")
    print(f"    password —— 账密 + 阿里云滑块，全自动")
    print(f"    browser  —— 人工在浏览器登录，本地 HTTP 回调收 token")
    print(f"    auto     —— 先试 password，依赖缺失/失败再退到 browser（默认）\n")

    b = backends.get("icve_toolkit")
    print("  通道 password 就绪检查：")
    if b is None or not b.available:
        print("    ✗ ICVE_Toolkit 后端不在岗")
    else:
        try:
            slider = b.loader.load("icve_toolkit", "slider_auto")
            missing = slider.missing_packages()
            ready = slider.deps_ready()
            print(f"    missing_packages() = {missing}")
            print(f"    deps_ready()       = {ready}")
            print("    ✓ 依赖齐备，可直接用账密自动登录" if ready
                  else f"    ✗ 缺 {missing}，zhijiao.login(channel='password') 会抛 BackendUnavailable")
        except Exception as e:  # noqa: BLE001
            print(f"    ✗ 加载 slider_auto 失败：{e}")

        print(f"\n  Chromium 内核目录（PLAYWRIGHT_BROWSERS_PATH）：")
        print(f"    {_os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '(未设置)')}")
        exes = sorted(st.browsers_dir.glob("chromium-*/chrome-win*/chrome.exe"))
        if exes:
            print(f"    内核可执行文件：{exes[0]}")
        else:
            print("    内核可执行文件：未找到（需 `python -m playwright install chromium`）")

    b2 = backends.get("mooc_work_answer")
    print(f"\n  通道 browser 就绪检查（只需 lxml，无需 Chromium）：")
    print(f"    mooc-work-answer 后端在岗：{bool(b2 and b2.available)}")

    store = SessionStore(st)
    print(f"\n  当前登录态：")
    env_state = LoginState.from_env()
    file_state = store.load()
    print(f"    环境变量 ZJ_SSO_TOKEN：{'有' if env_state else '无'}")
    print(f"    落盘 {store.path}：{'有' if file_state else '无'}")
    print(f"    结论：{'已登录' if (env_state or file_state) else '未登录 —— 需要先 zhijiao.login(...) 或设 ZJ_SSO_TOKEN'}")


def main() -> int:
    ap = argparse.ArgumentParser(description="智慧职教统一工具层 · 演示/自检")
    ap.add_argument("--health", action="store_true", help="只看后端可用性")
    ap.add_argument("--routes", action="store_true", help="只看路由表")
    ap.add_argument("--demo", action="store_true", help="只看降级演示")
    ap.add_argument("--envelope", action="store_true", help="只看统一信封")
    ap.add_argument("--login", action="store_true", help="只看登录通道就绪状态")
    args = ap.parse_args()

    reexec_with_project_venv()

    import zhijiao

    zhijiao.setup()  # 让 ZJ_LOG_LEVEL / log_jsonl / PLAYWRIGHT_BROWSERS_PATH 生效

    any_flag = args.health or args.routes or args.demo or args.envelope or args.login

    print("智慧职教统一工具层 —— Agent → Tool Layer → Router → Adapter → 上游")
    if not any_flag:
        print("（全程离线；真实联调请看 --help 里的 ZJ_SSO_TOKEN 说明）")

    if args.health or not any_flag:
        show_health()
    if args.routes or not any_flag:
        show_routes()
    if args.demo or not any_flag:
        show_fallback_demo()
    if args.envelope or not any_flag:
        show_envelope()
    if args.login or not any_flag:
        show_login()

    hr()
    print("提示：以上全部离线。真实使用有两种方式取得 SSO Token：")
    print("  ① 账密自动登录（需 Chromium 内核，本项目已内置在 browsers/）")
    print("     python -c \"import zhijiao; zhijiao.setup(); zhijiao.login('学号','密码')\"")
    print("  ② 人工浏览器回调（无需 Chromium）")
    print("     python -c \"import zhijiao; zhijiao.setup(); zhijiao.login(channel='browser')\"")
    print("  也可直接给 token：set ZJ_SSO_TOKEN=<token>")
    print("  之后：zhijiao.list_courses() / zhijiao.call_tool('get_attendance', course_id=...)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

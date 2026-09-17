#!/usr/bin/env python
"""登录智慧职教账号（交互式，密码不回显、不进命令行历史）。

用法::

    python login.py             # 交互式登录
    python login.py --force     # 已有登录态也强制重新登录
    python login.py --status    # 只看当前登录态与后端健康，不登录

登录成功后 token 写入 state/session.json（0600，只存 token 不存密码），
之后 7 个能力（list_courses / start_learning / get_attendance ...）自动带上登录态。

两条通道：
  1. 账密自动登录 —— 走上游 ICVE_Toolkit 的全自动滑块（需 Chromium，本项目已内置）
  2. 浏览器人工登录 —— 起本地回调服务，你在浏览器里手动登录（不需要 Chromium）

⚠️ 账密被拒**不要反复重试**：上游明确说明账密类拒绝重试无意义，
   且同 IP 高频尝试会触发风控惩罚。被拒后请核对账密、隔一段时间再试，或改用通道 2。
"""

from __future__ import annotations

import getpass
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def reexec_with_project_venv() -> None:
    """若项目内 `.venv` 存在且当前不是它，就用它重新执行本脚本。"""
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


def _hr(title: str = "") -> None:
    print("\n" + "=" * 62)
    if title:
        print(title)
        print("=" * 62)


def _ask(prompt: str) -> str:
    """读一行输入；Ctrl+D / 输入流关闭时礼貌退出而不是抛裸 traceback。"""
    try:
        return input(prompt).strip()
    except EOFError:
        print("\n  输入已结束（EOF / Ctrl+D），已取消登录")
        raise SystemExit(130) from None


def show_status() -> int:
    import zhijiao

    zhijiao.setup()
    state = zhijiao.load_state()
    _hr("当前登录态")
    if state.is_valid:
        who = state.nick_name or state.username or "(未知用户)"
        print(f"  ✓ 已登录：{who}")
        if state.school:
            print(f"    学校：{state.school}")
        if state.stu_id:
            print(f"    学号：{state.stu_id}")
        print(f"    凭据：{state.sso_token[:6]}...{state.sso_token[-4:] if len(state.sso_token) > 10 else '***'}")
    else:
        print("  ✗ 未登录")
        print("    运行  python login.py  开始登录")

    _hr("后端健康")
    for r in zhijiao.health():
        mark = "✓" if r["available"] else "✗"
        print(f"  {mark} {r['backend']:<22} {', '.join(r['capabilities']) or '(无能力)'}")
        if r.get("reason"):
            print(f"      原因：{r['reason']}")
    return 0


def login_interactive(force: bool) -> int:
    import zhijiao
    from zhijiao.errors import BackendUnavailable, CredentialRejected, ZhijiaoError

    st = zhijiao.setup()
    state = zhijiao.load_state()

    if state.is_valid and not force:
        _hr("已有登录态")
        print(f"  ✓ {state.nick_name or state.username or '(token 已存在)'}")
        print(f"    如需重新登录：python login.py --force")
        print(f"    直接开始用：  python demo.py --health")
        return 0

    _hr("智慧职教登录")
    print("  四个上游共用同一个 SSO Token，登录一次全部可用。\n")
    print("  通道：")
    print("    [1] 账密自动登录（约 2~4 秒；会弹出一个 Chromium 窗口过滑块）")
    print("    [2] 独立 Edge 窗口登录 ← 推荐的人工方式")
    print("        · 新起一个**独立 profile** 的 Edge 进程，与你正在用的任何 Edge 互不相干")
    print("        · 官方 SSO 页面上账号密码 / 短信 / 扫码 都能用")
    print("        · 需要 playwright（本项目 .venv 已装）")
    print("    [3] 系统默认浏览器登录（可能挤进你正在用的浏览器）")
    choice = _ask("\n  选择 [1/2/3]（回车 = 2）: ") or "2"

    try:
        if choice == "1":
            user = _ask("  学号 / 手机号: ")
            if not user:
                print("  ✗ 未输入账号，退出")
                return 1
            try:
                pwd = getpass.getpass("  密码（输入不回显）: ").strip()
            except EOFError:
                print("\n  输入已结束（EOF / Ctrl+D），已取消登录")
                return 130
            if not pwd:
                print("  ✗ 未输入密码，退出")
                return 1
            print("\n  正在自动登录（账密 → 过阿里云滑块 → SSO Token）...")
            state = zhijiao.login(user, pwd, channel="password")
        elif choice == "3":
            print("\n  正在启动本地回调服务并用系统默认浏览器打开登录页...")
            print("  （最长等 300 秒）")
            state = zhijiao.login(channel="browser", timeout=300)
        else:
            print("\n  正在启动一个**独立的 Edge**（独立 profile，不影响你现有的 Edge）...")
            print("  请在弹出的 Edge 窗口里完成登录（支持扫码 / 短信 / 账号密码），")
            print("  登录成功后页面会跳转并自动回传 token，窗口可手动关闭。")
            state = zhijiao.login(channel="edge", timeout=600)
    except CredentialRejected as e:
        print(f"\n  ✗ 登录被拒：{e.message}")
        print("  ─────────────────────────────────────────────")
        print("  上游明确说明：账密类拒绝**重试无意义**，")
        print("  且同 IP 高频尝试会触发风控惩罚 —— 请不要反复重试。")
        print("  建议：核对账密 → 隔一段时间再试，或改用通道 [2] 独立 Edge 登录。")
        return 1
    except BackendUnavailable as e:
        print(f"\n  ✗ 该通道当前不可用：{e.message}")
        if e.details.get("hint"):
            print(f"  提示：{e.details['hint']}")
        return 1
    except ZhijiaoError as e:
        print(f"\n  ✗ 登录失败 [{e.code}] {e.message}")
        if e.details:
            print(f"  详情：{e.details}")
        return 1
    except KeyboardInterrupt:
        print("\n  已取消")
        return 130

    _hr("✓ 登录成功")
    print(f"  用户：{state.nick_name or '(未取到昵称)'}")
    if state.username:
        print(f"  账号：{state.username}")
    if state.school:
        print(f"  学校：{state.school}")
    if state.stu_id:
        print(f"  学号：{state.stu_id}")
    tok = state.sso_token
    print(f"  SSO Token：{tok[:6]}...{tok[-4:] if len(tok) > 10 else '***'}（已掩码）")
    print(f"  已写入：{st.state_dir / 'session.json'}（0600，只存 token 不存密码）")

    _hr("后端状态")
    for r in zhijiao.health():
        mark = "✓" if r["available"] else "✗"
        print(f"  {mark} {r['backend']:<22} {len(r['capabilities'])} 个能力")

    _hr("下一步")
    print('  python -c "import zhijiao; zhijiao.setup(); [print(c.to_dict()) for c in zhijiao.list_courses()]"')
    print("  或者：python demo.py --health   # 看后端就绪情况")
    return 0


def main() -> int:
    reexec_with_project_venv()
    argv = [a for a in sys.argv[1:]]
    if "--status" in argv or "-s" in argv:
        return show_status()
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0
    # 直通入口：跳过菜单，直接用指定通道（便于从别处一键触发）
    if "--edge" in argv or "--browser" in argv:
        import zhijiao
        from zhijiao.errors import CredentialRejected, ZhijiaoError

        use_edge = "--edge" in argv
        st = zhijiao.setup()
        print("=" * 62)
        if use_edge:
            print("正在启动一个**独立的 Edge**（独立 profile，不影响你现有的 Edge）...")
            print("请在弹出的 Edge 窗口里完成登录（官方 SSO 页，支持扫码 / 短信 / 账号密码）。")
        else:
            print("正在用**系统默认浏览器**打开官方登录页（sso.icve.com.cn）...")
            print("请在其中完成登录 —— 支持扫码 / 短信 / 账号密码，任选。")
            print("登录成功后页面会跳转到本机地址并自动回传 token，那个标签页可直接关闭。")
        print("=" * 62)
        try:
            state = zhijiao.login(
                channel="edge" if use_edge else "browser",
                timeout=900 if use_edge else 600,
            )
        except CredentialRejected as e:
            print(f"\n✗ 未取得 token：{e.message}")
            return 1
        except ZhijiaoError as e:
            print(f"\n✗ 登录失败 [{e.code}] {e.message}")
            if e.details:
                print(f"   详情：{e.details}")
            return 1
        _hr("✓ 登录成功")
        print(f"  用户：{state.nick_name or '(未取到昵称)'}")
        if state.school:
            print(f"  学校：{state.school}")
        tok = state.sso_token
        print(f"  SSO Token：{tok[:6]}...{tok[-4:] if len(tok) > 10 else '***'}（已掩码）")
        print(f"  已写入：{st.state_dir / 'session.json'}")
        print("\n  浏览器任务完成 —— 之后所有功能都直接用 token 调 API，")
        print("  不会再打开任何浏览器。你可以继续正常使用 Edge。")
        return 0
    return login_interactive(force="--force" in argv or "-f" in argv)


if __name__ == "__main__":
    raise SystemExit(main())

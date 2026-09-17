# 智慧职教统一工具层

> ⚠️ **免责声明：本项目仅供个人学习、技术研究与交流参考，请勿用于任何商业或非法用途。** 仓库不含任何账号、凭据、cookie、题库或答案数据；上游第三方代码不入库，版权归属各自作者。使用者需自行遵守学校规定、平台服务条款与法律法规，一切使用后果由使用者自行承担。详见 [DISCLAIMER.md](DISCLAIMER.md) 与 [LICENSES_AND_COMPLIANCE.md](LICENSES_AND_COMPLIANCE.md)。

把四个开源项目整合成**一层**统一能力，对外只暴露 7 个 Tool：

```
          Agent / CLI / 你的脚本
                    │
        ┌───────────▼─────────────────────────┐
        │  Unified Tool Layer  zhijiao/tools   │  7 个稳定能力，签名固定
        └───────────┬─────────────────────────┘
                    │  统一登录态 / 配置 / 日志 / 错误 / 数据格式
        ┌───────────▼─────────────────────────┐
        │  Router              zhijiao/router  │  能力→项目优先级链 + 自动降级
        └───────────┬─────────────────────────┘
        ┌───────────┴────────┬─────────────┬──────────────┐
        ▼                    ▼             ▼              ▼
   icve_toolkit      mooc_work_answer   zjy_toolkit    ocsjs
   （API 主力）        （API 次选）      （远端占位）   （浏览器兜底）
        │                    │
        └──── sys.path 只读加载（零改造）────┐
                                            ▼
                            upstreams/ICVE_Toolkit 等四个原样 clone
```

**自写代码 7658 行**（`zhijiao/` 4983 + `tests/` 2191 + 入口脚本 484），
另有 701 行设计文档；**上游代码 0 行拷贝**。

---

## 自包含运行时（重要约定）

**本项目所有产出物、依赖、运行时都在项目目录内，不放项目外。** 这不是可选项，是硬约定：

| 资产 | 位置 | 谁来保证 |
|---|---|---|
| Python 依赖 | `.venv/` | `run_tests.py` / `demo.py` 会自动切到它（`reexec_with_project_venv`） |
| Chromium 内核（705MB） | `browsers/` | `Settings.apply_env()` 设置 `PLAYWRIGHT_BROWSERS_PATH` 指向这里 |
| 上游快照 | `upstreams/` | 阶段 1 测试逐文件 sha256 守卫 |
| 凭据 / 日志 | `state/` | 已 gitignore；`session.json` 权限 0600 |
| 技能 | `.workbuddy/skills/` | 项目级 |

playwright 默认会把浏览器下到 `%USERPROFILE%\AppData\Local\ms-playwright` ——
**本项目要求把它整体移到项目内 `browsers/`**。统一层会通过 `PLAYWRIGHT_BROWSERS_PATH`
让 playwright 去项目内找，所以换机器/换目录都不需要重新下载。
`tests/test_stage6_login.py` 里有两条测试盯着这件事：
一条断言项目外不再有 `ms-playwright`，一条真启动一次项目内 Chromium。

---

## 快速开始

```bash
# 1. 上游（本项目不修改它们，只读加载）
mkdir -p upstreams && cd upstreams
git clone https://github.com/atvkh/ICVE_Toolkit.git
git clone https://github.com/atvkh/ZJY-Toolkit.git
git clone https://github.com/11273/mooc-work-answer.git
git clone -b 4.0 https://github.com/ocsjs/ocsjs.git
cd ..

# 2. 项目内依赖（含账密自动登录所需的 playwright 全家桶）
python -m venv .venv
MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple
.venv/Scripts/python.exe -m pip install -i $MIRROR -r requirements.txt

# 3. 项目内 Chromium 内核（705MB；装完把 ms-playwright 整体移到 browsers/）
.venv/Scripts/python.exe -m playwright install chromium
mv "$LOCALAPPDATA/ms-playwright" browsers        # Git Bash；Windows 上移到项目内即可

# 4. 看现状（全程离线，不需要账号）
python demo.py              # 后端可用性 + 路由表 + 降级演示 + 统一信封 + 登录就绪
python run_tests.py         # 161 个用例，含"上游未被改动""项目外无浏览器缓存"的硬性守卫
```

### 登录（三条通道，一个凭据）

四个上游**共用同一个凭据源** —— `sso.icve.com.cn` 的 SSO Token。
所以只需要登录一次，所有后端各自换本域 Bearer（`GET {domain}/auth/passLogin?token=`）。

```python
import zhijiao
zhijiao.setup()

# ① 账密自动登录（默认 auto = 先试①，依赖缺失/失败再退②）
#    走上游 ICVE_Toolkit 的全自动滑块：账密 → 过阿里云滑块 → SSO Token
zhijiao.login("学号", "密码")
zhijiao.login("学号", "密码", channel="password")   # 强制只用①

# ② 人工浏览器回调（不需要 Chromium，只需要 lxml）
#    起本地 HTTP 服务，浏览器里手动登录，回调拿 token
zhijiao.login(channel="browser", timeout=300)

# ③ 或者直接给 token，连登录都不用跑
#    set ZJ_SSO_TOKEN=<token>
```

成功后会写入 `state/session.json`（权限 0600，只存 token 与身份，**不含密码**）；
`zhijiao.logout()` 清除。之后所有能力自动带上登录态：

```python
courses = zhijiao.list_courses()                    # 三域课程一次拉全
c = zhijiao.find_course(name="高等数学")
zhijiao.get_course_progress(course=c)               # 进度聚合
zhijiao.list_unfinished_tasks(course=c)             # 未完成课件
zhijiao.start_learning(course=c, mode="progress")   # 刷课
zhijiao.get_attendance(course=c)                    # 签到/考勤
zhijiao.get_results(course=c)                       # 作业/考试成绩
```

**注意**：账密被拒（`ZJ-2002 CredentialRejected`）**不重试、不换通道** ——
上游 README 明确说明账密类拒绝重试无意义，且同 IP 高频尝试会触发风控惩罚。

给 Agent 用字典化信封：

```python
from zhijiao import call_tool
r = call_tool("list_courses")
# {"ok": true, "data": [...], "error": null,
#  "route": {"capability": "...", "chosen": "icve_toolkit", "attempts": [...]}}
```

---

## 7 个统一能力 → 走哪个项目

| 能力 | 主选 | 次选 | 兜底 | 主选调用的上游方法 |
|---|---|---|---|---|
| `list_courses` | icve_toolkit | mooc_work_answer | ocsjs | `ZjyClient.get_my_courses()` |
| `get_course_detail` | icve_toolkit | mooc_work_answer | ocsjs | `ZjyClient.get_course_cells(include_completed=True)` |
| `get_course_progress` | icve_toolkit | mooc_work_answer | ocsjs | `get_course_cells` + `_parse_cell_speed` |
| `list_unfinished_tasks` | icve_toolkit | mooc_work_answer | ocsjs | `ZjyClient.get_course_cells(include_completed=False)` |
| `start_learning` | icve_toolkit | mooc_work_answer | ocsjs | `speed_course.run_speed_course()` |
| `get_attendance` | icve_toolkit | zjy_toolkit | ocsjs | `sign.get_signs()` |
| `get_results` | icve_toolkit | mooc_work_answer | ocsjs | `answer.get_course_exams_list()` |

完整理由见 [`docs/TOOL_BACKEND_MAP.md`](docs/TOOL_BACKEND_MAP.md)。
**没有任何一个能力需要重新实现** —— 全部映射到上游已有方法。

降级规则：`ZJ-3xxx`（后端不可用/能力不足）与 `ZJ-4xxx`（上游返回/网络）会继续下沉；
`ZJ-1xxx` 配置、`ZJ-2xxx` 鉴权、`ZJ-5xxx` 收敛结果**不降级**（换后端也没用）。

---

## 四个上游的整合结论（一句话版）

| 上游 | 结论 | 原因 |
|---|---|---|
| **ICVE_Toolkit** | **主力 Backend** | 唯一覆盖全 7 能力；`ZjyClient` 天然聚合三域；唯一有签到/考勤；刷课三分支心跳已实现 |
| **mooc-work-answer** | **次选 Backend** | `BaseAPIClient` 抽象最干净、覆盖 AI 域与资源库域；但**无签到**，刷课入口是**账号级** |
| **ZJY-Toolkit** | **远端占位（`available=False`）** | 仓库**只有 README.md**，实为托管 SaaS；无源码、无公开 API，物理上不可本地加载 |
| **ocsjs** | **浏览器兜底** | MIT；真浏览器执行；只产出**交接计划**（handoff），不抓数据 —— 在 Python 里复刻它才是"重写上游" |

---

## 目录结构

```
├── upstreams/            ① 上游只读区（git clone，本项目从不修改）
├── zhijiao/              ② 统一层（本项目唯一自写代码）
│   ├── contracts.py      统一数据格式（DTO + 枚举 + 字段名收敛）
│   ├── errors.py         统一错误树 + 稳定错误码（ZJ-1xxx…ZJ-5xxx）
│   ├── config.py         统一配置（env > config.json > 默认）+ PLAYWRIGHT_BROWSERS_PATH
│   ├── log.py            统一日志（掩码 + 路由轨迹 + JSONL）
│   ├── session.py        统一登录状态（以 SSO Token 为单一凭据源）
│   ├── router.py         能力路由（三层视图 + 降级 + 轨迹）
│   ├── tools.py          7 个统一能力门面
│   ├── api.py            字典化信封（给 Agent）+ login/logout
│   └── backends/         四个 Adapter + Backend 协议 + 隔离加载器
├── tests/                ③ 分阶段测试（161 用例，全离线）
├── .workbuddy/           ④ 项目级技能与记忆
│   ├── skills/           gitbash-sandbox-shell / windows-recycle-delete
│   └── memory/           MEMORY.md + 每日工作日志
├── .venv/                ⑤ 项目内 Python 运行时（gitignored）
├── browsers/             ⑥ 项目内 Chromium 内核 705MB（gitignored）
├── state/                ⑦ 凭据 / 日志（gitignored）
├── docs/                 设计文档
├── demo.py               离线演示 / 自检
└── run_tests.py          一键分阶段测试
```

设计文档：[`UPSTREAM_ANALYSIS.md`](docs/UPSTREAM_ANALYSIS.md)（四项目逐文件分析）、
[`INTEGRATION_ARCHITECTURE.md`](docs/INTEGRATION_ARCHITECTURE.md)（架构与目录设计）、
[`TOOL_BACKEND_MAP.md`](docs/TOOL_BACKEND_MAP.md)（Tool→Backend 路由表）。

---

## 阶段测试：怎么证明"没破坏原项目"

```bash
python run_tests.py            # 全部（会自动切到项目 .venv）
python run_tests.py --stage 3  # 只跑 Adapter 契约
python run_tests.py -v         # 逐用例
```

| 阶段 | 验证内容 |
|---|---|
| 1 上游完整性 | 四个仓库**逐文件 sha256** 与 `tests/upstream_manifest.json` 一致；附 `git status --porcelain` 尽力检查 |
| 2 统一数据格式 | 字段名收敛（含上游把状态码返回成数字的情况）、DTO 序列化与自愈、进度聚合 |
| 3 Adapter 契约 | **真正 import 上游**，逐条断言被依赖的方法与形参名仍存在 |
| 4 路由决策 | 优先级、降级、不可降级错误、`RouteExhausted` 明细、轨迹 |
| 5 Tool 端到端 | 7 个能力 + 统一信封 + 交接计划透传 |
| 6 登录通道 | 通道选择/降级、登录态落盘、**项目内 Chromium 真启动**、项目外无浏览器缓存 |

**上游一旦改签名/删方法，阶段 3 立刻变红** —— 这就是"方便以后单独升级某一个上游项目"的保障。

```bash
# 单独升级某个上游后
cd upstreams/ICVE_Toolkit && git pull && cd ../..
python run_tests.py --stage 3   # 契约是否有破坏，一眼可见
python run_tests.py --snapshot  # 确认上游确为原样后，重建完整性基线
```

---

## 已知语义差异（如实记录，不粉饰）

1. **`start_learning` 的执行报告用「前后未完成任务数差值」推导**
   ICVE 的 `run_speed_course()` 无返回值且内部 `try/except` 吞异常，无法拿到逐条结果。
   报告的 `details.source = "progress_diff"` 标注了这一点。
2. **mooc-work-answer 的 `start_learning` 是账号级**
   其 `AIMoocHandler.__init__` 内即调用 `start_courses()`，会遍历账号下所有课程。
   报告里 `details.scope = "account"` 且带 `warning`；排除课程请传 `skip_keywords="#课程A#课程B"`。
3. **mooc-work-answer 的 `get_results` 只有课程级最终成绩**（`finalScore`），
   没有逐次作业/考试的明细，`status` 标注为 `course_final_score`。需要明细请走 ICVE。
4. **`zjy_toolkit` 永远不可用**，但它会以 `skipped` + 原因出现在路由轨迹里，
   不是静默消失 —— 便于将来接入时对照。

---

## ⚠️ 许可证与合规（务必先读）

| 上游 | 许可证 | 能否商用 |
|---|---|---|
| ICVE_Toolkit | PolyForm Noncommercial 1.0.0 | ❌ 禁止 |
| ZJY-Toolkit | 无 LICENSE | ❌ 未授权 |
| mooc-work-answer | **无 LICENSE 文件**（默认保留所有权利） | ❌ 未授权 |
| ocsjs | MIT | ✅ 允许 |

**本项目仅限个人学习、研究与自用。** 不得销售、不得作为收费服务的一部分、
不得用于收费代刷或引流变现。若要公开发布，必须先移除 `mooc-work-answer`
Backend 或取得作者书面授权。详见 [`LICENSES_AND_COMPLIANCE.md`](LICENSES_AND_COMPLIANCE.md)。

平台侧：请自行确认使用行为符合所在学校规定与平台服务条款；
上游 README 明确警告**同 IP 高频尝试会触发风控**，请勿反复强行重试。

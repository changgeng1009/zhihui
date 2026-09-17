# 整合架构与目录设计（阶段 3–4 产出）

## 1. 分层架构

```
                          ┌──────────────────────────────────────┐
   Agent / CLI / 其他宿主 →│  Unified Tool Layer   zhijiao/tools.py│  7 个稳定能力的唯一门面
                          └───────────────┬──────────────────────┘
                                          │ 统一入参 / 统一出参 / 统一错误
                          ┌───────────────▼──────────────────────┐
                          │  Router           zhijiao/router.py  │  能力→Backend 优先级链
                          │  · 能力路由  · 健康检查  · 自动降级    │  路由轨迹 (RouteTrace)
                          └───────────────┬──────────────────────┘
                                          │ Backend 协议 (call/health)
        ┌─────────────────┬───────────────┴────────┬──────────────────┐
        ▼                 ▼                        ▼                  ▼
┌───────────────┐ ┌──────────────┐ ┌──────────────────────┐ ┌────────────────┐
│ icve_toolkit  │ │mooc_work_ans │ │ zjy_toolkit          │ │ ocsjs          │
│ (API 主力)    │ │(API 次选)    │ │ (远端占位 unavailable)│ │ (浏览器兜底)    │
│ in-process    │ │ in-process   │ │ HTTP 预留插槽         │ │ handoff 计划    │
└───────┬───────┘ └──────┬───────┘ └──────────────────────┘ └────────────────┘
        │ sys.path 隔离加载（只读，零改造）
        ▼                ▼
┌──────────────────┐ ┌────────────────────┐
│ ↑ upstreams/     │ │ ↑ upstreams/       │
│   ICVE_Toolkit   │ │   mooc-work-answer │
└──────────────────┘ └────────────────────┘
```

**核心原则落地**：

| 要求 | 落地方式 |
|---|---|
| 不重写、不大改上游核心代码 | `upstreams/` 为只读 git clone，全程 `git status` 保持 clean（有测试守护） |
| 上游作为独立 Backend / Adapter | 每个上游一个 Adapter 文件，实现同一 Backend 协议 |
| 只在外层加 Router + Tool Layer | `zhijiao/` 是本项目**唯一自写代码**，零业务抓取逻辑 |
| 自动判断调用哪个项目 | `router.py` 的 `ROUTING_TABLE` + 可用性/能力探测 |
| 统一登录状态 | `session.py` 以 **SSO Token** 为单一凭据源，各 Backend 自行派生域 Bearer |
| 统一配置 | `config.py`（env + config.json 合并） |
| 统一日志 | `log.py`（结构化 + 路由轨迹 + JSONL 落盘） |
| 统一错误返回 | `errors.py`（一个异常树 + 稳定 `code`） |
| 统一数据格式 | `contracts.py`（dataclass DTO，全部带 `to_dict()`） |
| 重复功能不重新实现 | 路由表把重复能力收敛到单一主 Backend（见 `TOOL_BACKEND_MAP.md`） |
| API 优先，OCS 兜底 | 路由表末位固定为 `ocsjs`，且它只产出计划、不抓数据 |
| 不复制大量源码 | 自写代码 ≈ 1600 行，上游代码 0 行拷贝 |
| 方便单独升级上游 | `upstreams/<name>` 可独立 `git pull`；Adapter 通过 `BackendInfo.upstream_commit` 记录版本 |

## 2. 目录设计

```
D:\CodexWork\智慧职教刷课\
├── upstreams/                     # ① 上游只读区（git clone，不修改）
│   ├── ICVE_Toolkit/              #    PolyForm NC 1.0.0
│   ├── ZJY-Toolkit/               #    仅 README（无源码）
│   ├── mooc-work-answer/          #    无 LICENSE（默认保留所有权利）
│   └── ocsjs/                     #    MIT
│
├── zhijiao/                       # ② 统一层（本项目唯一自写代码）
│   ├── __init__.py                #    对外导出 Tools / 契约 / 错误
│   ├── contracts.py               #    统一数据格式（DTO + 枚举 + 标准化函数）
│   ├── errors.py                  #    统一错误树 + 稳定错误码
│   ├── config.py                  #    统一配置
│   ├── log.py                     #    统一日志（含 RouteTrace 打印 + JSONL）
│   ├── session.py                 #    统一登录状态（SSO Token 单一凭据源）
│   ├── router.py                  #    能力路由器（优先级链 / 降级 / 轨迹）
│   ├── tools.py                   #    统一 Tool Layer（7 能力门面）
│   ├── api.py                     #    供 Agent 调用的字典化门面（call_tool）
│   └── backends/
│       ├── __init__.py            #    注册表
│       ├── base.py                #    Backend 协议 + BackendInfo + Capability
│       ├── loader.py              #    sys.path 隔离加载器（导入上游模块）
│       ├── icve_toolkit.py        #    Adapter 1（API 主力）
│       ├── mooc_work_answer.py    #    Adapter 2（API 次选）
│       ├── zjy_toolkit.py         #    Adapter 3（远端占位）
│       └── ocsjs.py               #    Adapter 4（浏览器兜底 handoff）
│
├── tests/                         # ③ 分阶段测试
│   ├── _common.py                 #    环境隔离 + 上游完整性清单 + 假 Backend
│   ├── upstream_manifest.json     #    上游 178 个文件的 sha256 基线
│   ├── test_stage1_upstream_intact.py
│   ├── test_stage2_contracts.py
│   ├── test_stage3_backends_import.py
│   ├── test_stage4_router.py
│   ├── test_stage5_tools_e2e.py
│   └── test_stage6_login.py       #    登录通道 + 项目内 Chromium 真启动
├── run_tests.py                   # 一键跑全部阶段（自动切到项目 .venv）
├── demo.py                        # 离线演示 / 自检
│
├── .workbuddy/                    # ④ 项目级技能与记忆（自包含）
│   ├── skills/                    #    gitbash-sandbox-shell / windows-recycle-delete
│   └── memory/                    #    MEMORY.md + 每日工作日志
├── .venv/                         # ⑤ 项目内 Python 运行时（gitignored）
├── browsers/                      # ⑥ 项目内 Chromium 内核 705MB（gitignored）
├── state/                         # ⑦ 凭据 / 路由日志（gitignored，session.json 0600）
│
├── docs/
│   ├── UPSTREAM_ANALYSIS.md       # 阶段 2 产出
│   ├── INTEGRATION_ARCHITECTURE.md# 阶段 3 产出（本文）
│   └── TOOL_BACKEND_MAP.md        # 阶段 4 产出
├── LICENSES_AND_COMPLIANCE.md     # 许可证与合规
├── config.example.json
└── requirements.txt
```

**⑤⑥⑦ 是本项目"自包含"的体现**：依赖、浏览器内核、凭据全部留在项目目录内，
不放项目外（2026-09-17 用户明确要求）。前两项由 `.gitignore` 排除，
按 `requirements.txt` 与 `playwright install chromium` 可重建。

## 3. 关键机制设计

### 3.1 零改造加载上游（`backends/loader.py`）

上游模块都是"顶层模块式导入"（如 ICVE 的 `from utils import log`、
mooc 的 `from MoocMain.log import Logger`），所以必须把上游根目录放进 `sys.path`。

```python
load_upstream("icve")        # 把 upstreams/ICVE_Toolkit 插入 sys.path，importlib 导入并缓存
```

隔离措施：

- 只**插入路径、导入模块**，不 patch、不改写上游文件；
- 导入结果缓存（模块只加载一次）；
- 记录 `upstream_commit`（`git rev-parse HEAD`）用于版本可追溯；
- 已知副作用如实记录：`mooc-work-answer` 的 `MoocMain/log.py` 在 import 时
  会在**当前工作目录**创建 `mooc-work-answer-log_<date>.log`（上游硬编码 `./`）。
  不修改上游 → 统一层启动时把 module 级日志目录约定为项目根，并在 `.gitignore` 忽略该文件。

### 3.2 统一登录状态（`session.py`）

关键发现：**四个上游共用同一个 SSO 域**。

```
                    ┌─────────────────────────────┐
   账密 / 滑块 / OAuth│  sso.icve.com.cn            │
                    │  → SSO Token（单一凭据源）    │
                    └──────────────┬──────────────┘
                                   │ 各域自取：GET /auth/passLogin?token=<SSO Token>
             ┌─────────────────────┼─────────────────────┐
             ▼                     ▼                     ▼
      zjy2 Bearer            ai X-AI-Token          zyk Bearer
      （ICVE_Toolkit 用）     （MWA 用）             （MWA 用）
```

因此 `LoginState` 只持久化 `sso_token`（+ 用户名，不含明文密码），
各 Adapter 用同一个 SSO Token 构造自己的客户端，上层"看起来"是统一登录态。

### 3.2.1 三条登录通道（`zhijiao.api.login`）

**口径**：登录是"取得统一登录态"的通道，**不是**对外 7 个能力之一
（`test_stage6_login.py` 有测试盯着"login 不在 Capability 里"）。

```
                     ┌─────────────────────────────────────────┐
   channel="auto" ──▶│ ① password  账密 + 阿里云滑块（全自动）   │
      （默认）        │    ICVE_Toolkit slider_auto             │
                     │    需 playwright + opencv + numpy        │
                     │       + scipy + pillow + Chromium 内核  │
                     └───────────────┬─────────────────────────┘
                                     │ BackendUnavailable（依赖缺失）
                                     ▼
                     ┌─────────────────────────────────────────┐
                     │ ② browser   人工浏览器 + 本地 HTTP 回调  │
                     │    mooc-work-answer oauth_login         │
                     │    只需 lxml，不需要 Chromium            │
                     └─────────────────────────────────────────┘
                                     │ 成功
                                     ▼
                          写入 state/session.json（0600）

  channel="password" → 只用 ① ；channel="browser" → 只用 ②
  （auto 之外的入口也可绕过登录：直接 set ZJ_SSO_TOKEN）
```

**关键取舍**：
* ① 失败于依赖缺失 → 可降级到 ②；① 失败于**账密被拒**（`ZJ-2002 CredentialRejected`）
  → **不降级、不重试**。上游 README 明确：账密类拒绝重试无意义，
  且同 IP 高频尝试会触发风控惩罚。所以这条错误是"立即抛出"型。
* ① 缺依赖时，`login()` 会往异常的 `details['hint']` 里补上
  "改用 channel='browser'" —— 不让人只看到一句"缺 playwright"。

### 3.2.2 项目内运行时（`Settings.apply_env`）

`slider_auto.py` 直接调 playwright，而 playwright 只认环境变量 `PLAYWRIGHT_BROWSERS_PATH`。
本项目把内核放在**项目内** `browsers/`，所以：

```python
def apply_env(self) -> None:
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(self.browsers_dir))
```

`ensure_dirs()` / `zhijiao.setup()` / `login_with_password()` 都会调用它。
用 `setdefault` 是为了尊重使用者显式设定的值（有测试钉住）。
这样项目换机器、换目录都不需要重新下载 705MB 内核。

### 3.3 统一错误（`errors.py`）

```python
ZhijiaoError
├── ConfigError                 ZJ-1001
├── AuthError                   ZJ-2001   # 凭据失效/未登录 → 不降级
│   └── CredentialRejected      ZJ-2002   # 账密错误 → 不重试（重试无意义）
├── BackendUnavailable          ZJ-3001   # 后端不可用 → 降级
├── CapabilityNotSupported      ZJ-3002   # 后端不支持该能力 → 降级
├── UpstreamError               ZJ-4001   # 上游返回异常/解析失败 → 降级
│   └── UpstreamTransientError  ZJ-4002   # 网络/超时 → 降级
├── RouteExhausted              ZJ-5001   # 所有后端都失败（聚合所有尝试）
└── NotSupported                ZJ-5002   # 全平台都不支持该能力
```

**降级判定**：`RETRYABLE = {BackendUnavailable, CapabilityNotSupported, UpstreamTransientError, UpstreamError}`
`AuthError / CredentialRejected / ConfigError` 视为**不可降级**（换个后端也一样缺凭据）。

### 3.4 路由器行为（`router.py`）

```python
Router.invoke(Capability.LIST_COURSES, session=st)
```

1. 取 `ROUTING_TABLE[cap]` 得到有序 Backend id 列表；
2. 逐个检查 `backend.available`（懒探测：能 import + 有凭据）；
3. 调 `backend.call(cap, session=..., **kwargs)`；
4. 捕获 `ZhijiaoError`：可降级 → 记录 `RouteTrace` 后试下一个；不可降级 → 立即抛出；
5. 全部失败 → 抛 `RouteExhausted`（内含每次尝试的 `backend/error/code` 明细）；
6. 成功 → 返回 `(result, RouteTrace)`。

支持 `backend="icve_toolkit"` 强制指定，`no_fallback=True` 关闭降级（调试用）。
支持 `health()` 主动探活。

## 4. 统一数据格式（`contracts.py` 摘要）

| DTO | 关键字段 |
|---|---|
| `Course` | `course_id, course_info_id, class_id, name, course_type, backend, raw` |
| `TaskNode` | `id, name, file_type, progress, finished, is_leaf, url, duration_s, parent_id, backend, raw` |
| `CourseDetail` | `course, nodes, backend` |
| `CourseProgress` | `course, total, finished, percent, by_file_type, backend` |
| `AttendanceRecord` | `sign_id, course_id, title, sign_type, status(SignStatus), status_text, time, backend, raw` |
| `ExamResult` | `exam_id, title, exam_type, score, submitted, status, backend, raw` |
| `LearningReport` | `course, mode, attempted, succeeded, failed, elapsed_s, errors, backend` |
| `BrowserHandoff` | `target_url, ocs_projects, scenario, params, script_path, reason` |

枚举统一：`CourseType{SPOC,MOOC,RESOURCE,UNKNOWN}`、`SignStatus{UNSIGNED,SIGNED,LATE,LEAVE,SICK,ABSENT}`、
`LearningMode{PROGRESS,ANSWER,DISCUSSION,ALL}`。

## 5. 分阶段测试策略

| 阶段 | 测试文件 | 验证内容 | 是否需要网络 |
|---|---|---|---|
| 1 | `test_stage1_upstream_intact.py` | 四个上游 `git status --porcelain` 为空；关键文件存在；许可证文件存在性；**逐文件 sha256 与基线一致** | 否 |
| 2 | `test_stage2_contracts.py` | DTO 序列化/反序列化与 `__post_init__` 自愈；类型标准化；枚举映射（含签到 0–5、状态码为数字） | 否 |
| 3 | `test_stage3_backends_import.py` | 真正 import 上下游模块；断言 `ZjyClient.get_my_courses` 等方法存在；Adapter 协议符合；ZJY 占位抛 `BackendUnavailable` | 否 |
| 4 | `test_stage4_router.py` | 用 Fake Backend 验证：优先级、降级、不可降级错误、`RouteExhausted` 明细、强制指定、三层路由视图 | 否 |
| 5 | `test_stage5_tools_e2e.py` | 用 Fake Backend 打通 7 个 Tool 的入参/出参/错误信封/交接计划透传 | 否 |
| 6 | `test_stage6_login.py` | 登录通道选择与降级、`CredentialRejected` 不换通道、登录态落盘（不含密码）、**项目内 Chromium 真启动**、项目外无 `ms-playwright` 残留 | 否 |
| 7 | `test_stage1_upstream_intact.py`（复跑） | 全流程跑完后上游仍然 clean | 否 |

**全部测试离线可跑**，不消耗真实账号、不对线上接口发请求。
会真的启动浏览器/打开浏览器的地方（账密登录、OAuth 回调）一律**打桩**；
唯一真启动的是 Chromium 内核可用性测试（不联网，只证明内核能跑）。
真实联调另设"手工冒烟"章节（需用户提供 SSO Token 或账密）。

## 6. 上游单独升级方案

```bash
cd upstreams/ICVE_Toolkit && git pull        # 只升这一个
```

升级后需确认的三处契约（Adapter 只依赖这些）：

1. `zjy_client.py` → `class ZjyClient` 及其被调用方法的签名；
2. `speed_course.py` → `run_speed_course(client, course, speed_type, ...)`；
3. `sign.py` / `answer.py` → `get_signs(...)` / `get_course_exams_list(...)`。

`test_stage3_backends_import.py` 会把这三组签名逐条断言 —— **上游一旦破坏契约，测试立刻红**。

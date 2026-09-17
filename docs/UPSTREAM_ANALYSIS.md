# 四个上游项目分析报告（阶段 2 产出）

> 本报告基于 `upstreams/` 下的 git clone 快照（`--depth 1`）逐文件阅读得出，非 README 转述。

## 0. 一览表

| 项目 | 语言/形态 | 入口 | 许可证 | 能否作本地 Backend |
|---|---|---|---|---|
| `atvkh/ICVE_Toolkit` | Python 3.8+ / 交互式 CLI | `main.py` | **PolyForm Noncommercial 1.0.0**（禁商用） | ✅ 可以，覆盖最全 |
| `atvkh/ZJY-Toolkit` | 无源码，托管 SaaS | 无（`study.atvkh.xyz`） | 无 LICENSE（README 自称 Educational） | ❌ 不能，仅有 README |
| `11273/mooc-work-answer` | Python ≥3.8 / 交互式 CLI | `StartWork.py` | **无 LICENSE 文件**（README 声明仅供学习） | ✅ 可以，结构最干净 |
| `ocsjs/ocsjs` | TypeScript pnpm monorepo / 浏览器用户脚本 | `packages/scripts` | **MIT** | ⚠️ 仅浏览器端兜底 |

## 1. ICVE_Toolkit（atvkh）

### 1.1 目录与入口

```
ICVE_Toolkit/
├── main.py            # CLI 入口 + 交互式菜单 (main/menu_login/menu_courses/menu_speed/menu_sign/menu_accounts)
├── zjy_client.py      # ★ 核心：ZjyClient（三域鉴权 + 课程/刷课/签到/答题） 2064+ 行
├── auth.py            # 登录：自动滑块 → 真实页 → 人工回调(127.0.0.1:9527) 降级链
├── slider_auto.py     # 全自动滑块登录（playwright + opencv + scipy 最小二乘自校准）
├── accounts.py        # 账号管理（accounts.json 持久化）
├── speed_course.py    # 刷课（SPOC/MOOC/资源库 进度 + 答题 + 讨论）
├── answer.py          # 自动答题（答案抓取 + 提交）
├── sign.py            # 签到列表 / 补签 / 代改考勤
├── utils.py           # 日志/格式化/输入校验
└── requirements.txt
```

### 1.2 依赖分层（重要）

```txt
# 核心（轻）：requests + pycryptodome
requests>=2.28.0
pycryptodome>=3.18.0
# 仅"自动滑块登录"需要（重，约 300MB Chromium）
playwright / numpy / opencv-python / scipy / pillow
```

→ 只做 API 调用时，**只需 requests + pycryptodome**，可以不装 playwright 全家桶。
这一点决定了 Adapter 可以"轻量可用、滑块登录可选"。

### 1.3 关键技术事实

`ZjyClient` 已经内置了多域聚合，等价于一个**上游自带的 Router**：

| 域名常量 | 用途 | 鉴权 |
|---|---|---|
| `https://zjy2.icve.com.cn/prod-api` (BASE_URL) | SPOC/MOOC 课程 | `Authorization: Bearer <token>` |
| `https://ai.icve.com.cn/prod-api` (AI_BASE_URL) | MOOC 课程设计/讨论/考试 | `X-AI-Token: Bearer <ai_token>` |
| `https://zyk.icve.com.cn/prod-api` (ZYK_BASE_URL) | 资源库课程 | `Authorization: Bearer <zyk_token>` |
| `https://sso.icve.com.cn` (SSO_BASE) | 单点登录 | 账密/滑块/人工回调 |

鉴权链：`SSO Token` --`/auth/passLogin?token=`--> 各域 `access_token`。
`refresh_token_from_sso()` 可在 Bearer 过期时无感刷新。

请求头伪装 Android 端：`platform-type: android`、`log-equipment-model: google Pixel 8`。

AES：`generate_aes_key() = md5(token)[:16]`，`aes_encrypt()` 走 AES-128-ECB，
用于 SPOC 刷课心跳体加密。

### 1.4 可直接复用的非交互 API（Adapter 的调用面）

| 上游方法 | 返回 | 对应统一能力 |
|---|---|---|
| `ZjyClient.get_my_courses()` | `list[dict]`（SPOC+MOOC+RESOURCE 去重） | `list_courses` |
| `ZjyClient.get_course_cells(course_info_id, class_id, course_id, include_completed, ctype)` | 叶子课件列表，每项带 `_speed` | `get_course_detail` / `list_unfinished_tasks` |
| `ZjyClient._parse_cell_speed(r, ctype)` (staticmethod) | `float` 0–100 | `get_course_progress` |
| `ZjyClient._fetch_course_tree_level(...)` / `zyk_get_course_tree(course_info_id)` | 课程树 | `get_course_detail` |
| `speed_course.run_speed_course(client, course, speed_type, ...)` | 执行结果 | `start_learning` |
| `sign.get_signs(client, class_id, course_info_id, course_id)` | 签到活动列表（最近 40 场会话） | `get_attendance` |
| `sign.batch_sign(...)` / `sign.one_click_sign(...)` / `sign.do_sign_action(...)` | 补签/改签结果 | （补签，非对外必需） |
| `answer.get_course_exams_list(client, class_id, course_info_id, ...)` | 作业/考试列表，含 `score` / `submit` | `get_results` |
| `ZjyClient.zyk_get_exam_list(...)` / `zyk_get_exam_paper(...)` | 试卷与标准答案 | `get_results` |

`ZjyClient.__init__(token=..., sso_token=..., question_bank_dir=..., accounts=...)` 是**非交互**的，
可以脱离 `main.py` 的菜单直接实例化 —— 这是能"不重写、直接复用"的前提。

### 1.5 签到状态码（统一映射依据）

`0 未签到 / 1 已签到 / 2 迟到 / 3 请假 / 4 病假 / 5 事假`

### 1.6 许可证约束 ⚠️

PolyForm Noncommercial 1.0.0。README「商用边界判定」明确：卖软件、收费代刷、
打包付费服务、引流变现均属商用。**本项目定位为个人学习/研究用途，禁止任何商业使用。**

---

## 2. ZJY-Toolkit（atvkh）

### 2.1 事实核查

```
$ git ls-files          # 仅 1 个文件
README.md
$ git branch -r
origin/main             # 仅此一个分支
```

**仓库不含任何源码。** 它是一个部署在 `study.atvkh.xyz` 的托管服务：

- 后端：Python + FastAPI
- 前端：Vanilla JS
- 授权：CDK 激活码体系
- 功能：24H 云端签到监听、自动补签、出勤统计、批量刷课、自动答题、试卷提取、
  成绩查询、设备验证清除、多账号、邮件推送

### 2.2 整合结论

**不能作为本地 Backend，也不能作为依赖引入。** 理由：

1. 无源码可加载（无 `git` 可 import 的模块）。
2. 无公开 API 契约文档，调用需 CDK 授权，且依赖第三方服务器可用性。
3. 擅自逆向其私有接口会引入不稳定的外部依赖与合规风险。

**处置**：实现为一个**远端占位 Adapter**（`zjy_toolkit.py`），
显式声明 `available=False`，所有能力抛 `BackendUnavailable`，
由 Router 自动跳过并降级到其他 Backend。它的价值在于：

- 保留"未来若获得授权/自建部署，可无缝接入"的插槽；
- 作为能力对照表，提示我们哪些能力在别处尚缺（如"24H 云端签到监听"）。

---

## 3. mooc-work-answer（11273）

### 3.1 目录与入口

```
mooc-work-answer/
├── StartWork.py           # CLI 入口（菜单：资源库 / MOOC或课堂版 / AI优课）
├── base/
│   ├── api_client.py      # ★ BaseAPIClient（get/post/put/login，支持多 base_url）
│   └── util.py            # create_session / parse_response / aes_encrypt_ecb / pad
├── AIMoocMain/            # ai.icve.com.cn（ai.icve.com.cn/prod-api）
│   ├── api.py             # ★ AIMoocApi(BaseAPIClient)
│   └── main.py            # AIMoocHandler（流程编排）
├── ZYKMoocMain/           # 资源库 zyk.icve.com.cn
│   ├── api.py             # ★ ZYKMoocApi(BaseAPIClient)
│   └── main.py            # ZYKMoocHandler
├── NewMoocMain/           # 新版 MOOC（当前主线）
│   ├── oauth_login.py     # ★ OAuthLoginHandler：本地 HTTP 回调服务器收 token
│   ├── init_mooc.py       # 学习记录保存 / 考试 / 讨论（含 learning_time_* 系列）
│   ├── acwv2.py           # 阿里滑块 acw_sc__v2 反爬绕过
│   ├── verify.py          # 滑块缺口识别与验证
│   └── icve_exam_parser.py# 试卷 HTML 解析
├── MoocMain/              # 旧版（README 标注"已停止支持"）
└── update.py              # 自更新
```

### 3.2 依赖

```txt
lxml==6.1.0
pycryptodome~=3.18.0
requests==2.31.0
# 注释掉的：aiohttp / numpy / opencv_python_headless / Pillow
```

同样**轻量**：requests + pycryptodome + lxml。

### 3.3 可直接复用的非交互 API

| 上游 | 方法 | 对应统一能力 |
|---|---|---|
| `ZYKMoocApi` | `my_course_list(page_num, page_size, flag)` → `/teacher/courseList/myCourseList` | `list_courses` |
| `ZYKMoocApi` | `study_design_list(course_info_id)` → `/teacher/courseContent/studyDesignList` | `get_course_detail` |
| `ZYKMoocApi` | `course_content(source_id)` | `get_course_detail` |
| `ZYKMoocApi` | `study_record(course_info_id, parent_id, study_time, source_id, actual_num, last_num, total_num)` | `start_learning`（AES-ECB，key `djekiytolkijduey`） |
| `AIMoocApi` | `my_course_list(...)` / `study_design_list(...)` / `get_cell_list(...)` / `study_record_list(...)` / `course_content(...)` / `study_record(...)` | 同上 |

**注意构造副作用**：`ZYKMoocApi.__init__(username, password, token)` 与 `AIMoocApi.__init__(token, username, password)`
都会**在构造时立即调用 `self.login()`**。Adapter 必须传入有效凭据或 token，否则构造即失败。

### 3.4 两点结构性优势（决定它当"备选"而非主力）

1. **`BaseAPIClient` 抽象干净**：`get/post/put` 支持 `base_url` 覆盖，天然适合多域；
   比 ICVE_Toolkit 的 `api_get/api_get_ai/api_get_zyk` 三套并列更易扩展。
2. **OAuth 本地回调登录**（`NewMoocMain/oauth_login.py`）：
   起本地 HTTP 服务接收回调 token，与 ICVE_Toolkit 的 `127.0.0.1:9527` 人工回调是同一思路，
   可作为**登录兜底的第二通道**。

但它**没有签到/考勤、没有代改考勤**，刷课流程 (`run()`) 是打印式长流程，
不适合被当作细粒度 API 复用 —— 所以定位为**第二优先级**。

### 3.5 许可证风险 ⚠️

仓库**没有 LICENSE 文件**。README 写"仅供技术学习和研究使用，严禁用于商业盈利"，
但法律上无许可证 = 默认"保留所有权利"。**仅限个人本地学习研究，不得分发、不得商用。**
若未来要对外发布，必须替换此 Backend 或取得作者授权。

---

## 4. ocsjs

### 4.1 结构与许可证

```
ocsjs/
├── package.json           # version 4.15.3, license: MIT ★
├── pnpm-workspace.yaml
├── packages/
│   ├── core/              # 框架（$ / OCSWorker / defaultAnswerWrapperHandler）
│   ├── scripts/           # ★ 各平台脚本（vite 打包为油猴脚本）
│   │   └── src/projects/
│   │       ├── zjy.ts     # ZJYProject  name='职教云'  domains=['icve.com.cn','zjy2.icve.com.cn','zyk.icve.com.cn']
│   │       ├── icve.ts    # IcveMoocProject  name='智慧职教'  domains=['icve.com.cn','ai.icve.com.cn']
│   │       ├── cx.ts / zhs.ts / yuketang.ts / icourse.ts / unipus.ts ...
│   ├── utils/
├── scripts/               # gulp 构建脚本
└── tests/
```

**许可证 MIT** —— 四个项目里唯一明确允许商用/二次分发的。

### 4.2 与本项目相关的两个 Project

| 文件 | Project 名 | domains |
|---|---|---|
| `projects/zjy.ts` | 职教云 | `icve.com.cn`, `zjy2.icve.com.cn`, `zyk.icve.com.cn` |
| `projects/icve.ts` | 智慧职教 | `icve.com.cn`, `ai.icve.com.cn` |

`zjy.ts` 内部通过 URL 片段区分场景：

- 作业：`icve-study/coursePreview/jobTes`、`study/spocjobTest`、`study/spockeepTest`、`study/courseteaching/test/homeWork`
- 考试：`icve-study/test`、`icve-study/coursePreview/test`、`study/spoctest`

### 4.3 整合结论

它是**浏览器用户脚本**（油猴/脚本猫），不是可被 Python 直接 import 的库；
且需 `pnpm install && pnpm build`（vite + rollup + workspace）才能产出可用脚本。

**因此定位为浏览器端兜底**：当所有 API Backend 都不可用时，
Adapter 不"实现"任何抓取逻辑，而是产出一份 **handoff 计划**：

- 目标 URL（`https://zjy2.icve.com.cn/` 或 `https://ai.icve.com.cn/`）
- 应启用的 OCS Project（`zjy` / `icve`）+ 场景（作业/考试）
- 课程定位参数（`courseId` / `courseInfoId` / `classId`）
- 上游脚本构建产物路径（若已 `pnpm build`）或官方分发地址

由人来执行这一步（装用户脚本 / 打开页面），符合"不重写、不复制源码"。

---

## 5. 功能重叠矩阵（决定路由的依据）

图例：✅ 完整可用｜🟡 部分可用｜❌ 无｜🖥️ 仅浏览器端

| 能力 | ICVE_Toolkit | ZJY-Toolkit | mooc-work-answer | ocsjs |
|---|---|---|---|---|
| 主域 zjy2（SPOC/MOOC）API | ✅ 最全 | 🟡（云端） | 🟡（新版 MOOC） | 🖥️ |
| AI 域 ai.icve.com.cn | ✅ | 🟡 | ✅ `AIMoocApi` | 🖥️ |
| 资源库域 zyk | ✅ | 🟡 | ✅ `ZYKMoocApi` | 🖥️ |
| 课程列表聚合（三域合一） | ✅ `get_my_courses` | ❌ | ❌（分域） | 🖥️ |
| 课程树 / 课件详情 | ✅ | ❌ | 🟡 `study_design_list` | 🖥️ |
| 进度（0–100） | ✅ `_parse_cell_speed` | 🟡 | 🟡 `study_record_list` | 🖥️ |
| 未完成任务 | ✅ `include_completed=False` | ❌ | 🟡 | 🖥️ |
| 刷课/心跳 | ✅ `run_speed_course` | ✅ | ✅ `run()` | 🖥️ |
| 签到 / 考勤 | ✅ `sign.py` | ✅ 云端监听 | ❌ | 🖥️ |
| 补签 / 代改考勤 | ✅ | ✅ | ❌ | ❌ |
| 成绩 / 作业考试列表 | ✅ `get_course_exams_list` | ✅ 成绩查询 | 🟡 | 🖥️ |
| 自动答题 | ✅ | ✅ | ✅ | ✅ |
| 登录方式 | 自动滑块 + 人工回调 | 短信验证码 | OAuth 回调 + 账密 | 页面已登录 |
| 反爬 | 阿里滑块（登录） | 未知 | acw_sc__v2 + 滑块 | 无（真浏览器） |

### 边界结论

1. **`ICVE_Toolkit` 是覆盖面最广、唯一提供"三域聚合课程列表 + 签到考勤"的项目 → 主力 Backend。**
2. `mooc-work-answer` 的 `BaseAPIClient` 抽象更干净，但缺签到考勤、刷课流程是打印式长流程
   → **次选 Backend**，用于 AI 域/资源库域的结构化查询与 `start_learning` 降级。
3. `ZJY-Toolkit` 无源码 → **占位 Backend（`available=False`）**。
4. `ocsjs` MIT、真浏览器执行 → **浏览器兜底**，以 handoff 计划形式交付，不内联复刻。
5. 结论：**不存在"需要新写一遍"的功能**。7 个统一能力全部能映射到上游已有方法。

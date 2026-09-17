# 统一 Tool → Backend 路由表（阶段 4 产出）

> 结论先行：**7 个统一能力全部映射到上游已有方法，没有任何一个需要重新实现。**
> 优先级 = 从左到右依次尝试；上一个抛"可降级错误"才轮到下一个。

## 1. 路由总表

| # | 统一 Tool | ① 主选 | ② 次选 | ③ 兜底 | 主选调用的上游方法 |
|---|---|---|---|---|---|
| 1 | `list_courses` | `icve_toolkit` | `mooc_work_answer` | `ocsjs` | `ZjyClient.get_my_courses()` |
| 2 | `get_course_detail` | `icve_toolkit` | `mooc_work_answer` | `ocsjs` | `ZjyClient.get_course_cells(..., include_completed=True)` |
| 3 | `get_course_progress` | `icve_toolkit` | `mooc_work_answer` | `ocsjs` | `ZjyClient.get_course_cells(...)` + `_parse_cell_speed()` |
| 4 | `list_unfinished_tasks` | `icve_toolkit` | `mooc_work_answer` | `ocsjs` | `ZjyClient.get_course_cells(..., include_completed=False)` |
| 5 | `start_learning` | `icve_toolkit` | `mooc_work_answer` | `ocsjs` | `speed_course.run_speed_course(client, course, speed_type, ...)` |
| 6 | `get_attendance` | `icve_toolkit` | `zjy_toolkit` | `ocsjs` | `sign.get_signs(client, class_id, course_info_id, course_id)` |
| 7 | `get_results` | `icve_toolkit` | `mooc_work_answer` | `ocsjs` | `answer.get_course_exams_list(client, class_id, course_info_id, ...)` |

## 2. 逐条说明与理由

### ① `list_courses`

```python
list_courses(user=None, course_type=None) -> list[Course]
```

- **主选 `icve_toolkit`**：`get_my_courses()` 是四个上游里**唯一一次性聚合三域**的方法
  （SPOC `spoc/courseInfoStudent/myCourseList` → MOOC `spoc/course/mooc/getMyCourseList`
  → 资源库 `zyk teacher/courseInfoStudent/myCourseList`），并按
  `(courseId, courseInfoId, classId)` 去重，带 `_courseType` 标记。
  **这一条就直接消灭了"三域分别拉取再合并"的重复实现。**
- 次选 `mooc_work_answer`：分域调 `ZYKMoocApi.my_course_list()` /
  `AIMoocApi.my_course_list()`，再由统一层合并（此处需要统一层做合并 —— 但仅在主选失败时发生，
  属于降级路径的必要成本，不算重复实现主路径功能）。
- 兜底 `ocsjs`：产出 handoff 计划（打开 `zjy2.icve.com.cn` 课程页，用户在浏览器面板读列表）。
- `course_type` 过滤在**统一层**做（主选已带 `_courseType`；次选按域判定）。

### ② `get_course_detail`

```python
get_course_detail(course_id, course_type=None, course=None) -> CourseDetail
```

- **主选 `icve_toolkit`**：`get_course_cells(course_info_id, class_id, course_id, include_completed=True, ctype=...)`
  已实现递归课程树展开、容器/叶子判定、图片类豁免、考试过滤。
  这是四个上游里**最完整的课程结构解析**（`_fetch_course_tree_level` 递归 + 兜底 API 链
  `spoc/courseDesign/study/record` → `studyList` → `mooc/courseDesign/studyList` → `nzyk/courseDesign/studyList`）。
- 次选 `mooc_work_answer`：`ZYKMoocApi.study_design_list(course_info_id)`（资源库）、
  `AIMoocApi.get_cell_list(...)`（AI 域）。粒度较粗，无容器判定。
- 兜底 `ocsjs`：handoff 计划。

### ③ `get_course_progress`

```python
get_course_progress(course_id, course_type=None) -> CourseProgress
```

- **主选 `icve_toolkit`**：复用 `get_course_cells(...)` 拿到的 `_speed`（由 `_parse_cell_speed`
  兼容多种字段名解析出的 0–100），统一层聚合成 `total / finished / percent / by_file_type`。
- 次选 `mooc_work_answer`：`AIMoocApi.study_record_list(...)` / `ZYKMoocApi` 学习记录。
- 兜底 `ocsjs`：handoff 计划（浏览器面板直接显示进度）。

> 进度聚合是**纯计算**，没有上游对应方法，属于统一层职责，不算重复实现。

### ④ `list_unfinished_tasks`

```python
list_unfinished_tasks(course_id, course_type=None, limit=None) -> list[TaskNode]
```

- **主选 `icve_toolkit`**：`get_course_cells(..., include_completed=False)` 的语义
  **天然就是"未完成任务"**（源码内 `_skipped_speed_count` 专门跳过 `_speed >= 100`，
  并过滤 `fileType == "考试"`）。直接复用，零额外逻辑。
- 次选 `mooc_work_answer`：学习记录里过滤未完成项。
- 兜底 `ocsjs`：handoff 计划。

### ⑤ `start_learning`

```python
start_learning(course_id, mode="progress", course_type=None, course=None, **opts) -> LearningReport
```

- **主选 `icve_toolkit`**：`speed_course.run_speed_course(client, course, speed_type, ...)`，
  `speed_type ∈ {all, progress, discussion, answer}`。上游已实现：
  - SPOC 心跳：AES-128-ECB 加密 + 并发提交（服务器每次 +5s，batch=100）
  - MOOC 心跳：6 个 API 探测 + 并发提交（每次 +5s，`heartbeat_interval=5`）
  - 资源库心跳：明文 JSON 串行（每次 +10s，URL 末尾斜杠必需）
  - 模拟真实模式 / 快速模式两档
  - 课件时长解析（`_parse_mp4_durations_parallel`，读 mp4 `moov` box）
  **这是本项目最不可能被替代的能力，绝不重写。**
- 次选 `mooc_work_answer`：`NewMoocMain.init_mooc.run(...)`（新版 MOOC 学习记录保存路径）
  或 `ZYKMoocApi.study_record(...)`（单条记录写入，AES-ECB key `djekiytolkijduey`）。
- 兜底 `ocsjs`：handoff 计划 —— 这是 OCS 最典型的场景（真浏览器播放视频），
  给出目标 URL + Project + 课程参数，由用户装脚本执行。
- **`mode` → `speed_type` 映射**：`progress→progress`、`discussion→discussion`、
  `answer→answer`、`all→all`。

### ⑥ `get_attendance`

```python
get_attendance(course_id, course_type=None, course=None, limit=40) -> list[AttendanceRecord]
```

- **主选 `icve_toolkit`**：`sign.get_signs(client, class_id, course_info_id, course_id)`
  会拉最近 40 个课堂会话的签到活动，逐个查学生签到状态，
  支持普通/手势/二维码三种类型（二维码还能读签到详情的 `qrCode`，已结束的也能补）。
  **这是唯一有签到能力的本地 Backend。**
- 次选 `zjy_toolkit`：它的"24H 云端签到监听 + 出勤统计"是缺失能力的最佳补位，
  但当前 `available=False`（无源码/无授权）→ 实际会直接跳到兜底。
- 兜底 `ocsjs`：handoff 计划（浏览器端签到页面）。
- 状态码统一：`0未签到/1已签到/2迟到/3请假/4病假/5事假` → `SignStatus` 枚举。

### ⑦ `get_results`

```python
get_results(course_id, course_type=None, course=None) -> list[ExamResult]
```

- **主选 `icve_toolkit`**：`answer.get_course_exams_list(client, class_id, course_info_id, ...)`
  返回 `[{id, title, type(作业/考试/测验), score, submit, ...}]`，分资源库/MOOC/SPOC 三路采集。
  另可叠加 `ZjyClient.zyk_get_exam_list(...)` 取更细的试卷层信息。
- 次选 `mooc_work_answer`：其 `MoocMain/workMain.py` 有 `is_work_score` / `workExamHistory` 相关参数，
  可作成绩补充来源。
- 兜底 `ocsjs`：handoff 计划。
- **注意**：`get_results` 只**读**成绩，不含"自动答题提交"。自动答题虽在上游存在
  （`answer.do_auto_answer_single_exam`），但不在本次要求暴露的 7 个能力内，
  统一层仅保留扩展位（`start_learning(mode="answer")` 已能覆盖 SPOC/MOOC 的答题）。

## 3. 降级触发矩阵

| 错误 | 是否降级到下一个 Backend | 说明 |
|---|---|---|
| `BackendUnavailable` | ✅ | 后端不可用（缺依赖/缺凭据/占位未实现） |
| `CapabilityNotSupported` | ✅ | 后端不支持该能力（如 ZJY 不支持 `list_courses`） |
| `UpstreamTransientError` | ✅ | 网络超时/连接失败 |
| `UpstreamError` | ✅ | 上游返回结构异常、解析失败 |
| `AuthError` | ❌ | 凭据失效 —— 换 Backend 也没用，需重新登录 |
| `CredentialRejected` | ❌ | 账密错误，重试无意义（上游明确不重试） |
| `ConfigError` | ❌ | 配置错误，属于环境问题 |

## 4. 为什么这样切分（避免重复实现的关键决策）

1. **能力收敛而非项目收敛**：不是"Took A 负责一半、B 负责一半"，而是
   "每个能力选一个主 Backend，其余仅作降级"。这样同一个功能只有一条主实现路径。
2. **主选全部落在 `ICVE_Toolkit`**：因为它同时具备三域聚合、签到考勤、刷课心跳，
   是唯一"一个 client 打天下"的上游。这样避免了在统一层重写三域合并/鉴权/重试逻辑。
3. **`mooc-work-answer` 只做降级**：它的 `BaseAPIClient` 更优雅，但缺签到考勤，
   且刷课是打印式长流程。强行提拔为主力会让统一层需要额外适配"打印流"。
4. **`ocsjs` 只产出计划不抓数据**：它是 MIT 的浏览器脚本，作为"真浏览器执行"兜底最有价值；
   若试图用 Python 复刻它的选择器逻辑，就违反"不重写、不复制源码"。
5. **`ZJY-Toolkit` 只占位**：无源码，物理上不可能被调用。保留插槽 + 能力对照。

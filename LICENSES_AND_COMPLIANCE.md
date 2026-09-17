# 许可证与合规说明

## 1. 四个上游的许可证实况

| 项目 | 许可证 | 文件位置 | 允许商用 | 备注 |
|---|---|---|---|---|
| `atvkh/ICVE_Toolkit` | **PolyForm Noncommercial License 1.0.0** | `upstreams/ICVE_Toolkit/LICENSE` | ❌ 禁止 | README 附「商用边界判定」表，明确收费代刷/打包付费服务/引流变现均属商用 |
| `atvkh/ZJY-Toolkit` | 无 LICENSE 文件（README 标 `license-Educational`） | — | ❌ 未授权 | 无源码，仅 README；本项目未调用其服务 |
| `11273/mooc-work-answer` | **无 LICENSE 文件** | — | ❌ 未授权 | 法律上默认「保留所有权利」；README 声明"仅供技术学习和研究，严禁用于商业盈利" |
| `ocsjs/ocsjs` | **MIT License** | `upstreams/ocsjs/LICENSE` | ✅ 允许 | `Copyright (c) 2022 enncy`，四个项目中唯一宽松许可 |

## 2. 对本项目的约束（必须遵守）

1. **本项目仅限个人学习、研究与自用。** 不得对外销售、不得作为收费服务的一部分、
   不得用于代刷获利（含实物/账号等对价）、不得用于引流变现。
2. **不得对外分发 `mooc-work-answer` 的代码**（无许可证）。本仓库的 `upstreams/`
   仅为本地研究快照；若要公开发布本项目，必须先移除该 Backend 或取得作者书面授权。
3. **不得对外分发 `ICVE_Toolkit` 的代码**做商业用途；PolyForm NC 要求分发时附带许可证条款
   （Notices 条款），且只允许非商业分发。
4. **`ocsjs` 是 MIT**：若后续发布，可包含其代码，但必须保留版权声明与许可证全文。
5. **不调用 `ZJY-Toolkit` 的未公开接口。** 本项目将其实现为 `available=False` 的占位 Backend，
   不逆向、不依赖其 CDK 授权服务。

## 3. 上游代码的物理隔离

- `upstreams/*` 全部是**原样 `git clone --depth 1`**，本项目从未修改其中任何文件；
  `tests/test_stage1_upstream_intact.py` 会断言每个仓库 `git status --porcelain` 为空。
- 统一层通过 `sys.path` **只读加载**上游模块，不 patch、不 monkey-patch、不复制源码到 `zhijiao/`。
- `zhijiao/` 内不含任何来自上游的抓取/加密/业务逻辑，只有：
  协议定义、数据结构转换、路由决策、日志/配置/错误处理。

## 4. 平台使用条款提示

智慧职教 / 职教云 / 学无止境（ICVE 系）平台的用户协议通常限制自动化访问。
四个上游 README 均有免责声明（"仅供学习交流""用户自行承担风险""请遵守所在学校规定"）。
本项目沿用同样立场：

- 使用者需自行确认其行为符合所在学校规定与平台服务条款；
- 建议仅用于自己账号的学习辅助，不用于大规模批量操作；
- 上游 README 明确警告：同 IP 高频尝试会触发风控惩罚，请勿反复强行重试。

## 5. 数据安全

- `LoginState` 只持久化 `sso_token` 与用户名，**默认不落盘明文密码**；
- `state/session.json` 已加入 `.gitignore`，建议文件权限 0600；
- 统一日志对 `token` 字段做掩码（只保留前 6 后 4 位）；
- 题库/账号等敏感目录不上传、不提交。

---

## 免责声明（2026-09-17 增补）

本项目**仅供个人学习、技术研究与交流参考**，不含任何账号、凭据、cookie、题库或答案数据；上游第三方源码不入库、不分发。使用者需自行遵守学校规定、平台服务条款与法律法规，一切使用后果由使用者自行承担。完整声明见 [DISCLAIMER.md](DISCLAIMER.md)。

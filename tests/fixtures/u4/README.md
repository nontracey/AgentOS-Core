# U4 固定验收样本（tests/fixtures/u4）

> 冻结日期：2026-09-30 ｜ 冻结树：`cfa12be5aacf06900cf98aa666ce40cfcf217c5e`（dirty=0）
> 用途：U4-01～U4-10 的判据先落盘后实现，**不随模型结果放宽预期**。全部为合成数据，不含真实凭据/个人信息。

| 文件 | 样本 | 覆盖阶段 |
|---|---|---|
| `a_access_matrix.json` | A 权限基准 | 01/02/10 |
| `s_simple_report.json` | S 简单报告 | 08 |
| `k_knowledge.json` | K 知识替换 | 06/07 |
| `c_collaboration.json` | C 复杂协作 | 09 |
| `f_failure_recovery.json` | F 失败/恢复 | 04/09/10 |

## 约定

- 每个样本同时给出 `inputs`（合成输入）与 `expectations`（可断言预期）；预期只允许在计划/进度中显式修订并留痕。
- 主体命名：`admin`（原 owner，role=admin）、`guest-a` / `guest-b`（role=guest，各自独立 subject 与 grant）、`peer`（同租户普通主体，非 admin）、`other-tenant`（另一 tenant）。
- 任务：`T1` 可授予 guest-a、`T2` 可授予 guest-b、`T3` 私有（任何 guest 均不可读）。
- 成果：`F1-v1` 可预览（默认不允许下载）、`F1-v2` 私有、`F3` 属 T2 私有；素材 `S2` 私有。
- 所有越权入口（HTTP/CLI/Webhook/voice/服务直调）均须拒绝，并同时断言：无数据正文泄露、无新 job/副作用、资源归属未变、审计有记录。
- `hash` 字段为 `sha256` 前 16 位，仅用于锁定输入，不表示内容机密。

修改本目录 = 修改判据；任何修订必须在 `docs/u4-progress.md` 与对应 `evidence/u4-XX.md` 留痕。

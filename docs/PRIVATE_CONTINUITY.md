# Private Continuity

私有连续状态用于保存一份“当前仍未解决的冲突”。它不是普通记忆桶，而是
CC、Codex 和未来聊天前端之间共享的生命周期状态。

## 存储与隔离

- 当前状态：`<vault>/private_continuity/unresolved_conflict.md`
- 最近恢复快照：`<vault>/private_continuity/previous_conflict.md`
- 目录和文件尽力使用 `0700` / `0600` 权限。
- 该目录不在 BucketManager 的 permanent、dynamic、feel、plan、letter
  扫描目录内，因此不会进入搜索、dream、feel 相关性、向量、衰减或日印象。
- 只保留一份上一版本，避免把私有传输状态变成长历史仓库。完整经历如需长期
  保存，仍应另存为普通记忆桶。

## Breath 行为

状态打开时，`breath()` 在核心准则之后、日印象之前逐字返回正文。私有状态有
独立的 2000 token 启动预算，不挤占普通近期记忆、自动精读或相关 feel 的预算。
写入时若正文超过配置的 `max_breath_tokens` 会被拒绝，不会在启动时静默截断。

## Dashboard API

以下接口都要求 Dashboard 登录态：

- `GET /api/private-continuity/conflict`：读取当前状态和恢复快照元数据。
- `PUT /api/private-continuity/conflict`：创建或更新，正文为 `content`；建议携带
  `expected_revision` 防止并发覆盖。
- `DELETE /api/private-continuity/conflict?confirm=true`：双方确认已说清后解决；
  需要 JSON body 中的 `expected_revision`。
- `POST /api/private-continuity/conflict/restore?confirm=true`：误解决时恢复最近快照。

## 模型侧：quarrel 工具（2026-10-03 起唯一写入口）

抽屉只放在 OB，不再有本地 `.conflict-unresolved` 文件、文件同步器和冲突钩子。
CC 和 Home SDK 都用 MCP 工具 `quarrel`：

- `quarrel(action="read")`：读当前没和好的架。
- `quarrel(action="write", content=..., expected_revision=...)`：新建或改写整份；
  抽屉已有内容时必须先 read 并带上版本号，两扇门不会互相覆盖。
- `quarrel(action="resolve", her_words=..., expected_revision=...)`：只有在她亲口
  同意和好后才能关，原话写进「上一份」恢复快照（不占抽屉正文长度），关和存在同一把锁里完成。

带钥匙的 `/internal/private-continuity/conflict` 只剩 `GET`：只回答开没开和版本号，
不回显正文，也不能写或关。早安 nudge 用它决定要不要先摊开冲突。

自动同步的状态只有正文 SHA-256 和 OB revision，保存在 root 可读的
`/home/node/grey-ws/.conflict-sync-state.json`。若 Dashboard 与本地文件各自修改，
定时任务拒绝覆盖并在 `/home/node/grey-ws/logs/private-continuity-sync.log` 留错误；
需人工对照后决定保留哪一版。当前 CC 会话不会因为上传立即重新读取 OB，
新窗口的 `breath()` 会读到它。

同步地址默认必须使用 HTTPS；`http://localhost`、`127.0.0.1`、`::1` 可用于本机
回环调试。其他明文 HTTP 会被拒绝，确有受控内网需求时才显式添加
`--allow-insecure-http`。

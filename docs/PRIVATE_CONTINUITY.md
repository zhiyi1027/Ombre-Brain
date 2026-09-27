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

## CC/Codex 文件同步

`scripts/sync-private-continuity.py` 默认读取
`/home/node/grey-ws/.conflict-unresolved`。普通运行只会创建或更新远端状态：本地
文件缺失绝不会被解释为“冲突已解决”。明确解决必须运行：

```bash
python3 scripts/sync-private-continuity.py \
  --url https://your-ombre.example/internal/private-continuity/conflict \
  --resolve --confirm RESOLVE
```

上传器先读取远端 revision，再带条件写入；两个客户端同时修改时，后写者收到
冲突错误而不是静默覆盖。写入响应和内部 GET 都不回显私有正文。

### 二号机自动上传

在二号机的 root 终端运行一次安装脚本。它先用现有
`/home/node/grey-ws/.ob-daily-note-token` 验证私有接口，再把上传器复制到 CC
的脚本目录，做一次初始同步，最后安装每分钟运行的 cron。令牌不会进入命令行、
cron 文件或日志。

```bash
sh /home/node/codex-ws/grey-codex/.worktrees/ob-upstream-backports/scripts/install-private-continuity-sync.sh
```

CC 仍要在确认有未解决冲突时建立或更新
`/home/node/grey-ws/.conflict-unresolved`；上传、后续修改同步和新窗口的
`breath()` 提醒会自动完成。定时任务只在本地正文改变时上传。文件消失只清理
本地同步标记，**不会**替双方宣告和好，也不会删除 OB 中的状态。双方确认解决时，
用 Dashboard 的「双方确认已解决」或明确运行上面的 `--resolve --confirm RESOLVE`，
并删除本地文件。Dashboard 已解决而旧文件仍在时，定时任务不会把它重新打开。

自动同步的状态只有正文 SHA-256 和 OB revision，保存在 root 可读的
`/home/node/grey-ws/.conflict-sync-state.json`。若 Dashboard 与本地文件各自修改，
定时任务拒绝覆盖并在 `/home/node/grey-ws/logs/private-continuity-sync.log` 留错误；
需人工对照后决定保留哪一版。当前 CC 会话不会因为上传立即重新读取 OB，
新窗口的 `breath()` 会读到它。

同步地址默认必须使用 HTTPS；`http://localhost`、`127.0.0.1`、`::1` 可用于本机
回环调试。其他明文 HTTP 会被拒绝，确有受控内网需求时才显式添加
`--allow-insecure-http`。

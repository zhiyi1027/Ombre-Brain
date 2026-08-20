# 每日连续性与昨日印象

每日连续性把 CC/Codex 的换窗交接便签作为私有输入，在每日边界后生成一张短小的“昨日印象”。它不是普通记忆桶，也不参与搜索、向量召回、dream、relations 或 feel。

## 时间规则

- 时区默认 `Asia/Shanghai`。
- 记忆日默认在凌晨 4 点切换，而不是零点。
- 便签必须以 `# YYYY-MM-DD 便签` 开头；标题日期就是 `memory_day`。
- 上传器根据文件 mtime 和 4 点切日规则校验标题日期。零点后、4 点前写完的便签仍归前一天。
- 日期归属由确定性程序完成；整理模型不能重新判断或排除输入日期。

## CC 固定日便签

当前 CC 使用一个每天覆盖更新的文件：

```text
/home/node/grey-ws/.daily-note
```

同一天的每次 autoswap 都更新同一个 OB 记录：

```text
cc-daily-note:YYYY-MM-DD
```

内容哈希不变时写入是幂等的；内容变化时覆盖当天 revision，并使已有日印象变为待重整。OB 只给模型当天最后收到的版本，不堆叠所有中间快照。

## 上传

服务端设置一个长随机密钥：

```text
OMBRE_DAILY_NOTE_TOKEN=<secret>
```

CC VPS 把同一个密钥以 mode `0600` 写入：

```text
/home/node/grey-ws/.ob-daily-note-token
```

在 autoswap 的“更新便签”和 `touch /tmp/autoswap-ready.flag` 之间执行：

```bash
python3 /path/to/sync-daily-note.py \
  --url https://your-ombre.example/internal/daily-notes
```

完整顺序：

```text
保存长期记忆
→ 原子更新 /home/node/grey-ws/.daily-note
→ 执行 sync-daily-note.py
→ 确认已上传或安全进入本地重试队列
→ touch /tmp/autoswap-ready.flag
```

上传器不会打印便签正文。临时网络失败时，它只在 mode `0600` 的 spool 中保留每个 `note_id` 的最新 revision；下一次调用自动补传。某一天的坏条目不会挡住其他日期上传。鉴权、格式或日期错误会保留待检查的条目并返回非零状态，不能伪装成已排队。

私有入口：

```text
POST /internal/daily-notes
Authorization: Bearer <OMBRE_DAILY_NOTE_TOKEN>
```

返回值只包含 `note_id`、日期、哈希和状态，不回显正文。

## 生成与读取

后台默认每 5 分钟检查最近 7 个已结束记忆日。只有 source revision 或 prompt 版本变化才调用一次配置的压缩 LLM；默认复用 `dehydration.model`，不另配模型。

生成结果保存在 `<vault>/daily_continuity/`，不位于 BucketManager 的活动目录。启动 `breath()` 在核心准则之后、最近记忆之前插入前一个已结束记忆日的成品；硬预算放不下时只给显式读取指针。

显式读取最近日印象：

```text
breath_advanced(domain="daily_impression")
```

## Dashboard 查看与修订

登录 Dashboard 后打开「日印象 / Daily」：

- 日期列表只读取摘要，选择某天后才加载原始便签正文。
- 原始 CC/Codex 便签只读，并显示接收时间与内容哈希。
- 人工编辑不会覆盖当前 DS 原稿；“当前用于 Breath 的版本”可以单独修订。
- 人工版本保存在独立 `overrides/` 目录，`breath()` 与显式日印象读取优先使用它。
- DS 因迟到来源重新生成时不会覆盖人工版本；Dashboard 会标出“DS 已更新”。
- “恢复 DS 版本”会先把人工版本保存进 `override_history/` 再取消覆盖，不做物理抹除。

Dashboard API 均要求登录态：

```text
GET    /api/daily-continuity
GET    /api/daily-continuity/{memory_day}
PATCH  /api/daily-continuity/{memory_day}/impression
DELETE /api/daily-continuity/{memory_day}/impression?confirm=true
```

## 模型边界

日印象使用独立版本化提示词 `daily-impression-v1`。输出中的每一项都必须引用输入 source ID；未知或空来源的项会被程序丢弃。第一人称感受没有明确资料依据时必须省略。模型只负责筛选、合并和压缩，不负责决定日期，也不能替主体创造感受。

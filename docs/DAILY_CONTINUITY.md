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

后台默认每 5 分钟检查最近 7 个已结束记忆日。候选日期来自便签和当天普通记忆；从配置的 `bucket_fallback_start_day`（当前为 `2026-08-20`）开始，即使便签漏传，只要当天已有可用记忆或 feel，仍会生成日印象，并在 Dashboard 明确标注“便签缺失”。更早日期不会因为启用兜底而历史回填；已经存在的 v2 日印象也不会只因提示词升级被重写，只有其来源真的变化时才升级为 v3。只有 source revision 或 prompt 版本变化才调用一次配置的压缩 LLM；默认复用 `dehydration.model`，不另配模型。可见正文以 450–650 token 为目标，最多保留 4 条事件、3 条未完事项和 3 条明确感受；整张成品约 900 token 硬预算，只按完整条目取舍，不截断句子。内部 JSON 输出上限默认 1400 token，为 `source_ids` 等结构开销留余量。

当天明确写下的 feel 可以作为“我留下的感觉”的证据输入，但 feel 本身仍不进入普通搜索、向量召回或被动浮现。模型读取的长来源受输入预算限制；staleness revision 使用完整正文哈希，因此来源在模型切片范围之外发生变化时也会触发重整。

生成结果保存在 `<vault>/daily_continuity/`，不位于 BucketManager 的活动目录。启动 `breath()` 在核心准则之后、最近记忆之前插入前一个已结束记忆日的成品；硬预算放不下时只给显式读取指针。

显式读取最近日印象：

```text
breath_advanced(domain="daily_impression")
```

## Dashboard 查看与修订

登录 Dashboard 后打开「日印象 / Daily」：

- 日期列表只读取摘要，选择某天后才加载原始便签正文。
- 原始 CC/Codex 便签只读，并显示接收时间与内容哈希。
- DS 原稿下方保留每一条生成内容对应的 `source_ids`；便签来源显示标签，普通记忆、计划和 feel 来源可以直接打开原桶核对。
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

日印象使用独立版本化提示词 `daily-impression-v3`。所有条目必须从当事人“我”的第一人称视角书写，伴侣称为“知知”或“她”，不得使用“用户/助手/AI/顾凛认为”等旁观者口吻。视角转换不等于补写心理活动：第一人称感受没有明确资料依据时仍必须省略。每一项都必须引用输入 source ID；未知或空来源的项会被程序丢弃。通过预算筛选后实际保留的逐条来源映射与正文一同落盘，不把未采用来源伪装成引用。模型只负责筛选、合并和压缩，不负责决定日期，也不能替主体创造感受。

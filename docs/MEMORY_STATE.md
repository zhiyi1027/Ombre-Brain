# 当前事实与历史状态

普通记忆默认仍是独立的历史记录。只有少数会变化、会争夺“当前答案”的事实，才使用可选的状态链元数据：

```yaml
state_key: project:kiwi-mem
superseded_by: <new_bucket_id>
superseded_at: 2026-08-20T...
```

`state_key` 是自由填写的稳定标识，不预设分类体系。建议使用短小、可复用的形式，例如 `project:kiwi-mem`、`health:current-medication`。普通事件、感受、对话片段不需要它。

## 显式确认流程

新状态先作为独立桶写入：

```text
hold(
  content="kiwi-mem 已经作废",
  importance=7,
  state_key="project:kiwi-mem"
)
```

如果同 key 仍有其他“当前版本”，返回值只列出候选，不自动修改。确认旧事实确实失效后，再单独调用：

```text
trace(
  bucket_id="<old_bucket_id>",
  state_key="project:kiwi-mem",
  superseded_by="<new_bucket_id>"
)
```

撤销错误标记：

```text
trace(bucket_id="<old_bucket_id>", superseded_by="\clear")
```

Dashboard 的桶详情提供同样的“标记为历史版本 / 撤销历史标记”操作，并要求二次确认。

## 读取行为

- `breath()`、dream、importance 批量读取和私有 breath hook 不再被动浮现历史版本。
- `breath_search()` 与完整 bucket ID 定位仍可读取历史版本，输出明确包含 `historical_state`、`state_key` 和 `superseded_by`。
- Dashboard 保留并显示历史桶；旧正文、embedding 和来源都不会删除。
- 搜索只在通过原相关性门槛后对历史版本降权，当前版本优先；历史版本仍可显式查到。

## 安全边界

- 仅支持普通 `dynamic` / `permanent` 记忆；plan 使用自身的状态和老化提醒，不进入此机制。
- 模型不能自动建立取代关系；`hold` 最多提示候选，写入必须是第二次显式操作。
- 自指、不同 `state_key`、目标已是历史版本、目标不存在和循环链全部拒绝。
- 不迁移全库。旧桶在实际遇到状态变化时再逐步补 key 和关系。

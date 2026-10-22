# OB MCP 工具瘦身：撤注册前调用核对

基线：OB `main` `c20bb139`，grey-kit `main` `9a6b064`，Home `main` `bb59163`。本表只核 Git 跟踪的程序、配置和说明；没有读取记忆正文、会话、令牌或生产日志。仓库存在不代表两台机器当前正在使用，部署前须再核 live 配置。

## 拟撤与合并入口的仓库调用

| 位置 | 找到的调用或文字 | 处理 |
| --- | --- | --- |
| `grey-kit/scripts/ob_recall_hook.py:121-123` | MCP `tools/call` 明确调用 `breath_search` | 保留工具；自动召回钩子无需换名 |
| `grey-kit/scripts/nudge.sh`、`scripts/nightswap.sh`、`scripts/nightswap1.sh`、`scripts/home_sdk_nightly.sh` | 未找到对拟撤工具的直接调用；夜间提示明确不额外跑 `dream` | 不修改运行脚本；现场配置仍待核 |
| `grey-kit/settings.json:115-120` | 旧 `mcp__claude_ai__pulse/dream/grow` 许可名 | 工具下架时应同步清理配置；此分支不改 grey-kit |
| `grey-kit/scripts/ob_image_memory.py:335` | 成功提示仍教 `media_read(bucket_id, index=0)` | 合并图片工具后须改提示；此分支不改 grey-kit |
| `grey-kit/CLAUDE.md:191` | 指引调用 `raw_search`、`breath_advanced(domain="feel")` | 应改为新原文工具和 `breath_search`；此分支不改 grey-kit |
| `grey-kit/docs/ob-manual-v2.7.6.md` | 列出旧工具名、用途和旧用量 | 需另版手册；本次数字以知知提供的 9-23 至 10-22 二号机用量为准 |
| Home `main` 的 SDK、提醒、夜间代码 | 未找到 `breath_advanced/raw_day/raw_search/media_catalog/media_read` 或拟撤工具的直接调用；`chat_ai.py` 保留的 OB 桥接只用 `breath`/`hold` | 不动 Home；上线前复查运行机部署版本 |
| 本仓 `docs/CLAUDE_PROMPT.md`、`README.md` | 工具列表、旧调用指引 | 与注册表同步更新 |
| 本仓 `src/ombrebrain/protocol/public_tools.py` 和 `tests/test_mcp_tools_docker_integration.py` | 公共工具契约及 19 工具集成断言 | 与新注册表同步更新；保留内部业务实现的单元测试 |

逐一 `git grep` 的范围包括 grey-kit 的 `scripts/`、`vps/`、`.claude/`、设置样板，Home `main` 的 SDK、服务、路由和脚本，以及本仓 `src/`、`tests/`、`docs/`。本轮没有核两台机器实际加载的私人配置、定时任务和 Zeabur 卷内热更新版本。静态未命中不等于生产现场没有调用。

## 数量边界

本次先撤 `dream`、`anchor`、`release`、`grow`、`pulse` 五个注册；合并两对读工具为各一个；把 `breath_advanced` 的必要通道迁入 `breath_search` 后撤注册。信件两个入口保留，所以此阶段从 19 变为 **11**。Home 已开封信件只读入口和五封信迁移完成、另行验收后，才可能再撤两封信工具，届时才是 **9**。

撤注册不删除记忆桶、不删除内部 `grow`/`dream` 等代码；自动夜间梦境和原文库按各自服务运行。主线合并会触发 Zeabur 部署，本分支不合并、不部署。

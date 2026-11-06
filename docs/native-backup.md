# 本机入口与密封备份

`src/native_host.py` 仅供 grey-kit 的 `runtime/ob_native.py` 管理。普通
`server.py` / Zeabur 入口不变。本机入口要求父进程、独立进程组、继承的
127.0.0.1 socket 和私有开关全部匹配；父进程丢失或开关关闭会终止自己的
进程组。不开启后台时，也禁止 breath 的延迟衰减启动。不要直接运行此入口。

`src/local_backup.py` 是不导入服务器的标准库备份核心，具体调用和本机
配置由 grey-kit 提供。它不读取模型密钥或登录身份，也不调用模型。

默认备份 permanent、dynamic、archive、feel、plans、letters、
daily_continuity、private_continuity、nightly_dreams、_media，以及
embeddings.db 和 .night_clerk/clerk.db。只有显式 `include_raw=True`
才包含允许的 _raw 数据文件。根配置、认证状态、队列、日志、运行代码不在
此清单；业务正文里曾引用的秘密不会被内容扫描或自动清除。

SQLite 使用在线 backup API，包含已提交 WAL；普通文件每个单独检查稳定性。
这是逐文件一致的备份，**不是整库跨文件原子快照**。正式切换前仍需停止旧端
写入并做最后同步。文件数量 50,000、单文件 512 MiB、总展开体积 2 GiB
为当前上限；超限应调整设计，不能忽略错误。

包内私有清单记录相对路径、大小和 SHA-256。恢复先固定源文件描述符、复制并
核对整个包 SHA，再检查路径、大小、成员摘要、重复项、大小写与 Unicode
碰撞、链接和 SQLite 侧文件。只恢复到全新私有目录，不覆盖在用 vault，
不自动导入或启动业务。清单和正文都不应输出到聊天、日志或 Git。

`seal_snapshot` 只用指定 age 程序和公钥收件人。实际备份先生成明文包并做
恢复核验，再加密，正常完成或可捕获异常后删除临时明文。强杀、断电可能遗留
私有临时明文，下次运行须先核对。它不接触私钥或恢复口令。
明文恢复成功与密文真正解密恢复是两项独立验收：必须在受保护私钥可用时，
将实际密文解密到私有暂存目录，用收据中的 snapshot.archive_sha256 恢复核验后，
才可声称该密文可恢复。不要因此提前删除旧恢复材料。

生成数据验证（age 密钥使用临时测试身份）：

```sh
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 \
  OB_TEST_AGE=/absolute/path/age OB_TEST_AGE_KEYGEN=/absolute/path/age-keygen \
  python -B -m unittest discover -s tests -p test_local_backup.py -v
PYTHONPATH=src python -B -m unittest discover -s tests -p test_native_host.py -v
```

缺少 age 时相应加密用例会跳过；报告检查结果时应明确这一点。进程组检查
需要 POSIX，并需要允许管理测试自身进程。测试不使用真实 vault 或登录身份。

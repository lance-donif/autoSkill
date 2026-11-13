# 保留数据的离线升级

用户要求升级时，按 `upgrade plan → prepare → apply → status` 执行完整流程，不停在建议复制文件。使用匹配本机平台的完整包，target 指向旧安装根目录。当前工作区和标准 workspace 自动纳入；额外外置目录及宿主配置用重复的 `--workspace`、`--config` 明确提供。

先读 plan 的版本、数据目录和 blockers。prepare 将新包深复制到安装目录外的持久升级任务目录，校验 SHA-256，并备份整个工作区，包括 SQLite、运行证据、明文凭据与登录态；不会用空白新 workspace 覆盖旧数据。使用 `--stop-services` 时，先返回持久 journal 和 launched，再由独立 helper 等当前短 CLI 退出后处理服务与备份，避免先关闭承载请求的后台而丢收据。查询 status 到 prepared 再 apply；同一次升级始终继续原 journal。

apply 返回 `launched` 后，当前 CLI 退出，独立升级器才使用新包运行时进行切换；HTTP 也先退出专用短命令进程。随后用 status 查询实际结果，同时检查 last_action 和 error，不能把前次 completed 当成最近一次回滚或恢复已经成功。升级器会校验新安装，并执行 verify/doctor；失败或中断按 journal 恢复，不能把进程启动成功当成升级完成。

服务仍运行时，可在已获升级授权的任务中使用 `--stop-services` 处理本安装的 autoSkill 服务；该选项不关闭 DSH，也不取消正在进行的业务。活动运行应先结束，继续同一任务，不重新创建一份升级。自动模式的 `upgrade status` 直接只读 journal，不依赖已关闭的 bridge，也不初始化工作区数据库；显式 `AUTOSKILL_BRIDGE=on` 仍表示强制转交。也可使用包内 `autoskill-upgrade.py status <journal>` 读取真实状态；缺少响应不证明切换未发生。

需要恢复旧程序时运行 `upgrade rollback <journal>`；异常中断先 `upgrade recover <journal>`。旧目录、失败目录、完整快照和日志保留作恢复依据；回滚也会保存当前数据，不丢掉升级后产生的用例或运行记录。除非用户另有明确要求，不自行清理这些恢复资料。

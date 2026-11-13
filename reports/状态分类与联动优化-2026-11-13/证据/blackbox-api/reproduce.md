# 复现入口

所有工具脚本只调用公开 CLI/API，不导入产品内部模块。产品命令固定使用主任务授权的公开系统 Python module 入口：

```sh
PYTHONPATH=/workspace/autoSkill/skills/autoskill/app \
AUTOSKILL_WORKSPACE=/workspace/autoskill-state-validation/blackbox-api/workspace-<轮次> \
AUTOSKILL_HEADED=0 python3 -m autoskill --session blackbox-api-<轮次> <命令>
```

先运行本目录自己的HTTP fixture：

```sh
python3 /workspace/autoskill-state-validation/blackbox-api/fixture.py --port 18882
```

收到产品冻结通知后，在全新数据根重复同一组验收任务：

```sh
python3 /workspace/autoskill-state-validation/blackbox-api/revalidate.py --round retest --port 18883
```

脚本遇到已存在的数据根会拒绝覆盖。每轮写入 `evidence/<轮次>/`，workspace和session独立；只在自身隔离工作区启动仪表盘，用后停止。原生任务按实际队列查询，观察取消、稳定终态、独立业务失败、失败重跑及旧结果不变。报告损坏只删除已公开返回的自身Excel文件，原文件先备份，通过task-report手动retry恢复。

工作流提交依照每次 next 返回的 item/attempt/lease，输入文件来自 `inputs/`。首个execute推进之后保留task/schema供人工阅读；若出现新的宿主任务，继续按公开schema提交业务诊断，不能把“脚本已结束”当作工作流完成。若执行发布报错，保存requirements/materialization/cases/status，保持失败。

基线全部原始命令已在 `evidence/baseline/*.json` 的command字段给出。`bb.py --label <名称> cli -- <命令>` 可执行并保存单条；`wf_step.py` 根据已捕获的next回执提交，不需要手抄租约字段。

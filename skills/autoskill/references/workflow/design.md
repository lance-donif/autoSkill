# 需求、设计与遗漏补齐

从 `workflow next` 返回的输入引用开始，只读取当前工作项需要的资料。大文档用 `workflow read --head <固定版本> --text-offset <下一偏移>` 分段读取；用例和知识也用分页接口，不把数千条定义一次塞进上下文。

来源是业务期望的依据，当前系统响应是观察。提取需求时保留稳定 id、来源关联、原意和未知点；不要把文档没有提供的规则写成已知事实。sources 阶段保留原附件 raw_ref，再提交提取正文；requirements 阶段提交 `requirements:[{id,text,source_ids}]`。

design/completion 阶段提交 `scenarios` 和 `oracles`：场景引用 requirement_ids/oracle_ids，判定条件引用需求。覆盖正常流程、边界、失败及权限等与实际需求有关的分支，不机械生成无依据的组合。代码会发现缺失映射并派发 completion；宿主还需检查语义遗漏，字段齐全不能证明自然语言全部覆盖。

例如需求明确“优先级应为 high”，判定仍保持 high。页面或接口返回 medium 应保存为缺陷，不得改预期为 medium。所有提交使用领取时的 lease/attempt 和稳定幂等标识；内容版本冲突时读新版本再合并，不直接覆盖其他任务的结果。

新需求或变更维护应形成新的领域版本，旧场景、失败和证据保留。当前阶段可提交的具体字段始终以 `payload_schema` 为准。

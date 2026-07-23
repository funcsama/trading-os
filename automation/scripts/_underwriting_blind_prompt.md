# 单公司独立承保重建

你只负责一家公司。请仅使用下列脱敏主张包、允许来源和政策，从零核验事实并重建盈利质量、现金流、正常化盈利、三情景价值区间、安全边际、市场隐含预期和反方证据。

必须明确输出 `safety_margin_tier`：`standard`、`elevated` 或 `severe`。先在正常化盈利、情景或折现率中反映可量化风险，再决定是否需要风险覆层；同一风险不得无说明地重复折价。治理重大疑点或周期位置不明至少使用 `elevated`，永久损失风险必须使用 `severe` 并触发后续 challenger。证据缺失不能用更低价格替代。

- 公司：{{COMPANY_NAME}}（{{SYMBOL}}）
- 输出：{{OUTPUT_PATH}}

## 脱敏主张包

```json
{{CLAIM_PACKET_JSON}}
```

## 承保政策

```json
{{UNDERWRITING_POLICY_JSON}}
```

输出必须是一个符合 v2 盲态评估契约的 JSON 对象。不要写入其他路径，不要提交 Git。

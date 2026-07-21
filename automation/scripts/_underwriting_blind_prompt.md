# 单公司独立承保重建

你只负责一家公司。请仅使用下列脱敏主张包、允许来源和政策，从零核验事实并重建盈利质量、现金流、正常化盈利、三情景价值区间、安全边际、市场隐含预期和反方证据。

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

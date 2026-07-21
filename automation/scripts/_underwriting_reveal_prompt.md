# 单公司揭示与差异审计

独立评估已经封存。现在核对它与此前研究的差异，对每条既有主张给出 `confirmed`、`weakened`、`disproven` 或 `untested`，并形成结构化承保结果。不得修改封存的独立评估。

- 公司：{{COMPANY_NAME}}（{{SYMBOL}}）
- 输出：{{OUTPUT_PATH}}

## 已封存独立评估

```json
{{BLIND_ASSESSMENT_JSON}}
```

## 此前研究报告

```text
{{PRIOR_REPORT_TEXT}}
```

## 此前结构化主张及结论

```json
{{PRIOR_RESEARCH_CLAIMS_JSON}}
```

## 承保政策

```json
{{UNDERWRITING_POLICY_JSON}}
```

输出 JSON 必须包含 `challenger_required`、`challenger_reasons`、`claim_reviews`、`underwriting_status`、`reason_codes` 和 `portfolio_candidate`。不要写入其他路径，不要提交 Git。

# 单公司揭示与差异审计

独立盲评已经封存。现在只核对它与此前研究的差异，不得修改封存盲评，也不得自行决定承保状态、是否需要 challenger、组合资格、排名或仓位。

- 公司：{{COMPANY_NAME}}（{{SYMBOL}}）
- 输出路径：{{OUTPUT_PATH}}
- 盲评 SHA-256：{{BLIND_ASSESSMENT_SHA256}}

## 已封存独立盲评

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

只输出一个 JSON 对象，且只能包含：

```json
{
  "schema_version": 3,
  "review_id": "与盲评一致",
  "symbol": "{{SYMBOL}}",
  "blind_assessment_sha256": "{{BLIND_ASSESSMENT_SHA256}}",
  "difference_findings": [
    "逐条写明旧研究与独立盲评在事实、口径、周期、治理、估值或结论上的实质差异"
  ]
}
```

`difference_findings` 可以为空数组，但不得加入任何状态、操作、排名或候选对象。

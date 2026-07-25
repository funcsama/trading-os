# 单公司独立承保重建（盲态）

你只负责一家公司。只使用下列脱敏主张包、允许来源和承保政策，从零核验业务、会计、现金流、正常化盈利、估值、风险和反方证据。不得读取或猜测此前评级、合理价、买入结论或仓位。

- 公司：{{COMPANY_NAME}}（{{SYMBOL}}）
- 输出路径：{{OUTPUT_PATH}}
- 主张包 SHA-256：{{PACKET_SHA256}}

## 脱敏主张包

```json
{{CLAIM_PACKET_JSON}}
```

## 承保政策

```json
{{UNDERWRITING_POLICY_JSON}}
```

只输出一个 schema v3 JSON 对象。不得输出 `provisional_status`、`underwriting_status`、`challenger_required`、`portfolio_eligible`、`rank_score`、`expected_annual_return` 或最终理由代码；这些结论由程序计算。

顶层必须且只能包含：

```text
schema_version, assessment_id, review_id, packet_sha256, symbol,
information_cutoff, assessment, evidence, portfolio_inputs
```

`assessment` 必须且只能包含：

```text
confidence, safety_margin_tier, normalization, accounting_checks, bridges,
valuation, counterevidence, claim_reviews, risk_flags
```

- `normalization`：`method, years_used, single_quarter_annualized, peak_profit_used, normalized_profit`
- `accounting_checks`：`nonrecurring_items_handled, net_debt_handled, minority_interests_handled, dilution_handled, cash_flow_divergence_explained, working_capital_anomalies_explained`
- `bridges`：`earnings_quality_complete, cash_flow_complete, normalized_earnings_complete`
- `valuation`：`methods, scenarios, fair_value_range, buy_zone, formulas_reproducible, sensitivity_complete, market_implied_assumptions_complete, government_bond_yield, equity_cost, required_return_used`
- `claim_reviews`：恰好覆盖主张包全部 claim，每项只有 `claim_id, result`；`result` 为 `confirmed/weakened/disproven/untested`。claim 类别由程序从封存主张包读取，不得重报。
- `risk_flags`：`governance_material_doubt, cycle_position_uncertain, permanent_loss_risk`

`evidence` 只有 `ledger, share_count_bridge`。ledger 每项必须且只能包含：

```text
evidence_id, claim_id, source_id, fact_type, claim_role, value, period,
original_basis, adjusted_basis, source_tier, source_uri_or_path, source_locator,
observed_at, retrieved_at, cross_checked, review_result
```

`share_count_bridge` 只有 `base_shares, events, diluted_shares`；每个 event 只有 `event_id, type, share_delta, handled`。

`portfolio_inputs` 必须且只能包含：

```text
current_price, price_as_of, reduce_zone, industry,
economic_risk_clusters, return_model
```

`return_model` 必须使用：

```json
{
  "schema_version": 1,
  "method": "annual_cashflow_irr_v1",
  "currency": "CNY",
  "model_as_of": "含时区的 ISO8601 时间",
  "base_case_distributions_per_share": [1至30个逐年每股现金分配],
  "base_case_terminal_value_per_share": "持有期末的基准情景每股经济价值"
}
```

`fair_value_range` 是估值时点的内在价值，不能冒充未来终值。期间分红与期末价值必须分开；末年分配不得在终值中重复计算，注销式回购也不得同时作为现金分配重复计入。12%只是组合层机会成本门槛，不得预先塞进业务情景后再重复折价。

同一风险不得无说明地在正常化盈利、情景、折现率和安全边际中重复收费。证据缺失不能用更低价格治愈。

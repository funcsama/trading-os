# Sources for 上海新阳 (CN:300236)

## Primary Sources (Financial Statements)

### Company Official
- 上海新阳官网 (https://www.sinyang.com.cn/) - 公司发展历程、产品线、新闻
  - 获取方式：curl https://www.sinyang.com.cn/
  - 获取日期：2026-07-08

### Eastmoney Datacenter APIs (Primary Financial Data)
所有API均通过 Eastmoney HSF10 数据中心获取
- 上海新阳利润表 (RPT_F10_FINANCE_GINCOME): filter=SECUCODE="300236.SZ"
  - URL: https://datacenter.eastmoney.com/securities/api/data/v1/get?reportName=RPT_F10_FINANCE_GINCOME
  - 覆盖期间：2022年报 - 2026Q1
- 上海新阳资产负债表 (RPT_F10_FINANCE_GBALANCE): 同上
  - 覆盖期间：2024中报 - 2026Q1
- 上海新阳现金流量表 (RPT_F10_FINANCE_GCASHFLOW): 同上
  - 覆盖期间：2024中报 - 2026Q1
- 上海新阳主营业务构成 (RPT_F10_FN_MAINOP): 同上
  - 覆盖期间：2024年报 - 2025年报
- 上海新阳公司概况 (HSF10 CompanySurvey): code=SZ300236
  - 含公司全名、行业、注册资本、上市日期、管理人员等

### Sina Finance
- 上海新阳前十大股东页面 (vCI_StockHolder): http://vip.stock.finance.sina.com.cn/corp/go.php/vCI_StockHolder/stockid/300236.phtml
  - 截至2025-12-31数据，公告日期 2026-03-13
- 上海新阳财务指南 (vFD_FinancialGuideLine): 每股收益、每股净资产、ROE等指标
- 上海新阳主要股东页面：股东总数 46,143，平均持股数 6,791股

### Tencent Quote API (Real-time Market Data)
- URL: http://qt.gtimg.cn/q=sz300236
  - 当前股价、总股本3.1338亿、流通股本2.7855亿
  - 数据日期：2026-07-08 15:35

## Competitor Financial Data (via Eastmoney)
- 安集科技 (688019.SH) - CMP抛光液+清洗液龙头
- 江丰电子 (300666.SZ) - 溅射靶材龙头
- 鼎龙股份 (002192.SZ) - CMP抛光垫+光刻胶
- 彤程新材 (603650.SH) - 光刻胶龙头
- 雅克科技 (002407.SZ) - 材料平台

## Stock Quotes (Tencent)
- 股价数据：http://qt.gtimg.cn/q=sz300236, sh688019, sz300666, sz002192, sh603650, sz002407

## Dates
- 数据获取日期：2026-07-08
- 报告完成日期：2026-07-08

## Notes on Data Reliability
- 财务数据：高置信度（一手财报数据）
- 股东数据：高置信度（截至2025-12-31公司公告）
- 行业市场规模（"中国半导体材料市场120亿美元"）：未核验，置信度中等
- 国产化率<20%：未核验，置信度中等

# -*- coding: utf-8 -*-
import urllib.request, json, ssl, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
def fetch(url, headers=None, timeout=30):
    req=urllib.request.Request(url, headers=headers or {"User-Agent":UA,"Referer":"https://emweb.securities.eastmoney.com/"})
    try:
        with urllib.request.urlopen(req,timeout=timeout,context=ctx) as r:
            d=r.read()
            for enc in ("utf-8-sig","utf-8","gbk"):
                try: return d.decode(enc)
                except: continue
            return d.decode("gbk",errors="replace")
    except Exception as e: return f"__ERR__:{type(e).__name__}:{e}"

# Full main financial data, 24 reports
url="https://datacenter.eastmoney.com/securities/api/data/get?type=RPT_F10_FINANCE_MAINFINADATA&sty=ALL&filter=(SECUCODE%3D%22600667.SH%22)&p=1&ps=24&sr=-1&st=REPORT_DATE"
t = fetch(url)
open("research/companies/CN/600667/sources/_fin_full.json","w",encoding="utf-8").write(t)
print("saved, len=", len(t))
j = json.loads(t, strict=False)
print("Year | Rev(亿) | RevYoY% | ParentNP(亿) | ParentNPTZ% | DeductNP(亿) | ROE% | GPM% | NPM% | OCF/share | DebtRatio% | TotalShare(亿)")
for row in j["result"]["data"]:
    rev = (row.get("TOTALOPERATEREVE") or 0)/1e8
    revyoy = (row.get("TOTALOPERATEREVETZ") or 0)*100
    pnp = (row.get("PARENTNETPROFIT") or 0)/1e8
    pnptz = (row.get("PARENTNETPROFITTZ") or 0)*100
    dnp = (row.get("KCFJCXSYJLR") or 0)/1e8
    roe = row.get("ROEJQ") or 0
    gpm = row.get("XSMLL") or 0
    npm = row.get("XSJLL") or 0
    ocfps = row.get("MGJYXJJE") or 0
    debt = row.get("ZCFZL") or 0
    ts = (row.get("TOTAL_SHARE") or 0)/1e8
    print(f"{row.get('REPORT_DATE_NAME', row.get('REPORT_DATE'))} | {rev:.2f} | {revyoy:.2f} | {pnp:.4f} | {pnptz:.2f} | {dnp:.4f} | {roe:.2f} | {gpm:.2f} | {npm:.2f} | {ocfps:.4f} | {debt:.2f} | {ts:.4f}")

# -*- coding: utf-8 -*-
"""回测数据抓取：沪深300 当前成分股 20 年原始价日K（不复权 + 成交额）。

为什么用不复权：回测要做「现价 vs 当日均价(成交额/成交量)」的 VWAP 比较
（旧判定规则的输入），两者必须同口径；前复权价早段会因大额分红变负/失真。
除权日跳空对旧/新两套判定规则影响等同，公平对比不受影响（结论写进报告）。

可续跑：已有 CSV 的股票直接跳过。跑法：python scripts/bt_tbias_fetch.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["NO_PROXY"] = "*"

import akshare as ak                                    # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "bt_tbias")
os.makedirs(OUT, exist_ok=True)

cons = ak.index_stock_cons_csindex("000300")
cols = list(cons.columns)
print("成分表列:", cols, flush=True)
code_col = next(c for c in cols if "代码" in c and "成分" in c or c == "成分券代码")
name_col = next(c for c in cols if "名称" in c)
codes = [str(x).strip().zfill(6) for x in cons[code_col]]
meta = [{"code": str(r[code_col]).strip().zfill(6), "name": str(r[name_col])}
        for _, r in cons.iterrows()]
json.dump(meta, open(os.path.join(OUT, "_cons.json"), "w", encoding="utf-8"),
          ensure_ascii=False)
print("成分股", len(codes), "只", flush=True)

fail = []
for i, code in enumerate(codes):
    f = os.path.join(OUT, f"{code}.csv")
    if os.path.exists(f):
        continue
    try:
        df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                start_date="20060101", end_date="20261108",
                                adjust="")
        if df is None or len(df) == 0:
            raise RuntimeError("empty")
        df.to_csv(f, index=False, encoding="utf-8")
    except Exception as exc:                            # noqa: BLE001
        fail.append({"code": code, "err": str(exc)[:120]})
        print(f"[{i}] {code} FAIL {str(exc)[:80]}", flush=True)
        time.sleep(1.5)
        continue
    if i % 20 == 0:
        print(f"[{i}/{len(codes)}] done {code}", flush=True)
    time.sleep(0.35)

json.dump(fail, open(os.path.join(OUT, "_fail.json"), "w", encoding="utf-8"),
          ensure_ascii=False)
print(f"ALL DONE: 成功 {len(codes) - len(fail)} / 失败 {len(fail)}", flush=True)

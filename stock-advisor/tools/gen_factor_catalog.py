"""从 Vibe-Trading registry 导出因子目录（主应用只读这份 JSON）。

    D:\\vibe-trading\\.venv\\Scripts\\python.exe tools/gen_factor_catalog.py

上游升级后重跑一次，diff 就是「算子集合变了什么」。主应用不在运行时 import 上游。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "integrations" / "factor_catalog.json"
UNIVERSES = ("equity_cn", "equity_hk")
PRICE_COLS = {"open", "high", "low", "close", "volume", "vwap", "amount"}


def main() -> int:
    from src.factors.registry import get_default_registry

    reg = get_default_registry()
    seen = {}
    for universe in UNIVERSES:
        for aid in reg.list(universe=universe):
            if aid in seen:
                seen[aid]["universes"].append(universe)
                continue
            alpha = reg.get(aid)
            m = alpha.meta
            cols = list(m.get("columns_required") or [])
            computable = (all(c in PRICE_COLS for c in cols)
                          and not m.get("requires_sector"))
            seen[aid] = {
                "id": aid,
                "zoo": str(getattr(alpha, "zoo", "") or aid.split("_", 1)[0]),
                "nickname": str(m.get("nickname") or ""),
                "themes": list(m.get("theme") or []),
                "columns": cols,
                "requires_sector": bool(m.get("requires_sector")),
                "universes": [universe],
                "warmup": int(m.get("min_warmup_bars") or 0),
                "decay": int(m.get("decay_horizon") or 0),
                "computable": computable,
                "notes": str(m.get("notes") or "")[:600],
            }
    try:
        ver = version("vibe-trading-ai")
    except Exception:                             # noqa: BLE001
        ver = "unknown"
    doc = {
        "upstream": "HKUDS/Vibe-Trading",
        "upstream_version": ver,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "src.factors.registry.get_default_registry()",
        "note": "computable=true 表示只需 open/high/low/close/volume/amount 且不需要行业分类；"
                "fund:/val: 字段当前数据面未提供。",
        "alphas": sorted(seen.values(), key=lambda x: x["id"]),
    }
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    n_cn = sum(1 for a in doc["alphas"] if "equity_cn" in a["universes"])
    n_hk = sum(1 for a in doc["alphas"] if "equity_hk" in a["universes"])
    n_c = sum(1 for a in doc["alphas"] if a["computable"])
    print("saved %s alphas=%d cn=%d hk=%d computable=%d"
          % (OUT, len(doc["alphas"]), n_cn, n_hk, n_c))
    return 0


if __name__ == "__main__":
    sys.exit(main())

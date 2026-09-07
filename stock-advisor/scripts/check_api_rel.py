"""验证按股票筛选时能命中关联新闻 + related 展示。筛 02513，看能否拿到茅台新闻里提到智谱的。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import list_news

rows = list_news(code="02513", limit=50)
print("02513 rows:", len(rows))
multi = [r for r in rows if len(r.get("related", [])) > 1]
print("multi-related:", len(multi))
for r in multi[:5]:
    print("-", r["related"], "|", r["title"][:56])

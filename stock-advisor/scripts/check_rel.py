"""单元验证交叉关联逻辑：模拟一条标题同时含"茅台"和"智谱"的新闻。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import news_fetcher
from news_fetcher import save_to_db, _save_relations
from app import get_conn

# 模拟 fetch_watchlist 里的交叉关联核心逻辑
stocks = [
    {"code": "600519", "name": "贵州茅台"},
    {"code": "02513", "name": "智谱"},
]
url_items, url_related = {}, {}
name_index = [(s["code"], s["name"],
               s["name"].replace("贵州", "").replace("股份", "").replace("-SW", ""))
              for s in stocks]
fake_results = [
    {"code": "600519", "title": "智谱发布新模型，茅台集团宣布战略合作",
     "url": "https://example.com/fake-cross-1", "source": "baidu", "media": "t"},
    {"code": "02513", "title": "智谱与茅台签署协议",  # 同 URL 第二次出现
     "url": "https://example.com/fake-cross-1", "source": "baidu", "media": "t"},
]
for s in stocks:
    for item in fake_results:
        if item["code"] != s["code"]:
            continue
        url = item["url"]
        if url in url_items:
            if s["code"] not in url_related[url]:
                url_related[url].append(s["code"])
        else:
            url_items[url] = item
            url_related[url] = [s["code"]]
        title = item["title"]
        for code, full, short in name_index:
            if code != s["code"] and code not in url_related.get(url, []) \
                    and ((full and full in title) or (short and short in title)):
                url_related.setdefault(url, []).append(code)

print("url_related:", url_related)
new = save_to_db(list(url_items.values()), url_related)
print("inserted:", new)

with get_conn() as conn, conn.cursor() as cur:
    cur.execute("SELECT code FROM sa_news_related WHERE url = %s ORDER BY code",
                ("https://example.com/fake-cross-1",))
    print("stored related:", [r[0] for r in cur.fetchall()])
    # 清理假数据
    cur.execute("DELETE FROM sa_news_related WHERE url = %s",
                ("https://example.com/fake-cross-1",))
    cur.execute("DELETE FROM sa_news WHERE url = %s",
                ("https://example.com/fake-cross-1",))
print("cleaned up")

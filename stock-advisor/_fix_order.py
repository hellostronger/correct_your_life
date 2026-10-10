# -*- coding: utf-8 -*-
"""修复 _store_article 中回复插入顺序：文章必须先于回复插入（外键约束）"""

with open('strategy_crawler.py', 'rb') as f:
    content = f.read()

# 在 "True))" 之后、"# 抓完就排进" 之前插入 _store_replies
old = b'True))\n    # \xe6\x8a\x93\xe5\xae\x8c\xe5\xb0\xb1\xe6\x8e\x92\xe8\xbf\x9b'
new = b'True))\n    # \xe6\x96\x87\xe7\xab\xa0\xe6\x8f\x92\xe5\x85\xa5\xe5\x90\x8e\xe6\x89\x8d\xe8\x83\xbd\xe6\x8f\x92\xe5\x9b\x9e\xe5\xa4\x8d\xef\xbc\x88\xe5\xa4\x96\xe9\x94\xae\xe7\xba\xa6\xe6\x9d\x9f sa_strategy_reply_article_id_fkey\xef\xbc\x89\n    _store_replies(cur, d["post_id"], replies, author=d.get("author", ""))\n    # \xe6\x8a\x93\xe5\xae\x8c\xe5\xb0\xb1\xe6\x8e\x92\xe8\xbf\x9b'

if old in content:
    content = content.replace(old, new)
    with open('strategy_crawler.py', 'wb') as f:
        f.write(content)
    print('OK: inserted _store_replies call')
else:
    print('FAIL: not found')
    # 找 True)) 后面 50 字节
    idx = content.find(b'True))')
    if idx >= 0:
        print('After True)): %r' % content[idx:idx+80])

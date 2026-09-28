"""config.yaml 的行级写入工具（供「调度设置」页改单个键/整段用）。

背景：config.yaml 里每个模块各写各的（app.py _write_conf 整块替换 news:、
llm_advisor.save_llm_conf 整块替换 llm:、notifier.save_notify_conf 整块替换
notify:），整文件重写会互相踩。这里的两个函数只做**外科手术式**改动：
- set_section_key：只改某段里的某一行（缺键在段内追加，缺段在文件尾追加），
  兄弟键、注释、其它段原样保留。
- replace_or_append_section：整段替换（渲染器与 save_llm_conf 同款）。

所有写操作走同一个 WRITE_LOCK：app.py 的 _write_conf（news 段）与本模块的
调度段写都是「读-改-写整文件」，并发 PUT 会丢段。本进程内只有 HTTP 线程与
守护线程会写，锁粒度粗一点没代价。
"""

import re
import threading
from pathlib import Path

import yaml

WRITE_LOCK = threading.Lock()


def _path(config_path) -> Path:
    """接受 str 或 Path（本模块的公开函数历史上只收 Path，调用方常传 CONFIG_FILE
    常量，但测试/脚本里很容易传字符串，统一在这里兜住）。"""
    return config_path if isinstance(config_path, Path) else Path(config_path)


def _fmt_value(value) -> str:
    """把 Python 值渲染成 YAML 行内标量（值已由调用方保证是标量）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    if s == "":
        return "''"
    # 带冒号/井号/引号/首尾空格的裸串会被 YAML 误解析，一律单引号转义
    if (s != s.strip() or any(c in s for c in ":#{}[]&*!|>'\"%@`")
            or s in ("true", "false", "null", "yes", "no", "on", "off", "~")
            or s.lstrip("-").replace(".", "", 1).isdigit()):
        return "'" + s.replace("'", "''") + "'"
    return s


def _find_section(lines: list[str], section_key: str):
    """返回 (start, end)：段首（含其上方紧邻注释行）到段尾（不含）的行号区间。

    段体 = 该顶层 key 行 + 其后所有缩进行/注释/空行，直到第一个非缩进的非注释行。
    注释头归本段（与现有 save_llm_conf / _write_conf 的锚定方式一致）。
    """
    start = next((i for i, l in enumerate(lines)
                  if l.rstrip() == f"{section_key}:" or l.rstrip() == f"{section_key}: "), None)
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j] and not lines[j][0].isspace() and not lines[j].startswith("#"):
            end = j
            break
    head = start
    while head > 0 and (lines[head - 1].startswith("#") or not lines[head - 1].strip()):
        head -= 1
    return head, end


def set_section_key(config_path, section_key: str, key: str, value) -> None:
    config_path = _path(config_path)
    """把某段里的某键设成 value（只动那一行；缺键追加到段尾；缺段追加到文件尾）。"""
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    lines = text.splitlines()
    rendered = f"  {key}: {_fmt_value(value)}"
    span = _find_section(lines, section_key)
    if span is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{section_key}:")
        lines.append(rendered)
    else:
        _, end = span
        for i in range(span[0] + 1, end):
            stripped = lines[i].strip()
            if stripped.startswith(f"{key}:"):
                lines[i] = rendered
                break
        else:
            # 段内追加：插在段尾第一个空行处（没有就放段末）
            tail = end
            while tail > span[0] + 1 and not lines[tail - 1].strip():
                tail -= 1
            lines.insert(tail, rendered)
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def replace_or_append_section(config_path, section_key: str, block_text: str) -> None:
    config_path = _path(config_path)
    """整段替换（block_text 是渲染好的多行文本，含注释头与 `section_key:` 行）。"""
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    lines = text.splitlines()
    block = block_text.rstrip("\n").splitlines()
    span = _find_section(lines, section_key)
    if span is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(block)
    else:
        lines[span[0]:span[1]] = [""] + block
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _flow_dict(line: str, key: str) -> dict | None:
    """行是 `key: {a: 1, b: 2}` 这种流式映射时，返回解析后的 dict；否则 None。"""
    m = re.match(rf"^(\s*){re.escape(key)}:\s*(\{{.*\}})\s*$", line)
    if not m:
        return None
    try:
        v = yaml.safe_load(m.group(2))
    except Exception:
        return None
    return v if isinstance(v, dict) else None


def _render_flow(indent: str, key: str, d: dict) -> str:
    """把流式映射重新渲染回一行（保持文件原有风格）。"""
    import yaml as _y
    body = _y.safe_dump(d, default_flow_style=True, allow_unicode=True,
                        sort_keys=False).strip()
    return f"{indent}{key}: {body}"


def set_nested_key(config_path, section_key: str, dotted: str, value) -> None:
    config_path = _path(config_path)
    """设置某段里的**嵌套**键，如
    ``set_nested_key(p, "news", "channels.baidu.min_interval", 3.0)``。

    为什么需要它：完整配置编辑器要能改 `notify.email.smtp_port` 这种多层路径，
    而 set_section_key 只认「段 + 一层键」。

    ⚠️ 必须同时认**块状**（``baidu:`` 换行缩进）与**流式**（``baidu: {enabled: true}``）
    两种写法。config.yaml 的 ``news.channels`` 就是流式的（2026-09-26 实测踩过）：
    只按块状找会「找不到 → 在段尾追加一个同名块」，于是同一个 key 出现两次，
    yaml 直接报 `mapping values are not allowed here`，**整个配置文件读不出来**。
    所以这里遇到流式就原地解析→改→重新渲染，不新增行。
    """
    parts = [p for p in str(dotted).split(".") if p]
    if not parts:
        raise ValueError("dotted 键不能为空")
    if len(parts) == 1:
        set_section_key(config_path, section_key, parts[0], value)
        return
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    lines = text.splitlines()
    leaf_indent = "  " * len(parts)
    rendered_leaf = f"{leaf_indent}{parts[-1]}: {_fmt_value(value)}"

    span = _find_section(lines, section_key)
    if span is None:
        # 段不存在：先建段，再在段里追加整条路径
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{section_key}:")
        start = len(lines) - 1
        end = len(lines)
    else:
        start, end = span[0], span[1]

    # 逐层下降
    lo, hi = start + 1, end
    missing_parent = None
    for depth in range(1, len(parts)):
        indent = "  " * depth
        key = parts[depth - 1]
        found = None
        for i in range(lo, hi):
            if not lines[i].startswith(indent) or lines[i].startswith(indent + " "):
                continue
            body = lines[i].strip()
            if body == f"{key}:":
                found = i
                break
            if body.startswith(f"{key}:"):
                # 流式映射：没有子键可下降，把它就地展开成块状再继续
                d = _flow_dict(lines[i], key)
                if d is None:
                    continue
                expanded = [f"{indent}{key}:"]
                for ck, cv in d.items():
                    expanded.append(f"{indent}  {ck}: {_fmt_value(cv)}")
                lines[i:i + 1] = expanded
                hi += len(expanded) - 1
                found = i
                break
        if found is None:
            missing_parent = (depth, key)
            break
        lo, hi = found + 1, _block_end(lines, found + 1, hi, len(indent))
    if missing_parent is not None:
        depth, key = missing_parent
        block = [""] + [f"{'  ' * depth}{part}:" for part in parts[depth - 1:-1]] \
            + [rendered_leaf]
        lines[hi:hi] = block
        config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    # 叶子层：块状就地替换 / 流式就地改值 / 都没有就在当前块末尾追加
    for i in range(lo, hi):
        stripped = lines[i].strip()
        if not stripped.startswith(f"{parts[-1]}:"):
            continue
        d = _flow_dict(lines[i], parts[-1])
        if d is not None:                      # 流式：改完仍渲染成流式
            d[parts[-1].split(".")[-1]] = value
            lines[i] = _render_flow(leaf_indent, parts[-1], d)
        else:
            lines[i] = rendered_leaf
        config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return
    lines.insert(hi, rendered_leaf)
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _block_end(lines: list[str], start: int, limit: int, parent_indent: int) -> int:
    """子块的结束行号：遇到第一个**缩进 ≤ 父级**的非空非注释行为止。

    ⚠️ 必须是「≤ 父级缩进」而不是「顶格」（2026-09-26 实测踩过）：只认顶格的话，
    ``channels:`` 的子块会一路吞掉后面 2 空缩进的 ``fetch_interval_minutes``，
    于是往 channels 里新增一个渠道时被插到了段外，YAML 报
    ``mapping values are not allowed here``，**整个配置文件读不出来**。
    """
    i = start
    while i < limit:
        l = lines[i]
        if l.strip() and not l.startswith("#") and _indent_of(l) <= parent_indent:
            break
        i += 1
    return i


def get_nested(conf: dict, dotted: str, default=None):
    """按点号路径从已解析的 dict 里取值，取不到返回 default。"""
    cur = conf
    for part in str(dotted).split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def write_many(config_path, section_block: tuple[str, str] | None = None,
               key_ops: list[tuple[str, str, object]] | None = None) -> None:
    """一次持锁完成「整段替换 + 若干单键手术」，中间不释放锁（防丢段）。

    key_ops 的 key 允许带点号（嵌套），如 ("news", "channels.baidu.enabled", True)。
    """
    with WRITE_LOCK:
        if section_block:
            replace_or_append_section(config_path, section_block[0], section_block[1])
        for section_key, key, value in (key_ops or []):
            if "." in str(key):
                set_nested_key(config_path, section_key, key, value)
            else:
                set_section_key(config_path, section_key, key, value)

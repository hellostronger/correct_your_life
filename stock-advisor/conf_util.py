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

import threading

WRITE_LOCK = threading.Lock()


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


def write_many(config_path, section_block: tuple[str, str] | None = None,
               key_ops: list[tuple[str, str, object]] | None = None) -> None:
    """一次持锁完成「整段替换 + 若干单键手术」，中间不释放锁（防丢段）。"""
    with WRITE_LOCK:
        if section_block:
            replace_or_append_section(config_path, section_block[0], section_block[1])
        for section_key, key, value in (key_ops or []):
            set_section_key(config_path, section_key, key, value)

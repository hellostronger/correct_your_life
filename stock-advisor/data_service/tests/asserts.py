"""测试断言。selftest 与 tests/* 共用，保证判定语义完全一致。

check 只有两种写法，含义明确：

    check("x", got, want)          值相等（want **可以是 None**）
    check("x", cond, note="说明")   条件为真 + 给人看的补充说明

两个会造成假失败的坑（都实际踩过，记在这里）：

1. **want 不能用 None 当默认值。**
   写成 `def check(name, got, want=None)` 时，
   `check("num(None)", None, None)` 会被当成「没传 want」→ 走条件分支 →
   `bool(None)` 为 False → 报 FAIL，而断言完全正确。
   我用 `want=None` 时一次造成 **13 个测试同时假失败**，排查了很久才定位到。
   必须用哨兵对象区分「未传」和「就是 None」。

2. **不要把第三个位置参数当「说明」传。**
   `check("x", cond, f"= {v}")` 是拿条件跟字符串比，永远 FAIL。
   说明一律用关键字 `note=`。
"""
from __future__ import annotations

PASS: list[str] = []
FAIL: list[str] = []

_MISSING = object()


def check(name, got, want=_MISSING, note: str = "") -> bool:
    if want is _MISSING:
        ok, want_repr = bool(got), "truthy"
    else:
        ok, want_repr = (got == want), repr(want)
    if ok:
        PASS.append(name)
        print(f"  PASS  {name}" + (f"   {note}" if note else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}\n          got  ={got!r}\n"
              f"          want ={want_repr}"
              + (f"\n          note={note}" if note else ""))
    return ok


def report(title: str = "结果") -> int:
    print()
    print("=" * 74)
    print(f"{title}: {len(PASS)} 通过 / {len(FAIL)} 失败")
    print("=" * 74)
    for f in FAIL:
        print("  x", f)
    return 1 if FAIL else 0

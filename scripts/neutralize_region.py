#!/usr/bin/env python3
"""地区限制中和 —— RDCL 0001 的替代实现（规划 §4.1）。

只中和「构成地区限制」的 network 判定，machine 判定原样保留：

    RegionUtils.init()                     -> 置空（不发起 ipwho.is 外呼）
    RegionUtils.isIsChinaMainlandNetwork() -> 恒 false（解除下载页整页禁用）
    RegionUtils.isIsChinaMainlandMachine() -> **不动**（下载源便利 / Terracotta
                                              节点与提示 / 离线认证特例保持与
                                              HMCL 完全一致）

为什么用脚本而不是 git patch：上游 583e004 与 8827fd0 对 init() 的实现不同
（后者删掉了 if 包裹块），静态 patch 的 hunk 上下文在另一个形态上会失配。
本脚本按**方法签名**定位，与方法体内容无关，因此对两种形态及未来演进均成立。

用法：
    python scripts/neutralize_region.py <RegionUtils.java> [--check]

    --check   只验证目标文件是否已处于中和状态，不做修改（CI 自检用）
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# 目标方法：签名正则 -> 替换后的完整方法体
METHODS: dict[str, str] = {
    r"public\s+static\s+void\s+init\s*\(":
        "public static void init() {\n"
        "        // RDCL: 地区中和 —— 不做网络地区预热（不发起 ipwho.is 请求）。\n"
        "    }",
    r"public\s+static\s+boolean\s+isIsChinaMainlandNetwork\s*\(":
        "public static boolean isIsChinaMainlandNetwork() {\n"
        "        // RDCL: 地区中和 —— 恒非中国大陆网络，解除下载页禁用。\n"
        "        return false;\n"
        "    }",
}

# 绝对不允许被本脚本改动的方法（保持 HMCL 行为）
PROTECTED = r"public\s+static\s+boolean\s+isIsChinaMainlandMachine\s*\("


class NeutralizeError(RuntimeError):
    """定位或替换失败 —— 直接终止构建，绝不静默跳过。"""


def find_method_span(source: str, signature: re.Pattern[str]) -> tuple[int, int]:
    """返回 (start, end) 偏移；方法体用花括号配对取出，找不到或不配对则报错。"""
    match = signature.search(source)
    if match is None:
        raise NeutralizeError(f"未定位到方法：{signature.pattern}")

    brace = source.find("{", match.end())
    if brace == -1:
        raise NeutralizeError(f"方法 {signature.pattern} 未找到方法体左花括号")

    depth, index = 0, brace
    while index < len(source):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return match.start(), index + 1
        index += 1
    raise NeutralizeError(f"方法 {signature.pattern} 花括号不配对")


def neutralize(source: str) -> str:
    """返回中和后的源码；任一方法未命中即抛错。"""
    if not re.search(PROTECTED, source):
        raise NeutralizeError("未找到 isIsChinaMainlandMachine —— 目标文件可能不是预期的 RegionUtils")

    result = source
    for signature, replacement in METHODS.items():
        start, end = find_method_span(result, re.compile(signature))
        result = result[:start] + replacement + result[end:]
    return result


def verify(source: str) -> None:
    """断言中和结果正确，且 machine 判定未被误伤。"""
    if not re.search(
        r"isIsChinaMainlandNetwork\s*\([^)]*\)\s*\{\s*// RDCL[^\n]*\n\s*return false;",
        source,
    ):
        raise NeutralizeError("isIsChinaMainlandNetwork 未处于中和状态")

    if not re.search(r"void\s+init\s*\(\s*\)\s*\{\s*// RDCL", source):
        raise NeutralizeError("init 未处于中和状态")

    machine = re.search(
        r"(public\s+static\s+boolean\s+isIsChinaMainlandMachine\s*\([^)]*\)\s*\{[^}]*\})",
        source,
    )
    if machine and "return false;" in machine.group(1):
        raise NeutralizeError("isIsChinaMainlandMachine 被误改为恒 false —— 必须保持 HMCL 行为")


def detect_line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="RDCL 地区限制中和")
    parser.add_argument("file", type=Path, help="RegionUtils.java 路径")
    parser.add_argument("--check", action="store_true", help="仅校验是否已中和")
    args = parser.parse_args()

    original = args.file.read_text(encoding="utf-8")
    newline = detect_line_ending(original)

    try:
        if args.check:
            verify(original)
            print(f"[neutralize] OK（已中和）: {args.file}")
            return 0

        updated = verify_and_build(original)
        if updated == original:
            print(f"[neutralize] 无需改动（已中和）: {args.file}")
            return 0

        args.file.write_text(updated.replace("\n", newline), encoding="utf-8", newline="")
        print(f"[neutralize] 已中和: {args.file}")
        print("  - init()                       -> 置空")
        print("  - isIsChinaMainlandNetwork()   -> return false")
        print("  - isIsChinaMainlandMachine()   -> 未改动（保持 HMCL 行为）")
        return 0
    except NeutralizeError as error:
        print(f"[neutralize] 失败: {error}", file=sys.stderr)
        print(
            "[neutralize] 提示：上游可能改动了 RegionUtils 的方法签名。"
            "请人工确认后更新 scripts/neutralize_region.py 的 METHODS。",
            file=sys.stderr,
        )
        return 1


def verify_and_build(source: str) -> str:
    updated = neutralize(source)
    verify(updated)
    return updated


if __name__ == "__main__":
    raise SystemExit(main())

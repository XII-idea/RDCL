#!/usr/bin/env python3
"""移除设置页的「更新」区块 —— 0003 的 UI 结构部分（规划 §4.3）。

为什么不用 patch：上游 `5789f8261` 调整了 `updateListener` 内部 if/else 的顺序，
导致该区块在 `v3.17.0.362` 与 `main` 上**内容不同** —— 基于任一版本生成的 patch
在另一个版本上必然冲突。本脚本按**语义标记 + 花括号配对**定位，与块内实现无关，
因此对两种形态（及未来演进）均成立。

删除三处（均已实测两版本通用）：
  1. `private final InvalidationListener updateListener;` 字段
     （final 字段，删块后不删会编译失败）
  2. 「更新」设置块：`ComponentList updatePaneList = ...` 起，到其所在 `{` 的配对 `}`
  3. `private void onUpdate() { ... }` 方法

保留：`Restarter` 依赖的 `UpdateHandler.startJava`、升级迁移逻辑（见 patch 0003）。

用法：
    python scripts/strip_update_ui.py <SettingsPage.java> [--check]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FIELD_MARKER = "private final InvalidationListener updateListener;"
BLOCK_MARKER = "ComponentList updatePaneList = new ComponentList();"
METHOD_MARKER = "private void onUpdate() {"
REPLACEMENT_NOTE = "        // RDCL: 已移除「更新」设置区块 —— 检查更新已按 0003 整体屏蔽。"


class StripError(RuntimeError):
    """定位失败 —— 直接终止构建，绝不静默跳过。"""


def find_matching_brace(lines: list[str], open_index: int) -> int:
    """从 lines[open_index] 的 `{` 出发做深度配对，返回配对 `}` 的行号（0-indexed）。"""
    depth = 0
    for index in range(open_index, len(lines)):
        depth += lines[index].count("{") - lines[index].count("}")
        if depth == 0 and index > open_index:
            return index
    raise StripError(f"第 {open_index + 1} 行的 {{ 未找到配对的 }}")


def find_line(lines: list[str], marker: str) -> int | None:
    """返回包含 marker 的行号（0-indexed）；找不到返回 None。"""
    for index, line in enumerate(lines):
        if marker in line:
            return index
    return None


def strip(lines: list[str]) -> list[str]:
    """删除三处；块内实现（if/else 顺序等）不影响定位。"""
    # 3. onUpdate 方法（从后往前删，先处理行号较大的）
    method_index = find_line(lines, METHOD_MARKER)
    if method_index is not None:
        if lines[method_index].strip().count("{") != 1:
            raise StripError(f"第 {method_index + 1} 行不是单行方法签名")
        method_end = find_matching_brace(lines, method_index)
        del lines[method_index : method_end + 1]
        # 连同其前的空行一并清理，避免留下连续空行
        while method_index < len(lines) and lines[method_index - 1].strip() == "":
            del lines[method_index - 1]

    # 2. 「更新」设置块
    #    注意：必须向下找块自身的 `{` —— `ComponentList updatePaneList = ...;`
    #    的**下一行**才是该块的开始；若向上找会命中「外层设置容器块」的 `{`，
    #    从而把语言、日志等后续设置块全部误删（已发生过线上事故）。
    block_marker = find_line(lines, BLOCK_MARKER)
    if block_marker is not None:
        open_index = block_marker + 1
        while open_index < len(lines) and lines[open_index].strip() == "":
            open_index += 1
        if open_index >= len(lines) or lines[open_index].strip() != "{":
            raise StripError(f"第 {block_marker + 1} 行之后未找到「更新」块的起始 {{")
        block_end = find_matching_brace(lines, open_index)
        # 从**声明行**删到块结束 —— 漏掉声明行会留下未使用的变量并被 verify 判为残留
        del lines[block_marker : block_end + 1]
        lines.insert(block_marker, REPLACEMENT_NOTE)

    # 1. final 字段（必须删，否则编译失败）
    field_index = find_line(lines, FIELD_MARKER)
    if field_index is not None:
        del lines[field_index]

    return lines


def verify(source: str) -> None:
    """断言删除彻底、结构完好，且**没有删掉后续设置块**。"""
    if source.count("{") != source.count("}"):
        raise StripError("花括号不配对 —— 删除范围有误")
    for leaked in ("updateListener", "updatePaneList", "UpdateChecker", "UpdateChannel", "onUpdate"):
        if leaked in source:
            raise StripError(f"残留 {leaked} —— 删除不彻底")
    # 防回归：若块定位错误（向上命中了外层容器块的 {），语言等后续设置块会被一并删除。
    # 括号仍然配对，仅靠结构检查发现不了，必须检查内容标记。
    for required in ("ComponentList languagePaneList", "settings.launcher.language"):
        if required not in source:
            raise StripError(
                f"疑似删除范围过大：未找到 `{required}` —— "
                "「更新」块定位可能命中了外层容器块，请人工检查上游是否重构了设置页结构"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="RDCL：移除设置页更新区块")
    parser.add_argument("file", type=Path, help="SettingsPage.java 路径")
    parser.add_argument("--check", action="store_true", help="仅校验是否已移除")
    args = parser.parse_args()

    original = args.file.read_text(encoding="utf-8-sig")
    newline = "\r\n" if "\r\n" in original else "\n"

    try:
        if args.check:
            verify(original)
            print(f"[strip-update-ui] OK（已移除）: {args.file}")
            return 0

        if find_line(original.splitlines(), BLOCK_MARKER) is None \
                and find_line(original.splitlines(), FIELD_MARKER) is None:
            verify(original)
            print(f"[strip-update-ui] 无需改动（已移除）: {args.file}")
            return 0

        updated = "\n".join(strip(original.splitlines()))
        verify(updated)
        args.file.write_text(updated.replace("\n", newline), encoding="utf-8", newline="")
    except StripError as error:
        print(f"[strip-update-ui] 失败: {error}", file=sys.stderr)
        print(
            "[strip-update-ui] 提示：上游可能重构了设置页结构，请人工确认后更新本脚本的标记。",
            file=sys.stderr,
        )
        return 1

    print(f"[strip-update-ui] 已移除更新区块: {args.file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

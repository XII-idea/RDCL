#!/usr/bin/env python3
"""RDCL 品牌替换 —— 规划 §5（R1 品牌 / R2 独立词 / R3 URL 重映射）。

设计要点：
  * R1 无条件全量替换（该串只出现在注释、i18n 值、HTML title、README、
    Metadata.FULL_NAME，不是标识符或路径）；
  * R2 只作用于「展示文本白名单文件」，用独立词正则，天然避开包名
    org.jackhuang.hmcl、属性 hmcl.*、类名 HMCL* 与主题 id hmcl.default；
  * assets/themes/*/manifest.json **结构感知**处理 —— 只改 description，
    绝不触碰 authors（署名不可篡改）与 themes[].name（HMCL 3.3 等历史年代命名）；
  * R3 按「文件 + 原文」精确成对替换，**命中数为 0 即失败**（防上游改地址
    后静默失配）；EULA_URL 与上游 issue 深链、AboutPage 开源声明显式排除。

用法：
    python scripts/rebrand.py <上游源码根目录> [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

RDCL_ISSUES = "https://github.com/XII-idea/RDCL/issues"
RDCL_RELEASES = "https://github.com/XII-idea/RDCL/releases"
RDCL_REPO = "https://github.com/XII-idea/RDCL"

R1_SOURCE = "Hello Minecraft! Launcher"
R1_TARGET = "Ray Dust Craft Launcher"

# 产品全称在非英语 i18n 中形式多样（`Hello Minecraft!` 无 Launcher、
# `Hello Minecraft！` 全角感叹号、`Hello Minecraftを…` 日文等），统一收敛：
#   Hello Minecraft! Launcher / Hello Minecraft! / Hello Minecraft！ / Hello Minecraft
#     -> Ray Dust Craft Launcher
R1_PATTERN = re.compile(r"Hello\s*Minecraft\s*[!！]?\s*(?:Launcher)?")

R2_SOURCE = "HMCL"
R2_TARGET = "RDCL"
# 前后不接 [word 或 - —— 避开 HMCL-dev、x-HMCL 这类复合词
R2_WORD = re.compile(r"(?<![\w.-])HMCL(?![\w-])")

# R2 必须原样保留的串（§4.4：上游仓库链接 / issue 深链指向原项目，GPLv3 §7(b)）
R2_PROTECTED = (
    "github.com/HMCL-dev/HMCL",   # AboutPage 开源声明、i18n 中的上游深链前缀
    "HMCL-dev",                   # 仓库组织名（issue 模板、workflow 等）
    "HMCL 3.3",                   # 历史年代命名（RDCL 从未有过 3.3，§6.3）
    "HMCL 2.3",                   # 同上
)
_PLACEHOLDER = "\x00RDCL_PROTECT_{i}\x00"

# R2 白名单：只在这些路径替换独立词（相对上游源码根）
R2_PATTERNS = (
    "HMCL/src/main/resources/assets/lang/*.properties",
    "HMCLBoot/src/main/resources/assets/lang/*.properties",
    "HMCL/src/main/resources/assets/microsoft_auth.html",
    "HMCL/src/main/resources/assets/about/*.json",
    "docs/README*.md",
)

# Metadata.java 的 R2 精确替换（只动展示常量，其余常量由 R3 处理）
METADATA_NAME_FIX = [
    ('public static final String NAME = "HMCL";', 'public static final String NAME = "RDCL";'),
    ('public static final String FULL_NAME = "Hello Minecraft! Launcher";',
     'public static final String FULL_NAME = "Ray Dust Craft Launcher";'),
]

# R3：精确映射。expected=0 表示允许不存在（如上游已改），None 表示必须至少命中一次
@dataclass(frozen=True)
class Replacement:
    files: tuple[str, ...]   # glob 列表
    old: str
    new: str
    required: bool = True    # False = 上游可能已移除，允许 0 命中（仅告警）

R3_REPLACEMENTS = (
    Replacement(("HMCL/src/main/java/org/jackhuang/hmcl/Metadata.java",),
                'public static final String PUBLISH_URL = "https://hmcl.huangyuhui.net";',
                f'public static final String PUBLISH_URL = "{RDCL_REPO}";'),
    Replacement(("HMCL/src/main/java/org/jackhuang/hmcl/Metadata.java",),
                'public static final String MANUAL_UPDATE_URL = "https://github.com/HMCL-dev/HMCL/releases";',
                f'public static final String MANUAL_UPDATE_URL = "{RDCL_RELEASES}";'),
    # CONTACT_URL 原为 DOCS_URL + "/help.html"（HMCL 文档站）—— 改为直接指向 RDCL issues
    Replacement(("HMCL/src/main/java/org/jackhuang/hmcl/Metadata.java",),
                'public static final String CONTACT_URL = DOCS_URL + "/help.html";',
                f'public static final String CONTACT_URL = "{RDCL_ISSUES}";'),
    Replacement(("HMCL/src/main/java/org/jackhuang/hmcl/Metadata.java",),
                'public static final String HMCL_UPDATE_URL = System.getProperty("hmcl.update_source.override", PUBLISH_URL + "/api/update_link");',
                f'public static final String HMCL_UPDATE_URL = System.getProperty("hmcl.update_source.override", "{RDCL_RELEASES}"); // RDCL: 已由 0003 屏蔽更新检查，此处仅防止误连上游',
                required=False),
    Replacement(("HMCL/src/main/java/org/jackhuang/hmcl/ui/main/FeedbackPage.java",),
                "https://github.com/HMCL-dev/HMCL/issues/new/choose",
                "https://github.com/XII-idea/RDCL/issues/new/choose"),
    # deb 包 Homepage 字段（⚠️ AboutPage 的同名 URL 是开源声明，**保留**，不在此列）
    Replacement(("buildSrc/src/main/java/org/jackhuang/hmcl/gradle/pack/CreateDeb.java",),
                "Homepage: https://github.com/HMCL-dev/HMCL",
                f"Homepage: {RDCL_REPO}"),
    # i18n 中「请下载官方构建」的链接（无 client_id 时展示）
    Replacement(("HMCL/src/main/resources/assets/lang/*.properties",),
                "https://hmcl.huangyuhui.net/download",
                RDCL_RELEASES,
                required=False),
)

# 结构感知：主题包 manifest —— 只改 description 的值
THEME_MANIFEST_GLOB = "HMCL/src/main/resources/assets/themes/*/manifest.json"


class RebrandError(RuntimeError):
    """替换失败 —— 直接终止构建。"""


def iter_files(root: Path, pattern: str) -> list[Path]:
    return [p for p in root.glob(pattern) if p.is_file()]


def apply_r1(root: Path, dry_run: bool) -> int:
    """全量替换产品全称（含非英语 i18n 的各种变体）；返回替换文件数。"""
    count = 0
    text_suffixes = {".java", ".properties", ".html", ".md", ".kts", ".kt",
                     ".json", ".xml", ".yml", ".yaml", ".svg", ".txt", ".bat", ".sh"}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in text_suffixes:
            continue
        if any(part in {".git", "build", ".gradle", ".idea"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8-sig", errors="ignore")
        if not R1_PATTERN.search(text):
            continue
        count += 1
        if not dry_run:
            path.write_text(R1_PATTERN.sub(R1_TARGET, text), encoding="utf-8")
    print(f"[rebrand] R1 产品全称 -> 「{R1_TARGET}」：{count} 个文件")
    return count


def protect(text: str) -> str:
    """把必须保留的串移入占位符，避免被 R2 误伤。"""
    result = text
    for index, pattern in enumerate(R2_PROTECTED):
        if pattern in result:
            result = result.replace(pattern, _PLACEHOLDER.format(i=index))
    return result


def unprotect(text: str) -> str:
    result = text
    for index, pattern in enumerate(R2_PROTECTED):
        result = result.replace(_PLACEHOLDER.format(i=index), pattern)
    return result


def apply_r2(root: Path, dry_run: bool) -> int:
    """白名单内替换独立词 HMCL（上游仓库链接/深链通过占位符保护）。"""
    count = 0
    for pattern in R2_PATTERNS:
        for path in iter_files(root, pattern):
            text = path.read_text(encoding="utf-8-sig", errors="ignore")
            if not R2_WORD.search(text):
                continue
            count += 1
            if not dry_run:
                path.write_text(unprotect(R2_WORD.sub(R2_TARGET, protect(text))), encoding="utf-8")

    # Metadata.java：只精确改 NAME / FULL_NAME 两个展示常量
    for path in root.glob("HMCL/src/main/java/**/Metadata.java"):
        text = path.read_text(encoding="utf-8-sig", errors="ignore")
        updated = text
        for old, new in METADATA_NAME_FIX:
            updated = updated.replace(old, new)
        if updated != text:
            count += 1
            if not dry_run:
                path.write_text(updated, encoding="utf-8")

    print(f"[rebrand] R2 独立词 {R2_SOURCE} -> {R2_TARGET}：{count} 个文件（白名单）")
    return count


def apply_theme_manifests(root: Path, dry_run: bool) -> int:
    """主题包 manifest 结构感知替换：只改 description，保护 authors 与 themes[].name。"""
    count = 0
    for path in iter_files(root, THEME_MANIFEST_GLOB):
        raw = path.read_text(encoding="utf-8-sig", errors="ignore")
        if not R2_WORD.search(raw):
            continue

        def walk(node: object, in_description: bool = False) -> object:
            """description 是多语言 map（{default: "...", zh-Hans: "..."}），
            因此要递归进入其**所有子串**；非 description 分支一律不下潜。"""
            if isinstance(node, dict):
                return {
                    key: walk(value, in_description or key == "description")
                    for key, value in node.items()
                }
            if isinstance(node, list):
                return [walk(item, in_description) for item in node]
            if in_description and isinstance(node, str):
                # 与 R2 同样受保护串约束（HMCL-dev / HMCL 3.3 等不得改）
                return unprotect(R2_WORD.sub(R2_TARGET, protect(node)))
            return node

        updated = json.dumps(walk(json.loads(raw)), ensure_ascii=False, indent=2) + "\n"
        count += 1
        if not dry_run:
            path.write_text(updated, encoding="utf-8")
    print(f"[rebrand] 主题 manifest（仅 description）：{count} 个文件")
    return count


def apply_r3(root: Path, dry_run: bool) -> None:
    """精确 URL 重映射；required=True 而命中 0 即失败。"""
    for item in R3_REPLACEMENTS:
        total = 0
        for pattern in item.files:
            for path in iter_files(root, pattern):
                text = path.read_text(encoding="utf-8-sig", errors="ignore")
                hits = text.count(item.old)
                if not hits:
                    continue
                total += hits
                if not dry_run:
                    path.write_text(text.replace(item.old, item.new), encoding="utf-8")
        if total == 0:
            if item.required:
                raise RebrandError(
                    f"R3 映射 0 命中（上游可能已改动该地址）：\n    {item.old}\n  涉及：{item.files}"
                )
            print(f"[rebrand] R3 可选映射 0 命中（跳过）：{item.old[:70]}")
        else:
            print(f"[rebrand] R3 {total:>2} 处：{item.old[:60]} -> {item.new[:60]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="RDCL 品牌替换")
    parser.add_argument("root", type=Path, help="上游源码根目录（含 HMCL/ HMCLCore/ 等）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    args = parser.parse_args()

    if not (args.root / "HMCL").is_dir():
        print(f"[rebrand] 失败：{args.root} 不是 HMCL 源码根目录（缺少 HMCL/）", file=sys.stderr)
        return 1

    try:
        apply_r1(args.root, args.dry_run)
        apply_r2(args.root, args.dry_run)
        apply_theme_manifests(args.root, args.dry_run)
        apply_r3(args.root, args.dry_run)
    except RebrandError as error:
        print(f"[rebrand] 失败: {error}", file=sys.stderr)
        return 1

    print(f"[rebrand] 完成{'（dry-run，未写入）' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

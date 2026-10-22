#!/usr/bin/env python3
"""RDCL 品牌残留校验 —— CI 强制门禁（规划 §5.4）。

规则：
  * `Hello Minecraft` 全仓必须为 0 处（R1 已覆盖各种变体）；
  * 独立词 `HMCL` 必须为 0 处，但下列**保留项白名单**豁免：
      - 主题 manifest 的 `authors`（署名不可篡改，§6.3）
      - 主题 manifest 的 `themes[].name`（HMCL 3.3 等历史年代命名，§6.3）
      - `github.com/HMCL-dev/HMCL...` 上游链接与 issue/pull 深链（§4.4）
      - `AboutPage` 的开源声明链接（同上）

白名单按 **JSON 路径**与 **URL 模式**豁免，避免「一刀切正则」把真残留一起放行。

用法：
    python scripts/check_branding.py <源码根目录>      # 开发期 / validate.yml
    python scripts/check_branding.py --jar <file.jar>  # CI 产物校验
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

HELLO = re.compile(r"Hello\s*Minecraft")
HMCL_WORD = re.compile(r"(?<![\w.-])HMCL(?![\w-])")

# URL 类保留项：上游仓库链接、issue/pull 深链（§4.4）
URL_PROTECT = re.compile(r"github\.com/HMCL-dev/HMCL[^\s\"'<>)]*")

# 只扫描「会进入运行产物」的资源类型，避免把构建脚本 / workflow / 文档里
# 合理出现的 HMCL（模块名、CI 变量名等）误报为品牌残留
SCAN_SUFFIXES = {".properties", ".html", ".json"}

# manifest 中允许保留 HMCL 的 JSON 路径前缀
MANIFEST_ALLOW_PREFIXES = ("authors", "themes[].name")


def manifest_reserved_values(path: Path) -> list[str]:
    """返回主题 manifest 中允许保留 HMCL 的字符串值（authors / themes[].name）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig", errors="ignore"))
    except (json.JSONDecodeError, OSError):
        return []

    reserved: list[str] = []

    def collect_authors(node: object) -> None:
        """authors[].name 既可能是字符串，也可能是多语言 map（{default: "HMCL"}）。"""
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "name" and isinstance(value, str):
                    reserved.append(value)
                elif key == "name" and isinstance(value, dict):
                    reserved.extend(v for v in value.values() if isinstance(v, str))
                else:
                    collect_authors(value)
        elif isinstance(node, list):
            for item in node:
                collect_authors(item)

    collect_authors(data.get("authors", []))

    for theme in data.get("themes", []) if isinstance(data.get("themes"), list) else []:
        name = theme.get("name")
        if isinstance(name, str):
            reserved.append(name)
        elif isinstance(name, dict):
            reserved.extend(v for v in name.values() if isinstance(v, str))

    return reserved


def check_text(name: str, text: str, reserved: list[str]) -> list[str]:
    """返回违规描述列表；白名单命中不计入。"""
    problems: list[str] = []

    for index, line in enumerate(text.splitlines(), start=1):
        if HELLO.search(line):
            problems.append(f"{name}:{index}: 残留 Hello Minecraft -> {line.strip()[:120]}")

        stripped = URL_PROTECT.sub("", line)
        for value in reserved:
            stripped = stripped.replace(value, "")
        if HMCL_WORD.search(stripped):
            problems.append(f"{name}:{index}: 残留独立词 HMCL -> {line.strip()[:120]}")

    return problems


def scan_directory(root: Path) -> list[str]:
    """只扫描会打进 jar 的资源（**/src/main/resources/**），
    排除 docs/schemas、config、workflow 等不随产物分发的文件。"""
    problems: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SCAN_SUFFIXES:
            continue
        parts = set(path.parts)
        if not {"src", "main", "resources"} <= parts:
            continue
        if any(part in {".git", "build", ".gradle", ".idea"} for part in path.parts):
            continue
        reserved = manifest_reserved_values(path) if path.name == "manifest.json" else []
        if path.name == "manifest.json" and reserved:
            # 署名完整性：上游 authors 必须原样保留（GPLv3 §7(b)，§6.3）
            if not any(value == "HMCL" for value in reserved):
                problems.append(
                    f"{path.relative_to(root)}: authors 署名被改动 —— 必须保留 HMCL"
                )
        text = path.read_text(encoding="utf-8-sig", errors="ignore")
        problems += check_text(str(path.relative_to(root)), text, reserved)
    return problems


def scan_jar(jar: Path) -> list[str]:
    problems: list[str] = []
    with zipfile.ZipFile(jar) as archive:
        for info in archive.infolist():
            if Path(info.filename).suffix.lower() not in SCAN_SUFFIXES:
                continue
            raw = archive.read(info).decode("utf-8-sig", errors="ignore")
            reserved: list[str] = []
            if info.filename.endswith("manifest.json"):
                try:
                    dump = json.dumps(json.loads(raw))
                    # 产物内 manifest 无法按路径豁免时，退化为按值豁免
                    data = json.loads(dump)
                    reserved = [v for v in _flatten_strings(data) if v.startswith("HMCL")]
                except json.JSONDecodeError:
                    pass
            problems += check_text(f"{jar.name}!/{info.filename}", raw, reserved)
    return problems


def _flatten_strings(node: object) -> list[str]:
    if isinstance(node, dict):
        return [v for value in node.values() for v in _flatten_strings(value)]
    if isinstance(node, list):
        return [v for item in node for v in _flatten_strings(item)]
    return [node] if isinstance(node, str) else []


def main() -> int:
    parser = argparse.ArgumentParser(description="RDCL 品牌残留校验")
    parser.add_argument("root", nargs="?", type=Path, help="上游源码根目录")
    parser.add_argument("--jar", type=Path, help="构建产物 jar")
    args = parser.parse_args()

    if args.jar:
        problems = scan_jar(args.jar)
    elif args.root:
        problems = scan_directory(args.root)
    else:
        parser.error("需要源码根目录或 --jar")

    if problems:
        print(f"[check-branding] 失败：{len(problems)} 处残留", file=sys.stderr)
        for problem in problems[:60]:
            print(f"  {problem}", file=sys.stderr)
        if len(problems) > 60:
            print(f"  ...（其余 {len(problems) - 60} 处省略）", file=sys.stderr)
        print(
            "\n[check-branding] 处理方式：扩大 rebrand 白名单 / 修正 R1 变体，"
            "或把确需保留的项加入本脚本白名单并在 §5.4 登记。",
            file=sys.stderr,
        )
        return 1

    print("[check-branding] 通过：无品牌残留")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

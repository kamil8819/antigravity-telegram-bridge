#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Auto-Reflect Tool for Antigravity
Automatically captures feedback, lessons, and persistent rules into GEMINI.md.
Inspired by claude-reflect.
"""

import os
import sys
import argparse
from datetime import datetime
from pathlib import Path

# Ensure UTF-8 output
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent
GEMINI_MD_PATH = WORKSPACE_ROOT / "GEMINI.md"


def add_rule(category: str, rule: str) -> bool:
    if not GEMINI_MD_PATH.exists():
        print(f"[!] GEMINI.md not found at {GEMINI_MD_PATH}", file=sys.stderr)
        return False
        
    with open(GEMINI_MD_PATH, "r", encoding="utf-8") as f:
        content = f.read()
        
    rule_entry = f"- **{category}**: {rule.strip()} *(добавлено {datetime.now().strftime('%d.%m.%Y')})*"
    
    if rule.strip() in content:
        print("[i] Правило уже присутствует в GEMINI.md.")
        return True
        
    section_marker = "## Правила самообучения и рефлексии (Auto-Reflect Rules)"
    if section_marker in content:
        content = content.replace(section_marker, f"{section_marker}\n{rule_entry}")
    else:
        content += f"\n\n{section_marker}\n{rule_entry}\n"
        
    with open(GEMINI_MD_PATH, "w", encoding="utf-8") as f:
        f.write(content)
        
    print(f"✅ Правило успешно сохранено в GEMINI.md:\n   {rule_entry}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Antigravity Auto-Reflect Rule Saver")
    parser.add_argument("category", help="Категория или область (например, 'Blender', 'Код', 'Telegram')")
    parser.add_argument("rule", help="Текст постоянного правила")
    
    args = parser.parse_args()
    success = add_rule(args.category, args.rule)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

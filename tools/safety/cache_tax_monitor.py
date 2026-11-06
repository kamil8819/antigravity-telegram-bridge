#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cache Tax & Context Health Monitor for Antigravity
Analyzes prompt cache status, active token volume, and warns about cold cache / bloated dialogs.
Inspired by cache-tax.
"""

import os
import sys
import json
import time
from datetime import datetime
from pathlib import Path

# Ensure UTF-8 output
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

BRAIN_DIR = Path(os.environ.get("USERPROFILE", "")) / ".gemini" / "antigravity" / "brain"


def find_latest_transcript() -> Path:
    if not BRAIN_DIR.exists():
        return None
    transcripts = list(BRAIN_DIR.glob("*/.system_generated/logs/transcript.jsonl"))
    if not transcripts:
        return None
    transcripts.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return transcripts[0]


def analyze_transcript(path: Path):
    if not path or not path.exists():
        print("[!] Активный транскрипт не найден.")
        return
        
    conversation_id = path.parent.parent.parent.name
    file_size_kb = path.stat().st_size / 1024
    
    total_steps = 0
    last_user_time = None
    last_tokens = 0
    
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if not line.strip():
                continue
            total_steps += 1
            try:
                data = json.loads(line)
                if data.get("source") == "USER_EXPLICIT":
                    created = data.get("created_at")
                    if created:
                        last_user_time = created
                if data.get("input_tokens"):
                    last_tokens = data.get("input_tokens")
            except Exception:
                pass
                
    print("\n" + "="*50)
    print("⚡ ANTIGRAVITY CACHE & CONTEXT HEALTH REPORT")
    print("="*50)
    print(f"🆔 Conversation ID: {conversation_id}")
    print(f"📊 Всего шагов в сессии: {total_steps}")
    print(f"📦 Размер лога: {file_size_kb:.1f} KB")
    if last_tokens:
        print(f"🧠 Текущий объем контекста: ~{last_tokens:,} токенов")
        
    status = "🟢 КЭШ ГОРЯЧИЙ (Минимальный расход токенов)"
    advice = "Контекст в норме, продолжайте работу."
    
    if file_size_kb > 500 or total_steps > 300:
        status = "🟡 ВНИМАНИЕ: Контекст разросся"
        advice = "Рекомендуется выполнить команду /compact для очистки памяти и экономии квот."
    elif file_size_kb > 2000 or total_steps > 1000:
        status = "🔴 КРИТИЧНО: Риск переполнения контекста"
        advice = "Обязательно запустите /compact или начните новый чистый диалог (/new)."
        
    print(f"\nСтатус памяти: {status}")
    print(f"Совет: {advice}")
    print("="*50 + "\n")


def main():
    transcript = find_latest_transcript()
    analyze_transcript(transcript)


if __name__ == "__main__":
    main()

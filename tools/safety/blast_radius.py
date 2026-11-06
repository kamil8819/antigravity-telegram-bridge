#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Blast Radius Safety Guard for Antigravity
Calculates blast radius before destructive file modifications and automatically
creates a rollback snapshot in backups/blast_radius/.
"""

import os
import sys
import argparse
import zipfile
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
BACKUP_DIR = WORKSPACE_ROOT / "backups" / "blast_radius"


def get_affected_files(targets: list[str]) -> list[Path]:
    affected = []
    for t in targets:
        p = Path(t)
        if not p.is_absolute():
            p = WORKSPACE_ROOT / p
        if p.is_file():
            affected.append(p)
        elif p.is_dir():
            for root, _, files in os.walk(p):
                # Skip heavy caches
                if any(x in root for x in [".git", "__pycache__", ".venv", "node_modules"]):
                    continue
                for f in files:
                    affected.append(Path(root) / f)
    return affected


def create_snapshot(files: list[Path], label: str = "safety_snapshot") -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = BACKUP_DIR / f"{label}_{timestamp}.zip"
    
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for f in files:
            if f.exists():
                try:
                    rel_path = f.relative_to(WORKSPACE_ROOT)
                except ValueError:
                    rel_path = f.name
                zipf.write(f, arcname=str(rel_path))
                
    return zip_path


def main():
    parser = argparse.ArgumentParser(description="Antigravity Blast Radius Safety Guard")
    parser.add_argument("targets", nargs="+", help="Files or directories about to be modified/deleted")
    parser.add_argument("--label", default="snapshot", help="Label prefix for the backup zip")
    parser.add_argument("--dry-run", action="store_true", help="Only calculate blast radius without creating zip")
    
    args = parser.parse_args()
    files = get_affected_files(args.targets)
    
    total_bytes = sum(f.stat().st_size for f in files if f.exists())
    
    print("\n" + "="*50)
    print("🛡️  ANTIGRAVITY BLAST RADIUS REPORT")
    print("="*50)
    print(f"📁 Targets checked: {len(args.targets)}")
    print(f"📄 Files in blast radius: {len(files)}")
    print(f"💾 Total size: {total_bytes / 1024:.2f} KB ({total_bytes / (1024*1024):.2f} MB)")
    
    if files:
        print("\n📋 Sample of affected files:")
        for f in files[:10]:
            try:
                rel = f.relative_to(WORKSPACE_ROOT)
            except ValueError:
                rel = f
            print(f"  • {rel}")
        if len(files) > 10:
            print(f"  ... and {len(files) - 10} more files.")
            
    if not args.dry_run and files:
        zip_path = create_snapshot(files, args.label)
        print(f"\n✅ Automatic Rollback Backup created:")
        print(f"   📦 {zip_path}")
        print("="*50 + "\n")
    else:
        print("\nℹ️ Dry run completed. No backup created.\n" + "="*50 + "\n")


if __name__ == "__main__":
    main()

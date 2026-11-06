import os
import sys
import time
import subprocess

try:
    sys.stdout.fileno()
except Exception:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8')
try:
    sys.stderr.fileno()
except Exception:
    sys.stderr = open(os.devnull, 'w', encoding='utf-8')

WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON_EXE = sys.executable or "python"
BRIDGE_SCRIPT = os.path.join(WORKSPACE_DIR, "bridge.py")
LIVE_SCRIPT = os.path.join(WORKSPACE_DIR, "tools", "live_voice", "gemini_live_server.py")
LOG_FILE = os.path.join(WORKSPACE_DIR, "bridge.log")
LIVE_LOG_FILE = os.path.join(WORKSPACE_DIR, "tools", "live_voice", "live_server.log")

CREATE_NO_WINDOW = 0x08000000

def log_msg(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [supervisor] {msg}\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass

def start_service(name, script_path, log_path):
    if not os.path.exists(script_path):
        return None, None
    try:
        log_f = open(log_path, "a", encoding="utf-8", buffering=1)
        proc = subprocess.Popen(
            [PYTHON_EXE, "-u", script_path],
            cwd=WORKSPACE_DIR,
            creationflags=CREATE_NO_WINDOW,
            stdout=log_f,
            stderr=subprocess.STDOUT,
        )
        log_msg(f"{name} запущен (pid={proc.pid})")
        return proc, log_f
    except Exception as e:
        log_msg(f"Ошибка запуска {name}: {e}")
        return None, None

def main():
    log_msg("Dual Supervisor запущен (Telegram Bridge + Gemini Live Voice)")
    
    bridge_proc, bridge_log = start_service("Telegram Bridge", BRIDGE_SCRIPT, LOG_FILE)
    live_proc, live_log = start_service("Gemini Live Server", LIVE_SCRIPT, LIVE_LOG_FILE)

    while True:
        time.sleep(3)
        # Check Bridge
        if bridge_proc is None or bridge_proc.poll() is not None:
            code = bridge_proc.returncode if bridge_proc else "None"
            log_msg(f"Telegram Bridge завершился (code={code}). Перезапуск...")
            if bridge_log:
                try: bridge_log.close()
                except Exception: pass
            bridge_proc, bridge_log = start_service("Telegram Bridge", BRIDGE_SCRIPT, LOG_FILE)

        # Check Live Voice Server
        if live_proc is None or live_proc.poll() is not None:
            code = live_proc.returncode if live_proc else "None"
            log_msg(f"Gemini Live Server завершился (code={code}). Перезапуск...")
            if live_log:
                try: live_log.close()
                except Exception: pass
            live_proc, live_log = start_service("Gemini Live Server", LIVE_SCRIPT, LIVE_LOG_FILE)

if __name__ == "__main__":
    main()

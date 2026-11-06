try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import os
import sys

# pythonw.exe закрывает stdout/stderr — перенаправляем в devnull чтобы не падать
if sys.stdout is None or (hasattr(sys.stdout, 'fileno') and not hasattr(sys.stdout, 'write')):
    sys.stdout = open(os.devnull, 'w', encoding='utf-8')
    sys.stderr = open(os.devnull, 'w', encoding='utf-8')
try:
    sys.stdout.fileno()
except Exception:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8')
    sys.stderr = open(os.devnull, 'w', encoding='utf-8')
import time
import json
import re
import html
import subprocess
import asyncio
import urllib.request
import urllib.error
import urllib.parse
import aiohttp
import ctypes
import shutil
import struct
import base64
from PIL import ImageGrab
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import Message, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, CallbackQuery, BotCommand
from aiogram.exceptions import TelegramNetworkError

class RobustAiohttpSession(AiohttpSession):
    """
    Кастомная aiohttp-сессия для aiogram с надежным TCPConnector:
    - keepalive_timeout=30s предотвращает сбросы прокси и SSL DECRYPTION_FAILED_OR_BAD_RECORD_MAC
    - limit=50 соединений
    - Строгая проверка TLS сохранена
    """
    def __init__(self, proxy: str | None = None):
        super().__init__(proxy=proxy)
        self._connector_init["limit"] = 50
        self._connector_init["keepalive_timeout"] = 30

# === НАСТРОЙКИ МОСТА ===
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

def get_working_proxy():
    """Автоопределение рабочего прокси для Telegram API с быстрой проверкой сокета"""
    if "TELEGRAM_PROXY" in os.environ:
        return os.environ["TELEGRAM_PROXY"]
    test_url = "https://api.telegram.org"
    import socket

    # 1. Проверяем локальные туннели ТОЛЬКО если порт реально слушается
    for p, host, port in [("http://127.0.0.1:12334", "127.0.0.1", 12334), ("http://127.0.0.1:10809", "127.0.0.1", 10809)]:
        try:
            with socket.create_connection((host, port), timeout=0.8):
                pass
        except Exception:
            continue  # Порт закрыт — не тратим время и не вешаем бота
        try:
            handler = urllib.request.ProxyHandler({"http": p, "https": p})
            opener = urllib.request.build_opener(handler)
            req = urllib.request.Request(test_url)
            with opener.open(req, timeout=4) as resp:
                if resp.status in (200, 302, 404):
                    return p
        except Exception:
            pass

    # 2. Прямой выход (AdGuard VPN / прямой интернет)
    try:
        req = urllib.request.Request(test_url)
        with urllib.request.urlopen(req, timeout=4) as resp:
            if resp.status in (200, 302, 404):
                return None
    except Exception:
        pass

    # Безопасный дефолт: прямой выход вместо зависания на несуществующем порту
    return None

WORKSPACE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOADS_DIR = os.path.join(WORKSPACE_DIR, "telegram_uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)
CONFIG_FILE = os.path.join(WORKSPACE_DIR, "config.json")
LOG_FILE = os.path.join(WORKSPACE_DIR, "bridge.log")
PENDING_TASK_FILE = os.path.join(WORKSPACE_DIR, "pending_telegram_task.json")

def save_pending_task(chat_id: int, status_msg_id: int, prompt: str, initial_line_count: int):
    """Энергонезависимый Watchdog: сохраняет состояние задачи на диск перед отправкой в Antigravity"""
    try:
        data = {
            "chat_id": chat_id,
            "status_msg_id": status_msg_id,
            "prompt": prompt,
            "initial_line_count": initial_line_count,
            "timestamp": time.time(),
            "conversation_id": get_active_conversation_id()
        }
        with open(PENDING_TASK_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log(f"💾 [Watchdog] Задача сохранена на диск (chat={chat_id}, msg={status_msg_id})")
    except Exception as e:
        log(f"Ошибка сохранения pending_task: {e}")

def clear_pending_task():
    """Очищает задачу с диска после успешной отправки ответа пользователю"""
    try:
        if os.path.exists(PENDING_TASK_FILE):
            os.remove(PENDING_TASK_FILE)
            log("🧹 [Watchdog] Задача выполнена и удалена с диска")
    except Exception:
        pass

def build_live_status_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура с кнопкой мгновенного скриншота экрана для Telegram"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 Скриншот экрана ПК", callback_data="live_screenshot")]
    ])

def format_live_status(steps_history: list, active_action: str | None, active_thinking: str | None, elapsed: int) -> str:
    """Форматирует консоль прогресса Antigravity для Telegram в реальном времени"""
    res = [f"🤖 <b>Antigravity в работе</b> <i>({elapsed}с)</i>", "━━━━━━━━━━━━━━━━━━━━━"]
    
    # Последние завершенные шаги
    completed = []
    if steps_history:
        for s in steps_history:
            if s and s != active_action and s not in completed:
                completed.append(s)
    for c in completed[-3:]:
        res.append(f"✅ {html.escape(c[:48])}")
        
    # Текущий шаг
    if active_action:
        res.append(f"⚙️ <b>{html.escape(active_action[:55])}</b> ⏳")
    else:
        res.append("⏳ <i>Анализирую задачу и готовлю шаги...</i>")
        
    # Мысли модели
    if active_thinking:
        res.append("━━━━━━━━━━━━━━━━━━━━━")
        res.append(f"💭 <i>«{html.escape(active_thinking[:100])}»</i>")
        
    return "\n".join(res)

PINNED_CONVERSATION_ID = "7423064b-262a-4006-a8f0-0b77ce027e7b"

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_config(new_data: dict):
    try:
        current = load_config()
        current.update(new_data)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"Ошибка сохранения config: {e}")

# === СИСТЕМА ДНЕВНЫХ ГОЛОСОВЫХ ЗАМЕТОК И ДИКТОВКИ ===
from datetime import datetime

NOTES_FILE = os.path.join(WORKSPACE_DIR, "daily_notes.json")

def load_notes() -> list:
    if os.path.exists(NOTES_FILE):
        try:
            with open(NOTES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_notes(notes: list):
    try:
        with open(NOTES_FILE, "w", encoding="utf-8") as f:
            json.dump(notes, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"Ошибка сохранения заметок: {e}")

def add_daily_note(text: str) -> dict:
    notes = load_notes()
    now = datetime.now()
    note = {
        "id": len(notes) + 1,
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "text": text.strip()
    }
    notes.append(note)
    save_notes(notes)
    log(f"📝 Сохранена заметка [{note['time']}]: {note['text'][:60]}")
    return note

def get_today_notes() -> list:
    notes = load_notes()
    today_str = datetime.now().strftime("%Y-%m-%d")
    return [n for n in notes if n.get("date") == today_str]

def clear_today_notes():
    notes = load_notes()
    today_str = datetime.now().strftime("%Y-%m-%d")
    remaining = [n for n in notes if n.get("date") != today_str]
    save_notes(remaining)
    log(f"🗑 Заметки за {today_str} очищены")

def is_notes_review_query(text: str) -> bool:
    """Определяет запросы на показ и озвучку мыслей за день"""
    if not isinstance(text, str):
        return False
    clean = text.strip().lower().strip(".!?, ")
    patterns = [
        r"^/notes\b",
        r"^/заметки\b",
        r"^заметки\b",
        r"повтори.*мысли",
        r"мои\s+мысли",
        r"что\s+я.*наговорил",
        r"что\s+записано",
        r"продиктуй",
        r"итоги\s+дня",
        r"^сводка\b",
        r"список\s+мыслей"
    ]
    return any(re.search(p, clean) for p in patterns)

def generate_voice_digest(notes: list, voice_name: str = "Charon") -> str:
    """Генерирует голосовую озвучку заметок за день через Google Neural TTS (голос Харон) + FFmpeg"""
    if not notes:
        return ""
    pcm_path = os.path.join(WORKSPACE_DIR, "temp_notes.pcm")
    ogg_path = os.path.join(WORKSPACE_DIR, "temp_notes.ogg")
    
    today_formatted = datetime.now().strftime("%d.%m")
    spoken_text = f"Привет! Вот твои мысли за {today_formatted}. "
    for i, n in enumerate(notes, 1):
        clean_item = re.sub(r'[\r\n]+', ' ', n.get("text", "")).strip()
        spoken_text += f"Пункт {i}: {clean_item}. "
    spoken_text += "Все мысли на месте, ничего не упущено."

    # 1. Приоритет: Google Studio Neural TTS (голос Charon / Харон)
    gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if gemini_key:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-tts:generateContent?key={gemini_key}"
            payload = {
                "contents": [{"parts": [{"text": spoken_text}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {
                            "prebuiltVoiceConfig": {
                                "voiceName": voice_name
                            }
                        }
                    }
                }
            }
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                pcm_data = base64.b64decode(res["candidates"][0]["content"]["parts"][0]["inlineData"]["data"])
                with open(pcm_path, "wb") as f:
                    f.write(pcm_data)
                subprocess.run(
                    ["ffmpeg", "-y", "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", pcm_path, "-c:a", "libopus", "-b:a", "32k", ogg_path],
                    capture_output=True, timeout=10
                )
                if os.path.exists(pcm_path):
                    try:
                        os.remove(pcm_path)
                    except Exception:
                        pass
                if os.path.exists(ogg_path):
                    log(f"Сгенерирована озвучка мыслей (голос {voice_name}): {os.path.getsize(ogg_path)} байт")
                    return ogg_path
        except Exception as e:
            log(f"Ошибка Gemini TTS ({voice_name}): {e}, пробую резерв SAPI...")

    # 2. Резерв: Windows SAPI (если оффлайн)
    try:
        txt_path = os.path.join(WORKSPACE_DIR, "temp_notes_text.txt")
        wav_path = os.path.join(WORKSPACE_DIR, "temp_notes.wav")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(spoken_text)
        ps_cmd = f'''
        Add-Type -AssemblyName System.Speech
        $txt = [System.IO.File]::ReadAllText('{txt_path}', [System.Text.Encoding]::UTF8)
        $s = New-Object System.Speech.Synthesis.SpeechSynthesizer
        $s.SelectVoice('Microsoft Irina Desktop')
        $s.SetOutputToWaveFile('{wav_path}')
        $s.Speak($txt)
        $s.Dispose()
        '''
        subprocess.run(['powershell', '-NoProfile', '-Command', ps_cmd], capture_output=True, text=True, timeout=15)
        if os.path.exists(wav_path):
            subprocess.run(['ffmpeg', '-y', '-i', wav_path, '-c:a', 'libopus', '-b:a', '32k', ogg_path], capture_output=True, timeout=10)
            if os.path.exists(ogg_path):
                return ogg_path
    except Exception as e:
        log(f"Ошибка SAPI fallback: {e}")

    return ""



def clean_text_for_speech(text: str, max_chars: int = 700) -> str:
    """Очищает текст от кода, ссылок и тегов для естественного чтения голосом"""
    text = re.sub(r'```.*?```', ' блок кода опущен. ', text, flags=re.DOTALL)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'[#*_\-~>`=|]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > max_chars:
        cut = text[:max_chars]
        last_punct = max(cut.rfind('.'), cut.rfind('!'), cut.rfind('?'))
        if last_punct > max_chars // 2:
            text = cut[:last_punct+1]
        else:
            text = cut + "..."
    return text


def generate_speech_audio(text: str, voice_name: str = "Charon") -> str:
    """Генерирует голосовое сообщение через Google Studio Neural TTS (голос Charon / Харон)"""
    spoken_text = clean_text_for_speech(text)
    if not spoken_text:
        return ""
        
    ts = int(time.time() * 1000)
    pcm_path = os.path.join(WORKSPACE_DIR, f"temp_voice_{ts}.pcm")
    ogg_path = os.path.join(WORKSPACE_DIR, f"temp_voice_{ts}.ogg")

    gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if gemini_key:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-tts:generateContent?key={gemini_key}"
            payload = {
                "contents": [{"parts": [{"text": spoken_text}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {
                            "prebuiltVoiceConfig": {
                                "voiceName": voice_name
                            }
                        }
                    }
                }
            }
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                pcm_data = base64.b64decode(res["candidates"][0]["content"]["parts"][0]["inlineData"]["data"])
                with open(pcm_path, "wb") as f:
                    f.write(pcm_data)
                subprocess.run(
                    ["ffmpeg", "-y", "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", pcm_path, "-c:a", "libopus", "-b:a", "32k", ogg_path],
                    capture_output=True, timeout=10
                )
                if os.path.exists(pcm_path):
                    try:
                        os.remove(pcm_path)
                    except Exception:
                        pass
                if os.path.exists(ogg_path):
                    log(f"Сгенерирован голосовой ответ: {os.path.getsize(ogg_path)} байт")
                    return ogg_path
        except Exception as e:
            log(f"Ошибка Gemini TTS в голосовом режиме: {e}")

    try:
        txt_path = os.path.join(WORKSPACE_DIR, f"temp_voice_{ts}.txt")
        wav_path = os.path.join(WORKSPACE_DIR, f"temp_voice_{ts}.wav")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(spoken_text)
        ps_cmd = f"""
        Add-Type -AssemblyName System.Speech
        $txt = [System.IO.File]::ReadAllText('{txt_path}', [System.Text.Encoding]::UTF8)
        $s = New-Object System.Speech.Synthesis.SpeechSynthesizer
        $s.SelectVoice('Microsoft Irina Desktop')
        $s.SetOutputToWaveFile('{wav_path}')
        $s.Speak($txt)
        $s.Dispose()
        """
        subprocess.run(['powershell', '-NoProfile', '-Command', ps_cmd], capture_output=True, text=True, timeout=15)
        if os.path.exists(txt_path):
            try:
                os.remove(txt_path)
            except Exception:
                pass
        if os.path.exists(wav_path):
            subprocess.run(['ffmpeg', '-y', '-i', wav_path, '-c:a', 'libopus', '-b:a', '32k', ogg_path], capture_output=True, timeout=10)
            if os.path.exists(wav_path):
                try:
                    os.remove(wav_path)
                except Exception:
                    pass
            if os.path.exists(ogg_path):
                return ogg_path
    except Exception as e:
        log(f"SAPI voice fallback failed: {e}")

    return ""


def get_active_conversation_id() -> str:
    """Возвращает закрепленный ID диалога («мост телеграмм») или свежий активный"""
    base_brain = os.path.join(os.environ.get("USERPROFILE", ""), r".gemini\antigravity\brain")
    
    # 1. Проверяем сохраненный ID в bridge_config.json
    cfg = load_config()
    pinned = cfg.get("pinned_conversation_id")
    if pinned:
        p_dir = os.path.join(base_brain, pinned)
        if os.path.exists(p_dir):
            return pinned

    # 2. Поиск самого свежего диалога при отсутствии закрепления
    if os.path.exists(base_brain):
        try:
            ignored = {"340323f4-eb49-4555-a63c-fd5e22fdc652", "7cceaa63-caf7-4375-b625-170aed95c2d1"}
            # ignored sessions
            candidates = []
            for item in os.listdir(base_brain):
                if item == "tempmediaStorage" or item.startswith(".") or item in ignored:
                    continue
                p = os.path.join(base_brain, item)
                if os.path.isdir(p):
                    t_path = os.path.join(p, ".system_generated", "logs", "transcript.jsonl")
                    if os.path.exists(t_path):
                        mtime = os.path.getmtime(t_path)
                        candidates.append((mtime, item))
            if candidates:
                candidates.sort(reverse=True)
                return candidates[0][1]
        except Exception:
            pass
    return PINNED_CONVERSATION_ID

def get_brain_paths():
    cid = get_active_conversation_id()
    b_dir = os.path.join(os.environ.get("USERPROFILE", ""), r".gemini\antigravity\brain", cid)
    u_dir = os.path.join(b_dir, ".user_uploaded")
    os.makedirs(u_dir, exist_ok=True)
    t_path = os.path.join(b_dir, r".system_generated\logs\transcript.jsonl")
    tf_path = os.path.join(b_dir, r".system_generated\logs\transcript_full.jsonl")
    return cid, b_dir, u_dir, t_path, tf_path

def log(msg: str):
    timestamp = time.strftime("[%Y-%m-%d %H:%M:%S]")
    line = f"{timestamp} {msg}\n"
    try:
        print(line, end="", flush=True)
    except Exception:
        pass  # pythonw: stdout закрыт
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass

allowed_chat_ids = set()
cfg = load_config()
if cfg.get("allowed_chat_ids"):
    allowed_chat_ids = set(cfg["allowed_chat_ids"])

env_chat_id = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("ALLOWED_CHAT_ID")
if env_chat_id:
    try:
        allowed_chat_ids.add(int(env_chat_id))
    except ValueError:
        pass

def save_allowed_chat_ids():
    save_config({"allowed_chat_ids": list(allowed_chat_ids)})

def verify_ls_port(port: int, token: str) -> bool:
    """Проверяет, что порт принимает вызовы LanguageServerService и возвращает 200 OK"""
    url = f"http://localhost:{port}/exa.language_server_pb.LanguageServerService/GetCascadeModelConfigs"
    headers = {
        "Content-Type": "application/json",
        "x-codeium-csrf-token": token,
        "Connect-Protocol-Version": "1"
    }
    req = urllib.request.Request(url, data=b"{}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            return resp.status == 200
    except Exception:
        return False

_cached_ls_addr = None
_cached_ls_token = None

def get_ls_credentials():
    """Динамически находит рабочий адрес и CSRF токен активного language_server.exe"""
    global _cached_ls_addr, _cached_ls_token

    # 1. Сначала проверяем кэшированный адрес (мгновенно 0.001 сек)
    if _cached_ls_addr and _cached_ls_token:
        try:
            port = int(_cached_ls_addr.split(":")[-1])
            if verify_ls_port(port, _cached_ls_token):
                return _cached_ls_addr, _cached_ls_token
        except Exception:
            pass

    # 2. Мгновенный поиск через psutil (без тяжелого вызова PowerShell)
    try:
        import psutil
        target_pid = None
        token = ""
        for p in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                name = p.info.get('name') or ''
                if 'language_server' in name.lower():
                    cmd = ' '.join(p.info.get('cmdline') or [])
                    if '--csrf_token' in cmd:
                        t_match = re.search(r'--csrf_token\s+([a-f0-9\-]+)', cmd)
                        cur_token = t_match.group(1) if t_match else ""
                        if '--subclient_type' in cmd:
                            target_pid = p.info['pid']
                            token = cur_token
                            break
                        elif not target_pid:
                            target_pid = p.info['pid']
                            token = cur_token
            except Exception:
                continue

        if target_pid:
            proc = psutil.Process(target_pid)
            ports = []
            try:
                for conn in proc.net_connections(kind='inet'):
                    if conn.status == psutil.CONN_LISTEN:
                        ports.append(conn.laddr.port)
            except Exception:
                pass

            for port in sorted(ports):
                if verify_ls_port(port, token):
                    _cached_ls_addr = f"localhost:{port}"
                    _cached_ls_token = token
                    log(f"Найдено подключение к Antigravity: {_cached_ls_addr}")
                    return _cached_ls_addr, _cached_ls_token
    except Exception as e:
        log(f"psutil discovery error: {e}")

    # 3. Fallback через PowerShell с надежным таймаутом 15с
    try:
        cmd = 'Get-CimInstance Win32_Process | Where-Object { $_.Name -like "*language_server*" } | Select-Object ProcessId, CommandLine | ConvertTo-Json'
        res = subprocess.run(['powershell', '-NoProfile', '-Command', cmd], capture_output=True, text=True, timeout=15)
        if res.stdout.strip():
            data = json.loads(res.stdout)
            if not isinstance(data, list):
                data = [data]
            
            target_proc = None
            for p in data:
                cmdline = p.get('CommandLine', '')
                if '--csrf_token' in cmdline and '--subclient_type' in cmdline:
                    target_proc = p
                    break
            if not target_proc:
                for p in data:
                    if '--csrf_token' in p.get('CommandLine', ''):
                        target_proc = p
                        break
            
            if target_proc:
                pid = target_proc.get('ProcessId')
                cmdline = target_proc.get('CommandLine', '')
                token_match = re.search(r'--csrf_token\s+([a-f0-9\-]+)', cmdline)
                token = token_match.group(1) if token_match else os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "")

                net_cmd = f'Get-NetTCPConnection -OwningProcess {pid} -State Listen | Select-Object -ExpandProperty LocalPort'
                net_res = subprocess.run(['powershell', '-NoProfile', '-Command', net_cmd], capture_output=True, text=True, timeout=15)
                ports = [int(p.strip()) for p in net_res.stdout.split() if p.strip().isdigit()]

                for port in sorted(ports):
                    if verify_ls_port(port, token):
                        _cached_ls_addr = f"localhost:{port}"
                        _cached_ls_token = token
                        return _cached_ls_addr, _cached_ls_token
    except Exception as e:
        log(f"PowerShell discovery error: {e}")

    return os.environ.get("ANTIGRAVITY_LS_ADDRESS", "localhost:54044"), os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "")

def format_telegram_response(text: str) -> str:
    """
    Превращает Markdown от нейросети в аккуратный, чистый Telegram HTML.
    Убирает решетки ###, звездочки **, цитаты >, полосы ---, оформляет ссылки в <a href> и списки.
    """
    if not text:
        return ""

    # 1. Извлекаем блоки кода
    code_blocks = []
    def extract_code_block(m):
        code = m.group(2).strip()
        escaped = html.escape(code)
        code_blocks.append(f"<pre><code>{escaped}</code></pre>")
        return f"___CODE_{len(code_blocks)-1}___"

    text = re.sub(r'```([a-zA-Z0-9_\-\+]*)\n?(.*?)```', extract_code_block, text, flags=re.DOTALL)

    # 2. Извлекаем инлайн-код
    inline_codes = []
    def extract_inline_code(m):
        code = m.group(1)
        escaped = html.escape(code)
        inline_codes.append(f"<code>{escaped}</code>")
        return f"___INLINE_{len(inline_codes)-1}___"

    text = re.sub(r'`([^`\n]+)`', extract_inline_code, text)

    # 3. Извлекаем Markdown ссылки [текст](url)
    link_items = []
    def extract_link(m):
        lt = html.escape(m.group(1).strip())
        lu = html.escape(m.group(2).strip())
        link_items.append(f'<a href="{lu}">{lt}</a>')
        return f"___LINK_{len(link_items)-1}___"

    text = re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', extract_link, text)

    # 4. Убираем цитатные стрелки > в начале строк
    text = re.sub(r'^[ \t]*>[ \t]*', '', text, flags=re.MULTILINE)

    # 5. Экранируем HTML в остальном тексте
    text = html.escape(text)

    # 6. Убираем HTML-артефакты типа &lt;kbd&gt;
    text = re.sub(r'&lt;/?kbd&gt;', '', text)
    text = re.sub(r'&lt;/?br\s*/?&gt;', '\n', text)

    # 7. Преобразуем заголовки (### Заголовок -> <b>Заголовок</b>)
    text = re.sub(r'^#{1,6}\s*(.*?)$', r'<b>\1</b>', text, flags=re.MULTILINE)

    # 8. Убираем разделительные линии --- или ***
    text = re.sub(r'^[ \t]*[-*_]{3,}[ \t]*$', '', text, flags=re.MULTILINE)

    # 9. Жирный текст **текст**
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)

    # 10. Курсив *текст* или _текст_
    text = re.sub(r'(?<!\w)\*([^\*\n]+?)\*(?!\w)', r'<i>\1</i>', text)
    text = re.sub(r'(?<!\w)_([^_\n]+?)_(?!\w)', r'<i>\1</i>', text)

    # 11. Очищаем оставшиеся одиночные звездочки
    text = text.replace('*', '')

    # 12. Красивые маркеры списков
    text = re.sub(r'^[ \t]*[-]\s+', '• ', text, flags=re.MULTILINE)

    # 13. Возвращаем блоки кода, инлайн-код и ссылки
    for i, b in enumerate(code_blocks):
        text = text.replace(f"___CODE_{i}___", b)
    for i, c in enumerate(inline_codes):
        text = text.replace(f"___INLINE_{i}___", c)
    for i, l in enumerate(link_items):
        text = text.replace(f"___LINK_{i}___", l)

    # 14. Сжимаем лишние переводы строк
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def strip_all_markdown(text: str) -> str:
    """Чистый текст без артефактов на случай ошибки парсера Telegram"""
    # Сохраняем содержимое блоков кода, не превращая в [Код]
    text = re.sub(r'```[a-zA-Z0-9_\-\+]*\n?(.*?)```', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', r'\1 (\2)', text)
    text = re.sub(r'^[ \t]*>[ \t]*', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'^#{1,6}\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[ \t]*[-*_]{3,}[ \t]*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('*', '')
    return text.strip()

def smart_chunk_text(text: str, max_size: int = 3800) -> list[str]:
    """Разбивает текст на части по границам абзацев без разрыва HTML-тегов"""
    if len(text) <= max_size:
        return [text]
    paras = text.split("\n\n")
    chunks = []
    curr = []
    curr_len = 0
    for p in paras:
        if curr_len + len(p) + 2 > max_size and curr:
            chunks.append("\n\n".join(curr))
            curr = [p]
            curr_len = len(p)
        else:
            curr.append(p)
            curr_len += len(p) + 2
    if curr:
        chunks.append("\n\n".join(curr))
    return chunks

async def send_clean_telegram_message(message: Message, text: str):
    """Отправляет сообщение с идеальным форматированием, автоматически подхватывая и отправляя фото"""
    img_matches = re.findall(r'!\[(.*?)\]\((.*?)\)', text)
    for caption, raw_img_path in img_matches:
        img_path = urllib.parse.unquote(raw_img_path.strip())
        if img_path.startswith("file:///"):
            img_path = img_path[8:]
        clean_path = img_path
        if not os.path.exists(clean_path):
            clean_path = img_path.lstrip('/')
        if not os.path.exists(clean_path):
            alt_path = os.path.join(WORKSPACE_DIR, os.path.basename(img_path))
            if os.path.exists(alt_path):
                clean_path = alt_path
        if os.path.exists(clean_path):
            try:
                await message.answer_photo(FSInputFile(clean_path), caption=caption or None)
                log(f"Фото отправлено в Telegram: {clean_path}")
            except Exception as e:
                log(f"Ошибка отправки фото {clean_path}: {e}")
        else:
            log(f"Файл фото не найден: {raw_img_path} (проверен как {clean_path})")

    text = re.sub(r'!\[.*?\]\(.*?\)', '', text).strip()
    if not text:
        return

    formatted = format_telegram_response(text)
    chunks = smart_chunk_text(formatted, max_size=3800)
    for chunk in chunks:
        try:
            await message.answer(chunk, parse_mode="HTML")
        except Exception as e:
            log(f"HTML отправка не удалась ({e}), отправляю чистым текстом...")
            clean = strip_all_markdown(chunk)
            for pc in smart_chunk_text(clean, max_size=3800):
                await message.answer(pc)

    # Проверка голосового режима (Voice Mode)
    cfg = load_config()
    if cfg.get("voice_mode"):
        try:
            voice_file = await asyncio.to_thread(generate_speech_audio, text)
            if voice_file and os.path.exists(voice_file):
                await message.answer_voice(FSInputFile(voice_file), caption="🎙 Голосовой ответ")
                try:
                    os.remove(voice_file)
                except Exception:
                    pass
        except Exception as e_voice:
            log(f"Ошибка отправки голосового ответа: {e_voice}")

def send_to_antigravity(prompt_text: str) -> bool:
    """Отправляет сообщение напрямую в текущий диалог Antigravity с авто-переподключением при смене порта"""
    global _cached_ls_addr, _cached_ls_token
    cid, _, _, _, _ = get_brain_paths()
    clean_prompt = prompt_text
    
    cfg = load_config()
    target_model = cfg.get("requested_model")
    planner_config = {"supportsLatexRendering": True}
    if target_model:
        planner_config["requestedModel"] = {"model": target_model}

    payload = {
        "cascadeId": cid,
        "items": [
            {"text": clean_prompt}
        ],
        "cascadeConfig": {
            "plannerConfig": planner_config
        }
    }
    for attempt in range(2):
        addr, token = get_ls_credentials()
        url = f"http://{addr}/exa.language_server_pb.LanguageServerService/SendUserCascadeMessage"
        headers = {
            "Content-Type": "application/json",
            "x-codeium-csrf-token": token,
            "Connect-Protocol-Version": "1"
        }
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                log(f"Отправлено в Antigravity ({addr}) [диалог {cid[:8]}...]: {prompt_text[:60]}... Статус: {resp.status}")
                return resp.status == 200
        except Exception as e:
            log(f"Ошибка отправки в Antigravity ({addr}, попытка {attempt+1}): {e}")
            _cached_ls_addr = None
            _cached_ls_token = None
            time.sleep(0.5)

    return False

async def transcribe_voice(file_bytes: bytes, filename: str = "voice.ogg") -> str:
    """Расшифровка голосовых: 1) Gemini 2.5 Flash STT (напрямую, без блокировок в РФ) -> 2) Groq Whisper API"""
    # 1. Google Gemini 2.5 Flash (нативно и без региональных блокировок)
    gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if gemini_key:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}"
            payload = {
                "contents": [{
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": "audio/ogg",
                                "data": base64.b64encode(file_bytes).decode("ascii")
                            }
                        },
                        {
                            "text": "Транскрибируй эту голосовую запись на русском языке в точности, как сказано. Выведи ТОЛЬКО расшифрованный текст, без комментариев, префиксов или кавычек."
                        }
                    ]
                }],
                "generationConfig": {
                    "temperature": 0.0
                }
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        candidates = result.get("candidates", [])
                        if candidates:
                            parts = candidates[0].get("content", {}).get("parts", [])
                            if parts:
                                text = parts[0].get("text", "").strip()
                                if text:
                                    log(f"🎙 Gemini STT: {text}")
                                    return text
                    else:
                        err = await resp.text()
                        log(f"Gemini STT error ({resp.status}): {err[:120]}")
        except Exception as e:
            log(f"Gemini STT exception: {e}")

    # 2. Groq Whisper API (резерв)
    if GROQ_API_KEY:
        try:
            url = "https://api.groq.com/openai/v1/audio/transcriptions"
            headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
            data = aiohttp.FormData()
            data.add_field("file", file_bytes, filename=filename, content_type="audio/ogg")
            data.add_field("model", "whisper-large-v3")
            data.add_field("language", "ru")

            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, data=data, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        text = result.get("text", "").strip()
                        log(f"🎙 Whisper: {text}")
                        return text
                    else:
                        err = await resp.text()
                        log(f"Groq Whisper error: {err[:120]}")
        except Exception as e:
            log(f"Groq Whisper exception: {e}")

    return ""

def get_transcript_line_count() -> int:
    _, _, _, t_path, tf_path = get_brain_paths()
    path = tf_path if os.path.exists(tf_path) else t_path
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return sum(1 for _ in f)
    return 0

def get_antigravity_run_status(cascade_id: str):
    """Read the current run state without starting or changing a conversation."""
    try:
        addr, token = get_ls_credentials()
        payload = json.dumps({"cascadeId": cascade_id}).encode("utf-8")
        request = urllib.request.Request(
            f"http://{addr}/exa.language_server_pb.LanguageServerService/GetCascadeTrajectory",
            data=payload,
            headers={"Content-Type": "application/json", "x-codeium-csrf-token": token,
                     "Connect-Protocol-Version": "1"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=5) as response:
            return json.load(response).get("status")
    except Exception as exc:
        log(f"Не удалось проверить состояние Antigravity: {type(exc).__name__}")
        return None


def format_antigravity_backend_error(error) -> str:
    """Explain a backend failure without exposing raw URLs, tokens or stack traces."""
    detail = error if isinstance(error, str) else json.dumps(error, ensure_ascii=False)
    lowered = detail.lower()
    if "no capacity" in lowered:
        reason = "У выбранной модели Gemini сейчас нет свободной мощности."
    elif "eof" in lowered or "network issue" in lowered or "connection" in lowered:
        reason = "Соединение Antigravity с сервером модели оборвалось."
    elif "resource_exhausted" in lowered or "quota" in lowered or "429" in lowered:
        reason = "Сервис модели сообщил об ограничении запросов или квоты."
    elif "unauthenticated" in lowered or "permission_denied" in lowered:
        reason = "Сервис модели отклонил авторизацию или доступ."
    else:
        reason = "Сервис модели вернул ошибку."
    return ("⚠️ Antigravity не смог получить ответ.\n" + reason
            + "\nЭто ошибка выполнения, а не продолжающаяся задача."
            + " Повторите запрос позже или выберите другую доступную модель в Antigravity.")


async def wait_for_agent_response(initial_line_count: int, timeout_sec: int = 1800, idle_timeout_sec: int = 600, status_updater=None) -> str:
    """
    Следит за transcript.jsonl и ждет финального ответа агента.
    Поддерживает динамический keep-alive при активных действиях агента (инструменты, команды).
    Не сбрасывает ожидание, пока агент активно работает.
    """
    cascade_id, _, _, t_path, tf_path = get_brain_paths()
    path = tf_path if os.path.exists(tf_path) else t_path
    start_time = time.time()
    last_activity_time = time.time()
    last_reported_action = ""
    last_seen_lines = initial_line_count
    last_error_status_check = 0.0
    last_checked_error = None

    while True:
        await asyncio.sleep(2)
        now = time.time()

        # 1. Общий предельный таймаут задачи (по умолчанию 600с)
        if now - start_time > timeout_sec:
            log(f"Общий таймаут ожидания Antigravity: прошло {int(now - start_time)}с (лимит {timeout_sec}с). Прерываю ожидание.")
            break

        # 2. Таймаут бездействия (нет новых записей в логе transcript.jsonl)
        if now - last_activity_time > idle_timeout_sec:
            log(f"Таймаут бездействия Antigravity: нет новых строк {int(now - last_activity_time)}с (лимит {idle_timeout_sec}с).")
            break

        if not os.path.exists(path):
            continue

        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()

            total_lines = len(lines)
            if total_lines <= initial_line_count:
                continue

            # Если добавились новые строки — фиксируем активность
            if total_lines > last_seen_lines:
                last_activity_time = now
                last_seen_lines = total_lines

            new_lines = lines[initial_line_count:]
            found_final = None
            active_action = None
            active_thinking = None
            steps_history = []

            for line in new_lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue

                d_type = data.get("type")
                if d_type == "PLANNER_RESPONSE":
                    tc = data.get("tool_calls")
                    cnt = data.get("content")
                    th = data.get("thinking")
                    if th and isinstance(th, str) and th.strip():
                        clean_th = re.sub(r'\s+', ' ', th.strip())
                        active_thinking = clean_th[:100] + "..." if len(clean_th) > 100 else clean_th

                    if cnt and not tc:
                        found_final = cnt
                    elif tc:
                        for call in tc:
                            c_name = call.get("name", "tool")
                            c_args = call.get("args", {})
                            s = c_args.get("toolSummary") or c_args.get("toolAction")
                            if not s:
                                if c_name == "run_command":
                                    cmd = c_args.get("CommandLine", "")
                                    s = f"Команда: {cmd[:35]}" if len(cmd) > 35 else f"Команда: {cmd}"
                                elif c_name == "read_file":
                                    s = f"Чтение {os.path.basename(c_args.get('Path', ''))}"
                                elif c_name == "edit_file":
                                    s = f"Правка {os.path.basename(c_args.get('Path', ''))}"
                                elif c_name == "write_file":
                                    s = f"Создание {os.path.basename(c_args.get('Path', ''))}"
                                elif c_name == "search_web":
                                    s = f"Поиск: {c_args.get('query', '')[:30]}"
                                else:
                                    s = c_name
                            s_clean = str(s).strip('"\'')
                            steps_history.append(s_clean)
                            active_action = s_clean

            if found_final:
                log(f"Получен финальный ответ от Antigravity ({len(found_final)} симв.)")
                return found_final

            # Обновляем статус каждые 2 секунды с детальным отображением прогресса
            if status_updater:
                elapsed = int(now - start_time)
                live_text = format_live_status(steps_history, active_action, active_thinking, elapsed)
                if live_text != last_reported_action:
                    last_reported_action = live_text
                    await status_updater(live_text)

        except Exception as e:
            log(f"Ошибка чтения transcript: {e}")

    return ""

async def background_wait_and_send(message: Message, initial_line_count: int, status_msg: Message):
    """Фоновый поллер: если задача длится очень долго (>15 мин), не теряет ответ, а дожидается и отправляет в чат."""
    cascade_id, _, _, t_path, tf_path = get_brain_paths()
    path = tf_path if os.path.exists(tf_path) else t_path
    start_time = time.time()
    last_action = ""
    last_error_status_check = 0.0
    last_checked_error = None

    while time.time() - start_time < 3600:
        await asyncio.sleep(4)
        if not os.path.exists(path):
            continue

        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()

            if len(lines) <= initial_line_count:
                continue

            new_lines = lines[initial_line_count:]
            found_final = None
            found_action = None

            for line in reversed(new_lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue

                if data.get("type") == "PLANNER_RESPONSE":
                    tc = data.get("tool_calls")
                    cnt = data.get("content")
                    if cnt and not tc and found_final is None:
                        found_final = cnt
                        break
                    if tc and not found_action:
                        t_first = tc[0]
                        args = t_first.get("args", {})
                        summary = args.get("toolSummary") or args.get("toolAction") or t_first.get("name", "tool")
                        found_action = str(summary).strip('"\'')

            if found_final:
                log(f"Фоновый поллер дождался ответа Antigravity ({len(found_final)} симв.)")
                try:
                    await status_msg.delete()
                except Exception:
                    pass
                clear_pending_task()
                await send_clean_telegram_message(message, found_final)
                return

            if found_action and found_action != last_action:
                last_action = found_action
                elapsed = int(time.time() - start_time)
                try:
                    await status_msg.edit_text(
                        f"⏳ <b>Задача выполняется в фоне</b> ({elapsed}с)...\n⚙️ {html.escape(found_action)}",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

        except Exception as e:
            log(f"Ошибка background_wait: {e}")

    try:
        await status_msg.edit_text("⚠️ Время ожидания выполнения задачи в Antigravity истекло (1 час).")
    except Exception:
        pass

async def resume_pending_task_if_any(bot: Bot):
    """Watchdog Recovery: подхватывает незавершенную задачу при перезапуске процесса"""
    if not os.path.exists(PENDING_TASK_FILE):
        return
    try:
        with open(PENDING_TASK_FILE, "r", encoding="utf-8") as f:
            task = json.load(f)
    except Exception:
        clear_pending_task()
        return

    chat_id = task.get("chat_id")
    status_msg_id = task.get("status_msg_id")
    initial_line_count = task.get("initial_line_count", 0)
    prompt = task.get("prompt", "")
    ts = task.get("timestamp", 0)

    # Задачи старше 2 часов не восстанавливаем
    if time.time() - ts > 7200:
        clear_pending_task()
        return

    log(f"🔄 [Watchdog Recovery] Подхватываем задачу: chat={chat_id}, msg={status_msg_id}")

    async def update_status(t: str):
        try:
            await bot.edit_message_text(t, chat_id=chat_id, message_id=status_msg_id, parse_mode="HTML")
        except Exception:
            try:
                await bot.edit_message_text(t, chat_id=chat_id, message_id=status_msg_id)
            except Exception:
                pass

    await update_status("🔄 <i>Связь восстановлена, подхватываю ответ Antigravity...</i>")

    class ResumeMessage:
        def __init__(self, cid, b):
            self.chat = type("Chat", (), {"id": cid})()
            self.bot = b
        async def answer(self, txt, parse_mode="HTML", **kwargs):
            return await self.bot.send_message(chat_id=self.chat.id, text=txt, parse_mode=parse_mode, **kwargs)

    resume_msg = ResumeMessage(chat_id, bot)
    reply = await wait_for_agent_response(initial_line_count, status_updater=update_status)

    if not reply:
        class ResumeStatusMsg:
            def __init__(self, cid, mid, b):
                self.chat_id = cid
                self.message_id = mid
                self.bot = b
            async def edit_text(self, txt, parse_mode="HTML", **kwargs):
                return await self.bot.edit_message_text(txt, chat_id=self.chat_id, message_id=self.message_id, parse_mode=parse_mode, **kwargs)
            async def delete(self):
                return await self.bot.delete_message(chat_id=self.chat_id, message_id=self.message_id)

        mock_status = ResumeStatusMsg(chat_id, status_msg_id, bot)
        asyncio.create_task(background_wait_and_send(resume_msg, initial_line_count, mock_status))
        return

    try:
        await bot.delete_message(chat_id=chat_id, message_id=status_msg_id)
    except Exception:
        pass

    clear_pending_task()
    await send_clean_telegram_message(resume_msg, reply)
    log("✅ [Watchdog Recovery] Ответ успешно доставлен пользователю!")

dp = Dispatcher()

@dp.callback_query(F.data == "live_screenshot")
async def cb_live_screenshot(callback: CallbackQuery):
    """Мгновенный снимок экрана по кнопке из Telegram на телефоне"""
    try:
        await callback.answer("📸 Делаю снимок экрана...")
    except Exception:
        pass
    try:
        shot = ImageGrab.grab()
        shot_path = os.path.join(WORKSPACE_DIR, "live_screen.png")
        shot.save(shot_path, "PNG")
        cur_time = time.strftime("%H:%M:%S")
        cid = get_active_conversation_id()[:8]
        await callback.message.answer_photo(
            FSInputFile(shot_path),
            caption=f"🖥 <b>Экран ПК</b> [{cur_time}] (сессия: <code>{cid}...</code>)",
            parse_mode="HTML"
        )
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка захвата экрана: {e}")



async def ensure_authorized(message: Message) -> bool:
    global allowed_chat_ids
    if not allowed_chat_ids:
        allowed_chat_ids.add(message.chat.id)
        save_allowed_chat_ids()
        await message.answer(f"✅ Вы авторизованы как владелец! Чат ID: <code>{message.chat.id}</code>", parse_mode="HTML")
        return True

    if message.chat.id not in allowed_chat_ids:
        await message.answer("⛔ Доступ запрещен. Бот настроен на конкретного владельца.")
        return False
    return True

def check_and_click_submit() -> bool:
    """Отключен перебор пикселей во избежание ложных нажатий по интерфейсу пользователя. 
    Авто-подтверждение достигается настройкой Antigravity (always-proceed)."""
    return False

last_evening_digest_date = ""
active_bot = None

async def auto_approval_loop(bot_placeholder=None):
    while True:
        await asyncio.sleep(30)
        check_and_click_submit()

def record_screen_video_note(duration_sec=3) -> str:
    """Записывает 3-секундный видео-кружок с экрана для Telegram"""
    user32 = ctypes.windll.user32
    hdesk = user32.OpenDesktopW('Default', 0, False, 0x0100)
    user32.SetThreadDesktop(hdesk)
    
    tmp = os.path.join(WORKSPACE_DIR, "temp_vn")
    os.makedirs(tmp, exist_ok=True)
    num_frames = int(duration_sec * 6)
    for i in range(num_frames):
        im = ImageGrab.grab()
        w, h = im.size
        side = min(w, h)
        left = (w - side) // 2
        im.crop((left, 0, left + side, side)).resize((480, 480)).save(f"{tmp}/f_{i:03d}.jpg", quality=80)
        time.sleep(0.15)
        
    out_path = os.path.join(WORKSPACE_DIR, "screen_note.mp4")
    cmd = ['ffmpeg', '-y', '-framerate', '6', '-i', f"{tmp}/f_%03d.jpg", '-c:v', 'libx264', '-pix_fmt', 'yuv420p', out_path]
    subprocess.run(cmd, capture_output=True)
    shutil.rmtree(tmp, ignore_errors=True)
    return out_path

def capture_screen_photo() -> str:
    """Делает полноэкранный снимок рабочего стола"""
    user32 = ctypes.windll.user32
    hdesk = user32.OpenDesktopW('Default', 0, False, 0x0100)
    user32.SetThreadDesktop(hdesk)
    im = ImageGrab.grab()
    path = os.path.join(WORKSPACE_DIR, "screen_shot.jpg")
    im.save(path, quality=90)
    return path


@dp.message(F.text.regexp(r"(?i)^(/help|/помощь|помощь|команды|меню)$"))
async def cmd_help(message: Message):
    if not await ensure_authorized(message):
        return
    help_text = (
        "🤖 <b>Команды и возможности Antigravity Bridge:</b>\n\n"
        "🎙 <b>Живой голосовой ассистент:</b>\n"
        "• <code>/call</code> (или <i>«звонок»</i>) — <b>Живой разговор в реальном времени (Gemini Live)</b>\n"
        "• <code>/voice on/off</code> — озвучка текстовых ответов бота\n\n"
        "🧠 <b>Управление моделями:</b>\n"
        "• <code>/model</code> — посмотреть текущую модель\n"
        "• <code>/model auto</code> — брать модель с ПК (Gemini 4 Argon / Flash)\n"
        "• <code>/model flash</code> — включить быстрый Gemini 3.8 Flash\n\n"
        "🖥 <b>Экран компьютера:</b>\n"
        "• <code>/screen</code> (или <i>«скрин»</i>) — снимок экрана прямо сейчас\n"
        "• <code>/circle</code> (или <i>«кружок»</i>) — видео-кружочек с экрана ПК\n\n"
        "⚡ <b>Сессия и кэш:</b>\n"
        "• <code>/cache</code> (или <i>«кэш»</i>) — проверка расхода памяти и токенов\n"
        "• <code>/session</code> — ID текущей сессии Antigravity\n"
        "• <code>/new</code> — начать новый чат в Antigravity\n\n"
        "🛍 <b>3D-печать:</b>\n"
        "• <code>/shop</code> — открыть интернет-магазин (Mini App)\n\n"
        "📝 <b>Заметки:</b>\n"
        "• <i>«Заметка: ...»</i> или <i>«Запиши: ...»</i> — сохранить мысль\n"
        "• <code>/notes</code> — зачитать список заметок голосом\n"
        "• <code>/clear_notes</code> — очистить список заметок\n\n"
        "💡 <i>Любое обычное текстовое или голосовое сообщение отправляется агенту Antigravity.</i>"
    )
    await message.answer(help_text, parse_mode="HTML")

@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    if await ensure_authorized(message):
        await message.answer(
            "👋 <b>Привет!</b> Я — прямой мост к <b>Antigravity</b>.\n\n"
            "✨ <b>Поддерживается:</b>\n"
            "• Текстовые сообщения\n"
            "• Голосовые команды (Whisper)\n"
            "• Фотографии и скриншоты (компьютерное зрение)\n"
            "• Файлы и документы\n"
            "• 🎥 <b>Кружочки экрана</b>: напиши «кружок» или «покажи экран»\n"
            "• 📸 <b>Скриншот экрана</b>: напиши «скрин»\n"
            "• 🛍 <b>Магазин 3D-печати</b>: команда /shop или слово «магазин»\n"
            "• ⚡ <b>Автоподтверждение</b>: подтверждает запросы доступа автоматически!\n\n"
            "Диалог форматируется красиво и аккуратно.",
            parse_mode="HTML"
        )

@dp.message(F.text.regexp(r"(?i)^(/session|/сессия|сессия)$"))
async def cmd_session(message: Message):
    if not await ensure_authorized(message):
        return
    cid = get_active_conversation_id()
    await message.answer(
        f"📌 <b>Привязанная сессия Antigravity:</b>\n"
        f"<code>{cid}</code>\n\n"
        f"💡 Напишите <code>/new</code>, чтобы сбросить привязку и переключиться на самый свежий чат.",
        parse_mode="HTML"
    )

@dp.message(F.text.regexp(r"(?i)^(/model|модель)(\s+.*)?$"))
async def cmd_model(message: Message):
    if not await ensure_authorized(message):
        return
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    cfg = load_config()
    current_model = cfg.get("requested_model")
    
    if len(parts) == 1:
        status_text = "🖥 <b>Наследовать выбор с ПК (авто: Gemini 4 Argon / Flash)</b>" if not current_model else f"⚙️ <code>{current_model}</code>"
        await message.answer(
            f"🧠 <b>Модель для сообщений из Telegram:</b>\n{status_text}\n\n"
            f"💡 <b>Команды переключения:</b>\n"
            f"• <code>/model auto</code> — автоматически использовать модель, выбранную на ПК в Antigravity (Argon/Flash)\n"
            f"• <code>/model flash</code> — принудительно быстрый Gemini 3.8 Flash\n",
            parse_mode="HTML"
        )
        return
        
    arg = parts[1].strip().lower()
    if arg in ("auto", "авто", "reset", "сброс", "комп", "pc"):
        save_config({"requested_model": None})
        await message.answer("✅ <b>Установлен режим Auto:</b> теперь сообщения с телефона используют модель, выбранную на ПК!", parse_mode="HTML")
    elif "flash" in arg or "3.8" in arg:
        save_config({"requested_model": "MODEL_PLACEHOLDER_M318"})
        await message.answer("⚡ <b>Установлена модель:</b> Gemini 3.8 Flash (быстрые ответы).", parse_mode="HTML")
    else:
        await message.answer("Используйте <code>/model auto</code> или <code>/model flash</code>.", parse_mode="HTML")


@dp.message(F.text.regexp(r"(?i)^(/call|/live|звонок|позвонить|голос живой|живой голос|войс)$"))
async def cmd_live_call(message: Message):
    if not await ensure_authorized(message):
        return
    cfg = load_config()
    url = cfg.get("live_webapp_url")
    if not url:
        tunnel_file = os.path.join(WORKSPACE_DIR, "tools", "live_voice", "tunnel_url.txt")
        if os.path.exists(tunnel_file):
            try:
                with open(tunnel_file, "r", encoding="utf-8") as f:
                    url = f.read().strip()
            except Exception:
                pass
    if not url:
        url = os.getenv("LIVE_WEBAPP_URL", "")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🎙 Начать живой разговор (Gemini Live)",
                web_app=WebAppInfo(url=url)
            )
        ],
        [
            InlineKeyboardButton(
                text="🌐 Открыть в браузере",
                url=url
            )
        ]
    ])
    await message.answer(
        "📞 <b>Живой голосовой ассистент Antigravity (Gemini Live)</b>\n\n"
        "⚡ <b>Как это работает:</b>\n"
        "• <b>Full-Duplex связь</b>: говорите прямо в микрофон смартфона, ассистент отвечает живым голосом без пауз (~300 мс).\n"
        "• <b>Мгновенные перебивания</b>: можно перебивать на полуслове, как в обычном разговоре.\n"
        "• <b>Синхронизация с кодом</b>: скажите «напиши функцию...» или «сделай скриншот» — задача сразу отправится в Antigravity на ПК!\n\n"
        "Нажмите кнопку ниже, чтобы начать разговор прямо в Telegram 👇",
        reply_markup=kb,
        parse_mode="HTML"
    )

@dp.message(F.text.regexp(r"(?i)^(/voice|голос)(\s+.*)?$"))
async def cmd_voice(message: Message):
    if not await ensure_authorized(message):
        return
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    cfg = load_config()
    is_voice = cfg.get("voice_mode", False)
    
    if len(parts) == 1:
        status_text = "🎙 <b>ВКЛЮЧЕН</b> (бот отвечает текстом + голосом)" if is_voice else "🔇 <b>ВЫКЛЮЧЕН</b> (только текст)"
        await message.answer(
            f"🔊 <b>Голосовой режим ответов:</b> {status_text}\n\n"
            f"💡 <b>Команды переключения:</b>\n"
            f"• <code>/voice on</code> — включить голосовые ответы\n"
            f"• <code>/voice off</code> — выключить голосовые ответы (только текст)\n",
            parse_mode="HTML"
        )
        return
        
    arg = parts[1].strip().lower()
    if arg in ("on", "1", "вкл", "включить", "да", "yes"):
        save_config({"voice_mode": True})
        await message.answer("🎙 <b>Голосовой режим ВКЛЮЧЕН!</b>\nТеперь на твои запросы я буду присылать аудиосообщение в дополнение к тексту.", parse_mode="HTML")
    elif arg in ("off", "0", "выкл", "выключить", "нет", "no"):
        save_config({"voice_mode": False})
        await message.answer("🔇 <b>Голосовой режим ВЫКЛЮЧЕН.</b>\nОтветы будут приходить только текстом.", parse_mode="HTML")
    else:
        await message.answer("Используйте <code>/voice on</code> или <code>/voice off</code>.", parse_mode="HTML")


@dp.message(F.text.regexp(r"(?i)^(/cache|кэш|память|лимиты)$"))
async def cmd_cache_health(message: Message):
    if not await ensure_authorized(message):
        return
    import subprocess
    try:
        res = subprocess.run([sys.executable, "tools/safety/cache_tax_monitor.py"], capture_output=True, text=True, encoding="utf-8")
        out = res.stdout.strip()
        await message.answer(f"<pre>{out}</pre>", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка проверки кэша: {e}")

@dp.message(F.text.regexp(r"(?i)^(/new|/reset|/новый|новый\s*чат)$"))
async def cmd_new(message: Message):
    if not await ensure_authorized(message):
        return
    save_config({"pinned_conversation_id": None})
    cid = get_active_conversation_id()
    await message.answer(
        f"✨ <b>Автоподхват свежего диалога активирован!</b>\n\n"
        f"Текущая сессия: <code>{cid}</code>\n\n"
        f"✅ При создании нового чата в Antigravity бот сразу начнёт отправлять сообщения в него.",
        parse_mode="HTML"
    )

async def send_notes_digest(message: Message, bot: Bot):
    notes = get_today_notes()
    if not notes:
        await message.answer(
            "📝 <b>За сегодня пока нет сохранённых мыслей и заметок.</b>\n\n"
            "💡 <b>Как сохранить:</b>\n"
            "Наговорите в голосовом или напишите:\n"
            "• <i>«Заметка: купить сверла 4мм»</i>\n"
            "• <i>«Мысль: идея для нового бота»</i>\n"
            "• <i>«Запиши: позвонить клиенту»</i>\n\n"
            "Вечером я зачитаю их все голосом!",
            parse_mode="HTML"
        )
        return

    today_str = datetime.now().strftime("%d.%m.%Y")
    lines = [f"📋 <b>Ваши мысли и заметки за {today_str}:</b>\n"]
    for i, n in enumerate(notes, 1):
        lines.append(f"<b>{i}.</b> [{n['time']}] {html.escape(n['text'])}")

    lines.append("\n🎙 <i>Генерирую голосовую озвучку мыслей...</i>")
    status_msg = await message.answer("\n".join(lines), parse_mode="HTML")

    voice_file = await asyncio.to_thread(generate_voice_digest, notes)
    if voice_file and os.path.exists(voice_file):
        try:
            await bot.send_voice(
                chat_id=message.chat.id,
                voice=FSInputFile(voice_file),
                caption=f"🎙 Голосовая сводка за {today_str}"
            )
            await status_msg.edit_text("\n".join(lines[:-1]), parse_mode="HTML")
        except Exception as e:
            log(f"Ошибка отправки voice digest: {e}")

@dp.message(F.text.func(is_notes_review_query))
async def cmd_notes(message: Message, bot: Bot):
    if not await ensure_authorized(message):
        return
    await send_notes_digest(message, bot)

@dp.message(F.text.regexp(r"(?i)^(/clear_notes|очисти\s+заметки|стереть\s+заметки|удалить\s+заметки)$"))
async def cmd_clear_notes(message: Message):
    if not await ensure_authorized(message):
        return
    clear_today_notes()
    await message.answer("🗑 <b>Заметки за сегодня очищены!</b>", parse_mode="HTML")



SHOP_URL = "https://lucky-llamas-stand.loca.lt"

@dp.message(F.text.regexp(r"(?i)^(/shop|/магазин|/catalog|/каталог|магазин|каталог|витрина)$"))
async def cmd_shop(message: Message):
    if not await ensure_authorized(message):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🛍 Открыть 3D-Магазин (Mini App)",
                web_app=WebAppInfo(url=SHOP_URL)
            )
        ],
        [
            InlineKeyboardButton(
                text="🌐 Открыть в браузере",
                url=SHOP_URL
            )
        ]
    ])
    await message.answer(
        "🛍 <b>Интернет-магазин 3D-печати готов!</b>\n\n"
        "✨ <b>В магазине доступны:</b>\n"
        "• <b>Каталог:</b> Корона Рейниры, Бигль-блогер, Лесной Гном, Кашпо-человечки, Шестерни\n"
        "• <b>Калькулятор печати:</b> расчет цены по весу и материалам (PET-G, PLA, Nylon, SLA)\n"
        "• <b>Корзина и оформление:</b> доставка (СДЭК, Почта), мгновенное оповещение вам о новом заказе!\n\n"
        "Нажмите кнопку ниже, чтобы запустить магазин прямо в Telegram 👇",
        reply_markup=kb,
        parse_mode="HTML"
    )

@dp.message(F.web_app_data)
async def handle_web_app_data(message: Message):
    try:
        data = json.loads(message.web_app_data.data)
        cust = data.get("customer", {})
        total = data.get("totalSum", 0)
        items = data.get("items", [])
        items_desc = "\n".join([f"• {it.get('title')} ({it.get('option', 'Стандарт')}) x{it.get('qty', 1)}" for it in items])
        await message.answer(
            f"🎉 <b>Ваш заказ успешно принят!</b>\n\n"
            f"👤 Клиент: <b>{cust.get('name')}</b> ({cust.get('contact')})\n"
            f"📦 Состав:\n{items_desc}\n\n"
            f"💰 Сумма: <b>{total:,} ₽</b>\n\n"
            f"Менеджер свяжется с вами для подтверждения доставки!",
            parse_mode="HTML"
        )
    except Exception as e:
        log(f"Ошибка web_app_data: {e}")

@dp.message(F.text.regexp(r"(?i)^(/circle|/video|/кружок|кружок|кружочек|видео|видеосообщение|покажи\s+экран)"))
async def handle_video_note_request(message: Message, bot: Bot):
    if not await ensure_authorized(message):
        return
    status_msg = await message.answer("🎥 Записываю видео-кружочек с экрана...")
    try:
        vn_path = await asyncio.to_thread(record_screen_video_note, 3)
        await bot.send_video_note(chat_id=message.chat.id, video_note=FSInputFile(vn_path))
        await status_msg.delete()
    except Exception as e:
        log(f"Ошибка записи кружочка: {e}")
        await status_msg.edit_text(f"❌ Не удалось записать кружочек: {e}")

@dp.message(F.text.regexp(r"(?i)^(/screen|/скрин|скрин|скриншот|экран|снимок)"))
async def handle_screen_request(message: Message, bot: Bot):
    if not await ensure_authorized(message):
        return
    status_msg = await message.answer("📸 Делаю снимок экрана...")
    try:
        photo_path = await asyncio.to_thread(capture_screen_photo)
        await bot.send_photo(chat_id=message.chat.id, photo=FSInputFile(photo_path), caption="🖥 Текущий снимок экрана")
        await status_msg.delete()
    except Exception as e:
        log(f"Ошибка создания скриншота: {e}")
        await status_msg.edit_text(f"❌ Не удалось сделать скриншот: {e}")

@dp.message(F.photo)
async def handle_photo_msg(message: Message, bot: Bot):
    if not await ensure_authorized(message):
        return

    status_msg = await message.answer("📸 Загружаю фото...")
    try:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        
        filename = f"photo_{int(time.time() * 1000)}.jpg"
        _, _, user_uploaded_dir, _, _ = get_brain_paths()
        save_path = os.path.join(user_uploaded_dir, filename)
        
        await bot.download_file(file_info.file_path, save_path)
        log(f"Фото сохранено: {save_path}")
        
        caption = message.caption or "Посмотри это фото и дай ответ."
        prompt = (
            f"[Пользователь прислал изображение: {save_path}]\n"
            f"Запрос пользователя: {caption}\n"
            f"Пожалуйста, изучи это изображение через view_file (или прямой просмотр) и ответь пользователю."
        )
        await status_msg.edit_text("⚙️ Передаю изображение на анализ...")
        await process_prompt_and_respond(message, prompt, status_msg)
    except Exception as e:
        log(f"Ошибка обработки фото: {e}")
        await status_msg.edit_text(f"❌ Ошибка обработки фото: {e}")

def find_blender_exe() -> str:
    default_path = r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe"
    if os.path.exists(default_path):
        return default_path
    base = r"C:\Program Files\Blender Foundation"
    if os.path.exists(base):
        for item in sorted(os.listdir(base), reverse=True):
            p = os.path.join(base, item, "blender.exe")
            if os.path.exists(p):
                return p
    return shutil.which("blender") or ""

def analyze_stl_file(path: str) -> dict:
    """Парсит STL-файл (бинарный или ASCII), вычисляет габариты, полигоны, объем и примерный вес"""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            header = f.read(80)
            num_triangles_bytes = f.read(4)
            num_triangles = struct.unpack('<I', num_triangles_bytes)[0] if len(num_triangles_bytes) == 4 else 0
            
            # Бинарный STL
            if size >= 84 and (size == 84 + num_triangles * 50 or (size - 84) % 50 == 0):
                min_x, max_x = float('inf'), float('-inf')
                min_y, max_y = float('inf'), float('-inf')
                min_z, max_z = float('inf'), float('-inf')
                vol = 0.0
                
                for _ in range(num_triangles):
                    data = f.read(50)
                    if len(data) < 50:
                        break
                    vals = struct.unpack('<12fH', data)
                    v1 = (vals[3], vals[4], vals[5])
                    v2 = (vals[6], vals[7], vals[8])
                    v3 = (vals[9], vals[10], vals[11])
                    for v in (v1, v2, v3):
                        min_x, max_x = min(min_x, v[0]), max(max_x, v[0])
                        min_y, max_y = min(min_y, v[1]), max(max_y, v[1])
                        min_z, max_z = min(min_z, v[2]), max(max_z, v[2])
                    vol += (v1[0]*(v2[1]*v3[2] - v2[2]*v3[1]) - v1[1]*(v2[0]*v3[2] - v2[2]*v3[0]) + v1[2]*(v2[0]*v3[1] - v2[1]*v3[0])) / 6.0
                
                dim_x = max(0.0, max_x - min_x)
                dim_y = max(0.0, max_y - min_y)
                dim_z = max(0.0, max_z - min_z)
                vol_cm3 = abs(vol) / 1000.0
                return {
                    'triangles': num_triangles,
                    'bbox': (round(dim_x, 2), round(dim_y, 2), round(dim_z, 2)),
                    'volume_cm3': round(vol_cm3, 2),
                    'weight_pla_g': round(vol_cm3 * 1.24, 2),
                    'weight_petg_g': round(vol_cm3 * 1.27, 2),
                }

        # Fallback для текстовых (ASCII) STL файлов
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            min_x, max_x = float('inf'), float('-inf')
            min_y, max_y = float('inf'), float('-inf')
            min_z, max_z = float('inf'), float('-inf')
            count = 0
            for line in f:
                line = line.strip()
                if line.startswith('vertex'):
                    parts = line.split()
                    if len(parts) >= 4:
                        try:
                            x, y, z = float(parts[1]), float(parts[2]), float(parts[3])
                            min_x, max_x = min(min_x, x), max(max_x, x)
                            min_y, max_y = min(min_y, y), max(max_y, y)
                            min_z, max_z = min(min_z, z), max(max_z, z)
                            count += 1
                        except ValueError:
                            pass
            if count > 0:
                triangles = count // 3
                dim_x = max(0.0, max_x - min_x)
                dim_y = max(0.0, max_y - min_y)
                dim_z = max(0.0, max_z - min_z)
                return {
                    'triangles': triangles,
                    'bbox': (round(dim_x, 2), round(dim_y, 2), round(dim_z, 2)),
                    'volume_cm3': 0.0,
                    'weight_pla_g': 0.0,
                    'weight_petg_g': 0.0,
                }
    except Exception as e:
        log(f"Ошибка парсинга STL {path}: {e}")
    return None

def render_stl_preview(stl_path: str, output_png: str) -> bool:
    """Генерирует изометрический CAD-рендер STL модели через Blender Workbench"""
    blender_exe = find_blender_exe()
    if not blender_exe or not os.path.exists(blender_exe):
        return False
    try:
        script = f"""
import bpy, os, mathutils

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.wm.stl_import(filepath=r"{stl_path}")

objs = [o for o in bpy.context.scene.objects if o.type == 'MESH']
if objs:
    obj = objs[0]
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.origin_set(type='ORIGIN_GEOMETRY', center='BOUNDS')
    obj.location = (0, 0, 0)
    
    dim = max(max(obj.dimensions), 1.0)
    
    cam_data = bpy.data.cameras.new("Cam")
    cam_obj = bpy.data.objects.new("Cam", cam_data)
    bpy.context.scene.collection.objects.link(cam_obj)
    bpy.context.scene.camera = cam_obj
    cam_obj.location = (dim * 1.5, -dim * 1.8, dim * 1.2)
    
    direction = -cam_obj.location
    cam_obj.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()
    
    scene = bpy.context.scene
    scene.render.engine = 'BLENDER_WORKBENCH'
    scene.render.resolution_x = 800
    scene.render.resolution_y = 600
    scene.render.filepath = r"{output_png}"
    scene.display.shading.light = 'MATCAP'
    scene.display.shading.color_type = 'SINGLE'
    scene.display.shading.single_color = (0.72, 0.75, 0.78)
    scene.display.render_aa = 'FXAA'
    
    bpy.ops.render.render(write_still=True)
"""
        py_tmp = os.path.join(WORKSPACE_DIR, f"temp_render_{int(time.time()*1000)}.py")
        with open(py_tmp, "w", encoding="utf-8") as f:
            f.write(script)
        
        subprocess.run([blender_exe, "-b", "--python", py_tmp], capture_output=True, timeout=20)
        try:
            os.remove(py_tmp)
        except Exception:
            pass
        return os.path.exists(output_png) and os.path.getsize(output_png) > 0
    except Exception as e:
        log(f"Ошибка рендера STL через Blender: {e}")
        return False

@dp.message(F.document)
async def handle_document_msg(message: Message, bot: Bot):
    if not await ensure_authorized(message):
        return

    doc = message.document
    filename = doc.file_name or f"file_{int(time.time() * 1000)}"
    is_stl = filename.lower().endswith(".stl")

    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        mb = round(doc.file_size / (1024 * 1024), 1)
        await message.answer(
            f"⚠️ <b>Файл слишком большой ({mb} МБ)</b>\n\n"
            f"Telegram Bot API не разрешает ботам скачивать файлы более <b>20 МБ</b>.\n\n"
            f"💡 <b>Решение:</b>\n"
            f"• Уменьшите полигональность (Decimate в Blender) или сохраните как бинарный STL (он в 5 раз меньше ASCII).\n"
            f"• Либо скопируйте файл прямо в папку проекта на ПК (<code>c:\\Users\\zalya\\Downloads\\антигравити 2</code>) и напишите его название в чат!",
            parse_mode="HTML"
        )
        return

    status_msg = await message.answer("📐 Загружаю STL-модель..." if is_stl else "📄 Загружаю файл...")
    try:
        file_info = await bot.get_file(doc.file_id)
        save_path = os.path.join(UPLOADS_DIR, filename)

        await bot.download_file(file_info.file_path, save_path)
        log(f"Файл сохранен: {save_path}")

        caption = message.caption or ""

        if is_stl:
            await status_msg.edit_text("⚙️ Анализирую 3D-геометрию и формирую превью...")
            stats = analyze_stl_file(save_path)
            preview_png = os.path.splitext(save_path)[0] + "_preview.png"
            has_render = render_stl_preview(save_path, preview_png)

            if stats and has_render:
                stl_card = (
                    f"📐 <b>3D-модель: {html.escape(filename)}</b>\n\n"
                    f"• Габариты: <b>{stats['bbox'][0]} × {stats['bbox'][1]} × {stats['bbox'][2]} мм</b>\n"
                    f"• Полигонов: <b>{stats['triangles']:,}</b>\n"
                    f"• Объем: <b>{stats['volume_cm3']} см³</b>\n"
                    f"• Расчетный вес: <b>~{stats['weight_pla_g']} г (PLA)</b>"
                )
                try:
                    await message.answer_photo(FSInputFile(preview_png), caption=stl_card, parse_mode="HTML")
                except Exception as pe:
                    log(f"Не удалось отправить фото превью STL: {pe}")

            geom_info = ""
            if stats:
                geom_info = (
                    f"\nПараметры геометрии модели:\n"
                    f"- Габариты (BBox): {stats['bbox'][0]} x {stats['bbox'][1]} x {stats['bbox'][2]} мм\n"
                    f"- Количество треугольников: {stats['triangles']}\n"
                    f"- Объем: {stats['volume_cm3']} см³\n"
                    f"- Расчетный вес для печати (PLA): ~{stats['weight_pla_g']} г\n"
                )

            render_info = f"- Сгенерирован CAD-рендер: {preview_png}\n" if has_render else ""

            prompt = (
                f"[Пользователь загрузил 3D-модель STL: {save_path}]\n"
                f"{geom_info}"
                f"{render_info}"
                f"Комментарий пользователя: {caption or 'Проанализируй модель и подскажи по ней или выполни необходимые операции.'}\n\n"
                f"Пожалуйста, изучи модель, учти указанные габариты и объем, и дай пользователю ответ."
            )
            await status_msg.edit_text("⚙️ Передаю задачу по 3D-модели в Antigravity...")
        else:
            caption_text = caption or f"Пользователь прислал файл {filename}."
            prompt = (
                f"[Пользователь прикрепил файл: {save_path}]\n"
                f"Комментарий: {caption_text}\n"
                f"Пожалуйста, открой этот файл через view_file, изучи его и ответь пользователю."
            )
            await status_msg.edit_text("⚙️ Файл передан в Antigravity...")

        await process_prompt_and_respond(message, prompt, status_msg)
    except Exception as e:
        log(f"Ошибка обработки документа/STL: {e}")
        await status_msg.edit_text(f"❌ Ошибка обработки файла: {e}")

@dp.message(F.video)
async def handle_video_msg(message: Message, bot: Bot):
    if not await ensure_authorized(message):
        return

    vid = message.video
    filename = vid.file_name or f"video_{int(time.time() * 1000)}.mp4"
    if vid.file_size and vid.file_size > 20 * 1024 * 1024:
        mb = round(vid.file_size / (1024 * 1024), 1)
        await message.answer(
            f"⚠️ <b>Видео слишком большое ({mb} МБ)</b>\n\nTelegram Bot API ограничивает загрузку 20 МБ. Скопируйте файл напрямую в папку проекта на ПК.",
            parse_mode="HTML"
        )
        return

    status_msg = await message.answer("🎬 Загружаю видео...")
    try:
        file_info = await bot.get_file(vid.file_id)
        save_path = os.path.join(UPLOADS_DIR, filename)
        await bot.download_file(file_info.file_path, save_path)
        log(f"Видео сохранено: {save_path}")

        caption = message.caption or ""
        caption_text = caption or f"Пользователь прислал видео {filename}."
        prompt = (
            f"[Пользователь прислал видео: {save_path}]\n"
            f"Комментарий: {caption_text}\n"
            f"Пожалуйста, изучи это видео и ответь пользователю."
        )
        await status_msg.edit_text("⚙️ Видео передано в Antigravity...")
        await process_prompt_and_respond(message, prompt, status_msg)
    except Exception as e:
        log(f"Ошибка обработки видео: {e}")
        await status_msg.edit_text(f"❌ Ошибка обработки видео: {e}")

@dp.message(F.video_note)
async def handle_video_note_incoming_msg(message: Message, bot: Bot):
    if not await ensure_authorized(message):
        return

    vn = message.video_note
    filename = f"video_note_{int(time.time() * 1000)}.mp4"
    status_msg = await message.answer("📹 Загружаю видеосообщение...")
    try:
        file_info = await bot.get_file(vn.file_id)
        save_path = os.path.join(UPLOADS_DIR, filename)
        await bot.download_file(file_info.file_path, save_path)
        log(f"Видеосообщение сохранено: {save_path}")

        prompt = (
            f"[Пользователь прислал кружочек/видеосообщение: {save_path}]\n"
            f"Пожалуйста, изучи это видео/звук и ответь пользователю."
        )
        await status_msg.edit_text("⚙️ Видеосообщение передано в Antigravity...")
        await process_prompt_and_respond(message, prompt, status_msg)
    except Exception as e:
        log(f"Ошибка обработки видеосообщения: {e}")
        await status_msg.edit_text(f"❌ Ошибка: {e}")

@dp.message(F.voice)
async def handle_voice_msg(message: Message, bot: Bot):
    if not await ensure_authorized(message):
        return

    status_msg = await message.answer("🎙 Слушаю голосовое...")
    try:
        file_io = await bot.download(message.voice.file_id)
        voice_bytes = file_io.read()
    except Exception as e:
        log(f"Ошибка загрузки аудио: {e}")
        await status_msg.edit_text(f"❌ Ошибка загрузки аудио: {e}")
        return

    text = await transcribe_voice(voice_bytes)
    if not text:
        await status_msg.edit_text("❌ Не удалось расшифровать голос. Попробуйте написать текстом.")
        return

    clean_text = text.strip().strip(".!?, ")

    # 1. Быстрый пинг / проверка связи
    if re.search(r"(?i)^(тут|ты тут|на связи|проверка|проверка связи)$", clean_text):
        await status_msg.delete()
        await message.answer("✅ <b>Да, я на связи!</b>\nСлушаю вас, готов к задачам.", parse_mode="HTML")
        return

    # 2. Запрос на прослушивание/показ заметок
    if is_notes_review_query(clean_text):
        await status_msg.delete()
        await send_notes_digest(message, bot)
        return

    # 2. Сохранение голосовой мысли/заметки
    note_match = re.match(r"(?i)^(?:заметка|запиши\s+мысль|мысль|запиши|напомни|в\s+заметки)[:\s]+(.*)$", text.strip())
    if note_match:
        note_text = note_match.group(1).strip()
        if note_text:
            saved = add_daily_note(note_text)
            await status_msg.edit_text(
                f"📌 <b>Мысль сохранена в копилку [{saved['time']}]:</b>\n\n"
                f"🗣 <i>«{html.escape(saved['text'])}»</i>\n\n"
                f"💡 Вечером напиши или скажи <b>«Заметки»</b> / <b>«Повтори мои мысли»</b>, и я зачитаю список голосом Харона!",
                parse_mode="HTML"
            )
            return

    await status_msg.edit_text(f"🗣 <i>«{html.escape(text)}»</i>\n\n⚙️ Передаю в Antigravity...", parse_mode="HTML")
    await process_prompt_and_respond(message, text, status_msg)

@dp.message(F.text)
async def handle_text_msg(message: Message):
    if not await ensure_authorized(message):
        return

    clean_text = message.text.strip()

    # 0. Быстрый пинг / проверка связи (мгновенный ответ 0.05 сек)
    if re.search(r"(?i)^(тут|ты тут|на связи|пинг|ping|проверка|проверка связи|\?|сурин)$", clean_text.strip(".!?, ")):
        addr, token = get_ls_credentials()
        is_alive = False
        try:
            port = int(addr.split(":")[-1])
            is_alive = verify_ls_port(port, token)
        except Exception:
            pass
        if is_alive:
            await message.answer("✅ <b>Да, я тут, на связи!</b>\nAntigravity работает в штатном режиме, слушаю вас.", parse_mode="HTML")
            return

    # 1. Запрос на показ/озвучку заметок
    if is_notes_review_query(clean_text):
        await send_notes_digest(message, message.bot)
        return

    # 2. Очистка заметок
    if re.search(r"(?i)^(/clear_notes|очисти\s+заметки|стереть\s+заметки|удалить\s+заметки)$", clean_text):
        clear_today_notes()
        await message.answer("🗑 <b>Заметки за сегодня очищены!</b>", parse_mode="HTML")
        return

    # 3. Сохранение текстовой заметки
    note_match = re.match(r"(?i)^(?:/note|заметка|мысль|запиши|напомни)[:\s]+(.*)$", clean_text)
    if note_match:
        note_text = note_match.group(1).strip()
        if note_text:
            saved = add_daily_note(note_text)
            await message.answer(
                f"📌 <b>Мысль сохранена в копилку [{saved['time']}]:</b>\n\n"
                f"<i>«{html.escape(saved['text'])}»</i>\n\n"
                f"💡 Вечером напиши или скажи <b>«Заметки»</b> / <b>«Продиктуй»</b>, и я озвучу сводку за день!",
                parse_mode="HTML"
            )
            return

    status_msg = await message.answer("⚙️ Отправляю в Antigravity...", reply_markup=build_live_status_keyboard())
    await process_prompt_and_respond(message, message.text, status_msg)

async def process_prompt_and_respond(message: Message, prompt: str, status_msg: Message):
    line_count = get_transcript_line_count()
    save_pending_task(message.chat.id, status_msg.message_id, prompt, line_count)
    ok = send_to_antigravity(prompt)
    if not ok:
        clear_pending_task()
        await status_msg.edit_text("❌ Ошибка отправки в Antigravity. Проверьте, активно ли приложение.")
        return

    async def update_status(text: str):
        try:
            await status_msg.edit_text(text, parse_mode="HTML", reply_markup=build_live_status_keyboard())
        except Exception:
            try:
                await status_msg.edit_text(text, reply_markup=build_live_status_keyboard())
            except Exception:
                pass

    await update_status("⏳ Antigravity выполняет задачу...")
    reply = await wait_for_agent_response(line_count, status_updater=update_status)

    if not reply:
        try:
            await status_msg.edit_text("⏳ Задача объемная, продолжаю отслеживать в фоне. Ответ придет сюда сразу по готовности...")
        except Exception:
            pass
        asyncio.create_task(background_wait_and_send(message, line_count, status_msg))
        return

    try:
        await status_msg.delete()
    except Exception:
        pass

    clear_pending_task()
    # Отправляем очищенное, красивое сообщение
    await send_clean_telegram_message(message, reply)

def enable_keep_awake():
    """Предотвращает переход Windows в спящий режим пока работает мост"""
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ES_AWAYMODE_REQUIRED = 0x00000040
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED)
        log("[*] Режим бодрствования Windows активирован (ПК не уснет)")
    except Exception as e:
        log(f"Не удалось включить режим бодрствования: {e}")

async def main():
    enable_keep_awake()
    asyncio.create_task(auto_approval_loop(None))
    
    while True:
        bot = None
        try:
            proxy_url = get_working_proxy()
            session = RobustAiohttpSession(proxy=proxy_url)
            bot = Bot(token=BOT_TOKEN, session=session)
            global active_bot
            active_bot = bot
            addr, _ = get_ls_credentials()
            proxy_info = f", Proxy: {proxy_url}" if proxy_url else ", Direct connection"
            try:
                commands = [
                    BotCommand(command="call", description="📞 Живой голосовой звонок (Gemini Live)"),
                    BotCommand(command="help", description="📖 Все команды и возможности"),
                    BotCommand(command="voice", description="🎙 Голосовые ответы (on/off)"),
                    BotCommand(command="model", description="🧠 Выбор модели (Argon / Flash / Auto)"),
                    BotCommand(command="screen", description="📸 Снимок экрана ПК"),
                    BotCommand(command="circle", description="🎥 Видео-кружочек экрана ПК"),
                    BotCommand(command="cache", description="⚡ Проверка памяти и токенов"),
                    BotCommand(command="new", description="✨ Начать новый чат в Antigravity"),
                    BotCommand(command="session", description="📌 Текущая сессия"),
                    BotCommand(command="shop", description="🛍 Магазин 3D-печати (Mini App)"),
                    BotCommand(command="notes", description="📝 Список заметок"),
                    BotCommand(command="clear_notes", description="🗑 Очистить заметки"),
                ]
                await bot.set_my_commands(commands)
                log("[*] Команды бота успешно зарегистрированы в меню Telegram!")
            except Exception as e_cmd:
                log(f"[!] Ошибка регистрации команд в меню: {e_cmd}")

            log(f"[*] Telegram мост подключен! Бот: @abertd_bot, LS: {addr}{proxy_info}")
            asyncio.create_task(resume_pending_task_if_any(bot))
            await dp.start_polling(bot, handle_signals=False, close_bot_session=True)
            log("[*] Сессия polling завершена. Перезапуск через 3 сек...")
            await asyncio.sleep(3)
        except (TelegramNetworkError, aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            log(f"[Связь] Сбой сети/Telegram ({type(e).__name__}: {e}). Повтор подключения через 5 сек...")
            await asyncio.sleep(5)
        except Exception as e:
            log(f"[Ошибка] Сбой моста ({type(e).__name__}: {e}). Перезапуск через 5 сек...")
            await asyncio.sleep(5)
        finally:
            if bot and bot.session:
                try:
                    await bot.session.close()
                except Exception:
                    pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        import traceback
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[CRASH] {type(e).__name__}: {e}\n")
                traceback.print_exc(file=f)
        except Exception:
            pass

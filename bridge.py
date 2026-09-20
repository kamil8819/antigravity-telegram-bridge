import os
import sys
import time
import json
import re
import html
import subprocess
import asyncio
import urllib.request
import urllib.error
import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message

# Загрузка переменных окружения из .env если доступен
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# === КОНФИГУРАЦИЯ ===
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_API_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
CONFIG_CONV_ID = os.getenv("ANTIGRAVITY_CONVERSATION_ID")
ALLOWED_CHAT_ID = os.getenv("ALLOWED_CHAT_ID")

APP_DATA_DIR = os.path.join(os.environ.get("USERPROFILE", ""), r".gemini\antigravity")
BRAIN_BASE_DIR = os.path.join(APP_DATA_DIR, "brain")

WORKSPACE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOADS_DIR = os.path.join(WORKSPACE_DIR, "telegram_uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(WORKSPACE_DIR, "config.json")
LOG_FILE = os.path.join(WORKSPACE_DIR, "bridge.log")

def log(msg: str):
    timestamp = time.strftime("[%Y-%m-%d %H:%M:%S]")
    line = f"{timestamp} {msg}\n"
    print(line, end="", flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass

allowed_chat_ids = set()
if ALLOWED_CHAT_ID:
    try:
        allowed_chat_ids.add(int(ALLOWED_CHAT_ID))
    except ValueError:
        pass

if os.path.exists(CONFIG_FILE):
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            allowed_chat_ids.update(cfg.get("allowed_chat_ids", []))
            if not BOT_TOKEN:
                BOT_TOKEN = cfg.get("bot_token")
            if not GROQ_API_KEY:
                GROQ_API_KEY = cfg.get("groq_api_key")
    except Exception:
        pass

def save_allowed_chat_ids():
    try:
        data = {"allowed_chat_ids": list(allowed_chat_ids)}
        if BOT_TOKEN: data["bot_token"] = BOT_TOKEN
        if GROQ_API_KEY: data["groq_api_key"] = GROQ_API_KEY
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"Ошибка сохранения config: {e}")

def get_active_conversation_id() -> str:
    """Определяет ID активного диалога Antigravity"""
    if CONFIG_CONV_ID:
        return CONFIG_CONV_ID
    
    # Ищем самый свежий диалог в папке brain
    if os.path.exists(BRAIN_BASE_DIR):
        conv_dirs = []
        for name in os.listdir(BRAIN_BASE_DIR):
            full_path = os.path.join(BRAIN_BASE_DIR, name)
            if os.path.isdir(full_path) and not name.startswith('.'):
                try:
                    mtime = os.path.getmtime(full_path)
                    conv_dirs.append((mtime, name))
                except Exception:
                    pass
        if conv_dirs:
            conv_dirs.sort(key=lambda x: x[0], reverse=True)
            return conv_dirs[0][1]
    return ""

def get_ls_credentials():
    """Автоматически определяет адрес и CSRF-токен активного Language Server"""
    try:
        cmd = 'Get-CimInstance Win32_Process | Where-Object { $_.Name -like "*language_server*" } | Select-Object ProcessId, CommandLine | ConvertTo-Json'
        res = subprocess.run(['powershell', '-NoProfile', '-Command', cmd], capture_output=True, text=True, timeout=5)
        if res.stdout.strip():
            data = json.loads(res.stdout)
            if isinstance(data, list):
                data = data[0]
            pid = data.get('ProcessId')
            cmdline = data.get('CommandLine', '')
            token_match = re.search(r'--csrf_token\s+([a-f0-9\-]+)', cmdline)
            token = token_match.group(1) if token_match else ""

            net_cmd = f'Get-NetTCPConnection -OwningProcess {pid} -State Listen | Select-Object -ExpandProperty LocalPort'
            net_res = subprocess.run(['powershell', '-NoProfile', '-Command', net_cmd], capture_output=True, text=True, timeout=5)
            ports = [p.strip() for p in net_res.stdout.split() if p.strip()]

            for port in sorted(ports, reverse=True):
                return f"localhost:{port}", token
    except Exception as e:
        log(f"Discovery error: {e}")
    return os.environ.get("ANTIGRAVITY_LS_ADDRESS", "localhost:54044"), os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "")

def format_telegram_response(text: str) -> str:
    """Форматирует ответ нейросети в аккуратный HTML для Telegram"""
    if not text:
        return ""

    code_blocks = []
    def extract_code_block(m):
        code = m.group(2).strip()
        escaped = html.escape(code)
        code_blocks.append(f"<pre><code>{escaped}</code></pre>")
        return f"___CODE_{len(code_blocks)-1}___"

    text = re.sub(r'```([a-zA-Z0-9_\-\+]*)\n?(.*?)```', extract_code_block, text, flags=re.DOTALL)

    inline_codes = []
    def extract_inline_code(m):
        code = m.group(1)
        escaped = html.escape(code)
        inline_codes.append(f"<code>{escaped}</code>")
        return f"___INLINE_{len(inline_codes)-1}___"

    text = re.sub(r'`([^`\n]+)`', extract_inline_code, text)
    text = html.escape(text)
    text = re.sub(r'&lt;/?kbd&gt;', '', text)
    text = re.sub(r'&lt;/?br\s*/?&gt;', '\n', text)
    text = re.sub(r'^#{1,6}\s*(.*?)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'^[ \t]*[-*_]{3,}[ \t]*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<!\w)\*([^\*\n]+?)\*(?!\w)', r'<i>\1</i>', text)
    text = re.sub(r'(?<!\w)_([^_\n]+?)_(?!\w)', r'<i>\1</i>', text)
    text = text.replace('*', '')
    text = re.sub(r'^[ \t]*[-]\s+', '• ', text, flags=re.MULTILINE)

    for i, b in enumerate(code_blocks):
        text = text.replace(f"___CODE_{i}___", b)
    for i, c in enumerate(inline_codes):
        text = text.replace(f"___INLINE_{i}___", c)

    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def strip_all_markdown(text: str) -> str:
    text = re.sub(r'```.*?```', '[Код]', text, flags=re.DOTALL)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'^#{1,6}\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[ \t]*[-*_]{3,}[ \t]*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('*', '')
    return text.strip()

async def send_clean_telegram_message(message: Message, text: str):
    formatted = format_telegram_response(text)
    chunks = [formatted[i:i+4000] for i in range(0, len(formatted), 4000)] if len(formatted) > 4000 else [formatted]
    for chunk in chunks:
        try:
            await message.answer(chunk, parse_mode="HTML")
        except Exception:
            clean = strip_all_markdown(text)
            plain_chunks = [clean[i:i+4000] for i in range(0, len(clean), 4000)] if len(clean) > 4000 else [clean]
            for pc in plain_chunks:
                await message.answer(pc)

def send_to_antigravity(prompt_text: str, conv_id: str) -> bool:
    """Отправляет сообщение напрямую в активный диалог Antigravity"""
    addr, token = get_ls_credentials()
    url = f"http://{addr}/exa.language_server_pb.LanguageServerService/SendUserCascadeMessage"
    headers = {
        "Content-Type": "application/json",
        "x-codeium-csrf-token": token,
        "Connect-Protocol-Version": "1"
    }
    payload = {
        "cascadeId": conv_id,
        "items": [
            {"text": prompt_text}
        ],
        "cascadeConfig": {
            "plannerConfig": {
                "supportsLatexRendering": True,
                "requestedModel": {
                    "model": "MODEL_PLACEHOLDER_M318"
                }
            }
        }
    }
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            log(f"Отправлено в Antigravity ({addr}): {prompt_text[:60]}... Статус: {resp.status}")
            return resp.status == 200
    except Exception as e:
        log(f"Ошибка отправки в Antigravity ({addr}): {e}")
        return False

async def transcribe_voice(file_bytes: bytes, filename: str = "voice.ogg") -> str:
    """Расшифровка через Groq Whisper API (0.3 сек)"""
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
                        log(f"Расшифровано Whisper: {text}")
                        return text
        except Exception as e:
            log(f"Groq Whisper error: {e}")
    return ""

def get_transcript_path(conv_id: str) -> tuple[str, str]:
    brain_dir = os.path.join(BRAIN_BASE_DIR, conv_id)
    t_path = os.path.join(brain_dir, r".system_generated\logs\transcript.jsonl")
    tf_path = os.path.join(brain_dir, r".system_generated\logs\transcript_full.jsonl")
    return t_path, tf_path

def get_transcript_line_count(conv_id: str) -> int:
    t_path, tf_path = get_transcript_path(conv_id)
    path = tf_path if os.path.exists(tf_path) else t_path
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return sum(1 for _ in f)
    return 0

async def wait_for_agent_response(conv_id: str, initial_line_count: int, timeout_sec: int = 400, status_updater=None) -> str:
    t_path, tf_path = get_transcript_path(conv_id)
    path = tf_path if os.path.exists(tf_path) else t_path
    start_time = time.time()
    last_reported_action = ""

    while time.time() - start_time < timeout_sec:
        await asyncio.sleep(2)
        if not os.path.exists(path):
            continue

        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()

            if len(lines) <= initial_line_count:
                continue

            new_lines = lines[initial_line_count:]
            last_line = new_lines[-1]
            last_data = json.loads(last_line)

            if last_data.get("type") == "PLANNER_RESPONSE":
                tool_calls = last_data.get("tool_calls")
                content = last_data.get("content")
                if tool_calls and status_updater:
                    first_tool = tool_calls[0]
                    t_name = first_tool.get("name", "tool")
                    args = first_tool.get("args", {})
                    summary = args.get("toolSummary") or args.get("toolAction") or t_name
                    clean_summary = str(summary).strip('"\'')
                    if clean_summary != last_reported_action:
                        last_reported_action = clean_summary
                        await status_updater(f"🛠 Действие: {clean_summary}...")

                if content and not tool_calls:
                    log(f"Получен ответ от Antigravity ({len(content)} симв.)")
                    return content
        except Exception:
            pass

    return "Задача обработана агентом (проверьте результат в IDE)."

dp = Dispatcher()

async def ensure_authorized(message: Message) -> bool:
    global allowed_chat_ids
    if not allowed_chat_ids:
        allowed_chat_ids.add(message.chat.id)
        save_allowed_chat_ids()
        await message.answer(f"✅ Вы авторизованы как владелец! Чат ID: <code>{message.chat.id}</code>", parse_mode="HTML")
        return True

    if message.chat.id not in allowed_chat_ids:
        await message.answer("⛔ Доступ запрещен. Бот привязан к конкретному владельцу.")
        return False
    return True

@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    if await ensure_authorized(message):
        conv_id = get_active_conversation_id()
        await message.answer(
            f"👋 <b>Привет!</b> Я — прямой мост к <b>Google Antigravity</b>.\n\n"
            f"🔗 Активный диалог: <code>{conv_id[:16]}...</code>\n\n"
            "✨ <b>Поддерживается:</b>\n"
            "• Текстовые сообщения\n"
            "• Голосовые команды (Whisper)\n"
            "• Фотографии и скриншоты (компьютерное зрение)\n"
            "• Файлы и документы",
            parse_mode="HTML"
        )

@dp.message(F.photo)
async def handle_photo_msg(message: Message, bot: Bot):
    if not await ensure_authorized(message):
        return

    conv_id = get_active_conversation_id()
    if not conv_id:
        await message.answer("❌ Нет активного диалога Antigravity. Запустите приложение.")
        return

    status_msg = await message.answer("📸 Загружаю фото...")
    try:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        
        user_uploaded_dir = os.path.join(BRAIN_BASE_DIR, conv_id, ".user_uploaded")
        os.makedirs(user_uploaded_dir, exist_ok=True)
        
        filename = f"photo_{int(time.time() * 1000)}.jpg"
        save_path = os.path.join(user_uploaded_dir, filename)
        
        await bot.download_file(file_info.file_path, save_path)
        log(f"Фото сохранено: {save_path}")
        
        caption = message.caption or "Посмотри это фото и дай ответ."
        prompt = (
            f"[Пользователь прислал изображение: {save_path}]\n"
            f"Запрос пользователя: {caption}\n"
            f"Пожалуйста, изучи это изображение через view_file и ответь пользователю."
        )
        await status_msg.edit_text("⚙️ Передаю изображение в Antigravity...")
        await process_prompt_and_respond(message, prompt, conv_id, status_msg)
    except Exception as e:
        log(f"Ошибка обработки фото: {e}")
        await status_msg.edit_text(f"❌ Ошибка: {e}")

@dp.message(F.document)
async def handle_document_msg(message: Message, bot: Bot):
    if not await ensure_authorized(message):
        return

    conv_id = get_active_conversation_id()
    if not conv_id:
        await message.answer("❌ Нет активного диалога Antigravity.")
        return

    status_msg = await message.answer("📄 Загружаю файл...")
    try:
        doc = message.document
        file_info = await bot.get_file(doc.file_id)
        filename = doc.file_name or f"file_{int(time.time() * 1000)}"
        save_path = os.path.join(UPLOADS_DIR, filename)

        await bot.download_file(file_info.file_path, save_path)
        log(f"Файл сохранен: {save_path}")

        caption = message.caption or f"Пользователь прислал файл {filename}."
        prompt = (
            f"[Пользователь прикрепил файл: {save_path}]\n"
            f"Комментарий: {caption}\n"
            f"Пожалуйста, открой этот файл через view_file, изучи его и ответь пользователю."
        )
        await status_msg.edit_text("⚙️ Файл передан в Antigravity...")
        await process_prompt_and_respond(message, prompt, conv_id, status_msg)
    except Exception as e:
        log(f"Ошибка обработки файла: {e}")
        await status_msg.edit_text(f"❌ Ошибка: {e}")

@dp.message(F.voice)
async def handle_voice_msg(message: Message, bot: Bot):
    if not await ensure_authorized(message):
        return

    conv_id = get_active_conversation_id()
    if not conv_id:
        await message.answer("❌ Нет активного диалога Antigravity.")
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

    await status_msg.edit_text(f"🗣 <i>«{html.escape(text)}»</i>\n\n⚙️ Передаю в Antigravity...", parse_mode="HTML")
    await process_prompt_and_respond(message, text, conv_id, status_msg)

@dp.message(F.text)
async def handle_text_msg(message: Message):
    if not await ensure_authorized(message):
        return

    conv_id = get_active_conversation_id()
    if not conv_id:
        await message.answer("❌ Нет активного диалога Antigravity. Запустите приложение.")
        return

    status_msg = await message.answer("⚙️ Отправляю в Antigravity...")
    await process_prompt_and_respond(message, message.text, conv_id, status_msg)

async def process_prompt_and_respond(message: Message, prompt: str, conv_id: str, status_msg: Message):
    line_count = get_transcript_line_count(conv_id)
    ok = send_to_antigravity(prompt, conv_id)
    if not ok:
        await status_msg.edit_text("❌ Ошибка отправки в Antigravity. Проверьте, запущена ли среда.")
        return

    async def update_status(text: str):
        try:
            await status_msg.edit_text(text)
        except Exception:
            pass

    await update_status("⏳ Antigravity выполняет задачу...")
    reply = await wait_for_agent_response(conv_id, line_count, status_updater=update_status)

    try:
        await status_msg.delete()
    except Exception:
        pass

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
    if not BOT_TOKEN:
        print("ОШИБКА: Токен Telegram-бота не задан! Укажите TELEGRAM_BOT_TOKEN в файле .env или config.json.", file=sys.stderr)
        sys.exit(1)

    bot = Bot(token=BOT_TOKEN)
    addr, _ = get_ls_credentials()
    conv_id = get_active_conversation_id()
    log(f"[*] Telegram мост запущен! LS: {addr}, Диалог: {conv_id}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

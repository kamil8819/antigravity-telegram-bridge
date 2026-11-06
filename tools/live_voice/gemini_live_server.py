#!/usr/bin/env python3
"""
Antigravity Live Voice Server (Telegram Mini App Full-Duplex Bridge)
Bidirectional streaming of audio (PCM 16kHz in, PCM 24kHz out) between
Telegram Mini App on mobile and Google Gemini 3.8 Live API.
Routes Google Live API through local proxy (127.0.0.1:12334) to prevent TSPU packet drops.
"""

import os
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import json
import time
import asyncio
import subprocess
import re
import urllib.request
import urllib.parse
import ssl
import certifi
import warnings
import aiohttp
from aiohttp import web
import requests
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
import google.genai.live as live_module
from websockets_proxy import Proxy, proxy_connect

warnings.filterwarnings("ignore", category=RuntimeWarning, module="duckduckgo_search")
try:
    from duckduckgo_search import DDGS
except ImportError:
    DDGS = None

# Monkeypatch google.genai to route WebSocket via Sweden proxy
LOCAL_PROXY_URL = os.environ.get("PROXY_URL") or os.environ.get("HTTPS_PROXY") or os.environ.get("ALL_PROXY") or "http://127.0.0.1:12334"

def setup_live_proxy():
    try:
        proxy = Proxy.from_url(LOCAL_PROXY_URL)
        def patched_ws_connect(uri, **kwargs):
            if "additional_headers" in kwargs:
                kwargs["extra_headers"] = kwargs.pop("additional_headers")
            kwargs["ping_interval"] = 15
            kwargs["ping_timeout"] = 25
            return proxy_connect(uri, proxy=proxy, **kwargs)
        live_module.ws_connect = patched_ws_connect
        print(f"[Proxy] Gemini Live WebSocket routed through {LOCAL_PROXY_URL}")
    except Exception as e:
        print(f"[Proxy] Warning: Failed to set up WebSocket proxy: {e}")

PORT = 8765
HOST = "127.0.0.1"
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(ROOT_DIR, "bridge_config.json")
TUNNEL_URL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tunnel_url.txt")

_cached_ls_addr = None
_cached_ls_token = None

def verify_ls_port(port: int, token: str) -> bool:
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

def get_ls_credentials():
    """Надежное автообнаружение порта и CSRF токена Antigravity с верификацией"""
    global _cached_ls_addr, _cached_ls_token
    if _cached_ls_addr and _cached_ls_token:
        try:
            port = int(_cached_ls_addr.split(":")[-1])
            if verify_ls_port(port, _cached_ls_token):
                return _cached_ls_addr, _cached_ls_token
        except Exception:
            pass

    try:
        cmd = 'powershell "Get-WmiObject Win32_Process -Filter \\"name like \'%language_server%\'\\" | Select-Object ProcessId, CommandLine | ConvertTo-Json"'
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=8)
        if res.returncode == 0 and res.stdout.strip():
            raw = json.loads(res.stdout)
            procs = raw if isinstance(raw, list) else [raw]
            for p in procs:
                cline = p.get("CommandLine", "")
                pid = p.get("ProcessId")
                m = re.search(r'--csrf_token\s+([a-f0-9-]+)', cline)
                if m:
                    token = m.group(1)
                    net_cmd = f'powershell "Get-NetTCPConnection -OwningProcess {pid} -State Listen | Select-Object LocalPort | ConvertTo-Json"'
                    nres = subprocess.run(net_cmd, shell=True, capture_output=True, text=True, timeout=5)
                    if nres.returncode == 0 and nres.stdout.strip():
                        nraw = json.loads(nres.stdout)
                        conns = nraw if isinstance(nraw, list) else [nraw]
                        for c in conns:
                            port = c.get("LocalPort")
                            if port and int(port) > 1024:
                                if verify_ls_port(int(port), token):
                                    _cached_ls_addr = f"127.0.0.1:{port}"
                                    _cached_ls_token = token
                                    return _cached_ls_addr, _cached_ls_token
    except Exception as e:
        print(f"[LS Auth] Discovery error: {e}")

    return None, None

def send_to_antigravity(prompt_text: str) -> bool:
    """Отправка задачи в активную сессию Antigravity"""
    global _cached_ls_addr, _cached_ls_token

    brain_dir = os.path.expanduser("~/.gemini/antigravity/brain")
    cid = None
    if os.path.exists(brain_dir):
        subs = [os.path.join(brain_dir, d) for d in os.listdir(brain_dir) if os.path.isdir(os.path.join(brain_dir, d))]
        subs.sort(key=os.path.getmtime, reverse=True)
        if subs:
            cid = os.path.basename(subs[0])

    if not cid:
        return False

    payload = {
        "cascadeId": cid,
        "items": [{"text": prompt_text}],
        "cascadeConfig": {
            "plannerConfig": {
                "supportsLatexRendering": True
            }
        }
    }

    for attempt in range(2):
        addr, token = get_ls_credentials()
        if not addr or not token:
            time.sleep(0.5)
            continue
        url = f"http://{addr}/exa.language_server_pb.LanguageServerService/SendUserCascadeMessage"
        headers = {
            "Content-Type": "application/json",
            "x-codeium-csrf-token": token,
            "Connect-Protocol-Version": "1"
        }
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                print(f"[Antigravity] Task dispatched: {prompt_text[:60]}... status={resp.status}")
                return resp.status == 200
        except Exception as e:
            print(f"[Antigravity] Dispatch attempt {attempt+1} failed: {e}")
            _cached_ls_addr = None
            _cached_ls_token = None
            time.sleep(0.5)

    return False

# Web Search & Internet Tools
def _sync_web_search(query: str) -> str:
    if not query:
        return "Пустой поисковый запрос."
    has_cyrillic = any('\u0400' <= c <= '\u04FF' for c in query)
    region = 'ru-ru' if has_cyrillic else 'wt-wt'
    if DDGS:
        try:
            with DDGS(proxy=LOCAL_PROXY_URL, timeout=8) as ddgs:
                results = list(ddgs.text(query, region=region, max_results=4))
                if not results and region == 'ru-ru':
                    results = list(ddgs.text(query, region='wt-wt', max_results=4))
                if results:
                    lines = []
                    for idx, r in enumerate(results, 1):
                        t = r.get("title", "").strip()
                        b = r.get("body", "").strip()
                        lines.append(f"{idx}. {t}: {b}")
                    return "\n".join(lines)
        except Exception as e:
            print(f"[Search DDG Error]: {e}")

    try:
        encoded = urllib.parse.quote(query)
        url = f"https://html.duckduckgo.com/html/?q={encoded}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = requests.get(url, headers=headers, proxies={"http": LOCAL_PROXY_URL, "https": LOCAL_PROXY_URL}, timeout=8)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            snippets = []
            for res in soup.find_all("a", class_="result__snippet")[:4]:
                t = res.get_text(strip=True)
                if t:
                    snippets.append(t)
            if snippets:
                return "\n".join(snippets)
    except Exception as e2:
        print(f"[Search Fallback Error]: {e2}")

    return "По данному запросу не удалось получить свежие результаты поиска."

async def execute_web_search(query: str) -> str:
    print(f"[Live Tool] Searching web: {query}")
    return await asyncio.to_thread(_sync_web_search, query)

async def execute_browse_page(url: str) -> str:
    if not url or not url.startswith(("http://", "https://")):
        return "Ошибка: URL должен начинаться с http:// или https://"
    print(f"[Live Tool] Browsing page: {url}")
    try:
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        conn = aiohttp.TCPConnector(ssl=ssl_ctx)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
        async with aiohttp.ClientSession(connector=conn) as session:
            async with session.get(url, proxy=LOCAL_PROXY_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return f"Сайт вернул код ответа {resp.status}."
                html = await resp.text(errors="ignore")
                soup = BeautifulSoup(html, "html.parser")
                for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
                    tag.decompose()
                text = " ".join(soup.stripped_strings)
                if len(text) > 3000:
                    text = text[:3000] + "... [текст сокращен]"
                return text if text else "На странице нет текстового содержимого."
    except Exception as e:
        print(f"[Live Tool Browse Error]: {e}")
        return f"Не удалось прочитать веб-страницу: {e}"

LIVE_TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="search_web",
                description="Поиск информации в интернете в реальном времени: актуальные курсы валют и криптовалют, свежие новости, погода, факты, события, справка.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "query": types.Schema(
                            type=types.Type.STRING,
                            description="Поисковый запрос на русском или английском языке"
                        )
                    },
                    required=["query"]
                )
            ),
            types.FunctionDeclaration(
                name="browse_page",
                description="Чтение содержимого веб-страницы по указанному URL для подробного изучения статьи или сайта.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "url": types.Schema(
                            type=types.Type.STRING,
                            description="Полный URL адрес страницы (начиная с http:// или https://)"
                        )
                    },
                    required=["url"]
                )
            ),
            types.FunctionDeclaration(
                name="dispatch_to_antigravity",
                description="Передача задачи на написание кода, запуск скриптов, исправление багов или работу с файлами в Antigravity на компьютере.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "task_description": types.Schema(
                            type=types.Type.STRING,
                            description="Четкая формулировка задачи для выполнения на ПК"
                        )
                    },
                    required=["task_description"]
                )
            )
        ]
    )
]

# Web Application Handlers
async def handle_index(request):
    return web.FileResponse(os.path.join(STATIC_DIR, "index.html"))

async def handle_style(request):
    return web.FileResponse(os.path.join(STATIC_DIR, "style.css"))

async def handle_app_js(request):
    return web.FileResponse(os.path.join(STATIC_DIR, "app.js"))

async def handle_dispatch(request):
    try:
        data = await request.json()
        prompt = data.get("prompt", "").strip()
        if not prompt:
            return web.json_response({"ok": False, "error": "Empty prompt"})
        success = send_to_antigravity(prompt)
        return web.json_response({"ok": success})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})

async def handle_ws_live(request):
    # Enable heartbeat to keep mobile connections alive across NAT/proxies
    ws = web.WebSocketResponse(heartbeat=10.0, receive_timeout=120.0)
    await ws.prepare(request)
    print(f"[WS] Client connected from {request.remote}")

    client = genai.Client()
    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Aoede")
            )
        ),
        tools=LIVE_TOOLS,
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        system_instruction=types.Content(
            parts=[types.Part(text="""Ты голосовой помощник Antigravity, работающий в реальном времени прямо на смартфоне пользователя в Telegram.
Ты говоришь живым, естественным, приятным голосом на русском языке без пауз и канцелярита.
Отвечай кратко и емко (1-3 предложения), как в живом телефонном разговоре.

У тебя есть инструменты для работы с интернетом и системой:
1. 'search_web' — когда пользователь спрашивает о текущих событиях, курсах валют или криптовалют, погоде, фактах, новостях или просит найти что-то в интернете, ВСЕГДА вызывай search_web. Получив результат поиска, кратко озвучь суть пользователю.
2. 'browse_page' — когда пользователь просит посмотреть конкретный сайт или ссылку, используй browse_page.
3. 'dispatch_to_antigravity' — когда пользователь просит написать код, запустить скрипт, исправить баг или создать проект на компьютере в Antigravity, вызывай dispatch_to_antigravity и голосом подтверди пользователю, что задача передана на ПК.""")]
        )
    )

    try:
        async with client.aio.live.connect(model="gemini-3.8-live", config=config) as gemini_session:
            print("[Gemini Live] Session connected successfully via Sweden proxy")

            async def client_to_gemini():
                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            # Raw PCM 16kHz from browser mic
                            pcm_data = msg.data
                            await gemini_session.send_realtime_input(
                                audio=types.Blob(data=pcm_data, mime_type="audio/pcm;rate=16000")
                            )
                        elif msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if "text" in data:
                                await gemini_session.send_realtime_input(text=data["text"])
                            elif data.get("type") == "ping":
                                if not ws.closed:
                                    await ws.send_json({"type": "pong"})
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                            break
                except Exception as e:
                    print(f"[Client->Gemini] Stream error: {e}")

            async def gemini_to_client():
                try:
                    while not ws.closed:
                        async for response in gemini_session.receive():
                            # Handle tool calls (web search, browse page, dispatch to Antigravity)
                            if response.tool_call:
                                f_responses = []
                                for fc in response.tool_call.function_calls:
                                    func_name = fc.name
                                    func_args = fc.args or {}
                                    print(f"[Live Server Tool Call] {func_name}({func_args})")

                                    result_text = ""
                                    if func_name == "search_web":
                                        query = func_args.get("query", "").strip()
                                        if not ws.closed:
                                            await ws.send_json({"type": "tool_active", "tool": "search", "detail": query})
                                        result_text = await execute_web_search(query)
                                    elif func_name == "browse_page":
                                        url = func_args.get("url", "").strip()
                                        if not ws.closed:
                                            await ws.send_json({"type": "tool_active", "tool": "browse", "detail": url})
                                        result_text = await execute_browse_page(url)
                                    elif func_name == "dispatch_to_antigravity":
                                        task_desc = func_args.get("task_description", "").strip()
                                        if not ws.closed:
                                            await ws.send_json({"type": "task_dispatched", "task": task_desc})
                                        ok = send_to_antigravity(task_desc)
                                        result_text = "Задача успешно отправлена в Antigravity на ПК." if ok else "Ошибка отправки в Antigravity."
                                    else:
                                        result_text = f"Неизвестный инструмент: {func_name}"

                                    f_responses.append(
                                        types.FunctionResponse(
                                            name=fc.name,
                                            id=fc.id,
                                            response={"result": result_text}
                                        )
                                    )

                                if f_responses:
                                    print(f"[Live Server Tool] Replying with {len(f_responses)} tool responses")
                                    await gemini_session.send_tool_response(function_responses=f_responses)

                            sc = response.server_content
                            if sc:
                                # 1. Output audio streaming
                                if sc.model_turn:
                                    for part in sc.model_turn.parts:
                                        if part.inline_data and part.inline_data.data:
                                            if not ws.closed:
                                                await ws.send_bytes(part.inline_data.data)

                                # 2. Transcriptions
                                if sc.input_transcription and sc.input_transcription.text:
                                    user_text = sc.input_transcription.text.strip()
                                    if not ws.closed:
                                        await ws.send_json({
                                            "type": "transcript",
                                            "role": "user",
                                            "text": user_text
                                        })

                                if sc.output_transcription and sc.output_transcription.text:
                                    ai_text = sc.output_transcription.text.strip()
                                    if not ws.closed:
                                        await ws.send_json({
                                            "type": "transcript",
                                            "role": "assistant",
                                            "text": ai_text
                                        })
                                        await ws.send_json({
                                            "type": "state",
                                            "state": "speaking"
                                        })

                                # 3. Interruption handling
                                if sc.interrupted:
                                    if not ws.closed:
                                        await ws.send_json({"type": "interrupted"})

                                # 4. Turn completion (finish single turn, prepare for next turn)
                                if sc.turn_complete:
                                    if not ws.closed:
                                        await ws.send_json({
                                            "type": "state",
                                            "state": "listening"
                                        })
                                    break
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    print(f"[Gemini->Client] Stream error: {e}")

            client_task = asyncio.create_task(client_to_gemini())
            gemini_task = asyncio.create_task(gemini_to_client())
            done, pending = await asyncio.wait(
                [client_task, gemini_task],
                return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()

    except Exception as e:
        print(f"[WS Live Error]: {e}")
    finally:
        print(f"[WS] Client disconnected")
        if not ws.closed:
            await ws.close()

    return ws

def setup_tunnel_and_get_url(port: int = 8765):
    """Настройка туннеля для Telegram Mini App (через SSH VPS или локальный cloudflared)"""
    ssh_host = os.environ.get("SSH_TUNNEL_HOST")
    if ssh_host:
        ssh_cmd = [
            "ssh", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=no",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            "-N", "-R", f"{port}:127.0.0.1:{port}",
            ssh_host
        ]
        print(f"[Tunnel] Starting reverse SSH tunnel to {ssh_host}...")
        proc = subprocess.Popen(ssh_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        try:
            url_cmd = ['ssh', '-o', 'ConnectTimeout=5', ssh_host, "grep -oE 'https://[a-zA-Z0-9-]+\\.trycloudflare\\.com' /var/log/cloudflared-live.log | tail -n 1"]
            res = subprocess.run(url_cmd, capture_output=True, text=True, timeout=8)
            url = res.stdout.strip()
            if url:
                print(f"[Tunnel] [OK] Cloudflare Mini App URL: {url}")
                with open(TUNNEL_URL_FILE, "w", encoding="utf-8") as f:
                    f.write(url)
                if os.path.exists(CONFIG_PATH):
                    try:
                        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                            cfg = json.load(f)
                        cfg["live_webapp_url"] = url
                        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                            json.dump(cfg, f, ensure_ascii=False, indent=2)
                        print("[Config] Updated live_webapp_url in config")
                    except Exception as e:
                        print(f"[Config] Error: {e}")
                return proc, url
        except Exception as e:
            print(f"[Tunnel] Failed to retrieve remote tunnel URL: {e}")
        return proc, None

    # Fallback to local cloudflared tunnel
    cf_bin = "cloudflared"
    import shutil
    for candidate in [os.path.join(ROOT_DIR, "cloudflared.exe"), "cloudflared.exe", "cloudflared"]:
        if os.path.exists(candidate) or shutil.which(candidate):
            cf_bin = candidate
            break

    try:
        cmd = [cf_bin, "tunnel", "--url", f"http://127.0.0.1:{port}"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="ignore")
        url = None
        start_t = time.time()
        while time.time() - start_t < 15:
            line = proc.stderr.readline()
            if not line:
                time.sleep(0.1)
                continue
            m = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', line)
            if m:
                url = m.group(0)
                break
        if url:
            print(f"[Tunnel] [OK] Cloudflare Quick Tunnel URL: {url}")
            with open(TUNNEL_URL_FILE, "w", encoding="utf-8") as f:
                f.write(url)
            if os.path.exists(CONFIG_PATH):
                try:
                    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                    cfg["live_webapp_url"] = url
                    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                        json.dump(cfg, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass
            return proc, url
    except Exception as e:
        print(f"[Tunnel] Local cloudflared failed: {e}")

    return None, None

async def main_async():
    # 1. Setup proxy monkeypatching
    setup_live_proxy()

    # 2. Setup Web App
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/style.css", handle_style)
    app.router.add_get("/app.js", handle_app_js)
    app.router.add_post("/api/dispatch", handle_dispatch)
    app.router.add_get("/ws/live", handle_ws_live)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()
    print(f"[LIVE] Web server running on http://{HOST}:{PORT}")

    tunnel_proc, tunnel_url = setup_tunnel_and_get_url(PORT)

    print("=======================================================")
    print("  [LIVE VOICE] Antigravity Full-Duplex Server Ready")
    print(f"  Local Address:  http://{HOST}:{PORT}")
    if tunnel_url:
        print(f"  Mini App URL:   {tunnel_url}")
    print("=======================================================")

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        if tunnel_proc:
            tunnel_proc.terminate()
        await runner.cleanup()

def main():
    try:
        asyncio.run(main_async())
    except (KeyboardInterrupt, SystemExit):
        pass

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Antigravity Live Voice Server (Telegram Mini App Full-Duplex Bridge)
Bidirectional streaming of audio (PCM 16kHz in, PCM 24kHz out) between
Telegram Mini App on mobile and Google Gemini 3.8 Live API.
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
import aiohttp
from aiohttp import web
from google import genai
from google.genai import types
import google.genai.live as live_module

# Optional proxy configuration for environments with network filters
LOCAL_PROXY_URL = os.environ.get("HTTPS_PROXY") or os.environ.get("ALL_PROXY") or os.environ.get("HTTP_PROXY")

def setup_live_proxy():
    if not LOCAL_PROXY_URL:
        return
    try:
        from websockets_proxy import Proxy, proxy_connect
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
        print(f"[Proxy] Warning: Could not configure WebSocket proxy: {e}")

PORT = int(os.environ.get("LIVE_VOICE_PORT", 8765))
HOST = os.environ.get("LIVE_VOICE_HOST", "127.0.0.1")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(ROOT_DIR, "config.json")
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
    """Автообнаружение порта и CSRF токена Language Server Antigravity"""
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

# Web Handlers
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
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        system_instruction=types.Content(
            parts=[types.Part(text="""Ты голосовой помощник Antigravity, работающий в реальном времени прямо на смартфоне пользователя в Telegram.
Ты говоришь живым, естественным, приятным голосом на русском языке без пауз и канцелярита.
Отвечай кратко и емко (1-3 предложения), как в живом телефонном разговоре.
Если пользователь просит написать код, запустить скрипт, проверить ошибки или выполнить действие в Antigravity на ПК — подтверди голосом (например: 'Принято, передаю задачу в Antigravity на компьютере') и четко сформулируй суть действия.""")]
        )
    )

    try:
        async with client.aio.live.connect(model="gemini-3.8-live", config=config) as gemini_session:
            print("[Gemini Live] Session connected successfully")

            async def client_to_gemini():
                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.BINARY:
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
                            sc = response.server_content
                            if sc:
                                # 1. Audio streaming chunks
                                if sc.model_turn:
                                    for part in sc.model_turn.parts:
                                        if part.inline_data and part.inline_data.data:
                                            if not ws.closed:
                                                await ws.send_bytes(part.inline_data.data)

                                # 2. Transcripts
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

                                # 4. Turn completion (continuous multi-turn)
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
    """Создание защищенного туннеля для Telegram Mini App"""
    # 1. Если настроен внешний SSH сервер (например VPS)
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
                return proc, url
        except Exception as e:
            print(f"[Tunnel] Could not read remote tunnel URL: {e}")
        return proc, None

    # 2. Локальный Cloudflare Quick Tunnel (cloudflared)
    cf_bin = "cloudflared"
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
            return proc, url
    except Exception as e:
        print(f"[Tunnel] Local cloudflared failed: {e}")

    return None, None

async def main_async():
    setup_live_proxy()

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

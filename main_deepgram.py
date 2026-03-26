import os
import json
import base64
import asyncio
import audioop
import datetime
import threading
import urllib.request
from fastapi import FastAPI, WebSocket, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse
from dotenv import load_dotenv
from google import genai
from google.genai import types
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse, Connect
from concurrent.futures import ThreadPoolExecutor
from deepgram import DeepgramClient
from deepgram.core.events import EventType

load_dotenv()

# Logger helper 
_log_file = open("debug_deepgram.log", "a", encoding="ascii", errors="replace")
def log_info(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] {msg}"
    _log_file.write(line + "\n")
    _log_file.flush()
    print(line)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Config
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY") 
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
YOUR_PHONE_NUMBER = os.getenv("YOUR_PHONE_NUMBER")

_audio_executor = ThreadPoolExecutor(max_workers=4)

def _decode_mulaw_sync(mulaw_bytes: bytes) -> bytes:
    return audioop.ulaw2lin(mulaw_bytes, 2)

def _downsample_sync(pcm_24k: bytes):
    pcm_8k, _ = audioop.ratecv(pcm_24k, 2, 1, 24000, 8000, None)
    return pcm_8k

def _encode_mulaw_sync(pcm_8k: bytes) -> bytes:
    return audioop.lin2ulaw(pcm_8k, 2)

# --- NGROK DETECTION ---
def get_ngrok_url():
    try:
        resp = urllib.request.urlopen("http://localhost:4040/api/tunnels", timeout=3)
        data = json.loads(resp.read().decode())
        for tunnel in data.get("tunnels", []):
            url = tunnel.get("public_url", "")
            if url.startswith("https://"): return url
    except: pass
    return os.getenv("NGROK_URL", "")

NGROK_URL = None

# --- SSE TRANSCRIPTS ---
class TranscriptController:
    def __init__(self): self.queues = []
    async def add_message(self, role, text):
        for q in self.queues: await q.put({"role": role, "text": text})
    def subscribe(self):
        q = asyncio.Queue()
        self.queues.append(q)
        return q
    def unsubscribe(self, q):
        if q in self.queues: self.queues.remove(q)

transcript_manager = TranscriptController()

@app.get("/events")
async def events(request: Request):
    async def msg_gen():
        q = transcript_manager.subscribe()
        try:
            while not await request.is_disconnected():
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                    yield {"data": json.dumps(msg)}
                except asyncio.TimeoutError: yield {"comment": "keepalive"}
        finally: transcript_manager.unsubscribe(q)
    return EventSourceResponse(msg_gen())

# --- UI DASHBOARD ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)

@app.get("/", response_class=HTMLResponse)
async def get_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        return f.read()

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# --- TWILIO ROUTES ---
@app.post("/make-call")
async def make_call():
    global NGROK_URL
    NGROK_URL = get_ngrok_url()
    client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    call = client.calls.create(
        to=YOUR_PHONE_NUMBER,
        from_=TWILIO_PHONE_NUMBER,
        url=f"{NGROK_URL}/twiml"
    )
    return {"status": "calling", "sid": call.sid}

@app.api_route("/twiml", methods=["GET", "POST"])
async def twiml_handler():
    response = VoiceResponse()
    connect = Connect()
    ws_url = f"wss://{NGROK_URL.replace('https://','').replace('http://','')}/media-stream"
    connect.stream(url=ws_url)
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")

# --- NATIVE WEB TEST UI ---
@app.get("/web", response_class=HTMLResponse)
async def web_ui():
    with open(os.path.join(STATIC_DIR, "web.html"), "r", encoding="utf-8") as f:
        return f.read()

@app.websocket("/web-stream")
async def web_stream(websocket: WebSocket):
    await websocket.accept()
    log_info("[WEB WS] Connected")
    
    loop = asyncio.get_event_loop()
    dg_client = DeepgramClient(api_key=DEEPGRAM_API_KEY)
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    
    gemini_config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=types.Content(parts=[types.Part.from_text("You are a short, conversational phone assistant. Response maximum 15 words.")])
    )

    try:
        async with gemini_client.aio.live.connect(model="gemini-2.5-flash-native-audio-preview-12-2025", config=gemini_config) as gemini_session:
            options = {
                "model": "nova-2-phonecall",
                "language": "en-US",
                "encoding": "linear16",
                "sample_rate": 16000, # Browser capture rate
                "eot_threshold": 0.7,
                "eot_timeout_ms": 5000,
            }
            with dg_client.listen.v2.connect(**options) as dg_connection:
                ready = threading.Event()
                dg_connection.on(EventType.OPEN, lambda *a, **k: (log_info(f"[DEBUG] Open Event Args: {a} {k}"), ready.set()))
                
                current_transcript = []
                def on_message(*args, **kwargs):
                    result = args[0] if args else None
                    if not result: return
                    transcript = getattr(result, "transcript", None)
                    event_type = getattr(result, "event", None)
                    if transcript: current_transcript.append(transcript)
                    if event_type == "EndOfTurn":
                        text = " ".join(current_transcript).strip()
                        if text: 
                            log_info(f"[WEB USER] {text}")
                            asyncio.run_coroutine_threadsafe(transcript_manager.add_message("user", text), loop)
                            asyncio.run_coroutine_threadsafe(gemini_session.send_realtime_input(text=text), loop)
                            asyncio.run_coroutine_threadsafe(websocket.send_text(json.dumps({"role": "user", "text": text})), loop)
                        current_transcript.clear()

                dg_connection.on(EventType.MESSAGE, on_message)
                dg_connection.start_listening()

                async def browser_to_dg():
                    try:
                        # Wait for Deepgram OPEN in an async-friendly way
                        while not ready.is_set():
                            await asyncio.sleep(0.1)
                        while True:
                            data = await websocket.receive_bytes()
                            dg_connection.send_media(data)
                    except Exception as e: log_info(f"B->D Error: {e}")

                async def gemini_to_browser():
                    try:
                        async for response in gemini_session.receive():
                            sc = response.server_content
                            if sc and sc.model_turn:
                                for part in sc.model_turn.parts:
                                    if part.inline_data:
                                        await websocket.send_bytes(part.inline_data.data)
                            if sc and sc.output_transcription:
                                txt = sc.output_transcription.text
                                if txt: 
                                    log_info(f"[WEB AI] {txt}")
                                    await transcript_manager.add_message("ai", txt)
                                    await websocket.send_text(json.dumps({"role": "ai", "text": txt}))
                    except Exception as e: log_info(f"G->W Error: {e}")
                
                await asyncio.gather(browser_to_dg(), gemini_to_browser())
    except Exception as e: log_info(f"Web Global Error: {e}")
    finally: await websocket.close()

# --- CORE ARCHITECTURE: DEEPGRAM + GEMINI ---
@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    log_info("[WS] Connected")
    
    stream_sid = None
    session_active = True
    loop = asyncio.get_event_loop()
    
    # Clients
    dg_client = DeepgramClient(api_key=DEEPGRAM_API_KEY)
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    
    gemini_config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=types.Content(parts=[types.Part.from_text("You are a short, conversational phone assistant. Response maximum 15 words.")])
    )

    try:
        async with gemini_client.aio.live.connect(model="gemini-2.5-flash-native-audio-preview-12-2025", config=gemini_config) as gemini_session:
            log_info("[GEMINI] Session Open")
            
            # Use Deepgram listen.v2 for Turn-based streaming
            options = {
                "model": "nova-2-phonecall",
                "language": "en-US",
                "encoding": "linear16",
                "sample_rate": 8000,
                "eot_threshold": 0.7,
                "eot_timeout_ms": 5000, # Wait 5s for silence
            }
            
            with dg_client.listen.v2.connect(**options) as dg_connection:
                log_info("[DEEPGRAM] Session Open (V2 Turn-Based)")
                
                ready = threading.Event()
                dg_connection.on(EventType.OPEN, lambda *a, **k: (log_info(f"[DEBUG] Twilio Open Event Args: {a} {k}"), ready.set()))
                
                # State tracking for the current turn
                current_transcript = []
 
                def on_message(*args, **kwargs):
                    result = args[0] if args else None
                    if not result: return
                    event_type = getattr(result, "event", None)
                    
                    transcript = getattr(result, "transcript", None)
                    if transcript:
                        current_transcript.append(transcript)
                    
                    if event_type == "EndOfTurn":
                        full_sentence = " ".join(current_transcript).strip()
                        if full_sentence:
                            log_info(f"[USER] {full_sentence}")
                            asyncio.run_coroutine_threadsafe(transcript_manager.add_message("user", full_sentence), loop)
                            # Feed final turn text to Gemini
                            asyncio.run_coroutine_threadsafe(gemini_session.send_realtime_input(text=full_sentence), loop)
                        current_transcript.clear()

                dg_connection.on(EventType.MESSAGE, on_message)
                dg_connection.start_listening()

                async def twilio_to_dg():
                    nonlocal stream_sid, session_active
                    try:
                        while not ready.is_set():
                            await asyncio.sleep(0.1)
                        async for message in websocket.iter_text():
                            data = json.loads(message)
                            if data['event'] == 'start': stream_sid = data['start']['streamSid']
                            if data['event'] == 'media':
                                mulaw = base64.b64decode(data['media']['payload'])
                                pcm_8k = await loop.run_in_executor(_audio_executor, _decode_mulaw_sync, mulaw)
                                dg_connection.send_media(pcm_8k)
                    except Exception as e: log_info(f"T->D Error: {e}")
                    finally: session_active = False

                async def gemini_to_twilio():
                    nonlocal session_active
                    try:
                        async for response in gemini_session.receive():
                            sc = response.server_content
                            if sc and sc.model_turn:
                                for part in sc.model_turn.parts:
                                    if part.inline_data and stream_sid:
                                        pcm_24k = part.inline_data.data
                                        pcm_8k = await loop.run_in_executor(_audio_executor, _downsample_sync, pcm_24k)
                                        mulaw = await loop.run_in_executor(_audio_executor, _encode_mulaw_sync, pcm_8k)
                                        payload = base64.b64encode(mulaw).decode()
                                        await websocket.send_json({"event":"media", "streamSid": stream_sid, "media": {"payload": payload}})
                            if sc and sc.output_transcription:
                                txt = sc.output_transcription.text
                                if txt: 
                                    log_info(f"[AI] {txt}")
                                    await transcript_manager.add_message("ai", txt)
                    except Exception as e: log_info(f"G->T Error: {e}")

                await asyncio.gather(twilio_to_dg(), gemini_to_twilio())

    except Exception as e: log_info(f"Global Error: {e}")
    finally:
        await websocket.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

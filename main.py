import os
import sys
import json
import base64
import asyncio
import audioop
import traceback
import datetime
import urllib.request
from fastapi import FastAPI, WebSocket, Request, BackgroundTasks
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

load_dotenv()

_log_file = open("debug.log", "a", encoding="ascii", errors="replace")
def log_info(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] {msg}"
    _log_file.write(line + "\n")
    _log_file.flush()
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except Exception:
        pass

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
YOUR_PHONE_NUMBER = os.getenv("YOUR_PHONE_NUMBER")

# Thread pool for CPU-bound audio ops (keeps async loop unblocked)
_audio_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="audio")
CALLER_SPEECH_RMS_THRESHOLD = 250
CALLER_SPEECH_FRAMES_TO_START = 3
CALLER_SILENCE_FRAMES_TO_END = 10

def _resample_sync(pcm_8k: bytes, ratecv_state):
    """Run in thread pool — keeps event loop free."""
    pcm_16k, new_state = audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, ratecv_state)
    return pcm_16k, new_state

def _decode_mulaw_sync(mulaw_bytes: bytes) -> bytes:
    return audioop.ulaw2lin(mulaw_bytes, 2)

def _encode_mulaw_sync(pcm_8k: bytes) -> bytes:
    return audioop.lin2ulaw(pcm_8k, 2)

def _downsample_sync(pcm_24k: bytes):
    pcm_8k, _ = audioop.ratecv(pcm_24k, 2, 1, 24000, 8000, None)
    return pcm_8k

def _rms_sync(pcm_8k: bytes) -> int:
    return audioop.rms(pcm_8k, 2)

def get_ngrok_url():
    try:
        resp = urllib.request.urlopen("http://localhost:4040/api/tunnels", timeout=3)
        data = json.loads(resp.read().decode())
        for tunnel in data.get("tunnels", []):
            url = tunnel.get("public_url", "")
            if url.startswith("https://"):
                return url
        if data.get("tunnels"):
            return data["tunnels"][0]["public_url"]
    except Exception as e:
        log_info(f"[NGROK] Could not auto-detect ngrok URL: {e}")
    env_url = os.getenv("NGROK_URL", "")
    if env_url:
        log_info(f"[NGROK] Falling back to .env URL: {env_url}")
    return env_url

NGROK_URL = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)


# -- SSE Transcripts System --
class TranscriptController:
    def __init__(self):
        self.queues = []

    async def add_message(self, role: str, text: str):
        message = {"role": role, "text": text}
        for q in self.queues:
            await q.put(message)

    def subscribe(self):
        q = asyncio.Queue()
        self.queues.append(q)
        return q

    def unsubscribe(self, q):
        if q in self.queues:
            self.queues.remove(q)

transcript_manager = TranscriptController()

@app.get("/events")
async def events(request: Request):
    async def event_generator():
        q = transcript_manager.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield {"data": json.dumps(msg)}
                except asyncio.TimeoutError:
                    yield {"comment": "keepalive"}
        except (asyncio.CancelledError, Exception):
            pass
        finally:
            transcript_manager.unsubscribe(q)

    return EventSourceResponse(event_generator())


@app.get("/", response_class=HTMLResponse)
async def get_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        return f.read()

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.post("/make-call")
async def make_call():
    global NGROK_URL
    NGROK_URL = get_ngrok_url()
    if not NGROK_URL:
        return JSONResponse(
            {"status": "error", "message": "ngrok is not running. Start ngrok first!"},
            status_code=503,
        )
    log_info(f"[CALL] Using ngrok URL: {NGROK_URL}")

    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    call = twilio_client.calls.create(
        to=YOUR_PHONE_NUMBER,
        from_=TWILIO_PHONE_NUMBER,
        url=f"{NGROK_URL}/twiml",
        status_callback=f"{NGROK_URL}/call-status",
        status_callback_event=["initiated", "ringing", "answered", "completed"],
    )
    log_info(f"[CALL] Call initiated! SID: {call.sid}")
    return JSONResponse({"status": "calling", "call_sid": call.sid})


@app.api_route("/twiml", methods=["GET", "POST"])
async def twiml_handler(request: Request):
    response = VoiceResponse()
    connect = Connect()
    ws_url = f"wss://{NGROK_URL.replace('https://', '').replace('http://', '')}/media-stream"
    log_info(f"[TWIML] Stream URL = {ws_url}")
    connect.stream(url=ws_url)
    response.append(connect)
    response.say("The AI agent has disconnected. Goodbye.")
    return HTMLResponse(content=str(response), media_type="application/xml")


@app.api_route("/call-status", methods=["GET", "POST"])
async def call_status(request: Request):
    body = await request.body()
    decoded = body.decode("utf-8")
    status = "unknown"
    for param in decoded.split("&"):
        if param.startswith("CallStatus="):
            status = param.split("=")[1]
            break
    log_info(f"[STATUS] Call status: {status}")
    if status == "completed":
        await transcript_manager.add_message("system", "Call ended")
    return HTMLResponse(content="OK")


# -- Twilio Media Stream WebSocket --
@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    log_info("[WS] Twilio media stream WebSocket ACCEPTED")
    await transcript_manager.add_message("system", "Call connected")

    stream_sid = None
    session_active = True
    loop = asyncio.get_event_loop()
    ai_playing = False
    drop_current_model_audio = False
    caller_speech_frames = 0
    caller_silence_frames = 0
    latest_user_audio_time = None
    user_activity_active = False

    client = genai.Client(api_key=GEMINI_API_KEY)
    model = "gemini-2.5-flash-native-audio-preview-12-2025"

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        output_audio_transcription=types.AudioTranscriptionConfig(),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=True,
            ),
            activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
            turn_coverage=types.TurnCoverage.TURN_INCLUDES_ONLY_ACTIVITY,
        ),
        system_instruction=types.Content(
            parts=[
                types.Part.from_text(
                    text=(
                        "You are a friendly and professional AI phone agent on a live phone call. "
                        "CRITICAL: Keep ALL responses extremely brief - maximum 1-2 short sentences. "
                        "Be conversational and natural like a human phone agent. Never give long explanations. "
                        "You are multilingual - respond in whatever language the caller uses (English, Hindi, or Marathi). "
                        "Start with a brief warm greeting."
                    )
                )
            ]
        ),
    )

    try:
        log_info("[GEMINI] Connecting to Gemini Live API...")
        async with client.aio.live.connect(model=model, config=config) as session:
            log_info("[GEMINI] Session ESTABLISHED")

            async def clear_twilio_audio(reason: str):
                if not stream_sid or not session_active:
                    return
                try:
                    await websocket.send_json({
                        "event": "clear",
                        "streamSid": stream_sid,
                    })
                    log_info(f"[TWILIO] Cleared buffered audio ({reason})")
                except Exception as e:
                    log_info(f"[TWILIO] Clear failed ({reason}): {e}")

            try:
                await session.send_realtime_input(
                    text="The call just connected. Please greet the caller."
                )
                log_info("[GEMINI] Greeting prompt sent")
            except Exception as e:
                log_info(f"[GEMINI] Greeting failed: {e}")

            # ----------------------------------------------------------------
            # Twilio -> Gemini: stream audio with minimal buffering
            # ----------------------------------------------------------------
            async def twilio_to_gemini():
                nonlocal stream_sid, session_active, ai_playing
                nonlocal drop_current_model_audio, caller_speech_frames
                nonlocal caller_silence_frames, latest_user_audio_time
                nonlocal user_activity_active
                chunks = 0
                sends = 0
                ratecv_state = None
                
                try:
                    async for message in websocket.iter_text():
                        if not session_active:
                            break

                        data = json.loads(message)
                        event = data.get("event")

                        if event == "start":
                            stream_sid = data["start"]["streamSid"]
                            log_info(f"[STREAM] Started: {stream_sid}")

                        elif event == "media":
                            mulaw_bytes = base64.b64decode(data["media"]["payload"])

                            # Offload CPU work to thread pool
                            pcm_8k = await loop.run_in_executor(
                                _audio_executor, _decode_mulaw_sync, mulaw_bytes
                            )
                            rms = await loop.run_in_executor(
                                _audio_executor, _rms_sync, pcm_8k
                            )

                            if rms >= CALLER_SPEECH_RMS_THRESHOLD:
                                latest_user_audio_time = loop.time()
                                caller_speech_frames += 1
                                caller_silence_frames = 0
                            else:
                                caller_speech_frames = 0
                                caller_silence_frames += 1

                            if (
                                not user_activity_active
                                and caller_speech_frames >= CALLER_SPEECH_FRAMES_TO_START
                            ):
                                user_activity_active = True
                                await session.send_realtime_input(
                                    activity_start=types.ActivityStart()
                                )
                                log_info(f"[VAD] Activity start (rms={rms})")

                            if (
                                ai_playing
                                and not drop_current_model_audio
                                and user_activity_active
                            ):
                                drop_current_model_audio = True
                                ai_playing = False
                                await clear_twilio_audio("caller barge-in")
                                log_info(
                                    f"[INTERRUPT] Caller speech detected while AI was talking (rms={rms})"
                                )

                            pcm_16k, ratecv_state = await loop.run_in_executor(
                                _audio_executor, _resample_sync, pcm_8k, ratecv_state
                            )

                            chunks += 1

                            await session.send_realtime_input(
                                audio=types.Blob(data=pcm_16k, mime_type="audio/pcm;rate=16000")
                            )
                            sends += 1

                            if (
                                user_activity_active
                                and caller_silence_frames >= CALLER_SILENCE_FRAMES_TO_END
                            ):
                                user_activity_active = False
                                await session.send_realtime_input(
                                    activity_end=types.ActivityEnd()
                                )
                                log_info("[VAD] Activity end")

                        elif event == "stop":
                            log_info("[STREAM] Twilio stop event")
                            break

                except Exception as e:
                    log_info(f"[TWILIO->GEMINI] Error: {e}")
                finally:
                    session_active = False
                    log_info(f"[TWILIO->GEMINI] Done. {chunks} chunks, {sends} sends.")

            # ----------------------------------------------------------------
            # Gemini -> Twilio: forward audio as fast as it arrives
            # ----------------------------------------------------------------
            async def gemini_to_twilio():
                nonlocal session_active, ai_playing, drop_current_model_audio
                nonlocal caller_speech_frames, caller_silence_frames
                nonlocal latest_user_audio_time, user_activity_active
                audio_out = 0
                first_audio_time = None
                try:
                    while session_active:
                        async for response in session.receive():
                            if not session_active:
                                break

                            sc = response.server_content
                            if not sc:
                                continue

                            if sc.input_transcription and sc.input_transcription.text:
                                text = sc.input_transcription.text.strip()
                                if text:
                                    log_info(f"[USER] {text}")
                                    await transcript_manager.add_message("user", text)

                            if sc.interrupted:
                                ai_playing = False
                                drop_current_model_audio = True
                                await clear_twilio_audio("Gemini interruption")
                                log_info("[GEMINI] Current response interrupted")

                            if sc.model_turn:
                                for part in sc.model_turn.parts:
                                    if part.inline_data and part.inline_data.data and stream_sid:
                                        if drop_current_model_audio:
                                            continue

                                        if first_audio_time is None:
                                            first_audio_time = loop.time()
                                            ai_playing = True
                                            if latest_user_audio_time:
                                                latency = first_audio_time - latest_user_audio_time
                                                log_info(f"[LATENCY] Speech-to-response: {latency:.2f}s")

                                        pcm_24k = part.inline_data.data

                                        # Offload downsampling to thread pool
                                        pcm_8k = await loop.run_in_executor(
                                            _audio_executor, _downsample_sync, pcm_24k
                                        )
                                        mulaw_8k = await loop.run_in_executor(
                                            _audio_executor, _encode_mulaw_sync, pcm_8k
                                        )

                                        payload = base64.b64encode(mulaw_8k).decode("utf-8")
                                        try:
                                            await websocket.send_json({
                                                "event": "media",
                                                "streamSid": stream_sid,
                                                "media": {"payload": payload},
                                            })
                                            ai_playing = True
                                            audio_out += 1
                                        except Exception:
                                            session_active = False
                                            break

                            if sc.output_transcription and sc.output_transcription.text:
                                text = sc.output_transcription.text.strip()
                                if text:
                                    log_info(f"[AI] {text}")
                                    await transcript_manager.add_message("ai", text)

                            if sc.turn_complete:
                                log_info("[GEMINI] Turn complete")
                                ai_playing = False
                                drop_current_model_audio = False
                                caller_speech_frames = 0
                                caller_silence_frames = 0
                                user_activity_active = False
                                first_audio_time = None
                                break

                except Exception as e:
                    log_info(f"[GEMINI->TWILIO] Error: {e}")
                finally:
                    session_active = False
                    log_info(f"[GEMINI->TWILIO] Done. {audio_out} chunks sent.")

            log_info("[RELAY] Starting bidirectional relay...")
            await asyncio.gather(
                twilio_to_gemini(),
                gemini_to_twilio(),
                return_exceptions=True,
            )

    except Exception as e:
        log_info(f"[SESSION] Error: {e}")
        traceback.print_exc()
    finally:
        log_info("[SESSION] Closed")
        await transcript_manager.add_message("system", "Call disconnected")
        try:
            await websocket.close()
        except:
            pass

@app.on_event("startup")
async def startup_event():
    global NGROK_URL
    NGROK_URL = get_ngrok_url()
    if NGROK_URL:
        log_info(f"[STARTUP] ngrok URL detected: {NGROK_URL}")
    else:
        log_info("[STARTUP] WARNING: ngrok not detected! Start ngrok before making calls.")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

import os
import sys
import json
import base64
import asyncio
import audioop
import traceback
import datetime
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

load_dotenv()

# Simple log function that works on Windows without encoding issues
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

# -- Config --
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
YOUR_PHONE_NUMBER = os.getenv("YOUR_PHONE_NUMBER")
NGROK_URL = os.getenv("NGROK_URL")

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
async def events():
    """SSE endpoint for dashboard transcript."""
    async def event_generator():
        q = transcript_manager.subscribe()
        try:
            while True:
                msg = await q.get()
                yield {"data": json.dumps(msg)}
        except asyncio.CancelledError:
            transcript_manager.unsubscribe(q)

    return EventSourceResponse(event_generator())


# -- Static / Dashboard --
@app.get("/", response_class=HTMLResponse)
async def get_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        return f.read()

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# -- Initiate outbound call --
@app.post("/make-call")
async def make_call():
    """Trigger an outbound call from Twilio to your personal number."""
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


# -- TwiML: tell Twilio to stream audio to our WebSocket --
@app.api_route("/twiml", methods=["GET", "POST"])
async def twiml_handler(request: Request):
    """Return TwiML that connects the call to a media stream."""
    response = VoiceResponse()

    # Immediately connect to the media stream (no pause/say - reduces latency)
    connect = Connect()
    ws_url = f"wss://{NGROK_URL.replace('https://', '').replace('http://', '')}/media-stream"
    log_info(f"[TWIML] Stream URL = {ws_url}")
    connect.stream(url=ws_url)
    response.append(connect)

    # Fallback if the media stream disconnects
    response.say("The AI agent has disconnected. Goodbye.")

    return HTMLResponse(content=str(response), media_type="application/xml")


# -- Call status callback --
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

    client = genai.Client(api_key=GEMINI_API_KEY)
    model = "gemini-2.5-flash-native-audio-preview-12-2025"

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        output_audio_transcription=types.AudioTranscriptionConfig(),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        system_instruction=types.Content(
            parts=[
                types.Part.from_text(
                    text=(
                        "You are a friendly and professional AI phone agent. "
                        "You are speaking to a person on a phone call. "
                        "Be conversational, polite, and helpful. Keep responses concise and short. "
                        "You are fully bilingual/multilingual and can fluently speak and understand English, Hindi, and Marathi. "
                        "If the user speaks Hindi, reply in Hindi. If the user speaks Marathi, reply in Marathi. "
                        "Start by greeting the user warmly and ask how you can assist them today."
                    )
                )
            ]
        ),
    )

    try:
        log_info("[GEMINI] Connecting to Gemini Live API...")
        async with client.aio.live.connect(model=model, config=config) as session:
            log_info("[GEMINI] Session ESTABLISHED")

            # Trigger Gemini to greet (uses realtime input for lowest latency)
            try:
                await session.send_realtime_input(
                    text="The call just connected. Please greet the caller."
                )
                log_info("[GEMINI] Greeting prompt sent")
            except Exception as e:
                log_info(f"[GEMINI] Greeting failed: {e}")

            # Audio queue for batched sending to Gemini
            audio_queue = asyncio.Queue()

            # -- Task: Drain audio queue and send batched to Gemini --
            async def audio_sender():
                """Batches audio chunks and sends to Gemini every ~100ms."""
                while session_active:
                    # Wait for at least one chunk
                    try:
                        first_chunk = await asyncio.wait_for(audio_queue.get(), timeout=0.1)
                    except asyncio.TimeoutError:
                        continue

                    # Collect all available chunks (drain queue)
                    chunks = [first_chunk]
                    while not audio_queue.empty():
                        try:
                            chunks.append(audio_queue.get_nowait())
                        except asyncio.QueueEmpty:
                            break

                    # Concatenate all PCM chunks into one blob
                    combined = b"".join(chunks)
                    try:
                        await session.send_realtime_input(
                            audio=types.Blob(data=combined, mime_type="audio/pcm;rate=16000")
                        )
                    except Exception as e:
                        log_info(f"[AUDIO_SENDER] Error: {e}")
                        break

            # -- Task: Twilio -> Gemini (user audio) --
            async def twilio_to_gemini():
                nonlocal stream_sid, session_active
                chunks = 0
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
                            # Decode mulaw -> PCM 8kHz -> PCM 16kHz
                            mulaw_bytes = base64.b64decode(data["media"]["payload"])
                            pcm_8k = audioop.ulaw2lin(mulaw_bytes, 2)
                            pcm_16k, _ = audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, None)

                            # Put in queue (non-blocking) for batched sending
                            audio_queue.put_nowait(pcm_16k)
                            chunks += 1

                        elif event == "stop":
                            log_info("[STREAM] Twilio stop event")
                            break

                except Exception as e:
                    log_info(f"[TWILIO->GEMINI] Error: {e}")
                finally:
                    session_active = False
                    log_info(f"[TWILIO->GEMINI] Done. {chunks} chunks queued.")

            # -- Task: Gemini -> Twilio (AI audio) --
            async def gemini_to_twilio():
                nonlocal session_active
                audio_out = 0
                try:
                    while session_active:
                        async for response in session.receive():
                            if not session_active:
                                break

                            sc = response.server_content
                            if not sc:
                                continue

                            # Forward audio to Twilio
                            if sc.model_turn:
                                for part in sc.model_turn.parts:
                                    if part.inline_data and part.inline_data.data and stream_sid:
                                        pcm_24k = part.inline_data.data
                                        pcm_8k, _ = audioop.ratecv(pcm_24k, 2, 1, 24000, 8000, None)
                                        mulaw_8k = audioop.lin2ulaw(pcm_8k, 2)

                                        payload = base64.b64encode(mulaw_8k).decode("utf-8")
                                        try:
                                            await websocket.send_json({
                                                "event": "media",
                                                "streamSid": stream_sid,
                                                "media": {"payload": payload},
                                            })
                                            audio_out += 1
                                        except Exception:
                                            session_active = False
                                            break

                            # AI speech transcription -> dashboard
                            if sc.output_transcription and sc.output_transcription.text:
                                text = sc.output_transcription.text.strip()
                                if text:
                                    log_info(f"[AI] {text}")
                                    await transcript_manager.add_message("ai", text)

                            # User speech transcription -> dashboard
                            if sc.input_transcription and sc.input_transcription.text:
                                text = sc.input_transcription.text.strip()
                                if text:
                                    log_info(f"[USER] {text}")
                                    await transcript_manager.add_message("user", text)

                            # Turn complete - loop will restart receive()
                            if sc.turn_complete:
                                log_info("[GEMINI] Turn complete")
                                break  # break inner for, while True restarts

                except Exception as e:
                    log_info(f"[GEMINI->TWILIO] Error: {e}")
                finally:
                    session_active = False
                    log_info(f"[GEMINI->TWILIO] Done. {audio_out} chunks sent.")

            log_info("[RELAY] Starting bidirectional relay...")
            await asyncio.gather(
                twilio_to_gemini(),
                audio_sender(),
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

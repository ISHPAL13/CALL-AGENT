import os
import json
import base64
import asyncio
from fastapi import FastAPI, WebSocket, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

app = FastAPI()

# Get the absolute path to the static directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)

# Main page
@app.get("/", response_class=HTMLResponse)
async def get_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        return f.read()

# Serve other static files (js, css)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("DEBUG: Client connected via WebSocket")
    
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        await websocket.close(code=1008, reason="GEMINI_API_KEY not set")
        return

    client = genai.Client(api_key=api_key)
    model = "gemini-2.5-flash-native-audio-preview-12-2025"
    
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=types.Content(parts=[types.Part.from_text(text="You are a helpful AI assistant. You are speaking to a user directly through their browser. Be polite and concise. IMMEDIATELY start by saying 'Hello! I can hear you clearly now. How can I help you today?' in a friendly voice.")]),
    )

    try:
        async with client.aio.live.connect(model=model, config=config) as session:
            print("DEBUG: Gemini session established")

            async def receive_from_browser():
                try:
                    count = 0
                    while True:
                        message = await websocket.receive_bytes()
                        count += 1
                        if count % 50 == 0:
                            print(f"DEBUG: Received {count} audio chunks from browser")
                        
                        await session.send_realtime_input(
                            audio=types.Blob(data=message, mime_type="audio/pcm;rate=16000")
                        )
                except Exception as e:
                    print(f"DEBUG: Browser receive error: {e}")

            async def send_to_browser():
                try:
                    async for response in session.receive():
                        if response.server_content and response.server_content.model_turn:
                            for part in response.server_content.model_turn.parts:
                                if part.inline_data:
                                    # print(f"DEBUG: Sending {len(part.inline_data.data)} bytes to browser")
                                    await websocket.send_bytes(part.inline_data.data)
                        
                        if response.server_content and response.server_content.interrupted:
                            # print("DEBUG: Gemini interrupted")
                            await websocket.send_json({"event": "interrupted"})
                            
                except Exception as e:
                    print(f"DEBUG: Gemini send error: {e}")

            # Run loops
            await asyncio.gather(receive_from_browser(), send_to_browser())

    except Exception as e:
        print(f"DEBUG: General session error: {e}")
    finally:
        print("DEBUG: Session closed")
        try:
            await websocket.close()
        except:
            pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

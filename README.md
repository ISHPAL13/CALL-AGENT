# Twilio AI Call Agent

A real-time AI phone agent powered by **Google Gemini Live API** and **Twilio**. The agent can make outbound calls, have natural conversations in English, Hindi, and Marathi, and display a live transcript on a web dashboard.

## Features

- **Real-time bidirectional audio** - Full-duplex conversation via Twilio Media Streams + Gemini Live API
- **Multilingual** - Supports English, Hindi, and Marathi with automatic language detection
- **Live transcript** - Web dashboard shows both user and AI speech in real-time via SSE
- **Low latency** - Optimized audio pipeline with batched PCM streaming

## Setup

1. **Install dependencies:**
   ```bash
   python -m venv venv
   venv\Scripts\activate  # Windows
   pip install -r requirements.txt
   ```

2. **Configure `.env`:**
   ```
   GEMINI_API_KEY=your_gemini_api_key
   TWILIO_ACCOUNT_SID=your_twilio_sid
   TWILIO_AUTH_TOKEN=your_twilio_auth_token
   TWILIO_PHONE_NUMBER=+1xxxxxxxxxx
   YOUR_PHONE_NUMBER=+91xxxxxxxxxx
   NGROK_URL=https://your-ngrok-url.ngrok-free.dev
   ```

3. **Start ngrok:**
   ```bash
   ngrok http 8000
   ```
   Update `NGROK_URL` in `.env` with the ngrok URL.

4. **Run the server:**
   ```bash
   venv\Scripts\python.exe main.py
   ```

5. **Open dashboard:** Navigate to `http://localhost:8000` and click "Make Call".

## Architecture

```
Phone <-> Twilio <-> ngrok <-> FastAPI WebSocket <-> Gemini Live API
                                    |
                              Web Dashboard (SSE)
```

## Tech Stack

- **Backend:** FastAPI + Uvicorn
- **AI:** Google Gemini 2.5 Flash Native Audio (Live API)
- **Telephony:** Twilio Programmable Voice + Media Streams
- **Audio:** mulaw <-> PCM conversion via audioop
- **Frontend:** Vanilla HTML/JS with SSE for live transcription
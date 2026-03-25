let audioContext;
let ws;
let processor;
let inputSource;
let isListening = false;
let audioQueue = [];
let nextStartTime = 0;

const micBtn = document.getElementById('mic-btn');
const statusDot = document.getElementById('dot');
const statusText = document.getElementById('status-text');
const transcript = document.getElementById('transcript');

async function initUI() {
    micBtn.addEventListener('click', toggleConnection);
}

async function toggleConnection() {
    if (!isListening) {
        startConnection();
    } else {
        stopConnection();
    }
}

async function startConnection() {
    try {
        // Create context at native rate for better stability
        audioContext = new (window.AudioContext || window.webkitAudioContext)();
        const sampleRate = audioContext.sampleRate;
        console.log(`DEBUG: Native sample rate: ${sampleRate}`);
        
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        inputSource = audioContext.createMediaStreamSource(stream);
        
        // 4096 samples at 48kHz is ~85ms
        processor = audioContext.createScriptProcessor(4096, 1, 1);
        
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        ws = new WebSocket(`${protocol}//${window.location.host}/ws`);
        ws.binaryType = 'arraybuffer';

        ws.onopen = () => {
            statusDot.classList.add('active');
            statusText.innerText = 'Connected';
            micBtn.classList.add('listening');
            transcript.innerText = 'Listening...';
            isListening = true;
            
            processor.onaudioprocess = (e) => {
                if (!isListening || ws.readyState !== WebSocket.OPEN) return;
                const inputData = e.inputBuffer.getChannelData(0);
                
                // Simple downsampling to 16kHz
                const resampledData = resampleTo16k(inputData, sampleRate);
                
                // Convert Float32 to Int16 PCM
                const pcmData = new Int16Array(resampledData.length);
                for (let i = 0; i < resampledData.length; i++) {
                    const s = Math.max(-1, Math.min(1, resampledData[i]));
                    pcmData[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
                }
                ws.send(pcmData.buffer);
            };

            inputSource.connect(processor);
            processor.connect(audioContext.destination);
        };

        ws.onmessage = async (e) => {
            if (typeof e.data === 'string') {
                const data = JSON.parse(e.data);
                if (data.event === 'interrupted') {
                    stopPlayback();
                }
                return;
            }
            console.log(`DEBUG: Received ${e.data.byteLength} bytes from server`);
            if (audioContext.state === 'suspended') {
                await audioContext.resume();
            }
            playAudioChunk(e.data);
        };

        ws.onclose = () => stopConnection();
        ws.onerror = (err) => console.error('WS Error:', err);

    } catch (err) {
        console.error('Failed to start microphone:', err);
        alert('Could not access microphone.');
    }
}

// Basic linear resampling to 16kHz
function resampleTo16k(inputData, originalSampleRate) {
    if (originalSampleRate === 16000) return inputData;
    
    const ratio = originalSampleRate / 16000;
    const newLength = Math.round(inputData.length / ratio);
    const result = new Float32Array(newLength);
    
    for (let i = 0; i < newLength; i++) {
        const index = Math.floor(i * ratio);
        result[i] = inputData[index];
    }
    return result;
}

function playAudioChunk(arrayBuffer) {
    if (!audioContext) return;
    
    const int16Array = new Int16Array(arrayBuffer);
    const float32Array = new Float32Array(int16Array.length);
    for (let i = 0; i < int16Array.length; i++) {
        float32Array[i] = int16Array[i] / 32768;
    }

    const buffer = audioContext.createBuffer(1, float32Array.length, 24000);
    buffer.getChannelData(0).set(float32Array);

    const source = audioContext.createBufferSource();
    source.buffer = buffer;
    source.connect(audioContext.destination);

    const startTime = Math.max(audioContext.currentTime, nextStartTime);
    source.start(startTime);
    nextStartTime = startTime + buffer.duration;
    
    audioQueue.push(source);
}

function stopPlayback() {
    audioQueue.forEach(s => {
        try { s.stop(); } catch(e) {}
    });
    audioQueue = [];
    nextStartTime = 0;
}

function stopConnection() {
    isListening = false;
    if (ws) ws.close();
    if (processor) processor.disconnect();
    if (inputSource) inputSource.disconnect();
    
    statusDot.classList.remove('active');
    statusText.innerText = 'Disconnected';
    micBtn.classList.remove('listening');
    transcript.innerText = 'Click to start conversation';
    stopPlayback();
}

window.addEventListener('DOMContentLoaded', initUI);

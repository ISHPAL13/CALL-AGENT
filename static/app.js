const callBtn = document.getElementById('call-btn');
const statusDot = document.getElementById('dot');
const statusText = document.getElementById('status-text');
const transcriptArea = document.getElementById('transcript-area');
const transcriptStatus = document.getElementById('transcript-status');

let callState = 'idle'; // idle | calling | connected
let eventSource = null;

// Connect to SSE for transcripts
function connectSSE() {
    if (eventSource) return;
    
    eventSource = new EventSource("/events");
    transcriptStatus.textContent = "Listening to events...";
    transcriptStatus.style.color = "var(--success)";
    
    eventSource.onmessage = (event) => {
        const data = JSON.parse(event.data);
        addTranscriptMessage(data.role, data.text);
        
        if (data.role === 'system') {
            if (data.text.includes("Call connected")) {
                callState = 'connected';
                updateUI();
            } else if (data.text.includes("Call ended")) {
                callState = 'idle';
                updateUI();
            }
        }
    };
    
    eventSource.onerror = () => {
        transcriptStatus.textContent = "Disconnected";
        transcriptStatus.style.color = "var(--danger)";
        eventSource.close();
        eventSource = null;
        
        // Reconnect after 3 seconds
        setTimeout(connectSSE, 3000);
    };
}

// Start SSE connection immediately
connectSSE();

function addTranscriptMessage(role, text) {
    // If it's an AI message, check if we should append to the last one or create new
    // We'll just create a new bubble for simplicity and clear presentation
    
    if (transcriptArea.children.length === 1 && transcriptArea.children[0].textContent.includes("Waiting for call")) {
        transcriptArea.innerHTML = '';
    }

    const msgDiv = document.createElement('div');
    msgDiv.className = `message msg-${role}`;
    
    if (role === 'ai') {
        msgDiv.textContent = `🤖 ${text}`;
    } else {
        msgDiv.textContent = text;
    }
    
    transcriptArea.appendChild(msgDiv);
    transcriptArea.scrollTop = transcriptArea.scrollHeight;
}

callBtn.addEventListener('click', async () => {
    if (callState === 'idle') {
        await initiateCall();
    } else if (callState === 'connected' || callState === 'calling') {
        // Technically this button just updates UI since Twilio controls the call leg
        // To actually hang up from the web, we'd need a backend endpoint to terminate the Twilio Call SID
        callState = 'idle';
        updateUI();
        addTranscriptMessage('system', 'Call ended manually from dashboard');
    }
});

async function initiateCall() {
    callState = 'calling';
    updateUI();
    transcriptArea.innerHTML = ''; // Clear previous transcript
    addTranscriptMessage('system', 'Initiating call...');

    try {
        const response = await fetch('/make-call', { method: 'POST' });
        const data = await response.json();

        if (data.status === 'calling') {
            addTranscriptMessage('system', `Call placed (SID: ${data.call_sid.substring(0, 8)}...)`);
            // The SSE events will handle switching state to 'connected' when Twilio hits the websocket
        } else {
            addTranscriptMessage('system', 'Failed to initiate call');
            callState = 'idle';
            updateUI();
        }
    } catch (err) {
        addTranscriptMessage('system', `Error: ${err.message}`);
        callState = 'idle';
        updateUI();
    }
}

function updateUI() {
    statusDot.className = 'status-dot';
    callBtn.className = '';

    switch (callState) {
        case 'idle':
            statusText.textContent = 'Ready to call';
            callBtn.innerHTML = '📞';
            break;
        case 'calling':
            statusDot.classList.add('calling');
            statusText.textContent = 'Calling your phone...';
            callBtn.classList.add('calling');
            callBtn.innerHTML = '📞';
            break;
        case 'connected':
            statusDot.classList.add('connected');
            statusText.textContent = 'AI Agent Active';
            callBtn.classList.add('connected');
            callBtn.innerHTML = '📴';
            break;
    }
}

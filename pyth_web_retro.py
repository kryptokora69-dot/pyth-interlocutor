from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import threading
import os
from starlette.concurrency import run_in_threadpool
import sys
import uvicorn
from pathlib import Path

HERE = Path(__file__).resolve().parent
if (HERE / "agent").is_dir() and (HERE / "tools").is_dir():
    ROOT = HERE
else:
    ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT))
_MOCK = (os.environ.get("PYTH_MOCK") or "").strip().lower() in ("1", "true", "yes", "on")
if _MOCK:
    from mock_core import run_agent  # deterministic local fallback (no Ollama)
else:
    from pyth_core import run_agent

app = FastAPI(title="PYTH RETRO TERMINAL")
GLOBAL_AGENT_HISTORY = []
UPLOAD_DIR = ROOT / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

HTML = r"""<!DOCTYPE html>
<html>
<head>
<title>PYTH ~ Ex nihilo</title>
<meta http-equiv="Cache-Control" content="no-store">
<script src="/static/marked.min.js?v=r14"></script>
<script src="/static/purify.min.js?v=r14"></script>
<style>
body { background:#000; color:#00ff41; font-family:'Courier New',monospace; margin:0; padding:20px; }
.crt { border:12px solid #222; padding:20px; max-width:1400px; margin:0 auto; box-shadow:0 0 40px rgba(0,255,65,0.6); position:relative; overflow:hidden; }
.crt.drag { box-shadow:0 0 40px rgba(0,204,255,0.9); }
.crt::after { content:""; position:absolute; top:0; left:0; width:100%; height:100%; background:repeating-linear-gradient(transparent 0px, transparent 4px, rgba(0,255,65,0.1) 4px, rgba(0,255,65,0.1) 8px); animation:scan 4s linear infinite; pointer-events:none; }
@keyframes scan { 0% {transform:translateY(-100%);} 100% {transform:translateY(100%);} }
#output { height:550px; overflow-y:auto; line-height:1.6; }
.user { color:#00ccff; margin:12px 0 4px 0; }
.pyth { color:#00ff41; margin:4px 0 16px 0; }
.thinking { color:#00cc33; font-style:italic; }
#input { background:#000; color:#00ff41; border:2px solid #00ff41; padding:10px; width:80%; font-family:inherit; font-size:16px; }
button { background:#00ff41; color:#000; border:none; padding:10px 16px; cursor:pointer; font-weight:bold; margin-left:8px; }
.pyth code { background:#001a08; color:#00ff41; padding:2px 5px; border-radius:3px; }
.pyth pre { background:#001a08; padding:10px; overflow-x:auto; border-left:3px solid #00ff41; }
.pyth a { color:#00ccff; }
.pyth .tablewrap { max-width:100%; overflow-x:auto; margin:8px 0; }
.pyth table { border-collapse:collapse; display:table; width:auto; margin:0; }
.pyth thead { display:table-header-group; } .pyth tbody { display:table-row-group; } .pyth tr { display:table-row; }
.pyth th, .pyth td { display:table-cell !important; border:1px solid #00ff41 !important; padding:4px 8px !important; }
.pyth table { display:table !important; border-collapse:collapse !important; }
.pyth .q { color:#007a20; font-size:12px; margin:0 0 2px 0; }
.pending { opacity:0.7; }
#micbtn.rec { background:#ff2a2a !important; color:#fff; animation:micpulse 1s ease-in-out infinite; }
@keyframes micpulse { 0%,100% { box-shadow:0 0 0 0 rgba(255,42,42,0.9); } 50% { box-shadow:0 0 0 10px rgba(255,42,42,0); } }
#uiver { position:absolute; right:30px; bottom:8px; font-size:11px; color:#007a20; }
.pyth th, .pyth td { border:1px solid #00ff41; padding:4px 8px; text-align:right; white-space:nowrap; }
.pyth th { background:#002b0e; color:#9dffb5; }
.pyth td:first-child, .pyth th:first-child { text-align:left; }
.pyth tr:last-child td { font-weight:bold; }
.pyth td .pos, .pyth td .neg { white-space:nowrap; }
.pyth p { margin:6px 0; }
.pyth ul { margin:4px 0 8px 18px; padding:0; }
.neg { color:#ff3b3b; font-weight:bold; }
.pos { color:#39ff14; font-weight:bold; text-shadow:0 0 4px #39ff14; }
</style>
</head>
<body>
<div class="crt" id="crt">
<h1 style="text-align:center;margin:10px 0;">PYTH ~ A Bespoke AI Interlocutor</h1>
<h3 style="text-align:center;color:#00cc33;margin-top:-10px;">Slow is Smooth ~ Smooth is Fast | 2026 kora@node01 | Ex nihilo</h3>
<hr style="border-color:#00ff41;">
<div id="output"></div>
<br>
<input type="text" id="input" placeholder="> Send auto-routes (news/NEAR); local: or LOCAL for Ollama" autofocus>
<button id="micbtn" onclick="toggleMic()" style="background:#ff66cc;" title="Click to talk, click again to stop">MIC</button>
<button onclick="sendMessage()" title="Auto-route: news cues → sonar; else NEAR (deep/quick); fallback local">SEND</button>
<button onclick="sendNews()" style="background:#cc66ff;" title="Ask a web-search model via Orbio (perplexity/sonar, ~0.007 CREDIT). Prefix [news] also works.">ORBIO</button>
<button onclick="sendNear()" style="background:#00e5ff;" title="Send this message to NEAR AI Cloud (DeepSeek V4.1 Flash TEE, costs a little)">NEAR</button>
<button onclick="sendLocal()" style="background:#39ff14;color:#000;" title="Force local Ollama / Pyth agent (tools, email, memory)">LOCAL</button>
<button onclick="input.value='check email'; sendLocal()" style="background:#00aaff;" title="Poll the team bridge inbox (Riley/Henry/Gladise replies only — not a full Gmail inbox)">TEAM INBOX</button>
<button onclick="confirmSendEmail()" style="background:#ffaa00;color:#000;" title="Send the pending general-email draft (requires a draft; never sends without one)">SEND EMAIL</button>
<button onclick="clearScreen()" style="background:#ff4444;">CLEAR</button>
<label style="margin-left:8px;font-size:13px;"><input type="checkbox" id="autosend"> auto-send after speaking</label>
<span id="micstate" style="margin-left:8px;font-size:13px;color:#ff66cc;"></span>
<input type="file" id="file" style="display:none">
<div id="uiver">ui r23</div>
</div>
<script>
if (window.marked) { marked.setOptions({gfm: true, breaks: true}); }
const output = document.getElementById('output');
const input = document.getElementById('input');
const crt = document.getElementById('crt');
const fileInput = document.getElementById('file');

function addThinking() {
    const thinking = document.createElement('div');
    thinking.className = 'thinking';
    thinking.id = 'thinking';
    thinking.textContent = 'PYTH is thinking...';
    output.appendChild(thinking);
    output.scrollTop = output.scrollHeight;
    return thinking;
}
function addUser(msg) {
    const userDiv = document.createElement('div');
    userDiv.className = 'user';
    userDiv.textContent = '> ' + msg;
    output.appendChild(userDiv);
    output.scrollTop = output.scrollHeight;
}
function paintNegatives(html) {
    const box = document.createElement('div'); box.innerHTML = html;
    const re = /(^|[^A-Za-z0-9])([-\u2212]\$?[\d,]*\.?\d+%?|\+\$?[\d,]*\.?\d+%?)/g;
    const walk = document.createTreeWalker(box, NodeFilter.SHOW_TEXT);
    const nodes = []; while (walk.nextNode()) nodes.push(walk.currentNode);
    for (const n of nodes) {
        const t = n.nodeValue; re.lastIndex = 0;
        if (!re.test(t)) continue; re.lastIndex = 0;
        const frag = document.createDocumentFragment(); let last = 0, m;
        while ((m = re.exec(t))) {
            const st = m.index + m[1].length;
            frag.appendChild(document.createTextNode(t.slice(last, st)));
            const sp = document.createElement('span');
            sp.className = (m[2][0] === '+') ? 'pos' : 'neg'; sp.textContent = m[2];
            frag.appendChild(sp); last = st + m[2].length;
        }
        frag.appendChild(document.createTextNode(t.slice(last)));
        n.parentNode.replaceChild(frag, n);
    }
    return box.innerHTML;
}
function fixTables(md) {
    // 1) re-split rows that were collapsed onto one line: "| a | b | | --- | --- | | x | y |"
    // 2) make sure every table starts after a blank line so marked parses it as a table
    const out = [];
    for (let line of (md || '').replace(/\r/g, '').split('\n')) {
        const m = line.match(/\|\s*\|\s*:?-{3,}/);
        if (m && (line.match(/\|/g) || []).length >= 9) {
            const k = line.indexOf('|'); const pre = line.slice(0, k).trim();
            const toks = line.slice(k).trim().replace(/^\||\|$/g, '').split('|').map(t => t.trim());
            const n = toks.filter(t => /^:?-{3,}:?$/.test(t)).length;
            const first = toks.findIndex(t => /^:?-{3,}:?$/.test(t));
            if (n >= 2 && first === n + 1) {
                if (pre) out.push(pre, '');
                for (let i = 0; i < toks.length; i += n + 1) out.push('| ' + toks.slice(i, i + n).join(' | ') + ' |');
                continue;
            }
        }
        out.push(line);
    }
    const res = [];
    for (let i = 0; i < out.length; i++) {
        const isRow = /^\s*\|.*\|\s*$/.test(out[i]);
        const prev = res.length ? res[res.length - 1] : '';
        if (isRow && prev.trim() !== '' && !/^\s*\|.*\|\s*$/.test(prev)) res.push('');
        res.push(out[i]);
    }
    return res.join('\n');
}
function wrapTables(box) {
    box.querySelectorAll('table').forEach(t => {
        if (t.parentNode.classList && t.parentNode.classList.contains('tablewrap')) return;
        const w = document.createElement('div'); w.className = 'tablewrap';
        t.parentNode.insertBefore(w, t); w.appendChild(t);
    });
}
function addReply(rawText) {
    const replyDiv = document.createElement('div');
    replyDiv.className = 'pyth';
    fillReply(replyDiv, rawText);
    output.appendChild(replyDiv);
    output.scrollTop = output.scrollHeight;
}
function renderMd(md) {
    md = fixTables(md || 'No clean response compiled.');
    if (window.marked && window.DOMPurify) return DOMPurify.sanitize(marked.parse(md, {gfm: true, breaks: true}));
    const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    return '<pre>' + esc(md) + '</pre><p class="q">(markdown library failed to load)</p>';
}
function fillReply(div, rawText) {
    div.innerHTML = paintNegatives(renderMd(rawText));
    wrapTables(div);
}

// Requests run one at a time, in order; each reply is written into the slot right under its own question.
const queue = []; let busy = false;
function enqueue(job) { queue.push(job); pump(); }
async function pump() {
    if (busy || !queue.length) return;
    busy = true;
    const job = queue.shift();
    job.slot.textContent = 'PYTH is thinking...'; job.slot.className = 'pyth thinking pending';
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 100000);
    try {
        const response = await fetch(job.url, {...job.opts, signal: ctl.signal});
        const data = await response.json();
        clearTimeout(timer);
        job.slot.className = 'pyth';
        fillReply(job.slot, data.reply);
    } catch (e) {
        clearTimeout(timer);
        job.slot.className = 'pyth';
        fillReply(job.slot, e.name === 'AbortError' ? 'No answer after 100s, so I gave up on that one. Try again or rephrase.' : 'Terminal connection desynced.');
    }
    output.scrollTop = output.scrollHeight;
    busy = false;
    pump();
}
function newSlot(label) {
    addUser(label);
    const slot = document.createElement('div');
    slot.className = 'pyth thinking pending';
    slot.textContent = queue.length || busy ? 'queued...' : 'PYTH is thinking...';
    output.appendChild(slot); output.scrollTop = output.scrollHeight;
    return slot;
}
function sendMessage() {
    const msg = input.value.trim();
    if (!msg) return;
    input.value = '';
    const slot = newSlot('[auto] ' + msg);
    enqueue({slot, url: '/chat', opts: {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message: msg})}});
}

// Talk-to-text: record in the browser, transcribe locally with faster-whisper, put text in the input box.
let mediaRec = null, micChunks = [], micStream = null;
const micBtn = document.getElementById('micbtn'), micState = document.getElementById('micstate');
async function toggleMic() {
    if (mediaRec && mediaRec.state === 'recording') { mediaRec.stop(); return; }
    try {
        micStream = await navigator.mediaDevices.getUserMedia({audio: true});
    } catch (e) { micState.textContent = 'mic blocked: ' + e.name; return; }
    const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus') ? 'audio/webm;codecs=opus' : '';
    mediaRec = new MediaRecorder(micStream, mime ? {mimeType: mime} : {});
    micChunks = [];
    mediaRec.ondataavailable = e => { if (e.data && e.data.size) micChunks.push(e.data); };
    mediaRec.onstop = async () => {
        micStream.getTracks().forEach(t => t.stop());
        micBtn.classList.remove('rec'); micBtn.textContent = 'MIC';
        micState.textContent = 'transcribing...';
        const fd = new FormData();
        fd.append('audio', new Blob(micChunks, {type: mediaRec.mimeType || 'audio/webm'}), 'speech.webm');
        try {
            const r = await fetch('/transcribe', {method: 'POST', body: fd});
            const d = await r.json();
            if (d.error) { micState.textContent = d.error; return; }
            const t = (d.text || '').trim();
            if (!t) { micState.textContent = "didn't catch that"; return; }
            input.value = (input.value.trim() ? input.value.trim() + ' ' : '') + t;
            micState.textContent = '';
            input.focus();
            if (document.getElementById('autosend').checked) sendMessage();
        } catch (e) { micState.textContent = 'transcribe failed'; }
    };
    mediaRec.start();
    micBtn.classList.add('rec'); micBtn.textContent = 'STOP';
    micState.textContent = 'listening...';
}

function sendExternal() {
    const msg = input.value.trim();
    if (!msg) return;
    input.value = '';
    const slot = newSlot('[external] ' + msg);
    enqueue({slot, url: '/external', opts: {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message: msg})}});
}

function sendNews() {
    const msg = input.value.trim();
    if (!msg) return;
    input.value = '';
    const slot = newSlot('[news] ' + msg);
    enqueue({slot, url: '/news', opts: {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message: msg})}});
}

function sendNear() {
    const msg = input.value.trim();
    if (!msg) return;
    input.value = '';
    const slot = newSlot('[near] ' + msg);
    enqueue({slot, url: '/near', opts: {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message: msg})}});
}

function confirmSendEmail() {
    // Mandatory confirm for general outbound email — never auto-sends from MIC alone.
    input.value = 'send the pending email';
    sendLocal();
}

function sendLocal() {
    const msg = input.value.trim();
    if (!msg) return;
    input.value = '';
    const slot = newSlot('[local] ' + msg);
    enqueue({slot, url: '/local', opts: {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message: msg})}});
}

function sendFile(file) {
    const fd = new FormData();
    fd.append('file', file);
    fd.append('note', input.value.trim());
    input.value = '';
    const slot = newSlot('[file] ' + file.name);
    enqueue({slot, url: '/upload', opts: {method: 'POST', body: fd}});
}

input.addEventListener('keypress', function(e) {
    if (e.key === 'Enter') sendMessage();
});
fileInput.addEventListener('change', function() {
    if (this.files && this.files[0]) sendFile(this.files[0]);
    this.value = '';
});
['dragenter','dragover'].forEach(ev => {
    crt.addEventListener(ev, e => { e.preventDefault(); crt.classList.add('drag'); });
});
['dragleave','drop'].forEach(ev => {
    crt.addEventListener(ev, e => { e.preventDefault(); crt.classList.remove('drag'); });
});
crt.addEventListener('drop', e => {
    e.preventDefault();
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) sendFile(f);
});

async function clearScreen() {
    output.innerHTML = '';
    await fetch('/clear', {method: 'POST'});
}
</script>
</body>
</html>
"""


app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
_AGENT_LOCK = threading.Lock()
_NO_STORE = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache", "Expires": "0"}


@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(HTML, headers=_NO_STORE)


TURN_TIMEOUT = 90
TOO_LONG = "That took too long, so I stopped. Try again or rephrase."


def _run_locked(message):
    """One agent turn at a time, capped at TURN_TIMEOUT seconds.
    The turn runs in a daemon thread on a copy of the history; if it overruns, we give up on it,
    keep the old history, and release the lock so the next message can run."""
    global GLOBAL_AGENT_HISTORY
    if not _AGENT_LOCK.acquire(timeout=TURN_TIMEOUT):
        return TOO_LONG
    try:
        box = {}
        hist = [dict(m) for m in GLOBAL_AGENT_HISTORY]

        def work():
            try:
                box["r"] = run_agent(message, hist)
            except Exception as e:
                box["e"] = e
        t = threading.Thread(target=work, daemon=True, name="pyth-turn")
        t.start()
        t.join(TURN_TIMEOUT)
        if t.is_alive():
            print(f"[turn timeout] '{message[:60]}' exceeded {TURN_TIMEOUT}s; abandoned")
            return TOO_LONG
        if "e" in box:
            raise box["e"]
        reply, GLOBAL_AGENT_HISTORY = box["r"]
        return reply
    finally:
        _AGENT_LOCK.release()


@app.post("/chat")
async def chat(request: Request):
    """Auto-route Send: news cues → Orbio sonar; else NEAR (deep/quick); fallbacks as in router.py.
    Explicit local: prefix forces local. Local history only mutates on the local path."""
    global GLOBAL_AGENT_HISTORY
    data = await request.json()
    message = (data.get("message") or "").strip()
    if not message:
        return {"reply": "No input compiled."}
    try:
        from router import route_message
        hist = [dict(m) for m in GLOBAL_AGENT_HISTORY]

        def local_runner(msg):
            return _run_locked(msg)

        reply, _info = await run_in_threadpool(route_message, message, hist, local_runner, False)
        # Team notice only when the turn actually ran the local agent (history may have grown).
        try:
            used_local = (_info or {}).get("provider") == "ollama" or (_info or {}).get("fallback") == "local"
            if used_local and reply and TOO_LONG not in (reply or ""):
                from pyth_core import team_reply_notice
                from tools.timeouts import run_with_timeout
                notice = await run_in_threadpool(lambda: run_with_timeout(team_reply_notice, 25))
                if notice:
                    reply = notice + "\n\n---\n\n" + reply
        except Exception as e:
            print(f"[team notice error] {e}")
        return {"reply": reply}
    except Exception as e:
        print(f"\n[INTERFACE PIPELINE CRASH]: {e}")
        return {"reply": f"ERROR: {e}"}


@app.post("/local")
async def local(request: Request):
    """Force local Ollama / Pyth agent (tools, email, memory). Same lock/history as old /chat."""
    global GLOBAL_AGENT_HISTORY
    data = await request.json()
    message = (data.get("message") or "").strip()
    if not message:
        return {"reply": "No input compiled."}
    try:
        from router import route_message

        def local_runner(msg):
            return _run_locked(msg)

        reply, _info = await run_in_threadpool(route_message, message, None, local_runner, True)
        try:
            if reply and TOO_LONG not in (reply or ""):
                from pyth_core import team_reply_notice
                from tools.timeouts import run_with_timeout
                notice = await run_in_threadpool(lambda: run_with_timeout(team_reply_notice, 25))
                if notice:
                    reply = notice + "\n\n---\n\n" + reply
        except Exception as e:
            print(f"[team notice error] {e}")
        return {"reply": reply}
    except Exception as e:
        print(f"\n[INTERFACE PIPELINE CRASH]: {e}")
        return {"reply": f"ERROR: {e}"}


@app.post("/external")
async def external(request: Request):
    """Send the message (plus recent chat context) to an external model via Orbio. Local history is not changed."""
    data = await request.json()
    message = (data.get("message") or "").strip()
    if not message:
        return {"reply": "No input compiled."}
    from orbio_remote import ask_external
    hist = [dict(m) for m in GLOBAL_AGENT_HISTORY]
    reply, _info = await run_in_threadpool(ask_external, message, hist)
    return {"reply": reply}


@app.post("/news")
async def news(request: Request):
    """Send the message to a web-search news model (perplexity/sonar) via Orbio. Local history is not changed."""
    data = await request.json()
    message = (data.get("message") or "").strip()
    if not message:
        return {"reply": "No input compiled."}
    from orbio_remote import ask_news
    reply, _info = await run_in_threadpool(ask_news, message)
    return {"reply": reply}


@app.post("/near")
async def near(request: Request):
    """Send the message (plus recent chat context) to NEAR AI Cloud. Local history is not changed."""
    data = await request.json()
    message = (data.get("message") or "").strip()
    if not message:
        return {"reply": "No input compiled."}
    from near_remote import ask_near
    hist = [dict(m) for m in GLOBAL_AGENT_HISTORY]
    reply, _info = await run_in_threadpool(ask_near, message, hist)
    return {"reply": reply}


@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    """Local speech-to-text (faster-whisper). Returns text only; nothing is sent to the agent."""
    data = await audio.read()
    if not data:
        return {"error": "no audio received"}
    try:
        from whisper_stt import transcribe_bytes
        text, info = await run_in_threadpool(transcribe_bytes, data)
        return {"text": text, **info}
    except Exception as e:
        print(f"[transcribe error] {type(e).__name__}: {e}")
        return {"error": f"transcribe failed: {type(e).__name__}"}


@app.post("/upload")
async def upload(file: UploadFile = File(...), note: str = Form("")):
    global GLOBAL_AGENT_HISTORY
    safe_name = Path(file.filename or "upload.bin").name.replace(" ", "_")
    dest = UPLOAD_DIR / safe_name
    dest.write_bytes(await file.read())
    prompt = f"{dest} {note or 'Analyze this file.'}"
    try:
        reply = await run_in_threadpool(_run_locked, prompt)
        return {"reply": reply, "path": str(dest)}
    except Exception as e:
        return {"reply": f"ERROR after save to {dest}: {e}"}


@app.post("/clear")
async def clear_history():
    global GLOBAL_AGENT_HISTORY
    GLOBAL_AGENT_HISTORY = []
    return {"status": "History wiped"}


class _Server(uvicorn.Server):
    """Ctrl+C: graceful stop, hard exit after 3s or on a second Ctrl+C (hung worker threads can't block it)."""
    _hits = 0

    def handle_exit(self, sig, frame):
        _Server._hits += 1
        if _Server._hits >= 2:
            print("\n[PYTH] second Ctrl+C: exiting now")
            os._exit(130)
        print("\n[PYTH] shutting down (Ctrl+C again to force)")
        threading.Timer(3.0, lambda: os._exit(130)).start()
        super().handle_exit(sig, frame)


if __name__ == "__main__":
    _Server(uvicorn.Config(app, host="0.0.0.0", port=7860)).run()
    os._exit(0)

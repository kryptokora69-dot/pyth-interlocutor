# PYTH — a personal AI Interlocutor (NEAR Hackathon 2026)

**PYTH** is a personal, philosophical companion: a local Python runtime and model that talks through a CRT-style chat UI, with optional routing to **NEAR AI Cloud** for stronger answers when you want them.

This repository is an entry for the **NEAR Hackathon** (build window **Nov 1–14, 2026**; must be public by **Nov 14 23:59 UTC**; target public date **Nov 9**). It is **not** a crypto trading bot. Wallet, trading, and personal portfolio tooling are intentionally omitted or stubbed.

> **Sister entry:** [Sage Sentinel](https://github.com/kryptokora69-dot/sage-sentinel) is this team's other, independent hackathon entry. The two repos do not share runtime code.

## What judges / AI screeners should know

| Fact | Detail |
|---|---|
| What it is | Local AI interlocutor (companion) with a FastAPI retro UI |
| Local runtime | Python agent (`agent/pyth_core.py`) + Ollama model (default `llama3.1:8b`) |
| Cloud path | NEAR AI Cloud OpenAI-compatible API (`near_remote.py` → `cloud-api.near.ai`) |
| UI | `pyth_web_retro.py` — FastAPI + uvicorn on `:7860`, neon CRT chat |
| Routing | `router.py` classifies Send → news / NEAR deep / NEAR quick / local |
| License | MIT © 2026 KryptoKora |
| Secrets in tree | None — use `.env.example` placeholders only |

## Architecture (short)

```
Browser CRT UI  ──POST /chat──►  pyth_web_retro.py (FastAPI)
                                   │
                                   ├─ router.py  (rule classifier)
                                   │    ├─ NEAR AI Cloud  (near_remote.py)
                                   │    ├─ optional news  (orbio_remote.py)
                                   │    └─ local agent    (agent/pyth_core.py → Ollama)
                                   │
                                   └─ tools/  (docs, weather, tides, memory, …)
                                      stubs/  (portfolio / email / trading omitted)
```

- **Local first:** personal / memory / document questions stay on-box.
- **NEAR when useful:** deep or quick philosophical / general questions can go to a Private TEE model on NEAR AI Cloud (default `deepseek-ai/DeepSeek-V4.1-Flash-1017`).
- **No keys in git:** API keys live in env vars or `~/.config/pith/*.env` (mode 600). This repo ships `.env.example` with placeholder names only.

## Quickstart (verified on a fresh tree with mock fallback)

These steps were run on the builder box from this tree with **no API key** and **no Ollama**, using the built-in mock. Say so in your own notes if you switch to live keys.

### 1) Clone and enter

```bash
git clone https://github.com/kryptokora69-dot/pyth-interlocutor.git
cd pyth-interlocutor
```

**Do X → See Y:** directory contains `pyth_web_retro.py`, `near_remote.py`, `README.md`, `LICENSE`.

### 2) Create a venv and install deps

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Do X → See Y:** `pip show fastapi uvicorn requests` prints package versions.

### 3) Copy example env (mock mode — no key needed)

```bash
cp .env.example .env
# .env already has PYTH_MOCK=1
set -a; source .env; set +a
```

**Do X → See Y:** `echo $PYTH_MOCK` prints `1`.

### 4) Start the CRT UI

```bash
python pyth_web_retro.py
# or: uvicorn pyth_web_retro:app --host 127.0.0.1 --port 7860
```

**Do X → See Y:** process listens on `http://127.0.0.1:7860/` and `GET /` returns HTML titled `PYTH ~ Ex nihilo`.

### 5) Send a chat turn (auto-router → NEAR mock)

```bash
curl -s -X POST http://127.0.0.1:7860/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"What is Pyth?"}'
```

**Do X → See Y:** JSON `reply` starts with `auto → NEAR (quick)` and includes `NEAR AI mock reply` (no API key). Verified on the builder box.

### 5b) Force the local mock path

```bash
curl -s -X POST http://127.0.0.1:7860/local \
  -H 'Content-Type: application/json' \
  -d '{"message":"What is Pyth?"}'
```

**Do X → See Y:** JSON `reply` contains `PYTH mock local` and echoes your question (no Ollama).

### 6) Hit the NEAR button route (mock cloud)

```bash
curl -s -X POST http://127.0.0.1:7860/near \
  -H 'Content-Type: application/json' \
  -d '{"message":"Explain solitude in one paragraph."}'
```

**Do X → See Y:** JSON `reply` contains `NEAR AI mock reply` and notes that no API key was used.

### 7) (Optional) Live NEAR AI Cloud

Unset mock, set a real key locally (never commit it):

```bash
unset PYTH_MOCK NEARAI_MOCK
export NEARAI_API_KEY=your_near_ai_api_key_here
# restart the server, then POST /near again
```

**Do X → See Y:** footer shows the live model id and token counts from `cloud-api.near.ai`.

### 8) (Optional) Live local Ollama

```bash
unset PYTH_MOCK
# ollama pull llama3.1:8b && ollama serve
python pyth_web_retro.py
```

**Do X → See Y:** `POST /local` returns a model-authored reply instead of the mock banner.

## Repository layout

```
pyth_web_retro.py   # CRT FastAPI UI (ui r23 lineage)
near_remote.py      # NEAR AI Cloud client (+ PYTH_MOCK)
near_credits.py     # local credit estimate helper
orbio_remote.py     # optional external/news path
router.py           # Send-button classifier
routing.json        # default route table
agent/              # local runtime (pyth_core + mock_core)
tools/              # local tools (+ stubs for omitted personal/trading)
memory/             # empty starter memory store (no personal docs)
static/             # marked + DOMPurify
.env.example        # placeholder names only
LICENSE             # MIT
```

## What was stripped for this public-facing tree

- API keys, tokens, SMTP passwords, seed phrases, wallet addresses
- Personal portfolio / holdings / trading code paths (stubbed)
- Personal email send + team-bridge messaging (stubbed)
- Personal documents (VA, banking, military, health, etc.) — not present
- Operator / miner service helpers

## License

MIT — Copyright (c) 2026 KryptoKora. See [LICENSE](LICENSE).

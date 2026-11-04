"""Local mock agent for demos without Ollama.

Enabled when PYTH_MOCK=1 (or true/yes/on). Returns a deterministic reply so the
CRT UI and /chat path can be verified from a fresh clone with no API keys.
"""
from __future__ import annotations


def run_agent(message: str, history=None):
    history = list(history or [])
    msg = (message or "").strip()
    reply = (
        "**[PYTH mock local]**\n\n"
        "Mock local runtime (no Ollama call).\n\n"
        f"**You said:** {msg[:800]}\n\n"
        "Pyth is a personal philosophical companion: a local Python agent with a "
        "CRT-style chat UI, optional NEAR AI Cloud routing, and local tools.\n\n"
        "Unset `PYTH_MOCK` and run Ollama (`llama3.1:8b`) for the real local agent, "
        "or set `NEARAI_API_KEY` for live NEAR AI Cloud replies."
    )
    history.append({"role": "user", "content": msg})
    history.append({"role": "assistant", "content": reply})
    return reply, history

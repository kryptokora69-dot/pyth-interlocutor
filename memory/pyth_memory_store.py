"""Simple local memory for PYTH. Facts + doc summaries + capped chat log."""
import json
import os
import re
from datetime import datetime
from pathlib import Path

MEMORY_FILE = os.environ.get(
    "PYTH_MEMORY_FILE",
    str(Path.home() / "pyth" / "memory" / "pyth_memory.json"),
)
MAX_TURNS = 40


def _empty():
    return {"facts": {}, "documents": {}, "conversations": []}


def load_memory():
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("facts", {})
            data.setdefault("documents", {})
            data.setdefault("conversations", [])
            return data
        except Exception:
            return _empty()
    return _empty()


def save_memory(memory):
    os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(memory, f, indent=2, ensure_ascii=False)


def add_fact(key, value):
    key = (key or "").strip()[:80] or "note"
    value = (value or "").strip()[:2000]
    memory = load_memory()
    memory["facts"][key] = value
    save_memory(memory)
    return {"saved": key, "value": value}


def add_document_summary(path, summary):
    memory = load_memory()
    memory["documents"][path] = {
        "summary": summary,
        "ingested_at": datetime.now().isoformat(),
    }
    save_memory(memory)


def append_turn(user_text, assistant_text):
    memory = load_memory()
    memory["conversations"].append(
        {
            "t": datetime.now().isoformat(timespec="seconds"),
            "user": (user_text or "")[:800],
            "pyth": (assistant_text or "")[:800],
        }
    )
    memory["conversations"] = memory["conversations"][-MAX_TURNS:]
    save_memory(memory)


def _score(query, text):
    q = set(re.findall(r"[a-z0-9]{3,}", (query or "").lower()))
    t = set(re.findall(r"[a-z0-9]{3,}", (text or "").lower()))
    if not q or not t:
        return 0
    return len(q & t)


def get_relevant_memory(query: str = "all"):
    memory = load_memory()
    q = (query or "all").strip()
    if q.lower() in {"all", "*", ""}:
        facts = "\n".join(f"- {k}: {v}" for k, v in memory["facts"].items())
        docs = "\n".join(
            f"- {p}: {d.get('summary', '')[:240]}"
            for p, d in list(memory["documents"].items())[:6]
        )
        recent = memory["conversations"][-4:]
        turns = "\n".join(f"- {c.get('t')}: {c.get('user', '')[:120]}" for c in recent)
        return f"Personal Facts:\n{facts}\n\nDocument Summaries:\n{docs}\n\nRecent turns:\n{turns}"

    scored_facts = []
    for k, v in memory["facts"].items():
        s = _score(q, k + " " + str(v))
        if s:
            scored_facts.append((s, k, v))
    scored_facts.sort(reverse=True)
    facts = "\n".join(f"- {k}: {v}" for _, k, v in scored_facts[:8]) or "(none matched)"

    scored_docs = []
    for p, d in memory["documents"].items():
        blob = p + " " + d.get("summary", "")
        s = _score(q, blob)
        if s:
            scored_docs.append((s, p, d.get("summary", "")[:400]))
    scored_docs.sort(reverse=True)
    docs = "\n".join(f"- {p}: {s}" for _, p, s in scored_docs[:4]) or "(none matched)"

    scored_turns = []
    for c in memory["conversations"]:
        blob = c.get("user", "") + " " + c.get("pyth", "")
        s = _score(q, blob)
        if s:
            scored_turns.append((s, c))
    scored_turns.sort(reverse=True, key=lambda x: x[0])
    turns = "\n".join(
        f"- {c.get('t')}: U:{c.get('user','')[:160]} | P:{c.get('pyth','')[:160]}"
        for _, c in scored_turns[:4]
    ) or "(none matched)"

    return f"Query: {q}\n\nFacts:\n{facts}\n\nDocs:\n{docs}\n\nRelated turns:\n{turns}"

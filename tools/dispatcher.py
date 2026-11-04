import sys
import json
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from .orbio_mcp_client import call_orbio_tool
from pyth.tools.lm_bridge import ask_model
from pyth.tools.file_loader import load_document
from pyth.tools.coingecko_tools import get_simple_prices


TOOL_ALIASES = {
    "fetch_pages": "fetch_page",
    "fetch_pages.text": "fetch_page",
    "fetch_page.py": "fetch_page",
    "page_fetch": "fetch_page",
    "page_fetch.py": "fetch_page",
    "file_loader": "load_document",
    "file_loader.py": "load_document",
    "load_file": "load_document",
    "get_prices": "get_simple_prices",
    "coingecko": "get_simple_prices",
}

BANNED_PHRASES = (
    "fetch_pages.text",
    "summary of fetch_pages.text",
    "using the page_fetch.py tool",
    "using page_fetch.py",
    "based on the tool_result output",
)

TOOL_SYSTEM_PROMPT = r"""You are Pyth Core, interacting with operator Kora.

There is NO file named fetch_pages.text. Never write that string.
Do not claim a tool ran unless you output a JSON tool call as your entire message.

Tools (output ONLY a raw JSON object, no markdown, no prose):

1) Live prices
{"tool": "get_simple_prices", "arguments": {"ids": ["bitcoin", "ethereum"]}}

2) Local file (pdf/txt/md/docx/jpg/png under this repo's memory/ or data/uploads/)
{"tool": "load_document", "arguments": {"path": "memory/example.txt"}}

3) HTTP/HTTPS page
{"tool": "fetch_page", "arguments": {"url": "https://example.com"}}

Rules:
- If the user already pasted the document, do NOT call a tool. Answer from the paste.
- Paths starting with /home or ~/ → load_document, never fetch_page.
- URLs starting with http → fetch_page, never load_document.
- NEVER invent prices, quotes, or file excerpts.
- NEVER assume a token is $1 or on Solana unless the tool result says so.
- If you cannot call a tool and do not have data, say you do not have it.
"""

DEFAULT_PRICE_IDS = [
    "bitcoin",
    "ethereum",
    "solana",
    "near",
    "jupiter-exchange-solana",
    "aerodrome-finance",
    "venice-vance",
    "zcash",
    "nosana",
]

ID_MAP = {
    "btc": "bitcoin",
    "eth": "ethereum",
    "sol": "solana",
    "near": "near",
    "jup": "jupiter-exchange-solana",
    "aero": "aerodrome-finance",
    "vvv": "venice-vance",
    "zec": "zcash",
    "nos": "nosana",
}

def handle_orbio_command(command: str):
    if command == "dashboard":
        return call_orbio_tool("get_dashboard", {})
    if command == "points":
        return call_orbio_tool("get_points", {})
    if command == "rank":
        return call_orbio_tool("get_rank", {})
    if command == "claim_usdg":
        return call_orbio_tool("claim_usdg", {})
    return {"error": f"Unknown Orbio command: {command}"}

def extract_text(response_payload):
    if isinstance(response_payload, dict):
        if "response" in response_payload:
            return str(response_payload["response"]).strip()
        if "message" in response_payload and "content" in response_payload["message"]:
            return str(response_payload["message"]["content"]).strip()
    return str(response_payload).strip()


def normalize_tool_name(name):
    if not name:
        return ""
    key = str(name).strip().lower()
    return TOOL_ALIASES.get(key, key)


def looks_like_url(text):
    t = (text or "").strip()
    return t.startswith("http://") or t.startswith("https://")


def looks_like_path(text):
    t = (text or "").strip().strip("'\"")
    return t.startswith("/home/") or t.startswith("~/") or t.startswith(str(Path(__file__).resolve().parents[1]))


def parse_tool_json(raw):
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = re.sub(r"^json", "", text, flags=re.I).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None
        try:
            obj = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if isinstance(obj, dict) and "tool" in obj:
        obj["tool"] = normalize_tool_name(obj.get("tool"))
        obj["arguments"] = obj.get("arguments") or {}
        return obj
    return None


def contains_banned(text):
    low = (text or "").lower()
    return any(p in low for p in BANNED_PHRASES)


def fetch_page(url: str):
    if not looks_like_url(url):
        return {"error": f"fetch_page requires http(s) URL, got: {url}"}
    try:
        from pyth.tools import page_fetch as pf
    except Exception as e:
        return {"error": f"page_fetch import failed: {e}"}

    for fn_name in ("fetch_page", "fetch", "get_page", "run", "main"):
        fn = getattr(pf, fn_name, None)
        if callable(fn):
            try:
                return fn(url)
            except TypeError:
                try:
                    return fn()
                except Exception as e:
                    return {"error": f"{fn_name}() failed: {e}"}
            except Exception as e:
                return {"error": f"{fn_name}({url}) failed: {e}"}
    return {
        "error": "page_fetch.py has no fetch_page/fetch/get_page/run. "
        "Export def fetch_page(url: str)."
    }


def execute_tool(tool_name, arguments):
    name = normalize_tool_name(tool_name)
    arguments = arguments or {}
    try:
        if name == "get_simple_prices":
            ids = arguments.get("ids") or DEFAULT_PRICE_IDS
            resolved = [ID_MAP.get(str(x).lower(), str(x).lower()) for x in ids]
            print(f"\n[API] CoinGecko ids={resolved}")
            return get_simple_prices(resolved)

        if name == "load_document":
            path = arguments.get("path") or arguments.get("file") or arguments.get("file_path")
            if not path:
                return {"error": "load_document needs arguments.path"}
            print(f"\n[FILE] load_document {path}")
            return {"path": path, "text": load_document(path)}

        if name == "fetch_page":
            url = arguments.get("url") or arguments.get("link") or arguments.get("href")
            if not url:
                return {"error": "fetch_page needs arguments.url"}
            print(f"\n[HTTP] fetch_page {url}")
            return {"url": url, "result": fetch_page(url)}

        return {"error": f"Tool '{name}' is not wired."}
    except Exception as e:
        return {"error": f"Dispatcher failed: {e}"}


def maybe_force_tool(user_input):
    """If the user only dropped a path or URL, do not wait for the model."""
    text = user_input.strip().strip("'\"")
    if looks_like_url(text):
        return {"tool": "fetch_page", "arguments": {"url": text}}
    if looks_like_path(text):
        return {"tool": "load_document", "arguments": {"path": str(Path(text).expanduser())}}
    return None


def synthesize(user_input, tool_name, tool_result):
    prompt = (
        "You are Pyth Core. The host already ran a tool. Use ONLY this result.\n"
        "Do not mention fetch_pages.text. Do not invent missing numbers.\n"
        "If the result is an error or empty text, say that plainly.\n\n"
        f"OPERATOR INPUT:\n{user_input}\n\n"
        f"TOOL RAN: {tool_name}\n"
        f"TOOL RESULT:\n{json.dumps(tool_result, indent=2, default=str)[:24000]}\n"
    )
    return extract_text(ask_model(prompt))


def run_agent(user_input):
    forced = maybe_force_tool(user_input)
    if forced:
        result = execute_tool(forced["tool"], forced["arguments"])
        return synthesize(user_input, forced["tool"], result)

    combined = f"{TOOL_SYSTEM_PROMPT}\n\nUser Request: {user_input}"
    first_reply = extract_text(ask_model(combined))
    if not first_reply:
        return "Pyth: [empty model response]"

    tool_call = parse_tool_json(first_reply)
    if tool_call:
        result = execute_tool(tool_call["tool"], tool_call.get("arguments", {}))
        return synthesize(user_input, tool_call["tool"], result)

    if contains_banned(first_reply):
        return (
            "Pyth: I started to invent a tool result (fetch_pages.text). "
            "No tool ran. Ask again with a URL, a /home path, or a pasted document."
        )

    return f"Pyth: {first_reply}"


def main():
    print("====================================================")
    print("Pyth Core Dispatch [v1.0.7]  prices | files | pages")
    print("====================================================")
    while True:
        user_input = input("You: ").strip()
        if user_input.lower() == "exit":
            print("Terminating. Smooth operations, Kora.")
            break
        if not user_input:
            continue
        print(run_agent(user_input))


if __name__ == "__main__":
    main()

# ~/pyth/tools/web_search_tool.py  (round 18: hard 12s timeout, limited engines)
from typing import Dict, Any
from tools.timeouts import run_with_timeout, HardTimeout

SEARCH_TIMEOUT = 12
BACKENDS = "duckduckgo,brave,bing"   # fewer engines than "auto" -> fewer hangs


def _search(query, num_results):
    from ddgs import DDGS
    with DDGS(timeout=8) as ddgs:
        try:
            rows = ddgs.text(query, max_results=num_results, backend=BACKENDS)
        except TypeError:
            rows = ddgs.text(query, max_results=num_results)
    return [{"title": r.get("title"), "url": r.get("href"), "snippet": r.get("body")} for r in rows or []]


def web_search(query: str, num_results: int = 10) -> Dict[str, Any]:
    """Web search using ddgs, never longer than SEARCH_TIMEOUT seconds."""
    try:
        results = run_with_timeout(_search, SEARCH_TIMEOUT, query, num_results)
        return {"query": query, "results": results, "total": len(results)}
    except HardTimeout:
        return {"query": query, "results": [], "total": 0, "error": f"web search timed out after {SEARCH_TIMEOUT}s"}
    except ImportError:
        return {"error": "ddgs not installed properly. pip install ddgs", "results": []}
    except Exception as e:
        return {"error": f"Web search failed: {str(e)[:200]}", "results": []}

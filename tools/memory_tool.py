from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.pyth_memory_store import add_fact, get_relevant_memory


def get_personal_memory(query: str = "all"):
    try:
        return {"memory": get_relevant_memory(query)}
    except Exception as e:
        return {"error": f"Memory retrieval failed: {e}"}


def remember_fact(key: str, value: str):
    try:
        return add_fact(key, value)
    except Exception as e:
        return {"error": f"Remember failed: {e}"}

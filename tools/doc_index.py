"""Local SQLite FTS5 index over the user's memory/ and data/uploads/ documents."""
import hashlib
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # repo root
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.file_loader import load_document, is_unreadable

DB = ROOT / "memory" / "doc_index.sqlite"
DIRS = [ROOT / "memory", ROOT / "data" / "uploads"]
SKIP_NAMES = {"pyth_venv", "__pycache__"}
SKIP_SUFFIX = {".py", ".pyc", ".sqlite", ".sqlite-journal", ".bak"}
SKIP_FILES = {"pyth_memory.json", "watchlist.json"}
MAX_BYTES = 50 * 1024 * 1024
MAX_PAGES = 0          # 0 = full document
BIG_BYTES = 20 * 1024 * 1024
BIG_PAGES = 60         # cap for very large PDFs unless --full
FULL_BIG = False
CHUNK = 800


def _db():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime REAL, size INT, chars INT, status TEXT)")
    con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(path UNINDEXED, name, text, tokenize='porter unicode61')")
    cols = {r[1] for r in con.execute("PRAGMA table_info(files)")}
    if "sha" not in cols:
        con.execute("ALTER TABLE files ADD COLUMN sha TEXT")
        con.execute("UPDATE files SET mtime=0")  # force one-time reindex to fill hashes
    if "head" not in cols:
        con.execute("ALTER TABLE files ADD COLUMN head TEXT")
    if "capped" not in cols:
        con.execute("ALTER TABLE files ADD COLUMN capped INT DEFAULT 0")
        con.execute("UPDATE files SET mtime=0")  # one-time full reindex without the old 40-page cap
    con.execute("CREATE TABLE IF NOT EXISTS vecs(id INTEGER PRIMARY KEY, path TEXT, vec BLOB)")
    return con


def _sha(f):
    h = hashlib.sha256()
    with open(f, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _chunks(text):
    text = re.sub(r"[ \t]+", " ", text)
    paras, out, cur = re.split(r"\n\s*\n", text), [], ""
    for p in paras:
        if len(cur) + len(p) > CHUNK and cur:
            out.append(cur.strip()); cur = cur[-150:]  # small overlap
        cur += "\n" + p
        while len(cur) > CHUNK * 1.6:
            out.append(cur[:CHUNK].strip()); cur = cur[CHUNK - 150:]
    if cur.strip():
        out.append(cur.strip())
    return out


def _candidates():
    files = [f for d in DIRS for f in d.rglob("*")]
    files.sort(key=lambda f: (bool(re.search(r"\(\d\)|copy", f.name, re.I)), "uploads" in f.parts, f.name))
    for f in files:
        if True:
            if not f.is_file() or any(s in f.parts for s in SKIP_NAMES):
                continue
            if f.suffix.lower() in SKIP_SUFFIX or f.name in SKIP_FILES or ".bak" in f.name or f.name.startswith("doc_index"):
                continue
            yield f


def index_file(con, f: Path, force=False):
    st = f.stat()
    row = con.execute("SELECT mtime, size FROM files WHERE path=?", (str(f),)).fetchone()
    if row and not force and row[0] == st.st_mtime and row[1] == st.st_size:
        if not (FULL_BIG and st.st_size > BIG_BYTES and (row_c := con.execute("SELECT capped FROM files WHERE path=?", (str(f),)).fetchone()) and row_c[0]):
            return "unchanged"
    con.execute("DELETE FROM chunks WHERE path=?", (str(f),))
    con.execute("DELETE FROM vecs WHERE path=?", (str(f),))
    sha = _sha(f)
    dup = con.execute("SELECT path FROM files WHERE sha=? AND path<>? AND status='ok'", (sha, str(f))).fetchone()
    text, head = "", ""
    if dup:
        status = f"duplicate of {Path(dup[0]).name}"
    elif st.st_size > MAX_BYTES:
        status = "skipped: >50MB"
    else:
        capped = st.st_size > BIG_BYTES and not FULL_BIG
        text = load_document(str(f), max_pages=BIG_PAGES if capped else MAX_PAGES) or ""
        bad = is_unreadable(text)
        status = f"unreadable: {bad}" if bad else "ok"
        if capped and status == "ok":
            status = "ok"; head_note = f" [indexed first {BIG_PAGES} pages]"
        head = " ".join(text[:600].split())
    if status == "ok":
        for c in _chunks(text):
            con.execute("INSERT INTO chunks(path, name, text) VALUES (?,?,?)", (str(f), f.name, c))
    con.execute("INSERT OR REPLACE INTO files(path, mtime, size, chars, status, sha, head, capped) VALUES (?,?,?,?,?,?,?,?)",
                (str(f), st.st_mtime, st.st_size, len(text), status, sha, head, int(bool(text) and st.st_size > BIG_BYTES and not FULL_BIG)))
    con.commit()
    return status


def update_index(verbose=False):
    con = _db()
    seen, stats = set(), {}
    for f in _candidates():
        seen.add(str(f))
        s = index_file(con, f)
        stats[s.split(":")[0]] = stats.get(s.split(":")[0], 0) + 1
        if verbose and s != "unchanged":
            print(f"{s:30} {f}")
    for (p,) in con.execute("SELECT path FROM files").fetchall():
        if p not in seen:  # file gone: drop from index only
            con.execute("DELETE FROM chunks WHERE path=?", (p,)); con.execute("DELETE FROM vecs WHERE path=?", (p,)); con.execute("DELETE FROM files WHERE path=?", (p,))
    con.commit()
    try:
        stats["embedded"] = embed_missing(con)
    except Exception as e:
        stats["embed_error"] = str(e)
    return stats


_STOP = set("a an the of to in on for and or is was were be been am are i me my mine you your what when where who how why did do does say says about from with at it this that these those tell give show any much many have has had which".split())


# ---- embeddings (Ollama nomic-embed-text) + hybrid retrieval -------------
import json as _json
import time as _time
import urllib.request as _ur
import numpy as np

EMBED_URL = "http://localhost:11434/api/embed"
EMBED_MODEL = "nomic-embed-text"
LAST_TIMING = {}
_VCACHE = {"n": -1}


def embed(texts, kind="search_document"):
    out = []
    for i in range(0, len(texts), 32):
        batch = [f"{kind}: {t[:2000]}" for t in texts[i:i + 32]]
        req = _ur.Request(EMBED_URL, data=_json.dumps({"model": EMBED_MODEL, "input": batch}).encode(),
                          headers={"Content-Type": "application/json"})
        with _ur.urlopen(req, timeout=300) as r:
            out.extend(_json.loads(r.read())["embeddings"])
    return [np.asarray(v, dtype=np.float32) for v in out]


def embed_missing(con, limit=100000):
    rows = con.execute("SELECT rowid, path, text FROM chunks WHERE rowid NOT IN (SELECT id FROM vecs) LIMIT ?", (limit,)).fetchall()
    for i in range(0, len(rows), 64):
        part = rows[i:i + 64]
        vs = embed([r[2] for r in part])
        con.executemany("INSERT OR REPLACE INTO vecs(id, path, vec) VALUES (?,?,?)",
                        [(r[0], r[1], v.tobytes()) for r, v in zip(part, vs)])
        con.commit()
    return len(rows)


def _vec_matrix(con):
    n = con.execute("SELECT count(*), max(id) FROM vecs").fetchone()
    if _VCACHE.get("n") != n:
        rows = con.execute("SELECT id, path, vec FROM vecs").fetchall()
        ids = np.array([r[0] for r in rows]); paths = [r[1] for r in rows]
        M = np.vstack([np.frombuffer(r[2], dtype=np.float32) for r in rows]) if rows else np.zeros((0, 768), np.float32)
        M = M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
        _VCACHE.update(n=n, ids=ids, paths=paths, M=M)
    return _VCACHE


def hybrid_search(query, k=8, paths=None, source="docs", per_file=4):
    """BM25 + cosine merged by reciprocal rank fusion. source: docs | conversation | all."""
    t0 = _time.time()
    con = _db()
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in _STOP and len(w) > 1]
    for y in [int(w) for w in words if re.fullmatch(r"(19|20)\d\d", w)]:
        words += [str(y + d) for d in (-2, -1, 1, 2)]  # date ranges like 1994-1997 cover 1995
    def ok_path(p):
        if paths is not None and p not in paths:
            return False
        is_conv = p.startswith("conv://")
        return source == "all" or (source == "conversation") == is_conv
    fts_ids = []
    if words:
        fts = " OR ".join(f'"{w}"' for w in words)
        for rid, p in con.execute("SELECT rowid, path FROM chunks WHERE chunks MATCH ? ORDER BY bm25(chunks, 0.0, 3.0, 1.0) LIMIT 300", (fts,)):
            if ok_path(p):
                fts_ids.append(rid)
    t1 = _time.time()
    vec_ids = []
    try:
        qv = embed([query], "search_query")[0]; qv = qv / (np.linalg.norm(qv) + 1e-9)
        t2 = _time.time()
        V = _vec_matrix(con)
        if len(V["ids"]):
            mask = np.array([ok_path(p) for p in V["paths"]])
            sims = V["M"] @ qv
            sims[~mask] = -9
            top = np.argsort(-sims)[:60]
            vec_ids = [int(V["ids"][i]) for i in top if sims[i] > -9]
    except Exception as e:
        t2 = _time.time(); LAST_TIMING["embed_error"] = str(e)
    score = {}
    for lst in (fts_ids[:60], vec_ids):
        for rank, rid in enumerate(lst):
            score[rid] = score.get(rid, 0) + 1.0 / (60 + rank)
    res, pf = [], {}
    for rid in sorted(score, key=lambda r: -score[r]):
        row = con.execute("SELECT path, name, text FROM chunks WHERE rowid=?", (rid,)).fetchone()
        if not row or pf.get(row[0], 0) >= per_file:
            continue
        pf[row[0]] = pf.get(row[0], 0) + 1
        res.append({"id": rid, "file": row[1], "path": row[0], "score": round(score[rid], 4), "text": row[2]})
        if len(res) >= k:
            break
    LAST_TIMING.update(fts_ms=round((t1 - t0) * 1000), embed_ms=round((t2 - t1) * 1000), total_ms=round((_time.time() - t0) * 1000))
    return res


def search_documents(query, k=8):
    try:
        update_index()
    except Exception:
        pass
    return {"query": query, "results": hybrid_search(query, k=k)}


def file_text(path):
    con = _db()
    return "\n".join(r[0] for r in con.execute("SELECT text FROM chunks WHERE path=? ORDER BY rowid", (path,)))


def add_conversation(user, reply, ts=None):
    import datetime as _dt, os as _os
    if _os.environ.get("PYTH_NO_CONVLOG"):
        return
    ts = ts or _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    con = _db()
    text = f"[{ts}] User asked: {user}\nPYTH replied: {reply[:1200]}"
    cur = con.execute("INSERT INTO chunks(path, name, text) VALUES (?,?,?)", (f"conv://{ts}", "past conversation", text))
    try:
        v = embed([text])[0]
        con.execute("INSERT OR REPLACE INTO vecs(id, path, vec) VALUES (?,?,?)", (cur.lastrowid, f"conv://{ts}", v.tobytes()))
    except Exception:
        pass
    con.commit()


def conversation_since(prefix):
    con = _db()
    return [r[0] for r in con.execute("SELECT text FROM chunks WHERE path >= ? AND path LIKE 'conv://%' ORDER BY path", ("conv://" + prefix,))]


def file_meta():
    con = _db()
    return [{"file": Path(p).name, "path": p, "chars": c or 0, "head": h or ""}
            for p, c, h in con.execute("SELECT path, chars, head FROM files WHERE status='ok'")]


def list_documents():
    con = _db()
    return [{"file": Path(p).name, "path": p, "chars": c, "status": s}
            for p, c, s in con.execute("SELECT path, chars, status FROM files ORDER BY path")]


if __name__ == "__main__":
    if "--full" in sys.argv:
        FULL_BIG = True
    print(update_index(verbose=True))

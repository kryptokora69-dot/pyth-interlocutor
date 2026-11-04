"""Robust local text extraction: PDF (pdftotext/pypdf + OCR fallback), DOCX, text, images (tesseract)."""
import re
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_ROOTS = [ROOT, ROOT / "memory", ROOT / "data" / "uploads"]
TEXT_EXT = {".txt", ".md", ".json", ".csv", ".tsv", ".log"}
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"}


def _allowed(file_path: Path) -> bool:
    try:
        resolved = file_path.resolve()
    except Exception:
        return False
    for root in ALLOWED_ROOTS:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _sniff(fp: Path) -> str:
    suffix = fp.suffix.lower()
    if suffix and len(suffix) <= 6 and suffix[1:].isalnum():
        if suffix in IMG_EXT | TEXT_EXT | {".pdf", ".docx", ".doc"}:
            return suffix
    try:
        head = fp.open("rb").read(8)
    except Exception:
        return suffix
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"\x89PNG"):
        return ".png"
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head.startswith(b"PK"):
        return ".docx"
    return suffix


def _run(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _ocr_variants(img):
    """Preprocessing variants: grayscale, 2x upscale, autocontrast, median denoise, Otsu-ish threshold."""
    from PIL import Image, ImageFilter, ImageOps
    g = ImageOps.grayscale(img)
    if max(g.size) < 4000:
        g = g.resize((g.width * 2, g.height * 2), Image.LANCZOS)
    g = ImageOps.autocontrast(g)
    den = g.filter(ImageFilter.MedianFilter(3))
    hist = den.histogram()
    total = sum(hist); sum_all = sum(i * h for i, h in enumerate(hist))
    wb = sb = 0; best = (0, 128)
    for t in range(256):  # Otsu threshold
        wb += hist[t]
        if wb == 0 or wb == total:
            continue
        sb += t * hist[t]
        mb, mf = sb / wb, (sum_all - sb) / (total - wb)
        var = wb * (total - wb) * (mb - mf) ** 2
        if var > best[0]:
            best = (var, t)
    th = den.point(lambda x, t=best[1]: 255 if x > t else 0)
    out = {"gray2x": g, "denoise": den, "threshold": th}
    for t in (90, 110):  # dark text over photo backgrounds (certificates)
        out[f"dark{t}"] = g.point(lambda x, t=t: 0 if x < t else 255).filter(ImageFilter.MedianFilter(3))
    return out


def ocr_best(fp):
    """Try preprocessing variants x psm 3/4/6; return (text, info) with the highest mean word confidence."""
    import pytesseract
    from PIL import Image
    img = Image.open(fp)
    img.load()
    best = ("", {"conf": -1})
    for vname, v in _ocr_variants(img).items():
        for psm in (3, 4, 6):
            try:
                d = pytesseract.image_to_data(v, config=f"--psm {psm}", output_type=pytesseract.Output.DICT, timeout=60)
            except Exception:
                continue
            confs = [float(c) for c, w in zip(d["conf"], d["text"]) if w.strip() and float(c) >= 0]
            words = [w for w in d["text"] if w.strip()]
            if not confs:
                continue
            good = [w for w, c in zip(d["text"], d["conf"]) if w.strip() and float(c) >= 60 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9'.,:()/&-]*", w)]
            # reward many confident real words, penalise junk tokens
            score = len(good) - 0.5 * (len(words) - len(good))
            if score > best[1]["conf"]:
                txt = pytesseract.image_to_string(v, config=f"--psm {psm}", timeout=60).strip()
                best = (txt, {"conf": round(score, 1), "good_words": len(good), "mean_conf": round(sum(confs) / len(confs), 1), "variant": vname, "psm": psm})
    return best


def ocr_image(fp) -> str:
    try:
        txt, info = ocr_best(fp)
        if txt:
            return txt
    except Exception:
        pass
    try:
        r = _run(["tesseract", str(fp), "-", "--psm", "3"], timeout=180)
        if r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def _pdf_pages(fp: Path) -> int:
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(fp)).pages)
    except Exception:
        try:
            out = _run(["pdfinfo", str(fp)]).stdout
            return int([l for l in out.splitlines() if l.startswith("Pages:")][0].split()[1])
        except Exception:
            return 0


def extract_pdf(fp: Path, max_pages: int = 0) -> str:
    n = _pdf_pages(fp)
    last = min(n, max_pages) if (max_pages and n) else n
    pages = []
    for i in range(1, (last or 0) + 1):
        txt = ""
        try:
            txt = _run(["pdftotext", "-layout", "-f", str(i), "-l", str(i), str(fp), "-"]).stdout.strip()
        except Exception:
            pass
        if len(txt) < 40:  # scanned page -> OCR
            try:
                with tempfile.TemporaryDirectory() as td:
                    _run(["pdftoppm", "-r", "200", "-f", str(i), "-l", str(i), "-png", str(fp), f"{td}/p"])
                    imgs = sorted(Path(td).glob("p*.png"))
                    if imgs:
                        o = ocr_image(imgs[0])
                        if len(o) > len(txt):
                            txt = o
            except Exception:
                pass
        if txt:
            pages.append(f"[page {i}]\n{txt}")
    if not pages and not n:
        try:
            from pypdf import PdfReader
            pages = [p.extract_text() or "" for p in PdfReader(str(fp)).pages]
        except Exception as e:
            return f"[pdf error: {e}]"
    return "\n\n".join(p for p in pages if p).strip()


def load_document(path: str, max_pages: int = 0) -> Optional[str]:
    """Return extracted text, or a bracketed '[...]' error string."""
    if not path:
        return None
    fp = Path(os.path.expanduser(path.strip().strip("'\"")))
    if not fp.exists():
        return f"[file not found: {fp}]"
    if not _allowed(fp):
        return f"[refused: path not in allowed roots: {fp}]"
    kind = _sniff(fp)
    try:
        if kind == ".pdf":
            return extract_pdf(fp, max_pages) or "[pdf had no extractable text, even with OCR]"
        if kind in TEXT_EXT:
            return fp.read_text(errors="ignore")
        if kind == ".docx":
            from docx import Document
            d = Document(str(fp))
            parts = [p.text for p in d.paragraphs if p.text.strip()]
            for t in d.tables:
                for row in t.rows:
                    parts.append(" | ".join(c.text for c in row.cells))
            return "\n".join(parts).strip() or "[docx had no text]"
        if kind in IMG_EXT:
            return ocr_image(fp) or "[OCR produced no text from the image]"
    except Exception as e:
        return f"[extract error: {e}]"
    return f"[unsupported type: {kind or 'unknown'}]"


def is_unreadable(text) -> Optional[str]:
    """Reason string if text is unusable, else None."""
    if text is None:
        return "no path given"
    t = text.strip()
    if t.startswith("[") and t.endswith("]") and "\n" not in t:
        return t.strip("[]")
    if len(t) < 40:
        return f"only {len(t)} characters of text were extracted"
    return None

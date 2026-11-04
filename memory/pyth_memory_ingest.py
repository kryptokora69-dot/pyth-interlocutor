"""
pyth_memory_ingest.py — Fixed Directory Ingest
"""

import os
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from memory.pyth_memory_store import add_document_summary
from tools.file_loader import load_document
import requests

OLLAMA_URL = "http://localhost:11434/api/chat"

def summarize_with_llm(text):
    prompt = f"""Detailed summary:

{text[:10000]}

Summary:"""
    try:
        r = requests.post(OLLAMA_URL, json={"model": "gemma4-12b:latest", "messages": [{"role": "user", "content": prompt}], "stream": False}, timeout=300)
        r.raise_for_status()
        return r.json()["message"]["content"].strip()
    except:
        return text[:1500] + "..."

def ingest_file(file_path):
    text = load_document(file_path)
    if not text:
        return False
    summary = summarize_with_llm(text)
    add_document_summary(file_path, summary)
    print(f"✅ {os.path.basename(file_path)}")
    return True

def ingest_directory(dir_path):
    count = 0
    for root, _, files in os.walk(dir_path):
        for file in files:
            if file.lower().endswith(('.pdf', '.txt', '.md')):
                full_path = os.path.join(root, file)
                if ingest_file(full_path):
                    count += 1
    print(f"✅ Ingested {count} documents.")

if __name__ == "__main__":
    ingest_directory(str(__import__("pathlib").Path(__file__).resolve().parents[1] / "memory"))

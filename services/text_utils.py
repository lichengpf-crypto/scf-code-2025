
# -*- coding: utf-8 -*-
import re, hashlib

_SPLIT_RE = re.compile(r"[\s,，、;；]+")

def parse_words(raw: str):
    if not raw: return []
    parts = _SPLIT_RE.split(raw)
    out, seen = [], set()
    for p in parts:
        w = p.strip()
        if not w or w in seen: continue
        seen.add(w)
        out.append(w)
    return out

def parse_dialogue(raw: str):
    if not raw: return []
    lines = [x.strip() for x in raw.splitlines() if x.strip()]
    out = []
    for i, line in enumerate(lines, 1):
        m = re.match(r"^\s*([A-Za-z])[:：]\s*(.+)$", line)
        if m:
            out.append({"id": f"d{i}", "type": "dialogue", "speaker": m.group(1), "text": m.group(2)})
        else:
            out.append({"id": f"d{i}", "type": "dialogue", "text": line})
    return out

def normalize_text(text: str):
    if text is None: return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"\s+", " ", t).strip()
    return t

def tts_cfg_string(tts: dict):
    keys = ["language","voice","format","rate","pitch","style"]
    vals = [str(tts.get(k, "")) for k in keys]
    return "|".join(vals)

def tts_sha1(text: str, tts: dict):
    norm = normalize_text(text)
    cfg = tts_cfg_string(tts or {})
    s = (cfg + "|" + norm).encode("utf-8")
    return hashlib.sha1(s).hexdigest()

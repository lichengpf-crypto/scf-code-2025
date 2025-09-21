# handlers/text_tools.py
# 统一文本工具：
# - POST /text/check_words  —— 单词拼写检查（词表 + 常见错拼 + 编辑距离 + 连写拆分）
# - POST /text/validate     —— 规范化 words & dialogue（空格/大小写/句末标点等），并给出拆分建议

import re
from difflib import get_close_matches
from handlers.common import ok, err
from services import db_index

# ===== 配置 =====
LEXICON_KEY = "db/lexicon/english_top70k.txt"  # 你已上传的 6-7 万词词表
SPLIT_MIN_LEN = 6      # 无分隔连写尝试拆分的最小长度
ED_MAX_SUGGEST = 3     # 编辑距离建议的最多返回数
# 注意：我们用 difflib 的近邻建议，默认相当于 ED≈1~2 的近邻，已足够“applee/orangee”

# ===== 常见错拼映射（命中则直接给出建议） =====
COMMON_MISSPELL = {
    "bananna": "banana",
    "recieve": "receive",
    "teh": "the",
    "acommodate": "accommodate",
    "accomodate": "accommodate",
    "adress": "address",
    "seperate": "separate",
    "definately": "definitely",
    "occured": "occurred",
    "occurence": "occurrence",
}

_WORD_PAT = re.compile(r"^[a-z][a-z\-']*$")  # 合法词（只含字母/连字符/撇号）
_ALNUM_PAT = re.compile(r"^[A-Za-z][A-Za-z\.\-']*$")  # 允许带点号（用于 needs_split）

# ===== 词表缓存 =====
_LEXICON = None  # set[str]，全小写
def _get_lexicon():
    """加载并缓存词表。优先 read_text；无该函数时回退 read_lines。"""
    global _LEXICON
    if _LEXICON is not None:
        return _LEXICON
    try:
        if hasattr(db_index, "read_text"):
            txt = db_index.read_text(LEXICON_KEY)
            lines = txt.splitlines()
        else:
            # 回退方案：用 read_lines 读取整文件；limit 放大到 20 万行以确保覆盖
            lines = db_index.read_lines(LEXICON_KEY, limit=200000)

        words = []
        pat = re.compile(r"^[a-z][a-z\-']*$")
        for ln in lines:
            w = str(ln).strip().lower()
            if w and pat.match(w):
                words.append(w)
        _LEXICON = set(words)
    except Exception:
        _LEXICON = set()
    return _LEXICON

def _diag():
    L = _get_lexicon()
    return {"lexicon_loaded": bool(L), "lexicon_size": len(L)}

# ===== 基础工具 =====
def _normalize_token(w: str) -> str:
    return (w or "").strip().lower()

def _illegal_chars(w: str) -> bool:
    # “非法字符”用于判错（不把点号当非法，用于 needs_split）
    return not _ALNUM_PAT.match(w)

def _suggest_by_ed(w: str, lexicon: set, n=ED_MAX_SUGGEST):
    # 用 difflib 找近邻；限制候选数量
    try:
        cand = get_close_matches(w, list(lexicon), n=n, cutoff=0.84)
        return cand
    except Exception:
        return []

def _try_split_by_dot(w: str, lexicon: set):
    # good.morning -> ["good morning"]，要求两边都在词表
    if "." not in w:
        return None
    parts = [p for p in w.split(".") if p]
    if len(parts) != 2:
        return None
    a, b = parts[0], parts[1]
    if a in lexicon and b in lexicon:
        return f"{a} {b}"
    return None

def _try_split_nodelim(w: str, lexicon: set):
    # 无分隔的连写拆分（长度阈值；两边都在词表）
    if len(w) < SPLIT_MIN_LEN:
        return None
    # 遍历可能的切分点（2..len-2），避免太短碎片
    for i in range(2, len(w)-1):
        a, b = w[:i], w[i:]
        if a in lexicon and b in lexicon:
            return f"{a} {b}"
    return None

# ===== /text/check_words =====
def check_words(event, tail, query, body):
    """
    入参: { "words": ["bananna","applee","good.morning","goodmorning"] }
    出参: {
      "results": [
        { "word": "applee", "ok": false, "reason": "edit_distance", "suggestions": ["apple", ...] },
        { "word": "bananna", "ok": false, "reason": "common_misspell", "suggestions": ["banana"] },
        { "word": "good.morning", "ok": false, "reason": "needs_split", "suggestions": ["good morning"] },
        { "word": "goodmorning", "ok": false, "reason": "needs_split_no_delim", "suggestions": ["good morning"] },
        ...
      ],
      "diag": { "lexicon_loaded": true, "lexicon_size": 68637 }
    }
    约定:
      - ok=True  : 明确正确
      - ok=False : 明确拼写存在问题（给出 reason/suggestions）
      - ok=None  : 未校对（词表未读到/异常），前端不得显示“全部正确”
    """
    try:
        words = (body or {}).get("words") or []
    except Exception:
        return ok({"results": [], "diag": _diag()})

    L = _get_lexicon()
    results = []

    for raw in words:
        w0 = raw if isinstance(raw, str) else str(raw)
        w = _normalize_token(w0)

        if not w:
            results.append({"word": w0, "ok": None, "suggestions": [], "reason": "empty"})
            continue

        # 1) 常见错拼（最优先）
        if w in COMMON_MISSPELL:
            results.append({
                "word": w0, "ok": False,
                "reason": "common_misspell",
                "suggestions": [COMMON_MISSPELL[w]]
            })
            continue

        # 2) 词表尚未加载成功 → 不判定，提示 unchecked
        if not L:
            results.append({"word": w0, "ok": None, "suggestions": [], "reason": "unchecked"})
            continue

        # 3) 合法性（允许点号：用于后续 needs_split）
        if _illegal_chars(w):
            # 非法字符：仍可给一点编辑距离建议（去掉非字母字符尝试）
            w_alpha = re.sub(r"[^a-z\-']+", "", w)
            sug = _suggest_by_ed(w_alpha, L) if w_alpha else []
            results.append({"word": w0, "ok": False, "reason": "illegal_chars", "suggestions": sug})
            continue

        # 4) 正确词（在词表中）
        if "." not in w and w in L:
            results.append({"word": w0, "ok": True, "suggestions": []})
            continue

        # 5) —— 顺序很关键：先“编辑距离”，再“无分隔拆分” —— #
        #    避免 applee -> app lee 的误判；应优先给 apple。
        sug_ed = _suggest_by_ed(w.replace(".", ""), L)
        if sug_ed:
            results.append({"word": w0, "ok": False, "reason": "edit_distance", "suggestions": sug_ed})
            continue

        # 6) 带点号的连写拆分（good.morning）
        if "." in w:
            split = _try_split_by_dot(w, L)
            if split:
                results.append({"word": w0, "ok": False, "reason": "needs_split", "suggestions": [split]})
                continue

        # 7) 无分隔连写拆分（goodmorning）
        split2 = _try_split_nodelim(w.replace(".", ""), L)
        if split2:
            results.append({"word": w0, "ok": False, "reason": "needs_split_no_delim", "suggestions": [split2]})
            continue

        # 8) 未命中任何规则：视为未校对/未知
        results.append({"word": w0, "ok": None, "suggestions": [], "reason": "unchecked"})

    return ok({"results": results, "diag": _diag()})

# ===== /text/validate ：统一规范化（words + dialogue）========
def _cap_sentence(s: str) -> str:
    if not s:
        return s
    # 句末标点后补空格（.?! 之后若无空格则补一个）
    s = re.sub(r"([.!?])(\S)", r"\1 \2", s)
    # 折叠多空格
    s = re.sub(r"\s+", " ", s).strip()
    # 首字母大写
    return s[:1].upper() + s[1:] if s else s

def _norm_speaker(spk: str) -> str:
    spk = (spk or "").strip()
    return spk[:1].upper() if spk else ""

def validate(event, tail, query, body):
    """
    入参：{ words: [..], dialogue: [{speaker?, text}...] }
    返回：{ ok, normalized: {words, dialogue}, issues: [...] }
    - 对话：说话人首字母大写；句末标点规范化；a.b 模式给拆分建议
    - words：仅去首尾空白，不做拼写判断（拼写交给 /text/check_words）
    """
    words = (body or {}).get("words") or []
    dialogue = (body or {}).get("dialogue") or []

    issues = []
    normalized_words = []
    for idx, w in enumerate(words):
        ww = ("" if w is None else str(w)).strip()
        if ww != w:
            issues.append({"field": f"words[{idx}]", "type": "trim", "from": w, "to": ww})
        normalized_words.append(ww)

    L = _get_lexicon()
    normalized_dialogue = []
    for i, d in enumerate(dialogue):
        spk0 = (d.get("speaker") or "").strip()
        spk = _norm_speaker(spk0)
        if spk != spk0:
            issues.append({"field": f"dialogue[{i}].speaker", "type": "speaker_cap", "from": spk0, "to": spk})

        txt0 = (d.get("text") or "").strip()
        # 拆分建议（a.b → a b），只提示不强行替换
        if "." in txt0:
            # 找出形如 “word.word” 的片段并建议拆分
            for m in re.finditer(r"\b([A-Za-z]+)\.([A-Za-z]+)\b", txt0):
                a, b = m.group(1).lower(), m.group(2).lower()
                if L and a in L and b in L:
                    issues.append({
                        "field": f"dialogue[{i}].text",
                        "type": "needs_split",
                        "from": f"{m.group(1)}.{m.group(2)}",
                        "to": f"{m.group(1)} {m.group(2)}",
                        "hint": "建议将连写单词用空格分开"
                    })

        txt = _cap_sentence(txt0)
        if txt != txt0:
            issues.append({
                "field": f"dialogue[{i}].text",
                "type": "normalize",
                "from": txt0,
                "to": txt,
                "hint": "句末标点后空格、首字母大写、折叠空格"
            })

        normalized_dialogue.append({"speaker": spk, "text": txt})

    return ok({
        "ok": True,
        "normalized": { "words": normalized_words, "dialogue": normalized_dialogue },
        "issues": issues
    })




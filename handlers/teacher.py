# handlers/teacher.py —— 老师端接口：TTS 预览 / 批量发布（词+对话）/ 列表 / 详情
# 说明：
# - 依赖 services.cos_client: tts_synthesize_cached(text, tts) -> cos_key
# - 依赖 services.db_index:  append_json_line, write_json, read_lines, read_json
# - 新增：_push_inbox_for_students() 将作业投递到 db/inbox/<sid>.ndjson
# - 在 publish_tts() 返回前，读取 body.target_students（可为空），并执行投递

import os, json, time, datetime
from handlers.common import ok, err
from services import db_index
from services import cos_client

# ========= 小工具 =========

def _iso_now():
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def _push_inbox_for_students(assignment: dict, target_students):
    """
    将作业投递到每个学生的收件箱：db/inbox/<student_id>.ndjson
    assignment: 必须至少包含 assignment_id/title/note/created_at/created_by
    target_students: list[str]，如 ["S001","S002"]
    """
    if not target_students:
        return
    rec = {
        "type": "assignment",
        "assignment_id": assignment.get("assignment_id"),
        "title": assignment.get("title", ""),
        "note": assignment.get("note", ""),
        "created_at": assignment.get("created_at"),
        "from": assignment.get("created_by"),
    }
    for sid in target_students:
        sid = (sid or "").strip()
        if not sid:
            continue
        try:
            db_index.append_json_line(f"db/inbox/{sid}.ndjson", rec)
        except Exception:
            # 不阻断主流程；按需打印日志
            pass

def _coerce_tts(body_tts: dict):
    """SAFE：统一 TTS 字段默认值，避免 None / 空串带来的坑"""
    t = body_tts or {}
    return {
        "language": t.get("language") or "en-GB",
        "voice":    t.get("voice")    or "en-GB-LibbyNeural",
        "rate":     t.get("rate")     or "+0%",
        "pitch":    t.get("pitch")    or "+0st",
        "style":    t.get("style")    or "",
        "format":   t.get("format")   or "mp3-16k",
    }

def _mk_assignment_id():
    """SAFE：可读 ID（周序号 + 时间低位）"""
    week = time.strftime("%G-W%V", time.gmtime())
    return f"a_{week}_{int(time.time()) & 0xfffffff:08x}"

# ========= 接口实现 =========

def tts_preview(event, tail, query, body):
    """
    POST /tts/preview
    入参：{ text, language, voice, rate, pitch, style, format }
    返回：{ ok:true, fileUrl:"/cos/resign/<cos_key>" }
    说明：前端会对 fileUrl 再 GET 一次换真实 COS 临时直链
    """
    if not body or not body.get("text"):
        return err("bad_request", "text required")

    text = (body.get("text") or "").strip()
    if not text:
        return err("bad_request", "text required")

    tts = _coerce_tts(body.get("language") and body or body.get("tts") or body)  # FIX：兼容两种传参形态

    try:
        cos_key = cos_client.tts_synthesize_cached(text, tts)
    except Exception as e:
        return err("tts_failed", f"{e}")

    resign_url = f"/cos/resign/{cos_key}"
    return ok({"ok": True, "fileUrl": resign_url})

def publish(event, tail, query, body):
    """
    POST /assignments/publish  —— 兼容旧接口（整段音频 audio_b64 发布）
    如你仍在用旧接口，请保留你原有实现；此处仅占位。
    """
    return err("not_implemented", "legacy /assignments/publish is not implemented here")

def publish_tts(event, tail, query, body):
    """
    POST /assignments/publish_tts
    入参：
      {
        teacher_id, title, note,
        words: ["apple","banana",...],
        dialogue: [{speaker?, text}, ...],
        tts: { language, voice, rate, pitch, style, format },
        target_students?: ["S001","S002", ...]
      }
    返回：
      { ok:true, assignment_id, items:[ {id,type,text,audio_cos_key,fileUrl}, ... ] }
    """
    if not body:
        return err("bad_request", "missing body")

    need = []
    teacher_id = (body.get("teacher_id") or "").strip()
    title      = (body.get("title") or "").strip()
    note       = body.get("note") or ""
    words      = body.get("words") or []
    dialogue   = body.get("dialogue") or []
    tts        = _coerce_tts(body.get("tts"))  # FIX：统一默认

    # SAFE：容错清洗
    words = [str(w).strip() for w in words if str(w or "").strip()]
    clean_dialogue = []
    for d in dialogue:
        if not isinstance(d, dict): 
            continue
        tx = (d.get("text") or "").strip()
        if not tx:
            continue
        obj = {"text": tx}
        spk = (d.get("speaker") or "").strip()
        if spk:
            obj["speaker"] = spk
        clean_dialogue.append(obj)
    dialogue = clean_dialogue

    if not teacher_id: need.append("teacher_id")
    if not title:      need.append("title")
    if not (words or dialogue): need.append("words_or_dialogue")
    if need:
        return err("bad_request", "missing fields", need=need)

    # 1) 组装条目（ID 连号）
    items = []
    for i, w in enumerate(words, 1):
        items.append({"id": f"w{i}", "type": "word", "text": str(w)})
    for i, d in enumerate(dialogue, 1):
        text = d.get("text", "")
        speaker = d.get("speaker")
        obj = {"id": f"d{i}", "type": "dialogue", "text": text}
        if speaker: obj["speaker"] = speaker
        items.append(obj)

    # 2) 针对每条生成/复用 TTS（带最小重试）
    out = []
    for it in items:
        try:
            cos_key = cos_client.tts_synthesize_cached(it["text"], tts)
        except Exception as e:
            # SAFE：个别失败也不中断整个发布；前端仍可看到失败项
            out.append({**it, "audio_cos_key": None, "fileUrl": None, "tts_error": str(e)})
            continue
        resign_url = f"/cos/resign/{cos_key}"
        out.append({**it, "audio_cos_key": cos_key, "fileUrl": resign_url})

    # 3) 写入“作业索引与详情”
    aid  = _mk_assignment_id()
    created_at = _iso_now()

    assignment = {
        "assignment_id": aid,
        "title": title,
        "note": note,
        "referenceText": "",
        "language": tts.get("language", "en-GB"),
        "created_at": created_at,
        "created_by": teacher_id,
        "itemsCount": len(out),
        "hasAudio": any(x.get("audio_cos_key") for x in out)
    }

    # 索引（简化版）
    db_index.append_json_line("db/assignments.ndjson", {
        "assignment_id": aid,
        "title": title,
        "note": note,
        "created_at": created_at,
        "created_by": teacher_id,
        "itemsCount": len(out),
        "hasAudio": assignment["hasAudio"]
    })

    # 详情
    db_index.write_json(f"db/assignments/{aid}.json", {
        "assignment": assignment,
        "items": out
    })

    # 4) 发布时“投递”到学生收件箱（若未显式传，尝试读取名册）
    target_students = (body.get("target_students") or [])
    if not target_students:
        # 兜底：若老师未传，尝试读取示例名册（最多 20 人）
        try:
            roster_path = f"db/roster/teachers/{teacher_id}.json"
            roster = db_index.read_json(roster_path)
            target_students = [str(s.get("student_id", "")).strip() 
                               for s in (roster.get("students") or []) if str(s.get("student_id", "")).strip()]
            target_students = target_students[:20]
        except Exception:
            target_students = []

    _push_inbox_for_students(assignment, target_students)

    return ok({"ok": True, "assignment_id": aid, "items": out})

def list_assignments(event, tail, query, body):
    """
    GET /assignments/list?teacher_id=T001
    返回：{ ok:true, items:[...], nextCursor:null }
    """
    teacher_id = (query.get("teacher_id") if query else "") or ""
    try:
        lines = db_index.read_lines("db/assignments.ndjson", limit=500)  # SAFE：上限 500
    except Exception:
        lines = []
    items = []
    for ln in lines:
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        if teacher_id and obj.get("created_by") != teacher_id:
            continue
        items.append(obj)
    items = list(reversed(items))  # 最近的在前
    return ok({"ok": True, "items": items, "nextCursor": None})

def get_assignment(event, tail, query, body):
    """
    GET /assignments/get/<id>
    返回：{ ok:true, assignment:{...}, items:[...]} 或 { ok:false, error:"not_found" }
    """
    aid = (tail or "").strip("/").strip()
    if not aid:
        return err("bad_request", "assignment_id required in path")
    try:
        data = db_index.read_json(f"db/assignments/{aid}.json")
    except Exception:
        return err("not_found", f"assignment {aid} not found")  # FIX：总是带 msg，避免参数错误
    return ok(data)

def list_submissions(event, tail, query, body):
    """
    GET /submissions/list?assignment_id=... 或 ?teacher_id=...
    简易占位：按需扩展
    """
    return ok({"ok": True, "items": [], "nextCursor": None})

def get_submission(event, tail, query, body):
    """
    GET /submissions/get/<id>
    简易占位：按需扩展
    """
    sid = (tail or "").strip("/")
    if not sid:
        return err("bad_request", "submission_id required in path")
    return err("not_found", f"submission {sid} not found")

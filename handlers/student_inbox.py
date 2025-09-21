# handlers/student_inbox.py
# 简易收件箱：db/inbox/<student_id>.ndjson
import json
from handlers.common import ok, err
from services import db_index

INBOX_DIR = "db/inbox"

def list_inbox(event, tail, query, body):
    student_id = (query.get("student_id") if query else None) or ""
    limit = int((query.get("limit") if query else 20) or 20)
    if not student_id:
        return err("bad_request", "student_id required")
    path = f"{INBOX_DIR}/{student_id}.ndjson"
    try:
        lines = db_index.read_lines(path, limit=limit)
    except Exception:
        lines = []
    items = [json.loads(x) for x in lines if x.strip()]
    return ok({"items": items, "nextCursor": None})

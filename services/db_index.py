# services/db_index.py
# 轻量级 COS 上的 JSON/NDJSON 读写工具
# 依赖 services.cos_client 提供的：
#   - get_text(key) / put_text(key, text, content_type?)
#   - get_bytes(key) / put_bytes(key, bytes, content_type?)
#   - cos_exists(key)

import json
from typing import List, Optional

from services.cos_client import (
    get_text, put_text, get_bytes, put_bytes, cos_exists
)

# ========== 基础 JSON ==========

def write_json(key: str, data) -> None:
    """
    将对象序列化为 JSON 并写入 COS。
    """
    text = json.dumps(data, ensure_ascii=False)
    put_text(key, text, content_type="application/json")

def read_json(key: str):
    """
    从 COS 读取 JSON 并反序列化。
    """
    text = get_text(key)
    return json.loads(text)

# ========== NDJSON（逐行 JSON） ==========

def append_json_line(key: str, record: dict) -> None:
    """
    读取旧内容 + 追加一行 JSON + 回写。
    A 阶段并发低，直接全量回写即可。
    """
    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        old = get_text(key)
        new_text = old + line
    except Exception:
        # 文件不存在或读取失败时，从空开始
        new_text = line
    put_text(key, new_text, content_type="application/x-ndjson")

def read_lines(key: str, limit: Optional[int] = None) -> List[str]:
    """
    读取 NDJSON 文本，返回行列表（原样字符串）。
    可选 limit：返回最后 N 行。
    """
    try:
        text = get_text(key)
    except Exception:
        return []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if limit and limit > 0:
        return lines[-limit:]
    return lines

def upsert_json_line(key: str, id_field: str, id_value: str, updater) -> None:
    """
    读取 NDJSON → 查找 id_field=id_value 的对象 → 调用 updater(it) 修改/补充 →
    全量回写（不存在则新增）。
    """
    # 读取
    try:
        text = get_text(key)
        rows = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
    except Exception:
        rows = []

    found = False
    for it in rows:
        if it.get(id_field) == id_value:
            updater(it)
            found = True
            break
    if not found:
        it = {id_field: id_value}
        updater(it)
        rows.append(it)

    # 回写
    new_text = "\n".join(json.dumps(x, ensure_ascii=False) for x in rows) + "\n"
    put_text(key, new_text, content_type="application/x-ndjson")

# ====== 与你旧工具兼容的别名（如旧代码使用 ndjson_* 命名） ======
def ndjson_append(key: str, record: dict) -> None:
    append_json_line(key, record)

def ndjson_all(key: str):
    try:
        text = get_text(key)
        return [json.loads(ln) for ln in text.splitlines() if ln.strip()]
    except Exception:
        return []

def ndjson_upsert(key: str, id_field: str, id_value: str, updater) -> None:
    upsert_json_line(key, id_field, id_value, updater)

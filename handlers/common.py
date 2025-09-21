# handlers/common.py —— CORS/JSON 基础响应（后续复用）
import json

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Expose-Headers": "*",
}

def ok(payload, code=200):
    return {"statusCode": code, "headers": CORS, "body": json.dumps(payload, ensure_ascii=False)}

def err(code, err, msg, need=None):
    body = {"ok": False, "error": err, "message": msg}
    if need:
        body["need"] = need
    return {"statusCode": code, "headers": CORS, "body": json.dumps(body, ensure_ascii=False)}

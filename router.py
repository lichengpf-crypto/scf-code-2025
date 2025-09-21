# router.py —— 轻量路由：支持 method + 前缀匹配；采用“最长前缀优先”
import json

def _parse_query(event):
    qs = event.get("queryString") or event.get("queryStringParameters") or {}
    if isinstance(qs, dict):
        return {k: (v if v is not None else "") for k, v in qs.items()}
    return {}

def _parse_body(event):
    body = event.get("body")
    if not body:
        return None
    if isinstance(body, dict):
        return body
    try:
        return json.loads(body)
    except Exception:
        return {"_raw": body}

def route_with_fallback(event, context, routes, legacy_handler):
    method = (event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method") or "GET").upper()
    path = event.get("path") or event.get("requestContext", {}).get("path") or "/"

    # “最长前缀优先”筛选候选
    candidates = [(m, p, h) for (m, p, h) in routes if m == method and path.startswith(p)]
    if candidates:
        # 选前缀最长的那个
        m, p, handler = sorted(candidates, key=lambda x: len(x[1]), reverse=True)[0]
        tail = path[len(p):]  # 去掉前缀后的尾巴（可能为空或以 / 开头）
        query = _parse_query(event)
        body = _parse_body(event)
        try:
            return handler(event, tail, query, body)
        except Exception as e:
            return {
                "statusCode": 500,
                "headers": {"Content-Type": "application/json; charset=utf-8"},
                "body": json.dumps({"ok": False, "error": "handler_error", "message": str(e)})
            }

    # 回退到旧入口
    return legacy_handler(event, context)

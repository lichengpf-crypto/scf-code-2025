# handlers/student.py
# 学生端/通用接口 Handler（已包含：/ping, /cos/info, /cos/test,
# /submissions/create, /submissions/upload_image, /score/run, /results/<id>）
import os
import time
from handlers.common import ok, err

# /ping —— 健康检查 + 版本号
API_VERSION = os.getenv("API_VERSION", "2025-09-15-1")
def ping(event, tail, query, body):
    # router: "new" 方便确认走到新路由；后续需要可删
    return ok({"ok": True, "ver": API_VERSION, "time": int(time.time()), "router": "new"})

# /cos/info —— 返回 COS 基本配置（仅读环境变量）
def cos_info(event, tail, query, body):
    """
    建议在 SCF 环境变量里设置：
      COS_BUCKET=eng-homework-1374188029
      COS_REGION=ap-beijing
      COS_TEMP_CRED=true  (如函数使用临时凭证)
      ALLOWED_RESIGN_PREFIXES=tts/,submissions/,db/
    """
    bucket = os.getenv("COS_BUCKET", "")
    region = os.getenv("COS_REGION", "ap-beijing")
    temp_cred = os.getenv("COS_TEMP_CRED", "").lower() in ("1", "true", "yes")
    allowed = os.getenv("ALLOWED_RESIGN_PREFIXES", "tts/,submissions/,db/")

    if not bucket:
        return err(500, "cos_not_configured", "COS_BUCKET is not set",
                   need=["COS_BUCKET", "COS_REGION"])

    return ok({
        "ok": True,
        "bucket": bucket,
        "region": region,
        "useTempCredential": temp_cred,
        "allowedResignPrefixes": [p.strip() for p in allowed.split(",") if p.strip()],
    })

# /cos/test —— 代理到 legacy（保持行为 100% 一致）
def cos_test(event, tail, query, body):
    try:
        import index_legacy
    except Exception as e:
        return err(500, "legacy_missing", f"index_legacy not importable: {e}")
    return index_legacy.main_handler(event, None)

# /submissions/create —— 代理到 legacy
def create_submission(event, tail, query, body):
    try:
        import index_legacy
    except Exception as e:
        return err(500, "legacy_missing", f"index_legacy not importable: {e}")
    return index_legacy.main_handler(event, None)

# /submissions/upload_image —— 代理到 legacy
def upload_images(event, tail, query, body):
    try:
        import index_legacy
    except Exception as e:
        return err(500, "legacy_missing", f"index_legacy not importable: {e}")
    return index_legacy.main_handler(event, None)

# /score/run —— 代理到 legacy
def score_run(event, tail, query, body):
    try:
        import index_legacy
    except Exception as e:
        return err(500, "legacy_missing", f"index_legacy not importable: {e}")
    return index_legacy.main_handler(event, None)

# /results/<submission_id> —— 代理到 legacy
def get_result(event, tail, query, body):
    try:
        import index_legacy
    except Exception as e:
        return err(500, "legacy_missing", f"index_legacy not importable: {e}")
    return index_legacy.main_handler(event, None)

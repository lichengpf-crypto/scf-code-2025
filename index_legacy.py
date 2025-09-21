# index.py — SCF 路由：TTS发布 / 学生提交(Base64) / 图片上传 / 评分(含STT失败友好) / 查询结果 / 重签URL / 诊断
import os, sys, json, base64, time, urllib.request, urllib.parse, hashlib, uuid, datetime, re

# 依赖与 index.py 同层时确保可 import
sys.path.insert(0, os.path.dirname(__file__))

from qcloud_cos import CosConfig, CosS3Client

# ========= 配置 / CORS =========
ALLOW_ORIGIN   = "*"
MAX_UPLOAD_MB  = int(os.environ.get("MAX_UPLOAD_MB", "2"))  # Base64 提交体积上限（MB）
API_TOKEN      = os.environ.get("API_TOKEN")                # 若设置，要求 Authorization: Bearer <token>
DEFAULT_VOICE  = os.environ.get("DEFAULT_VOICE", "en-US-JennyNeural")
DEFAULT_LANG   = os.environ.get("DEFAULT_LANG", "en-US")    # STT 默认语言
RESIGN_EXPIRES = int(os.environ.get("RESIGN_EXPIRES", "3600"))  # /cos/resign 链接有效期（秒）
# 仅允许这些前缀的对象被重签，防止任意对象重签
ALLOWED_RESIGN_PREFIXES = [p.strip() for p in os.environ.get("ALLOWED_RESIGN_PREFIXES", "tts/,submissions/,db/").split(",") if p.strip()]

VERSION = os.environ.get("API_VERSION", "2025-09-15-1")

def resp(status, data):
    return {
        "isBase64Encoded": False,
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json; charset=utf-8",
            "Access-Control-Allow-Origin": ALLOW_ORIGIN,
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Expose-Headers": "*",
        },
        "body": json.dumps(data, ensure_ascii=False),
    }

def parse_json_body(event):
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    try:
        return json.loads(body)
    except Exception:
        return {}

def bearer_ok(event):
    """若配置了 API_TOKEN，则校验 Authorization: Bearer <token>。未配置则放行。"""
    if not API_TOKEN:
        return True
    headers = event.get("headers") or {}
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    return auth.strip() == f"Bearer {API_TOKEN}"

# ========= Azure TTS =========
def tts_azure(text, voice=DEFAULT_VOICE, fmt="audio-16khz-32kbitrate-mono-mp3"):
    key = os.environ.get("SPEECH_KEY")
    region = os.environ.get("SPEECH_REGION", "eastasia")
    if not key:
        raise RuntimeError("SPEECH_KEY missing")

    url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
    ssml = f"""<speak version='1.0' xml:lang='{DEFAULT_LANG}'>
  <voice name='{voice}'>{text}</voice>
</speak>""".encode("utf-8")

    req = urllib.request.Request(url, data=ssml, method="POST")
    req.add_header("Ocp-Apim-Subscription-Key", key)
    req.add_header("Content-Type", "application/ssml+xml")
    req.add_header("X-Microsoft-OutputFormat", fmt)
    with urllib.request.urlopen(req, timeout=20) as r:
        audio_bytes = r.read()
    return base64.b64encode(audio_bytes).decode("ascii")

# ========= Azure STT（短音频同步识别，REST）=========
def stt_azure_bytes(audio_bytes: bytes, language: str = DEFAULT_LANG, content_type: str = "audio/mpeg"):
    """
    使用 Conversation 识别 REST，同步短音频。
    content_type 可为 'audio/mpeg'（mp3）或 'audio/wav; codecs=audio/pcm; samplerate=16000'
    """
    key = os.environ.get("SPEECH_KEY")
    region = os.environ.get("SPEECH_REGION", "eastasia")
    if not key:
        raise RuntimeError("SPEECH_KEY missing")

    q = urllib.parse.urlencode({"language": language, "format": "simple"})
    url = f"https://{region}.stt.speech.microsoft.com/speech/recognition/conversation/cognitiveservices/v1?{q}"

    req = urllib.request.Request(url, data=audio_bytes, method="POST")
    req.add_header("Ocp-Apim-Subscription-Key", key)
    req.add_header("Content-Type", content_type)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode("utf-8", "ignore")
    try:
        js = json.loads(raw)
    except Exception:
        return raw.strip()
    status = (js.get("RecognitionStatus") or "").lower()
    if status != "success":
        return ""  # 触发 stt_failed 分支
    txt = js.get("DisplayText") or js.get("Text") or ""
    return (txt or "").strip()

# ========= COS 客户端 =========
_REGION = os.environ.get("COS_REGION", "ap-beijing")
_BUCKET = os.environ.get("COS_BUCKET")  # 必须 {bucketname}-{appid}

_SECRET_ID = os.environ.get("TENCENTCLOUD_SECRETID")
_SECRET_KEY = os.environ.get("TENCENTCLOUD_SECRETKEY")
_TOKEN     = os.environ.get("TENCENTCLOUD_SESSIONTOKEN")  # 执行角色会注入

_cfg = CosConfig(
    Region=_REGION,
    SecretId=_SECRET_ID,
    SecretKey=_SECRET_KEY,
    Token=_TOKEN,
    Scheme="https",
)
_cos = CosS3Client(_cfg)

def cos_exists(key: str) -> bool:
    try:
        _cos.head_object(Bucket=_BUCKET, Key=key)
        return True
    except Exception:
        return False

def put_cos_bytes(key: str, blob: bytes, content_type: str = None):
    if content_type:
        _cos.put_object(Bucket=_BUCKET, Key=key, Body=blob, ContentType=content_type)
    else:
        _cos.put_object(Bucket=_BUCKET, Key=key, Body=blob)

def put_cos_text(key: str, text: str, content_type: str = "application/json"):
    _cos.put_object(Bucket=_BUCKET, Key=key, Body=text.encode("utf-8"), ContentType=content_type)

def get_cos_bytes(key: str) -> bytes:
    obj = _cos.get_object(Bucket=_BUCKET, Key=key)
    return obj["Body"].get_raw_stream().read()

def sign_url(key: str, expires: int = 3600) -> str:
    # 先拿到基础预签名
    url = _cos.get_presigned_url(
        Method="GET",
        Bucket=_BUCKET,
        Key=key,
        Expired=expires,
    )
    # 若是临时凭证，把 token 作为查询参数拼上（腾讯 COS 要求）
    if _TOKEN:
        sep = '&' if ('?' in url) else '?'
        url = f"{url}{sep}x-cos-security-token={urllib.parse.quote(_TOKEN)}"
    return url

# ========= 工具函数：TTS 去重 / 路径规划 / ndjson 简单存取 =========
def _norm_text(t: str) -> str:
    return " ".join((t or "").strip().split()).lower()

def tts_fingerprint(text: str, voice=DEFAULT_VOICE, fmt="mp3-16k"):
    raw = f"{_norm_text(text)}|{voice}|{fmt}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()

def tts_cos_key(text: str, voice=DEFAULT_VOICE, fmt="mp3-16k"):
    fp = tts_fingerprint(text, voice, fmt)
    return f"tts/{DEFAULT_LANG}/{voice}/{fmt}/{fp}.mp3"

def week_prefix(student_id: str):
    y, w, _ = datetime.datetime.utcnow().isocalendar()
    return f"submissions/{student_id}/{y}-W{w:02d}/"

def ndjson_append(key: str, record: dict):
    try:
        old = get_cos_bytes(key)
        buf = old + (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    except Exception:
        buf = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    put_cos_bytes(key, buf, content_type="application/x-ndjson")

def ndjson_all(key: str):
    try:
        raw = get_cos_bytes(key).decode("utf-8", "ignore")
        return [json.loads(line) for line in raw.splitlines() if line.strip()]
    except Exception:
        return []

def ndjson_upsert(key: str, id_field: str, id_value: str, updater):
    items = ndjson_all(key)
    found = False
    for it in items:
        if it.get(id_field) == id_value:
            updater(it)
            found = True
            break
    if not found:
        it = {id_field: id_value}
        updater(it)
        items.append(it)
    text = "\n".join(json.dumps(x, ensure_ascii=False) for x in items) + "\n"
    put_cos_text(key, text, content_type="application/x-ndjson")

# ========= 文本对齐与打分（基线 WER）=========
_word_re = re.compile(r"[A-Za-z']+")

def tokenize_en(s: str):
    return [m.group(0).lower() for m in _word_re.finditer(s or "")]

def levenshtein_align(ref_words, hyp_words):
    """返回 (ops, N,S,D,I)"""
    n, m = len(ref_words), len(hyp_words)
    dp = [[0]*(m+1) for _ in range(n+1)]
    bt = [[None]*(m+1) for _ in range(n+1)]
    for i in range(1, n+1):
        dp[i][0] = i; bt[i][0] = 'D'
    for j in range(1, m+1):
        dp[0][j] = j; bt[0][j] = 'I'
    for i in range(1, n+1):
        for j in range(1, m+1):
            cost = 0 if ref_words[i-1] == hyp_words[j-1] else 1
            choices = [
                (dp[i-1][j-1] + cost, 'N' if cost==0 else 'S'),
                (dp[i-1][j] + 1, 'D'),
                (dp[i][j-1] + 1, 'I'),
            ]
            dp[i][j], bt[i][j] = min(choices, key=lambda x: x[0])
    ops = []
    i, j = n, m
    N = S = D = I = 0
    while i>0 or j>0:
        op = bt[i][j]
        if op == 'N' or op == 'S':
            rw = ref_words[i-1] if i>0 else None
            hw = hyp_words[j-1] if j>0 else None
            ops.append({"ref": rw, "hyp": hw, "op": op})
            if op == 'N': N += 1
            else: S += 1
            i -= 1; j -= 1
        elif op == 'D':
            rw = ref_words[i-1] if i>0 else None
            ops.append({"ref": rw, "hyp": None, "op": "D"})
            D += 1
            i -= 1
        elif op == 'I':
            hw = hyp_words[j-1] if j>0 else None
            ops.append({"ref": None, "hyp": hw, "op": "I"})
            I += 1
            j -= 1
        else:
            break
    ops.reverse()
    return ops, N, S, D, I

def score_from_alignment(N, S, D, I):
    denom = max(1, N + S + D)  # 以参考词数为基数
    wer = (S + D + I) / denom
    overall = max(0, 1.0 - wer) * 100.0
    overall = round(overall)
    return {
        "overall": overall,
        "accuracy": overall,
        "fluency": overall,
        "pronunciation": overall
    }, wer

# ========= resign 辅助：从多来源解析 key =========
def _extract_resign_key(event, path: str):
    """按优先级提取 key：
       1) 路径 /cos/resign/<key...>（URL 解码）
       2) queryStringParameters.key
       3) queryString/rawQueryString（字符串）解析
    """
    if path.startswith("/cos/resign/"):
        tail = path[len("/cos/resign/"):]
        try:
            tail = urllib.parse.unquote(tail)
        except Exception:
            pass
        if tail:
            return tail
    qsp = event.get("queryStringParameters") or {}
    if isinstance(qsp, dict):
        k = (qsp.get("key") or "").strip()
        if k:
            return k
    raw_q = event.get("queryString") or event.get("rawQueryString") or ""
    if isinstance(raw_q, str) and raw_q:
        parsed = urllib.parse.parse_qs(raw_q, keep_blank_values=True)
        vals = parsed.get("key")
        if vals and len(vals) > 0 and (vals[0] or "").strip():
            return (vals[0] or "").strip()
    return ""

# ========= 路由 =========
def route(event):
    path = (event.get("path") or "/").rstrip("/")
    method = (event.get("httpMethod") or "GET").upper()

    # CORS 预检
    if method == "OPTIONS":
        return resp(200, {"ok": True})

    # 心跳
    if path == "/ping" and method == "GET":
        return resp(200, {"ok": True, "ver": VERSION})

    # 诊断：查看 COS 配置
    if path == "/cos/info" and method == "GET":
        return resp(200, {
            "bucket": _BUCKET,
            "region": _REGION,
            "has_secret": bool(_SECRET_ID),
            "has_token": bool(_TOKEN),
        })

    # COS 自测（返回带 token 的可下载链接）
    if path == "/cos/test" and method == "GET":
        try:
            if not _BUCKET:
                return resp(500, {"ok": False, "error": "COS_BUCKET env missing"})
            key = f"hello-{int(time.time())}.txt"
            _cos.put_object(Bucket=_BUCKET, Key=key, Body=b"hello cos", ContentType="text/plain")
            url = sign_url(key, expires=600)
            return resp(200, {"ok": True, "key": key, "fileUrl": url})
        except Exception as e:
            return resp(500, {"ok": False, "error": str(e)})

    # 老师：预览 TTS（不落 COS，返回 base64）
    if path == "/assignments/create" and method == "POST":
        try:
            data = parse_json_body(event)
            text = (data.get("text") or "").strip()
            voice = (data.get("voice") or DEFAULT_VOICE).strip()
            if not text:
                return resp(400, {"ok": False, "error": "text_required"})
            audio_b64 = tts_azure(text, voice=voice)
            return resp(200, {
                "ok": True,
                "assignment": {
                    "referenceText": text,
                    "provider": "azure-tts",
                    "version": "basic-tts-v1",
                    "format": "mp3-16khz-mono",
                    "voice": voice,
                    "audio_b64": audio_b64
                }
            })
        except Exception as e:
            return resp(500, {"ok": False, "error": str(e)})

    # 老师：确认发布 → 若 COS 无该音频则落盘 → 返回签名 URL
    if path == "/assignments/publish" and method == "POST":
        try:
            if not bearer_ok(event):
                return resp(401, {"ok": False, "error": "unauthorized"})
            data = parse_json_body(event)
            text  = (data.get("text") or "").strip()
            voice = (data.get("voice") or DEFAULT_VOICE).strip()
            fmt   = (data.get("format") or "mp3-16k").strip()  # 仅用于路径命名
            if not text:
                return resp(400, {"ok": False, "error": "text_required"})

            key = tts_cos_key(text, voice, fmt)
            if not cos_exists(key):
                audio_b64 = tts_azure(text, voice=voice, fmt="audio-16khz-32kbitrate-mono-mp3")
                put_cos_bytes(key, base64.b64decode(audio_b64), content_type="audio/mpeg")

            url = sign_url(key, expires=3600)  # 1 小时有效
            return resp(200, {
                "ok": True,
                "assignment": {
                    "referenceText": text,
                    "voice": voice,
                    "format": fmt,
                    "cos_key": key,
                    "fileUrl": url
                }
            })
        except Exception as e:
            return resp(500, {"ok": False, "error": str(e)})

    # 学生：提交作业（音频 Base64）→ COS → pending
    if path == "/submissions/create" and method == "POST":
        try:
            if not bearer_ok(event):
                return resp(401, {"ok": False, "error": "unauthorized"})
            data = parse_json_body(event)
            assignment_id = (data.get("assignment_id") or "").strip()
            student_id    = (data.get("student_id") or "").strip()
            audio_b64     = data.get("audio_b64")

            need = []
            if not assignment_id: need.append("assignment_id")
            if not student_id:    need.append("student_id")
            if not audio_b64:     need.append("audio_b64")
            if need:
                return resp(400, {"ok": False, "error": "missing_fields", "need": need})

            # 体积限制（Base64 约 +33%，用长度估算）
            raw_len = len(audio_b64 or "")
            approx_bytes = int(raw_len * 3 / 4)
            if approx_bytes > MAX_UPLOAD_MB * 1024 * 1024:
                return resp(413, {"ok": False, "error": "too_large", "limit_mb": MAX_UPLOAD_MB})

            submission_id = data.get("submission_id") or uuid.uuid4().hex[:12]
            # 按扩展名推断 content-type，默认 mp3；前端若传 wav 建议扩展名 .wav
            ext = ".mp3"
            dst_key = week_prefix(student_id) + f"{submission_id}{ext}"
            put_cos_bytes(dst_key, base64.b64decode(audio_b64), content_type="audio/mpeg")

            # 记录 meta（A 阶段：COS ndjson）
            meta_key = "db/submissions.ndjson"
            record = {
                "id": submission_id,
                "student_id": student_id,
                "assignment_id": assignment_id,
                "cos_key": dst_key,
                "status": "pending",
                "created_at": datetime.datetime.utcnow().isoformat() + "Z"
            }
            ndjson_append(meta_key, record)

            return resp(200, {"ok": True, "submission_id": submission_id,
                              "status": "pending", "cos_key": dst_key})
        except Exception as e:
            return resp(500, {"ok": False, "error": str(e)})

    # 学生：提交图片（Base64 列表）→ COS；不评分
    if path == "/submissions/upload_image" and method == "POST":
        try:
            if not bearer_ok(event):
                return resp(401, {"ok": False, "error": "unauthorized"})

            data = parse_json_body(event)
            assignment_id = (data.get("assignment_id") or "").strip()
            student_id    = (data.get("student_id") or "").strip()
            images        = data.get("images") or []

            need = []
            if not assignment_id: need.append("assignment_id")
            if not student_id:    need.append("student_id")
            if not images:        need.append("images[]")
            if need:
                return resp(400, {"ok": False, "error": "missing_fields", "need": need})

            submission_id = (data.get("submission_id") or uuid.uuid4().hex[:12])
            base_prefix   = f"{week_prefix(student_id)}images/{submission_id}/"

            saved = []
            for i, item in enumerate(images):
                fn   = (item.get("filename") or f"img_{i+1}.jpg")
                b64  = item.get("image_b64") or ""
                mime = (item.get("mime") or "").strip()
                if not b64:
                    return resp(400, {"ok": False, "error": "missing_fields", "need": [f"images[{i}].image_b64"]})

                try:
                    blob = base64.b64decode(b64, validate=True)
                except Exception:
                    return resp(400, {"ok": False, "error": "bad_base64", "index": i})

                if len(blob) > MAX_UPLOAD_MB * 1024 * 1024:
                    return resp(413, {"ok": False, "error": "too_large", "limit_mb": MAX_UPLOAD_MB, "index": i})

                key = base_prefix + fn
                ct  = mime if mime else ("image/png" if fn.lower().endswith(".png")
                                         else "image/jpeg" if fn.lower().endswith((".jpg",".jpeg"))
                                         else None)
                put_cos_bytes(key, blob, content_type=ct)
                saved.append({"filename": fn, "cos_key": key, "size_bytes": len(blob)})

            meta_key = "db/submissions_images.ndjson"
            ndjson_append(meta_key, {
                "submission_id": submission_id,
                "student_id": student_id,
                "assignment_id": assignment_id,
                "count": len(saved),
                "prefix": base_prefix,
                "created_at": datetime.datetime.utcnow().isoformat() + "Z"
            })

            return resp(200, {"ok": True, "submission_id": submission_id, "saved": saved})
        except Exception as e:
            return resp(500, {"ok": False, "error": str(e)})

    # 评分：按 submission_id + referenceText 进行识别与对齐
    if path == "/score/run" and method == "POST":
        try:
            if not bearer_ok(event):
                return resp(401, {"ok": False, "error": "unauthorized"})
            data = parse_json_body(event)
            submission_id  = (data.get("submission_id") or "").strip()
            reference_text = (data.get("referenceText") or "").strip()
            language       = (data.get("language") or DEFAULT_LANG).strip()

            if not (submission_id and reference_text):
                return resp(400, {"ok": False, "error": "missing_fields",
                                  "need": ["submission_id","referenceText"]})

            # 找到提交记录
            subs = ndjson_all("db/submissions.ndjson")
            found = next((x for x in subs if x.get("id")==submission_id), None)
            if not found:
                return resp(404, {"ok": False, "error": "submission_not_found"})
            cos_key = found.get("cos_key")
            if not cos_key or not cos_exists(cos_key):
                return resp(404, {"ok": False, "error": "audio_not_found"})

            audio_bytes = get_cos_bytes(cos_key)
            content_type = "audio/mpeg" if cos_key.lower().endswith(".mp3") else "audio/wav"

            try:
                recognized = stt_azure_bytes(audio_bytes, language=language, content_type=content_type)
            except Exception:
                recognized = ""  # 触发 stt_failed

            # STT 失败友好分支
            if not recognized:
                result = {
                    "provider": "azure-s2t",
                    "version": "scoring-v1",
                    "recognizedText": "",
                    "referenceText": reference_text,
                    "scores": {},
                    "alignment": {"words": []},
                    "analysis": {"N": 0, "S": 0, "D": 0, "I": 0, "WER": None},
                    "submission_id": submission_id,
                    "language": language,
                    "scored_at": datetime.datetime.utcnow().isoformat() + "Z",
                    "status": "stt_failed",
                    "error": "no_speech_or_invalid_audio"
                }
                res_key = f"db/results/{submission_id}.json"
                put_cos_text(res_key, json.dumps(result, ensure_ascii=False))
                ndjson_append("db/results.ndjson", {"submission_id": submission_id, "result_key": res_key,
                                                    "overall": None, "scored_at": result["scored_at"], "status":"stt_failed"})
                def _upd_fail(it):
                    if it.get("id")==submission_id:
                        it["status"]="stt_failed"
                        it["result_key"]=res_key
                ndjson_upsert("db/submissions.ndjson", "id", submission_id, _upd_fail)
                return resp(200, {"ok": True, "status": "stt_failed", "submission_id": submission_id,
                                  "result_key": res_key, "result": result})

            # 正常对齐打分
            ref_words = tokenize_en(reference_text)
            hyp_words = tokenize_en(recognized)
            ops, N, S, D, I = levenshtein_align(ref_words, hyp_words)
            scores, wer = score_from_alignment(N, S, D, I)

            result = {
                "provider": "azure-s2t",
                "version": "scoring-v1",
                "recognizedText": recognized,
                "referenceText": reference_text,
                "scores": scores,
                "alignment": {"words": ops},
                "analysis": {"N": N, "S": S, "D": D, "I": I, "WER": round(wer, 4)},
                "submission_id": submission_id,
                "language": language,
                "scored_at": datetime.datetime.utcnow().isoformat() + "Z",
                "status": "scored"
            }

            res_key = f"db/results/{submission_id}.json"
            put_cos_text(res_key, json.dumps(result, ensure_ascii=False))
            ndjson_append("db/results.ndjson", {"submission_id": submission_id, "result_key": res_key,
                                                "overall": scores["overall"], "scored_at": result["scored_at"], "status":"scored"})
            def _upd_ok(it):
                if it.get("id")==submission_id:
                    it["status"]="scored"
                    it["result_key"]=res_key
            ndjson_upsert("db/submissions.ndjson", "id", submission_id, _upd_ok)

            return resp(200, {"ok": True, "status": "scored", "submission_id": submission_id,
                              "result_key": res_key, "result": result})
        except Exception as e:
            return resp(500, {"ok": False, "error": str(e)})

    # 查询结果：支持 /results/<submission_id>
    if path.startswith("/results/") and method == "GET":
        try:
            submission_id = path.split("/")[-1]
            res_key = f"db/results/{submission_id}.json"
            if not cos_exists(res_key):
                subs = ndjson_all("db/submissions.ndjson")
                found = next((x for x in subs if x.get("id")==submission_id), None)
                status = found.get("status") if found else "unknown"
                return resp(200, {"ok": True, "status": status, "submission_id": submission_id})
            raw = get_cos_bytes(res_key).decode("utf-8", "ignore")
            data = json.loads(raw)
            return resp(200, {"ok": True, "status": data.get("status","scored"), "submission_id": submission_id, "result": data})
        except Exception as e:
            return resp(500, {"ok": False, "error": str(e)})

    # 重新签名：支持两种形态
    #   1) GET /cos/resign/<key...>   （推荐）
    #   2) GET /cos/resign?key=<key>
    if (path == "/cos/resign" or path.startswith("/cos/resign/")) and method == "GET":
        try:
            if not bearer_ok(event):
                return resp(401, {"ok": False, "error": "unauthorized"})
            key = _extract_resign_key(event, path)
            if not key:
                return resp(400, {"ok": False, "error": "key_required"})
            if not any(key.startswith(p) for p in ALLOWED_RESIGN_PREFIXES):
                return resp(403, {"ok": False, "error": "prefix_not_allowed", "allowed": ALLOWED_RESIGN_PREFIXES})
            if not cos_exists(key):
                return resp(404, {"ok": False, "error": "object_not_found"})
            url = sign_url(key, expires=RESIGN_EXPIRES)
            return resp(200, {"ok": True, "key": key, "fileUrl": url, "expires_in": RESIGN_EXPIRES})
        except Exception as e:
            return resp(500, {"ok": False, "error": str(e)})

    # 兜底
    return resp(404, {"ok": False, "error": "not_found", "path": path, "method": method})

def main_handler(event, context):
    return route(event)
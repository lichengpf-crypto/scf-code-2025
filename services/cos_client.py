# services/cos_client.py
# Azure TTS + COS 缓存工具（含 get_text/put_text 以兼容 db_index）

import os, json, hashlib, urllib.request, html, re
from qcloud_cos import CosConfig, CosS3Client

# ===== COS 客户端 =====
_REGION = os.environ.get("COS_REGION", "ap-beijing")
_BUCKET = os.environ.get("COS_BUCKET")  # 形如 name-appid
_SECRET_ID  = os.environ.get("TENCENTCLOUD_SECRETID")
_SECRET_KEY = os.environ.get("TENCENTCLOUD_SECRETKEY")
_TOKEN      = os.environ.get("TENCENTCLOUD_SESSIONTOKEN")

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

def cos_put_bytes(key: str, blob: bytes, content_type: str = None):
    kwargs = dict(Bucket=_BUCKET, Key=key, Body=blob)
    if content_type:
        kwargs["ContentType"] = content_type
    _cos.put_object(**kwargs)

def cos_get_bytes(key: str) -> bytes:
    obj = _cos.get_object(Bucket=_BUCKET, Key=key)
    return obj["Body"].get_raw_stream().read()

# 兼容层（db_index 需要这两个名字）
def get_bytes(key: str) -> bytes:
    return cos_get_bytes(key)

def put_bytes(key: str, blob: bytes, content_type: str = None):
    return cos_put_bytes(key, blob, content_type)

def get_text(key: str, encoding: str = "utf-8") -> str:
    return cos_get_bytes(key).decode(encoding, "ignore")

def put_text(key: str, text: str, content_type: str = "application/json", encoding: str = "utf-8"):
    cos_put_bytes(key, text.encode(encoding), content_type=content_type)

# ===== Azure TTS =====
_SPEECH_KEY = os.environ.get("SPEECH_KEY")
_SPEECH_REGION = os.environ.get("SPEECH_REGION", "eastasia")

_FMT_TO_AZURE = {
    "mp3-16k": "audio-16khz-32kbitrate-mono-mp3",
    "mp3-24k": "audio-24khz-48kbitrate-mono-mp3",
    "mp3-48k": "audio-48khz-96kbitrate-mono-mp3",
    "wav-16k": "riff-16khz-16bit-mono-pcm",
    "wav-8k":  "riff-8khz-16bit-mono-pcm",
}

def _norm_text(t: str) -> str:
    return " ".join((t or "").strip().split()).lower()

def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def _ext_for_format(fmt: str) -> str:
    return ".wav" if fmt and fmt.startswith("wav") else ".mp3"

def _content_type_for_format(fmt: str) -> str:
    return "audio/wav" if fmt and fmt.startswith("wav") else "audio/mpeg"

def _azure_format(fmt: str) -> str:
    return _FMT_TO_AZURE.get(fmt or "mp3-16k", _FMT_TO_AZURE["mp3-16k"])

def _escape_ssml_text(txt: str) -> str:
    txt = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", txt or "")
    return html.escape(txt, quote=False)

def _build_ssml(text: str, voice: str, language: str, rate: str, pitch: str, style: str) -> bytes:
    t = _escape_ssml_text(text)
    if style:
        ssml = f"""
<speak version='1.0' xml:lang='{language}'>
  <voice name='{voice}'>
    <mstts:express-as style='{style}' xmlns:mstts='https://www.w3.org/2001/mstts'>
      <prosody rate='{rate}' pitch='{pitch}'>{t}</prosody>
    </mstts:express-as>
  </voice>
</speak>""".strip()
    else:
        ssml = f"""
<speak version='1.0' xml:lang='{language}'>
  <voice name='{voice}'>
    <prosody rate='{rate}' pitch='{pitch}'>{t}</prosody>
  </voice>
</speak>""".strip()
    return ssml.encode("utf-8")

def _azure_tts_bytes(text: str, tts: dict) -> bytes:
    if not _SPEECH_KEY:
        raise RuntimeError("SPEECH_KEY missing")
    region   = _SPEECH_REGION
    language = tts.get("language", "en-GB")
    voice    = tts.get("voice",    "en-GB-LibbyNeural")
    rate     = tts.get("rate",     "+0%")
    pitch    = tts.get("pitch",    "+0st")
    style    = tts.get("style",    "")
    fmt      = tts.get("format",   "mp3-16k")

    ssml = _build_ssml(text, voice, language, rate, pitch, style)
    url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
    req = urllib.request.Request(url, data=ssml, method="POST")
    req.add_header("Ocp-Apim-Subscription-Key", _SPEECH_KEY)
    req.add_header("Content-Type", "application/ssml+xml")
    req.add_header("X-Microsoft-OutputFormat", _azure_format(fmt))

    with urllib.request.urlopen(req, timeout=20) as r:
        audio_bytes = r.read()
    return audio_bytes

# ===== 主函数：TTS 合成并缓存到 COS =====
def tts_synthesize_cached(text: str, tts: dict) -> str:
    """
    入参:
      text: 文本
      tts:  { language, voice, rate, pitch, style, format }
    返回:
      cos_key: tts/<lang>/<voice>/<format>/<sha1>.{mp3|wav}
    """
    language = tts.get("language", "en-GB")
    voice    = tts.get("voice",    "en-GB-LibbyNeural")
    rate     = tts.get("rate",     "+0%")
    pitch    = tts.get("pitch",    "+0st")
    style    = tts.get("style",    "")
    fmt      = tts.get("format",   "mp3-16k")

    fp_src = "|".join([_norm_text(text), language, voice, rate, pitch, style, fmt])
    fp = _sha1(fp_src)

    ext = _ext_for_format(fmt)
    cos_key = f"tts/{language}/{voice}/{fmt}/{fp}{ext}"

    if cos_exists(cos_key):
        return cos_key

    audio_bytes = _azure_tts_bytes(text, tts)
    put_bytes(cos_key, audio_bytes, content_type=_content_type_for_format(fmt))
    return cos_key

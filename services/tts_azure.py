# services/tts_azure.py
# Azure TTS 极简封装（使用 urllib，无第三方依赖）
import os
import hashlib
import urllib.request
import urllib.error
from typing import Tuple, Dict

DEFAULT_LANG = os.getenv("DEFAULT_LANG", "en-GB")
DEFAULT_VOICE = os.getenv("DEFAULT_VOICE", "en-GB-LibbyNeural")
DEFAULT_FMT = os.getenv("DEFAULT_TTS_FORMAT", "mp3-16k")

SPEECH_KEY = os.getenv("SPEECH_KEY", "")
SPEECH_REGION = os.getenv("SPEECH_REGION", "")

# 将简短格式映射到 Azure 的 OutputFormat
# 可按需扩展
FMT_MAP = {
    "mp3-16k": "audio-16khz-32kbitrate-mono-mp3",
    "mp3-24k": "audio-24khz-48kbitrate-mono-mp3",
    "mp3-48k": "audio-48khz-96kbitrate-mono-mp3",
    "wav-16k": "riff-16khz-16bit-mono-pcm",
}

def _azure_endpoint() -> str:
    if not SPEECH_REGION:
        raise RuntimeError("SPEECH_REGION not set")
    return f"https://{SPEECH_REGION}.tts.speech.microsoft.com/cognitiveservices/v1"

def _azure_headers(fmt_key: str) -> Dict[str, str]:
    if not SPEECH_KEY:
        raise RuntimeError("SPEECH_KEY not set")
    fmt = FMT_MAP.get(fmt_key, FMT_MAP[DEFAULT_FMT])
    return {
        "Ocp-Apim-Subscription-Key": SPEECH_KEY,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": fmt,
        "User-Agent": "scf-homework-api",
    }

def _ssml(text: str, language: str, voice: str, rate: str, pitch: str, style: str) -> str:
    # rate: "+0%", pitch: "+0st"; style 可为空
    # 为最大兼容性，只有在提供时才包 style
    text = text or ""
    language = language or DEFAULT_LANG
    voice = voice or DEFAULT_VOICE
    prosody_attrs = []
    if rate: prosody_attrs.append(f'rate="{rate}"')
    if pitch: prosody_attrs.append(f'pitch="{pitch}"')
    prosody_attr = " ".join(prosody_attrs)
    if prosody_attr:
        prosody_attr = " " + prosody_attr

    if style:
        # style 对部分 voice 有效，不支持也会忽略
        ssml = f'''<speak version="1.0" xml:lang="{language}">
  <voice name="{voice}">
    <mstts:express-as style="{style}" xmlns:mstts="https://www.w3.org/2001/mstts">
      <prosody{prosody_attr}>{_xml_escape(text)}</prosody>
    </mstts:express-as>
  </voice>
</speak>'''
    else:
        ssml = f'''<speak version="1.0" xml:lang="{language}">
  <voice name="{voice}">
    <prosody{prosody_attr}>{_xml_escape(text)}</prosody>
  </voice>
</speak>'''
    return ssml

def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("'", "&apos;"))

def synthesize(text: str,
               language: str = DEFAULT_LANG,
               voice: str = DEFAULT_VOICE,
               rate: str = "+0%",
               pitch: str = "+0st",
               style: str = "",
               fmt_key: str = DEFAULT_FMT) -> Tuple[bytes, str]:
    """
    返回 (音频字节, content_type)
    """
    ssml = _ssml(text, language, voice, rate, pitch, style)
    data = ssml.encode("utf-8")
    req = urllib.request.Request(
        url=_azure_endpoint(),
        data=data,
        method="POST",
        headers=_azure_headers(fmt_key)
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            audio = resp.read()
            # 简单推断 content-type
            ct = "audio/mpeg" if fmt_key.startswith("mp3") else ("audio/wav" if fmt_key.startswith("wav") else "application/octet-stream")
            return audio, ct
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Azure TTS HTTP {e.code}: {err}")
    except Exception as e:
        raise RuntimeError(f"Azure TTS error: {e}")

def key_for_text(text: str,
                 language: str,
                 voice: str,
                 rate: str,
                 pitch: str,
                 style: str,
                 fmt_key: str) -> Tuple[str, str]:
    """
    生成可复用缓存的 COS key；返回 (cos_key, content_type)
    目录：tts/{lang}/{voice}/{fmt}/{sha1}.mp3
    """
    fmt_key = fmt_key or DEFAULT_FMT
    h = hashlib.sha1(f"{text}|||{language}|||{voice}|||{rate}|||{pitch}|||{style}|||{fmt_key}".encode("utf-8")).hexdigest()
    ext = "mp3" if fmt_key.startswith("mp3") else ("wav" if fmt_key.startswith("wav") else "bin")
    cos_key = f"tts/{language}/{voice}/{fmt_key}/{h}.{ext}"
    ct = "audio/mpeg" if ext == "mp3" else ("audio/wav" if ext == "wav" else "application/octet-stream")
    return cos_key, ct

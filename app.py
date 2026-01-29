import os
import tempfile
import subprocess
from typing import Optional

import requests
from fastapi import FastAPI, Request
from openai import OpenAI

# ====== ENV ======
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

client = OpenAI(api_key=OPENAI_API_KEY)

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TG_FILE_API = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}"

app = FastAPI()


# ====== Telegram helpers ======
def tg_send_message(chat_id: int, text: str) -> None:
    """Send a message to Telegram and log the result."""
    try:
        r = requests.post(
            f"{TG_API}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=30,
        )
        print("sendMessage status:", r.status_code, "resp:", r.text[:500])
    except Exception as e:
        print("sendMessage exception:", repr(e))


def tg_get_file_path(file_id: str) -> Optional[str]:
    """Resolve Telegram file_id to file_path."""
    try:
        r = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=30)
        print("getFile status:", r.status_code, "resp:", r.text[:500])
        data = r.json()
        return data.get("result", {}).get("file_path")
    except Exception as e:
        print("getFile exception:", repr(e))
        return None


def tg_download_file(file_path: str, dst_path: str) -> None:
    """Download a file from Telegram file API to dst_path."""
    url = f"{TG_FILE_API}/{file_path}"
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dst_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)


# ====== Audio helpers ======
def ffmpeg_to_mp3(src_path: str, dst_path: str) -> None:
    """
    Convert almost anything Telegram gives (ogg/opus, mp4, etc.) to mp3.
    Requires ffmpeg installed in the container.
    """
    subprocess.run(
        ["ffmpeg", "-y", "-i", src_path, "-vn", "-acodec", "libmp3lame", "-q:a", "4", dst_path],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def transcribe_with_openai(audio_path: str) -> str:
    """Transcribe audio using OpenAI transcription model."""
    with open(audio_path, "rb") as f:
        tr = client.audio.transcriptions.create(
            model="gpt-4o-transcribe",
            file=f,
            # language="ru",  # uncomment if you want to force Russian
        )
    return (tr.text or "").strip()


# ====== Routes ======
@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    update = await req.json()

    # Debug: show what we got
    print("UPDATE KEYS:", list(update.keys()))

    message = update.get("message") or update.get("edited_message")
    if not message:
        return {"ok": True}

    print("MESSAGE KEYS:", list(message.keys()))

    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        print("No chat_id in message:", message)
        return {"ok": True}

    # Identify message type
    text = message.get("text")
    voice = message.get("voice")  # Telegram voice message (usually ogg/opus)
    audio = message.get("audio")  # Telegram audio file
    document = message.get("document")  # sometimes users send audio as a document

    # 1) Text handling
    if text and not (voice or audio or document):
        help_text = (
            "Я умею превращать голос/аудио в текст.\n\n"
            "• Пришли голосовое (микрофон) или аудиофайл.\n"
            "• Можно коротко, 5–60 сек.\n"
        )
        # You can also add commands:
        if text.strip().lower() in ("/start", "start", "hi", "help", "/help"):
            tg_send_message(chat_id, help_text)
        else:
            tg_send_message(chat_id, "Пришли голосовое или аудиофайл — верну текст 👂➡️📝")
        return {"ok": True}

    # 2) Audio/voice handling
    file_id = None
    file_ext_hint = "ogg"

    if voice and isinstance(voice, dict):
        file_id = voice.get("file_id")
        file_ext_hint = "ogg"
        tg_send_message(chat_id, "Принял голосовое. Распознаю…")
    elif audio and isinstance(audio, dict):
        file_id = audio.get("file_id")
        file_ext_hint = "audio"
        tg_send_message(chat_id, "Принял аудио. Распознаю…")
    elif document and isinstance(document, dict):
        # If user sent an audio file as "document", we try it too.
        mime = (document.get("mime_type") or "").lower()
        if mime.startswith("audio/") or mime in ("application/ogg", "video/mp4"):
            file_id = document.get("file_id")
            file_ext_hint = "doc"
            tg_send_message(chat_id, "Принял файл. Пытаюсь распознать…")
        else:
            tg_send_message(chat_id, "Вижу файл, но это не похоже на аудио. Пришли голосовое или аудиофайл 🙂")
            return {"ok": True}
    else:
        tg_send_message(chat_id, "Не понял формат. Пришли голосовое или аудиофайл 🙂")
        return {"ok": True}

    if not file_id:
        tg_send_message(chat_id, "Не смог получить file_id. Попробуй ещё раз.")
        return {"ok": True}

    try:
        file_path = tg_get_file_path(file_id)
        if not file_path:
            tg_send_message(chat_id, "Не смог получить файл из Telegram (getFile).")
            return {"ok": True}

        with tempfile.TemporaryDirectory() as tmp:
            src_path = os.path.join(tmp, f"input_{file_ext_hint}")
            mp3_path = os.path.join(tmp, "audio.mp3")

            tg_download_file(file_path, src_path)

            # Convert to mp3 (best compatibility)
            try:
                ffmpeg_to_mp3(src_path, mp3_path)
                audio_path = mp3_path
            except Exception as e:
                print("ffmpeg convert failed, using original:", repr(e))
                audio_path = src_path

            text_out = transcribe_with_openai(audio_path)

        if not text_out:
            tg_send_message(chat_id, "Не смог распознать (пусто). Попробуй записать ближе к микрофону.")
            return {"ok": True}

        # Telegram message length safety
        if len(text_out) > 3500:
            text_out = text_out[:3500] + "…"

        tg_send_message(chat_id, text_out)
        return {"ok": True}

    except Exception as e:
        print("ERROR:", repr(e))
        tg_send_message(chat_id, f"Ошибка при распознавании: {type(e).__name__}")
        return {"ok": True}

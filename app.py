import os
import tempfile
import subprocess
from typing import Optional, Dict, Any, List

import requests
from fastapi import FastAPI, Request
from openai import OpenAI

# ====== ENV ======
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")  # required for issue creation
GITHUB_REPO = os.environ.get("GITHUB_REPO")    # "owner/repo"
GITHUB_LABELS = os.environ.get("GITHUB_LABELS", "")  # "voice,telegram"

client = OpenAI(api_key=OPENAI_API_KEY)

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TG_FILE_API = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}"

app = FastAPI()

# ====== In-memory state (OK for one instance). For scaling: Redis. ======
# pending[chat_id] = {"stage": "confirm"|"edit", "text": "...", "meta": {...}}
PENDING: Dict[int, Dict[str, Any]] = {}


# ====== Telegram helpers ======
def tg_send_message(chat_id: int, text: str) -> None:
    try:
        r = requests.post(
            f"{TG_API}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=30,
        )
        print("sendMessage status:", r.status_code, "resp:", r.text[:300])
    except Exception as e:
        print("sendMessage exception:", repr(e))


def tg_get_file_path(file_id: str) -> Optional[str]:
    try:
        r = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=30)
        print("getFile status:", r.status_code, "resp:", r.text[:300])
        data = r.json()
        return data.get("result", {}).get("file_path")
    except Exception as e:
        print("getFile exception:", repr(e))
        return None


def tg_download_file(file_path: str, dst_path: str) -> None:
    url = f"{TG_FILE_API}/{file_path}"
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dst_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)


# ====== Audio helpers ======
def ffmpeg_to_mp3(src_path: str, dst_path: str) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-i", src_path, "-vn", "-acodec", "libmp3lame", "-q:a", "4", dst_path],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def transcribe_with_openai(audio_path: str) -> str:
    with open(audio_path, "rb") as f:
        tr = client.audio.transcriptions.create(
            model="gpt-4o-transcribe",
            file=f,
            # language="ru",
        )
    return (tr.text or "").strip()


# ====== GitHub helpers ======
def parse_labels() -> List[str]:
    labels = [x.strip() for x in GITHUB_LABELS.split(",") if x.strip()]
    return labels


def github_create_issue(title: str, body: str) -> str:
    """
    Returns issue HTML URL.
    Requires GITHUB_TOKEN + GITHUB_REPO.
    """
    if not GITHUB_TOKEN or not GITHUB_REPO:
        raise RuntimeError("GitHub is not configured (missing GITHUB_TOKEN or GITHUB_REPO).")

    url = f"https://api.github.com/repos/{GITHUB_REPO}/issues"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload: Dict[str, Any] = {
        "title": title,
        "body": body,
    }
    labels = parse_labels()
    if labels:
        payload["labels"] = labels

    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"GitHub issue create failed {r.status_code}: {r.text[:500]}")

    data = r.json()
    return data["html_url"]


# ====== Ticket formatting ======
def make_issue_title_and_body(text: str, chat_id: int, user: Dict[str, Any]) -> Dict[str, str]:
    """
    Simple heuristic: first line (or first sentence) becomes title.
    You can later replace this with smarter parsing / templates.
    """
    clean = " ".join(text.split()).strip()

    # Title: first 80 chars or up to first punctuation
    title = clean
    for sep in [". ", "! ", "? ", "\n"]:
        if sep in title:
            title = title.split(sep, 1)[0]
            break
    title = title[:80].strip()
    if not title:
        title = "Voice ticket"

    username = user.get("username") or f'{user.get("first_name","")} {user.get("last_name","")}'.strip()
    body = (
        f"{clean}\n\n"
        f"---\n"
        f"Source: Telegram voice\n"
        f"From: {username or 'unknown'}\n"
        f"Chat ID: {chat_id}\n"
    )
    return {"title": title, "body": body}


def show_confirmation(chat_id: int, text: str) -> None:
    msg = (
        "Вот что я распознал:\n\n"
        f"“{text}”\n\n"
        "Подтверждаешь создание GitHub Issue?\n"
        "Ответь одним словом:\n"
        "✅ *создать*  |  ✏️ *правка*  |  ❌ *отмена*\n"
    )
    # Telegram plain text only (без Markdown), чтобы не париться с экранированием
    tg_send_message(chat_id, msg)


# ====== Routes ======
@app.get("/")
def health():
    return {"status": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    update = await req.json()

    message = update.get("message") or update.get("edited_message")
    if not message:
        return {"ok": True}

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    if not chat_id:
        return {"ok": True}

    user = message.get("from", {}) or {}

    text = (message.get("text") or "").strip()
    voice = message.get("voice")
    audio = message.get("audio")
    document = message.get("document")

    # ====== 0) Handle confirmation flow if we have pending state ======
    if text and chat_id in PENDING:
        state = PENDING[chat_id]
        lower = text.lower()

        if state["stage"] == "confirm":
            if lower in ("создать", "да", "ок", "yes", "y", "✅"):
                try:
                    formatted = make_issue_title_and_body(state["text"], chat_id, user)
                    url = github_create_issue(formatted["title"], formatted["body"])
                    tg_send_message(chat_id, f"Готово. Issue создан:\n{url}")
                except Exception as e:
                    print("GitHub create error:", repr(e))
                    tg_send_message(chat_id, f"Не смог создать issue: {type(e).__name__}\nПроверь GITHUB_TOKEN / GITHUB_REPO и права.")
                finally:
                    PENDING.pop(chat_id, None)
                return {"ok": True}

            if lower in ("правка", "редактировать", "edit", "✏️"):
                state["stage"] = "edit"
                tg_send_message(chat_id, "Ок. Пришли одним сообщением исправленный текст тикета.")
                return {"ok": True}

            if lower in ("отмена", "нет", "cancel", "❌"):
                PENDING.pop(chat_id, None)
                tg_send_message(chat_id, "Отменил. Ничего не отправляю в GitHub.")
                return {"ok": True}

            tg_send_message(chat_id, "Не понял ответ. Напиши: создать / правка / отмена.")
            return {"ok": True}

        if state["stage"] == "edit":
            # User sent corrected text
            state["text"] = text
            state["stage"] = "confirm"
            show_confirmation(chat_id, state["text"])
            return {"ok": True}

    # ====== 1) Plain text (no pending) ======
    if text and not (voice or audio or document):
        if text.lower() in ("/start", "/help", "help", "старт"):
            tg_send_message(
                chat_id,
                "Пришли голосовое — я переведу в текст и предложу создать GitHub Issue.\n"
                "После распознавания ответь: создать / правка / отмена."
            )
        else:
            tg_send_message(chat_id, "Пришли голосовое или аудиофайл. Текст я использую для подтверждения/правки.")
        return {"ok": True}

    # ====== 2) Voice/Audio handling ======
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
        mime = (document.get("mime_type") or "").lower()
        if mime.startswith("audio/") or mime in ("application/ogg", "video/mp4"):
            file_id = document.get("file_id")
            file_ext_hint = "doc"
            tg_send_message(chat_id, "Принял файл. Распознаю…")
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

            try:
                ffmpeg_to_mp3(src_path, mp3_path)
                audio_path = mp3_path
            except Exception as e:
                print("ffmpeg convert failed, using original:", repr(e))
                audio_path = src_path

            recognized = transcribe_with_openai(audio_path)

        if not recognized:
            tg_send_message(chat_id, "Не смог распознать (пусто). Попробуй записать ближе к микрофону.")
            return {"ok": True}

        # Save to pending and ask for confirmation
        PENDING[chat_id] = {"stage": "confirm", "text": recognized}
        show_confirmation(chat_id, recognized)
        return {"ok": True}

    except Exception as e:
        print("ERROR:", repr(e))
        tg_send_message(chat_id, f"Ошибка при распознавании: {type(e).__name__}")
        return {"ok": True}

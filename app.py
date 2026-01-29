import os
import json
import time
import tempfile
import subprocess
from typing import Optional, Dict, Any, List, Tuple

import requests
from fastapi import FastAPI, Request
from openai import OpenAI

# ===================== ENV =====================
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")  # "owner/repo"
GITHUB_LABELS = os.environ.get("GITHUB_LABELS", "")

# Optional: use /ticket command in groups to avoid noise. Default: true.
REQUIRE_TICKET_COMMAND = os.environ.get("REQUIRE_TICKET_COMMAND", "true").lower() in ("1", "true", "yes", "y")

# Optional: when in group, should bot post confirmations in the group or DM? Default: group.
CONFIRM_IN_GROUP = os.environ.get("CONFIRM_IN_GROUP", "true").lower() in ("1", "true", "yes", "y")

# NOTE: In-memory state. For real multi-instance reliability -> Redis.
PENDING: Dict[str, Dict[str, Any]] = {}  # key = f"{chat_id}:{user_id}"

client = OpenAI(api_key=OPENAI_API_KEY)

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TG_FILE_API = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}"

app = FastAPI()


# ===================== TELEGRAM HELPERS =====================
def tg_send_message(chat_id: int, text: str, reply_to_message_id: Optional[int] = None) -> None:
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id
        payload["allow_sending_without_reply"] = True
    requests.post(f"{TG_API}/sendMessage", json=payload, timeout=30)


def tg_send_message_with_keyboard(
    chat_id: int,
    text: str,
    keyboard: List[List[Dict[str, str]]],
    reply_to_message_id: Optional[int] = None,
) -> None:
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": {"inline_keyboard": keyboard},
    }
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id
        payload["allow_sending_without_reply"] = True
    requests.post(f"{TG_API}/sendMessage", json=payload, timeout=30)


def tg_answer_callback(callback_id: str, text: Optional[str] = None, show_alert: bool = False) -> None:
    payload: Dict[str, Any] = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = show_alert
    requests.post(f"{TG_API}/answerCallbackQuery", json=payload, timeout=10)


def tg_get_file_path(file_id: str) -> Optional[str]:
    r = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=30)
    return r.json().get("result", {}).get("file_path")


def tg_download_file(file_path: str, dst_path: str) -> None:
    url = f"{TG_FILE_API}/{file_path}"
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dst_path, "wb") as f:
            for chunk in r.iter_content(1024 * 256):
                if chunk:
                    f.write(chunk)


# ===================== AUDIO =====================
def ffmpeg_to_mp3(src: str, dst: str) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-vn", "-acodec", "libmp3lame", "-q:a", "4", dst],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def transcribe(audio_path: str) -> str:
    with open(audio_path, "rb") as f:
        res = client.audio.transcriptions.create(
            model="gpt-4o-transcribe",
            file=f,
            # language="ru",
        )
    return (res.text or "").strip()


# ===================== GITHUB =====================
def parse_labels() -> List[str]:
    return [x.strip() for x in GITHUB_LABELS.split(",") if x.strip()]


def github_create_issue(title: str, body: str) -> str:
    if not GITHUB_TOKEN or not GITHUB_REPO:
        raise RuntimeError("GitHub ENV not configured (missing GITHUB_TOKEN or GITHUB_REPO)")

    r = requests.post(
        f"https://api.github.com/repos/{GITHUB_REPO}/issues",
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={
            "title": title,
            "body": body,
            "labels": parse_labels(),
        },
        timeout=30,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"GitHub create issue failed {r.status_code}: {r.text[:500]}")
    return r.json()["html_url"]


def format_issue(text: str, chat_id: int, user: dict) -> Dict[str, str]:
    clean = " ".join(text.split()).strip()

    # Title: first sentence-ish up to 80 chars
    title = clean
    for sep in [". ", "! ", "? ", "\n"]:
        if sep in title:
            title = title.split(sep, 1)[0]
            break
    title = title[:80].strip() or "Voice ticket"

    username = user.get("username") or f'{user.get("first_name","")} {user.get("last_name","")}'.strip()
    body = (
        f"{clean}\n\n---\n"
        f"Source: Telegram\n"
        f"From: {username or 'unknown'}\n"
        f"Chat ID: {chat_id}\n"
    )
    return {"title": title, "body": body}


# ===================== STATE & UI =====================
def state_key(chat_id: int, user_id: int) -> str:
    return f"{chat_id}:{user_id}"


def show_confirmation(
    chat_id: int,
    user_id: int,
    text: str,
    reply_to_message_id: Optional[int] = None,
    in_group: bool = True,
) -> None:
    # Bind buttons to author user_id
    keyboard = [
        [{"text": "✅ Создать", "callback_data": f"create:{user_id}"}],
        [
            {"text": "✏️ Правка", "callback_data": f"edit:{user_id}"},
            {"text": "❌ Отмена", "callback_data": f"cancel:{user_id}"},
        ],
    ]

    msg = f"Вот что я распознал:\n\n“{text}”\n\nСоздать GitHub Issue?"
    tg_send_message_with_keyboard(chat_id, msg, keyboard, reply_to_message_id=reply_to_message_id)


def is_group(chat: dict) -> bool:
    return chat.get("type") in ("group", "supergroup")


def extract_command_text(text: str) -> Tuple[bool, str]:
    """
    If text starts with /ticket, return (True, rest_of_text).
    """
    t = (text or "").strip()
    if not t:
        return False, ""
    if t.startswith("/ticket"):
        rest = t[len("/ticket") :].strip()
        return True, rest
    return False, ""


# ===================== ROUTES =====================
@app.get("/")
def health():
    return {"ok": True}


@app.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    update = await req.json()

    # ---------- CALLBACK QUERIES (BUTTONS) ----------
    if "callback_query" in update:
        cq = update["callback_query"]
        callback_id = cq["id"]
        data = (cq.get("data") or "").strip()  # create:uid / edit:uid / cancel:uid
        from_user = cq.get("from", {}) or {}
        clicker_id = from_user.get("id")

        message_obj = cq.get("message", {}) or {}
        chat = message_obj.get("chat", {}) or {}
        chat_id = chat.get("id")
        reply_to_message_id = message_obj.get("message_id")

        # Acknowledge click ASAP
        try:
            tg_answer_callback(callback_id)
        except Exception:
            pass

        if not chat_id or not clicker_id or ":" not in data:
            return {"ok": True}

        action, author_id_str = data.split(":", 1)
        try:
            author_id = int(author_id_str)
        except ValueError:
            return {"ok": True}

        # Only the author can act
        if clicker_id != author_id:
            tg_answer_callback(callback_id, text="Это не твой черновик 🙂", show_alert=False)
            return {"ok": True}

        key = state_key(chat_id, author_id)
        if key not in PENDING:
            tg_send_message(chat_id, "Черновик не найден. Пришли голосовое ещё раз.", reply_to_message_id=reply_to_message_id)
            return {"ok": True}

        state = PENDING[key]

        if action == "create":
            try:
                issue = format_issue(state["text"], chat_id, from_user)
                url = github_create_issue(issue["title"], issue["body"])
                tg_send_message(chat_id, f"Готово 🎉\n{url}", reply_to_message_id=reply_to_message_id)
            except Exception as e:
                tg_send_message(chat_id, f"Ошибка создания issue: {type(e).__name__}\n{e}", reply_to_message_id=reply_to_message_id)
            finally:
                PENDING.pop(key, None)
            return {"ok": True}

        if action == "edit":
            state["stage"] = "edit"
            tg_send_message(chat_id, "Ок, пришли исправленный текст одним сообщением.", reply_to_message_id=reply_to_message_id)
            return {"ok": True}

        if action == "cancel":
            PENDING.pop(key, None)
            tg_send_message(chat_id, "Отменил.", reply_to_message_id=reply_to_message_id)
            return {"ok": True}

        return {"ok": True}

    # ---------- MESSAGES ----------
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return {"ok": True}

    chat = msg.get("chat", {}) or {}
    chat_id = chat.get("id")
    user = msg.get("from", {}) or {}
    user_id = user.get("id")
    message_id = msg.get("message_id")

    if not chat_id or not user_id:
        return {"ok": True}

    in_group = is_group(chat)

    text = (msg.get("text") or "").strip()
    voice = msg.get("voice")
    audio = msg.get("audio")
    document = msg.get("document")

    key = state_key(chat_id, user_id)

    # ---------- HELP ----------
    if text.lower() in ("/start", "/help", "help"):
        tg_send_message(
            chat_id,
            "Пришли голосовое — я распознаю и предложу создать GitHub Issue.\n"
            "В группе: используй команду /ticket (если включено REQUIRE_TICKET_COMMAND).\n"
            "Кнопки: ✅ Создать / ✏️ Правка / ❌ Отмена.",
            reply_to_message_id=message_id,
        )
        return {"ok": True}

    # ---------- EDIT FLOW: user sends corrected text after pressing ✏️ Правка ----------
    if text and key in PENDING and PENDING[key].get("stage") == "edit":
        PENDING[key]["text"] = text
        PENDING[key]["stage"] = "confirm"
        show_confirmation(chat_id, user_id, text, reply_to_message_id=message_id, in_group=in_group)
        return {"ok": True}

    # ---------- GROUP MODE: optionally require /ticket ----------
    allow_ticket_flow = True
    ticket_text_from_command = ""

    if in_group and REQUIRE_TICKET_COMMAND:
        is_ticket_cmd, rest = extract_command_text(text)
        if is_ticket_cmd:
            # text-based ticket via /ticket <text>
            if rest:
                ticket_text_from_command = rest
            allow_ticket_flow = True
        else:
            # If not /ticket, we only accept voice if it's attached as a reply to a /ticket message,
            # but for simplicity we just ignore non-/ticket traffic.
            allow_ticket_flow = False

    # ---------- TEXT-BASED TICKET via /ticket <text> ----------
    if ticket_text_from_command:
        PENDING[key] = {"stage": "confirm", "text": ticket_text_from_command, "ts": int(time.time())}
        show_confirmation(chat_id, user_id, ticket_text_from_command, reply_to_message_id=message_id, in_group=in_group)
        return {"ok": True}

    # ---------- PLAIN TEXT (no pending, no /ticket text) ----------
    if text and not (voice or audio or document):
        if in_group and REQUIRE_TICKET_COMMAND:
            tg_send_message(chat_id, "В группе используй: /ticket + голосовое или /ticket <текст>", reply_to_message_id=message_id)
        else:
            tg_send_message(chat_id, "Пришли голосовое или /ticket <текст> — я сделаю GitHub Issue.", reply_to_message_id=message_id)
        return {"ok": True}

    # ---------- VOICE/AUDIO: accept only if allowed in group mode ----------
    if in_group and REQUIRE_TICKET_COMMAND and not allow_ticket_flow:
        # ignore to avoid noise
        return {"ok": True}

    # Try to find file object
    file_obj = None
    if voice and isinstance(voice, dict):
        file_obj = voice
    elif audio and isinstance(audio, dict):
        file_obj = audio
    elif document and isinstance(document, dict):
        mime = (document.get("mime_type") or "").lower()
        if mime.startswith("audio/") or mime in ("application/ogg", "video/mp4"):
            file_obj = document

    if not file_obj:
        return {"ok": True}

    tg_send_message(chat_id, "Распознаю голос…", reply_to_message_id=message_id)

    try:
        file_path = tg_get_file_path(file_obj["file_id"])
        if not file_path:
            tg_send_message(chat_id, "Не смог получить файл из Telegram (getFile).", reply_to_message_id=message_id)
            return {"ok": True}

        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "input")
            mp3 = os.path.join(tmp, "audio.mp3")

            tg_download_file(file_path, src)

            try:
                ffmpeg_to_mp3(src, mp3)
                audio_path = mp3
            except Exception:
                audio_path = src

            recognized = transcribe(audio_path)

        if not recognized:
            tg_send_message(chat_id, "Не смог распознать 😕", reply_to_message_id=message_id)
            return {"ok": True

            }

        # Store draft per (chat_id, user_id)
        PENDING[key] = {"stage": "confirm", "text": recognized, "ts": int(time.time())}

        show_confirmation(chat_id, user_id, recognized, reply_to_message_id=message_id, in_group=in_group)
        return {"ok": True}

    except Exception as e:
        tg_send_message(chat_id, f"Ошибка: {type(e).__name__}\n{e}", reply_to_message_id=message_id)
        return {"ok": True}

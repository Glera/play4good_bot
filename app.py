import os
import re
import time
import base64
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

# In groups: require /ticket to avoid noise (recommended)
REQUIRE_TICKET_COMMAND = os.environ.get("REQUIRE_TICKET_COMMAND", "true").lower() in ("1", "true", "yes", "y")
ARM_TTL_SECONDS = int(os.environ.get("ARM_TTL_SECONDS", "120"))  # /ticket -> wait next voice within TTL

# WebApp URLs (Netlify)
WEBAPP_URL_PRODUCTION = os.environ.get("WEBAPP_URL_PRODUCTION", "")  # main branch
WEBAPP_URL_DEV_1 = os.environ.get("WEBAPP_URL_DEV_1", "")           # dev branch #1
WEBAPP_URL_DEV_2 = os.environ.get("WEBAPP_URL_DEV_2", "")           # dev branch #2
WEBAPP_DEV_1_NAME = os.environ.get("WEBAPP_DEV_1_NAME", "Dev 1")    # display name
WEBAPP_DEV_2_NAME = os.environ.get("WEBAPP_DEV_2_NAME", "Dev 2")    # display name

client = OpenAI(api_key=OPENAI_API_KEY)

TG_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TG_FILE_API = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}"
GH_API = "https://api.github.com"

app = FastAPI()

# ===== In-memory state. For production/multi-instance use Redis. =====
# key = f"{chat_id}:{user_id}"
PENDING: Dict[str, Dict[str, Any]] = {}
ARMED: Dict[str, int] = {}  # key chat_id:user_id -> expires_at unix


# ===================== UTIL =====================
def is_group(chat: dict) -> bool:
    return chat.get("type") in ("group", "supergroup")


def state_key(chat_id: int, user_id: int) -> str:
    return f"{chat_id}:{user_id}"


def now_ts() -> int:
    return int(time.time())


def slugify(s: str, max_len: int = 40) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9а-яё]+", "-", s, flags=re.IGNORECASE)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:max_len] or "ticket"


def parse_labels() -> List[str]:
    return [x.strip() for x in GITHUB_LABELS.split(",") if x.strip()]


def extract_ticket_command(text: str) -> Tuple[bool, str]:
    t = (text or "").strip()
    if not t:
        return False, ""
    if t.startswith("/ticket"):
        rest = t[len("/ticket") :].strip()
        return True, rest
    return False, ""


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


def tg_download_file_bytes(file_path: str) -> bytes:
    url = f"{TG_FILE_API}/{file_path}"
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    return r.content


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
def gh_headers() -> Dict[str, str]:
    if not GITHUB_TOKEN:
        raise RuntimeError("Missing GITHUB_TOKEN")
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def gh_repo_parts() -> Tuple[str, str]:
    if not GITHUB_REPO or "/" not in GITHUB_REPO:
        raise RuntimeError("Missing or invalid GITHUB_REPO (expected owner/repo)")
    owner, repo = GITHUB_REPO.split("/", 1)
    return owner, repo


def gh_get_default_branch() -> str:
    owner, repo = gh_repo_parts()
    r = requests.get(f"{GH_API}/repos/{owner}/{repo}", headers=gh_headers(), timeout=30)
    r.raise_for_status()
    return r.json()["default_branch"]


def gh_get_branch_sha(branch: str) -> str:
    owner, repo = gh_repo_parts()
    r = requests.get(f"{GH_API}/repos/{owner}/{repo}/git/ref/heads/{branch}", headers=gh_headers(), timeout=30)
    r.raise_for_status()
    return r.json()["object"]["sha"]


def gh_branch_exists(branch: str) -> bool:
    owner, repo = gh_repo_parts()
    r = requests.get(f"{GH_API}/repos/{owner}/{repo}/git/ref/heads/{branch}", headers=gh_headers(), timeout=15)
    return r.status_code == 200


def gh_create_branch(new_branch: str, from_branch: str) -> None:
    owner, repo = gh_repo_parts()
    base_sha = gh_get_branch_sha(from_branch)
    payload = {"ref": f"refs/heads/{new_branch}", "sha": base_sha}
    r = requests.post(f"{GH_API}/repos/{owner}/{repo}/git/refs", headers=gh_headers(), json=payload, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Create branch failed {r.status_code}: {r.text[:500]}")


def gh_put_file(branch: str, path: str, content_bytes: bytes, message: str) -> str:
    owner, repo = gh_repo_parts()
    b64 = base64.b64encode(content_bytes).decode("utf-8")
    payload: Dict[str, Any] = {"message": message, "content": b64, "branch": branch}

    r = requests.put(
        f"{GH_API}/repos/{owner}/{repo}/contents/{path}",
        headers=gh_headers(),
        json=payload,
        timeout=60,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Upload file failed {r.status_code}: {r.text[:500]}")

    data = r.json()
    return data["content"]["html_url"]


def gh_create_issue(title: str, body: str) -> Dict[str, Any]:
    owner, repo = gh_repo_parts()
    payload: Dict[str, Any] = {"title": title, "body": body}
    labels = parse_labels()
    if labels:
        payload["labels"] = labels

    r = requests.post(f"{GH_API}/repos/{owner}/{repo}/issues", headers=gh_headers(), json=payload, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Create issue failed {r.status_code}: {r.text[:500]}")
    return r.json()


def gh_update_issue(number: int, body: str) -> None:
    owner, repo = gh_repo_parts()
    r = requests.patch(
        f"{GH_API}/repos/{owner}/{repo}/issues/{number}",
        headers=gh_headers(),
        json={"body": body},
        timeout=30,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Update issue failed {r.status_code}: {r.text[:500]}")


def gh_list_branches(page: int = 1, per_page: int = 10) -> List[str]:
    owner, repo = gh_repo_parts()
    url = f"{GH_API}/repos/{owner}/{repo}/branches"
    r = requests.get(
        url,
        headers=gh_headers(),
        params={"per_page": per_page, "page": page},
        timeout=30,
    )
    print("GH branches GET", url, "status", r.status_code)
    print("GH branches resp (first 300):", r.text[:300])

    if r.status_code >= 300:
        raise RuntimeError(f"List branches failed {r.status_code}: {r.text[:500]}")

    data = r.json()
    names = [b.get("name") for b in data if isinstance(b, dict)]
    return [n for n in names if n]


def format_issue(text: str, chat_id: int, user: dict) -> Dict[str, str]:
    clean = " ".join(text.split()).strip()
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


# ===================== UI / FLOW =====================
def show_apps_menu(chat_id: int, reply_to_message_id: Optional[int] = None) -> None:
    """Send inline keyboard with WebApp buttons for all three environments."""
    keyboard: List[List[Dict[str, Any]]] = []

    if WEBAPP_URL_PRODUCTION:
        keyboard.append([{
            "text": f"\U0001f7e2 Основное приложение",
            "web_app": {"url": WEBAPP_URL_PRODUCTION},
        }])
    if WEBAPP_URL_DEV_1:
        keyboard.append([{
            "text": f"\U0001f535 Тест — {WEBAPP_DEV_1_NAME}",
            "web_app": {"url": WEBAPP_URL_DEV_1},
        }])
    if WEBAPP_URL_DEV_2:
        keyboard.append([{
            "text": f"\U0001f7e1 Тест — {WEBAPP_DEV_2_NAME}",
            "web_app": {"url": WEBAPP_URL_DEV_2},
        }])

    if not keyboard:
        tg_send_message(chat_id, "WebApp URLs не настроены. Задайте WEBAPP_URL_* в env.", reply_to_message_id=reply_to_message_id)
        return

    tg_send_message_with_keyboard(
        chat_id,
        "Выбери приложение:",
        keyboard,
        reply_to_message_id=reply_to_message_id,
    )


def confirmation_text(state: Dict[str, Any]) -> str:
    screenshot = state.get("screenshot")
    branch_mode = state.get("branch_mode")  # "new"|"existing"|None
    branch_name = state.get("branch_name")

    meta = []
    meta.append(f"Скриншот: {'✅ есть' if screenshot else '— нет'}")
    if branch_mode == "new":
        meta.append("Ветка: 🌱 новая")
    elif branch_mode == "existing":
        meta.append(f"Ветка: ↩️ существующая ({branch_name or 'не выбрана'})")
    else:
        meta.append("Ветка: — не выбрана")

    return (
        "Вот что я распознал:\n\n"
        + f"\u201c{state['text']}\u201d\n\n"
        + " | ".join(meta)
        + "\n\n"
        + "Что делаем?"
    )


def show_confirmation(chat_id: int, author_id: int, state: Dict[str, Any], reply_to_message_id: Optional[int] = None) -> None:
    keyboard = [
        [{"text": "✅ Создать issue", "callback_data": f"create:{author_id}"}],
        [{"text": "✏️ Правка текста", "callback_data": f"edit:{author_id}"}],
        [{"text": "📎 Скриншот", "callback_data": f"shot:{author_id}"}],
        [{"text": "🌱 Новая ветка", "callback_data": f"branch_new:{author_id}"}],
        [{"text": "↩️ Выбрать ветку", "callback_data": f"branch_pick:{author_id}"}],
        [{"text": "🔎 Ввести ветку вручную", "callback_data": f"branch_manual:{author_id}"}],
        [{"text": "❌ Отмена", "callback_data": f"cancel:{author_id}"}],
    ]
    tg_send_message_with_keyboard(chat_id, confirmation_text(state), keyboard, reply_to_message_id=reply_to_message_id)


def show_branch_picker(chat_id: int, author_id: int, state: Dict[str, Any], reply_to_message_id: Optional[int] = None) -> None:
    page = int(state.get("branch_page") or 1)
    try:
        branches = gh_list_branches(page=page, per_page=10)
    except Exception as e:
        tg_send_message(chat_id, f"Не могу получить ветки из GitHub: {type(e).__name__}\n{e}", reply_to_message_id=reply_to_message_id)
        return
    branches = gh_list_branches(page=page, per_page=10)

    kb: List[List[Dict[str, str]]] = []
    if not branches:
        kb.append([{"text": "↩️ Назад", "callback_data": f"back_confirm:{author_id}"}])
        tg_send_message_with_keyboard(chat_id, "Веток не найдено (или страница пустая).", kb, reply_to_message_id=reply_to_message_id)
        return

    for name in branches:
        # callback_data size is limited; branch names are usually short enough
        kb.append([{"text": name, "callback_data": f"pick:{author_id}:{name}"}])

    nav_row: List[Dict[str, str]] = []
    if page > 1:
        nav_row.append({"text": "⬅️ Prev", "callback_data": f"branch_prev:{author_id}"})
    nav_row.append({"text": f"Page {page}", "callback_data": f"noop:{author_id}"})
    nav_row.append({"text": "Next ➡️", "callback_data": f"branch_next:{author_id}"})
    kb.append(nav_row)

    kb.append([{"text": "🔎 Ввести вручную", "callback_data": f"branch_manual:{author_id}"}])
    kb.append([{"text": "↩️ Назад", "callback_data": f"back_confirm:{author_id}"}])

    tg_send_message_with_keyboard(chat_id, "Выбери существующую ветку:", kb, reply_to_message_id=reply_to_message_id)


def extract_image_from_message(msg: dict) -> Optional[Dict[str, Any]]:
    if msg.get("photo"):
        biggest = msg["photo"][-1]
        return {"file_id": biggest["file_id"], "ext": "jpg"}

    doc = msg.get("document")
    if doc and isinstance(doc, dict):
        mime = (doc.get("mime_type") or "").lower()
        if mime.startswith("image/"):
            ext = mime.split("/", 1)[1] or "png"
            ext = ext.replace("jpeg", "jpg")
            return {"file_id": doc["file_id"], "ext": ext}
    return None


# ===================== ROUTES =====================
@app.get("/")
def health():
    return {"ok": True}


@app.post("/telegram/webhook")
async def telegram_webhook(req: Request):
    update = await req.json()

    # ---------- CALLBACK QUERIES ----------
    if "callback_query" in update:
        cq = update["callback_query"]
        cb_id = cq["id"]
        data = (cq.get("data") or "").strip()
        from_user = cq.get("from", {}) or {}
        clicker_id = from_user.get("id")

        msg_obj = cq.get("message", {}) or {}
        chat = msg_obj.get("chat", {}) or {}
        chat_id = chat.get("id")
        reply_to_id = msg_obj.get("message_id")

        try:
            tg_answer_callback(cb_id)
        except Exception:
            pass

        if not chat_id or not clicker_id:
            return {"ok": True}

        parts = data.split(":")
        action = parts[0] if parts else ""
        if len(parts) < 2:
            return {"ok": True}

        try:
            author_id = int(parts[1])
        except ValueError:
            return {"ok": True}

        extra = ":".join(parts[2:]) if len(parts) > 2 else ""

        if clicker_id != author_id:
            tg_answer_callback(cb_id, text="Это не твой черновик 🙂", show_alert=False)
            return {"ok": True}

        key = state_key(chat_id, author_id)
        state = PENDING.get(key)
        if not state:
            tg_send_message(chat_id, "Черновик не найден. Пришли голосовое ещё раз.", reply_to_message_id=reply_to_id)
            return {"ok": True}

        if action == "noop":
            return {"ok": True}

        if action == "cancel":
            PENDING.pop(key, None)
            tg_send_message(chat_id, "Отменил.", reply_to_message_id=reply_to_id)
            return {"ok": True}

        if action == "edit":
            state["stage"] = "edit"
            tg_send_message(chat_id, "Пришли исправленный текст одним сообщением.", reply_to_message_id=reply_to_id)
            return {"ok": True}

        if action == "shot":
            state["stage"] = "await_screenshot"
            tg_send_message(chat_id, "Ок. Пришли скриншот (картинку) одним сообщением.", reply_to_message_id=reply_to_id)
            return {"ok": True}

        if action == "branch_new":
            state["branch_mode"] = "new"
            state["branch_name"] = None
            state["stage"] = "confirm"
            show_confirmation(chat_id, author_id, state, reply_to_message_id=reply_to_id)
            return {"ok": True}

        if action == "branch_pick":
            state["branch_mode"] = "existing"
            state["stage"] = "pick_branch"
            state["branch_page"] = int(state.get("branch_page") or 1)
            show_branch_picker(chat_id, author_id, state, reply_to_message_id=reply_to_id)
            return {"ok": True}

        if action == "branch_next":
            state["branch_page"] = int(state.get("branch_page") or 1) + 1
            show_branch_picker(chat_id, author_id, state, reply_to_message_id=reply_to_id)
            return {"ok": True}

        if action == "branch_prev":
            state["branch_page"] = max(1, int(state.get("branch_page") or 1) - 1)
            show_branch_picker(chat_id, author_id, state, reply_to_message_id=reply_to_id)
            return {"ok": True}

        if action == "branch_manual":
            state["branch_mode"] = "existing"
            state["stage"] = "await_branch_name"
            tg_send_message(chat_id, "Напиши название ветки (например: feature/ui-fix).", reply_to_message_id=reply_to_id)
            return {"ok": True}

        if action == "back_confirm":
            state["stage"] = "confirm"
            show_confirmation(chat_id, author_id, state, reply_to_message_id=reply_to_id)
            return {"ok": True}

        if action == "pick":
            branch_name = extra.strip()
            state["branch_mode"] = "existing"
            state["branch_name"] = branch_name
            state["stage"] = "confirm"
            show_confirmation(chat_id, author_id, state, reply_to_message_id=reply_to_id)
            return {"ok": True}

        if action == "create":
            try:
                issue_fmt = format_issue(state["text"], chat_id, from_user)
                issue = gh_create_issue(issue_fmt["title"], issue_fmt["body"])
                issue_url = issue["html_url"]
                issue_number = issue["number"]
                issue_body = issue_fmt["body"]

                branch_info = ""
                screenshot_info = ""
                chosen_branch: Optional[str] = None
                default_branch = None

                if state.get("branch_mode") == "new":
                    default_branch = gh_get_default_branch()
                
                    username = (
                        from_user.get("username")
                        or from_user.get("first_name")
                        or f"u{from_user.get('id')}"
                    )
                
                    username_slug = slugify(username, max_len=20)  # кириллицу оставляем
                    new_branch = f"ticket/{username_slug}-{issue_number}"
                
                    gh_create_branch(new_branch, default_branch)
                    chosen_branch = new_branch
                    branch_info = f"\nBranch: `{new_branch}`"

                elif state.get("branch_mode") == "existing":
                    bn = (state.get("branch_name") or "").strip()
                    if not bn:
                        tg_send_message(chat_id, "Ветка не указана. Выбери ветку и попробуй снова.", reply_to_message_id=reply_to_id)
                        return {"ok": True}
                    if not gh_branch_exists(bn):
                        tg_send_message(chat_id, f"Ветка `{bn}` не найдена.", reply_to_message_id=reply_to_id)
                        return {"ok": True}
                    chosen_branch = bn
                    branch_info = f"\nBranch: `{bn}`"

                if state.get("screenshot"):
                    if not chosen_branch:
                        default_branch = default_branch or gh_get_default_branch()
                        chosen_branch = default_branch
                        branch_info = f"\nBranch: `{chosen_branch}` (default)"

                    shot = state["screenshot"]
                    file_path = tg_get_file_path(shot["file_id"])
                    if not file_path:
                        raise RuntimeError("Could not resolve screenshot file_path in Telegram")

                    img_bytes = tg_download_file_bytes(file_path)
                    ext = shot.get("ext", "jpg")
                    path_in_repo = f"tickets/issue-{issue_number}/screenshot.{ext}"
                    html_url = gh_put_file(
                        branch=chosen_branch,
                        path=path_in_repo,
                        content_bytes=img_bytes,
                        message=f"Add screenshot for issue #{issue_number}",
                    )
                    screenshot_info = f"\nScreenshot: {html_url}"

                updated_body = issue_body + "\n\n---\n" + (branch_info + screenshot_info).strip()
                gh_update_issue(issue_number, updated_body)

                tg_send_message(chat_id, f"Готово 🎉\nIssue: {issue_url}", reply_to_message_id=reply_to_id)

            except Exception as e:
                tg_send_message(chat_id, f"Ошибка: {type(e).__name__}\n{e}", reply_to_message_id=reply_to_id)
            finally:
                PENDING.pop(key, None)

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
    key = state_key(chat_id, user_id)

    text = (msg.get("text") or "").strip()

    # Help
    if text.lower() in ("/start", "/help", "help"):
        if in_group and REQUIRE_TICKET_COMMAND:
            tg_send_message(chat_id, "В группе: /ticket (и потом голосовое в течение 120 сек) или /ticket <текст>.\n/apps — открыть приложения.", reply_to_message_id=message_id)
        else:
            tg_send_message(chat_id, "Пришли голосовое (или /ticket <текст>) — я создам GitHub Issue.\n/apps — открыть приложения.", reply_to_message_id=message_id)
        return {"ok": True}

    # Apps menu
    if text.lower() == "/apps":
        show_apps_menu(chat_id, reply_to_message_id=message_id)
        return {"ok": True}

    # If pending exists: allow edit, branch name, screenshot image anytime
    if key in PENDING:
        state = PENDING[key]

        # branch name input
        if state.get("stage") == "await_branch_name" and text:
            state["branch_name"] = text.strip()
            state["stage"] = "confirm"
            show_confirmation(chat_id, user_id, state, reply_to_message_id=message_id)
            return {"ok": True}

        # screenshot input (photo or image doc)
        img = extract_image_from_message(msg)
        if img:
            state["screenshot"] = {"file_id": img["file_id"], "ext": img["ext"]}
            state["stage"] = "confirm"
            show_confirmation(chat_id, user_id, state, reply_to_message_id=message_id)
            return {"ok": True}

        # text edit
        if state.get("stage") == "edit" and text:
            state["text"] = text
            state["stage"] = "confirm"
            show_confirmation(chat_id, user_id, state, reply_to_message_id=message_id)
            return {"ok": True}

    # Group gating: /ticket arms next voice
    if in_group and REQUIRE_TICKET_COMMAND:
        is_cmd, rest = extract_ticket_command(text)
        if is_cmd and rest:
            # Text ticket
            PENDING[key] = {
                "stage": "confirm",
                "text": rest,
                "ts": now_ts(),
                "screenshot": None,
                "branch_mode": None,
                "branch_name": None,
                "branch_page": 1,
            }
            show_confirmation(chat_id, user_id, PENDING[key], reply_to_message_id=message_id)
            return {"ok": True}

        if is_cmd and not rest:
            ARMED[key] = now_ts() + ARM_TTL_SECONDS
            tg_send_message(
                chat_id,
                f"Ок. Пришли голосовое в течение {ARM_TTL_SECONDS} сек — сделаю черновик тикета.",
                reply_to_message_id=message_id,
            )
            return {"ok": True}

        # ignore other texts in group
        if text and not msg.get("voice") and not msg.get("audio"):
            return {"ok": True}

    # Plain text outside group gating
    if text and not msg.get("voice") and not msg.get("audio"):
        tg_send_message(chat_id, "Пришли голосовое (или /ticket <текст>). Скриншот можно отправить после распознавания.", reply_to_message_id=message_id)
        return {"ok": True}

    # Voice/audio handling
    file_obj = msg.get("voice") or msg.get("audio")
    if not file_obj:
        return {"ok": True}

    # If in group and require command: accept only if armed
    if in_group and REQUIRE_TICKET_COMMAND:
        expires = ARMED.get(key, 0)
        if expires < now_ts():
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
            return {"ok": True}

        # consume arm
        ARMED.pop(key, None)

        PENDING[key] = {
            "stage": "confirm",
            "text": recognized,
            "ts": now_ts(),
            "screenshot": None,
            "branch_mode": None,
            "branch_name": None,
            "branch_page": 1,
        }
        show_confirmation(chat_id, user_id, PENDING[key], reply_to_message_id=message_id)
        return {"ok": True}

    except Exception as e:
        tg_send_message(chat_id, f"Ошибка: {type(e).__name__}\n{e}", reply_to_message_id=message_id)
        return {"ok": True}

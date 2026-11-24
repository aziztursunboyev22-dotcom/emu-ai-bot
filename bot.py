"""
Clinic of EMU — AI javob beruvchi bot (Telegram + Instagram).

Ishlaydigan joylar:
  1) Telegram guruh — savollarga javob beradi (savol bo'lmagan xabarlarni o'tkazib yuboradi).
  2) Telegram lichka (klinika akkaunti) — Telegram Business orqali.
  3) Telegram botning o'ziga yozilgan xabarlar.
  4) Instagram Direct — Instagram API (Instagram Login) webhook orqali.

Bemor telefon raqam yozsa — Bitrix24'da lid ochiladi.
Operator o'zi yozsa (Telegram lichka yoki Instagram) — bot shu chatda vaqtincha jim turadi.

Railway Variables:
  TELEGRAM_TOKEN      — BotFather bergan token
  BITRIX_WEBHOOK      — Bitrix24 kiruvchi webhook havolasi
  AI kaliti (bittasi yetarli):
    ANTHROPIC_API_KEY — Claude API kaliti (bo'lsa, Claude ishlatiladi)
    GEMINI_API_KEY    — Google Gemini kaliti
  Instagram (ixtiyoriy):
    IG_ACCESS_TOKEN   — Meta'dagi Instagram access token
    IG_VERIFY_TOKEN   — webhook tasdiqlash uchun o'zingiz o'ylab topgan so'z
    IG_APP_SECRET     — Meta ilovasining App Secret (imzo tekshiruvi uchun, tavsiya etiladi)
  Ixtiyoriy:
    GEMINI_MODEL      — standart: gemini-3.8-flash
    CLAUDE_MODEL      — standart: claude-haiku-5-5
    BITRIX_SOURCE_ID  — Bitrix'dagi manba kodi
    PAUSE_MINUTES     — operator yozgandan keyin bot jim turadigan vaqt (standart: 60)
"""

import os
import re
import time
import json
import hmac
import hashlib
import logging
import threading
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("emu-bot")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
BITRIX_WEBHOOK = os.environ.get("BITRIX_WEBHOOK", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip()
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5").strip()
BITRIX_SOURCE_ID = os.environ.get("BITRIX_SOURCE_ID", "").strip()
PAUSE_MINUTES = int(os.environ.get("PAUSE_MINUTES", "60"))
IG_ACCESS_TOKEN = os.environ.get("IG_ACCESS_TOKEN", "").strip()
IG_VERIFY_TOKEN = os.environ.get("IG_VERIFY_TOKEN", "").strip()
IG_APP_SECRET = os.environ.get("IG_APP_SECRET", "").strip()
PORT = int(os.environ.get("PORT", "8080"))

if BITRIX_WEBHOOK and not BITRIX_WEBHOOK.endswith("/"):
    BITRIX_WEBHOOK += "/"

TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/"
IG_API = "https://graph.instagram.com/"
HISTORY_LEN = 12  # har bir suhbatdan oxirgi nechta xabar eslab qolinadi

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE_DIR, "knowledge.txt"), encoding="utf-8") as f:
    KNOWLEDGE = f.read()

SYSTEM_PROMPT = f"""Sen "Clinic of EMU" stomatologiya klinikasining administratorisan (Telegram va Instagram).
Bemorlarning savollariga qisqa, xushmuomala va aniq javob berasan.

QOIDALAR:
1. Faqat quyidagi KLINIKA MA'LUMOTLARIga tayanib javob ber. U yerda yo'q narxni, xizmatni,
   shifokorni yoki boshqa ma'lumotni o'ylab topma. Bilmasang: "Bu bo'yicha operatorimiz aniq
   ma'lumot beradi" deb ayt va telefon raqam qoldirishni so'ra.
2. Tashxis qo'yma, dori tavsiya qilma. Og'riq yoki shikoyat bo'lsa — konsultatsiyaga yozilishni taklif qil.
3. Maqsad — bemorni konsultatsiyaga yozdirish. Mos joyda telefon raqamini qoldirishni so'ra:
   "Raqamingizni qoldiring, operatorimiz siz bilan bog'lanadi."
4. Bemor qaysi tilda yozsa (o'zbek lotin, o'zbek kirill yoki rus), o'sha tilda javob ber.
5. Javob 1–4 gapdan oshmasin. Markdown, yulduzcha (*), # belgilarini ishlatma — oddiy matn yoz.
6. Salomlashuvga salom bilan javob ber va qanday yordam kerakligini so'ra.

KLINIKA MA'LUMOTLARI:
{KNOWLEDGE}
"""

GROUP_RULE = """
GURUH QOIDASI: Bu xabar guruhdan keldi. Agar xabar klinikaga savol, murojaat yoki yozilish
istagi BO'LMASA (masalan: oddiy suhbat, rahmat, reklama, boshqa odamga yozilgan gap),
faqat bitta so'z yoz: SKIP
"""

PHONE_NOTE = """
TIZIM ESLATMASI: Bemor hozirgina telefon raqamini qoldirdi va u operatorga yuborildi.
Rahmat ayt va operator tez orada bog'lanishini ayt. Raqamni qayta so'rama.
"""

START_TEXT = ("Assalomu alaykum! Clinic of EMU stomatologiya klinikasiga xush kelibsiz. "
              "Savolingizni yozing yoki konsultatsiyaga yozilish uchun telefon raqamingizni qoldiring.")

FALLBACK_TEXT = "Rahmat, xabaringiz qabul qilindi. Operatorimiz tez orada javob beradi."

# --- Xotira (server qayta ishga tushsa tozalanadi) ---
histories = defaultdict(lambda: deque(maxlen=HISTORY_LEN))  # suhbat kaliti -> [(role, text)]
sent_phones = set()            # Bitrix'ga yuborilgan raqamlar
business_owners = {}           # Telegram business_connection_id -> klinika akkaunti user_id
paused_until = {}              # suhbat kaliti -> vaqt (operator yozgan bo'lsa)
ig_sent_mids = set()           # bot Instagram'ga yuborgan xabarlar ID'si
bot_info = {}
chat_locks = defaultdict(threading.Lock)       # har bir suhbat uchun alohida navbat
executor = ThreadPoolExecutor(max_workers=10)  # bir vaqtda 10 tagacha suhbatga javob


# =============================== AI ===============================
AI_TIMEOUT = 25
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/"
GEMINI_HEADERS = {"x-goog-api-key": GEMINI_API_KEY}
MAX_MODEL_TRIES = 4
gemini_models = [GEMINI_MODEL]
SKIP_WORDS = ("image", "tts", "audio", "live", "embedding", "vision", "robotics", "computer")


def load_gemini_models():
    """Asosiy model band bo'lsa ishlatiladigan zaxira 'flash' modellar ro'yxati."""
    global gemini_models
    try:
        r = requests.get(GEMINI_BASE + "models", headers=GEMINI_HEADERS,
                         params={"pageSize": 200}, timeout=20)
        names = []
        for m in r.json().get("models", []):
            name = m.get("name", "").replace("models/", "")
            if ("generateContent" in m.get("supportedGenerationMethods", [])
                    and "flash" in name and not any(w in name for w in SKIP_WORDS)):
                names.append(name)
        names.sort(reverse=True)
        gemini_models = [GEMINI_MODEL] + [n for n in names if n != GEMINI_MODEL]
        log.info("Gemini modellar navbati: %s", ", ".join(gemini_models[:MAX_MODEL_TRIES]))
    except Exception as e:
        log.error("Gemini modellar ro'yxatini olib bo'lmadi: %s", e)


def clean(text):
    return (text or "").replace("**", "").replace("*", "").strip()


def ask_claude(history, extra_rules=""):
    messages = []
    for role, text in history:
        r = "assistant" if role == "model" else "user"
        if messages and messages[-1]["role"] == r:
            messages[-1]["content"] += "\n" + text
        else:
            messages.append({"role": r, "content": text})
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": CLAUDE_MODEL, "max_tokens": 500,
                  "system": SYSTEM_PROMPT + extra_rules, "messages": messages},
            timeout=AI_TIMEOUT)
        data = r.json()
        if r.status_code != 200:
            log.error("Claude xato %s: %s", r.status_code, data.get("error", data))
            return None
        return clean("".join(b.get("text", "") for b in data.get("content", [])
                             if b.get("type") == "text")) or None
    except Exception as e:
        log.error("Claude ulanish xatosi: %s", e)
        return None


def ask_gemini(history, extra_rules=""):
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT + extra_rules}]},
        "contents": [{"role": role, "parts": [{"text": text}]} for role, text in history],
        "generationConfig": {"temperature": 0.4},
    }
    for model in gemini_models[:MAX_MODEL_TRIES]:
        try:
            r = requests.post(f"{GEMINI_BASE}models/{model}:generateContent",
                              headers=GEMINI_HEADERS, json=body, timeout=AI_TIMEOUT)
            data = r.json()
            if r.status_code != 200:
                log.warning("Gemini %s xato %s: %s", model, r.status_code,
                            str(data.get("error", {}).get("message", data))[:200])
                continue
            parts = data["candidates"][0]["content"]["parts"]
            text = clean("".join(p.get("text", "") for p in parts if not p.get("thought")))
            if text:
                return text
        except Exception as e:
            log.warning("Gemini %s ulanish xatosi: %s", model, e)
    log.error("Gemini: hech bir model javob bermadi")
    return None


def ask_ai(history, extra_rules=""):
    if ANTHROPIC_API_KEY:
        return ask_claude(history, extra_rules)
    if GEMINI_API_KEY:
        return ask_gemini(history, extra_rules)
    log.error("AI kaliti kiritilmagan (ANTHROPIC_API_KEY yoki GEMINI_API_KEY)")
    return None


# =============================== Telefon raqam ===============================
PHONE_RE = re.compile(r"\+?\d[\d\s\-\(\)]{7,18}\d")


def extract_phone(text):
    for m in PHONE_RE.findall(text or ""):
        digits = re.sub(r"\D", "", m)
        if len(digits) == 12 and digits.startswith("998"):
            return "+" + digits
        if len(digits) == 9:
            return "+998" + digits
    return None


# =============================== Bitrix24 ===============================
def bitrix(method, payload):
    r = requests.post(BITRIX_WEBHOOK + method + ".json", json=payload, timeout=30)
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"{data.get('error')}: {data.get('error_description')}")
    return data.get("result")


def send_lead(phone, name, contact, place, history):
    if not BITRIX_WEBHOOK:
        log.warning("BITRIX_WEBHOOK kiritilmagan — lid yuborilmadi: %s", phone)
        return False

    dialog = "\n".join(("Bemor: " if role == "user" else "Bot: ") + text for role, text in history)
    comment = f"Manba: {place}\nAloqa: {contact or '-'}\n\nYozishma:\n{dialog}"

    try:
        # Dublikat tekshiruvi: shu raqamli lid bo'lsa — yangisini ochmaymiz, izoh qo'shamiz
        found = bitrix("crm.duplicate.findbycomm",
                       {"entity_type": "LEAD", "type": "PHONE", "values": [phone]})
        lead_ids = (found or {}).get("LEAD") if isinstance(found, dict) else None
        if lead_ids:
            bitrix("crm.timeline.comment.add", {"fields": {
                "ENTITY_ID": lead_ids[0], "ENTITY_TYPE": "lead",
                "COMMENT": "Qayta murojaat.\n\n" + comment,
            }})
            log.info("Mavjud lidga izoh qo'shildi: %s (%s)", lead_ids[0], phone)
            return True

        fields = {
            "TITLE": f"{place.split(' ')[0]} AI: {name or phone}",
            "NAME": name or "",
            "PHONE": [{"VALUE": phone, "VALUE_TYPE": "MOBILE"}],
            "SOURCE_DESCRIPTION": f"AI bot ({place})",
            "COMMENTS": comment.replace("\n", "<br>"),
        }
        if BITRIX_SOURCE_ID:
            fields["SOURCE_ID"] = BITRIX_SOURCE_ID
        lead_id = bitrix("crm.lead.add", {"fields": fields, "params": {"REGISTER_SONET_EVENT": "Y"}})
        log.info("Yangi lid ochildi: %s (%s)", lead_id, phone)
        return True
    except Exception as e:
        log.error("Bitrix xato: %s", e)
        return False


# =============================== Umumiy suhbat logikasi ===============================
def run_dialog(key, text, name, contact, place, send, is_group=False, typing=None):
    """Barcha kanallar uchun umumiy: tarix, lid, AI javob, yuborish."""
    with chat_locks[key]:
        if paused_until.get(key, 0) > time.time():
            return
        if typing:
            typing()

        history = histories[key]
        history.append(("user", text))

        extra = GROUP_RULE if is_group else ""
        phone = extract_phone(text)
        if phone and phone not in sent_phones:
            if send_lead(phone, name, contact, place, list(history)):
                sent_phones.add(phone)
            extra += PHONE_NOTE

        answer = ask_ai(list(history), extra)

        if answer and answer.upper().startswith("SKIP"):
            history.pop()
            return
        if not answer:
            if is_group and not phone:
                history.pop()
                return
            answer = FALLBACK_TEXT

        history.append(("model", answer))
        send(answer)


# =============================== Telegram ===============================
def tg(method, **params):
    try:
        r = requests.post(TG_API + method, json=params, timeout=70)
        data = r.json()
        if not data.get("ok"):
            log.warning("Telegram %s xato: %s", method, data)
        return data
    except Exception as e:
        log.error("Telegram %s ulanish xatosi: %s", method, e)
        return {"ok": False}


def tg_send(chat_id, text, reply_to=None, business_connection_id=None):
    params = {"chat_id": chat_id, "text": text}
    if reply_to:
        params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    if business_connection_id:
        params["business_connection_id"] = business_connection_id
    return tg("sendMessage", **params)


def get_business_owner(conn_id):
    if conn_id in business_owners:
        return business_owners[conn_id]
    data = tg("getBusinessConnection", business_connection_id=conn_id)
    if data.get("ok"):
        business_owners[conn_id] = data["result"]["user"]["id"]
        return business_owners[conn_id]
    return None


def handle_telegram(msg, business_connection_id=None):
    text = msg.get("text") or msg.get("caption")
    if not text:
        return
    chat = msg["chat"]
    chat_id = chat["id"]
    user = msg.get("from") or {}
    if user.get("is_bot"):
        return
    is_group = chat["type"] in ("group", "supergroup")

    if text.strip().startswith("/start"):
        if not is_group:
            tg_send(chat_id, START_TEXT, business_connection_id=business_connection_id)
        return
    if text.strip().startswith("/"):
        return

    name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])).strip()
    username = user.get("username")
    contact = f"Telegram @{username}" if username else "Telegram (username yo'q)"
    key = ("tg", business_connection_id or "", chat_id, user.get("id") if is_group else 0)

    if business_connection_id:
        owner_id = get_business_owner(business_connection_id)
        if owner_id and user.get("id") == owner_id:
            paused_until[key] = time.time() + PAUSE_MINUTES * 60  # operator o'zi yozdi
            return
        place = "Telegram lichka"
    elif is_group:
        place = f"Telegram guruh: {chat.get('title', '')}"
    else:
        place = "Telegram bot"

    def send(answer):
        tg_send(chat_id, answer, reply_to=msg["message_id"] if is_group else None,
                business_connection_id=business_connection_id)

    def typing():
        if not is_group:
            p = {"chat_id": chat_id, "action": "typing"}
            if business_connection_id:
                p["business_connection_id"] = business_connection_id
            tg("sendChatAction", **p)

    run_dialog(key, text, name, contact, place, send, is_group=is_group, typing=typing)


def telegram_loop():
    me = tg("getMe")
    if not me.get("ok"):
        log.error("TELEGRAM_TOKEN noto'g'ri — Telegram o'chiq")
        return
    bot_info.update(me["result"])
    tg("deleteWebhook")
    log.info("Telegram bot ishga tushdi: @%s", bot_info.get("username"))

    offset = None
    while True:
        params = {"timeout": 50,
                  "allowed_updates": ["message", "business_connection", "business_message"]}
        if offset:
            params["offset"] = offset
        data = tg("getUpdates", **params)
        if not data.get("ok"):
            time.sleep(5)
            continue
        for upd in data["result"]:
            offset = upd["update_id"] + 1
            if "business_connection" in upd:
                bc = upd["business_connection"]
                business_owners[bc["id"]] = bc["user"]["id"]
                log.info("Telegram Business akkaunt ulandi: %s", bc["user"].get("first_name"))
            elif "business_message" in upd:
                m = upd["business_message"]
                executor.submit(safe, handle_telegram, m, m.get("business_connection_id"))
            elif "message" in upd:
                executor.submit(safe, handle_telegram, upd["message"], None)


# =============================== Instagram ===============================
ig_names = {}
# Token turiga qarab API manzili har xil bo'ladi: Instagram Login tokeni — graph.instagram.com,
# Meta "Unified login" (tizim foydalanuvchisi) tokeni — graph.facebook.com. Ikkalasi sinab ko'riladi.
IG_HOSTS = ["https://graph.instagram.com/", "https://graph.facebook.com/v25.0/"]
ig_host_ok = {"host": None}


def ig_request(method, path, **kwargs):
    hosts = [ig_host_ok["host"]] if ig_host_ok["host"] else IG_HOSTS
    last = None
    for host in hosts:
        try:
            r = requests.request(method, host + path, timeout=30,
                                 headers={"Authorization": f"Bearer {IG_ACCESS_TOKEN}"}, **kwargs)
            data = r.json()
            if r.status_code == 200:
                if ig_host_ok["host"] != host:
                    ig_host_ok["host"] = host
                    log.info("Instagram API manzili: %s", host)
                return data
            last = (r.status_code, data)
        except Exception as e:
            last = ("ulanish", str(e))
    log.error("Instagram %s %s xato: %s", method, path, last)
    return None


def ig_send(recipient_id, text):
    data = ig_request("POST", "me/messages",
                      json={"recipient": {"id": recipient_id}, "message": {"text": text[:1000]}})
    if data and data.get("message_id"):
        ig_sent_mids.add(data["message_id"])


def ig_profile(user_id):
    if user_id in ig_names:
        return ig_names[user_id]
    d = ig_request("GET", user_id, params={"fields": "name,username"}) or {}
    ig_names[user_id] = (d.get("name", ""), d.get("username", ""))
    return ig_names[user_id]


def handle_instagram_event(ev):
    sender = (ev.get("sender") or {}).get("id")
    recipient = (ev.get("recipient") or {}).get("id")
    message = ev.get("message") or {}
    text = message.get("text")
    if not sender or not text or message.get("is_deleted"):
        return

    if message.get("is_echo"):
        # Akkauntdan yuborilgan xabar: bot yozgan bo'lsa — e'tibor bermaymiz,
        # operator qo'lda yozgan bo'lsa — bot shu chatda jim turadi
        if message.get("mid") not in ig_sent_mids:
            paused_until[("ig", recipient)] = time.time() + PAUSE_MINUTES * 60
        return

    name, username = ig_profile(sender)
    contact = f"Instagram @{username}" if username else "Instagram"
    run_dialog(("ig", sender), text, name or username, contact, "Instagram Direct",
               send=lambda answer: ig_send(sender, answer))


class WebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, code, body=b"OK"):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/instagram":
            q = parse_qs(url.query)
            if (q.get("hub.mode", [""])[0] == "subscribe"
                    and IG_VERIFY_TOKEN
                    and q.get("hub.verify_token", [""])[0] == IG_VERIFY_TOKEN):
                log.info("Instagram webhook tasdiqlandi")
                return self._reply(200, q.get("hub.challenge", [""])[0].encode())
            return self._reply(403, b"Forbidden")
        return self._reply(200, b"EMU AI bot ishlayapti")

    def do_POST(self):
        if urlparse(self.path).path != "/instagram":
            return self._reply(404, b"Not found")
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))

        if IG_APP_SECRET:
            expected = "sha256=" + hmac.new(IG_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, self.headers.get("X-Hub-Signature-256", "")):
                log.warning("Instagram webhook: imzo noto'g'ri")
                return self._reply(403, b"Bad signature")

        self._reply(200)  # Meta'ga darhol javob beramiz, xabarni fonda ishlaymiz
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            return
        for entry in payload.get("entry", []):
            for ev in entry.get("messaging", []):
                executor.submit(safe, handle_instagram_event, ev)


def web_server():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), WebhookHandler)
    log.info("Veb-server ishga tushdi: port %s (Instagram webhook: /instagram)", PORT)
    server.serve_forever()


# =============================== Ishga tushirish ===============================
def safe(func, *args):
    try:
        func(*args)
    except Exception as e:
        log.exception("Xabarni qayta ishlashda xato: %s", e)


def main():
    if ANTHROPIC_API_KEY:
        log.info("AI: Claude (%s)", CLAUDE_MODEL)
    elif GEMINI_API_KEY:
        log.info("AI: Gemini (%s)", GEMINI_MODEL)
        load_gemini_models()
    else:
        log.warning("AI kaliti yo'q — bot faqat lid yig'adi va tayyor javob yuboradi")

    if IG_ACCESS_TOKEN:
        log.info("Instagram yoqilgan")
    threading.Thread(target=web_server, daemon=True).start()

    if TELEGRAM_TOKEN:
        telegram_loop()
    else:
        log.warning("TELEGRAM_TOKEN yo'q — faqat Instagram ishlaydi")
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()

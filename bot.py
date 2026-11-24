"""
Clinic of EMU — AI javob beruvchi Telegram bot.

Ishlaydigan joylar:
  1) Telegram guruh — bot guruhga qo'shiladi, bemorlarning savollariga javob beradi
     (savol bo'lmagan xabarlarni o'tkazib yuboradi).
  2) Lichka (klinika akkaunti) — Telegram Business orqali, klinika akkaunti nomidan javob beradi.
  3) Botning o'ziga yozilgan xabarlar.

Bemor telefon raqam yozsa — Bitrix24'da lid ochiladi va operatorga tushadi.

Railway Variables:
  TELEGRAM_TOKEN   — BotFather bergan token
  GEMINI_API_KEY   — Google AI Studio kaliti
  BITRIX_WEBHOOK   — Bitrix24 kiruvchi webhook havolasi (oxiri / bilan)
  GEMINI_MODEL     — ixtiyoriy, standart: gemini-2.5-flash
  BITRIX_SOURCE_ID — ixtiyoriy, Bitrix'dagi manba kodi (masalan: TELEGRAM)
  PAUSE_MINUTES    — ixtiyoriy, operator lichkada o'zi yozsa, bot shu chatda necha daqiqa jim turadi (standart: 60)
"""

import os
import re
import time
import logging
from collections import defaultdict, deque

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("emu-bot")

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
BITRIX_WEBHOOK = os.environ.get("BITRIX_WEBHOOK", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
BITRIX_SOURCE_ID = os.environ.get("BITRIX_SOURCE_ID", "").strip()
PAUSE_MINUTES = int(os.environ.get("PAUSE_MINUTES", "60"))

if BITRIX_WEBHOOK and not BITRIX_WEBHOOK.endswith("/"):
    BITRIX_WEBHOOK += "/"

TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/"
HISTORY_LEN = 12  # har bir suhbatdan oxirgi nechta xabar eslab qolinadi

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE_DIR, "knowledge.txt"), encoding="utf-8") as f:
    KNOWLEDGE = f.read()

SYSTEM_PROMPT = f"""Sen "Clinic of EMU" stomatologiya klinikasining Telegramdagi administratorisan.
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

FALLBACK_TEXT = "Rahmat, xabaringiz qabul qilindi. Operatorimiz tez orada javob beradi."

# --- Xotira (server qayta ishga tushsa tozalanadi) ---
histories = defaultdict(lambda: deque(maxlen=HISTORY_LEN))  # suhbat kaliti -> [(role, text)]
sent_phones = set()            # bu ishga tushishda Bitrix'ga yuborilgan raqamlar
business_owners = {}           # business_connection_id -> klinika akkaunti user_id
paused_until = {}              # (business_connection_id, chat_id) -> vaqt
bot_info = {}


# ---------------- Telegram ----------------
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


def send_text(chat_id, text, reply_to=None, business_connection_id=None):
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


# ---------------- Gemini ----------------
def ask_gemini(history, extra_rules=""):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    contents = [{"role": role, "parts": [{"text": text}]} for role, text in history]
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT + extra_rules}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.4},
    }
    try:
        r = requests.post(url, params={"key": GEMINI_API_KEY}, json=body, timeout=60)
        data = r.json()
        if r.status_code != 200:
            log.error("Gemini xato %s: %s", r.status_code, data)
            return None
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        return text.replace("**", "").replace("*", "") or None
    except Exception as e:
        log.error("Gemini ulanish xatosi: %s", e)
        return None


# ---------------- Telefon raqam ----------------
PHONE_RE = re.compile(r"\+?\d[\d\s\-\(\)]{7,18}\d")


def extract_phone(text):
    for m in PHONE_RE.findall(text or ""):
        digits = re.sub(r"\D", "", m)
        if len(digits) == 12 and digits.startswith("998"):
            return "+" + digits
        if len(digits) == 9:
            return "+998" + digits
    return None


# ---------------- Bitrix24 ----------------
def bitrix(method, payload):
    r = requests.post(BITRIX_WEBHOOK + method + ".json", json=payload, timeout=30)
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"{data.get('error')}: {data.get('error_description')}")
    return data.get("result")


def send_lead(phone, name, username, place, history):
    if not BITRIX_WEBHOOK:
        log.warning("BITRIX_WEBHOOK kiritilmagan — lid yuborilmadi: %s", phone)
        return False

    dialog = "\n".join(
        ("Bemor: " if role == "user" else "Bot: ") + text for role, text in history
    )
    comment = (
        f"Manba: Telegram ({place})\n"
        f"Telegram: {'@' + username if username else 'username yo`q'}\n\n"
        f"Yozishma:\n{dialog}"
    )

    try:
        # Dublikat tekshiruvi: shu raqamli lid bo'lsa — yangisini ochmaymiz, izoh qo'shamiz
        found = bitrix("crm.duplicate.findbycomm",
                       {"entity_type": "LEAD", "type": "PHONE", "values": [phone]})
        lead_ids = (found or {}).get("LEAD") if isinstance(found, dict) else None
        if lead_ids:
            bitrix("crm.timeline.comment.add", {"fields": {
                "ENTITY_ID": lead_ids[0], "ENTITY_TYPE": "lead",
                "COMMENT": "Telegram orqali qayta murojaat.\n\n" + comment,
            }})
            log.info("Mavjud lidga izoh qo'shildi: %s (%s)", lead_ids[0], phone)
            return True

        fields = {
            "TITLE": f"Telegram AI: {name or phone}",
            "NAME": name or "",
            "PHONE": [{"VALUE": phone, "VALUE_TYPE": "MOBILE"}],
            "SOURCE_DESCRIPTION": f"Telegram AI bot ({place})",
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


# ---------------- Xabarni qayta ishlash ----------------
def handle_message(msg, business_connection_id=None):
    text = msg.get("text") or msg.get("caption")
    if not text:
        return

    chat = msg["chat"]
    chat_id = chat["id"]
    user = msg.get("from") or {}
    if user.get("is_bot"):
        return

    is_group = chat["type"] in ("group", "supergroup")
    name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])).strip()
    username = user.get("username")

    # Lichka (Telegram Business)
    if business_connection_id:
        owner_id = get_business_owner(business_connection_id)
        pause_key = (business_connection_id, chat_id)
        if owner_id and user.get("id") == owner_id:
            # Operator o'zi yozdi — bot shu chatda vaqtincha jim turadi
            paused_until[pause_key] = time.time() + PAUSE_MINUTES * 60
            return
        if paused_until.get(pause_key, 0) > time.time():
            return
        place = "lichka"
    elif is_group:
        place = f"guruh: {chat.get('title', '')}"
    else:
        place = "bot"

    key = (business_connection_id or "", chat_id, user.get("id") if is_group else 0)
    history = histories[key]
    history.append(("user", text))

    extra = GROUP_RULE if is_group else ""
    phone = extract_phone(text)
    if phone and phone not in sent_phones:
        if send_lead(phone, name, username, place, list(history)):
            sent_phones.add(phone)
        extra += PHONE_NOTE

    answer = ask_gemini(list(history), extra)

    if answer and answer.strip().upper().startswith("SKIP"):
        history.pop()
        return
    if not answer:
        if is_group and not phone:
            history.pop()
            return
        answer = FALLBACK_TEXT

    history.append(("model", answer))
    send_text(chat_id, answer,
              reply_to=msg["message_id"] if is_group else None,
              business_connection_id=business_connection_id)


def main():
    me = tg("getMe")
    if not me.get("ok"):
        raise SystemExit("TELEGRAM_TOKEN noto'g'ri.")
    bot_info.update(me["result"])
    tg("deleteWebhook")
    log.info("Bot ishga tushdi: @%s, model: %s", bot_info.get("username"), GEMINI_MODEL)

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
            try:
                if "business_connection" in upd:
                    bc = upd["business_connection"]
                    business_owners[bc["id"]] = bc["user"]["id"]
                    log.info("Business akkaunt ulandi: %s", bc["user"].get("first_name"))
                elif "business_message" in upd:
                    m = upd["business_message"]
                    handle_message(m, business_connection_id=m.get("business_connection_id"))
                elif "message" in upd:
                    handle_message(upd["message"])
            except Exception as e:
                log.exception("Xabarni qayta ishlashda xato: %s", e)


if __name__ == "__main__":
    main()

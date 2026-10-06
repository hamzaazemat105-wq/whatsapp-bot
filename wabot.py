#!/usr/bin/env python3
"""
WhatsApp Smart Bot for Hamza's e-commerce.
Receives customer messages via Meta webhook, replies intelligently in Darija,
integrates with the shop supplier API, and takes admin commands from Hamza.

Env vars (Railway, never in code):
  WA_TOKEN        WhatsApp Business API permanent access token
  WA_PHONE_ID     Phone Number ID
  WA_VERIFY       Webhook verify token (any random string you choose)
  ADMIN_WA        Hamza's WhatsApp number (e.g. 212600000000) for admin commands
  SHOP_API_KEY    Shop Reseller API key (for live product data)
  SHOP_BASE_URL   Shop API host (default: worker-production-53ca.up.railway.app)
  PORT            (Railway sets this)
"""
import html
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

VERSION = "2026-10-06-w1"

# ---------------------------------------------------------------- config ---
def clean(v):
    return "".join(c for c in (v or "").strip() if ord(c) < 128 and not c.isspace())

WA_TOKEN = clean(os.environ.get("WA_TOKEN", ""))
WA_PHONE_ID = clean(os.environ.get("WA_PHONE_ID", ""))
WA_VERIFY = os.environ.get("WA_VERIFY", "hamza_verify_123").strip()
ADMIN_WA = "".join(c for c in os.environ.get("ADMIN_WA", "") if c.isdigit())
SHOP_API_KEY = clean(os.environ.get("SHOP_API_KEY", ""))
SHOP_BASE_URL = os.environ.get("SHOP_BASE_URL", "worker-production-53ca.up.railway.app").rstrip("/")

GRAPH = f"https://graph.facebook.com/v21.0/{WA_PHONE_ID}/messages"

_HERE = os.path.dirname(os.path.abspath(__file__))
CONV_FILE = os.path.join(_HERE, "conversations.json")
PAUSED_FILE = os.path.join(_HERE, "paused.flag")

# ---------------------------------------------------------------- helpers --
def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _save_json(path, data):
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(path + ".tmp", path)
    except Exception as e:
        print("save failed:", e)

CONV = _load_json(CONV_FILE, {})  # wa_id -> {name, lang, state, history:[...], paused}

def wa_send(to, text):
    """Send a WhatsApp text message via Business API."""
    if not WA_TOKEN or not WA_PHONE_ID:
        print("WA not configured, would send to", to, ":", text[:80])
        return False
    body = json.dumps({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text[:4000]},
    }).encode()
    req = urllib.request.Request(GRAPH, data=body, method="POST",
                                 headers={"Authorization": f"Bearer {WA_TOKEN}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read()).get("messages") is not None
    except Exception as e:
        print("wa_send failed:", e)
        return False

def shop(path):
    url = f"https://{SHOP_BASE_URL}{path}"
    req = urllib.request.Request(url, headers={"X-API-Key": SHOP_API_KEY})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

PRODUCTS = {"items": [], "ts": 0}
def get_products(force=False):
    if not force and time.time() - PRODUCTS["ts"] < 300 and PRODUCTS["items"]:
        return PRODUCTS["items"]
    try:
        res = shop("/api/products")
        items = res.get("products", res.get("items", [])) if isinstance(res, dict) else res
        PRODUCTS["items"] = items or []
        PRODUCTS["ts"] = time.time()
    except Exception as e:
        print("shop products failed:", e)
    return PRODUCTS["items"]

def prod_name(p, lang="ar"):
    for k in (f"name_{lang}", "name_en", "name_ar", "name_ru", "name", "title"):
        v = p.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return f"#{p.get('id', '?')}"

def prod_price(p):
    try:
        return round(float(p.get("price", 0)) * 1.30, 2)
    except Exception:
        return 0.0

# ------------------------------------------------------- smart replies ----
GREETINGS = ("سلام", "salam", "مرحبا", "مرحب", "hello", "hi", "hey",
             "bonjour", "salut", "cc", "صباح", "مساء", "اهلا", "أهلا")

INTENTS = [
    (("ثمن", "شحال", "prix", "price", "combien", "tarif", "سعر", "بشحال"),
     "price"),
    (("توصيل", "livraison", "delivery", "توصل", "شحن", "تجيب"),
     "delivery"),
    (("خلص", "دفع", "paiement", "payment", "pay", "بينانس", "binance", "usdt"),
     "payment"),
    (("منتج", "product", "produit", "شنو عندك", "catalogue", "كتالوج", "list"),
     "products"),
    (("طلب", "order", "commande", "كوموند", "بغيت نشري", "acheter", "buy"),
     "order"),
    (("انسان", "humain", "personne", "بشر", "مسؤول", "مول", "صاحب"),
     "human"),
    (("شكرا", "merci", "thanks", "thank"),
     "thanks"),
    (("باي", "bye", "au revoir", "سلامة", "الى اللقاء"),
     "bye"),
]

REPLIES = {
    "greeting": (
        "مرحباً بيك! 👋\n"
        "أنا المساعد الذكي ديال المتجر.\n\n"
        "شنو نقدر نعاونك فيه؟\n"
        "• 🛍️ المنتجات والأثمنة — كتب *منتجات*\n"
        "• 💳 طرق الدفع\n"
        "• 🚚 التوصيل\n"
        "• 📦 تتبع طلبك"
    ),
    "price": (
        "💰 الأثمنة ديالنا مناسبة بزاف!\n"
        "كتب ليا *اسم المنتج* اللي بغيتي (مثلاً: Netflix ولا ChatGPT) ونعطيك الثمن ديالو نيشان.\n\n"
        "ولا كتب *منتجات* باش تشوف اللائحة كاملة."
    ),
    "delivery": (
        "🚚 **التوصيل:**\n"
        "المنتجات الرقمية كتوصلك **فوراً** بعد تأكيد الدفع — عبر واتساب ولا تيليغرام.\n\n"
        "ما كاين حتى انتظار! ⚡"
    ),
    "payment": (
        "💳 **طرق الدفع المتوفرة:**\n"
        "• 🟡 Binance Pay\n"
        "• 🟢 USDT (BEP20 / TRC20)\n\n"
        "ملي تختار المنتج غادي نعطيك التفاصيل باش تخلص."
    ),
    "thanks": "العفو! 😊 إلا احتجتي شي حاجة أخرى أنا هنا.",
    "bye": "مع السلامة! 👋 نتمنى نشوفك قريب.",
    "human": (
        "تمام، غادي نوصّل رسالتك للمسؤول وهو غادي يجاوبك قريباً. 🙏\n"
        "كتب ليا شنو بغيتي توصّل ليه:"
    ),
    "fallback": (
        "سمح ليا، ما فهمتش مزيان 🤔\n\n"
        "جرب تكتب:\n"
        "• *منتجات* — لائحة المنتجات\n"
        "• *ثمن* + اسم المنتج\n"
        "• *مسؤول* — تهضر مع الإنسان"
    ),
}

def detect_intent(text):
    low = text.lower()
    if any(g in low for g in GREETINGS) and len(low) < 30:
        return "greeting"
    for keywords, intent in INTENTS:
        if any(k in low for k in keywords):
            return intent
    return None

def find_product(query):
    """Search products by name fragment."""
    q = query.lower().strip()
    if len(q) < 2:
        return []
    out = []
    for p in get_products():
        name = prod_name(p).lower()
        if q in name:
            out.append(p)
    return out[:5]

def handle_products(to):
    prods = get_products()
    if not prods:
        return "😔 دابا ما كاين حتى منتج متوفر. جرب من بعد!"
    lines = ["🛍️ **المنتجات المتوفرة:**\n"]
    for p in prods[:15]:
        name = prod_name(p)
        price = prod_price(p)
        stock = p.get("stock", p.get("stock_count", 1))
        mark = "🟢" if (isinstance(stock, (int, float)) and stock > 0) else "🔴"
        lines.append(f"{mark} {name} — **${price}**")
    if len(prods) > 15:
        lines.append(f"\n...و {len(prods) - 15} منتجات أخرى. كتب اسم منتج معين للبحث!")
    lines.append("\nكتب *اسم المنتج* باش تشوف التفاصيل.")
    return "\n".join(lines)

def handle_product_query(to, text):
    found = find_product(text)
    if not found:
        return ("🔍 ما لقيتش هاد المنتج.\n"
                "كتب *منتجات* باش تشوف اللائحة، ولا جرب اسم آخر.")
    lines = []
    for p in found:
        name = prod_name(p)
        price = prod_price(p)
        desc = p.get("description_ar") or p.get("description_en") or ""
        stock = p.get("stock", p.get("stock_count", 1))
        avail = "🟢 متوفر" if (isinstance(stock, (int, float)) and stock > 0) else "🔴 غير متوفر"
        lines.append(f"📦 **{name}**\n{avail} — **${price}**" +
                     (f"\n📝 {desc[:150]}" if desc else ""))
    lines.append("\nباش تشري، كتب: *بغيت نشري* + اسم المنتج")
    return "\n\n".join(lines)

def smart_reply(to, text, name=""):
    """Main reply logic. Returns reply text (or None if admin handles)."""
    # check pause
    if os.path.exists(PAUSED_FILE):
        return None  # admin handles manually

    conv = CONV.setdefault(to, {"name": name, "history": []})
    conv["history"].append({"from": "user", "text": text[:500], "ts": time.time()})
    conv["history"] = conv["history"][-20:]
    _save_json(CONV_FILE, CONV)

    # human handoff state
    if conv.get("awaiting_human_msg"):
        conv["awaiting_human_msg"] = False
        _save_json(CONV_FILE, CONV)
        notify_admin(f"🙋 زبون طلب المسؤول\n👤 {name} ({to})\n💬 {text[:500]}")
        return "✅ توصّلت برسالتك! المسؤول غادي يجاوبك قريباً. 🙏"

    intent = detect_intent(text)
    if intent == "greeting":
        reply = REPLIES["greeting"]
    elif intent == "products":
        reply = handle_products(to)
    elif intent == "human":
        conv["awaiting_human_msg"] = True
        _save_json(CONV_FILE, CONV)
        reply = REPLIES["human"]
    elif intent in REPLIES:
        reply = REPLIES[intent]
    elif intent == "order":
        reply = ("تمام! 🛒\n"
                 "كتب ليا *اسم المنتج* اللي بغيتي تشريه ونكمل معاك الخطوات.")
    else:
        # try product search before fallback
        found = find_product(text)
        if found:
            reply = handle_product_query(to, text)
        else:
            reply = REPLIES["fallback"]

    conv["history"].append({"from": "bot", "text": reply[:500], "ts": time.time()})
    _save_json(CONV_FILE, CONV)
    return reply

def notify_admin(text):
    """Send notification to Hamza (via Telegram bot if configured, else log)."""
    print("ADMIN NOTIFY:", text[:200])
    # TODO: forward to Telegram admin via bot API when TELEGRAM_ADMIN configured

# ------------------------------------------------------- admin commands ---
def handle_admin(text):
    """Hamza's commands. Returns reply or None."""
    parts = text.strip().split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "/help":
        return ("🤖 **أوامر الإدارة:**\n\n"
                "/stats — إحصائيات المحادثات\n"
                "/pause — إيقاف الردود التلقائية\n"
                "/resume — استئناف الردود\n"
                "/reply <رقم> <رسالة> — الرد على زبون\n"
                "/broadcast <رسالة> — رسالة جماعية ⚠️\n"
                "/products — تحديث لائحة المنتجات")
    if cmd == "/stats":
        n = len(CONV)
        total = sum(len(c.get("history", [])) for c in CONV.values())
        paused = "⏸️ متوقفة" if os.path.exists(PAUSED_FILE) else "▶️ خدامة"
        return f"📊 **الإحصائيات:**\n👥 الزبناء: {n}\n💬 الرسائل: {total}\n🤖 الحالة: {paused}"
    if cmd == "/pause":
        open(PAUSED_FILE, "w").write("1")
        return "⏸️ الردود التلقائية **توقفت**. نتا غادي تجاوب يدوياً دابا."
    if cmd == "/resume":
        if os.path.exists(PAUSED_FILE):
            os.remove(PAUSED_FILE)
        return "▶️ الردود التلقائية **خدامة** من جديد."
    if cmd == "/products":
        prods = get_products(force=True)
        return f"🔄 تحدثات اللائحة: {len(prods)} منتج."
    if cmd == "/reply" and arg:
        sp = arg.split(None, 1)
        if len(sp) == 2 and wa_send(sp[0], f"💬 {sp[1]}"):
            return f"✅ تصيفطات لـ {sp[0]}"
        return "⚠️ الاستعمال: /reply <رقم> <رسالة>"
    if cmd == "/broadcast" and arg:
        count = 0
        for wa_id in list(CONV.keys()):
            if wa_send(wa_id, f"📢 {arg}"):
                count += 1
            time.sleep(1)
        return f"📢 تصيفطات لـ {count} زبون."
    return None

# ------------------------------------------------------------ webhook -----
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if self.path.startswith("/webhook"):
            # Meta verification
            if (q.get("hub.mode") == ["subscribe"] and
                    q.get("hub.verify_token") == [WA_VERIFY]):
                challenge = q.get("hub.challenge", [""])[0]
                self.send_response(200); self.end_headers()
                self.wfile.write(challenge.encode())
                return
            self.send_response(403); self.end_headers(); return
        if self.path == "/health":
            self.send_response(200); self.end_headers()
            self.wfile.write(b"ok"); return
        if self.path == "/version":
            self.send_response(200); self.end_headers()
            self.wfile.write(f"wabot:{VERSION}".encode()); return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        if not self.path.startswith("/webhook"):
            self.send_response(404); self.end_headers(); return
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            payload = {}
        threading.Thread(target=process_payload, args=(payload,), daemon=True).start()
        self.send_response(200); self.end_headers()
        self.wfile.write(b"ok")

def process_payload(payload):
    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                val = change.get("value", {})
                for msg in val.get("messages", []):
                    if msg.get("type") != "text":
                        continue
                    wa_id = msg.get("from", "")
                    text = msg.get("text", {}).get("body", "")
                    name = val.get("contacts", [{}])[0].get("profile", {}).get("name", "")
                    print(f"INCOMING WA {wa_id}: {text[:80]}")
                    handle_message(wa_id, text, name)
    except Exception as e:
        print("payload error:", e)

def handle_message(wa_id, text, name=""):
    # admin?
    if ADMIN_WA and wa_id.endswith(ADMIN_WA[-9:]):
        reply = handle_admin(text)
        if reply:
            wa_send(wa_id, reply)
        return
    # customer
    reply = smart_reply(wa_id, text, name)
    if reply:
        wa_send(wa_id, reply)

def main():
    port = int(os.environ.get("PORT", "8080"))
    print(f"WhatsApp bot {VERSION} on :{port} (verify token: {WA_VERIFY[:4]}...)")
    HTTPServer(("0.0.0.0", port), H).serve_forever()

if __name__ == "__main__":
    main()

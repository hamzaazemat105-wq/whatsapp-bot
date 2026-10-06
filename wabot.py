#!/usr/bin/env python3
"""
WhatsApp Smart Bot for Hamza's e-commerce.
Receives customer messages via Meta webhook, replies intelligently in Darija,
and takes admin commands from Hamza (including product management).

Env vars (Railway, never in code):
  WA_TOKEN        WhatsApp Business API permanent access token
  WA_PHONE_ID     Phone Number ID
  WA_VERIFY       Webhook verify token (any random string you choose)
  ADMIN_WA        Hamza's WhatsApp number (e.g. 212600000000) for admin commands
  PORT            (Railway sets this)

Products are managed via WhatsApp admin commands:
  /addproduct <name> | <price> | <description>
  /delproduct <name>
  /products
"""
import html
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

VERSION = "2026-10-06-DISABLED"

# ---------------------------------------------------------------- config ---
def clean(v):
    return "".join(c for c in (v or "").strip() if ord(c) < 128 and not c.isspace())

WA_TOKEN = clean(os.environ.get("WA_TOKEN", ""))
WA_PHONE_ID = clean(os.environ.get("WA_PHONE_ID", ""))
WA_VERIFY = os.environ.get("WA_VERIFY", "hamza_verify_123").strip()
ADMIN_WA = "".join(c for c in os.environ.get("ADMIN_WA", "") if c.isdigit())

GRAPH = f"https://graph.facebook.com/v21.0/{WA_PHONE_ID}/messages"

_HERE = os.path.dirname(os.path.abspath(__file__))
CONV_FILE = os.path.join(_HERE, "conversations.json")
PAUSED_FILE = os.path.join(_HERE, "paused.flag")
PRODUCTS_FILE = os.path.join(_HERE, "products.json")

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
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode()[:500]
        except Exception:
            err_body = "<unreadable>"
        print(f"wa_send failed: HTTP {e.code}: {err_body}")
        return False
    except Exception as e:
        print("wa_send failed:", e)
        return False

def wa_send_audio(to, audio_url):
    """Send a WhatsApp audio/voice message via Business API."""
    if not WA_TOKEN or not WA_PHONE_ID:
        print("WA not configured, would send audio to", to, ":", audio_url[:80])
        return False
    body = json.dumps({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "audio",
        "audio": {"link": audio_url},
    }).encode()
    req = urllib.request.Request(GRAPH, data=body, method="POST",
                                 headers={"Authorization": f"Bearer {WA_TOKEN}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()).get("messages") is not None
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode()[:500]
        except Exception:
            err_body = "<unreadable>"
        print(f"wa_send_audio failed: HTTP {e.code}: {err_body}")
        return False
    except Exception as e:
        print("wa_send_audio failed:", e)
        return False

# ------------------------------------------------------- voice -----
# Voice messages recorded by Hamza, stored in audio/ directory.
# Maps intent -> audio filename. Served via /audio/<filename> endpoint.
# Hamza sends voice notes via WhatsApp; admin command /setvoice maps them.
VOICE_FILE = os.path.join(_HERE, "voice.json")

def get_voice_map():
    return _load_json(VOICE_FILE, {})

def save_voice_map(m):
    _save_json(VOICE_FILE, m)

def voice_url(filename):
    """Public URL for an audio file served by this bot."""
    base = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if base:
        return f"https://{base}/audio/{filename}"
    # fallback: construct from known domain
    return f"https://whatsapp-bot-production-ba5c.up.railway.app/audio/{filename}"

def send_voice_for_intent(to, intent):
    """Send Hamza's voice recording for an intent, if one is mapped. Returns True if sent."""
    vmap = get_voice_map()
    filename = vmap.get(intent)
    if not filename:
        return False
    fpath = os.path.join(_HERE, "audio", filename)
    if not os.path.exists(fpath):
        print(f"voice file missing: {filename}")
        return False
    return wa_send_audio(to, voice_url(filename))

# ------------------------------------------------------- products -----
# Products are managed by Hamza via WhatsApp admin commands.
# Stored in products.json: [{"name": "...", "price": "...", "desc": "..."}]
SEED_VERSION = 2  # bump when products_seed.json changes

def get_products():
    prods = _load_json(PRODUCTS_FILE, [])
    seed_ver = _load_json(os.path.join(_HERE, "seed_version.json"), 0)
    if not prods or seed_ver < SEED_VERSION:
        # first run or seed updated: load from bundled product list
        seed = _load_json(os.path.join(_HERE, "products_seed.json"), [])
        if seed:
            _save_json(PRODUCTS_FILE, seed)
            _save_json(os.path.join(_HERE, "seed_version.json"), SEED_VERSION)
            return seed
    return prods

def save_products(items):
    _save_json(PRODUCTS_FILE, items)

def _similarity(a, b):
    """Simple similarity ratio 0-1 for typo-tolerant matching."""
    a, b = a.lower(), b.lower()
    if not a or not b:
        return 0.0
    # exact substring = perfect
    if a in b or b in a:
        return 1.0
    # character overlap ratio
    set_a, set_b = set(a), set(b)
    overlap = len(set_a & set_b) / max(len(set_a | set_b), 1)
    # prefix bonus
    prefix = 0
    for x, y in zip(a, b):
        if x == y:
            prefix += 1
        else:
            break
    prefix_ratio = prefix / max(len(a), len(b), 1)
    return overlap * 0.5 + prefix_ratio * 0.5

def find_product(query):
    """Search products by name fragment, typo-tolerant."""
    q = query.lower().strip()
    # remove common filler words
    for w in ("بغيت", "بغيت نشري", "عطيني", "شحال", "ثمن", "ديال", "plus",
              "je veux", "acheter", "donne", "moi", "prix", "de", "le", "la", "un", "une"):
        q = q.replace(w, " ")
    q = " ".join(q.split())
    if len(q) < 2:
        return []
    scored = []
    for p in get_products():
        name = p.get("name", "").lower()
        # score each word of query against product name
        words = [w for w in q.split() if len(w) >= 2]
        if not words:
            continue
        # also try the whole query
        best = _similarity(q, name)
        for w in words:
            # check word against each word in product name
            for nw in name.split():
                s = _similarity(w, nw)
                if s > best:
                    best = s
        if best >= 0.55:
            scored.append((best, p))
    scored.sort(key=lambda x: -x[0])
    return [p for _, p in scored[:5]]

def handle_product_choice(to, text, matches):
    """Customer is choosing between multiple product variants. Fully generic."""
    low = text.lower().strip()
    # 1. number selection: "1", "2", "الاول", "premier", etc.
    num_words = {"1": 0, "2": 1, "3": 2, "4": 3, "5": 4,
                 "الاول": 0, "الأول": 0, "الاول": 0, "الزوج": 1, "الثاني": 1,
                 "premier": 0, "deuxième": 1, "deuxieme": 1, "troisième": 2,
                 "first": 0, "second": 1, "third": 2}
    for kw, idx in num_words.items():
        if kw in low and idx < len(matches):
            return format_single_product(matches[idx], to)
    # 2. find distinguishing words between variants
    #    (words that appear in one variant but not others)
    all_names = [p.get("name", "").lower() for p in matches]
    common = set(all_names[0].split())
    for n in all_names[1:]:
        common &= set(n.split())
    for p in matches:
        name_words = [w for w in p.get("name", "").lower().split()
                      if w not in common and len(w) >= 3]
        # also check description words
        desc_words = [w for w in p.get("desc", "").lower().split()
                      if len(w) >= 4][:5]
        for w in name_words + desc_words:
            if w in low:
                return format_single_product(p, to)
    # 3. variant keywords (shared/private, career/business, etc.)
    variant_kws = {
        "personnel": ("personnel", "privé", "prive", "خاص", "شخصي", "personal", "private"),
        "partagé": ("partagé", "partage", "مشترك", "shared"),
        "career": ("career", "carrière", "carriere"),
        "business": ("business", "entreprise"),
    }
    for p in matches:
        nl = p.get("name", "").lower()
        for vkey, kws in variant_kws.items():
            if vkey in nl and any(k in low for k in kws):
                return format_single_product(p, to)
    # 4. fuzzy match against variant names
    best, best_score = None, 0
    for p in matches:
        s = _similarity(low, p.get("name", "").lower())
        if s > best_score:
            best_score, best = s, p
    if best and best_score >= 0.5:
        return format_single_product(best, to)
    # 5. still unclear: list options again
    lines = ["ما فهمتش، ختار واحد من هادو 👇\n"]
    for i, p in enumerate(matches, 1):
        lines.append(f"{i}️⃣ {p.get('name', '?')} — **{p.get('price', '?')}**")
    return "\n".join(lines)

def format_single_product(p, to=None):
    """Show product and guide toward purchase. Sets conversational state."""
    desc = p.get("desc", "")
    out = (f"📦 **{p.get('name', '?')}**\n"
           f"💰 الثمن: **{p.get('price', '?')}**")
    if desc:
        out += f"\n📝 {desc[:200]}"
    out += ("\n\n👌 **بغيتي تاخدو؟**\n"
            "كتب *اه* باش نكملو، ولا *لا* باش تشوف منتجات أخرى")
    # track interest for conversation flow
    if to:
        conv = CONV.setdefault(to, {"name": "", "history": []})
        conv["interested_product"] = p.get("name", "")
        conv["awaiting_purchase_decision"] = True
        _save_json(CONV_FILE, CONV)
    return out

YES_WORDS = ("اه", "نعم", "واخا", "ok", "oui", "yes", "بغيتو", "سير", "ناخدو",
             "تمام", "صافي", "يلا", "بغيت", "موافق")
NO_WORDS = ("لا", "non", "no", "بلاش", "ما بغيتش", "مابغيتش")

def detect_yes_no(text):
    low = text.lower().strip()
    if any(w == low or low.startswith(w + " ") or low.endswith(" " + w) or f" {w} " in f" {low} "
           for w in YES_WORDS):
        return "yes"
    if any(w == low or low.startswith(w + " ") or low.endswith(" " + w) or f" {w} " in f" {low} "
           for w in NO_WORDS):
        return "no"
    return None

# ------------------------------------------------------- smart replies ----
GREETINGS = ("سلام", "salam", "مرحبا", "مرحب", "hello", "hi", "hey",
             "bonjour", "salut", "cc", "صباح", "مساء", "اهلا", "أهلا")

INTENTS = [
    (("ثمن", "شحال", "prix", "price", "combien", "tarif", "سعر", "بشحال",
      "شحال داير", "شحال ثمن", "بكم", "كم السعر"),
     "price"),
    (("توصيل", "livraison", "delivery", "توصل", "شحن", "تجيب",
      "وقتاش", "شحال الوقت", "مدة التوصيل", "امتى توصل"),
     "delivery"),
    (("خلص", "دفع", "paiement", "payment", "pay", "بينانس", "binance", "usdt",
      "كيفاش نخلص", "طريقة الدفع", "comment payer", "نخلص", "الدفع"),
     "payment"),
    (("منتج", "product", "produit", "شنو عندك", "catalogue", "كتالوج", "list",
      "شنو عندكم", "شنو كتبيعو", "شنو متوفر", "لائحة", "liste", "اش عندك"),
     "products"),
    (("طلب", "order", "commande", "كوموند", "بغيت نشري", "acheter", "buy",
      "بغيت ناخد", "ناخد", "نشري"),
     "order"),
    (("انسان", "humain", "personne", "بشر", "مسؤول", "مول", "صاحب"),
     "human"),
    (("شكرا", "merci", "thanks", "thank", "متشكر"),
     "thanks"),
    (("باي", "bye", "au revoir", "سلامة", "الى اللقاء"),
     "bye"),
    (("متوفر", "كاين", "موجود", "disponible", "dispo", "واش كاين"),
     "availability"),
]

REPLIES = {
    "greeting": (
        "مرحباً بيك أخي! 👋\n"
        "كيف نقدر نعاونك اليوم؟ 😊\n\n"
        "عندنا خدمات رقمية بأثمنة مناسبة:\n"
        "• 🤖 ChatGPT Plus\n"
        "• 🎨 Canva Pro\n"
        "• 🎬 CapCut Pro\n"
        "• 💎 وغيرهم...\n\n"
        "شنو كتقلب على؟ كتب ليا اسم المنتج ولا *منتجات* باش تشوف الكل"
    ),
    "price": (
        "💰 الأثمنة ديالنا مناسبة بزاف!\n"
        "كتب ليا *اسم المنتج* اللي بغيتي (مثلاً: Netflix ولا ChatGPT) ونعطيك الثمن ديالو نيشان.\n\n"
        "ولا كتب *منتجات* باش تشوف اللائحة كاملة."
    ),
    "delivery": (
        "🚚 **التوصيل:**\n"
        "خدمات رقمية — التوصيل خلال **10 دقائق** ⏱️ بعد تأكيد الدفع.\n\n"
        "التوصيل عبر واتساب نيشان 📩"
    ),
    "payment": (
        "💳 **طريقة الدفع:**\n\n"
        "شنو البنك اللي عندك؟ 🏦\n\n"
        "1️⃣ CIH\n"
        "2️⃣ Cash Plus\n"
        "3️⃣ التجاري وفا بنك\n\n"
        "كتب ليا الرقم ولا اسم البنك 👇"
    ),
    "thanks": "العفو! 😊 إلا احتجتي شي حاجة أخرى أنا هنا.",
    "bye": "مع السلامة! 👋 نتمنى نشوفك قريب.",
    "availability": (
        "✅ اه، كلشي **متوفر** دابا! 🟢\n\n"
        "شنو بغيتي؟ كتب ليا اسم المنتج 👇"
    ),
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

# ------------------------------------------------------- payment details ---
# Hamza's payment methods. Bot asks client which bank they have,
# then sends the matching details. Defaults to CIH.
PAYMENT_DETAILS = {
    "cih": (
        "🏦 **CIH Bank**\n\n"
        "💳 البطاقة:\n"
        "5025483211017100\n\n"
        "🔢 RIB:\n"
        "230640502548321101710027\n\n"
        "👤 الاسم: AZEMAT Hamza\n\n"
        "ملي تخلص، صيفط ليا **سكرينشوت** التحويل 📸"
    ),
    "cashplus": (
        "💰 **Cash Plus**\n\n"
        "📱 الرقم:\n"
        "0627469836\n\n"
        "ملي تخلص، صيفط ليا **سكرينشوت** التحويل 📸"
    ),
    "tijari": (
        "🏦 **التجاري وفا بنك**\n\n"
        "🔢 RIB:\n"
        "007640001004200030300712\n\n"
        "👤 الاسم: Azemat hamza\n\n"
        "ملي تخلص، صيفط ليا **سكرينشوت** التحويل 📸"
    ),
}

BANK_KEYWORDS = {
    "cih": ("cih", "سياش", "1"),
    "cashplus": ("cash", "كاش", "plus", "بلس", "2"),
    "tijari": ("tijari", "تجاري", "وفا", "wafa", "3"),
}

def detect_bank(text):
    """Detect which bank the client chose. Returns key or None."""
    low = text.lower()
    for key, kws in BANK_KEYWORDS.items():
        if any(k in low for k in kws):
            return key
    return None

def handle_payment_choice(text):
    """Client answered the bank question. Returns reply text."""
    bank = detect_bank(text)
    if bank:
        return PAYMENT_DETAILS[bank]
    # no bank matched — default to CIH as Hamza instructed
    return ("ما فهمتش البنك بالضبط، ها معلومات **CIH** (تقدر تختار غيرو):\n\n"
            + PAYMENT_DETAILS["cih"])

def detect_intent(text):
    low = text.lower()
    if any(g in low for g in GREETINGS) and len(low) < 30:
        return "greeting"
    for keywords, intent in INTENTS:
        if any(k in low for k in keywords):
            return intent
    return None

def handle_products(to):
    prods = get_products()
    if not prods:
        return ("🛍️ **Produits:**\n\n"
                "Aucun produit pour le moment.\n"
                "Contactez-nous pour la liste! 📩")
    lines = ["🛍️ **Produits disponibles:**\n"]
    for p in prods[:20]:
        lines.append(f"🟢 {p.get('name', '?')} — **{p.get('price', '?')}**")
    if len(prods) > 20:
        lines.append(f"\n...et {len(prods) - 20} autres produits.")
    lines.append("\nÉcris le *nom du produit* pour voir les détails.")
    return "\n".join(lines)

def handle_product_query(to, text):
    found = find_product(text)
    if not found:
        return ("🔍 Produit non trouvé.\n"
                "Écris *produits* pour voir la liste.")
    if len(found) == 1:
        return format_single_product(found[0], to)
    # multiple variants: ask which one (e.g. ChatGPT shared vs private)
    conv = CONV.setdefault(to, {"name": "", "history": []})
    conv["awaiting_product_choice"] = [p.get("name", "") for p in found]
    _save_json(CONV_FILE, CONV)
    lines = ["🤔 كاين جوج أنواع — **شنو بغيتي؟** 👇\n"]
    for i, p in enumerate(found, 1):
        lines.append(f"{i}️⃣ {p.get('name', '?')} — **{p.get('price', '?')}**")
    lines.append("\nكتب *مشترك* ولا *خاص* (ولا الرقم)")
    return "\n".join(lines)

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

    # payment bank choice state
    if conv.get("awaiting_payment_choice"):
        conv["awaiting_payment_choice"] = False
        _save_json(CONV_FILE, CONV)
        reply = handle_payment_choice(text)
        conv["history"].append({"from": "bot", "text": reply[:500], "ts": time.time()})
        _save_json(CONV_FILE, CONV)
        return reply

    # product variant choice state (e.g. ChatGPT shared vs private)
    if conv.get("awaiting_product_choice"):
        names = conv.pop("awaiting_product_choice")
        _save_json(CONV_FILE, CONV)
        matches = [p for p in get_products() if p.get("name", "") in names]
        reply = handle_product_choice(to, text, matches)
        conv["history"].append({"from": "bot", "text": reply[:500], "ts": time.time()})
        _save_json(CONV_FILE, CONV)
        return reply

    # purchase decision state (client said yes/no to buying)
    if conv.get("awaiting_purchase_decision"):
        yn = detect_yes_no(text)
        if yn == "yes":
            conv.pop("awaiting_purchase_decision", None)
            conv["awaiting_payment_choice"] = True
            _save_json(CONV_FILE, CONV)
            prod = conv.get("interested_product", "")
            reply = (f"ممتاز! 🎉 اخترتي **{prod}**\n\n" + REPLIES["payment"])
            conv["history"].append({"from": "bot", "text": reply[:500], "ts": time.time()})
            _save_json(CONV_FILE, CONV)
            return reply
        elif yn == "no":
            conv.pop("awaiting_purchase_decision", None)
            conv.pop("interested_product", None)
            _save_json(CONV_FILE, CONV)
            reply = ("ما مشكل! 👍\n\n" + handle_products(to))
            conv["history"].append({"from": "bot", "text": reply[:500], "ts": time.time()})
            _save_json(CONV_FILE, CONV)
            return reply
        # unclear: check if they mentioned another product instead
        found = find_product(text)
        if found:
            conv.pop("awaiting_purchase_decision", None)
            _save_json(CONV_FILE, CONV)
            # fall through to normal product handling below
        else:
            reply = "كتب *اه* باش نكملو الشراء، ولا *لا* باش تشوف منتجات أخرى 😊"
            conv["history"].append({"from": "bot", "text": reply[:500], "ts": time.time()})
            _save_json(CONV_FILE, CONV)
            return reply

    intent = detect_intent(text)
    if intent == "greeting":
        reply = REPLIES["greeting"]
    elif intent == "products":
        reply = handle_products(to)
    elif intent == "payment":
        conv["awaiting_payment_choice"] = True
        _save_json(CONV_FILE, CONV)
        reply = REPLIES["payment"]
    elif intent == "human":
        conv["awaiting_human_msg"] = True
        _save_json(CONV_FILE, CONV)
        reply = REPLIES["human"]
    elif intent in REPLIES:
        # contextual: if asking price and we know which product they mean
        if intent == "price" and conv.get("interested_product"):
            pname = conv["interested_product"]
            for p in get_products():
                if p.get("name", "") == pname:
                    reply = (f"💰 **{pname}**\nالثمن: **{p.get('price', '?')}**\n\n"
                             f"👌 بغيتي تاخدو؟ كتب *اه*")
                    conv["awaiting_purchase_decision"] = True
                    _save_json(CONV_FILE, CONV)
                    break
            else:
                reply = REPLIES[intent]
        else:
            reply = REPLIES[intent]
    elif intent == "order":
        reply = ("تمام! 🛒 ختار المنتج اللي بغيتي 👇\n\n" + handle_products(to))
    else:
        # try product search before fallback
        found = find_product(text)
        if found:
            reply = handle_product_query(to, text)
        else:
            # Hamza's rule: when confused, send product list (keeps client engaged)
            reply = ("🤔 هاك شوف شنو عندنا 👇\n\n" + handle_products(to))
            # log unknown query for learning
            log_unknown_query(text)

    conv["history"].append({"from": "bot", "text": reply[:500], "ts": time.time()})
    _save_json(CONV_FILE, CONV)
    return reply

def log_unknown_query(text):
    """Track questions the bot couldn't answer so Hamza can teach it."""
    if len(text.strip()) < 3:
        return
    path = os.path.join(_HERE, "unknown_queries.json")
    unknown = _load_json(path, [])
    # increment count if already logged
    for q in unknown:
        if q.get("text", "").lower() == text.lower().strip():
            q["count"] = q.get("count", 1) + 1
            q["last"] = time.time()
            _save_json(path, unknown)
            return
    unknown.append({"text": text.strip()[:200], "count": 1, "last": time.time()})
    _save_json(path, unknown[-50:])  # keep last 50

def notify_admin(text):
    """Send notification to Hamza on WhatsApp (and log)."""
    print("ADMIN NOTIFY:", text[:200])
    if ADMIN_WA:
        wa_send(ADMIN_WA, text)

# ------------------------------------------------------- admin commands ---
def handle_admin(text):
    """Hamza's commands. Returns reply or None."""
    parts = text.strip().split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "/help":
        return ("🤖 **أوامر الإدارة:**\n\n"
                "📦 **المنتجات:**\n"
                "/addproduct <اسم> | <ثمن> | <وصف> — زيد منتج\n"
                "/delproduct <اسم> — مسح منتج\n"
                "/products — لائحة المنتجات\n\n"
                "🎙️ **الصوت:**\n"
                "/setvoice <الموضوع> — ربط فويس نوت بموضوع\n"
                "/voices — لائحة الأصوات\n"
                "/delvoice <الموضوع> — مسح صوت\n\n"
                "⚙️ **التحكم:**\n"
                "/stats — إحصائيات المحادثات\n"
                "/chats — آخر المحادثات (للمراجعة)\n"
                "/chat <رقم> — المحادثة الكاملة مع كليان\n"
                "/unknown — الأسئلة الغير مفهومة\n"
                "/clearunknown — مسح الأسئلة\n"
                "/pause — إيقاف الردود التلقائية\n"
                "/resume — استئناف الردود\n"
                "/reply <رقم> <رسالة> — الرد على زبون\n"
                "/broadcast <رسالة> — رسالة جماعية ⚠️")
    if cmd == "/stats":
        n = len(CONV)
        total = sum(len(c.get("history", [])) for c in CONV.values())
        paused = "⏸️ متوقفة" if os.path.exists(PAUSED_FILE) else "▶️ خدامة"
        unknown = _load_json(os.path.join(_HERE, "unknown_queries.json"), [])
        return (f"📊 **الإحصائيات:**\n👥 الزبناء: {n}\n💬 الرسائل: {total}\n"
                f"🤖 الحالة: {paused}\n❓ أسئلة غير مفهومة: {len(unknown)}")
    if cmd == "/chats":
        # show recent conversations for Hamza to review
        if not CONV:
            return "📭 ما كاين حتى محادثة."
        lines = ["💬 **آخر المحادثات:**\n"]
        items = sorted(CONV.items(),
                       key=lambda x: x[1].get("history", [{}])[-1].get("ts", 0) if x[1].get("history") else 0,
                       reverse=True)[:5]
        for wa_id, c in items:
            name = c.get("name", "?")
            hist = c.get("history", [])[-4:]
            lines.append(f"\n👤 {name} ({wa_id}):")
            for h in hist:
                who = "🧑" if h.get("from") == "user" else "🤖"
                lines.append(f"  {who} {h.get('text', '')[:80]}")
        lines.append("\n💡 باش تشوف محادثة كاملة: /chat <الرقم>")
        lines.append("💡 راجع المحادثات وقول ليا شنو نصلح!")
        return "\n".join(lines)
    if cmd == "/chat":
        # show FULL conversation with a specific client: /chat <number>
        target = arg.strip().replace(" ", "").replace("+", "")
        if not target:
            return "📝 الاستعمال: /chat <رقم الهاتف>\nمثال: /chat 0612345678"
        # find matching conversation (partial match on last digits)
        match_id = None
        for wa_id in CONV:
            if wa_id.endswith(target[-9:]) or target[-9:] in wa_id:
                match_id = wa_id
                break
        if not match_id:
            return f"❌ ما لقيت حتى محادثة مع {target}"
        c = CONV[match_id]
        name = c.get("name", "?")
        hist = c.get("history", [])
        if not hist:
            return f"📭 ما كاين حتى رسالة مع {name}."
        lines = [f"💬 **محادثة كاملة مع {name} ({match_id}):**\n"]
        for h in hist:
            who = "🧑 كليان" if h.get("from") == "user" else "🤖 بوت"
            lines.append(f"{who}: {h.get('text', '')}")
            lines.append("")
        return "\n".join(lines)[:4000]
    if cmd == "/unknown":
        # show questions the bot didn't understand (learning)
        unknown = _load_json(os.path.join(_HERE, "unknown_queries.json"), [])
        if not unknown:
            return "✅ ما كاين حتى سؤال غير مفهوم!"
        lines = ["❓ **أسئلة ما فهمتهاش:**\n"]
        for q in unknown[-10:]:
            lines.append(f"• {q.get('text', '')[:100]} ({q.get('count', 1)}x)")
        lines.append("\n💡 قول ليا الجواب المناسب لكل سؤال!")
        return "\n".join(lines)
    if cmd == "/clearunknown":
        _save_json(os.path.join(_HERE, "unknown_queries.json"), [])
        return "🗑️ تمسحات الأسئلة الغير مفهومة."
    if cmd == "/pause":
        open(PAUSED_FILE, "w").write("1")
        return "⏸️ الردود التلقائية **توقفت**. نتا غادي تجاوب يدوياً دابا."
    if cmd == "/resume":
        if os.path.exists(PAUSED_FILE):
            os.remove(PAUSED_FILE)
        return "▶️ الردود التلقائية **خدامة** من جديد."
    if cmd == "/products":
        prods = get_products()
        if not prods:
            return "📦 ما كاين حتى منتج. زيد بـ /addproduct"
        lines = [f"📦 **المنتجات ({len(prods)}):**\n"]
        for p in prods:
            lines.append(f"• {p.get('name')} — {p.get('price')}")
        return "\n".join(lines)
    if cmd == "/addproduct" and arg:
        parts_p = [x.strip() for x in arg.split("|")]
        if len(parts_p) < 2:
            return "⚠️ الاستعمال:\n/addproduct <اسم> | <ثمن> | <وصف اختياري>\n\nمثال:\n/addproduct Netflix 1 شهر | 50 درهم | حساب خاص"
        prods = get_products()
        # replace if same name exists
        prods = [p for p in prods if p.get("name", "").lower() != parts_p[0].lower()]
        prods.append({"name": parts_p[0], "price": parts_p[1],
                      "desc": parts_p[2] if len(parts_p) > 2 else ""})
        save_products(prods)
        return f"✅ تزاد المنتج: **{parts_p[0]}** — {parts_p[1]}"
    if cmd == "/delproduct" and arg:
        prods = get_products()
        before = len(prods)
        prods = [p for p in prods if arg.lower() not in p.get("name", "").lower()]
        if len(prods) == before:
            return f"⚠️ ما لقيتش منتج باسم '{arg}'"
        save_products(prods)
        return f"🗑️ تمسح. بقاو {len(prods)} منتجات."
    if cmd == "/voices":
        vmap = get_voice_map()
        if not vmap:
            return ("🎙️ **الرسائل الصوتية:**\n\n"
                    "ما كاين حتى تسجيل.\n"
                    "صيفط فويس نوت + /setvoice <الموضوع>")
        lines = ["🎙️ **الرسائل الصوتية:**\n"]
        for intent, fname in vmap.items():
            lines.append(f"• {intent} → {fname}")
        return "\n".join(lines)
    if cmd in ("/setvoice", "/setboice") and arg:
        # Usage: /setvoice <intent> — then send the voice note as the next message
        intent = arg.strip().lower()
        # find admin conversation to store pending intent
        # (we don't have wa_id here, so store globally keyed by intent expectation)
        # Instead: store in a temp file that handle_admin_audio will check
        vmap = get_voice_map()
        vmap[intent] = f"{intent}.ogg"
        save_voice_map(vmap)
        # mark pending in CONV for admin (find admin wa_id)
        for wid, c in CONV.items():
            if ADMIN_WA and wid.endswith(ADMIN_WA[-9:]):
                c["pending_voice_intent"] = intent
        _save_json(CONV_FILE, CONV)
        # also save to a simple pending file as fallback
        try:
            with open(os.path.join(_HERE, "pending_voice.txt"), "w") as f:
                f.write(intent)
        except Exception:
            pass
        return (f"🎙️ تسجل الموضوع: **{intent}**\n\n"
                f"دابا صيفط ليا **الفويس نوت** هنا في واتساب 📩\n\n"
                f"المواضيع: greeting, products, price, payment, delivery, order, thanks, bye, product, fallback")
    if cmd == "/delvoice" and arg:
        vmap = get_voice_map()
        if arg.strip().lower() in vmap:
            del vmap[arg.strip().lower()]
            save_voice_map(vmap)
            return f"🗑️ تمسح الصوت ديال {arg}"
        return f"⚠️ ما لقيتش صوت للموضوع '{arg}'"
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
        if self.path.startswith("/audio/"):
            # Serve Hamza's voice recordings
            fname = os.path.basename(urllib.parse.urlparse(self.path).path)
            if not fname or "/" in fname or fname.startswith("."):
                self.send_response(400); self.end_headers(); return
            fpath = os.path.join(_HERE, "audio", fname)
            if not os.path.exists(fpath):
                self.send_response(404); self.end_headers(); return
            ctype = "audio/ogg" if fname.endswith(".ogg") else "audio/mpeg"
            try:
                with open(fpath, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                print("audio serve error:", e)
                self.send_response(500); self.end_headers()
            return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        if not self.path.startswith("/webhook"):
            self.send_response(404); self.end_headers(); return
        # Some clients/proxies send Expect: 100-continue; answer it before reading body
        if self.headers.get("Expect", "").lower() == "100-continue":
            self.send_response_only(100)
            self.end_headers()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        try:
            raw = self.rfile.read(length) if length > 0 else b""
            payload = json.loads(raw or b"{}")
        except Exception as e:
            print("webhook read error:", e)
            payload = {}
        print(f"WEBHOOK POST hit, payload keys: {list(payload.keys())}")
        threading.Thread(target=process_payload, args=(payload,), daemon=True).start()
        self.send_response(200); self.end_headers()
        self.wfile.write(b"ok")

def wa_download_media(media_id, save_path):
    """Download media from WhatsApp API and save to file."""
    if not WA_TOKEN:
        print("WA not configured, cannot download media")
        return False
    try:
        # Step 1: get download URL
        url = f"https://graph.facebook.com/v21.0/{media_id}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {WA_TOKEN}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            info = json.loads(r.read())
        dl_url = info.get("url")
        if not dl_url:
            print("no download url for media", media_id)
            return False
        # Step 2: download file
        req2 = urllib.request.Request(dl_url, headers={"Authorization": f"Bearer {WA_TOKEN}"})
        with urllib.request.urlopen(req2, timeout=60) as r2:
            data = r2.read()
        with open(save_path, "wb") as f:
            f.write(data)
        print(f"downloaded media {media_id} -> {save_path} ({len(data)} bytes)")
        return True
    except Exception as e:
        print("wa_download_media failed:", e)
        return False

def handle_admin_audio(wa_id, media_id):
    """Hamza sent a voice note — save it for the pending /setvoice intent."""
    conv = CONV.get(wa_id, {})
    pending = conv.get("pending_voice_intent")
    if not pending:
        # fallback: check pending file
        try:
            pf = os.path.join(_HERE, "pending_voice.txt")
            if os.path.exists(pf):
                with open(pf) as f:
                    pending = f.read().strip()
        except Exception:
            pass
    if not pending:
        wa_send(wa_id, "🎙️ صيفط /setvoice <الموضوع> أولاً، من بعد صيفط الفويس نوت.")
        return
    filename = f"{pending}.ogg"
    fpath = os.path.join(_HERE, "audio", filename)
    os.makedirs(os.path.join(_HERE, "audio"), exist_ok=True)
    if wa_download_media(media_id, fpath):
        conv.pop("pending_voice_intent", None)
        _save_json(CONV_FILE, CONV)
        try:
            os.remove(os.path.join(_HERE, "pending_voice.txt"))
        except Exception:
            pass
        wa_send(wa_id, f"✅ تحفظ الصوت للموضوع: **{pending}** 🎙️\nدابا الكليان غادي يسمع صوتك!")
    else:
        wa_send(wa_id, "⚠️ فشل تحميل الصوت. عاود المحاولة.")

BOT_DISABLED = True  # cancelled by Hamza 2026-10-06

def process_payload(payload):
    if BOT_DISABLED:
        print("Bot disabled, ignoring webhook")
        return
    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                val = change.get("value", {})
                msgs = val.get("messages", [])
                if msgs:
                    print(f"WEBHOOK: {len(msgs)} message(s) from {val.get('contacts', [{}])[0].get('wa_id', '?')}")
                for msg in msgs:
                    wa_id = msg.get("from", "")
                    name = val.get("contacts", [{}])[0].get("profile", {}).get("name", "")
                    mtype = msg.get("type", "")
                    # log referral (ad clicks) for debugging
                    if msg.get("referral"):
                        print(f"AD REFERRAL from {wa_id}: {msg['referral'].get('source_type', '?')}")
                    # admin voice notes
                    if mtype == "audio" and ADMIN_WA and wa_id.endswith(ADMIN_WA[-9:]):
                        media_id = msg.get("audio", {}).get("id", "")
                        print(f"INCOMING WA AUDIO {wa_id}: {media_id}")
                        handle_admin_audio(wa_id, media_id)
                        continue
                    if mtype != "text":
                        print(f"SKIP non-text {mtype} from {wa_id}")
                        continue
                    text = msg.get("text", {}).get("body", "")
                    if not text.strip():
                        print(f"SKIP empty text from {wa_id}")
                        continue
                    print(f"INCOMING WA {wa_id}: {text[:80]}")
                    try:
                        handle_message(wa_id, text, name)
                    except Exception as e:
                        print(f"HANDLE ERROR for {wa_id}: {e}")
                        # fallback: try simple reply so client isn't ignored
                        try:
                            wa_send(wa_id, "مرحباً! 👋\n" + handle_products(wa_id))
                        except Exception as e2:
                            print(f"FALLBACK FAILED: {e2}")
    except Exception as e:
        print("payload error:", e)

def handle_message(wa_id, text, name=""):
    # admin? (admin commands still handled; anything else falls through to
    # the customer flow so Hamza can test the bot as a customer from his
    # own number)
    if ADMIN_WA and wa_id.endswith(ADMIN_WA[-9:]):
        reply = handle_admin(text)
        if reply:
            wa_send(wa_id, reply)
            return
    # customer — forward copy to admin so Hamza sees it in his WhatsApp
    is_new = wa_id not in CONV or not CONV[wa_id].get("history")
    fwd = f"📩 من {name or wa_id} ({wa_id}):\n{text[:500]}"
    notify_admin(fwd)
    if is_new:
        notify_admin(f"🆕 كليان جديد: {name or wa_id} ({wa_id})")
    # customer
    reply = smart_reply(wa_id, text, name)
    if reply:
        wa_send(wa_id, reply)
        # forward bot's reply to admin too
        notify_admin(f"🤖 رد البوت على {name or wa_id}:\n{reply[:500]}")
        # send Hamza's voice recording for this intent (if mapped)
        intent = detect_intent(text)
        if not intent and find_product(text):
            intent = "product"
        if intent:
            send_voice_for_intent(wa_id, intent)

def main():
    port = int(os.environ.get("PORT", "8080"))
    print(f"WhatsApp bot {VERSION} on :{port} (verify token: {WA_VERIFY[:4]}...)")
    HTTPServer(("0.0.0.0", port), H).serve_forever()

if __name__ == "__main__":
    main()

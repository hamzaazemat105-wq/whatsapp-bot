# بوت واتساب الذكي — دليل الإعداد

## شنو كيدير
بوت واتساب ذكي كيجاوب الكليان تلقائياً بالدارجة:
- 👋 الترحيب والتعريف
- 🛍️ لائحة المنتجات (مربوطة مع API المورّد)
- 💰 الأثمنة والبحث عن منتج
- 💳 طرق الدفع (Binance / USDT)
- 🚚 التوصيل
- 🙋 تحويل للمسؤول ملي ما يفهمش

**أوامر الإدارة** (من رقم حمزة فقط):
- `/help` — لائحة الأوامر
- `/stats` — إحصائيات المحادثات
- `/pause` — إيقاف الردود التلقائية
- `/resume` — استئناف الردود
- `/reply <رقم> <رسالة>` — الرد على زبون معين
- `/broadcast <رسالة>` — رسالة جماعية ⚠️
- `/products` — تحديث لائحة المنتجات

## خطوات التشغيل

### 1. في Meta (developers.facebook.com)
1. الـ App ديالك ← **WhatsApp** ← **Configuration**
2. في **Webhook**:
   - Callback URL: `https://YOUR-RAILWAY-URL/webhook`
   - Verify token: القيمة اللي درتي في `WA_VERIFY`
   - Subscribe to: **messages**
3. في **API Setup**: نسخ **Phone Number ID**

### 2. في Railway (سيرفس جديدة)
Environment Variables:
```
WA_TOKEN=<المفتاح الدائم>
WA_PHONE_ID=<Phone Number ID>
WA_VERIFY=<كلمة سر من اختيارك>
ADMIN_WA=<رقم حمزة: 212XXXXXXXXX>
SHOP_API_KEY=<مفتاح API المورّد>
SHOP_BASE_URL=worker-production-53ca.up.railway.app
```

### 3. التحقق
- `https://YOUR-RAILWAY-URL/health` → `ok`
- `https://YOUR-RAILWAY-URL/version` → `wabot:...`
- صيفط رسالة واتساب للرقم وشوف الرد التلقائي ✅

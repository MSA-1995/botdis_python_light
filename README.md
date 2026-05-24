# BotDis Python Light

نسخة Python خفيفة من نظام الرومات المؤقتة، مناسبة لـ Koyeb.

## التشغيل المحلي

```bash
pip install -r requirements.txt
copy .env.example .env
python main.py
```

ضع `BOT_TOKEN` في ملف `.env`.

## التشغيل على Koyeb

1. ارفع هذا المجلد إلى GitHub.
2. في Koyeb أنشئ Web Service جديد.
3. Runtime: Python.
4. Start command:

```bash
python main.py
```

5. أضف Environment Variables:

```env
BOT_TOKEN=توكن_البوت
GUILD_ID=ايدي_السيرفر
PORT=8000
INSTANCE_LOCK_CHANNEL_ID=ايدي_روم_اللوق
SINGLETON_CHANNEL_NAME=🤖・bot-status
MUSIC_SEARCH_PROVIDER=soundcloud
MUSIC_AUDIO_MODE=pcm
MUSIC_DOWNLOAD_BEFORE_PLAY=true
FFMPEG_EXECUTABLE=/usr/bin/ffmpeg
VOICE_BOT_1=توكن_بوت_مساعد
VOICE_BOT_2=توكن_بوت_مساعد
VOICE_BOT_3=توكن_بوت_مساعد
VOICE_BOT_4=توكن_بوت_مساعد
VOICE_BOT_5=توكن_بوت_مساعد
```

في Discord Developer Portal فعّل:

- Server Members Intent
- Message Content Intent غير مطلوب لهذه النسخة

بعد تشغيل البوت في السيرفر:

```text
/setup
```

أو أضف هذه المتغيرات بدل استخدام `/setup`:

```env
CATEGORY_ID=ايدي_الكاتقوري
JOIN_CHANNEL_ID=ايدي_روم_انشاء_روم
LOG_CHANNEL_ID=ايدي_روم_اللوق
```

## الأوامر

- `/setup` إعداد نظام الرومات.
- `/room_info` معلومات رومك.
- `/room_panel` إرسال لوحة تحكم جديدة.
- `/room_claim` أخذ ملكية روم بلا مالك.
- `/room_kick` طرد عضو من رومك.
- `/room_ban` حظر عضو من رومك.
- `/room_unban` فك حظر عضو.
- `/room_trust` السماح لعضو بالدخول.
- `/room_transfer` نقل الملكية.
- `/play` تشغيل أغنية من رابط أو بحث.
- `/skip` تخطي الأغنية الحالية.
- `/stop` إيقاف الموسيقى ومسح القائمة.
- `/queue` عرض قائمة التشغيل.

## ملاحظات

- النسخة خفيفة: بدون موسيقى وبدون MongoDB.
- يوجد Health Server داخلي على `PORT` حتى يقبل Koyeb تشغيل الخدمة.
- يحتاج البوت صلاحيات: Manage Channels, Move Members, View Channels, Connect.

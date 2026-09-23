# دليل نشر MaxPay Portal على VPS

**لمن:** التقني الذي يستلم المشروع ويشغّله على خادم إنتاجي.
**المصادر:** `README.md`، `STATUS.md`، `.env.example`، `config/settings/{base,prod,dev,env}.py`،
وأوامر الإدارة في `apps/*/management/commands/`. كل قيمة تطبيقية هنا مأخوذة من الكود.
حيث يلزم اختيار لا يحدّده الكود (مسار، اسم مستخدم، عدد العمّال) فهو **اختيار هذا الدليل**
ومُعلَّم كذلك، وتستطيع تغييره.

**اصطلاحات:**

- `YOUR_DOMAIN` — النطاق الذي سيُخدم عليه النظام (مثلاً النطاق الفرعي الذي سيؤطّره B2CORE).
  غير معروف للكود، ولا يُخترع هنا.
- المستخدم التشغيلي: `maxpay`. مجلّد التطبيق: `/srv/maxpay/app`. مجلّد النسخ: `/srv/maxpay/backups`
  (المسار الأخير هو المثال الوارد في `.env.example`).
- الأوامر مكتوبة لـ Debian/Ubuntu. **الكود لا يحدّد نظام تشغيل.**

> **اقرأ هذا أولاً — ثلاثة أشياء تكسر النشر بصمت:**
>
> 1. **الفرع.** `master` على GitHub ما يزال عند الإيداع الأول وحده (`c42cddb`). كل العمل على
>    الفرع `portal/one-screen-compose`. انسخ هذا الفرع بالاسم.
> 2. **`manage.py` يرفض العمل بلا `DJANGO_SETTINGS_MODULE`.** كان يعود بصمت إلى إعدادات
>    التطوير؛ صار يتوقّف برسالة تسمّي السطر الناقص. الطريق الوحيد إلى `dev` بلا تسمية هو
>    `MAXPAY_LOCAL_DEV=true` — **لا يُضبط على الخادم أبداً.** على الخادم: السطر
>    `DJANGO_SETTINGS_MODULE=config.settings.prod` في `.env`، وإلا لن يعمل أي أمر.
> 3. **لا تنسخ `.env` من جهاز المطوّر.** هو مضبوط للعرض المحلي: SQLite، و`DEBUG=true`،
>    وB2CORE مزيّف على `127.0.0.1`، وباب كلمة مرور التاجر مفتوح. انظر §4.4.

---

## 1. المتطلبات

| المكوّن | الإصدار | المصدر في المشروع |
| --- | --- | --- |
| **نظام التشغيل** | غير محدَّد في الكود. الأوامر هنا لـ Debian/Ubuntu | — |
| **Python** | **3.13** | `requirements.txt`: «Pinned to the versions verified against Django 5.2 / Python 3.13»، و`pyproject.toml`: `target-version = "py313"`. PyJWT مثبَّت على 2.x لأن 1.x لا يعمل على 3.13 |
| **PostgreSQL** | 16 هو ما يشغّله `docker-compose.yml` محلياً. `backup_database` يحتاج أدوات العميل (`pg_dump`) في `PATH` | `docker-compose.yml`، `backup_database.py` |
| **Redis** | 7 هو ما يشغّله `docker-compose.yml`. حزمة العميل `redis==8.1.0` في `requirements.txt` | `docker-compose.yml`، `base.py` (`REDIS_URL`) |
| **nginx** | أي إصدار حديث مع TLS | لا إعداد له في المشروع — يُكتب في §6 |
| **gunicorn** | `26.2.0`، مثبَّت في `requirements.txt`. **لم يُشغَّل مع المشروع بعد**: لا يعمل على Windows حيث طُوِّر، فأوّل تشغيل له على الخادم | `requirements.txt`، `STATUS.md` B4 |
| **Node.js** | اختياري: لتشغيل اختبارات `tests/js/` فقط، ولا يلزم للتشغيل | `README.md` «Tests and checks» |

**ملاحظة على Python 3.13:** Debian 13 يحمله في مستودعاته. Ubuntu 24.04 يحمل 3.12، فتحتاج
مصدراً آخر (مثل deadsnakes PPA). لا تنزل إلى 3.12: الحزم لم يُتحقَّق منها عليه.

---

## 2. تجهيز الخادم

### 2.1 تحديث النظام وتثبيت الحزم

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.13 python3.13-venv git nginx redis-server \
    postgresql postgresql-client certbot python3-certbot-nginx ufw
```

### 2.2 مستخدم إداري غير root

إن كنت تدخل الآن كـ root:

```bash
adduser taha
usermod -aG sudo taha
```

ومستخدم تشغيلي بلا صدفة دخول، يملك التطبيق ويشغّل gunicorn (اختيار هذا الدليل):

```bash
sudo adduser --system --group --home /srv/maxpay --shell /usr/sbin/nologin maxpay
sudo mkdir -p /srv/maxpay/app /srv/maxpay/backups
sudo chown -R maxpay:maxpay /srv/maxpay
```

### 2.3 SSH بالمفتاح فقط

من **جهازك** (لا من الخادم):

```bash
ssh-copy-id taha@SERVER_IP
ssh taha@SERVER_IP    # تأكّد أن الدخول بالمفتاح يعمل قبل الخطوة التالية
```

على الخادم، في `/etc/ssh/sshd_config`:

```
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
```

```bash
sudo sshd -t && sudo systemctl reload ssh
```

**لا تغلق الجلسة الحالية** حتى تفتح جلسة ثانية بنجاح.

### 2.4 جدار الحماية: 80 و443 فقط للعموم

**تحذير:** «80 و443 فقط» حرفياً يعني أنك تغلق SSH على نفسك. المقصود: 80 و443 للعالم،
وSSH **لعنوانك الإداري وحده**.

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from ADMIN_IP to any port 22 proto tcp   # عنوانك الثابت
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status verbose
```

PostgreSQL (5432) وRedis (6379) **لا يُفتحان**: كلاهما يستمع على `127.0.0.1` فقط.

```bash
sudo ss -tlnp | grep -E ':5432|:6379'   # يجب أن يظهر 127.0.0.1 لا 0.0.0.0
```

---

## 3. PostgreSQL — ثم الاختبارات كاملة قبل أي شيء آخر

> **هذا أول تشغيل للمشروع على PostgreSQL على الإطلاق.**
> كل الهجرات وكل الاختبارات حتى اليوم جرت على SQLite فقط (`STATUS.md` B3، و`README.md`
> «Nothing has yet been run against PostgreSQL»). مُشغِّلات `plpgsql` التي تجعل سجل التدقيق
> غير قابل للتعديل (هجرة `core.0002`)، والقيد الجزئي على `Wallet`، وقيد `CheckConstraint`
> على `PaymentMethod`، وحقول `JSONField` — **لم يُنفَّذ أيٌّ منها مرّة واحدة على المحرّك
> الحقيقي.** لذلك تُشغَّل المجموعة كاملة هنا، على هذا الخادم، **قبل** `migrate` على قاعدة
> الإنتاج. أي إخفاق هنا نتيجة حقيقية، لا تذبذب: الاختبار المتذبذب الوحيد المعروف (B14) أُصلح
> في `f104780`.

### 3.1 القاعدة والمستخدم

```bash
sudo -u postgres psql
```

```sql
CREATE ROLE maxpay WITH LOGIN PASSWORD 'ضع-كلمة-مرور-طويلة-عشوائية';
CREATE DATABASE maxpay OWNER maxpay ENCODING 'UTF8';
-- مؤقتاً: مشغّل اختبارات Django ينشئ قاعدة test_maxpay ويحذفها
ALTER ROLE maxpay CREATEDB;
\q
```

الأسماء `maxpay` هي القيم الافتراضية في `base.py` (`POSTGRES_DB`، `POSTGRES_USER`).
كلمة المرور **لا افتراضي لها** — ولّدها:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 3.2 نسخة اختبار مؤقتة من الكود

نسخة منفصلة عن نسخة الإنتاج (§4)، بلا `.env`، تُحذف بعد النجاح:

```bash
cd ~
git clone --branch portal/one-screen-compose \
    https://github.com/qaljabri-creator/maxpay-portal.git maxpay-test
cd maxpay-test
python3.13 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 3.3 تشغيل المجموعة كاملة على PostgreSQL

المجموعة مكتوبة لإعدادات التطوير (`prod` يفرض `SECURE_SSL_REDIRECT` فيحوّل كل طلب اختبار).
القيم تُمرَّر في سطر الأمر لأن لا `.env` في هذه النسخة:

```bash
DJANGO_SETTINGS_MODULE=config.settings.dev \
DJANGO_SECRET_KEY=test-only-not-used-anywhere-else \
POSTGRES_DB=maxpay POSTGRES_USER=maxpay \
POSTGRES_PASSWORD='كلمة-مرور-3.1' \
POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=5432 \
python manage.py test
```

- تستغرق وقتاً (على SQLite تجاوز التشغيل الكامل عشرين دقيقة).
- **المطلوب:** `OK`، بلا `FAIL` ولا `ERROR`.
- **إن أخفق شيء فتوقّف هنا** وأرسل المخرجات للفريق. لا تتابع إلى `migrate`.

وإن كان Node مثبّتاً (اختياري):

```bash
node --test "tests/js/**/*.test.js"
```

### 3.4 بعد النجاح

```bash
sudo -u postgres psql -c "ALTER ROLE maxpay NOCREATEDB;"
deactivate
rm -rf ~/maxpay-test
```

---

## 4. الكود والبيئة و`.env`

### 4.1 النسخ

```bash
sudo -u maxpay git clone --branch portal/one-screen-compose \
    https://github.com/qaljabri-creator/maxpay-portal.git /srv/maxpay/app
```

### 4.2 البيئة الافتراضية والمتطلبات

```bash
cd /srv/maxpay/app
sudo -u maxpay python3.13 -m venv .venv
sudo -u maxpay .venv/bin/pip install --upgrade pip
sudo -u maxpay .venv/bin/pip install -r requirements.txt   # يشمل gunicorn
```

### 4.3 `.env` من `.env.example`

```bash
sudo -u maxpay cp .env.example .env
sudo chmod 600 .env
sudo -u maxpay nano .env
```

`manage.py` و`wsgi.py` كلاهما يقرأ `.env` من جذر المشروع بـ `python-dotenv`، فلا حاجة
لتمرير المتغيّرات لـ systemd.

ولّد المفتاح السرّي:

```bash
.venv/bin/python -c "from django.core.management.utils import get_random_secret_key as k; print(k())"
```

### 4.4 كل متغيّر: ما هو، قيمته الإنتاجية، ومن أين تُؤخذ

**الأساس**

| المتغيّر | ما هو | القيمة الإنتاجية | المصدر |
| --- | --- | --- | --- |
| `DJANGO_SETTINGS_MODULE` | أي ملف إعدادات يُحمَّل | **`config.settings.prod`** | إلزامي هنا. `.env.example` يحمل `dev` |
| `DJANGO_SECRET_KEY` | يشتقّ منه الكوكيز وروابط المرفقات الموقّعة ورموز CSRF | 50 محرفاً عشوائياً (الأمر أعلاه) | `prod.py` يرفض الإقلاع بلا قيمة. `core.E014` إن كانت قيمة التطوير، `core.W015` إن قصُرت |
| `DJANGO_DEBUG` | — | احذفه أو اتركه `false` | `prod.py` يفرض `DEBUG = False` أياً كانت القيمة |
| `DJANGO_ALLOWED_HOSTS` | أسماء المضيف المقبولة | `YOUR_DOMAIN` | `prod.py` يرفض الإقلاع بلا قيمة. `core.E013` إن احتوت `*` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | أصول موثوقة لـ CSRF | `https://YOUR_DOMAIN` | اختياري، يقرؤه `prod.py` فقط. **غير موجود في `.env.example`** |
| `TRUSTED_PROXY_COUNT` | كم بروكسي أمام التطبيق يُصدَّق في `X-Forwarded-For` | **`1`** (nginx) | `base.py`، افتراضه `0`. انظر §6.2 |
| `MAXPAY_LOCAL_DEV` | يسمح لـ `manage.py` بالعودة إلى `dev` بلا تسمية | **لا يُضبط** | `manage.py`. للمطوّر فقط |
| `ALLOW_DEMO_RESET` | المفتاح الثاني لـ `reset_demo` | **لا يُضبط** | `base.py`، افتراضه `false` |

**قاعدة البيانات**

| المتغيّر | ما هو | القيمة الإنتاجية | المصدر |
| --- | --- | --- | --- |
| `POSTGRES_DB` | اسم القاعدة | `maxpay` | §3.1 |
| `POSTGRES_USER` | المستخدم | `maxpay` | §3.1 |
| `POSTGRES_PASSWORD` | كلمة المرور | ما ولّدته في §3.1 | لا افتراضي |
| `POSTGRES_HOST` | المضيف | `127.0.0.1` | افتراضي `base.py` |
| `POSTGRES_PORT` | المنفذ | `5432` | افتراضي `base.py` |
| `DATABASE_URL` | بديل بسطر واحد، **يتقدّم** على `POSTGRES_*` | **احذفه** | إن بقي `sqlite:///...` من جهاز المطوّر فالإنتاج يعمل على SQLite |

**التخزين المؤقت والمصادقة الثنائية**

| المتغيّر | ما هو | القيمة الإنتاجية | المصدر |
| --- | --- | --- | --- |
| `REDIS_URL` | الذاكرة المشتركة لمُحدِّدَي المعدّل (البوابة وقفل الدخول) | `redis://127.0.0.1:6379/0` | `.env.example`. بدونه `core.W010`: كل عامل يعدّ وحده، فلا حدّ فعلياً |
| `CACHE_KEY_PREFIX` | بادئة المفاتيح | `maxpay` (الافتراضي) | `base.py` |
| `OTP_TOTP_ISSUER` | الاسم في تطبيق المصادقة | `MaxPay Portal` (الافتراضي) | `base.py` |
| `LOGIN_FAILURE_LIMIT` | محاولات فاشلة قبل القفل، لكل (بريد، IP) | `10` (الافتراضي) | `base.py`. `core.E006` إن كانت منخفضة جداً |
| `LOGIN_FAILURE_WINDOW_SECONDS` | مدّة القفل | `900` (الافتراضي) | `base.py` |

**B2CORE** (تفصيل في §7)

| المتغيّر | ما هو | القيمة الإنتاجية | المصدر |
| --- | --- | --- | --- |
| `B2CORE_ORIGIN` | أصل بوابة B2CORE التي تؤطّرنا: `scheme://host` بلا مسار | **من فريق B2CORE.** المرشّح: `https://my.maxifyfx.com` (README: لوحة التاجر «loaded as an iframe from `https://my.maxifyfx.com`») — **أكّده** | `STATUS.md` B9: غير مؤكَّد. `portal.E002` إن كان له مسار |
| `B2CORE_JWKS_URL` | عنوان مفاتيح التحقق العامة | **من فريق B2CORE. مجهول حتى الآن** | `STATUS.md` B9 و§3.3 بند 4. قيمة `.env.example` وهمية |
| `B2CORE_JWT_ISSUER` | قيمة `iss` حرفياً | `https://api.maxifyfx.com/srvsz/auth/clients/v1/` — **بالشرطة المائلة الأخيرة** | افتراضي `base.py`، مقروء من توكن حقيقي في 4 سبتمبر. **ليس هو الأصل.** `prod.py` يرفض الإقلاع إن كان فارغاً أو مساوياً للأصل |
| `B2CORE_JWT_AUDIENCE` | — | **فارغ. دائماً.** | B2CORE لا يرسل `aud`. ضبطه يرفض كل توكن حقيقي. `portal.E006` |
| `B2CORE_JWT_ALGORITHMS` | الخوارزميات المقبولة | اتركه (الافتراضي يبدأ بـ `EdDSA`) | `base.py`. `portal.E004` إن دخلته خوارزمية متماثلة أو `none` |
| `B2CORE_JWT_LEEWAY_SECONDS` | سماحية الساعة | `30` (الافتراضي) | `base.py` |
| `B2CORE_ACCOUNT_CLAIM` | ادعاء رقم الحساب | اتركه `account_number` | لا ادعاء كهذا في توكن B2CORE. **لا توجّهه إلى `sub` أو `sid`** |
| `B2CORE_MAX_TOKEN_BYTES` / `B2CORE_JWKS_CACHE_SECONDS` / `B2CORE_JWKS_TIMEOUT_SECONDS` | حدود تقنية | `8192` / `600` / `5` (الافتراضي) | `base.py`، غير موجودة في `.env.example` |

**جلسات الإطارين**

| المتغيّر | القيمة الإنتاجية | ملاحظة |
| --- | --- | --- |
| `PORTAL_SESSION_COOKIE_NAME` | `maxpay_embed_sid` (الافتراضي) | يجب ألا يساوي اسم كوكي الجلسة الداخلية (`portal.E011`) |
| `MERCHANT_SESSION_COOKIE_NAME` | الافتراضي | يجب أن يختلف عن الاثنين الآخرين (`merchant.E020`، `E021`) |
| `PORTAL_SESSION_COOKIE_SAMESITE` / `_SECURE` | `None` / `true` | **`prod.py` يفرضهما** أياً كان في `.env` |
| `MERCHANT_SESSION_COOKIE_SAMESITE` / `_SECURE` | `None` / `true` | **`prod.py` يفرضهما** كذلك |
| `PORTAL_SESSION_MAX_SECONDS` / `MERCHANT_SESSION_MAX_SECONDS` | `28800` (الافتراضي، 8 ساعات) | |
| `PORTAL_SESSION_RATE` / `MERCHANT_SESSION_RATE` | `30/minute` (الافتراضي) | |
| `PORTAL_SESSION_COOKIE_DOMAIN` / `MERCHANT_SESSION_COOKIE_DOMAIN` | فارغ (الافتراضي) | غير موجودان في `.env.example` |
| `PORTAL_ALLOW_STANDALONE` | **`false`** | يسمح بتشغيل صفحة المصافحة خارج إطار. للتطوير المحلي فقط، ويشمل لوحة التاجر |
| `MERCHANT_PASSWORD_LOGIN` | **`false`** | باب الطوارئ. `merchant.W025` ما دام مفتوحاً. افتحه لانقطاع B2CORE فقط، وأغلقه بعده |

**تدفّق الطلبات**

| المتغيّر | القيمة الحالية | ملاحظة |
| --- | --- | --- |
| `PORTAL_DEPOSIT_MIN_USD` / `MAX` | `1` / `100000` | **قيم مؤقّتة صراحةً.** تُضبط بعد جواب المالية (آخر الدليل) |
| `PORTAL_WITHDRAWAL_MIN_USD` / `MAX` | `1` / `100000` | نفس الشيء |
| `PORTAL_SUBMISSION_RATE` | `20/hour` | |
| `PORTAL_CATALOG_RATE` | `240/minute` | |
| `PORTAL_MESSAGE_RATE` | `30/minute` | |
| `PORTAL_DESTINATION_MIN_DIGITS` / `MAX` | `6` / `32` | فحص شكل لا صحّة |
| `PORTAL_ATTACHMENT_URL_MAX_AGE` | `300` | عمر رابط المرفق الموقّع بالثواني |
| `PORTAL_MESSAGE_MAX_CHARS` | `1000` | |
| `PANEL_POLL_SECONDS` | `10` | `core.E003` تحت 2، `core.W004` فوق 120 |

**النسخ الاحتياطي**

| المتغيّر | القيمة الإنتاجية | المصدر |
| --- | --- | --- |
| `BACKUP_DIR` | `/srv/maxpay/backups` — **على قرص غير قرص القاعدة** | مثال `.env.example`. `core.W011` إن كان فارغاً |
| `BACKUP_RETENTION_DAYS` | `14` (الافتراضي) | `core.W012` إن كان يوماً واحداً |
| `BACKUP_TIMEOUT_SECONDS` | `1800` (الافتراضي) | |

**البريد** — `EMAIL_HOST`، `EMAIL_PORT`، `EMAIL_HOST_USER`، `EMAIL_HOST_PASSWORD`،
`EMAIL_USE_TLS`، `DEFAULT_FROM_EMAIL`: يقرؤها `prod.py` بافتراضيات، و**لا شيء في الكود يرسل
بريداً اليوم**. اتركها.

### 4.5 القيم المحلية التي تكسر الإنتاج

كلها موجودة في `.env` على جهاز المطوّر اليوم، أو في كتلة «Trying it locally» من `.env.example`:

| القيمة المحلية | ماذا تفعل في الإنتاج |
| --- | --- |
| `DJANGO_SETTINGS_MODULE=config.settings.dev` | `manage.py` يعمل بإعدادات التطوير: `DEBUG` قد يكون `True`، ولا HSTS، ولا إعادة توجيه HTTPS، ولا فحوص `prod.py` |
| `DJANGO_DEBUG=true` | يُتجاهَل تحت `prod`، لكنه يعمل تحت `dev` — انظر السطر السابق |
| `DATABASE_URL=sqlite:///dev.sqlite3` | يتقدّم على `POSTGRES_*`: الإنتاج يكتب في ملف SQLite بجانب الكود |
| `B2CORE_ORIGIN=http://127.0.0.1:8000` | لا يستطيع B2CORE الحقيقي تأطير البوابة |
| `B2CORE_JWKS_URL=http://127.0.0.1:8000/static/dev/b2core-jwks.json` | يثق بمفتاح تطوير يملكه أي شخص معه `devdata/` |
| `B2CORE_JWT_ISSUER=https://api.b2core.local/...` | يرفض كل توكن حقيقي |
| `PORTAL_ALLOW_STANDALONE=true` | صفحة المصافحة تعمل خارج الإطار |
| `PORTAL_/MERCHANT_SESSION_COOKIE_SAMESITE=Lax`، `_SECURE=false` | `prod.py` يتجاوزها، لكن لا تعتمد على ذلك: احذفها |
| `MERCHANT_PASSWORD_LOGIN=true` | باب الطوارئ مفتوح: التاجر يدخل بكلمة مرور خارج ربط B2CORE |
| `ALLOW_DEMO_RESET=true`، `MAXPAY_LOCAL_DEV=true` | أدوات العرض المحلي. الأول يفتح `reset_demo` (الذي يرفض PostgreSQL على أي حال)، والثاني يُسقط رفض `manage.py` |

و**لا تشغّل `seed_demo` ولا `reset_demo` في الإنتاج.** كلاهما يرفض العمل ما لم يكن `DEBUG`
مفعّلاً. و`reset_demo` — الذي يحذف الطلبات وشبكة التجار وسجل التدقيق — يحمل حارسين آخرين لا
يقرآن `DEBUG`: يرفض PostgreSQL دائماً، ويرفض ما لم يُضبط `ALLOW_DEMO_RESET=true`. **لا تضع
`ALLOW_DEMO_RESET` ولا `MAXPAY_LOCAL_DEV` في `.env` الخادم.**

---

## 5. الهجرات، الملفات الساكنة، الأدوار، وأول مدير

من `/srv/maxpay/app`، بالمستخدم `maxpay`. اختصاراً:

```bash
cd /srv/maxpay/app
alias dj='sudo -u maxpay /srv/maxpay/app/.venv/bin/python manage.py'
```

### 5.1 تأكّد أولاً من الإعدادات المحمَّلة

```bash
dj diffsettings --all | grep -E 'DEBUG = |SETTINGS_MODULE'
```

المطلوب سطران: `SETTINGS_MODULE = 'config.settings.prod'` و`### DEBUG = False`
(`###` تعني «مساوٍ لافتراضي Django»، و`--all` لازم: بدونه لا يُطبع `DEBUG` حين يكون `False`).
إن ظهر `config.settings.dev` أو `DEBUG = True` فتوقّف وأصلح `DJANGO_SETTINGS_MODULE`.

### 5.2 الهجرات

```bash
dj migrate
dj migrate --check          # لا شيء معلّق
dj makemigrations --check --dry-run   # "No changes detected"
```

### 5.3 الملفات الساكنة

```bash
dj collectstatic --noinput
```

تُجمَع في `staticfiles/` (`STATIC_ROOT` في `base.py`)، ويخدمها nginx في §6.

### 5.4 الأدوار

تُزامَن تلقائياً بعد `migrate`، لكن شغّلها صراحةً لترى فحص إخفاء هوية العميل:

```bash
dj bootstrap_roles
```

المطلوب ثلاثة أسطر (`finance_admin`، `finance_staff`، `merchant`)، ثم:
`Client anonymity check passed: merchant holds none of the ... identity permissions.`

### 5.5 أول مدير

```bash
dj create_internal_user --email you@maxifyfx.com --full-name "الاسم الكامل" \
    --role finance_admin --superuser
```

- **لا تمرّر `--password`**: سيُطلب منك مرّتين. تمريره يتركه في تاريخ الصدفة.
- كلمة المرور 12 محرفاً على الأقل، وليست شائعة ولا أرقاماً فقط (`AUTH_PASSWORD_VALIDATORS`).
- بعد §6: ادخل من `https://YOUR_DOMAIN/account/login/`. المصادقة الثنائية **إلزامية** لكل حساب
  داخلي، ويطلب منك تسجيل تطبيق مصادقة (TOTP) في الدخول الأول.
- بقية الحسابات يُنشئها هذا المدير من اللوحة، لا من سطر الأوامر.

---

## 6. gunicorn كخدمة systemd، ثم nginx مع TLS

### 6.1 خدمة gunicorn

`/etc/systemd/system/maxpay.service`:

```ini
[Unit]
Description=MaxPay Portal (gunicorn)
After=network.target postgresql.service redis-server.service
Requires=postgresql.service redis-server.service

[Service]
User=maxpay
Group=www-data
WorkingDirectory=/srv/maxpay/app
RuntimeDirectory=maxpay
UMask=0007
ExecStart=/srv/maxpay/app/.venv/bin/gunicorn config.wsgi:application \
    --bind unix:/run/maxpay/gunicorn.sock \
    --workers 3 \
    --access-logfile - --error-logfile -
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

- `config.wsgi` يقرأ `.env` بنفسه ويعود إلى `config.settings.prod` افتراضياً.
- `--workers 3` **اختيار هذا الدليل**، لا قيمة من الكود. أكثر من عامل واحد يجعل `REDIS_URL`
  إلزامياً لا اختيارياً (`core.W010`).

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now maxpay
sudo systemctl status maxpay
journalctl -u maxpay -n 50
```

إن رفض الإقلاع بـ `ImproperlyConfigured` فالرسالة تسمّي المتغيّر الناقص — غالباً
`DJANGO_SECRET_KEY` أو `DJANGO_ALLOWED_HOSTS` أو `B2CORE_JWT_ISSUER`.

### 6.2 nginx

`/etc/nginx/sites-available/maxpay`:

```nginx
server {
    listen 80;
    server_name YOUR_DOMAIN;

    # يخدم الملفات الساكنة. private_media/ لا يُخدم إطلاقاً — انظر أدناه.
    location /static/ {
        alias /srv/maxpay/app/staticfiles/;
    }

    location / {
        proxy_pass http://unix:/run/maxpay/gunicorn.sock;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        # يُستبدَل لا يُضاف: التطبيق يأخذ أول عنوان في السلسلة.
        proxy_set_header X-Forwarded-For $remote_addr;
        client_max_body_size 11m;
    }
}
```

**ثلاثة أسطر ليست تفضيلاً:**

1. **`X-Forwarded-Proto`** — `prod.py` يضبط `SECURE_SSL_REDIRECT = True` ويقرأ
   `SECURE_PROXY_SSL_HEADER` من هذه الترويسة. بدونها يرى Django كل طلب HTTP ويعيد التوجيه
   إلى HTTPS بلا نهاية.
2. **`X-Forwarded-For` مع `TRUSTED_PROXY_COUNT=1` في `.env`** — عليه يعدّ قفل الدخول ومُحدِّد
   البوابة. `apps/core/services.client_ip` لا يثق بالترويسة إلا بعدد البروكسيات المُعلَن، ويأخذ
   العنوان **من اليمين** بذلك العدد: ما على اليسار كتبه المُرسِل نفسه. افتراضياً `0` = تُتجاهَل
   الترويسة ويُستعمل `REMOTE_ADDR` — وخلف nginx هذا يعني أن **كل العملاء عنوان واحد** (عنوان
   nginx)، فيقفل خطأُ عميلٍ واحد الجميع. لذلك `1` إلزامي هنا. `$remote_addr` كما في الإعداد أعلاه
   يعطي سلسلة من عنصر واحد، و`$proxy_add_x_forwarded_for` يعمل كذلك مع `1`. إن وُضع موازن
   حمل أمام nginx فالعدد `2`.
3. **`client_max_body_size 11m`** — `MAX_UPLOAD_SIZE_BYTES` في `base.py` هو 10 ميغابايت،
   والسطر يترك هامشاً لغلاف الـ multipart. أقلّ منه يرفض nginx إثبات تحويل مسموحاً به.

**ولا تُضف في nginx** `X-Frame-Options` ولا `Content-Security-Policy` ولا `add_header` لأيٍّ
منهما: التطبيق يضع سياسة **لكل واجهة** (`apps/core/middleware.py`) — `/portal/` و`/merchant/`
تسمحان بالتأطير من `B2CORE_ORIGIN` وحده، والباقي `frame-ancestors 'none'`. ترويسة عامة من
nginx تكسر أحد الاثنين.

**ولا تنشئ `location` لـ `private_media/`.** `MEDIA_URL` في `base.py` هو `/__never_served__/`
عمداً: إثباتات التحويل تُخدم فقط عبر روابط موقّعة قصيرة العمر من التطبيق.

```bash
sudo ln -s /etc/nginx/sites-available/maxpay /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

### 6.3 TLS

```bash
sudo certbot --nginx -d YOUR_DOMAIN
sudo certbot renew --dry-run
```

certbot يضيف كتلة 443 ويحوّل 80 إليها. تأكّد أن كتلة 443 التي أنشأها تحمل أسطر `location`
أعلاه كما هي.

**قبل أن تفتح النطاق:** `prod.py` يرسل HSTS لسنة كاملة، مع `includeSubDomains` و`preload`.
المتصفّح الذي يزور مرّة يرفض أي نطاق فرعي لـ `YOUR_DOMAIN` على HTTP طوال سنة. تأكّد أن هذا
مقبول للنطاق الأب قبل أول زيارة.

### 6.4 تحقّق

```bash
curl -sI https://YOUR_DOMAIN/account/login/ | grep -iE 'strict-transport|x-frame|content-security'
curl -sI http://YOUR_DOMAIN/ | head -3        # 301 إلى https
```

---

## 7. B2CORE

### 7.1 بندا القائمة في B2CORE

يُضافان في B2CORE (من فريقه، لا من هنا):

| البند | العنوان المؤطَّر | لمن | ما يحدث |
| --- | --- | --- | --- |
| **البوابة** | `https://YOUR_DOMAIN/portal/` | العملاء | الإطار يعلن `embed-iframe-ready`، يطلب التوكن بـ `embed-request-jwt-token`، ويستقبل `embed-jwt-token` أو `embed-jwt-token-error` |
| **لوحة التاجر** | `https://YOUR_DOMAIN/merchant/embed/` | نوع عميل التاجر فقط (تقييد في B2CORE) | نفس المصافحة ونفس التحقق، ثم تحويل إلى `/merchant/` |

الرسائل الأخرى المدعومة: `embed-logout`، `embed-theme-change`، `embed-language-change`
(`STATUS.md` §3.3 بند 1، أكّدها طه في 7 سبتمبر).

**تقييد القائمة في B2CORE ليس ضابطاً أمنياً.** توكن B2CORE لا يحمل نوع العميل، فأي عميل
يملك توكناً يستطيع إرساله إلى `/merchant/session/`. الحارس الوحيد: `sub` الموقَّع يجب أن
يساوي `b2core_id` لتاجر نشط غير مؤرشف له حساب دخول. لذلك:

- المالية تكتب لكل تاجر **معرّفه في B2CORE** (`Merchant.b2core_id`) من شاشة التاجر في
  اللوحة. تاجر بلا معرّف لا يدخل لوحته. خطأ في كتابته يمنعه.
- إيقاف تاجر أو أرشفته يسري من النقرة التالية، لا من الدخول التالي.

**زر النسخ في البوابة** يحتاج أن يمنح B2CORE الإطار `allow="clipboard-write"`
(`STATUS.md` B12). لم يُجرَّب داخل إطار حقيقي بعد.

### 7.2 القيم الثلاث

| القيمة | من أين | تحذير |
| --- | --- | --- |
| `B2CORE_ORIGIN` | من فريق B2CORE: أصل الصفحة التي تحتوي الإطارين | **أصل واحد** للواجهتين. `scheme://host` بلا مسار ولا شرطة أخيرة. منه تُبنى `frame-ancestors` وهدف `postMessage` وقائمة CORS |
| `B2CORE_JWKS_URL` | من فريق B2CORE | **مجهول حتى اليوم.** `STATUS.md` يرجّح أنه تحت مسار المُصدِر لا في جذر `api.*` — تخمين، لا تبنِ عليه |
| `B2CORE_JWT_ISSUER` | الافتراضي في الكود | `https://api.maxifyfx.com/srvsz/auth/clients/v1/` حرفياً. إن أعطاك أحد قيمة أخرى، اطلب توكناً حقيقياً وافحص `iss` فيه |

وتحقّق من الخادم أن JWKS يُجلب فعلاً:

```bash
curl -fsS "B2CORE_JWKS_URL_الحقيقي" | head -c 400; echo
```

المطلوب JSON فيه `"keys"`. B2CORE يوقّع بـ EdDSA، فالمفتاح `"kty": "OKP"`.

### 7.3 شاشة إعدادات التكامل

ادخل كمدير مالية إلى:

```
https://YOUR_DOMAIN/finance/integration/b2core/
```

تعرض عنوان JWKS والمُصدِر والجمهور والأصل والخوارزميات والسماحية. **للقراءة فقط**: لا
نموذج ولا مسار كتابة. أي تعديل يمرّ عبر `.env` ثم `systemctl restart maxpay`.

**كيف تقرؤها — وما لا تعنيه:**

- **سلبية.** لا تتصل بالشبكة عند فتحها. «آخر حلّ ناجح للمفتاح» يعني أن حركة حقيقية حلّت
  مفتاحاً، لا أن B2CORE متاح الآن. **قبل أول دخول حقيقي لن تعرض نجاحاً.**
- **لكل عامل.** مع 3 عمّال يحمل كلٌّ قراءته، وتحديث الصفحة قد يصل إلى عامل آخر برقم آخر.
- النجاح قد يكون من الذاكرة المؤقتة (`B2CORE_JWKS_CACHE_SECONDS`، 600 ثانية افتراضياً).

**الاختبار الفعلي الوحيد:** افتح بند البوابة من B2CORE بحساب عميل تجريبي، ثم بند لوحة
التاجر بحساب تاجر مربوط. بعدها أعد فتح الشاشة: يجب أن تعرض حلّاً ناجحاً. وراقب:

```bash
journalctl -u maxpay -f | grep -i b2core
```

(مسجِّل `maxpay.b2core` في `base.py`.)

---

## 8. النسخ الاحتياطي، مع استعادة فعلية

### 8.1 ما يغطّيه `backup_database` وما لا يغطّيه

- **يغطّي القاعدة:** بـ `pg_dump --format=custom`، في `BACKUP_DIR`، باسم `maxpay-<UTC>.dump`.
  كلمة المرور في بيئة العملية لا في سطر الأمر.
- **ويغطّي الملفات المرفوعة:** `private_media/` — إثباتات التحويل ومرفقات الرسائل، وهي أدلّة
  مالية خارج القاعدة — في `maxpay-media-<UTC>.tar.gz` **بنفس الطابع الزمني**. إن تعذّر أرشفتها
  يفشل الأمر كلّه (وتبقى نسخة القاعدة).
- يحذف الاثنين معاً حين يتجاوزان `BACKUP_RETENTION_DAYS`.
- **لا يشفّر، ولا ينقل الملفات خارج الخادم** (مكتوب صراحةً في رأس `backup_database.py`).

`BACKUP_DIR` يجب أن يكون **قرصاً آخر** (volume مركّب) لا يشارك القاعدة مصيرها. النقل خارج
الخادم والتشفير قرار تشغيلي غير موجود في المشروع — **يجب أن يُحسم قبل الإطلاق.**

### 8.2 نسخة يدوية أولى

```bash
dj backup_database
ls -lh /srv/maxpay/backups/
```

### 8.3 الجدولة اليومية (systemd timer)

`/etc/systemd/system/maxpay-backup.service`:

```ini
[Unit]
Description=MaxPay daily database backup

[Service]
Type=oneshot
User=maxpay
WorkingDirectory=/srv/maxpay/app
ExecStart=/srv/maxpay/app/.venv/bin/python manage.py backup_database
```

`/etc/systemd/system/maxpay-backup.timer`:

```ini
[Unit]
Description=Run MaxPay backup daily

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now maxpay-backup.timer
systemctl list-timers | grep maxpay
```

وانقل محتوى `BACKUP_DIR` — الملفّين معاً — خارج الخادم، بالأداة التي تعتمدونها.

### 8.4 تجربة الاستعادة — فعلياً، لا على الورق

المواصفة §11 تطلب «restore tested before go-live»، و`STATUS.md` B7: **لم يُجرَّب قط.** الأمر
نفسه يطبع عند كل نسخة: «Spec §11 asks for that to have been tried, on a real dump».

استعد إلى **قاعدة منفصلة**، لا فوق قاعدة الإنتاج:

```bash
LATEST=$(ls -t /srv/maxpay/backups/*.dump | head -1); echo "$LATEST"

sudo -u postgres createdb -O maxpay maxpay_restore_test
sudo -u postgres pg_restore --no-owner --role=maxpay -d maxpay_restore_test "$LATEST"
```

قارن الأعداد بين القاعدتين:

```bash
for db in maxpay maxpay_restore_test; do
  echo "== $db"
  sudo -u postgres psql -d "$db" -Atc "
    SELECT 'requests', count(*) FROM transactions_request
    UNION ALL SELECT 'audit', count(*) FROM core_auditlog
    UNION ALL SELECT 'users', count(*) FROM accounts_user
    UNION ALL SELECT 'merchants', count(*) FROM merchants_merchant;"
done
```

وتأكّد أن مُشغِّلات سجل التدقيق عادت مع الاستعادة — بتشغيل فحص Django على القاعدة المستعادة:

```bash
sudo -u maxpay env POSTGRES_DB=maxpay_restore_test \
    /srv/maxpay/app/.venv/bin/python manage.py check --database default
```

المطلوب: لا `core.E021` («The audit log is not append-only»).

ثم احذف قاعدة التجربة، وسجّل التاريخ والملف والأعداد:

```bash
sudo -u postgres dropdb maxpay_restore_test
```

وجرّب الملفات أيضاً، في مجلّد مؤقّت:

```bash
MEDIA=$(ls -t /srv/maxpay/backups/*-media-*.tar.gz | head -1); echo "$MEDIA"
mkdir -p /tmp/media-restore && tar -xzf "$MEDIA" -C /tmp/media-restore
find /tmp/media-restore/private_media -type f | wc -l
find /srv/maxpay/app/private_media -type f | wc -l     # يجب أن يتساوى العددان
rm -rf /tmp/media-restore
```

الاستعادة الحقيقية فوق الإنتاج هي ما يطبعه الأمر — بعد إيقاف `maxpay`:
`pg_restore --clean --if-exists -d <database> <file>`، ثم
`tar -xzf <media-file> -C /srv/maxpay/app`.

---

## 9. `check --deploy`

```bash
dj check --deploy --database default
```

`--deploy` يشغّل فحوص الإعدادات، و`--database default` يسأل القاعدة نفسها إن كانت مُشغِّلات
سجل التدقيق ما تزال موجودة. `README.md`: مع `REDIS_URL` و`BACKUP_DIR` وقيم B2CORE،
**المخرجات نظيفة تحت `config.settings.prod`** — هذا هو الحدّ.

المطلوب حرفياً: `System check identified no issues (0 silenced).`

ما قد يظهر ومعناه:

| المعرّف | المعنى | الإصلاح |
| --- | --- | --- |
| `portal.E001` | B2CORE غير مضبوط | `B2CORE_ORIGIN` و`B2CORE_JWKS_URL` |
| `portal.E002` | الأصل ليس أصلاً مجرّداً | احذف المسار والشرطة الأخيرة |
| `portal.E005` | المُصدِر فارغ أو يساوي الأصل | القيمة في §7.2 |
| `portal.E006` | الجمهور مضبوط | أفرغ `B2CORE_JWT_AUDIENCE` |
| `portal.E004` / `E003` | خوارزميات خاطئة أو فارغة | احذف `B2CORE_JWT_ALGORITHMS` |
| `portal.E010`–`E012`، `merchant.E020`–`E022` | تصادم أسماء كوكيز أو `None` بلا `Secure` | أسماء مختلفة، و`SECURE=true` |
| `portal.W013`، `merchant.W024` | كوكي إطار ليس `SameSite=None` | لن تعمل الجلسة داخل الإطار |
| `portal.W014` | كوكي الجلسة الداخلية `None` | يجب أن يبقى `Lax` |
| `merchant.W025` | باب كلمة مرور التاجر مفتوح | `MERCHANT_PASSWORD_LOGIN=false` |
| `core.W010` | ذاكرة مؤقتة لكل عملية | `REDIS_URL` |
| `core.W011` / `W012` | `BACKUP_DIR` فارغ / احتفاظ يوم واحد | §8 |
| `core.E013` | `*` في `ALLOWED_HOSTS` | سمِّ النطاق |
| `core.E014` / `W015` | مفتاح التطوير / مفتاح قصير | §4.3 |
| `core.E021` / `W020` | مُشغِّلات سجل التدقيق مفقودة / تعذّر فحصها | `migrate`، وإن كانت مطبّقة فأحدٌ حذفها يدوياً — **حادثة تستحق التحقيق** |
| `core.E001`–`E006`، `W004`، `W005` | إعدادات الاستطلاع والقفل والوسائط | القيم الافتراضية نظيفة |

وأي تحذير `security.W0xx` من Django نفسه يعني أن `prod.py` لم يُحمَّل — عُد إلى §5.1.

---

## 10. قائمة التحقق قبل أول عميل حقيقي

**البنية**

- [ ] §3.3: المجموعة كاملة **مرّت على PostgreSQL** على هذا الخادم، والمخرجات محفوظة
- [ ] `dj diffsettings --all` يعرض `config.settings.prod` و`DEBUG = False`
- [ ] `dj check --deploy --database default` نظيف تماماً
- [ ] `systemctl status maxpay` و`redis-server` و`postgresql` كلها `active`
- [ ] `ufw status`: 80 و443 للعموم، 22 لعنوانك وحده؛ 5432 و6379 على `127.0.0.1` فقط
- [ ] `TRUSTED_PROXY_COUNT=1`: سجلات الدخول في لوحة التدقيق تُظهر عناوين العملاء، لا عنوان nginx
- [ ] `https://YOUR_DOMAIN` بشهادة صالحة، و`certbot renew --dry-run` ناجح
- [ ] `/static/` يُخدم، و`private_media/` **لا** يُخدم من أي عنوان

**الأسرار**

- [ ] `.env` بصلاحية `600` ويملكه `maxpay`، ولم يُنسخ من جهاز مطوّر
- [ ] `DJANGO_SECRET_KEY` جديد ولم يُستعمل في أي مكان آخر
- [ ] لا `DATABASE_URL` ولا أي سطر من كتلة «Trying it locally» في `.env`

**B2CORE**

- [ ] `B2CORE_ORIGIN` و`B2CORE_JWKS_URL` **من فريق B2CORE كتابةً**، لا من هذا الدليل
- [ ] `curl` على JWKS من الخادم يعيد مفاتيح
- [ ] بندا القائمة مضافان، وبند التاجر مقيَّد بنوع التاجر
- [ ] عميل تجريبي فتح البوابة من داخل B2CORE ووصل إلى شاشة الطلب
- [ ] تاجر تجريبي مربوط (`b2core_id`) فتح لوحته من داخل B2CORE
- [ ] عميل عادي حاول فتح لوحة التاجر **ورُفض**
- [ ] شاشة التكامل تعرض حلّاً ناجحاً للمفتاح بعد الحركة أعلاه

**المالية والتشغيل**

- [ ] أول `finance_admin` دخل وسجّل المصادقة الثنائية
- [ ] كل تاجر فعلي له `b2core_id` وحساب دخول ومحفظة نشطة لكل طريقة
- [ ] أسعار الإيداع والسحب مُدخلة
- [ ] ساعات العمل مضبوطة
- [ ] حدود المبالغ `PORTAL_*_MIN/MAX_USD` مضبوطة **على جواب المالية**، لا على `1`/`100000`
- [ ] `MERCHANT_PASSWORD_LOGIN=false`

**النسخ**

- [ ] نسخة يومية مجدولة (`list-timers`) إلى قرص غير قرص القاعدة
- [ ] كل نسخة لها أرشيف `-media-` بجانبها
- [ ] نقل خارج الخادم وتشفير محسومان ومفعّلان
- [ ] §8.4: **استعادة فعلية جرت**، بتاريخ وأعداد مسجّلة

**بوّابات لا يغلقها الكود**

- [ ] اختبار اختراق من طرف ثالث (المواصفة §11، السطر الأخير)

---

## ما هو غير مختبَر بعد

| البند | الحالة | المرجع |
| --- | --- | --- |
| **PostgreSQL** | لم يُشغَّل عليه سطر قط قبل §3 من هذا الدليل. مُشغِّلات `plpgsql` لسجل التدقيق والقيود الجزئية لم تُنفَّذ على المحرّك الحقيقي | `STATUS.md` B3، §2.3 |
| **مكدّس التشغيل** | gunicorn وnginx وsystemd **لم تُشغَّل مع المشروع مرّة**. لا Dockerfile. gunicorn مثبَّت في `requirements.txt` لكنه لم يُقلِع مرّة. كل ما في §6 مكتوب من الإعدادات، لا من تشغيل سابق | `STATUS.md` B4 |
| **B2CORE الحقيقي** | لم يجرِ `postMessage` واحد بين الإطار وبوابة B2CORE حقيقية. كل اختبارات المصافحة مقابل مضيف مُقلَّد في `node:vm`، وجلب JWKS مُزيَّف في المجموعة. شكل غلاف الرسالة ما يزال متسامحاً عمداً (`type\|event\|action\|name`) | `STATUS.md` §2.3، B12 |
| **داخل إطار حقيقي** | زر النسخ (`clipboard-write`)، ارتفاع الإطار على طلب طويل، لوحة المفاتيح الرقمية على الهاتف، والثيم القادم من المضيف | `STATUS.md` B12 |
| **استطلاع اللوحتين** | `panel.js` لم يُتحقَّق منه في متصفّح | `STATUS.md` §2.3 |
| **الاستعادة** | لم تُجرَّب قط | `STATUS.md` B7 |
| **اختبار الاختراق** | لم يُجرَ | المواصفة §11، `STATUS.md` B6 |
| **CI** | لا يوجد. الاختبارات تُشغَّل باليد فقط، ومنها `tests/js/` التي لا يشغّلها `manage.py test` | `STATUS.md` B2 |
| **مُحدِّدات المعدّل** | نوافذ ثابتة في الذاكرة المؤقتة، و**تفشل مفتوحة** عمداً إن سقط Redis. سقف تكلفة، لا ضابط أمني | `STATUS.md` B10 |
| **الفرع** | `portal/one-screen-compose` لم يُراجَع ولم يُدمج في `master` | `STATUS.md` B1-b |

---

## القرارات المعلّقة مع المالية

**نُفِّذت بالتوصية ولم تُؤكَّد كتابةً** (`STATUS.md` §3.1) — لا شيء متوقّف، لكن اعتراضاً متأخّراً
يعني إعادة عمل:

1. **قاعدة التقريب:** الدولار بالسنت، الدينار بالدينار الصحيح، السعر بمنزلتين. الخطر: متوسط.
2. **إعادة التسعير عند تصحيح المبلغ:** السعر لا يتحرّك والعمولة تُعاد نسبياً. **الخطر: عالٍ —
   أموال**، والقول بأن المالية وافقت غير موثّق في أي مكان.
3. **إلغاء التاجر:** «إعادة الطلب إلى المالية» إلى `pending` بملاحظة داخلية إلزامية. الخطر: متوسط.
4. **سجل العميل:** للمالية فقط خلف صلاحية منفصلة. الخطر: منخفض.

**لم يُسأل عنها بعد** (`STATUS.md` §3.2):

1. **حدود الإيداع والسحب الحقيقية بالدولار** — `1`/`100000` مؤقّتة، و**تمنع الإطلاق** (§10).
2. اتجاه التحويل: يُدخل العميل دولاراً أم ديناراً؟ المبني: دولار ← دينار.
3. سقف المحفظة اليومي: الإجمالي بالعمولة أم المحوَّل وحده؟ ومتى يدور اليوم؟
4. عمولة السحب: تُخصم من المدفوع (الحالي، استنتاج) أم تُضاف؟
5. توقيت خصم رصيد B2CORE في السحب: عند `under_review` (الحالي) أم `assigned`؟
6. ساعات عمل مختلفة لكل يوم وعطلات؟ اليوم نافذة واحدة لكل الأيام.
7. تصدير سجل التدقيق: من يحقّ له؟ (غير موجود اليوم.)
8. شارة عدم القراءة: عدد الطلبات (الحالي) أم عدد الرسائل؟
9. عرف مكتوب لعدم كتابة اسم العميل في الرسائل، وهل يُقبل أن يعرّف العميل بنفسه للتاجر؟
10. صيغ حسابات لكل طريقة دفع (اليوم: 6–32 رقماً للجميع).
11. **كيف تربط المالية الطلب بحساب B2CORE للقيد اليدوي؟** التوكن لا يحمل رقم حساب، فحقل رقم
    الحساب فارغ لكل عميل، والبريد هو المُعرِّف. هل يكفي في الـ Back Office، أم يلزم `sub`؟
    **الأهمّ في القائمة**، لأنه يحجب القيد اليدوي نفسه.

**وبانتظار فريق B2CORE** (`STATUS.md` §3.3): عنوان JWKS الإنتاجي، و`B2CORE_ORIGIN` الإنتاجي
مؤكَّداً. بدونهما لا يعمل الدخول — لا للعميل ولا للتاجر.

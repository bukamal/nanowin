# بناء نسخة ويندوز (nanowin)

هذه النسخة هي **نفس كود نانو** بنكهة بناء ويندوز — لا لغة جديدة ولا fork.
منطق العمل وقاعدة البيانات والشاشات مشتركة مع مستودع `nano` حرفيًا.

## 1) المتطلبات (مرة واحدة)

| المتطلب | التفاصيل |
|---|---|
| Windows 10/11 (64-bit) | جهاز البناء أو التطوير |
| Python 3.12 | من python.org، مع تفعيل "Add to PATH" |
| Flutter SDK (stable) | عبر `subosito/flutter-action` في CI، أو تثبيت يدوي محليًا |
| Visual Studio Build Tools | عبء عمل "Desktop development with C++" (يلزم `flet build windows`) |
| Git for Windows | يتضمن rsync اللازم لسكربت المزامنة |

## 2) البناء محليًا

```powershell
pip install --upgrade pip setuptools wheel
pip install "flet==0.28.3" pyyaml coverage
pip install -e extensions/flet_native_files
pip install -e ".[dev]"
python tools/quality_gate.py
flet build windows --skip-flutter-doctor
```

الناتج النهائي **مثبّت (installer) لا نسخة محمولة**: بعد `flet build windows`
يجمّع Inno Setup مجلد الـRelease في `Nano-Setup-<version>.exe` عبر
`windows/installer.iss` (النسخة والملف التنفيذي تُمرَّر كـ`/D defines` فلا
شيء مُصلَّب). وعبر CI: كل push يشغّل `Build Windows EXE` ويرفع المثبّت
كـartifact باسم `nano-windows-installer`.

## 3) أين البيانات على ويندوز؟

`%APPDATA%\Nano` لقاعدة البيانات والنسخ الاحتياطي، والصادرات في `Documents`.
عند أول تشغيل،
تُنقل البيانات القديمة من `%USERPROFILE%\.nano` بنسخ آمن؛ قاعدة SQLite تُنسخ
بآليتها الرسمية مع فحص سلامتها، وتبقى الملفات القديمة كما هي ولا يُستبدل أي
ملف موجود في الوجهة.

## 4) الفروقات عن نسخة أندرويد

المرجع الكامل: `core/platform_capabilities.py` (الحالة لكل ميزة).
المختصر: اختيار الملفات ومشاركتها والإشعارات والأصوات تعمل أصليًا؛ الطباعة
وإنشاء PDF يفتحان المستند في المتصفح للطباعة أو الحفظ كـPDF.
مسح الباركود بالكاميرا والتعرّف الصوتي والودجت ودرج النقدية غير متاحة على ويندوز. يمكن اختيار صورة
إيصال من الجهاز، ويعمل قارئ الباركود USB (وضع wedge) عبر حقل الباركود مباشرة.

## 5) سياسة المزامنة (مهم — اقرأ قبل أي تعديل)

اتجاه واحد فقط: `nano` ←→ `nanowin` عبر `tools/sync_from_nano.sh`
(من `nano` إلى `nanowin` فقط). منطق العمل المشترك يُطوَّر في `nano`
ثم يُسحب هنا. الملفات المملوكة لـnanowin والمستثناة من المزامنة:

ابدأ دائمًا بمعاينة التغييرات:

```bash
tools/sync_from_nano.sh ../nano --dry-run
```

التطبيق الافتراضي لا يحذف ملفات الوجهة؛ استخدم `--apply` لإضافة وتحديث الملفات.
حذف الملفات الزائدة يحتاج تأكيدًا صريحًا بـ`--delete` مع `--apply`، ويمكن معاينة
الحذف أولًا عبر `--delete` وحده.

- `src/nano_offline/core/platform_capabilities.py`
- `tools/sync_from_nano.sh`
- `.github/workflows/build-windows.yml`
- غياب `.github/workflows/build-android-apk.yml` مقصود: بناء الـAPK يبقى في
  مستودع `nano` وحده حتى لا يبني هذا المستودع مخرجات أندرويد بالخطأ
- `windows/installer.iss`
- `docs/WINDOWS_BUILD_AR.md` (هذا الملف)
- `NANOWIN.md`

# arabize

**خلّي تطبيق أندرويد عربي — بانعكاس RTL صحيح — بأمر واحد.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)

العربية · [English](README.md)

---

أغلب محاولات "تعريب" التطبيقات تطلع نص عربي وواجهة مقلوبة: القائمة الجانبية
عاليسار، سهم الرجوع يشير لليسار، شريط التقدم يمتلئ بالعكس، الـ`%d` يضيع،
وعلامات الترقيم تنتقل للجهة الغلط.

`arabize` يتولّى الجزء اللي **مو ترجمة**: اتجاه الواجهة، ترحيل الخصائص،
مخاطر الـbidi، وتهريب النصوص في موارد أندرويد.

## شنو يسوي

| الأمر | الناتج |
|---|---|
| `scan` | تقرير للقراءة فقط: اللغات، حالة RTL، عدد `left/right` القديمة، نسبة التغطية |
| `decode` | يفك الـAPK لمشروع قابل للتعديل |
| `rtl` | يضبط `android:supportsRtl` ويحوّل `left/right` → `start/end` بكل مكان |
| `scaffold` | يبني `res/values-ar/` + جدول ترجمة (`strings.tsv`) |
| `apply` | يستورد جدول مترجم إلى `values-ar/strings.xml` |
| `lint` | مخاطر bidi، أخطاء التهريب، اختلاف الـplaceholders، التغطية — يصلح للـCI |
| `build` | إعادة بناء، `zipalign`، توقيع، تحقق |
| `all` | السلسلة كاملة |

## التنصيب

```sh
pipx install git+https://github.com/<you>/arabize
```

ملف واحد، مكتبة قياسية فقط. يستخدم أدوات موجودة عندك:

* `apktool` → `ARABIZE_APKTOOL=/path/to/apktool.jar`
* `aapt2`, `zipalign`, `apksigner` → من Android SDK build-tools
  (`ANDROID_HOME` أو `ARABIZE_AAPT2` إلخ)

```sh
arabize scan app.apk
```

```
[FINDING] apk: app.apk
  package        : com.example.app
  locales        : 87 -> --_--, af, am, ar, as, az, be, bg, bn, bs, ca, cs, da, de
  arabic present : yes
  supportsRtl    : no (or not detectable in binary XML)
```

## البدء السريع

```sh
arabize decode app.apk -o work
arabize rtl    work                 # جرّب --dry-run أول
arabize scaffold work               # -> work/res/values-ar/strings.tsv
#    ... ترجم العمود الثالث (DeepL، LLM، أو مترجم بشري) ...
arabize apply  work --tsv work/res/values-ar/strings.tsv
arabize lint   work                 # يرجع كود خطأ عند وجود أخطاء حقيقية
arabize build  work --ks release.keystore --ks-pass … --ks-alias …
```

أمر واحد:

```sh
arabize all app.apk --tsv ar.tsv --ks release.keystore --ks-pass …
```

## الجزء اللي ما يسويه غيرنا: فحص الـbidi

العربية تكتب من اليمين، بس الأرقام والكلمات اللاتينية والمسارات والـ`%1$s`
تكتب من اليسار. لما يختلطون، خوارزمية Unicode للاتجاهين تعيد ترتيب المقاطع —
وعلامات الترقيم تطلع بالجهة الغلط.

```
النص:      المجلد test.
يظهر:      المجلد test.        ← النقطة لازم تجي يسار "test"، ما تجي
الحل:      المجلد test\u200f.  ← U+200F (علامة من اليمين لليسار) تثبّتها
```

`arabize lint` يمسك هاي، plus الأخطاء اللي **تكسر البناء** فعلاً:

```
[FINDING] lint res/values-ar: 148/190 translated (77.9%)
  [WARN]  abc_menu_ctrl_shortcut_label: identical to source and contains Latin — probably untranslated
  [ERROR] discount: bare `%` — must be `%%` or a format specifier, aapt will reject it
  [ERROR] welcome: format specifiers differ: source ['%1$s'] vs arabic ['%s']
  [ERROR] note: unescaped apostrophe — use \' inside Android string resources
[FAIL] 3 error(s), 0 warning(s)
```

القواعد: اختلاف الـformat specifiers، `%` عارية، `'` غير مهربة، `@`/`?` ببداية
النص، اختلاف وسوم XML، مقاطع لاتينية بنهاية النص قبل الترقيم، نصوص غير مترجمة،
اختلاف المسافات، وصيغ الجمع (plural) اللي تسقط العدد بحالة `one`/`two`
بالعربي — تحذير، مو خطأ.

كود الخروج غير صفري عند وجود أخطاء، فينزل مباشرة بالـCI.

## شنو يصلّح

* `android:supportsRtl="true"` على `<application>`
* `paddingLeft/Right` → `paddingStart/End`، ومثلها `layout_margin*`،
  `drawable*`، `layout_align*`، `layout_alignParent*`، `layout_to*Of`
* ConstraintLayout: `app:layout_constraint{Left,Right}_to{Left,Right}Of` → `{Start,End}`
* قيم `gravity` / `layout_gravity` / `foregroundGravity`
* `textAlignment` → `viewStart` / `viewEnd`
* مراجع الـtheme اللي تبقى تشير لقيمة LTR
  (`?listPreferredItemPaddingLeft` → `?…Start`)
* تهريب موارد أندرويد، بما فيها `'`، و`@`/`?` بالبداية، والمسافات المهمة

## شنو **ما** يسوي

* **ما يترجم شي.** لا ترجمة آلية ولا AI. العمود الثالث بالـTSV شغلك — الأداة
  تضمن إن الميكانيكا تصمد بعد ما تملاه.
* ما يلمس النصوص المخبوزة بالـDEX/smali أو الكود الأصلي.
* ما ينعكس الصور أو الأصول اللي فيها نص لاتيني محروق.
* **ما** يفرض `android:layoutDirection="rtl"` — هذا يكسر باقي اللغات بتطبيق
  متعدد اللغات. `supportsRtl` + `start/end` هو الميكانيزم الصحيح.

## المتطلبات

Python 3.9+، `apktool`، و Android build-tools. لا شي غير. بلا اعتماديات pip.

## الاختبارات

```sh
python tests/test_arabize.py     # 47 تأكيد، بلا framework، بلا شبكة
```

## خطة الطريق

* التحقق من صيغ الجمع الست بالعربي (`zero/one/two/few/many/other`)
* توليد `android:localeConfig` لأندرويد 13+ (منتقي لغة لكل تطبيق)
* تحذيرات انعكاس النص داخل الـvector drawables

## الرخصة

MIT — شوف [LICENSE](LICENSE).

## تنبيه

استعمله على تطبيقات تملكها أو عندك إذن تعدّلها. إعادة نشر APK معاد بناؤه
ممكن تخالف رخصة التطبيق الأصلي أو شروط المتجر — هذه مسؤوليتك، مو الأداة.

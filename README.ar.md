<div align="center">

<img src="assets/banner.svg" alt="aqlab — تعريب أندرويد واتجاه RTL" width="100%">

**اقلب أي تطبيق أندرويد للعربي — باتجاه RTL صحيح — بأمر واحد.**

[![tests](https://github.com/Gilgamesh-110/aqlab/actions/workflows/ci.yml/badge.svg)](https://github.com/Gilgamesh-110/aqlab/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](#المتطلبات)

**العربية** · [English](README.md)

</div>

---

ترجمة التطبيق للعربي هي النص السهل. النص اللي يغلطون فيه الجميع هو الواجهة:
القائمة الجانبية تبقى عاليسار، سهم الرجوع لسا يشير لليسار، شريط التقدم يمتلئ
بالعكس، الـ`%d` يضيع، وعلامات الترقيم تنتقل للجهة الغلط.

**`aqlab` يتولى الجزء اللي مو ترجمة** — اتجاه الواجهة، ترحيل الخصائص، مخاطر
الـbidi في Unicode، وتهريب موارد أندرويد.

## المحتويات

- [ليش](#ليش)
- [التنصيب](#التنصيب)
- [البدء السريع](#البدء-السريع)
- [الأوامر](#الأوامر)
- [شنو يصلّح](#شنو-يصلّح)
- [الجزء اللي ما يسويه غيرنا: فحص bidi](#الجزء-اللي-ما-يسويه-غيرنا-فحص-bidi)
- [شنو **ما** يسوي](#شنو-ما-يسوي)
- [مجرّب على](#مجرّب-على)
- [أسئلة متكررة](#أسئلة-متكررة)
- [المتطلبات](#المتطلبات)
- [المساهمة](#المساهمة)
- [الرخصة](#الرخصة)

## ليش

العربية تكتب من اليمين لليسار. وأندرويد عنده ميكانيزم جاهز لهذا من API 17 —
خصائص `start`/`end` المنطقية مع `android:supportsRtl` — ومع هذا نسبة كبيرة من
التطبيقات المنشورة ما شغّلته، أو شغّلته وخلّت مئات الخصائص `left`/`right` مكتوبة
بالثابت.

العمل اليدوي يعني تدقيق كل layout وكل menu وكل style، وتكتشف الأخطاء فقط لما
تفتح التطبيق بالعربي. `aqlab` يشتغل الـ95% الميكانيكية و**يفحص الباقي**، فتصير
أول نسخة عربية تفتحها صحيحة من أول مرة.

## التنصيب

```sh
pipx install git+https://github.com/Gilgamesh-110/aqlab
```

ملف واحد، مكتبة بايثون القياسية فقط. يشتغل على أدوات عندك أصلاً:

| الأداة | كيف يلگاها |
|---|---|
| `apktool` | `AQLAB_APKTOOL=/path/to/apktool.jar`، وإلا `apktool` على `PATH` |
| `aapt2`, `zipalign`, `apksigner` | `ANDROID_HOME` / `AQLAB_SDK`، وإلا `PATH` |

وتكدر تتجاوز كل أداة لحالها: `AQLAB_AAPT2`, `AQLAB_ZIPALIGN`, `AQLAB_APKSIGNER`,
`AQLAB_JAVA`.

## البدء السريع

```sh
aqlab scan app.apk          # شنو حالة هذا التطبيق؟
```

```
[FINDING] apk: app.apk
  package        : com.example.app
  label          : 'Example'
  locales        : 87 -> --_--, af, am, ar, as, az, be, bg, bn, bs, ca, cs, da, de
  arabic present : yes
  supportsRtl    : no (or not detectable in binary XML)
  native libs    : 6 (renaming packages in native apps can break self-checks)
```

```sh
aqlab decode app.apk -o work        # فك التطبيق
aqlab rtl    work --dry-run         # عاين الترحيل قبل ما تطبّقه
aqlab rtl    work                   # طبّقه
aqlab scaffold work                 # -> work/res/values-ar/strings.tsv
#     ... ترجم العمود الأخير (DeepL، LLM، أو مترجم بشري) ...
aqlab apply  work --tsv work/res/values-ar/strings.tsv
aqlab lint   work                   # يرجع كود خطأ عند وجود أخطاء حقيقية
aqlab build  work --ks release.keystore --ks-alias release
```

أو كل شي بأمر واحد:

```sh
aqlab all app.apk --tsv ar.tsv --ks release.keystore --ks-alias release
```

## الأوامر

| الأمر | شيسوي |
|---|---|
| `scan <apk\|dir>` | تقرير للقراءة فقط: اللغات، `supportsRtl`، عدد `left/right` الباقية، نسبة التغطية. `--json` للأدوات. |
| `decode <apk>` | يفك الـAPK لمشروع قابل للتعديل |
| `rtl <dir>` | يضبط `android:supportsRtl` ويحوّل `left/right` → `start/end` بكل `res/**`. `--dry-run` للمعاينة. |
| `scaffold <dir>` | ينشئ `res/values-ar/` وجدول ترجمة `strings.tsv` |
| `apply <dir> --tsv` | يستورد جدول مترجم إلى `values-ar/strings.xml` |
| `lint <dir>` | مخاطر bidi، أخطاء التهريب، اختلاف الـformat specifiers، التغطية. كود خروج غير صفري عند الأخطاء. |
| `build <dir>` | `apktool b` → `zipalign` → `apksigner sign` → `apksigner verify` |
| `all <apk>` | السلسلة كاملة |

## شنو يصلّح

* `android:supportsRtl="true"` على `<application>`
* `paddingLeft/Right` → `paddingStart/End`، ومثلها `layout_margin*`،
  `drawable*`، `layout_align*`، `layout_alignParent*`، `layout_to*Of`
* ConstraintLayout: `app:layout_constraint{Left,Right}_to{Left,Right}Of` → `{Start,End}`
* قيم `gravity` / `layout_gravity` / `foregroundGravity`
* `textAlignment` → `viewStart` / `viewEnd`
* عناصر `<item name="android:paddingLeft">` داخل `<style>`
* مراجع الـtheme اللي تبقى ترجع لقيمة LTR
  (`?listPreferredItemPaddingLeft` → `?…Start`)
* تهريب موارد أندرويد: `'`، و`@`/`?` بالبداية، والمسافات المهمة

كل تبديل محروس: لما الوسم يكون عنده التوأم `start`/`end` أصلاً، الخصيصة القديمة
**تنحذف** بدل ما تتكرر. هاي الحماية هي الفرق بين مشروع يبني ومشروع يموت داخل
`aapt2` بخطأ `error: duplicate attribute` — وهي السبب اللي يخلي الأداة تشتغل على
تصاميم AppCompat و Material، لأنها تجيب الاثنين سوا.

## الجزء اللي ما يسويه غيرنا: فحص bidi

العربية تمشي من اليمين لليسار. بس الأرقام والكلمات اللاتينية ومسارات الملفات
و`%1$s` داخلها تمشي من اليسار لليمين. لما يلتقون، خوارزمية Unicode للاتجاهين
تعيد ترتيب المقاطع — وعلامات الترقيم تطلع بالجهة الغلط.

```
text:     المجلد test.
renders:  المجلد test.          النقطة تجي يمين "test"؛ لازم تجي يساره
fix:      المجلد test\u200f.    U+200F (علامة من اليمين لليسار) تثبّتها بمكانها
```

`aqlab lint` يمسك هاي، plus الأخطاء اللي **تكسر البناء** فعلاً:

```
[FINDING] lint res/values-ar: 148/190 translated (77.9%)
  [ERROR] discount: bare `%` — must be `%%` or a format specifier, aapt will reject it
  [ERROR] welcome: format specifiers differ: source ['%1$s'] vs arabic ['%s']
  [ERROR] note: unescaped apostrophe — use \' inside Android string resources
  [ERROR] folder: trailing Latin/digit run before punctuation — needs U+200F (RLM)
[FAIL] 4 error(s), 1 warning(s)
```

| القاعدة | المستوى |
|---|---|
| اختلاف الـformat specifiers عن المصدر | error |
| `%` عارية يرفضها `aapt` | error |
| `'` غير مهربة داخل مورد نصي | error |
| القيمة تبدأ بـ`@` أو `?` | error |
| اختلاف وسوم markup (`<b>`، `<xliff:g>`) عن المصدر | error |
| مقطع لاتيني/رقمي بنهاية النص قبل الترقيم (يبي U+200F) | error |
| القيمة مطابقة للمصدر وفيها لاتيني | warn |
| ما بيه حروف عربية بالقيمة العربية | warn |
| فرق بالمسافات بالبداية أو النهاية | warn |
| صيغة جمع تسقط العدد | warn — العربي يسقطه فعلاً بحالة `one`/`two` |
| خلط عربي + لاتيني بنفس النص | info |

كود الخروج غير صفري عند الأخطاء، فينزل مباشرة بالـCI.

## شنو **ما** يسوي

* **ما يترجم شي.** لا ترجمة آلية ولا AI. العمود الأخير بالـTSV شغلك — الأداة
  تضمن إن الميكانيكا تصمد بعد ما تملاه.
* ما يلمس النصوص المخبوزة بالـDEX/smali أو الكود الأصلي.
* ما ينعكس الصور أو الأصول اللي فيها نص لاتيني محروق.
* **ما** يفرض `android:layoutDirection="rtl"` — هذا يكسر باقي اللغات بتطبيق
  متعدد اللغات. `supportsRtl` مع `start`/`end` هو الميكانيزم الصحيح، وهو اللي
  توثّقه مستندات گوگل نفسها.

## مجرّب على

تطبيقات حقيقية، من البداية للنهاية (`decode` → `rtl` → `build` → توقيع →
تحقق) — مو اختبارات وحدات بس. `supportsRtl=true` نقرأها من المانيفست
**المنصّب**، والمخرج مفحوص بـ`apksigner verify` و `zipalign -c -v 4`:

| التطبيق | اللغات | إعادة بناء | توقيع | `supportsRtl` بالمانيفست المنصّب |
|---|---|---|---|---|
| تطبيق متعدد اللغات (`com.gilgamesh.vip`) | 87 | إي | إي | ✅ `true` |
| تطبيق أدوات (`TS_Vip_v115`) | — | إي | إي | ✅ `true` |
| تطبيق أدوات (`bs_1.0`) | — | إي | إي | ✅ `true` |

تمريرة `rtl` **idempotent**: لو شغّلتها مرة ثانية تكول
`already RTL-clean — nothing to migrate` وما تكتب شي.

## أسئلة متكررة

**البناء فشل بـ`error: duplicate attribute`.**
أنت على نسخة قبل حماية التوأم. حدّث، أو افتح issue واذكر اسم الملف اللي بالخطأ.

**يرفض يشتغل ويگول `minSdkVersion=15`.**
موارد `start`/`end` ما توجد قبل API 17. ارفع `minSdkVersion`، أو مرّر
`--force-min-sdk` وأنت تدري إن البناء راح يفشل.

**التطبيق المعاد بناؤه يسوي crash أو يكتشف التعديل.**
متوقّع إذا الأصل يتحقق من توقيعه أو checksums الـdex بشكل أصلي — إعادة البناء
تغيّر الاثنين. شوف [SECURITY.md](SECURITY.md).

**يشتغل مع العبري أو الفارسي أو الأردو؟**
الميكانيكا نفسها — `rtl` و `lint` وجداول الخصائص ما تفرّق بين الحروف، و
`--locale` يقبل أي كود لغة. الجزء العربي هو قواعد الجمع وقواعد bidi.

**ليش ما أستعمل معاينة RTL بالأندرويد ستوديو؟**
هاي تعرض لك *تصميم واحد*. `aqlab` يصلّح كل تصاميم التطبيق ويكولك أي نصوص لسا
مكسورة.

## المتطلبات

Python 3.9+، `apktool`، و Android SDK build-tools (`aapt2`, `zipalign`,
`apksigner`). ما شي غير — بلا اعتماديات pip، بلا `lxml`، بلا `androguard`.

## المساهمة

شوف [CONTRIBUTING.md](CONTRIBUTING.md). مجموعة الاختبار `assert`s عادية وتشتغل
بأي مكان:

```sh
python tests/test_aqlab.py       # 60 فحص، بلا framework، بلا شبكة، بلا apktool
```

الأكثر طلباً: التحقق من صيغ الجمع الست بالعربي، توليد `android:localeConfig`
لمنتقي لغة التطبيق بأندرويد 13+، وتحذيرات انعكاس النص بالـvector drawables.
شوف [CHANGELOG.md](CHANGELOG.md).

## الرخصة

MIT — شوف [LICENSE](LICENSE).

## تنبيه

شغّله على تطبيقات تملكها أو مسموح لك تعدّلها. إعادة نشر APK معاد بناؤه ممكن
تخالف رخصة التطبيق الأصلي أو شروط المتجر. هذه مسؤوليتك، مو مسؤولية الأداة.

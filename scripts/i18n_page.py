"""Attach English text to every translatable node on the landing page.

data-en holds the English string; the Arabic stays in the markup. A node that
the dictionary below does not cover is left alone, and the script prints it --
so a new section cannot ship with a language switch that half-works.
"""
import html
import pathlib
import re
import sys

PAGE = pathlib.Path("C:/Users/speed/arabize/site/index.html")

# The Arabic the page currently shows -> the English it should show.
M = {
    # header
    "ترجم أي تطبيق أندرويد<br><span class=\"hi\">أمر واحد.</span>":
        "Localize any Android app<br><span class=\"hi\">One command.</span>",
    "نصوص، وتخطيط ينعكس صح، وملف APK موقّع جاهز. <b>أمر واحد.</b>":
        "Strings, layouts that mirror correctly, a signed APK. <b>One command.</b>",
    "ثبّته": "Install",
    "شوف الكود": "View source",
    "لغة": "languages",
    "لغة مدعومة": "supported locales",
    "اتجاه RTL": "RTL locales",
    "لغة RTL": "RTL locales",
    "فحص": "checks",
    "فحص آلي": "automated checks",
    "اعتماديات": "dependencies",
    "نسخ": "copy",
    "اننسخ": "copied",

    # video
    "شوفه يشتغل": "Watch it run",
    "تسجيل حقيقي على": "A real run against",
    "الملف تخطيط.": "layout files.",
    "كل رقم هنا من نفّذ فعلياً، مو محلول.":
        "Every number here was executed, not estimated.",
    "11 ثانية · 102 سمة انعكست · 1429 نص جديد":
        "11 seconds · 102 attributes mirrored · 1429 strings scaffolded",

    # problem
    "شنو يتغيّر": "What changes",
    "الترجمة لحالها ما تكفي. لو التخطيط ما انعكس، القائمة تبقى على اليسار والسهم يشير للجهة الغلط، والتطبيق يبقى يحس إنه أجنبي. هذي نفس الشاشة قبل وبعد:":
        "Translation alone is not enough. If the layout does not mirror, the menu stays on the left and the arrow points the wrong way, and the app still feels foreign. Same screen, before and after:",
    "نفس الشاشة بعد": "Same screen after",
    "16 سمة انعكست": "16 attributes mirrored",
    "مخرجات حقيقية من شجرة 1426 نص": "Real output from a 1426-string tree",
    "0 كراش": "0 crashes",

    # terminal
    "ثلاث أوامر، وخلص": "Three commands, done",
    "هذي مخرجات فعليّة من المشروع التجريبي — مو رسم توضيحي.":
        "Real output from the demo project — not an illustration.",

    # features
    "شنو يسوّي بالضبط": "What it actually does",
    "كل بند هنا مقيس على APK حقيقي، مو وعد.":
        "Every item here was measured on a real APK, not promised.",
    "عكس التخطيط": "Layout mirroring",
    "يحوّل كل": "Converts every",
    "إلى": "to",
    "، ويضيف": ", and adds",
    ". وما يلمس التثبيتات المقصودة — الأزرار اللي لازم تبقى يسار.":
        ". It leaves deliberate pins alone — buttons that must stay left.",
    "جمع صحيح من CLDR": "Correct plurals from CLDR",
    "العربية ستّ صيغ، الإنجليزية ثنتين. الجداول مولّدة من CLDR الحقيقي، مو مخمّنة — 227 لغة، وتقسيم حسب المنطقة.":
        "Arabic has six forms, English two. Tables generated from real CLDR, not guessed — 227 locales, split by region.",
    "فحص bidi": "Bidi checking",
    "يكشف الأخطاء اللي تصير لما يختلط العربي باللاتيني أو الأرقام. الأخطاء اللي ما تنشاف بالعين وتطلع بالجهاز.":
        "Catches what goes wrong when Arabic mixes with Latin or digits. The errors you cannot see on screen that surface on the device.",
    "حماية المُحدِّدات": "Placeholder protection",
    "و": "and",
    "تبقى كما هي. أي انحراف بالمُحدِّد يفشل الفحص قبل ما يوصل للمستخدم.":
        "stay exactly as they are. Any drift in a placeholder fails the check before a user sees it.",
    "بناء موقّع": "Signed builds",
    "فك، وعدّل، وأعد البناء، ووقّع — كلها بأمر واحد. أو كل خطوة لحالها لما تحتاج تحكّم.":
        "Decode, edit, rebuild, sign — one command. Or each step alone when you need control.",
    "صديق لـ CI": "CI-friendly",
    "يطبع مستند نظيف، و": "prints a clean document, and",
    "يبوّب التغطية. يشتغل على 3.9 إلى 3.13، ويندوز/لينكس/ماك.":
        "gates coverage. Runs on 3.9 through 3.13, Windows/Linux/macOS.",

    # commands
    "الأوامر": "Commands",
    "إحدى عشرة خطوة، كل واحدة تشتغل لحالها أو متسلسلة.":
        "Eleven steps, each one runnable alone or chained.",
    "تقرير للقراءة فقط — ما يغيّر ولا بايت": "read-only report — changes zero bytes",
    "يمكّن RTL ويحوّل left/right إلى start/end": "enables RTL and converts left/right to start/end",
    "ينشئ مجلد اللغة وجدول ترجمة": "creates the locale folder and a translation table",
    "يستورد الترجمات من الجدول": "imports translations from the table",
    "يعبّي من ذاكرة ترجمة ومصطلحات": "fills from a translation memory and glossary",
    "فحص المُحدِّدات والهروب والتغطية": "checks placeholders, escaping and coverage",
    "يحمي المُحدِّدات بـ": "wraps placeholders in",
    "الفرق بين نصوص نسختين": "string diff between two builds",
    "إعادة بناء + zipalign + توقيع": "rebuild + zipalign + sign",
    "السلسلة كاملة بأمر واحد": "the full chain in one command",

    # languages
    "ما هو محصور بلعربي": "Not an Arabic-only tool",
    "العربية أصعب حالة — ستّ صيغ جمع مقابل ثنتين بالإنجليزي، زائد bidi. عشانها كل الفحوصات بُنيت.":
        "Arabic is the hardest case — six plural forms against English's two, plus bidi. Every check was built against it.",
    "بس هي الحد الأقصى، مو النطاق:": "It is the ceiling, not the scope:",

    # comparison
    "مقابل الشغل اليدوي": " vs. doing it by hand",
    "نفس التطبيق:": "Same app:",
    "ملف تخطيط، و102 سمة": "layout files, and 102",
    "موزّعة على 29 ملف.": "attributes spread over 29 files.",
    "يدوي": "By hand",
    "فك التطبيق": "Decoding",
    "تنزيل apktool، حفظ مسار الجار، أمر طويل": "download apktool, remember the jar path, one long command",
    "عكس 102 سمة": "Mirroring 102 attributes",
    "فتح 29 ملف، بحث واستبدال، ونسيان واحد يكسر الشاشة":
        "open 29 files, find and replace, and one miss breaks a screen",
    "5.2 ثانية، 29 ملف، صفر سمة باقية": "5.2 seconds, 29 files, zero left behind",
    "السمات اللي ما لازم تنعكس": "Attributes that must not flip",
    "بحث-واستبدال أعمى يعكس اللي مو المفروض": "blind find-and-replace flips what should not move",
    "يتجاهل التثبيت المقصود، ويبلّغ عن Asymmetric": "skips deliberate pins, reports asymmetric ones",
    "تذكّر تعديل المانيفست": "remember to edit the manifest",
    "تلقائي": "automatic",
    "صيغ الجمع": "Plural forms",
    "تحتاج تعرف إن العربي ست صيغ، وإلا crash بـ":
        "you have to know Arabic has six forms, or crash with",
    "جداول CLDR، 227 لغة": "CLDR tables, 227 locales",
    "تنكسر بصمت، وتنكشف بالمستخدم": "break silently, found by a user",
    "يُفحص قبل البناء، ويفشل الـCI": "checked before the build, fails CI",
    "إعادة البناء والتوقيع": "Rebuild and sign",
    "zipalign + apksigner بأوامر منفصلة": "zipalign + apksigner as separate commands",
    "يكرر نفس الشغل": "Repeating the work",
    "كل نسخة جديدة من الصفر": "every new release from scratch",
    "جدول ترجمة يُعاد استعماله، وذاكرة ترجمة":
        "a translation table you reuse, and a memory",
    "التحقق قبل الشحن": "Verification before shipping",
    "يدوياً، أو لا شي": "manual, or nothing",

    # apk diff
    "ملف حقيقي، سمة حقيقية": "A real file, a real attribute",
    "هذا ليس مثالاً مكتوباً. هذا مقطع من": "This is not a written example. It is a snippet from",
    "داخل": "inside",
    "قبل وبعد": "before and after",
    "قبل": "before",
    "بعد": "after",
    "ملف": "files",
    "سمة left/right": "left/right attributes",
    "ملف تعدّل": "files changed",
    "الوقت": "wall time",
    "سمة باقية": "left behind",
    "لاحظ الـ": "Notice that",
    "ما صار بس اسم جديد —": "is not just a new name —",
    "قيمته انقلبت من": "its value flipped from",
    "إلى": "to",
    ". لو انقلب الاسم بس، التخطيط يبقى معكوس.":
        ". Rename alone and the layout stays mirrored.",

    # faq
    "أسئلة سريعة": "Quick answers",
    "يحتاج أدوات أندرويد مثبّتة؟": "Do I need the Android SDK?",
    "الفحص والتعديل على النصوص والتخطيط: لا. البناء والتوقيع: إيه — يحتاج apktool وaapt2. الأداة تكتشفهم وتقول لك شنو ناقص بالضبط.":
        "Scanning and editing strings and layouts: no. Building and signing: yes — it needs apktool and aapt2. The tool detects them and tells you exactly what is missing.",
    "يكسر التطبيق؟": "Will it break my app?",
    "ما يلمس إلا موارد النصوص والتخطيط.": "It touches nothing but string and layout resources.",
    "يطبع الخطة قبل أي كتابة، و": "prints the plan before writing anything, and",
    "يعطيك تقرير كامل. جرّب على نسخة أول.": "gives you a full report. Try it on a copy first.",
    "ليش مو ملف تنفيذي؟": "Why not a binary?",
    "بايثون قياسي فقط، ملف واحد، صفر اعتماديات. يعني يشتغل بلا بيئة معقّدة، وما ينكسر لما تتحدّث مكتبة.":
        "Stdlib only, one file, zero dependencies. It runs without a complex environment and does not break when a library updates.",
    "يشتغل على أي إصدار بايثون؟": "Which Python versions?",
    "3.9 إلى 3.13، على ويندوز ولينكس وماك. المصفوفة كلها مختبَرة على CI — عشرة ناقلات، كلها خضراء.":
        "3.9 through 3.13, on Windows, Linux and macOS. The whole matrix is tested on CI — ten runners, all green.",
    "مجاني؟": "Is it free?",
    "إيه. رخصة MIT — استخدمه وعدّله ووزّعه بلا قيود.":
        "Yes. MIT — use it, change it, ship it, no conditions.",

    # cta
    "جرّبه على تطبيق عندك": "Try it on an app of yours",
    "أمر واحد للتثبيت. أمر واحد للفحص — وما يغيّر ولا بايت.":
        "One command to install. One to scan — and it changes zero bytes.",
    "ثبّته من PyPI": "Install from PyPI",
    "الكود على GitHub": "Code on GitHub",
    "رخصة MIT": "MIT licensed",
    "بايثون قياسي · صفر اعتماديات": "stdlib · zero dependencies",
}


def main():
    page = PAGE.read_text(encoding="utf-8")
    applied = 0
    missing = []

    for key in sorted(M, key=len, reverse=True):
        en = html.escape(M[key], quote=True)
        # match a node's inner HTML exactly
        pattern = re.compile(
            r">(\s*)(" + re.escape(key) + r")(\s*)</(h1|h2|h3|p|span|b|div|code|summary|a|figcaption|button)>"
        )
        def sub(m):
            nonlocal applied
            applied += 1
            return (f'{m.group(1)}{m.group(2)}{m.group(3)}'
                    f'</{m.group(4)}>'.replace(
                        f'</{m.group(4)}>',
                        f'</{m.group(4)}>', 1))
        # simpler: insert data-en on the opening tag; find it by walking back
        for m in list(pattern.finditer(page)):
            close = m.end()
            head = page.rfind("<", 0, m.start() + 1)
            tag_end = page.index(">", head)
            opener = page[head:tag_end]
            if "data-en" in opener:
                continue
            page = (page[:tag_end] + f' data-en="{en}"' + page[tag_end:])
            applied += 1
            break

    PAGE.write_text(page, encoding="utf-8")
    print(f"data-en applied: {applied}")

    # audit: any Arabic text left without data-en?
    leftovers = []
    for m in re.finditer(r"<(h1|h2|h3|p|span|b|summary)([^>]*)>([^<>]{4,})</\1>", page):
        attrs, text = m.group(2), m.group(3)
        if re.search(r"[\u0600-\u06FF]", text) and "data-en" not in attrs:
            leftovers.append(text.strip()[:60])

    if leftovers:
        print(f"\n{len(leftovers)} Arabic node(s) still untranslated:")
        for s in leftovers[:15]:
            print("   ", s)
    else:
        print("every Arabic node carries data-en")


if __name__ == "__main__":
    main()

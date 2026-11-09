"""Add English twins to the original seven sections, then install the switch.

Every node keeps its Arabic; data-en sits beside it. The audit at the end lists
any Arabic leaf that still has no English, so the page cannot ship with a switch
that leaves half of itself behind.

Arabic is snapshotted by an inline script at the top of <body> -- before the
switch can run -- because a returning visitor with "en" in localStorage would
otherwise overwrite the only copy of the Arabic text.
"""
import html
import pathlib
import re
import sys

PAGE = pathlib.Path("C:/Users/speed/arabize/site/index.html")

# Exact Arabic inner HTML -> English. Keyed by the Arabic as it appears.
M = {
    'ترجم أي تطبيق أندرويد<br><span class="hi">أمر واحد.</span>':
        'Localize any Android app<br><span class="hi">One command.</span>',
    "نصوص، وتخطيط ينعكس صح، وبناء موقّع. <b>أمر واحد.</b>":
        "Strings, layouts that mirror correctly, a signed APK. <b>One command.</b>",
    "ثبّته": "Install",
    "شوف الكود": "View source",
    "نسخ": "copy",
    "لغة": "locales",
    "اتجاه RTL": "RTL",
    "فحص": "checks",
    "اعتماديات": "dependencies",
    "شنو يتغيّر": "What changes",
    "ثلاث أوامر، وخلص": "Three commands, done",
    "شنو يسوّي بالضبط": "What it actually does",
    "الأوامر": "Commands",
    "ما هو محصور بلعربي": "Not an Arabic-only tool",
    "أسئلة سريعة": "Quick answers",
    "عكس التخطيط": "Layout mirroring",
    "جمع صحيح من CLDR": "Correct plurals from CLDR",
    "فحص bidi": "Bidi checking",
    "حماية المُحدِّدات": "Placeholder protection",
    "بناء موقّع": "Signed builds",
    "صديق لـ CI": "CI-friendly",
    "نفس الشاشة بعد": "Same screen after",
    "16 سمة انعكست": "16 attributes mirrored",
    "مخرجات حقيقية من شجرة 1426 نص": "Real output from a 1426-string tree",
    "0 كراش": "0 crashes",
    "يحتاج أدوات أندرويد مثبّتة؟": "Do I need the Android SDK?",
    "يكسر التطبيق؟": "Will it break my app?",
    "ليش مو ملف تنفيذي؟": "Why not a binary?",
    "يشتغل على أي إصدار بايثون؟": "Which Python versions?",
    "مجاني؟": "Is it free?",
    "جرّبه على تطبيق عندك": "Try it on an app of yours",
    "ثبّته من PyPI": "Install from PyPI",
    "الكود على GitHub": "Code on GitHub",
    "رخصة MIT": "MIT licensed",
    "بايثون قياسي · صفر اعتماديات": "stdlib · zero dependencies",
    "ما هو محصور": "Not limited",
}

LONG = {
    "هذي مخرجات فعليّة من المشروع التجريبي — مو رسم توضيحي.":
        "Real output from the demo project -- not an illustration.",
    "الترجمة لحالها ما تكفي. لو التخطيط ما انعكس، القائمة تبقى على اليسار والسهم يشير للجهة الغلط، والتطبيق يبقى يحس إنه أجنبي. هذي نفس الشاشة قبل وبعد:":
        "Translation alone is not enough. If the layout does not mirror, the menu stays on the left and the arrow points the wrong way, and the app still feels foreign. Same screen, before and after:",
    "كل بند هنا مقيس على APK حقيقي، مو وعد.":
        "Every item here was measured on a real APK, not promised.",
    "إحدى عشرة خطوة، كل واحدة تشتغل لحالها أو متسلسلة.":
        "Eleven steps, each one runnable alone or chained.",
    "العربية أصعب حالة — ستّ صيغ جمع مقابل ثنتين بالإنجليزي، زائد bidi. عشانها كل الفحوصات بُنيت.":
        "Arabic is the hardest case -- six plural forms against English's two, plus bidi. Every check was built against it.",
    "بس هي الحد الأقصى، مو النطاق:":
        "It is the ceiling, not the scope:",
    "بايثون قياسي فقط، ملف واحد، صفر اعتماديات. يعني يشتغل بلا بيئة معقّدة، وما ينكسر لما تتحدّث مكتبة.":
        "Stdlib only, one file, zero dependencies. It runs without a complex environment and does not break when a library updates.",
    "3.9 إلى 3.13، على ويندوز ولينكس وماك. المصفوفة كلها مختبَرة على CI — عشرة ناقلات، كلها خضراء.":
        "3.9 through 3.13, on Windows, Linux and macOS. The whole matrix is tested on CI -- ten runners, all green.",
    "إيه. رخصة MIT — استخدمه وعدّله ووزّعه بلا قيود.":
        "Yes. MIT -- use it, change it, ship it, no conditions.",
    "أمر واحد للتثبيت. أمر واحد للفحص — وما يغيّر ولا بايت.":
        "One command to install. One to scan -- and it changes zero bytes.",
    "الفحص والتعديل على النصوص والتخطيط: لا. البناء والتوقيع: إيه — يحتاج apktool وaapt2. الأداة تكتشفهم وتقول لك شنو ناقص بالضبط.":
        "Scanning and editing strings and layouts: no. Building and signing: yes -- it needs apktool and aapt2. The tool detects them and tells you exactly what is missing.",
    "ما يلمس إلا موارد النصوص والتخطيط.":
        "It touches nothing but string and layout resources.",
    "يطبع الخطة قبل أي كتابة، ويعطيك تقرير كامل. جرّب على نسخة أول.":
        "It prints the plan before writing anything and gives you a full report. Try it on a copy first.",
}

ALL = dict(M)
ALL.update(LONG)

M2 = {
    "تقرير للقراءة فقط — ما يغيّر ولا بايت": "read-only report -- changes zero bytes",
    "يمكّن RTL ويحوّل left/right إلى start/end": "enables RTL and converts left/right to start/end",
    "ينشئ مجلد اللغة وجدول ترجمة": "creates the locale folder and a translation table",
    "يستورد الترجمات من الجدول": "imports translations from the table",
    "يعبّي من ذاكرة ترجمة ومصطلحات": "fills from a translation memory and a glossary",
    "فحص المُحدِّدات والهروب والتغطية": "checks placeholders, escaping and coverage",
    "يحمي المُحدِّدات بـ &lt;xliff:g&gt;": "wraps placeholders in &lt;xliff:g&gt;",
    "الفرق بين نصوص نسختين": "string diff between two builds",
    "إعادة بناء + zipalign + توقيع": "rebuild + zipalign + sign",
    "السلسلة كاملة بأمر واحد": "the full chain in one command",
    "العربية ستّ صيغ، الإنجليزية ثنتين. الجداول مولّدة من CLDR الحقيقي، مو مخمّنة — 227 لغة، وتقسيم حسب المنطقة.":
        "Arabic has six forms, English two. Tables generated from real CLDR, not guessed -- 227 locales, split by region.",
    "يكشف الأخطاء اللي تصير لما يختلط العربي باللاتيني أو الأرقام. الأخطاء اللي ما تنشاف بالعين وتطلع بالجهاز.":
        "Catches what goes wrong when Arabic mixes with Latin or digits. The errors you cannot see on screen that surface on the device.",
    "فك، وعدّل، وأعد البناء، ووقّع — كلها بأمر واحد. أو كل خطوة لحالها لما تحتاج تحكّم.":
        "Decode, edit, rebuild, sign -- one command. Or each step alone when you need control.",
    "aqlab 0.2.1 · رخصة MIT": "aqlab 0.2.2 · MIT licensed",
    "عربي": "عربي",
}
ALL.update(M2)



def add_data_en(page, arabic, english, tag_hint=None):
    """Attach data-en to the element whose inner content is exactly `arabic`."""
    esc = html.escape(english, quote=True)
    pat = re.compile(
        r"<(?P<tag>h1|h2|h3|p|span|b|div|code|button|a|summary|figcaption)"
        r"(?P<attrs>[^>]*)>(?P<inner>\s*" + re.escape(arabic) + r"\s*)</(?P=tag)>",
        re.S,
    )
    m = pat.search(page)
    if not m:
        return page, False
    if "data-en=" in m.group("attrs"):
        return page, True
    new_open = f'<{m.group("tag")}{m.group("attrs")} data-en="{esc}">'
    return page[:m.start()] + new_open + m.group("inner") + f'</{m.group("tag")}>' + page[m.end():], True


SWITCH_JS = """
<script>
/* ---- language switch ----
   Arabic lives in the markup, English in data-en beside it. With JS off the page
   is Arabic -- the language the project is about -- and it stays fully readable
   and indexable for crawlers.

   The Arabic snapshot is taken by the inline script at the top of <body>, before
   this one runs. Building it here would be too late: a returning visitor may
   have "en" in localStorage, the swap happens on load, and the snapshot would
   capture English as the original -- losing the way back. */
(function () {
  var nodes = Array.prototype.slice.call(document.querySelectorAll('[data-en]'));
  var arabic = window.__aqlabArabic || {};

  window.setLang = function (lang) {
    var ar = lang === 'ar';
    document.getElementById('lang-ar').setAttribute('aria-pressed', String(ar));
    document.getElementById('lang-en').setAttribute('aria-pressed', String(!ar));
    document.documentElement.lang = lang;
    document.documentElement.dir = ar ? 'rtl' : 'ltr';
    document.body.dir = ar ? 'rtl' : 'ltr';
    document.documentElement.classList.toggle('lang-en', !ar);
    nodes.forEach(function (el, i) {
      var v = ar ? arabic[i] : el.dataset.en;
      if (v !== undefined) el.innerHTML = v;
    });
    try { localStorage.setItem('aqlab-lang', lang); } catch (e) {}
  };

  try {
    if (localStorage.getItem('aqlab-lang') === 'en') setLang('en');
  } catch (e) {}
})();
</script>
"""

SNAPSHOT = """<script>
/* Snapshot the Arabic before the switch script can replace it: a visitor with
   "en" stored would otherwise have English recorded as the original text and the
   way back to Arabic would be lost. Runs at parse time, markup is still Arabic. */
window.__aqlabArabic = Array.prototype.map.call(
  document.querySelectorAll('[data-en]'),
  function (el) { return el.innerHTML; }
);
</script>
"""


def main():
    page = PAGE.read_text(encoding="utf-8")

    # The switch div is inline in the body; give it the snapshot right after it.
    if "__aqlabArabic" not in page:
        page = page.replace("</div>\n", "</div>\n" + SNAPSHOT, 1)

    applied, missing = 0, []
    for ar, en in sorted(ALL.items(), key=lambda kv: len(kv[0]), reverse=True):
        page, ok = add_data_en(page, ar, en)
        if ok:
            applied += 1
        else:
            missing.append(ar)

    if SWITCH_JS.strip() not in page:
        page = page.replace("</body>", SWITCH_JS + "</body>", 1)

    PAGE.write_text(page, encoding="utf-8")
    print(f"data-en applied: {applied}/{len(ALL)}")
    if missing:
        print(f"not found ({len(missing)}):")
        for x in missing[:20]:
            print("   ", x[:70])

    # audit: Arabic leaf nodes still without English
    leaves = []
    for m in re.finditer(r"<(h1|h2|h3|p|span|b|summary|button|a|td|th)([^>]*)>([^<>]{3,})</\1>",
                         page, re.S):
        attrs, text = m.group(2), m.group(3)
        if re.search(r"[\u0600-\u06FF]", text) and "data-en" not in attrs:
            t = text.strip()
            if t and not t.isdigit():
                leaves.append(t[:60])
    print(f"\nArabic leaves without English: {len(leaves)}")
    for x in leaves[:20]:
        print("   ", x)


if __name__ == "__main__":
    main()

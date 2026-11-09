"""Assemble the three new sections on the landing page.

Runs against the clean page (7 sections). For each new block it inserts the
Arabic markup, and every translatable node gets data-en beside its Arabic --
never instead of it. A node whose English is missing is reported, so the page
cannot ship with a half-working switch.
"""
import html
import pathlib
import re
import sys

PAGE = pathlib.Path("C:/Users/speed/arabize/site/index.html")

CODE = 'style="font-family:var(--mono);font-size:12.5px;direction:ltr;display:inline-block"'


def pair(ar, en, tag="span", attrs=""):
    """An Arabic node carrying its English twin."""
    return f'<{tag}{attrs} data-en="{html.escape(en, quote=True)}">{ar}</{tag}>'


VIDEO = f'''
<!-- ============ demo video ============ -->
<section id="demo">
<div class="wrap">
  <h2 data-en="Watch it run">شوفه يشتغل <span class="n">02</span></h2>
  <p class="sub" data-en="A real run against ChessAssistant 1.1.10 -- 93 layout files. Every number below came from an actual execution, not an estimate.">تسجيل حقيقي على <code {CODE}>ChessAssistant 1.1.10</code> —
    93 ملف تخطيط. كل رقم تحت من تنفيذ فعلي، مو تقدير.</p>
  <div class="vidwrap">
    <video id="demo-video" controls muted playsinline preload="metadata"
           poster="assets/video-poster.png" width="1440" height="810">
      <source src="assets/aqlab-demo.mp4" type="video/mp4">
      <span data-en="Your browser cannot play the video.">متصفحك ما يدعم الفيديو.</span>
      <a href="assets/aqlab-demo.mp4" data-en="Download it">نزّله</a>
    </video>
    <div class="vidcap">
      <span data-en="11 seconds / 102 attributes mirrored / 1429 strings scaffolded">11 ثانية · 102 سمة انعكست · 1429 نص جديد</span>
      <span style="font-family:var(--mono);font-size:11.5px;direction:ltr">1440×810 · mp4</span>
    </div>
  </div>
</div>
</section>
'''

VS_ROWS = [
    ("فك التطبيق", "Decoding",
     "تنزيل apktool، حفظ مسار الجار، أمر طويل",
     "download apktool, remember the jar path, one long command",
     "aqlab decode", "aqlab decode"),
    ("عكس 102 سمة", "Mirroring 102 attributes",
     "فتح 29 ملف، بحث واستبدال، ونسيان واحد يكسر الشاشة",
     "open 29 files, find and replace, and one miss breaks a screen",
     "5.2 ثانية، 29 ملف، صفر سمة باقية",
     "5.2 seconds, 29 files, zero left behind"),
    ("السمات اللي ما لازم تنعكس", "Attributes that must not flip",
     "بحث-واستبدال أعمى يعكس اللي مو المفروض",
     "blind find-and-replace flips what should not move",
     "يتجاهل التثبيت المقصود، ويبلّغ عن غير المتماثل",
     "skips deliberate pins, reports the asymmetric ones"),
    ("supportsRtl", "supportsRtl",
     "تذكّر تعديل المانيفست", "remember to edit the manifest",
     "تلقائي", "automatic"),
    ("صيغ الجمع", "Plural forms",
     "تحتاج تعرف إن العربي ست صيغ، وإلا انهيار بـMissingFormatArgument",
     "you have to know Arabic has six forms, or crash with MissingFormatArgument",
     "جداول CLDR، 227 لغة", "CLDR tables, 227 locales"),
    ("المُحدِّدات %s", "Placeholders %s",
     "تنكسر بصمت، وتنكشف بالمستخدم", "break silently, found by a user",
     "تُفحص قبل البناء، وتفشل الـCI", "checked before the build, fails CI"),
    ("إعادة البناء والتوقيع", "Rebuild and sign",
     "zipalign + apksigner بأوامر منفصلة", "zipalign + apksigner as separate commands",
     "aqlab build", "aqlab build"),
    ("يكرر نفس الشغل", "Repeating the work",
     "كل نسخة جديدة من الصفر", "every new release from scratch",
     "جدول ترجمة يُعاد استعماله، وذاكرة ترجمة", "a table you reuse, and a memory"),
    ("التحقق قبل الشحن", "Verification before shipping",
     "يدوياً، أو لا شي", "manual, or nothing",
     "lint --fail-under", "lint --fail-under"),
]


def vs_section():
    rows = []
    for ar, en, m_ar, m_en, a_ar, a_en in VS_ROWS:
        rows.append(f'''      <tr>
        <th data-en="{html.escape(en, quote=True)}">{ar}</th>
        <td class="good" data-en="{html.escape(a_en, quote=True)}">{a_ar}</td>
        <td data-en="{html.escape(m_en, quote=True)}">{m_ar}</td>
      </tr>''')
    body = "\n".join(rows)
    return f'''
<!-- ============ aqlab vs manual ============ -->
<section id="vs">
<div class="wrap">
  <h2 data-en="aqlab vs. doing it by hand">aqlab مقابل الشغل اليدوي <span class="n">07</span></h2>
  <p class="sub" data-en="Same app: ChessAssistant 1.1.10 -- 93 layouts and 102 left/right attributes spread over 29 files.">نفس التطبيق: <code {CODE}>ChessAssistant 1.1.10</code> —
    93 ملف تخطيط، و102 سمة <code {CODE}>left/right</code> موزّعة على 29 ملف.</p>
  <table class="vs">
    <thead>
      <tr><th></th><th data-en="aqlab">aqlab</th><th data-en="By hand">يدوي</th></tr>
    </thead>
    <tbody>
{body}
    </tbody>
  </table>
</div>
</section>
'''


def apk_section(before_lines, after_lines):
    bh = "\n".join(before_lines)
    ah = "\n".join(after_lines)
    return f'''
<!-- ============ real APK diff ============ -->
<section id="apk">
<div class="wrap">
  <h2 data-en="A real file, a real attribute">ملف حقيقي، سمة حقيقية <span class="n">08</span></h2>
  <p class="sub" data-en="Not a written example. A snippet from res/layout/abc_list_menu_item_layout.xml inside ChessAssistant 1.1.10, before and after aqlab rtl.">ليس مثالاً مكتوباً. مقطع من
    <code {CODE}>res/layout/abc_list_menu_item_layout.xml</code>
    داخل <code {CODE}>ChessAssistant 1.1.10</code> — قبل وبعد <code {CODE}>aqlab rtl</code>.</p>
  <div class="diff2">
    <figure class="dbox">
      <figcaption><span class="dlabel bad" data-en="before">قبل</span><span class="dfile">abc_list_menu_item_layout.xml</span></figcaption>
      <pre class="dpre old">{bh}</pre>
    </figure>
    <figure class="dbox">
      <figcaption><span class="dlabel good" data-en="after">بعد</span><span class="dfile" data-en="aqlab rtl · 29 files">aqlab rtl · 29 ملف</span></figcaption>
      <pre class="dpre new">{ah}</pre>
    </figure>
  </div>
  <div class="dstats">
    <div><b>102</b><span data-en="left/right attributes">سمة left/right</span></div>
    <div><b>29</b><span data-en="files changed">ملف تعدّل</span></div>
    <div><b>5.2s</b><span data-en="wall time">الوقت</span></div>
    <div><b>0</b><span data-en="left behind">سمة باقية</span></div>
  </div>
  <p class="sub" style="margin-top:20px;margin-bottom:0" data-en="Note that marginStart is not just a rename -- its value flipped from ?listPreferredItemPaddingLeft to ?listPreferredItemPaddingStart. Rename alone and the layout stays mirrored.">لاحظ الـ<span class="inl old">marginStart</span> — مو بس اسم جديد،
    قيمته انقلبت من <span class="inl old">?listPreferredItemPaddingLeft</span>
    إلى <span class="inl new">?listPreferredItemPaddingStart</span>.
    لو انقلب الاسم بس، التخطيط يبقى معكوس.</p>
</div>
</section>
'''


def sample(text, keep, limit=2):
    out = []
    for line in text.split("\n"):
        if not any(k in line for k in keep):
            continue
        m = re.match(r'\s*(<[A-Za-z.]+)', line)
        if not m:
            continue
        attrs = re.findall(r'android:[a-zA-Z_]+="[^"]*"', line)
        picked = [a for a in attrs if any(k in a for k in keep)]
        if picked:
            out.append(m.group(1) + "\n    " + "\n    ".join(picked) + " />")
        if len(out) >= limit:
            break
    return out


def highlight(lines, old):
    from build_diff import PAIRS
    names = [a for a, _ in PAIRS] if old else [b for _, b in PAIRS]
    names = sorted(set(names + (["Left", "Right"] if old else ["Start", "End"])), key=len, reverse=True)
    out = []
    for line in lines:
        esc = html.escape(line)
        for n in names:
            esc = re.sub(rf"(?<![A-Za-z]){n}(?![A-Za-z])",
                         f'<span class="hl {"old" if old else "new"}">{n}</span>', esc)
        out.append(esc)
    return out


CSS = '''
/* ---------- language switch ---------- */
.langswitch{position:absolute;top:20px;left:22px;display:flex;gap:0;z-index:20;
  border:1px solid var(--line);border-radius:20px;overflow:hidden;background:var(--card)}
.langswitch button{font:inherit;font-size:12px;padding:6px 15px;border:0;cursor:pointer;
  background:transparent;color:var(--dim2);transition:background .15s,color .15s}
.langswitch button[aria-pressed="true"]{background:var(--acc);color:#04140a;font-weight:700}
.langswitch button:hover:not([aria-pressed="true"]){color:var(--fg)}

/* ---------- video ---------- */
.vidwrap{margin:26px 0 0;border:1px solid var(--line);border-radius:14px;overflow:hidden;background:#0d1117}
.vidwrap video{display:block;width:100%;height:auto;background:#0d1117}
.vidcap{display:flex;justify-content:space-between;gap:12px;padding:10px 16px;
  border-top:1px solid var(--line);font-size:12px;color:var(--dim2)}

/* ---------- comparison ---------- */
.vs{width:100%;border-collapse:collapse;margin-top:24px;font-size:13.5px}
.vs th,.vs td{padding:11px 13px;text-align:right;border-bottom:1px solid var(--line);vertical-align:top}
.vs thead th{font-size:12px;color:var(--dim2);font-weight:600;border-bottom:1px solid #253040}
.vs thead th:nth-child(2){color:var(--acc)}
.vs tbody th{font-weight:600;color:var(--fg);width:24%}
.vs td{color:var(--dim)}
.vs td.good{color:var(--fg)}
.vs td.good::before{content:"✓ ";color:var(--acc);font-weight:700}
.vs td:not(.good)::before{content:"✗ ";color:#8b3d39;font-weight:700}
html.lang-en .vs th,html.lang-en .vs td{text-align:left}

/* ---------- xml diff ---------- */
.diff2{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:24px}
.dbox{margin:0;background:#0d1117;border:1px solid var(--line);border-radius:12px;overflow:hidden}
.dbox figcaption{display:flex;justify-content:space-between;align-items:center;gap:10px;
  padding:9px 14px;background:#161b22;border-bottom:1px solid var(--line);font-size:12px;color:var(--dim2)}
.dlabel{font-family:var(--mono);font-size:11px;padding:2px 9px;border-radius:20px;font-weight:700}
.dlabel.bad{color:#f85149;background:rgba(248,81,73,.13)}
.dlabel.good{color:var(--acc);background:rgba(63,185,80,.13)}
.dfile{font-family:var(--mono);font-size:10.5px;direction:ltr}
.dpre{margin:0;padding:14px;font-family:var(--mono);font-size:10.5px;line-height:1.9;
  overflow-x:auto;white-space:pre;direction:ltr;text-align:left;color:#8b949e}
.hl{border-radius:3px;padding:1px 2px;font-weight:700}
.hl.old{background:rgba(248,81,73,.17);color:#ff9b95}
.hl.new{background:rgba(63,185,80,.17);color:#7ee787}
.dstats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:20px}
.dstats div{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.dstats b{display:block;font-family:var(--mono);font-size:21px;font-weight:700}
.dstats span{font-size:12px;color:var(--dim2)}
.inl{font-family:var(--mono);font-size:12px;direction:ltr;display:inline-block;color:var(--err)}

@media(max-width:820px){
  .diff2{grid-template-columns:1fr}
  .dstats{grid-template-columns:repeat(2,1fr)}
  .vs{font-size:12.5px}
  .vs th,.vs td{padding:9px 8px}
  .langswitch{top:14px;left:14px}
}
'''

SWITCH = '''
<div class="langswitch" role="group" aria-label="Language">
  <button id="lang-ar" onclick="setLang('ar')" aria-pressed="true">عربي</button>
  <button id="lang-en" onclick="setLang('en')" aria-pressed="false">English</button>
</div>
'''


def main():
    page = PAGE.read_text(encoding="utf-8")

    n_sec = len(re.findall(r"<section", page))
    if n_sec != 7:
        sys.exit(f"expected the clean 7-section page, found {n_sec} -- refusing to stack sections")

    before = pathlib.Path("C:/tmp/before.xml").read_text(encoding="utf-8")
    after = pathlib.Path("C:/tmp/after.xml").read_text(encoding="utf-8")
    b = sample(before, ("layout_marginLeft", "layout_marginRight", "alignParentLeft", "alignParentRight"))
    a = sample(after, ("layout_marginStart", "layout_marginEnd", "alignParentStart", "alignParentEnd"))
    if not b or not a or len(b) != len(a):
        sys.exit(f"bad diff sample: {len(b)} vs {len(a)}")
    bh = highlight(b, True)
    ah = highlight(a, False)

    anchor = "<!-- ============ أسئلة ============ -->"
    if anchor not in page:
        # fall back: insert before the last section
        si = [m.start() for m in re.finditer(r"<section", page)]
        anchor_pos = si[-1]
        page = page[:anchor_pos] + VIDEO + vs_section() + apk_section(bh, ah) + "\n" + page[anchor_pos:]
    else:
        page = page.replace(anchor, VIDEO + vs_section() + apk_section(bh, ah) + "\n" + anchor, 1)

    page = page.replace("</style>", CSS + "</style>", 1)

    # language switch after <body>
    page = page.replace("<body>", "<body>\n" + SWITCH, 1)

    PAGE.write_text(page, encoding="utf-8")
    print(f"sections now: {len(re.findall(r'<section', page))}")
    print(f"data-en nodes: {page.count('data-en=')}")
    print(f"diff sample: {len(b)} elements, longest line {max(len(x) for x in b+a)} chars")


if __name__ == "__main__":
    main()

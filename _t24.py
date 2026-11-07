import io
p = "tests/test_aqlab.py"
s = io.open(p, encoding="utf-8", newline="").read()
old = """    (res / "layout").mkdir(parents=True)\r
    (res / "layout" / "a.xml").write_text(\r
        '<?xml version="1.0" encoding="utf-8"?>\n'\r
        '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"'\r
        ' xmlns:app="http://schemas.android.com/apk/res-auto">\n'\r
"""
new = """    (res / "layout").mkdir(parents=True)\r
    # `layoutDirection` is only a finding in a build that opted into RTL: with\r
    # no switch the platform mirrors nothing and every `ltr` row renders as\r
    # written. `test_rtl_enabled_gates` owns that gate and the opt-out finding.\r
    (root / "AndroidManifest.xml").write_text(\r
        '<?xml version="1.0" encoding="utf-8"?>' + NL +\r
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android">' + NL +\r
        '  <application android:supportsRtl="true"/>' + NL +\r
        '</manifest>', encoding="utf-8")\r
    (res / "layout" / "a.xml").write_text(\r
        '<?xml version="1.0" encoding="utf-8"?>\n'\r
        '<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"'\r
        ' xmlns:app="http://schemas.android.com/apk/res-auto">\n'\r
"""
assert s.count(old) == 1
s = s.replace(old, new)
io.open(p, "w", encoding="utf-8", newline="").write(s)
print("ok")

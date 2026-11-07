import io
p = "tests/test_aqlab.py"
s = io.open(p, encoding="utf-8", newline="").read()
def rep(old, new):
    global s
    assert s.count(old) == 1, (s.count(old), old[:60])
    s = s.replace(old, new)

# The bare ltr row is a leaf: its own pin is the mirrored-glyph case and stays
# silent. Make it a container so the row rule is what is measured here.
rep("""        \'  <TextView android:layoutDirection="ltr" android:text="a"/>\' + NL +\n""",
    """        \'  <LinearLayout android:layoutDirection="ltr">\' + NL +\n"""
    """        \'    <TextView android:text="a"/>\' + NL +\n"""
    """        \'  </LinearLayout>\' + NL +\n""")

rep('''    # Absent is what `aqlab rtl` is about to add, so the rows are still worth
    # naming. Only the two reference rows are claimed here: the `ltr` literal
    # sits on a bare `TextView`, a leaf, and a leaf is silent by the glyph rule
    # above.
    check("an absent supportsRtl still lets the rows be reported",
          [g[3] for g in A.rtl_enabled(root) if g[0] == "layoutDirection"]
          == ["@string/row_dir", "?attr/row_dir"],
          str(A.rtl_enabled(root)))''',
'''    # Absent is what `aqlab rtl` is about to add, so the rows are still worth
    # naming: none of them can follow a locale that was never switched on.
    check("an absent supportsRtl still lets the rows be reported",
          [g[3] for g in A.rtl_enabled(root) if g[0] == "layoutDirection"]
          == ["ltr", "@string/row_dir", "?attr/row_dir"],
          str(A.rtl_enabled(root)))''')
io.open(p, "w", encoding="utf-8", newline="").write(s)
print("ok")

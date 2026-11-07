import io
p = "tests/test_aqlab.py"
s = io.open(p, encoding="utf-8", newline="").read()
old = ('    check("an absent supportsRtl still lets the rows be reported",\r\n'
       '          len([g for g in A.rtl_enabled(root) if g[0] == "layoutDirection"]) == 3,\r\n'
       '          str(A.rtl_enabled(root)))\r\n')
new = ('    # Absent is what `aqlab rtl` is about to add, so the rows are still worth\r\n'
       '    # naming; the three that live on a `TextView` are order-free and show up\r\n'
       '    # under either reading of the gate.\r\n'
       '    check("an absent supportsRtl still lets the rows be reported",\r\n'
       '          [g[3] for g in A.rtl_enabled(root) if g[0] == "layoutDirection"]\r\n'
       '          == ["@string/row_dir", "?attr/row_dir"],\r\n'
       '          str(A.rtl_enabled(root)))\r\n')
assert s.count(old) == 1
s = s.replace(old, new)
io.open(p, "w", encoding="utf-8", newline="").write(s)
print("ok")

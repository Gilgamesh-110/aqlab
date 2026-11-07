import io
p = "tests/test_aqlab.py"
s = io.open(p, encoding="utf-8", newline="").read()
old = "    argv.dir = str(root)\r\n"
assert s.count(old) == 1
s = s.replace(old, "    argv.proj = str(root)\r\n    argv.dir = str(root)\r\n")
io.open(p, "w", encoding="utf-8", newline="").write(s)
print("ok")

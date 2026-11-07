import io
p = "tests/test_aqlab.py"
s = io.open(p, encoding="utf-8", newline="").read()
old = ("""  <TextView android:layoutDirection="ltr" android:text="a"/>""" + "\n")
assert s.count(old) == 1, s.count(old)
new = ("""  <TextView android:layoutDirection="ltr" android:text="a" android:id="@+id/a"/>""" + "\n")
s = s.replace(old, new)
io.open(p, "w", encoding="utf-8", newline="").write(s)
print("ok")

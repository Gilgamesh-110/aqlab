p='aqlab.py'
s=open(p,encoding='utf-8').read()
BS=chr(92)+'b'
bad="'+chr(8)+"(?:android:)?(?:gravity|layout_gravity|foregroundGravity|textAlignment)"+chr(92)+"s*="
new="'("+BS+"(?:android:)?(?:gravity|layout_gravity|foregroundGravity|textAlignment)"+chr(92)+"s*="
assert bad in s, "pattern missing"
s=s.replace(bad,new)
open(p,'w',encoding='utf-8').write(s)

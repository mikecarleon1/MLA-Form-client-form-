"""One-off helper: parse Google Form FB_PUBLIC_LOAD_DATA for entry IDs."""
import ast
import re
import urllib.request

u = "https://docs.google.com/forms/d/e/1FAIpQLSePxpEhm3MDlMydeTCYN-sYCSVy6ukey508rxFIsy70EybAzA/viewform"
c = urllib.request.urlopen(u, timeout=30).read().decode("utf-8", "replace")
m = re.search(r"FB_PUBLIC_LOAD_DATA_\s*=\s*(\[.*?\]);?\s*</script>", c, re.DOTALL)
if not m:
    raise SystemExit("Could not find FB_PUBLIC_LOAD_DATA")
raw = m.group(1)
# ast.literal_eval may fail on null/true - replace
raw_js = raw.replace("null", "None").replace("true", "True").replace("false", "False")
try:
    data = ast.literal_eval(raw_js)
except Exception as e:
    print("literal_eval failed", e)
    print(raw[:3000])
    raise

# data[1][1] is list of field defs
fields = data[1][1]
for i, f in enumerate(fields):
    if not isinstance(f, list) or len(f) < 2:
        continue
    title = f[1] if isinstance(f[1], str) else "?"
    inner = f[4] if len(f) > 4 else None
    entry = None
    if isinstance(inner, list) and len(inner) and isinstance(inner[0], list):
        inner0 = inner[0]
        if len(inner0) >= 1 and isinstance(inner0[0], int):
            entry = inner0[0]
    print(i, repr(title), "entry", entry)

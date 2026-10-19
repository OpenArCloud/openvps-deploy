#!/usr/bin/env python3
"""Drive the OAuth authorization-code flow to completion and leave a session cookie jar."""
import http.cookiejar, urllib.request, urllib.parse, re, html, json, sys

def cfg(path, key):
    for line in open(path):
        if line.startswith(key + "="): return line.split("=", 1)[1].strip()
    return ""

BUILDER = cfg("/opt/openvps/docker.env", "MAPBUILDER_URL")
ISSUER  = cfg("/opt/openvps/docker.env", "AUTH_FUSIONAUTH_ISSUER")
EMAIL   = cfg("/opt/openvps/fusionauth.env", "FUSIONAUTH_ADMIN_EMAIL")
PASS    = cfg("/opt/openvps/fusionauth.env", "FUSIONAUTH_ADMIN_PASSWORD")

jar = http.cookiejar.MozillaCookieJar("/tmp/py-cookies.txt")
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
follow = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
stop   = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect())

def req(url, fields=None, f=True):
    data = urllib.parse.urlencode(fields).encode() if fields is not None else None
    r = urllib.request.Request(url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"} if data else {})
    try:
        h = (follow if f else stop).open(r, timeout=60)
        return h.getcode(), h.read().decode("utf8","replace"), dict(h.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf8","replace"), dict(e.headers)

def loc(h, base=""):
    for k, v in h.items():
        if k.lower() == "location":
            return urllib.parse.urljoin(base, v) if base else v
    return ""

def forms(body):
    out = []
    for m in re.finditer(r"<form[^>]*>.*?</form>", body, re.S):
        b = m.group(0)
        a = re.search(r'action="([^"]*)"', b)
        fields = {}
        for i in re.finditer(r"<input[^>]*>", b):
            t = i.group(0)
            n = re.search(r'name="([^"]+)"', t); v = re.search(r'value="([^"]*)"', t)
            if n: fields[html.unescape(n.group(1))] = html.unescape(v.group(1) if v else "")
        out.append((html.unescape(a.group(1)) if a else "", fields))
    return out

code, body, _ = req(f"{BUILDER}/maps")
print(f"  /maps without a session -> HTTP {code}")

csrf = json.loads(req(f"{BUILDER}/auth/csrf")[1])["csrfToken"]
code, body, h = req(f"{BUILDER}/auth/signin/fusionauth",
                    {"csrfToken": csrf, "callbackUrl": BUILDER + "/"}, f=False)
target = loc(h, f"{BUILDER}/auth/signin/fusionauth")
print(f"  signin -> {code}")

for step in range(1, 8):
    if not target: print("  no further redirect"); break
    if "/auth/callback/" in target:
        code, body, h = req(target)
        print(f"  step {step}: app callback -> HTTP {code}")
        break
    code, body, h = req(target)
    fs = forms(body)
    kind = "consent" if "/consent" in target else "login"
    print(f"  step {step}: {kind} page HTTP {code}, {len(fs)} form(s)")
    if not fs:
        print("    no form:", re.sub(r"<[^>]*>", " ", body)[:160].strip()); break
    action, fields = fs[0]
    if kind == "login":
        fields["loginId"] = EMAIL; fields["password"] = PASS
    au = action if action.startswith("http") else urllib.parse.urljoin(ISSUER, action)
    code, body, h = req(au, fields, f=False)
    target = loc(h, au)
    print(f"    posted {len(fields)} fields -> {code}")

print(f"  /auth/session -> {req(f'{BUILDER}/auth/session')[1][:180]}")
jar.save(ignore_discard=True, ignore_expires=True)
code, body, _ = req(f"{BUILDER}/maps")
print(f"  /maps with a session -> HTTP {code}: {body[:120]}")
sys.exit(0 if code == 200 else 1)

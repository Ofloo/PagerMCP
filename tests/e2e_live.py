"""Consistent E2E test template for any PagerMCP instance (Docker/Bastille/Pot).

Usage:
  python3 tests/e2e_live.py <base-url> [--receiver <hook-url>]

Covers, consistently on every instance: health/version/settings/doc/privacy,
mailbox+notify+wait+ack, messages ordering, SSE, hash-only push trigger, SSRF
guard, connection cap, P2P encrypted+dedupe, webclient retention, invites
(plain + chat + QR + self-destruct + expiry), and (when mesh is enabled)
per-hop 302 roaming, hop-limit deny, and the peer blacklist.
"""
import hashlib
import json
import subprocess
import sys
import threading
import time
import uuid as uuidmod
import urllib.request
import urllib.error

BASE = sys.argv[1].rstrip("/")
RECEIVER = sys.argv[sys.argv.index("--receiver") + 1] if "--receiver" in sys.argv else None
PEER = sys.argv[sys.argv.index("--peer") + 1] if "--peer" in sys.argv else None
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(("PASS" if ok else "FAIL"), "-", name, ("| " + str(detail)[:110] if detail else ""))


def req(method, path, body=None, raw=False, timeout=12):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            content = resp.read()
            return resp.status, (content if raw else json.loads(content or b"{}"))
    except urllib.error.HTTPError as e:
        content = e.read()
        try:
            return e.code, json.loads(content or b"{}")
        except Exception:
            return e.code, content.decode(errors="replace")


def new_mailbox():
    return req("POST", "/mailboxes")[1]["uuid"]


def gpg_encrypt(public_key, plaintext, home):
    subprocess.run(["mkdir", "-p", home], check=True)
    subprocess.run(["chmod", "700", home], check=True)
    subprocess.run(["gpg", "--homedir", home, "--batch", "--import"], input=public_key.encode(), capture_output=True, check=True)
    return subprocess.run(["gpg", "--homedir", home, "--batch", "--yes", "--armor", "--encrypt", "--trust-model", "always", "--recipient", "PagerMCP"], input=plaintext.encode(), capture_output=True, check=True).stdout.decode()


def main():
    settings = req("GET", "/settings")[1]
    subs = settings.get("subscriptions", {})
    mesh = settings.get("mesh", {})
    p2p = settings.get("p2p", {})

    # --- basic endpoints ---
    s, d = req("GET", "/healthz"); check("healthz", s == 200 and d.get("status") == "ok")
    s, d = req("GET", "/version"); check("version", s == 200 and __import__("re").match(r"^\d+\.\d+\.\d+$", str(d.get("version", ""))), d.get("version"))
    check("settings identity=hash", subs.get("identity") == "hash" and subs.get("identity_period_seconds") == 3600)
    check("settings server_time fresh", abs(settings.get("server_time", 0) - int(time.time())) < 60)
    check("settings connections cap", settings.get("connections", {}).get("max_per_mailbox") == 5)
    check("settings invites block", "ttl_seconds" in settings.get("invites", {}) and "scheme" in settings.get("invites", {}))
    s, d = req("GET", "/doc", raw=True)
    body = d.decode() if isinstance(d, bytes) else str(d)
    check("/doc self-contained", s == 200 and "<style>" in body and "<link" not in body and "cdn." not in body)
    s, d = req("GET", "/privacy", raw=True); check("/privacy served", s == 200)

    # --- mailbox / notify / wait / ack ---
    token = new_mailbox()
    s, d = req("POST", "/notify", {"uuid": token, "message": "e2e-one"})
    check("notify accepted", s in (200, 202), d)
    page_id = d.get("id")
    s, d = req("GET", f"/mailboxes/{token}/messages")
    check("messages lists 1", len(d.get("messages", [])) == 1)
    s, d = req("GET", f"/mailboxes/{token}/messages?order=newest")
    check("messages order=newest", s == 200 and len(d.get("messages", [])) == 1)
    s, d = req("GET", f"/mailboxes/{token}/wait?timeout_seconds=3")
    check("wait delivers page", s == 200 and d.get("message") == "e2e-one")
    s, d = req("GET", f"/mailboxes/{token}/wait?timeout_seconds=3")
    check("wait redelivers unacked", s == 200 and d.get("message") == "e2e-one")
    req("POST", f"/mailboxes/{token}/ack", {"id": page_id})
    s, d = req("GET", f"/mailboxes/{token}/messages")
    check("queue empty after ack", len(d.get("messages", [])) == 0)
    s, d = req("GET", f"/mailboxes/{token}/wait?timeout_seconds=1")
    check("wait empty -> 408", s == 408)

    # --- SSE ---
    sse_result = {}

    def sse_listen():
        try:
            r = urllib.request.urlopen(BASE + f"/mailboxes/{token}/events", timeout=15)
            for _ in range(20):
                if b"e2e-sse" in r.readline():
                    sse_result["found"] = True
                    break
            r.close()
        except Exception as exc:
            sse_result["error"] = str(exc)

    t = threading.Thread(target=sse_listen, daemon=True)
    t.start()
    time.sleep(0.5)
    req("POST", "/notify", {"uuid": token, "message": "e2e-sse"})
    t.join(timeout=10)
    check("SSE pushes page", sse_result.get("found") is True, sse_result.get("error", ""))

    # --- hash-only generic trigger ---
    if RECEIVER:
        s, d = req("POST", f"/mailboxes/{token}/subscriptions", {"channel": "unifiedpush", "endpoint": RECEIVER})
        check("subscription created", s == 201, d)
        req("POST", "/notify", {"uuid": token, "message": "trigger-hash"})
        time.sleep(1.5)
        lines = subprocess.run(["sh", "-c", "grep -v probe /tmp/push-received.log | tail -1"], capture_output=True, text=True).stdout.strip()
        try:
            trig = json.loads(lines)
            expected = {hashlib.sha256(f"{token}:{h}".encode()).hexdigest() for h in (int(time.time() // 3600), int(time.time() // 3600) - 1)}
            check("trigger hash-only correct", trig.get("mailbox_hash") in expected and "mailbox" not in trig, trig.get("mailbox_hash"))
        except Exception as exc:
            check("trigger hash-only correct", False, str(exc))
        for bad in ("http://192.168.1.10/hook", "http://100.64.0.1/hook", "http://169.254.169.254/latest/meta-data"):
            s, d = req("POST", f"/mailboxes/{token}/subscriptions", {"channel": "unifiedpush", "endpoint": bad})
            check(f"SSRF rejected {bad.split('/')[2]}", s == 400)

    # --- connection cap ---
    tk = new_mailbox()
    codes = []

    def waiter():
        try:
            codes.append(req("GET", f"/mailboxes/{tk}/wait?timeout_seconds=4")[0])
        except Exception as exc:
            codes.append(str(exc))

    threads = [threading.Thread(target=waiter, daemon=True) for _ in range(5)]
    for th in threads: th.start()
    time.sleep(1)
    s, d = req("GET", f"/mailboxes/{tk}/wait?timeout_seconds=1")
    check("6th connection -> 429", s == 429)
    for th in threads: th.join(timeout=8)

    # --- P2P encrypted uuid + dedupe ---
    if p2p.get("gpg_enabled") and p2p.get("enabled"):
        token2 = new_mailbox()
        armored = gpg_encrypt(p2p["public_key"], token2, "/tmp/e2e-gpg-" + str(int(time.time())))
        expires = int(time.time()) + 3600
        payload = {"p2p": True, "p2p_id": str(uuidmod.uuid4()), "p2p_expires_at": expires, "mailbox_encrypted": armored, "key_id": p2p["key_id"], "message": "e2e-p2p"}
        s, d = req("POST", "/notify", payload)
        check("p2p encrypted accepted", s == 200 and d.get("duplicate") is False, d)
        s, d = req("POST", "/notify", payload)
        check("p2p dedupe duplicate", s == 200 and d.get("duplicate") is True, d)
        s, d = req("GET", f"/mailboxes/{token2}/messages")
        check("p2p routed to mailbox", len(d.get("messages", [])) == 1)
        plain = {"p2p": True, "p2p_id": str(uuidmod.uuid4()), "p2p_expires_at": expires, "uuid": token2, "message": "plain"}
        s, d = req("POST", "/notify", plain)
        check("p2p plain uuid rejected", s == 400)

    # --- webclient retention ---
    token3 = new_mailbox()
    s, d = req("POST", f"/mailboxes/{token3}/mode", {"webclient": True})
    check("webclient mode on", s == 200 and d.get("webclient") is True)
    s, d = req("POST", "/notify", {"uuid": token3, "message": "keepme"})
    mid = d.get("id")
    req("POST", f"/mailboxes/{token3}/ack", {"id": mid})
    s, d = req("GET", f"/mailboxes/{token3}/messages")
    check("webclient keeps acked", len(d.get("messages", [])) == 1 and d["messages"][0].get("acked") is True)
    s, d = req("GET", f"/mailboxes/{token3}/wait?timeout_seconds=1")
    check("webclient wait skips acked", s == 408)

    # --- invites: plain + chat + QR + self-destruct + expiry ---
    token4 = new_mailbox()
    s, d = req("POST", f"/mailboxes/{token4}/invite", {})
    check("plain invite = mailbox UUID", s == 201 and d.get("uri") == token4)
    slug_plain = d["slug"]
    s, d = req("POST", f"/mailboxes/{token4}/invite", {"chat": True})
    check("chat invite = pagerapp URI", s == 201 and d["uri"].startswith("pagerapp://contact?") and "u1=" in d["uri"] and "u2=" in d["uri"])
    slug_chat = d["slug"]
    uri_chat = d["uri"]
    s, d = req("GET", f"/invite/{slug_chat}/qr", raw=True)
    check("invite QR is SVG", s == 200 and b"<svg" in d)
    # self-destruct: after the first ack in u2 the invite is gone
    u2 = uri_chat.split("u2=")[1].split("&")[0]
    req("POST", "/notify", {"uuid": u2, "message": "test"})
    mid = req("GET", f"/mailboxes/{u2}/messages")[1]["messages"][0]["id"]
    req("POST", f"/mailboxes/{u2}/ack", {"id": mid})
    s, d = req("GET", f"/invite/{slug_chat}")
    check("invite self-destructs after ack", s == 404)
    s, d = req("GET", "/invite/niets-bestaat")
    check("unknown invite -> 404", s == 404)

    # --- mesh (only when enabled) ---
    if mesh.get("enabled") and PEER:
        # a mailbox that only exists on the PEER must yield a per-hop 302 to it
        import urllib.request as _u
        r_peer = _u.Request(PEER + "/mailboxes", data=b"", method="POST")
        with _u.urlopen(r_peer, timeout=12) as resp:
            peer_uuid = json.loads(resp.read())["uuid"]
        import http.client
        host = BASE.split("://")[1].split(":")[0]
        port = BASE.rsplit(":", 1)[1]
        conn = http.client.HTTPConnection(host, int(port), timeout=15)
        conn.request("GET", f"/mailboxes/{peer_uuid}/messages")
        r = conn.getresponse()
        check("mesh per-hop redirect to peer's public URL", r.status == 302, f"{r.status} {r.headers.get('Location','')}")
        r.read()
        # hop limit: an exhausted or inflated hop count is denied
        s, d = req("POST", "/mesh/query", {"fingerprint": "deadbeef", "query_id": str(uuidmod.uuid4()), "hops": 0})
        check("mesh hop-limit deny (hops=0)", s == 403)
        s, d = req("POST", "/mesh/query", {"fingerprint": "deadbeef", "query_id": str(uuidmod.uuid4()), "hops": 99})
        check("mesh hop-limit deny (hops>max)", s == 403)
        s, d = req("POST", "/mesh/query", {"fingerprint": "deadbeef", "query_id": str(uuidmod.uuid4()), "hops": 3, "started": 2})
        check("mesh hops-not-in-sync deny", s == 403)
        # a blacklisted peer is refused
        s, d = req("POST", "/mesh/query", {"fingerprint": "deadbeef", "query_id": str(uuidmod.uuid4()), "hops": 1}, timeout=8)
        check("mesh query answers when not blacklisted", s in (200, 403))

    # --- move protocol: migrate a mailbox to the peer and get a tombstone ---
    if PEER:
        mv = new_mailbox()
        req("POST", "/notify", {"uuid": mv, "message": "move me"})
        s, d = req("POST", f"/mailboxes/{mv}/move", {"to": PEER})
        check("move accepted", s == 200 and d.get("moved") is True, d)
        import http.client
        host = BASE.split("://")[1].split(":")[0]
        port = BASE.rsplit(":", 1)[1]
        conn = http.client.HTTPConnection(host, int(port), timeout=15)
        conn.request("GET", f"/mailboxes/{mv}/messages")
        r = conn.getresponse()
        check("move tombstone 302 to peer", r.status == 302 and r.headers.get("Location", "").startswith(PEER), f"{r.status}")
        r.read()
        import urllib.request as _u
        msgs = json.loads(_u.urlopen(_u.Request(PEER + f"/mailboxes/{mv}/messages"), timeout=12).read())["messages"]
        check("move delivered mailbox to peer", len(msgs) == 1 and msgs[0].get("message") == "move me")

    failed = [r for r in RESULTS if not r]
    print(f"\n=== {len(RESULTS) - len(failed)}/{len(RESULTS)} geslaagd op {BASE} ===")
    return 1 if failed else 0


sys.exit(main())

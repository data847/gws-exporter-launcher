"""GWS Exporter web page. Started by launcher.py (run-ui.command / run-ui.bat); not meant to be run by hand.

A small local web server (127.0.0.1 only, standard library) that builds the run_org_classify.py command from a form, runs it one user at a time
(so it uses --user, which needs only the Drive + Gmail scopes, never --only), shows the live log, and lists what was written.
Nothing leaves this computer except what the exporter itself does (Google APIs, plus S3 / Hetzner if you choose them).
"""
import http.server
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser

HERE = os.environ.get("GWS_UI_HERE") or os.path.dirname(os.path.abspath(__file__))
CODE = os.environ["GWS_UI_CODE"]
OUT = os.environ["GWS_UI_OUT"]
PY = sys.executable
SA = os.path.join(HERE, "service_account.json")
TOKEN = secrets.token_urlsafe(24)
EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LOCK = threading.Lock()
RUN = {"active": False, "proc": None, "stop": False, "log": [], "started": 0, "plan": [], "current": "", "results": []}


def log(line):
    with LOCK:
        RUN["log"].append(line.rstrip("\n"))
        del RUN["log"][:-5000]


def env_status():
    e = os.environ
    return {
        "service_account": os.path.isfile(SA) or bool(e.get("GOOGLE_SERVICE_ACCOUNT_FILE")),
        "anthropic": bool(e.get("ANTHROPIC_API_KEY") or e.get("OPENAI_API_KEY")),
        "aws": bool(e.get("AWS_ACCESS_KEY_ID") or e.get("AWS_PROFILE")),
        "hetzner": all(e.get(k) for k in ("SFTP_HOST", "SFTP_USERNAME", "SFTP_PASSWORD")),
    }


def build_commands(f):
    """-> (list of (label, argv), error). One command per user; org-wide modes get a single command."""
    admin = (f.get("admin") or "").strip()
    if not EMAIL.match(admin):
        return None, "Enter the admin email (the delegated admin of the Workspace)."
    users = [u for u in re.split(r"[\s,;]+", f.get("users") or "") if u]
    bad = [u for u in users if not EMAIL.match(u)]
    if bad:
        return None, f"Not an email address: {bad[0]}"
    mode, dest = f.get("mode", "export"), f.get("dest", "local")
    st = env_status()
    if not st["service_account"]:
        return None, "Upload the service-account key first (section 1)."
    base = [PY, os.path.join(CODE, "run_org_classify.py"), "--admin", admin, "--out", OUT]
    if os.path.isfile(SA) and not os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE"):
        base += ["--sa-file", SA]
    for key, flag in (("after", "--modified-after"), ("before", "--modified-before")):
        v = (f.get(key) or "").strip()
        if v:
            if not DATE.match(v):
                return None, f"Dates must look like 2026-04-01 ({key})."
            base += [flag, v]
    org_wide = {"count": "--count-files-only", "size": "--size-only", "dry": "--dry-run"}
    if mode in org_wide:
        return [("org", base + [org_wide[mode]])], None
    if mode == "classify" and not st["anthropic"]:
        return None, "Classify needs ANTHROPIC_API_KEY (or OPENAI_API_KEY) in .env."
    if mode == "export":
        base.append("--export-only")
    if f.get("no_gmail"):
        base.append("--no-gmail")
    if dest == "s3":
        bucket = (f.get("s3_bucket") or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]", bucket):
            return None, "Enter a valid S3 bucket name."
        base += ["--s3-bucket", bucket]
        if (f.get("s3_prefix") or "").strip():
            base += ["--s3-prefix", f["s3_prefix"].strip()]
    elif dest == "hetzner":
        if not st["hetzner"]:
            return None, "Hetzner needs SFTP_HOST, SFTP_USERNAME and SFTP_PASSWORD in .env or operator.env."
        base.append("--hetzner")
    else:
        base.append("--local-only")
    if users:
        return [(u, base + ["--user", u]) for u in users], None
    if not f.get("whole_org"):
        return None, "Enter at least one user, or tick 'every user in the org'."
    return [("org", base)], None


def worker(cmds):
    with LOCK:
        RUN.update(active=True, stop=False, started=time.time(), plan=[c[0] for c in cmds], results=[], current="")
        RUN["log"] = []
    for label, argv in cmds:
        if RUN["stop"]:
            break
        with LOCK:
            RUN["current"] = label
        log(f"\n=== {label}: {' '.join(a if a != SA else 'service_account.json' for a in argv[1:])}\n")
        try:
            p = subprocess.Popen(argv, cwd=HERE, env=dict(os.environ, PYTHONUNBUFFERED="1"), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
            with LOCK:
                RUN["proc"] = p
            for line in p.stdout:
                log(line)
            rc = p.wait()
        except Exception as e:
            rc = -1
            log(f"could not start: {e}")
        with LOCK:
            RUN["results"].append({"label": label, "rc": rc})
        log(f"=== {label}: {'finished' if rc == 0 else 'stopped' if RUN['stop'] else f'failed (exit {rc})'}")
    with LOCK:
        RUN.update(active=False, proc=None, current="")
    log("\nAll done. Run the same form again to resume anything that stopped.")


def outputs():
    rows = []
    try:
        for n in sorted(os.listdir(OUT)):
            p = os.path.join(OUT, n)
            if os.path.isdir(p):
                size = files = 0
                for r, _, fs in os.walk(p):
                    for x in fs:
                        files += 1
                        try:
                            size += os.path.getsize(os.path.join(r, x))
                        except OSError:
                            pass
                rows.append({"name": n, "files": files, "bytes": size})
            else:
                rows.append({"name": n, "files": 1, "bytes": os.path.getsize(p)})
    except OSError:
        pass
    return rows


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _ok_host(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._ok_host():
            return self._send(403, {"error": "bad host"})
        if self.path == "/":
            return self._send(200, PAGE.replace("__TOKEN__", TOKEN), "text/html")
        if self.path.startswith("/api/state"):
            m = re.search(r"since=(\d+)", self.path)
            since = int(m.group(1)) if m else 0
            with LOCK:
                lines = RUN["log"][since:]
                total = len(RUN["log"])
                state = {k: RUN[k] for k in ("active", "plan", "current", "results", "started")}
            state.update(lines=lines, total=total, env=env_status(), outputs=outputs(), out=OUT)
            return self._send(200, state)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._ok_host() or self.headers.get("X-Token") != TOKEN:
            return self._send(403, {"error": "forbidden"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > 200_000:
            return self._send(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        if self.path == "/api/start":
            if RUN["active"]:
                return self._send(409, {"error": "A run is already going."})
            cmds, err = build_commands(body)
            if err:
                return self._send(400, {"error": err})
            threading.Thread(target=worker, args=(cmds,), daemon=True).start()
            return self._send(200, {"ok": True, "commands": len(cmds)})
        if self.path == "/api/stop":
            with LOCK:
                RUN["stop"] = True
                p = RUN["proc"]
            if p and p.poll() is None:
                p.terminate()
            return self._send(200, {"ok": True})
        if self.path == "/api/sa":
            try:
                d = json.loads(body.get("json") or "")
                assert d.get("type") == "service_account" and d.get("client_email") and d.get("private_key") and d.get("client_id")
            except Exception:
                return self._send(400, {"error": "That is not a service-account key file (needs type, client_email, client_id, private_key)."})
            tmp = SA + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(d, f)
            os.replace(tmp, SA)
            return self._send(200, {"ok": True, "client_email": d["client_email"], "client_id": d["client_id"]})
        self._send(404, {"error": "not found"})


def free_port(start=8770):
    for p in range(start, start + 30):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    raise SystemExit("no free port near 8770")


def main():
    os.makedirs(OUT, exist_ok=True)
    port = int(os.environ.get("GWS_UI_PORT") or free_port())
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    url = f"http://127.0.0.1:{port}/"
    print(f"GWS Exporter is open at {url}  (Ctrl+C here to quit)", flush=True)
    if not os.environ.get("GWS_UI_NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    p = RUN["proc"]
    if p and p.poll() is None:
        p.terminate()


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GWS Exporter</title><style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1b1f24;--mute:#667085;--line:#d9dde3;--acc:#2457d6;--ok:#17803d;--bad:#b42318}
@media(prefers-color-scheme:dark){:root{--bg:#14171b;--card:#1d2127;--ink:#e8eaed;--mute:#98a2b3;--line:#323840;--acc:#6b9bff;--ok:#4ade80;--bad:#f87171}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:20px 16px 60px}h1{font-size:22px;margin:4px 0 2px}.sub{color:var(--mute);margin:0 0 18px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:0 0 14px}
h2{font-size:15px;margin:0 0 10px}label{display:block;font-weight:600;margin:10px 0 4px}label.i{font-weight:400;display:flex;gap:8px;align-items:center;margin:6px 0}
input[type=text],input[type=email],textarea,select{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);font:inherit}
textarea{min-height:72px;font-family:ui-monospace,monospace;font-size:13px}.row{display:flex;gap:12px;flex-wrap:wrap}.row>div{flex:1;min-width:180px}
button{font:inherit;padding:9px 16px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
button.p{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}button:disabled{opacity:.5;cursor:default}
.hint{color:var(--mute);font-size:13px;margin:4px 0}.pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;border:1px solid var(--line)}
.ok{color:var(--ok)}.bad{color:var(--bad)}#log{background:#0e1114;color:#d6dbe0;border-radius:8px;padding:10px;height:320px;overflow:auto;white-space:pre-wrap;font:12px/1.4 ui-monospace,monospace}
#err{color:var(--bad);margin:8px 0;min-height:1em}table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;padding:5px 6px;border-bottom:1px solid var(--line)}
.hide{display:none}code{font-family:ui-monospace,monospace;font-size:13px}
</style></head><body><main>
<h1>GWS Exporter</h1><p class="sub">Export Drive files and Gmail for Google Workspace users. Runs on this computer.</p>

<section><h2>1. Service-account key <span id="sa" class="pill"></span></h2>
<p class="hint">The JSON key of a service account with domain-wide delegation (scopes: drive.readonly, gmail.readonly). Saved next to the launcher, permission 600.</p>
<input type="file" id="safile" accept=".json,application/json"> <span id="samsg" class="hint"></span></section>

<section><h2>2. What to run</h2>
<div class="row"><div><label>Admin email</label><input type="email" id="admin" placeholder="admin@domain.com"></div>
<div><label>Mode</label><select id="mode">
<option value="export">Export Drive + Gmail (no AI)</option><option value="classify">Classify with AI + export</option>
<option value="count">Count files (Drive API)</option><option value="size">Total storage size</option><option value="dry">List users (dry run)</option></select></div></div>
<div id="usersbox"><label>Users (one per line or comma separated)</label><textarea id="users" placeholder="person@domain.com"></textarea>
<label class="i"><input type="checkbox" id="whole"> or every user in the org (needs the admin to have Admin SDK scopes granted)</label></div>
<div class="row"><div><label>Modified after (optional)</label><input type="text" id="after" placeholder="2026-04-01"></div>
<div><label>Modified before (optional, exclusive)</label><input type="text" id="before" placeholder="2026-07-01"></div></div></section>

<section id="destsec"><h2>3. Where to put it</h2>
<div class="row"><div><select id="dest"><option value="local">This computer only</option><option value="s3">Upload to S3, then delete local</option><option value="hetzner">Upload to Hetzner, then delete local</option></select></div>
<div id="s3box" class="hide"><input type="text" id="bucket" placeholder="bucket-name"></div><div id="s3p" class="hide"><input type="text" id="prefix" placeholder="key prefix (optional)"></div></div>
<label class="i"><input type="checkbox" id="nogmail"> Skip Gmail (Drive only)</label>
<p class="hint" id="destnote"></p></section>

<section><button class="p" id="go">Start</button> <button id="stop" disabled>Stop</button> <span id="status" class="hint"></span><div id="err"></div>
<div id="log"></div></section>

<section><h2>Output</h2><p class="hint">Folder: <code id="out"></code></p><table id="outs"></table></section>
</main><script>
const T="__TOKEN__",$=id=>document.getElementById(id);let since=0,active=false;
async function post(p,b){const r=await fetch(p,{method:"POST",headers:{"X-Token":T,"Content-Type":"application/json"},body:JSON.stringify(b||{})});const j=await r.json();if(!r.ok)throw new Error(j.error||r.status);return j}
function fmt(n){const u=["B","KB","MB","GB","TB"];let i=0;while(n>=1024&&i<4){n/=1024;i++}return n.toFixed(i?1:0)+" "+u[i]}
function layout(){const m=$("mode").value,exp=m=="export"||m=="classify";$("usersbox").classList.toggle("hide",!exp);$("destsec").classList.toggle("hide",!exp);
const d=$("dest").value;$("s3box").classList.toggle("hide",d!="s3");$("s3p").classList.toggle("hide",d!="s3");}
["mode","dest"].forEach(i=>$(i).onchange=layout);layout();
$("safile").onchange=async e=>{const f=e.target.files[0];if(!f)return;try{const j=await post("/api/sa",{json:await f.text()});$("samsg").textContent="Saved: "+j.client_email+" (client ID "+j.client_id+")"}catch(x){$("samsg").textContent=x.message}};
$("go").onclick=async()=>{$("err").textContent="";try{since=0;$("log").textContent="";await post("/api/start",{admin:$("admin").value,mode:$("mode").value,users:$("users").value,whole_org:$("whole").checked,
after:$("after").value,before:$("before").value,dest:$("dest").value,s3_bucket:$("bucket").value,s3_prefix:$("prefix").value,no_gmail:$("nogmail").checked})}catch(x){$("err").textContent=x.message}};
$("stop").onclick=()=>post("/api/stop").catch(()=>{});
async function tick(){try{const r=await fetch("/api/state?since="+since),s=await r.json();active=s.active;
if(s.lines.length){const l=$("log"),bot=l.scrollTop+l.clientHeight>=l.scrollHeight-20;l.textContent+=s.lines.join("\n")+"\n";if(bot)l.scrollTop=l.scrollHeight}since=s.total;
$("go").disabled=active;$("stop").disabled=!active;
const done=s.results.length,tot=s.plan.length;$("status").textContent=active?("Running "+s.current+" ("+done+"/"+tot+")"):(tot?(s.results.filter(x=>x.rc==0).length+" of "+tot+" finished"):"");
const e=s.env;$("sa").textContent=e.service_account?"present":"missing";$("sa").className="pill "+(e.service_account?"ok":"bad");
$("destnote").textContent="Keys in .env: AI "+(e.anthropic?"yes":"no")+", AWS "+(e.aws?"yes":"no")+", Hetzner "+(e.hetzner?"yes":"no")+". Re-running the same form resumes users already done.";
$("out").textContent=s.out;$("outs").innerHTML="<tr><th>Name</th><th>Files</th><th>Size</th></tr>"+(s.outputs.map(o=>"<tr><td>"+o.name.replace(/[<&]/g,"")+"</td><td>"+o.files+"</td><td>"+fmt(o.bytes)+"</td></tr>").join("")||"<tr><td colspan=3 class=hint>Nothing yet.</td></tr>")}catch(x){}
setTimeout(tick,1000)}tick();
</script></body></html>"""

if __name__ == "__main__":
    main()

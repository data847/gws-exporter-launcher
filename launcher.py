#!/usr/bin/env python3
"""GWS Exporter starter. Fetches the program from its public GitHub repository, builds a private Python environment for it, then runs it. Open this file in a text editor to read everything it does.

    launcher.py ui        the web page (what run-ui.command / run-ui.bat start)
    launcher.py cli ...   run the exporter (what run.command / run.bat start); extra words go to run_org_classify.py
    launcher.py prepare   only fetch / update the program and build the environment
    launcher.py build-id  print which version is installed, without touching the network

What it does, in order:
  1. Asks GitHub which version is the latest (github.com/anujchahar-lh2/gws-workspace-exporter). Nothing about you or your data is sent.
  2. If that is not the version already in the "app" folder next to this file, downloads it and replaces "app" with it.
  3. If GitHub cannot be reached it uses the version already in "app". The first run needs internet; later runs do not.
  4. Creates ".venv" next to this file and installs the program's requirements into it (again only when they change).
  5. Loads ".env" and "operator.env" from next to this file, uses "service_account.json" from next to this file unless --sa-file is given,
     and runs the program. Results go to the "output" folder next to this file unless --out is given.
To stay on one exact version, put its tag or commit on the first line of ref.txt next to this file (default: main).
Delete "app" or ".venv" at any time to start clean. Only the Python standard library is used here; no git needed.
"""
import hashlib
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

REPO = os.environ.get("GWS_EXPORTER_REPO") or "anujchahar-lh2/gws-workspace-exporter"
API = os.environ.get("GWS_EXPORTER_API") or "https://api.github.com"                # the overrides exist for tests only
ARCHIVE = os.environ.get("GWS_EXPORTER_ARCHIVE") or "https://codeload.github.com"
HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "app")
CODE = os.path.join(APP, "current")
VENV = os.path.join(HERE, ".venv")
STAMP = ".build.json"
ENTRY = "run_org_classify.py"
MAX_FILES, MAX_BYTES = 5000, 300 * 1024 * 1024
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# pandas 2.1.0 (pinned by the repo) has no wheels for 3.12+
PY_CANDIDATES = ["python3.11", "python3.10", "python3.9", "python3", "python"]
# imported by the repo but missing from its requirements.txt
EXTRA_REQUIREMENTS = ["boto3", "paramiko", "anthropic", "openai", "tiktoken", "openpyxl", "python-docx", "python-pptx", "pypdf", "xlrd"]
_quiet = False


def say(msg):
    print(msg, file=sys.stderr if _quiet else sys.stdout, flush=True)


def wanted_ref():
    r = os.environ.get("GWS_EXPORTER_REF", "").strip()
    if not r:
        try:
            with open(os.path.join(HERE, "ref.txt"), encoding="utf-8") as f:
                r = (f.readline() or "").strip()
        except OSError:
            r = ""
    return r if re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", r or "") and ".." not in r else "main"


def get(url, headers=None, timeout=30):
    """-> bytes. urllib first; on a certificate failure (python.org Python on a Mac without its certificates) curl, which uses the system's."""
    req = urllib.request.Request(url, headers=dict({"User-Agent": "gws-exporter-launcher"}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except (ssl.SSLError, urllib.error.URLError) as e:
        why = getattr(e, "reason", e)
        if isinstance(why, ssl.SSLError) or "CERTIFICATE" in str(e).upper():
            curl = shutil.which("curl")
            if curl:
                cmd = [curl, "-fsSL", "--max-time", str(timeout), "-A", "gws-exporter-launcher"]
                for k, v in (headers or {}).items():
                    cmd += ["-H", f"{k}: {v}"]
                p = subprocess.run(cmd + [url], capture_output=True)
                if p.returncode == 0:
                    return p.stdout
        raise


def latest_sha(ref):
    body = get(f"{API}/repos/{REPO}/commits/{ref}", {"Accept": "application/vnd.github.sha"}, 20).decode("ascii", "replace").strip()
    if not SHA_RE.match(body):
        raise ValueError("GitHub did not answer with a version id")
    return body


def installed():
    try:
        with open(os.path.join(CODE, STAMP), encoding="utf-8") as f:
            d = json.load(f)
        if os.path.isfile(os.path.join(CODE, ENTRY)):
            return d
    except (OSError, ValueError):
        pass
    return None


def unpack(data, dest):
    """Extract a GitHub zip (one top folder) into dest. Refuses names that could write outside dest, links, or an absurd size. -> commit id from the zip comment."""
    z = zipfile.ZipFile(io.BytesIO(data))
    infos = [i for i in z.infolist() if not i.filename.endswith("/")]
    if not 0 < len(infos) <= MAX_FILES or sum(i.file_size for i in infos) > MAX_BYTES:
        raise ValueError("the download is not the size of this program")
    if len({i.filename.split("/", 1)[0] for i in z.infolist()}) != 1:
        raise ValueError("unexpected layout in the download")
    base = os.path.realpath(dest)
    for i in infos:
        name = i.filename
        rel = name.split("/", 1)[1] if "/" in name else ""
        if not rel or name.startswith(("/", "\\")) or "\\" in name or ".." in name.split("/") or re.match(r"^[A-Za-z]:", name):
            raise ValueError(f"unsafe name in the download: {name!r}")
        if (i.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError(f"a link in the download: {name!r}")
        target = os.path.realpath(os.path.join(base, rel))
        if os.path.commonpath([base, target]) != base:
            raise ValueError(f"unsafe name in the download: {name!r}")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with z.open(i) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
    for must in (ENTRY, "requirements.txt"):
        if not os.path.isfile(os.path.join(base, must)):
            raise ValueError(f"the download has no {must}")
    return z.comment.decode("ascii", "replace").strip()


def install(data, sha_hint, ref):
    os.makedirs(APP, exist_ok=True)
    new = os.path.join(APP, f"new.{os.getpid()}")
    shutil.rmtree(new, ignore_errors=True)
    os.makedirs(new)
    try:
        sha = unpack(data, new)
        sha = sha if SHA_RE.match(sha) else (sha_hint or "")
        with open(os.path.join(new, STAMP), "w", encoding="utf-8") as f:
            json.dump({"sha": sha, "ref": ref, "fetched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, f)
        old = None
        if os.path.isdir(CODE):
            old = os.path.join(APP, f"old.{os.getpid()}")
            os.rename(CODE, old)
        try:
            os.rename(new, CODE)
        except OSError:
            if old:
                os.rename(old, CODE)
            raise
        if old:
            shutil.rmtree(old, ignore_errors=True)
        return sha
    finally:
        shutil.rmtree(new, ignore_errors=True)


def tidy():
    try:
        for n in os.listdir(APP):
            p = os.path.join(APP, n)
            if (n.startswith("old.") or n.startswith("new.")) and time.time() - os.path.getmtime(p) > 3600:
                shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def fetch_program():
    """-> folder of the program; downloads or updates it when a newer version exists, keeps what is there when GitHub cannot be reached."""
    ref = wanted_ref()
    have = installed()
    say(f"GWS Exporter - checking GitHub for the latest version ({REPO}, {ref}) ...")
    sha, problem = "", ""
    try:
        sha = latest_sha(ref)
    except Exception as e:
        problem = str(getattr(e, "reason", e)) or e.__class__.__name__
    if have and sha and have.get("sha") == sha and have.get("ref") == ref:
        say(f"Version {sha[:7]} is up to date.")
        return CODE
    if have and not sha:
        say(f"Could not reach GitHub ({problem}). Using the version already here: {have.get('sha', '')[:7] or 'unknown'}.")
        return CODE
    try:
        say(f"Downloading version {sha[:7] or ref} ...")
        data = get(f"{ARCHIVE}/{REPO}/zip/{sha or ref}", None, 120)
        got = install(data, sha, ref)
        say(f"Installed version {(got or sha or ref)[:7]} ({len(data) // 1024} KB).")
        tidy()
        return CODE
    except Exception as e:
        why = str(getattr(e, "reason", e)) or e.__class__.__name__
        if have and installed():
            say(f"Could not update ({why}). Using the version already here.")
            return CODE
        say(f"\nThe program could not be downloaded, and there is no copy here yet.\n  Reason: {why}")
        say("  The first start needs internet access to github.com. Check the connection, a VPN or proxy, and try again.")
        sys.exit(2)


def venv_python():
    return os.path.join(VENV, "Scripts" if os.name == "nt" else "bin", "python.exe" if os.name == "nt" else "python")


def find_uv():
    for cand in (shutil.which("uv"), os.path.join(HERE, "tools", "uv.exe"), os.path.join(HERE, "tools", "uv"),
                 os.path.join(os.path.dirname(HERE), ".tools", "uv")):
        if cand and os.path.isfile(cand):
            return cand
    return None


def pick_python():
    """First interpreter that is 3.9-3.11 (this program's pinned pandas has no wheels for 3.12+); None when there is none."""
    probe = "import sys;print(int((3,9)<=sys.version_info[:2]<=(3,11)))"
    if (3, 9) <= sys.version_info[:2] <= (3, 11):
        return sys.executable                       # e.g. started by run-ui.bat through uv's own Python 3.11
    for name in PY_CANDIDATES:
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            if subprocess.run([exe, "-c", probe], capture_output=True, text=True).stdout.strip() == "1":
                return exe
        except OSError:
            pass
    return None


def ensure_env(code):
    """Create .venv and install requirements.txt + the packages it forgot to list. Re-installs only when that list changes."""
    with open(os.path.join(code, "requirements.txt"), "rb") as f:
        req = f.read()
    want = hashlib.sha256(req + "\n".join(EXTRA_REQUIREMENTS).encode()).hexdigest()
    stamp = os.path.join(VENV, ".requirements.sha")
    try:
        with open(stamp, encoding="ascii") as f:
            if f.read().strip() == want and os.path.isfile(venv_python()):
                return
    except OSError:
        pass
    if not os.path.isfile(venv_python()):
        py = pick_python()
        shutil.rmtree(VENV, ignore_errors=True)
        if py:
            say(f"Creating the Python environment (.venv) with {py} ...")
            subprocess.run([py, "-m", "venv", VENV], check=True)
        elif find_uv():
            say("No Python 3.9 - 3.11 here; asking uv for Python 3.11 (one download) ...")
            subprocess.run([find_uv(), "venv", "--python", "3.11", "--seed", VENV], check=True)
        else:
            sys.exit("No Python 3.9 - 3.11 found (this program needs one). Install Python 3.11 from python.org, or install uv, and run this again.")
    say("Installing requirements (first run takes a few minutes) ...")
    vp = venv_python()
    subprocess.run([vp, "-m", "pip", "install", "--quiet", "--upgrade", "pip"], check=False)
    subprocess.run([vp, "-m", "pip", "install", "--quiet", "-r", os.path.join(code, "requirements.txt")], check=True)
    subprocess.run([vp, "-m", "pip", "install", "--quiet"] + EXTRA_REQUIREMENTS, check=True)
    with open(stamp, "w", encoding="ascii") as f:
        f.write(want)


def read_env_file(path):
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                if k and v:
                    out[k] = v
    except OSError:
        pass
    return out


def out_root():
    """./output next to this file; inside the DataLabs workspace, its single outputs root."""
    if os.environ.get("GWS_EXPORTER_OUT"):
        return os.path.abspath(os.environ["GWS_EXPORTER_OUT"])
    parent = os.path.dirname(HERE)
    if os.path.isfile(os.path.join(parent, "datalabs_paths.py")):
        sys.path.insert(0, parent)
        try:
            from datalabs_paths import outputs_for
            return str(outputs_for("gws-exporter"))
        except Exception:
            pass
    return os.path.join(HERE, "output")


def has_flag(args, name):
    return any(a == name or a.startswith(name + "=") for a in args)


def base_env(code):
    env = dict(os.environ)
    for name in (".env", "operator.env"):
        for k, v in read_env_file(os.path.join(HERE, name)).items():
            env.setdefault(k, v)
    env["GWS_EXPORTER_BUILD"] = (installed() or {}).get("sha", "")[:12]
    env["PYTHONUNBUFFERED"] = "1"
    return env


def run(args):
    code = fetch_program()
    ensure_env(code)
    env = base_env(code)
    args = list(args)
    if not has_flag(args, "--out"):
        args += ["--out", out_root()]
    sa = os.path.join(HERE, "service_account.json")
    if not has_flag(args, "--sa-file") and not env.get("GOOGLE_SERVICE_ACCOUNT_FILE") and os.path.isfile(sa):
        args += ["--sa-file", sa]
    say("")
    sys.exit(subprocess.call([venv_python(), os.path.join(code, ENTRY)] + args, cwd=HERE, env=env))


def run_ui():
    code = fetch_program()
    ensure_env(code)
    env = base_env(code)
    env.update(GWS_UI_HERE=HERE, GWS_UI_CODE=code, GWS_UI_OUT=out_root())
    sys.exit(subprocess.call([venv_python(), os.path.join(HERE, "ui.py")], cwd=HERE, env=env))


def main(argv):
    global _quiet
    mode = argv[0] if argv else "cli"
    rest = argv[1:]
    if mode == "build-id":
        _quiet = True
        print((installed() or {}).get("sha", "")[:12] or "none")
    elif mode == "prepare":
        ensure_env(fetch_program())
        say("Ready.")
    elif mode == "ui":
        run_ui()
    elif mode in ("cli", "run"):
        run(rest)
    else:
        sys.exit(f"unknown mode {mode!r}: use ui, cli, prepare or build-id")


if __name__ == "__main__":
    main(sys.argv[1:])

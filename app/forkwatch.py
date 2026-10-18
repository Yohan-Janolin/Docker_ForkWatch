#!/usr/bin/env python3
"""
ForkWatch v2 — watches upstream Git repositories, keeps archived copies,
blocks suspicious updates (force-push, emptied repository), notifies Discord,
and serves a web dashboard with a small JSON API.

Standard library only. Usage:
    forkwatch.py                 start the server (dashboard + scheduler)
    forkwatch.py run             check all repositories now        (talks to the running server)
    forkwatch.py check NAME      check one repository now
    forkwatch.py accept NAME     apply a blocked update
    forkwatch.py test-notif      send a Discord test message
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "2.0.0"

DATA = Path(os.environ.get("FORKWATCH_DATA_DIR", "/data"))
LEGACY_REPOS_FILE = Path(os.environ.get("FORKWATCH_REPOS_FILE", "/config/repos.conf"))
WWW = Path(os.environ.get("FORKWATCH_WWW", str(Path(__file__).resolve().parent / "www")))
PORT = int(os.environ.get("FORKWATCH_PORT", "8080"))

REPOS_DIR = DATA / "repos"
ARCH_DIR = DATA / "archives"
STATE_DIR = DATA / "state"
STORE_FILE = DATA / "forkwatch.json"
LOG_FILE = DATA / "forkwatch.log"

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
STATES = ("ok", "blocked", "missing", "error", "pending")
INTERVALS = (1, 2, 3, 6, 12, 24)
HISTORY_MAX = 60
USER_AGENT = f"ForkWatch/{VERSION}"
GIT_ENV = dict(os.environ, GIT_TERMINAL_PROMPT="0")


def now():
    return int(time.time())


_log_lock = threading.Lock()


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with _log_lock:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def env(name):
    return os.environ.get(name, "").strip()


def env_int(name, default, lo, hi):
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        value = default
    return max(lo, min(hi, value))


# --------------------------------------------------------------------------- #
# Secrets: set from the dashboard (data/secrets.json, root-only), env as fallback
# --------------------------------------------------------------------------- #
SECRETS_FILE = DATA / "secrets.json"
SECRET_ENV = {"discord_webhook": "DISCORD_WEBHOOK", "github_token": "GITHUB_TOKEN", "public_url": "FORKWATCH_URL"}
_secrets_lock = threading.Lock()


def _load_secrets():
    try:
        return json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def secret(key):
    with _secrets_lock:
        value = (_load_secrets().get(key) or "").strip()
    return value or env(SECRET_ENV[key])


def secret_source(key):
    with _secrets_lock:
        if (_load_secrets().get(key) or "").strip():
            return "ui"
    return "env" if env(SECRET_ENV[key]) else None


def save_secrets(changes):
    with _secrets_lock:
        data = _load_secrets()
        for k, v in changes.items():
            if v:
                data[k] = v
            else:
                data.pop(k, None)
        SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SECRETS_FILE.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, SECRETS_FILE)
        os.chmod(SECRETS_FILE, 0o600)


def secrets_status():
    def one(key):
        value, src = secret(key), secret_source(key)
        return {"set": bool(value), "source": src, "hint": ("…" + value[-4:]) if value and len(value) > 8 else None}
    return {"discord": one("discord_webhook"), "github": one("github_token"), "public_url": secret("public_url") or None}


def default_settings():
    lang = env("FORKWATCH_LANGUAGE").lower() or "fr"
    hours = env_int("INTERVAL_HOURS", 6, 1, 24)
    return {
        "language": lang if lang in ("fr", "en") else "fr",
        "interval_hours": hours if hours in INTERVALS else 6,
        "keep_archives": env_int("KEEP_ARCHIVES", 5, 1, 100),
        "shrink_threshold": env_int("SHRINK_THRESHOLD", 50, 10, 95),
        "min_files_for_check": env_int("MIN_FILES_FOR_CHECK", 10, 1, 100000),
        "notify_no_change": env("NOTIFY_NO_CHANGE") == "1",
    }


class ApiError(Exception):
    def __init__(self, code, status=400, detail=None):
        super().__init__(code)
        self.code, self.status, self.detail = code, status, detail


# --------------------------------------------------------------------------- #
# Git helpers
# --------------------------------------------------------------------------- #
class GitError(Exception):
    pass


def git(*args, repo=None, input=None, timeout=600, check=True):
    cmd = ["git"] + (["-C", str(repo)] if repo else []) + [str(a) for a in args]
    try:
        r = subprocess.run(cmd, input=input, capture_output=True, encoding="utf-8",
                           errors="replace", env=GIT_ENV, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GitError(f"timeout: git {args[0]}")
    if check and r.returncode != 0:
        raise GitError((r.stderr or r.stdout).strip() or f"git {args[0]} failed ({r.returncode})")
    return r.stdout


def ls_remote(url):
    out = git("ls-remote", "--symref", url, "HEAD", "refs/heads/*", "refs/tags/*", timeout=180)
    default, refs = None, {}
    for line in out.splitlines():
        if line.startswith("ref: "):
            target, _, sym = line[5:].partition("\t")
            if sym == "HEAD" and target.startswith("refs/heads/"):
                default = target[len("refs/heads/"):]
            continue
        sha, _, ref = line.partition("\t")
        if not ref or ref == "HEAD" or ref.endswith("^{}"):
            continue
        refs[ref] = sha
    return default, refs


def refs_of(repo, *prefixes):
    out = git("for-each-ref", "--format=%(refname) %(objectname)", *prefixes, repo=repo)
    return dict(line.split(" ", 1) for line in out.splitlines() if " " in line)


def is_ancestor(repo, old, new):
    r = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", old, new],
                       capture_output=True, env=GIT_ENV)
    return r.returncode == 0


def count_files(repo, rev):
    out = git("ls-tree", "-r", "--name-only", rev, repo=repo)
    return len([l for l in out.splitlines() if l])


def read_seen(name):
    p = STATE_DIR / f"{name}.seen"
    if not p.exists():
        return None
    seen = {}
    for line in p.read_text(errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 2:
            seen[parts[0]] = parts[1]
    return seen


def write_seen(name, refs):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"{name}.seen").write_text("".join(f"{r} {s}\n" for r, s in sorted(refs.items())))


def list_archives(name):
    d = ARCH_DIR / name
    items = []
    if d.is_dir():
        for e in os.scandir(d):
            if e.is_file() and e.name.endswith(".bundle"):
                st = e.stat()
                items.append({"file": e.name, "size": st.st_size, "date": int(st.st_mtime)})
    items.sort(key=lambda x: (x["date"], x["file"]), reverse=True)
    return items


def make_archive(name, repo, keep):
    if not refs_of(repo, "refs/heads", "refs/tags"):
        return None
    d = ARCH_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.bundle"
    n = 1
    while f.exists():
        n += 1
        f = d / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}-{n}.bundle"
    try:
        git("bundle", "create", f, "--exclude=refs/staging/*", "--all", repo=repo, timeout=3600)
    except GitError as e:
        f.unlink(missing_ok=True)
        log(f"[{name}] archive failed: {e}")
        return None
    for old in list_archives(name)[keep:]:
        (d / old["file"]).unlink(missing_ok=True)
    return f.name


# --------------------------------------------------------------------------- #
# HTTP / GitHub helpers
# --------------------------------------------------------------------------- #
def http_json(method, url, body=None, headers=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw)
        except Exception:
            payload = raw.decode(errors="replace")[:300]
        return e.code, payload


def gh_headers():
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if secret("github_token"):
        h["Authorization"] = f"Bearer {secret('github_token')}"
    return h


def parse_github(text):
    t = text.strip()
    m = re.match(r"^git@github\.com:([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", t)
    if m:
        return m.group(1), m.group(2)
    if "github.com" in t:
        u = urllib.parse.urlparse(t if "://" in t else "https://" + t)
        if u.hostname in ("github.com", "www.github.com"):
            parts = [p for p in u.path.split("/") if p]
            if len(parts) >= 2:
                return parts[0], re.sub(r"\.git$", "", parts[1])
        return None
    if re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", t):
        owner, repo = t.split("/")
        return owner, re.sub(r"\.git$", "", repo)
    return None


def github_full_name(url):
    gh = parse_github(url or "")
    return f"{gh[0]}/{gh[1]}" if gh else None


def resolve(text):
    t = (text or "").strip()
    if not t:
        raise ApiError("invalid_url")
    gh = parse_github(t)
    if gh:
        try:
            code, body = http_json("GET", f"https://api.github.com/repos/{gh[0]}/{gh[1]}", headers=gh_headers())
        except Exception as e:
            raise ApiError("network", 502, str(e))
        if code == 404:
            raise ApiError("not_found", 404)
        if code != 200 or not isinstance(body, dict):
            raise ApiError("network", 502, f"GitHub API HTTP {code}")
        if body.get("fork") and body.get("parent"):
            p = body["parent"]
            return {"name": body["name"], "url": p["clone_url"], "fork": body["full_name"],
                    "kind": "fork", "upstream": p["full_name"]}
        return {"name": body["name"], "url": body["clone_url"], "fork": None,
                "kind": "repo", "upstream": body["full_name"]}
    if not re.match(r"^(https?://|git@|ssh://|git://|/)", t):
        raise ApiError("invalid_url")
    name = re.sub(r"\.git$", "", re.split(r"[/:]", t.rstrip("/"))[-1])
    return {"name": name, "url": t, "fork": None, "kind": "other", "upstream": None}


# --------------------------------------------------------------------------- #
# Discord messages (FR / EN)
# --------------------------------------------------------------------------- #
TXT = {
    "fr": {
        "added": "📦 {name} : ajouté à la surveillance",
        "added_d": "Premier clonage terminé ({refs} branches et tags).",
        "updated": "🔄 {name} : mis à jour",
        "minor": "ℹ️ {name} : changements mineurs",
        "blocked": "🚨 {name} : mise à jour BLOQUÉE",
        "blocked_intro": "Changement suspect détecté upstream, rien n'a été appliqué :",
        "blocked_outro": "Ta copie locale et ton fork GitHub sont intacts. Vérifie sur le tableau de bord et accepte si besoin.",
        "force": "**{branch}** : historique réécrit (force-push), l'ancien commit `{old}` n'est plus dans la nouvelle version",
        "shrink": "**{branch}** : {before} → {after} fichiers (−{pct} %)",
        "missing": "⚠️ {name} : upstream injoignable",
        "missing_d": "Le dépôt ne répond plus (supprimé, passé en privé ou renommé ?) :\n{url}\n\nTa copie locale est intacte.",
        "back": "✅ {name} : upstream de nouveau joignable",
        "accepted": "✅ {name} : mise à jour acceptée manuellement",
        "accepted_d": "L'ancien état est conservé dans `{backup}`.",
        "error": "❌ {name} : {stage}",
        "stage_clone": "échec du clonage", "stage_fetch": "échec de la récupération",
        "stage_apply": "échec de l'application", "stage_internal": "erreur interne",
        "unchanged": "{name} : aucun changement",
        "commits": "**{branch}** : {n} commit(s)", "more": "… et {n} autre(s)",
        "new_branch": "nouvelle branche `{ref}`",
        "deleted_branch": "branche `{ref}` supprimée upstream (conservée ici)",
        "new_tag": "nouveau tag `{ref}`",
        "moved_tag": "tag `{ref}` déplacé upstream (ancienne version conservée ici)",
        "deleted_tag": "tag `{ref}` supprimé upstream (conservé ici)",
        "archive": "État précédent archivé : `{file}`",
        "fork_ok": "Fork GitHub {repo} : synchronisé ✔",
        "fork_diverged": "Fork GitHub {repo} : NON synchronisé (historique divergent, à faire à la main)",
        "fork_error": "Fork GitHub {repo} : erreur HTTP {code} {detail}",
        "test": "🧪 ForkWatch", "test_d": "Notification de test : le webhook fonctionne.",
        "dashboard": "Tableau de bord",
    },
    "en": {
        "added": "📦 {name}: added to watch list",
        "added_d": "Initial clone done ({refs} branches and tags).",
        "updated": "🔄 {name}: updated",
        "minor": "ℹ️ {name}: minor changes",
        "blocked": "🚨 {name}: update BLOCKED",
        "blocked_intro": "Suspicious upstream change detected, nothing was applied:",
        "blocked_outro": "Your local copy and GitHub fork are untouched. Review it on the dashboard and accept it if needed.",
        "force": "**{branch}**: history rewritten (force-push), old commit `{old}` is no longer in the new version",
        "shrink": "**{branch}**: {before} → {after} files (−{pct}%)",
        "missing": "⚠️ {name}: upstream unreachable",
        "missing_d": "The repository no longer answers (deleted, made private or renamed?):\n{url}\n\nYour local copy is intact.",
        "back": "✅ {name}: upstream reachable again",
        "accepted": "✅ {name}: update accepted manually",
        "accepted_d": "The previous state is kept in `{backup}`.",
        "error": "❌ {name}: {stage}",
        "stage_clone": "clone failed", "stage_fetch": "fetch failed",
        "stage_apply": "update failed", "stage_internal": "internal error",
        "unchanged": "{name}: no change",
        "commits": "**{branch}**: {n} commit(s)", "more": "… and {n} more",
        "new_branch": "new branch `{ref}`",
        "deleted_branch": "branch `{ref}` deleted upstream (kept here)",
        "new_tag": "new tag `{ref}`",
        "moved_tag": "tag `{ref}` moved upstream (old version kept here)",
        "deleted_tag": "tag `{ref}` deleted upstream (kept here)",
        "archive": "Previous state archived: `{file}`",
        "fork_ok": "GitHub fork {repo}: synced ✔",
        "fork_diverged": "GitHub fork {repo}: NOT synced (diverged history, sync it manually)",
        "fork_error": "GitHub fork {repo}: HTTP error {code} {detail}",
        "test": "🧪 ForkWatch", "test_d": "Test notification: the webhook works.",
        "dashboard": "Dashboard",
    },
}
COLORS = {"ok": 3066993, "info": 3447003, "warn": 15105570, "alert": 15158332}


def render_discord(name, url, ev, lang):
    T = TXT.get(lang, TXT["fr"])
    p, code = ev["params"], ev["code"]
    lines = []

    def fork_line(f):
        if f:
            lines.append(T["fork_" + f["result"]].format(repo=f["repo"], code=f.get("code"), detail=f.get("detail", "")).strip())

    if code == "added":
        title = T["added"].format(name=name)
        lines.append(T["added_d"].format(refs=p.get("refs", "?")))
    elif code in ("updated", "minor"):
        title = T[code].format(name=name)
        for b in p.get("branches", []):
            lines.append(T["commits"].format(branch=b["branch"], n=b["n"]))
            lines += [f"`{c[0]}` {c[1]}" for c in b.get("commits", [])]
            if b["n"] > len(b.get("commits", [])):
                lines.append(T["more"].format(n=b["n"] - len(b.get("commits", []))))
        if p.get("infos"):
            lines.append("")
            lines += ["• " + T[i["kind"]].format(ref=i["ref"]) for i in p["infos"]]
        if p.get("archive"):
            lines += ["", T["archive"].format(file=p["archive"])]
        fork_line(p.get("fork"))
    elif code == "blocked":
        title = T["blocked"].format(name=name)
        lines.append(T["blocked_intro"])
        for r in p.get("reasons", []):
            if r["type"] == "force":
                lines.append("• " + T["force"].format(branch=r["branch"], old=r["old"][:7]))
            else:
                lines.append("• " + T["shrink"].format(**r))
        lines += ["", T["blocked_outro"]]
    elif code == "missing":
        title = T["missing"].format(name=name)
        lines.append(T["missing_d"].format(url=url))
        if p.get("error"):
            lines.append(f"```{p['error'][:400]}```")
    elif code == "back":
        title = T["back"].format(name=name)
        lines.append(url)
    elif code == "accepted":
        title = T["accepted"].format(name=name)
        lines.append(T["accepted_d"].format(backup=p.get("backup", "?")))
        if p.get("archive"):
            lines.append(T["archive"].format(file=p["archive"]))
        fork_line(p.get("fork"))
    elif code == "error":
        title = T["error"].format(name=name, stage=T.get("stage_" + p.get("stage", "internal"), p.get("stage")))
        lines.append(f"```{p.get('detail', '')[:800]}```")
    elif code == "unchanged":
        title = T["unchanged"].format(name=name)
        lines.append(url)
    elif code == "test":
        title, lines = T["test"], [T["test_d"]]
    else:
        title = name
        lines.append(p.get("text", code))
    if secret("public_url"):
        lines += ["", f"[{T['dashboard']}]({secret('public_url')})"]
    return title, "\n".join(lines).strip(), COLORS.get(ev["level"], COLORS["info"])


def send_discord(name, url, ev, lang, hook=None):
    hook = hook or secret("discord_webhook")
    if not hook:
        return False, "not_configured"
    title, desc, color = render_discord(name, url, ev, lang)
    if len(desc) > 3900:
        desc = desc[:3900] + "\n…"
    payload = {"username": "ForkWatch", "embeds": [{"title": title[:250], "description": desc, "color": color}]}
    try:
        code, body = http_json("POST", hook, payload, timeout=20)
    except Exception as e:
        log(f"Discord: send failed: {e}")
        return False, str(e)
    if code >= 300:
        log(f"Discord: HTTP {code} {body}")
        return False, f"HTTP {code}"
    return True, None


# --------------------------------------------------------------------------- #
# Persistent store
# --------------------------------------------------------------------------- #
def new_repo(name, url, fork):
    t = now()
    return {"name": name, "url": url, "fork": fork or None, "paused": False, "added": t,
            "state": "pending", "since": t, "last_check": None, "last_change": None,
            "event": None, "history": [], "blocked": None, "info": {}}


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        for d in (REPOS_DIR, ARCH_DIR, STATE_DIR):
            d.mkdir(parents=True, exist_ok=True)
        if STORE_FILE.exists():
            self.data = json.loads(STORE_FILE.read_text(encoding="utf-8"))
        else:
            self.data = self._migrate()
        settings = default_settings()
        settings.update(self.data.get("settings") or {})
        self.data["settings"] = settings
        self.data.setdefault("repos", [])
        self.data.setdefault("last_run", None)
        self.save()

    def save(self):
        with self.lock:
            tmp = STORE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, STORE_FILE)

    def repo(self, name):
        return next((r for r in self.data["repos"] if r["name"] == name), None)

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.data))

    def _migrate(self):
        """First start: import the v1 repos.conf and per-repo state files."""
        data = {"version": 2, "settings": default_settings(), "repos": [], "last_run": None}
        if not LEGACY_REPOS_FILE.is_file():
            return data
        for line in LEGACY_REPOS_FILE.read_text(errors="replace").splitlines():
            parts = line.split()
            if len(parts) < 2 or parts[0].startswith("#"):
                continue
            name, url, fork = parts[0], parts[1], (parts[2] if len(parts) > 2 else None)
            if not NAME_RE.match(name) or any(r["name"] == name for r in data["repos"]):
                continue
            r = new_repo(name, url, fork)
            st = STATE_DIR / f"{name}.state"
            if st.exists():
                lines = st.read_text(errors="replace").splitlines()
                if lines and lines[0] in STATES:
                    r["state"] = lines[0]
                if len(lines) > 1 and lines[1].strip().isdigit():
                    r["since"] = int(lines[1])
            for key, suffix in (("last_check", "checked"), ("last_change", "changed")):
                p = STATE_DIR / f"{name}.{suffix}"
                if p.exists() and p.read_text().strip().isdigit():
                    r[key] = int(p.read_text().strip())
            msg = STATE_DIR / f"{name}.msg"
            if msg.exists() and msg.read_text(errors="replace").strip():
                level = {"ok": "ok", "blocked": "alert", "error": "alert", "missing": "warn"}.get(r["state"], "info")
                ev = {"ts": r["since"], "code": "legacy", "level": level,
                      "params": {"text": msg.read_text(errors="replace").strip()}}
                r["event"], r["history"] = ev, [ev]
            if r["state"] == "blocked":
                r["blocked"] = {"reasons": [], "at": r["since"], "legacy": True}
            data["repos"].append(r)
        log(f"Imported {len(data['repos'])} repositories from {LEGACY_REPOS_FILE} — the list is now managed from the dashboard")
        return data


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class Engine:
    def __init__(self, store):
        self.store = store

    # ---- store helpers ----
    def settings(self):
        with self.store.lock:
            return dict(self.store.data["settings"])

    def get(self, name):
        with self.store.lock:
            r = self.store.repo(name)
            return json.loads(json.dumps(r)) if r else None

    def update(self, name, **fields):
        with self.store.lock:
            r = self.store.repo(name)
            if r:
                if "state" in fields and fields["state"] != r["state"]:
                    r["since"] = now()
                r.update(fields)
                self.store.save()

    def record(self, name, code, level, state=None, notify=True, **params):
        ev = {"ts": now(), "code": code, "level": level, "params": params}
        with self.store.lock:
            r = self.store.repo(name)
            if r is None:
                return ev
            if code != "unchanged":
                r["event"] = ev
                r["history"] = ([ev] + r["history"])[:HISTORY_MAX]
            if state and state != r["state"]:
                r["state"], r["since"] = state, ev["ts"]
            url = r["url"]
            lang = self.store.data["settings"]["language"]
            self.store.save()
        log(f"[{name}] {code} {json.dumps(params, ensure_ascii=False)[:300]}")
        if notify:
            send_discord(name, url, ev, lang)
        return ev

    # ---- repository info for the dashboard ----
    def refresh_info(self, name):
        repo = REPOS_DIR / f"{name}.git"
        info = {}
        if (repo / "HEAD").exists():
            try:
                branch = git("symbolic-ref", "--short", "HEAD", repo=repo, check=False).strip()
                head = git("log", "-1", "--format=%h%x1f%ct%x1f%s", repo=repo, check=False).strip().split("\x1f")
                heads = git("for-each-ref", "--format=%(refname)", "refs/heads", repo=repo).split()
                tags = git("for-each-ref", "--sort=-creatordate", "--format=%(refname:short)", "refs/tags", repo=repo).split()
                info = {
                    "branch": branch or None,
                    "head": {"sha": head[0], "date": int(head[1]), "subject": head[2]} if len(head) == 3 else None,
                    "branches": len(heads), "tags": len(tags), "latest_tag": tags[0] if tags else None,
                }
            except (GitError, ValueError) as e:
                log(f"[{name}] info failed: {e}")
        self.update(name, info=info)

    def sync_fork(self, fork, branch):
        if not fork or not branch or not secret("github_token"):
            return None
        try:
            code, body = http_json("POST", f"https://api.github.com/repos/{fork}/merge-upstream",
                                   {"branch": branch}, headers=gh_headers(), timeout=30)
        except Exception as e:
            return {"repo": fork, "result": "error", "code": 0, "detail": str(e)[:200]}
        if code == 200:
            return {"repo": fork, "result": "ok", "code": 200}
        if code == 409:
            return {"repo": fork, "result": "diverged", "code": 409}
        detail = body.get("message", "") if isinstance(body, dict) else str(body)
        return {"repo": fork, "result": "error", "code": code, "detail": detail[:200]}

    # ---- the check itself ----
    def check(self, name):
        """Returns False when the check itself failed (counted as a run error)."""
        r = self.get(name)
        if not r:
            return True
        s = self.settings()
        url, repo = r["url"], REPOS_DIR / f"{name}.git"
        self.update(name, last_check=now())

        # 1. Did upstream move? (ls-remote: downloads nothing)
        try:
            default, remote = ls_remote(url)
        except GitError as e:
            if not (repo / "HEAD").exists():
                # never cloned: most likely a wrong URL, not a repository that disappeared
                if r["state"] != "error":
                    self.record(name, "error", "alert", state="error", stage="clone", detail=str(e)[:600])
                return False
            if r["state"] != "missing":
                self.update(name, state_before_missing=r["state"])
                self.record(name, "missing", "warn", state="missing", error=str(e)[:400])
            else:
                log(f"[{name}] still unreachable")
            return True
        if r["state"] == "missing":
            previous = r.get("state_before_missing") or "ok"
            r["state"] = previous if previous != "missing" else "ok"
            self.record(name, "back", "ok", state=r["state"])

        # First time: full clone
        if not (repo / "HEAD").exists():
            shutil.rmtree(repo, ignore_errors=True)
            try:
                git("clone", "--bare", "--quiet", url, repo, timeout=3600)
                git("config", "core.logAllRefUpdates", "always", repo=repo)
            except GitError as e:
                shutil.rmtree(repo, ignore_errors=True)
                self.record(name, "error", "alert", state="error", stage="clone", detail=str(e)[:600])
                return False
            write_seen(name, remote)
            make_archive(name, repo, s["keep_archives"])
            self.update(name, last_change=now(), blocked=None)
            self.record(name, "added", "info", state="ok", refs=len(remote))
            self.refresh_info(name)
            return True

        seen = read_seen(name)
        if seen == remote:
            log(f"[{name}] no change")
            if r["state"] in ("error", "pending"):
                self.update(name, state="ok")
            if s["notify_no_change"]:
                self.record(name, "unchanged", "info")
            if not r.get("info"):
                self.refresh_info(name)
            return True

        # 2. It moved: fetch into a staging area, without touching the copy
        stale = refs_of(repo, "refs/staging")
        if stale:
            git("update-ref", "--stdin", repo=repo, input="".join(f"delete {ref}\n" for ref in stale))
        try:
            git("fetch", "--quiet", "--no-tags", url,
                "+refs/heads/*:refs/staging/heads/*", "+refs/tags/*:refs/staging/tags/*",
                repo=repo, timeout=3600)
        except GitError as e:
            self.record(name, "error", "alert", state="error", stage="fetch", detail=str(e)[:600])
            return False

        local = refs_of(repo, "refs/heads", "refs/tags")
        staged = {"refs/" + k[len("refs/staging/"):]: v for k, v in refs_of(repo, "refs/staging").items()}
        previous = seen if seen is not None else local

        # 3. Analysis: real update or trap?
        reasons, branches, infos, heads_changed = [], [], [], False
        for ref, new in sorted(staged.items()):
            old = local.get(ref)
            if ref.startswith("refs/heads/"):
                b = ref[len("refs/heads/"):]
                if old == new:
                    continue
                if old is None:
                    infos.append({"kind": "new_branch", "ref": b})
                    heads_changed = True
                    continue
                if not is_ancestor(repo, old, new):
                    reasons.append({"type": "force", "branch": b, "old": old, "new": new})
                    continue
                before, after = count_files(repo, old), count_files(repo, new)
                if before >= s["min_files_for_check"] and after * 100 < before * (100 - s["shrink_threshold"]):
                    reasons.append({"type": "shrink", "branch": b, "old": old, "new": new,
                                    "before": before, "after": after,
                                    "pct": round((before - after) * 100 / before)})
                    continue
                n = int(git("rev-list", "--count", f"{old}..{new}", repo=repo).strip() or 0)
                commits = [line.split("\x1f", 1) for line in
                           git("log", "--format=%h%x1f%s", "-n", "5", f"{old}..{new}", repo=repo).splitlines()]
                branches.append({"branch": b, "n": n, "commits": commits, "old": old, "new": new})
                heads_changed = True
            elif ref.startswith("refs/tags/") and previous.get(ref) != new:
                infos.append({"kind": "new_tag" if ref not in previous else "moved_tag", "ref": ref[len("refs/tags/"):]})
        for ref in sorted(set(previous) - set(staged)):
            if ref.startswith("refs/heads/"):
                infos.append({"kind": "deleted_branch", "ref": ref[len("refs/heads/"):]})
            elif ref.startswith("refs/tags/"):
                infos.append({"kind": "deleted_tag", "ref": ref[len("refs/tags/"):]})

        if reasons:
            write_seen(name, remote)       # do not alert again until upstream moves again
            self.update(name, blocked={"reasons": reasons, "at": now()})
            self.record(name, "blocked", "alert", state="blocked", reasons=reasons)
            return True

        # 4. Healthy update: archive the current state, then apply
        ops = [f"update {ref} {new}\n" for ref, new in staged.items() if ref not in local]
        ops += [f"update refs/heads/{b['branch']} {b['new']} {b['old']}\n" for b in branches]
        archive, fork = None, None
        if ops:
            archive = make_archive(name, repo, s["keep_archives"])
            try:
                git("update-ref", "--stdin", repo=repo, input="".join(ops))
            except GitError as e:
                self.record(name, "error", "alert", state="error", stage="apply", detail=str(e)[:600])
                return False
            if heads_changed:
                fork = self.sync_fork(r["fork"], default)
        git("update-ref", "--stdin", repo=repo,
            input="".join(f"delete {ref}\n" for ref in refs_of(repo, "refs/staging")))
        write_seen(name, remote)
        self.update(name, blocked=None)
        public = [{"branch": b["branch"], "n": b["n"], "commits": b["commits"]} for b in branches]
        if branches:
            self.update(name, last_change=now())
            self.record(name, "updated", "ok", state="ok", total=sum(b["n"] for b in branches),
                        branches=public, infos=infos, archive=archive, fork=fork)
        elif infos:
            self.update(name, last_change=now())
            self.record(name, "minor", "info", state="ok", infos=infos, archive=archive, fork=fork)
        else:
            self.update(name, state="ok")
        self.refresh_info(name)
        return True

    def accept(self, name):
        r = self.get(name)
        repo = REPOS_DIR / f"{name}.git"
        if not r or not (repo / "HEAD").exists():
            raise ApiError("unknown_repo", 404)
        staged = refs_of(repo, "refs/staging")
        if not any(k.startswith("refs/staging/heads/") for k in staged):
            self.update(name, state="ok" if r["state"] == "blocked" else r["state"], blocked=None)
            raise ApiError("nothing_pending", 409)
        s = self.settings()
        ts = time.strftime("%Y%m%d-%H%M%S")
        archive = make_archive(name, repo, s["keep_archives"])
        backup = f"refs/forkwatch/before-accept/{ts}/"
        current = refs_of(repo, "refs/heads", "refs/tags")
        git("update-ref", "--stdin", repo=repo,
            input="".join(f"create {backup}{ref[5:]} {sha}\n" for ref, sha in current.items()))
        git("update-ref", "--stdin", repo=repo,
            input="".join(f"update refs/{k[len('refs/staging/'):]} {v}\n" for k, v in staged.items()))
        git("update-ref", "--stdin", repo=repo, input="".join(f"delete {k}\n" for k in staged))
        default = git("symbolic-ref", "--short", "HEAD", repo=repo, check=False).strip()
        fork = self.sync_fork(r["fork"], default)
        self.update(name, last_change=now(), blocked=None)
        self.record(name, "accepted", "warn", state="ok", backup=backup, archive=archive, fork=fork)
        self.refresh_info(name)

    def add(self, name, url, fork):
        name, url, fork = (name or "").strip(), (url or "").strip(), (fork or "").strip() or None
        if not NAME_RE.match(name):
            raise ApiError("invalid_name")
        if not url:
            raise ApiError("invalid_url")
        if fork and not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", fork):
            fork = github_full_name(fork)
            if not fork:
                raise ApiError("invalid_fork")
        with self.store.lock:
            if self.store.repo(name):
                raise ApiError("name_taken", 409)
            key = github_full_name(url) or url.rstrip("/").removesuffix(".git")
            for other in self.store.data["repos"]:
                if (github_full_name(other["url"]) or other["url"].rstrip("/").removesuffix(".git")) == key:
                    raise ApiError("already_watched", 409, other["name"])
            if (REPOS_DIR / f"{name}.git").exists() or (STATE_DIR / f"{name}.seen").exists():
                # leftovers of a removed repo with the same name: start clean (archives are kept)
                shutil.rmtree(REPOS_DIR / f"{name}.git", ignore_errors=True)
                (STATE_DIR / f"{name}.seen").unlink(missing_ok=True)
            self.store.data["repos"].append(new_repo(name, url, fork))
            self.store.save()
        log(f"[{name}] added ({url}{', fork ' + fork if fork else ''})")
        return name

    def remove(self, name, purge):
        with self.store.lock:
            r = self.store.repo(name)
            if not r:
                raise ApiError("unknown_repo", 404)
            self.store.data["repos"].remove(r)
            self.store.save()
        if purge:
            shutil.rmtree(REPOS_DIR / f"{name}.git", ignore_errors=True)
            for p in STATE_DIR.glob(f"{name}.*"):
                p.unlink(missing_ok=True)
        log(f"[{name}] removed{' (local copy deleted, archives kept)' if purge else ''}")


# --------------------------------------------------------------------------- #
# Job runner (one job at a time: no concurrent git operations) + scheduler
# --------------------------------------------------------------------------- #
class Runner:
    def __init__(self, engine, store):
        self.engine, self.store = engine, store
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.pending = []
        self.current = None        # current job
        self.current_repo = None   # repository being checked
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, op, name=None, **kw):
        with self.lock:
            same = [j for j in self.pending if j["op"] == op and j["name"] == name]
            if same and op in ("check", "run_all"):
                return False
            if op == "run_all" and self.current and self.current["op"] == "run_all":
                return False
            job = {"op": op, "name": name, "kw": kw}
            self.pending.append(job)
        self.q.put(job)
        return True

    def busy_with(self, op):
        with self.lock:
            return any(j["op"] == op for j in self.pending) or bool(self.current and self.current["op"] == op)

    def snapshot(self):
        with self.lock:
            queued = [j["name"] for j in self.pending if j["op"] == "check"]
            all_queued = any(j["op"] == "run_all" for j in self.pending)
            running_all = bool(self.current and self.current["op"] == "run_all")
            return {"current": self.current_repo, "queued": queued,
                    "run_all": all_queued or running_all, "busy": bool(self.current or self.pending)}

    def _loop(self):
        while True:
            job = self.q.get()
            with self.lock:
                self.pending = [j for j in self.pending if j is not job]
                self.current = job
            try:
                self._do(job)
            except ApiError as e:
                log(f"job {job['op']} {job['name'] or ''}: {e.code}")
            except Exception:
                log(f"job {job['op']} {job['name'] or ''} crashed:\n{traceback.format_exc()}")
                if job["name"]:
                    self.engine.record(job["name"], "error", "alert", state="error", stage="internal",
                                       detail=traceback.format_exc(limit=2)[-600:])
            finally:
                with self.lock:
                    self.current, self.current_repo = None, None

    def _checked(self, name):
        with self.lock:
            self.current_repo = name
        try:
            return self.engine.check(name)
        except Exception:
            log(f"[{name}] check crashed:\n{traceback.format_exc()}")
            self.engine.record(name, "error", "alert", state="error", stage="internal",
                               detail=traceback.format_exc(limit=2)[-600:])
            return False

    def _do(self, job):
        op, name = job["op"], job["name"]
        if op == "check":
            self._checked(name)
        elif op == "accept":
            with self.lock:
                self.current_repo = name
            self.engine.accept(name)
        elif op == "run_all":
            started, errors = now(), 0
            try:
                with self.store.lock:
                    names = [r["name"] for r in self.store.data["repos"] if not r.get("paused")]
                log(f"=== Run started ({job['kw'].get('trigger', 'manual')}, {len(names)} repositories) ===")
                for n in names:
                    if self.engine.get(n) and not self.engine.get(n).get("paused"):
                        errors += 0 if self._checked(n) else 1
                log(f"=== Run finished ({errors} error(s)) ===")
            finally:
                with self.store.lock:
                    self.store.data["last_run"] = {"started": started, "finished": now(), "errors": errors,
                                                   "ok": errors == 0, "trigger": job["kw"].get("trigger", "manual")}
                    self.store.save()


def scheduler(store, runner):
    while True:
        try:
            with store.lock:
                hours = store.data["settings"]["interval_hours"]
                last = store.data.get("last_run")
            due = (last["started"] + hours * 3600) if last else 0
            if time.time() >= due and not runner.busy_with("run_all"):
                runner.submit("run_all", trigger="schedule")
        except Exception:
            log(f"scheduler error:\n{traceback.format_exc()}")
        time.sleep(15)


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #
def build_status(store, runner):
    d = store.snapshot()
    s = d["settings"]
    last = d.get("last_run")
    for r in d["repos"]:
        r["archives"] = list_archives(r["name"])
        r["upstream_full"] = github_full_name(r["url"])
    return {
        "version": VERSION, "now": now(), "settings": s,
        "options": {"intervals": INTERVALS, "languages": ["fr", "en"]},
        "secrets": secrets_status(),
        "run": dict(runner.snapshot(), last=last,
                    next_due=(last["started"] + s["interval_hours"] * 3600) if last else None),
        "repos": d["repos"],
    }


def make_handler(store, engine, runner):
    class Handler(BaseHTTPRequestHandler):
        server_version = USER_AGENT
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def send(self, code, body=None, ctype="application/json; charset=utf-8", extra=None):
            data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                data = json.loads(self.rfile.read(n))
                return data if isinstance(data, dict) else {}
            except ValueError:
                raise ApiError("bad_request")

        def known(self, name):
            name = urllib.parse.unquote(name)
            if not engine.get(name):
                raise ApiError("unknown_repo", 404)
            return name

        def route(self, method):
            path = urllib.parse.urlparse(self.path)
            p, qs = path.path.rstrip("/") or "/", urllib.parse.parse_qs(path.query)
            m = re.match(r"^/api/repos/([^/]+)(?:/([a-z-]+))?(?:/(.+))?$", p)

            if method in ("GET", "HEAD") and p in ("/", "/index.html"):
                return self.send(200, (WWW / "index.html").read_bytes(), "text/html; charset=utf-8")
            if method == "GET" and p == "/api/health":
                return self.send(200, {"ok": True, "version": VERSION})
            if method == "GET" and p == "/api/status":
                return self.send(200, build_status(store, runner))
            if method == "PUT" and p == "/api/settings":
                b = self.body()
                new = {}
                if "language" in b:
                    if b["language"] not in ("fr", "en"):
                        raise ApiError("invalid_setting", detail="language")
                    new["language"] = b["language"]
                if "interval_hours" in b:
                    if int(b["interval_hours"]) not in INTERVALS:
                        raise ApiError("invalid_setting", detail="interval_hours")
                    new["interval_hours"] = int(b["interval_hours"])
                for key, lo, hi in (("keep_archives", 1, 100), ("shrink_threshold", 10, 95),
                                    ("min_files_for_check", 1, 100000)):
                    if key in b:
                        try:
                            v = int(b[key])
                        except (TypeError, ValueError):
                            raise ApiError("invalid_setting", detail=key)
                        if not lo <= v <= hi:
                            raise ApiError("invalid_setting", detail=key)
                        new[key] = v
                if "notify_no_change" in b:
                    new["notify_no_change"] = bool(b["notify_no_change"])
                with store.lock:
                    store.data["settings"].update(new)
                    store.save()
                return self.send(200, store.snapshot()["settings"])
            if method == "PUT" and p == "/api/secrets":
                b = self.body()
                changes = {}
                if "discord_webhook" in b:
                    v = (b["discord_webhook"] or "").strip()
                    if v and not re.match(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+/?$", v):
                        raise ApiError("invalid_webhook")
                    changes["discord_webhook"] = v
                if "github_token" in b:
                    v = (b["github_token"] or "").strip()
                    if v and (len(v) < 20 or re.search(r"\s", v)):
                        raise ApiError("invalid_token")
                    changes["github_token"] = v
                if "public_url" in b:
                    v = (b["public_url"] or "").strip().rstrip("/")
                    if v and not re.match(r"^https?://\S+$", v):
                        raise ApiError("invalid_url")
                    changes["public_url"] = v
                save_secrets(changes)
                log("Secrets updated from the dashboard: " + ", ".join(
                    f"{k} {'set' if v else 'cleared'}" for k, v in changes.items()))
                return self.send(200, secrets_status())
            if method == "POST" and p == "/api/run":
                runner.submit("run_all", trigger="manual")
                return self.send(202, {"queued": True})
            if method == "POST" and p == "/api/resolve":
                return self.send(200, resolve(self.body().get("url")))
            if method == "POST" and p == "/api/test-notification":
                with store.lock:
                    lang = store.data["settings"]["language"]
                hook = (self.body().get("discord_webhook") or "").strip() or None
                ok, err = send_discord("ForkWatch", "", {"code": "test", "level": "ok", "params": {}}, lang, hook=hook)
                if not ok:
                    raise ApiError("not_configured" if err == "not_configured" else "notify_failed", 502, err)
                return self.send(200, {"sent": True})
            if method == "POST" and p == "/api/repos":
                b = self.body()
                name = engine.add(b.get("name"), b.get("url"), b.get("fork"))
                runner.submit("check", name)
                return self.send(201, {"name": name})
            if m:
                name, action, rest = self.known(m.group(1)), m.group(2), m.group(3)
                if method == "POST" and action == "check" and not rest:
                    runner.submit("check", name)
                    return self.send(202, {"queued": True})
                if method == "POST" and action == "accept" and not rest:
                    r = engine.get(name)
                    if r["state"] != "blocked":
                        raise ApiError("nothing_pending", 409)
                    runner.submit("accept", name)
                    return self.send(202, {"queued": True})
                if method == "PATCH" and not action:
                    b = self.body()
                    if "paused" in b:
                        engine.update(name, paused=bool(b["paused"]))
                        log(f"[{name}] {'paused' if b['paused'] else 'resumed'}")
                    if "fork" in b:
                        fork = (b["fork"] or "").strip() or None
                        if fork and not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", fork):
                            fork = github_full_name(fork)
                            if not fork:
                                raise ApiError("invalid_fork")
                        engine.update(name, fork=fork)
                    return self.send(200, engine.get(name))
                if method == "DELETE" and not action:
                    if runner.snapshot()["current"] == name:
                        raise ApiError("busy", 409)
                    engine.remove(name, qs.get("purge", ["0"])[0] in ("1", "true"))
                    return self.send(200, {"removed": name})
                if method == "GET" and action == "archives" and rest:
                    files = {a["file"] for a in list_archives(name)}
                    fname = urllib.parse.unquote(rest)
                    if fname not in files:
                        raise ApiError("not_found", 404)
                    f = ARCH_DIR / name / fname
                    size = f.stat().st_size
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(size))
                    self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                    self.end_headers()
                    with open(f, "rb") as fh:
                        shutil.copyfileobj(fh, self.wfile)
                    return None
            raise ApiError("not_found", 404)

        def handle_method(self, method):
            try:
                self.route(method)
            except ApiError as e:
                self.send(e.status, {"error": e.code, "detail": e.detail})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                log(f"API error {method} {self.path}:\n{traceback.format_exc()}")
                try:
                    self.send(500, {"error": "internal"})
                except Exception:
                    pass

        def do_GET(self):
            self.handle_method("GET")

        def do_HEAD(self):
            self.handle_method("HEAD")

        def do_POST(self):
            self.handle_method("POST")

        def do_PUT(self):
            self.handle_method("PUT")

        def do_PATCH(self):
            self.handle_method("PATCH")

        def do_DELETE(self):
            self.handle_method("DELETE")

    return Handler


def serve():
    DATA.mkdir(parents=True, exist_ok=True)
    # Locks left behind by an interrupted git command (container stopped mid-run)
    for lock in REPOS_DIR.glob("*.git/**/*.lock"):
        lock.unlink(missing_ok=True)
    store = Store()
    engine = Engine(store)
    for r in store.snapshot()["repos"]:
        if not r.get("info"):
            engine.refresh_info(r["name"])
    runner = Runner(engine, store)
    threading.Thread(target=scheduler, args=(store, runner), daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), make_handler(store, engine, runner))
    server.daemon_threads = True
    log(f"ForkWatch {VERSION} — dashboard on port {PORT}, {len(store.data['repos'])} repositories, "
        f"check every {store.data['settings']['interval_hours']} h")
    server.serve_forever()


def cli(args):
    base = f"http://127.0.0.1:{PORT}"
    usage = "usage: forkwatch.py [run | check NAME | accept NAME | test-notif]"
    cmd = args[0]
    if cmd == "run":
        method, path = "POST", "/api/run"
    elif cmd in ("check", "accept") and len(args) == 2:
        method, path = "POST", f"/api/repos/{urllib.parse.quote(args[1])}/{cmd}"
    elif cmd == "test-notif":
        method, path = "POST", "/api/test-notification"
    else:
        print(usage)
        return 2
    try:
        code, body = http_json(method, base + path, {})
    except Exception as e:
        print(f"ForkWatch server unreachable: {e}")
        return 1
    print(code, json.dumps(body, ensure_ascii=False))
    return 0 if code < 300 else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] != "serve":
        sys.exit(cli(sys.argv[1:]))
    serve()

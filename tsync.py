#!/usr/bin/env python3
"""tsync - backup-first save sync for Android apps between devices over adb.

Every device's save is snapshotted into a bare git repo (store/<app>.git)
before anything is written anywhere, so every sync can be undone.  Git only
stores changed files and delta-compresses them, so snapshots are cheap and a
new one is only created when the save actually changed.

Sync direction is decided three-way against the state both devices had after
the last sync: if only one side changed, it is proposed as the source; if
both changed, the user decides.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

HERE = Path(os.environ.get("TSYNC_HOME") or Path(__file__).resolve().parent)
APPS_DIR = HERE / "apps"
STORE_DIR = HERE / "store"
CONFIG_FILE = HERE / "devices.json"
REMOTE_STAGE = ".tsync"  # scratch dir on the device, inside the app root
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "tsync", "GIT_AUTHOR_EMAIL": "tsync@localhost",
    "GIT_COMMITTER_NAME": "tsync", "GIT_COMMITTER_EMAIL": "tsync@localhost",
}
SHRINK_RATIO = 0.7  # source smaller than this fraction of target -> suspicious


class TsyncError(Exception):
    pass


def say(msg: str = "") -> None:
    print(msg, flush=True)


def shq(s: str) -> str:
    """Quote for the device's POSIX sh."""
    return "'" + s.replace("'", "'\\''") + "'"


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def fmt_time(epoch: float | None) -> str:
    if not epoch:
        return "?"
    return dt.datetime.fromtimestamp(epoch).strftime("%d.%m. %H:%M")


def human(n: int) -> str:
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def prompt(text: str) -> str:
    try:
        return input(text).strip()
    except EOFError:
        raise TsyncError("zrušeno (žádná odpověď)")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def location(path: str) -> str:
    """'active_state/SideWalk.tlws.bak' -> 'SideWalk'."""
    return PurePosixPath(path).name.split(".")[0]


def pair_key(a: str, b: str) -> str:
    return "+".join(sorted([a, b]))


def glob_match(pattern: str, path: str) -> bool:
    """Patterns without '/' match the top-level entry, others the whole path
    (same semantics as `find -path ./PATTERN` used on the device)."""
    if "/" in pattern:
        return fnmatch.fnmatchcase(path, pattern)
    return fnmatch.fnmatchcase(path.split("/", 1)[0], pattern)


def summarize(names, limit: int = 8) -> str:
    names = sorted(names)
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f" (+{len(names) - limit} dalších)"


# --------------------------------------------------------------------- adb

class Adb:
    def __init__(self, exe: str):
        self.exe = exe
        # Start the server detached from our pipes, otherwise the spawned
        # server inherits them and capture_output never sees EOF (Windows).
        subprocess.run([exe, "start-server"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def run(self, args, timeout: int = 600) -> subprocess.CompletedProcess:
        try:
            return subprocess.run([self.exe, *args], capture_output=True,
                                  stdin=subprocess.DEVNULL, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise TsyncError(f"adb {' '.join(args[:3])} nedoběhlo včas")

    def devices(self) -> dict[str, str]:
        out = self.run(["devices"]).stdout.decode(errors="replace")
        res = {}
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                res[parts[0]] = parts[1]
        return res


class Device:
    def __init__(self, alias: str, label: str, serial: str, adb: Adb, root: str):
        self.alias, self.label, self.serial, self.adb = alias, label, serial, adb
        self.root = root  # the app's data dir on this device

    def __str__(self) -> str:
        return self.label

    def sh(self, script: str, check: bool = True, timeout: int = 600) -> tuple[int, str]:
        """Run a sh script on the device; exit code comes back via a sentinel
        because old adb versions don't propagate it."""
        wrapped = f"({script}) 2>&1; echo __TSYNC_RC=$?"
        p = self.adb.run(["-s", self.serial, "exec-out", wrapped], timeout=timeout)
        out = p.stdout.decode("utf-8", errors="replace")
        m = re.search(r"__TSYNC_RC=(\d+)\s*$", out)
        if not m:
            err = p.stderr.decode(errors="replace").strip() or out.strip()[-500:]
            raise TsyncError(f"{self}: adb příkaz selhal: {err}")
        rc, body = int(m.group(1)), out[:m.start()]
        if check and rc != 0:
            raise TsyncError(f"{self}: příkaz selhal (rc={rc}): {body.strip()[-800:]}")
        return rc, body

    def pull(self, remotes: list[str], local_dir: Path) -> None:
        """Pull remote files/dirs into an existing local dir (as its children)."""
        batch: list[str] = []
        for i, r in enumerate(remotes):
            batch.append(r)
            if i == len(remotes) - 1 or sum(len(b) + 3 for b in batch) > 12000:
                p = self.adb.run(["-s", self.serial, "pull", *batch, str(local_dir)])
                if p.returncode != 0:
                    raise TsyncError(f"{self}: adb pull selhal: "
                                     f"{(p.stderr or p.stdout).decode(errors='replace').strip()[-500:]}")
                batch = []

    def push(self, locals_: list[Path], remote_dir: str) -> None:
        """Push files into an existing remote dir.  Explicit files, because
        adb versions disagree on what pushing a directory does."""
        batch: list[str] = []
        for i, f in enumerate(locals_):
            batch.append(str(f))
            if i == len(locals_) - 1 or sum(len(b) + 3 for b in batch) > 12000:
                p = self.adb.run(["-s", self.serial, "push", *batch, remote_dir + "/"])
                if p.returncode != 0:
                    raise TsyncError(f"{self}: adb push do {remote_dir} selhal: "
                                     f"{(p.stderr or p.stdout).decode(errors='replace').strip()}")
                batch = []

    def is_running(self, package: str) -> bool:
        rc, out = self.sh(f"pidof {shq(package)}", check=False)
        return rc == 0 and out.strip() != ""

    def clock(self) -> int:
        return int(self.sh("date +%s")[1].strip())

    def app_version(self, package: str) -> list:
        """[versionCode, versionName] of the installed app."""
        out = self.sh(f"dumpsys package {shq(package)} | grep -E 'versionCode=|versionName='")[1]
        code = re.search(r"versionCode=(\d+)", out)
        name = re.search(r"versionName=(\S+)", out)
        if not code:
            raise TsyncError(f"{self}: {package} není nainstalovaná")
        return [int(code.group(1)), name.group(1) if name else "?"]


@dataclass
class FileInfo:
    size: int
    mtime: int
    uid: int
    gid: int
    mode: str
    sha: str


STAT_RE = re.compile(r"^S (\d+) (\d+) (\d+) (\d+) ([0-7]+) \./(.+)$")
SHA_RE = re.compile(r"^([0-9a-f]{64})  \./(.+)$")


def list_files(dev: Device, base: str, prune: list[str] = ()) -> dict[str, FileInfo]:
    """All regular files under `base` with sha256, skipping paths matching
    the `prune` globs (relative to base) and our own staging dir."""
    pr = " -o ".join(f"-path {shq('./' + p)}" for p in [REMOTE_STAGE, *prune])
    sel = f"\\( {pr} \\) -prune -o -type f"
    script = (f"cd {shq(base)} || exit 3; "
              f"find . {sel} -exec stat -c 'S %s %Y %u %g %a %n' {{}} + || exit 4; "
              f"find . {sel} -exec sha256sum {{}} + || exit 5")
    rc, out = dev.sh(script, check=False)
    if rc == 3:
        raise TsyncError(f"{dev}: složka {base} neexistuje (hra tu nemá žádná data?)")
    if rc != 0:
        raise TsyncError(f"{dev}: výpis souborů selhal (rc={rc}): {out.strip()[-500:]}")
    stats, shas = {}, {}
    for line in out.splitlines():
        line = line.rstrip("\r")
        if m := STAT_RE.match(line):
            stats[m.group(6)] = m.groups()[:5]
        elif m := SHA_RE.match(line):
            shas[m.group(2)] = m.group(1)
        elif line:
            raise TsyncError(f"{dev}: nečekaný řádek ve výpisu: {line!r}")
    if set(stats) != set(shas):
        raise TsyncError(f"{dev}: výpis souborů je nekonzistentní (mění se soubory?)")
    return {p: FileInfo(int(s[0]), int(s[1]), int(s[2]), int(s[3]), s[4], shas[p])
            for p, s in stats.items()}


# ------------------------------------------------------------------- store

class Store:
    """Bare git repo used purely through plumbing: there is no work tree,
    so nothing in it can ever be overwritten by a checkout."""

    def __init__(self, app_name: str):
        self.git_dir = STORE_DIR / f"{app_name}.git"
        self.state_file = STORE_DIR / f"{app_name}.state.json"
        self.log_file = STORE_DIR / f"{app_name}.log"
        if not (self.git_dir / "HEAD").exists():
            STORE_DIR.mkdir(parents=True, exist_ok=True)
            subprocess.run([find_git(), "init", "--bare", "-q", str(self.git_dir)], check=True)
            for key, val in [("core.autocrlf", "false"), ("core.safecrlf", "false"),
                             ("gc.auto", "0"), ("core.logAllRefUpdates", "true")]:
                self.git("config", key, val)
            (self.git_dir / "info").mkdir(exist_ok=True)
            (self.git_dir / "info" / "attributes").write_text("* -text -diff -merge\n")

    def proc(self, *args, input: bytes | None = None, env: dict | None = None):
        e = dict(os.environ, **GIT_IDENTITY, **(env or {}))
        return subprocess.run([find_git(), f"--git-dir={self.git_dir}", *args],
                              input=input, capture_output=True, env=e)

    def git(self, *args, input: bytes | None = None, env: dict | None = None) -> bytes:
        p = self.proc(*args, input=input, env=env)
        if p.returncode != 0:
            raise TsyncError(f"git {args[0]} selhal: {p.stderr.decode(errors='replace').strip()}")
        return p.stdout

    def resolve(self, rev: str) -> str:
        p = self.proc("rev-parse", "--verify", "-q", f"{rev}^{{commit}}")
        if p.returncode != 0:
            raise TsyncError(f"snapshot '{rev}' neexistuje")
        return p.stdout.decode().strip()

    def head(self, device: str) -> str | None:
        p = self.proc("rev-parse", "--verify", "-q", f"refs/heads/{device}")
        return p.stdout.decode().strip() if p.returncode == 0 else None

    def branches(self) -> list[str]:
        out = self.git("for-each-ref", "--format=%(refname:short)", "refs/heads/")
        return out.decode().split()

    def history(self, device: str, n: int) -> list[str]:
        if not self.head(device):
            return []
        return self.git("rev-list", f"--max-count={n}", f"refs/heads/{device}").decode().split()

    def tree_of(self, commit: str) -> str:
        return self.git("rev-parse", f"{commit}^{{tree}}").decode().strip()

    def manifest(self, commit: str) -> dict:
        raw = self.git("cat-file", "commit", commit).decode("utf-8")
        message = raw.split("\n\n", 1)[1]
        return json.loads(message.split("\n\n", 1)[1])

    def entries(self, commit: str) -> dict[str, str]:
        out = self.git("ls-tree", "-r", "-z", "--full-tree", commit)
        res = {}
        for rec in out.split(b"\0"):
            if rec:
                meta, path = rec.split(b"\t", 1)
                res[path.decode("utf-8")] = meta.split()[2].decode()
        return res

    def read_blobs(self, shas: list[str]) -> dict[str, bytes]:
        if not shas:
            return {}
        out = self.git("cat-file", "--batch", input="".join(s + "\n" for s in shas).encode())
        res, i = {}, 0
        for s in shas:
            nl = out.index(b"\n", i)
            header = out[i:nl].split()
            if len(header) != 3 or header[1] != b"blob":
                raise TsyncError(f"v úložišti chybí objekt {s}")
            size, start = int(header[2]), nl + 1
            res[s] = out[start:start + size]
            i = start + size + 1
        return res

    def commit(self, device: str, src_dir: Path, manifest: dict, title: str) -> tuple[str, bool]:
        """Store the files of src_dir listed in manifest; returns (commit, created).
        Nothing is committed when the content equals the device's last snapshot."""
        paths = sorted(manifest["files"])
        blobs = self.git("hash-object", "-w", "--no-filters", "--stdin-paths",
                         input="\n".join(str(src_dir / p) for p in paths).encode("utf-8")).split()
        if len(blobs) != len(paths):
            raise TsyncError("git hash-object vrátil špatný počet objektů")
        with tempfile.TemporaryDirectory(prefix="tsync-idx-") as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            info = "".join(f"100644 {b.decode()}\t{p}\n" for b, p in zip(blobs, paths))
            self.git("update-index", "--add", "--index-info", input=info.encode("utf-8"), env=env)
            tree = self.git("write-tree", env=env).decode().strip()
        parent = self.head(device)
        if parent and self.tree_of(parent) == tree:
            return parent, False
        message = f"{device} {manifest['taken_at']} {title}\n\n{json.dumps(manifest, indent=1, ensure_ascii=False)}\n"
        commit = self.git("commit-tree", tree, *(["-p", parent] if parent else []),
                          input=message.encode("utf-8")).decode().strip()
        self.git("update-ref", "-m", title, f"refs/heads/{device}", commit, parent or "")
        return commit, True

    def verify(self, commit: str) -> None:
        """Re-read every file of a snapshot and check it against its manifest."""
        man, ents = self.manifest(commit), self.entries(commit)
        if set(ents) != set(man["files"]):
            raise TsyncError(f"snapshot {commit[:8]} nesedí s manifestem")
        blobs = self.read_blobs(list(ents.values()))
        for path, blob in ents.items():
            if sha256_bytes(blobs[blob]) != man["files"][path][2]:
                raise TsyncError(f"snapshot {commit[:8]}: {path} je poškozený")

    def export(self, commit: str, dest: Path, paths: list[str]) -> None:
        man, ents = self.manifest(commit), self.entries(commit)
        blobs = self.read_blobs([ents[p] for p in paths])
        for p in paths:
            data = blobs[ents[p]]
            if sha256_bytes(data) != man["files"][p][2]:
                raise TsyncError(f"snapshot {commit[:8]}: {p} je poškozený")
            target = dest / p
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    def load_state(self) -> dict:
        """{"bases": {"<alias>+<alias>": {...}}} - last common state per device pair."""
        state = json.loads(self.state_file.read_text("utf-8")) if self.state_file.exists() else {}
        old = state.pop("base", None)  # format before per-pair bases
        bases = state.setdefault("bases", {})
        if old:
            bases.setdefault(pair_key(*old["devices"]), old)
        return state

    def save_state(self, state: dict) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False), "utf-8")
        os.replace(tmp, self.state_file)

    def log(self, msg: str) -> None:
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(f"{now_iso()} {msg}\n")


class Lock:
    """Held for the lifetime of the process; the OS drops it if we crash."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise TsyncError("tsync už běží v jiném okně")


# ----------------------------------------------------------------- context

@dataclass
class Snap:
    device: str
    commit: str
    manifest: dict
    created: bool = False


class Ctx:
    def __init__(self, app_name: str, yes: bool, force: bool):
        # devices.json is local (device IDs); a fresh checkout starts empty and
        # devices get named on first contact.
        self.cfg = (json.loads(CONFIG_FILE.read_text("utf-8")) if CONFIG_FILE.exists()
                    else {"adb": "platform-tools/adb.exe", "devices": {}})
        self.app_name = app_name
        self.app = json.loads((APPS_DIR / f"{app_name}.json").read_text("utf-8"))
        self.yes, self.force = yes, force
        self.lock = Lock(STORE_DIR / f"{app_name}.lock")
        self.store = Store(app_name)
        self._adb: Adb | None = None

    @property
    def adb(self) -> Adb:
        if self._adb is None:
            self._adb = Adb(find_adb(self.cfg))
        return self._adb

    @property
    def exclude(self) -> list[str]:
        return self.app.get("exclude", [])

    def kind(self, path: str) -> str | None:
        """'sync', 'backup' (backed up but never written) or None (ignored).
        Everything not listed in the profile is synced, so state the game
        adds in a future version isn't silently left behind."""
        if any(glob_match(g, path) for g in self.exclude):
            return None
        if any(glob_match(g, path) for g in self.app.get("backup_only", [])):
            return "backup"
        return "sync"

    def is_synced(self, path: str) -> bool:
        return self.kind(path) == "sync"

    def sync_files(self, manifest: dict) -> dict[str, list]:
        return {p: v for p, v in manifest["files"].items() if self.is_synced(p)}

    def area(self, path: str) -> str:
        """Human-sized unit of change: a location inside the locations dir,
        otherwise the top-level entry."""
        top, _, rest = path.partition("/")
        if rest and top == self.app.get("locations_dir"):
            return location(rest)
        return top

    def sync_id(self, manifest: dict) -> str:
        files = self.sync_files(manifest)
        return sha256_bytes("".join(f"{p}\0{files[p][2]}\n" for p in sorted(files)).encode())

    def log(self, msg: str) -> None:
        self.store.log(f"[{self.app_name}] {msg}")

    def ask(self, question: str, default: bool = True) -> bool:
        if self.yes:
            say(f"{question} -> ano (--yes)")
            return True
        hint = "[A/n]" if default else "[a/N]"
        while True:
            ans = prompt(f"{question} {hint} ").lower()
            if not ans:
                return default
            if ans in ("a", "ano", "y", "yes"):
                return True
            if ans in ("n", "ne", "no"):
                return False

    def choose(self, question: str, options: list[str]) -> int:
        """Returns index into options; the last option must be the 'do nothing' one."""
        if self.yes:
            raise TsyncError("tady je potřeba rozhodnout ručně; spusť bez --yes, nebo použij `push`")
        say(question)
        for i, opt in enumerate(options, 1):
            say(f"  {i}) {opt}")
        while True:
            ans = prompt("Volba: ")
            if ans.isdigit() and 1 <= int(ans) <= len(options):
                return int(ans) - 1

    def confirm_word(self, question: str, word: str = "ANO") -> bool:
        if self.force:
            return True
        if self.yes:
            raise TsyncError("podezřelá operace; potvrď ručně, nebo přidej --force")
        return prompt(f"{question}\nPro pokračování napiš {word}: ") == word

    def connected(self, quiet: bool = False) -> list[Device]:
        online = self.adb.devices()
        # Emulators started after the adb server aren't discovered on their own.
        for d in self.cfg["devices"].values():
            serials = d["serial"] if isinstance(d["serial"], list) else [d["serial"]]
            if not any(online.get(s) == "device" for s in serials):
                for s in serials:
                    if re.fullmatch(r"[\w.-]+:\d+", s):
                        self.adb.run(["connect", s], timeout=10)
                        online = self.adb.devices()
        for s, state in online.items():
            if state != "device" and not quiet:
                say(f"! zařízení {s} je ve stavu '{state}'"
                    + (" – potvrď na něm povolení USB ladění" if state == "unauthorized" else ""))
        # Devices are identified by their Android ID, not the adb serial:
        # every BlueStacks is "emulator-5554", so the serial alone would let
        # a different PC's emulator pass for ours.
        ids = {s: device_id(self.adb, s) for s, state in online.items() if state == "device"}
        for s, i in ids.items():
            if i is None and not quiet:
                say(f"! {s}: nepodařilo se zjistit identitu zařízení, přeskakuju ho (zkus to znovu)")
        res, taken = [], set()
        for alias, d in self.cfg["devices"].items():
            serials = d["serial"] if isinstance(d["serial"], list) else [d["serial"]]
            if d.get("id"):
                match = next((s for s, i in ids.items() if i == d["id"]), None)
            else:  # first contact: trust the serial once and remember the ID
                match = next((s for s in serials if ids.get(s) and ids[s] not in taken), None)
                if match:
                    d["id"] = ids[match]
                    self.save_config()
                    self.log(f"learned id of {alias}")
            if match:
                taken.add(ids[match])
                if d.get("ignore"):
                    continue
                root = d.get("roots", {}).get(self.app_name, self.app["root"])
                res.append(Device(alias, d.get("label", alias), match, self.adb, root))
        unknown: dict[str, str] = {}
        for s, i in ids.items():
            if i and i not in taken and i not in unknown.values():
                unknown[s] = i
        for s, i in unknown.items():
            if quiet:
                continue
            dev = self.adopt(s, i)
            if dev:
                res.append(dev)
        return res

    def adopt(self, serial: str, dev_id: str) -> Device | None:
        """Offer to name a device we haven't seen before."""
        model = self.adb.run(["-s", serial, "exec-out", "getprop ro.product.model"]).stdout.decode(errors="replace").strip()
        lookalike = [d.get("label", a) for a, d in self.cfg["devices"].items()
                     if serial in (d["serial"] if isinstance(d["serial"], list) else [d["serial"]])]
        say(f"\nNové zařízení: {model} ({serial})")
        if lookalike:
            say(f"  Má stejné adb jméno jako '{lookalike[0]}', ale je to JINÉ zařízení.")
        if self.yes:
            say("  Přeskakuju (pojmenovat ho jde jen bez --yes).")
            return None
        while True:
            name = prompt("Jak ho pojmenovat? (krátce bez mezer, např. pc2; Enter = přeskočit): ").lower()
            if not name:
                return None
            if not re.fullmatch(r"[a-z0-9_-]{1,20}", name):
                say("  Jen malá písmena, čísla, - a _.")
            elif name in self.cfg["devices"]:
                say(f"  '{name}' už existuje.")
            else:
                break
        label = prompt(f"Popisek, který se bude ukazovat [{model}]: ") or model
        self.cfg["devices"][name] = {"label": label, "serial": [serial], "id": dev_id}
        self.save_config()
        self.log(f"adopted {serial} as {name}")
        say(f"Uloženo jako '{name}'.")
        return Device(name, label, serial, self.adb, self.app["root"])

    def save_config(self) -> None:
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cfg, indent=1, ensure_ascii=False) + "\n", "utf-8")
        os.replace(tmp, CONFIG_FILE)

    def device(self, alias: str) -> Device:
        if alias not in self.cfg["devices"]:
            raise TsyncError(f"neznámé zařízení '{alias}' (známá: {', '.join(self.cfg['devices'])})")
        for d in self.connected(quiet=True):
            if d.alias == alias:
                return d
        raise TsyncError(f"{self.cfg['devices'][alias].get('label', alias)} není připojené")


def device_id(adb: Adb, serial: str) -> str | None:
    """Stable identity of a device: Android ID + hardware serial."""
    if os.environ.get("TSYNC_TEST_IDS") == "serial":  # tests: two serials of one emulator = two devices
        return serial
    p = adb.run(["-s", serial, "exec-out", "settings get secure android_id; getprop ro.serialno"], timeout=20)
    parts = p.stdout.decode(errors="replace").split()
    if p.returncode != 0 or not parts or parts[0] in ("null", ""):
        return None
    return "/".join(parts[:2])


@functools.lru_cache(maxsize=1)
def find_git() -> str:
    """git from PATH, or Git for Windows' default locations (a fresh install
    isn't on PATH until a new console is opened)."""
    candidates = [shutil.which("git"),
                  Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/cmd/git.exe",
                  Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Git/cmd/git.exe"]
    for c in candidates:
        if c and Path(c).is_file():
            return str(c)
    raise TsyncError("Git nenalezen – spusť setup.bat")


def find_adb(cfg: dict) -> str:
    # Deliberately no fallback to BlueStacks' HD-Adb: it can't reach USB devices,
    # so the tablet would silently look disconnected.
    candidates = [cfg.get("adb") and HERE / cfg["adb"], shutil.which("adb"),
                  HERE / "platform-tools" / "adb.exe",
                  Path(os.environ.get("LOCALAPPDATA", "")) / "Android/Sdk/platform-tools/adb.exe"]
    for c in candidates:
        if c and Path(c).is_file():
            return str(c)
    raise TsyncError("adb nenalezeno – spusť setup.bat")


# ------------------------------------------------------------- operations

def wait_until_saved(ctx: Ctx, dev: Device, timeout: int = 30) -> None:
    """Poll the save files until they stop changing."""
    prev, deadline = None, time.time() + timeout
    time.sleep(2)
    while time.time() < deadline:
        cur = {p: f.sha for p, f in list_files(dev, dev.root, ctx.exclude).items()}
        if cur == prev:
            return
        prev = cur
        time.sleep(3)
    raise TsyncError(f"{dev}: save se pořád mění, hra asi pořád zapisuje")


def ensure_closed(ctx: Ctx, dev: Device, required: bool) -> bool:
    """Make sure the app isn't running. Returns True if it is closed."""
    pkg = ctx.app["package"]
    if not dev.is_running(pkg):
        return True
    say(f"{dev}: {ctx.app['label']} běží.")
    if not ctx.ask("Mám ji uložit a zavřít? (pošlu Home, počkám až dopíše save, pak ji ukončím)"):
        if required:
            raise TsyncError(f"{dev}: hra musí být při syncu zavřená")
        return False
    dev.sh("input keyevent 3")  # HOME -> the game gets onPause and saves
    wait_until_saved(ctx, dev)
    dev.sh(f"am force-stop {shq(pkg)}")
    time.sleep(1)
    if dev.is_running(pkg):
        raise TsyncError(f"{dev}: hru se nepodařilo ukončit")
    ctx.log(f"{dev.alias}: closed {pkg}")
    say(f"{dev}: hra uložena a zavřená.")
    return True


def take_snapshot(ctx: Ctx, dev: Device, title: str, running: bool = False) -> Snap:
    """Pull the device's save into the store and verify the stored copy."""
    listing = list_files(dev, dev.root, ctx.exclude)
    if not any(ctx.is_synced(p) for p in listing):
        raise TsyncError(f"{dev}: nenašel jsem žádný save v {dev.root}")
    version = dev.app_version(ctx.app["package"])
    offset = dev.clock() - int(time.time())
    with tempfile.TemporaryDirectory(prefix="tsync-pull-") as tmp:
        tmp_path = Path(tmp)
        dev.pull(sorted({f"{dev.root}/{p.split('/', 1)[0]}" for p in listing}), tmp_path)
        for p, info in listing.items():
            f = tmp_path / p
            if not f.is_file():
                raise TsyncError(f"{dev}: {p} se nestáhl")
            if sha256_bytes(f.read_bytes()) != info.sha:
                raise TsyncError(f"{dev}: {p} se při stahování poškodil (nebo se zrovna měnil)")
        extra = [p for p in (f.relative_to(tmp_path).as_posix() for f in tmp_path.rglob("*") if f.is_file())
                 if p not in listing and ctx.kind(p) is not None]
        if extra:
            raise TsyncError(f"{dev}: stažené soubory nesedí s výpisem: {summarize(extra, 5)}")
        manifest = {
            "v": 1, "app": ctx.app_name, "device": dev.alias, "label": dev.label,
            "serial": dev.serial, "taken_at": now_iso(), "note": title,
            "app_version": version, "clock_offset": offset, "app_running": running,
            "files": {p: [i.size, i.mtime, i.sha] for p, i in sorted(listing.items())},
        }
        commit, created = ctx.store.commit(dev.alias, tmp_path, manifest, title)
    ctx.store.verify(commit)
    if not created:
        manifest = ctx.store.manifest(commit)
    ctx.log(f"{dev.alias}: snapshot {commit[:10]} {'new' if created else 'unchanged'} ({title})")
    files = ctx.sync_files(manifest)
    say(f"{dev}: záloha OK – {len(files)} souborů, {human(sum(v[0] for v in files.values()))}, "
        f"snapshot {commit[:8]} {'(nový)' if created else '(beze změny od minula)'}")
    return Snap(dev.alias, commit, manifest, created)


def newest(ctx: Ctx, manifest: dict) -> float:
    """Newest save-file mtime, corrected for the device clock's offset."""
    files = ctx.sync_files(manifest)
    return max((v[1] for v in files.values()), default=0) - manifest.get("clock_offset", 0)


def changed_locations(ctx: Ctx, a: dict, b: dict) -> list[str]:
    fa, fb = ctx.sync_files(a), ctx.sync_files(b)
    return sorted({ctx.area(p) for p in set(fa) | set(fb)
                   if (fa.get(p) or [0, 0, None])[2] != (fb.get(p) or [0, 0, None])[2]})


def shrink_warnings(ctx: Ctx, src: dict, dst: dict) -> list[str]:
    fs, fd = ctx.sync_files(src), ctx.sync_files(dst)
    warns = []
    # Only whole locations count; small state dirs come and go between game versions.
    ld = ctx.app.get("locations_dir")
    world = (lambda p: p.split("/", 1)[0] == ld) if ld else (lambda p: True)
    missing = {ctx.area(p) for p in fd if world(p)} - {ctx.area(p) for p in fs if world(p)}
    if missing:
        warns.append(f"zdroji chybí lokace, které cíl má: {summarize(missing)}")
    vs, vd = src.get("app_version"), dst.get("app_version")
    if vs and vd and vs[0] > vd[0]:
        warns.append(f"save je z novější verze hry ({vs[1]}) než je na cíli ({vd[1]}) – "
                     "starší hra ho nemusí umět načíst, nejdřív hru na cíli aktualizuj")
    size_s, size_d = sum(v[0] for v in fs.values()), sum(v[0] for v in fd.values())
    if size_d and size_s < SHRINK_RATIO * size_d:
        warns.append(f"zdrojový save je výrazně menší ({human(size_s)} vs {human(size_d)}) – "
                     "nejde o čerstvě nainstalovanou/resetovanou hru?")
    return warns


class StagingError(TsyncError):
    """Failure before the device's save was touched."""


def write_save(ctx: Ctx, dev: Device, commit: str, expected: dict | None) -> None:
    """Make dev's synced files exactly equal to the snapshot `commit`.

    Transfer goes to a staging dir on the device and is verified first; only
    then an on-device script overwrites the files in place (keeping their
    inodes and ownership) and removes files the snapshot doesn't have.
    Raises StagingError if it failed before touching the save."""
    man = ctx.store.manifest(commit)
    want = ctx.sync_files(man)
    remote_stage = f"{dev.root}/{REMOTE_STAGE}"
    dirs = sorted({str(PurePosixPath(p).parent) for p in want} - {"."})
    with tempfile.TemporaryDirectory(prefix="tsync-push-") as tmp:
        try:
            stage = Path(tmp) / "in"
            ctx.store.export(commit, stage, sorted(want))
            current = {p: f for p, f in list_files(dev, dev.root, ctx.exclude).items() if ctx.is_synced(p)}
            if expected is not None and {p: f.sha for p, f in current.items()} != \
                    {p: v[2] for p, v in ctx.sync_files(expected).items()}:
                raise TsyncError(f"{dev}: save se od zálohy změnil (běží hra?)")
            # Old adbd can't create missing dirs on /sdcard itself.
            mk = " ".join(shq(f"{remote_stage}/in/{d}") for d in dirs)
            dev.sh(f"rm -rf {shq(remote_stage)} && mkdir -p {shq(remote_stage + '/in')} {mk}")
            by_dir: dict[str, list[Path]] = {}
            for p in sorted(want):
                parent = str(PurePosixPath(p).parent)
                by_dir.setdefault(parent, []).append(stage / p)
            for d, files in by_dir.items():
                dev.push(files, f"{remote_stage}/in" + ("" if d == "." else f"/{d}"))
            staged = list_files(dev, f"{remote_stage}/in")
            if {p: f.sha for p, f in staged.items()} != {p: v[2] for p, v in want.items()}:
                raise TsyncError(f"{dev}: nahrané soubory nesedí")
        except (TsyncError, OSError) as e:
            raise StagingError(f"{e} – save jsem nepřepsal") from e
        except KeyboardInterrupt as e:
            raise StagingError("přerušeno – save jsem nepřepsal") from e

        # Ignore hangup so a pulled cable can't stop the script half-way.
        lines = ["trap '' HUP INT PIPE", "set -e", f"cd {shq(dev.root)}"]
        lines += [f"mkdir -p {shq(d)}" for d in dirs]
        lines += [f"cat {shq(f'{REMOTE_STAGE}/in/{p}')} > {shq(p)}" for p in sorted(want)]
        lines += [f"rm -f {shq(p)}" for p in sorted(set(current) - set(want))]
        if os.environ.pop("TSYNC_TEST_FAULT", None) == "corrupt_after_apply":  # one-shot, for tests
            lines.append(f"echo tsync-test-fault >> {shq(sorted(want)[0])}")
        script = Path(tmp) / "apply.sh"
        script.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
        dev.push([script], remote_stage)
        dev.sh(f"sh {shq(remote_stage + '/apply.sh')}")

    after = {p: f for p, f in list_files(dev, dev.root, ctx.exclude).items() if ctx.is_synced(p)}
    if {p: f.sha for p, f in after.items()} != {p: v[2] for p, v in want.items()}:
        raise TsyncError(f"{dev}: kontrola po zápisu nesedí")
    owners = {(f.uid, f.gid) for p, f in current.items()}
    odd = [p for p in set(want) - set(current) if owners and (after[p].uid, after[p].gid) not in owners]
    if odd:
        say(f"! {dev}: nové soubory mají jiného vlastníka než původní ({summarize(odd, 3)}); "
            "když je hra neuvidí, obnov zálohu přes `tsync restore`.")
    dev.sh(f"rm -rf {shq(remote_stage)}")


def do_push(ctx: Ctx, src: Snap, dev: Device, backup: Snap, title: str) -> Snap:
    """Write snapshot `src` to `dev`, whose current state is `backup`."""
    if ctx.cfg["devices"][dev.alias].get("write_protected"):
        raise TsyncError(f"{dev} je chráněné proti zápisu (\"write_protected\" v devices.json) – "
                         f"zálohy fungují, zápis ne")
    warns = shrink_warnings(ctx, src.manifest, backup.manifest)
    if warns:
        say("\n!!! POZOR !!!")
        for w in warns:
            say(f"  - {w}")
        if not ctx.confirm_word(f"Opravdu přepsat save na {dev}? (záloha zůstane v {backup.commit[:8]})"):
            raise TsyncError("zrušeno")
    ensure_closed(ctx, dev, required=True)
    ctx.log(f"{dev.alias}: writing {src.commit[:10]} (backup {backup.commit[:10]}) - {title}")
    try:
        write_save(ctx, dev, src.commit, backup.manifest)
    except StagingError as e:
        ctx.log(f"{dev.alias}: staging failed, save untouched: {e}")
        raise
    except (TsyncError, OSError, subprocess.SubprocessError, KeyboardInterrupt) as e:
        ctx.log(f"{dev.alias}: write failed: {e}; rolling back to {backup.commit[:10]}")
        say(f"\n{dev}: zápis selhal ({e})\nVracím save do stavu ze zálohy {backup.commit[:8]}…")
        try:
            write_save(ctx, dev, backup.commit, None)
        except (TsyncError, OSError, subprocess.SubprocessError) as e2:
            ctx.log(f"{dev.alias}: ROLLBACK FAILED: {e2}")
            raise TsyncError(f"ROLLBACK SELHAL ({e2}). Záloha je bezpečně v úložišti – "
                             f"obnov ji: python tsync.py restore {dev.alias} {backup.commit[:10]}")
        ctx.log(f"{dev.alias}: rolled back to {backup.commit[:10]}")
        raise TsyncError(f"{dev}: nic se nezměnilo, save je vrácený na zálohu {backup.commit[:8]}")
    after = take_snapshot(ctx, dev, title)
    if ctx.sync_id(after.manifest) != ctx.sync_id(src.manifest):
        raise TsyncError(f"{dev}: po zápisu se save liší od zdroje – obnov zálohu {backup.commit[:8]}")
    ctx.log(f"{dev.alias}: now at {after.commit[:10]} (= {src.commit[:10]})")
    return after


def set_base(ctx: Ctx, snap: Snap, devices: list[str]) -> None:
    state = ctx.store.load_state()
    state["bases"][pair_key(*devices)] = {"sync_id": ctx.sync_id(snap.manifest), "commit": snap.commit,
                                          "devices": sorted(devices), "at": now_iso()}
    ctx.store.save_state(state)


def get_base(ctx: Ctx, a: str, b: str) -> dict | None:
    return ctx.store.load_state()["bases"].get(pair_key(a, b))


def describe_side(ctx: Ctx, snap: Snap, base_man: dict | None, dev: Device) -> str:
    when = fmt_time(newest(ctx, snap.manifest))
    if base_man is None:
        files = ctx.sync_files(snap.manifest)
        locs = {ctx.area(p) for p in files if p.split("/", 1)[0] == ctx.app.get("locations_dir")}
        return (f"{dev}: {len(locs)} lokací, {human(sum(v[0] for v in files.values()))}, "
                f"poslední změna {when}")
    changed = changed_locations(ctx, base_man, snap.manifest)
    if not changed:
        return f"{dev}: beze změny od posledního syncu"
    return f"{dev}: ZMĚNĚNO od posledního syncu (poslední změna {when}) – {summarize(changed)}"


# ---------------------------------------------------------------- commands

def cmd_sync(ctx: Ctx, args, dry: bool = False) -> int:
    devs = ctx.connected()
    if not devs:
        raise TsyncError("není připojené žádné známé zařízení (viz `python tsync.py devices`)")
    if len(devs) > 2:
        raise TsyncError("připojená jsou víc než 2 zařízení; použij `push ODKUD KAM`")
    snaps = {}
    for d in devs:
        if dry:
            running = d.is_running(ctx.app["package"])
            if running:
                say(f"! {d}: hra běží, snapshot nemusí obsahovat poslední změny")
        else:
            running = not ensure_closed(ctx, d, required=True)
        snaps[d.alias] = take_snapshot(ctx, d, "status" if dry else "před syncem", running=running)
    if len(devs) == 1:
        say(f"\nPřipojené je jen {devs[0]} – udělal jsem zálohu. Na sync připoj i druhé zařízení.")
        return 0

    a, b = devs
    sa, sb = snaps[a.alias], snaps[b.alias]
    base = get_base(ctx, a.alias, b.alias)
    base_man = ctx.store.manifest(base["commit"]) if base else None
    ida, idb = ctx.sync_id(sa.manifest), ctx.sync_id(sb.manifest)
    say("")
    if ida == idb:
        say("Obě zařízení mají stejný save. Není co synchronizovat.")
        if not dry and (not base or base["sync_id"] != ida):
            set_base(ctx, sa, [a.alias, b.alias])
        return 0
    say(describe_side(ctx, sa, base_man, a))
    say(describe_side(ctx, sb, base_man, b))
    diff = changed_locations(ctx, sa.manifest, sb.manifest)
    say(f"Liší se: {summarize(diff)}")

    newer_first = (a, sa, b, sb) if newest(ctx, sa.manifest) >= newest(ctx, sb.manifest) else (b, sb, a, sa)
    if base and ida == base["sync_id"]:
        plan, kind = (b, sb, a, sa), "one"
    elif base and idb == base["sync_id"]:
        plan, kind = (a, sa, b, sb), "one"
    else:
        plan, kind = newer_first, "first" if not base else "conflict"

    src_dev, src, dst_dev, dst = plan
    if kind == "one":
        say(f"\nNávrh: {src_dev} → {dst_dev}  (změny jsou jen na {src_dev})")
    elif kind == "first":
        # No common history: the newer save may well be a fresh install, so no proposal.
        src_dev, src, dst_dev, dst = a, sa, b, sb
        say(f"\nTahle dvě zařízení se ještě nesynchronizovala. Vyber směr sám "
            f"(to, odkud se kopíruje, přepíše to druhé).")
    else:
        say(f"\nKONFLIKT: měnilo se na obou zařízeních. Novější změny má {src_dev}. "
            f"Save se kopíruje celý, změny druhého zařízení se přepíšou (ale zůstanou v záloze).")
    if dry:
        return 0

    if kind == "one":
        if not ctx.ask(f"Zkopírovat save {src_dev} → {dst_dev}?"):
            say("Nic jsem nezměnil. Zálohy jsou uložené.")
            return 0
    else:
        idx = ctx.choose("Co udělat?", [f"{src_dev} → {dst_dev}", f"{dst_dev} → {src_dev}", "nic (jen nechat zálohy)"])
        if idx == 2:
            say("Nic jsem nezměnil. Zálohy jsou uložené.")
            return 0
        if idx == 1:
            src_dev, src, dst_dev, dst = dst_dev, dst, src_dev, src

    after = do_push(ctx, src, dst_dev, dst, f"po syncu z {src_dev.alias}")
    set_base(ctx, after, [a.alias, b.alias])
    ctx.store.git("gc", "--quiet")
    say(f"\nHotovo: {dst_dev} má teď save z {src_dev}. Předchozí stav {dst_dev} je v záloze "
        f"{dst.commit[:8]} (python tsync.py restore {dst_dev.alias} {dst.commit[:8]}).")
    return 0


def cmd_status(ctx: Ctx, args) -> int:
    return cmd_sync(ctx, args, dry=True)


def cmd_backup(ctx: Ctx, args) -> int:
    devs = [ctx.device(a) for a in args.device] if args.device else ctx.connected()
    if not devs:
        raise TsyncError("není připojené žádné známé zařízení")
    for d in devs:
        closed = ensure_closed(ctx, d, required=False)
        take_snapshot(ctx, d, "záloha", running=not closed)
    return 0


def cmd_log(ctx: Ctx, args) -> int:
    synced = {b["sync_id"] for b in ctx.store.load_state()["bases"].values()}
    for dev in [args.device] if args.device else ctx.store.branches():
        commits = ctx.store.history(dev, args.n + 1)
        if not commits:
            say(f"{dev}: žádné snapshoty")
            continue
        label = ctx.cfg["devices"].get(dev, {}).get("label", dev)
        say(f"\n{label} ({dev}):")
        mans = [ctx.store.manifest(c) for c in commits]
        for i, (c, m) in enumerate(zip(commits[:args.n], mans)):
            files = ctx.sync_files(m)
            prev = mans[i + 1] if i + 1 < len(mans) else None
            ch = changed_locations(ctx, prev, m) if prev else ["(první snapshot)"]
            mark = " *sync*" if ctx.sync_id(m) in synced else ""
            say(f"  {c[:8]}  {m['taken_at'][:16].replace('T', ' ')}  {len(files):3d} souborů "
                f"{human(sum(v[0] for v in files.values())):>9}  {m.get('note', '')}{mark}")
            say(f"            změny: {summarize(ch, 6) if ch else '-'}")
    return 0


def cmd_push(ctx: Ctx, args) -> int:
    src_dev, dst_dev = ctx.device(args.src), ctx.device(args.dst)
    ensure_closed(ctx, src_dev, required=True)
    ensure_closed(ctx, dst_dev, required=True)  # so the backup has its latest state
    src = take_snapshot(ctx, src_dev, "před pushem")
    dst = take_snapshot(ctx, dst_dev, "před pushem")
    if ctx.sync_id(src.manifest) == ctx.sync_id(dst.manifest):
        say("Save je na obou stejný, není co kopírovat.")
        return 0
    say(f"Liší se: {summarize(changed_locations(ctx, src.manifest, dst.manifest))}")
    if not ctx.ask(f"Přepsat save na {dst_dev} savem z {src_dev}?", default=False):
        return 0
    after = do_push(ctx, src, dst_dev, dst, f"po pushi z {src_dev.alias}")
    set_base(ctx, after, [src_dev.alias, dst_dev.alias])
    ctx.store.git("gc", "--quiet")
    say(f"Hotovo. Předchozí stav {dst_dev}: {dst.commit[:8]}")
    return 0


def cmd_restore(ctx: Ctx, args) -> int:
    commit = ctx.store.resolve(args.snapshot)
    man = ctx.store.manifest(commit)
    dev = ctx.device(args.device)
    say(f"Snapshot {commit[:8]}: {man.get('label', man['device'])}, {man['taken_at'][:16].replace('T', ' ')} "
        f"({man.get('note', '')})")
    ensure_closed(ctx, dev, required=True)
    backup = take_snapshot(ctx, dev, "před obnovou")
    if ctx.sync_id(backup.manifest) == ctx.sync_id(man):
        say(f"{dev} už tenhle save má.")
        return 0
    say(f"Liší se: {summarize(changed_locations(ctx, backup.manifest, man))}")
    if not ctx.ask(f"Obnovit tento snapshot na {dev}?", default=False):
        return 0
    do_push(ctx, Snap(man["device"], commit, man), dev, backup, f"obnoveno z {commit[:8]}")
    say(f"Hotovo. Stav před obnovou: {backup.commit[:8]}")
    return 0


def cmd_export(ctx: Ctx, args) -> int:
    commit = ctx.store.resolve(args.snapshot)
    dest = Path(args.dir)
    if dest.exists() and any(dest.iterdir()):
        raise TsyncError(f"{dest} není prázdná složka")
    files = sorted(ctx.store.manifest(commit)["files"])
    ctx.store.export(commit, dest, files)
    say(f"Snapshot {commit[:8]} vyexportován do {dest} ({len(files)} souborů)")
    return 0


def cmd_devices(ctx: Ctx, args) -> int:
    say(f"adb: {ctx.adb.exe}")
    for d in ctx.connected():
        rc, model = d.sh("getprop ro.product.model; getprop ro.build.version.release", check=False)
        running = d.is_running(ctx.app["package"])
        try:
            version = d.app_version(ctx.app["package"])[1]
        except TsyncError:
            version = "nenainstalováno"
        say(f"  {d.alias:8} {d.serial:18} {' / Android '.join(model.strip().splitlines())}  "
            f"{ctx.app['label']} {version}{'  (běží)' if running else ''}")
    return 0


def cmd_verify(ctx: Ctx, args) -> int:
    ctx.store.git("fsck", "--full", "--strict")
    n = 0
    for dev in ctx.store.branches():
        for c in ctx.store.history(dev, 100000):
            ctx.store.verify(c)
            n += 1
    say(f"Úložiště je v pořádku ({n} snapshotů ověřeno).")
    return 0


BLUESTACKS_CONF = Path(os.environ.get("ProgramData", "C:/ProgramData")) / "BlueStacks_nxt/bluestacks.conf"


def cmd_doctor(ctx: Ctx, args) -> int:
    """Read-only check of everything sync needs, with a hint for each problem."""
    problems = []

    def check(good: bool, text: str, hint: str = "") -> bool:
        say(f"  [{'OK' if good else '!!'}] {text}")
        if not good:
            problems.append(hint or text)
        return good

    say("Nástroje:")
    check(True, f"git: {find_git()}")
    check(True, f"adb: {ctx.adb.exe}")

    if os.name == "nt":
        say("BlueStacks:")
        if BLUESTACKS_CONF.is_file():
            conf = BLUESTACKS_CONF.read_text("utf-8", errors="replace")
            check('bst.enable_adb_access="1"' in conf, "ADB v BlueStacks zapnuté",
                  "V BlueStacks zapni Nastavení → Pokročilé → Android Debug Bridge (ADB)")
        else:
            say("  [--] BlueStacks není nainstalovaný (nevadí, pokud ho tady nepoužíváš)")

    say("Zařízení:")
    devs = ctx.connected()
    if not devs:
        check(False, "žádné známé zařízení není připojené",
              "Zapni BlueStacks a/nebo připoj tablet USB kabelem (s povoleným USB laděním)")
    versions = {}
    for d in devs:
        try:
            versions[d.alias] = d.app_version(ctx.app["package"])
        except TsyncError:
            check(False, f"{d}: {ctx.app['label']} není nainstalovaná", f"Nainstaluj {ctx.app['label']} na {d}")
            continue
        has_data = d.sh(f"[ -d {shq(d.root)} ]", check=False)[0] == 0
        check(has_data, f"{d}: {ctx.app['label']} {versions[d.alias][1]}"
              + ("" if has_data else " – zatím nemá žádná data"),
              f"Na {d} jednou spusť {ctx.app['label']}, ať si vytvoří save")
    if len({v[0] for v in versions.values()}) > 1:
        check(False, "verze hry na zařízeních se liší: "
              + ", ".join(f"{ctx.cfg['devices'][a].get('label', a)} {v[1]}" for a, v in versions.items()),
              "Aktualizuj hru na všech zařízeních na stejnou verzi (Play Store)")
    offline = [d.get("label", a) for a, d in ctx.cfg["devices"].items()
               if not d.get("ignore") and a not in {x.alias for x in devs}]
    if offline:
        say(f"  [--] nepřipojené: {', '.join(offline)}")

    say("Zálohy:")
    n = sum(len(ctx.store.history(b, 100000)) for b in ctx.store.branches())
    ctx.store.git("fsck", "--strict")
    check(True, f"úložiště v pořádku, {n} snapshotů ({ctx.store.git_dir})")

    if problems:
        say("\nCo je potřeba udělat:")
        for p in problems:
            say(f"  - {p}")
        return 1
    say("\nVšechno je připravené, můžeš spustit sync.bat.")
    return 0


COMMANDS = {"sync": cmd_sync, "status": cmd_status, "backup": cmd_backup, "log": cmd_log,
            "push": cmd_push, "restore": cmd_restore, "export": cmd_export,
            "devices": cmd_devices, "verify": cmd_verify, "doctor": cmd_doctor}


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="tsync", description="Sync savů mezi zařízeními, vždy se zálohou.")
    ap.add_argument("--app", help="profil z apps/ (výchozí: jediný existující)")
    ap.add_argument("-y", "--yes", action="store_true", help="na otázky odpovídat ano")
    ap.add_argument("--force", action="store_true", help="povolit i podezřelé přepsání (zmenšení savu)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("sync", help="zálohovat obě zařízení a navrhnout sync (výchozí)")
    sub.add_parser("status", help="zálohovat a ukázat, co by sync udělal")
    p = sub.add_parser("backup", help="jen zálohovat")
    p.add_argument("device", nargs="*")
    p = sub.add_parser("log", help="historie snapshotů")
    p.add_argument("device", nargs="?")
    p.add_argument("-n", type=int, default=15)
    p = sub.add_parser("push", help="ručně zkopírovat save ODKUD KAM")
    p.add_argument("src")
    p.add_argument("dst")
    p = sub.add_parser("restore", help="nahrát snapshot na zařízení")
    p.add_argument("device")
    p.add_argument("snapshot", help="id snapshotu z `log`, nebo např. tablet~1")
    p = sub.add_parser("export", help="vybalit snapshot do složky")
    p.add_argument("snapshot")
    p.add_argument("dir")
    sub.add_parser("devices", help="připojená zařízení")
    sub.add_parser("verify", help="zkontrolovat integritu úložiště")
    sub.add_parser("doctor", help="zkontrolovat, že je vše připravené (nic nemění)")
    args = ap.parse_args(argv)

    app = args.app
    if not app:
        profiles = sorted(p.stem for p in APPS_DIR.glob("*.json"))
        if len(profiles) != 1:
            say(f"Vyber aplikaci přes --app ({', '.join(profiles)})")
            return 2
        app = profiles[0]
    try:
        ctx = Ctx(app, args.yes, args.force)
        return COMMANDS[args.cmd or "sync"](ctx, args)
    except TsyncError as e:
        say(f"\nCHYBA: {e}")
        return 1
    except KeyboardInterrupt:
        say("\nPřerušeno.")
        return 130


if __name__ == "__main__":
    sys.exit(main())

"""DotAbyss desktop backend: static player + translation/LLM proxy + one-click
download/decrypt/extract/update orchestration.

Runs as the sidecar behind the Tauri shell (or standalone for dev). Everything the
client does at runtime goes through here:

  * Serves the static AdvPlayer, the extracted data dir (``/data/``), and the
    downloaded community translations (``/translations/``).
  * ``/api/translate`` + ``/api/llm-config`` — optional user-key LLM fill-in (unchanged
    behaviour, cache/config now live in the data dir).
  * ``/api/setup`` / ``/api/progress`` / ``/api/update/*`` / ``/api/repair`` — drive the
    streaming pipeline (tools/pipeline.py): C# downloader ``--dry-run`` produces the
    manifest, then Python streams download -> extract -> decode -> clean.

Data dir resolution (writable, ~4 GB, configurable — keeps C: from filling up):
  1. ``$DOTABYSS_DATA_DIR`` (the Tauri shell sets this explicitly)
  2. portable: ``<repo>/portable.txt`` present -> ``<repo>/data-store``
  3. default: ``%LOCALAPPDATA%/DotAbyssPlayer/data``
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlparse

import http.server
import socketserver

REPO_ROOT = Path(__file__).resolve().parents[1]

# When frozen with PyInstaller, static assets + tool modules are bundled under _MEIPASS;
# in dev they live in the source tree.
FROZEN = getattr(sys, "frozen", False)
BUNDLE_BASE = Path(getattr(sys, "_MEIPASS", str(REPO_ROOT))) if FROZEN else REPO_ROOT
PLAYER_ROOT = (BUNDLE_BASE / "AdvPlayer") if FROZEN else (REPO_ROOT / "src" / "AdvPlayer")
TOOLS_DIR = BUNDLE_BASE if FROZEN else (REPO_ROOT / "tools")

# pipeline lives in tools/ (dev) or bundled top-level (frozen); import it for orchestration.
sys.path.insert(0, str(TOOLS_DIR))
import pipeline  # noqa: E402

TRANSLATIONS_REPO = "s88037zz/dotabyss-translation"

_cache_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Paths / config
# --------------------------------------------------------------------------- #
def resolve_data_dir() -> Path:
    env = os.environ.get("DOTABYSS_DATA_DIR")
    if env:
        return Path(env)
    if (REPO_ROOT / "portable.txt").exists():
        return REPO_ROOT / "data-store"
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "DotAbyssPlayer" / "data"


DATA_DIR = resolve_data_dir()
TRANSLATIONS_DIR = DATA_DIR / "translations"
LLM_CONFIG_PATH = DATA_DIR / "llm.json"
CACHE_ROOT = DATA_DIR / "llm_cache"
WORK_DIR = DATA_DIR / "_download"                       # manifest + catalog cache
MANIFEST_PATH = WORK_DIR / "download_manifest.tsv"
CATALOG_JSON = WORK_DIR / "_catalog" / "catalog_1.extracted.json"
DEFAULT_PROFILE = "android-dmm-r18"


def _ensure_dirs():
    for d in (DATA_DIR, TRANSLATIONS_DIR, CACHE_ROOT, WORK_DIR):
        d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# LLM config + Chat Completions (preserved from the original serve)
# --------------------------------------------------------------------------- #
def load_llm_config() -> dict:
    try:
        if LLM_CONFIG_PATH.exists():
            return json.loads(LLM_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[llm] read {LLM_CONFIG_PATH} failed: {exc}", file=sys.stderr)
    return {}


def save_llm_config(cfg: dict):
    _ensure_dirs()
    tmp = LLM_CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(LLM_CONFIG_PATH)


def _cache_path(prefixed_id: str) -> Path:
    safe = "".join(c for c in prefixed_id if c.isalnum() or c in "_-") or "unknown"
    return CACHE_ROOT / f"{safe}.json"


def load_cache(prefixed_id: str) -> dict:
    path = _cache_path(prefixed_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_cache(prefixed_id: str, data: dict) -> None:
    path = _cache_path(prefixed_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _chat_completions_url(base_url: str) -> str:
    url = (base_url or "").rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return url + "/chat/completions"
    return url + "/v1/chat/completions"


SYSTEM_PROMPT = (
    "你是资深的日语→繁体中文（台湾）视觉小说本地化译者。翻译要求：\n"
    "1. 忠实、自然，符合台湾用语习惯；语气贴合角色。\n"
    "2. 严格保留原文中的所有控制标记（如 <br>、<user>、<size=48>、</size> 等）原样不译、不增删、位置不变。\n"
    "3. 人名、怪物名、专有名词一律使用给定的【角色名对照】；未列出的音译或保留原文。\n"
    "4. 结合【剧情上下文】理解语境，保持人称与术语跨句一致。\n"
    "5. 只翻译【待译句子】列表，不要翻译上下文，不要输出解释。"
)


def _build_messages(batch: list[str], context: dict, max_context_lines: int) -> list[dict]:
    names = context.get("names") or {}
    script = context.get("script") or []
    if max_context_lines and len(script) > max_context_lines:
        script = script[:max_context_lines]
    parts = []
    if names:
        parts.append("【角色名对照】(日 => 繁)\n" + "\n".join(f"{k} => {v}" for k, v in names.items()))
    if script:
        parts.append("【剧情上下文】(全剧台词顺序，仅供理解语境，勿翻译)\n"
                     + "\n".join(f"{i + 1}. {line}" for i, line in enumerate(script)))
    system_content = SYSTEM_PROMPT + ("\n\n" + "\n\n".join(parts) if parts else "")
    user_content = (
        "【待译句子】(JSON 数组)\n" + json.dumps(batch, ensure_ascii=False)
        + "\n\n请只输出一个 JSON 对象：键为上面每个原文句子（逐字一致），值为其繁体中文译文，"
        "必须覆盖数组中的全部句子。"
    )
    return [{"role": "system", "content": system_content}, {"role": "user", "content": user_content}]


def llm_translate_batch(config: dict, batch: list[str], context: dict) -> dict:
    base_url = config.get("base_url") or "https://api.deepseek.com"
    api_key = config.get("api_key") or ""
    model = config.get("model") or "deepseek-v4-flash"
    max_context_lines = int(config.get("max_context_lines", 600))
    payload = {
        "model": model,
        "messages": _build_messages(batch, context, max_context_lines),
        "temperature": config.get("temperature", 0.3),
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    if config.get("max_tokens"):
        payload["max_tokens"] = config["max_tokens"]
    if isinstance(config.get("extra_body"), dict):
        payload.update(config["extra_body"])
    req = urllib.request.Request(
        _chat_completions_url(base_url),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=int(config.get("timeout", 180))) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    parsed = json.loads(data["choices"][0]["message"]["content"])
    if not isinstance(parsed, dict):
        raise ValueError("LLM did not return a JSON object")
    return {k: parsed[k] for k in batch if k in parsed and isinstance(parsed[k], str)}


def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# --------------------------------------------------------------------------- #
# Downloader (C# --dry-run -> manifest) + translations fetch
# --------------------------------------------------------------------------- #
def downloader_cmd() -> list[str]:
    """Resolve the bundled self-contained downloader exe, or fall back to `dotnet run`."""
    exe = os.environ.get("DOTABYSS_DOWNLOADER")
    if exe and Path(exe).exists():
        return [exe]
    packaged = REPO_ROOT / "bin" / ("DotAbyssClient.exe" if os.name == "nt" else "DotAbyssClient")
    if packaged.exists():
        return [str(packaged)]
    return ["dotnet", "run", "-c", "Release", "--project", str(REPO_ROOT / "src" / "DotAbyssClient"), "--"]


def refresh_manifest(profile: str = DEFAULT_PROFILE, version: str | None = None) -> dict:
    """Run the downloader with --dry-run to (re)produce the manifest + catalog. Returns {version}."""
    _ensure_dirs()
    cmd = downloader_cmd() + [
        "download", "--profile", profile, "-o", str(WORK_DIR),
        "--catalog-dir", str(WORK_DIR / "_catalog"),
        "--write-catalog-json", "--overwrite-catalog", "--dry-run",
    ]
    if version:
        cmd += ["--version", version]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    resolved = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("Resolved ") and ":" in line:
            resolved = line.split(":", 1)[1].strip()
    if proc.returncode != 0 and not MANIFEST_PATH.exists():
        raise RuntimeError((proc.stderr or proc.stdout or "downloader failed")[:800])
    return {"version": resolved}


def _github_default_branch() -> str:
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{TRANSLATIONS_REPO}",
            headers={"User-Agent": "DotAbyssPlayer", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())["default_branch"]
    except Exception:  # noqa: BLE001
        return "main"


def translations_remote_commit(branch: str | None = None) -> str | None:
    branch = branch or _github_default_branch()
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{TRANSLATIONS_REPO}/commits/{branch}",
            headers={"User-Agent": "DotAbyssPlayer", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())["sha"]
    except Exception:  # noqa: BLE001
        return None


def fetch_translations() -> str | None:
    """Download the translations repo zip and extract its ``translations/`` tree locally."""
    _ensure_dirs()
    branch = _github_default_branch()
    url = f"https://codeload.github.com/{TRANSLATIONS_REPO}/zip/refs/heads/{branch}"
    req = urllib.request.Request(url, headers={"User-Agent": "DotAbyssPlayer"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        blob = resp.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        members = zf.namelist()
        root = members[0].split("/", 1)[0] if members else ""
        prefix = f"{root}/translations/"
        staging = DATA_DIR / "_translations_new"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        for name in members:
            if not name.startswith(prefix) or name.endswith("/"):
                continue
            rel = name[len(prefix):]
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, dest.open("wb") as out:
                shutil.copyfileobj(src, out)
        if staging.exists():
            if TRANSLATIONS_DIR.exists():
                shutil.rmtree(TRANSLATIONS_DIR, ignore_errors=True)
            staging.replace(TRANSLATIONS_DIR)
    return translations_remote_commit(branch)


# --------------------------------------------------------------------------- #
# Job manager (one background pipeline job at a time)
# --------------------------------------------------------------------------- #
class JobManager:
    def __init__(self):
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.progress = pipeline.Progress(DATA_DIR / ".pipeline_progress.json")
        self.kind = "idle"

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, kind: str, target, *args):
        with self._lock:
            if self.running():
                return False
            self.progress = pipeline.Progress(DATA_DIR / ".pipeline_progress.json")
            self.kind = kind

            def run():
                try:
                    target(self.progress, *args)
                except Exception as exc:  # noqa: BLE001
                    self.progress.error(kind, repr(exc))
                    self.progress.update(phase="error", finished=True, message=str(exc)[:300])

            self._thread = threading.Thread(target=run, daemon=True)
            self._thread.start()
            return True

    def snapshot(self) -> dict:
        snap = self.progress.snapshot()
        snap["kind"] = self.kind
        snap["running"] = self.running()
        return snap


JOBS = JobManager()


def _vgmstream() -> str | None:
    hit = pipeline.find_vgmstream_cli(os.environ.get("DOTABYSS_VGMSTREAM"))
    return str(hit) if hit else None


def _job_setup(progress: pipeline.Progress, profile: str):
    progress.update(phase="planning", message="fetching catalog/manifest")
    info = refresh_manifest(profile)
    pipeline.run_setup(
        MANIFEST_PATH, DATA_DIR,
        vgmstream=_vgmstream(),
        catalog=CATALOG_JSON if CATALOG_JSON.exists() else None,
        progress=progress, version=info.get("version"), profile=profile,
    )
    progress.update(phase="translations", message="fetching translations")
    try:
        commit = fetch_translations()
        state = pipeline.load_state(DATA_DIR)
        state["translationsCommit"] = commit
        pipeline.save_state(DATA_DIR, state)
    except Exception as exc:  # noqa: BLE001
        progress.error("translations", repr(exc))
    progress.update(phase="done", finished=True, message="setup complete")


def _job_update(progress: pipeline.Progress, profile: str):
    progress.update(phase="planning", message="checking versions")
    info = refresh_manifest(profile)
    rows = pipeline.read_manifest(MANIFEST_PATH)
    plan = pipeline.plan_from_manifest(rows)
    state = pipeline.load_state(DATA_DIR)
    known = state.get("stories", {})
    changed = [sid for sid, u in plan.stories.items()
               if known.get(sid, {}).get("scriptHash") != u.script_hash]
    removed = [sid for sid in known if sid not in plan.stories]
    progress.update(phase="stories", total=len(changed), done=0,
                    message=f"{len(changed)} changed, {len(removed)} removed")
    vg = _vgmstream()
    tmp_root = DATA_DIR / "_work"
    for sid in changed:
        try:
            entry = pipeline.process_story(plan.stories[sid], DATA_DIR, tmp_root, vg, MANIFEST_PATH, progress=progress)
            pipeline.upsert_index(DATA_DIR, entry)
            known[sid] = {"scriptHash": plan.stories[sid].script_hash}
        except Exception as exc:  # noqa: BLE001
            progress.error(f"story:{sid}", repr(exc))
        progress.tick(sid)
    for sid in removed:
        shutil.rmtree(DATA_DIR / "stories" / sid, ignore_errors=True)
        known.pop(sid, None)
        _remove_from_index(sid)
    # Version bumped -> refresh shared base + translations too.
    if info.get("version") and info["version"] != state.get("version"):
        pipeline.run_base(plan, DATA_DIR, tmp_root, vg,
                          CATALOG_JSON if CATALOG_JSON.exists() else None, progress)
    try:
        state["translationsCommit"] = fetch_translations()
    except Exception as exc:  # noqa: BLE001
        progress.error("translations", repr(exc))
    state["stories"] = known
    state["version"] = info.get("version") or state.get("version")
    pipeline.save_state(DATA_DIR, state)
    shutil.rmtree(tmp_root, ignore_errors=True)
    progress.update(phase="done", finished=True, message="update complete")


def _job_repair(progress: pipeline.Progress, story_id: str):
    if not MANIFEST_PATH.exists():
        refresh_manifest(DEFAULT_PROFILE)
    pipeline.extract_one(MANIFEST_PATH, DATA_DIR, story_id, vgmstream=_vgmstream(), progress=progress)


def _remove_from_index(story_id: str):
    path = DATA_DIR / "index.json"
    if not path.exists():
        return
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    index["stories"] = [s for s in index.get("stories", []) if s.get("id") != story_id]
    path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- #
# State reporting
# --------------------------------------------------------------------------- #
def compute_state() -> dict:
    story_count = 0
    index = DATA_DIR / "index.json"
    if index.exists():
        try:
            story_count = len(json.loads(index.read_text(encoding="utf-8")).get("stories", []))
        except Exception:  # noqa: BLE001
            pass
    state = pipeline.load_state(DATA_DIR)
    try:
        free = shutil.disk_usage(DATA_DIR if DATA_DIR.exists() else DATA_DIR.parent).free
    except Exception:  # noqa: BLE001
        free = None
    return {
        "dataDir": str(DATA_DIR),
        "installed": story_count > 0,
        "storyCount": story_count,
        "version": state.get("version"),
        "translationsCommit": state.get("translationsCommit"),
        "translationsInstalled": (TRANSLATIONS_DIR / "novels").exists() or any(TRANSLATIONS_DIR.glob("*")),
        "diskFree": free,
        "longPathsOk": _long_paths_ok(),
        "llmEnabled": bool(load_llm_config().get("api_key")),
    }


def _long_paths_ok() -> bool:
    if os.name != "nt":
        return True
    try:
        import winreg  # noqa: PLC0415
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SYSTEM\CurrentControlSet\Control\FileSystem")
        val, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
        return bool(val)
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class AdvPlayerHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PLAYER_ROOT), **kwargs)

    # ---- static path mapping: /data/, /data_r18_all/, /translations/ -> DATA_DIR ----
    def translate_path(self, path: str) -> str:
        pure = unquote(urlparse(path).path)
        for prefix, base in (
            ("/translations/", TRANSLATIONS_DIR),
            ("/data_r18_all/", DATA_DIR),
            ("/data/", DATA_DIR),
        ):
            if pure.startswith(prefix):
                rel = pure[len(prefix):].lstrip("/")
                root = base.resolve()
                target = (root / rel).resolve()
                if target == root or str(target).startswith(str(root) + os.sep):
                    return str(target)
                return str(root)
        return super().translate_path(path)

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/healthz":
            return self._send_json({"ok": True, "dataDir": str(DATA_DIR)})
        if p == "/api/state":
            return self._send_json(compute_state())
        if p == "/api/progress":
            return self._send_json(JOBS.snapshot())
        if p == "/api/llm-config":
            cfg = load_llm_config()
            return self._send_json({"enabled": bool(cfg.get("api_key")),
                                    "model": cfg.get("model") or "",
                                    "base_url": cfg.get("base_url") or ""})
        if p == "/api/update/check":
            return self._handle_update_check()
        return super().do_GET()

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/translate":
            return self._handle_translate()
        if p == "/api/setup":
            body = self._read_json()
            ok = JOBS.start("setup", _job_setup, body.get("profile") or DEFAULT_PROFILE)
            return self._send_json({"started": ok, "busy": not ok})
        if p == "/api/update/apply":
            body = self._read_json()
            ok = JOBS.start("update", _job_update, body.get("profile") or DEFAULT_PROFILE)
            return self._send_json({"started": ok, "busy": not ok})
        if p == "/api/repair":
            body = self._read_json()
            sid = str(body.get("id") or "")
            if not sid:
                return self._send_json({"error": "missing id"}, status=400)
            ok = JOBS.start("repair", _job_repair, sid)
            return self._send_json({"started": ok, "busy": not ok})
        if p == "/api/llm-config":
            body = self._read_json()
            cfg = load_llm_config()
            for k in ("api_key", "base_url", "model", "temperature", "batch_size", "extra_body"):
                if k in body:
                    cfg[k] = body[k]
            save_llm_config(cfg)
            return self._send_json({"enabled": bool(cfg.get("api_key"))})
        return self.send_error(404, "Not Found")

    # ---- handlers -----------------------------------------------------------
    def _handle_update_check(self):
        result = {"resourceUpdate": False, "translationUpdate": False,
                  "currentVersion": None, "newVersion": None}
        state = pipeline.load_state(DATA_DIR)
        result["currentVersion"] = state.get("version")
        try:
            info = refresh_manifest(state.get("profile") or DEFAULT_PROFILE)
            result["newVersion"] = info.get("version")
            result["resourceUpdate"] = bool(info.get("version")) and info["version"] != state.get("version")
        except Exception as exc:  # noqa: BLE001
            result["error"] = str(exc)[:300]
        remote = translations_remote_commit()
        result["translationUpdate"] = bool(remote) and remote != state.get("translationsCommit")
        return self._send_json(result)

    def _handle_translate(self):
        body = self._read_json()
        prefixed_id = str(body.get("prefixed_id") or "unknown")
        items = list(dict.fromkeys(x for x in (body.get("items") or []) if isinstance(x, str) and x))
        context = body.get("context") or {}
        config = load_llm_config()
        with _cache_lock:
            cache = load_cache(prefixed_id)
        translations: dict = {}
        sources: dict = {}
        todo: list[str] = []
        for it in items:
            if it in cache:
                translations[it] = cache[it]
                sources[it] = "cache"
            else:
                todo.append(it)
        errors = []
        if todo and config.get("api_key"):
            newly = {}
            for batch in _chunks(todo, int(config.get("batch_size", 40))):
                try:
                    trans = llm_translate_batch(config, batch, context)
                    for k, v in trans.items():
                        translations[k] = v
                        sources[k] = "llm"
                        newly[k] = v
                except urllib.error.HTTPError as exc:
                    errors.append(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'ignore')[:200]}")
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))
            if newly:
                with _cache_lock:
                    disk = load_cache(prefixed_id)
                    disk.update(newly)
                    save_cache(prefixed_id, disk)
        missing = [it for it in todo if it not in translations]
        self._send_json({"translations": translations, "sources": sources, "missing": missing,
                         "llm_enabled": bool(config.get("api_key")), "errors": errors})

    # ---- helpers ------------------------------------------------------------
    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:  # noqa: BLE001
            return {}

    def _send_json(self, obj: dict, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        if "/api/" in (self.path or ""):
            super().log_message(fmt, *args)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser(description="DotAbyss desktop backend.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--data-dir", default=None, help="Override data dir (else env/portable/localappdata).")
    args = parser.parse_args()

    global DATA_DIR, TRANSLATIONS_DIR, LLM_CONFIG_PATH, CACHE_ROOT, WORK_DIR, MANIFEST_PATH, CATALOG_JSON
    if args.data_dir:
        os.environ["DOTABYSS_DATA_DIR"] = args.data_dir
        DATA_DIR = Path(args.data_dir)
        TRANSLATIONS_DIR = DATA_DIR / "translations"
        LLM_CONFIG_PATH = DATA_DIR / "llm.json"
        CACHE_ROOT = DATA_DIR / "llm_cache"
        WORK_DIR = DATA_DIR / "_download"
        MANIFEST_PATH = WORK_DIR / "download_manifest.tsv"
        CATALOG_JSON = WORK_DIR / "_catalog" / "catalog_1.extracted.json"
    _ensure_dirs()

    server = ThreadingHTTPServer((args.host, args.port), AdvPlayerHandler)
    print(f"Serving {PLAYER_ROOT.as_posix()} at http://{args.host}:{args.port}/")
    print(f"  data dir      -> {DATA_DIR}")
    print(f"  /translations -> {TRANSLATIONS_DIR}")
    print(f"  LLM {'ENABLED: ' + (load_llm_config().get('model') or '?') if load_llm_config().get('api_key') else 'DISABLED (no api_key)'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

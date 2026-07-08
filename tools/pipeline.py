"""Streaming download -> extract -> decode -> cleanup pipeline for the desktop client.

Design (see plan 1-declarative-sprout.md):
  1. The C# downloader is run with --dry-run once; it performs the encrypted Laravel
     catalog/maintenance handshake and writes ``download_manifest.tsv`` (every bundle's
     URL + size + hash) WITHOUT downloading any bundle. The bundles themselves are plain
     (unauthenticated) HTTP GETs, so from here on Python does all the network work.
  2. This module reads the manifest, groups rows into work units (per-novel-story, plus
     shared base groups), and streams each unit: download only that unit's bundles into an
     isolated temp dir -> extract with the proven ``adv_extract.extract_story`` -> decode
     audio with vgmstream -> transcode wav->ogg -> delete the temp bundles + wav + acb so
     only the ~few-MB final assets (ogg/json/png/moc3) survive. Peak disk stays ~5-6 GB
     instead of the ~30 GB batch peak.

The module is importable (the backend server calls :func:`run_setup`, :func:`extract_one`,
:func:`plan_from_manifest`) and also runnable as a CLI for testing/repair.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

# adv_extract lives next to this file; import its proven extraction primitives.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import adv_extract  # noqa: E402
from adv_extract import extract_story, find_vgmstream_cli  # noqa: E402

try:
    # wav -> ogg helpers (soundfile). Optional so `plan`/`--no-audio` still import.
    from convert_wav_audio_to_ogg import (  # noqa: E402
        resolve_relative_audio, update_audio_paths, update_json_files,
    )
    import soundfile as _sf  # noqa: E402
    _HAVE_SF = True
except Exception:  # noqa: BLE001
    _HAVE_SF = False

REPO_ROOT = Path(__file__).resolve().parents[1]
NOVEL_ID_PREFIXES = adv_extract.NOVEL_ID_PREFIXES  # ("evs","hmn","hmr","mas","men")

# .../novel/<cat>/<prefix>/<num>/...  (script + optional per-story l2d subtree)
_NOVEL_RE = re.compile(
    r"(?P<rootrel>.*?/novel/[^/]+/(?P<prefix>%s)/(?P<num>\d{8,}))/"
    % "|".join(NOVEL_ID_PREFIXES),
    re.IGNORECASE,
)
# .../voice/.../<prefix>/<num>.acb|.awb...   (per-story voice, prefix disambiguates hmn/men)
_VOICE_RE = re.compile(
    r"/voice/.*/(?P<prefix>%s)/(?P<num>\d{8,})\.(?:acb|awb)" % "|".join(NOVEL_ID_PREFIXES),
    re.IGNORECASE,
)

# Path substrings identifying the shared base bundle groups the shared extractors consume.
# Matched against the lowercased outputRelativePath, first-match-wins in dict order.
# (Voice sound-cri rows are already claimed by story units before this loop is reached.)
BASE_GROUPS: dict[str, tuple[str, ...]] = {
    "background": ("general-ui-bg-novel", "general-ui-bg-event"),
    "charastand": ("r18-only-charastand", "ui/emotion/charastand"),
    "se": ("general-sound-cri", "r18-only-sound-cri"),
}


# --------------------------------------------------------------------------- #
# Manifest model
# --------------------------------------------------------------------------- #
@dataclass
class BundleRow:
    remote: str
    output: str
    url: str
    size: int
    hash: str
    crc: str


def read_manifest(manifest_path: Path) -> list[BundleRow]:
    rows: list[BundleRow] = []
    with manifest_path.open("r", encoding="utf-8", errors="ignore") as fh:
        header = fh.readline().rstrip("\n").split("\t")
        idx = {name: i for i, name in enumerate(header)}
        for line in fh:
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 3:
                continue

            def col(name: str, default: str = "") -> str:
                i = idx.get(name)
                return cols[i] if i is not None and i < len(cols) else default

            try:
                size = int(col("expectedSize") or "0")
            except ValueError:
                size = 0
            rows.append(BundleRow(
                remote=col("remoteRelativePath"),
                output=col("outputRelativePath") or col("remoteRelativePath"),
                url=col("url"),
                size=size,
                hash=col("hash"),
                crc=col("crc"),
            ))
    return rows


@dataclass
class StoryUnit:
    story_id: str          # e.g. "hmr_10010100012"
    prefix: str
    num: str
    root_rel: str          # manifest-relative folder that is the extract_story root
    rows: list[BundleRow] = field(default_factory=list)
    script_hash: str = ""  # hash of the .txt script bundle, for update diffing


@dataclass
class Plan:
    stories: dict[str, StoryUnit]
    base: dict[str, list[BundleRow]]


def plan_from_manifest(rows: list[BundleRow]) -> Plan:
    """Group manifest rows into per-story units and shared base groups."""
    stories: dict[str, StoryUnit] = {}
    base: dict[str, list[BundleRow]] = {name: [] for name in BASE_GROUPS}

    for row in rows:
        out = row.output.replace("\\", "/")
        low = out.lower()

        m = _NOVEL_RE.match(out)
        if m and "/novel/" in low:
            prefix = m.group("prefix").lower()
            num = m.group("num")
            sid = f"{prefix}_{num}"
            unit = stories.get(sid)
            if unit is None:
                unit = StoryUnit(story_id=sid, prefix=prefix, num=num, root_rel=m.group("rootrel"))
                stories[sid] = unit
            unit.rows.append(row)
            if re.search(r"\.txt_[0-9a-f]+\.bundle$", low):
                unit.script_hash = row.hash
            continue

        v = _VOICE_RE.search(out)
        if v and "sound" in low:
            prefix = v.group("prefix").lower()
            num = v.group("num")
            sid = f"{prefix}_{num}"
            unit = stories.get(sid)
            if unit is None:
                # Voice may be parsed before the script row; back-fill root later on merge.
                unit = StoryUnit(story_id=sid, prefix=prefix, num=num, root_rel="")
                stories[sid] = unit
            unit.rows.append(row)
            continue

        for name, needles in BASE_GROUPS.items():
            if any(n in low for n in needles):
                base[name].append(row)
                break

    # Drop voice-only phantom units that never had a script bundle (not a real novel).
    stories = {sid: u for sid, u in stories.items() if u.root_rel}
    return Plan(stories=stories, base=base)


# --------------------------------------------------------------------------- #
# Progress reporting
# --------------------------------------------------------------------------- #
class Progress:
    """Thread-safe progress sink: in-memory snapshot + optional JSON file for polling."""

    def __init__(self, path: Path | None = None):
        self._lock = threading.Lock()
        self._path = path
        self.state = {
            "phase": "idle",       # idle|planning|base|stories|updating|done|error
            "message": "",
            "done": 0,
            "total": 0,
            "currentStory": "",
            "bytes": 0,
            "startedAt": time.time(),
            "updatedAt": time.time(),
            "errors": [],
            "finished": False,
        }

    def update(self, **kw):
        with self._lock:
            self.state.update(kw)
            self.state["updatedAt"] = time.time()
            self._flush()

    def add_bytes(self, n: int):
        with self._lock:
            self.state["bytes"] += n
            self.state["updatedAt"] = time.time()

    def tick(self, story: str = ""):
        with self._lock:
            self.state["done"] += 1
            if story:
                self.state["currentStory"] = story
            self.state["updatedAt"] = time.time()
            self._flush()

    def error(self, where: str, err: str):
        with self._lock:
            self.state["errors"].append({"where": where, "error": err[:500]})
            self.state["updatedAt"] = time.time()
            self._flush()

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.state)

    def _flush(self):
        if not self._path:
            return
        try:
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)
        except Exception:  # noqa: BLE001 - progress must never crash the pipeline
            pass


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
_UA = "DotAbyssPlayer-pipeline/1.0"


def _download_one(row: BundleRow, dest_root: Path, retries: int, progress: Progress | None) -> Path:
    dest = dest_root / row.output.replace("\\", "/")
    if dest.exists() and (row.size <= 0 or dest.stat().st_size == row.size):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(row.url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as out:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    if progress:
                        progress.add_bytes(len(chunk))
            if row.size > 0 and tmp.stat().st_size != row.size:
                raise IOError(f"size mismatch {tmp.stat().st_size} != {row.size}")
            tmp.replace(dest)
            return dest
        except Exception as exc:  # noqa: BLE001
            last = exc
            if tmp.exists():
                try:
                    tmp.unlink()
                except Exception:  # noqa: BLE001
                    pass
            if attempt < retries:
                time.sleep(0.5 * (2 ** attempt))
    raise RuntimeError(f"download failed {row.url}: {last}")


def download_rows(rows: list[BundleRow], dest_root: Path, workers: int = 8,
                  retries: int = 3, progress: Progress | None = None) -> list[Path]:
    paths: list[Path] = []
    if not rows:
        return paths
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_download_one, r, dest_root, retries, progress): r for r in rows}
        for fut in as_completed(futs):
            paths.append(fut.result())
    return paths


# --------------------------------------------------------------------------- #
# Per-story processing
# --------------------------------------------------------------------------- #
def _story_ogg_and_clean(story_out: Path):
    """Transcode this story's decoded wav -> ogg, rewrite story.json paths, delete intermediates."""
    audio_dir = story_out / "audio"
    if _HAVE_SF and audio_dir.exists():
        for wav in sorted(audio_dir.rglob("*.wav")):
            ogg = wav.with_suffix(".ogg")
            if ogg.exists() and ogg.stat().st_mtime >= wav.stat().st_mtime:
                continue
            try:
                with _sf.SoundFile(wav, "r") as src:
                    with _sf.SoundFile(ogg, "w", samplerate=src.samplerate,
                                       channels=src.channels, format="OGG", subtype="VORBIS") as dst:
                        while True:
                            block = src.read(4096, dtype="float32", always_2d=True)
                            if len(block) == 0:
                                break
                            dst.write(block)
            except Exception:  # noqa: BLE001
                pass
        # Rewrite story.json wav paths -> ogg (+ fallbackPath) using the shared helper.
        story_json = story_out / "story.json"
        if story_json.exists() and "update_audio_paths" in globals():
            try:
                data = json.loads(story_json.read_text(encoding="utf-8"))
                if update_audio_paths(data, story_json, story_out):
                    story_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass

    # Delete intermediates: raw acb/awb payloads + decoded wav (keep ogg).
    raw = audio_dir / "raw"
    if raw.exists():
        shutil.rmtree(raw, ignore_errors=True)
    if audio_dir.exists():
        for wav in audio_dir.rglob("*.wav"):
            try:
                wav.unlink()
            except Exception:  # noqa: BLE001
                pass


def _download_story(unit: StoryUnit, tmp_root: Path, progress: Progress | None = None) -> Path:
    """Producer half: fetch a story's bundles into an isolated temp dir; return it."""
    story_tmp = tmp_root / unit.story_id
    if story_tmp.exists():
        shutil.rmtree(story_tmp, ignore_errors=True)
    story_tmp.mkdir(parents=True, exist_ok=True)
    download_rows(unit.rows, story_tmp, progress=progress)
    return story_tmp


def _extract_downloaded_story(unit: StoryUnit, story_tmp: Path, data_root: Path,
                              vgmstream: str | None, manifest_path: Path,
                              export_audio: bool = True, progress: Progress | None = None) -> dict:
    """Consumer half: extract + decode + ogg + clean; always removes the temp bundle dir."""
    try:
        root = story_tmp / unit.root_rel
        if not root.exists():
            root = story_tmp
        entry = extract_story(
            root,
            data_root,
            manifest_path=manifest_path,
            export_textures=True,
            bundle_root=story_tmp,
            audio_roots=[story_tmp],
            export_audio=export_audio and bool(vgmstream),
            vgmstream_path=vgmstream,
            story_id_override=unit.story_id,
        )
        _story_ogg_and_clean(data_root / "stories" / unit.story_id)
        entry["category"] = unit.prefix
        entry["scriptHash"] = unit.script_hash
        entry["scriptTitle"] = _story_script_title(data_root / "stories" / unit.story_id / "story.json")
        return entry
    finally:
        shutil.rmtree(story_tmp, ignore_errors=True)


def process_story(unit: StoryUnit, data_root: Path, tmp_root: Path,
                  vgmstream: str | None, manifest_path: Path,
                  export_audio: bool = True, progress: Progress | None = None) -> dict:
    """Single-story download+extract (used by extract_one / repair; not concurrent)."""
    story_tmp = _download_story(unit, tmp_root, progress)
    return _extract_downloaded_story(unit, story_tmp, data_root, vgmstream, manifest_path,
                                     export_audio, progress)


def _default_workers() -> int:
    env = os.environ.get("DOTABYSS_WORKERS")
    if env and env.isdigit() and int(env) > 0:
        return int(env)
    return min(os.cpu_count() or 4, 6)


def run_stories_concurrent(units, data_root: Path, tmp_root: Path, vgmstream: str | None,
                           manifest_path: Path, progress: Progress, on_entry,
                           workers: int | None = None):
    """Producer-consumer pipeline: 1 downloader thread prefetches story bundles into a bounded
    queue while N worker threads extract+decode in parallel (download<->extract overlap + parallel
    extraction). ``on_entry(entry)`` runs under a lock per completed story for index/state writes.

    Threads (not processes): vgmstream (subprocess) and soundfile (C) release the GIL, so N
    threads run that many decodes in parallel; freeze-safe (no multiprocessing). The bounded
    queue (maxsize workers+2) caps how many downloaded-but-unextracted stories sit on disk,
    preserving the streaming ~5-6 GB peak.
    """
    workers = workers or _default_workers()
    q: "queue.Queue" = queue.Queue(maxsize=workers + 2)
    index_lock = threading.Lock()
    _SENTINEL = object()

    def producer():
        for unit in units:
            try:
                story_tmp = _download_story(unit, tmp_root, progress)
            except Exception as exc:  # noqa: BLE001 — skip this story, keep the pipeline going
                progress.error(f"download:{unit.story_id}", repr(exc))
                progress.tick(unit.story_id)  # count the skip so done/total stays consistent
                continue
            q.put((unit, story_tmp))          # blocks when full -> disk backpressure
        for _ in range(workers):
            q.put(_SENTINEL)

    def consumer():
        while True:
            item = q.get()
            try:
                if item is _SENTINEL:
                    return
                unit, story_tmp = item
                try:
                    progress.update(currentStory=unit.story_id)
                    entry = _extract_downloaded_story(unit, story_tmp, data_root, vgmstream,
                                                      manifest_path, progress=progress)
                    with index_lock:
                        on_entry(entry)
                except Exception as exc:  # noqa: BLE001
                    progress.error(f"story:{unit.story_id}", repr(exc))
                finally:
                    progress.tick(unit.story_id)
            finally:
                q.task_done()

    prod = threading.Thread(target=producer, daemon=True)
    prod.start()
    consumers = [threading.Thread(target=consumer, daemon=True) for _ in range(workers)]
    for t in consumers:
        t.start()
    prod.join()
    for t in consumers:
        t.join()


_TITLE_PLACEHOLDERS = {"", "タイトル", "タイトルを設定してください"}


def _story_script_title(story_json: Path) -> str | None:
    try:
        data = json.loads(story_json.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    for script in data.get("scripts", []):
        for cmd in script.get("commands", []):
            if cmd.get("command") == "title" and cmd.get("args"):
                t = (cmd["args"][0] or "").strip()
                if t not in _TITLE_PLACEHOLDERS:
                    return t
    return None


# --------------------------------------------------------------------------- #
# Incremental index + state
# --------------------------------------------------------------------------- #
def _index_path(data_root: Path) -> Path:
    return data_root / "index.json"


def upsert_index(data_root: Path, entry: dict):
    path = _index_path(data_root)
    index = {"generatedBy": "tools/pipeline.py", "stories": []}
    if path.exists():
        try:
            index = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    stories = index.setdefault("stories", [])
    by_id = {s.get("id"): i for i, s in enumerate(stories)}
    if entry["id"] in by_id:
        stories[by_id[entry["id"]]] = entry
    else:
        stories.append(entry)
    index.setdefault("commandInfo", adv_extract.COMMAND_INFO)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_state(data_root: Path) -> dict:
    path = data_root / "state.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"version": None, "profile": None, "stories": {}, "translationsCommit": None}


def save_state(data_root: Path, state: dict):
    path = data_root / "state.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def _ensure_command_info():
    if not adv_extract.COMMAND_INFO:
        adv_extract.load_command_analysis_info(REPO_ROOT)


def run_base(plan: Plan, data_root: Path, tmp_root: Path, vgmstream: str | None,
             catalog: Path | None, progress: Progress | None = None):
    """Download shared base bundle groups, run the existing shared extractors, then clean."""
    data_dir = data_root  # shared extractors output under data_root (backgrounds/chara/audio)
    stories_root = data_root / "stories"
    for name, rows in plan.base.items():
        if not rows:
            continue
        if progress:
            progress.update(phase="base", message=f"base:{name}", currentStory=name)
        group_tmp = tmp_root / f"_base_{name}"
        group_tmp.mkdir(parents=True, exist_ok=True)
        try:
            download_rows(rows, group_tmp, progress=progress)
            _run_shared_extractor(name, group_tmp, data_dir, stories_root, vgmstream, progress)
        except Exception as exc:  # noqa: BLE001
            if progress:
                progress.error(f"base:{name}", repr(exc))
        finally:
            shutil.rmtree(group_tmp, ignore_errors=True)

    # BGM self-downloads its awb from the CDN using the catalog; run it last.
    if catalog and catalog.exists():
        if progress:
            progress.update(phase="base", message="base:bgm", currentStory="bgm")
        try:
            _run_bgm_extractor(catalog, data_dir, tmp_root, vgmstream)
        except Exception as exc:  # noqa: BLE001
            if progress:
                progress.error("base:bgm", repr(exc))

    _finalize_base_audio(data_dir)


def _finalize_base_audio(data_dir: Path):
    """Transcode shared SE/BGM wav -> ogg, rewrite their index.json, delete wav + raw."""
    audio_dir = data_dir / "audio"
    if not audio_dir.exists():
        return
    if _HAVE_SF:
        for wav in sorted(audio_dir.rglob("*.wav")):
            ogg = wav.with_suffix(".ogg")
            if ogg.exists() and ogg.stat().st_mtime >= wav.stat().st_mtime:
                continue
            try:
                with _sf.SoundFile(wav, "r") as src:
                    with _sf.SoundFile(ogg, "w", samplerate=src.samplerate,
                                       channels=src.channels, format="OGG", subtype="VORBIS") as dst:
                        while True:
                            block = src.read(4096, dtype="float32", always_2d=True)
                            if len(block) == 0:
                                break
                            dst.write(block)
            except Exception:  # noqa: BLE001
                pass
        if "update_json_files" in globals():
            try:
                update_json_files(data_dir)
            except Exception:  # noqa: BLE001
                pass
    for raw in audio_dir.rglob("raw"):
        if raw.is_dir():
            shutil.rmtree(raw, ignore_errors=True)
    for wav in audio_dir.rglob("*.wav"):
        try:
            wav.unlink()
        except Exception:  # noqa: BLE001
            pass


def _invoke_module_main(module_name: str, args: list[str]) -> int:
    """Run a shared-extractor's main() in-process (freeze-safe: no python/.py at runtime)."""
    import importlib
    mod = importlib.import_module(module_name)
    old_argv = sys.argv
    sys.argv = [module_name, *args]
    try:
        return int(mod.main() or 0)
    finally:
        sys.argv = old_argv


def _run_shared_extractor(name: str, bundle_root: Path, data_dir: Path, stories_root: Path,
                          vgmstream: str | None, progress: Progress | None):
    common = ["--bundle-root", str(bundle_root), "--story-root", str(stories_root)]
    if name == "background":
        _invoke_module_main("extract_bg_assets",
                            ["--output", str(data_dir / "backgrounds" / "novel"), *common])
    elif name == "charastand":
        _invoke_module_main("extract_charastand_assets",
                            ["--output", str(data_dir / "chara"),
                             "--emotion-root", str(bundle_root),
                             "--emotion-output", str(data_dir / "emotion" / "charastand"), *common])
    elif name == "se":
        args = ["--output", str(data_dir / "audio" / "se"), *common]
        if vgmstream:
            args += ["--vgmstream", vgmstream]
        _invoke_module_main("extract_global_se_assets", args)


def _run_bgm_extractor(catalog: Path, data_dir: Path, tmp_root: Path, vgmstream: str | None):
    args = ["--catalog", str(catalog),
            "--output", str(data_dir / "audio" / "bgm"),
            "--raw-root", str(tmp_root / "_bgm_awb")]
    if vgmstream:
        args += ["--vgmstream", vgmstream]
    _invoke_module_main("extract_global_bgm_assets", args)
    shutil.rmtree(tmp_root / "_bgm_awb", ignore_errors=True)


def run_setup(manifest_path: Path, data_root: Path, *, tmp_root: Path | None = None,
              vgmstream: str | None = None, catalog: Path | None = None,
              do_base: bool = True, progress: Progress | None = None,
              version: str | None = None, profile: str | None = None,
              only_missing: bool = False) -> dict:
    """Full streaming setup: all novel stories, then shared base groups."""
    _ensure_command_info()
    progress = progress or Progress()
    data_root.mkdir(parents=True, exist_ok=True)
    tmp_root = tmp_root or (data_root / "_work")
    tmp_root.mkdir(parents=True, exist_ok=True)
    vgmstream = vgmstream or (str(find_vgmstream_cli()) if find_vgmstream_cli() else None)

    progress.update(phase="planning", message="reading manifest")
    rows = read_manifest(manifest_path)
    plan = plan_from_manifest(rows)
    state = load_state(data_root)
    story_ids = sorted(plan.stories)
    if only_missing:
        story_ids = [sid for sid in story_ids
                     if not (data_root / "stories" / sid / "story.json").exists()]

    units = [plan.stories[sid] for sid in story_ids]
    progress.update(phase="stories", total=len(units), done=0)
    _save_n = {"n": 0}

    def on_entry(entry):
        upsert_index(data_root, entry)
        state.setdefault("stories", {})[entry["id"]] = {"scriptHash": entry.get("scriptHash", "")}
        _save_n["n"] += 1
        if _save_n["n"] % 25 == 0:
            save_state(data_root, state)

    run_stories_concurrent(units, data_root, tmp_root, vgmstream, manifest_path, progress, on_entry)

    if do_base:
        run_base(plan, data_root, tmp_root, vgmstream, catalog, progress)

    state["version"] = version or state.get("version")
    state["profile"] = profile or state.get("profile")
    save_state(data_root, state)
    shutil.rmtree(tmp_root, ignore_errors=True)
    progress.update(phase="done", finished=True, message="setup complete")
    return progress.snapshot()


def extract_one(manifest_path: Path, data_root: Path, story_id: str, *,
                tmp_root: Path | None = None, vgmstream: str | None = None,
                progress: Progress | None = None) -> dict:
    """Repair / on-demand re-extract of a single story."""
    _ensure_command_info()
    progress = progress or Progress()
    tmp_root = tmp_root or (data_root / "_work")
    tmp_root.mkdir(parents=True, exist_ok=True)
    vgmstream = vgmstream or (str(find_vgmstream_cli()) if find_vgmstream_cli() else None)
    rows = read_manifest(manifest_path)
    plan = plan_from_manifest(rows)
    unit = plan.stories.get(story_id)
    if unit is None:
        raise KeyError(f"story {story_id} not found in manifest")
    progress.update(phase="stories", total=1, done=0, currentStory=story_id)
    entry = process_story(unit, data_root, tmp_root, vgmstream, manifest_path, progress=progress)
    upsert_index(data_root, entry)
    progress.tick(story_id)
    progress.update(phase="done", finished=True)
    return entry


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    adv_extract.configure_stdout()
    ap = argparse.ArgumentParser(description="Streaming download/extract/clean pipeline.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan", help="Parse manifest and print unit counts.")
    p_plan.add_argument("--manifest", required=True)

    p_story = sub.add_parser("story", help="Process a single story (download+extract+clean).")
    p_story.add_argument("--manifest", required=True)
    p_story.add_argument("--data", required=True)
    p_story.add_argument("--id", required=True)
    p_story.add_argument("--vgmstream", default=None)

    p_setup = sub.add_parser("setup", help="Full streaming setup.")
    p_setup.add_argument("--manifest", required=True)
    p_setup.add_argument("--data", required=True)
    p_setup.add_argument("--catalog", default=None)
    p_setup.add_argument("--vgmstream", default=None)
    p_setup.add_argument("--no-base", action="store_true")
    p_setup.add_argument("--only-missing", action="store_true")
    p_setup.add_argument("--limit", type=int, default=0)

    args = ap.parse_args()

    if args.cmd == "plan":
        rows = read_manifest(Path(args.manifest))
        plan = plan_from_manifest(rows)
        by_prefix: dict[str, int] = {}
        for u in plan.stories.values():
            by_prefix[u.prefix] = by_prefix.get(u.prefix, 0) + 1
        print(f"bundles={len(rows)} stories={len(plan.stories)} by_prefix={by_prefix}")
        for name, rws in plan.base.items():
            print(f"  base[{name}]={len(rws)} bundles")
        return 0

    if args.cmd == "story":
        prog = Progress()
        entry = extract_one(Path(args.manifest), Path(args.data), args.id, vgmstream=args.vgmstream, progress=prog)
        print(json.dumps({"entry": entry, "progress": prog.snapshot()}, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "setup":
        prog = Progress(Path(args.data) / ".pipeline_progress.json")
        if args.limit:
            # Debug: process only the first N stories, no base.
            _ensure_command_info()
            rows = read_manifest(Path(args.manifest))
            plan = plan_from_manifest(rows)
            data_root = Path(args.data)
            tmp_root = data_root / "_work"
            vg = args.vgmstream or (str(find_vgmstream_cli()) if find_vgmstream_cli() else None)
            for sid in sorted(plan.stories)[:args.limit]:
                entry = process_story(plan.stories[sid], data_root, tmp_root, vg, Path(args.manifest), progress=prog)
                upsert_index(data_root, entry)
                prog.tick(sid)
                print(f"done {sid}: {entry['stats']}")
            shutil.rmtree(tmp_root, ignore_errors=True)
            return 0
        run_setup(
            Path(args.manifest), Path(args.data),
            vgmstream=args.vgmstream,
            catalog=Path(args.catalog) if args.catalog else None,
            do_base=not args.no_base,
            only_missing=args.only_missing,
            progress=prog,
        )
        print(json.dumps(prog.snapshot(), ensure_ascii=False, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""下载并解码全局 BGM（CRI awb 流式音频）→ data/audio/bgm/index.json。

背景：BGM 的真实音频是 catalog 里 `CriWare.Assets.CriResourceProvider` 注册的
`.awb`（AFS2，含 loop 点），此前下载器 IsBundleLocation 只收 `.bundle` 结尾条目，
把整类 CRI 资源漏下了（bgm*.acb 只是 ~5KB cuesheet）。实测每个 bgmXXXX.awb 就是
一条完整 BGM，vgmstream 可直接解码，无需 acb。

流程：从 catalog 取 bgm*.awb 的远端相对路径 → 拼 baseUrl 从 CDN 直下（无需认证，
与 bundle 同一 CDN）→ vgmstream 解码为 wav 落到 data/audio/bgm/decoded/。
随后跑 tools/convert_wav_audio_to_ogg.py 转 ogg 并把 index.json 的 path 改为 .ogg。
前端 loadGlobalAudioSources 加载本 index 后，bgmplay 的 cue（bgm0031…）即可解析出声。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import urllib.request
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = (
    WORKSPACE / "workspace" / "bundles" / "android-dmm-r18" / "_catalog" / "catalog_1.extracted.json"
)
DEFAULT_OUTPUT_ROOT = WORKSPACE / "src" / "AdvPlayer" / "data" / "audio" / "bgm"
DEFAULT_RAW_ROOT = WORKSPACE / "workspace" / "bgm_awb"  # 原始 awb 缓存（不进 served 目录）
# 用一个已提取 story 的 manifest 反推 CDN baseUrl（url = baseUrl + remoteRelativePath）。
REF_STORY_MANIFEST = (
    WORKSPACE / "src" / "AdvPlayer" / "data_r18_all" / "stories" / "hmr_10680100021" / "story.json"
)
PLACEHOLDER_SPLIT = "}/"
USER_AGENT = "DotAbyssClient/1.0"


def load_adv_extract():
    # Frozen-safe: a normal import works in the packaged client (bundled hiddenimport) and via
    # the pipeline / standalone script; the .py-by-path fallback is only for exotic setups.
    try:
        import adv_extract  # noqa: PLC0415
        return adv_extract
    except Exception:  # noqa: BLE001
        pass
    path = WORKSPACE / "tools" / "adv_extract.py"
    spec = importlib.util.spec_from_file_location("adv_extract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def derive_base_url(manifest_path: Path) -> str:
    story = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = story.get("manifest", {})
    entries = manifest.get("audio") or manifest.get("entries") or []
    if not entries:
        raise RuntimeError(f"no manifest entries in {manifest_path}")
    entry = entries[0]
    url, rel = entry["url"], entry["remoteRelativePath"]
    if not url.endswith(rel):
        raise RuntimeError("manifest url does not end with remoteRelativePath")
    return url[: len(url) - len(rel)]


def find_bgm_awb_locations(catalog: dict):
    out, seen = [], set()
    for loc in catalog.get("locations", []):
        if not isinstance(loc, dict):
            continue
        if loc.get("providerId") != "CriWare.Assets.CriResourceProvider":
            continue
        iid = loc.get("internalId") or ""
        m = re.search(r"(bgm\d+)\.awb", iid, re.IGNORECASE)
        if not m:
            continue
        cue = m.group(1).lower()
        if cue in seen:
            continue
        idx = iid.find(PLACEHOLDER_SPLIT)
        rel = iid[idx + len(PLACEHOLDER_SPLIT):] if idx >= 0 else iid
        seen.add(cue)
        out.append({"cue": cue, "rel": rel, "size": int((loc.get("data") or {}).get("bundleSize") or 0)})
    return sorted(out, key=lambda x: x["cue"])


def download(url: str, dest: Path, expected_size: int = 0):
    if dest.exists() and (expected_size <= 0 or dest.stat().st_size == expected_size):
        return "cached"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=180) as resp, open(tmp, "wb") as f:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    tmp.replace(dest)
    return "downloaded"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--raw-root", default=str(DEFAULT_RAW_ROOT))
    parser.add_argument("--base-url", default=None, help="覆盖 CDN baseUrl（默认从参考 manifest 反推）")
    parser.add_argument("--vgmstream", default=None)
    parser.add_argument("--limit", type=int, default=0, help="仅处理前 N 个（调试/spike）")
    args = parser.parse_args()

    adv = load_adv_extract()
    vgm = adv.find_vgmstream_cli(args.vgmstream)
    if not vgm:
        raise SystemExit("vgmstream-cli.exe not found（放到 tools/bin/vgmstream/）")

    base_url = args.base_url or derive_base_url(REF_STORY_MANIFEST)
    catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    locs = find_bgm_awb_locations(catalog)
    if args.limit:
        locs = locs[: args.limit]
    print(f"base_url={base_url}")
    print(f"found {len(locs)} bgm awb locations")

    output_root = Path(args.output)
    raw_root = Path(args.raw_root)
    decoded_root = output_root / "decoded"

    index = {
        "generatedBy": "tools/extract_global_bgm_assets.py",
        "decoder": Path(vgm).name,
        "cues": {},
        "errors": [],
    }
    for loc in locs:
        cue = loc["cue"]
        url = base_url + loc["rel"]
        raw_path = raw_root / f"{cue}.awb"
        try:
            state = download(url, raw_path, loc["size"])
            info = adv.run_vgmstream_info(vgm, raw_path)
            out_wav = decoded_root / f"{cue}.wav"
            adv.run_vgmstream_decode(vgm, raw_path, out_wav)
            sr = info.get("sampleRate")
            samples = info.get("numberOfSamples") or info.get("playSamples")
            duration = samples / sr if sr and samples else None
            index["cues"][cue] = {
                "name": cue,
                "category": "bgm",
                "path": f"audio/bgm/decoded/{cue}.wav",
                "source": loc["rel"],
                "subsong": 1,
                "duration": duration,
                "sampleRate": sr,
                "channels": info.get("channels"),
                "encoding": info.get("encoding"),
                "bytes": out_wav.stat().st_size,
            }
            print(f"[{state}] decoded {cue} -> {index['cues'][cue]['path']} ({sr}Hz {info.get('channels')}ch)")
        except Exception as exc:
            index["errors"].append({"cue": cue, "url": url, "error": repr(exc)})
            print(f"failed {cue}: {exc}")

    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"cues={len(index['cues'])} errors={len(index['errors'])} -> {output_root / 'index.json'}")
    return 1 if index["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

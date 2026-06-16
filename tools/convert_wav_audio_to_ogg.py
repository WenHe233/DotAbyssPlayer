from __future__ import annotations

import json
from pathlib import Path

import soundfile as sf


WORKSPACE = Path(__file__).resolve().parents[1]
DATA_ROOTS = [
    WORKSPACE / "src" / "AdvPlayer" / "data",
    WORKSPACE / "src" / "AdvPlayer" / "data_r18_all",
]


def convert_wavs(root: Path) -> dict[str, dict]:
    converted: dict[str, dict] = {}
    errors: list[dict] = []
    wavs = sorted((root).rglob("audio/**/*.wav"))
    for index, wav_path in enumerate(wavs, 1):
        try:
            ogg_path = wav_path.with_suffix(".ogg")
            if ogg_path.exists() and ogg_path.stat().st_mtime >= wav_path.stat().st_mtime:
                converted[str(wav_path.relative_to(root)).replace("\\", "/")] = {
                    "ogg": str(ogg_path.relative_to(root)).replace("\\", "/"),
                    "wavBytes": wav_path.stat().st_size,
                    "oggBytes": ogg_path.stat().st_size,
                }
                continue
            with sf.SoundFile(wav_path, "r") as source:
                with sf.SoundFile(
                    ogg_path,
                    "w",
                    samplerate=source.samplerate,
                    channels=source.channels,
                    format="OGG",
                    subtype="VORBIS",
                ) as target:
                    while True:
                        block = source.read(4096, dtype="float32", always_2d=True)
                        if len(block) == 0:
                            break
                        target.write(block)
            converted[str(wav_path.relative_to(root)).replace("\\", "/")] = {
                "ogg": str(ogg_path.relative_to(root)).replace("\\", "/"),
                "wavBytes": wav_path.stat().st_size,
                "oggBytes": ogg_path.stat().st_size,
            }
            if index % 10 == 0:
                print(f"  {index}/{len(wavs)}", flush=True)
        except Exception as exc:
            errors.append({"path": str(wav_path), "error": repr(exc)})
            print(f"failed {wav_path}: {exc}")
    if errors:
        (root / "audio" / "ogg_conversion_errors.json").write_text(
            json.dumps(errors, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return converted


def resolve_relative_audio(json_path: Path, root: Path, value: str) -> Path | None:
    if not value.lower().endswith(".wav"):
        return None
    for base in (json_path.parent, root):
        candidate = base / value
        if candidate.exists():
            return candidate
    return None


def update_audio_paths(node, json_path: Path, root: Path) -> bool:
    changed = False
    if isinstance(node, dict):
        if isinstance(node.get("path"), str):
            wav_path = resolve_relative_audio(json_path, root, node["path"])
            if wav_path is not None and wav_path.with_suffix(".ogg").exists():
                if "fallbackPath" not in node:
                    node["fallbackPath"] = node["path"]
                node["path"] = node["path"][:-4] + ".ogg"
                node["bytes"] = wav_path.with_suffix(".ogg").stat().st_size
                changed = True
        for value in node.values():
            changed = update_audio_paths(value, json_path, root) or changed
    elif isinstance(node, list):
        for value in node:
            changed = update_audio_paths(value, json_path, root) or changed
    return changed


def update_json_files(root: Path) -> int:
    count = 0
    json_paths = [
        *sorted((root / "audio").rglob("index.json")),
        *sorted((root / "stories").glob("*/story.json")),
    ]
    for json_path in json_paths:
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if update_audio_paths(data, json_path, root):
            json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            count += 1
    return count


def main() -> int:
    total_wav = 0
    total_ogg = 0
    total_count = 0
    updated_json = 0
    for root in DATA_ROOTS:
        print(f"converting {root}", flush=True)
        updated_json += update_json_files(root)
        converted = convert_wavs(root)
        updated_json += update_json_files(root)
        total_count += len(converted)
        total_wav += sum(item["wavBytes"] for item in converted.values())
        total_ogg += sum(item["oggBytes"] for item in converted.values())
        print(f"{root}: converted={len(converted)}", flush=True)
    print(f"converted={total_count} wavBytes={total_wav} oggBytes={total_ogg} jsonUpdated={updated_json}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

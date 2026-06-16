from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import UnityPy


WORKSPACE = Path(__file__).resolve().parents[1]
DEFAULT_STORY_ROOTS = [
    WORKSPACE / "src" / "AdvPlayer" / "data" / "stories",
    WORKSPACE / "src" / "AdvPlayer" / "data_r18_all" / "stories",
]
DEFAULT_BG_BUNDLE_ROOT = (
    WORKSPACE
    / "workspace"
    / "bundles"
    / "android-dmm-r18"
    / "general-ui-bg-novel"
    / "assets"
    / "assets"
    / "project"
    / "lazyassets"
    / "general"
    / "ui"
    / "bg"
    / "novel"
)
DEFAULT_OUTPUT_ROOT = WORKSPACE / "src" / "AdvPlayer" / "data" / "backgrounds" / "novel"


def safe_name(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return text.strip("._").lower() or "unnamed"


def clean_source_path(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.name


def collect_background_ids(story_roots: list[Path]) -> list[str]:
    ids: set[str] = set()
    for story_root in story_roots:
        if not story_root.exists():
            continue
        for story_file in story_root.glob("*/story.json"):
            story = json.loads(story_file.read_text(encoding="utf-8"))
            for script in story.get("scripts", []):
                for command in script.get("commands", []):
                    if str(command.get("command", "")).lower() != "bg":
                        continue
                    args = command.get("args") or []
                    if args and args[0]:
                        ids.add(safe_name(str(args[0])))
    return sorted(ids)


def find_bundle(background_id: str, bundle_root: Path) -> Path | None:
    needle = f"{background_id}.png"
    matches = [path for path in bundle_root.rglob("*.bundle") if needle in path.name.lower()]
    return matches[0] if matches else None


def extract_background(background_id: str, bundle: Path, bundle_root: Path, output_root: Path) -> dict:
    env = UnityPy.load(str(bundle))
    best = None
    for obj in env.objects:
        if obj.type.name not in {"Sprite", "Texture2D"}:
            continue
        data = obj.read()
        name = safe_name(str(getattr(data, "name", "") or obj.path_id))
        try:
            image = data.image
        except Exception:
            continue
        if image is None:
            continue
        score = 0
        if name == background_id:
            score += 10_000_000_000
        if obj.type.name == "Sprite":
            score += 1_000_000_000
        score += image.width * image.height
        if best is None or score > best[0]:
            best = (score, name, obj.type.name, image)

    if best is None:
        raise RuntimeError("no Sprite/Texture2D image found")

    _, sprite_name, object_type, image = best
    output_root.mkdir(parents=True, exist_ok=True)
    out_path = output_root / f"{background_id}.png"
    image.save(out_path)
    return {
        "id": background_id,
        "path": f"backgrounds/novel/{out_path.name}",
        "sourceBundle": clean_source_path(bundle, bundle_root),
        "sourceObject": sprite_name,
        "sourceType": object_type,
        "width": image.width,
        "height": image.height,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract novel background sprites referenced by story.json files.")
    parser.add_argument("--story-root", action="append", default=[], help="Story root directory containing <story-id>/story.json. Can be repeated.")
    parser.add_argument("--bundle-root", default=str(DEFAULT_BG_BUNDLE_ROOT), help="Bundle root that contains the background prefab bundles.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_ROOT), help="Output directory for exported background PNGs and index.json.")
    args = parser.parse_args()

    story_roots = [Path(value) for value in args.story_root] if args.story_root else DEFAULT_STORY_ROOTS
    bundle_root = Path(args.bundle_root)
    output_root = Path(args.output)

    output_root.mkdir(parents=True, exist_ok=True)
    index = {
        "generatedBy": "tools/extract_bg_assets.py",
        "backgrounds": {},
        "errors": [],
    }

    for background_id in collect_background_ids(story_roots):
        bundle = find_bundle(background_id, bundle_root)
        if bundle is None:
            index["errors"].append({"id": background_id, "error": "bundle not found"})
            continue
        try:
            item = extract_background(background_id, bundle, bundle_root, output_root)
            index["backgrounds"][background_id] = item
            print(f"exported {background_id} -> {item['path']}")
        except Exception as exc:
            index["errors"].append({"id": background_id, "sourceBundle": clean_source_path(bundle, bundle_root), "error": repr(exc)})
            print(f"failed {background_id}: {exc}")

    (output_root / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"backgrounds={len(index['backgrounds'])} errors={len(index['errors'])}")
    return 1 if index["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

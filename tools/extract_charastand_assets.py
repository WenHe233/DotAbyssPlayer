from __future__ import annotations

import json
import re
import argparse
from pathlib import Path

import UnityPy


WORKSPACE = Path(__file__).resolve().parents[1]
STORY_ROOTS = [
    WORKSPACE / "src" / "AdvPlayer" / "data" / "stories",
    WORKSPACE / "src" / "AdvPlayer" / "data_r18_all" / "stories",
]
CHARASTAND_ROOT = WORKSPACE / "workspace" / "bundles" / "android-dmm-r18" / "r18-only-charastand"
OUTPUT_ROOT = WORKSPACE / "src" / "AdvPlayer" / "data" / "chara"
EMOTION_ROOT = WORKSPACE / "workspace" / "bundles" / "android-dmm-r18" / "general-ui" / "assets" / "assets" / "project" / "lazyassets" / "general" / "ui" / "emotion" / "charastand" / "prefabs" / "emo"
EMOTION_OUTPUT_ROOT = WORKSPACE / "src" / "AdvPlayer" / "data" / "emotion" / "charastand"


def safe_name(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return text.strip("._") or "unnamed"


def normalize_face_name(value: str) -> str:
    return safe_name(value).lower()


def pptr_reader(pptr):
    if pptr is None:
        return None
    reader = getattr(pptr, "object_reader", None)
    if reader is not None:
        return reader
    try:
        obj = pptr.read()
    except Exception:
        return None
    return getattr(obj, "reader", None)


def ref_path_id(pptr):
    try:
        return pptr.path_id
    except Exception:
        reader = pptr_reader(pptr)
        return reader.path_id if reader is not None else None


def vector2_to_json(value):
    if value is None:
        return {"x": 0.0, "y": 0.0}
    if isinstance(value, dict):
        return {"x": float(value.get("x", 0.0)), "y": float(value.get("y", 0.0))}
    return {"x": float(getattr(value, "x", getattr(value, "X", 0.0))), "y": float(getattr(value, "y", getattr(value, "Y", 0.0)))}


def vector3_to_json(value):
    if value is None:
        return {"x": 0.0, "y": 0.0, "z": 0.0}
    if isinstance(value, dict):
        return {"x": float(value.get("x", 0.0)), "y": float(value.get("y", 0.0)), "z": float(value.get("z", 0.0))}
    return {
        "x": float(getattr(value, "x", getattr(value, "X", 0.0))),
        "y": float(getattr(value, "y", getattr(value, "Y", 0.0))),
        "z": float(getattr(value, "z", getattr(value, "Z", 0.0))),
    }


def collect_story_character_ids() -> list[str]:
    ids: set[str] = set()
    for root in STORY_ROOTS:
        if not root.exists():
            continue
        for story_file in root.glob("*/story.json"):
            story = json.loads(story_file.read_text(encoding="utf-8"))
            for script in story.get("scripts", []):
                for command in script.get("commands", []):
                    if str(command.get("command", "")).lower() != "charaload":
                        continue
                    args = command.get("args") or []
                    if len(args) > 1 and args[1]:
                        ids.add(str(args[1]).upper())
    return sorted(ids)


def normalize_character_id(value: str) -> str:
    return str(value or "").strip().upper()


def load_existing_index() -> dict:
    path = OUTPUT_ROOT / "index.json"
    if not path.exists():
        return {"characters": [], "emotions": [], "failures": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"characters": [], "emotions": [], "failures": []}
    data.setdefault("characters", [])
    data.setdefault("emotions", [])
    data.setdefault("failures", [])
    return data


def merge_character_index(existing: dict, extracted: list[dict], failures: list[dict], emotions: list[dict]) -> dict:
    characters = {
        normalize_character_id(item.get("id")): item
        for item in existing.get("characters", [])
        if item.get("id")
    }
    for item in extracted:
        characters[normalize_character_id(item.get("id"))] = item

    failure_map = {
        normalize_character_id(item.get("id")): item
        for item in existing.get("failures", [])
        if item.get("id")
    }
    for item in failures:
        failure_map[normalize_character_id(item.get("id"))] = item
    for item in extracted:
        failure_map.pop(normalize_character_id(item.get("id")), None)

    return {
        "characters": sorted(characters.values(), key=lambda item: normalize_character_id(item.get("id"))),
        "emotions": emotions or existing.get("emotions", []),
        "failures": sorted(failure_map.values(), key=lambda item: normalize_character_id(item.get("id"))),
    }


def find_bundle(character_id: str) -> Path | None:
    needle = f"charastand{character_id.lower()}"
    matches = [path for path in CHARASTAND_ROOT.rglob("*.bundle") if needle in path.name.lower()]
    return matches[0] if matches else None


def read_rect_transform(reader) -> dict:
    rect = reader.read()
    tree = reader.read_typetree()
    return {
        "anchoredPosition": vector2_to_json(tree.get("m_AnchoredPosition")),
        "sizeDelta": vector2_to_json(tree.get("m_SizeDelta")),
        "anchorMin": vector2_to_json(tree.get("m_AnchorMin")),
        "anchorMax": vector2_to_json(tree.get("m_AnchorMax")),
        "pivot": vector2_to_json(tree.get("m_Pivot")),
        "localPosition": vector3_to_json(rect.m_LocalPosition),
        "localScale": vector3_to_json(rect.m_LocalScale),
        "father": ref_path_id(rect.m_Father),
        "children": [ref_path_id(child) for child in rect.m_Children],
    }


def read_rect_transform_with_id(reader) -> dict:
    data = read_rect_transform(reader)
    data["rectId"] = reader.path_id
    return data


def find_emotion_root(rects: dict[int, dict], names: dict[int, str]) -> int | None:
    for go_id, name in names.items():
        if safe_name(name).lower().startswith("emo_") and go_id in rects:
            return go_id
    for go_id, rect in rects.items():
        if not rect.get("father"):
            return go_id
    return next(iter(rects.keys()), None)


def parent_game_object(go_id: int, rects: dict[int, dict], rect_owner_by_rect_id: dict[int, int]) -> int | None:
    father = rects.get(go_id, {}).get("father")
    return rect_owner_by_rect_id.get(father)


def cumulative_rect_position(go_id: int | None, rects: dict[int, dict], rect_owner_by_rect_id: dict[int, int]) -> dict:
    total = {"x": 0.0, "y": 0.0}
    current = go_id
    seen: set[int] = set()
    while current is not None and current not in seen:
        seen.add(current)
        position = rects.get(current, {}).get("anchoredPosition", {})
        total["x"] += float(position.get("x", 0.0))
        total["y"] += float(position.get("y", 0.0))
        current = parent_game_object(current, rects, rect_owner_by_rect_id)
    return total


def rect_depth(go_id: int | None, rects: dict[int, dict], rect_owner_by_rect_id: dict[int, int]) -> int:
    depth = 0
    current = parent_game_object(go_id, rects, rect_owner_by_rect_id) if go_id is not None else None
    seen: set[int] = set()
    while current is not None and current not in seen:
        seen.add(current)
        depth += 1
        current = parent_game_object(current, rects, rect_owner_by_rect_id)
    return depth


def emotion_duration_seconds(env) -> float:
    durations = []
    for obj in env.objects:
        if obj.type.name != "AnimationClip":
            continue
        try:
            tree = obj.read_typetree()
        except Exception:
            continue
        name = safe_name(str(tree.get("m_Name", ""))).lower()
        if name == "singleanim":
            continue
        muscle = tree.get("m_MuscleClip") or {}
        start = float(muscle.get("m_StartTime", 0.0) or 0.0)
        stop = float(muscle.get("m_StopTime", 0.0) or 0.0)
        if stop > start:
            durations.append(stop - start)
    return max(durations) if durations else 1.0


def unique_part_name(base: str, used_names: set[str]) -> str:
    candidate = base or "part"
    if candidate not in used_names:
        used_names.add(candidate)
        return candidate
    index = 2
    while f"{candidate}_{index}" in used_names:
        index += 1
    final = f"{candidate}_{index}"
    used_names.add(final)
    return final


def game_object_rects(env) -> dict[str, dict]:
    rects: dict[str, dict] = {}
    for obj in env.objects:
        if obj.type.name != "GameObject":
            continue
        data = obj.read()
        for component in getattr(data, "m_Components", []) or []:
            reader = pptr_reader(component)
            if reader is not None and reader.type.name == "RectTransform":
                rects[data.name] = read_rect_transform(reader)
                break
    return rects


def game_object_rect_maps(env):
    rects_by_name: dict[str, dict] = {}
    rects_by_go_id: dict[int, dict] = {}
    rect_owner_by_rect_id: dict[int, int] = {}
    go_id_by_name: dict[str, int] = {}
    for obj in env.objects:
        if obj.type.name != "GameObject":
            continue
        data = obj.read()
        name = str(data.name)
        go_id_by_name[name] = obj.path_id
        for component in getattr(data, "m_Components", []) or []:
            reader = pptr_reader(component)
            if reader is None or reader.type.name != "RectTransform":
                continue
            rect = read_rect_transform_with_id(reader)
            rects_by_name[name] = rect
            rects_by_go_id[obj.path_id] = rect
            rect_owner_by_rect_id[rect["rectId"]] = obj.path_id
            break
    return rects_by_name, rects_by_go_id, rect_owner_by_rect_id, go_id_by_name


def rect_with_world_position(name: str, rects_by_name: dict[str, dict], rects_by_go_id: dict[int, dict], rect_owner_by_rect_id: dict[int, int], go_id_by_name: dict[str, int]) -> dict:
    rect = dict(rects_by_name.get(name, {}))
    go_id = go_id_by_name.get(name)
    if rect and go_id is not None:
        rect["worldPosition"] = cumulative_rect_position(go_id, rects_by_go_id, rect_owner_by_rect_id)
    return rect


def image_sprite_refs(env) -> dict[str, int]:
    refs: dict[str, int] = {}
    for obj in env.objects:
        if obj.type.name != "GameObject":
            continue
        data = obj.read()
        for component in getattr(data, "m_Components", []) or []:
            reader = pptr_reader(component)
            if reader is None or reader.type.name != "MonoBehaviour":
                continue
            try:
                tree = reader.read_typetree()
            except Exception:
                continue
            sprite = tree.get("m_Sprite")
            if isinstance(sprite, dict):
                path_id = int(sprite.get("m_PathID") or 0)
                if path_id:
                    refs[data.name] = path_id
    return refs


def extract_character(character_id: str, bundle: Path) -> dict:
    env = UnityPy.load(str(bundle))
    out_dir = OUTPUT_ROOT / character_id.lower()
    faces_dir = out_dir / "faces"
    faces_dir.mkdir(parents=True, exist_ok=True)

    sprites = {}
    sprite_sizes = {}
    for obj in env.objects:
        if obj.type.name != "Sprite":
            continue
        data = obj.read()
        name = str(getattr(data, "name", "") or obj.path_id)
        sprites[obj.path_id] = (name, data)

    image_refs = image_sprite_refs(env)
    rects, rects_by_go_id, rect_owner_by_rect_id, go_id_by_name = game_object_rect_maps(env)
    files: dict[str, str] = {}
    faces: dict[str, str] = {}

    for game_object_name, path_id in image_refs.items():
        sprite_entry = sprites.get(path_id)
        if sprite_entry is None:
            continue
        sprite_name, sprite = sprite_entry
        try:
            image = sprite.image
        except Exception:
            continue
        if image is None:
            continue

        sprite_sizes[sprite_name] = {"width": image.width, "height": image.height}
        if game_object_name == "Body":
            dest = out_dir / "body.png"
            image.save(dest)
            files["body"] = "body.png"
        elif game_object_name not in {"SilhouettePanel"}:
            face_key = normalize_face_name(game_object_name)
            dest = faces_dir / f"{face_key}.png"
            image.save(dest)
            faces[face_key] = f"faces/{face_key}.png"

    metadata = {
        "id": character_id,
        "sourceBundle": str(bundle.relative_to(WORKSPACE)).replace("\\", "/"),
        "rootRect": rect_with_world_position(f"CharaStand{character_id}", rects, rects_by_go_id, rect_owner_by_rect_id, go_id_by_name)
        or rect_with_world_position(f"CharaStand{character_id.upper()}", rects, rects_by_go_id, rect_owner_by_rect_id, go_id_by_name)
        or rect_with_world_position("Root", rects, rects_by_go_id, rect_owner_by_rect_id, go_id_by_name),
        "bodyRect": rect_with_world_position("Body", rects, rects_by_go_id, rect_owner_by_rect_id, go_id_by_name),
        "faceContentRect": rect_with_world_position("FaceContent", rects, rects_by_go_id, rect_owner_by_rect_id, go_id_by_name),
        "emotionRect": rect_with_world_position("EmotionPos", rects, rects_by_go_id, rect_owner_by_rect_id, go_id_by_name),
        "effectRect": rect_with_world_position("EffectPos", rects, rects_by_go_id, rect_owner_by_rect_id, go_id_by_name),
        "zoomRect": rect_with_world_position("StandZoomPos", rects, rects_by_go_id, rect_owner_by_rect_id, go_id_by_name),
        "poseRect": rect_with_world_position("Pose", rects, rects_by_go_id, rect_owner_by_rect_id, go_id_by_name),
        "files": files,
        "faces": faces,
        "spriteSizes": sprite_sizes,
    }

    (out_dir / "meta.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


def extract_emotions() -> list[dict]:
    EMOTION_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    extracted = []
    for bundle in sorted(EMOTION_ROOT.glob("*.bundle")):
        key = bundle.name.split(".prefab_", 1)[0].lower()
        env = UnityPy.load(str(bundle))
        parts_dir = EMOTION_OUTPUT_ROOT / "parts" / key
        parts_dir.mkdir(parents=True, exist_ok=True)

        rects: dict[int, dict] = {}
        rect_owner_by_rect_id: dict[int, int] = {}
        names: dict[int, str] = {}
        image_refs: dict[int, int] = {}
        sprites = {}

        for obj in env.objects:
            if obj.type.name == "Sprite":
                data = obj.read()
                try:
                    image = data.image
                except Exception:
                    image = None
                if image is not None:
                    sprites[obj.path_id] = {"name": str(getattr(data, "name", "") or obj.path_id), "image": image}
            elif obj.type.name == "GameObject":
                data = obj.read()
                names[obj.path_id] = str(data.name)
                for component in getattr(data, "m_Components", []) or []:
                    reader = pptr_reader(component)
                    if reader is None:
                        continue
                    if reader.type.name == "RectTransform":
                        rect = read_rect_transform_with_id(reader)
                        rects[obj.path_id] = rect
                        rect_owner_by_rect_id[rect["rectId"]] = obj.path_id
                    elif reader.type.name == "MonoBehaviour":
                        try:
                            tree = reader.read_typetree()
                        except Exception:
                            continue
                        sprite = tree.get("m_Sprite")
                        if isinstance(sprite, dict):
                            sprite_id = int(sprite.get("m_PathID") or 0)
                            if sprite_id:
                                image_refs[obj.path_id] = sprite_id

        if not sprites:
            continue

        root_go = find_emotion_root(rects, names)
        root_rect = rects.get(root_go, {}) if root_go is not None else {}
        root_position = cumulative_rect_position(root_go, rects, rect_owner_by_rect_id) if root_go is not None else {"x": 0.0, "y": 0.0}
        best = max(sprites.values(), key=lambda item: item["image"].width * item["image"].height)
        dest = EMOTION_OUTPUT_ROOT / f"{key}.png"
        best["image"].save(dest)

        parts = []
        used_names: set[str] = set()
        for go_id, sprite_id in image_refs.items():
            sprite = sprites.get(sprite_id)
            rect = rects.get(go_id)
            if sprite is None or rect is None:
                continue
            part_name = unique_part_name(safe_name(names.get(go_id, "part")).lower(), used_names)
            part_file = f"parts/{key}/{part_name}.png"
            sprite["image"].save(EMOTION_OUTPUT_ROOT / part_file)
            position = cumulative_rect_position(go_id, rects, rect_owner_by_rect_id)
            parts.append({
                "name": names.get(go_id, part_name),
                "file": part_file,
                "sprite": sprite["name"],
                "position": {
                    "x": position["x"] - root_position["x"],
                    "y": position["y"] - root_position["y"],
                },
                "size": rect.get("sizeDelta", {"x": sprite["image"].width, "y": sprite["image"].height}),
                "pivot": rect.get("pivot", {"x": 0.5, "y": 0.5}),
                "localScale": rect.get("localScale", {"x": 1.0, "y": 1.0, "z": 1.0}),
                "naturalWidth": sprite["image"].width,
                "naturalHeight": sprite["image"].height,
                "depth": rect_depth(go_id, rects, rect_owner_by_rect_id),
            })
        parts.sort(key=lambda item: (item.get("depth", 0), item["name"]))

        extracted.append({
            "id": key,
            "file": f"{key}.png",
            "width": best["image"].width,
            "height": best["image"].height,
            "rootRect": root_rect,
            "duration": emotion_duration_seconds(env),
            "parts": parts,
        })
    (EMOTION_OUTPUT_ROOT / "index.json").write_text(json.dumps({"emotions": extracted}, ensure_ascii=False, indent=2), encoding="utf-8")
    return extracted


def main() -> int:
    global STORY_ROOTS, CHARASTAND_ROOT, OUTPUT_ROOT, EMOTION_ROOT, EMOTION_OUTPUT_ROOT
    parser = argparse.ArgumentParser(description="Extract r18 charastand assets used by ADV stories.")
    parser.add_argument("--id", action="append", default=[], help="Character id to extract, e.g. 101601000X. Can be repeated.")
    parser.add_argument("--story-root", action="append", default=[], help="Story root directory containing <story-id>/story.json. Can be repeated.")
    parser.add_argument("--bundle-root", default=str(CHARASTAND_ROOT), help="Bundle root that contains charastand prefab bundles.")
    parser.add_argument("--emotion-root", default=str(EMOTION_ROOT), help="Bundle root that contains shared emotion prefab bundles.")
    parser.add_argument("--output", default=str(OUTPUT_ROOT), help="Output directory for extracted character data.")
    parser.add_argument("--emotion-output", default=str(EMOTION_OUTPUT_ROOT), help="Output directory for extracted emotion sprites.")
    parser.add_argument("--no-emotions", action="store_true", help="Skip shared emotion prefab extraction.")
    args = parser.parse_args()

    STORY_ROOTS = [Path(value) for value in args.story_root] if args.story_root else STORY_ROOTS
    CHARASTAND_ROOT = Path(args.bundle_root)
    EMOTION_ROOT = Path(args.emotion_root)
    OUTPUT_ROOT = Path(args.output)
    EMOTION_OUTPUT_ROOT = Path(args.emotion_output)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    failures = []
    extracted = []
    existing = load_existing_index()
    character_ids = sorted({normalize_character_id(value) for value in args.id if normalize_character_id(value)}) or collect_story_character_ids()
    for character_id in character_ids:
        bundle = find_bundle(character_id)
        if bundle is None:
            failures.append({"id": character_id, "reason": "bundle not found"})
            continue
        try:
            metadata = extract_character(character_id, bundle)
            extracted.append({"id": character_id, "faces": sorted(metadata["faces"].keys()), "body": "body" in metadata["files"]})
        except Exception as exc:
            failures.append({"id": character_id, "reason": repr(exc)})

    emotions = existing.get("emotions", []) if args.no_emotions else extract_emotions()
    index = merge_character_index(existing, extracted, failures, emotions)
    (OUTPUT_ROOT / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(index, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""重解析空脚本 story + 重建 index.json（补 category / scriptTitle）。

背景：修复 adv_extract.looks_like_script（此前只看脚本第一行）后，部分以
`live2dinit` 等未登记命令开头的剧情不再被误判为空脚本。本脚本读取这些 story
已落盘的 `textassets/*.txt` 重新解析，回填 `story.json` 的 scripts/primaryScript/
stats/标题；不重跑音频/Live2D，也不重新下载。

同时为全部 story 的 index.json 条目补充：
  - category：前缀（evs/hmn/hmr/mas/men）。
  - scriptTitle：脚本内 `title,xxx` 的值，占位符（空 / タイトル / タイトルを設定
    してください）置为 null，供前端"标题模式"逐条回退。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adv_extract import (  # noqa: E402
    COMMAND_INFO,
    looks_like_script,
    make_title,
    parse_script,
    safe_name,
    write_json,
)

DATA_ROOT = Path(__file__).resolve().parent.parent / "src" / "AdvPlayer" / "data_r18_all"
STORIES_DIR = DATA_ROOT / "stories"
INDEX_PATH = DATA_ROOT / "index.json"

TITLE_PLACEHOLDERS = {"", "タイトル", "タイトルを設定してください"}
# 合法剧情 id = 前缀_数字；过滤掉 live2dcheck 之类的调试聚合条目（如 id 恰为 "hmr"）。
VALID_ID = re.compile(r"^(evs|hmn|hmr|mas|men)_\d+$")


def find_script_textasset(story_dir: Path):
    ta_dir = story_dir / "textassets"
    if not ta_dir.is_dir():
        return None
    story_id = story_dir.name
    # 优先与 story 目录同名的 txt，其次任意 looks_like_script 的
    candidates = sorted(ta_dir.glob("*.txt"), key=lambda p: (p.stem != story_id, p.name))
    for txt in candidates:
        try:
            text = txt.read_text(encoding="utf-8")
        except Exception:
            continue
        if looks_like_script(text):
            return txt
    return None


def script_title_from_commands(commands):
    for cmd in commands:
        if cmd.get("command") == "title":
            args = cmd.get("args") or []
            value = args[0].strip() if args else ""
            return None if value in TITLE_PLACEHOLDERS else value
    return None


def reparse_story(story_dir: Path, story: dict) -> bool:
    """回填空脚本 story 的 scripts/stats；返回是否修改。"""
    txt = find_script_textasset(story_dir)
    if not txt:
        return False
    text = txt.read_text(encoding="utf-8")
    parsed = parse_script(text)
    name = txt.stem
    entry = {
        "id": safe_name(name),
        "name": name,
        "bundle": "",
        "text": txt.relative_to(story_dir).as_posix(),
        "lineCount": len(text.splitlines()),
        **parsed,
    }
    story["scripts"] = [entry]
    story["primaryScript"] = entry["id"]
    story["commandDefinitions"] = {cmd: COMMAND_INFO.get(cmd, {}) for cmd in entry["commandCounts"]}
    story.setdefault("stats", {})
    story["stats"]["scriptCount"] = 1
    story["stats"]["messageCount"] = len(entry["messages"])
    return True


def main():
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    all_stories = index.get("stories", [])
    stories = [m for m in all_stories if VALID_ID.match(str(m.get("id", "")))]
    dropped = len(all_stories) - len(stories)
    index["stories"] = stories
    reparsed = 0
    for meta in stories:
        story_id = meta["id"]
        story_dir = STORIES_DIR / story_id
        story_path = story_dir / "story.json"
        if not story_path.is_file():
            continue
        story = json.loads(story_path.read_text(encoding="utf-8"))

        if story.get("stats", {}).get("scriptCount", 0) == 0 and reparse_story(story_dir, story):
            primary = story["scripts"][0] if story["scripts"] else None
            write_json(story_path, story)
            meta["title"] = make_title(primary, story_id)
            reparsed += 1

        meta["category"] = story_id.split("_", 1)[0]
        primary = (story.get("scripts") or [None])[0]
        commands = primary.get("commands", []) if primary else []
        meta["scriptTitle"] = script_title_from_commands(commands)
        meta["stats"] = story.get("stats", meta.get("stats"))

    write_json(INDEX_PATH, index)
    empty_left = sum(1 for m in stories if (m.get("stats") or {}).get("scriptCount", 0) == 0)
    with_title = sum(1 for m in stories if m.get("scriptTitle"))
    print(
        f"reparsed {reparsed} empty-script stories; dropped {dropped} invalid-id entries; "
        f"index has {len(stories)} stories; scriptCount==0 left: {empty_left}; "
        f"stories with real scriptTitle: {with_title}"
    )


if __name__ == "__main__":
    main()

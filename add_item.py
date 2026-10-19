"""ページの「追加」から作られた GitHub Issue の本文を読み、data/*.json に書き足す。

本文の形式（1行1項目）:
  type: collab | schedule | post | deadline | exclude
  url: ...
  start: 2026-11-03T20:00:00+09:00   (schedule)
  title: ... / game: ... / own: yes|no (schedule)
  time: ... / announce: yes|no        (post)
  deadline: ... / title: ...          (deadline)
"""
import json
import os
import re
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent


def load(name, default):
    p = DATA / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def save(name, obj):
    (DATA / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    body = os.environ.get("ISSUE_BODY", "")
    f = {}
    for line in body.splitlines():
        m = re.match(r"\s*([a-z]+):\s*(.+?)\s*$", line)
        if m and m.group(1) not in f:
            f[m.group(1)] = m.group(2)
    t = f.get("type")

    if t == "collab":
        url = f.get("url", "")
        if not re.search(r"(?:v=|youtu\.be/|/live/|/shorts/)[A-Za-z0-9_-]{11}", url):
            sys.exit("YouTube の URL が読み取れません")
        items = load("collab_videos.json", [])
        if url not in items:
            items.append(url)
        save("collab_videos.json", items)
        # 以前に除外していたら除外を解除
        vid = re.search(r"(?:v=|youtu\.be/|/live/|/shorts/)([A-Za-z0-9_-]{11})", url).group(1)
        ex = [x for x in load("collab_exclude.json", []) if vid not in x]
        save("collab_exclude.json", ex)

    elif t == "schedule":
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+09:00", f.get("start", "")):
            sys.exit("開始日時が読み取れません")
        item = {"start": f["start"], "title": f.get("title", "配信予定")[:200],
                "game": f.get("game", "その他"), "own": f.get("own", "yes") == "yes"}
        if f.get("url", "").startswith("https://"):
            item["url"] = f["url"]
        items = load("schedule.json", [])
        if item not in items:
            items.append(item)
        save("schedule.json", items)

    elif t == "post":
        url = f.get("url", "")
        if not re.search(r"(?:x|twitter)\.com/[^/]+/status/\d+", url):
            sys.exit("X の URL が読み取れません")
        announce = f.get("announce") == "yes"
        item = {"time": f.get("time"), "text": "配信告知" if announce else "ポスト", "url": url}
        if announce:
            item["kind"] = "announce"
        items = [p for p in load("posts.json", []) if p.get("url") != url]
        items.append(item)
        items.sort(key=lambda p: p.get("time") or "")
        save("posts.json", items)

    elif t == "exclude":
        url = f.get("url", "")
        m = re.search(r"(?:v=|youtu\.be/|/live/|/shorts/)([A-Za-z0-9_-]{11})", url)
        if not m:
            sys.exit("YouTube の URL が読み取れません")
        vid = m.group(1)
        ex = load("collab_exclude.json", [])
        if not any(vid in x for x in ex):
            ex.append(f"https://www.youtube.com/watch?v={vid}")
        save("collab_exclude.json", ex)
        # 手動で登録していた場合は登録も外す
        save("collab_videos.json", [x for x in load("collab_videos.json", []) if vid not in x])

    elif t == "deadline":
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+09:00", f.get("deadline", "")):
            sys.exit("締切日時が読み取れません")
        item = {"title": f.get("title", "締切")[:200], "deadline": f["deadline"]}
        if f.get("url", "").startswith("https://"):
            item["url"] = f["url"]
        items = load("deadlines.json", [])
        if item not in items:
            items.append(item)
        items.sort(key=lambda d: d["deadline"])
        save("deadlines.json", items)

    else:
        sys.exit("type が読み取れません")
    print("追加しました:", t)


if __name__ == "__main__":
    main()

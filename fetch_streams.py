"""YouTube から配信データを取得し、data/streams.json（表示用）を作り直す。

取得するもの
  1. 本人チャンネルの配信（終了済み・配信中・予定の枠）
  2. コラボ相手のチャンネルの配信のうち、タイトルか概要欄にキーワードを含むもの（自動）
  3. data/collab_videos.json に書いた動画（手動。他チャンネルの配信・動画）
  4. data/schedule.json に書いた予定（手動。枠が立つ前の告知など）

2・3 は、本人の配信と時間が重なっていれば「コラボ相手の枠」としてその配信に付け、
重なる本人の配信がなければ「本人の枠なし」の配信として単独で表示する。

環境変数 YOUTUBE_API_KEY が必要。FULL_REFRESH=1 か、config.json の since を変えると全件を取り直す。
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
KEY = os.environ.get("YOUTUBE_API_KEY")
FULL = os.environ.get("FULL_REFRESH") == "1"
API = "https://www.googleapis.com/youtube/v3/"
OWN_CACHE = DATA / "cache_own.json"          # 本人チャンネルの取得結果
PARTNER_CACHE = DATA / "cache_partners.json"  # 他チャンネルの取得結果
OUT = DATA / "streams.json"


# ---------- 共通 ----------
def call(endpoint, **params):
    params["key"] = KEY
    url = API + endpoint + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def load(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso_duration(s):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return timedelta(0)
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return timedelta(days=d, hours=h, minutes=mi, seconds=se)


def video_id(text):
    """URL または動画 ID から動画 ID を取り出す。"""
    text = text.strip()
    m = re.search(r"(?:v=|youtu\.be/|/live/|/shorts/)([A-Za-z0-9_-]{11})", text)
    if m:
        return m.group(1)
    return text if re.fullmatch(r"[A-Za-z0-9_-]{11}", text) else None


LEGACY = {"valo": "VALORANT", "apex": "APEX", "chat": "雑談", "other": "その他"}


def normalize_game(name):
    """表記ゆれをそろえる（config.json の game_aliases）。"""
    if not name:
        return "その他"
    name = LEGACY.get(name, name).strip()
    low = name.lower()
    for canon, words in CONFIG.get("game_aliases", {}).items():
        if any(w.lower() in low for w in words):
            return canon
    return name


def classify(title):
    """タイトル先頭の【】の中身をゲーム名とする。【】がなければ「その他」。"""
    m = re.search(r"【([^】]+)】", title or "")
    return normalize_game(m.group(1) if m else None)


KATA = "ァ-ヺー"
KANJI = "一-龥々"


def keyword_pattern(words):
    """キーワードの前後が同じ種類の文字（カタカナ・漢字）で続く場合は一致としない。
    例：「ロウ」は「ハロウィン」「フロウ」には当たらない。"""
    parts = []
    for w in words:
        p = re.escape(w)
        if re.match(f"[{KATA}]", w):
            p = f"(?<![{KATA}])" + p
        elif re.match(f"[{KANJI}]", w):
            p = f"(?<![{KANJI}])" + p
        if re.search(f"[{KATA}]$", w):
            p += f"(?![{KATA}])"
        elif re.search(f"[{KANJI}]$", w):
            p += f"(?![{KANJI}])"
        parts.append(p)
    return re.compile("|".join(parts), re.I) if parts else None


def resolve_channel(ref):
    """@ハンドル または UC で始まるチャンネル ID から (チャンネル名, アップロード再生リスト ID) を返す。"""
    if ref.startswith("UC"):
        res = call("channels", part="contentDetails,snippet", id=ref)
    else:
        res = call("channels", part="contentDetails,snippet", forHandle=ref)
    items = res.get("items") or []
    if not items:
        print(f"チャンネルが見つかりません: {ref}")
        return None, None
    return items[0]["snippet"]["title"], items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def list_uploads(playlist, since, known=None, max_pages=None):
    """アップロード一覧から動画 ID を新しい順に取得。since より古いか、known の ID に当たったら止める。"""
    ids, token, pages = [], None, 0
    while True:
        params = dict(part="contentDetails", playlistId=playlist, maxResults=50)
        if token:
            params["pageToken"] = token
        res = call("playlistItems", **params)
        pages += 1
        for it in res.get("items", []):
            vid = it["contentDetails"]["videoId"]
            pub = it["contentDetails"].get("videoPublishedAt")
            if pub and parse_time(pub) < since:
                return ids
            if known is not None and vid in known:
                return ids
            ids.append(vid)
        token = res.get("nextPageToken")
        if not token or (max_pages and pages >= max_pages):
            return ids


def list_uploads_matching(playlist, since, words, max_pages=None):
    """アップロード一覧のタイトルを見て、キーワードを含む動画 ID だけを返す（動画の多い公式チャンネル向け）。"""
    ids, token, pages = [], None, 0
    words = [w.lower() for w in words]
    while True:
        params = dict(part="snippet,contentDetails", playlistId=playlist, maxResults=50)
        if token:
            params["pageToken"] = token
        res = call("playlistItems", **params)
        pages += 1
        for it in res.get("items", []):
            pub = it["contentDetails"].get("videoPublishedAt")
            if pub and parse_time(pub) < since:
                return ids
            title = it["snippet"].get("title", "").lower()
            if any(w in title for w in words):
                ids.append(it["contentDetails"]["videoId"])
        token = res.get("nextPageToken")
        if not token or (max_pages and pages >= max_pages):
            return ids


def fetch_videos(ids):
    """動画情報を取得して正規化する。存在しない（削除・非公開）動画は結果に含まれない。"""
    out = {}
    ids = list(dict.fromkeys(ids))
    for i in range(0, len(ids), 50):
        res = call("videos", part="snippet,liveStreamingDetails,contentDetails", id=",".join(ids[i:i + 50]))
        for v in res.get("items", []):
            sn, live = v["snippet"], v.get("liveStreamingDetails") or {}
            thumbs = sn.get("thumbnails", {})
            rec = {
                "id": v["id"],
                "title": sn["title"],
                "description": sn.get("description", "")[:3000],
                "channel": sn["channelTitle"],
                "channel_id": sn["channelId"],
                "url": f"https://www.youtube.com/watch?v={v['id']}",
                "thumb": (thumbs.get("medium") or thumbs.get("high") or thumbs.get("default") or {}).get("url"),
            }
            if live.get("actualEndTime"):
                rec.update(kind="stream", status="done", start=live["actualStartTime"], end=live["actualEndTime"])
            elif live.get("actualStartTime"):
                rec.update(kind="stream", status="live", start=live["actualStartTime"], end=None)
            elif live.get("scheduledStartTime"):
                rec.update(kind="stream", status="upcoming", start=live["scheduledStartTime"], end=None)
            else:  # 通常の動画：公開時刻から動画の長さ分を表示
                start = parse_time(sn["publishedAt"])
                end = start + max(iso_duration(v.get("contentDetails", {}).get("duration")), timedelta(minutes=15))
                rec.update(kind="video", status="done", start=sn["publishedAt"],
                           end=end.isoformat().replace("+00:00", "Z"))
            out[v["id"]] = rec
    return out


# ---------- 1. 本人チャンネル ----------
def update_own(since):
    cache = {} if FULL else load(OWN_CACHE, {})
    name, playlist = resolve_channel(CONFIG["channel"])
    if not playlist:
        sys.exit("config.json の channel を確認してください。")
    print("本人チャンネル:", name)
    done_ids = {k for k, v in cache.items() if v["status"] == "done"}
    new_ids = list_uploads(playlist, since, known=done_ids or None)
    # 予定・配信中の枠は毎回取り直す（時刻変更・削除・終了を反映）
    pending = [k for k, v in cache.items() if v["status"] != "done"]
    for k in pending:
        cache.pop(k)
    got = fetch_videos(new_ids + pending)
    for vid, rec in got.items():
        if rec["kind"] == "video" and not CONFIG.get("include_own_videos", False):
            continue
        rec["own"] = True
        cache[vid] = rec
    save(OWN_CACHE, cache)
    print(f"本人: 新規・更新 {len(got)} 件 / 合計 {len(cache)} 件")
    return cache, name


# ---------- 2・3. 他チャンネル ----------
def update_partners(since, scanned):
    cache = {} if FULL else load(PARTNER_CACHE, {})
    exclude = {video_id(x) for x in load(DATA / "collab_exclude.json", [])}
    kw = keyword_pattern(CONFIG.get("collab_keywords", []))
    matches = lambda r: bool(kw and kw.search(r["title"] + "\n" + r.get("description", "")))
    # キーワードの条件を変えたときのため、自動で拾った分は毎回条件を当て直す
    for vid in [k for k, r in cache.items() if r.get("source") == "auto" and not matches(r)]:
        cache.pop(vid)
    to_fetch = []

    # 自動：コラボ相手チャンネルの新しい動画
    for ref in CONFIG.get("collab_channels", []):
        name, playlist = resolve_channel(ref)
        if not playlist:
            continue
        # 初めて見るチャンネルは since まで全件、2回目以降は最新50件だけ確認
        first_time = FULL or ref not in scanned
        ids = list_uploads(playlist, since, max_pages=None if first_time else 1)
        scanned.add(ref)
        to_fetch += [i for i in ids if i not in cache]
        print(f"コラボ相手 {name}: {len(ids)} 件を確認" + ("（初回のため全件）" if first_time else ""))
    auto_ids = set(to_fetch)

    # 指定チャンネル：タイトルにキーワードを含むものを、時間の重なりに関係なく取り込む（例：にじさんじ公式の MECHATU-A）
    watch_ids = set()
    for w in CONFIG.get("watch_channels", []):
        ref = w["channel"]
        name, playlist = resolve_channel(ref)
        if not playlist:
            continue
        key = "watch:" + ref + ":" + "|".join(w.get("keywords", []))
        first_time = FULL or key not in scanned
        ids = list_uploads_matching(playlist, since, w.get("keywords", []), max_pages=None if first_time else 2)
        scanned.add(key)
        watch_ids |= set(ids)
        to_fetch += [i for i in ids if i not in cache]
        print(f"指定チャンネル {name}: 該当 {len(ids)} 件" + ("（初回のため全件）" if first_time else ""))
    include_watch_videos = CONFIG.get("watch_include_videos", True)

    # 手動：collab_videos.json に書いた動画
    manual_ids = {video_id(x) for x in load(DATA / "collab_videos.json", [])} - {None}
    to_fetch += [i for i in manual_ids if i not in cache]
    # 予定・配信中は取り直す
    pending = [k for k, v in cache.items() if v["status"] != "done"]
    pending_src = {k: cache[k].get("source") for k in pending}  # 予定・配信中の取り直しでも出どころを保つ
    for k in pending:
        cache.pop(k)
    to_fetch += pending

    for vid, rec in fetch_videos(to_fetch).items():
        if vid in watch_ids:
            if rec["kind"] == "stream" or include_watch_videos:
                rec["source"] = "watch"
                cache[vid] = rec
        elif vid in manual_ids or vid in pending:
            if pending_src.get(vid) == "watch":
                rec["source"] = "watch"
            cache[vid] = rec
        elif vid in auto_ids and rec["kind"] == "stream":
            if matches(rec):
                cache[vid] = rec
    for vid in exclude:
        cache.pop(vid, None)
    for vid, rec in cache.items():
        rec["own"] = False
        if rec.get("source") != "watch" or vid in manual_ids:
            rec["source"] = "manual" if vid in manual_ids else "auto"
    save(PARTNER_CACHE, cache)
    print(f"他チャンネル: 合計 {len(cache)} 件")
    return cache


# ---------- 統合 ----------
def span(rec):
    s = parse_time(rec["start"])
    e = parse_time(rec["end"]) if rec.get("end") else s + timedelta(hours=2)
    return s, e


def public(rec, own_name):
    keep = ("id", "title", "kind", "status", "start", "end", "url", "thumb", "channel", "own")
    out = {k: rec.get(k) for k in keep}
    out["game"] = normalize_game(rec["game"]) if rec.get("game") else classify(rec["title"])
    out["collabs"] = rec.get("collabs", [])
    return out


def merge(own, partners, own_name):
    items = {k: dict(v, collabs=[]) for k, v in own.items()}
    min_overlap = timedelta(minutes=CONFIG.get("collab_min_overlap_minutes", 30))
    for p in sorted(partners.values(), key=lambda r: r["start"]):
        ps, pe = span(p)
        best, best_ov = None, timedelta(0)
        for o in items.values():
            if not o.get("own"):
                continue
            os_, oe = span(o)
            ov = min(pe, oe) - max(ps, os_)
            if ov > best_ov:
                best, best_ov = o, ov
        if best and best_ov >= min(min_overlap, (pe - ps) / 2):
            best["collabs"].append({"channel": p["channel"], "title": p["title"], "url": p["url"]})
        elif p.get("source") in ("manual", "watch"):
            # 本人の枠がない配信は、手動で登録したものと指定チャンネルのものだけ表示する
            items[p["id"]] = dict(p, collabs=[])
        # 自動で拾ったもので本人の配信と重ならないものは、無関係の可能性があるので表示しない

    # 手動の予定（枠が立ったら、同じ時間帯の枠があるので自動的に消える）
    for i, s in enumerate(load(DATA / "schedule.json", [])):
        st = parse_time(s["start"])
        own_flag = s.get("own", True)
        clash = any(abs(span(o)[0] - st) < timedelta(hours=3) for o in items.values())
        if clash:
            continue
        items[f"schedule-{i}"] = {
            "id": f"schedule-{i}", "title": s.get("title", "配信予定"), "kind": "stream", "status": "upcoming",
            "manual": True, "start": s["start"], "end": s.get("end"), "url": s.get("url", "https://x.com/"),
            "thumb": None, "channel": own_name if own_flag else s.get("channel", "他の人の枠"), "own": own_flag,
            "game": s.get("game"), "collabs": [],
        }

    # ゲーム分類の手動修正
    for vid, patch in load(DATA / "override.json", {}).items():
        if vid in items:
            items[vid].update(patch)

    out = []
    for rec in items.values():
        pub = public(rec, own_name)
        if rec.get("manual"):
            pub["manual"] = True
        out.append(pub)
    return sorted(out, key=lambda r: r["start"])


def main():
    if not KEY:
        sys.exit("環境変数 YOUTUBE_API_KEY が設定されていません。")
    since = datetime.fromisoformat(CONFIG["since"]).replace(tzinfo=timezone.utc)
    # config.json の since を変えたら、自動で全件を取り直す（前回の since を cache_meta.json に記録）
    global FULL
    meta = load(DATA / "cache_meta.json", {})
    if meta.get("since") != CONFIG["since"]:
        if meta:
            print(f"since が {meta.get('since')} → {CONFIG['since']} に変わったため全件を取り直します")
        FULL = True
    # config.json の channel を変えたら（別のライバー用にコピーしたときなど）、自動で全件を取り直す
    if meta.get("channel") != CONFIG["channel"]:
        print(f"channel が {meta.get('channel')} → {CONFIG['channel']} に変わったため全件を取り直します")
        FULL = True
    own, own_name = update_own(since)
    scanned = set() if FULL else set(meta.get("scanned_channels", []))
    partners = update_partners(since, scanned)
    data = merge(own, partners, own_name)
    save(OUT, data)
    save(DATA / "cache_meta.json", {"since": CONFIG["since"], "channel": CONFIG["channel"], "scanned_channels": sorted(scanned)})
    print(f"表示用データ: {len(data)} 件")


if __name__ == "__main__":
    main()

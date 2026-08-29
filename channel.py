"""채널 식별 + 영상 목록 수집."""

import datetime
import json
import os
import subprocess
import sys

from subtitle import BROWSER_UA

YOUTUBE_HOSTS = ("youtube.com", "youtu.be")
SEARCH_COUNT = 20
# 채널 탭의 flat-playlist는 업로드일을 비워두므로 근사 업로드일 추출기 옵션을 쓴다
APPROX_DATE_ARGS = ["--extractor-args", "youtubetab:approximate_date"]


def _run_ytdlp(args, timeout=600):
    """venv 안의 yt-dlp를 실행하고 (stdout, stderr, returncode)를 돌려준다."""
    # venv 밖의 yt-dlp를 잘못 집지 않도록 현재 인터프리터의 모듈로 실행한다
    cmd = [sys.executable, "-m", "yt_dlp",
           "--user-agent", BROWSER_UA, "--ignore-config"] + args
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.stdout, proc.stderr, proc.returncode


def _is_url(value):
    return value.startswith(("http://", "https://")) or any(
        host in value for host in YOUTUBE_HOSTS)


# 사용자가 직접 고른 콘텐츠 탭은 그대로 존중한다
CONTENT_TABS = ("/videos", "/shorts", "/streams", "/live")
# 영상 목록이 아닌 탭은 떼어내고 /videos 로 바꾼다
NON_CONTENT_TABS = ("/featured", "/about", "/playlists", "/community", "/podcasts")


def _normalize_channel_url(url):
    """채널 URL을 영상 목록 탭으로 정규화한다.

    - 플레이리스트 URL(?list=...)은 손대지 않는다.
      채널의 업로드 전체(쇼츠·라이브 포함)를 받고 싶을 때 쓰는 형태다.
    - /shorts, /streams 처럼 사용자가 명시한 탭은 그대로 둔다.
    - 그 밖에는 /videos 탭을 쓴다.
    """
    if "list=" in url:
        return url

    base = url.split("?")[0].rstrip("/")
    lowered = base.lower()

    for tab in CONTENT_TABS:
        if lowered.endswith(tab):
            return base
    for tab in NON_CONTENT_TABS:
        if lowered.endswith(tab):
            return base[: -len(tab)] + "/videos"
    return base + "/videos"


def search_channels(query):
    """채널명으로 검색해 후보 채널 목록을 뽑는다 (channel_url 기준 중복 제거)."""
    stdout, stderr, code = _run_ytdlp([
        "--flat-playlist", "-J", "ytsearch{}:{}".format(SEARCH_COUNT, query)])
    if code != 0 or not stdout.strip():
        raise RuntimeError("채널 검색 실패: {}".format(stderr.strip()[:300]))

    data = json.loads(stdout)
    candidates = []
    by_url = {}
    for entry in data.get("entries") or []:
        url = entry.get("channel_url") or entry.get("uploader_url")
        name = entry.get("channel") or entry.get("uploader")
        if not url:
            continue

        if url not in by_url:
            by_url[url] = {
                "name": name or "(이름 없음)",
                "url": url,
                "sample_title": "",
                "hits": 0,
            }
            candidates.append(by_url[url])

        candidate = by_url[url]
        candidate["hits"] += 1
        # 채널 항목의 title은 채널명이므로 영상 항목의 제목을 예시로 쓴다
        if not candidate["sample_title"] and entry.get("ie_key") == "Youtube":
            candidate["sample_title"] = entry.get("title") or ""

    return candidates


def choose_channel(query):
    """채널명 검색 결과를 번호로 보여주고 사용자가 고르게 한다."""
    candidates = search_channels(query)
    if not candidates:
        raise RuntimeError("'{}' 로 검색된 채널이 없습니다.".format(query))

    print("\n'{}' 검색 결과 — 채널을 선택하세요:".format(query))
    for i, c in enumerate(candidates, 1):
        print("  {:2d}. {}  (검색결과 {}건)".format(i, c["name"], c["hits"]))
        print("      {}".format(c["url"]))
        if c["sample_title"]:
            print("      예시 영상: {}".format(c["sample_title"][:60]))

    while True:
        try:
            answer = input("\n번호 입력 (취소는 q): ").strip()
        except EOFError:
            raise SystemExit("\n입력을 받을 수 없습니다. 채널 URL을 직접 지정해 주세요.")
        if answer.lower() == "q":
            raise SystemExit("취소했습니다.")
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            return candidates[int(answer) - 1]
        print("1 ~ {} 사이의 번호를 입력하세요.".format(len(candidates)))


def resolve_channel(query):
    """입력값이 URL이면 그대로, 채널명이면 검색 후 선택하게 한다.

    Returns:
        {"name": 채널명 또는 None, "url": /videos 탭 URL}
    """
    if _is_url(query):
        return {"name": None, "url": _normalize_channel_url(query)}
    chosen = choose_channel(query)
    return {"name": chosen["name"], "url": _normalize_channel_url(chosen["url"])}


def _upload_date(entry):
    """flat-playlist 항목에서 업로드일(YYYYMMDD)을 뽑는다. 없으면 00000000."""
    raw = entry.get("upload_date")
    if raw:
        return str(raw)
    for key in ("timestamp", "release_timestamp"):
        ts = entry.get(key)
        if ts:
            return datetime.datetime.fromtimestamp(
                ts, datetime.timezone.utc).strftime("%Y%m%d")
    return "00000000"


def fetch_video_list(channel_url, out_dir):
    """채널 전체 영상의 video_id / 제목 / 업로드일을 수집해 videos.json에 저장한다.

    videos.json이 이미 존재하면 무조건 재사용한다 (새로 수집하려면 수동 삭제).
    """
    path = os.path.join(out_dir, "videos.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        print("videos.json 재사용 — 영상 {}개 "
              "(새로 수집하려면 {} 를 삭제하세요)".format(len(saved["videos"]), path))
        return saved

    print("영상 목록 수집 중... ({})".format(channel_url))
    stdout, stderr, code = _run_ytdlp(
        ["--flat-playlist", "-J"] + APPROX_DATE_ARGS + [channel_url])
    if code != 0 or not stdout.strip():
        raise RuntimeError("영상 목록 수집 실패: {}".format(stderr.strip()[:500]))

    data = json.loads(stdout)
    videos = []
    for entry in data.get("entries") or []:
        video_id = entry.get("id")
        if not video_id:
            continue
        videos.append({
            "video_id": video_id,
            "title": entry.get("title") or video_id,
            "upload_date": _upload_date(entry),
            "url": "https://www.youtube.com/watch?v={}".format(video_id),
        })

    result = {
        "channel_name": data.get("channel") or data.get("uploader") or "channel",
        "channel_url": channel_url,
        "videos": videos,
    }
    os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result

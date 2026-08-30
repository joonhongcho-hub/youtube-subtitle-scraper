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
                "thumbnail": _best_thumbnail(entry),
                # 검색 결과에는 구독자 수가 없다. 채널을 고른 뒤 개수를 셀 때 채워진다.
                "subscribers": None,
                "hits": 0,
            }
            candidates.append(by_url[url])

        candidate = by_url[url]
        candidate["hits"] += 1
        if not candidate["thumbnail"]:
            candidate["thumbnail"] = _best_thumbnail(entry)
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


def _best_thumbnail(entry):
    """항목에서 가장 큰 썸네일 URL을 고른다.

    유튜브는 //yt3.ggpht.com/... 처럼 프로토콜 없는 URL을 주므로 https를 붙인다.
    """
    thumbnails = entry.get("thumbnails") or []
    if not thumbnails:
        return None
    url = thumbnails[-1].get("url")
    if not url:
        return None
    return "https:" + url if url.startswith("//") else url


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


# 채널 탭 이름 → 캐시 파일명
SOURCE_TABS = {
    "videos": "videos.json",
    "shorts": "shorts.json",
    "streams": "streams.json",
}
# 쇼츠는 유튜브가 목록에 길이·업로드일을 주지 않아 필터를 적용할 수 없다
SOURCES_WITHOUT_METADATA = ("shorts",)


def _to_video(entry, order=0):
    """flat-playlist 항목을 내부 영상 레코드로 바꾼다.

    order는 유튜브가 준 목록 순서다. 각 탭이 최신순으로 오므로 이 순서 자체가
    "채널 기본 순서"이며, 업로드일이 없는 쇼츠를 정렬할 때 유일한 기준이 된다.
    """
    video_id = entry.get("id")
    if not video_id:
        return None
    return {
        "video_id": video_id,
        "title": entry.get("title") or video_id,
        "upload_date": _upload_date(entry),
        # 필터·정렬에 쓰이므로 버리지 않고 남긴다 (쇼츠는 duration이 None)
        "duration": entry.get("duration"),
        "view_count": entry.get("view_count"),
        "order": order,
        "url": "https://www.youtube.com/watch?v={}".format(video_id),
    }


# 영상 레코드 형식이 바뀌면 올린다. 옛 캐시는 새 필드가 없어 정렬·필터가
# 조용히 무시되므로, 버전이 다르면 자동으로 다시 받는다.
CACHE_VERSION = 2


def fetch_entries(url, cache_path, force=False, log=None):
    """URL의 영상 목록을 수집해 cache_path에 저장한다.

    캐시가 있으면 재사용한다. force=True면 캐시를 무시하고 다시 받는다 —
    웹 UI의 "목록 새로고침"이 이 경로를 쓴다. CLI는 기본값 그대로 재사용한다.
    """
    log = log or (lambda level, message: None)

    if not force and os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            saved = json.load(f)
        if saved.get("cache_version") == CACHE_VERSION:
            log("info", "목록 재사용 — {}개 ({})".format(
                len(saved["videos"]), os.path.basename(cache_path)))
            return saved
        log("info", "목록 형식이 바뀌어 다시 받습니다 ({})".format(
            os.path.basename(cache_path)))

    log("info", "목록 수집 중... ({})".format(url))
    stdout, stderr, code = _run_ytdlp(
        ["--flat-playlist", "-J"] + APPROX_DATE_ARGS + [url])
    if code != 0 or not stdout.strip():
        raise RuntimeError("목록 수집 실패: {}".format(stderr.strip()[:500]))

    data = json.loads(stdout)
    videos = []
    for index, entry in enumerate(data.get("entries") or []):
        video = _to_video(entry, order=index)
        if video:
            videos.append(video)

    result = {
        "cache_version": CACHE_VERSION,
        "channel_name": data.get("channel") or data.get("uploader") or "channel",
        "channel_url": url,
        "videos": videos,
        "fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result


def channel_base(url):
    """어떤 형태의 채널 URL이든 탭 없는 기본 URL로 만든다.

    _normalize_channel_url()이 붙인 탭을 떼어낸다. 단순히 마지막 / 뒤를 자르면
    ?list= 플레이리스트 URL이 "https://www.youtube.com" 으로 깨지므로 따로 다룬다.
    """
    normalized = _normalize_channel_url(url)
    if "list=" in normalized:
        return normalized
    lowered = normalized.lower()
    for tab in CONTENT_TABS:
        if lowered.endswith(tab):
            return normalized[: -len(tab)]
    return normalized


def fetch_source(channel_base_url, source, out_dir, force=False, log=None):
    """채널의 특정 탭(videos/shorts/streams) 목록을 가져온다."""
    tab_url = "{}/{}".format(channel_base_url.rstrip("/"), source)
    cache = os.path.join(out_dir, SOURCE_TABS[source])
    return fetch_entries(tab_url, cache, force=force, log=log)


def fetch_playlist(playlist_id, out_dir, force=False, log=None):
    """재생목록 하나의 영상 목록을 가져온다."""
    url = "https://www.youtube.com/playlist?list={}".format(playlist_id)
    cache = os.path.join(out_dir, "playlist_{}.json".format(playlist_id))
    return fetch_entries(url, cache, force=force, log=log)


def fetch_channel_meta(url):
    """채널 썸네일·구독자 수를 싸게 가져온다 (약 1초).

    --playlist-items 0 은 항목을 하나도 받지 않아 빠르지만, 유튜브가 총개수를
    주지 않으므로 영상 개수는 알 수 없다. 개수는 목록을 통째로 받아야 한다.
    """
    stdout, stderr, code = _run_ytdlp(
        ["--flat-playlist", "--playlist-items", "0", "-J", url], timeout=120)
    if code != 0 or not stdout.strip():
        raise RuntimeError("채널 정보 수집 실패: {}".format(stderr.strip()[:300]))

    data = json.loads(stdout)
    return {
        "name": data.get("channel") or data.get("uploader") or "",
        "url": data.get("channel_url") or url,
        "subscribers": data.get("channel_follower_count"),
        "thumbnail": _best_thumbnail(data),
    }


def fetch_playlists(channel_base_url):
    """채널의 재생목록 목록. 항목 수는 주지 않으므로 제목·ID만 돌려준다."""
    url = "{}/playlists".format(channel_base_url.rstrip("/"))
    stdout, stderr, code = _run_ytdlp(
        ["--flat-playlist", "-J", url], timeout=180)
    if code != 0 or not stdout.strip():
        raise RuntimeError("재생목록 수집 실패: {}".format(stderr.strip()[:300]))

    data = json.loads(stdout)
    playlists = []
    for entry in data.get("entries") or []:
        pid = entry.get("id")
        if not pid:
            continue
        playlists.append({"playlist_id": pid, "title": entry.get("title") or pid})
    return playlists


def fetch_video_list(channel_url, out_dir):
    """CLI 진입점 — 채널의 일반 영상 목록을 videos.json으로 관리한다."""
    path = os.path.join(out_dir, "videos.json")
    reused = os.path.exists(path)
    result = fetch_entries(channel_url, path,
                           log=lambda level, message: print(message))
    if reused:
        print("(새로 수집하려면 {} 를 삭제하세요)".format(path))
    return result

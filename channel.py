"""채널 식별 + 영상 목록 수집."""

import datetime
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

import storage
from subtitle import BROWSER_UA

YOUTUBE_HOSTS = ("youtube.com", "youtu.be")
SEARCH_COUNT = 20
# 채널 탭의 flat-playlist는 업로드일을 비워두므로 근사 업로드일 추출기 옵션을 쓴다
APPROX_DATE_ARGS = ["--extractor-args", "youtubetab:approximate_date"]


def _ytdlp_command(args):
    # venv 밖의 yt-dlp를 잘못 집지 않도록 현재 인터프리터의 모듈로 실행한다
    return [sys.executable, "-m", "yt_dlp",
            "--user-agent", BROWSER_UA, "--ignore-config"] + args


def _run_ytdlp(args, timeout=600):
    """venv 안의 yt-dlp를 실행하고 (stdout, stderr, returncode)를 돌려준다."""
    proc = subprocess.run(_ytdlp_command(args), capture_output=True,
                          text=True, timeout=timeout)
    return proc.stdout, proc.stderr, proc.returncode


# 감시 스레드가 프로세스 상태를 들여다보는 간격
WATCHDOG_POLL_SECONDS = 0.2


def _stream_ytdlp(args, timeout, on_line, cancel=None):
    """yt-dlp를 띄우고 stdout을 한 줄씩 on_line에 넘긴다.

    _run_ytdlp와 달리 끝나기를 기다리지 않으므로 진행 상황을 그때그때 알릴 수
    있다. 대신 두 가지를 직접 챙겨야 한다.

    - stderr는 별도 스레드로 계속 비운다. 재시도 경고가 파이프 버퍼를 채우면
      yt-dlp가 쓰지 못해 그대로 멈춰 선다.
    - 시간 초과·취소는 감시 스레드가 프로세스를 죽여서 알린다. stdout 읽기는
      막혀 있을 수 있어 읽는 쪽에서 시계를 볼 수 없다.

    Returns:
        (stderr_text, returncode, stopped) — stopped는 왜 끝났는지를 담은
        {"timeout": bool, "cancelled": bool}
    """
    # 파이프로 넘길 때 파이썬이 출력을 뭉쳐 두면 진행 상황이 끝에 몰려 나온다
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(_ytdlp_command(args), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1,
                            env=env)
    errors = []
    stopped = {"timeout": False, "cancelled": False}

    def drain_stderr():
        for line in proc.stderr:
            errors.append(line)

    def watchdog():
        deadline = time.time() + timeout
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                stopped["cancelled"] = True
                proc.kill()
                return
            if time.time() >= deadline:
                stopped["timeout"] = True
                proc.kill()
                return
            time.sleep(WATCHDOG_POLL_SECONDS)

    threading.Thread(target=drain_stderr, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()

    for line in proc.stdout:
        on_line(line)
    proc.wait()
    return "".join(errors), proc.returncode, stopped


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
    """채널의 특정 탭(videos/shorts/streams) 목록을 가져온다.

    목록 캐시는 채널 폴더가 아니라 앱 안쪽에 둔다 — 사용자가 고른 저장 폴더에는
    자막 .txt만 남아야 한다.
    """
    tab_url = "{}/{}".format(channel_base_url.rstrip("/"), source)
    cache = os.path.join(storage.state_dir(out_dir), SOURCE_TABS[source])
    return fetch_entries(tab_url, cache, force=force, log=log)


def fetch_playlist(playlist_id, out_dir, force=False, log=None):
    """재생목록 하나의 영상 목록을 가져온다."""
    url = "https://www.youtube.com/playlist?list={}".format(playlist_id)
    cache = os.path.join(storage.state_dir(out_dir),
                         "playlist_{}.json".format(playlist_id))
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


# 채널 '정보' 탭에서 총개수를 읽는 데 쓰는 값들.
ABOUT_TIMEOUT = 15
# 개수가 들어 있는 구조의 이름. 같은 페이지에 붙는 추천 채널 카드도
# videoCountText를 갖고 있어서, 이 이름으로 찾아야 남의 개수를 집지 않는다.
ABOUT_NODE = "aboutChannelViewModel"


def _initial_data(html):
    """페이지에 박혀 있는 ytInitialData를 꺼낸다."""
    match = re.search(r"ytInitialData\s*=\s*(\{.*?\});</script>", html, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except ValueError:
        return None


def _find_node(node, key):
    """중첩된 어디에 있든 key를 가진 값을 찾아 돌려준다."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            found = _find_node(value, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_node(value, key)
            if found is not None:
                return found
    return None


def fetch_channel_total(channel_url):
    """채널의 총 영상 수(일반+쇼츠+라이브)를 '정보' 탭에서 가져온다 (약 0.6초).

    탭별 목록을 통째로 받으면 20초가 넘게 걸리는데, 유튜브 자신은 '정보' 패널에
    총개수를 갖고 있다. 화면이 그 20초 동안 빈 채로 있지 않게 이 값을 먼저 띄운다.

    문서화된 경로가 아니라 페이지 구조가 바뀌면 조용히 못 찾는다. 그래서 무슨 일이
    있어도 예외를 올리지 않고 None만 돌려준다 — 화면은 지금처럼 탭별 집계를
    기다리면 되고, 없어도 수집은 아무 지장이 없다.
    """
    url = "{}/about".format(channel_url.rstrip("/"))
    try:
        request = urllib.request.Request(url, headers={
            "User-Agent": BROWSER_UA,
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(request, timeout=ABOUT_TIMEOUT) as response:
            html = response.read().decode("utf-8", "replace")
    except Exception:
        return None

    data = _initial_data(html)
    about = _find_node(data, ABOUT_NODE) if data else None
    if not isinstance(about, dict):
        return None
    # '정보' 패널은 "동영상 2,576개" 같은 문자열이고, 추천 채널 카드는 runs 객체다.
    # 숫자만 뽑으므로 "2,576 videos" 처럼 언어가 달라도 그대로 걸린다.
    text = about.get("videoCountText")
    if not isinstance(text, str):
        return None
    match = re.search(r"[\d,]+", text)
    if not match:
        return None
    try:
        return int(match.group(0).replace(",", ""))
    except ValueError:
        return None


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


# 영상 하나를 해석할 때 뽑는 항목. 저장에 쓰는 키(제목·업로드일)가 반드시 있어야
# 하므로 _to_video()와 같은 모양으로 맞춘다.
VIDEO_PRINT_FIELDS = ("original_url", "id", "title", "upload_date",
                      "channel", "channel_url", "duration", "view_count")
# 제목에 |가 들어갈 수 있어 구분자는 흔치 않은 문자로 둔다
PRINT_SEPARATOR = "\x1f"


def _print_template():
    return PRINT_SEPARATOR.join("%({})s".format(f) for f in VIDEO_PRINT_FIELDS)


def _as_number(text):
    """yt-dlp가 값이 없을 때 주는 'NA'를 None으로 바꾼다."""
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


# 링크 해석은 사람이 화면 앞에서 기다리는 작업이다. 유튜브가 응답하지 않을 때
# 기본 재시도를 다 돌면 링크 하나에 몇 분씩 걸리므로 짧게 끊는다.
RESOLVE_SOCKET_TIMEOUT = 15
RESOLVE_RETRIES = 1


def resolve_videos(urls, timeout=None, on_progress=None, cancel=None):
    """영상 링크 여러 개를 한 번의 yt-dlp 호출로 해석한다.

    링크마다 따로 부르면 하나에 몇 초씩 걸려 10개만 돼도 한참이다. yt-dlp는
    URL을 여러 개 받으면 한 번에 처리하므로 프로세스 하나로 끝낸다.

    --no-playlist 는 watch?v=..&list=.. 형태에서 그 영상 하나만 집게 한다.
    --ignore-errors 로 죽은 링크가 있어도 나머지는 계속 간다. 실패한 링크는
    stdout에 줄이 안 나오므로, %(original_url)s로 짝지어 빠진 것을 찾아낸다.

    사람이 화면 앞에서 기다리는 작업이라 결과를 한꺼번에 받지 않고 흘려 읽는다.
    on_progress(done, total)로 몇 개까지 됐는지 알리고, 시간이 다 되거나
    cancel이 서면 그때까지 건진 것을 그대로 돌려준다 — 전부 버리고 다시
    시작하게 만들면 기다린 시간이 통째로 사라진다.

    Returns:
        (videos, failed) — videos는 _to_video()와 같은 모양에 channel_name·
        channel_url이 붙은 레코드, failed는 해석하지 못한 입력 URL 목록
    """
    urls = list(urls)
    if not urls:
        return [], []
    # 링크가 많으면 그만큼은 기다려 주되, 전체 상한을 둔다
    timeout = timeout or min(600, 30 + 25 * len(urls))

    videos = []
    seen_inputs = set()

    def take(line):
        parts = line.rstrip("\n").split(PRINT_SEPARATOR)
        if len(parts) != len(VIDEO_PRINT_FIELDS):
            return
        (original, video_id, title, upload_date,
         channel_name, channel_url, duration, view_count) = parts
        if not video_id or video_id == "NA":
            return
        seen_inputs.add(original)
        videos.append({
            "video_id": video_id,
            "title": title or video_id,
            # 업로드일이 없으면 목록 쪽과 같은 표시(00000000)를 쓴다
            "upload_date": upload_date if upload_date.isdigit() else "00000000",
            "duration": _as_number(duration),
            "view_count": _as_number(view_count),
            "order": len(videos),
            "url": "https://www.youtube.com/watch?v={}".format(video_id),
            "channel_name": channel_name if channel_name != "NA" else "",
            "channel_url": channel_url if channel_url != "NA" else "",
        })
        if on_progress:
            on_progress(len(videos), len(urls))

    stderr, code, stopped = _stream_ytdlp(
        ["--skip-download", "--no-playlist", "--ignore-errors",
         "--socket-timeout", str(RESOLVE_SOCKET_TIMEOUT),
         "--retries", str(RESOLVE_RETRIES),
         "--extractor-retries", str(RESOLVE_RETRIES),
         "--print", _print_template()] + urls,
        timeout, take, cancel=cancel)

    failed = [u for u in urls if u not in seen_inputs]
    # 하나라도 건졌으면 그것을 돌려준다. 나머지는 failed에 담겨 화면에 그대로
    # 나열되므로, 어떤 링크가 빠졌는지 사용자가 보고 다시 걸 수 있다.
    if not videos and not stopped["cancelled"]:
        if stopped["timeout"]:
            raise RuntimeError(
                "영상 정보를 가져오지 못했습니다 — 유튜브 응답이 너무 느립니다. "
                "잠시 뒤 다시 시도해 주세요.")
        if code != 0:
            raise RuntimeError("영상 정보를 가져오지 못했습니다 — {}".format(
                _ytdlp_reason(stderr)))
    return videos, failed

def _ytdlp_reason(stderr):
    """yt-dlp가 쏟아낸 출력에서 사람이 읽을 한 줄을 고른다.

    재시도 경고가 수십 줄씩 쌓여 그대로 보여주면 무슨 일인지 알 수 없다.
    ERROR 줄이 있으면 그것을, 없으면 마지막 줄을 쓴다.
    """
    lines = [l.strip() for l in (stderr or "").splitlines() if l.strip()]
    if not lines:
        return "알 수 없는 오류"
    errors = [l for l in lines if l.startswith("ERROR:")]
    line = errors[-1] if errors else lines[-1]
    if "timed out" in line.lower() or "timeout" in line.lower():
        return "유튜브 응답이 없습니다. 잠시 뒤 다시 시도해 주세요."
    return line[:200]


def fetch_video_list(channel_url, out_dir):
    """CLI 진입점 — 채널의 일반 영상 목록을 videos.json으로 관리한다."""
    path = os.path.join(storage.state_dir(out_dir), "videos.json")
    reused = os.path.exists(path)
    result = fetch_entries(channel_url, path,
                           log=lambda level, message: print(message))
    if reused:
        print("(새로 수집하려면 {} 를 삭제하세요)".format(path))
    return result

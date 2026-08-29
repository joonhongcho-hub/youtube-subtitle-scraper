"""자막 수집 — youtube-transcript-api 기본 경로 + yt-dlp 폴백."""

import requests
from youtube_transcript_api import YouTubeTranscriptApi

# 일반 브라우저 User-Agent (IP/봇 차단 완화)
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 사전 검증용 프로브 영상 (자막이 확실히 있는 공개 영상)
PROBE_VIDEO_IDS = [
    ("dQw4w9WgXcQ", "Rick Astley - Never Gonna Give You Up"),
    ("jNQXAC9IVRw", "Me at the zoo"),
]

MODE_API = "transcript_api"
MODE_YTDLP = "ytdlp_only"


def make_session():
    """브라우저 UA를 심은 requests 세션."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": BROWSER_UA,
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    })
    return session


def make_api():
    """youtube-transcript-api 클라이언트. (1.x 인스턴스 API)"""
    return YouTubeTranscriptApi(http_client=make_session())


def verify_transcript_api(verbose=True):
    """[안전장치 B] 본 작업 전 youtube-transcript-api가 실제로 동작하는지 확인한다.

    프로브 영상으로 실제 요청을 보내 성공하면 MODE_API,
    모두 실패하면 MODE_YTDLP(단독 경로)로 자동 전환한다.

    Returns:
        (mode, sample) — mode는 MODE_API 또는 MODE_YTDLP,
        sample은 성공 시 가져온 자막 앞부분 (실패 시 None)
    """
    if verbose:
        print("[사전 검증] youtube-transcript-api 동작 확인 중...")

    api = make_api()
    errors = []

    for video_id, label in PROBE_VIDEO_IDS:
        try:
            fetched = api.fetch(video_id, languages=["en", "ko"])
            snippets = list(fetched)
            if not snippets:
                raise RuntimeError("빈 자막 응답")
            sample = " ".join(s.text for s in snippets[:5])
            if verbose:
                print("  OK  {} ({})".format(video_id, label))
                print("      언어: {} / 자동생성: {} / 조각 {}개".format(
                    fetched.language_code, fetched.is_generated, len(snippets)))
                print("      샘플: {}".format(sample[:120]))
                print("[사전 검증] 통과 → transcript-api 기본 경로로 진행합니다.")
            return MODE_API, sample
        except Exception as exc:
            errors.append("  FAIL {} ({}): {}: {}".format(
                video_id, label, type(exc).__name__, str(exc).strip().splitlines()[0][:160]))

    if verbose:
        print("\n".join(errors))
        print("[사전 검증] 실패 → yt-dlp 단독 경로로 자동 전환합니다.")
    return MODE_YTDLP, None


if __name__ == "__main__":
    mode, _ = verify_transcript_api()
    print("\n판정된 모드: {}".format(mode))


# --- 자막 수집 -----------------------------------------------------------

import glob
import json
import os
import subprocess
import sys
import tempfile
import time

from youtube_transcript_api import (
    AgeRestricted, IpBlocked, NoTranscriptFound, PoTokenRequired,
    RequestBlocked, TranscriptsDisabled, VideoUnavailable, VideoUnplayable,
    YouTubeRequestFailed,
)

import cleaner

# 실패 사유 — 재시도 불가
NO_SUBTITLES = "NO_SUBTITLES"
PRIVATE_VIDEO = "PRIVATE_VIDEO"
# 실패 사유 — 재시도 가능
FETCH_FAILED = "FETCH_FAILED"
RATE_LIMITED = "RATE_LIMITED"
TIMEOUT = "TIMEOUT"

SUCCESS = "OK"
NON_RETRYABLE = (NO_SUBTITLES, PRIVATE_VIDEO)
RETRYABLE = (FETCH_FAILED, RATE_LIMITED, TIMEOUT)

YTDLP_TIMEOUT = 180
# 차단 응답 시 exponential backoff
BACKOFF_SECONDS = (1, 2, 4)

# yt-dlp stderr에서 비공개/삭제/멤버십 전용을 판별하는 표지
PRIVATE_MARKERS = (
    "private video", "members-only", "members only", "video unavailable",
    "has been removed", "account associated with this video has been terminated",
    "this video is not available", "sign in to confirm your age",
)
RATE_MARKERS = ("http error 429", "too many requests", "rate-limit", "rate limit")


class SubtitleResult(object):
    """자막 수집 결과."""

    def __init__(self, status, text=None, lang=None, source=None, detail=None):
        self.status = status
        self.text = text
        self.lang = lang
        self.source = source
        self.detail = detail

    @property
    def ok(self):
        return self.status == SUCCESS


def _classify_ytdlp_error(stderr):
    low = stderr.lower()
    if any(m in low for m in PRIVATE_MARKERS):
        return PRIVATE_VIDEO
    if any(m in low for m in RATE_MARKERS):
        return RATE_LIMITED
    return FETCH_FAILED


def fetch_via_api(video_id, langs, api=None):
    """youtube-transcript-api 경로. 수동 자막 → 자동생성 자막 순으로 찾는다."""
    api = api or make_api()
    try:
        listing = api.list(video_id)
        try:
            transcript = listing.find_manually_created_transcript(langs)
        except NoTranscriptFound:
            transcript = listing.find_generated_transcript(langs)
        fetched = transcript.fetch()
    except (TranscriptsDisabled, NoTranscriptFound) as exc:
        return SubtitleResult(NO_SUBTITLES, detail=type(exc).__name__)
    except (VideoUnavailable, VideoUnplayable, AgeRestricted) as exc:
        return SubtitleResult(PRIVATE_VIDEO, detail=type(exc).__name__)
    except (RequestBlocked, IpBlocked) as exc:
        return SubtitleResult(RATE_LIMITED, detail=type(exc).__name__)
    except (PoTokenRequired, YouTubeRequestFailed) as exc:
        return SubtitleResult(FETCH_FAILED, detail=type(exc).__name__)
    except Exception as exc:
        return SubtitleResult(
            FETCH_FAILED,
            detail="{}: {}".format(type(exc).__name__, str(exc)[:120]))

    text = cleaner.clean_snippets_text(list(fetched))
    if not text.strip():
        return SubtitleResult(NO_SUBTITLES, detail="빈 자막")
    source = "auto" if transcript.is_generated else "manual"
    return SubtitleResult(SUCCESS, text=text,
                          lang=fetched.language_code, source="api/" + source)


# 브라우저 위장(impersonation) 대상 — 429 차단을 크게 줄여준다
IMPERSONATE_TARGET = "Chrome-136:Macos-15"
_impersonate_ok = None


def impersonate_args():
    """curl_cffi 기반 위장이 가능하면 --impersonate 인자를 돌려준다."""
    global _impersonate_ok
    if _impersonate_ok is None:
        try:
            import curl_cffi  # noqa: F401
            _impersonate_ok = True
        except ImportError:
            _impersonate_ok = False
    return ["--impersonate", IMPERSONATE_TARGET] if _impersonate_ok else []


def _pick_vtt(tmpdir, langs, manual_langs):
    """받은 .vtt 중 우선순위가 가장 높은 파일을 고른다.

    수동 자막이 자동생성보다 우선하고, 같은 종류면 langs 순서를 따른다.
    manual_langs는 yt-dlp가 알려준 수동 자막 언어 코드 집합이다.
    """
    found = glob.glob(os.path.join(tmpdir, "*.vtt"))
    if not found:
        return None, None, None

    def lang_of(path):
        # {video_id}.{lang}.vtt
        parts = os.path.basename(path).split(".")
        return parts[-2] if len(parts) >= 3 else ""

    def kind_of(lang):
        return "manual" if lang in manual_langs else "auto"

    for want in ("manual", "auto"):
        for lang in langs:
            for path in found:
                actual = lang_of(path)
                if kind_of(actual) != want:
                    continue
                if actual == lang or actual.startswith(lang + "-"):
                    return path, actual, want

    # 우선순위에 없는 언어라도 있으면 쓴다
    path = found[0]
    actual = lang_of(path)
    return path, actual, kind_of(actual)


def fetch_via_ytdlp(video_id, langs):
    """yt-dlp 폴백 경로. 자막만 받고 영상은 받지 않는다.

    -J --no-simulate 로 자막 파일을 받으면서 메타데이터도 함께 얻는다.
    메타데이터의 subtitles 키가 수동 자막 언어 목록이라, 어느 파일이 수동이고
    어느 것이 자동생성인지 로그 문자열에 의존하지 않고 정확히 구분할 수 있다.
    """
    url = "https://www.youtube.com/watch?v={}".format(video_id)
    with tempfile.TemporaryDirectory() as tmpdir:
        cmd = [
            sys.executable, "-m", "yt_dlp", "--ignore-config",
            "--user-agent", BROWSER_UA,
        ] + impersonate_args() + [
            "--skip-download", "--write-subs", "--write-auto-subs",
            "--sub-langs", ",".join(langs), "--sub-format", "vtt",
            # 한 언어에서 실패해도 나머지 언어는 계속 받는다
            "--ignore-errors", "--retries", "3", "--extractor-retries", "3",
            "-J", "--no-simulate",
            "-o", os.path.join(tmpdir, "%(id)s.%(ext)s"), url,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=YTDLP_TIMEOUT)
        except subprocess.TimeoutExpired:
            return SubtitleResult(TIMEOUT, detail="yt-dlp 시간 초과")

        # stdout에 JSON이 여러 줄 섞여 나올 수 있으므로 줄 단위로 훑는다
        manual_langs = set()
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                info = json.loads(line)
            except ValueError:
                continue
            if isinstance(info, dict) and "subtitles" in info:
                manual_langs = set((info.get("subtitles") or {}).keys())
                break

        # --ignore-errors 때문에 일부 언어만 실패해도 returncode가 0이 아닐 수 있다.
        # 파일이 하나라도 받아졌으면 성공으로 본다.
        path, lang, kind = _pick_vtt(tmpdir, langs, manual_langs)
        if path is None:
            if proc.returncode != 0:
                reason = _classify_ytdlp_error(proc.stderr)
                return SubtitleResult(reason, detail=proc.stderr.strip()[:200])
            return SubtitleResult(NO_SUBTITLES, detail="자막 파일 없음")

        with open(path, encoding="utf-8", errors="replace") as f:
            text = cleaner.clean_vtt_text(f.read())

    if not text.strip():
        return SubtitleResult(NO_SUBTITLES, detail="정제 후 빈 텍스트")
    return SubtitleResult(SUCCESS, text=text, lang=lang,
                          source="yt-dlp/" + kind)


def _fetch_once(video_id, langs, mode, api):
    """한 번의 수집 시도 — transcript-api 실패 시 yt-dlp로 폴백."""
    if mode != MODE_API:
        return fetch_via_ytdlp(video_id, langs)

    result = fetch_via_api(video_id, langs, api=api)
    if result.ok or result.status == PRIVATE_VIDEO:
        return result
    # NO_SUBTITLES 포함 — transcript-api가 못 봐도 yt-dlp는 볼 수 있다
    fallback = fetch_via_ytdlp(video_id, langs)
    if fallback.ok:
        return fallback
    # 두 경로 모두 실패하면 더 구체적인 사유를 남긴다
    return fallback if fallback.status != FETCH_FAILED else result


def fetch_subtitle(video_id, langs, mode=MODE_API, api=None, verbose=True):
    """자막 수집 진입점.

    429 등 차단 응답이면 exponential backoff(1초 → 2초 → 4초)로 재시도한다.
    """
    result = _fetch_once(video_id, langs, mode, api)
    for delay in BACKOFF_SECONDS:
        if result.status not in (RATE_LIMITED, TIMEOUT):
            break
        if verbose:
            print("      {} — {}초 후 재시도".format(result.status, delay))
        time.sleep(delay)
        result = _fetch_once(video_id, langs, mode, api)
    return result

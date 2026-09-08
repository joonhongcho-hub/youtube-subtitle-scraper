"""리서치 프롬프트용 소스 영상 자막 수집 스크립트.

실행 (이 폴더에서):
    source venv/bin/activate
    python research_fetch.py

결과는 output-research/<카테고리>/ 아래 텍스트 파일로 저장됩니다.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import subtitle  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output-research")

# (카테고리, 제목, video_id 또는 재생목록ID, 언어우선순위)
ITEMS = [
    ("1_연구방법론", "논문 검색 꿀팁! 손쉽게 논문 찾는 방법", "HWBBLRjFIKs", ["ko", "en"]),
    ("1_연구방법론", "Writing the Literature Review (Part One)", "2IUZWZX4OGI", ["en", "ko"]),
    ("1_연구방법론", "How To Do A Literature Review (STRESS-FREE!)", "jJntl74QNWo", ["en", "ko"]),
    ("1_연구방법론", "How To Write A Literature Review From Start To Finish", "bhXA0emLH-k", ["en", "ko"]),
    ("1_연구방법론", "How to Write a Literature Review - Scribbr", "zIYC6zG265E", ["en", "ko"]),

    ("2_팩트체크", "Webinar - Fact-Checking for Journalists", "VWbeZpiPP1k", ["en", "ko"]),
    ("2_팩트체크", "OSINT At Home #1 - reverse image search", "qW96515QG6Y", ["en", "ko"]),
    ("2_팩트체크", "Open source intelligence with Bellingcat", "dVWyNUmUvMA", ["en", "ko"]),
    ("2_팩트체크_playlist", "JTBC 뉴스룸 팩트체크", "PL3Eb1N33oAXgQrRBThE4TPSOIR8ZgfSug", ["ko", "en"]),
    ("2_팩트체크_playlist", "AFP Fact Check", "PLo9T0OZu4qjk7MVqK7VxTFoiI5LTM4FYF", ["en", "ko"]),

    ("3_AI딥리서치", "Perplexity Deep Research", "UobQwGTli5w", ["en", "ko"]),
    ("3_AI딥리서치", "ChatGPT Deep Research Tutorial", "DiG5nS3pRmE", ["en", "ko"]),
    ("3_AI딥리서치", "ChatGPT vs Gemini vs Perplexity Deep Research", "oBXoB0Zbm94", ["en", "ko"]),
    ("3_AI딥리서치", "How To Use Perplexity AI", "nzgrB56LMbI", ["en", "ko"]),
    ("3_AI딥리서치", "NEW Deep Research in ChatGPT", "JczuO_ppLiI", ["en", "ko"]),
]


def resolve_playlist_first_video(playlist_id):
    """재생목록에서 첫 번째 영상 id를 yt-dlp로 가져온다."""
    url = "https://www.youtube.com/playlist?list=" + playlist_id
    try:
        out = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--playlist-items", "1", "--print", "id", url],
            capture_output=True, text=True, timeout=60,
        )
        lines = [l.strip() for l in out.stdout.strip().splitlines() if l.strip()]
        return lines[0] if lines else None
    except Exception as exc:
        print("  [실패] 재생목록 첫 영상 조회 실패: {}".format(exc))
        return None


def safe_filename(name):
    for ch in '/\\:*?"<>|':
        name = name.replace(ch, "_")
    return name[:100]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    api = subtitle.make_api()
    results = []

    for category, title, ref, langs in ITEMS:
        video_id = ref
        if category.endswith("_playlist"):
            print("[재생목록] {} 에서 첫 영상 찾는 중...".format(title))
            video_id = resolve_playlist_first_video(ref)
            category = category.replace("_playlist", "")
            if not video_id:
                results.append((category, title, ref, "PLAYLIST_RESOLVE_FAILED"))
                continue

        print("[{}] {} ({}) 자막 수집 중...".format(category, title, video_id))
        result = subtitle.fetch_via_api(video_id, langs, api=api)
        cat_dir = os.path.join(OUT_DIR, category)
        os.makedirs(cat_dir, exist_ok=True)

        if result.ok:
            fname = safe_filename(title) + ".txt"
            path = os.path.join(cat_dir, fname)
            with open(path, "w", encoding="utf-8") as f:
                f.write("제목: {}\n원본: https://www.youtube.com/watch?v={}\n언어: {} / 출처: {}\n\n".format(
                    title, video_id, result.lang, result.source))
                f.write(result.text)
            print("  OK -> {}".format(path))
            results.append((category, title, video_id, "OK"))
        else:
            print("  실패: {}".format(result.status))
            results.append((category, title, video_id, result.status))

    print("\n===== 요약 =====")
    for category, title, video_id, status in results:
        print("[{}] {} - {}".format(category, title, status))


if __name__ == "__main__":
    main()

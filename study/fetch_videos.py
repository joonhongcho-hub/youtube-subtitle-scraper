"""유닛 재료 수집 — 지정한 영상들의 자막만 받아온다.

코워크가 "이 영상들 받아줘" 하고 video_id 목록을 넘긴다.
어떤 영상을 받을지는 코워크가 정한다. 여기서 검색하지 않는다.

    python study/fetch_videos.py --ids Abc123,Def456 --out "챕터1/유닛3"
    python study/fetch_videos.py --ids-file ids.txt --out "챕터1/유닛3"

결과 JSON 을 stdout 으로 뱉는다. 파일로 남기지 않는다.
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(ROOT)
sys.path.insert(0, APP)

import channel      # noqa: E402
import engine       # noqa: E402
import storage      # noqa: E402
import subtitle     # noqa: E402

SETTINGS_PATH = os.path.join(ROOT, "config", "settings.json")


def resolve_root(settings):
    """output_root 가 상대경로면 앱 폴더 기준으로 푼다.

    절대경로를 설정 파일에 박아두면 저장소에 넣을 수 없고, 폴더를 옮기면
    조용히 엉뚱한 곳에 쌓인다. 상대경로를 허용해 설정을 같이 버전 관리한다.
    """
    root = settings.get("output_root") or ""
    return root if os.path.isabs(root) else os.path.join(APP, root)


def log(level, message):
    # 결과 JSON 이 stdout 을 쓰므로 진행 로그는 stderr 로 보낸다
    print(message, file=sys.stderr, flush=True)


def read_ids(args):
    ids = []
    if args.ids:
        ids += [x.strip() for x in args.ids.split(",") if x.strip()]
    if args.ids_file:
        with open(args.ids_file, encoding="utf-8") as f:
            ids += [line.strip() for line in f if line.strip()]
    # 순서를 지키면서 중복만 없앤다
    seen, out = set(), []
    for vid in ids:
        if vid not in seen:
            seen.add(vid)
            out.append(vid)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description="지정한 영상들의 자막 수집")
    parser.add_argument("--ids", default=None, help="video_id 를 쉼표로 구분")
    parser.add_argument("--ids-file", default=None, help="한 줄에 하나씩 담긴 파일")
    parser.add_argument("--out", required=True,
                        help="저장 위치 (output_root 아래 상대 경로)")
    args = parser.parse_args(argv)

    ids = read_ids(args)
    if not ids:
        print("[중단] --ids 또는 --ids-file 이 필요합니다.", file=sys.stderr)
        return 1

    with open(SETTINGS_PATH, encoding="utf-8") as f:
        settings = json.load(f)

    out_dir = os.path.join(resolve_root(settings), args.out)
    os.makedirs(out_dir, exist_ok=True)

    urls = ["https://www.youtube.com/watch?v={}".format(v) for v in ids]
    log("info", "영상 {}개 해석 중...".format(len(urls)))
    videos, unresolved = channel.resolve_videos(urls)
    if unresolved:
        log("fail", "해석 실패 {}건".format(len(unresolved)))

    store = engine.Store(out_dir)
    mode, _ = subtitle.verify_transcript_api(log=log)
    api = subtitle.make_api() if mode == subtitle.MODE_API else None

    engine.process_videos(videos, args.out, store, 
                          [x.strip() for x in settings.get("lang", "ko,en").split(",")],
                          mode, api, log=log,
                          timestamps=settings.get("timestamps", True))

    files, failures, missing = [], [], []
    for record in store.records_for([v["video_id"] for v in videos]):
        if record["status"] != subtitle.SUCCESS:
            failures.append({"video_id": record["video_id"],
                             "reason": record["status"]})
            continue
        abs_path = os.path.join(out_dir, record["path"])
        if not os.path.exists(abs_path):
            missing.append({"video_id": record["video_id"], "expected": abs_path})
            continue
        files.append({
            "video_id": record["video_id"],
            "title": record["title"],
            "upload_date": record["upload_date"],
            "url": record["url"],
            "path": abs_path,
            "chars": record.get("char_count", 0),
        })

    print(json.dumps({
        "out_dir": out_dir,
        "requested": ids,
        "unresolved": unresolved,
        "files": files,
        "failures": failures,
        "missing_files": missing,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

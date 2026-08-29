"""유튜브 채널 자막 일괄 수집 도구 — 오케스트레이션."""

import argparse
import os
import random
import sys
import time

import channel
import report
import storage
import subtitle

DEFAULT_LANGS = ["ko", "en"]
# 요청 사이 랜덤 대기 (IP 차단 방지)
SLEEP_MIN, SLEEP_MAX = 3.0, 8.0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="유튜브 채널의 모든 영상 자막을 수집해 텍스트로 저장합니다.")
    parser.add_argument("channel", help="채널 URL 또는 채널명")
    parser.add_argument("--limit", type=int, default=None,
                        help="앞에서부터 N개 영상만 처리 (테스트용)")
    parser.add_argument("--output", default="./output", help="출력 폴더 (기본: ./output)")
    parser.add_argument("--lang", default="ko,en", help="자막 언어 우선순위 (기본: ko,en)")
    return parser.parse_args(argv)


def polite_sleep(multiplier=1.0):
    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX) * multiplier)


class Store(object):
    """[안전장치 C] 영상 1개를 처리할 때마다 즉시 디스크에 반영한다.

    중간에 끊겨도 다음 실행에서 이어서 할 수 있도록,
    처리 기록과 실패 목록, index.xlsx를 매번 새로 써낸다.
    """

    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.processed_path = os.path.join(out_dir, "processed_ids.json")
        self.failed_path = os.path.join(out_dir, "failed_videos.json")
        self.index_path = os.path.join(out_dir, "index.xlsx")
        self.records = storage.load_json(self.processed_path, {})

    def get(self, video_id):
        return self.records.get(video_id)

    def is_done(self, video_id):
        """성공했거나 재시도해도 소용없는 실패면 건너뛴다."""
        record = self.records.get(video_id)
        if not record:
            return False
        return (record["status"] == subtitle.SUCCESS
                or record["status"] in subtitle.NON_RETRYABLE)

    def all_records(self):
        return list(self.records.values())

    def update(self, record):
        self.records[record["video_id"]] = record
        storage.save_json(self.processed_path, self.records)

        failed = {vid: {
            "reason": r["status"],
            "title": r["title"],
            "url": r["url"],
            "attempts": r.get("attempts", 1),
            "retryable": r["status"] in subtitle.RETRYABLE,
            "detail": r.get("detail", ""),
        } for vid, r in self.records.items() if r["status"] != subtitle.SUCCESS}
        storage.save_json(self.failed_path, failed)
        storage.write_index(self.all_records(), self.index_path)


def process_videos(videos, channel_name, store, langs, mode, api,
                   sleep_multiplier=1.0, skip_done=True):
    """영상 목록을 순회하며 자막을 수집·저장한다."""
    total = len(videos)
    for i, video in enumerate(videos, 1):
        video_id = video["video_id"]

        if skip_done and store.is_done(video_id):
            print("[{}/{}] Skipping: {}".format(i, total, video_id))
            continue

        print("[{}/{}] {} — {}".format(i, total, video_id, video["title"][:60]))
        result = subtitle.fetch_subtitle(video_id, langs, mode=mode, api=api)

        previous = store.get(video_id) or {}
        record = {
            "video_id": video_id,
            "title": video["title"],
            "upload_date": video["upload_date"],
            "url": video["url"],
            "status": result.status,
            "path": previous.get("path", ""),
            "attempts": previous.get("attempts", 0) + 1,
            "detail": str(result.detail or ""),
        }

        if result.ok:
            path = storage.save_transcript(
                store.out_dir, channel_name, video["upload_date"],
                video["title"], result.text)
            record["path"] = os.path.relpath(path, store.out_dir)
            print("      저장: {}  ({})".format(record["path"], result.source))
        else:
            print("      실패: {} ({})".format(result.status, record["detail"][:80]))

        store.update(record)  # 영상 1개마다 즉시 반영
        if i < total:
            polite_sleep(sleep_multiplier)


def run_retries(store, channel_name, langs, mode, api):
    """실패 리포트를 보여주고, 재시도 가능한 영상만 다시 돌린다."""
    for round_index in range(report.MAX_RETRY_ROUNDS + 1):
        records = store.all_records()
        report.print_report(records)

        pending = report.retryable_records(records)
        if not report.ask_retry(pending, round_index):
            return

        multiplier = report.RETRY_SLEEP_MULTIPLIER
        print("\n재시도 {}회차 — 대기 시간을 {}배로 늘려 {}개를 다시 시도합니다.\n"
              .format(round_index + 1, multiplier, len(pending)))
        process_videos(pending, channel_name, store, langs, mode, api,
                       sleep_multiplier=multiplier, skip_done=False)


def main(argv=None):
    args = parse_args(argv)
    langs = [x.strip() for x in args.lang.split(",") if x.strip()] or DEFAULT_LANGS
    out_dir = os.path.abspath(args.output)
    os.makedirs(out_dir, exist_ok=True)

    # 1단계 — 채널 식별
    resolved = channel.resolve_channel(args.channel)

    # 2단계 — 영상 목록 수집
    listing = channel.fetch_video_list(resolved["url"], out_dir)
    channel_name = resolved["name"] or listing["channel_name"]
    videos = listing["videos"]
    print("\n채널: {} — 총 영상 {}개".format(channel_name, len(videos)))

    if args.limit is not None:
        videos = videos[: args.limit]
        print("--limit {} 적용 → {}개만 처리합니다.".format(args.limit, len(videos)))
    else:
        answer = input(
            "전체 {}개를 처리합니다. 계속할까요? (y/n): ".format(len(videos))).strip().lower()
        if answer not in ("y", "yes"):
            print("취소했습니다.")
            return 0

    store = Store(out_dir)
    done = sum(1 for v in videos if store.is_done(v["video_id"]))
    if done:
        print("이미 처리한 {}개는 건너뜁니다.".format(done))
    print()

    # 안전장치 B — 라이브러리 사전 검증
    mode, _ = subtitle.verify_transcript_api()
    api = subtitle.make_api() if mode == subtitle.MODE_API else None
    print()

    # 3~5단계 — 자막 수집 / 정제 / 저장
    process_videos(videos, channel_name, store, langs, mode, api)

    # 리포트 + 재시도
    run_retries(store, channel_name, langs, mode, api)
    print("\n인덱스: {}".format(store.index_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())

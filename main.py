"""유튜브 채널 자막 일괄 수집 도구 — CLI.

수집 로직은 engine.py에 있다. 이 파일은 인자를 받고 사람에게 묻고
터미널에 찍는 일만 한다.
"""

import argparse
import os
import sys

import channel
import engine
import report
import storage
import subtitle

# 무한 반복 방지 — 재시도 라운드 상한
MAX_RETRY_ROUNDS = 3
# 재시도 시 대기 시간 배수
RETRY_SLEEP_MULTIPLIER = 2.5


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="유튜브 채널의 모든 영상 자막을 수집해 텍스트로 저장합니다.")
    parser.add_argument("channel", help="채널 URL 또는 채널명")
    parser.add_argument("--limit", type=int, default=None,
                        help="앞에서부터 N개 영상만 처리 (테스트용)")
    parser.add_argument("--output", default="./output", help="출력 폴더 (기본: ./output)")
    parser.add_argument("--lang", default="ko,en", help="자막 언어 우선순위 (기본: ko,en)")
    parser.add_argument("--timestamps", action="store_true",
                        help="자막에 [00:12] 형태의 시각을 남긴다")
    return parser.parse_args(argv)


def cli_log(level, message):
    print(message)


def print_report(records):
    """실행 종료 후 리포트를 출력한다."""
    counts = report.counts_by_reason(records)
    success = report.summarize(records).get(subtitle.SUCCESS, 0)
    failed = sum(counts.values())

    print("\n" + "=" * 46)
    print("성공: {}개".format(success))
    print("실패: {}개".format(failed))
    for reason in (subtitle.NO_SUBTITLES, subtitle.PRIVATE_VIDEO,
                   subtitle.FETCH_FAILED, subtitle.RATE_LIMITED, subtitle.TIMEOUT):
        n = counts.get(reason, 0)
        if not n:
            continue
        suffix = " (재시도 불가)" if reason in subtitle.NON_RETRYABLE else ""
        print("  - {}: {}개{}".format(reason, n, suffix))
    print("=" * 46)


def ask_retry(pending, round_index):
    """재시도 여부를 묻는다. 상한에 도달하면 묻지 않는다."""
    if not pending:
        return False
    if round_index >= MAX_RETRY_ROUNDS:
        print("\n재시도 가능한 {}개가 남았지만 재시도 상한({}회)에 도달했습니다."
              .format(len(pending), MAX_RETRY_ROUNDS))
        return False
    answer = input("\n재시도 가능한 {}개가 있습니다. 다시 시도할까요? (y/n): "
                   .format(len(pending))).strip().lower()
    return answer in ("y", "yes")


def run_retries(store, channel_name, langs, mode, api, timestamps):
    """실패 리포트를 보여주고, 재시도 가능한 영상만 다시 돌린다."""
    for round_index in range(MAX_RETRY_ROUNDS + 1):
        records = store.all_records()
        print_report(records)

        pending = report.retryable_records(records)
        if not ask_retry(pending, round_index):
            return

        print("\n재시도 {}회차 — 대기 시간을 {}배로 늘려 {}개를 다시 시도합니다.\n"
              .format(round_index + 1, RETRY_SLEEP_MULTIPLIER, len(pending)))
        engine.process_videos(
            pending, channel_name, store, langs, mode, api,
            sleep_multiplier=RETRY_SLEEP_MULTIPLIER, skip_done=False,
            log=cli_log, timestamps=timestamps)


def main(argv=None):
    args = parse_args(argv)
    langs = [x.strip() for x in args.lang.split(",") if x.strip()] or engine.DEFAULT_LANGS

    # 1단계 — 채널 식별
    resolved = channel.resolve_channel(args.channel)

    # 채널 이름을 먼저 알아야 결과 폴더를 정할 수 있다 (약 1초)
    channel_name = resolved["name"]
    if not channel_name:
        channel_name = channel.fetch_channel_meta(resolved["url"])["name"] or "channel"

    # 채널 하나가 자기 완결적으로 담기도록 채널별 폴더를 쓴다
    out_dir = os.path.join(os.path.abspath(args.output),
                           storage.sanitize_filename(channel_name))
    os.makedirs(out_dir, exist_ok=True)

    # 2단계 — 영상 목록 수집
    listing = channel.fetch_video_list(resolved["url"], out_dir)
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

    store = engine.Store(out_dir)
    done = sum(1 for v in videos if store.is_done(v["video_id"]))
    if done:
        print("이미 처리한 {}개는 건너뜁니다.".format(done))
    print()

    # 안전장치 B — 라이브러리 사전 검증
    mode, _ = subtitle.verify_transcript_api(log=cli_log)
    api = subtitle.make_api() if mode == subtitle.MODE_API else None
    print()

    # 3~5단계 — 자막 수집 / 정제 / 저장
    engine.process_videos(videos, channel_name, store, langs, mode, api,
                          log=cli_log, timestamps=args.timestamps)

    # 리포트 + 재시도
    run_retries(store, channel_name, langs, mode, api, args.timestamps)
    print("\n인덱스: {}".format(store.index_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())

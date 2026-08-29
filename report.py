"""결과 리포트 + 재시도 처리."""

import subtitle

# 무한 반복 방지 — 재시도 라운드 상한
MAX_RETRY_ROUNDS = 3
# 재시도 시 대기 시간 배수
RETRY_SLEEP_MULTIPLIER = 2.5

REASON_LABELS = {
    subtitle.NO_SUBTITLES: "자막이 아예 없음",
    subtitle.PRIVATE_VIDEO: "비공개/삭제/멤버십 전용",
    subtitle.FETCH_FAILED: "요청 실패",
    subtitle.RATE_LIMITED: "차단",
    subtitle.TIMEOUT: "시간 초과",
}


def summarize(records):
    """상태별 개수를 센다."""
    counts = {}
    for record in records:
        status = record["status"]
        counts[status] = counts.get(status, 0) + 1
    return counts


def retryable_records(records):
    """재시도 가능한 실패만 골라낸다 (시도 횟수 상한 미만인 것)."""
    return [r for r in records
            if r["status"] in subtitle.RETRYABLE
            and r.get("attempts", 1) < MAX_RETRY_ROUNDS + 1]


def print_report(records):
    """실행 종료 후 리포트를 출력한다."""
    counts = summarize(records)
    success = counts.pop(subtitle.SUCCESS, 0)
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

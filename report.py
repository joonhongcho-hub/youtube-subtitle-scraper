"""결과 리포트 + 재시도 처리."""

import subtitle

# 무한 반복 방지 — 재시도 라운드 상한
MAX_RETRY_ROUNDS = 3
# 재시도 시 대기 시간 배수
RETRY_SLEEP_MULTIPLIER = 2.5

# 사용자에게 보여줄 실패 원인 설명
REASON_LABELS = {
    subtitle.NO_SUBTITLES: "이 영상에는 자막이 없습니다 (자동 생성 자막도 없음)",
    subtitle.PRIVATE_VIDEO: "멤버십 전용·비공개·삭제된 영상이라 자막을 가져올 수 없습니다",
    subtitle.FETCH_FAILED: "일시적인 요청 실패입니다",
    subtitle.RATE_LIMITED: "일시적인 요청 실패입니다 (유튜브가 요청을 제한했습니다)",
    subtitle.TIMEOUT: "일시적인 요청 실패입니다 (시간 초과)",
}

# 터미널 리포트용 짧은 표기
SHORT_LABELS = {
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


def counts_by_reason(records):
    """실패 사유별 개수 — 성공은 제외한다."""
    counts = summarize(records)
    counts.pop(subtitle.SUCCESS, None)
    return counts


def describe(reason):
    """실패 사유를 사용자가 이해할 수 있는 문구로 바꾼다."""
    return REASON_LABELS.get(reason, reason)

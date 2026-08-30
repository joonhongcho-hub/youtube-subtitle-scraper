"""수집 엔진 — CLI와 웹 서버가 함께 쓰는 자막 수집 루프.

터미널에 직접 출력하는 대신 log 콜백으로 알리고, cancel 이벤트로 중단할 수 있어
CLI와 백그라운드 작업 양쪽에서 그대로 쓸 수 있다.
"""

import os
import random
import time

import storage
import subtitle

DEFAULT_LANGS = ["ko", "en"]
# 요청 사이 랜덤 대기 (IP 차단 방지)
SLEEP_MIN, SLEEP_MAX = 3.0, 8.0
# 영상 1개당 평균 처리 시간 — 실측값(725개 수집에서 401개를 2,604초에 처리)
SECONDS_PER_VIDEO = 6.5

# 로그 수준 — 프런트엔드의 색 구분에 그대로 대응한다
LEVEL_INFO = "info"
LEVEL_OK = "ok"
LEVEL_FAIL = "fail"
LEVEL_SKIP = "skip"


def silent_log(level, message):
    """로그를 버리는 기본 콜백."""


def print_log(level, message):
    """CLI용 — 기존 터미널 출력을 그대로 재현한다."""
    print(message)


def estimate_seconds(count):
    """영상 개수로 예상 소요 시간(초)을 낸다."""
    return int(count * SECONDS_PER_VIDEO)


def polite_sleep(multiplier=1.0, cancel=None):
    """요청 사이 랜덤 대기. 대기 중에도 중지 요청에 바로 반응한다."""
    remaining = random.uniform(SLEEP_MIN, SLEEP_MAX) * multiplier
    while remaining > 0:
        if cancel is not None and cancel.is_set():
            return
        step = min(0.5, remaining)
        time.sleep(step)
        remaining -= step


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

    def records_for(self, video_ids):
        """주어진 영상 집합에 해당하는 기록만 돌려준다 (작업 단위 조회용)."""
        wanted = set(video_ids)
        return [r for vid, r in self.records.items() if vid in wanted]

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
                   sleep_multiplier=1.0, skip_done=True,
                   log=None, cancel=None, on_progress=None,
                   timestamps=False):
    """영상 목록을 순회하며 자막을 수집·저장한다.

    log        log(level, message) — 진행 상황 알림
    cancel     threading.Event — 영상 사이마다 확인해 중지한다
    on_progress on_progress(done, total, record) — 영상 1개를 끝낼 때마다 호출
    """
    log = log or silent_log
    total = len(videos)

    for i, video in enumerate(videos, 1):
        if cancel is not None and cancel.is_set():
            log(LEVEL_INFO, "중지 요청 — {}개까지 처리하고 멈춥니다.".format(i - 1))
            break

        video_id = video["video_id"]

        if skip_done and store.is_done(video_id):
            log(LEVEL_SKIP, "[{}/{}] Skipping: {}".format(i, total, video_id))
            if on_progress:
                on_progress(i, total, store.get(video_id))
            continue

        log(LEVEL_INFO, "[{}/{}] {} — {}".format(
            i, total, video_id, video["title"][:60]))
        result = subtitle.fetch_subtitle(
            video_id, langs, mode=mode, api=api, log=log, timestamps=timestamps)

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
            "char_count": previous.get("char_count", 0),
        }

        if result.ok:
            path = storage.save_transcript(
                store.out_dir, video["upload_date"], video["title"], result.text)
            record["path"] = os.path.relpath(path, store.out_dir)
            record["char_count"] = len(result.text)
            log(LEVEL_OK, "      저장: {}  ({})".format(
                record["path"], result.source))
        else:
            log(LEVEL_FAIL, "      실패: {} ({})".format(
                result.status, record["detail"][:80]))

        store.update(record)  # 영상 1개마다 즉시 반영
        if on_progress:
            on_progress(i, total, record)
        if i < total:
            polite_sleep(sleep_multiplier, cancel=cancel)

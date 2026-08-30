"""작업 레지스트리 — 수집 작업의 상태·로그·결과를 job_id로 관리한다.

작업은 디스크에 저장된다. 서버를 재시작하거나 브라우저를 새로고침해도
job_id로 결과를 다시 찾을 수 있고, 중단된 작업은 이어서 실행할 수 있다.
"""

import json
import os
import threading
import time
import uuid

import engine
import report
import storage
import subtitle

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(BASE_DIR, "output", "_jobs")

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_STOPPED = "stopped"
STATUS_ERROR = "error"
# 프로세스가 내려가 스레드가 사라진 작업
STATUS_INTERRUPTED = "interrupted"

# 로그가 무한정 쌓이지 않도록 상한을 둔다 (영상 725개면 2천 줄 남짓)
MAX_LOG_LINES = 20000


class Job(object):
    """수집 작업 하나. 백그라운드 스레드가 채우고 WebSocket이 읽는다."""

    def __init__(self, channel_name, channel_url, out_dir, targets, options,
                 job_id=None):
        self.id = job_id or uuid.uuid4().hex[:12]
        self.channel_name = channel_name
        self.channel_url = channel_url
        self.out_dir = out_dir
        # 대상 영상의 전체 레코드. id만 들고 있으면 재개할 때 제목·업로드일이 없어
        # 파일을 저장할 수 없으므로 통째로 보관한다.
        self.targets = list(targets)
        self.options = options

        self.status = STATUS_RUNNING
        self.error = None
        self.done = 0
        self.total = len(self.targets)
        self.started_at = time.time()
        self.finished_at = None
        self.last_progress_at = time.time()

        self.logs = []
        self.logs_loaded = False
        self.cancel = threading.Event()
        self.thread = None
        self._lock = threading.Lock()

    @property
    def video_ids(self):
        return [v["video_id"] for v in self.targets]

    # --- 디스크 ---

    @property
    def meta_path(self):
        return os.path.join(JOBS_DIR, "{}.json".format(self.id))

    @property
    def log_path(self):
        return os.path.join(JOBS_DIR, "{}.log".format(self.id))

    def save(self):
        """메타를 저장한다. 자막과 처리 기록은 채널 폴더에 따로 있다."""
        storage.save_json(self.meta_path, {
            "job_id": self.id,
            "channel_name": self.channel_name,
            "channel_url": self.channel_url,
            "out_dir": self.out_dir,
            "targets": self.targets,
            "options": self.options,
            "status": self.status,
            "error": self.error,
            "done": self.done,
            "total": self.total,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        })

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        job = cls(data["channel_name"], data["channel_url"], data["out_dir"],
                  data.get("targets") or [], data.get("options") or {},
                  job_id=data["job_id"])
        job.status = data.get("status", STATUS_INTERRUPTED)
        job.error = data.get("error")
        job.done = data.get("done", 0)
        job.total = data.get("total", len(job.targets))
        job.started_at = data.get("started_at", time.time())
        job.finished_at = data.get("finished_at")
        # 실행 중으로 저장됐는데 이 프로세스에는 스레드가 없다 → 중단된 것이다
        if job.status == STATUS_RUNNING:
            job.status = STATUS_INTERRUPTED
        return job

    # --- 로그 ---

    def load_logs(self):
        """로그는 작업을 실제로 열 때만 읽는다."""
        if self.logs_loaded:
            return
        self.logs_loaded = True
        if not os.path.exists(self.log_path):
            return
        with open(self.log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.logs.append(json.loads(line))
                except ValueError:
                    continue

    def log(self, level, message):
        entry = {"level": level, "message": message}
        with self._lock:
            if len(self.logs) >= MAX_LOG_LINES:
                return
            self.logs.append(entry)
        os.makedirs(JOBS_DIR, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def logs_since(self, index):
        """index 이후의 로그만 돌려준다 — 재접속 시 이어받기용."""
        self.load_logs()
        with self._lock:
            return self.logs[index:], len(self.logs)

    # --- 진행률 ---

    def set_progress(self, done, total, record=None):
        self.done = done
        self.total = total
        self.last_progress_at = time.time()
        self.save()

    def elapsed(self):
        end = self.finished_at or time.time()
        return end - self.started_at

    def eta_seconds(self):
        """남은 시간 추정. 진행이 있을 때만 실제 속도를 쓴다."""
        if self.status != STATUS_RUNNING:
            return None
        rate = (self.elapsed() / self.done) if self.done else engine.SECONDS_PER_VIDEO
        return int(rate * max(0, self.total - self.done))

    def stalled_minutes(self):
        """마지막 진행 이후 흐른 시간(분). 상태를 바꾸지 않고 화면 힌트로만 쓴다."""
        if self.status != STATUS_RUNNING:
            return 0
        return int((time.time() - self.last_progress_at) / 60)

    def is_alive(self):
        return self.thread is not None and self.thread.is_alive()

    def finish(self, status, error=None):
        self.status = status
        self.error = error
        self.finished_at = time.time()
        self.save()

    # --- 결과 ---

    def store(self):
        return engine.Store(self.out_dir)

    def records(self, scope="job"):
        """scope=job 이면 이 작업이 대상으로 한 영상만, channel 이면 누적 전체."""
        store = self.store()
        if scope == "channel":
            return store.all_records()
        return store.records_for(self.video_ids)

    def summary(self, scope="job"):
        records = self.records(scope)
        counts = report.counts_by_reason(records)
        return {
            "success": report.summarize(records).get(subtitle.SUCCESS, 0),
            "failed": sum(counts.values()),
            "reasons": [
                {
                    "reason": reason,
                    "count": count,
                    "message": report.describe(reason),
                    "retryable": reason in subtitle.RETRYABLE,
                }
                for reason, count in sorted(counts.items())
            ],
            "retryable": len(report.retryable_records(records)),
            "total_chars": sum(r.get("char_count", 0) for r in records),
        }

    def state(self):
        return {
            "job_id": self.id,
            "status": self.status,
            "error": self.error,
            "channel_name": self.channel_name,
            "channel_url": self.channel_url,
            "done": self.done,
            "total": self.total,
            "elapsed": int(self.elapsed()),
            "eta": self.eta_seconds(),
            "stalled_minutes": self.stalled_minutes(),
            "log_count": len(self.logs),
            "resumable": self.status in (
                STATUS_INTERRUPTED, STATUS_STOPPED, STATUS_ERROR),
        }


class Registry(object):
    """작업 보관소. 동시 실행은 1건으로 제한한다.

    유튜브 rate limit 때문에 병렬로 돌리면 오히려 느려지고 429를 부른다.
    """

    def __init__(self):
        self.jobs = {}
        self._lock = threading.Lock()

    def restore(self):
        """서버가 뜰 때 디스크에 저장된 작업을 되살린다."""
        if not os.path.isdir(JOBS_DIR):
            return 0
        restored = 0
        for name in sorted(os.listdir(JOBS_DIR)):
            if not name.endswith(".json"):
                continue
            try:
                job = Job.load(os.path.join(JOBS_DIR, name))
            except (ValueError, KeyError, OSError):
                continue
            self.jobs[job.id] = job
            restored += 1
        return restored

    def get(self, job_id):
        return self.jobs.get(job_id)

    def active(self):
        """실제로 돌고 있는 작업. 스레드가 죽었으면 중단으로 내린다.

        시간으로 판정하면 안 된다 — 영상 1개가 최악의 경우 12분 넘게 걸려서
        (yt-dlp 180초 × backoff 재시도) 정상 작업을 중단으로 오인한다.
        """
        for job in self.jobs.values():
            if job.status != STATUS_RUNNING:
                continue
            if job.is_alive():
                return job
            job.finish(STATUS_INTERRUPTED)
        return None

    def latest(self):
        """가장 최근에 시작한 작업."""
        if not self.jobs:
            return None
        return max(self.jobs.values(), key=lambda j: j.started_at)

    def add(self, job):
        with self._lock:
            self.jobs[job.id] = job
        job.save()
        return job


registry = Registry()

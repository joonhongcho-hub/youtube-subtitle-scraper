"""FastAPI 서버 — CLI 수집 엔진을 웹에서 쓸 수 있게 감싼다."""

import asyncio
import concurrent.futures
import io
import os
import shlex
import subprocess
import threading
import time
import urllib.parse
import zipfile

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import channel
import engine
import jobs
import report
import search
import storage
import subtitle

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_ROOT = os.path.join(BASE_DIR, "output")
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = FastAPI(title="유튜브 자막 수집기")


@app.on_event("startup")
def restore_jobs():
    """서버가 다시 떠도 이전 작업을 job_id로 찾을 수 있게 되살린다."""
    count = jobs.registry.restore()
    if count:
        print("이전 작업 {}건을 복원했습니다.".format(count))


# --- 요청 모델 ---

class SearchRequest(BaseModel):
    query: str


class SourcesRequest(BaseModel):
    channel_url: str
    force: bool = False


class Filters(BaseModel):
    date_from: str = ""      # YYYY-MM-DD
    date_to: str = ""
    min_minutes: float = 0
    max_minutes: float = 0   # 0이면 제한 없음


class Options(BaseModel):
    langs: str = "ko,en"
    timestamps: bool = False


class JobRequest(BaseModel):
    channel_url: str
    channel_name: str = ""
    sources: list = []          # ["videos", "shorts", "streams"]
    playlist_ids: list = []
    filters: Filters = Filters()
    options: Options = Options()
    # channel(기본 순서) / views_desc / views_asc / date_desc / date_asc
    sort: str = "channel"
    limit: int = 0              # 0이면 전체


# --- 공통 ---

def attachment_headers(filename):
    """한글 파일명을 Content-Disposition에 안전하게 싣는다.

    HTTP 헤더는 latin-1만 담을 수 있어 한글을 그대로 넣으면 500이 난다.
    RFC 5987 형식(filename*)으로 UTF-8을 퍼센트 인코딩하고,
    구형 클라이언트를 위해 ASCII 대체 이름도 함께 준다.
    """
    quoted = urllib.parse.quote(filename)
    ascii_name = filename.encode("ascii", "ignore").decode("ascii") or "download"
    return {"Content-Disposition":
            'attachment; filename="{}"; filename*=UTF-8\'\'{}'.format(
                ascii_name, quoted)}


SETTINGS_PATH = os.path.join(BASE_DIR, "output", "_settings.json")


def load_settings():
    """저장 위치 설정. 써 본 폴더를 모두 기억해 검색이 전부를 훑게 한다."""
    data = storage.load_json(SETTINGS_PATH, {})
    root = data.get("output_root") or OUTPUT_ROOT
    roots = data.get("known_roots") or []
    if root not in roots:
        roots = [root] + roots
    return {"output_root": root, "known_roots": roots}


def save_settings(root):
    current = load_settings()
    roots = [r for r in current["known_roots"] if r != root]
    storage.save_json(SETTINGS_PATH, {
        "output_root": root,
        "known_roots": [root] + roots,
    })
    return load_settings()


def output_root():
    return load_settings()["output_root"]


def channel_dir(channel_name):
    """채널별 결과 폴더. 재실행하면 이미 받은 자막을 건너뛰어 이어받는다."""
    return os.path.join(output_root(), storage.sanitize_filename(channel_name))


def apply_filters(videos, filters, source):
    """날짜·길이 필터를 적용한다.

    쇼츠는 유튜브가 목록에 업로드일·길이를 주지 않으므로 필터를 적용할 수 없다.
    거르지 않고 그대로 통과시킨다 (UI에서도 필터를 비활성화한다).
    """
    if source in channel.SOURCES_WITHOUT_METADATA:
        return videos

    out = []
    for video in videos:
        date = video.get("upload_date") or ""
        if filters.date_from and date and date < filters.date_from.replace("-", ""):
            continue
        if filters.date_to and date and date > filters.date_to.replace("-", ""):
            continue

        duration = video.get("duration")
        if duration:
            minutes = duration / 60.0
            if filters.min_minutes and minutes < filters.min_minutes:
                continue
            if filters.max_minutes and minutes > filters.max_minutes:
                continue
        out.append(video)
    return out


SORT_CHANNEL = "channel"
SORT_VIEWS_DESC = "views_desc"
SORT_VIEWS_ASC = "views_asc"
SORT_DATE_DESC = "date_desc"
SORT_DATE_ASC = "date_asc"


def sort_targets(videos, sort):
    """대상 목록을 정렬한다.

    조회수나 업로드일이 없는 항목은 어떤 정렬에서도 맨 뒤로 보낸다.
    유튜브는 일반 영상의 약 20%에 조회수를 주지 않고, 쇼츠에는 업로드일을
    아예 주지 않는다. 값이 없다고 0으로 취급하면 조용히 섞여 버린다.
    """
    if sort == SORT_CHANNEL or not sort:
        return sorted(videos, key=lambda v: v.get("order", 0))

    def views(v):
        return v.get("view_count")

    def date(v):
        raw = v.get("upload_date") or ""
        return raw if raw and raw != "00000000" else None

    if sort in (SORT_VIEWS_DESC, SORT_VIEWS_ASC):
        pick, reverse = views, sort == SORT_VIEWS_DESC
    else:
        pick, reverse = date, sort == SORT_DATE_DESC

    known = [v for v in videos if pick(v) is not None]
    unknown = [v for v in videos if pick(v) is None]
    known.sort(key=lambda v: (pick(v), -v.get("order", 0)), reverse=reverse)
    unknown.sort(key=lambda v: v.get("order", 0))
    return known + unknown


def missing_sort_value(videos, sort):
    """정렬 기준 값이 없어 뒤로 밀리는 항목 수 — 화면에 알려주기 위한 값."""
    if sort in (SORT_VIEWS_DESC, SORT_VIEWS_ASC):
        return sum(1 for v in videos if v.get("view_count") is None)
    if sort in (SORT_DATE_DESC, SORT_DATE_ASC):
        return sum(1 for v in videos
                   if not v.get("upload_date") or v["upload_date"] == "00000000")
    return 0


def collect_targets(req, out_dir, log=None):
    """선택한 소스·재생목록을 합쳐 대상 영상 목록을 만든다.

    재생목록은 대개 일반 영상·쇼츠와 겹치므로 video_id로 중복을 제거한다.
    """
    merged = {}
    for source in req.sources:
        if source not in channel.SOURCE_TABS:
            continue
        listing = channel.fetch_source(req.channel_url, source, out_dir, log=log)
        for video in apply_filters(listing["videos"], req.filters, source):
            merged.setdefault(video["video_id"], video)

    for playlist_id in req.playlist_ids:
        listing = channel.fetch_playlist(playlist_id, out_dir, log=log)
        for video in apply_filters(listing["videos"], req.filters, "playlist"):
            merged.setdefault(video["video_id"], video)

    # 필터 → 중복 제거(위에서 끝남) → 정렬 → 상위 N개
    videos = sort_targets(list(merged.values()), req.sort)
    if req.limit and req.limit > 0:
        videos = videos[: req.limit]
    return videos


# --- 채널 API ---

@app.post("/api/search")
def api_search(req: SearchRequest):
    query = req.query.strip()
    if not query:
        raise HTTPException(400, "검색어를 입력하세요.")

    # URL이면 검색하지 않고 그 채널 하나로 확정한다
    if channel._is_url(query):
        url = channel._normalize_channel_url(query)
        try:
            meta = channel.fetch_channel_meta(url)
        except RuntimeError as exc:
            raise HTTPException(400, str(exc))
        return {"is_url": True, "candidates": [{
            "name": meta["name"] or query,
            "url": url,
            "thumbnail": meta["thumbnail"],
            "subscribers": meta["subscribers"],
            "sample_title": "",
        }]}

    try:
        candidates = channel.search_channels(query)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    if not candidates:
        raise HTTPException(404, "'{}' 로 검색된 채널이 없습니다.".format(query))
    return {"is_url": False, "candidates": candidates[:8]}


@app.get("/api/channel/meta")
def api_channel_meta(url: str):
    """썸네일·구독자 수 (약 1초)."""
    try:
        return channel.fetch_channel_meta(url)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


ALL_SOURCES = ("videos", "shorts", "streams")


def count_sources(base, force=False):
    """탭별 영상 개수를 센다.

    유튜브가 총개수를 주지 않아 목록을 통째로 받아야만 안다(약 21초).
    탭 3개를 병렬로 받아 가장 느린 쇼츠 기준 약 12초로 줄인다.
    결과는 수집 때 쓰는 채널 폴더에 캐시되므로, 여기서 한 번 세어두면
    수집 범위 화면이 그 캐시를 그대로 재사용한다.
    """
    meta = channel.fetch_channel_meta(base)
    name = meta["name"] or "channel"
    out_dir = channel_dir(name)

    def one(source):
        try:
            listing = channel.fetch_source(base, source, out_dir, force=force)
            return source, len(listing["videos"]), listing.get("fetched_at", "")
        except RuntimeError:
            return source, 0, ""

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = dict((src, (n, at)) for src, n, at
                       in pool.map(one, ALL_SOURCES))

    sources = []
    for source in ALL_SOURCES:
        count, fetched_at = results[source]
        sources.append({
            "source": source,
            "count": count,
            "seconds": engine.estimate_seconds(count),
            "fetched_at": fetched_at,
            "filterable": source not in channel.SOURCES_WITHOUT_METADATA,
        })

    total = sum(s["count"] for s in sources)
    return {
        "channel_name": name,
        "channel_url": base,
        "thumbnail": meta["thumbnail"],
        "subscribers": meta["subscribers"],
        "sources": sources,
        "total": total,
        "total_seconds": engine.estimate_seconds(total),
    }


@app.get("/api/channel/counts")
def api_channel_counts(url: str, force: bool = False):
    """검색 카드용 — 일반 영상·쇼츠·라이브를 합친 개수."""
    try:
        return count_sources(channel.channel_base(url), force=force)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/channel/sources")
def api_channel_sources(req: SourcesRequest):
    """탭별 개수와 예상 소요 시간. 카드에서 이미 셌다면 캐시로 즉시 응답한다."""
    try:
        return count_sources(channel.channel_base(req.channel_url), force=req.force)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/channel/playlists")
def api_channel_playlists(url: str):
    base = channel.channel_base(url)
    try:
        return {"playlists": channel.fetch_playlists(base)}
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


# --- 작업 API ---

def run_collection(job, videos, sleep_multiplier=1.0, skip_done=True):
    """백그라운드 스레드에서 도는 수집 루프.

    처음 실행·재개·재시도가 모두 이 함수를 지난다. 옵션은 job에 저장돼 있으므로
    서버를 재시작한 뒤에도 같은 조건으로 이어갈 수 있다.
    """
    try:
        options = job.options or {}
        langs = [x.strip() for x in options.get("langs", "ko,en").split(",")
                 if x.strip()]
        store = engine.Store(job.out_dir)

        mode, _ = subtitle.verify_transcript_api(log=job.log)
        api = subtitle.make_api() if mode == subtitle.MODE_API else None

        engine.process_videos(
            videos, job.channel_name, store, langs or engine.DEFAULT_LANGS,
            mode, api, sleep_multiplier=sleep_multiplier, skip_done=skip_done,
            log=job.log, cancel=job.cancel, on_progress=job.set_progress,
            timestamps=options.get("timestamps", False))

        job.finish(jobs.STATUS_STOPPED if job.cancel.is_set() else jobs.STATUS_DONE)
        try:      # 검색이 방금 받은 자막을 바로 찾을 수 있게
            search.build_index(load_settings()["known_roots"],
                               log=lambda m: job.log("info", m))
        except Exception as exc:
            job.log("fail", "색인 갱신 실패: {}".format(exc))
    except Exception as exc:  # 스레드에서 죽으면 흔적이 남지 않으므로 붙잡는다
        message = "{}: {}".format(type(exc).__name__, exc)
        job.log("fail", "작업 실패: {}".format(message))
        job.finish(jobs.STATUS_ERROR, error=message)


def start_thread(job, videos, sleep_multiplier=1.0, skip_done=True):
    """수집 스레드를 띄우고 job에 매달아 둔다.

    스레드 참조가 있어야 '실행 중으로 표시돼 있지만 실제로는 죽은' 작업을
    구분할 수 있다 (registry.active 참고).
    """
    thread = threading.Thread(
        target=run_collection, args=(job, videos, sleep_multiplier, skip_done),
        daemon=True)
    job.thread = thread
    thread.start()
    return thread


@app.post("/api/jobs")
def api_create_job(req: JobRequest):
    active = jobs.registry.active()
    if active:
        raise HTTPException(409, {
            "message": "이미 실행 중인 작업이 있습니다.",
            "job_id": active.id,
        })

    base = channel.channel_base(req.channel_url)
    req.channel_url = base
    name = req.channel_name or channel.fetch_channel_meta(base)["name"] or "channel"
    out_dir = channel_dir(name)
    os.makedirs(out_dir, exist_ok=True)

    try:
        targets = collect_targets(req, out_dir)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    if not targets:
        raise HTTPException(400, "조건에 맞는 영상이 없습니다.")

    job = jobs.Job(name, base, out_dir, targets, req.options.dict())
    jobs.registry.add(job)
    start_thread(job, job.targets)
    return {"job_id": job.id, "total": job.total}


@app.get("/api/jobs/latest")
def api_job_latest():
    """브라우저가 새로고침 뒤 되살릴 작업을 찾을 때 쓴다."""
    active = jobs.registry.active()
    job = active or jobs.registry.latest()
    if not job:
        return {"job": None}
    return {"job": job.state()}


@app.post("/api/jobs/{job_id}/resume")
def api_job_resume(job_id: str):
    """중단된 작업을 이어서 실행한다.

    저장해 둔 대상 목록으로 다시 돌리기만 하면 된다.
    이미 받은 영상은 Store.is_done()이 건너뛰므로 멈춘 지점부터 이어진다.
    """
    job = require_job(job_id)
    active = jobs.registry.active()
    if active:
        raise HTTPException(409, {
            "message": "이미 실행 중인 작업이 있습니다.",
            "job_id": active.id,
        })
    if not job.targets:
        raise HTTPException(400, "이어서 실행할 대상 목록이 없습니다.")

    job.cancel = threading.Event()
    job.status = jobs.STATUS_RUNNING
    job.error = None
    job.done = 0
    job.total = len(job.targets)
    job.started_at = time.time()
    job.finished_at = None
    job.last_progress_at = time.time()
    job.save()
    job.log("info", "이어서 실행합니다 — 이미 받은 영상은 건너뜁니다.")

    start_thread(job, job.targets)
    return {"job_id": job.id, "total": job.total}


def require_job(job_id):
    job = jobs.registry.get(job_id)
    if not job:
        raise HTTPException(404, "작업을 찾을 수 없습니다.")
    return job


@app.get("/api/jobs/{job_id}")
def api_job_state(job_id: str, scope: str = "job"):
    job = require_job(job_id)
    state = job.state()
    state["summary"] = job.summary(scope)
    return state


@app.post("/api/jobs/{job_id}/stop")
def api_job_stop(job_id: str):
    job = require_job(job_id)
    job.cancel.set()
    job.log("info", "중지를 요청했습니다. 처리 중인 영상까지 마치고 멈춥니다.")
    return {"ok": True}


# 재시도할 때는 대기 시간을 늘려 차단을 피한다
RETRY_SLEEP_MULTIPLIER = 2.5


@app.post("/api/jobs/{job_id}/retry")
def api_job_retry(job_id: str):
    """재시도 가능한 실패만 다시 돌린다."""
    job = require_job(job_id)
    active = jobs.registry.active()
    if active:
        raise HTTPException(409, {
            "message": "이미 실행 중인 작업이 있습니다.",
            "job_id": active.id,
        })

    pending = report.retryable_records(job.records("job"))
    if not pending:
        raise HTTPException(400, "재시도할 수 있는 영상이 없습니다.")

    job.cancel = threading.Event()
    job.status = jobs.STATUS_RUNNING
    job.error = None
    job.done = 0
    job.total = len(pending)
    job.started_at = time.time()
    job.finished_at = None
    job.last_progress_at = time.time()
    job.save()
    job.log("info", "재시도 — {}개를 대기 시간 {}배로 다시 시도합니다.".format(
        len(pending), RETRY_SLEEP_MULTIPLIER))

    start_thread(job, pending, sleep_multiplier=RETRY_SLEEP_MULTIPLIER,
                 skip_done=False)
    return {"ok": True, "count": len(pending)}


@app.websocket("/api/jobs/{job_id}/ws")
async def api_job_ws(websocket: WebSocket, job_id: str, since: int = 0):
    """실시간 로그·진행률.

    0.3초마다 새 로그만 보낸다. 클라이언트가 since로 마지막 위치를 알려주면
    새로고침·재접속 시 이전 로그가 그대로 복원된다.
    """
    await websocket.accept()
    job = jobs.registry.get(job_id)
    if not job:
        await websocket.send_json({"type": "error", "message": "작업을 찾을 수 없습니다."})
        await websocket.close()
        return

    cursor = max(0, since)
    last_state = None
    try:
        while True:
            entries, total = job.logs_since(cursor)
            cursor = total
            if entries:
                await websocket.send_json({
                    "type": "logs", "cursor": cursor, "entries": entries})

            # 0.3초마다 무조건 보내면 소켓이 폭주한다 — 바뀔 때만 보낸다
            state = job.state()
            fingerprint = (state["done"], state["total"], state["status"])
            if fingerprint != last_state:
                last_state = fingerprint
                await websocket.send_json({"type": "state", "state": state})

            if job.status != jobs.STATUS_RUNNING:
                await websocket.send_json({
                    "type": "finished",
                    "state": job.state(),
                    "summary": job.summary("job"),
                })
                break
            await asyncio.sleep(0.3)
    except WebSocketDisconnect:
        return
    except RuntimeError:
        return


# --- 결과 조회 / 다운로드 ---

@app.get("/api/jobs/{job_id}/videos")
def api_job_videos(job_id: str, scope: str = Query("job", pattern="^(job|channel)$")):
    job = require_job(job_id)
    rows = []
    for record in job.records(scope):
        rows.append({
            "video_id": record["video_id"],
            "title": record["title"],
            "upload_date": record["upload_date"],
            "url": record["url"],
            "status": record["status"],
            "char_count": record.get("char_count", 0),
            "message": (report.describe(record["status"])
                        if record["status"] != subtitle.SUCCESS else ""),
            "has_text": bool(record.get("path")),
        })
    rows.sort(key=lambda r: r["upload_date"], reverse=True)
    return {"videos": rows, "scope": scope}


@app.get("/api/jobs/{job_id}/videos/{video_id}")
def api_job_video_text(job_id: str, video_id: str):
    """미리보기. 경로는 기록에서 찾는다 — 입력값으로 경로를 조립하지 않는다."""
    job = require_job(job_id)
    record = job.store().get(video_id)
    if not record or not record.get("path"):
        raise HTTPException(404, "자막 파일이 없습니다.")

    path = os.path.join(job.out_dir, record["path"])
    if not os.path.exists(path):
        raise HTTPException(404, "자막 파일이 없습니다.")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    return {"video_id": video_id, "title": record["title"], "text": text}


def merge_documents(items):
    """자막 여러 개를 한 파일로 이어붙인다.

    items는 {path, title, upload_date, url} 목록이다. 작업이 아니라 경로를 받으므로
    수집 결과와 검색 결과 양쪽에서 쓸 수 있다 — 검색 결과는 여러 채널·여러
    저장 폴더에 흩어져 있어 작업 폴더 하나를 전제할 수 없다.
    """
    parts = []
    for item in items:
        path = item.get("path")
        if not path or not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            body = f.read().strip()
        parts.append(
            "{sep}\n제목: {title}\n업로드일: {date}\nURL: {url}\n{sep}\n\n{body}\n".format(
                sep="=" * 70, title=item.get("title", ""),
                date=item.get("upload_date", ""), url=item.get("url", ""), body=body))
    return "\n\n".join(parts)


def zip_documents(items, extra=None):
    """자막 여러 개를 ZIP으로 묶는다. 채널명을 폴더로 둔다.

    extra는 (실제경로, ZIP안의이름) 목록 — 인덱스 파일을 함께 넣을 때 쓴다.
    """
    buffer = io.BytesIO()
    used = set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in items:
            path = item.get("path")
            if not path or not os.path.exists(path):
                continue
            name = os.path.join(item.get("channel") or "", os.path.basename(path))
            if name in used:      # 다른 폴더에 같은 이름이 있을 수 있다
                stem, ext = os.path.splitext(name)
                name = "{}_{}{}".format(stem, len(used), ext)
            used.add(name)
            zf.write(path, name)
        for src, arcname in (extra or []):
            if os.path.exists(src):
                zf.write(src, arcname)
    return buffer.getvalue()


def job_items(job, records):
    """작업 기록을 merge_documents가 받는 형태로 바꾼다."""
    items = []
    for record in records:
        if record["status"] != subtitle.SUCCESS or not record.get("path"):
            continue
        items.append({
            "path": os.path.join(job.out_dir, record["path"]),
            "channel": job.channel_name,
            "title": record.get("title", ""),
            "upload_date": record.get("upload_date", ""),
            "url": record.get("url", ""),
        })
    return items


@app.get("/api/jobs/{job_id}/download")
def api_job_download(job_id: str,
                     type: str = Query("zip", pattern="^(zip|xlsx|merged)$"),
                     scope: str = Query("job", pattern="^(job|channel)$")):
    job = require_job(job_id)
    records = job.records(scope)
    stem = storage.sanitize_filename(job.channel_name) or "transcripts"

    if type == "xlsx":
        # 요청한 범위만 담은 인덱스를 즉석에서 만든다.
        # 자막만 있어야 할 채널 폴더가 아니라 작업 폴더에 쓴다.
        os.makedirs(jobs.JOBS_DIR, exist_ok=True)
        tmp = os.path.join(jobs.JOBS_DIR, "{}_index_{}.xlsx".format(job.id, scope))
        storage.write_index(records, tmp)
        return FileResponse(tmp, filename="{}_index.xlsx".format(stem),
                            media_type="application/vnd.openxmlformats-"
                                       "officedocument.spreadsheetml.sheet")

    if type == "merged":
        text = merge_documents(job_items(job, records))
        return Response(
            content=text.encode("utf-8"),
            media_type="text/plain; charset=utf-8",
            headers=attachment_headers("{}_merged.txt".format(stem)))

    os.makedirs(jobs.JOBS_DIR, exist_ok=True)
    index_tmp = os.path.join(jobs.JOBS_DIR, "{}_index_{}.xlsx".format(job.id, scope))
    storage.write_index(records, index_tmp)
    payload = zip_documents(job_items(job, records),
                            extra=[(index_tmp, "index.xlsx")])
    return Response(content=payload, media_type="application/zip",
                    headers=attachment_headers("{}.zip".format(stem)))


# --- 저장 위치 ---

class RootRequest(BaseModel):
    path: str


@app.get("/api/settings")
def api_settings():
    settings = load_settings()
    settings["default_root"] = OUTPUT_ROOT
    return settings


@app.post("/api/settings/root")
def api_set_root(req: RootRequest):
    path = os.path.abspath(os.path.expanduser(req.path.strip()))
    if not os.path.isdir(path):
        raise HTTPException(400, "폴더를 찾을 수 없습니다: {}".format(path))
    if not os.access(path, os.W_OK):
        raise HTTPException(400, "이 폴더에 쓸 권한이 없습니다: {}".format(path))
    return save_settings(path)


# 폴더 선택 창은 사용자가 닫을 때까지 돌아오지 않는다. 서버가 멈추지 않도록
# 별도 스레드에서 돌리고 상한을 둔다.
FOLDER_PICK_TIMEOUT = 180


def _choose_folder():
    """맥 기본 폴더 선택 창을 띄운다.

    activate를 넣지 않으면 창이 브라우저 뒤에 떠서 아무 일도 없는 것처럼 보인다.
    """
    script = (
        'tell application "System Events" to activate\n'
        'set f to choose folder with prompt "자막을 저장할 폴더를 고르세요"\n'
        'return POSIX path of f'
    )
    proc = subprocess.run(["osascript", "-e", script],
                          capture_output=True, text=True,
                          timeout=FOLDER_PICK_TIMEOUT)
    if proc.returncode != 0:
        return None            # 사용자가 취소한 경우도 여기로 온다
    return proc.stdout.strip() or None


@app.post("/api/pick-folder")
def api_pick_folder():
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            path = pool.submit(_choose_folder).result(
                timeout=FOLDER_PICK_TIMEOUT + 5)
    except (subprocess.TimeoutExpired, concurrent.futures.TimeoutError):
        raise HTTPException(408, "폴더 선택 창이 응답하지 않았습니다.")
    except FileNotFoundError:
        raise HTTPException(400, "이 시스템에서는 폴더 선택 창을 열 수 없습니다. 경로를 직접 입력해 주세요.")
    if not path:
        return {"cancelled": True}
    return {"cancelled": False, "path": os.path.normpath(path)}


# --- 자막 검색 ---

class ExportRequest(BaseModel):
    paths: list = []
    type: str = "merged"      # merged | zip


@app.get("/api/search/status")
def api_search_status():
    status = search.index_status()
    status["roots"] = load_settings()["known_roots"]
    return status


@app.post("/api/search/index")
def api_search_index(force: bool = False):
    roots = load_settings()["known_roots"]
    return search.build_index(roots, force=force)


@app.get("/api/search")
def api_search(q: str, channel: str = "", limit: int = 30):
    result = search.search(q, channel=channel or None, limit=max(1, min(limit, 200)))
    meta = search.file_meta([r["path"] for r in result["results"]])
    for row in result["results"]:
        info = meta.get(row["path"], {})
        row["upload_date"] = info.get("upload_date", "")
        row["url"] = info.get("url", "")
        row["char_count"] = info.get("char_count", 0)
    return result


@app.post("/api/search/export")
def api_search_export(req: ExportRequest):
    """검색에서 고른 자막만 내보낸다 — AI에 넣을 묶음을 만드는 단계."""
    if not req.paths:
        raise HTTPException(400, "내보낼 자막을 선택하세요.")
    meta = search.file_meta(req.paths)
    items = [meta[p] for p in req.paths if p in meta]
    if not items:
        raise HTTPException(404, "선택한 자막을 찾을 수 없습니다. 색인을 갱신해 보세요.")

    if req.type == "zip":
        return Response(content=zip_documents(items), media_type="application/zip",
                        headers=attachment_headers("자막모음.zip"))
    text = merge_documents(items)
    return Response(content=text.encode("utf-8"),
                    media_type="text/plain; charset=utf-8",
                    headers=attachment_headers("자막모음.txt"))


# --- 프런트엔드 ---

@app.get("/")
def index():
    path = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(path):
        return HTMLResponse("<h1>준비 중</h1>")
    with open(path, encoding="utf-8") as f:
        return HTMLResponse(f.read())


if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

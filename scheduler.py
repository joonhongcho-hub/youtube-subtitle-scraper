"""예약 수집 — 화면이 다루는 설정과 launchd 배선.

수집 자체는 study/collect.py 가 한다. 이 파일은 "언제 돌지"(schedule.json →
launchd)와 "무엇을 돌지"(channels.json)만 다루고, 지금 한 번 돌릴 때는 기존
작업 큐에 그대로 얹는다 — 진행률·로그·중지 화면을 새로 만들지 않기 위해서다.

장부(state.json·주차 파일·status.json)는 예약 실행과 똑같은 규칙으로 쓴다.
규칙이 두 벌이 되면 화면 숫자와 예약 실행 숫자가 갈라진다.
"""

import datetime
import json
import os
import plistlib
import shutil
import subprocess
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STUDY_DIR = os.path.join(BASE_DIR, "study")
if STUDY_DIR not in sys.path:
    sys.path.insert(0, STUDY_DIR)

import channel      # noqa: E402
import collect      # noqa: E402  (study/collect.py — 장부 규칙을 함께 쓴다)
import engine       # noqa: E402
import jobs         # noqa: E402
import storage      # noqa: E402

SHARED_DIR = collect.SHARED_CONFIG_DIR
SCHEDULE_PATH = collect.SCHEDULE_PATH
SETTINGS_PATH = os.path.join(collect.DEFAULT_CONFIG_DIR, "settings.json")

LABEL = "com.jhc.subtitle-weekly"
PLIST_SRC = os.path.join(STUDY_DIR, "{}.plist".format(LABEL))
PLIST_DST = os.path.expanduser("~/Library/LaunchAgents/{}.plist".format(LABEL))
INSTALL_SH = os.path.join(STUDY_DIR, "install-schedule.sh")
LOG_PATH = os.path.join(collect.DATA_DIR, "collect.log")

# 지금 예약이 수·일 06:00 이다. 설정 파일이 없을 때 이 값에서 시작한다.
DEFAULT_SCHEDULE = {
    "schema": collect.SCHEMA,
    "enabled": True,
    "days": [0, 3],          # launchd 규칙 — 0=일요일, 3=수요일
    "hour": 6,
    "minute": 0,
    "first_run_days": collect.FIRST_RUN_DAYS,
}

DAY_NAMES = ["일", "월", "화", "수", "목", "금", "토"]


def now_stamp():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# --- 설정 파일 -----------------------------------------------------------

def load_json(path, default):
    """읽다 실패하면 기본값을 준다. 서버는 어떤 파일 때문에도 죽으면 안 된다."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_settings():
    """collect.py 가 쓰는 settings.json — output_root 를 절대경로로 풀어서 준다."""
    settings = load_json(SETTINGS_PATH, {})
    settings.setdefault("output_root", "output/자막")
    settings["output_root"] = collect.resolve_root(settings)
    return settings


def channels_file():
    return collect.channels_path()


def load_channels():
    """(경로, 목록, 문제목록) — 형식이 깨져 있어도 화면은 떠야 한다."""
    path = channels_file()
    config = load_json(path, None)
    if not isinstance(config, dict):
        return path, {"schema": collect.SCHEMA, "channels": []}, [
            "channels.json 을 읽을 수 없습니다: {}".format(path)]
    return path, config, collect.channel_problems(config)


def save_channels(config):
    """검사를 통과한 목록만 쓴다. 쓰기는 임시 파일 → os.replace (storage.save_json).

    수집이 도는 중에 반쯤 쓰인 파일을 읽으면 검증에서 멈춘다.
    """
    problems = collect.channel_problems(config)
    if problems:
        raise ValueError("채널 목록 형식이 맞지 않습니다: {}".format(
            " / ".join(problems[:3])))
    config["updated"] = datetime.date.today().strftime("%Y%m%d")
    os.makedirs(SHARED_DIR, exist_ok=True)
    storage.save_json(os.path.join(SHARED_DIR, "channels.json"), config)


def load_schedule():
    data = load_json(SCHEDULE_PATH, None)
    if not isinstance(data, dict):
        data = dict(DEFAULT_SCHEDULE)
    merged = dict(DEFAULT_SCHEDULE)
    merged.update(data)
    merged["days"] = sorted({int(d) for d in merged.get("days") or []
                             if 0 <= int(d) <= 6}) or list(DEFAULT_SCHEDULE["days"])
    merged["hour"] = max(0, min(23, int(merged.get("hour", 6))))
    merged["minute"] = max(0, min(59, int(merged.get("minute", 0))))
    merged["enabled"] = bool(merged.get("enabled", True))
    return merged


def save_schedule(schedule):
    schedule = dict(schedule)
    schedule["schema"] = collect.SCHEMA
    schedule["updated"] = now_stamp()
    os.makedirs(SHARED_DIR, exist_ok=True)
    storage.save_json(SCHEDULE_PATH, schedule)
    return schedule


# --- 다음 실행 시각 ------------------------------------------------------

def next_run(schedule, now=None):
    """다음으로 돌 시각. 켜진 요일이 없으면 None.

    launchd 의 Weekday 는 0=일요일이고 파이썬 weekday() 는 0=월요일이라
    한쪽으로 맞춰 센다. 여기서 틀리면 화면만 틀린 요일을 말한다.
    """
    days = schedule.get("days") or []
    if not days:
        return None
    now = now or datetime.datetime.now()
    for ahead in range(0, 8):
        day = now + datetime.timedelta(days=ahead)
        if (day.weekday() + 1) % 7 not in days:
            continue
        when = day.replace(hour=schedule["hour"], minute=schedule["minute"],
                           second=0, microsecond=0)
        if when > now:
            return when
    return None


# --- launchd -------------------------------------------------------------

def launchd_loaded():
    """launchctl 에 실제로 올라가 있는지. 설정 파일의 enabled 는 믿지 않는다.

    파일은 켜져 있는데 launchd 에는 없는 상태가 실제로 생긴다.
    """
    try:
        proc = subprocess.run(["launchctl", "list"], capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return LABEL in (proc.stdout or "")


def plist_body(schedule):
    """schedule.json 으로 plist 내용을 만든다.

    요일이 여러 개면 StartCalendarInterval 을 배열로 준다 — launchd 는 사전
    하나만 받으면 요일 하나만 잡는다.
    """
    intervals = [{"Weekday": int(day), "Hour": int(schedule["hour"]),
                  "Minute": int(schedule["minute"])}
                 for day in schedule["days"]]
    command = ('cd "{}" && ./venv/bin/python study/collect.py'.format(BASE_DIR))
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/zsh", "-lc", command],
        "StartCalendarInterval": intervals,
        "StandardOutPath": LOG_PATH,
        "StandardErrorPath": LOG_PATH,
        # 서버가 뜰 때마다 수집이 시작되면 곤란하다
        "RunAtLoad": False,
    }


def write_plist(schedule):
    """plist 를 새로 쓴다. 손으로 쓴 처음 파일은 .bak 으로 한 번만 남긴다.

    저장할 때마다 백업을 만들면 요일 하나 바꿀 때마다 파일이 쌓인다. 되돌릴 때
    필요한 것은 "화면이 건드리기 전의 그 파일" 하나다.
    """
    backup = PLIST_SRC + ".bak"
    if os.path.exists(PLIST_SRC) and not os.path.exists(backup):
        shutil.copy2(PLIST_SRC, backup)
    tmp = PLIST_SRC + ".tmp"
    with open(tmp, "wb") as f:
        plistlib.dump(plist_body(schedule), f)
    os.replace(tmp, PLIST_SRC)
    return PLIST_SRC


def run_command(args):
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    output = "\n".join(x for x in (proc.stdout, proc.stderr) if x and x.strip())
    return proc.returncode == 0, output.strip()


def apply_schedule(schedule):
    """plist 를 다시 쓰고 launchd 에 반영한다. 실패는 삼키지 않고 그대로 올린다.

    켜기는 install-schedule.sh 가 이미 하는 일(복사 → bootout → bootstrap →
    확인)을 그대로 쓴다. 끄기는 bootout 만 한다 — 설치된 파일은 지우지 않는다.
    """
    write_plist(schedule)
    if schedule.get("enabled"):
        ok, output = run_command(["/bin/zsh", INSTALL_SH])
    else:
        ok, output = run_command(
            ["launchctl", "bootout", "gui/{}/{}".format(os.getuid(), LABEL)])
        # 이미 내려가 있으면 bootout 이 실패로 끝난다. 그건 실패가 아니다.
        if not ok and not launchd_loaded():
            ok, output = True, (output + "\n(이미 내려가 있었습니다)").strip()
    return {"ok": ok, "output": output, "loaded": launchd_loaded()}


# --- 채널 현황 -----------------------------------------------------------

def channel_folder(settings, entry, find_dir=None):
    """이 채널의 자막 폴더. 이미 만들어진 폴더가 있으면 그쪽을 쓴다.

    예약 실행은 output_root/카테고리/채널명 에 쌓는데, 사용자가 폴더를 옮겼을
    수 있다. 옮긴 자리를 찾아 쓰지 않으면 같은 채널이 두 곳으로 갈라진다.
    """
    name = entry.get("name") or ""
    if find_dir:
        found = find_dir(name)
        if found:
            return found
    return collect.channel_out_dir(settings, entry, name)


def channel_rows(find_dir=None):
    """화면이 그릴 채널 목록 — 파일 수와 장부 수를 지금 이 자리에서 센다.

    status.json 은 수집이 돈 뒤에야 갱신된다. 화면을 열 때마다 세어야 사용자가
    방금 지운 파일이 바로 반영된다. 유튜브에는 요청하지 않는다(썸네일 없음).
    """
    settings = load_settings()
    path, config, problems = load_channels()
    state = load_json(collect.STATE_PATH, {}) or {}
    channels_state = state.get("channels", {}) if isinstance(state, dict) else {}
    status = load_json(collect.STATUS_PATH, {}) or {}
    last_flags = {c.get("name"): c for c in status.get("channels", [])
                  if isinstance(c, dict)}

    rows = []
    for entry in config.get("channels", []):
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or ""
        folder = channel_folder(settings, entry, find_dir)
        files = collect.count_txt(folder)
        recorded = collect.count_records(folder)
        known = channels_state.get(name, {}) if isinstance(channels_state, dict) else {}
        previous = last_flags.get(name, {})

        flags = []
        if files and files > recorded:
            flags.append("orphan_files")
        if previous.get("channel_failed"):
            flags.append("channel_error")

        rows.append({
            "name": name,
            "url": entry.get("url", ""),
            "handle": handle_of(entry.get("url", "")),
            "role": entry.get("role", "news"),
            "active": bool(entry.get("active", True)),
            "state": entry.get("state", "confirmed"),
            "category_folder": entry.get("category_folder", ""),
            "folder": folder,
            "total_files": files,
            "recorded_files": recorded,
            "last_content_at": known.get("last_content_at"),
            "last_count": known.get("last_content_count", 0),
            "last_success": known.get("last_success"),
            "last_failed": previous.get("last_failed", 0),
            "flags": flags,
        })
    return {"path": path, "channels": rows, "problems": problems,
            "folders": known_folders(settings)}


def handle_of(url):
    """URL 에서 @핸들만 떼어낸다. 채널 ID 주소면 빈 문자열."""
    part = (url or "").rstrip("/").split("/")[-1]
    return part if part.startswith("@") else ""


def known_folders(settings):
    """저장 폴더 드롭다운에 채울 후보 — output_root 바로 아래 카테고리 폴더."""
    root = settings["output_root"]
    if not os.path.isdir(root):
        return []
    try:
        return sorted(name for name in os.listdir(root)
                      if os.path.isdir(os.path.join(root, name))
                      and not name.startswith((".", "_")))
    except OSError:
        return []


# --- 지난 실행 이력 ------------------------------------------------------

def history(limit=10):
    """주차 파일들에서 최근 실행을 모은다. 새 것이 앞에 온다."""
    runs = []
    if os.path.isdir(collect.COLLECTED_DIR):
        for name in sorted(os.listdir(collect.COLLECTED_DIR), reverse=True):
            if not name.endswith(".json"):
                continue
            doc = load_json(os.path.join(collect.COLLECTED_DIR, name), {})
            week = doc.get("week") or name[:-5]
            for run in reversed(doc.get("runs") or []):
                reasons = {}
                for item in run.get("failures") or []:
                    key = item.get("reason") or "UNKNOWN"
                    reasons.setdefault(key, {"reason": key, "count": 0,
                                             "channels": []})
                    reasons[key]["count"] += 1
                    who = item.get("channel")
                    if who and who not in reasons[key]["channels"]:
                        reasons[key]["channels"].append(who)
                runs.append({
                    "week": week,
                    "ran_at": run.get("ran_at"),
                    "collected": sum(c.get("collected") or 0
                                     for c in run.get("channels") or []),
                    "channels": len(run.get("channels") or []),
                    "failed": len(run.get("failures") or []),
                    "missing_files": len(run.get("missing_files") or []),
                    "reasons": sorted(reasons.values(),
                                      key=lambda r: -r["count"]),
                })
                if len(runs) >= limit:
                    return runs
    return runs


def tail_log(lines=200):
    if not os.path.exists(LOG_PATH):
        return ""
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except OSError as exc:
        return "로그를 읽지 못했습니다: {}".format(exc)


# --- 지금 한 번 수집 -----------------------------------------------------

# 한 번에 하나만 돈다. 두 벌이 같은 장부를 쓰면 편수가 엉킨다.
RUN_LOCK = threading.Lock()
RUN = {
    "running": False,
    "index": 0,
    "total": 0,
    "channel": "",
    "limit": None,
    "started_at": None,
    "finished_at": None,
    "collected": 0,
    "failed": 0,
    "error": None,
    "logs": [],
    "job_id": None,
}
# 채널을 도는 동안 남기는 짧은 기록. 작업 하나하나의 로그는 기존 작업 화면에 있다.
MAX_RUN_LOGS = 200
# 작업이 끝났는지 들여다보는 간격
POLL_SECONDS = 1.0


def run_state():
    snapshot = dict(RUN)
    snapshot["logs"] = list(RUN["logs"])[-30:]
    return snapshot


def note(message):
    RUN["logs"].append("{} {}".format(
        datetime.datetime.now().strftime("%H:%M:%S"), message))
    del RUN["logs"][:-MAX_RUN_LOGS]


def start_run(limit, enqueue, find_dir):
    """예약 수집을 지금 한 번 돌린다. 채널마다 기존 작업 큐에 작업을 넣는다."""
    with RUN_LOCK:
        if RUN["running"]:
            raise RuntimeError("이미 예약 수집이 돌고 있습니다.")
        _, config, problems = load_channels()
        if problems:
            raise ValueError("채널 목록 형식이 맞지 않습니다: {}".format(
                " / ".join(problems[:3])))
        targets = collect.collect_targets(config)
        if not targets:
            raise ValueError("수집할 채널이 없습니다. 역할과 on/off 를 확인하세요.")

        RUN.update({"running": True, "index": 0, "total": len(targets),
                    "channel": "", "limit": limit, "started_at": time.time(),
                    "finished_at": None, "collected": 0, "failed": 0,
                    "error": None, "logs": [], "job_id": None})

    thread = threading.Thread(target=_run_all,
                              args=(config, targets, limit, enqueue, find_dir),
                              daemon=True)
    thread.start()
    return {"total": len(targets)}


def _wait_for(job):
    """작업이 끝날 때까지 기다린다. 큐에 줄을 섰다면 그 시간까지 기다린다."""
    while job.status in (jobs.STATUS_RUNNING, jobs.STATUS_QUEUED):
        time.sleep(POLL_SECONDS)
        if job.status == jobs.STATUS_RUNNING and not job.is_alive():
            # 스레드가 사라졌는데 상태가 그대로면 영원히 기다리게 된다
            jobs.registry.active()
    return job.status


def _collect_one(entry, settings, state, limit, enqueue, find_dir):
    """채널 하나 — 목록을 받아 받을 것만 골라 작업으로 넘기고 요약을 돌려준다.

    고르는 규칙(기준일 이후 + 아직 없는 것)은 예약 실행과 같은 함수를 쓴다.
    """
    name = entry["name"]
    since = (state.get("channels", {}).get(name, {}).get("last_success")
             or collect.default_since())

    resolved = channel.resolve_channel(entry["url"])
    resolved_name = resolved.get("name") or entry["name"]
    folder = channel_folder(settings, entry, find_dir)
    os.makedirs(folder, exist_ok=True)

    cache_path = os.path.join(storage.state_dir(folder), "videos.json")
    listing = channel.fetch_entries(resolved["url"], cache_path, force=True)
    store = engine.Store(folder)
    fresh = [v for v in listing["videos"]
             if collect._is_new(v, since) and not store.is_done(v["video_id"])]
    if limit:
        fresh = fresh[:limit]

    if not fresh:
        note("{} — 새로 받을 영상 없음".format(name))
        return collect._summary(name, folder, [], store, since)

    note("{} — {}편 받으러 갑니다".format(name, len(fresh)))
    job = jobs.Job(resolved_name, resolved["url"], folder, fresh, {
        "langs": settings.get("lang", "ko,en"),
        "timestamps": settings.get("timestamps", True),
    })
    enqueue(job)
    RUN["job_id"] = job.id
    _wait_for(job)

    # 방금 쓴 기록을 다시 읽는다 — 작업 스레드의 Store 와는 다른 객체다
    return collect._summary(name, folder, [v["video_id"] for v in fresh],
                            engine.Store(folder), since)


def _run_all(config, targets, limit, enqueue, find_dir):
    settings = load_settings()
    state = load_json(collect.STATE_PATH, {"channels": {}}) or {"channels": {}}
    state.setdefault("channels", {})
    today = datetime.date.today().strftime("%Y%m%d")
    summaries, failures, missing = [], [], []

    try:
        for i, entry in enumerate(targets, 1):
            RUN["index"], RUN["channel"] = i, entry["name"]
            try:
                summary = _collect_one(entry, settings, state, limit,
                                       enqueue, find_dir)
            except Exception as exc:                      # noqa: BLE001
                # 한 채널이 넘어져도 나머지는 계속 간다 (예약 실행과 같다)
                note("{} — 실패: {}".format(entry["name"], exc))
                failures.append({"channel": entry["name"], "video_id": None,
                                 "reason": "CHANNEL_ERROR", "detail": str(exc)})
                continue

            failures.extend(summary.pop("_failures"))
            missing.extend(summary.pop("_missing"))
            summaries.append(summary)
            RUN["collected"] += summary["collected"]
            # 편수 제한을 건 실행은 나머지를 남겨두므로 기준일을 옮기지 않는다
            collect.remember_run(state, entry["name"], today,
                                 summary["collected"],
                                 advance_baseline=not limit)

        RUN["failed"] = len(failures)
        _write_ledger(config, settings, state, summaries, failures, missing,
                      targets)
        note("끝 — 수집 {}편 / 실패 {}건".format(RUN["collected"], len(failures)))
    except Exception as exc:                              # noqa: BLE001
        RUN["error"] = "{}: {}".format(type(exc).__name__, exc)
        note("멈췄습니다 — {}".format(RUN["error"]))
    finally:
        RUN["running"] = False
        RUN["finished_at"] = time.time()
        RUN["channel"] = ""


def _write_ledger(config, settings, state, summaries, failures, missing, targets):
    """예약 실행과 같은 규칙으로 장부를 쓴다."""
    week = collect.week_id()
    ran_at = now_stamp()
    _, totals = collect.append_run(summaries, failures, missing, ran_at, week)
    storage.save_json(collect.STATE_PATH, state)
    status = collect.build_status(
        config, settings, state, summaries, failures,
        {t["name"] for t in targets}, week, ran_at, missing, totals)
    storage.save_json(collect.STATUS_PATH, status)

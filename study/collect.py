"""일요일 수집 — 뉴스 채널의 신규 영상 자막만 받아온다.

코워크가 일주일에 한 번 이 스크립트를 부른다. 판단은 하지 않는다.
자막을 파일로 떨구고, 무엇을 받았는지 JSON으로 남기는 것까지가 전부다.

    python study/collect.py [--dry-run] [--only 채널명] [--limit N]
                            [--config-dir 폴더]

기준일은 요일이 아니라 config/../data/state.json 의 last_success 다.
맥이 꺼져 2주를 건너뛰어도 다음 실행에서 2주치를 받는다.
"""

import argparse
import datetime
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

# 설정 폴더는 --config-dir 로 바꿀 수 있다. 원본 설정을 건드리지 않고 시험하려고 둔다.
DEFAULT_CONFIG_DIR = os.path.join(ROOT, "config")
DATA_DIR = os.path.join(ROOT, "data")
COLLECTED_DIR = os.path.join(DATA_DIR, "collected")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
# 채널 탭이 읽을 한 벌. 맥이 쓰고 코워크는 읽기만 한다.
STATUS_PATH = os.path.join(DATA_DIR, "status.json")

# 주고받는 JSON 의 형식 번호. 형식을 바꾸면 번호를 올리고 양쪽을 같이 고친다.
SCHEMA = 1

# channels.json 에 올 수 있는 값. 여기 없는 값이 오면 수집을 시작하지 않는다.
ROLES = ("news", "material")
STATES = ("confirmed", "pending", "failed")

# 기준일이 아예 없는 채널을 처음 돌릴 때 얼마나 거슬러 올라갈지
FIRST_RUN_DAYS = 14


def resolve_root(settings):
    """output_root 가 상대경로면 앱 폴더 기준으로 푼다.

    절대경로를 설정 파일에 박아두면 저장소에 넣을 수 없고, 폴더를 옮기면
    조용히 엉뚱한 곳에 쌓인다. 상대경로를 허용해 설정을 같이 버전 관리한다.
    """
    root = settings.get("output_root") or ""
    return root if os.path.isabs(root) else os.path.join(APP, root)


def log(level, message):
    print(message, flush=True)


def die(message):
    print("[중단] {}".format(message), file=sys.stderr)
    sys.exit(1)


def read_json(path, what):
    if not os.path.exists(path):
        die("{} 가 없습니다: {}".format(what, path))
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except ValueError as exc:
        die("{} 를 읽을 수 없습니다 ({}): {}".format(what, exc, path))


def week_id(today=None):
    today = today or datetime.date.today()
    year, week, _ = today.isocalendar()
    return "{}-W{:02d}".format(year, week)


def default_since():
    day = datetime.date.today() - datetime.timedelta(days=FIRST_RUN_DAYS)
    return day.strftime("%Y%m%d")


def check_schema(data, what):
    """형식 번호를 확인한다. 모르는 번호면 수집을 시작하지 않는다.

    같은 파일을 코워크와 맥이 번갈아 읽고 쓴다. 형식이 바뀐 줄 모르고 옛 코드가
    읽으면 필드를 조용히 놓친 채로 몇 시간을 돈다. 번호부터 맞춘다.
    """
    schema = data.get("schema")
    if schema != SCHEMA:
        die("{} 의 schema 가 {} 이어야 합니다 (지금 {!r}). "
            "형식을 확인하고 고친 뒤 다시 실행하세요.".format(what, SCHEMA, schema))


def load_week(path, week):
    """주차 파일을 실행 목록(runs) 형태로 읽는다. 없으면 빈 채로 시작한다.

    한 주에 두 번 돌 수 있다. 매번 덮어쓰면 먼저 받은 실행이 사라져 주간 누계를
    낼 근거가 없어진다. 실제로 3편을 받은 실행이 그렇게 사라진 적이 있다.
    """
    if not os.path.exists(path):
        return {"schema": SCHEMA, "week": week, "runs": []}

    doc = read_json(path, "주차 파일")
    if "runs" in doc:
        check_schema(doc, os.path.basename(path))
        if not isinstance(doc["runs"], list):
            die("{} 의 runs 가 배열이 아닙니다.".format(path))
        doc.setdefault("week", week)
        return doc

    # runs 가 없던 옛 형식 — 통째로 첫 실행으로 감싸 옮긴다. 지우지 않는다.
    return {
        "schema": SCHEMA,
        "week": doc.get("week") or week,
        "runs": [{
            "ran_at": doc.get("ran_at"),
            "channels": doc.get("channels") or [],
            "failures": doc.get("failures") or [],
            "missing_files": doc.get("missing_files") or [],
        }],
    }


def week_total(week, doc):
    """주차 파일에 쌓인 실행을 모두 합한다."""
    collected = failed = 0
    runs = doc.get("runs") or []
    for run in runs:
        for item in run.get("channels") or []:
            collected += item.get("collected") or 0
        failed += len(run.get("failures") or [])
    return {"week": week, "collected": collected, "failed": failed,
            "runs": len(runs)}


def validate_channels(config):
    """channels.json 형식을 확인한다. 어긋나면 수집을 시작하지 않고 멈춘다.

    코워크가 덮어쓰는 파일이라 형식이 깨질 수 있다. 기본값으로 얼버무리고
    진행하면 잘못된 목록으로 몇 시간을 돌리게 된다. 그게 가장 나쁘다.
    """
    check_schema(config, "channels.json")

    entries = config.get("channels")
    if not isinstance(entries, list):
        die("channels.json 에 channels 배열이 없습니다.")

    problems, seen = [], {}
    for i, entry in enumerate(entries):
        where = "channels[{}]".format(i)
        if not isinstance(entry, dict):
            problems.append("{}: 객체가 아닙니다 ({!r})".format(where, entry))
            continue

        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            problems.append("{}: name 이 비어 있습니다 (지금 {!r})".format(where, name))
        else:
            where = '{} "{}"'.format(where, name)
            if name in seen:
                problems.append("{}: name 이 channels[{}] 와 중복됩니다".format(
                    where, seen[name]))
            seen[name] = i

        url = entry.get("url")
        if not isinstance(url, str) or not url.strip():
            problems.append("{}: url 이 없습니다".format(where))
        elif not channel._is_url(url):
            # 이름만 있으면 resolve_channel 이 사람에게 번호를 물어본다.
            # 예약 실행에는 답할 사람이 없으므로 시작 전에 잡는다.
            problems.append("{}: url 이 링크가 아닙니다 ({!r})".format(where, url))

        if entry.get("role") not in ROLES:
            problems.append("{}: role 은 {} 중 하나여야 합니다 (지금 {!r})".format(
                where, " 또는 ".join(ROLES), entry.get("role")))

        if not isinstance(entry.get("active"), bool):
            problems.append("{}: active 는 true/false 여야 합니다 (지금 {!r})".format(
                where, entry.get("active")))

        # state 는 없으면 confirmed 로 본다. 있으면 아는 값이어야 한다.
        if entry.get("state", "confirmed") not in STATES:
            problems.append("{}: state 는 {} 중 하나여야 합니다 (지금 {!r})".format(
                where, " / ".join(STATES), entry.get("state")))

    if not problems:
        return
    print("[중단] channels.json 형식이 맞지 않습니다 ({}건):".format(len(problems)),
          file=sys.stderr)
    for problem in problems:
        print("  - {}".format(problem), file=sys.stderr)
    print("\n고치기 전에는 수집하지 않습니다.", file=sys.stderr)
    sys.exit(1)


def news_channels(config):
    """수집 대상만 골라낸다 — 확인된 뉴스 채널 중 켜져 있는 것."""
    out = []
    for entry in config.get("channels", []):
        if entry.get("role") != "news":
            continue
        if not entry.get("active", True):
            continue
        if entry.get("state", "confirmed") != "confirmed":
            continue
        out.append(entry)
    return out


def channel_out_dir(settings, entry, resolved_name):
    """자막이 떨어질 폴더 — {output_root}/{카테고리}/{채널명}"""
    parts = [settings["output_root"]]
    folder = entry.get("category_folder")
    if folder:
        parts.append(folder)
    parts.append(storage.sanitize_filename(resolved_name))
    return os.path.join(*parts)


def collect_channel(entry, settings, state, langs, mode, api, limit=None):
    """채널 하나를 수집하고 결과 요약을 돌려준다."""
    name = entry["name"]
    since = state.get("channels", {}).get(name, {}).get("last_success") or default_since()

    log("info", "\n=== {} — {} 이후 ===".format(name, since))

    url = entry.get("url") or ""
    if not channel._is_url(url):
        # 이름만 있으면 resolve_channel 이 사람에게 번호를 물어본다. 예약 실행에는
        # 답할 사람이 없고, 그때 나오는 SystemExit 은 아래 except Exception 을
        # 그냥 지나쳐 나머지 채널까지 통째로 멈춘다.
        raise RuntimeError(
            "url 이 링크가 아닙니다: {!r} — channels.json 에 채널 URL을 넣어주세요.".format(url))

    resolved = channel.resolve_channel(url)
    resolved_name = resolved.get("name") or channel.fetch_channel_meta(
        resolved["url"]).get("name") or name

    out_dir = channel_out_dir(settings, entry, resolved_name)
    os.makedirs(out_dir, exist_ok=True)

    # 목록은 매번 새로 받는다. 캐시를 재사용하면 새 영상이 안 잡힌다.
    cache_path = os.path.join(storage.state_dir(out_dir), "videos.json")
    listing = channel.fetch_entries(resolved["url"], cache_path, force=True, log=log)
    videos = listing["videos"]

    fresh = [v for v in videos if _is_new(v, since)]
    if limit:
        fresh = fresh[:limit]
    log("info", "전체 {}개 중 신규 후보 {}개".format(len(videos), len(fresh)))

    store = engine.Store(out_dir)
    if not fresh:
        return _summary(name, out_dir, [], store, since), True

    engine.process_videos(fresh, resolved_name, store, langs, mode, api,
                          log=log, timestamps=settings.get("timestamps", True))

    ids = [v["video_id"] for v in fresh]
    return _summary(name, out_dir, ids, store, since), True


def _is_new(video, since):
    """업로드일을 모르는 영상은 남긴다 — 놓치는 것보다 중복이 낫다.

    이미 받은 영상은 engine.Store 가 processed_ids.json 으로 다시 걸러낸다.
    """
    raw = str(video.get("upload_date") or "")
    known = len(raw) == 8 and raw.isdigit() and raw != "00000000"
    return (not known) or raw >= since


def _summary(name, out_dir, video_ids, store, since):
    files, failures, missing = [], [], []
    for record in store.records_for(video_ids):
        if record["status"] != subtitle.SUCCESS:
            failures.append({
                "channel": name,
                "video_id": record["video_id"],
                "title": record["title"],
                "reason": record["status"],
            })
            continue
        abs_path = os.path.join(out_dir, record["path"])
        # 기록만 있고 파일이 없는 상태가 실제로 발생한 적이 있다. 드러나게 둔다.
        if not os.path.exists(abs_path):
            missing.append({"channel": name, "video_id": record["video_id"],
                            "expected": abs_path})
            continue
        files.append({
            "video_id": record["video_id"],
            "title": record["title"],
            "upload_date": record["upload_date"],
            "url": record["url"],
            "path": abs_path,
            "chars": record.get("char_count", 0),
        })
    return {
        "name": name,
        "since": since,
        "out_dir": out_dir,
        "new_videos": len(video_ids),
        "collected": len(files),
        "failed": len(failures),
        "files": files,
        "_failures": failures,
        "_missing": missing,
    }


def count_txt(folder):
    """폴더의 자막 개수. 폴더가 없으면 None — 0 과 구분해야 사유를 적을 수 있다."""
    if not os.path.isdir(folder):
        return None
    try:
        return sum(1 for name in os.listdir(folder) if name.endswith(".txt"))
    except OSError:
        return None


def status_folder(settings, entry, summary):
    """total_files 를 셀 폴더.

    이번에 수집한 채널은 자막이 실제로 떨어진 폴더를 그대로 쓴다. 나머지는
    channels.json 의 name 으로 만든다 — 유튜브가 부르는 채널명을 모르기 때문이다.
    둘이 어긋나면 폴더를 못 찾고, 그 사실은 note 에 그대로 드러난다.
    """
    if summary and summary.get("out_dir"):
        return summary["out_dir"]
    parts = [settings["output_root"]]
    folder = entry.get("category_folder")
    if folder:
        parts.append(folder)
    parts.append(storage.sanitize_filename(entry["name"]))
    return os.path.join(*parts)


def build_status(config, settings, state, summaries, failures, targets,
                 week, ran_at, missing, totals):
    """채널 탭이 읽을 한 벌.

    수집 대상이 아니었던 채널도 빠짐없이 넣는다 — 화면에는 목록 전체가 떠야 한다.
    절대 경로와 자막 본문은 넣지 않는다. 코워크가 그대로 페이지에 올릴 파일이라
    가벼워야 한다.

    flags 에는 코드 문자열만 넣는다. 화면에 뜰 문구는 페이지가 만든다 — 여기에
    한국어를 넣으면 문구 하나 고치려고 맥 코드를 고치게 된다.
    """
    done = {s["name"]: s for s in summaries}
    failed = {}
    for item in failures:
        failed[item["channel"]] = failed.get(item["channel"], 0) + 1

    channels = []
    for entry in config["channels"]:
        name = entry["name"]
        summary = done.get(name)
        total = count_txt(status_folder(settings, entry, summary))
        known = state["channels"].get(name, {})
        last_at = known.get("last_content_at")

        flags = []
        if name not in targets:
            flags.append("not_targeted")
        if last_at is None and total:
            # 받은 기록은 없는데 자막이 있다. 다른 경로로 받았다는 신호다.
            # 화면이 "한 번도 수집 안 함 / 19편 보유"를 그대로 그리지 않게 한다.
            flags.append("orphan_files")

        channels.append({
            "name": name,
            "role": entry.get("role"),
            "active": bool(entry.get("active", True)),
            "state": entry.get("state", "confirmed"),
            # 날짜와 편수는 늘 같은 실행을 가리킨다 — 마지막으로 1편 이상 받은 실행
            "last_content_at": last_at,
            "last_count": known.get("last_content_count", 0),
            "this_run": summary["collected"] if summary else 0,
            # 폴더가 없으면 null. 0(폴더는 있는데 빔)과 다른 상태다.
            "total_files": total,
            "last_failed": failed.get(name, 0),
            "flags": flags,
        })

    return {
        "schema": SCHEMA,
        "updated": ran_at,
        "last_run": {
            "week": week,
            "ran_at": ran_at,
            "collected": sum(c["this_run"] for c in channels),
            "failed": len(failures),
            "missing_files": len(missing),
        },
        "week_total": totals,
        "channels": channels,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="뉴스 채널 신규 자막 수집")
    parser.add_argument("--dry-run", action="store_true",
                        help="무엇을 받을지만 보여주고 수집하지 않는다")
    parser.add_argument("--only", default=None, help="이 채널만 처리")
    parser.add_argument("--limit", type=int, default=None,
                        help="채널당 N개까지만 (테스트용)")
    parser.add_argument("--config-dir", default=None,
                        help="설정을 읽을 폴더 (기본: study/config)")
    args = parser.parse_args(argv)

    config_dir = args.config_dir or DEFAULT_CONFIG_DIR
    settings = read_json(os.path.join(config_dir, "settings.json"), "settings.json")
    config = read_json(os.path.join(config_dir, "channels.json"), "channels.json")
    # 네트워크를 건드리기 전에 목록부터 확인한다
    validate_channels(config)
    if not settings.get("output_root"):
        die("settings.json 에 output_root 가 없습니다.")
    settings["output_root"] = resolve_root(settings)

    state = {"channels": {}}
    if os.path.exists(STATE_PATH):
        state = read_json(STATE_PATH, "state.json")
    state.setdefault("channels", {})

    targets = news_channels(config)
    if args.only:
        targets = [t for t in targets if t["name"] == args.only]
    if not targets:
        die("수집할 채널이 없습니다. channels.json 의 role/active/state 를 확인하세요.")

    if args.dry_run:
        for entry in targets:
            last = state["channels"].get(entry["name"], {}).get("last_success")
            print("{:<24} since={}".format(entry["name"], last or default_since()))
        return 0

    langs = [x.strip() for x in settings.get("lang", "ko,en").split(",") if x.strip()]
    mode, _ = subtitle.verify_transcript_api(log=log)
    api = subtitle.make_api() if mode == subtitle.MODE_API else None

    today = datetime.date.today().strftime("%Y%m%d")
    channels, failures, missing = [], [], []

    for entry in targets:
        try:
            summary, ok = collect_channel(entry, settings, state, langs,
                                          mode, api, limit=args.limit)
        except Exception as exc:                      # noqa: BLE001
            # 한 채널이 넘어져도 나머지는 계속 간다
            log("fail", "[{}] 실패 — {}".format(entry["name"], exc))
            failures.append({"channel": entry["name"], "video_id": None,
                             "reason": "CHANNEL_ERROR", "detail": str(exc)})
            continue

        failures.extend(summary.pop("_failures"))
        missing.extend(summary.pop("_missing"))
        channels.append(summary)
        if ok:
            # 성공한 채널만 기준일을 옮긴다. 실패한 채널은 다음에 다시 시도된다.
            known = dict(state["channels"].get(entry["name"], {}))
            # last_success 는 --since 기준일이다. 뜻을 바꾸지 않는다.
            known["last_success"] = today
            known["last_run"] = datetime.datetime.now().isoformat(timespec="seconds")
            if summary["collected"] > 0:
                # 1편이라도 받았을 때만 옮긴다. 0편 실행이 지난 편수를 지우면
                # 화면에는 "마지막 수집 0편"이라는 거짓말이 뜬다.
                known["last_content_at"] = today
                known["last_content_count"] = summary["collected"]
            state["channels"][entry["name"]] = known

    week = week_id()
    ran_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    os.makedirs(COLLECTED_DIR, exist_ok=True)
    out_path = os.path.join(COLLECTED_DIR, "{}.json".format(week))

    # 이번 실행은 이번에 받은 것만 담아 뒤에 덧붙인다. 실행끼리 파일이 겹치지 않는다.
    doc = load_week(out_path, week)
    doc["runs"].append({
        "ran_at": ran_at,
        "channels": channels,
        "failures": failures,
        "missing_files": missing,
    })
    storage.save_json(out_path, doc)
    storage.save_json(STATE_PATH, state)

    totals = week_total(week, doc)
    status = build_status(config, settings, state, channels, failures,
                          {t["name"] for t in targets}, week, ran_at,
                          missing, totals)
    storage.save_json(STATUS_PATH, status)

    total = sum(c["collected"] for c in channels)
    print("\n" + "=" * 46)
    print("{} — 수집 {}편 / 실패 {}건 / 파일없음 {}건".format(
        week, total, len(failures), len(missing)))
    print("{} 누계 — 수집 {}편 / 실패 {}건 / 실행 {}번".format(
        week, totals["collected"], totals["failed"], totals["runs"]))
    print(out_path)
    print(STATUS_PATH)
    print("=" * 46)
    # 부분 성공을 정상으로 본다. 실패는 JSON 에 남아 코워크가 읽는다.
    return 0


if __name__ == "__main__":
    sys.exit(main())

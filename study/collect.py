"""예약 수집 — 뉴스·학습 채널의 신규 영상 자막만 받아온다.

launchd 가 정해둔 요일·시각에 이 스크립트를 부른다(웹의 예약 탭이 그 일정을
관리한다). 판단은 하지 않는다. 자막을 파일로 떨구고, 무엇을 받았는지 JSON으로
남기는 것까지가 전부다.

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
# 채널 목록은 자막이 쌓이는 곳 옆으로 옮겼다 — 폴더 하나만 열면 "무엇을 모으고
# 있고 무엇이 모였는지"가 한자리에 있다. 옛 자리도 계속 읽어 조용히 깨지지 않게 한다.
SHARED_CONFIG_DIR = os.path.join(APP, "output", "_설정")
SCHEDULE_PATH = os.path.join(SHARED_CONFIG_DIR, "schedule.json")
DATA_DIR = os.path.join(ROOT, "data")
COLLECTED_DIR = os.path.join(DATA_DIR, "collected")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
# 채널 탭이 읽을 한 벌. 맥이 쓰고 코워크는 읽기만 한다.
STATUS_PATH = os.path.join(DATA_DIR, "status.json")

# 주고받는 JSON 의 형식 번호. 형식을 바꾸면 번호를 올리고 양쪽을 같이 고친다.
SCHEMA = 1

# channels.json 에 올 수 있는 값. 여기 없는 값이 오면 수집을 시작하지 않는다.
# 역할은 자막을 받을지 말지가 아니라 "받아둔 자막을 어디에 쓰는지"다.
#   news   주간 브리핑에 들어간다
#   study  학습 탭의 재료로 쓴다
# 한 채널이 둘 다 가질 수 있다. 어느 쪽이든 자막은 한 번만 받는다.
# 등록된 채널은 모두 수집 대상이고, 쉬게 하려면 active 를 끈다.
ROLES = ("news", "study")
STATES = ("confirmed", "pending", "failed")

# 옛 형식의 role(문자열) → roles(배열). material 은 쌓아만 두던 채널이라
# 학습 재료로 옮긴다 — 브리핑에 넣으면 성격이 달라진다.
LEGACY_ROLES = {"news": ["news"], "study": ["study"], "material": ["study"]}

# 기준일이 아예 없는 채널을 처음 돌릴 때 얼마나 거슬러 올라갈지
FIRST_RUN_DAYS = 14


def channels_path(config_dir=None):
    """채널 목록을 어디서 읽을지 정한다.

    --config-dir 이 주어지면 그 폴더가 최우선이다 (시험용). 그 다음이 새 자리인
    output/_설정, 마지막이 옛 자리인 study/config 다. 옛 자리를 계속 보는 것은
    설정을 옮긴 뒤에도 예약 실행이 멈추지 않게 하기 위해서다.
    """
    if config_dir:
        return os.path.join(config_dir, "channels.json")
    shared = os.path.join(SHARED_CONFIG_DIR, "channels.json")
    if os.path.exists(shared):
        return shared
    return os.path.join(DEFAULT_CONFIG_DIR, "channels.json")


def roles_of(entry):
    """채널의 역할 목록. 옛 형식(role 문자열)도 읽어 배열로 준다.

    파일은 코워크와 화면이 함께 쓴다. 옛 키가 남아 있는 파일을 만나도 멈추지
    않고 읽되, 쓸 때는 항상 새 형식으로만 쓴다 (upgrade_roles 참고).
    """
    raw = entry.get("roles", entry.get("role"))
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, str)]
    # roles 자리에 문자열 하나가 들어 있어도 옛 형식과 같게 읽는다
    if isinstance(raw, str):
        return list(LEGACY_ROLES.get(raw, [raw]))
    return []


def upgrade_roles(entry):
    """항목 하나를 새 형식으로 맞춘다 — roles 만 남기고 옛 role 키는 지운다."""
    entry["roles"] = roles_of(entry)
    entry.pop("role", None)
    return entry


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


def remember_run(state, name, today, collected, advance_baseline=True):
    """이 채널을 방금 돌았다는 사실을 장부에 적는다.

    last_success 는 수집 범위를 정하는 기준일이다. 편수 제한을 걸고 돌린 실행은
    앞의 N편만 받고 나머지를 남겨두므로 기준일을 옮기면 안 된다 — 옮기면 남은
    영상이 다음 실행의 후보에서 빠져 영영 들어오지 않는다.
    """
    known = dict(state.setdefault("channels", {}).get(name, {}))
    if advance_baseline:
        known["last_success"] = today
    known["last_run"] = datetime.datetime.now().isoformat(timespec="seconds")
    if collected > 0:
        # 1편이라도 받았을 때만 옮긴다. 0편 실행이 지난 편수를 지우면
        # 화면에는 "마지막 수집 0편"이라는 거짓말이 뜬다.
        known["last_content_at"] = today
        known["last_content_count"] = collected
    state["channels"][name] = known
    return known


def append_run(channels, failures, missing, ran_at, week=None):
    """이번 실행을 주차 파일 뒤에 덧붙이고 (파일 경로, 주간 누계)를 돌려준다."""
    week = week or week_id()
    os.makedirs(COLLECTED_DIR, exist_ok=True)
    path = os.path.join(COLLECTED_DIR, "{}.json".format(week))
    doc = load_week(path, week)
    # 이번 실행은 이번에 받은 것만 담는다. 실행끼리 파일이 겹치지 않는다.
    doc["runs"].append({
        "ran_at": ran_at,
        "channels": channels,
        "failures": failures,
        "missing_files": missing,
    })
    storage.save_json(path, doc)
    return path, week_total(week, doc)


def count_records(folder):
    """장부(processed_ids.json)에 성공으로 적힌 자막 수. 장부가 없으면 0."""
    path = os.path.join(storage.state_dir(folder), "processed_ids.json")
    records = storage.load_json(path, {})
    if not isinstance(records, dict):
        return 0
    return sum(1 for r in records.values()
               if isinstance(r, dict) and r.get("status") == subtitle.SUCCESS)


def role_problems(entry, where):
    """역할이 어긋난 곳을 적어 돌려준다. 무엇이 잘못됐는지 그대로 적는다."""
    raw = entry.get("roles", entry.get("role"))
    if isinstance(raw, str):
        # 옛 형식(role 문자열)은 읽어준다. 저장할 때 새 형식으로 바뀐다.
        if raw not in LEGACY_ROLES:
            return ["{}: 모르는 역할 {!r} — {} 만 쓸 수 있습니다".format(
                where, raw, " 와 ".join(ROLES))]
        raw = LEGACY_ROLES[raw]
    if raw is None:
        return ["{}: roles 가 없습니다. {} 중 하나 이상을 넣으세요".format(
            where, " 또는 ".join(ROLES))]
    if not isinstance(raw, list):
        return ["{}: roles 는 배열이어야 합니다 (지금 {!r})".format(where, raw)]
    if not raw:
        return ["{}: roles 가 비어 있습니다. {} 중 하나 이상을 넣으세요 "
                "(쉬게 하려면 active 를 끄세요)".format(where, " 또는 ".join(ROLES))]
    unknown = [r for r in raw if r not in ROLES]
    if unknown:
        return ["{}: 모르는 역할 {} — {} 만 쓸 수 있습니다".format(
            where, ", ".join(repr(u) for u in unknown), " 와 ".join(ROLES))]
    return []


def channel_problems(config):
    """channels.json 형식을 살펴 어긋난 곳을 모두 적어 돌려준다.

    멈추지 않고 목록만 돌려주는 것은 화면(예약 탭)도 같은 검사를 써야 하기
    때문이다. 서버 안에서 sys.exit 을 부를 수는 없다.
    """
    problems, seen = [], {}
    if config.get("schema") != SCHEMA:
        problems.append("schema 가 {} 이어야 합니다 (지금 {!r})".format(
            SCHEMA, config.get("schema")))

    entries = config.get("channels")
    if not isinstance(entries, list):
        problems.append("channels 배열이 없습니다")
        return problems

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

        problems.extend(role_problems(entry, where))

        if not isinstance(entry.get("active"), bool):
            problems.append("{}: active 는 true/false 여야 합니다 (지금 {!r})".format(
                where, entry.get("active")))

        # state 는 없으면 confirmed 로 본다. 있으면 아는 값이어야 한다.
        if entry.get("state", "confirmed") not in STATES:
            problems.append("{}: state 는 {} 중 하나여야 합니다 (지금 {!r})".format(
                where, " / ".join(STATES), entry.get("state")))

    return problems


def validate_channels(config):
    """형식이 어긋나면 수집을 시작하지 않고 멈춘다.

    코워크가 덮어쓰는 파일이라 형식이 깨질 수 있다. 기본값으로 얼버무리고
    진행하면 잘못된 목록으로 몇 시간을 돌리게 된다. 그게 가장 나쁘다.
    """
    problems = channel_problems(config)
    if not problems:
        return
    print("[중단] channels.json 형식이 맞지 않습니다 ({}건):".format(len(problems)),
          file=sys.stderr)
    for problem in problems:
        print("  - {}".format(problem), file=sys.stderr)
    print("\n고치기 전에는 수집하지 않습니다.", file=sys.stderr)
    sys.exit(1)


def collect_targets(config):
    """수집 대상만 골라낸다 — 확인됐고 켜져 있는 채널 전부.

    역할은 받아둔 자막을 어디에 쓰는지일 뿐이라 대상을 가르지 않는다.
    한 채널이 뉴스이면서 학습이어도 자막은 한 번만 받는다.
    """
    out = []
    for entry in config.get("channels", []):
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

    store = engine.Store(out_dir)
    candidates = [v for v in videos if _is_new(v, since)]
    # 이미 받은 영상은 여기서 뺀다. 목록 날짜가 하루 이틀 흔들려 받은 영상이
    # since 창에 다시 들어오는데, 남겨두면 "이번에 받음"으로 또 세어진다.
    fresh = [v for v in candidates if not store.is_done(v["video_id"])]
    skipped = len(candidates) - len(fresh)
    if limit:
        fresh = fresh[:limit]
    log("info", "전체 {}개 중 신규 후보 {}개 (이미 받음 {}개 건너뜀)".format(
        len(videos), len(fresh), skipped))

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
    # 채널을 못 연 실패와 영상에 자막이 없는 실패는 사람이 할 일이 완전히 다르다.
    # 주소를 고쳐야 하는 쪽과, 그냥 그런 영상인 쪽을 섞지 않는다.
    video_failed, channel_failed = {}, {}
    for item in failures:
        bucket = (channel_failed if item.get("reason") == "CHANNEL_ERROR"
                  else video_failed)
        bucket[item["channel"]] = bucket.get(item["channel"], 0) + 1

    channels = []
    for entry in config["channels"]:
        name = entry["name"]
        summary = done.get(name)
        folder = status_folder(settings, entry, summary)
        total = count_txt(folder)
        recorded = count_records(folder)
        known = state["channels"].get(name, {})
        last_at = known.get("last_content_at")

        flags = []
        if name not in targets:
            flags.append("not_targeted")
        if total and total > recorded:
            # 폴더의 자막이 장부보다 많다. 다른 경로로 받았거나 장부를 잃은 것이라
            # 이어받기가 어긋난다. 고치지는 않고 사람이 보게만 한다.
            flags.append("orphan_files")
        if channel_failed.get(name):
            flags.append("channel_error")

        channels.append({
            "name": name,
            "roles": roles_of(entry),
            "active": bool(entry.get("active", True)),
            "state": entry.get("state", "confirmed"),
            # 날짜와 편수는 늘 같은 실행을 가리킨다 — 마지막으로 1편 이상 받은 실행
            "last_content_at": last_at,
            "last_count": known.get("last_content_count", 0),
            "this_run": summary["collected"] if summary else 0,
            # 폴더가 없으면 null. 0(폴더는 있는데 빔)과 다른 상태다.
            "total_files": total,
            # 장부에 적힌 수 — total_files 와 다르면 위 orphan_files 가 선다
            "recorded_files": recorded,
            "last_failed": video_failed.get(name, 0),
            "channel_failed": channel_failed.get(name, 0),
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

    settings_dir = args.config_dir or DEFAULT_CONFIG_DIR
    settings = read_json(os.path.join(settings_dir, "settings.json"), "settings.json")
    chan_path = channels_path(args.config_dir)
    # 설정이 두 자리에 있을 수 있다. 어느 쪽을 읽었는지 남겨야 "고쳤는데 왜
    # 그대로냐"를 몇 시간 뒤에 알아채지 않는다.
    log("info", "채널 목록: {}".format(chan_path))
    config = read_json(chan_path, "channels.json")
    # 네트워크를 건드리기 전에 목록부터 확인한다
    validate_channels(config)
    if not settings.get("output_root"):
        die("settings.json 에 output_root 가 없습니다.")
    settings["output_root"] = resolve_root(settings)

    state = {"channels": {}}
    if os.path.exists(STATE_PATH):
        state = read_json(STATE_PATH, "state.json")
    state.setdefault("channels", {})

    targets = collect_targets(config)
    if args.only:
        targets = [t for t in targets if t["name"] == args.only]
    if not targets:
        die("수집할 채널이 없습니다. channels.json 의 active/state 를 확인하세요.")

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
            # 편수 제한을 걸었으면 나머지가 남아 있으므로 기준일을 두고 간다.
            remember_run(state, entry["name"], today, summary["collected"],
                         advance_baseline=not args.limit)

    week = week_id()
    ran_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    out_path, totals = append_run(channels, failures, missing, ran_at, week)
    storage.save_json(STATE_PATH, state)
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

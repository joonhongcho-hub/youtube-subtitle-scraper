"""파일 저장 + 파일명 정제 + index.xlsx."""

import json
import os
import re
import shutil
import unicodedata

import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 윈도우/맥에서 파일명에 쓸 수 없는 문자
FORBIDDEN = r'/\:*?"<>|'
MAX_FILENAME_LEN = 100
# 이모지·기호·제어문자로 취급해 지우는 유니코드 카테고리
DROP_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "So", "Sk"}

INDEX_COLUMNS = ["영상 제목", "업로드일", "파일경로", "원본 URL", "상태", "글자 수"]


def sanitize_filename(name, max_len=MAX_FILENAME_LEN):
    """[안전장치 A] 유튜브 제목을 안전한 파일명으로 바꾼다."""
    if not name:
        return "untitled"

    chars = []
    for ch in name:
        if ch in FORBIDDEN:
            chars.append("_")
        elif unicodedata.category(ch) in DROP_CATEGORIES:
            continue  # 이모지·제어문자 제거
        else:
            chars.append(ch)

    cleaned = re.sub(r"\s+", " ", "".join(chars)).strip()
    # "_ _" 처럼 밑줄과 공백이 섞여 이어지는 구간은 하나로 줄인다
    cleaned = re.sub(r"[\s_]*_[\s_]*", "_", cleaned)
    cleaned = cleaned[:max_len]
    # 끝의 점·공백은 OS가 싫어한다
    cleaned = cleaned.rstrip(" ._")
    return cleaned or "untitled"


# --- 부기 파일 자리 -------------------------------------------------------

# 사용자가 고른 저장 폴더에는 채널 폴더와 자막 .txt만 둔다. 처리 기록·목록 캐시
# 같은 앱 사정은 여기 모은다 — 저장 폴더를 열었을 때 자막만 보여야 한다.
STATE_ROOT = os.path.join(BASE_DIR, "output", "_state")

# 예전에는 채널 폴더 안에 함께 두던 것들. index.xlsx는 이제 아예 만들지 않고
# 다운로드할 때 그 자리에서 만든다.
STATE_FILES = ("processed_ids.json", "failed_videos.json",
               "videos.json", "shorts.json", "streams.json", "index.xlsx")


def state_dir(out_dir):
    """채널 폴더의 부기 파일이 들어갈 자리.

    경로가 아니라 폴더 이름으로 자리를 잡는다 — 채널을 카테고리 폴더 사이로
    옮겨도 기록이 따라와야 이미 받은 수백 개를 처음부터 다시 받지 않는다.
    채널 폴더를 이름으로 찾는 server.channel_dir()과 같은 기준이다.
    """
    folder = sanitize_filename(os.path.basename(os.path.normpath(out_dir)))
    return os.path.join(STATE_ROOT, folder)


def _is_state_file(name):
    return name in STATE_FILES or (name.startswith("playlist_")
                                   and name.endswith(".json"))


def migrate_state(out_dir):
    """채널 폴더에 남아 있는 옛 부기 파일을 state 폴더로 옮긴다.

    지우지 않고 옮긴다 — 이어받기 기록이라 잃으면 수백 개를 다시 받는다.
    양쪽에 다 있으면 최근에 손댄 쪽을 남긴다.
    """
    if not os.path.isdir(out_dir):
        return
    try:
        names = [n for n in os.listdir(out_dir) if _is_state_file(n)]
    except OSError:
        return
    if not names:
        return

    target = state_dir(out_dir)
    os.makedirs(target, exist_ok=True)
    for name in names:
        source = os.path.join(out_dir, name)
        dest = os.path.join(target, name)
        try:
            if os.path.exists(dest) and (os.path.getmtime(dest)
                                         >= os.path.getmtime(source)):
                os.remove(source)     # state 쪽이 더 새것 — 남은 자국만 치운다
            else:
                # 저장 폴더가 다른 볼륨일 수 있어 rename 대신 move를 쓴다
                shutil.move(source, dest)
        except OSError:
            continue


def unique_path(dir_path, stem, ext=".txt"):
    """이름이 겹치면 _1, _2 를 붙여 비어 있는 경로를 돌려준다."""
    candidate = os.path.join(dir_path, stem + ext)
    if not os.path.exists(candidate):
        return candidate
    i = 1
    while True:
        candidate = os.path.join(dir_path, "{}_{}{}".format(stem, i, ext))
        if not os.path.exists(candidate):
            return candidate
        i += 1


def save_transcript(out_dir, upload_date, title, text):
    """자막 텍스트를 {채널폴더}/{업로드일}_{영상제목}.txt 로 저장한다.

    out_dir이 곧 채널 폴더다 — 하위에 채널명 폴더를 또 만들지 않는다.
    처리 기록·index.xlsx도 같은 폴더에 있어 채널 하나가 자기 완결적으로 담긴다.
    """
    os.makedirs(out_dir, exist_ok=True)
    stem = sanitize_filename("{}_{}".format(upload_date, title))
    path = unique_path(out_dir, stem)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
        if not text.endswith("\n"):
            f.write("\n")
    return path


# --- 상태 파일 (증분 저장 / 재개) ---------------------------------------

def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)  # 쓰는 도중 끊겨도 기존 파일이 깨지지 않게


def write_index(records, path):
    """처리 기록 전체에서 index.xlsx를 새로 써낸다 (append 아님)."""
    rows = [{
        "영상 제목": r.get("title", ""),
        "업로드일": r.get("upload_date", ""),
        "파일경로": r.get("path", ""),
        "원본 URL": r.get("url", ""),
        "상태": r.get("status", ""),
        "글자 수": r.get("char_count", 0),
    } for r in records]
    df = pd.DataFrame(rows, columns=INDEX_COLUMNS)
    df.to_excel(path, index=False)
    return path

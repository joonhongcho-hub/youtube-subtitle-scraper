"""파일 저장 + 파일명 정제 + index.xlsx."""

import json
import os
import re
import unicodedata

import pandas as pd

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

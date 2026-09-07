"""수집한 자막 전체를 대상으로 하는 키워드 검색.

SQLite에 내장된 FTS5를 쓴다. 새 의존성이 없고, 자막 1,651개(30MB) 기준
색인이 2초대에 끝나며 검색은 1ms 미만이다.

한국어라 tokenize='trigram'을 쓴다. 형태소 분석기 없이도 부분 일치가 되지만
**세 글자 미만은 색인되지 않는다** — '수익화'는 찾아도 '수익'·'창업'은 한 건도
나오지 않는다. 그래서 두 글자 단어는 LIKE로 따로 본다(build_query 참고).
"""

import json
import os
import re
import sqlite3
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 색인은 저장 폴더가 아니라 앱 폴더에 둔다. 저장 위치는 바뀔 수 있고,
# 그때마다 색인이 갈라지면 예전 자막이 검색에서 사라진다.
INDEX_DIR = os.path.join(BASE_DIR, "output", "_index")
INDEX_PATH = os.path.join(INDEX_DIR, "search.db")

# trigram이 색인하는 최소 글자 수
MIN_FTS_TERM = 3
# 색인 형식이 바뀌면 올린다. 옛 색인을 그대로 쓰면 없는 컬럼을 조회해
# 검색이 통째로 죽으므로, 버전이 다르면 말없이 다시 만든다 (전체 2초대).
SCHEMA_VERSION = 2
# 검색 의도가 아니라 '요청하는 말'. 남겨두면 어디에나 있어서 순위를 망친다.
STOPWORDS = {
    "찾아줘", "찾아", "알려줘", "알려", "보여줘", "보여", "추천해줘", "추천",
    "싶은데", "싶어", "싶다", "도움이", "도움", "될만한", "관련", "관련된",
    "내용", "자막", "자막에서", "유튜브", "영상", "영상에서", "중에서",
    "지금", "그리고", "하는", "하고", "대해", "대한", "무엇", "어떤", "어떻게",
}


def _create_tables(con):
    con.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
            path UNINDEXED, root, channel, title, body,
            tokenize='trigram'
        )""")
    # 증분 색인 판단용 — 파일이 바뀌었는지만 알면 된다
    con.execute("""
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            root TEXT, mtime REAL, size INTEGER,
            channel TEXT, title TEXT, upload_date TEXT, url TEXT,
            char_count INTEGER
        )""")


def connect():
    os.makedirs(INDEX_DIR, exist_ok=True)
    con = sqlite3.connect(INDEX_PATH)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")

    row = con.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
    version = int(row["value"]) if row else 0
    if version != SCHEMA_VERSION:
        # 형식이 다르면 통째로 버린다. 남겨두면 없는 컬럼을 조회해 크래시가 난다.
        con.execute("DROP TABLE IF EXISTS docs")
        con.execute("DROP TABLE IF EXISTS files")
        _create_tables(con)
        con.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)",
                    (str(SCHEMA_VERSION),))
        con.commit()
    else:
        _create_tables(con)
    return con


# --- 색인 ---

def _channel_meta(channel_dir):
    """채널 폴더의 처리 기록에서 파일별 원본 메타를 읽는다.

    파일명은 정제 과정에서 특수문자가 치환되고 100자로 잘리지만,
    processed_ids.json에는 원본 제목과 URL이 그대로 남아 있다.
    """
    path = os.path.join(channel_dir, "processed_ids.json")
    by_file = {}
    try:
        with open(path, encoding="utf-8") as f:
            records = json.load(f)
    except (OSError, ValueError):
        return by_file
    for record in records.values():
        rel = record.get("path")
        if rel:
            by_file[os.path.basename(rel)] = record
    return by_file


def _fallback_meta(filename):
    """기록에 없는 파일은 파일명에서 최소 정보만 뽑는다."""
    stem = filename[:-4] if filename.endswith(".txt") else filename
    if len(stem) > 9 and stem[:8].isdigit() and stem[8] == "_":
        return {"upload_date": stem[:8], "title": stem[9:]}
    return {"upload_date": "", "title": stem}


def iter_transcripts(roots):
    """저장 폴더들 아래의 자막 파일과 메타를 훑는다.

    카테고리 폴더를 몇 겹으로 두든 상관없도록 깊이를 정해두지 않고,
    .txt를 담고 있는 폴더를 채널로 본다. 예전에는 루트/채널/*.txt 두 단계만
    훑어서, 채널을 카테고리 폴더로 옮기면 그 자막이 통째로 검색에서 사라졌다.
    """
    for root in roots:
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # 내부용 폴더(_index, _jobs, _보관)와 숨김 폴더는 아예 들어가지 않는다
            dirnames[:] = sorted(d for d in dirnames
                                 if not d.startswith("_") and not d.startswith("."))
            if dirpath == root:
                continue
            names = sorted(f for f in filenames if f.endswith(".txt"))
            if not names:
                continue
            channel = os.path.basename(dirpath)
            meta = _channel_meta(dirpath)
            for filename in names:
                record = meta.get(filename) or _fallback_meta(filename)
                yield os.path.join(dirpath, filename), root, channel, record


def build_index(roots, force=False, log=None):
    """바뀐 자막만 다시 색인한다. force면 전부 새로 만든다."""
    log = log or (lambda message: None)
    started = time.time()
    con = connect()

    if force:
        con.execute("DELETE FROM docs")
        con.execute("DELETE FROM files")

    known = {row["path"]: (row["mtime"], row["size"])
             for row in con.execute("SELECT path, mtime, size FROM files")}

    seen, added, updated = set(), 0, 0
    for path, root, channel, record in iter_transcripts(roots):
        seen.add(path)
        stat = os.stat(path)
        if known.get(path) == (stat.st_mtime, stat.st_size):
            continue

        with open(path, encoding="utf-8", errors="replace") as f:
            body = f.read()
        title = record.get("title") or _fallback_meta(os.path.basename(path))["title"]

        if path in known:
            con.execute("DELETE FROM docs WHERE path = ?", (path,))
            updated += 1
        else:
            added += 1
        con.execute(
            "INSERT INTO docs (path, root, channel, title, body) VALUES (?,?,?,?,?)",
            (path, root, channel, title, body))
        con.execute("""INSERT OR REPLACE INTO files
                       (path, root, mtime, size, channel, title, upload_date, url, char_count)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (path, root, stat.st_mtime, stat.st_size, channel, title,
                     record.get("upload_date", ""), record.get("url", ""), len(body)))

    # 지워진 자막은 색인에서도 뺀다 — 남으면 없는 파일을 결과로 준다
    removed = 0
    for path in list(known):
        if path not in seen:
            con.execute("DELETE FROM docs WHERE path = ?", (path,))
            con.execute("DELETE FROM files WHERE path = ?", (path,))
            removed += 1

    con.commit()
    total = con.execute("SELECT count(*) FROM files").fetchone()[0]
    con.close()

    elapsed = time.time() - started
    log("색인 갱신 — 추가 {} · 변경 {} · 삭제 {} · 전체 {}개 ({:.1f}초)".format(
        added, updated, removed, total, elapsed))
    return {"added": added, "updated": updated, "removed": removed,
            "total": total, "seconds": round(elapsed, 2)}


def index_status(root=None):
    """색인 현황. root를 주면 그 폴더 안의 채널만 센다."""
    con = connect()
    if root:
        root = os.path.abspath(root)
        total = con.execute("SELECT count(*) FROM files WHERE root = ?",
                            (root,)).fetchone()[0]
        rows = con.execute("SELECT channel, count(*) n FROM files WHERE root = ? "
                           "GROUP BY channel ORDER BY n DESC", (root,))
    else:
        total = con.execute("SELECT count(*) FROM files").fetchone()[0]
        rows = con.execute("SELECT channel, count(*) n FROM files "
                           "GROUP BY channel ORDER BY n DESC")
    channels = [r["channel"] for r in rows]
    roots = [r["root"] for r in con.execute(
        "SELECT root, count(*) n FROM files GROUP BY root ORDER BY n DESC")]
    con.close()
    return {"total": total, "channels": channels, "roots": roots,
            "exists": os.path.exists(INDEX_PATH)}


# --- 질의 ---

def build_query(text):
    """사용자가 친 글을 안전한 검색 조건으로 바꾼다.

    원문을 그대로 MATCH에 넘기면 안 된다. 실제로 확인한 두 가지 때문이다.
      - 'AI (인공지능)', 'C++ 강의' 같은 입력이 FTS5 문법과 충돌해 오류를 낸다
      - FTS5 기본이 AND라 문장을 넣으면 모든 단어를 가진 문서만 남아 0건이 된다

    그래서 글자·숫자만 남겨 단어로 쪼개고, 요청하는 말을 걸러낸 뒤
    세 글자 이상은 OR로 묶는다. 두 글자는 trigram이 색인하지 않으므로
    따로 돌려주어 LIKE로 가산점을 준다(필수 조건으로 걸면 오히려 0건이 된다).
    """
    words = [w for w in re.split(r"[^0-9A-Za-z가-힣]+", text or "") if w]
    kept = [w for w in words if w not in STOPWORDS and len(w) >= 2]
    if not kept:                      # 전부 걸러졌으면 원래 단어라도 쓴다
        kept = [w for w in words if len(w) >= 2]

    long_terms = [w for w in kept if len(w) >= MIN_FTS_TERM]
    short_terms = [w for w in kept if len(w) < MIN_FTS_TERM]
    return long_terms, short_terms, words


def _rows_to_results(rows, short_terms):
    results = []
    for row in rows:
        body_hits = sum(1 for t in short_terms if t in (row["body_sample"] or ""))
        results.append({
            "path": row["path"],
            "channel": row["channel"],
            "title": row["title"],
            "snippet": row["snippet"],
            "score": row["score"],
            "short_hits": body_hits,
        })
    return results


def search(query, channel=None, root=None, limit=30):
    """자막을 검색한다. 결과가 비면 조건을 단계적으로 풀고 무엇을 풀었는지 알린다."""
    long_terms, short_terms, words = build_query(query)
    if not words:
        return {"results": [], "terms": [], "note": "검색어를 입력하세요.", "relaxed": False}

    con = connect()
    note, relaxed = "", False

    def run(terms, shorts, use_and=False):
        params, where = [], []
        if terms:
            joiner = " AND " if use_and else " OR "
            where.append("docs MATCH ?")
            params.append(joiner.join('"{}"'.format(t) for t in terms))
        for term in shorts:
            where.append("docs.body LIKE ?")
            params.append("%{}%".format(term))
        if not where:
            return []
        if channel:
            where.append("docs.channel = ?")
            params.append(channel)
        if root:
            # LIKE로 접두어를 맞추면 안 된다 — 밑줄이 와일드카드라
            # my_folder 로 거르면 myXfolder 까지 걸린다. 그래서 컬럼 비교를 쓴다.
            where.append("docs.root = ?")
            params.append(os.path.abspath(root))
        order = "bm25(docs)" if terms else "docs.rowid"
        sql = ("SELECT path, root, channel, title, {} AS score, "
               "snippet(docs, 3, '«', '»', '…', 14) AS snippet, "
               "substr(body, 1, 4000) AS body_sample "
               "FROM docs WHERE {} ORDER BY score LIMIT ?"
               .format(order, " AND ".join(where)))
        return con.execute(sql, params + [limit]).fetchall()

    # 1) 긴 단어는 OR, 두 글자는 가산점용으로만 따로 센다
    rows = run(long_terms, [])
    # 2) 긴 단어가 없으면 두 글자 단어로 LIKE 검색
    if not rows and short_terms:
        rows = run([], short_terms[:2])
        if rows:
            note = "세 글자 이상 단어가 없어 '{}'(으)로 찾았습니다.".format(
                " ".join(short_terms[:2]))
            relaxed = True
    # 3) 그래도 없으면 불용어까지 되살려 다시 시도
    if not rows:
        fallback = [w for w in words if len(w) >= MIN_FTS_TERM]
        if fallback:
            rows = run(fallback, [])
            if rows:
                note = "일치하는 결과가 없어 조건을 넓혔습니다."
                relaxed = True

    results = _rows_to_results(rows, short_terms)
    con.close()

    # 두 글자 핵심어가 본문에 있으면 위로 올린다 (필터가 아니라 가산점)
    if short_terms:
        results.sort(key=lambda r: (-r["short_hits"], r["score"]))

    if not results:
        note = "결과가 없습니다. 핵심 단어만 남겨 다시 검색해 보세요."
    elif len(words) > 6 and not note:
        note = "문장보다 핵심 단어만 넣으면 더 정확합니다 (예: 창업 수익화)."

    return {"results": results, "terms": long_terms + short_terms,
            "note": note, "relaxed": relaxed}


def file_meta(paths):
    """내보내기에 쓸 제목·URL·업로드일을 경로로 조회한다."""
    if not paths:
        return {}
    con = connect()
    marks = ",".join("?" * len(paths))
    rows = con.execute(
        "SELECT path, channel, title, upload_date, url, char_count "
        "FROM files WHERE path IN ({})".format(marks), list(paths)).fetchall()
    con.close()
    return {r["path"]: dict(r) for r in rows}

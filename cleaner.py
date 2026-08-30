"""자막 정제 — VTT/원본 자막 데이터를 순수 텍스트로 가공."""

import html
import re

# 00:00:00.000 --> 00:00:02.000 align:start position:0%
TIMESTAMP_LINE = re.compile(r"^\d{1,3}:\d{2}(:\d{2})?[.,]\d{3}\s*-->")
# 큐 시작 시각 — [hh:]mm:ss.mmm
CUE_START = re.compile(r"^(?:(\d{1,3}):)?(\d{1,2}):(\d{2})[.,](\d{3})\s*-->")
# WEBVTT, Kind:, Language: 등 메타데이터 헤더
META_LINE = re.compile(r"^(WEBVTT|Kind\s*:|Language\s*:|X-TIMESTAMP)", re.I)
# NOTE / STYLE / REGION 은 빈 줄까지 이어지는 블록
BLOCK_START = re.compile(r"^(NOTE|STYLE|REGION)\b", re.I)
# <c>, </c>, <00:00:00.000>, <c.colorE5E5E5> 등 인라인 태그
INLINE_TAG = re.compile(r"<[^>]*>")
# 겹치는 판정을 할 때 이보다 짧은 줄은 부분 문자열 규칙을 적용하지 않는다
MIN_CONTAINMENT_LEN = 8
# 중복 병합 시 거슬러 올라가 비교할 최대 줄 수
LOOKBACK = 4
# 문단 하나의 목표 길이 (글자)
MAX_PARAGRAPH_CHARS = 400
# 문장 끝 — 뒤에 공백이 오는 마침표/물음표/느낌표
SENTENCE_END = re.compile(r"(?<=[.!?。？！])\s+")
# 자막의 화자 표시
SPEAKER = re.compile(r"\s*>>\s*")


def _normalize(text):
    """인라인 태그·HTML 엔티티 제거 후 공백 정리."""
    text = INLINE_TAG.sub("", text)
    text = html.unescape(text)
    text = text.replace("​", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _parse_cue_start(line):
    """타임스탬프 줄에서 시작 시각(초)을 뽑는다."""
    m = CUE_START.match(line)
    if not m:
        return None
    hours, minutes, seconds, millis = m.groups()
    return (int(hours or 0) * 3600 + int(minutes) * 60
            + int(seconds) + int(millis) / 1000.0)


def clean_vtt_cues(raw):
    """.vtt 원문에서 (시작시각, 텍스트) 쌍을 뽑아낸다 (중복 병합 전)."""
    lines = raw.splitlines()
    out = []
    in_block = False
    current_start = None

    for i, line in enumerate(lines):
        stripped = line.strip()

        if in_block:
            # NOTE/STYLE/REGION 블록은 빈 줄이 나올 때까지 통째로 버린다
            if not stripped:
                in_block = False
            continue

        if not stripped:
            continue
        if BLOCK_START.match(stripped):
            in_block = True
            continue
        if META_LINE.match(stripped):
            continue
        if TIMESTAMP_LINE.match(stripped):
            # 타임스탬프 줄은 버리되 시작 시각은 이후 텍스트 줄에 물려준다
            current_start = _parse_cue_start(stripped)
            continue
        # 타임스탬프 바로 앞의 큐 번호 줄
        if stripped.isdigit() and i + 1 < len(lines) \
                and TIMESTAMP_LINE.match(lines[i + 1].strip()):
            continue

        text = _normalize(stripped)
        if text:
            out.append((current_start, text))

    return out


def clean_vtt(raw):
    """.vtt 원문에서 자막 텍스트 줄만 뽑아낸다 (중복 병합 전)."""
    return [text for _, text in clean_vtt_cues(raw)]


def dedupe_cues(cues):
    """자동생성 자막의 중복줄 병합.

    자동자막은 한 단어씩 누적되며 같은 문장이 여러 줄 반복된다.
    새 줄이 직전 줄들이 합쳐진 형태인 경우가 많으므로, 바로 앞 한 줄만이 아니라
    최근 LOOKBACK줄까지 이어붙여 비교하고 겹치면 더 긴 쪽 하나로 합친다.

    합칠 때 시작 시각은 합쳐지는 무리의 첫 조각 것을 남긴다 — 그 시점이
    해당 발화가 실제로 시작된 때이기 때문이다.
    """
    out = []
    for start, line in cues:
        merged = False
        for k in range(min(len(out), LOOKBACK), 0, -1):
            tail = " ".join(text for _, text in out[-k:])
            # 직전 줄(들)이 현재 줄로 자라난 경우 → 현재 줄 하나로 합친다
            if line.startswith(tail) or (
                    len(tail) >= MIN_CONTAINMENT_LEN and tail in line):
                first_start = out[-k][0]
                out[-k:] = [(first_start, line)]
                merged = True
                break
            # 현재 줄이 이미 직전 줄(들)에 담겨 있는 경우 → 버린다
            if tail.startswith(line) or (
                    len(line) >= MIN_CONTAINMENT_LEN and line in tail):
                merged = True
                break
        if not merged:
            out.append((start, line))
    return out


def dedupe_lines(lines):
    """텍스트 줄만 받아 중복을 병합한다."""
    return [text for _, text in dedupe_cues([(None, line) for line in lines])]


def _group(units, limit=MAX_PARAGRAPH_CHARS):
    """조각들을 limit 글자를 넘지 않는 선에서 하나로 이어붙인다."""
    out = []
    buf = ""
    for unit in units:
        unit = unit.strip()
        if not unit:
            continue
        candidate = (buf + " " + unit).strip()
        if buf and len(candidate) > limit:
            out.append(buf)
            buf = unit
        else:
            buf = candidate
    if buf:
        out.append(buf)
    return out


def _split_block(block):
    """한 화자의 발화를 문단들로 나눈다."""
    sentences = SENTENCE_END.split(block)
    if len(sentences) > 1:
        return _group(sentences)
    # 문장부호가 전혀 없는 자동생성 자막 — 어절 단위로 끊는다
    return _group(block.split(" "))


def to_paragraphs(lines):
    """정제된 자막 줄들을 읽기 좋은 문단으로 이어붙인다.

    자막 조각은 문장 중간에서 끊기므로 그대로 두면 읽기 어렵다.
    공백으로 이어붙인 뒤 화자 표시(>>)와 문장 끝을 기준으로 다시 나눈다.
    """
    text = re.sub(r"\s+", " ", " ".join(lines)).strip()
    if not text:
        return ""

    # 화자가 바뀌는 지점은 문단 경계로 본다
    text = SPEAKER.sub("\n>> ", text).strip()

    paragraphs = []
    for block in text.split("\n"):
        paragraphs.extend(_split_block(block))
    return "\n\n".join(paragraphs)


def format_timestamp(seconds):
    """초를 [mm:ss] 또는 [hh:mm:ss] 형태로 만든다."""
    if seconds is None:
        return "[--:--]"
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "[{:02d}:{:02d}:{:02d}]".format(hours, minutes, secs)
    return "[{:02d}:{:02d}]".format(minutes, secs)


def group_cues(cues, limit=MAX_PARAGRAPH_CHARS):
    """(시작시각, 텍스트) 쌍을 문단으로 묶는다. 문단 시각은 첫 조각의 것."""
    out = []
    start = None
    buf = ""
    for cue_start, text in cues:
        text = text.strip()
        if not text:
            continue
        # 화자가 바뀌면 문단을 나눈다
        new_speaker = text.startswith(">>")
        candidate = (buf + " " + text).strip()
        if buf and (new_speaker or len(candidate) > limit):
            out.append((start, buf))
            start, buf = cue_start, text
        else:
            if not buf:
                start = cue_start
            buf = candidate
    if buf:
        out.append((start, buf))
    return out


def render_cues(cues, timestamps=False):
    """정제된 조각들을 최종 텍스트로 만든다.

    타임스탬프를 끄면 기존과 똑같이 전체를 이어붙인 뒤 화자·문장 단위로 다시 나눈다.
    켜면 시각을 잃지 않도록 조각 순서를 유지한 채 문단으로만 묶는다.
    """
    if not timestamps:
        return to_paragraphs([text for _, text in cues])
    return "\n\n".join(
        "{} {}".format(format_timestamp(start), text)
        for start, text in group_cues(cues))


def clean_vtt_text(raw, timestamps=False):
    """.vtt 원문 → 정제된 텍스트."""
    return render_cues(dedupe_cues(clean_vtt_cues(raw)), timestamps=timestamps)


def clean_snippets_text(snippets, timestamps=False):
    """youtube-transcript-api 조각 목록 → 정제된 텍스트."""
    cues = []
    for snippet in snippets:
        text = _normalize(getattr(snippet, "text", ""))
        if not text:
            continue
        start = getattr(snippet, "start", None)
        for part in text.split("\n"):
            if part:
                cues.append((start, part))
    return render_cues(dedupe_cues(cues), timestamps=timestamps)

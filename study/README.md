# study — 자막 학습 파이프라인의 맥 쪽

유튜브와 통신하는 일만 여기서 한다. 자막을 파일로 떨구고, 무엇을 받았는지 JSON으로
남기는 것까지가 전부다. 자막을 읽고 판단하는 일(요약·분류·세그먼트·유닛 생성)은
코워크가 하고, 보여주는 일은 발행 페이지가 한다. 이 폴더의 코드는 **자막 내용을
들여다보지 않는다.**

## 바깥과 만나는 두 파일

방향이 서로 반대다. 형식이 어긋나면 조용히 깨지므로 형식을 지키는 것이 중요하다.

| 파일 | 방향 | 누가 쓰나 |
|---|---|---|
| `config/channels.json` | 페이지 → 맥 | **코워크가 쓴다.** 맥 쪽 코드는 읽기만 |
| `data/status.json` | 맥 → 페이지 | **맥이 쓴다.** 코워크가 읽어 페이지에 올린다 |

`channels.json`이 형식에 어긋나면 `collect.py`는 무엇이 잘못됐는지 찍고 **수집을
시작하지 않는다.** 기본값으로 얼버무리고 몇 시간을 돌리는 것보다 낫기 때문이다.
`role`은 `news` 또는 `material`, `state`는 `confirmed` / `pending` / `failed`,
`url`은 반드시 링크여야 한다. 채널명만 적으면 `resolve_channel`이 사람에게 번호를
물어보는데 예약 실행에는 답할 사람이 없다.

수집 이력은 `channels.json`에 두지 않는다. `data/state.json`(맥이 아는 사실)과
`data/status.json`(화면에 보여줄 값)만 갖는다.

**`status.json`은 맥이 쓰고 코워크는 읽기만 한다.** 코워크가 덧붙일 정보(다음 수집
예정 등)는 페이지에 올릴 때 붙이고 이 파일은 건드리지 않는다. 맥은 스케줄을 모르므로
그런 값을 추측해 채우지 않는다.

세 파일(`channels.json` `status.json` `data/collected/{주차}.json`)은 맨 위에
`"schema": 1`을 갖는다. `collect.py`는 읽을 때 이 번호부터 확인하고, 모르는 번호면
수집을 시작하지 않는다.

`status.json`의 채널 한 줄은 이렇게 읽는다.

| 필드 | 뜻 |
|---|---|
| `last_content_at` / `last_count` | **마지막으로 1편 이상 받은 날과 그때 편수.** 둘은 늘 같은 실행을 가리킨다 |
| `this_run` | 이번 실행에서 받은 편수 |
| `total_files` | 폴더의 자막 수. **폴더가 없으면 `null`** — 0(폴더는 있는데 빔)과 다른 상태다 |
| `flags` | `not_targeted`(이번 실행 대상 아님) · `orphan_files`(기록은 없는데 자막이 있음) |

`flags`에는 코드 문자열만 들어간다. 화면에 뜰 문구는 페이지가 만든다.

`data/collected/{주차}.json`은 그 주의 실행을 `runs` 배열에 **덧붙인다.** 같은 주에
두 번 돌아도 먼저 받은 실행이 지워지지 않는다. `status.json`의 `week_total`이 이
배열을 합한 값이다.

기록은 있는데 파일이 없는 상태, 파일은 있는데 기록이 없는 상태를 **자동으로 고치지
않는다.** `missing_files`와 `flags`에 드러나게만 두고 무엇을 할지는 사람이 정한다.

## 실행

```bash
source venv/bin/activate

# 일요일 수집 — 뉴스 채널의 신규분만
python study/collect.py --dry-run           # 무엇을 받을지만 본다
python study/collect.py                     # 실제 수집
python study/collect.py --only Fireship --limit 3

# 원본 설정을 건드리지 않고 시험할 때 — 설정 폴더만 바꾼다
python study/collect.py --config-dir /tmp/test-config --dry-run

# 유닛 재료 — 코워크가 고른 영상만
python study/fetch_videos.py --ids Abc123,Def456 --out "챕터1/유닛3"
python study/fetch_videos.py --ids-file ids.txt --out "챕터1/유닛3"
```

기준일은 요일이 아니라 채널별 **마지막 수집 성공일**(`data/state.json`)이다.
맥이 꺼져 2주를 건너뛰어도 다음 실행에서 2주치를 받는다. 21개월을 도는 시스템에서
비어 있는 주는 반드시 생긴다.

`fetch_videos.py`는 결과 JSON을 stdout으로, 진행 로그를 stderr로 내보낸다.
그대로 파이프에 물릴 수 있다.

## 설정

`config/settings.json`

```json
{ "output_root": "output/자막", "lang": "ko,en", "timestamps": true }
```

`output_root`가 상대경로면 저장소 루트 기준으로 푼다. 예약 작업은 실행 디렉터리를
보장하지 못하므로 어디서 실행해도 같은 곳에 쌓인다. 절대경로를 넣어도 그대로 동작한다.

**`timestamps`를 끄지 않는다.** 세그먼트를 시간 구간으로 자르는 구조라 타임스탬프
없는 자막은 쓸 수 없고, 껐다가 다시 켜려면 전부 다시 받아야 한다.

## 손대지 않는 것

기존 스크래퍼는 완성되어 잘 돌고 있다.

- `output/**` — 읽기만 한다. 이동·삭제·이름변경 금지
- `server.py` `jobs.py` `search.py` `storage.py` `cleaner.py` `report.py` — 수정 금지
- `channel.py` `subtitle.py` `engine.py` — 읽어서 재사용만. 시그니처 변경 금지
- `main.py` — 이 폴더의 작업으로는 건드리지 않는다

`data/`는 이 맥에서만 의미가 있어 저장소에 넣지 않는다(`.gitignore`).

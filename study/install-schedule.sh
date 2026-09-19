#!/bin/zsh
# plist 에 적힌 일정으로 자막 자동 수집을 맥에 등록한다.
# 일정 자체는 웹의 예약 탭이 정하고(schedule.json → plist), 이 스크립트는
# 등록만 한다. 터미널에서 직접 돌려도 된다.
# 쓰는 법:  bash "/Users/jhc/code/vibe coding/youtube-subtitle-scraper/study/install-schedule.sh"
set -e

REPO="/Users/jhc/code/vibe coding/youtube-subtitle-scraper"
LABEL="com.jhc.subtitle-weekly"
SRC="$REPO/study/$LABEL.plist"
DST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "▸ 확인 중…"
[ -f "$SRC" ]            || { echo "✗ plist 를 못 찾음: $SRC"; exit 1; }
[ -x "$REPO/venv/bin/python" ] || { echo "✗ venv 파이썬이 없음: $REPO/venv/bin/python"; exit 1; }
[ -f "$REPO/study/collect.py" ] || { echo "✗ collect.py 가 없음"; exit 1; }

mkdir -p "$HOME/Library/LaunchAgents"
cp "$SRC" "$DST"
echo "▸ 설치: $DST"

# 이미 등록돼 있으면 먼저 내린다 (다시 돌려도 안전하게)
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DST"
echo "▸ 등록 완료"

echo
if launchctl list | grep -q "$LABEL"; then
  echo "✓ launchd 에 올라갔다."
  launchctl list | grep "$LABEL" | sed 's/^/    /'
else
  echo "✗ 목록에 안 보인다. 아래를 직접 확인해줘:"
  echo "    launchctl list | grep $LABEL"
  exit 1
fi

echo
echo "일정: plist 의 StartCalendarInterval (맥이 자고 있으면 깨어난 직후)"
echo "로그     : $REPO/study/data/collect.log"
echo
echo "지금 바로 한 번 돌려보려면:"
echo "    launchctl kickstart -k gui/\$(id -u)/$LABEL"
echo "    tail -f \"$REPO/study/data/collect.log\""

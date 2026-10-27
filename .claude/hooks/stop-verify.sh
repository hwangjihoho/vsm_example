#!/usr/bin/env bash
# Stop hook: 코드 변경이 있을 때만 lint/test를 실행하고, 실패하면 exit 2로 Claude에게 수정을 요구한다.
# 프로젝트에 맞게 VERIFY 명령만 바꿔서 쓴다.
#
# 등록: .claude/settings.json → hooks.Stop
# 입력: {"stop_hook_active": bool, ...}  (stdin)
# 종료 코드: 0 = 종료 허용, 2 = 종료 차단 (stderr가 Claude에게 전달되어 수정을 이어간다)
# 의존: git (jq 불필요)
#
# 실행 조건
#   1) stop_hook_active == true  → 통과 (이미 한 번 차단된 후라면 무한 루프 방지)
#   2) phase 실행 중 + 메인 세션 → 통과 (phases/.running 있고 HARNESS_STEP 없음)
#   3) 코드 변경 없음            → 통과 (phases/와 PROPOSALS.md 변경만 있으면 검증 생략)
#   4) VERIFY 실패               → 출력 마지막 40줄과 함께 exit 2
if [ -f make.cmd ] && command -v cmd.exe >/dev/null 2>&1; then
  VERIFY="${VERIFY:-cmd.exe //C '.\make.cmd test'}"
else
  VERIFY="${VERIFY:-make test}"
fi

input=$(cat)
# 이미 Stop hook 때문에 계속 진행 중이면 무한 루프를 막기 위해 통과시킨다.
if printf '%s' "$input" | grep -Eq '"stop_hook_active"[[:space:]]*:[[:space:]]*true'; then
  exit 0
fi

# execute.py가 phase를 실행하는 중(phases/.running 있음)이면 메인 세션에서는 검증하지 않는다
# (step 세션이 작업 중인 코드에 테스트를 돌려 메인 턴을 막지 않도록).
# step 세션은 execute.py가 HARNESS_STEP을 설정하므로 그대로 검증해 자가 교정을 유지한다.
if [ -f phases/.running ] && [ -z "${HARNESS_STEP:-}" ]; then
  exit 0
fi

# 질의응답처럼 코드 변경이 없는 턴에서는 검증을 건너뛴다.
if [ -z "$(git status --porcelain -- . ':!phases' ':!docs/PROPOSALS.md' 2>/dev/null)" ]; then
  exit 0
fi

if ! out=$(bash -c "$VERIFY" 2>&1); then
  echo "검증 실패 — 범위 안에서 수정하라 (새 기능 추가 금지):" >&2
  echo "$out" | tail -40 >&2
  exit 2
fi
exit 0

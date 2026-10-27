---
description: 승인된 phase 계획을 execute.py로 실행한다. "계획 실행", "구현 시작", "계획대로 구현" 같은 요청에 사용한다. 메인 세션에서 직접 구현하지 않는다.
argument-hint: "[phase 디렉토리명, 예: 0-mvp]"
---

승인된 phase 계획을 `scripts/execute.py`로 실행하라. **메인 세션에서 코드를 직접 구현하지 않는다.**
대상 phase: $ARGUMENTS (비어 있으면 `phases/index.json`에서 completed가 아닌 첫 phase)

## 1. 사전 점검

하나라도 통과하지 못하면 실행하지 말고, 결과와 조치 방법을 보고한 뒤 멈춘다.

| 점검 | 통과 조건 | 실패 시 |
|---|---|---|
| phase 파일 | `phases/<phase>/index.json`과 pending step의 `step{N}.md`가 있음 | /harness로 계획부터 |
| error / blocked step | 없음 | 3-2 절차로 안내 |
| 실행 중 표시 | `phases/.running` 없음 | 다른 실행 중인지 확인. 실행 중이 아니면 파일 삭제를 **제안** |
| 작업 트리 | `git status --short`가 비어 있음 | phase 파일만 남았으면 `git add -- phases && git commit -m "chore: plan <phase>"`를 제안. 그 외 변경은 보고 |
| 독립 환경 | 프로젝트에 `claude-isolated.cmd`(또는 `.sh`)가 있으면 `CLAUDE_CONFIG_DIR`이 설정돼 있음 | 이 세션을 끝내고 런처로 새 세션을 열라고 안내 |

## 2. 실행

1. 실행 계획을 표로 보여준다: phase, 실행할 step(번호, 이름, 요구사항 ID), 브랜치 `feat-<phase>`.
2. 명령을 **백그라운드로** 실행한다. Windows는 `py`, macOS·Linux는 `python3`.
   ```
   py scripts/execute.py <phase>
   ```
   이 명령은 프로젝트 설정의 ask 규칙 때문에 승인 창이 뜬다. 사용자가 거절하면 실행하지 않고 멈춘다.
3. 실행 중에는 **읽기만 한다.** 코드, 문서, phase 파일을 수정하지 않는다 (step 세션과 충돌).
   진행 상황은 `phases/<phase>/index.json`의 step status로 확인하고, 사용자가 물으면 표로 보여준다.

## 3. 결과 보고

### 3-1. completed
- step별 `summary` 표
- `py scripts/harness_map.py` 실행 후 Health error/warn 요약
- 다음 단계로 `/review` 제안

### 3-2. error 또는 blocked
- 해당 step의 `error_message` 또는 `blocked_reason`, 관련 `phases/<phase>/step{N}-output.json` 요약
- **직접 코드를 고치지 않는다.** step 파일 수정안(작업 지시, AC, 변경 허용 범위)을 제시하고 승인을 받는다
- 승인 후: step 파일 수정 → 해당 step의 status를 `"pending"`으로, `error_message`/`blocked_reason` 삭제 → 커밋 → 이 절차를 처음부터 다시 실행
- blocked가 API 키·인증·수동 설정 때문이면 사용자가 할 일을 정리해 주고 기다린다

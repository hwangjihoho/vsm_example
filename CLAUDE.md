# 프로젝트: {프로젝트명}

> 공통 응답 스타일과 MVP-First Guard 원칙은 글로벌 CLAUDE.md를 따른다.
> 이 파일에는 프로젝트 고유 규칙과, 팀원도 지켜야 하는 Scope Lock만 둔다.

## 기술 스택
- {언어/프레임워크 (예: Python 3.12 + pytest + Playwright)}

## 아키텍처 규칙
- CRITICAL: {절대 규칙 1}
- {일반 규칙}

## Scope Lock (MVP-First Guard 적용)
- CRITICAL: 구현 범위는 `docs/PRD.md`의 "MVP 범위"와 현재 step 파일의 "변경 허용 범위"로 한정한다.
- CRITICAL: 리뷰와 검증은 승인된 AC만 기준으로 한다. 결함 수정은 기존 범위 안에서만 한다.
- CRITICAL: 범위 밖 아이디어는 코드로 구현하지 말고 `docs/PROPOSALS.md`에 한 줄씩 추가한다. 승인 표시(`[x]`)는 Human만 한다.
- 범위 검증: `python3 scripts/scope_check.py phases/{task}/step{N}.md`

## Phase 실행
- CRITICAL: 승인된 phase 계획의 구현은 `/run-phase` 절차(execute.py)로만 한다. "계획 실행", "구현 시작", "계획대로 구현해" 같은 요청도 메인 세션에서 직접 코드를 고치지 말고 `/run-phase`를 따른다.
- CRITICAL: execute.py 실행 중(`phases/.running` 있음)에는 메인 세션에서 코드와 문서를 수정하지 않는다. 진행 확인은 `phases/<phase>/index.json`으로 한다.

## 개발 프로세스
- 테스트를 먼저 쓰고, 테스트를 통과시키는 구현을 쓴다 (TDD).
- 커밋 메시지는 conventional commits 형식을 따른다.

## 명령어
```bash
{test 명령}   # 예: pytest -q
{lint 명령}   # 예: ruff check .
```

## 하네스 현황
```bash
python3 scripts/harness_map.py            # harness_map.html 생성 (execute.py가 자동 갱신)
python3 scripts/harness_map.py --strict   # Health error 시 exit 1 (CI용)
```

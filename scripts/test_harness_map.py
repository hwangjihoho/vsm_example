"""
harness_map.py 테스트 — 수집, Health 점검, 렌더링, execute.py 연동, docs 연결을 검증한다.

fixture 구성
  project: 결함을 일부러 심은 하네스 프로젝트
           (잘못된 PreToolUse hook, loop 위험 Stop hook, PRD에 없는 R9, 파일 없는 step2,
            범위/AC가 없는 step1, step이 없는 요구사항 R2, HTML 태그가 든 error_message)
  linked:  project + ADR.md(Accepted/Superseded/Proposed), ARCHITECTURE.md 트리,
           참조 문서·근거 ADR·ARCHITECTURE 밖 범위를 가진 step0

Health 테스트는 메시지 부분 문자열로 판정한다.
메시지 문구를 바꾸면 해당 테스트 오라클도 함께 바꿔야 한다.

테스트 주석 규약 (각 테스트 docstring)
  대상: 검증하는 함수/메서드와 조건
  구분: 정상 | 경계 | 예외 | 탐지 | 보안 | 연동
        - 정상: 설계된 기본 동작
        - 경계: 빈 값, 누락 파일, 한계값 등 입력 경계
        - 예외: 실패 경로 (예외, 비정상 종료)
        - 탐지: 결함을 심은 입력을 Health 규칙이 잡아내는지
        - 보안: 이스케이프, 주입 방지
        - 연동: 모듈 간 호출 (execute.py ↔ harness_map.py)
  오라클: 통과 판정 기준. 괄호 안은 오라클 종류
        - (명세)     문서화된 규칙/형식과 결과가 일치
        - (상태)     실행 후 파일·JSON 상태
        - (비교)     원본/이전 값/시간 범위와 비교
        - (계산)     입력에서 손으로 계산한 기대값
        - (호출)     mock에 전달된 인자나 호출 횟수
        - (종료코드) SystemExit 코드 또는 main() 반환값
        - (출력)     stdout/stderr 메시지
        - (예외)     특정 예외 발생
        - (예외부재) 예외 없이 끝나는 것만 확인 — 약한 오라클
  비고: ⚠ 표시는 오라클이 약해 보강이 필요한 테스트
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import execute as ex
import harness_map as hm


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# 정상 step: 요구사항 ID, 변경 허용 범위, 실행 가능한 AC 코드 블록을 모두 갖춤.
STEP_OK = """# Step 0: core

## 요구사항 ID

R1

## 변경 허용 범위

- `src/core/*`
- `tests/test_core.py`

## Acceptance Criteria

```bash
pytest -q
```
"""

# 결함 step: PRD에 없는 R9, 변경 허용 범위 없음, AC에 코드 블록 없음.
STEP_BAD = """# Step 1: extra

## 요구사항 ID

R9

## Acceptance Criteria

설명만 있고 명령 없음
"""


@pytest.fixture
def project(tmp_path):
    """PRD, PROPOSALS, phases, hooks를 갖춘 임시 하네스 프로젝트 (결함을 일부러 심음).

    심어 둔 결함과 이를 잡는 테스트
      - PreToolUse hook: $CLAUDE_TOOL_INPUT 사용 + exit 1   → TestHealth.test_pretooluse_*
      - Stop hook(stop.sh): exit 2는 있으나 stop_hook_active 없음 → test_stop_hook_loop_risk
      - step1: R9(PRD에 없음), 범위/AC 없음                → test_unknown_requirement_id, test_missing_scope_and_ac
      - step2: index.json에만 있고 파일 없음               → test_missing_step_file
      - R2: 연결된 step 없음                              → test_uncovered_requirement
      - step1 error_message에 HTML 태그                   → TestRender.test_values_are_escaped
    """
    (tmp_path / "CLAUDE.md").write_text(
        "# 프로젝트: demo\n\n- CRITICAL: 범위 밖 수정 금지\n- 일반 규칙\n", encoding="utf-8"
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "PRD.md").write_text(
        "# PRD\n\n| ID | 요구사항 | 완료 기준 |\n|----|---|---|\n"
        "| R1 | 로그인 | `pytest tests/test_core.py` |\n"
        "| R2 | 로그아웃 | `pytest tests/test_out.py` |\n",
        encoding="utf-8",
    )
    (docs / "PROPOSALS.md").write_text(
        "# Proposals\n\n> - [ ] 규칙 예시는 무시된다\n\n"
        "- [ ] P1 | mvp/step0 | 스크린샷 첨부 | 디버깅\n"
        "- [x] P2 | mvp/step1 | 재시도 리포트 | 가시성\n",
        encoding="utf-8",
    )

    claude = tmp_path / ".claude"
    (claude / "commands").mkdir(parents=True)
    (claude / "commands" / "review.md").write_text("변경 사항을 리뷰하라.\n", encoding="utf-8")
    (claude / "settings.json").write_text(json.dumps({"hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
            "command": "echo \"$CLAUDE_TOOL_INPUT\" | grep -q rm && exit 1"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "bash .claude/hooks/stop.sh"}]}],
    }}), encoding="utf-8")
    (claude / "hooks").mkdir()
    (claude / "hooks" / "stop.sh").write_text("pytest || exit 2\n", encoding="utf-8")

    phase = tmp_path / "phases" / "mvp"
    phase.mkdir(parents=True)
    (tmp_path / "phases" / "index.json").write_text(
        json.dumps({"phases": [{"dir": "mvp", "status": "error"}]}), encoding="utf-8"
    )
    (phase / "index.json").write_text(json.dumps({"project": "demo", "phase": "mvp", "steps": [
        {"step": 0, "name": "core", "status": "completed", "summary": "core 모듈 생성"},
        {"step": 1, "name": "extra", "status": "error", "error_message": "<b>빌드 실패</b>"},
        {"step": 2, "name": "ghost", "status": "pending"},
    ]}), encoding="utf-8")
    (phase / "step0.md").write_text(STEP_OK, encoding="utf-8")
    (phase / "step1.md").write_text(STEP_BAD, encoding="utf-8")
    return tmp_path


def _messages(health, level=None):
    """Health 결과를 "대상 :: 메시지" 문자열 리스트로 바꾼다. level을 주면 해당 level만 남긴다."""
    return [f"{x['target']} :: {x['message']}" for x in health if level in (None, x["level"])]


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------

class TestCollect:
    """harness_map.collect() — 저장소 파일을 읽어 렌더링/점검용 model dict를 만드는 단계."""

    def test_project_name_from_claude_md(self, project):
        """
        대상: collect() — 프로젝트명
        구분: 정상
        오라클: (명세) CLAUDE.md의 "# 프로젝트: demo" 제목에서 "demo"를 추출한다.
        """
        assert hm.collect(project)["project"] == "demo"

    def test_requirements_parsed_from_prd_table(self, project):
        """
        대상: _collect_requirements() — PRD 표
        구분: 정상
        오라클: (명세) R-ID가 표 순서대로 [R1, R2]이고, AC 칸의 백틱이 제거된다.
        """
        reqs = hm.collect(project)["requirements"]
        assert [r["id"] for r in reqs] == ["R1", "R2"]
        assert reqs[0]["ac"] == "pytest tests/test_core.py"

    def test_step_sections_parsed(self, project):
        """
        대상: _collect_step() — 정상 step 파일
        구분: 정상
        오라클: (명세) 요구사항 ID [R1], 변경 허용 범위 2개, AC 코드 블록 있음.
        """
        s0 = hm.collect(project)["phases"][0]["steps"][0]
        assert s0["req_ids"] == ["R1"]
        assert s0["scope"] == ["src/core/*", "tests/test_core.py"]
        assert s0["has_ac"] is True

    def test_step_without_scope_and_code_ac(self, project):
        """
        대상: _collect_step() — 범위 없음, AC에 코드 블록 없음
        구분: 경계
        오라클: (명세) scope == [], has_ac is False (설명 문장만 있는 AC는 AC로 보지 않음).
        """
        s1 = hm.collect(project)["phases"][0]["steps"][1]
        assert s1["scope"] == []
        assert s1["has_ac"] is False

    def test_proposals_skip_blockquote(self, project):
        """
        대상: _collect_proposals()
        구분: 정상
        오라클: (명세) 인용문(>) 안의 예시는 무시하고 P1(대기), P2(승인)만 파싱한다.
        """
        props = hm.collect(project)["proposals"]
        assert [(p["id"], p["approved"]) for p in props] == [("P1", False), ("P2", True)]

    def test_critical_rules_and_commands(self, project):
        """
        대상: _collect_layer() — 프로젝트 레이어
        구분: 정상
        오라클: (명세) CRITICAL이 붙은 줄만 규칙으로 수집되고, commands/review.md는 "/review"로 수집된다.
        """
        layer = hm.collect(project)["layers"][0]
        assert layer["critical"] == ["CRITICAL: 범위 밖 수정 금지"]
        assert layer["commands"][0]["name"] == "/review"

    def test_hook_body_includes_referenced_script(self, project):
        """
        대상: _collect_hooks() — 스크립트 참조 hook
        구분: 정상
        오라클: (명세) command가 참조한 .claude/hooks/stop.sh 본문("exit 2")이 body에 합쳐진다 (Health 점검 입력).
        """
        stop = next(x for x in hm.collect(project)["layers"][0]["hooks"] if x["event"] == "Stop")
        assert "exit 2" in stop["body"]

    def test_global_layer_optional(self, project, tmp_path_factory):
        """
        대상: collect(global_dir) — 글로벌 레이어
        구분: 정상
        오라클: (명세) global_dir을 주면 레이어 순서가 [Global, Project]다.
        """
        g = tmp_path_factory.mktemp("global")
        (g / "CLAUDE.md").write_text("# Global\n", encoding="utf-8")
        names = [layer["name"] for layer in hm.collect(project, g)["layers"]]
        assert names == ["Global", "Project"]

    def test_skills_listed_with_commands(self, project, tmp_path_factory):
        """
        대상: _collect_layer() — skills/<이름>/SKILL.md
        구분: 정상
        오라클: (명세) 스킬이 kind=skill로 commands 목록에 함께 들어가고, 설명은 YAML 머리말의 description이다.
        """
        g = tmp_path_factory.mktemp("g_skill")
        (g / "skills" / "handoff").mkdir(parents=True)
        (g / "skills" / "handoff" / "SKILL.md").write_text(
            "---\nname: handoff\ndescription: Compact the conversation.\n---\n본문\n", encoding="utf-8")
        layer = hm.collect(project, g)["layers"][0]
        skill = next(c for c in layer["commands"] if c["name"] == "/handoff")
        assert skill["kind"] == "skill" and skill["desc"] == "Compact the conversation."

    def test_phases_without_top_index(self, project):
        """
        대상: _collect_phases() — phases/index.json 없음
        구분: 경계
        오라클: (명세) phases/*/index.json을 직접 찾고, phase status를 step 상태로 계산한다 (error 포함 → error).
        """
        (project / "phases" / "index.json").unlink()
        ph = hm.collect(project)["phases"][0]
        assert ph["dir"] == "mvp" and ph["status"] == "error"

    def test_empty_repo(self, tmp_path):
        """
        대상: collect() — 빈 디렉토리
        구분: 경계
        오라클: (예외부재+명세) 예외 없이 phases와 requirements가 빈 리스트다.
        """
        model = hm.collect(tmp_path)
        assert model["phases"] == [] and model["requirements"] == []


# ---------------------------------------------------------------------------
# check_health
# ---------------------------------------------------------------------------

class TestHealth:
    """harness_map.check_health() — 결함을 심어 둔 project fixture에서 각 규칙이 결함을 잡는지 확인.
    (fixture에 심은 결함: PreToolUse exit 1 + $CLAUDE_TOOL_INPUT, stop_hook_active 없는 Stop hook,
     PRD에 없는 R9, step2.md 누락, step1 범위/AC 누락, R2 미계획)"""

    @pytest.fixture
    def health(self, project):
        return hm.check_health(hm.collect(project))

    def test_pretooluse_exit1_is_error(self, health):
        """
        대상: check_health() — PreToolUse hook의 exit 1
        구분: 탐지
        오라클: (명세) error 메시지 "exit 1은 ..."이 있다 (exit 1은 도구 호출을 차단하지 않음).
        """
        assert any("exit 1은" in m for m in _messages(health, "error"))

    def test_pretooluse_env_var_warn(self, health):
        """
        대상: check_health() — $CLAUDE_TOOL_INPUT 사용
        구분: 탐지
        오라클: (명세) warn에 CLAUDE_TOOL_INPUT 메시지가 있다 (hook 입력은 stdin JSON).
        """
        assert any("CLAUDE_TOOL_INPUT" in m for m in _messages(health, "warn"))

    def test_stop_hook_loop_risk(self, health):
        """
        대상: check_health() — Stop hook에 stop_hook_active 체크 없음
        구분: 탐지
        오라클: (명세) warn에 stop_hook_active 메시지가 있다 (무한 루프 위험).
        """
        assert any("stop_hook_active" in m for m in _messages(health, "warn"))

    def test_unknown_requirement_id(self, health):
        """
        대상: check_health() — PRD에 없는 R-ID(R9)
        구분: 탐지
        오라클: (명세) error에 R9가 있다.
        """
        assert any("R9" in m for m in _messages(health, "error"))

    def test_missing_step_file(self, health):
        """
        대상: check_health() — index.json에는 있고 파일이 없는 step2
        구분: 탐지
        오라클: (명세) error에 step2.md가 있다.
        """
        assert any("step2.md" in m for m in _messages(health, "error"))

    def test_missing_scope_and_ac(self, health):
        """
        대상: check_health() — step1 범위/AC 누락
        구분: 탐지
        오라클: (명세) step1에 대해 "변경 허용 범위" warn과 "AC" warn이 각각 있다.
        """
        warns = _messages(health, "warn")
        assert any("step1" in m and "변경 허용 범위" in m for m in warns)
        assert any("step1" in m and "AC" in m for m in warns)

    def test_uncovered_requirement(self, health):
        """
        대상: check_health() — step이 없는 요구사항(R2)
        구분: 탐지
        오라클: (명세) 대상이 "R2"인 warn이 있다.
        """
        assert any(m.startswith("R2 ::") for m in _messages(health, "warn"))

    def test_sorted_by_level(self, health):
        """
        대상: check_health() — 정렬
        구분: 정상
        오라클: (명세) 결과가 error → warn → info 순서다 (LEVEL_ORDER 오름차순).
        """
        order = [hm.LEVEL_ORDER[x["level"]] for x in health]
        assert order == sorted(order)

    def test_clean_stop_hook_passes(self, project):
        """
        대상: check_health() — stop_hook_active를 체크하는 Stop hook
        구분: 정상
        오라클: (명세) stop_hook_active 경고가 없다 (오탐 방지).
        """
        (project / ".claude" / "hooks" / "stop.sh").write_text(
            'jq -e .stop_hook_active && exit 0\npytest || exit 2\n', encoding="utf-8"
        )
        health = hm.check_health(hm.collect(project))
        assert not any("stop_hook_active" in m for m in _messages(health))


# ---------------------------------------------------------------------------
# render / generate / main
# ---------------------------------------------------------------------------

class TestRender:
    """harness_map.render() / generate() / main() — HTML 출력과 CLI."""

    def test_html_has_all_sections(self, project):
        """
        대상: generate() — 섹션 구성
        구분: 정상
        오라클: (명세) 7개 섹션 id(summary~health)가 모두 있다.
        """
        out, _ = hm.generate(project)
        text = out.read_text(encoding="utf-8")
        for sec in ("summary", "layers", "flow", "trace", "scope", "proposals", "health"):
            assert f'id="{sec}"' in text

    def test_values_are_escaped(self, project):
        """
        대상: render() — HTML 이스케이프
        구분: 보안
        오라클: (명세) error_message의 "<b>" 태그가 원문으로 나오지 않고 &lt;b&gt;로 이스케이프된다 (XSS 방지).
        """
        out, _ = hm.generate(project)
        text = out.read_text(encoding="utf-8")
        assert "<b>빌드 실패</b>" not in text
        assert "&lt;b&gt;빌드 실패&lt;/b&gt;" in text

    def test_coverage_kpi(self, project):
        """
        대상: _render_summary() — 요구사항 커버리지
        구분: 정상
        오라클: (계산) R1만 step에 연결되었으므로 "(1/2)"와 "50%"가 표시된다.
        """
        text = hm.generate(project)[0].read_text(encoding="utf-8")
        assert "요구사항 커버리지 (1/2)" in text and ">50%<" in text

    def test_custom_out_path(self, project, tmp_path_factory):
        """
        대상: generate(out) — 출력 경로 지정
        구분: 정상
        오라클: (상태) 반환 경로가 지정 경로와 같고 파일이 생성된다.
        """
        dest = tmp_path_factory.mktemp("out") / "map.html"
        out, _ = hm.generate(project, dest)
        assert out == dest and dest.exists()

    def test_main_strict_exit_code(self, project):
        """
        대상: main() — --strict
        구분: 정상
        오라클: (종료코드) Health error가 있는 fixture에서 기본 실행은 0, --strict는 1을 반환한다.
        """
        with patch.object(hm, "ROOT", project):
            assert hm.main([]) == 0
            assert hm.main(["--strict"]) == 1

    def test_main_strict_clean_repo(self, tmp_path):
        """
        대상: main() — --strict, 빈 저장소
        구분: 경계
        오라클: (종료코드) error가 없으면 --strict여도 0을 반환한다.
        """
        with patch.object(hm, "ROOT", tmp_path):
            assert hm.main(["--strict"]) == 0


# ---------------------------------------------------------------------------
# execute.py 자동 갱신 연동
# ---------------------------------------------------------------------------

class TestExecuteIntegration:
    """StepExecutor._refresh_map() — execute.py의 Harness Map 자동 갱신 연동."""

    def _executor(self, project):
        """ROOT를 fixture 프로젝트로 바꾼 상태에서 StepExecutor를 만든다 (git, Claude 호출 없음)."""
        with patch.object(ex, "ROOT", project):
            return ex.StepExecutor("mvp")

    def test_refresh_map_writes_file(self, project):
        """
        대상: _refresh_map()
        구분: 연동
        오라클: (상태) 프로젝트 루트에 harness_map.html이 생성된다.
        """
        executor = self._executor(project)
        with patch.object(ex, "ROOT", project):
            executor._refresh_map()
        assert (project / hm.DEFAULT_OUT).exists()

    def test_refresh_map_failure_is_non_fatal(self, project, capsys):
        """
        대상: _refresh_map() — generate 예외
        구분: 예외
        오라클: (출력) 예외가 전파되지 않고 "harness map 갱신 실패: boom" 경고만 출력된다 (step 실행은 계속).
        """
        executor = self._executor(project)
        with patch.object(ex, "ROOT", project), patch.object(hm, "generate", side_effect=RuntimeError("boom")):
            executor._refresh_map()
        assert "harness map 갱신 실패: boom" in capsys.readouterr().out

    def test_finalize_refreshes_map(self, project):
        """
        대상: _finalize()
        구분: 연동
        오라클: (호출) phase 완료 처리 중 _refresh_map이 정확히 1회 호출된다.
        """
        executor = self._executor(project)
        with patch.object(ex, "ROOT", project), \
             patch.object(executor, "_run_git"), \
             patch.object(executor, "_refresh_map") as refresh:
            executor._finalize()
        refresh.assert_called_once()


# ---------------------------------------------------------------------------
# docs 연결: ADR / ARCHITECTURE / 참조 문서 / PROPOSALS 주입 제외
# ---------------------------------------------------------------------------

# ADR 상태 3종: Accepted(정상 참조), Superseded(경고), Proposed(미승인 경고).
ADR_MD = """# ADR

### ADR-001: pytest 채택
**Status**: Accepted
**결정**: pytest

### ADR-002: unittest 채택
**Status**: Superseded by ADR-001

### ADR-003: Playwright 도입
**Status**: Proposed
"""

# 트리: src/ 아래 core/(하위 auth/ 정의됨), pages/(leaf), main.py(파일).
ARCH_MD = """# 아키텍처

## 디렉토리 구조
```
src/
├── core/              # 핵심 로직
│   └── auth/
├── pages/
└── main.py
```
"""

# docs 연결 step: 참조 문서 지정, ADR 4종 참조(ADR-777은 없는 ID),
# 범위에 ARCHITECTURE 밖 경로(src/utils/*)와 검사 제외 경로(tests/...)를 섞음.
STEP_DOCS = """# Step 0: core

## 참조 문서

- `docs/ARCHITECTURE.md`

## 근거 ADR

- ADR-001: pytest
- ADR-002: unittest
- ADR-003: playwright
- ADR-777: 없는 결정

## 요구사항 ID

R1

## 변경 허용 범위

- `src/core/auth/*`
- `src/pages/login.py`
- `src/main.py`
- `src/utils/*`
- `tests/test_core.py`

## Acceptance Criteria

```bash
pytest -q
```
"""


@pytest.fixture
def linked(project):
    """project + ADR.md, ARCHITECTURE.md, step0을 STEP_DOCS로 교체 (docs 연결 테스트용)."""
    docs = project / "docs"
    (docs / "ADR.md").write_text(ADR_MD, encoding="utf-8")
    (docs / "ARCHITECTURE.md").write_text(ARCH_MD, encoding="utf-8")
    (project / "phases" / "mvp" / "step0.md").write_text(STEP_DOCS, encoding="utf-8")
    return project


class TestArchTree:
    """_parse_arch_tree() / scope_outside_arch() — ARCHITECTURE 트리 파싱과 변경 허용 범위 대조."""

    def test_tree_paths(self, linked):
        """
        대상: _parse_arch_tree()
        구분: 정상
        오라클: (명세) 트리 문자(├──, │)와 주석(#)을 처리해 전체 경로 목록이 순서대로 나온다.
        """
        arch = hm.collect(linked)["arch"]
        assert [e["path"] for e in arch] == [
            "src/", "src/core/", "src/core/auth/", "src/pages/", "src/main.py",
        ]

    @pytest.mark.parametrize("pattern, outside", [
        ("src/core/auth/*", False),     # 트리에 있는 디렉토리
        ("src/core/auth/x/y.py", False),  # leaf 디렉토리 하위는 허용
        ("src/pages/login.py", False),
        ("src/main.py", False),          # 트리에 있는 파일
        ("src/*", False),                # 최상위 디렉토리 직접
        ("src/utils/*", True),           # 트리에 없는 디렉토리
        ("src/core/db/*", True),         # 하위가 정의된 디렉토리에 새 하위 추가
        ("tests/test_core.py", False),   # 트리 최상위 밖은 검사 대상 아님
    ])
    def test_scope_outside_arch(self, linked, pattern, outside):
        """
        대상: scope_outside_arch() — parametrize 8개 경계 케이스
        구분: 경계
        오라클: (명세) 각 패턴의 기대 판정(outside)과 같다. 케이스별 의도는 parametrize 주석 참고.
        """
        assert hm.scope_outside_arch(pattern, hm.collect(linked)["arch"]) is outside

    def test_no_tree_means_no_check(self):
        """
        대상: scope_outside_arch() — 트리 없음
        구분: 경계
        오라클: (명세) ARCHITECTURE 트리가 없으면 어떤 경로도 범위 밖으로 판정하지 않는다.
        """
        assert hm.scope_outside_arch("src/anything/*", []) is False


class TestDocLinks:
    """ADR / 참조 문서 연결 수집과 Health 규칙 (linked fixture 사용)."""

    @pytest.fixture
    def health(self, linked):
        return hm.check_health(hm.collect(linked))

    def test_adr_parsed_with_status(self, linked):
        """
        대상: _collect_adrs() + _adr_state()
        구분: 정상
        오라클: (명세) ADR-001 accepted, ADR-002 superseded("Superseded by ..." 접두 처리), ADR-003 proposed.
        """
        adrs = {a["id"]: hm._adr_state(a["status"]) for a in hm.collect(linked)["adrs"]}
        assert adrs == {"ADR-001": "accepted", "ADR-002": "superseded", "ADR-003": "proposed"}

    def test_step_adr_and_ref_docs(self, linked):
        """
        대상: _collect_step() — 근거 ADR, 참조 문서
        구분: 정상
        오라클: (명세) ADR-ID가 숫자 순으로 정렬되고, 참조 문서는 경로에서 파일명만 남는다.
        """
        s0 = hm.collect(linked)["phases"][0]["steps"][0]
        assert s0["adr_ids"] == ["ADR-001", "ADR-002", "ADR-003", "ADR-777"]
        assert s0["ref_docs"] == ["ARCHITECTURE.md"]

    def test_ref_docs_none_without_section(self, linked):
        """
        대상: _collect_step() — 참조 문서 섹션 없음
        구분: 경계
        오라클: (명세) ref_docs is None (빈 리스트가 아님 → 전체 주입 모드를 뜻함).
        """
        assert hm.collect(linked)["phases"][0]["steps"][1]["ref_docs"] is None

    def test_unknown_adr_error(self, health):
        """
        대상: check_health() — ADR.md에 없는 ADR-777
        구분: 탐지
        오라클: (명세) error에 ADR-777이 있다.
        """
        assert any("ADR-777" in m for m in _messages(health, "error"))

    def test_superseded_and_proposed_warn(self, health):
        """
        대상: check_health() — ADR 상태별 판정
        구분: 탐지
        오라클: (명세) ADR-002 Superseded warn, ADR-003 미승인 warn이 있고, Accepted인 ADR-001에 대한 경고("ADR-001는 ...")는 없다.
        비고: ADR-002 메시지에 "Superseded by ADR-001"이 들어가므로 ADR-001 부재 확인은 "ADR-001는" 접두로 한다.
        """
        warns = _messages(health, "warn")
        assert any("ADR-002" in m and "Superseded" in m for m in warns)
        assert any("ADR-003" in m and "승인되지 않은" in m for m in warns)
        assert not any("ADR-001는" in m for m in _messages(health))  # Accepted는 경고 없음

    def test_scope_outside_arch_warn(self, health):
        """
        대상: check_health() — 범위가 ARCHITECTURE 밖
        구분: 탐지
        오라클: (명세) 경고 메시지에 src/utils/*는 있고, 트리에 있는 src/main.py는 없다 (오탐 방지).
        """
        msg = next(m for m in _messages(health, "warn") if "ARCHITECTURE 구조 밖" in m)
        assert "src/utils/*" in msg and "src/main.py" not in msg

    def test_missing_ref_doc_error(self, linked):
        """
        대상: check_health() — 참조 문서가 docs/에 없음
        구분: 탐지
        오라클: (명세) error에 SPEC.md가 있다.
        """
        step = linked / "phases" / "mvp" / "step0.md"
        step.write_text(STEP_DOCS.replace("docs/ARCHITECTURE.md", "docs/SPEC.md"), encoding="utf-8")
        health = hm.check_health(hm.collect(linked))
        assert any("SPEC.md" in m for m in _messages(health, "error"))

    def test_render_adr_matrix_and_injection(self, linked):
        """
        대상: render() — docs 연결 표시
        구분: 정상
        오라클: (명세) ADR × Step 표, PROPOSALS "주입 제외" 표시, step0 주입 문서(PRD.md + ARCHITECTURE.md)가 HTML에 있다.
        """
        text = hm.generate(linked)[0].read_text(encoding="utf-8")
        assert "ADR × Step" in text
        assert "주입 제외" in text
        assert "<code>PRD.md</code> <code>ARCHITECTURE.md</code>" in text


class TestGuardrailInjection:
    """StepExecutor._load_guardrails(only) / _step_docs() — PROPOSALS 주입 제외와 step별 선택 주입."""

    def _executor(self, root):
        """ROOT를 fixture 프로젝트로 바꾼 상태에서 StepExecutor를 만든다."""
        with patch.object(ex, "ROOT", root):
            return ex.StepExecutor("mvp")

    def test_proposals_never_injected(self, linked):
        """
        대상: _load_guardrails() — PROPOSALS.md
        구분: 정상
        오라클: (명세) 전체 모드와 선택 모드(PROPOSALS를 직접 지정) 모두 제안 본문이 없고, 다른 docs(ADR)는 전체 모드에서 그대로 주입된다.
        """
        executor = self._executor(linked)
        with patch.object(ex, "ROOT", linked):
            full = executor._load_guardrails()
            selected = executor._load_guardrails(["PROPOSALS.md"])
        assert "스크린샷 첨부" not in full and "스크린샷 첨부" not in selected
        assert "ADR-001" in full  # 다른 docs는 그대로 주입

    def test_selective_injection_keeps_prd(self, linked):
        """
        대상: _load_guardrails(only) — 선택 주입
        구분: 정상
        오라클: (명세) 지정한 ARCHITECTURE와 항상 포함되는 PRD·CLAUDE.md는 있고, 지정하지 않은 ADR은 없다.
        """
        executor = self._executor(linked)
        with patch.object(ex, "ROOT", linked):
            result = executor._load_guardrails(["ARCHITECTURE.md"])
        assert "디렉토리 구조" in result      # 지정한 문서
        assert "로그인" in result            # PRD는 항상
        assert "ADR-001" not in result       # 지정하지 않은 문서
        assert "범위 밖 수정 금지" in result   # CLAUDE.md는 항상

    def test_step_docs_parsed(self, linked):
        """
        대상: _step_docs()
        구분: 경계
        오라클: (명세) 섹션 있음 → 파일명 리스트, 섹션 없음 → None, step 파일 없음 → None.
        """
        executor = self._executor(linked)
        assert executor._step_docs(0) == ["ARCHITECTURE.md"]
        assert executor._step_docs(1) is None   # 섹션 없음 → 전체 주입
        assert executor._step_docs(9) is None   # 파일 없음

    def test_single_step_uses_selected_docs(self, linked):
        """
        대상: _execute_single_step() — 선택 주입 적용
        구분: 연동
        오라클: (호출) _build_preamble에 전달된 가드레일이 인자로 받은 "FULL"이 아니라 step0의 선택 주입 결과다. 첫 preamble 생성 시점에 SystemExit로 루프를 끊어 Claude 호출 없이 확인한다.
        """
        executor = self._executor(linked)
        seen = {}

        def fake_preamble(guardrails, ctx, prev_error=None):
            seen["g"] = guardrails
            raise SystemExit(0)

        with patch.object(ex, "ROOT", linked), \
             patch.object(executor, "_build_preamble", side_effect=fake_preamble):
            with pytest.raises(SystemExit):
                executor._execute_single_step({"step": 0, "name": "core"}, "FULL")
        assert seen["g"] != "FULL" and "디렉토리 구조" in seen["g"]


# ---------------------------------------------------------------------------
# 독립 환경: 글로벌 위치 지정 / 상위 폴더 CLAUDE.md
# ---------------------------------------------------------------------------

class TestIsolatedEnv:
    """글로벌 위치(--global-dir, CLAUDE_CONFIG_DIR)와 상위 폴더 CLAUDE.md(ancestors) 처리."""

    @pytest.fixture
    def nested(self, tmp_path):
        """workspace/CLAUDE.md 아래에 프로젝트 workspace/proj 를 둔 구조."""
        ws = tmp_path / "workspace"
        proj = ws / "proj"
        (proj / ".claude").mkdir(parents=True)
        (ws / "CLAUDE.md").write_text("# 상위 규칙\n", encoding="utf-8")
        return proj

    def test_ancestor_claude_md_detected(self, nested):
        """
        대상: _collect_ancestors() — 상위 폴더에 CLAUDE.md 있음
        구분: 정상
        오라클: (명세) workspace/CLAUDE.md가 excluded=False로 수집되고, Health에 info가 생긴다.
        """
        model = hm.collect(nested)
        anc = [a for a in model["ancestors"] if a["path"].endswith("workspace/CLAUDE.md")]
        assert anc and anc[0]["excluded"] is False
        assert any("workspace/CLAUDE.md" in m for m in _messages(hm.check_health(model), "info"))

    @pytest.mark.parametrize("pattern", ["**/workspace/CLAUDE.md", "{abs}"])
    def test_ancestor_excluded_by_setting(self, nested, pattern):
        """
        대상: _collect_ancestors() — settings.local.json의 claudeMdExcludes
        구분: 정상
        오라클: (명세) **/폴더명 패턴과 절대 경로 패턴 모두 excluded=True가 되고, Health info가 사라진다.
        """
        abs_path = (nested.parent / "CLAUDE.md").resolve().as_posix()
        (nested / ".claude" / "settings.local.json").write_text(
            json.dumps({"claudeMdExcludes": [pattern.replace("{abs}", abs_path)]}), encoding="utf-8")
        model = hm.collect(nested)
        assert all(a["excluded"] for a in model["ancestors"] if a["path"] == abs_path)
        assert not any(abs_path in m for m in _messages(hm.check_health(model)))

    def test_global_dir_option(self, project, tmp_path_factory):
        """
        대상: main(["--global-dir", DIR])
        구분: 정상
        오라클: (명세) 지정한 폴더가 Global 레이어로 렌더링된다 (경로가 HTML에 표시됨).
        """
        g = tmp_path_factory.mktemp("iso_home")
        (g / "CLAUDE.md").write_text("# iso\n", encoding="utf-8")
        with patch.object(hm, "ROOT", project):
            hm.main(["--global-dir", str(g)])
        assert str(g / "CLAUDE.md") in (project / hm.DEFAULT_OUT).read_text(encoding="utf-8")

    def test_with_global_uses_claude_config_dir(self, project, tmp_path_factory, monkeypatch):
        """
        대상: main(["--with-global"]) — CLAUDE_CONFIG_DIR 설정됨
        구분: 연동
        오라클: (명세) ~/.claude 대신 CLAUDE_CONFIG_DIR 폴더를 Global 레이어로 쓴다 (Claude Code와 같은 규칙).
        """
        g = tmp_path_factory.mktemp("cfg_dir")
        (g / "CLAUDE.md").write_text("# cfg\n", encoding="utf-8")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(g))
        with patch.object(hm, "ROOT", project):
            hm.main(["--with-global"])
        assert str(g / "CLAUDE.md") in (project / hm.DEFAULT_OUT).read_text(encoding="utf-8")

    def test_absolute_hook_script_is_read(self, project, tmp_path_factory):
        """
        대상: _collect_hooks() — command가 따옴표로 감싼 절대 경로 스크립트
        구분: 경계
        오라클: (명세) 절대 경로 스크립트 본문이 body에 포함된다 (설치기가 쓰는 "bash \\"<절대경로>\\"" 형식).
        """
        g = tmp_path_factory.mktemp("abs_home")
        (g / "hooks").mkdir()
        (g / "hooks" / "guard.sh").write_text("echo x\nexit 2\n", encoding="utf-8")
        (g / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": f'bash "{(g / "hooks" / "guard.sh").as_posix()}"'}]}]}}),
            encoding="utf-8")
        layer = hm.collect(project, g)["layers"][0]
        assert "exit 2" in layer["hooks"][0]["body"]

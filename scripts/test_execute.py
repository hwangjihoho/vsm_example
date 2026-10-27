"""
execute.py 단위 테스트 — 리팩터링 전후 동작이 동일한지 확인하는 안전망.

격리 전략
  - 파일시스템: tmp_path 기반 임시 프로젝트 (tmp_project → phase_dir → executor fixture)
  - 경로:       patch.object(ex, "ROOT", tmp_project)로 프로젝트 루트 교체
  - Claude CLI: subprocess.run mock (실제 claude 호출 없음)
  - git:        executor._run_git을 가짜 함수로 교체해 호출 인자/순서만 검증

범위 밖 (이 파일에서 다루지 않음)
  - _execute_single_step의 재시도 → error / blocked 분기, _execute_all_steps, run() 전체 흐름
  - 선택 주입 / PROPOSALS 제외 → test_harness_map.py::TestGuardrailInjection

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
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import execute as ex


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_project(tmp_path):
    """phases/, CLAUDE.md, docs/ 를 갖춘 임시 프로젝트 구조."""
    phases_dir = tmp_path / "phases"
    phases_dir.mkdir()

    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# Rules\n- rule one\n- rule two", encoding="utf-8")

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "arch.md").write_text("# Architecture\nSome content", encoding="utf-8")
    (docs_dir / "guide.md").write_text("# Guide\nAnother doc", encoding="utf-8")

    return tmp_path


@pytest.fixture
def phase_dir(tmp_project):
    """step 3개를 가진 phase 디렉토리.

    step0, step1: completed + summary (컨텍스트 누적 대상)
    step2: pending, step2.md만 존재 (_invoke_claude 대상)
    """
    d = tmp_project / "phases" / "0-mvp"
    d.mkdir()

    index = {
        "project": "TestProject",
        "phase": "mvp",
        "steps": [
            {"step": 0, "name": "setup", "status": "completed", "summary": "프로젝트 초기화 완료"},
            {"step": 1, "name": "core", "status": "completed", "summary": "핵심 로직 구현"},
            {"step": 2, "name": "ui", "status": "pending"},
        ],
    }
    (d / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    (d / "step2.md").write_text("# Step 2: UI\n\nUI를 구현하세요.", encoding="utf-8")

    return d


@pytest.fixture
def top_index(tmp_project):
    """phases/index.json (top-level)."""
    top = {
        "phases": [
            {"dir": "0-mvp", "status": "pending"},
            {"dir": "1-polish", "status": "pending"},
        ]
    }
    p = tmp_project / "phases" / "index.json"
    p.write_text(json.dumps(top, indent=2), encoding="utf-8")
    return p


@pytest.fixture
def executor(tmp_project, phase_dir):
    """테스트용 StepExecutor 인스턴스. git 호출은 별도 mock 필요."""
    with patch.object(ex, "ROOT", tmp_project):
        inst = ex.StepExecutor("0-mvp")
    # 내부 경로를 tmp_project 기준으로 재설정
    inst._root = str(tmp_project)
    inst._phases_dir = tmp_project / "phases"
    inst._phase_dir = phase_dir
    inst._phase_dir_name = "0-mvp"
    inst._index_file = phase_dir / "index.json"
    inst._top_index_file = tmp_project / "phases" / "index.json"
    return inst


# ---------------------------------------------------------------------------
# _stamp (= 이전 now_iso)
# ---------------------------------------------------------------------------

class TestStamp:
    """StepExecutor._stamp() — index.json에 기록하는 타임스탬프 생성."""

    def test_returns_kst_timestamp(self, executor):
        """
        대상: _stamp()
        구분: 정상
        오라클: (명세) 반환 문자열에 KST 오프셋 "+0900"이 포함된다.
        """
        result = executor._stamp()
        assert "+0900" in result

    def test_format_is_iso(self, executor):
        """
        대상: _stamp()
        구분: 정상
        오라클: (명세) "%Y-%m-%dT%H:%M:%S%z" 형식으로 파싱되고 tzinfo가 있다.
        """
        result = executor._stamp()
        dt = datetime.strptime(result, "%Y-%m-%dT%H:%M:%S%z")
        assert dt.tzinfo is not None

    def test_is_current_time(self, executor):
        """
        대상: _stamp()
        구분: 정상
        오라클: (비교) 호출 전후 시각 사이 값이다 (초 단위 절삭 고려해 +1초 허용).
        """
        before = datetime.now(ex.StepExecutor.TZ).replace(microsecond=0)
        result = executor._stamp()
        after = datetime.now(ex.StepExecutor.TZ).replace(microsecond=0) + timedelta(seconds=1)
        parsed = datetime.strptime(result, "%Y-%m-%dT%H:%M:%S%z")
        assert before <= parsed <= after


# ---------------------------------------------------------------------------
# _read_json / _write_json
# ---------------------------------------------------------------------------

class TestJsonHelpers:
    """StepExecutor._read_json / _write_json — index.json 입출력 헬퍼."""

    def test_roundtrip(self, tmp_path):
        """
        대상: _write_json → _read_json
        구분: 정상
        오라클: (비교) 쓰고 다시 읽은 dict가 원본과 같다 (한글, 중첩 리스트 포함).
        """
        data = {"key": "값", "nested": [1, 2, 3]}
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, data)
        loaded = ex.StepExecutor._read_json(p)
        assert loaded == data

    def test_save_ensures_ascii_false(self, tmp_path):
        """
        대상: _write_json()
        구분: 정상
        오라클: (상태) 파일 원문에 한글이 그대로 있고 유니코드 이스케이프 시퀀스가 없다 (ensure_ascii=False).
        """
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, {"한글": "테스트"})
        raw = p.read_text(encoding="utf-8")
        assert "한글" in raw
        assert "\\u" not in raw

    def test_save_indented(self, tmp_path):
        """
        대상: _write_json()
        구분: 정상
        오라클: (상태) 파일 원문에 줄바꿈이 있다 (indent 적용).
        비고: 들여쓰기 폭(2)까지는 확인하지 않는다.
        """
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, {"a": 1})
        raw = p.read_text(encoding="utf-8")
        assert "\n" in raw

    def test_load_nonexistent_raises(self, tmp_path):
        """
        대상: _read_json() — 파일 없음
        구분: 예외
        오라클: (예외) FileNotFoundError가 발생한다.
        """
        with pytest.raises(FileNotFoundError):
            ex.StepExecutor._read_json(tmp_path / "nope.json")


# ---------------------------------------------------------------------------
# _load_guardrails
# ---------------------------------------------------------------------------

class TestLoadGuardrails:
    """StepExecutor._load_guardrails() — 매 step 프롬프트에 주입할 CLAUDE.md + docs 결합.
    (선택 주입 / PROPOSALS 제외는 test_harness_map.py::TestGuardrailInjection에서 검증)"""

    def test_loads_claude_md_and_docs(self, executor, tmp_project):
        """
        대상: _load_guardrails() — 전체 주입 모드
        구분: 정상
        오라클: (명세) 결과에 CLAUDE.md 본문과 docs/*.md 두 문서 본문이 모두 있다.
        """
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "# Rules" in result
        assert "rule one" in result
        assert "# Architecture" in result
        assert "# Guide" in result

    def test_sections_separated_by_divider(self, executor, tmp_project):
        """
        대상: _load_guardrails() — 섹션 구분자
        구분: 정상
        오라클: (명세) 결과에 "---" 구분자가 있다.
        비고: ⚠ 약한 오라클: 구분자 개수와 위치는 확인하지 않는다.
        """
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "---" in result

    def test_docs_sorted_alphabetically(self, executor, tmp_project):
        """
        대상: _load_guardrails() — 문서 순서
        구분: 정상
        오라클: (명세) arch.md 섹션이 guide.md 섹션보다 앞에 나온다 (파일명 오름차순).
        """
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        arch_pos = result.index("arch")
        guide_pos = result.index("guide")
        assert arch_pos < guide_pos

    def test_no_claude_md(self, executor, tmp_project):
        """
        대상: _load_guardrails() — CLAUDE.md 없음
        구분: 경계
        오라클: (명세) CLAUDE.md 섹션 없이 docs만 포함된다.
        """
        (tmp_project / "CLAUDE.md").unlink()
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "CLAUDE.md" not in result
        assert "Architecture" in result

    def test_no_docs_dir(self, executor, tmp_project):
        """
        대상: _load_guardrails() — docs/ 없음
        구분: 경계
        오라클: (명세) CLAUDE.md만 포함되고 docs 본문은 없다.
        """
        import shutil
        shutil.rmtree(tmp_project / "docs")
        with patch.object(ex, "ROOT", tmp_project):
            result = executor._load_guardrails()
        assert "Rules" in result
        assert "Architecture" not in result

    def test_empty_project(self, tmp_path):
        """
        대상: _load_guardrails() — CLAUDE.md와 docs 모두 없음
        구분: 경계
        오라클: (명세) 빈 문자열을 반환한다.
        """
        with patch.object(ex, "ROOT", tmp_path):
            # executor가 필요 없는 static-like 동작이므로 임시 인스턴스
            phases_dir = tmp_path / "phases" / "dummy"
            phases_dir.mkdir(parents=True)
            idx = {"project": "T", "phase": "t", "steps": []}
            (phases_dir / "index.json").write_text(json.dumps(idx), encoding="utf-8")
            inst = ex.StepExecutor.__new__(ex.StepExecutor)
            result = inst._load_guardrails()
        assert result == ""


# ---------------------------------------------------------------------------
# _build_step_context
# ---------------------------------------------------------------------------

class TestBuildStepContext:
    """StepExecutor._build_step_context() — 완료 step의 summary를 다음 step 프롬프트용으로 누적."""

    def test_includes_completed_with_summary(self, phase_dir):
        """
        대상: _build_step_context()
        구분: 정상
        오라클: (명세) completed이고 summary가 있는 step이 "Step N (name): summary" 형식으로 들어간다.
        """
        index = json.loads((phase_dir / "index.json").read_text(encoding="utf-8"))
        result = ex.StepExecutor._build_step_context(index)
        assert "Step 0 (setup): 프로젝트 초기화 완료" in result
        assert "Step 1 (core): 핵심 로직 구현" in result

    def test_excludes_pending(self, phase_dir):
        """
        대상: _build_step_context() — pending step
        구분: 정상
        오라클: (명세) pending step 이름이 결과에 없다.
        """
        index = json.loads((phase_dir / "index.json").read_text(encoding="utf-8"))
        result = ex.StepExecutor._build_step_context(index)
        assert "ui" not in result

    def test_excludes_completed_without_summary(self, phase_dir):
        """
        대상: _build_step_context() — summary 없는 completed
        구분: 경계
        오라클: (명세) summary가 없는 step은 빠지고 나머지는 남는다.
        """
        index = json.loads((phase_dir / "index.json").read_text(encoding="utf-8"))
        del index["steps"][0]["summary"]
        result = ex.StepExecutor._build_step_context(index)
        assert "setup" not in result
        assert "core" in result

    def test_empty_when_no_completed(self):
        """
        대상: _build_step_context() — 완료 step 0개
        구분: 경계
        오라클: (명세) 빈 문자열을 반환한다 (헤더도 없음).
        """
        index = {"steps": [{"step": 0, "name": "a", "status": "pending"}]}
        result = ex.StepExecutor._build_step_context(index)
        assert result == ""

    def test_has_header(self, phase_dir):
        """
        대상: _build_step_context()
        구분: 정상
        오라클: (명세) 결과가 "## 이전 Step 산출물" 헤더로 시작한다.
        """
        index = json.loads((phase_dir / "index.json").read_text(encoding="utf-8"))
        result = ex.StepExecutor._build_step_context(index)
        assert result.startswith("## 이전 Step 산출물")


# ---------------------------------------------------------------------------
# _build_preamble
# ---------------------------------------------------------------------------

class TestBuildPreamble:
    """StepExecutor._build_preamble() — step 프롬프트 머리말(가드레일 + 컨텍스트 + 재시도 피드백 + 작업 규칙)."""

    def test_includes_project_name(self, executor):
        """
        대상: _build_preamble()
        구분: 정상
        오라클: (명세) index.json의 project 값이 포함된다.
        """
        result = executor._build_preamble("", "")
        assert "TestProject" in result

    def test_includes_guardrails(self, executor):
        """
        대상: _build_preamble()
        구분: 정상
        오라클: (명세) 전달한 가드레일 문자열이 그대로 포함된다.
        """
        result = executor._build_preamble("GUARD_CONTENT", "")
        assert "GUARD_CONTENT" in result

    def test_includes_step_context(self, executor):
        """
        대상: _build_preamble()
        구분: 정상
        오라클: (명세) 전달한 step 컨텍스트가 포함된다.
        """
        ctx = "## 이전 Step 산출물\n\n- Step 0: done"
        result = executor._build_preamble("", ctx)
        assert "이전 Step 산출물" in result

    def test_includes_commit_example(self, executor):
        """
        대상: _build_preamble()
        구분: 정상
        오라클: (명세) FEAT_MSG 형식의 커밋 예시("feat(mvp):")가 포함된다.
        """
        result = executor._build_preamble("", "")
        assert "feat(mvp):" in result

    def test_includes_rules(self, executor):
        """
        대상: _build_preamble()
        구분: 정상
        오라클: (명세) "작업 규칙" 섹션과 AC 지시가 포함된다.
        """
        result = executor._build_preamble("", "")
        assert "작업 규칙" in result
        assert "AC" in result

    def test_no_retry_section_by_default(self, executor):
        """
        대상: _build_preamble() — 첫 시도
        구분: 정상
        오라클: (명세) prev_error가 없으면 재시도 섹션이 없다.
        """
        result = executor._build_preamble("", "")
        assert "이전 시도 실패" not in result

    def test_retry_section_with_prev_error(self, executor):
        """
        대상: _build_preamble() — 재시도
        구분: 정상
        오라클: (명세) prev_error가 있으면 재시도 섹션과 이전 에러 메시지가 포함된다 (자가 교정 피드백).
        """
        result = executor._build_preamble("", "", prev_error="타입 에러 발생")
        assert "이전 시도 실패" in result
        assert "타입 에러 발생" in result

    def test_includes_max_retries(self, executor):
        """
        대상: _build_preamble()
        구분: 정상
        오라클: (명세) MAX_RETRIES 값이 규칙 문구에 포함된다.
        """
        result = executor._build_preamble("", "")
        assert str(ex.StepExecutor.MAX_RETRIES) in result

    def test_includes_index_path(self, executor):
        """
        대상: _build_preamble()
        구분: 정상
        오라클: (명세) Claude가 status를 갱신할 index.json 경로가 포함된다.
        """
        result = executor._build_preamble("", "")
        assert "/phases/0-mvp/index.json" in result


# ---------------------------------------------------------------------------
# _update_top_index
# ---------------------------------------------------------------------------

class TestUpdateTopIndex:
    """StepExecutor._update_top_index() — phases/index.json의 phase 상태와 타임스탬프 갱신."""

    def test_completed(self, executor, top_index):
        """
        대상: _update_top_index("completed")
        구분: 정상
        오라클: (상태) 해당 phase의 status가 completed이고 completed_at이 기록된다.
        """
        executor._top_index_file = top_index
        executor._update_top_index("completed")
        data = json.loads(top_index.read_text(encoding="utf-8"))
        mvp = next(p for p in data["phases"] if p["dir"] == "0-mvp")
        assert mvp["status"] == "completed"
        assert "completed_at" in mvp

    def test_error(self, executor, top_index):
        """
        대상: _update_top_index("error")
        구분: 정상
        오라클: (상태) 해당 phase의 status가 error이고 failed_at이 기록된다.
        """
        executor._top_index_file = top_index
        executor._update_top_index("error")
        data = json.loads(top_index.read_text(encoding="utf-8"))
        mvp = next(p for p in data["phases"] if p["dir"] == "0-mvp")
        assert mvp["status"] == "error"
        assert "failed_at" in mvp

    def test_blocked(self, executor, top_index):
        """
        대상: _update_top_index("blocked")
        구분: 정상
        오라클: (상태) 해당 phase의 status가 blocked이고 blocked_at이 기록된다.
        """
        executor._top_index_file = top_index
        executor._update_top_index("blocked")
        data = json.loads(top_index.read_text(encoding="utf-8"))
        mvp = next(p for p in data["phases"] if p["dir"] == "0-mvp")
        assert mvp["status"] == "blocked"
        assert "blocked_at" in mvp

    def test_other_phases_unchanged(self, executor, top_index):
        """
        대상: _update_top_index() — 다른 phase
        구분: 정상
        오라클: (비교) 대상이 아닌 phase(1-polish)의 status가 그대로다.
        """
        executor._top_index_file = top_index
        executor._update_top_index("completed")
        data = json.loads(top_index.read_text(encoding="utf-8"))
        polish = next(p for p in data["phases"] if p["dir"] == "1-polish")
        assert polish["status"] == "pending"

    def test_nonexistent_dir_is_noop(self, executor, top_index):
        """
        대상: _update_top_index() — 목록에 없는 phase
        구분: 경계
        오라클: (비교) 모든 phase의 status가 호출 전과 같다.
        """
        executor._top_index_file = top_index
        executor._phase_dir_name = "no-such-dir"
        original = json.loads(top_index.read_text(encoding="utf-8"))
        executor._update_top_index("completed")
        after = json.loads(top_index.read_text(encoding="utf-8"))
        for p_before, p_after in zip(original["phases"], after["phases"]):
            assert p_before["status"] == p_after["status"]

    def test_no_top_index_file(self, executor, tmp_path):
        """
        대상: _update_top_index() — phases/index.json 없음
        구분: 경계
        오라클: (예외부재) 예외 없이 끝난다 (no-op).
        """
        executor._top_index_file = tmp_path / "nonexistent.json"
        executor._update_top_index("completed")  # should not raise


# ---------------------------------------------------------------------------
# _checkout_branch (mocked)
# ---------------------------------------------------------------------------

class TestCheckoutBranch:
    """StepExecutor._checkout_branch() — feat-{phase} 브랜치 준비. git은 _run_git을 응답 시퀀스로 대체."""

    def _mock_git(self, executor, responses):
        """_run_git을 응답 시퀀스로 대체한다. 호출 순서대로 responses를 반환하고, 소진되면 성공(0)을 반환한다.

        _checkout_branch의 git 호출 순서: rev-parse --abbrev-ref HEAD → rev-parse --verify {branch} → checkout [-b]
        """
        call_idx = {"i": 0}
        def fake_git(*args):
            idx = call_idx["i"]
            call_idx["i"] += 1
            if idx < len(responses):
                return responses[idx]
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

    def test_already_on_branch(self, executor):
        """
        대상: _checkout_branch() — 이미 feat-mvp 브랜치
        구분: 정상
        오라클: (예외부재) 예외 없이 끝난다.
        비고: ⚠ 약한 오라클: checkout이 호출되지 않았는지는 확인하지 않는다.
        """
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="feat-mvp\n", stderr=""),
        ])
        executor._checkout_branch()  # should return without checkout

    def test_branch_exists_checkout(self, executor):
        """
        대상: _checkout_branch() — 브랜치 존재
        구분: 정상
        오라클: (예외부재) 예외 없이 끝난다.
        비고: ⚠ 약한 오라클: `checkout feat-mvp`(-b 없음) 호출 인자를 확인하지 않는다.
        """
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ])
        executor._checkout_branch()

    def test_branch_not_exists_create(self, executor):
        """
        대상: _checkout_branch() — 브랜치 없음
        구분: 정상
        오라클: (예외부재) 예외 없이 끝난다.
        비고: ⚠ 약한 오라클: `checkout -b feat-mvp` 호출 인자를 확인하지 않는다.
        """
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="not found"),
            MagicMock(returncode=0, stdout="", stderr=""),
        ])
        executor._checkout_branch()

    def test_checkout_fails_exits(self, executor):
        """
        대상: _checkout_branch() — checkout 실패 (dirty tree)
        구분: 예외
        오라클: (종료코드) SystemExit(1).
        """
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="dirty tree"),
        ])
        with pytest.raises(SystemExit) as exc_info:
            executor._checkout_branch()
        assert exc_info.value.code == 1

    def test_no_git_exits(self, executor):
        """
        대상: _checkout_branch() — git repo 아님
        구분: 예외
        오라클: (종료코드) SystemExit(1).
        """
        self._mock_git(executor, [
            MagicMock(returncode=1, stdout="", stderr="not a git repo"),
        ])
        with pytest.raises(SystemExit) as exc_info:
            executor._checkout_branch()
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _commit_step (mocked)
# ---------------------------------------------------------------------------

class TestCommitStep:
    """StepExecutor._commit_step() — 코드(feat)와 메타데이터(chore) 2단계 커밋. git은 _run_git 대체."""

    def test_two_phase_commit(self, executor):
        """
        대상: _commit_step() — 코드와 메타데이터 모두 변경
        구분: 정상
        오라클: (호출) commit이 2회, 첫 번째는 "feat(mvp):", 두 번째는 "chore(mvp):" 메시지다.
        """
        calls = []
        def fake_git(*args):
            calls.append(args)
            if args[:2] == ("diff", "--cached"):
                return MagicMock(returncode=1)
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

        executor._commit_step(2, "ui")

        commit_calls = [c for c in calls if c[0] == "commit"]
        assert len(commit_calls) == 2
        assert "feat(mvp):" in commit_calls[0][2]
        assert "chore(mvp):" in commit_calls[1][2]

    def test_no_code_changes_skips_feat_commit(self, executor):
        """
        대상: _commit_step() — 코드 변경 없음
        구분: 경계
        오라클: (호출) commit이 1회이고 chore 메시지다 (빈 feat 커밋을 만들지 않음).
        """
        call_count = {"diff": 0}
        calls = []
        def fake_git(*args):
            calls.append(args)
            if args[:2] == ("diff", "--cached"):
                call_count["diff"] += 1
                if call_count["diff"] == 1:
                    return MagicMock(returncode=0)
                return MagicMock(returncode=1)
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

        executor._commit_step(2, "ui")

        commit_msgs = [c[2] for c in calls if c[0] == "commit"]
        assert len(commit_msgs) == 1
        assert "chore" in commit_msgs[0]

    def test_stage_limited_to_project_dir(self, executor):
        """
        대상: _commit_step() — git add 범위
        구분: 경계
        오라클: (호출) 모든 `git add`가 `-A -- .` 형태다. 프로젝트가 상위 저장소의 하위 폴더일 때
                저장소의 다른 폴더 변경이 step 커밋에 섞이지 않도록 하기 위함.
        """
        calls = []
        def fake_git(*args):
            calls.append(args)
            return MagicMock(returncode=1 if args[:2] == ("diff", "--cached") else 0, stdout="", stderr="")
        executor._run_git = fake_git

        executor._commit_step(2, "ui")

        adds = [c for c in calls if c[0] == "add"]
        assert adds and all(c == ("add", "-A", "--", ".") for c in adds)


# ---------------------------------------------------------------------------
# _invoke_claude (mocked)
# ---------------------------------------------------------------------------

class TestInvokeClaude:
    """StepExecutor._invoke_claude() — `claude -p` 서브프로세스 호출. subprocess.run을 mock."""

    def test_invokes_claude_with_correct_args(self, executor):
        """
        대상: _invoke_claude() — CLI 인자
        구분: 정상
        오라클: (호출) `claude -p --dangerously-skip-permissions --output-format ...`로 호출되고, 마지막 인자(프롬프트)에 preamble과 step 파일 본문이 모두 있다.
        """
        mock_result = MagicMock(returncode=0, stdout='{"result": "ok"}', stderr="")
        step = {"step": 2, "name": "ui"}
        preamble = "PREAMBLE\n"

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            executor._invoke_claude(step, preamble)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--output-format" in cmd
        assert "PREAMBLE" in cmd[-1]
        assert "UI를 구현하세요" in cmd[-1]

    def test_saves_output_json(self, executor):
        """
        대상: _invoke_claude() — 실행 기록
        구분: 정상
        오라클: (상태) step2-output.json에 step, name, exitCode가 저장된다.
        """
        mock_result = MagicMock(returncode=0, stdout='{"ok": true}', stderr="")
        step = {"step": 2, "name": "ui"}

        with patch("subprocess.run", return_value=mock_result):
            executor._invoke_claude(step, "preamble")

        output_file = executor._phase_dir / "step2-output.json"
        assert output_file.exists()
        data = json.loads(output_file.read_text(encoding="utf-8"))
        assert data["step"] == 2
        assert data["name"] == "ui"
        assert data["exitCode"] == 0

    def test_nonexistent_step_file_exits(self, executor):
        """
        대상: _invoke_claude() — step 파일 없음
        구분: 예외
        오라클: (종료코드) SystemExit(1). Claude를 호출하기 전에 중단한다.
        """
        step = {"step": 99, "name": "nonexistent"}
        with pytest.raises(SystemExit) as exc_info:
            executor._invoke_claude(step, "preamble")
        assert exc_info.value.code == 1

    def test_timeout_is_1800(self, executor):
        """
        대상: _invoke_claude() — 타임아웃
        구분: 경계
        오라클: (호출) subprocess.run의 timeout 인자가 1800초다.
        """
        mock_result = MagicMock(returncode=0, stdout="{}", stderr="")
        step = {"step": 2, "name": "ui"}

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            executor._invoke_claude(step, "preamble")

        assert mock_run.call_args[1]["timeout"] == 1800


# ---------------------------------------------------------------------------
# progress_indicator (= 이전 Spinner)
# ---------------------------------------------------------------------------

class TestProgressIndicator:
    """progress_indicator() — 터미널 진행 표시 컨텍스트 매니저."""

    def test_context_manager(self):
        """
        대상: progress_indicator()
        구분: 정상
        오라클: (비교) 0.15초 대기 후 elapsed >= 0.1.
        """
        import time
        with ex.progress_indicator("test") as pi:
            time.sleep(0.15)
        assert pi.elapsed >= 0.1

    def test_elapsed_increases(self):
        """
        대상: progress_indicator()
        구분: 정상
        오라클: (비교) elapsed > 0.
        비고: ⚠ 약한 오라클: test_context_manager와 중복이며 더 느슨하다.
        """
        import time
        with ex.progress_indicator("test") as pi:
            time.sleep(0.2)
        assert pi.elapsed > 0


# ---------------------------------------------------------------------------
# main() CLI 파싱 (mocked)
# ---------------------------------------------------------------------------

class TestMainCli:
    """main() — CLI 인자 파싱과 phase 디렉토리 검증."""

    def test_no_args_exits(self):
        """
        대상: main() — 인자 없음
        구분: 예외
        오라클: (종료코드) argparse가 SystemExit(2)로 종료한다.
        """
        with patch("sys.argv", ["execute.py"]):
            with pytest.raises(SystemExit) as exc_info:
                ex.main()
            assert exc_info.value.code == 2  # argparse exits with 2

    def test_invalid_phase_dir_exits(self):
        """
        대상: main() — 없는 phase 디렉토리
        구분: 예외
        오라클: (종료코드) SystemExit(1).
        """
        with patch("sys.argv", ["execute.py", "nonexistent"]):
            with patch.object(ex, "ROOT", Path("/tmp/fake_nonexistent")):
                with pytest.raises(SystemExit) as exc_info:
                    ex.main()
                assert exc_info.value.code == 1

    def test_missing_index_exits(self, tmp_project):
        """
        대상: main() — index.json 없는 phase
        구분: 예외
        오라클: (종료코드) SystemExit(1).
        """
        (tmp_project / "phases" / "empty").mkdir()
        with patch("sys.argv", ["execute.py", "empty"]):
            with patch.object(ex, "ROOT", tmp_project):
                with pytest.raises(SystemExit) as exc_info:
                    ex.main()
                assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _check_blockers (= 이전 main() error/blocked 체크)
# ---------------------------------------------------------------------------

class TestCheckBlockers:
    """StepExecutor._check_blockers() — 이전 실행이 error/blocked로 끝난 phase의 재실행 차단."""

    def _make_executor_with_steps(self, tmp_project, steps):
        """__init__을 거치지 않고 주어진 steps로 index.json을 만든 executor를 조립한다."""
        d = tmp_project / "phases" / "test-phase"
        d.mkdir(exist_ok=True)
        index = {"project": "T", "phase": "test", "steps": steps}
        (d / "index.json").write_text(json.dumps(index), encoding="utf-8")

        with patch.object(ex, "ROOT", tmp_project):
            inst = ex.StepExecutor.__new__(ex.StepExecutor)
        inst._root = str(tmp_project)
        inst._phases_dir = tmp_project / "phases"
        inst._phase_dir = d
        inst._phase_dir_name = "test-phase"
        inst._index_file = d / "index.json"
        inst._top_index_file = tmp_project / "phases" / "index.json"
        inst._phase_name = "test"
        inst._total = len(steps)
        return inst

    def test_error_step_exits_1(self, tmp_project):
        """
        대상: _check_blockers() — error step 존재
        구분: 예외
        오라클: (종료코드) SystemExit(1). 사람이 status를 pending으로 되돌리기 전에는 재실행하지 않는다.
        """
        steps = [
            {"step": 0, "name": "ok", "status": "completed"},
            {"step": 1, "name": "bad", "status": "error", "error_message": "fail"},
        ]
        inst = self._make_executor_with_steps(tmp_project, steps)
        with pytest.raises(SystemExit) as exc_info:
            inst._check_blockers()
        assert exc_info.value.code == 1

    def test_blocked_step_exits_2(self, tmp_project):
        """
        대상: _check_blockers() — blocked step 존재
        구분: 예외
        오라클: (종료코드) SystemExit(2). error(1)와 구분되는 종료 코드다.
        """
        steps = [
            {"step": 0, "name": "ok", "status": "completed"},
            {"step": 1, "name": "stuck", "status": "blocked", "blocked_reason": "API key"},
        ]
        inst = self._make_executor_with_steps(tmp_project, steps)
        with pytest.raises(SystemExit) as exc_info:
            inst._check_blockers()
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# phase 실행 안전장치: 독립 환경 점검 / 실행 중 표시 / step 세션 표시 / Stop hook 연동
# ---------------------------------------------------------------------------

class TestRunSafety:
    """StepExecutor._check_isolation / _acquire_lock / _release_lock / run() / _invoke_claude env,
    그리고 .claude/hooks/stop-verify.sh의 실행 중 분기."""

    def _launcher(self, root, home=None):
        (root / "claude-isolated.cmd").write_text("@echo off\r\n", encoding="utf-8")
        if home is not None:
            (root / ".harness-kit").mkdir(exist_ok=True)
            (root / ".harness-kit" / "manifest.json").write_text(
                json.dumps({"created": [], "claude_home": str(home)}), encoding="utf-8")

    def test_isolation_skipped_without_launcher(self, executor, tmp_project, monkeypatch):
        """
        대상: _check_isolation() — 런처 없는 프로젝트 (기본 모드 설치)
        구분: 정상
        오라클: (예외부재) CLAUDE_CONFIG_DIR이 없어도 통과한다.
        """
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        with patch.object(ex, "ROOT", tmp_project):
            executor._check_isolation()

    def test_isolation_requires_config_dir(self, executor, tmp_project, monkeypatch, capsys):
        """
        대상: _check_isolation() — 런처 있음 + CLAUDE_CONFIG_DIR 없음
        구분: 예외
        오라클: (종료코드) SystemExit(1), (출력) 런처로 실행하라는 안내.
        """
        self._launcher(tmp_project)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        with patch.object(ex, "ROOT", tmp_project), pytest.raises(SystemExit) as e:
            executor._check_isolation()
        assert e.value.code == 1 and "claude-isolated" in capsys.readouterr().out

    def test_isolation_matches_manifest(self, executor, tmp_project, tmp_path_factory, monkeypatch):
        """
        대상: _check_isolation() — CLAUDE_CONFIG_DIR이 설치 기록의 claude_home과 같음
        구분: 정상
        오라클: (예외부재) 통과한다.
        """
        home = tmp_path_factory.mktemp("iso_home")
        self._launcher(tmp_project, home)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
        with patch.object(ex, "ROOT", tmp_project):
            executor._check_isolation()

    def test_isolation_mismatch_exits(self, executor, tmp_project, tmp_path_factory, monkeypatch):
        """
        대상: _check_isolation() — CLAUDE_CONFIG_DIR이 설치 기록과 다른 글로벌
        구분: 예외
        오라클: (종료코드) SystemExit(1). 다른 독립 환경으로 step이 실행되는 것을 막는다.
        """
        self._launcher(tmp_project, tmp_path_factory.mktemp("expected"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path_factory.mktemp("other")))
        with patch.object(ex, "ROOT", tmp_project), pytest.raises(SystemExit) as e:
            executor._check_isolation()
        assert e.value.code == 1

    def test_lock_acquire_and_release(self, executor):
        """
        대상: _acquire_lock() / _release_lock()
        구분: 정상
        오라클: (상태) 생성된 phases/.running에 phase 이름이 기록되고, release 후 파일이 없다.
        """
        lock = executor._phases_dir / ex.StepExecutor.LOCK_NAME
        executor._acquire_lock()
        assert json.loads(lock.read_text(encoding="utf-8"))["phase"] == "0-mvp"
        executor._release_lock()
        assert not lock.exists()

    def test_lock_exists_refuses_and_keeps_lock(self, executor, tmp_project, monkeypatch):
        """
        대상: run() — 이미 phases/.running 있음 (다른 실행 중)
        구분: 예외
        오라클: (종료코드) SystemExit(1), (상태) 기존 실행 중 표시를 지우지 않는다 (다른 실행을 방해하지 않음).
        """
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        lock = executor._phases_dir / ex.StepExecutor.LOCK_NAME
        lock.write_text('{"phase": "other"}', encoding="utf-8")
        with patch.object(ex, "ROOT", tmp_project), pytest.raises(SystemExit) as e:
            executor.run()
        assert e.value.code == 1 and json.loads(lock.read_text(encoding="utf-8"))["phase"] == "other"

    def test_run_releases_lock_on_exit(self, executor, tmp_project, monkeypatch):
        """
        대상: run() — 실행 중 sys.exit (예: error/blocked로 중단)
        구분: 예외
        오라클: (상태) 종료 코드와 관계없이 phases/.running이 삭제된다 (finally).
        """
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        with patch.object(ex, "ROOT", tmp_project), \
             patch.object(executor, "_check_blockers", side_effect=SystemExit(2)), \
             pytest.raises(SystemExit):
            executor.run()
        assert not (executor._phases_dir / ex.StepExecutor.LOCK_NAME).exists()

    def test_invoke_claude_sets_step_env(self, executor):
        """
        대상: _invoke_claude() — step 세션 환경변수
        구분: 연동
        오라클: (호출) subprocess.run의 env에 HARNESS_STEP=<phase>/step<N>이 있고, 기존 환경변수도 유지된다.
        """
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="{}", stderr="")) as run:
            executor._invoke_claude({"step": 2, "name": "ui"}, "p")
        env = run.call_args[1]["env"]
        assert env["HARNESS_STEP"] == "0-mvp/step2" and "PATH" in env


HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "stop-verify.sh"


@pytest.mark.skipif(not __import__("shutil").which("bash") or not HOOK.exists(), reason="bash 또는 hook 없음")
class TestStopHookDuringRun:
    """stop-verify.sh — execute.py 실행 중(phases/.running) 메인 세션과 step 세션의 분기."""

    @pytest.fixture
    def repo(self, tmp_path):
        """커밋 후 코드 변경이 하나 있는 git 저장소 (Stop hook이 검증 대상으로 보는 상태)."""
        import subprocess as sp
        (tmp_path / "phases").mkdir()
        (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
        for args in (["init", "-q"], ["add", "-A"],
                     ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b"]):
            sp.run(["git", *args], cwd=tmp_path, check=True)
        (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
        return tmp_path

    def _hook(self, repo, **env):
        import os
        import subprocess as sp
        e = {k: v for k, v in os.environ.items() if k != "HARNESS_STEP"}
        e.update(VERIFY="false", **env)  # 검증이 실행되면 반드시 실패하도록
        return sp.run(["bash", str(HOOK)], cwd=repo, input='{"stop_hook_active": false}',
                      text=True, capture_output=True, env=e).returncode

    def test_verifies_when_not_running(self, repo):
        """
        대상: stop-verify.sh — 실행 중 표시 없음
        구분: 정상
        오라클: (종료코드) 2. 코드 변경이 있으면 VERIFY를 실행하고 실패 시 종료를 막는다.
        """
        assert self._hook(repo) == 2

    def test_main_session_skips_during_run(self, repo):
        """
        대상: stop-verify.sh — phases/.running 있음, HARNESS_STEP 없음 (메인 세션)
        구분: 정상
        오라클: (종료코드) 0. step 세션이 작업 중인 코드에 대해 메인 턴을 막지 않는다.
        """
        (repo / "phases" / ".running").write_text("{}", encoding="utf-8")
        assert self._hook(repo) == 0

    def test_step_session_still_verifies(self, repo):
        """
        대상: stop-verify.sh — phases/.running 있음, HARNESS_STEP 있음 (step 세션)
        구분: 연동
        오라클: (종료코드) 2. step 세션의 자가 교정(검증)은 유지된다.
        """
        (repo / "phases" / ".running").write_text("{}", encoding="utf-8")
        assert self._hook(repo, HARNESS_STEP="0-mvp/step0") == 2

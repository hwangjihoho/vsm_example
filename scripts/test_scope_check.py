"""
scope_check.py 테스트 — 임시 git 저장소에서 변경 파일과 허용 범위를 대조하는지 검증한다.

격리 전략
  - 실제 git 저장소를 tmp_path에 만든다 (git CLI 필요). scope_check.ROOT를 프로젝트 폴더로 교체.
  - nested fixture: 프로젝트가 상위 저장소의 하위 폴더인 경우 (예: 2nd_NGV/setup_harness)

주석 규약은 test_execute.py 상단 참고 (대상 / 구분 / 오라클).
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import scope_check as sc

STEP = "# Step 0\n\n## 변경 허용 범위\n\n- `src/*`\n\n## Acceptance Criteria\n"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_repo(repo: Path, project: Path):
    """repo에 git 저장소를 만들고 project에 step 파일을 둔 뒤 기준 커밋을 만든다."""
    (project / "phases" / "t").mkdir(parents=True)
    (project / "src").mkdir()
    (project / "phases" / "t" / "step0.md").write_text(STEP, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")


@pytest.fixture(params=["root", "nested"])
def proj(request, tmp_path):
    """root: 프로젝트 = 저장소 루트 / nested: 프로젝트 = 저장소 하위 폴더 sub/"""
    project = tmp_path if request.param == "root" else tmp_path / "sub"
    project.mkdir(exist_ok=True)
    _make_repo(tmp_path, project)
    return project


def _run(project):
    with patch.object(sc, "ROOT", project), \
         patch("sys.argv", ["scope_check.py", str(project / "phases" / "t" / "step0.md")]):
        try:
            sc.main()
            return 0
        except SystemExit as e:
            return e.code


class TestScopeCheck:
    """scope_check.main() — 저장소 루트 / 하위 폴더 프로젝트 두 경우 모두 (proj fixture parametrize)."""

    def test_change_inside_scope(self, proj):
        """
        대상: main() — 허용 범위(src/*) 안의 새 파일
        구분: 정상
        오라클: (종료코드) 0
        """
        (proj / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        assert _run(proj) == 0

    def test_tracked_change_inside_scope(self, proj):
        """
        대상: main() — 이미 커밋된 파일 수정 (git diff 경로)
        구분: 정상
        오라클: (종료코드) 0. nested에서도 --relative로 경로가 프로젝트 기준이 되어 src/*에 맞는다.
        """
        f = proj / "src" / "b.py"
        f.write_text("y = 1\n", encoding="utf-8")
        _git(proj, "add", "-A")
        _git(proj, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b")
        f.write_text("y = 2\n", encoding="utf-8")
        assert _run(proj) == 0

    def test_change_outside_scope(self, proj, capsys):
        """
        대상: main() — 허용 범위 밖 파일
        구분: 탐지
        오라클: (종료코드) 1, (출력) 위반 파일명이 출력된다.
        """
        (proj / "other.py").write_text("z\n", encoding="utf-8")
        assert _run(proj) == 1
        assert "other.py" in capsys.readouterr().out

    def test_phases_always_allowed(self, proj):
        """
        대상: main() — phases/ 변경 (ALWAYS_ALLOWED)
        구분: 정상
        오라클: (종료코드) 0
        """
        (proj / "phases" / "t" / "index.json").write_text("{}", encoding="utf-8")
        assert _run(proj) == 0

    def test_missing_section_exits_2(self, proj):
        """
        대상: main() — step 파일에 변경 허용 범위 섹션 없음
        구분: 예외
        오라클: (종료코드) 2
        """
        (proj / "phases" / "t" / "step0.md").write_text("# Step 0\n", encoding="utf-8")
        assert _run(proj) == 2

    def test_trailing_slash_allows_directory(self, proj):
        """
        대상: main() — `/`로 끝나는 허용 패턴(`fixtures/loop/`) 아래 하위 폴더 파일
        구분: 정상
        오라클: (종료코드) 0. `/`로 끝나는 패턴은 디렉토리 접두사로 읽는다.
        """
        (proj / "phases" / "t" / "step0.md").write_text(
            STEP.replace("`src/*`", "`fixtures/loop/`"), encoding="utf-8")
        (proj / "fixtures" / "loop" / "a").mkdir(parents=True)
        (proj / "fixtures" / "loop" / "a" / "b.xml").write_text("<x/>\n", encoding="utf-8")
        assert _run(proj) == 0

    def test_trailing_slash_is_not_name_prefix(self, proj, capsys):
        """
        대상: main() — `fixtures/loop/` 패턴과 이름만 앞부분이 같은 파일(`fixtures/loopy.txt`)
        구분: 경계
        오라클: (종료코드) 1. 디렉토리 경계에서만 맞는다.
        """
        (proj / "phases" / "t" / "step0.md").write_text(
            STEP.replace("`src/*`", "`fixtures/loop/`"), encoding="utf-8")
        (proj / "fixtures").mkdir()
        (proj / "fixtures" / "loopy.txt").write_text("x\n", encoding="utf-8")
        assert _run(proj) == 1
        assert "fixtures/loopy.txt" in capsys.readouterr().out


def test_nested_ignores_changes_outside_project(tmp_path):
    """
    대상: main() — 하위 폴더 프로젝트에서, 프로젝트 밖(상위 저장소)의 추적 파일 변경
    구분: 경계
    오라클: (종료코드) 0. 프로젝트 밖 변경은 이 step의 범위 판정 대상이 아니다.
    """
    project = tmp_path / "sub"
    project.mkdir()
    (tmp_path / "outside.txt").write_text("a\n", encoding="utf-8")
    _make_repo(tmp_path, project)
    (tmp_path / "outside.txt").write_text("b\n", encoding="utf-8")
    assert _run(project) == 0

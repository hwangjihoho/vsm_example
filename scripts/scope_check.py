#!/usr/bin/env python3
"""
Scope Lock 검증 — step 파일의 "## 변경 허용 범위"에 없는 파일이 변경되었는지 확인한다.

Usage:
    python3 scripts/scope_check.py phases/<task>/step<N>.md [--base HEAD]

Exit code: 0 = 범위 내, 1 = 범위 이탈, 2 = 사용 오류

판정 방식
    변경 파일 = git diff --name-only --relative <base> (staged + unstaged) + 추적되지 않은 파일
               두 목록 모두 프로젝트 루트(ROOT) 기준 경로 — 상위 저장소의 하위 폴더여도 동작
    허용 패턴 = step 파일 "## 변경 허용 범위"의 백틱 경로 + ALWAYS_ALLOWED
    허용 패턴 어디에도 맞지 않는 변경 파일이 하나라도 있으면 위반 (exit 1)

사용 시점
    step AC의 마지막 명령으로 넣는다. Claude가 커밋하기 전에 실행되므로 base 기본값 HEAD로
    이번 step의 작업 트리 변경만 본다.

주의
    fnmatch의 '*'는 '/'도 매칭한다. 즉 `src/auth/*`는 `src/auth/a/b.py`도 허용한다.
    `/`로 끝나는 패턴(`tests/fixtures/`)은 디렉토리로 읽어 그 아래 파일을 모두 허용한다.
"""

import argparse
import fnmatch
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECTION = "## 변경 허용 범위"
# 하네스 메타데이터와 제안 백로그는 어느 step에서나 변경을 허용한다.
ALWAYS_ALLOWED = ["phases/*", "docs/PROPOSALS.md"]


def load_allowed(step_file: Path) -> list[str]:
    """step 파일의 "## 변경 허용 범위" 섹션에서 백틱으로 감싼 목록 항목(- `패턴`)을 읽는다. 섹션이 없으면 exit 2."""
    text = step_file.read_text(encoding="utf-8")
    if SECTION not in text:
        print(f"ERROR: {step_file}에 '{SECTION}' 섹션이 없습니다.")
        sys.exit(2)
    body = text.split(SECTION, 1)[1]
    body = re.split(r"\n## ", body, maxsplit=1)[0]  # 다음 "## " 섹션 전까지만
    return re.findall(r"^\s*-\s*`([^`]+)`", body, flags=re.M)


def changed_files(base: str) -> list[str]:
    """base 대비 변경된 추적 파일 + 추적되지 않은 파일(.gitignore 제외) 목록. git 실패 시 exit 2."""
    def git(*args):
        r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"ERROR: git {' '.join(args)} 실패: {r.stderr.strip()}")
            sys.exit(2)
        return [line for line in r.stdout.splitlines() if line]

    # --relative: 프로젝트가 상위 저장소의 하위 폴더여도 경로를 프로젝트 기준으로 받고, 프로젝트 밖 변경은 제외한다.
    tracked = git("diff", "--name-only", "--relative", base)
    untracked = git("ls-files", "--others", "--exclude-standard")
    return sorted(set(tracked + untracked))


def matches(path: str, pattern: str) -> bool:
    """`/`로 끝나는 패턴은 그 디렉토리 아래 전부, 그 밖은 fnmatch로 판정한다."""
    if pattern.endswith("/"):
        return path.startswith(pattern)
    return fnmatch.fnmatch(path, pattern)


def main():
    """CLI 진입점: 허용 패턴과 변경 파일을 대조해 위반 목록을 출력한다."""
    p = argparse.ArgumentParser()
    p.add_argument("step_file")
    p.add_argument("--base", default="HEAD")
    args = p.parse_args()

    allowed = load_allowed(Path(args.step_file)) + ALWAYS_ALLOWED
    violations = [
        f for f in changed_files(args.base)
        if not any(matches(f, pat) for pat in allowed)
    ]

    if violations:
        print("SCOPE VIOLATION — 허용 범위 밖 파일이 변경되었습니다:")
        for f in violations:
            print(f"  - {f}")
        print("범위 밖 변경은 되돌리고, 필요하면 docs/PROPOSALS.md에 제안으로 기록하세요.")
        sys.exit(1)

    print("Scope OK")


if __name__ == "__main__":
    main()

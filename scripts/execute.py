#!/usr/bin/env python3
"""
Harness Step Executor — phase 내 step을 순차 실행하고 자가 교정한다.

Usage:
    python3 scripts/execute.py <phase-dir> [--push]

실행 흐름 (run)
    _check_isolation  독립 설치 프로젝트인데 CLAUDE_CONFIG_DIR이 없거나 다르면 중단
    _acquire_lock     phases/.running 생성 (이미 있으면 중단). 끝날 때 항상 삭제
    _check_blockers   이전 실행이 error/blocked로 끝났으면 중단
    _checkout_branch  feat-{phase} 브랜치 준비
    _load_guardrails  CLAUDE.md + docs/*.md 결합 (PROPOSALS.md 제외)
    _execute_all_steps
        └ _execute_single_step  pending step 하나를 새 Claude 세션(claude -p)으로 실행
            ├ completed → 2단계 커밋 → Harness Map 갱신 → 다음 step
            ├ blocked   → 즉시 중단 (exit 2)
            └ 그 외     → 에러 메시지를 피드백해 재시도, MAX_RETRIES 초과 시 error (exit 1)
    _finalize         phase completed 기록, 커밋, (--push) push, Harness Map 갱신

상태 프로토콜
    step의 status는 Claude 세션이 phases/{phase}/index.json에 직접 쓴다
    (completed + summary / error + error_message / blocked + blocked_reason).
    executor는 세션 종료 후 그 값을 읽어 분기하고, 타임스탬프만 덧붙인다.

파일
    phases/index.json               phase 목록과 상태 (top index)
    phases/{phase}/index.json       step 목록과 상태
    phases/{phase}/step{N}.md       step 지시서 (Claude 프롬프트 본문)
    phases/{phase}/step{N}-output.json  claude CLI 원본 출력 (gitignore)

종료 코드
    0 정상 완료 / 1 error 또는 설정 오류 / 2 blocked (사람 조치 필요)
"""

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import types
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent


@contextlib.contextmanager
def progress_indicator(label: str):
    """터미널 진행 표시기. with 문으로 사용하며 .elapsed 로 경과 시간을 읽는다."""
    frames = "◐◓◑◒"
    stop = threading.Event()
    t0 = time.monotonic()

    def _animate():
        idx = 0
        while not stop.wait(0.12):
            sec = int(time.monotonic() - t0)
            sys.stderr.write(f"\r{frames[idx % len(frames)]} {label} [{sec}s]")
            sys.stderr.flush()
            idx += 1
        sys.stderr.write("\r" + " " * (len(label) + 20) + "\r")
        sys.stderr.flush()

    th = threading.Thread(target=_animate, daemon=True)
    th.start()
    info = types.SimpleNamespace(elapsed=0.0)
    try:
        yield info
    finally:
        stop.set()
        th.join()
        info.elapsed = time.monotonic() - t0


class StepExecutor:
    """Phase 디렉토리 안의 step들을 순차 실행하는 하네스."""

    # step 하나당 Claude 세션 최대 시도 횟수 (첫 시도 포함).
    MAX_RETRIES = 3
    # 승인 전 제안이 step 프롬프트에 노출되면 범위 확장을 유도하므로 주입하지 않는다.
    GUARDRAIL_EXCLUDE = {"PROPOSALS.md"}
    # Scope Lock의 기준 문서라서 선택 주입 모드에서도 항상 포함한다.
    ALWAYS_DOCS = {"PRD.md"}
    # 2단계 커밋 메시지: 코드 변경(feat)과 하네스 메타데이터(chore)를 분리해 리뷰/revert를 쉽게 한다.
    FEAT_MSG = "feat({phase}): step {num} — {name}"
    CHORE_MSG = "chore({phase}): step {num} output"
    # 모든 타임스탬프는 KST로 기록한다.
    TZ = timezone(timedelta(hours=9))
    # 실행 중 표시 파일 (phases/ 아래, gitignore 대상). 메인 세션의 Stop hook이 이 파일을 보고 검증을 건너뛴다.
    LOCK_NAME = ".running"
    # step 세션에 넘기는 환경변수. Stop hook이 메인 세션과 step 세션을 구분하는 데 쓴다.
    STEP_ENV = "HARNESS_STEP"
    LAUNCHERS = ("claude-isolated.cmd", "claude-isolated.sh")

    def __init__(self, phase_dir_name: str, *, auto_push: bool = False):
        """phase 디렉토리와 index.json을 검증하고 메타데이터를 읽는다. 없으면 exit 1."""
        self._root = str(ROOT)
        self._phases_dir = ROOT / "phases"
        self._phase_dir = self._phases_dir / phase_dir_name
        self._phase_dir_name = phase_dir_name
        self._top_index_file = self._phases_dir / "index.json"
        self._auto_push = auto_push

        if not self._phase_dir.is_dir():
            print(f"ERROR: {self._phase_dir} not found")
            sys.exit(1)

        self._index_file = self._phase_dir / "index.json"
        if not self._index_file.exists():
            print(f"ERROR: {self._index_file} not found")
            sys.exit(1)

        idx = self._read_json(self._index_file)
        self._project = idx.get("project", "project")
        self._phase_name = idx.get("phase", phase_dir_name)
        self._total = len(idx["steps"])

    def run(self):
        """phase 전체 실행 진입점. 순서는 모듈 docstring의 "실행 흐름" 참고.

        독립 환경 점검 → 실행 중 표시 생성 → 실행 → (정상·오류·중단 모두) 실행 중 표시 삭제.
        """
        self._check_isolation()
        self._acquire_lock()
        try:
            self._print_header()
            self._check_blockers()
            self._checkout_branch()
            guardrails = self._load_guardrails()
            self._ensure_created_at()
            self._execute_all_steps(guardrails)
            self._finalize()
        finally:  # sys.exit(1/2)도 여기를 거친다
            self._release_lock()

    # --- 실행 환경 점검 / 실행 중 표시 ---

    def _check_isolation(self):
        """독립 설치 프로젝트(런처 있음)인데 독립 환경이 아니면 실행하지 않는다 (exit 1).

        step 세션은 이 프로세스의 환경변수를 물려받는다. CLAUDE_CONFIG_DIR이 없으면 개인 ~/.claude의
        hook·설정으로 step이 실행되므로, 런처로 연 세션(또는 런처 경유 명령)에서만 실행되게 한다.
        설치 기록(.harness-kit/manifest.json)의 claude_home과 다른 글로벌이어도 중단한다.
        """
        if not any((ROOT / name).exists() for name in self.LAUNCHERS):
            return
        cfg = os.environ.get("CLAUDE_CONFIG_DIR")
        if not cfg:
            print("ERROR: 독립 환경으로 설치된 프로젝트인데 CLAUDE_CONFIG_DIR이 없다.")
            print("  런처로 실행하라: claude-isolated.cmd py scripts\\execute.py <phase>  (또는 런처로 연 Claude 세션에서)")
            sys.exit(1)
        try:
            manifest = json.loads((ROOT / ".harness-kit" / "manifest.json").read_text(encoding="utf-8"))
            expected = manifest.get("claude_home")
        except (OSError, json.JSONDecodeError):
            expected = None
        if expected and Path(cfg).resolve() != Path(expected).resolve():
            print(f"ERROR: CLAUDE_CONFIG_DIR({cfg})이 이 프로젝트의 독립 글로벌({expected})과 다르다.")
            sys.exit(1)

    def _acquire_lock(self):
        """phases/.running 생성. 이미 있으면 다른 실행 중이거나 이전 실행의 비정상 종료 흔적이므로 중단."""
        lock = self._phases_dir / self.LOCK_NAME
        if lock.exists():
            print(f"ERROR: {lock} 가 있다. 다른 phase가 실행 중이거나 이전 실행이 비정상 종료됐다.")
            print("  실행 중인 execute.py가 없다면 이 파일을 지우고 다시 실행하라.")
            sys.exit(1)
        lock.write_text(json.dumps(
            {"phase": self._phase_dir_name, "pid": os.getpid(), "started_at": self._stamp()},
            ensure_ascii=False), encoding="utf-8")

    def _release_lock(self):
        try:
            (self._phases_dir / self.LOCK_NAME).unlink()
        except FileNotFoundError:
            pass

    # --- timestamps ---

    def _stamp(self) -> str:
        """KST ISO 형식 타임스탬프 (예: 2026-10-24T18:00:00+0900)."""
        return datetime.now(self.TZ).strftime("%Y-%m-%dT%H:%M:%S%z")

    # --- JSON I/O ---

    @staticmethod
    def _read_json(p: Path) -> dict:
        return json.loads(p.read_text(encoding="utf-8"))

    @staticmethod
    def _write_json(p: Path, data: dict):
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # --- git ---

    def _run_git(self, *args) -> subprocess.CompletedProcess:
        """프로젝트 루트에서 git을 실행한다. 실패해도 예외를 던지지 않고 returncode로 판단한다."""
        cmd = ["git"] + list(args)
        return subprocess.run(cmd, cwd=self._root, capture_output=True, text=True)

    def _checkout_branch(self):
        """feat-{phase} 브랜치로 이동한다. 이미 그 브랜치면 그대로, 있으면 checkout, 없으면 생성.

        git repo가 아니거나 checkout이 실패하면(예: 커밋 안 된 변경) exit 1.
        """
        branch = f"feat-{self._phase_name}"

        r = self._run_git("rev-parse", "--abbrev-ref", "HEAD")
        if r.returncode != 0:
            print("  ERROR: git을 사용할 수 없거나 git repo가 아닙니다.")
            print(f"  {r.stderr.strip()}")
            sys.exit(1)

        if r.stdout.strip() == branch:
            return

        r = self._run_git("rev-parse", "--verify", branch)
        r = self._run_git("checkout", branch) if r.returncode == 0 else self._run_git("checkout", "-b", branch)

        if r.returncode != 0:
            print(f"  ERROR: 브랜치 '{branch}' checkout 실패.")
            print(f"  {r.stderr.strip()}")
            print("  Hint: 변경사항을 stash하거나 commit한 후 다시 시도하세요.")
            sys.exit(1)

        print(f"  Branch: {branch}")

    def _commit_step(self, step_num: int, step_name: str):
        """step 결과를 2단계로 커밋한다.

        stage는 `git add -A -- .`로 프로젝트 폴더(ROOT) 안만 대상으로 한다. 프로젝트가 상위 저장소의
        하위 폴더여도 저장소의 다른 폴더 변경이 step 커밋에 섞이지 않게 하기 위해서다.

        1) feat: output.json과 index.json을 stage에서 뺀 코드 변경만 커밋 (변경 없으면 생략)
        2) chore: 남은 메타데이터(index.json 등) 커밋
        커밋 실패는 경고만 출력하고 실행을 계속한다.
        """
        output_rel = f"phases/{self._phase_dir_name}/step{step_num}-output.json"
        index_rel = f"phases/{self._phase_dir_name}/index.json"

        self._run_git("add", "-A", "--", ".")
        self._run_git("reset", "HEAD", "--", output_rel)
        self._run_git("reset", "HEAD", "--", index_rel)

        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = self.FEAT_MSG.format(phase=self._phase_name, num=step_num, name=step_name)
            r = self._run_git("commit", "-m", msg)
            if r.returncode == 0:
                print(f"  Commit: {msg}")
            else:
                print(f"  WARN: 코드 커밋 실패: {r.stderr.strip()}")

        self._run_git("add", "-A", "--", ".")
        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = self.CHORE_MSG.format(phase=self._phase_name, num=step_num)
            r = self._run_git("commit", "-m", msg)
            if r.returncode != 0:
                print(f"  WARN: housekeeping 커밋 실패: {r.stderr.strip()}")

    # --- top-level index ---

    def _update_top_index(self, status: str):
        """phases/index.json에서 이 phase의 status와 해당 타임스탬프를 갱신한다.

        top index 파일이 없거나 목록에 이 phase가 없으면 아무것도 하지 않는다.
        """
        if not self._top_index_file.exists():
            return
        top = self._read_json(self._top_index_file)
        ts = self._stamp()
        for phase in top.get("phases", []):
            if phase.get("dir") == self._phase_dir_name:
                phase["status"] = status
                ts_key = {"completed": "completed_at", "error": "failed_at", "blocked": "blocked_at"}.get(status)
                if ts_key:
                    phase[ts_key] = ts
                break
        self._write_json(self._top_index_file, top)

    # --- harness map ---

    def _refresh_map(self):
        """harness_map.html을 재생성한다. 실패해도 step 실행은 계속한다."""
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import harness_map
            out, _ = harness_map.generate(ROOT)
            print(f"  Map: {out.name} updated")
        except Exception as e:  # noqa: BLE001 — map은 부가 기능
            print(f"  WARN: harness map 갱신 실패: {e}")

    # --- guardrails & context ---

    def _load_guardrails(self, only: Optional[list[str]] = None) -> str:
        """CLAUDE.md + docs/*.md를 가드레일로 합친다.

        only가 주어지면 그 문서와 ALWAYS_DOCS만 포함한다 (step의 "참조 문서" 선택 주입).
        GUARDRAIL_EXCLUDE 문서는 항상 제외한다.
        """
        sections = []
        claude_md = ROOT / "CLAUDE.md"
        if claude_md.exists():
            sections.append(f"## 프로젝트 규칙 (CLAUDE.md)\n\n{claude_md.read_text(encoding="utf-8")}")
        docs_dir = ROOT / "docs"
        if docs_dir.is_dir():
            for doc in sorted(docs_dir.glob("*.md")):
                if doc.name in self.GUARDRAIL_EXCLUDE:
                    continue
                if only is not None and doc.name not in self.ALWAYS_DOCS and doc.name not in only:
                    continue
                sections.append(f"## {doc.stem}\n\n{doc.read_text(encoding="utf-8")}")
        return "\n\n---\n\n".join(sections) if sections else ""

    def _step_docs(self, step_num: int) -> Optional[list[str]]:
        """step 파일의 "## 참조 문서" 섹션에 적힌 문서 파일명. 섹션이 없으면 None (전체 주입)."""
        step_file = self._phase_dir / f"step{step_num}.md"
        if not step_file.exists():
            return None
        m = re.search(r"^## 참조 문서[ \t]*$(.*?)(?=^## |\Z)", step_file.read_text(encoding="utf-8"), re.M | re.S)
        if not m:
            return None
        return [Path(p).name for p in re.findall(r"^\s*-\s*`([^`]+)`", m.group(1), re.M)]

    @staticmethod
    def _build_step_context(index: dict) -> str:
        """completed이고 summary가 있는 step을 "## 이전 Step 산출물" 목록으로 만든다.

        step마다 새 세션이 시작되므로, 이 요약이 이전 step의 결과를 전달하는 유일한 통로다.
        """
        lines = [
            f"- Step {s['step']} ({s['name']}): {s['summary']}"
            for s in index["steps"]
            if s["status"] == "completed" and s.get("summary")
        ]
        if not lines:
            return ""
        return "## 이전 Step 산출물\n\n" + "\n".join(lines) + "\n\n"

    def _build_preamble(self, guardrails: str, step_context: str,
                        prev_error: Optional[str] = None) -> str:
        """step 프롬프트 머리말을 만든다. step{N}.md 본문 앞에 붙는다.

        구성: 역할 → 가드레일(CLAUDE.md + docs) → 이전 step 산출물
              → (재시도 시) 이전 에러 → 작업 규칙(범위, 제안 기록, status 갱신, 커밋)
        """
        commit_example = self.FEAT_MSG.format(
            phase=self._phase_name, num="N", name="<step-name>"
        )
        retry_section = ""
        if prev_error:
            retry_section = (
                f"\n## ⚠ 이전 시도 실패 — 아래 에러를 반드시 참고하여 수정하라\n\n"
                f"{prev_error}\n\n---\n\n"
            )
        return (
            f"당신은 {self._project} 프로젝트의 개발자입니다. 아래 step을 수행하세요.\n\n"
            f"{guardrails}\n\n---\n\n"
            f"{step_context}{retry_section}"
            f"## 작업 규칙\n\n"
            f"1. 이전 step에서 작성된 코드를 확인하고 일관성을 유지하라.\n"
            f"2. 이 step에 명시된 작업만 수행하라. 추가 기능이나 파일을 만들지 마라.\n"
            f"   - 범위 밖 아이디어는 구현하지 말고 docs/PROPOSALS.md에 `- [ ]` 항목으로만 추가하라.\n"
            f"   - 재시도 시에도 실패 원인만 고쳐라. 구조 변경이나 기능 추가로 우회하지 마라.\n"
            f"3. 기존 테스트를 깨뜨리지 마라.\n"
            f"4. AC(Acceptance Criteria) 검증을 직접 실행하라.\n"
            f"5. /phases/{self._phase_dir_name}/index.json의 해당 step status를 업데이트하라:\n"
            f"   - AC 통과 → \"completed\" + \"summary\" 필드에 이 step의 산출물을 한 줄로 요약\n"
            f"   - {self.MAX_RETRIES}회 수정 시도 후에도 실패 → \"error\" + \"error_message\" 기록\n"
            f"   - 사용자 개입이 필요한 경우 (API 키, 인증, 수동 설정 등) → \"blocked\" + \"blocked_reason\" 기록 후 즉시 중단\n"
            f"6. 모든 변경사항을 커밋하라:\n"
            f"   {commit_example}\n\n---\n\n"
        )

    # --- Claude 호출 ---

    def _invoke_claude(self, step: dict, preamble: str) -> dict:
        """step 하나를 새 Claude 세션(`claude -p`)으로 실행하고 원본 출력을 step{N}-output.json에 저장한다.

        - --dangerously-skip-permissions: 무인 실행이라 권한 확인을 끈다.
          따라서 위험 명령 차단 hook과 scope_check가 실제 안전장치다.
        - timeout 1800초. 비정상 종료는 경고만 출력하고, 판정은 index.json status로 한다.
        - step 파일이 없으면 Claude를 호출하지 않고 exit 1.
        - 환경변수 HARNESS_STEP=<phase>/step<N>을 넘겨 Stop hook이 step 세션임을 알게 한다.
        """
        step_num, step_name = step["step"], step["name"]
        step_file = self._phase_dir / f"step{step_num}.md"

        if not step_file.exists():
            print(f"  ERROR: {step_file} not found")
            sys.exit(1)

        prompt = preamble + step_file.read_text(encoding="utf-8")
        result = subprocess.run(
            ["claude", "-p", "--dangerously-skip-permissions", "--output-format", "json", prompt],
            cwd=self._root, capture_output=True, text=True, timeout=1800,
            env={**os.environ, self.STEP_ENV: f"{self._phase_dir_name}/step{step_num}"},
        )

        if result.returncode != 0:
            print(f"\n  WARN: Claude가 비정상 종료됨 (code {result.returncode})")
            if result.stderr:
                print(f"  stderr: {result.stderr[:500]}")

        output = {
            "step": step_num, "name": step_name,
            "exitCode": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr,
        }
        out_path = self._phase_dir / f"step{step_num}-output.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        return output

    # --- 헤더 & 검증 ---

    def _print_header(self):
        print(f"\n{'='*60}")
        print("  Harness Step Executor")
        print(f"  Phase: {self._phase_name} | Steps: {self._total}")
        if self._auto_push:
            print("  Auto-push: enabled")
        print(f"{'='*60}")

    def _check_blockers(self):
        """이전 실행이 error/blocked로 끝났으면 재실행을 막는다 (error → exit 1, blocked → exit 2).

        뒤에서부터 pending이 아닌 첫 step까지만 본다. 사람이 해당 step을 pending으로
        되돌려야 다시 실행된다.
        """
        index = self._read_json(self._index_file)
        for s in reversed(index["steps"]):
            if s["status"] == "error":
                print(f"\n  ✗ Step {s['step']} ({s['name']}) failed.")
                print(f"  Error: {s.get('error_message', 'unknown')}")
                print("  Fix and reset status to 'pending' to retry.")
                sys.exit(1)
            if s["status"] == "blocked":
                print(f"\n  ⏸ Step {s['step']} ({s['name']}) blocked.")
                print(f"  Reason: {s.get('blocked_reason', 'unknown')}")
                print("  Resolve and reset status to 'pending' to retry.")
                sys.exit(2)
            if s["status"] != "pending":
                break

    def _ensure_created_at(self):
        """phase index.json에 created_at을 최초 실행 시 한 번만 기록한다."""
        index = self._read_json(self._index_file)
        if "created_at" not in index:
            index["created_at"] = self._stamp()
            self._write_json(self._index_file, index)

    # --- 실행 루프 ---

    def _execute_single_step(self, step: dict, guardrails: str) -> bool:
        """단일 step 실행 (재시도 포함). 완료되면 True를 반환하고, error/blocked면 프로세스를 종료한다.

        step에 "## 참조 문서" 섹션이 있으면 인자로 받은 전체 가드레일 대신
        CLAUDE.md + PRD.md + 지정 문서만 주입한다.
        """
        step_num, step_name = step["step"], step["name"]
        done = sum(1 for s in self._read_json(self._index_file)["steps"] if s["status"] == "completed")
        prev_error = None
        step_docs = self._step_docs(step_num)
        if step_docs is not None:
            guardrails = self._load_guardrails(step_docs)

        for attempt in range(1, self.MAX_RETRIES + 1):
            # 매 시도마다 index를 다시 읽는다 (이전 시도에서 Claude가 파일을 바꿨을 수 있음).
            index = self._read_json(self._index_file)
            step_context = self._build_step_context(index)
            preamble = self._build_preamble(guardrails, step_context, prev_error)

            tag = f"Step {step_num}/{self._total - 1} ({done} done): {step_name}"
            if attempt > 1:
                tag += f" [retry {attempt}/{self.MAX_RETRIES}]"

            with progress_indicator(tag) as pi:
                self._invoke_claude(step, preamble)
                elapsed = int(pi.elapsed)

            # 판정은 Claude 세션이 index.json에 남긴 status로만 한다 (CLI 종료 코드는 보지 않음).
            # status를 갱신하지 않고 끝나면 pending 그대로 → 실패로 보고 재시도한다.
            index = self._read_json(self._index_file)
            status = next((s.get("status", "pending") for s in index["steps"] if s["step"] == step_num), "pending")
            ts = self._stamp()

            if status == "completed":
                for s in index["steps"]:
                    if s["step"] == step_num:
                        s["completed_at"] = ts
                self._write_json(self._index_file, index)
                self._commit_step(step_num, step_name)
                print(f"  ✓ Step {step_num}: {step_name} [{elapsed}s]")
                self._refresh_map()
                return True

            if status == "blocked":
                for s in index["steps"]:
                    if s["step"] == step_num:
                        s["blocked_at"] = ts
                self._write_json(self._index_file, index)
                reason = next((s.get("blocked_reason", "") for s in index["steps"] if s["step"] == step_num), "")
                print(f"  ⏸ Step {step_num}: {step_name} blocked [{elapsed}s]")
                print(f"    Reason: {reason}")
                self._update_top_index("blocked")
                self._refresh_map()
                sys.exit(2)

            err_msg = next(
                (s.get("error_message", "Step did not update status") for s in index["steps"] if s["step"] == step_num),
                "Step did not update status",
            )

            # 재시도: status를 pending으로 되돌리고, 에러 메시지를 다음 프롬프트에 피드백한다.
            if attempt < self.MAX_RETRIES:
                for s in index["steps"]:
                    if s["step"] == step_num:
                        s["status"] = "pending"
                        s.pop("error_message", None)
                self._write_json(self._index_file, index)
                prev_error = err_msg
                print(f"  ↻ Step {step_num}: retry {attempt}/{self.MAX_RETRIES} — {err_msg}")
            else:
                # 마지막 시도까지 실패: error로 확정하고, 실패 상태도 커밋해 기록을 남긴다.
                for s in index["steps"]:
                    if s["step"] == step_num:
                        s["status"] = "error"
                        s["error_message"] = f"[{self.MAX_RETRIES}회 시도 후 실패] {err_msg}"
                        s["failed_at"] = ts
                self._write_json(self._index_file, index)
                self._commit_step(step_num, step_name)
                print(f"  ✗ Step {step_num}: {step_name} failed after {self.MAX_RETRIES} attempts [{elapsed}s]")
                print(f"    Error: {err_msg}")
                self._update_top_index("error")
                self._refresh_map()
                sys.exit(1)

        return False  # unreachable

    def _execute_all_steps(self, guardrails: str):
        """pending step이 없을 때까지 앞에서부터 하나씩 실행한다. 처음 시작하는 step에는 started_at을 기록한다."""
        while True:
            index = self._read_json(self._index_file)
            pending = next((s for s in index["steps"] if s["status"] == "pending"), None)
            if pending is None:
                print("\n  All steps completed!")
                return

            step_num = pending["step"]
            for s in index["steps"]:
                if s["step"] == step_num and "started_at" not in s:
                    s["started_at"] = self._stamp()
                    self._write_json(self._index_file, index)
                    break

            self._execute_single_step(pending, guardrails)

    def _finalize(self):
        """모든 step 완료 후: completed_at 기록 → top index completed → Harness Map 갱신 → 커밋 → (--push) push."""
        index = self._read_json(self._index_file)
        index["completed_at"] = self._stamp()
        self._write_json(self._index_file, index)
        self._update_top_index("completed")
        self._refresh_map()

        self._run_git("add", "-A", "--", ".")
        if self._run_git("diff", "--cached", "--quiet").returncode != 0:
            msg = f"chore({self._phase_name}): mark phase completed"
            r = self._run_git("commit", "-m", msg)
            if r.returncode == 0:
                print(f"  ✓ {msg}")

        if self._auto_push:
            branch = f"feat-{self._phase_name}"
            r = self._run_git("push", "-u", "origin", branch)
            if r.returncode != 0:
                print(f"\n  ERROR: git push 실패: {r.stderr.strip()}")
                sys.exit(1)
            print(f"  ✓ Pushed to origin/{branch}")

        print(f"\n{'='*60}")
        print(f"  Phase '{self._phase_name}' completed!")
        print(f"{'='*60}")


def main():
    """CLI 진입점. phase 디렉토리명과 --push 옵션을 받는다."""
    parser = argparse.ArgumentParser(description="Harness Step Executor")
    parser.add_argument("phase_dir", help="Phase directory name (e.g. 0-mvp)")
    parser.add_argument("--push", action="store_true", help="Push branch after completion")
    args = parser.parse_args()

    StepExecutor(args.phase_dir, auto_push=args.push).run()


if __name__ == "__main__":
    main()

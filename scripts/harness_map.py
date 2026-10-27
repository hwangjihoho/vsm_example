#!/usr/bin/env python3
"""
Harness Map — 저장소의 하네스 구조를 단일 HTML 문서로 출력한다.

섹션: Summary / Layers / Flow / Trace / Scope / Proposals / Health

Usage:
    python3 scripts/harness_map.py [-o harness_map.html] [--with-global | --global-dir DIR] [--strict]

    --with-global  글로벌 레이어 포함. 위치는 CLAUDE_CONFIG_DIR, 없으면 ~/.claude (Claude Code와 같은 규칙)
    --global-dir   글로벌 위치를 직접 지정 (독립 환경 확인용)

Exit code: 0 = 생성 완료, 1 = --strict이고 Health error 존재

파이프라인
    collect(root)        저장소 파일 → model dict (파싱만, 판정 없음)
    check_health(model)  model → [{level, target, message}] (판정만)
    render(model, health) → 단일 HTML 문자열 (외부 리소스 없음)
    generate()           위 세 단계 + 파일 쓰기. execute.py가 step 종료 때마다 호출한다.

입력 파일과 읽는 위치
    CLAUDE.md                      프로젝트명("# 프로젝트: X"), CRITICAL 규칙
    .claude/settings.json          hooks (command가 참조한 .sh/.py 본문까지 읽음)
    .claude/commands/*.md          슬래시 커맨드 (첫 줄을 설명으로 사용)
    docs/PRD.md                    "| R1 | 요구사항 | AC |" 표
    docs/ADR.md                    "### ADR-001: 제목" + "**Status**: ..."
    docs/ARCHITECTURE.md           "## 디렉토리 구조" 코드 블록 트리
    docs/PROPOSALS.md              "- [ ] P1 | 출처 | 제안 | 이유" (인용문 > 줄은 무시)
    phases/index.json, phases/*/index.json, phases/*/step{N}.md

step{N}.md에서 읽는 섹션 (SEC_* 상수)
    ## 요구사항 ID     R-ID 목록
    ## 변경 허용 범위   백틱으로 감싼 경로/glob 목록
    ## Acceptance Criteria  코드 블록(```)이 있어야 AC로 인정
    ## 근거 ADR        ADR-ID 목록
    ## 참조 문서        백틱 경로 목록 (섹션이 없으면 None = 전체 주입)

파싱은 관대하게 한다: 파일이나 섹션이 없으면 빈 값으로 두고, 누락 판정은 check_health가 한다.
"""

import argparse
import fnmatch
import html
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
TZ = timezone(timedelta(hours=9))
DEFAULT_OUT = "harness_map.html"          # 프로젝트 루트 기준 출력 파일 (gitignore 대상)
STATUSES = ("completed", "pending", "error", "blocked")  # execute.py step 상태와 동일
# Health level 정렬 순서. error: 하네스가 의도대로 동작하지 않음 / warn: 동작하지만 Guard가 약함 / info: 참고
LEVEL_ORDER = {"error": 0, "warn": 1, "info": 2}

# step{N}.md / ARCHITECTURE.md에서 찾는 "## 섹션" 제목. 템플릿(harness.md)과 반드시 일치해야 한다.

SEC_REQ = "요구사항 ID"
SEC_SCOPE = "변경 허용 범위"
SEC_AC = "Acceptance Criteria"
SEC_ADR = "근거 ADR"
SEC_DOCS = "참조 문서"
SEC_TREE = "디렉토리 구조"


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _read(p: Path) -> str:
    """파일이 있으면 UTF-8 텍스트를, 없으면 빈 문자열을 반환한다."""
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def _read_json(p: Path) -> dict:
    """JSON을 읽는다. 파일이 없거나 깨졌으면 빈 dict (Map 생성이 멈추지 않도록)."""
    try:
        return json.loads(_read(p)) if p.is_file() else {}
    except json.JSONDecodeError:
        return {}


def _section(text: str, title: str) -> Optional[str]:
    """'## {title}' 섹션 본문을 반환한다. 섹션이 없으면 None."""
    m = re.search(rf"^## {re.escape(title)}[ \t]*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1) if m else None


def _first_line(text: str) -> str:
    """첫 번째 비어 있지 않은 줄에서 앞의 '#'과 공백을 뗀 문자열 (문서 제목/커맨드 설명용)."""
    return next((ln.strip().lstrip("# ").strip() for ln in text.splitlines() if ln.strip()), "")


# ---------------------------------------------------------------------------
# Collect
# ---------------------------------------------------------------------------

def _resolve_script(token: str, root: Path, global_dir: Optional[Path]) -> Path:
    """hook command 안의 스크립트 경로를 실제 파일 경로로 바꾼다.

    ~/.claude/...          → global_dir 기준 (테스트에서 글로벌 디렉토리를 바꿀 수 있도록)
    ~/...                  → 홈 디렉토리
    $CLAUDE_PROJECT_DIR/... → 프로젝트 루트
    상대 경로               → 프로젝트 루트 기준
    """
    if token.startswith("~/.claude/") and global_dir is not None:
        return global_dir / token[len("~/.claude/"):]
    if token.startswith("~"):
        return Path(token).expanduser()
    token = token.replace("$CLAUDE_PROJECT_DIR/", "").replace("${CLAUDE_PROJECT_DIR}/", "")
    p = Path(token)
    return p if p.is_absolute() else root / p


def _collect_hooks(settings: dict, root: Path, global_dir: Optional[Path]) -> list[dict]:
    """settings.json의 hooks를 평탄화한다.

    body = command + 참조한 스크립트 본문. Health는 body에서 exit 코드와 stop_hook_active를 찾는다
    (command가 "bash x.sh" 한 줄이어도 실제 로직을 점검하기 위해).
    """
    hooks = []
    for event, groups in (settings.get("hooks") or {}).items():
        for group in groups or []:
            for hk in group.get("hooks") or []:
                cmd = hk.get("command", "")
                # ':'와 '\\'는 Windows 절대 경로(C:/... , C:\\...)를 위해 포함
                scripts = re.findall(r"[\w$~{}./:\\-]+\.(?:sh|py)\b", cmd)
                body = cmd + "".join(
                    "\n" + _read(_resolve_script(s, root, global_dir)) for s in scripts
                )
                hooks.append({
                    "event": event,
                    "matcher": group.get("matcher", "") or "*",
                    "command": cmd,
                    "body": body,
                })
    return hooks


def _command_desc(text: str) -> str:
    """커맨드 설명: YAML 머리말(---)의 description을 우선, 없으면 머리말 다음 첫 줄."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if m:
        d = re.search(r"^description:\s*(.+)$", m.group(1), re.M)
        if d:
            return d.group(1).strip().strip("\"'")
        text = text[m.end():]
    return _first_line(text)


def _collect_layer(name: str, base: Path, claude_md: Path, root: Path,
                   global_dir: Optional[Path]) -> dict:
    """설정 레이어(Global 또는 Project) 하나를 수집한다: CLAUDE.md의 CRITICAL 규칙, hooks, commands, skills.

    commands 목록에는 commands/*.md(kind=command)와 skills/<이름>/SKILL.md(kind=skill)를 함께 담는다.
    둘 다 /<이름>으로 호출되기 때문이다.
    """
    md = _read(claude_md)
    commands_dir = base / "commands"
    skills_dir = base / "skills"
    return {
        "name": name,
        "claude_md": str(claude_md),
        "exists": claude_md.is_file(),
        "critical": [ln.strip().lstrip("-* ").strip() for ln in md.splitlines() if "CRITICAL:" in ln],
        "hooks": _collect_hooks(_read_json(base / "settings.json"), root, global_dir),
        "commands": ([
            {"name": "/" + p.stem, "desc": _command_desc(_read(p)), "kind": "command"}
            for p in sorted(commands_dir.glob("*.md"))
        ] if commands_dir.is_dir() else []) + ([
            {"name": "/" + p.parent.name, "desc": _command_desc(_read(p)), "kind": "skill"}
            for p in sorted(skills_dir.glob("*/SKILL.md"))
        ] if skills_dir.is_dir() else []),
    }


def _collect_requirements(prd: str) -> list[dict]:
    """PRD 표에서 R-ID 행을 읽는다. 열 순서는 | ID | 요구사항 | AC | 로 고정, AC의 백틱은 제거한다."""
    reqs = []
    for m in re.finditer(r"^\|\s*(R\d+)\s*\|([^|]*)\|([^|]*)\|", prd, re.M):
        reqs.append({
            "id": m.group(1),
            "title": m.group(2).strip(),
            "ac": m.group(3).strip().strip("`"),
        })
    return reqs


def _collect_adrs(text: str) -> list[dict]:
    """ADR.md를 '### ADR-NNN' 헤더 단위로 나눠 id, 제목, Status를 읽는다. Status 줄이 없으면 'Unknown'."""
    adrs = []
    blocks = re.split(r"^(?=###\s*ADR-\d+)", text, flags=re.M)
    for b in blocks:
        m = re.match(r"###\s*(ADR-\d+):?\s*(.*)", b)
        if not m:
            continue
        st = re.search(r"\*\*Status\*\*:\s*(.+)", b)
        adrs.append({
            "id": m.group(1),
            "title": m.group(2).strip(),
            "status": st.group(1).strip() if st else "Unknown",
        })
    return adrs


def _adr_state(status: str) -> str:
    """ADR Status를 accepted / proposed / superseded / unknown 중 하나로 정규화한다."""
    low = status.lower()
    for key in ("superseded", "proposed", "accepted"):
        if low.startswith(key):
            return key
    return "unknown"


def _parse_arch_tree(text: str) -> list[dict]:
    """ARCHITECTURE.md '## 디렉토리 구조' 코드 블록의 트리를 경로 목록으로 바꾼다."""
    sec = _section(text, SEC_TREE)
    m = re.search(r"```[^\n]*\n(.*?)```", sec or "", re.S)
    if not m:
        return []
    entries, stack = [], []
    for raw in m.group(1).splitlines():
        line = raw.split("#", 1)[0].rstrip()
        lm = re.match(r"^([│ \t]*)(?:[├└]──\s*)?(\S+)$", line)
        if not lm:
            continue
        # 깊이 = 들여쓰기("│   " 또는 공백 4칸) 개수 + 연결자(├── / └──)가 있으면 1
        depth = len(lm.group(1).expandtabs(4)) // 4 + (1 if "──" in line else 0)
        stack = stack[:depth]
        if stack:
            stack[-1]["has_children"] = True
        name = lm.group(2)
        e = {
            "path": (stack[-1]["path"] if stack else "") + name,
            "is_dir": name.endswith("/"),
            "depth": depth,
            "has_children": False,
        }
        entries.append(e)
        if e["is_dir"]:
            stack.append(e)
    return entries


def scope_outside_arch(pattern: str, arch: list[dict]) -> bool:
    """scope 패턴이 ARCHITECTURE 트리의 최상위 디렉토리 아래인데 트리에 없는 경로면 True.

    트리 최상위 디렉토리 밖의 경로(tests/, 루트 파일 등)는 검사하지 않는다.
    하위 항목이 없는 디렉토리(leaf)는 그 아래 전체를 허용한다.
    """
    tops = [e["path"] for e in arch if e["depth"] == 0 and e["is_dir"]]
    if not any(pattern.startswith(t) for t in tops):
        return False
    prefix = re.split(r"[*?\[]", pattern, maxsplit=1)[0]
    pdir = prefix.rsplit("/", 1)[0] + "/" if "/" in prefix else ""
    # 통과 조건: 트리의 파일과 정확히 일치 / 트리 디렉토리 바로 아래 / 하위가 정의되지 않은(leaf) 디렉토리의 하위
    for e in arch:
        if not e["is_dir"]:
            if pattern == e["path"]:
                return False
        elif pdir == e["path"] or (pdir.startswith(e["path"]) and not e["has_children"]):
            return False
    return True


def _collect_step(phase_dir: Path, s: dict) -> dict:
    """step 하나의 index.json 항목과 step{N}.md 섹션을 합쳐 dict로 만든다. 파일이 없으면 file_exists=False."""
    f = phase_dir / f"step{s.get('step')}.md"
    text = _read(f)
    req_sec = _section(text, SEC_REQ)
    scope_sec = _section(text, SEC_SCOPE)
    ac_sec = _section(text, SEC_AC)
    adr_sec = _section(text, SEC_ADR)
    docs_sec = _section(text, SEC_DOCS)
    return {
        "step": s.get("step"),
        "name": s.get("name", ""),
        "status": s.get("status", "pending"),
        "summary": s.get("summary", ""),
        "error_message": s.get("error_message", ""),
        "blocked_reason": s.get("blocked_reason", ""),
        "file": f"phases/{phase_dir.name}/{f.name}",
        "file_exists": f.is_file(),
        "req_ids": sorted(set(re.findall(r"\bR\d+\b", req_sec or "")), key=lambda r: int(r[1:])),
        "has_req_section": req_sec is not None,
        "scope": re.findall(r"^\s*-\s*`([^`]+)`", scope_sec or "", re.M),
        "has_ac": bool(ac_sec and "```" in ac_sec),
        "adr_ids": sorted(set(re.findall(r"\bADR-\d+\b", adr_sec or "")), key=lambda a: int(a[4:])),
        # None = "참조 문서" 섹션 없음 → execute.py가 docs 전체를 주입한다.
        "ref_docs": None if docs_sec is None else [
            Path(p).name for p in re.findall(r"^\s*-\s*`([^`]+)`", docs_sec, re.M)
        ],
    }


def _derive_status(steps: list[dict]) -> str:
    """top index가 없을 때 phase 상태를 step 상태로 계산한다: error > blocked > (전부 completed) > pending."""
    states = {s["status"] for s in steps}
    for st in ("error", "blocked"):
        if st in states:
            return st
    return "completed" if steps and states == {"completed"} else "pending"


def _collect_phases(root: Path) -> list[dict]:
    """phases/index.json 순서대로 phase를 읽는다. top index가 없으면 phases/*/index.json을 이름순으로 찾는다."""
    phases_dir = root / "phases"
    top = _read_json(phases_dir / "index.json")
    entries = top.get("phases") or [
        {"dir": p.parent.name} for p in sorted(phases_dir.glob("*/index.json"))
    ]
    phases = []
    for e in entries:
        pdir = phases_dir / e.get("dir", "")
        idx = _read_json(pdir / "index.json")
        steps = [_collect_step(pdir, s) for s in idx.get("steps", [])]
        phases.append({
            "dir": e.get("dir", ""),
            "status": e.get("status") or _derive_status(steps),
            "project": idx.get("project", ""),
            "steps": steps,
        })
    return phases


def _collect_proposals(text: str) -> list[dict]:
    """PROPOSALS.md의 체크박스 줄을 읽는다. 인용문(>) 안의 형식 예시는 건너뛴다. '|'로 id/출처/제안/이유를 나눈다."""
    props = []
    for ln in text.splitlines():
        if ln.lstrip().startswith(">"):
            continue
        m = re.match(r"^\s*-\s*\[( |x|X)\]\s*(.+)$", ln)
        if not m:
            continue
        parts = [p.strip() for p in m.group(2).split("|")]
        parts += [""] * (4 - len(parts))
        props.append({
            "approved": m.group(1).lower() == "x",
            "id": parts[0], "source": parts[1], "text": parts[2], "reason": parts[3],
        })
    return props


def _collect_ancestors(root: Path, global_dir: Optional[Path]) -> list[dict]:
    """프로젝트 위쪽 폴더의 CLAUDE.md / CLAUDE.local.md. Claude Code는 이 파일들을 세션 시작 때 함께 읽는다.

    claudeMdExcludes(프로젝트 settings.json, settings.local.json, 글로벌 settings.json)에 걸리면 excluded=True.
    """
    patterns = []
    sources = [root / ".claude" / "settings.json", root / ".claude" / "settings.local.json"]
    if global_dir is not None:
        sources.append(global_dir / "settings.json")
    for f in sources:
        patterns += _read_json(f).get("claudeMdExcludes", [])
    found = []
    for anc in root.resolve().parents:
        for name in ("CLAUDE.md", "CLAUDE.local.md"):
            f = anc / name
            if f.is_file():
                path = f.as_posix()
                found.append({"path": path, "excluded": any(fnmatch.fnmatch(path, pat) for pat in patterns)})
    return found


def _project_name(root: Path, phases: list[dict]) -> str:
    """프로젝트명 우선순위: CLAUDE.md '# 프로젝트: X' → phase index의 project → 디렉토리명."""
    m = re.search(r"^#\s*프로젝트:\s*(.+)$", _read(root / "CLAUDE.md"), re.M)
    if m:
        return m.group(1).strip()
    return next((p["project"] for p in phases if p["project"]), root.name)


def collect(root: Path, global_dir: Optional[Path] = None) -> dict:
    """저장소를 읽어 model dict를 만든다.

    model 키
      project, generated_at
      layers        [{name, claude_md, exists, critical, hooks, commands}]  (Global은 global_dir 지정 시만)
      docs          [{name, title}]
      has_prd, requirements [{id, title, ac}]
      adrs          [{id, title, status}]
      arch          [{path, is_dir, depth, has_children}]
      phases        [{dir, status, project, steps: [_collect_step 결과]}]
      has_proposals, proposals [{approved, id, source, text, reason}]
      ancestors     [{path, excluded}]  상위 폴더 CLAUDE.md
    """
    phases = _collect_phases(root)
    layers = []
    if global_dir is not None:
        layers.append(_collect_layer("Global", global_dir, global_dir / "CLAUDE.md", root, global_dir))
    layers.append(_collect_layer("Project", root / ".claude", root / "CLAUDE.md", root, global_dir))
    docs_dir = root / "docs"
    prd_path = docs_dir / "PRD.md"
    prop_path = docs_dir / "PROPOSALS.md"
    return {
        "project": _project_name(root, phases),
        "generated_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %z"),
        "layers": layers,
        "docs": [
            {"name": p.name, "title": _first_line(_read(p))}
            for p in sorted(docs_dir.glob("*.md"))
        ] if docs_dir.is_dir() else [],
        "has_prd": prd_path.is_file(),
        "requirements": _collect_requirements(_read(prd_path)),
        "phases": phases,
        "adrs": _collect_adrs(_read(docs_dir / "ADR.md")),
        "arch": _parse_arch_tree(_read(docs_dir / "ARCHITECTURE.md")),
        "has_proposals": prop_path.is_file(),
        "proposals": _collect_proposals(_read(prop_path)),
        "ancestors": _collect_ancestors(root, global_dir),
    }


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

def check_health(model: dict) -> list[dict]:
    """model을 점검해 [{level, target, message}]를 error → warn → info 순으로 반환한다.

    규칙
      hooks     PreToolUse: $CLAUDE_TOOL_INPUT 사용(warn), exit 1만 있고 exit 2 없음(error)
                Stop/SubagentStop: exit 2 없음(info), exit 2는 있으나 stop_hook_active 없음(warn)
      docs      PRD 없음(warn), PROPOSALS 없음(info)
      step      파일 없음(error), R-ID 없음(warn), PRD에 없는 R-ID(error), 범위 없음(warn), AC 코드 없음(warn)
                없는 ADR(error), Superseded ADR(warn), 미승인 ADR(warn), 참조 문서 없음(error),
                범위가 ARCHITECTURE 밖(warn), 실행 error(error), blocked(warn)
      요구사항   연결된 step 없음(warn)
    """
    out = []

    def add(level, target, msg):
        out.append({"level": level, "target": target, "message": msg})

    for layer in model["layers"]:
        for hk in layer["hooks"]:
            target = f"{layer['name']} hook {hk['event']}({hk['matcher']})"
            body = hk["body"]
            if hk["event"] == "PreToolUse":
                if "CLAUDE_TOOL_INPUT" in body:
                    add("warn", target, "$CLAUDE_TOOL_INPUT는 제공되지 않는다. hook 입력은 stdin JSON으로 읽어야 한다.")
                if re.search(r"\bexit 1\b", body) and not re.search(r"\bexit 2\b", body):
                    add("error", target, "exit 1은 도구 호출을 차단하지 않는다. 차단하려면 exit 2를 써야 한다.")
            if hk["event"] in ("Stop", "SubagentStop"):
                if not re.search(r"\bexit 2\b", body):
                    add("info", target, "검증이 실패해도 Claude가 수정을 이어가지 않는다 (exit 2 필요).")
                elif "stop_hook_active" not in body:
                    add("warn", target, "stop_hook_active 체크가 없어 무한 루프 위험이 있다.")

    for a in model.get("ancestors", []):
        if not a["excluded"]:
            add("info", a["path"], "상위 폴더 CLAUDE.md가 이 프로젝트 세션에 함께 로드된다 "
                "(독립 환경이면 claudeMdExcludes로 제외).")

    if not model["has_prd"]:
        add("warn", "docs/PRD.md", "PRD가 없어 요구사항 추적을 할 수 없다.")
    if not model["has_proposals"]:
        add("info", "docs/PROPOSALS.md", "제안 백로그가 없다 (MVP-First Guard #3).")

    req_ids = {r["id"] for r in model["requirements"]}
    adrs = {a["id"]: a for a in model["adrs"]}
    doc_names = {d["name"] for d in model["docs"]}
    covered = set()
    for ph in model["phases"]:
        for s in ph["steps"]:
            target = f"{ph['dir']}/step{s['step']} ({s['name']})"
            if not s["file_exists"]:
                add("error", target, f"{s['file']} 파일이 없다.")
                continue
            if not s["req_ids"]:
                add("warn", target, "요구사항 ID가 없다. PRD 범위에 연결되지 않은 step이다.")
            unknown = [r for r in s["req_ids"] if req_ids and r not in req_ids]
            if unknown:
                add("error", target, f"PRD에 없는 요구사항 ID: {', '.join(unknown)}")
            covered.update(s["req_ids"])
            if not s["scope"]:
                add("warn", target, "변경 허용 범위가 없다. scope_check를 할 수 없다.")
            if not s["has_ac"]:
                add("warn", target, "실행 가능한 AC 코드 블록이 없다.")
            for a in s["adr_ids"]:
                state = _adr_state(adrs[a]["status"]) if a in adrs else None
                if state is None:
                    add("error", target, f"ADR.md에 없는 ADR: {a}")
                elif state == "superseded":
                    add("warn", target, f"{a}는 {adrs[a]['status']} 상태다. 대체된 ADR을 참조해야 한다.")
                elif state != "accepted":
                    add("warn", target, f"{a}는 승인되지 않은 상태다 ({adrs[a]['status']}).")
            missing_docs = [d for d in s["ref_docs"] or [] if d not in doc_names]
            if missing_docs:
                add("error", target, f"참조 문서가 docs/에 없다: {', '.join(missing_docs)}")
            outside = [p for p in s["scope"] if scope_outside_arch(p, model["arch"])]
            if outside:
                add("warn", target, f"변경 허용 범위가 ARCHITECTURE 구조 밖이다: {', '.join(outside)}")
            if s["status"] == "error":
                add("error", target, f"실행 실패: {s['error_message'] or 'unknown'}")
            if s["status"] == "blocked":
                add("warn", target, f"차단됨: {s['blocked_reason'] or 'unknown'}")

    for r in model["requirements"]:
        if r["id"] not in covered:
            add("warn", r["id"], f"'{r['title']}'을(를) 다루는 step이 없다.")

    return sorted(out, key=lambda h: LEVEL_ORDER[h["level"]])


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def h(v) -> str:
    """HTML 이스케이프. 저장소 내용(에러 메시지, 제안 등)은 모두 이 함수를 거쳐 출력한다 (XSS 방지)."""
    return html.escape(str(v), quote=True)


CSS = """
:root{--bg:#f7f7f5;--card:#fff;--fg:#1c1c1a;--muted:#6b6b66;--line:#e2e2dc;--code:#f0f0ec;
--ok:#1f8a4c;--ok-bg:#e3f4ea;--pend:#6b6b66;--pend-bg:#ededea;--err:#c0392b;--err-bg:#fbe6e3;
--blk:#a86400;--blk-bg:#fbefd9;--info:#2d6cb5;--info-bg:#e4eefa;--accent:#2d6cb5}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#141413;--card:#1d1d1b;--fg:#ececea;
--muted:#9a9a94;--line:#2f2f2c;--code:#262624;--ok:#5cc98a;--ok-bg:#183626;--pend:#a3a39d;--pend-bg:#2a2a27;
--err:#f07b6c;--err-bg:#3d1f1b;--blk:#e8a94a;--blk-bg:#3a2c14;--info:#78aef0;--info-bg:#1a2a40;--accent:#78aef0}}
:root[data-theme="dark"]{--bg:#141413;--card:#1d1d1b;--fg:#ececea;--muted:#9a9a94;--line:#2f2f2c;--code:#262624;
--ok:#5cc98a;--ok-bg:#183626;--pend:#a3a39d;--pend-bg:#2a2a27;--err:#f07b6c;--err-bg:#3d1f1b;--blk:#e8a94a;
--blk-bg:#3a2c14;--info:#78aef0;--info-bg:#1a2a40;--accent:#78aef0}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 system-ui,-apple-system,"Segoe UI","Noto Sans KR",sans-serif}
header{padding:24px 16px 8px;max-width:1200px;margin:0 auto}
header h1{margin:0;font-size:22px}
header p{margin:4px 0 0;color:var(--muted)}
nav{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line)}
nav div{max-width:1200px;margin:0 auto;padding:8px 16px;display:flex;gap:16px;overflow-x:auto}
nav a{color:var(--muted);text-decoration:none;white-space:nowrap}
nav a:hover{color:var(--accent)}
main{max-width:1200px;margin:0 auto;padding:8px 16px 48px}
section{margin-top:28px}
h2{font-size:16px;margin:0 0 10px}
h3{font-size:14px;margin:14px 0 6px;color:var(--muted)}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px;min-width:0}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(170px,1fr))}
.kpi .v{font-size:24px;font-weight:600}
.kpi .l{color:var(--muted);font-size:12px}
.layers{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(min(320px,100%),1fr))}
.scroll{overflow-x:auto}
.scroll>table{min-width:560px}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:12px}
code{background:var(--code);padding:1px 5px;border-radius:4px;font-size:12px;word-break:break-all}
.chip{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:500;white-space:nowrap}
.completed,.approved{color:var(--ok);background:var(--ok-bg)}
.pending,.open{color:var(--pend);background:var(--pend-bg)}
.error{color:var(--err);background:var(--err-bg)}
.blocked,.warn{color:var(--blk);background:var(--blk-bg)}
.info{color:var(--info);background:var(--info-bg)}
.flow{display:flex;align-items:stretch;gap:6px;overflow-x:auto;padding-bottom:4px}
.phase-label{min-width:120px;display:flex;flex-direction:column;justify-content:center;gap:4px}
.step{min-width:150px;max-width:220px;border:1px solid var(--line);border-left:4px solid var(--pend);border-radius:6px;padding:8px;background:var(--card)}
.step.s-completed{border-left-color:var(--ok)}.step.s-error{border-left-color:var(--err)}.step.s-blocked{border-left-color:var(--blk)}
.step .n{font-weight:600}
.step .d{color:var(--muted);font-size:12px;margin-top:4px}
.arrow{align-self:center;color:var(--muted)}
.dot{text-align:center;color:var(--ok);font-weight:700}
.miss{color:var(--err)}
.muted{color:var(--muted)}
ul{margin:4px 0;padding-left:18px}
"""


def _chip(cls: str, text: str = "") -> str:
    """상태 배지 HTML. cls는 CSS 클래스(completed/pending/error/blocked/warn/info...)와 같다."""
    return f'<span class="chip {h(cls)}">{h(text or cls)}</span>'


def _all_steps(model: dict):
    """(phase, step) 쌍을 phase 순서 → step 순서로 순회한다. Trace 열 순서의 기준."""
    for ph in model["phases"]:
        for s in ph["steps"]:
            yield ph, s


def _render_summary(model: dict, health: list[dict]) -> str:
    """① Summary KPI 카드.

    완료 step          completed 수 / 전체 step 수
    error / blocked    각 상태의 step 수
    요구사항 커버리지     step에 연결된 R-ID 수 / PRD R-ID 수 (계획 비율이며 구현 완료율이 아님)
    제안 대기 / 승인     PROPOSALS의 [ ] 수 / [x] 수
    Health error / warn
    """
    steps = [s for _, s in _all_steps(model)]
    counts = {st: sum(1 for s in steps if s["status"] == st) for st in STATUSES}
    reqs = model["requirements"]
    covered = {r for s in steps for r in s["req_ids"]} & {r["id"] for r in reqs}
    cov = f"{round(len(covered) / len(reqs) * 100)}%" if reqs else "—"
    approved = sum(1 for p in model["proposals"] if p["approved"])
    hcount = {lv: sum(1 for x in health if x["level"] == lv) for lv in LEVEL_ORDER}
    kpis = [
        (f"{counts['completed']}/{len(steps)}", "완료 step"),
        (f"{counts['error']} / {counts['blocked']}", "error / blocked"),
        (cov, f"요구사항 커버리지 ({len(covered)}/{len(reqs)})"),
        (f"{len(model['proposals']) - approved} / {approved}", "제안 대기 / 승인"),
        (f"{hcount['error']} / {hcount['warn']}", "Health error / warn"),
    ]
    cards = "".join(
        f'<div class="card kpi"><div class="v">{h(value)}</div><div class="l">{h(label)}</div></div>'
        for value, label in kpis
    )
    return f'<section id="summary"><h2>① Summary</h2><div class="grid">{cards}</div></section>'


def _render_layers(model: dict) -> str:
    """② Layers: 레이어별 CRITICAL 규칙, hooks, commands와 docs 목록 (PROPOSALS는 '주입 제외' 표시)."""
    blocks = []
    for layer in model["layers"]:
        crit = "".join(f"<li>{h(c)}</li>" for c in layer["critical"]) or '<li class="muted">없음</li>'
        hooks = "".join(
            f"<tr><td>{h(x['event'])}</td><td><code>{h(x['matcher'])}</code></td>"
            f"<td><code>{h(x['command'])}</code></td></tr>"
            for x in layer["hooks"]
        ) or '<tr><td colspan="3" class="muted">없음</td></tr>'
        cmds = "".join(
            f"<tr><td><code>{h(c['name'])}</code>"
            f"{' ' + _chip('info', 'skill') if c.get('kind') == 'skill' else ''}</td><td>{h(c['desc'])}</td></tr>"
            for c in layer["commands"]
        ) or '<tr><td colspan="2" class="muted">없음</td></tr>'
        state = "" if layer["exists"] else " " + _chip("warn", "CLAUDE.md 없음")
        blocks.append(
            f'<div class="card"><h3 style="margin-top:0">{h(layer["name"])}{state}</h3>'
            f'<div class="muted"><code>{h(layer["claude_md"])}</code></div>'
            f"<h3>CRITICAL 규칙</h3><ul>{crit}</ul>"
            f'<h3>Hooks</h3><div class="scroll"><table><tr><th>event</th><th>matcher</th><th>command</th></tr>{hooks}</table></div>'
            f'<h3>Commands · Skills</h3><div class="scroll"><table>{cmds}</table></div></div>'
        )
    docs = "".join(
        f"<tr><td><code>docs/{h(d['name'])}</code></td><td>{h(d['title'])}"
        f"{' ' + _chip('pending', '주입 제외') if d['name'] == 'PROPOSALS.md' else ''}</td></tr>"
        for d in model["docs"]
    ) or '<tr><td colspan="2" class="muted">없음</td></tr>'
    blocks.append(f'<div class="card"><h3 style="margin-top:0">Docs (가드레일 주입 대상)</h3><table>{docs}</table></div>')
    if model.get("ancestors"):
        rows = "".join(
            f"<tr><td><code>{h(a['path'])}</code></td>"
            f"<td>{_chip('pending', '제외됨') if a['excluded'] else _chip('warn', '함께 로드됨')}</td></tr>"
            for a in model["ancestors"]
        )
        blocks.append('<div class="card"><h3 style="margin-top:0">Ancestors (상위 폴더 CLAUDE.md)</h3>'
                      f"<table>{rows}</table></div>")
    return f'<section id="layers"><h2>② Layers</h2><div class="layers">{"".join(blocks)}</div></section>'


def _render_flow(model: dict) -> str:
    """③ Flow: phase별 step 파이프라인. 상태 색상, R-ID, summary/에러를 카드로 표시한다."""
    if not model["phases"]:
        return '<section id="flow"><h2>③ Flow</h2><div class="card muted">phases/ 없음</div></section>'
    rows = []
    for ph in model["phases"]:
        items = [f'<div class="phase-label"><b>{h(ph["dir"])}</b>{_chip(ph["status"])}</div>']
        for i, s in enumerate(ph["steps"]):
            detail = s["error_message"] or s["blocked_reason"] or s["summary"]
            reqs = ", ".join(s["req_ids"]) or "R-ID 없음"
            if i:
                items.append('<div class="arrow">→</div>')
            items.append(
                f'<div class="step s-{h(s["status"])}" title="{h(detail)}">'
                f'<div class="n">S{h(s["step"])} {h(s["name"])}</div>'
                f'<div>{_chip(s["status"])}</div>'
                f'<div class="d">{h(reqs)}</div>'
                + (f'<div class="d">{h(detail[:90])}</div>' if detail else "")
                + "</div>"
            )
        rows.append(f'<div class="card"><div class="flow">{"".join(items)}</div></div>')
    return f'<section id="flow"><h2>③ Flow</h2><div style="display:grid;gap:10px">{"".join(rows)}</div></section>'


def _render_trace(model: dict) -> str:
    """④ Trace: 요구사항 × step 매트릭스와 요구사항별 상태(구현됨/진행중/미계획), 이어서 ADR × step 매트릭스."""
    steps = list(_all_steps(model))
    head = "".join(f'<th title="{h(ph["dir"])}">{h(ph["dir"])}<br>S{h(s["step"])}</th>' for ph, s in steps)
    if not model["requirements"]:
        return (
            '<section id="trace"><h2>④ Trace</h2><div class="card muted">PRD 요구사항(R-ID) 없음</div>'
            f"{_render_adr_trace(model, steps, head)}</section>"
        )
    rows = []
    for r in model["requirements"]:
        linked = [s for _, s in steps if r["id"] in s["req_ids"]]
        cells = "".join(
            f'<td class="dot">{"●" if r["id"] in s["req_ids"] else ""}</td>' for _, s in steps
        )
        if not linked:
            cov = _chip("warn", "미계획")
        elif all(s["status"] == "completed" for s in linked):
            cov = _chip("completed", "구현됨")
        else:
            cov = _chip("pending", "진행중")
        rows.append(
            f"<tr><td><b>{h(r['id'])}</b></td><td>{h(r['title'])}</td>"
            f"<td><code>{h(r['ac'])}</code></td>{cells}<td>{cov}</td></tr>"
        )
    return (
        '<section id="trace"><h2>④ Trace — 요구사항 × Step</h2><div class="card scroll"><table>'
        f"<tr><th>ID</th><th>요구사항</th><th>AC</th>{head}<th>상태</th></tr>{''.join(rows)}</table></div>"
        f"{_render_adr_trace(model, steps, head)}</section>"
    )


ADR_CHIP = {"accepted": "completed", "proposed": "pending", "superseded": "blocked", "unknown": "warn"}


def _render_adr_trace(model: dict, steps: list, head: str) -> str:
    """ADR × step 매트릭스. ADR이 하나도 없으면 빈 문자열."""
    if not model["adrs"]:
        return ""
    rows = []
    for a in model["adrs"]:
        cells = "".join(
            f'<td class="dot">{"●" if a["id"] in s["adr_ids"] else ""}</td>' for _, s in steps
        )
        n = sum(1 for _, s in steps if a["id"] in s["adr_ids"])
        rows.append(
            f"<tr><td><b>{h(a['id'])}</b></td><td>{h(a['title'])}</td>"
            f"<td>{_chip(ADR_CHIP[_adr_state(a['status'])], a['status'])}</td>{cells}<td>{n} step</td></tr>"
        )
    return (
        '<h3>ADR × Step</h3><div class="card scroll"><table>'
        f"<tr><th>ID</th><th>결정</th><th>Status</th>{head}<th>참조</th></tr>{''.join(rows)}</table></div>"
    )


def _render_scope(model: dict) -> str:
    """⑤ Scope Lock: step별 변경 허용 범위, AC 유무, 근거 ADR, 주입 문서."""
    rows = "".join(
        f"<tr><td>{h(ph['dir'])}/S{h(s['step'])} {h(s['name'])}</td>"
        f"<td>{' '.join(f'<code>{h(p)}</code>' for p in s['scope']) or '<span class=miss>없음</span>'}</td>"
        f"<td>{'✓' if s['has_ac'] else '<span class=miss>없음</span>'}</td>"
        f"<td>{h(', '.join(s['adr_ids']) or '—')}</td>"
        f"<td>{_injected_docs(s)}</td></tr>"
        for ph, s in _all_steps(model)
    ) or '<tr><td colspan="5" class="muted">step 없음</td></tr>'
    return (
        '<section id="scope"><h2>⑤ Scope Lock</h2><div class="card scroll"><table>'
        "<tr><th>step</th><th>변경 허용 범위</th><th>AC</th><th>근거 ADR</th><th>주입 문서</th></tr>"
        f"{rows}</table></div></section>"
    )


def _injected_docs(step: dict) -> str:
    """execute.py가 이 step에 주입하는 docs (PROPOSALS.md는 항상 제외)."""
    if step["ref_docs"] is None:
        return '<span class="muted">전체</span>'
    docs = ["PRD.md"] + [d for d in step["ref_docs"] if d != "PRD.md"]
    return " ".join(f"<code>{h(d)}</code>" for d in docs)


def _render_proposals(model: dict) -> str:
    """⑥ Proposals: 승인 항목을 먼저, 대기 항목을 뒤에 표시한다."""
    rows = "".join(
        f"<tr><td>{_chip('approved' if p['approved'] else 'open', '승인' if p['approved'] else '대기')}</td>"
        f"<td>{h(p['id'])}</td><td>{h(p['source'])}</td><td>{h(p['text'])}</td><td>{h(p['reason'])}</td></tr>"
        for p in sorted(model["proposals"], key=lambda p: not p["approved"])
    ) or '<tr><td colspan="5" class="muted">제안 없음</td></tr>'
    return (
        '<section id="proposals"><h2>⑥ Proposals</h2><div class="card scroll"><table>'
        f"<tr><th>상태</th><th>ID</th><th>출처</th><th>제안</th><th>이유</th></tr>{rows}</table></div></section>"
    )


def _render_health(health: list[dict]) -> str:
    """⑦ Health: check_health 결과 표. 비어 있으면 '문제 없음'."""
    rows = "".join(
        f"<tr><td>{_chip(x['level'])}</td><td>{h(x['target'])}</td><td>{h(x['message'])}</td></tr>"
        for x in health
    ) or f'<tr><td colspan="3">{_chip("completed", "문제 없음")}</td></tr>'
    return (
        '<section id="health"><h2>⑦ Health</h2><div class="card scroll"><table>'
        f"<tr><th>level</th><th>대상</th><th>내용</th></tr>{rows}</table></div></section>"
    )


def render(model: dict, health: list[dict]) -> str:
    """model과 health로 단일 HTML 문서를 만든다. CSS는 인라인, 외부 스크립트나 폰트는 쓰지 않는다."""
    nav = "".join(
        f'<a href="#{k}">{v}</a>' for k, v in [
            ("summary", "Summary"), ("layers", "Layers"), ("flow", "Flow"), ("trace", "Trace"),
            ("scope", "Scope"), ("proposals", "Proposals"), ("health", "Health"),
        ]
    )
    body = "".join([
        _render_summary(model, health), _render_layers(model), _render_flow(model),
        _render_trace(model), _render_scope(model), _render_proposals(model), _render_health(health),
    ])
    return (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Harness Map</title><style>{CSS}</style></head><body>"
        f"<header><h1>Harness Map — {h(model['project'])}</h1>"
        f"<p>generated {h(model['generated_at'])} · scripts/harness_map.py</p></header>"
        f"<nav><div>{nav}</div></nav><main>{body}</main></body></html>"
    )


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def generate(root: Path, out: Optional[Path] = None,
             global_dir: Optional[Path] = None) -> tuple[Path, list[dict]]:
    """collect → check_health → render → 파일 쓰기. (출력 경로, health)를 반환한다. execute.py가 이 함수를 직접 호출한다."""
    model = collect(root, global_dir)
    health = check_health(model)
    out = out or root / DEFAULT_OUT
    out.write_text(render(model, health), encoding="utf-8")
    return out, health


def main(argv: Optional[list[str]] = None) -> int:
    """CLI 진입점. argv를 받는 것은 테스트에서 직접 호출하기 위해서다."""
    p = argparse.ArgumentParser(description="저장소의 하네스 구조를 HTML로 출력한다.")
    p.add_argument("-o", "--out", help=f"출력 경로 (기본: {DEFAULT_OUT})")
    p.add_argument("--with-global", action="store_true",
                   help="글로벌 레이어 포함 (CLAUDE_CONFIG_DIR, 없으면 ~/.claude)")
    p.add_argument("--global-dir", type=Path, help="글로벌 레이어 위치를 직접 지정")
    p.add_argument("--strict", action="store_true", help="Health error가 있으면 exit 1")
    args = p.parse_args(argv)

    global_dir = args.global_dir
    if global_dir is None and args.with_global:
        global_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    out, health = generate(ROOT, Path(args.out) if args.out else None, global_dir)
    errors = sum(1 for x in health if x["level"] == "error")
    warns = sum(1 for x in health if x["level"] == "warn")
    print(f"Harness Map: {out} (health error {errors}, warn {warns})")
    return 1 if args.strict and errors else 0


if __name__ == "__main__":
    sys.exit(main())

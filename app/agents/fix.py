"""Fix agent: a replay-gated candidate fix for a defect the triage agent found, for human review.

    python -m app.agents.fix --analysis local_data/agent_runs/<run>/result.json \\
        --vergeo-repo ~/AndroidStudioProjects/Vergeo [--base <commit>]

Runs only when the triage result has needs_fix_investigation=true (or --backtest, which replays a
historical defect on the commit before its human fix, to measure the workflow itself).

The workflow, and who enforces each step:

1. worktree   (code)  `git worktree add -b fix-agent/<id>` at --base, outside the main checkout.
2. reproduce  (model) commit 1 = a regression/replay test ONLY (files under src/test/).
3. fix        (model) commit 2+ = the narrowest change.
4. validate   (code)  validate_candidate(): commit 1 must FAIL its new tests on the baseline,
              HEAD must pass them, the complete JVM suite must pass with no new failures versus
              the base commit, and the golden corpus, every *ReplayTest and the ML isolation
              tests must be green.
5. review     (code)  REVIEW.md with the diff, test evidence, risks and an explicit human
              approval line.

What it can never do, and how that is enforced rather than promised:
- check_tool_call() (wired as the SDK permission callback and a PreToolUse hook) denies git
  merge/push/rebase/reset/checkout of main, any adb, any gradle install/assemble/publish task,
  network tools, and any file path outside the worktree;
- this module's own subprocess calls go through _git() and _gradle(), which whitelist read-only
  git subcommands and test tasks (tests/agents/test_fix_safety.py pins both);
- nothing here merges, pushes, installs or deletes the worktree; the human does that.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .schemas import IncidentAnalysis

REVIEWS_DIR = Path("local_data") / "fix_reviews"
JAVA_HOME = os.environ.get("JAVA_HOME", r"C:\Program Files\Android\Android Studio\jbr")
FULL_SUITE_TASK = ":app:testReleaseUnitTest"
TEST_RESULTS = Path("android/app/build/test-results/testReleaseUnitTest")
TEST_SRC = "android/app/src/test/"
# Suites the validation names explicitly in the review, beyond "everything passed".
GUARD_SUITES = (
    "GoldenReliabilityCorpusTest",  # every field incident the reliability corpus pins
    "MlShadowCorpusParityTest",  # corpus decisions identical with the ML shadow on and off
    "MlShadowDecisionEquivalenceTest",
    "MlShadowIsolationTest",  # no decision class reads the ML package
    "MlResearchCaptureTest",
)

# --- policy -------------------------------------------------------------------------------------

_DENY_BASH = [
    (
        r"\bgit\s+(push|merge|rebase|pull|fetch|remote|config|tag|worktree|stash|clean)\b",
        "git command that changes history, remotes or other checkouts",
    ),
    (r"\bgit\s+reset\b.*--hard", "git reset --hard"),
    (
        r"\bgit\s+(checkout|switch)\s+(-f\s+)?(master|main|integration/\S+)\b",
        "switching to a shared branch",
    ),
    (r"\bgit\s+branch\s+-[dDmM]\b", "deleting or renaming branches"),
    (r"\badb\b", "adb (device access)"),
    (
        r"gradlew(\.bat)?\b[^\n]*\b(install\w*|uninstall\w*|assemble\w*|bundle\w*|publish\w*|"
        r"upload\w*|connected\w*)",
        "gradle task that builds, installs or ships an app",
    ),
    (r"\b(curl|wget|Invoke-WebRequest|iwr|scp|ssh|ftp|gh)\b", "network tool"),
    (r"\bnpm\s+(publish|install|i)\b|\bpip\s+install\b", "package install or publish"),
    (r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|\$HOME|[A-Za-z]:)", "recursive delete outside the worktree"),
    (r"(^|[;&|]\s*)cd\s+(\.\.|/|~|[A-Za-z]:)", "changing directory out of the worktree"),
]
_ABS_PATH = re.compile(r"(?<![\w.])(?:[A-Za-z]:[\\/]|/c/|/mnt/|~[\\/])[^\s'\"|;&]*")
FILE_TOOLS = {"Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep"}
ALLOWED_TOOLS = FILE_TOOLS | {"Bash", "TodoWrite"}


def _inside(path: str, root: Path) -> bool:
    try:
        p = Path(path)
        p = p if p.is_absolute() else root / p
        return p.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def check_tool_call(tool: str, args: dict, root: Path) -> tuple[bool, str]:
    """Decide one fix-agent tool call. Pure function; the SDK callback and hook both use it."""
    if tool not in ALLOWED_TOOLS:
        return False, f"tool {tool} is not available to the fix agent"
    if tool in FILE_TOOLS:
        for key in ("file_path", "path", "notebook_path"):
            value = args.get(key)
            if value and not _inside(value, root):
                return False, f"{key} {value} is outside the worktree"
        return True, "ok"
    if tool == "Bash":
        cmd = args.get("command", "")
        for pattern, why in _DENY_BASH:
            if re.search(pattern, cmd, re.I):
                return False, f"denied: {why}"
        for m in _ABS_PATH.finditer(cmd):
            candidate = m.group(0)
            if candidate.startswith("/c/"):
                candidate = "C:/" + candidate[3:]
            if not _inside(candidate, root) and not _allowed_outside(candidate):
                return False, f"path {m.group(0)} is outside the worktree"
        return True, "ok"
    return True, "ok"


def _allowed_outside(path: str) -> bool:
    """The JDK is the one outside path a test run needs."""
    return (
        Path(path.rstrip("\\/")).as_posix().lower().startswith(Path(JAVA_HOME).as_posix().lower())
    )


# --- whitelisted subprocesses ---------------------------------------------------------------

_GIT_READ = {"rev-parse", "rev-list", "log", "diff", "show", "status", "ls-files"}


def _git(repo: Path, *args: str, check: bool = True) -> str:
    sub = args[0]
    if sub == "init":
        if args[1:2] != ("-q",) or len(args) != 3:
            raise PermissionError("only 'git init -q <dir>' is allowed")
    elif sub == "fetch":
        # Backtest only: one commit from a local repository into a new fix-agent branch.
        if not (
            len(args) == 5
            and args[1:3] == ("-q", "--no-tags")
            and Path(args[3]).is_dir()
            and re.fullmatch(r"[0-9a-f]{40}:refs/heads/fix-agent/[\w-]+", args[4])
        ):
            raise PermissionError(f"git fetch {' '.join(args[1:])} is not allowed")
    elif sub == "worktree":
        if args[1:2] != ("add",):
            raise PermissionError("only 'git worktree add' is allowed")
    elif sub == "checkout":
        # Only detached checkouts of a commit, or returning to the fix branch, inside a worktree.
        if not (
            args[1:2] == ("--detach",) or (len(args) == 2 and args[1].startswith("fix-agent/"))
        ):
            raise PermissionError(f"git checkout {' '.join(args[1:])} is not allowed")
    elif sub not in _GIT_READ:
        raise PermissionError(f"git {sub} is not allowed in the fix workflow")
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout


def _gradle(worktree: Path, *tasks_and_args: str, timeout: int = 3600) -> tuple[int, str, float]:
    tasks = [t for t in tasks_and_args if t.startswith(":")]
    if not tasks or any(not re.fullmatch(r":app:test\w*UnitTest", t) for t in tasks):
        raise PermissionError(f"only unit-test tasks may run, got {tasks}")
    wrapper = worktree / "android" / ("gradlew.bat" if os.name == "nt" else "gradlew")
    env = {**os.environ, "JAVA_HOME": JAVA_HOME}
    started = time.perf_counter()
    out = subprocess.run(
        [str(wrapper), "-p", str(worktree / "android"), *tasks_and_args, "--console=plain"],
        capture_output=True,
        text=True,
        env=env,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=worktree,
    )
    return out.returncode, (out.stdout + out.stderr)[-4000:], time.perf_counter() - started


# --- worktree -------------------------------------------------------------------------------


@dataclass
class Worktree:
    path: Path
    branch: str
    base: str


def create_worktree(
    repo: Path, fix_id: str, base: str = "HEAD", parent: Path | None = None
) -> Worktree:
    base_sha = _git(repo, "rev-parse", base).strip()
    parent = parent or repo.parent
    path = parent / f"{repo.name}-fixagent-{fix_id}"
    branch = f"fix-agent/{fix_id}"
    _git(repo, "worktree", "add", "-b", branch, str(path), base_sha)
    return Worktree(path=path, branch=branch, base=base_sha)


def create_backtest_clone(
    repo: Path, fix_id: str, base: str, parent: Path | None = None
) -> Worktree:
    """A backtest must not see its own answer. A worktree shares the object store and refs, so
    `git log --all` shows the later human fix (the first 11-15 backtest found c567346 there and
    closed the case as a duplicate). A new repository holding only the base commit and its
    ancestors, with no remote, hides every later commit."""
    base_sha = _git(repo, "rev-parse", base).strip()
    parent = parent or repo.parent
    path = parent / f"{repo.name}-backtest-{fix_id}"
    branch = f"fix-agent/{fix_id}"
    _git(parent, "init", "-q", str(path))
    _git(path, "fetch", "-q", "--no-tags", str(repo.resolve()), f"{base_sha}:refs/heads/{branch}")
    _git(path, "checkout", branch)
    return Worktree(path=path, branch=branch, base=base_sha)


def prepare_android_build(repo: Path, wt: Path) -> None:
    """A fresh worktree lacks the untracked local.properties (SDK path) and node_modules
    (Capacitor's Android libraries). Copy the first; link the second read-only."""
    src = repo / "android" / "local.properties"
    if src.exists():
        shutil.copyfile(src, wt / "android" / "local.properties")
    modules = repo / "node_modules"
    link = wt / "node_modules"
    if modules.exists() and not link.exists():
        if os.name == "nt":
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(modules)],
                capture_output=True,
                check=True,
            )
        else:
            link.symlink_to(modules, target_is_directory=True)


# --- validation -----------------------------------------------------------------------------


@dataclass
class SuiteResult:
    exit_code: int
    seconds: float
    tests: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    failed_cases: list[str] = field(default_factory=list)
    classes: dict[str, str] = field(default_factory=dict)  # class -> passed|failed
    tail: str = ""


def read_junit(results_dir: Path) -> SuiteResult:
    r = SuiteResult(exit_code=0, seconds=0.0)
    for xml in sorted(results_dir.glob("TEST-*.xml")):
        suite = ET.parse(xml).getroot()
        name = suite.get("name", xml.stem).rsplit(".", 1)[-1]
        r.tests += int(suite.get("tests", 0))
        f, e = int(suite.get("failures", 0)), int(suite.get("errors", 0))
        r.failures += f
        r.errors += e
        r.skipped += int(suite.get("skipped", 0))
        r.classes[name] = "failed" if (f or e) else "passed"
        for case in suite.iter("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                r.failed_cases.append(f"{name}.{case.get('name')}")
    return r


def run_suite(wt: Path, test_filters: list[str] | None = None) -> SuiteResult:
    results = wt / TEST_RESULTS
    if results.exists():
        shutil.rmtree(results)
    args = [FULL_SUITE_TASK]
    for t in test_filters or []:
        args += ["--tests", t]
    code, tail, secs = _gradle(wt, *args)
    r = read_junit(results)
    r.exit_code, r.seconds, r.tail = code, round(secs, 1), tail
    return r


@dataclass
class Validation:
    accepted: bool
    reasons: list[str]
    commits: list[str]
    regression_classes: list[str]
    regression_on_baseline: dict | None = None
    regression_on_head: dict | None = None
    full_suite_base: dict | None = None
    full_suite_head: dict | None = None
    guard_suites: dict = field(default_factory=dict)
    replay_suites: dict = field(default_factory=dict)


def test_classes_in(repo: Path, commit: str) -> tuple[list[str], list[str]]:
    """(test classes touched, non-test files touched) by one commit."""
    files = _git(repo, "show", "--name-only", "--format=", commit).split()
    tests, other = [], []
    for f in files:
        if f.startswith(TEST_SRC) and f.endswith(".java"):
            tests.append(f.split("/java/", 1)[1].removesuffix(".java").replace("/", "."))
        else:
            other.append(f)
    return tests, other


def validate_candidate(
    wt: Worktree, *, run=run_suite, base_suite: SuiteResult | None = None
) -> Validation:
    reasons: list[str] = []
    commits = _git(wt.path, "rev-list", "--reverse", f"{wt.base}..HEAD").split()
    v = Validation(accepted=False, reasons=reasons, commits=commits, regression_classes=[])
    if len(commits) < 2:
        reasons.append(f"need a regression commit then a fix commit; found {len(commits)}")
        return v
    tests, other = test_classes_in(wt.path, commits[0])
    v.regression_classes = tests
    if other or not tests:
        reasons.append(f"commit 1 must touch only test sources; also touched {other or 'nothing'}")
        return v

    _git(wt.path, "checkout", "--detach", commits[0])
    try:
        on_base = run(wt.path, tests)
    finally:
        _git(wt.path, "checkout", wt.branch)
    v.regression_on_baseline = asdict(on_base)
    if on_base.exit_code == 0:
        reasons.append("the regression passes without the fix: it does not reproduce the defect")
    elif on_base.tests == 0:
        reasons.append(
            "the regression did not compile or run on the baseline (no test results); "
            "a reproduction must fail on an assertion"
        )

    on_head = run(wt.path, tests)
    v.regression_on_head = asdict(on_head)
    if on_head.exit_code != 0 or on_head.failures or on_head.errors:
        reasons.append(f"the regression still fails with the fix: {on_head.failed_cases[:5]}")

    head = run(wt.path, None)
    v.full_suite_head = asdict(head)
    if base_suite is None:
        _git(wt.path, "checkout", "--detach", wt.base)
        try:
            base_suite = run(wt.path, None)
        finally:
            _git(wt.path, "checkout", wt.branch)
    v.full_suite_base = asdict(base_suite)
    new_failures = sorted(set(head.failed_cases) - set(base_suite.failed_cases))
    if new_failures:
        reasons.append(f"full suite: {len(new_failures)} new failures: {new_failures[:10]}")
    if head.tests < base_suite.tests:
        reasons.append(
            f"full suite ran fewer tests ({head.tests}) than the base ({base_suite.tests})"
        )
    v.guard_suites = {s: head.classes.get(s, "missing") for s in GUARD_SUITES}
    for s, status in v.guard_suites.items():
        if status != "passed":
            reasons.append(f"{s} is {status}")
    v.replay_suites = {c: s for c, s in head.classes.items() if c.endswith("ReplayTest")}
    broken = [
        c
        for c, s in v.replay_suites.items()
        if s != "passed" and base_suite.classes.get(c) == "passed"
    ]
    if broken:
        reasons.append(f"historical replays broken by the change: {broken}")
    v.accepted = not reasons
    return v


# --- the agent --------------------------------------------------------------------------------

FIX_SYSTEM = """\
You are the fix engineer for Vergeo (Android, Java). You work ONLY inside the git worktree that is
your current directory, on branch {branch}. A triage agent found a suspected defect; its analysis
is below. Your job is a candidate fix that a human will review. You never merge, push, install,
or touch the device; those commands are blocked.

Required order (an automated validator checks it and rejects anything else):
1. Read the evidence and the relevant source. Find the narrowest place the defect lives.
2. Write a regression/replay test under android/app/src/test/java/com/vergeo/app/ that reproduces
   the defect with the field values from the analysis. Commit it ALONE:
   git add <test file> && git commit -m "test: reproduce <defect>"
   Run it and confirm it FAILS on an assertion (not a compile error):
   android\\gradlew.bat -p android :app:testReleaseUnitTest --tests <Class> --console=plain
3. Make the narrowest fix in main sources. Preserve every historical case: generic alarms,
   direct routes, folded routes, arrival paths, ML on/off parity. Commit it separately:
   git commit -am "fix: <what>"
4. Run the new test again and the existing replays it touches. Do not run the whole suite more
   than once; the validator runs it.
Do not weaken or delete existing tests. If you cannot reproduce the defect, stop and say so:
an honest "not reproduced" is a valid outcome.
Finish with a short report: root cause, the test, the change, behaviour changed, behaviour
intentionally unchanged, risks.
"""


async def run_fix_agent(
    wt: Worktree, analysis: IncidentAnalysis, *, model: str, max_turns: int = 120
) -> dict:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        HookMatcher,
        PermissionResultAllow,
        PermissionResultDeny,
        ResultMessage,
        TextBlock,
    )

    denials: list[dict] = []

    async def can_use_tool(tool_name, tool_input, context):
        ok, why = check_tool_call(tool_name, tool_input, wt.path)
        if ok:
            return PermissionResultAllow()
        denials.append({"tool": tool_name, "input": str(tool_input)[:300], "why": why})
        return PermissionResultDeny(message=why)

    async def pre_tool(input_data, tool_use_id, context):
        ok, why = check_tool_call(
            input_data.get("tool_name", ""), input_data.get("tool_input", {}) or {}, wt.path
        )
        if ok:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": why,
            }
        }

    options = ClaudeAgentOptions(
        system_prompt=FIX_SYSTEM.format(branch=wt.branch),
        model=model,
        cwd=str(wt.path),
        tools=sorted(ALLOWED_TOOLS),
        allowed_tools=[],  # nothing pre-approved: every call goes through can_use_tool
        can_use_tool=can_use_tool,
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool])]},
        setting_sources=[],
        max_turns=max_turns,
        env={"JAVA_HOME": JAVA_HOME},
    )
    started = time.perf_counter()
    report, result_meta = [], {}
    async with ClaudeSDKClient(options=options) as client:
        await client.query("Triage analysis (JSON):\n" + analysis.model_dump_json(indent=1))
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                report += [b.text for b in msg.content if isinstance(b, TextBlock)]
            elif isinstance(msg, ResultMessage):
                usage = msg.usage or {}
                result_meta = {
                    "turns": msg.num_turns,
                    "cost_usd": msg.total_cost_usd,
                    "output_tokens": usage.get("output_tokens"),
                    "subtype": msg.subtype,
                }
    return {
        "final_report": report[-1] if report else "",
        "denied_calls": denials,
        "wall_seconds": round(time.perf_counter() - started, 1),
        **result_meta,
    }


# --- review artifact --------------------------------------------------------------------------


def write_review(
    out_dir: Path, wt: Worktree, analysis: IncidentAnalysis, agent: dict, v: Validation
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    diff = _git(wt.path, "diff", f"{wt.base}..HEAD")
    stat = _git(wt.path, "diff", "--stat", f"{wt.base}..HEAD")
    (out_dir / "candidate.diff").write_text(diff, encoding="utf-8")
    fs_h, fs_b = v.full_suite_head or {}, v.full_suite_base or {}
    lines = [
        f"# Candidate fix: {analysis.defect_hypothesis or 'unnamed defect'}",
        "",
        f"**Status: {'READY FOR HUMAN REVIEW' if v.accepted else 'REJECTED BY VALIDATION'}.** "
        "Not merged. Not installed. A human must review, merge and install, or discard.",
        "",
        f"- Worktree: `{wt.path}`  branch `{wt.branch}`  base `{wt.base[:10]}`",
        f"- Incident: report `{analysis.report_id}`, verdict `{analysis.verdict.value}`, "
        f"trigger `{analysis.trigger_path.value}`",
        "",
        "## Causal chain (from triage)",
        *[f"- {s.time or ''} {s.step}" for s in analysis.causal_chain],
        "",
        "## Reproduction",
        f"- Regression commit: `{v.commits[0][:10] if v.commits else '-'}`, classes "
        f"{v.regression_classes}",
        f"- On the baseline: exit {(v.regression_on_baseline or {}).get('exit_code')}, failures "
        f"{(v.regression_on_baseline or {}).get('failed_cases')}",
        f"- With the fix: exit {(v.regression_on_head or {}).get('exit_code')}",
        "",
        "## Full JVM suite",
        f"- Base: {fs_b.get('tests')} tests, {fs_b.get('failures')} failures, "
        f"{fs_b.get('errors')} errors",
        f"- Candidate: {fs_h.get('tests')} tests, {fs_h.get('failures')} failures, "
        f"{fs_h.get('errors')} errors ({fs_h.get('seconds')} s)",
        f"- Guard suites: {v.guard_suites}",
        f"- Historical replays: {sum(1 for s in v.replay_suites.values() if s == 'passed')}/"
        f"{len(v.replay_suites)} passed",
        "",
        "## Validation verdict",
        *([f"- {r}" for r in v.reasons] or ["- all gates passed"]),
        "",
        "## Change",
        "```",
        stat.strip(),
        "```",
        "",
        "## Fix agent report",
        agent.get("final_report", "").strip(),
        "",
        f"Denied tool calls: {len(agent.get('denied_calls', []))}. Agent wall time "
        f"{agent.get('wall_seconds')} s, cost {agent.get('cost_usd')}.",
        "",
        "## Human approval required",
        "Passing tests are necessary, not sufficient. Review the diff against the field evidence, "
        "then merge and install by hand, or discard the worktree.",
    ]
    path = out_dir / "REVIEW.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "validation.json").write_text(json.dumps(asdict(v), indent=2), encoding="utf-8")
    (out_dir / "agent.json").write_text(json.dumps(agent, indent=2), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Replay-gated candidate fix for a triaged defect.")
    p.add_argument("--analysis", type=Path, required=True, help="triage result.json")
    p.add_argument(
        "--vergeo-repo", type=Path, default=Path.home() / "AndroidStudioProjects" / "Vergeo"
    )
    p.add_argument("--base", default="HEAD", help="commit to branch from")
    p.add_argument(
        "--backtest",
        action="store_true",
        help="run even without needs_fix_investigation (historical replay of a known "
        "defect at the commit before its human fix)",
    )
    p.add_argument("--model", default="claude-opus-5-5")
    args = p.parse_args(argv)

    data = json.loads(args.analysis.read_text(encoding="utf-8"))
    analysis = IncidentAnalysis.model_validate(data.get("analysis", data))
    if not (analysis.needs_fix_investigation or args.backtest):
        print("triage did not ask for a fix investigation; nothing to do")
        return 0
    fix_id = f"{datetime.now():%m%d%H%M}-{uuid.uuid4().hex[:4]}"
    make = create_backtest_clone if args.backtest else create_worktree
    wt = make(args.vergeo_repo, fix_id, args.base)
    prepare_android_build(args.vergeo_repo, wt.path)
    print(f"worktree {wt.path} on {wt.branch} at {wt.base[:10]}", file=sys.stderr)
    agent = asyncio.run(run_fix_agent(wt, analysis, model=args.model))
    v = validate_candidate(wt)
    review = write_review(REVIEWS_DIR / fix_id, wt, analysis, agent, v)
    print(f"{'ACCEPTED for human review' if v.accepted else 'REJECTED'}: {review}")
    return 0 if v.accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())

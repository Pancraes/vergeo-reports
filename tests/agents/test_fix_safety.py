"""The fix agent's safety boundary: isolation, denied actions, and the validation gate."""

import re
import subprocess
from pathlib import Path

import pytest

from app.agents import fix
from app.agents.fix import (
    SuiteResult,
    Worktree,
    _git,
    _gradle,
    check_tool_call,
    create_backtest_clone,
    create_worktree,
    validate_candidate,
)

ROOT = Path("C:/work/Vergeo-fixagent-1") if fix.os.name == "nt" else Path("/work/wt")


@pytest.mark.parametrize(
    "cmd",
    [
        "git push origin fix-agent/1",
        "git merge fix-agent/1",
        "git rebase master",
        "git reset --hard HEAD~3",
        "git checkout master",
        "git switch main",
        "git branch -D integration/2.0",
        "git worktree remove ../Vergeo",
        "adb install -r app-release.apk",
        "android/gradlew.bat -p android installRelease",
        "android\\gradlew.bat -p android :app:assembleRelease",
        "./gradlew bundleRelease",
        "curl https://example.com",
        "gh pr merge 3",
        "cd .. && ls",
        "cd C:/Users && dir",
        "rm -rf /",
        "npm install left-pad",
    ],
)
def test_dangerous_commands_are_denied(cmd):
    ok, why = check_tool_call("Bash", {"command": cmd}, ROOT)
    assert not ok, cmd
    assert why


@pytest.mark.parametrize(
    "cmd",
    [
        "git status",
        "git add android/app/src/test/java/com/vergeo/app/XTest.java",
        'git commit -m "test: reproduce"',
        "git diff HEAD~1",
        "android\\gradlew.bat -p android :app:testReleaseUnitTest --tests com.vergeo.app.XTest",
    ],
)
def test_ordinary_development_commands_are_allowed(cmd):
    assert check_tool_call("Bash", {"command": cmd}, ROOT) == (True, "ok")


def test_file_tools_are_confined_to_the_worktree():
    assert check_tool_call("Edit", {"file_path": str(ROOT / "android/x.java")}, ROOT)[0]
    assert check_tool_call("Read", {"file_path": "android/x.java"}, ROOT)[0]  # relative = inside
    assert not check_tool_call("Read", {"file_path": str(Path.home() / ".ssh/id_rsa")}, ROOT)[0]
    assert not check_tool_call("Write", {"file_path": str(ROOT.parent / "Vergeo/x")}, ROOT)[0]
    assert not check_tool_call("Grep", {"path": "../Vergeo"}, ROOT)[0]


def test_tools_outside_the_list_are_denied():
    assert not check_tool_call("WebFetch", {"url": "https://x"}, ROOT)[0]
    assert not check_tool_call("mcp__other__deploy", {}, ROOT)[0]


def test_bash_paths_outside_the_worktree_are_denied():
    assert not check_tool_call("Bash", {"command": "type C:\\Users\\me\\secrets.txt"}, ROOT)[0]


@pytest.mark.parametrize(
    "args",
    [
        ("merge", "x"),
        ("push",),
        ("reset", "--hard"),
        ("checkout", "master"),
        ("worktree", "remove", "x"),
        ("branch", "-D", "x"),
        ("commit", "-m", "x"),
    ],
)
def test_the_workflow_itself_cannot_run_mutating_git(tmp_path, args):
    with pytest.raises(PermissionError):
        _git(tmp_path, *args)


@pytest.mark.parametrize(
    "task", [":app:installRelease", ":app:assembleRelease", ":app:bundleRelease", "installDebug"]
)
def test_the_workflow_itself_runs_only_unit_test_tasks(tmp_path, task):
    with pytest.raises(PermissionError):
        _gradle(tmp_path, task)


def test_fix_module_has_no_unguarded_subprocess_calls():
    """Every subprocess in fix.py is inside _git, _gradle or the node_modules junction."""
    src = Path(fix.__file__).read_text(encoding="utf-8")
    calls = [m.start() for m in re.finditer(r"subprocess\.run\(", src)]
    owners = []
    for pos in calls:
        owner = re.findall(r"\ndef (\w+)\(", src[:pos])[-1]
        owners.append(owner)
    assert set(owners) <= {"_git", "_gradle", "prepare_android_build"}
    assert "mklink" in src and not re.search(r"\"(merge|push)\"", src)


# --- worktree isolation and the validation gate, on a throwaway repo ------------------------


def sh(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "Vergeo"
    r.mkdir()
    sh(r, "init", "-q", "-b", "master")
    sh(r, "config", "user.email", "t@example.com")
    sh(r, "config", "user.name", "t")
    (r / "android/app/src/main/java/com/vergeo/app").mkdir(parents=True)
    (r / "android/app/src/main/java/com/vergeo/app/Gate.java").write_text("class Gate {}\n")
    sh(r, "add", "-A")
    sh(r, "commit", "-qm", "base")
    return r


def test_worktree_is_isolated_from_the_main_checkout(repo, tmp_path):
    main_head = sh(repo, "rev-parse", "HEAD").strip()
    wt = create_worktree(repo, "t1", "HEAD", parent=tmp_path)
    assert wt.path.exists() and wt.path != repo
    assert sh(wt.path, "rev-parse", "--abbrev-ref", "HEAD").strip() == "fix-agent/t1"
    (wt.path / "android/app/src/main/java/com/vergeo/app/Gate.java").write_text("changed\n")
    sh(wt.path, "commit", "-qam", "wip")
    assert sh(repo, "rev-parse", "HEAD").strip() == main_head
    assert sh(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "master"
    assert (
        repo / "android/app/src/main/java/com/vergeo/app/Gate.java"
    ).read_text() == "class Gate {}\n"


def test_a_backtest_cannot_see_commits_after_its_base(repo, tmp_path):
    base = sh(repo, "rev-parse", "HEAD").strip()
    (repo / "android/app/src/main/java/com/vergeo/app/Gate.java").write_text("human fix\n")
    sh(repo, "commit", "-qam", "fix(gate): the human answer")
    answer = sh(repo, "rev-parse", "HEAD").strip()

    wt = create_backtest_clone(repo, "b1", base, parent=tmp_path)
    assert sh(wt.path, "rev-parse", "HEAD").strip() == base
    assert sh(wt.path, "rev-parse", "--abbrev-ref", "HEAD").strip() == "fix-agent/b1"
    assert answer not in sh(wt.path, "log", "--all", "--format=%H")
    assert sh(wt.path, "remote").strip() == ""
    with pytest.raises(subprocess.CalledProcessError):
        sh(wt.path, "cat-file", "-e", answer)
    assert sh(repo, "rev-parse", "HEAD").strip() == answer  # the source repo is untouched


@pytest.mark.parametrize(
    "args",
    [
        ("fetch", "origin"),
        ("fetch", "-q", "--no-tags", "https://example.com/r", "a" * 40 + ":refs/heads/fix-agent/x"),
        ("fetch", "-q", "--no-tags", ".", "a" * 40 + ":refs/heads/master"),
        ("init", "--bare", "x"),
    ],
)
def test_the_workflow_fetches_only_one_local_commit_into_a_fix_branch(tmp_path, args):
    with pytest.raises(PermissionError):
        fix._git(tmp_path, *args)


def candidate(repo, tmp_path, *, test_only_first=True, fix_commit=True) -> Worktree:
    wt = create_worktree(repo, "t2", "HEAD", parent=tmp_path)
    t = wt.path / "android/app/src/test/java/com/vergeo/app/GateReplayTest.java"
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_text("class GateReplayTest {}\n")
    if not test_only_first:
        (wt.path / "android/app/src/main/java/com/vergeo/app/Gate.java").write_text("x\n")
    sh(wt.path, "add", "-A")
    sh(wt.path, "commit", "-qm", "test: reproduce")
    if fix_commit:
        (wt.path / "android/app/src/main/java/com/vergeo/app/Gate.java").write_text("fixed\n")
        sh(wt.path, "commit", "-qam", "fix: gate")
    return wt


def fake_run(on_baseline_passes=False, head_fails=False, new_failure=False):
    def run(path: Path, tests):
        fixed = "fixed" in (path / "android/app/src/main/java/com/vergeo/app/Gate.java").read_text()
        classes = {
            "GoldenReliabilityCorpusTest": "passed",
            "MlShadowIsolationTest": "passed",
            "MlResearchCaptureTest": "passed",
            "MlShadowCorpusParityTest": "passed",
            "MlShadowDecisionEquivalenceTest": "passed",
            "SomeArrivalReplayTest": "passed",
        }
        if tests:  # the regression alone
            failing = not fixed and not on_baseline_passes or (fixed and head_fails)
            return SuiteResult(
                exit_code=1 if failing else 0,
                seconds=1,
                tests=1,
                failures=1 if failing else 0,
                failed_cases=["GateReplayTest.t"] if failing else [],
            )
        failed = ["OtherTest.t"] if (fixed and new_failure) else []
        return SuiteResult(
            exit_code=1 if failed else 0,
            seconds=1,
            tests=500,
            failures=len(failed),
            failed_cases=failed,
            classes=classes,
        )

    return run


def test_a_proper_candidate_is_accepted_for_human_review(repo, tmp_path):
    v = validate_candidate(candidate(repo, tmp_path), run=fake_run())
    assert v.accepted, v.reasons
    assert v.regression_classes == ["com.vergeo.app.GateReplayTest"]
    assert v.guard_suites["GoldenReliabilityCorpusTest"] == "passed"


def test_a_regression_that_passes_without_the_fix_is_rejected(repo, tmp_path):
    v = validate_candidate(candidate(repo, tmp_path), run=fake_run(on_baseline_passes=True))
    assert not v.accepted
    assert any("does not reproduce" in r for r in v.reasons)


def test_a_fix_that_breaks_another_test_is_rejected(repo, tmp_path):
    v = validate_candidate(candidate(repo, tmp_path), run=fake_run(new_failure=True))
    assert not v.accepted and any("new failures" in r for r in v.reasons)


def test_test_and_fix_in_one_commit_is_rejected(repo, tmp_path):
    v = validate_candidate(
        candidate(repo, tmp_path, test_only_first=False, fix_commit=False), run=fake_run()
    )
    assert not v.accepted


def test_validation_returns_the_worktree_to_its_branch(repo, tmp_path):
    wt = candidate(repo, tmp_path)
    validate_candidate(wt, run=fake_run())
    assert sh(wt.path, "rev-parse", "--abbrev-ref", "HEAD").strip() == "fix-agent/t2"

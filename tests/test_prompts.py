from hone.bitbucket import PullRequest
from hone.prompts import SYSTEM_PROMPT, initial_context


def test_initial_context_contains_metadata_and_diffstat():
    pr = PullRequest(id=7, title="Add thing", description="does a thing",
                     author="Richard", source_branch="feature",
                     source_commit="c" * 40, dest_branch="main",
                     dest_commit="d" * 40)
    ctx = initial_context(pr, [("app.py", "modified", 12, 3),
                               ("new.py", "added", 40, 0)])
    assert "Add thing" in ctx
    assert "feature" in ctx and "main" in ctx
    assert "c" * 8 in ctx
    assert "app.py" in ctx and "+12/-3" in ctx
    assert "new.py" in ctx and "+40/-0" in ctx


def test_system_prompt_tells_agent_to_investigate_and_post():
    lowered = SYSTEM_PROMPT.lower()
    for word in ("rg_search", "read_file", "git_log", "run_static_check",
                 "post_review", "severity", "advisory"):
        assert word in lowered


def test_initial_context_has_no_previous_rounds_section_by_default():
    pr = PullRequest(id=7, title="Add thing", description="", author="R",
                     source_branch="feature", source_commit="c" * 40,
                     dest_branch="main", dest_commit="d" * 40)
    ctx = initial_context(pr, [("app.py", "modified", 1, 1)])
    assert "previous review rounds" not in ctx.lower()


def test_initial_context_includes_previous_rounds_oldest_first():
    pr = PullRequest(id=7, title="Add thing", description="", author="R",
                     source_branch="feature", source_commit="c" * 40,
                     dest_branch="main", dest_commit="d" * 40)
    ctx = initial_context(pr, [], previous_reviews=["first round findings",
                                                    "second round findings"])
    assert "first round findings" in ctx
    assert "second round findings" in ctx
    # chronological: the most recent round reads last, closest to the task
    assert ctx.index("first round findings") < ctx.index(
        "second round findings")


def test_system_prompt_carries_delta_review_policy():
    lowered = SYSTEM_PROMPT.lower()
    # the re-review contract: verify priors, don't re-report standers,
    # only new findings count, and a short verdict is a valid review
    for phrase in ("previous review rounds", "no new findings",
                   "do not report", "new findings"):
        assert phrase in lowered

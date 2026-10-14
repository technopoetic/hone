"""Marker protocol: mute/dedup state lives in PR comments, not local files.

Contract notes: get_comments returns bodies oldest-first (verified against
the live API's default ordering), so "latest marker wins" means last in the
list wins."""

from hone.markers import (
    MUTE_MARKER,
    UNMUTE_MARKER,
    control_comment,
    latest_control_state,
    prior_review_bodies,
)
from hone.tools import MARKER_PREFIX


def review_comment(sha: str, body: str = "## findings") -> str:
    return f"{MARKER_PREFIX}{sha} -->\n{body}"


def test_no_control_markers_means_unset():
    assert latest_control_state(["a plain comment",
                                 review_comment("abc123")]) is None


def test_no_comments_at_all_is_unset():
    assert latest_control_state([]) is None


def test_mute_marker_sets_muted():
    comments = [review_comment("a"), control_comment("mute")]
    assert latest_control_state(comments) == "muted"


def test_unmute_marker_sets_unmuted():
    comments = [control_comment("mute"), review_comment("a"),
                control_comment("unmute")]
    assert latest_control_state(comments) == "unmuted"


def test_last_marker_in_list_wins():
    # order, not count: whichever control marker appears last chronologically
    # is the state, regardless of how many came before it
    muted_then_unmuted = [control_comment("mute"), control_comment("unmute")]
    unmuted_then_muted = [control_comment("unmute"), control_comment("mute")]
    assert latest_control_state(muted_then_unmuted) == "unmuted"
    assert latest_control_state(unmuted_then_muted) == "muted"


def test_control_comment_mute_is_marker_plus_visible_note():
    text = control_comment("mute")
    assert text.startswith(MUTE_MARKER)
    assert "@hone unmute" in text  # the visible note teaches the off-ramp


def test_control_comment_unmute_is_marker_plus_visible_note():
    text = control_comment("unmute")
    assert text.startswith(UNMUTE_MARKER)
    assert "reviewed" in text


def test_prior_reviews_extracts_own_review_bodies():
    comments = [
        "a plain human comment",
        review_comment("sha1", "first round findings"),
        "another human comment",
        review_comment("sha2", "second round findings"),
    ]
    assert prior_review_bodies(comments) == [
        "first round findings", "second round findings"]


def test_prior_reviews_strips_marker_line():
    body = prior_review_bodies([review_comment("sha1")])[0]
    assert MARKER_PREFIX not in body
    assert body == "## findings"


def test_prior_reviews_skips_empty_bodies():
    comments = [f"{MARKER_PREFIX}abc123 -->"]  # marker with no body at all
    assert prior_review_bodies(comments) == []


def test_prior_reviews_empty_when_no_reviews():
    assert prior_review_bodies(["hello", "<!-- someoneelse:marker -->"]) == []

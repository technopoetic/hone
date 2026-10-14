"""Marker protocol: hone's control state lives in PR comments, not in
local files on the droplet. Mute/dedup state therefore survives redeploys,
is human-visible and human-editable on the forge, and rides the one
paginated get_comments call run_review already pays for its dedup scan —
no extra API traffic.

Ordering contract: Bitbucket's comments API returns bodies oldest-first
(verified against the live default ordering), so "latest marker wins"
means last in the list wins."""

from __future__ import annotations

from .tools import MARKER_PREFIX

# Control markers end with --> so they render invisibly on Bitbucket; each
# control comment pairs the hidden marker (the machine-readable bit) with a
# visible one-liner so a human reading the PR can audit the state.
MUTE_MARKER = "<!-- hone:muted -->"
UNMUTE_MARKER = "<!-- hone:unmuted -->"

MUTE_NOTE = ("hone muted for this pull request — pushes will not be "
             "reviewed. Comment exactly `@hone unmute` to resume.")
UNMUTE_NOTE = ("hone resumed for this pull request — pushes will be "
               "reviewed again.")


def control_comment(action: str) -> str:
    """Full comment text that records a control state. `action` is the
    CommandRequest action ("mute" or "unmute")."""
    if action == "mute":
        return f"{MUTE_MARKER}\n{MUTE_NOTE}"
    return f"{UNMUTE_MARKER}\n{UNMUTE_NOTE}"


def latest_control_state(comments: list[str]) -> str | None:
    """'muted', 'unmuted', or None (never explicitly set): the state carried
    by the most recent control marker in the comment list."""
    state = None
    for comment in comments:
        if MUTE_MARKER in comment:
            state = "muted"
        elif UNMUTE_MARKER in comment:
            state = "unmuted"
    return state


def prior_review_bodies(comments: list[str]) -> list[str]:
    """Bodies of hone's own prior review comments, marker line stripped,
    oldest first — the 'previous rounds' the delta prompt checks findings
    against. Comments from anyone else are ignored."""
    bodies: list[str] = []
    for comment in comments:
        start = comment.find(MARKER_PREFIX)
        if start == -1:
            continue
        end = comment.find("-->", start)
        body = comment[end + len("-->"):].strip() if end != -1 else ""
        if body:
            bodies.append(body)
    return bodies

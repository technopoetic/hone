"""The agent loop. Max turns capped because agents that don't stop are a
real failure mode, not a hypothetical one."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import anthropic

from .prompts import FINAL_TURN_MESSAGE

SYNTHETIC_BASE_URL = "https://api.synthetic.new/anthropic"

log = logging.getLogger("hone")


@dataclass(frozen=True)
class AgentOutcome:
    final_text: str | None
    posted: bool


def make_client(api_key: str) -> anthropic.Anthropic:
    # No default timeout on the SDK is not acceptable: one hung request
    # would freeze the whole review job (and later, a webhook worker).
    return anthropic.Anthropic(api_key=api_key, base_url=SYNTHETIC_BASE_URL,
                               timeout=300.0, max_retries=1)


def _text_of(response) -> str:
    return "".join(b.text for b in response.content if b.type == "text")


def run_agent(client, model: str, system: str, initial_context: str,
              toolbox, max_turns: int = 15) -> AgentOutcome:
    messages = [{"role": "user", "content": initial_context}]
    for turn in range(1, max_turns + 1):
        if turn == max_turns:
            # nudge the model to conclude rather than vanish past the cap
            messages.append({"role": "user", "content": FINAL_TURN_MESSAGE})
        resp = client.messages.create(
            model=model, max_tokens=8192, system=system,
            tools=toolbox.schemas(), messages=messages,
        )
        messages.append({"role": "assistant",
                         "content": [b.model_dump() for b in resp.content]})
        if resp.stop_reason != "tool_use":
            log.info("turn %d: stop_reason=%s (ending)", turn,
                     resp.stop_reason)
            return AgentOutcome(final_text=_text_of(resp), posted=False)
        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            log.info("turn %d: %s(%s)", turn, block.name, block.input)
            result = toolbox.execute(block.name, block.input)
            log.info("turn %d: %s → %d chars%s", turn, block.name,
                     len(result.content),
                     " ERROR" if result.is_error else "")
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": result.content,
                            "is_error": result.is_error})
            if block.name == "post_review" and not result.is_error:
                return AgentOutcome(final_text=_text_of(resp), posted=True)
        messages.append({"role": "user", "content": results})
    return AgentOutcome(final_text=None, posted=False)

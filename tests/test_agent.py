from dataclasses import dataclass, field

from hone.agent import run_agent
from hone.prompts import FINAL_TURN_MESSAGE
from hone.tools import ToolResult


@dataclass
class FakeBlock:
    type: str
    text: str | None = None
    name: str | None = None
    input: dict = field(default_factory=dict)
    id: str = "toolu_x"

    def model_dump(self):
        d = {"type": self.type}
        if self.type == "text":
            d["text"] = self.text
        else:
            d.update(id=self.id, name=self.name, input=self.input)
        return d


@dataclass
class FakeResponse:
    content: list
    stop_reason: str


class FakeClientWrapper:
    """Mimics anthropic.Client.messages.create for the loop under test."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    class _Messages:
        def __init__(self, outer):
            self._outer = outer

        def create(self, **kwargs):
            self._outer.calls.append(kwargs)
            return self._outer._responses.pop(0)

    @property
    def messages(self):
        return FakeClientWrapper._Messages(self)


class FakeToolbox:
    def __init__(self, behavior=None):
        self._behavior = behavior or {}
        self.called = []

    def schemas(self):
        return [{"name": n} for n in ("get_pr_diff", "post_review")]

    def execute(self, name, inputs):
        self.called.append(name)
        return self._behavior.get(name, ToolResult(f"{name} ok"))


def test_loop_executes_tools_until_posted_then_stops():
    responses = [
        FakeResponse(content=[FakeBlock(type="tool_use", name="get_pr_diff")],
                     stop_reason="tool_use"),
        FakeResponse(content=[FakeBlock(type="text", text="done"),
                              FakeBlock(type="tool_use", name="post_review",
                                        input={"markdown": "LG?"})],
                     stop_reason="tool_use"),
    ]
    client = FakeClientWrapper(responses)
    toolbox = FakeToolbox()
    outcome = run_agent(client, model="m", system="s",
                        initial_context="ctx", toolbox=toolbox,
                        max_turns=10)
    assert outcome.posted is True
    assert toolbox.called == ["get_pr_diff", "post_review"]
    assert len(client.calls) == 2


def test_text_only_response_ends_without_posting():
    client = FakeClientWrapper([
        FakeResponse(content=[FakeBlock(type="text", text="no tools here")],
                     stop_reason="end_turn"),
    ])
    outcome = run_agent(client, model="m", system="s",
                        initial_context="ctx", toolbox=FakeToolbox(),
                        max_turns=10)
    assert outcome.posted is False
    assert outcome.final_text == "no tools here"


def test_turn_limit_returns_unposted():
    responses = [FakeResponse(
        content=[FakeBlock(type="tool_use", name="get_pr_diff",
                           id=f"t{i}")],
        stop_reason="tool_use") for i in range(3)]
    outcome = run_agent(FakeClientWrapper(responses), model="m", system="s",
                        initial_context="ctx", toolbox=FakeToolbox(),
                        max_turns=3)
    assert outcome.posted is False
    assert outcome.final_text is None


def test_final_turn_injects_wrap_up_nudge():
    responses = [FakeResponse(
        content=[FakeBlock(type="tool_use", name="get_pr_diff",
                           id=f"t{i}")],
        stop_reason="tool_use") for i in range(3)]
    client = FakeClientWrapper(responses)
    run_agent(client, model="m", system="s", initial_context="ctx",
              toolbox=FakeToolbox(), max_turns=3)
    # the fake records messages by reference, so inspect the final list
    user_texts = [m["content"] for m in client.calls[-1]["messages"]
                  if isinstance(m.get("content"), str)]
    assert user_texts[-1] == FINAL_TURN_MESSAGE

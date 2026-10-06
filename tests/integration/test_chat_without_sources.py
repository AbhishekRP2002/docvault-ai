"""Keep source-free chat turns on the real agent and persisted conversation path."""

import json
from uuid import uuid4

import pytest
from test_api import api as api
from test_api import ask, create_chat, ready_source
from test_api import test_database_url as test_database_url
from test_chat_validation import install_agent_script

from docvault.tools.models import AgentToolCall

pytestmark = pytest.mark.integration

CLARIFICATION = {
    "response": "Which file should I use? Select it with +, or upload it in Files first.",
    "suggestions": ["How do I upload a file?"],
    "citation_ids": [],
    "outcome": "clarification_needed",
    "response_kind": "clarification",
}


@pytest.mark.parametrize("accept", ["application/json", "text/event-stream"])
@pytest.mark.parametrize(
    "question,answer",
    [
        ("When is payment due in my contract?", CLARIFICATION),
        (
            "How can you help me?",
            {
                "response": "I can help you find documents and answer questions about files you select.",
                "suggestions": [],
                "citation_ids": [],
                "outcome": "answered",
                "response_kind": "assistant_help",
            },
        ),
    ],
)
def test_no_files_question_reaches_agent_and_persists_reply(
    api, monkeypatch, accept, question, answer
):
    """Send an ordinary model turn, including SSE, instead of a UI/API rejection."""
    install_agent_script(api, monkeypatch, [[]], answer)
    chat_id = create_chat(api, [])
    response = ask(api, chat_id, question, accept=accept)
    assert response.status_code == 200, response.text
    if accept == "text/event-stream":
        frames = []
        for frame in response.text.split("\n\n"):
            lines = frame.splitlines()
            if lines and lines[0].startswith("event: "):
                payload = json.loads(
                    "\n".join(line[6:] for line in lines if line.startswith("data: "))
                )
                frames.append((lines[0][7:], payload))
        assert frames[0][0] == "message.started"
        assert frames[-1][0] == "answer.completed"
        assert (
            "".join(payload["text"] for name, payload in frames if name == "answer.delta")
            == answer["response"]
        )
        result = frames[-1][1]["message"]
    else:
        result = response.json()
    assert result["status"] == "complete"
    assert result["content"] == answer["response"]
    assert result["outcome"] == answer["outcome"]
    assert result["version_ids"] == result["citations"] == result["agent_trace"] == []
    assert len(api.llm.calls) == 1
    assert api.llm.calls[0]["question"] == question
    assert api.llm.calls[0]["selected_documents"] == api.llm.calls[0]["evidence"] == []
    history = api.client.get(f"/v1/chats/{chat_id}/messages").json()["items"]
    assert len(history) == 2
    assert history[0]["content"] == question and history[0]["version_ids"] == []
    assert history[1] == result


def test_no_files_tool_request_returns_selection_needed_to_agent(api, monkeypatch):
    """A model-chosen document tool cannot silently read the entire library."""
    ready_source()
    install_agent_script(
        api,
        monkeypatch,
        [
            [
                AgentToolCall(
                    id="missing-selection",
                    name="retrieve_relevant_chunks",
                    arguments={"query": "Payment due date"},
                )
            ],
            [],
        ],
        CLARIFICATION,
    )
    chat_id = create_chat(api, [])
    response = ask(api, chat_id)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "complete" and result["content"] == CLARIFICATION["response"]
    assert result["version_ids"] == result["citations"] == []
    assert result["agent_trace"][0]["status"] == "selection_needed"
    payload = api.llm.calls[0]
    assert payload["selected_documents"] == payload["evidence"] == []
    assert payload["observations"][0]["content"]["status"] == "selection_needed"


def test_attach_after_clarification_keeps_retry_empty_and_grounds_next_turn(api, monkeypatch):
    """Attaching a source changes subsequent questions, never an earlier turn's snapshot."""
    original_chat_turn = api.llm.stream_chat_turn
    install_agent_script(api, monkeypatch, [[], []], CLARIFICATION)
    created = api.client.post("/v1/chats", json={})
    assert created.status_code == 201, created.text
    chat_id = created.json()["id"]
    first = ask(api, chat_id).json()
    assert first["status"] == "complete"
    source = ready_source()
    selected = api.client.patch(f"/v1/chats/{chat_id}", json={"version_ids": [source.version]})
    assert selected.status_code == 200, selected.text
    retry = api.client.post(
        f"/v1/chats/{chat_id}/messages/{first['id']}/retry",
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "complete" and retry.json()["version_ids"] == []
    assert api.llm.calls[-1]["selected_documents"] == []
    monkeypatch.setattr(api.llm, "stream_chat_turn", original_chat_turn)
    next_turn = ask(api, chat_id).json()
    assert next_turn["status"] == "complete" and "30 days" in next_turn["content"]
    assert next_turn["version_ids"] == [source.version]
    assert {citation["version_id"] for citation in next_turn["citations"]} == {source.version}
    history = api.client.get(f"/v1/chats/{chat_id}/messages").json()["items"]
    assert [message["version_ids"] for message in history] == [
        [],
        [],
        [source.version],
        [source.version],
    ]


def test_deselecting_files_does_not_reuse_previous_document_evidence(api, monkeypatch):
    """An empty selection stays empty even when the conversation previously cited a file."""
    chat_id = create_chat(api, [ready_source()])
    first = ask(api, chat_id).json()
    assert first["status"] == "complete" and first["citations"]
    cleared = api.client.patch(f"/v1/chats/{chat_id}", json={"version_ids": []})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["version_ids"] == []
    install_agent_script(api, monkeypatch, [[]], CLARIFICATION)
    reply = ask(api, chat_id, "Summarise it again").json()
    assert reply["status"] == "complete" and reply["outcome"] == "clarification_needed"
    assert reply["version_ids"] == reply["citations"] == []
    assert api.llm.calls[-1]["selected_documents"] == api.llm.calls[-1]["evidence"] == []
    sessions = api.client.get("/v1/chats").json()["items"]
    assert next(item for item in sessions if item["id"] == chat_id)["version_ids"] == []

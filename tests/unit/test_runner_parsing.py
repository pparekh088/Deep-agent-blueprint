from __future__ import annotations

import json

from app.agent.prompts import render_research_prompt
from app.worker.runner import _message_text, _parse_agent_output


def test_parses_fenced_json_block():
    output = _parse_agent_output(
        'Findings ahead.\n```json\n{"summary": "found it", "proposed_actions": []}\n```'
    )
    assert output.summary == "found it"
    assert output.proposed_actions == []


def test_uses_last_fenced_block_when_several():
    text = (
        '```json\n{"summary": "draft"}\n```\nrevised:\n'
        '```json\n{"summary": "final", "sources": [{"url_or_id": "x"}]}\n```'
    )
    assert _parse_agent_output(text).summary == "final"


def test_parses_bare_json_object():
    output = _parse_agent_output(json.dumps({"summary": "bare", "details": {"k": 1}}))
    assert output.summary == "bare"
    assert output.details == {"k": 1}


def test_malformed_output_degrades_to_findings_only():
    output = _parse_agent_output("I looked around but forgot the contract { not json")
    assert "looked around" in output.summary
    assert output.proposed_actions == []


def test_message_text_handles_content_blocks():
    class Msg:
        content = [{"type": "text", "text": "part1 "}, {"type": "text", "text": "part2"}]

    assert _message_text(Msg()) == "part1 part2"
    assert _message_text({"content": "plain"}) == "plain"


def test_prompt_renders_action_catalog_from_adapter():
    from app.adapters.jira import JiraAdapter
    from app.adapters.websearch import WebSearchAdapter

    jira_prompt = render_research_prompt(JiraAdapter(base_url="https://jira.local"))
    assert "`update_issue`" in jira_prompt
    assert "`add_comment`" in jira_prompt
    assert "issue_key" in jira_prompt

    search_prompt = render_research_prompt(WebSearchAdapter(api_key="k" * 10))
    assert "research-only" in search_prompt

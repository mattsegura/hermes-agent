"""P4a: grounded pre-interview research.

Proves ``run_pre_interview_research`` grounds its brief in REAL web search
results (via the Hermes ``web`` toolset) when both the web backend and the
auxiliary slot are available, persists the fetched sources as evidence, and
marks ``grounded=True``. When the web toolset is unavailable or the aux slot is
blank, it degrades to the prior best-effort behavior marked ``grounded=False``
without crashing. The aux model is never asked to invent URLs when real results
exist -- it is handed the real results and its source URLs are filtered against
the fetched set.

All web + aux calls are mocked; nothing here touches the network.
"""

from __future__ import annotations

import json

from hermes_cli import company_launch as kli


_FAKE_HITS = [
    {
        "title": "FTC: Telemarketing Sales Rule",
        "url": "https://www.ftc.gov/legal-library/browse/rules/telemarketing-sales-rule",
        "snippet": "Federal rules governing outbound telemarketing and the Do Not Call registry.",
    },
    {
        "title": "DNC Registry",
        "url": "https://www.donotcall.gov/",
        "snippet": "The National Do Not Call Registry lets consumers limit telemarketing calls.",
    },
]


def _grounded_synthesis_response(*_args, **_kwargs):
    """Aux model returns a brief that cites the real URLs (plus an invented one
    that must be filtered out)."""
    payload = {
        "research": [
            {
                "query": "cold calling compliance",
                "summary": "Outbound cold-calling in the US is governed by the FTC "
                "Telemarketing Sales Rule and the National Do Not Call Registry.",
                "sources": [
                    "https://www.ftc.gov/legal-library/browse/rules/telemarketing-sales-rule",
                    "https://www.donotcall.gov/",
                    # An invented URL the model should never get away with:
                    "https://totally-made-up-source.example/fake",
                ],
            }
        ]
    }
    return json.dumps(payload), False


def test_grounded_research_uses_real_search_urls(monkeypatch):
    """With the web toolset returning fixed results, research returns ONLY those
    real URLs and is marked grounded=True; invented URLs are filtered out."""
    monkeypatch.setattr(kli, "_web_search_available", lambda: True)
    monkeypatch.setattr(kli, "aux_configured", lambda: True)
    monkeypatch.setattr(kli, "_web_search", lambda query, **kw: list(_FAKE_HITS))

    captured: dict = {}

    def _fake_call_model(system_prompt, user_payload, **kwargs):
        captured["system_prompt"] = system_prompt
        captured["user_payload"] = user_payload
        return _grounded_synthesis_response()

    monkeypatch.setattr(kli, "_call_model", _fake_call_model)

    result = kli.run_pre_interview_research("Launch a cold-calling sales workflow")

    assert result.ok is True
    assert result.degraded is False
    assert result.grounded is True
    assert result.items, "expected at least one research item"

    real_urls = {h["url"] for h in _FAKE_HITS}
    item_sources = set(result.items[0].sources)
    # Only real URLs survive; the invented one is dropped.
    assert item_sources <= real_urls
    assert item_sources, "grounded item should retain real sources"
    assert "https://totally-made-up-source.example/fake" not in item_sources

    # Fetched sources are persisted as evidence on the result.
    persisted_urls = {s["url"] for s in result.sources}
    assert persisted_urls == real_urls

    # The caller-facing dict view carries the real, citeable sources.
    dicts = result.as_dicts()
    assert dicts and set(dicts[0]["sources"]) <= real_urls


def test_grounded_research_feeds_real_results_to_model_not_invent(monkeypatch):
    """The model is handed the PROVIDED real results (so it has no reason to
    invent URLs), and the grounded prompt explicitly forbids inventing URLs."""
    monkeypatch.setattr(kli, "_web_search_available", lambda: True)
    monkeypatch.setattr(kli, "aux_configured", lambda: True)
    monkeypatch.setattr(kli, "_web_search", lambda query, **kw: list(_FAKE_HITS))

    captured: dict = {}

    def _fake_call_model(system_prompt, user_payload, **kwargs):
        captured["system_prompt"] = system_prompt
        captured["user_payload"] = user_payload
        return _grounded_synthesis_response()

    monkeypatch.setattr(kli, "_call_model", _fake_call_model)

    result = kli.run_pre_interview_research("Launch a cold-calling sales workflow")
    assert result.grounded is True

    # The synthesis call received the real search results as input.
    payload = captured["user_payload"]
    assert "search_results" in payload
    serialized = json.dumps(payload)
    for hit in _FAKE_HITS:
        assert hit["url"] in serialized

    # The prompt used is the grounded one and explicitly forbids invention.
    assert captured["system_prompt"] == kli._PRE_INTERVIEW_RESEARCH_PROMPT
    assert "NEVER invent" in captured["system_prompt"]
    assert "copied verbatim" in captured["system_prompt"]


def test_web_unavailable_falls_back_ungrounded(monkeypatch):
    """When the web toolset is unavailable, research falls back to the
    ungrounded model path (grounded=False) without crashing or searching."""
    monkeypatch.setattr(kli, "_web_search_available", lambda: False)
    monkeypatch.setattr(kli, "aux_configured", lambda: True)

    def _boom(*_a, **_kw):  # _web_search must NOT be called on this path
        raise AssertionError("web search should not run when web is unavailable")

    monkeypatch.setattr(kli, "_web_search", _boom)

    def _fake_call_model(system_prompt, user_payload, **kwargs):
        assert system_prompt == kli._PRE_INTERVIEW_RESEARCH_PROMPT_UNGROUNDED
        payload = {
            "research": [
                {
                    "query": "cold calling",
                    "summary": "General domain knowledge about cold calling.",
                    "sources": ["https://example.org/guide"],
                }
            ]
        }
        return json.dumps(payload), False

    monkeypatch.setattr(kli, "_call_model", _fake_call_model)

    result = kli.run_pre_interview_research("Launch a cold-calling sales workflow")
    assert result.ok is True
    assert result.grounded is False
    assert result.items
    assert result.sources == []


def test_web_and_aux_unavailable_degrades_cleanly(monkeypatch):
    """When neither web nor aux is configured (the default offline test
    environment), research degrades without crashing and without a network
    call: degraded=True, grounded=False."""
    monkeypatch.setattr(kli, "_web_search_available", lambda: False)
    monkeypatch.setattr(kli, "aux_configured", lambda: False)

    def _boom(*_a, **_kw):
        raise AssertionError("no web search should occur when aux is blank")

    monkeypatch.setattr(kli, "_web_search", _boom)

    result = kli.run_pre_interview_research("Launch a cold-calling sales workflow")
    assert result.ok is False
    assert result.degraded is True
    assert result.grounded is False
    assert result.items == []


def test_grounded_attempt_with_no_hits_falls_back(monkeypatch):
    """If the web toolset is available but returns no real hits, research falls
    back to the ungrounded path rather than emitting an empty grounded brief."""
    monkeypatch.setattr(kli, "_web_search_available", lambda: True)
    monkeypatch.setattr(kli, "aux_configured", lambda: True)
    monkeypatch.setattr(kli, "_web_search", lambda query, **kw: [])

    seen_prompts: list[str] = []

    def _fake_call_model(system_prompt, user_payload, **kwargs):
        seen_prompts.append(system_prompt)
        payload = {
            "research": [
                {"query": "x", "summary": "ungrounded summary", "sources": []}
            ]
        }
        return json.dumps(payload), False

    monkeypatch.setattr(kli, "_call_model", _fake_call_model)

    result = kli.run_pre_interview_research("Launch a workflow")
    assert result.ok is True
    assert result.grounded is False
    # Only the ungrounded prompt was used (no grounded synthesis attempted).
    assert seen_prompts == [kli._PRE_INTERVIEW_RESEARCH_PROMPT_UNGROUNDED]


def test_web_search_parses_backend_envelope(monkeypatch):
    """The in-process _web_search normalizes the web_search_tool JSON envelope
    into {title,url,snippet} dicts."""
    envelope = {
        "success": True,
        "data": {
            "web": [
                {
                    "title": "Example",
                    "url": "https://example.com/a",
                    "description": "A description.",
                    "position": 1,
                },
                {"title": "No URL", "url": "", "description": "skipme"},
            ]
        },
    }

    import tools.web_tools as wt

    monkeypatch.setattr(wt, "web_search_tool", lambda query, limit=5: json.dumps(envelope))

    hits = kli._web_search("anything", limit=3)
    assert hits == [
        {"title": "Example", "url": "https://example.com/a", "snippet": "A description."}
    ]


def test_derive_search_queries_uses_goal_and_context():
    queries = kli._derive_search_queries("sell solar panels", context="in California")
    assert queries[0] == "sell solar panels"
    assert any("California" in q for q in queries)
    # Empty goal yields no queries.
    assert kli._derive_search_queries("") == []

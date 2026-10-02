"""Perplexity API request shaping (Agent API)."""

from unittest.mock import MagicMock, patch

from tradingagents.dataflows import perplexity_api as px
from tradingagents.dataflows.perplexity_api import (
    _append_primary_source_url,
    _extract_search_results_from_output,
    _format_search_after_date,
    _make_request,
    _resolve_preset,
    get_sec_filings_snapshot,
)


def test_resolve_preset_maps_legacy_sonar_tiers():
    assert _resolve_preset("sonar") == "fast"
    assert _resolve_preset("sonar-pro") == "low"
    assert _resolve_preset("unknown-model") == "low"


def test_extract_search_results_from_output_agent_trace():
    result = MagicMock()
    result.title = "SEC Filing"
    result.url = "https://sec.gov/x"
    result.date = "2026-01-01"
    item = MagicMock()
    item.type = "search_results"
    item.results = [result]
    response = MagicMock()
    response.output = [item]
    response.output_text = "body"
    sources = _extract_search_results_from_output(response)
    assert sources == [{"title": "SEC Filing", "url": "https://sec.gov/x", "date": "2026-01-01"}]


def test_sec_snapshot_uses_explicit_web_search_not_preset():
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.output_text = '{"filings": []}'
    mock_response.output = []
    mock_client.responses.create.return_value = mock_response

    with patch.object(px, "_api_mode", return_value="agent"), patch.object(
        px, "get_agent_client", return_value=mock_client
    ):
        get_sec_filings_snapshot("GOOG", after_date="2025-08-24")

    mock_client.responses.create.assert_called_once()
    kwargs = mock_client.responses.create.call_args.kwargs
    assert "preset" not in kwargs
    assert kwargs["model"] == "perplexity/sonar"
    assert kwargs["max_output_tokens"] == 4000
    tools = kwargs["tools"]
    assert tools[0]["type"] == "web_search"
    filters = tools[0]["filters"]
    assert "sec.gov" in filters["search_domain_filter"]
    assert filters["search_after_date_filter"] == "08/24/2025"
    assert _format_search_after_date("not-a-date") is None


def test_make_request_preset_for_unfiltered_news():
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.output_text = "ok"
    mock_response.output = []
    mock_client.responses.create.return_value = mock_response

    with patch.object(px, "_api_mode", return_value="agent"), patch.object(
        px, "get_agent_client", return_value=mock_client
    ):
        _make_request("News for TEST", model="sonar", max_tokens=1500)

    kwargs = mock_client.responses.create.call_args.kwargs
    assert kwargs["preset"] == "fast"
    assert "tools" not in kwargs
    assert "temperature" not in kwargs


def test_append_primary_source_url_for_agent_multimodal_gap():
    text = _append_primary_source_url("Summarize", "https://example.com/10k.pdf")
    assert "Primary source URL" in text
    assert "10k.pdf" in text

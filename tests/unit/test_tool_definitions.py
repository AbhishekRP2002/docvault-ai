"""Keep native schemas and the selector's advertised tool catalog in agreement."""

from docvault.tools import definitions


def test_catalog_updates_with_native_function_descriptions(monkeypatch):
    """A description change must reach both SDK-native tools and the generated prompt catalog."""
    description = "Discover filenames by topic in the workspace library without changing QA scope."
    monkeypatch.setitem(definitions.TOOL_DESCRIPTIONS, "search_documents", description)
    functions = {
        item["function"]["name"]: item["function"]
        for item in definitions.build_agent_tool_definitions()
    }
    assert functions["search_documents"]["description"] == description
    assert f"search_documents:\n{description}" in definitions.build_agent_tool_catalog_prompt()
    assert set(functions) == {
        "search_documents",
        "get_selected_document_overviews",
        "retrieve_relevant_chunks",
        "get_document_processing_metrics",
        "get_document_processing_diagnostics",
        "get_document_analysis_result",
    }


def test_discovery_and_passage_qa_expose_distinct_server_scoped_inputs():
    """Library search can paginate document cards; selected factual retrieval cannot change scope."""
    functions = {
        item["function"]["name"]: item["function"]
        for item in definitions.build_agent_tool_definitions()
    }
    assert set(functions["search_documents"]["parameters"]["required"]) == {"query", "cursor"}
    assert set(functions["retrieve_relevant_chunks"]["parameters"]["required"]) == {"query"}
    assert "workspace library" in functions["search_documents"]["description"]
    assert "selected documents" in functions["retrieve_relevant_chunks"]["description"]
    for function in functions.values():
        assert function["strict"] is True
        assert "version_ids" not in function["parameters"]["properties"]

"""Purpose-specific native tool descriptions and SDK-generated strict input schemas."""

from openai import pydantic_function_tool

from docvault.tools.models import TOOL_INPUT_MODELS

TOOL_DESCRIPTIONS = {
    "get_selected_document_overviews": """Read bounded previews of documents already selected for this chat.
Use for 'summarise this PDF', key takeaways, or as one fallback after empty factual retrieval.
Returns cited snippets and explicit partial coverage when insights are unavailable.
This does not discover library documents: use search_documents for names matching a topic.
Pagination advances selected documents, not raw sections. Do not repeat an unchanged read.""",
    "retrieve_relevant_chunks": """Find up to five original passages in selected documents for a specific factual question.
Use a standalone question with clear references resolved. This is passage QA, not library
file discovery: use search_documents for 'which documents cover topic X?'. Empty results
do not prove absence; read selected overviews once for possible supporting snippets before
abstaining. Do not repeat the same failed search. The server fixes selected scope.""",
    "search_documents": """Find which documents in the workspace library cover a topic and list their names.
Use first for 'Which files discuss reinforcement learning?', 'find renewal files',
or 'what documents do we have about X?', even when some/all files are selected for chat.
Searches indexed document summaries; returns ranked filename/title cards and availability.
Do not substitute passage retrieval or selected overviews. Results never expand QA scope.""",
    "get_document_processing_metrics": """Read fresh selected-document or workspace processing counts.
Returns timestamped queued, processing, ready, failed and insight states, counting latest
uploads once per document. Never infer live counts from metadata or prior conversation.""",
    "get_document_processing_diagnostics": """Inspect safe persisted diagnostics for a selected version.
Returns bounded stage, error and attempt information without replaying jobs or changing
state. Use processing metrics for workspace totals.""",
    "get_document_analysis_result": """Read saved analysis status and a bounded result preview for selected sources.
Use an artifact ID provided by the user or an earlier result. Returns explicit pending,
failed or ready state plus a few original supporting snippets.
Never starts analysis jobs or returns a complete raw document. Open the saved result for
full analysis; do not poll repeatedly in one turn when it remains pending.""",
}


def build_agent_tool_definitions() -> list[dict]:
    """Keep SDK model wrappers intact so native parse validates arguments as Pydantic models."""
    return [
        dict(pydantic_function_tool(model, name=name, description=TOOL_DESCRIPTIONS[name]))
        for name, model in TOOL_INPUT_MODELS.items()
    ]


def build_agent_tool_catalog_prompt() -> str:
    """Generate the prompt catalog from the same descriptions used for native tool schemas."""
    return "\n\n".join(f"{name}:\n{description}" for name, description in TOOL_DESCRIPTIONS.items())

# Docling, Chonkie, and LlamaIndex chunking comparison

Reviewed 5 October 2026. Decision: retain Docling's native splitter. Chonkie and LlamaIndex are not installed, benchmarked, or added to the project. Their behavior below is based on official documentation and source inspection, not an application integration test.

## Compare the right layers

Our pipeline first converts PDF/DOCX into a structured `DoclingDocument`, then chunks it. Replacing a prose splitter does not replace PDF layout analysis, OCR, or table recognition. Conversion and chunking must be timed separately before deciding where to optimize.

Installed baseline: Docling 2.132.0, docling-core 2.99.0, semchunk 3.2.5. Our contextualized embedding-input limit is **750 tokens**, including filename and headings; there is no whole-file extracted-token ceiling. Existing ready versions retain their stored chunks.

The alternatives reviewed are **Chonkie SentenceChunker** (upstream main identifies itself as 1.7.0; not a verified installed/released version) and **LlamaIndex SentenceSplitter**. This does not assess every other chunker those projects provide.

## Algorithms and integration

| Dimension | Current Docling HybridChunker | Chonkie SentenceChunker | LlamaIndex SentenceSplitter |
|---|---|---|---|
| Input | Structured document with typed source items | Text | Text or LlamaIndex documents/nodes |
| Boundary strategy | Hierarchical elements, token-aware splitting, compatible peer merging | Sentence delimiters, then token-budget grouping | Paragraph boundaries, sentence tokenizer, recursive fallback |
| Sentence handling | Installed semchunk favors newlines/tabs/whitespace before punctuation; sentence preference is heuristic | Defaults include `. `, `! `, `? `, and newline; configurable delimiters/minimum sentence length | Default sentence tokenizer uses NLTK Punkt; falls back to smaller units when needed |
| Wrapped lines | May cut at a newline inside a sentence | Default newline delimiter can produce the same problem | Sentence tokenizer is a more promising candidate for wrapped prose; needs evaluation |
| Abbreviations and decimals | Punctuation heuristics can misidentify boundaries | Default delimiters are not a linguistic abbreviation model | Punkt can provide more linguistic segmentation; domain-specific accuracy remains unverified |
| Oversized sentence | Native fallback splits it to respect capacity | Reviewed implementation can emit an oversized sentence; would need a fallback | Recursive fallback to sub-sentence, word, and character splits |
| Table structure | Native table-aware segmentation; our adapter repeats headers and reserves context capacity | SentenceChunker has no table-aware contract; retain Docling's table path | SentenceSplitter has no table-aware contract; retain Docling's table path |
| Provenance | Docling source-item references; our PDF/DOCX locators are item-scoped | Text offsets exposed; mapping to Docling items/pages would be application work | Node relationships/metadata exposed; mapping to Docling items/pages would be application work |
| Context budget | Filename/headings included in actual token counting | Must reserve DocVault context separately and validate final combined input | Metadata-aware splitting exists; DocVault filename/headings must be represented consistently |
| Dependencies | Already installed | Adds Chonkie/core/tokenizer dependencies; manifest review required | Adds framework dependencies and sentence-tokenizer resources |
| Conversion speed | Owns our conversion pipeline | Changing SentenceChunker does not accelerate Docling conversion | Changing SentenceSplitter does not accelerate Docling conversion |

Docling's hierarchy/split/merge behavior is described in its [chunking documentation](https://docling-project.github.io/docling/concepts/chunking/). The installed semchunk source defines the newline/whitespace/punctuation priority; its [official repository](https://github.com/isaacus-dev/semchunk) documents the library, but upstream behavior can differ from installed 3.2.5.

Chonkie's reviewed [SentenceChunker implementation](https://github.com/feyninc/chonkie/blob/main/src/chonkie/chunker/sentence.py) uses delimiter splitting and groups sentence token counts. It computes actual chunk tokens after grouping, so the final combined input needs independent validation. Its [manifest](https://github.com/feyninc/chonkie/blob/main/pyproject.toml) lists chonkie-core and tokie among base dependencies. A Rust-backed grouping operation alone does not establish an end-to-end speed improvement.

LlamaIndex documents paragraph/sentence preference and recursive fallback in [SentenceSplitter](https://developers.llamaindex.ai/python/framework-api-reference/node_parsers/sentence_splitter/). Its [sentence-tokenization utility](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/node_parser/text/utils.py) uses Punkt sentence spans. A better boundary model is a hypothesis to test, not measured retrieval improvement.

## What we actually reproduced

Using our current TXT adapter and native splitter at 750 tokens:

| Repeated fixture | Observed result |
|---|---|
| `This policy applies! Do these terms change? Payment is due annually. ` | Three chunks; observed non-final chunks ended after complete sentences. |
| `Customers must provide notice\nbefore the renewal date. Refunds are unavailable\nafter renewal. ` | Five chunks; some boundaries occurred after `notice` or `unavailable`, inside a sentence. |
| `The annual plan costs 1200 dollars.  Customers must provide thirty days notice.  ` | Six chunks; some boundaries occurred after `costs`, inside a sentence. |

These examples preserve all source characters and remain within the token budget. They demonstrate that native splitting is **sentence aware, but not sentence preserving in every formatting case**. Short-sentence tests cover `.`, `?`, `!`, and CRLF boundaries; oversized-sentence tests cover lossless fallback. We have not run these fixtures through Chonkie or LlamaIndex, so candidate results remain unknown.

## Decision and evaluation before any replacement

Retain Docling for now. The measured conversion bottleneck and required table/provenance behavior make replacing the whole chunker unjustified. If sentence-boundary quality becomes the priority, evaluate a prose-only splitter through Docling's `segment()` extension point; keep hierarchy, peer merging, tables, context counting, and source references intact.

Use the same parsed document, tokenizer, 750-token budget, context prefixes, and zero overlap for all candidates. Include normal prose, wrapped lines, double spaces, quotations, abbreviations, decimals, Unicode, long sentences, oversized whitespace, headings, tables, and picture OCR text. Pin any experimental versions in an isolated environment before testing.

Measure source coverage, final embedding-input size, sentence-boundary errors, table/header retention, citation mapping, chunk count, chunking time, and peak memory. Then compare retrieval and cited answers on the same labeled questions. No content loss, oversized input, lost table context, or invented precise PDF offsets is acceptable. Select a replacement only after reviewing those results; do not infer quality from a package name or vendor speed claim.

PDF tuning and conversion measurements are recorded in [progress.md](progress.md). `scripts/benchmark_parser.py` measures initialization, conversion, and chunking separately without indexing, changing stored documents, or calling an LLM.

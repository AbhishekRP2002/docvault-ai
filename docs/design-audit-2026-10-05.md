# DocVault interface audit — 5 October 2026

## Outcome

Files is now a compact document workspace. Usage shows recorded activity over time. The shared shell, controls, statuses, loading states, and document detail selectors use the same neutral visual language. No dependencies were added and no existing API contract was removed.

## Findings and changes

| Finding | Change |
| --- | --- |
| Three summary cards and a separate upload receipt pushed the file table below the primary working area. | Removed both. File count and active processing are inline; uploads enter the table immediately. |
| Large icons, generous row padding, rounded containers, and repeated headings made routine file management feel like a landing page. | White working canvas, quieter gray navigation, compact toolbar, small file icons, tighter rows, and restrained status colors. |
| Uploads waited for a subsequent list fetch to become visible. Leaving Files lost the local upload presentation. | App-owned upload state, temporary transfer rows, immediate merge of server-confirmed documents, in-row errors/retry, and cancellation of older list requests before updating the cache. |
| Dropdowns used the browser's native list styling. | Locally owned shadcn-style Radix Select controls for status, sorting, version, summary length, and tone. Portaled menus retain keyboard navigation, focus management, and disabled states. |
| Usage consisted of lifetime totals and prose. | Interactive daily requests/tokens/reported-cost chart, 7/30/90-day ranges, keyboard/pointer/touch inspection, model breakdown, and a separate all-time processing distribution. |
| The API did not expose time-series usage. | Added read-only `/v1/metrics/usage/history?days=30`, bounded to 1–90 days, with SQL aggregates of stored `LLMCall` records and UTC day boundaries. |
| Missing costs risk being interpreted as zero. | Unknown-only cost buckets remain null and create chart gaps; partial totals and unknown call counts are explicit. Empty dates have zero activity. |
| Loading feedback did not consistently resemble the destination. | Route-specific skeletons, table rows while loading, lazy document-detail/comparison modules, detail skeletons, short transitions, and a global reduced-motion fallback. |
| Narrow layouts compressed filenames and chart labels. | The file table scrolls inside its own container below 560px; chart labels scale for mobile; navigation becomes a drawer. |

The existing grid view, document selection, comparisons, chat entry points, versions, downloads, retry processing, and deletion confirmation remain available. Upload percentages describe batch transfer; processing status comes from the server and is never fabricated from transfer progress.

## Reference principles

- **Extend and Reducto:** the user-provided screenshots supply the visual reference: quiet navigation, compact controls, a dominant table, meaningful status labels, and minimal decorative containers. This is an adaptation of that hierarchy to DocVault's actual features, not a copy of their navigation or branding.
- **Linear:** [A calmer interface for a product in motion](https://linear.app/now/behind-the-latest-design-refresh) prioritizes the user's working area, reduces competing navigation/icon treatments, and uses separators sparingly. [Its earlier redesign account](https://linear.app/now/how-we-redesigned-the-linear-ui) also emphasizes alignment, density, and testing hierarchy across application states. Applied here to the shell, table, and chart controls.
- **Y Combinator:** [Practical Design: User Observation](https://www.ycombinator.com/blog/practical-design-user-observation) frames design around understanding a specific user's need; [Practical Design: Design Brief](https://www.ycombinator.com/blog/practical-design-design-brief) calls for concrete product context. The acceptance scenarios here are locating a file, observing its upload/processing state, recovering a failed upload, and inspecting actual usage. These articles are process guidance, not a prescribed YC dashboard aesthetic.
- **Component API:** [shadcn Select](https://ui.shadcn.com/docs/components/radix/select) documents the Root/Trigger/Value/Content/Item composition and popper placement. This implementation uses the already installed `radix-ui` 1.6.7 (`@radix-ui/react-select` 2.3.7), React 19, and Tailwind 4.

## Verification

Baseline: `cd web && bun run typecheck && bun run test` exited 0: 32 tests passed.

Final checks:

- `cd web && bun run typecheck && bun run test && bun run build` — exit 0; **43 tests passed, 111 assertions**; Vite built 2,648 modules, including separate Files, Usage, document detail, and comparison chunks.
- `pytest tests/integration/test_usage_metrics.py tests/integration/test_api.py -k 'usage or history' -q --tb=short` with `TEST_DATABASE_URL` pointing to the dedicated local `docvault_test` database — exit 0; **10 passed, 21 deselected**. Fixtures use temporary isolated schemas. Covers stored totals, UTC boundaries, zero-filled dates, null/partial cost semantics, model grouping, and invalid ranges.
- Ruff check/format-check for the changed backend files and `git diff --check` — exit 0.
- Desktop browser: reviewed the actual local Files layout; verified custom status dropdown and filtering/empty state in an isolated fixture preview.
- Upload browser harness: exercised the actual drop handler with generated non-sensitive files against a temporary fixture API. Confirmed immediate Saving rows, navigation while uploading, successful row reconciliation, advancing processing status, and partial failure with an inline retry action. Existing workspace documents were not modified.
- Usage browser harness: verified date-range and metric changes, keyboard day inspection, unknown-cost display and chart gaps. Checked Files, Usage, and navigation at a 390px viewport; restored the viewport afterward.

## Remaining boundaries

- The user will restart their existing API process on port 8000 to load the new history route. Until that restart, the running instance returns 404 for the new endpoint. The endpoint passed real PostgreSQL integration tests; dashboard browser checks used fixture data.
- Chrome's extension lacked local-file URL access, so automated native file selection could not complete. The upload lifecycle was tested through the app's real drop handler instead. The real provider processing pipeline was not rerun as part of this visual redesign.
- The API's existing all-time document metrics count version statuses; the dashboard labels these as ready versions. No invented time-series values or estimated cost totals are included.
- Temporary preview processes and the in-repository test harness are removed after verification. The user's original API and Vite processes remain untouched.

## Files processing columns follow-up

The table now shows Started at, Processing time, Run ID, Pages, Chunks, and Size beside Name and Status. A keyboard-accessible Columns menu exposes Added at, Finished at, Attempts, Content tokens, Parser, and Embedding model, remembers visibility in browser storage, and can reset to defaults. Run IDs are searchable and copyable. The filename stays visible while scrolling horizontally on desktop; mobile scrolls the table without widening the page.

Values come from the latest document version and its latest ingestion job/attempt. Processing time excludes upload, queue wait, retry backoff, and insight generation. Running attempts use server-observed elapsed time; completed attempts retain their recorded duration. Automatic retry backoff retains the last attempt's duration; a manual retry displays unknown timing until its new attempt starts. Run IDs persist across retries, while the attempt count resets for a manual retry. Unknown output counts and timestamps display a dash. Content tokens describe parsed chunks, not billed model usage.

Document responses now expose additive processing metadata, parser, embedding model, and content token count. The list loads versions, jobs, and attempts in batches: four read queries for a nonempty library page with jobs, and five for a stored upload batch, independent of row count. No migration or dependency was added.

Follow-up verification:

- `cd web && bun run typecheck && bun run test && bun run build` — exit 0; **47 tests passed, 133 assertions**; Vite built 2,650 modules.
- `pytest tests/integration/test_document_processing_metadata.py tests/integration/test_document_validation.py -q --tb=short` against isolated schemas in `docvault_test` — exit 0; **27 passed**. Covers persisted run IDs on upload, running/completed/failed timing, missing metadata, version selection, automatic/manual retries, and bounded list/batch query counts.
- Ruff lint and format checks for the three changed backend files — exit 0. Final diff reviewed; unrelated configuration work preserved.
- Desktop fixture preview: default and optional columns, persistence after reload, reset, run ID filtering, full-ID clipboard round trip, and keyboard horizontal scrolling with a sticky filename all passed. At 390px, page and main scroll widths both matched the viewport; restored the viewport afterward. [Desktop evidence](/private/tmp/docvault-design-review/files-processing-columns.jpg) uses synthetic fixture data.
- Temporary fixture API and Vite preview were stopped. Existing API/Vite processes on ports 8000/5173 were left running. The user still needs to restart the API to serve the new document fields; the real provider pipeline was not rerun for this follow-up.

## Commit preparation checks

Fresh automated checks on the combined changes: whole-project Pyright **zero diagnostics** after correcting nine nullable argument annotations in the usage helpers; backend Ruff and changed-file formatting pass. The first backend run could not access local service sockets from the sandbox. The service-enabled rerun passed **232 tests, one deselected** in isolated test schemas; the unchanged 12,000-vector HNSW benchmark was excluded. Frontend typecheck, **47 tests / 133 assertions**, and the **2,650-module** production build pass. No new browser or live-provider validation was performed during commit preparation; the earlier fixture-browser evidence above is separate. No development processes were started or stopped.

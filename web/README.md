# DocVault web

React + TypeScript + Vite dashboard with shadcn-style, locally owned Radix primitives and Tailwind CSS. The Python API owns document processing, conversations, citations, and structured answers; the frontend renders persisted API state.

```sh
bun install --frozen-lockfile
bun run dev
```

Open http://localhost:5173. The development server proxies `/v1` (including the update WebSocket) and `/health` to http://127.0.0.1:8000. Override the target with `API_PROXY_TARGET`. For a separately hosted API, set `VITE_API_URL` at build time and configure its CORS origins.

```sh
bun run typecheck
bun test src/lib
bun run build
```

Files: batch upload with transfer progress, processing status, search, list/grid view, document insights with citations, versions, customized summaries, and multi-document comparisons. Select ready documents to create a scoped conversation.

Agent: New Run and collapsible, searchable Past Runs in the main sidebar. One main canvas holds persisted sessions, searchable source selection, streaming responses, up to three suggestions, citations, stop, and regeneration of the latest answer. A stopped or disconnected browser does not imply that server processing stopped; Stop calls the cancellation endpoint explicitly.

Usage: persisted document, processing, token, and cost metrics. Missing provider cost is shown explicitly.

The source selector loads every page of the document library; it has no hidden document-count cap.

`Dockerfile` produces a static nginx deployment, with same-origin API, WebSocket, and SSE proxying to `api:8000`. Use the repository Compose stack so that hostname resolves.

The application polls during processing and reconnects the update WebSocket after disconnection. Markdown is rendered without raw HTML, and source identifiers become numbered citation buttons. The frontend has no provider credentials or client-side AI calls.

The shadcn primitives follow [the official Vite installation](https://ui.shadcn.com/docs/installation/vite), [Radix sheet implementation](https://github.com/shadcn-ui/ui/blob/main/apps/v4/registry/new-york-v4/ui/sheet.tsx), and [Radix AlertDialog](https://ui.shadcn.com/docs/components/alert-dialog). Keeping the primitives local avoids introducing a second chat runtime alongside the Python streaming API.

The user selected manual browser validation on 2 October 2026. Build and transport tests passed; desktop/mobile appearance and interactive browser flows remain unverified. See [the progress ledger](../docs/progress.md) for actual evidence and outstanding gates.

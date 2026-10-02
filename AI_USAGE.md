# AI assistance record

Updated 2 October 2026. Codex assisted with the specification, implementation plan, research against official documentation, Python backend, LangGraph/provider integration, queue processing, React UI, tests, and documentation. The user supplied the assignment, provider preference/key locally, removed limits, deferred IAM, and corrected the initial UI direction.

Concrete corrections and checks:

- Selected RQ for the Python worker setup after reviewing queue options; BullMQ also has Python bindings and was not described as Node-only.
- Kept SQL as the persistence authority for jobs/history; transient LangGraph invocations do not imply durable graph resume.
- Packed adjacent parser blocks with compatible page/heading provenance. The supplied four-page PDF produced 26 chunks after initially producing 116; image-only PDF OCR recovered expected fixture text.
- Enforced one active generation per session while retaining cancellation and explicit latest-turn regeneration. Deterministic API tests exercise admission, source scope, idempotency, and cancellation races.
- Replaced the initial three-column conversation layout after the user's review with a single sidebar, Agent/New Run/Past Runs, and a focused main canvas. Existing shadcn/Radix primitives were reused. 21st catalog retrieval returned HTTP 401; public sidebar guidance and local review were used instead.
- Integrated assistant-ui 0.15.23 at the user's request after reading official components/runtime docs through the web and Context7 and checking installed source/types. External Store Runtime adapts Python history, SSE, cancellation and latest-turn regeneration. Added published-runtime tests without a browser or new test dependency; fixed the textarea focus indicator found by 21st review.
- Corrected a verification script to decode SSE event names separately from payloads. The live synthetic policy answer produced 39 deltas, two citations and three suggestions; persisted history matched the final response.

Executed checks and remaining gates are in [docs/progress.md](docs/progress.md). The full backend suite passed 47 tests with dedicated PostgreSQL integration enabled. Frontend transport tests, typecheck and production build passed. These checks do not establish held-out RAG quality or production readiness.

The user chose manual browser validation; no agent-driven browser test is claimed. Frozen evaluation, complete failure/load testing, quota enforcement, clean Compose reproduction, and the required demo remain tracked as incomplete. No provider key, private source PDF, fabricated metrics, time-saving claim, or unverified deployment is included in this repository.

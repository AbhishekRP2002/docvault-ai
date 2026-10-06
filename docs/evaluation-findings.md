# Live pilot findings, 5 October 2026

The operator approved sending the assignment PDF and two repository fixtures to OpenRouter. Tests used a disposable Docker project with three native workers, PostgreSQL/pgvector, Redis, the current application source and migrations, real Docling 2.132.0 parsing/OCR and the configured OpenRouter models. The original application data and services were untouched. The generated JSON reports were removed during repository cleanup; this document retains the historical findings. The optional evaluation runners and their dedicated fixtures have also been removed; these findings describe the earlier pilot.

## Processing and recovery

The initial assignment ingest exhausted three leases while the laptop was in clamshell sleep. `pmset -g log` independently shows sleep at 16:45:13 IST and wake at 17:10:11 IST, overlapping the failed attempts. Those stage durations are environmental interruption evidence, not awake parser benchmarks. The same persisted job was manually replayed while awake, preserving old attempts. It completed without changing the 120-second lease: four pages, 25 chunks, conversion 27.961 seconds, chunking 0.159 seconds, embeddings 2.856 seconds, and automatic insights 8.674 seconds.

The one-page OCR fixture contains zero PDF text-layer characters. Docker parsed it through Docling, embedded/indexed it, generated insights, and answered its USD1200/30-day facts with an original page-1 citation. Host Docling additionally preserved its negative refund rule and original-page provenance. The conflicting TXT draft also processed successfully. All three sources were ready with completed insights after recovery.

## Quality gate: failing

Only **three of eight reviewed QA cases meet the frozen answerability expectations**. The pilot is intentionally retained as failing; this is not a factual-accuracy estimate for production.

| Case | Observed result | Source review |
| --- | --- | --- |
| Required database/queue stack | No retrieved evidence | Known facts on assignment page 2 were parsed, but omitted by retrieval. |
| Optional frontend paraphrase | No retrieved evidence | Known optional-frontend facts on page 3 were parsed, but omitted by retrieval. |
| Mandatory demo follow-up | Clarification requested | No fabricated facts, but the required demo remains unanswered after the previous retrieval gap. |
| Advanced bonus features | No retrieved evidence | Known bonus facts on page 4 were parsed, but omitted by retrieval. |
| OCR subscription price/notice | USD1200 / 30 days, page-1 citation | Complete, source-supported answer. |
| Policy versus unapproved draft | Draft only, partial response | USD900 / 14 days and non-override are supported; policy terms were omitted, so comparison fails completeness. The wording that policy terms “are not provided” also risks implying absence from the whole document rather than the retrieved evidence. |
| Absent CFO email | Scoped no-evidence abstention | No invented email; appropriate abstention. |
| False refund premise | Correctly rejects refunds after renewal | Complete, source-supported negative rule and original page-1 citation. |

Read-only SQL measurements using the already cached query embeddings explain the recall gap. Maximum cosine similarity was 0.426664 for the stack question, 0.475298 for the frontend paraphrase and 0.579707 for bonuses. In the comparison, the policy scored 0.667992 while the draft scored 0.781157. The strict similarity threshold **greater than 0.7**, applied to both semantic and lexical candidates, excluded valid policy/assignment evidence. The user's threshold was preserved. Changing it or exempting lexical matches requires an explicit retrieval-design decision and another reviewed pilot.

The follow-up's diagnostic query score in the recovery report is marked diagnostic-only: the original workflow requested clarification and skipped retrieval. The runner originally repeated this query once for measurement, causing one extra embedding request; it now skips that diagnostic for clarification outcomes. It is not counted as actual workflow evidence recall.

## Concurrent chat and usage

Five distinct chat sessions started together and all completed in **2.578 seconds** (1.939 completed requests/second for this tiny batch). Three produced real streamed, source-supported answers; two abstained even though the source contained the answer. Transport completion is 5/5; grounded answer completion is 3/5. This does not establish sustained throughput or a production SLA.

The disposable workspace recorded **26 provider requests**, 36,118 input tokens, 2,701 output tokens and **USD0.015318555** reported cost, with zero unknown-cost calls: 16 embeddings, six chat generations, three insights calls and one rewrite. The assignment's single insights call consumed 25,468 input tokens for 1,357 extracted source tokens; citation/provenance metadata is a separate prompt-size optimization opportunity.

The coding assistant reviewed returned claims against the original PDF pages and frozen fixture facts. Independent human acceptance is unrecorded. No model judge was used, and valid citation IDs/expected-word matches alone are not treated as proof of factual support.

After final readiness and usage readback, the owned live-test containers, network and fresh volumes were removed. Its private environment directory and temporary awake assertion were also removed. The original application PostgreSQL/Redis remain healthy; no application migration or browser validation was performed.

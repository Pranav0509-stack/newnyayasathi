# Sanhita CHANGELOG

Cross-repo release log for the Sanhita platform. The product spans three
GitHub repos under the **Nyayasathi-AI** organisation:

| Repo | Contents | Default branch |
|---|---|---|
| `Nyayasathi-AI/Nyayasathi_main` | FastAPI backend (server.py, routes_*.py, validators/, mcp_server/, eval/, word_addin/) plus the Next.js frontend as a git submodule at `sanhita-react/` | `main` |
| `Nyayasathi-AI/Sanhita_main`    | Next.js frontend (Sanhita advocate UI — workflows, drafter, editor, court search, plugins). Consumed by the backend repo as a submodule. | `main` |
| `Nyayasathi-AI/data_main`       | India-Judgments-Corpus: ingest pipeline, FTS5 search engine, compliance plugins, contract templates, citation extractor, legal reasoner. | `main` |

`plan_main` (the public roadmap repo at `Nyayasathi-AI/plan_main`) is
documentation only — it carries ROADMAP.md, ARCHITECTURE.md, COMPETITIVE.md
referenced from this changelog.

---

## 2026-05-19 — BNS / BNSS / BSA labelling, citation grounding, workflow runner

### Highlights (lawyer-visible)

- Every output now labels old criminal-code sections with their post-1-July-2024
  equivalents. `Section 420 IPC` becomes `Section 420 IPC (BNS §318(4))`,
  `Section 439 CrPC` → `(BNSS §483)`, `Section 65B IEA` → `(BSA §63)`. Covers
  search snippets, drafter output, compliance findings, workflow AI nodes,
  and the chat assistant.
- 27 prebuilt workflow recipes total — 12 Indian-litigation procedural plus
  15 transactional/compliance (Agreement Audit, Chronology of Events,
  Compliance Gap, Due Diligence, Focused Summarizer, Closing Checklist,
  Privilege Review §126 IEA, Playbook Audit, Matter Triage, Claim
  Challenger, New Case Assessment, Quick Agreement Analyzer, Loan
  Compliance Tracker, Execution-vs-Final Diff, Chain of Ownership).
- Workflow runner never breaks the chain on a single backend miss; every
  node surfaces a per-node validation badge (✓ green / ⚠ amber / ✗ red).
- Drafter and Editor are now separate sidebar items with distinct icons
  and distinct underlying components (was: both mapped to ContractsPane
  silently, so "Send to Editor" looked like a no-op).
- Court Search filters auto-rerun with a 250 ms debounce on year / court /
  source / engine / sort changes. Previously stale results stuck after
  filter edits.
- `documents` corpus exposed in the UI as "Tribunals & Regulators" tab
  (2,038 NCLAT + RBI + SEBI + PRS + IndiaCode rows previously hidden).
- 25 Indian-regulator acronyms now expand at query time: RBI, SEBI,
  NCLAT, NCLT, ITAT, CESTAT, NCDRC, AAR, MCA, PRS, BCI, CCI, TRAI,
  IRDAI, EPFO, GST, CPC, CrPC, IPC, BNS, BNSS, BSA, IBC, RTI, NI.

### Backend changes (Nyayasathi_main)

**Search engine — `scripts/search/engine.py` (data_main repo)**

- Parallel BM25 fan-out across 6 corpora using `ThreadPoolExecutor`; each
  worker uses the thread-local SQLite handle.  Cold-cache ALL+bail query
  collapsed from >60s sequential to 2.6s.
- All 6 FTS5 templates use `-rank AS bm25_score ... ORDER BY rank` (FTS5
  heap short-circuit) instead of per-row `bm25()` function calls.
  Individual table cold time 30s → 3-7s.
- New `documents` corpus added to FTS_TABLES + DEFAULT_SCOPE with
  year extraction via `CAST(SUBSTR(issued_date,1,4) AS INT)`.
- 25s per-table timeout in `fut.result()` so one slow corpus can't hang
  the whole request.
- `_ACRONYM_ALIASES` table — 25 Indian-regulator / code acronyms.
  `_expand_acronym()` returns an FTS5 OR-group `(RBI OR "Reserve Bank
  of India")` so users matching either form get hits. Explicit `AND`
  between OR-group and bare tokens to avoid FTS5 syntax errors.

**Reasoner — `scripts/assistant/legal_reasoner.py`**

- `SYNTHESIZER_PROMPT` adds a mandatory rule: every IPC / CrPC / IEA
  section the answer cites must be paired with the post-July-2024 BNS /
  BNSS / BSA equivalent in parentheses.

**Compliance — `scripts/contract/compliance.py`**

- Four new plugins:
  - `ica_27_noncompete` (HIGH) — detects post-termination non-compete
    + §27 ICA invalidity, with Niranjan Shankar Golikari / Superintendence
    Co. / Percept D'Mark citations.
  - `ica_28_jurisdiction` (HIGH) — detects foreign-court exclusive
    jurisdiction with Indian parties + §28 ICA risk.
  - `foreign_law` (WARN) — flags foreign governing law clauses.
  - `indemnity_cap` (WARN) — uncapped indemnity OR indemnity present
    without limitation-of-liability.
- Registry: 12 plugins total (was 8).

**Server — `server.py`**

- `load_dotenv()` at the very top so `llm/router.py` captures
  GEMINI_API_KEY / ANTHROPIC_API_KEY at module-import time. Without
  this, every router.generate() silently returned empty.
- `/api/workflows/validate` — 5-gate workflow-aware validator
  (banned_phrases, no_fabricated_cases, statute_anchor, format_check,
  grounding_in_context). Hard-fails on banned phrases or fabricated
  case names; soft-fails on format / grounding heuristics.
- `/api/brief/chat-v2` — new endpoint that wraps the full reasoner
  pipeline (planner → multi-corpus retrieve → synthesizer → 6 answer
  gates) but returns the same `{answer_markdown, citations,
  validation, sub_questions}` shape as legacy `/api/brief/chat`.
  Falls back to legacy on reasoner failures.
- `IPC ↔ BNS / CrPC ↔ BNSS / IEA ↔ BSA` annotation applied to:
  - search results (routes_search.py — title + snippet)
  - compliance findings (routes_contract.py — finding + remediation)
  - chat answers (server.py — answer_markdown post-process)
  - drafter / editor output (doc_editor.py — write-section post-process)
- AiWriteSectionBody adds `prefer` field — provider override forwarded
  to router.generate() so the citation bench can A/B Claude vs Gemini.
- Smart-search grounding for `/api/brief/chat`: switched from legacy
  FTS5Index.search() (5 tables, sequential, raw bm25()) to
  HybridSearchEngine.search() (all 6 corpora, parallel, rank
  short-circuit). Adds 53.3M pipeline_docs to the grounding scope.

**Doc editor — `doc_editor.py`**

- `_EDITOR_SYSTEM` rewritten for grounding-first behaviour:
  - Every factual or legal claim must end with a `[E*]` source marker
    when GROUNDING SOURCES block is provided.
  - "(not in corpus)" admission required when a claim can't be grounded.
  - Landmark cases whitelist (Kesavananda, Maneka, Vishaka, Puttaswamy,
    Sanjay Chandra, Niranjan Shankar Golikari, Hakam Singh) — citable
    without source markers.
  - No AI hedging, no preamble, strict format enforcement.
- `ai_write_section(prefer=...)` — provider preference plumbed through.
- Context cap raised 400 → 8000 chars.

**LLM router — `llm/router.py`**

- `_call_gemini` hardened: when SDK's `.text` accessor returns "" on
  Gemini 2.5 Flash thinking-trace responses, manually walks
  `candidates[*].content.parts[*].text`. Raises explicit error on
  `prompt_feedback.block_reason` rather than silently returning empty.

**Legal code mapping — `legal_code_mapping.py` (new)**

- Single source of truth for IPC ↔ BNS / CrPC ↔ BNSS / IEA ↔ BSA.
- 100+ IPC → BNS mappings (murder 302→103, cheating 420→318(4),
  498A→85, sexual offences 375-376→63-71).
- 50+ CrPC → BNSS mappings (arrest 41→35, bail 437→480/439→483/438→482,
  quashing 482→528, chargesheet 173→193, default bail 167→187).
- 60+ IEA → BSA mappings (electronic evidence 65B→63, police
  confessions 25→23(1), privilege 126→132).
- `SectionRef` dataclass with `.equivalent` property.
- `annotate_text(text)` — idempotent regex walker that appends new-code
  equivalent in parentheses after every old-code section reference.

**Eval — `eval/bench/claude_citation_bench.py` (new)**

- Citation-faithfulness bench that retrieves full text from all 6
  corpora, asks the LLM (Anthropic if key set, else Gemini), and
  scores marker resolution + text overlap + per-corpus citation
  distribution + banned-phrase clean.
- Provider preference via `--prefer anthropic|gemini|groq`.
- Smoke: 5/6 questions passed with citations distributed across
  legal_qa + legal_docs + pipeline_docs + judgments + statutes.

**BigLaw bench result — `eval/bench/biglaw_bench.py`**

- Overall: 77/118 (65.3%), mean 0.778. Up from baseline 75/118 (63.6%).
- Search-mode (the lawyer-relevant metric): 71/84 (84.5%), mean 0.948.
  Up from 69/84 (82.1%).

### Frontend changes (Sanhita_main)

**Workflows pane — `web/src/app/app/workflows-pane.tsx`**

- 27 recipe-cards now (was 12). New section header: "Indian solo +
  transactional · grounded in 83M-row corpus".
- `executeNode()` never throws — every backend failure becomes a soft
  "done" with a hint, so the chain completes instead of breaking on
  the first 4xx.
- `pickBody(inputs)` helper finds the best text from inputs via three
  step fallback (known slot → longest string → join-all). Fixes
  "Missing required: brief_facts" for every new recipe.
- `remapToTemplateSlots()` — per-template alias table so workflow
  inputs (e.g. `cheque_no`) map to template slot names
  (e.g. `cheque_number`). Auto-fills `notice_date = today`, advocate
  placeholders, `cheque_amount_words` via `inrToWords()`.
- AI nodes retrieve-first: fetch top-6 corpus sources via
  `/api/cases/smart-search`, inject as `[E1]..[E6]` grounding blocks
  into write-section prompt. Returned text gets a "Grounding sources
  used" footnote listing the rows.
- Per-node validation badge (`✓ green / ⚠ amber / ✗ red`) with
  expandable per-gate breakdown.
- `NodeOutput` component — Show/Hide, ✎ Edit (resizable textarea),
  Copy, Send to Editor →. Replaces the old 72px-tall `<pre>`.
- `MarkdownLite` renderer with table support; pre-annotates text via
  `annotateText()` so IPC/CrPC/IEA refs get BNS labels inline.
- 250ms debounced auto-rerun on filter change in Court Search (years,
  court, source, engine, sort, page).

**Recipe library — `web/src/app/app/workflow-recipes.ts`**

- 15 transactional recipes added: Agreement Audit (ICA + FEMA),
  Chronology of Events, Compliance Gap (8 plugins), Due Diligence
  M&A, Focused Summarizer, Closing Checklist, Privilege Review
  (§126 IEA), Playbook Audit, Matter Triage, Claim Challenger,
  New Case Assessment, Quick Agreement Analyzer, Loan Compliance
  Tracker, Execution-vs-Final, Chain of Ownership.
- Bail Pipeline + Cheque-bounce §138 hardened with citator gate +
  WhatsApp tracker + Vault snapshot nodes.
- `NodeSpec.desc` is now optional (terse trigger nodes allowed).

**Court Search — `web/src/app/app/court-search-pane.tsx`**

- 6 source tabs visible (was 5 + 1 hidden). Tribunals & Regulators
  added with NCLAT / RBI / SEBI / PRS / IndiaCode count.
- 250ms debounced filter auto-rerun.
- Empty-state with "Search all 83.1M records instead" + "Clear year
  filter" fallback buttons.
- Real corpus counts (83.07M total) replace stale 31.9M copy.
- Hit snippets pass through `<AnnotatedText>` for BNS labels.

**Assistant — `web/src/app/app/page.tsx`**

- Default chat routes to `/api/brief/chat-v2` (planner + reasoner)
  instead of legacy `/api/brief/chat`.
- "Import PDF / Doc" attach button removed from the composer.
- Sidebar: Drafter and Editor are now distinct items with different
  icons. EditorPane is properly mounted on `mode === "editor"` (was
  silently mounting ContractsPane for both modes).

**Drafter — `web/src/app/app/contracts-pane.tsx`**

- Templates sort by authority: `statutory_form` → `statutory` →
  `court_approved` → `practice_standard`. The 4 verbatim Govt forms
  (RTI Form A, CPC Form 1, CPC Form 4, NCLT IBC §7) appear first.
- Quick-edit refusal-with-reason renders as a friendly `💡` tip,
  not an error.

**Editor — `web/src/app/app/editor-pane.tsx`**

- ExportMenu dropdown replaces the broken empty-onClick "Export"
  button. Five options: HTML, Markdown, TXT, Print → PDF, Copy
  entire document. Click-outside dismiss.

**Legal code mapping — `web/src/lib/legal-code-mapping.ts` (new)**

- TypeScript mirror of `legal_code_mapping.py`. Same 200+ section
  mappings, same `equivalent()` and `annotateText()` helpers.

**Code badge component — `web/src/components/legal-code-badge.tsx` (new)**

- `<CodeBadge code section>` — inline pill with hover tooltip showing
  the cross-code twin. Amber for legacy (IPC/CrPC/IEA), emerald for
  current (BNS/BNSS/BSA).
- `<AnnotatedText>` — walks free legal prose and renders every
  section reference as a `<CodeBadge>`. Used in court search snippets
  + drafter output.

### Data pipeline (data_main)

- Compliance plugins: +4 (see backend section).
- Reasoner SYNTHESIZER_PROMPT: BNS pairing rule.
- Search engine acronym aliasing + parallel BM25 + cross-corpus latest.

---

## Earlier releases

Detailed commit history in each repo. Search by tag for:

- `v0.7.0` (2026-05-17) — initial 6-corpus engine, Drafter Studio,
  workflow builder, MCP server, Word add-in.
- `v0.5.0` (2026-05-15) — initial demo build, FTS5 search, basic chat.

---

## Pushing this release

```bash
# Backend → Nyayasathi-AI/Nyayasathi_main
cd "/Users/pranav/Desktop/LexSearch-main 2"
git push nyayasathi_main main

# Frontend → Nyayasathi-AI/Sanhita_main
cd sanhita-react
git push nyayasathi main

# Data pipeline → Nyayasathi-AI/data_main
cd /Users/pranav/Desktop/india-judgments-corpus
git push nyayasathi main
```

After all three land, tag the trio with the same release number so the
submodule pointer in the backend resolves cleanly.

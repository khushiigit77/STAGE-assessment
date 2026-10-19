# AI collaboration log

## Tools

* **Claude Code** (Anthropic, model Claude Opus 5.5) in VS Code on Windows. I used it as the main implementer, working
  from a detailed task specification I gave it.
* **[FILL IN: any other tools you used, such as ChatGPT/Codex, documentation sites or a colleague's review. Write
  "none" if there were none.]**

No pack data was sent to any external service other than the coding assistant session itself.

## How the work was split

| Area | AI contribution | How I checked it |
|---|---|---|
| Data inspection | Wrote throwaway profiling scripts and ran them on the real files. Found duplicate ids, clock skew and offline buffering, conflicting profile rows, d7/NA/null dialects, inconsistent deeplink dialects, two catalogue prompt injections, the sparse d3 catalogue, and the pilot's arm/served structure | Each finding is quantified in ARCHITECTURE.md §2 and handled in code. Spot-check any row with the commands in that section |
| Design | Proposed the architecture: deterministic eligibility and verification, the model only ranks, holdout shadow decisions for arm-blind measurement, SQLite with compare-and-set | **[FILL IN: which design choices you reviewed, questioned or changed]** |
| Code (`src/`) | Wrote all modules | 39 automated tests; an end-to-end replay with 0 simulator policy problems; manual reading of the generated reports |
| Tests (`tests/`) | Wrote tests that exercise behaviour, not implementation details | When tests failed, the cause was diagnosed and fixed rather than the test weakened (see below) |
| Statistics | Implemented the ITT, the stratified estimate and the permutation test | I reproduced the internal pilot report's own figures (217 served at 40.1%, 3,674 others at 17.7%) before replacing them with the randomized comparison. **[FILL IN: if you hand-checked a CI, say so]** |
| Documents | Drafted all documents from the generated artifacts | Every number is copied from `sample_run/`; the worked journey was traced through the actual records |

## Problems caught by checking AI output

* **A real precedence bug, caught by a test.** A purchase that happened before the profile export but was *received*
  after it was being ignored. The rule now also uses receipt time, because the export cannot reflect events it had
  not received. The fix was in the code; the test was not changed.
* **A test fixture error.** A viewer with plays but no app-open events correctly qualified for G12 ("stopped
  visiting"). The fixture was wrong, not the code, so I fixed the fixture.
* **An over-strict determinism test.** A field-by-field diff of two runs showed that only measured timing differed.
  The test now excludes only those timing fields.
* **A performance bug.** Loading took 47 s because of an O(n·m) check. Fixed to O(n), now about 2-3 s.
* **A false claim in a generated report.** It stated that every cell had n ≥ 30. Small cells are now flagged instead.
* **A documentation error.** Numbers in the docs were checked against the artifacts, and three were corrected (the
  outage fallback count, the verifier test count, and "pages shown" vs "page views").

## What I would not delegate without review

These judgements are mine to defend, whoever drafted them: goal priority, the G06/G10/G11/G12 assumptions, not using
WhatsApp, the next-day 19:00 push, the 5 s model timeout and the 2 s store timeout, and the confidence formula. Each
is stated as an assumption in ARCHITECTURE.md and KNOWN_LIMITATIONS.md.

**[FILL IN before submitting: 2-3 sentences, in your own words, on what you personally reviewed or changed, and how
long you spent on it. Evaluators value an honest account over an impressive one.]**

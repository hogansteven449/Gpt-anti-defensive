---
name: anti-defensive-writing
description: Draft or revise Chinese or English technical reports with atomic reader questions, frozen probability estimates, and affirmative writing objectives. The main agent prepares PDF, LaTeX, Word, or text inputs, runs the five-stage Markdown writing harness, then formats and verifies editable Word or LaTeX plus PDF deliverables.
---

# Anti-Defensive Writing

Use three logical agents and at most five writing calls: Reviewer A; Probability Agent B with separate B1 and B2 calls; Writer with separate body and optional future-section calls. The main Codex agent owns document tools before and after those calls. Read the [five-stage content contract](references/workflow-contract.md) and follow the [document preparation and delivery procedure](references/document-workflow.md).

Every logical agent must accept Chinese and English input. Where the task is available, follow its requested output language for free-text questions, reasons, objectives, and prose. B1 instead uses the review-question language, otherwise the main source language; do not expose or infer the withheld task for language selection. Keep schema keys unchanged and evidence quotations exact, including their original language. Keep technical identifiers intact. If no output language is specified, use the main source language; the future call follows the frozen body's language. The Writer produces formal, submission-ready prose without drafting shorthand, process commentary, or accidental language mixing.

## Main-agent preparation

Preserve original inputs. Use PDF, LaTeX, Word, and image tools in the main agent to inspect the documents and prepare checked UTF-8 Markdown, local resources, and provenance. The `prepare` command provides preliminary extraction and findings; it does not certify lossless conversion. Resolve formulas into editable math and tables into editable cell structures. Preserve figures, captions, citations, references, labels, units, and source identity. Do not replace formulas or tables with images. If extraction is incomplete, inspect the original pages and repair the editable representation, or report the unresolved conversion with diagnostics before running on incomplete material.

Save the user's actual writing task unchanged in a UTF-8 task file. Keep skill invocation, extraction notes, export settings, operational budgets, and threshold overrides separate. Use prepared `.md` or `.txt` files as the writing inputs. Do not pass binary files or raw LaTeX projects directly to `run`.

```text
python "$SKILL_DIR/scripts/run_harness.py" prepare --source "draft.docx" --out "prepared"
python "$SKILL_DIR/scripts/run_harness.py" run --source "prepared/materials.md" --task-file "task.txt" --out "new-run"
```

Use Python 3.11+ and an authenticated native Codex executable. A WindowsApps alias is not a usable interpreter. Supply `--codex-bin` when needed; Windows `.cmd` and `.bat` wrappers are unsupported. No additional API key or MCP connection is needed. Honor an explicitly requested model through `--model`; otherwise use the actual configured model without guessing or substituting. `doctor --auth --formats` checks dependencies and authentication, not document fidelity.

## Five writing calls

Follow the linked content contract exactly. The five original prompts remain unchanged byte-for-byte as prefixes; only language-interface requirements are appended to all five and Markdown-representation requirements to the two Writer prompts. Do not rewrite the original prompt text. Do not add a baseline writer, semantic evaluator, external research, hidden review, extra child agents, or automatic rewrite loop. Child calls have no document-tool or delegation responsibility. Preparation and formatting by the main agent do not authorize substantive review or new facts.

Pass each prepared source with a separate `--source`. Set length guidance with `--word-budget` and `--future-words` where appropriate, for example `--word-budget 500 --future-words 80`. Preserve the user's actual character or word requirement in the task; do not treat whitespace counts as Chinese character counts. Do not truncate or resample to force length. Use `--threshold` only within `[0.33, 0.67]`; use `--future-work off` when the user excludes future work. Do not force a question count or change frozen probabilities to alter selection.

Writer outputs are Markdown in the schema's `document` and `appendix` fields. The first call returns the complete main text; the optional second call adds only future work. `outputs/document.md` is the complete assembled document. Output-format requests are fulfilled afterward by the main agent; they do not switch Writer into LaTeX, Word, or PDF generation. Preserve editable math, editable tables, local image references, and citations throughout the writing stages.

## Main-agent formatting and delivery

Use frozen complete Markdown to create editable Word or a LaTeX project, plus PDF, with the separate export tool and available document tools. Carry provenance and required resources into delivery. Formatting may repair syntax, encoding, fonts, references, or layout; it must not silently alter claims, add limitations, remove evidence, or rewrite the frozen content.

```text
python "$SKILL_DIR/scripts/run_harness.py" export --main-file "new-run/outputs/document.md" --provenance-file "prepared/provenance.json" --output-format latex --output-format pdf --latex-check required --out "delivery"
```

Before declaring success, actually compile LaTeX when applicable, reopen the editable artifact and PDF, render and inspect every page, and verify formulas, table cells, figures, citations, references, UTF-8 source files, and Chinese glyphs. Use `verify --file PATH --file PATH --report NEW.json` to record automated readback checks; it checks containers and UTF-8, not actual application opening or visual completeness. A text extraction or successful exit code alone does not establish readable output. Screenshots cannot replace editable math or tables. If compilation, opening, rendering, or a required structural check fails, retain diagnostics and identify the incomplete step; never mark the delivery fully verified.

Keep actual prompts, input provenance, outputs, frozen records, token usage, timing, validation results, and failure logs. Use new preparation, run, and delivery directories. Link requested editable files and PDF, with Markdown and logs when requested. Fixture outputs are synthetic and must be labeled as such.

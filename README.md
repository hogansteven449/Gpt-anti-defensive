# Anti-Defensive Writing — Codex Skill 3.1.2

A three-agent, five-stage writing harness with a document workflow managed by the main Codex agent. Reviewer A, Probability Agent B, and Writer handle content. The main agent handles PDF, LaTeX, and Word preparation, then formats and verifies the deliverables. Every agent supports Chinese and English; the user's requested output language takes priority.

The Writer returns Markdown. For document delivery, the main agent produces editable Word (`.docx`) or LaTeX (`.tex` with its project resources), plus PDF. Formulas and tables must remain editable. Pictures of formulas or tables are not accepted conversions.

## Install

Extract the ZIP into a folder named `anti-defensive-writing`, and open PowerShell in that folder. The archive entries are directly at its root; there is no additional `skills/` wrapper:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

The default destination is `~/.agents/skills/anti-defensive-writing`. No Git, gh, MCP service, additional API key, or Codex configuration change is required. The native Codex CLI must already be authenticated. To back up and replace a different installed version:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Replace
```

Backups are kept in a sibling `skill-backups` directory. Optional document dependencies can be installed into the chosen Python environment:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -WithFormats -WithPandoc
```

`-WithFormats` installs optional Python document libraries. `-WithPandoc` installs the packaged Pandoc dependency. These options do not install a TeX distribution, fonts, Microsoft Word, or LibreOffice. Use appropriate local tools for compilation and rendering.

The installer detects Python Launcher, the Codex bundled interpreter, and usable Python executables on PATH. Supply an explicit Python 3.11+ interpreter with `-PythonExe` when needed. Cross-platform and project-local installation:

```text
python3 install.py
python3 install.py --skills-dir /path/to/project/.agents/skills
python3 install.py --with-formats --with-pandoc
python3 install.py --dry-run
```

Alternatively, place this root folder, named `anti-defensive-writing`, in a Codex skill directory. `SKILL.md`, `agents/`, `scripts/`, and `references/` must remain siblings. The supplied installer copies only those runtime resources; `tests/` and the release/install files are not installed. Invoke it with `$anti-defensive-writing` or the available skill selector.

## Use

Example user requests:

```text
Use $anti-defensive-writing to expand this project draft into a rigorous English report of approximately 500 words to submit to my supervisor. Deliver editable Word and PDF files and save the workflow logs.
```

```text
使用 $anti-defensive-writing，将附件扩写为约1500字、提交给主管的正式中文报告。交付可编辑的LaTeX工程和PDF，保留公式、表格、图片和参考文献，并保存运行日志。
```

The main agent preserves the user's actual writing request unchanged in a UTF-8 task file. Skill invocation, extraction notes, format settings, operational budgets, and threshold overrides remain separate.

```text
PDF / LaTeX / DOCX / text
          |
Main agent: inspect, extract, resolve editable structures, prepare checked Markdown
          |
A review -> B1 probabilities -> B2 selection -> Writer body -> optional Writer future
          |
Complete Markdown, frozen writing records, prompts and logs
          |
Main agent: format -> DOCX or LaTeX + PDF -> compile/open/render/encoding checks
```

Extraction is preliminary. Arbitrary PDFs do not automatically provide recoverable formula syntax, table cells, reading order, or reference structure. The main agent must inspect the source and resolve findings before the writing calls. If a required editable structure cannot be recovered, retain diagnostics and report incomplete conversion; do not substitute screenshots or claim success.

Follow [the document workflow](references/document-workflow.md). A manual sequence, after selecting a usable Python interpreter:

```text
python "$SKILL_DIR/scripts/run_harness.py" doctor --auth --formats
python "$SKILL_DIR/scripts/run_harness.py" prepare --source "draft.docx" --out "prepared"
```

Inspect and complete `prepared/materials.md`, its resources, provenance, and findings. Preparation alone does not certify fidelity. Then run:

```text
python "$SKILL_DIR/scripts/run_harness.py" run --source "prepared/materials.md" --task-file "task.txt" --out "new-run" --word-budget 500 --future-words 80
```

Export the complete frozen Markdown separately:

```text
python "$SKILL_DIR/scripts/run_harness.py" export --main-file "new-run/outputs/document.md" --provenance-file "prepared/provenance.json" --output-format docx --output-format pdf --out "delivery-word"
python "$SKILL_DIR/scripts/run_harness.py" export --main-file "new-run/outputs/document.md" --provenance-file "prepared/provenance.json" --output-format latex --output-format pdf --latex-check required --out "delivery-latex"
```

`run` writes Markdown; `prepare` and `export` are main-agent document tools. The main agent must still reopen the artifacts, render and inspect every page, and check structure and encoding. A successful export command alone does not establish readable delivery.

`--source` may be repeated. Preparation supports LaTeX project settings `--project-root` and `--main-tex`, and Word settings `--docx-engine python-docx|pandoc` with optional `--pandoc-bin`. Use a new directory for each operation. See command help for the current options.

Use an authenticated native Codex executable. The runner uses the actual configured model unless the user specifies one with `--model`; it does not silently substitute models. Windows `.cmd` and `.bat` wrappers are unsupported; supply a native `codex.exe` with `--codex-bin` when needed.

## Contract and records

The [five-stage contract](references/workflow-contract.md) defines exact input boundaries. The five original prompt files are retained byte-for-byte as prefixes; language-interface requirements are appended to all five, and Markdown-representation requirements are appended only to the two Writer prompts. No original content instruction is rewritten or removed. B1 estimates P1 (misunderstanding or critical challenge) and P2 (constructive follow-up) before receiving the writing task. B2 receives that task afterward and uses one threshold `T` in `[0.33, 0.67]`; both streams admit values `>= T`. The optional `--threshold` override has priority. The future call is skipped when no future points qualify or the user selects `--future-work off`.

All logical agents support Chinese, English, and mixed-language evidence. JSON field names stay fixed; free-text values and prose follow the requested language where the task is available. B1 follows the review-question language, otherwise the main source language; it does not receive or infer the withheld task. Evidence quotations remain exact. Preserve the user's word or character requirement in the task; whitespace counts do not establish compliance with Chinese length requirements.

The writing run saves actual prompts, inputs, outputs, frozen records, token usage, timings, receipts, validation records, and failure diagnostics. Preparation and export records remain separate. There is no baseline writer, defensive-writing evaluator, automatic semantic rewrite loop, or extra review agent inside the harness. Evaluation belongs outside the skill.

Before delivery, the main agent checks actual compilation where applicable; reopens the editable document and PDF; inspects formulas, tables, figures, references, and rendered pages; and verifies UTF-8 text and Chinese glyphs. The separate readback helper can record container and encoding checks:

```text
python "$SKILL_DIR/scripts/run_harness.py" verify --file "delivery-latex/document.tex" --file "delivery-latex/document.pdf" --report "readback.json"
```

The helper does not perform the main agent's actual application-opening, visual, or Chinese glyph checks and therefore does not certify complete delivery. Missing tools and failed checks must be reported as incomplete validation.

## Root layout and developer tests

```text
anti-defensive-writing/
  SKILL.md
  agents/
  scripts/
  references/
  tests/
  install.py
  install.ps1
  README.md
  release-manifest.json
  verification.json
```

Set `SKILL_DIR` to this root folder, or to the installed skill folder, for the example commands above. Runtime paths are resolved relative to the scripts themselves, not the current working directory. Keep the runtime folders together when relocating them. No personal absolute file path or benchmark dataset is required.

`tests/` contains optional developer regression tests for input/output processing, control flow, format adapters, and installer backup/rollback. Normal skill use does not run them. It is not the benchmark corpus or a defensive-writing quality evaluation. Tests import the sibling `scripts/` folder and the root installer; run them from this distribution, not the minimal installed payload. Their fake paths are mocked inputs or path-safety cases, not dependencies on actual files. You can omit `tests/` when distributing only the runtime; the installer does so automatically.

Offline tests do not call models:

```text
python3 -B -m unittest discover -s tests -p test_*.py -v
```

`--backend fixture` is synthetic control-flow verification, not a real writing result. Read `verification.json` for the release's actual verification scope. The package contains no personal API keys, credentials, benchmark datasets, or historical run logs. `release-manifest.json` records the installed skill files and hashes.

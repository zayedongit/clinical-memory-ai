# Rendered PDFs

Generated from the Markdown in `docs/`. **Do not edit these** — edit the
Markdown and run:

```bash
make docs-pdf
```

| File | Pages | Source |
|---|---|---|
| `Clinical-Memory-AI-Interview-Prep.pdf` | 13 | `../INTERVIEW_PREP.md` |
| `Clinical-Memory-AI-Project-Guide.pdf` | 29 | `../CLINICAL_MEMORY_AI_PROJECT_GUIDE.md` |
| `Clinical-Memory-AI-Model-Card.pdf` | 6 | `../MODEL_CARD.md` |

They are committed so the documents can be handed to someone without a
toolchain. That does mean they go stale the moment the Markdown changes — the
Markdown is the source of truth, and these are a convenience.

**How they are built.** pandoc renders Markdown to standalone HTML against
`../pdf.css`, Chrome headless prints it to PDF, and
`scripts/stamp_page_numbers.py` merges the footers on afterwards — Chrome's
command line can only produce a footer containing the source `file://` URL,
which is not something to hand to anyone. Requires `pandoc` and Chrome or
Chromium; no LaTeX.

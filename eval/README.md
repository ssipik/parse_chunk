# PDF parser evaluation

Offline comparison of PDF parsers for stage 4, on the local corpus in
`../../data`. Not part of the shipped stage: own `pyproject.toml`, own venv, no
effect on `parse_chunk/uv.lock`.

```bash
uv sync
.venv/bin/python parser_eval.py                       # both scenarios, all 10 PDFs
.venv/bin/python parser_eval.py --scenarios pymupdf   # one scenario
.venv/bin/python parser_eval.py --data-dir /some/dir --out-dir /some/out
```

## Output

```
out/<scenario>/<pdf-stem>.md     what the parser produced
out/<scenario>/<pdf-stem>.pdf    that Markdown rendered back to PDF, for reading by eye
out/summary.csv                  per file and scenario: parse_ms, chars, headings, tables, rules
```

Open the same stem under both scenario directories to compare. Each rendered
page 1 carries a banner with the scenario, source file, parse time and length.

Two adjustments happen at render time only, never in the saved `.md`: image
links are dropped (they point at files no parser writes), and HTML comments are
escaped so parser noise such as pymupdf4llm's `<!-- picture text -->` markers
stays visible instead of vanishing into the markup.

## Scenarios

- `pymupdf` — what stage 4 uses since 2026-07-22: pymupdf4llm on the
  pymupdf-layout engine. Mirrors `rag_parse_chunk.parsers.parse_pdf`, noise
  strip included; keep the two in step.
- `pymupdf_fast` — the same library with the layout engine off, i.e. what a
  source gets from `pdf_parser: fast`. No source uses it.
- `liteparse` — the parser it replaced, kept as the comparison baseline.

Corpus totals, all three in one process (2026-07-22, 10 files): `pymupdf`
48.9 s / 172 headings / 40 tables, `pymupdf_fast` 26.0 s / 24 / 10,
`liteparse` 0.8 s / 189 / 29. Fast mode buys under 2× — the layout model's
warm-up amortizes over a batch — and loses most of the structure, so `layout`
stays the default. LiteParse's heading count is high and meaningless: those
are key-value lines it mistook for headings.

Add one by putting a `bytes -> str` function in `SCENARIOS` in
[parser_eval.py](parser_eval.py) and its dependency in `pyproject.toml`.

## What the metrics do and don't say

`headings`, `tables` and `rules` are counts of Markdown structure, not quality
scores: a parser that invents headings scores high on `headings`. They are there
to point at files worth opening. `rules` counts `---` lines, which LiteParse
emits as block separators — high values mean interleaved columns.

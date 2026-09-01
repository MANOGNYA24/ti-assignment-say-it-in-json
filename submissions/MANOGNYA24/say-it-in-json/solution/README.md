# .pfcfg → JSON

Converts `.pfcfg` configs (includes, `@ifdef`/`@ifndef` conditionals,
`${VAR}`/`${VAR:-default}`/`${VAR:+alt}` interpolation) into a JSON bundle
that a consumer can evaluate for a given environment without re-implementing
the `.pfcfg` parser — only a flat resolve algorithm over the JSON tree. It
also ships a verifier that proves the JSON evaluator agrees with the
reference `.pfcfg` evaluator, and a report of configs the format can't
represent losslessly. See [`../DECISIONS.md`](../DECISIONS.md) for why the
schema, the resolution rules, and the verification approach look the way
they do — this file is only about running the code.

## Requirements

- Python 3 stdlib only — no `pip install` needed. Tested on Python 3.13.2.
- The `jsonschema` package is used for schema validation *if present*, but
  it's optional: `pfcfg/schema_check.py` falls back to a hand-written
  structural checker when it isn't installed, so validation still runs on a
  clean machine. This was tested with `jsonschema` **not** installed.

## Working directory

Run every command below from this directory —
`submissions/MANOGNYA24/say-it-in-json/solution/`. The `-m pfcfg.cli` module
form only resolves from here (it needs `pfcfg` importable as a package from
the current directory).

```
cd submissions/MANOGNYA24/say-it-in-json/solution
```

## Commands

`convert-all`, `verify`, and `report` all default `configs_dir` to this
repo's own `starter/configs`, found by walking up from `pfcfg/cli.py`
regardless of cwd (`pfcfg/cli.py:41-65`) — so all three run with **zero
arguments**. `convert` is the one exception: it converts a single arbitrary
`.pfcfg` file, not the default set of 5 entries, so it takes a required
direct path with no default — see below.

### Convert all five starter entry points

```
python3 -m pfcfg.cli convert-all
```

Expect five `<entry>.pfcfg -> out/<entry>.json (valid)` lines on stderr,
exit 0, and the converted tree written under `out/`.

### Run the verifier

```
python3 -m pfcfg.cli verify
```

Diffs the reference `.pfcfg` evaluator against the independent JSON
evaluator across the 5 starter entries × 3 environment fixtures (`ci`,
`non-ci`, `globex-production`) in `fixtures/`. Expect a table of all `PASS`
and a final `15/15 passed`, exit 0.

### Run the unmigratable report

```
python3 -m pfcfg.cli report
```

Expect `wrote 2 rows to out/unmigratable.ndjson (2 cycle); 0 cell(s)
skipped (not PASS in verify)` on stderr, exit 0. `out/` is gitignored, so
the file won't be in a fresh clone — see the sample output below.

### Convert one config

```
python3 -m pfcfg.cli convert ../../../../starter/configs/customers/acme-corp/pipeline.pfcfg -o out/acme.json
```

Expect: `wrote out/acme.json (valid against .../schema/pfcfg.schema.json)` on
stderr, exit 0. Unlike the three commands above, `convert` has no
`configs_dir` concept — `entry` is a required path to one `.pfcfg` file, so
the `../../../../` here walks up from this nested `solution/` directory to
the repo's `starter/configs/`; it's not something the other commands need.

### Run the test suite

```
python3 -m unittest discover -s tests
```

Expect `Ran 22 tests in ...s` followed by `OK`, exit 0. (One test prints an
extra debug line — `globex PRODUCTION=1 deploy.* : {...}` — that's expected
output from the test itself, not a failure.)

## Sample: `out/unmigratable.ndjson`

Both starter configs that can't be losslessly migrated resolve to the same
root cause — an interpolation cycle in `edge-cases/interpolation-cascade.pfcfg`
— so the report is 2 rows, one per affected key, each deduped across all 3
fixtures into a single `observed_under` list:

```ndjson
{"file": "../../../../starter/configs/edge-cases/interpolation-cascade.pfcfg", "section": "cascade.loop", "key": "a", "reason": "circular reference: cascade.loop.a -> cascade.loop.b -> cascade.loop.a", "kind": "cycle", "entry": "edge-cases/interpolation-cascade.pfcfg", "line": 19, "observed_under": ["ci", "globex-production", "non-ci"]}
{"file": "../../../../starter/configs/edge-cases/interpolation-cascade.pfcfg", "section": "cascade.loop", "key": "b", "reason": "depends on cascade.loop.a, which failed to resolve: circular reference: cascade.loop.a -> cascade.loop.b -> cascade.loop.a", "kind": "cycle", "entry": "edge-cases/interpolation-cascade.pfcfg", "line": 20, "observed_under": ["ci", "globex-production", "non-ci"]}
```

(`file` is printed relative to the current working directory when `report`
ran, i.e. relative to this `solution/` directory — hence the same
`../../../../starter/configs/...` prefix as the `convert` command above.)

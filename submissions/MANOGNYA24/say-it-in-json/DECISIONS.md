# Decisions

## Schema design

The schema mirrors the parse tree (`model.Node`), not a flattened map — one JSON
document per entry config, bundling every reachable file, keyed by resolved path.
Includes are structural directives (target, `once` flag, position); a plain
`@include` marks its path seen for dedup. Conditionals (`@ifdef`/`@ifndef`) are
nodes with an unresolved `body`, so nested includes/assignments are just nodes
inside it. Interpolation is `_ValueParser`'s own structured AST
(`Literal`/`EnvRef`/`KeyRef`), not a raw string — one grammar in the system.
Last-writer-wins needs no encoding: it falls out of array order plus
overwrite-in-walk-order.

Core tradeoff: nothing environment-dependent is resolved at conversion time —
baking it in would freeze one environment's answer into an environment-agnostic
file, the failure mode this format exists to avoid. That keeps per-environment
semantics intact at the cost of a JSON consumer replicating the walker's
algorithm exactly, not just `merge()`. Round-trip (JSON → `.pfcfg`) is out of
scope.

## What "effective settings" means, and how I verified it

`effective_settings = f(config, environment)` — the fully resolved flat key/value
map after includes, conditionals, and interpolation, for one environment; the same
JSON yields different maps under different environments.

Verified by full-map equivalence: the reference evaluator (`walker.walk` +
`interpolate.resolve_all`) and an independent JSON evaluator
(`json_eval.evaluate_bundle`, importing no resolver code) are compared on the
entire values map and error set, not a sample — across 5 starter entry configs ×
3 environment fixtures (`ci`, `non-ci`, `globex-production`) = 15 cells, all PASS
(`pfcfg/verify.py`; also `tests/test_json_eval.py::TestFullMapEquivalence`).

## What the verifier proves — and doesn't

Two independently-written implementations agreeing on every value and diagnostic
across all 15 cells is real evidence the conversion preserves semantics — a second
opinion, not code agreeing with itself, since `json_eval.py` was built from
scratch, never copied from `walker.py`/`interpolate.py`.

The limitation: both sides consume the same `parser.py`/`_ValueParser` grammars to
reach a tree from source text. A systematic misreading — a wrong comment-strip
rule, a wrong `:-`/`:+` split — would corrupt what both "independent" evaluators
see identically before either algorithm runs, and the verifier would pass anyway.
It proves the resolve algorithm was ported correctly, not that the grammar was
read correctly.

## Empirical rules chosen where the spec was silent

- **Include/conditional resolution is one interleaved top-to-bottom walk** —
  `@include` splices inline, so ordering effects (last-writer-wins, dedup) are
  position-based.
- **A plain `@include` also marks its target seen for `@include_once` dedup** —
  proven by globex non-PRODUCTION: `overrides.pfcfg`'s `@include_once` of
  `defaults.pfcfg` no-ops because `pipeline.pfcfg` already included it, keeping
  `retention_days=7` instead of reverting to `14`.
- **A missing `$(section.key)` target is a hard `missing_ref` error**, not empty
  string; an unset `${VAR}` with no default resolves to `""` — only `EnvRef`
  defaults silently.
- **Max interpolation depth = 10** — matches the oracle's locked-in cap.
- **Trailing comments stripped with quote-and-interpolation awareness** — a bare
  `#` scan would corrupt `${SLACK_CHANNEL:-#builds}`.
- **The unmigratable report dedupes per item into an `observed_under` list** — an
  environment-invariant failure (e.g. an include cycle) shouldn't repeat per
  fixture; a differing reason stays distinct.

## Known gaps and the next four hours

- **Shared-grammar blind spot** (above): undetectable by this verifier.
- **Fixture coverage is starter-corpus only** — 5 entries × 3 fixtures, nothing
  adversarial beyond `starter/configs`.

Priority order for the next four hours:
1. An independent parser (source text → tree, without reading `parser.py`) to
   cross-check the grammar — the highest-value gap here.
2. More adversarial fixtures: include cycles in realistic multi-file trees (not
   just `cascade.loop`), and environment-only failures (a `missing_ref` behind
   an inactive `@ifdef`).
3. The JSON → `.pfcfg` round-trip, left unbuilt — the AST is lossless enough to
   support it, out of scope this pass.

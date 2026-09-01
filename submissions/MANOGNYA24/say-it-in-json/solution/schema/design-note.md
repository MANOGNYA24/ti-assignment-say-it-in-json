# Phase 2 design note — target JSON schema (draft, pre-review)

Scope: schema design only. No converter, no schema file, no JSON-emitting
code. This is the artifact to review before I write the formal schema.

## Core decision, stated up front

**The schema is a JSON mirror of the parse tree (`model.Node`), not a
flattened/merged map.** One JSON document per entry config, bundling every
file it can reach, keyed by resolved path. A JSON evaluator re-runs the
*same algorithm* `walker.py` + `interpolate.py` run — interleaved
top-to-bottom traversal, last-writer-wins by walk position, memoized DFS
interpolation — except over JSON nodes instead of Python dataclasses.

Everything below falls out of that one decision. The alternative
("resolve now, annotate later") was tried on paper for each of the five
points and rejected every time for the same reason: `@ifdef`, `@ifndef`,
and `@include`-inside-`@ifdef` are all environment-dependent, and
anything computed from them at conversion time is a per-environment fact
frozen into an environment-agnostic file. That's Jordan's exact failure
mode, just moved from the converter's interpolation logic to its include
logic.

---

## 1. Includes

**Decision: preserve include *directives* as structural nodes (target,
`once` flag, position). Do not emit a post-merge flattened map, ever, as
part of the schema.**

Why a flattened map is disqualified, not just suboptimal: `@include` can
be nested inside `@ifdef`/`@ifndef` (globex: `@ifdef PRODUCTION` →
`on-prem.pfcfg`, `@ifndef PRODUCTION` → `overrides.pfcfg`). A merge is a
single fixed set of keys; which files even get walked is an environment
question. Emitting one flattened map bakes in one environment's answer.
There is no "the" flattened map for a config that has a conditional
include — only "the flattened map under environment E."

Less obviously, `@include_once` dedup is *itself* walk-order-and-environment
contingent, not a static property of the include graph. Walking globex
under a non-PRODUCTION env: `pipeline.pfcfg` includes `defaults.pfcfg`
directly, then (via the `@ifndef PRODUCTION` branch) includes
`overrides.pfcfg`, which does `@include_once ../../_base/defaults.pfcfg`.
That second include is skipped only because the *first* one already
marked the resolved path seen. `walker.py`'s own comment calls this out
explicitly (decision 1 in its docstring): without the dedup, re-walking
`defaults.pfcfg` a second time would silently revert `ci-shared.pfcfg`'s
CI overlay (`retry_count=0` → back to `1`) — this is Jordan's "silent
failure" scenario, already present in the oracle, already guarded against
by tracking a `seen_once` set across the whole walk. A JSON schema that
tried to precompute "which includes are deduped" per file, independent of
environment, cannot express this — it depends on which conditional
branches upstream were active, which depends on env. So the JSON has to
carry the raw ingredients (target path, `once` flag, position) and let
the evaluator compute the seen-set live, exactly like `_WalkContext.seen_once`.

**Round-trip consequence:** full fidelity. The JSON is structurally
isomorphic to `ParsedFile` — order, nesting, `once` flags, and
conditional-gated includes all survive because nothing is resolved. The
cost is that a JSON consumer cannot just `merge()` the file; it needs a
~40-line walker (the JSON evaluator), same as the Python side has one.
That's an intentional cost — a JSON blob that *looks* mergeable but isn't
is worse than one that's honest about needing an evaluator.

**Bundling shape:** one JSON document per entry config (`pipeline.pfcfg`
et al. — matches the five listed entry points), containing every reachable
file's node array keyed by a normalized path, plus an `entry` pointer.
Self-contained on purpose: a consumer shouldn't need filesystem access or
its own relative-path-joining logic to evaluate it (that logic is
resolved once, at conversion time, since it's purely static — unlike
*whether* an include executes, *where* it points is never
environment-dependent). Rejected alternative: one JSON file per `.pfcfg`
file with includes as cross-file references a consumer resolves at eval
time — rejected because it reintroduces exactly the kind of
filesystem-dependent, path-joining logic in the JSON evaluator that the
"self-contained blob" design exists to avoid, for no round-trip benefit
(shared files like `_base/defaults.pfcfg` just get duplicated verbatim
across the entry-config bundles that reach them, which is cheap at this
corpus's scale).

---

## 2. Conditionals

**Decision: `Conditional` is a node type — `{kind, var, body}` — where
`body` is an array of the same node union, recursively. No special
casing for "conditional gates an include" vs. "conditional gates an
assignment": both are just nodes inside `body`.**

This is what makes point 1's "includes can be gated" fall out for free
instead of needing a separate mechanism: an `Include` node living inside
a `Conditional.body` *is* a conditional include, structurally, with zero
extra vocabulary. Nesting is native (JSON arrays nest); depth is whatever
the source has (`_FileParser._parse_block` recurses the same way).

Rejected: JSON Schema `if`/`then`/`allOf` conditionals keyed by
environment variable. Two problems: (a) it requires enumerating known
environments/variables ahead of time to build the `if` predicates, which
inverts the actual requirement (the schema must work for environments not
yet imagined, since "same config, many fake environments" is explicit in
`model.py`'s `Environment` comment); (b) JSON Schema conditionals validate
a document against branches, they don't *select* which branch's data
exists in the document — doesn't map onto "gate a sequence of
walk-order-sensitive mutations."

Rejected: flatten each conditional into a keyed patch object
(`{"when": "PRODUCTION", "set": {...}}`) sitting outside the tree. This
looked promising until nesting: a conditional inside a conditional (or an
include inside a conditional whose target itself contains conditionals)
needs its `body` to be the same recursive node type, not a flat patch —
otherwise you're rebuilding the tree inside patch values anyway, just
with an extra indirection.

---

## 3. Interpolation

**Decision: parse to the same structured AST `interpolate.py` already
defines (`Literal` / `EnvRef{var,mode,expr}` / `KeyRef{path}`), using the
*actual* `_ValueParser`, not raw strings re-parsed later by a
second implementation.**

The real argument here isn't AST-vs-string in the abstract, it's:
*whichever parser produces the JSON must be the one the oracle already
trusts.* `_ValueParser`'s grammar is more subtle than it looks —
`${VAR:-default}` where `default` is itself a segment sequence
(recursive), the `:-` vs `:+` disambiguation happening mid-scan of the
var name, and a comment-stripper in `parser.py` that has to track
"currently inside a `${`/`$(` span" just so a literal `#` inside
`${SLACK_CHANNEL:-#builds}` isn't treated as a comment. That's real,
easy-to-get-subtly-wrong logic. If the JSON schema stores raw strings,
every consumer (the JSON evaluator, and later, presumably, other
language's ports) has to reimplement that grammar a second time, and a
second implementation that's 99% right is exactly Jordan's nightmare —
"translated syntax correctly, semantics incorrectly," except now the
divergence is in interpolation instead of includes. Converting through
the oracle's own `_ValueParser` at conversion time and serializing its
output means there is exactly one interpolation grammar in the entire
system; the JSON evaluator only ever has to *walk* the AST (env lookup,
default/alt branching, DFS over `KeyRef` edges), never *parse* pfcfg
syntax again.

This is not "baking out" interpolation — parsing `${VAR:-X}` into
`{var, mode:"default", expr:[...]}` doesn't touch the environment at all;
`VAR` and `X` stay symbolic. It's the same move as point 2: turn syntax
into structure without resolving it. Concretely, for
`${ACME_RELEASE_TAG:-$(build.node_version)-${GIT_SHA:-dev}}` (acme's
container tag — nested `KeyRef` inside an `EnvRef` default, which is
itself followed by a literal `-` and another nested `EnvRef`), the AST
is a lossless tokenization: reserializing it (concatenate `Literal` text,
re-wrap `EnvRef`/`KeyRef` in their original syntax) reproduces the exact
source string. Nothing is discarded, just structured.

Rejected: storing the raw string only. Forces a second grammar
implementation in every JSON consumer (see above) — the single highest
divergence-risk option on this whole list, because interpolation grammar
is the subtlest syntax in the format.

Rejected: storing both raw string and AST. The AST is a lossless
encoding of the string (verified above), so the raw string adds no
information — only a second source of truth that could silently drift
from the AST if a converter bug ever touched one path and not the other.

Rejected: special-casing pure-literal values (e.g. `platform =
pipelineforge`, no `$` anywhere) as a bare JSON string instead of a
one-element segment array. Saves bytes, costs a type-branch in every
consumer ("is this a string or a segment array?") for a distinction the
oracle itself doesn't make (`RawAssignment.value` is always a string,
parsed lazily and uniformly by `_ValueParser` regardless of content).
Uniform segment arrays, always — even `[{"type":"literal","text":"pipelineforge"}]`.

Rejected: parsing comma-separated values (`steps =
compile,test,package`) into a JSON array. Neither `walker.py` nor
`interpolate.py` treat commas specially anywhere — `RawAssignment.value`
and the resolved value are both opaque strings with commas in them. This
would be adding structure the oracle doesn't have, which is exactly
backwards: the schema's job is to match the oracle's semantics, not
improve on them. A JSON evaluator must resolve `steps` to the string
`"compile,test,package"`, not `["compile","test","package"]`, or it
diverges from the oracle on every list-valued key in the corpus.

One more structural point worth flagging even though it's not one of the
five: **section headers are pointer-setting nodes, not containers.**
`model.SectionHeader` has no body — it just changes `current_section` for
subsequent bare `KeyAssignment` nodes, and per `walker.py`'s own comment,
that pointer is shared across the *entire* walk, not reset on
entering/leaving an included file. The JSON schema carries this over
faithfully: `{"type":"section","path":"build","line":N}` as a flat
sibling node, not `{"sections":{"build":{...}}}`. The tempting
"more JSON-native" nested-object alternative was rejected because it
silently assumes sections are file-scoped or reset-on-include, which is
false here, and it can't represent a bare `SectionHeader` node sitting
inside a `Conditional.body` on its own (no accompanying key on that line)
the way the flat-node form can.

---

## 4. Last-writer-wins ordering

**This isn't a schema problem at all, given points 1–3 — it's resolved
by the fact that the schema is an ordered tree, not a merged object.**

JSON arrays guarantee order; JSON objects don't (spec-wise, and in
practice enough implementations reorder object keys that depending on it
would be fragile). Every design that tries to encode "who wins" as an
annotation on a flattened object (a `priority` counter, a `sequence`
field, a `winner: true` flag) is solving a problem that only exists
*because* the object was flattened in the first place. If two `@ifdef`
blocks both assign `deploy.requires_approval` (acme: an unconditional
`requires_approval = true` at line 20, then `@ifdef ACME_DEPLOY_TARGET`
→ `requires_approval = false` at line 24), the JSON just contains both
`KeyAssignment` nodes, in source order, as siblings/nested-under-their-conditional
— exactly where they appear in the file. The JSON evaluator's walk
(a dict keyed by dotted path, overwritten in traversal order — literally
`_apply_assignment`'s three lines, ported) determines the winner **at
eval time**, per environment, identically to the oracle. No "who wins"
fact is ever computed or stored at conversion time, because there isn't
one fact — under `ACME_DEPLOY_TARGET` unset it's `true`, under it set
it's `false`, and the JSON has to support both without a rebuild.

Rejected: flat dotted-path object plus a `priority`/`sequence` int per
candidate value to break ties. Doesn't actually avoid keeping every
losing candidate around (so it isn't "flat" in any way that saves
anything), and doesn't compose with conditionals — each candidate would
still need its own guard-variable annotation, which just reinvents the
`Conditional` node wrapping it, at a second layer, for no benefit over
leaving it in its original tree position.

---

## 5. Unmigratable cases

**Decision: nothing about resolution outcomes gets marked in the schema
itself. The schema stays environment-oblivious by construction (points
1–4). The unmigratable report is a separate artifact, produced by
*running* the JSON evaluator (and the oracle) against concrete
environment fixtures and diffing/collecting diagnostics — never by
annotating the JSON at conversion time.**

Two genuinely different categories, and it matters which is which:

- **Conversion-time unmigratable** (rare in this corpus): the converter
  itself cannot produce a node for some input — malformed syntax that
  `parser.py` would raise `ParseError` on. This is environment-independent
  by construction (it fails before any env is ever consulted) and *is*
  a legitimate schema-adjacent fact: file, line, reason are known at
  conversion time with no environment in the loop. None of the five
  starter entry points actually hit this, but the converter should still
  emit a report row (not silently drop the file) if it ever does.

- **Evaluation-time unresolvable** (the actual interesting cases —
  `cascade.loop`'s `a`/`b` cycle, `include_cycle`, `max_depth`,
  `missing_ref`): these are `Diagnostic`s the oracle only produces by
  *running* `walk()`/`resolve_all()` for a specific `(entry, env)` pair.
  Whether a given diagnostic even fires can depend on environment — a
  `missing_ref` to a key that's itself behind an `@ifdef` only fails
  under environments where that `@ifdef` is inactive; an `include_cycle`
  behind a conditional only exists under environments where that branch
  is walked. Marking a key "broken" in the JSON for one assumed
  environment would be false for another — the same silent-divergence
  risk as everything else on this list, just relocated into the
  diagnostics layer. So: don't. The JSON evaluator surfaces the identical
  `Diagnostic`-shaped output (`kind`, `reason`, `loc`, `path`) as the
  oracle, per environment, and the unmigratable report is built from
  running both sides across the required fixtures (at least one CI-like,
  one non-CI, per the assignment) and collecting what fails — this is
  also exactly what the equivalence verifier needs to do, so the report
  and the verifier share a code path rather than being two separate
  guesses at the same thing.

One implementation trap worth flagging now, found while re-reading
`interpolate.py`: `resolve_all()` returns `ResolvedConfig.errors =
list(ctx.errors)` — it does **not** fold in `MergedConfig.diagnostics`
(the `include_cycle` list from the walk). The oracle keeps walk-time and
interpolation-time diagnostics in two separate places and never merges
them itself. Report-generation (on both the oracle side and the JSON
evaluator side) has to pull from *both* `MergedConfig.diagnostics` and
`ResolvedConfig.errors`, or `include_cycle` cases will silently vanish
from the report — not a schema issue, but a real gap I'd otherwise hit
while wiring up the verifier in the next phase.

Also explicitly **not** treating as unmigratable: `${VAR}` (plain, no
default) with `VAR` unset — e.g. `initech/secrets.pfcfg`'s
`REQUIRED_SIGNING_SECRET`, `conditional-includes.pfcfg`'s
`REQUIRED_API_ENDPOINT`. Per spec and per `interpolate.py`
("Leaf: never a graph edge" — `ctx.env.get(seg.var, "")`), these resolve
cleanly to `""`. They're semantically alarming (a signing key of `""`)
but the oracle doesn't error on them, so the JSON evaluator can't either
without diverging from the ground truth it's supposed to match. Tempting
to lint these as "probably unmigratable" — rejected, because "the test of
this schema is reproducing the reference evaluator's exact output," and
inventing an error the oracle doesn't raise is exactly as wrong as
missing one it does.

---

## Node shapes (informal, for the worked example below)

```
File     := { path: string, nodes: Node[] }
Node     := Section | Assign | Include | Conditional
Section  := { type: "section", path: string, line: int }
Assign   := { type: "assign", key: string, value: Segment[], line: int }
Include  := { type: "include", target: string, once: bool,
               resolved_path: string, line: int }
Conditional := { type: "conditional", kind: "ifdef"|"ifndef",
                  var: string, body: Node[], line: int }

Segment  := Literal | EnvRef | KeyRef
Literal  := { type: "literal", text: string }
EnvRef   := { type: "env", var: string, mode: "plain"|"default"|"alt",
               expr?: Segment[] }   // present iff mode != "plain"
KeyRef   := { type: "key", path: string }

Bundle   := { entry: string, files: { [resolved_path]: File } }
```

`target` is the literal directive text (unresolved, for round-trip
display); `resolved_path` is the same join-and-normalize `walker.py`
already does (`os.path.realpath(os.path.join(base_dir, target))`) —
purely static, computed once at conversion time, and used as the key
into `files` so the JSON evaluator never touches a filesystem. Line
numbers are `SourceLoc.line`, carried through so the JSON evaluator's
diagnostics can cite the same file:line the oracle's would.

For this worked example I'm using repo-relative paths
(`starter/configs/...`) instead of `os.path.realpath` absolute paths, for
readability — the real converter will key by realpath, same as
`ParsedFile.path`/`SourceLoc.file` already do.

---

## Worked example: globex `pipeline.pfcfg`

This is the specific case requested: the `@ifdef PRODUCTION` /
`@ifndef PRODUCTION` include pair preserved as two sibling `Conditional`
nodes, neither resolved. Under a `PRODUCTION`-set environment the JSON
evaluator's walk activates the first and skips the second; under any
other environment, the reverse — the JSON is identical either way, only
the evaluator's traversal differs.

I've fully expanded every file globex's entry config can reach
(`defaults.pfcfg`, `toolchains.pfcfg`, `ci-shared.pfcfg`, `on-prem.pfcfg`,
`overrides.pfcfg`) so the include-graph and `@include_once` mechanics are
visible end to end — including the exact case `walker.py`'s docstring
calls out: `overrides.pfcfg` re-`@include_once`-ing `defaults.pfcfg`,
which the JSON evaluator must dedup against the *first* `defaults.pfcfg`
include (from `pipeline.pfcfg` directly) via a walk-scoped seen-set, not
anything recorded in the JSON. `notifications.pfcfg` is included too
(reached via `defaults.pfcfg`), to show a same-guard-var
`@ifdef`/`@ifndef` pair (`SLACK_WEBHOOK`) on a repeated section header.

```json
{
  "entry": "starter/configs/customers/globex/pipeline.pfcfg",
  "files": {
    "starter/configs/customers/globex/pipeline.pfcfg": {
      "path": "starter/configs/customers/globex/pipeline.pfcfg",
      "nodes": [
        { "type": "include", "target": "../../_base/defaults.pfcfg", "once": false,
          "resolved_path": "starter/configs/_base/defaults.pfcfg", "line": 3 },
        { "type": "include", "target": "../../environments/ci-shared.pfcfg", "once": false,
          "resolved_path": "starter/configs/environments/ci-shared.pfcfg", "line": 4 },

        { "type": "conditional", "kind": "ifdef", "var": "PRODUCTION", "line": 6,
          "body": [
            { "type": "include", "target": "../../environments/on-prem.pfcfg", "once": false,
              "resolved_path": "starter/configs/environments/on-prem.pfcfg", "line": 7 }
          ]
        },
        { "type": "conditional", "kind": "ifndef", "var": "PRODUCTION", "line": 10,
          "body": [
            { "type": "include", "target": "overrides.pfcfg", "once": false,
              "resolved_path": "starter/configs/customers/globex/overrides.pfcfg", "line": 11 }
          ]
        },

        { "type": "section", "path": "customer", "line": 14 },
        { "type": "assign", "key": "id", "line": 15,
          "value": [ { "type": "literal", "text": "globex" } ] },
        { "type": "assign", "key": "tier", "line": 16,
          "value": [ { "type": "literal", "text": "standard" } ] },

        { "type": "section", "path": "build", "line": 18 },
        { "type": "assign", "key": "steps", "line": 19,
          "value": [ { "type": "literal", "text": "compile,test,package" } ] },
        { "type": "assign", "key": "language", "line": 20,
          "value": [ { "type": "literal", "text": "go" } ] },
        { "type": "assign", "key": "go_version", "line": 21,
          "value": [ { "type": "key", "path": "toolchain.go.version" } ] },

        { "type": "section", "path": "package", "line": 23 },
        { "type": "assign", "key": "format", "line": 24,
          "value": [ { "type": "literal", "text": "tar.gz" } ] },
        { "type": "assign", "key": "output_dir", "line": 25,
          "value": [ { "type": "literal", "text": "dist/" } ] },

        { "type": "section", "path": "deploy", "line": 27 },
        { "type": "assign", "key": "target", "line": 28,
          "value": [
            { "type": "env", "var": "GLOBEX_ENV", "mode": "default",
              "expr": [ { "type": "literal", "text": "development" } ] }
          ] }
      ]
    },

    "starter/configs/_base/defaults.pfcfg": {
      "path": "starter/configs/_base/defaults.pfcfg",
      "nodes": [
        { "type": "include", "target": "toolchains.pfcfg", "once": true,
          "resolved_path": "starter/configs/_base/toolchains.pfcfg", "line": 2 },
        { "type": "include", "target": "notifications.pfcfg", "once": true,
          "resolved_path": "starter/configs/_base/notifications.pfcfg", "line": 3 },

        { "type": "section", "path": "meta", "line": 5 },
        { "type": "assign", "key": "format_version", "line": 6,
          "value": [ { "type": "literal", "text": "3" } ] },
        { "type": "assign", "key": "platform", "line": 7,
          "value": [ { "type": "literal", "text": "pipelineforge" } ] },

        { "type": "section", "path": "build", "line": 9 },
        { "type": "assign", "key": "timeout_minutes", "line": 10,
          "value": [ { "type": "literal", "text": "45" } ] },
        { "type": "assign", "key": "retry_count", "line": 11,
          "value": [ { "type": "literal", "text": "1" } ] },
        { "type": "assign", "key": "image", "line": 12,
          "value": [
            { "type": "env", "var": "PF_BUILD_IMAGE", "mode": "default",
              "expr": [ { "type": "literal", "text": "pfci/builder:22.04" } ] }
          ] },
        { "type": "assign", "key": "parallel", "line": 13,
          "value": [ { "type": "literal", "text": "false" } ] },

        { "type": "section", "path": "cache", "line": 15 },
        { "type": "assign", "key": "enabled", "line": 16,
          "value": [ { "type": "literal", "text": "true" } ] },
        { "type": "assign", "key": "key_prefix", "line": 17,
          "value": [
            { "type": "env", "var": "CI", "mode": "alt",
              "expr": [ { "type": "literal", "text": "ci-" } ] },
            { "type": "env", "var": "CACHE_NAMESPACE", "mode": "default",
              "expr": [ { "type": "literal", "text": "default" } ] }
          ] },

        { "type": "section", "path": "artifacts", "line": 19 },
        { "type": "assign", "key": "retention_days", "line": 20,
          "value": [ { "type": "literal", "text": "14" } ] },
        { "type": "assign", "key": "compress", "line": 21,
          "value": [ { "type": "literal", "text": "true" } ] }
      ]
    },

    "starter/configs/_base/toolchains.pfcfg": {
      "path": "starter/configs/_base/toolchains.pfcfg",
      "nodes": [
        { "type": "section", "path": "toolchain.node", "line": 3 },
        { "type": "assign", "key": "version", "line": 4,
          "value": [
            { "type": "env", "var": "NODE_VERSION", "mode": "default",
              "expr": [ { "type": "literal", "text": "20" } ] }
          ] },
        { "type": "assign", "key": "binary", "line": 5,
          "value": [ { "type": "literal", "text": "node" } ] },
        { "type": "assign", "key": "package_manager", "line": 6,
          "value": [
            { "type": "env", "var": "PKG_MGR", "mode": "default",
              "expr": [ { "type": "literal", "text": "npm" } ] }
          ] },
        { "type": "assign", "key": "install_cmd", "line": 7,
          "value": [
            { "type": "env", "var": "PKG_MGR", "mode": "default",
              "expr": [ { "type": "literal", "text": "npm" } ] },
            { "type": "literal", "text": " ci" }
          ] },

        { "type": "section", "path": "toolchain.go", "line": 9 },
        { "type": "assign", "key": "version", "line": 10,
          "value": [
            { "type": "env", "var": "GO_VERSION", "mode": "default",
              "expr": [ { "type": "literal", "text": "1.22" } ] }
          ] },
        { "type": "assign", "key": "module_mode", "line": 11,
          "value": [ { "type": "literal", "text": "on" } ] },

        { "type": "section", "path": "toolchain.rust", "line": 13 },
        { "type": "assign", "key": "version", "line": 14,
          "value": [
            { "type": "env", "var": "RUST_VERSION", "mode": "default",
              "expr": [ { "type": "literal", "text": "stable" } ] }
          ] },
        { "type": "assign", "key": "target", "line": 15,
          "value": [
            { "type": "env", "var": "RUST_TARGET", "mode": "default",
              "expr": [ { "type": "literal", "text": "x86_64-unknown-linux-gnu" } ] }
          ] },

        { "type": "section", "path": "toolchain.default", "line": 17 },
        { "type": "assign", "key": "name", "line": 18,
          "value": [ { "type": "literal", "text": "node" } ] },
        { "type": "assign", "key": "compiler", "line": 19,
          "value": [ { "type": "key", "path": "toolchain.node.binary" } ] }
      ]
    },

    "starter/configs/_base/notifications.pfcfg": {
      "path": "starter/configs/_base/notifications.pfcfg",
      "nodes": [
        { "type": "section", "path": "notify", "line": 3 },
        { "type": "assign", "key": "on_success", "line": 4,
          "value": [
            { "type": "env", "var": "NOTIFY_SUCCESS", "mode": "default",
              "expr": [ { "type": "literal", "text": "log" } ] }
          ] },
        { "type": "assign", "key": "on_failure", "line": 5,
          "value": [
            { "type": "env", "var": "NOTIFY_FAILURE", "mode": "default",
              "expr": [ { "type": "literal", "text": "email" } ] }
          ] },

        { "type": "section", "path": "notify.email", "line": 7 },
        { "type": "assign", "key": "recipients", "line": 8,
          "value": [
            { "type": "env", "var": "BUILD_NOTIFY_LIST", "mode": "default",
              "expr": [ { "type": "literal", "text": "ops@example.invalid" } ] }
          ] },
        { "type": "assign", "key": "from", "line": 9,
          "value": [ { "type": "literal", "text": "pipelineforge-noreply@example.invalid" } ] },

        { "type": "section", "path": "notify.slack", "line": 11 },
        { "type": "conditional", "kind": "ifdef", "var": "SLACK_WEBHOOK", "line": 12,
          "body": [
            { "type": "assign", "key": "enabled", "line": 13,
              "value": [ { "type": "literal", "text": "true" } ] },
            { "type": "assign", "key": "channel", "line": 14,
              "value": [
                { "type": "env", "var": "SLACK_CHANNEL", "mode": "default",
                  "expr": [ { "type": "literal", "text": "#builds" } ] }
              ] }
          ]
        },
        { "type": "conditional", "kind": "ifndef", "var": "SLACK_WEBHOOK", "line": 17,
          "body": [
            { "type": "section", "path": "notify.slack", "line": 18 },
            { "type": "assign", "key": "enabled", "line": 19,
              "value": [ { "type": "literal", "text": "false" } ] }
          ]
        }
      ]
    },

    "starter/configs/environments/ci-shared.pfcfg": {
      "path": "starter/configs/environments/ci-shared.pfcfg",
      "nodes": [
        { "type": "conditional", "kind": "ifdef", "var": "CI", "line": 3,
          "body": [
            { "type": "section", "path": "build", "line": 4 },
            { "type": "assign", "key": "parallel", "line": 5,
              "value": [ { "type": "literal", "text": "true" } ] },
            { "type": "assign", "key": "retry_count", "line": 6,
              "value": [ { "type": "literal", "text": "0" } ] },

            { "type": "section", "path": "cache", "line": 8 },
            { "type": "assign", "key": "key_prefix", "line": 9,
              "value": [
                { "type": "literal", "text": "ci-" },
                { "type": "env", "var": "CACHE_NAMESPACE", "mode": "default",
                  "expr": [ { "type": "literal", "text": "shared" } ] }
              ] },

            { "type": "section", "path": "artifacts", "line": 11 },
            { "type": "assign", "key": "retention_days", "line": 12,
              "value": [ { "type": "literal", "text": "7" } ] },

            { "type": "section", "path": "notify", "line": 14 },
            { "type": "assign", "key": "on_failure", "line": 15,
              "value": [ { "type": "literal", "text": "email,slack" } ] }
          ]
        }
      ]
    },

    "starter/configs/environments/on-prem.pfcfg": {
      "path": "starter/configs/environments/on-prem.pfcfg",
      "nodes": [
        { "type": "section", "path": "deploy", "line": 3 },
        { "type": "assign", "key": "strategy", "line": 4,
          "value": [ { "type": "literal", "text": "manual" } ] },
        { "type": "assign", "key": "requires_approval", "line": 5,
          "value": [ { "type": "literal", "text": "true" } ] },
        { "type": "assign", "key": "target", "line": 6,
          "value": [ { "type": "literal", "text": "on-prem" } ] },

        { "type": "section", "path": "container", "line": 8 },
        { "type": "assign", "key": "registry", "line": 9,
          "value": [ { "type": "literal", "text": "registry.globex.internal" } ] },
        { "type": "assign", "key": "push", "line": 10,
          "value": [ { "type": "literal", "text": "false" } ] },

        { "type": "section", "path": "build", "line": 12 },
        { "type": "assign", "key": "image", "line": 13,
          "value": [ { "type": "literal", "text": "pfci/builder:enterprise-rhel8" } ] },

        { "type": "section", "path": "notify", "line": 15 },
        { "type": "assign", "key": "on_success", "line": 16,
          "value": [ { "type": "literal", "text": "email" } ] },
        { "type": "assign", "key": "on_failure", "line": 17,
          "value": [ { "type": "literal", "text": "email,pager" } ] },

        { "type": "section", "path": "notify.email", "line": 19 },
        { "type": "assign", "key": "recipients", "line": 20,
          "value": [ { "type": "literal",
            "text": "sre@globex.example.invalid,release@globex.example.invalid" } ] }
      ]
    },

    "starter/configs/customers/globex/overrides.pfcfg": {
      "path": "starter/configs/customers/globex/overrides.pfcfg",
      "nodes": [
        { "type": "include", "target": "../../_base/defaults.pfcfg", "once": true,
          "resolved_path": "starter/configs/_base/defaults.pfcfg", "line": 2 },

        { "type": "section", "path": "build", "line": 4 },
        { "type": "assign", "key": "parallel", "line": 5,
          "value": [ { "type": "literal", "text": "true" } ] },
        { "type": "assign", "key": "timeout_minutes", "line": 6,
          "value": [ { "type": "literal", "text": "30" } ] },

        { "type": "section", "path": "cache", "line": 8 },
        { "type": "assign", "key": "enabled", "line": 9,
          "value": [ { "type": "literal", "text": "false" } ] },

        { "type": "section", "path": "notify", "line": 11 },
        { "type": "assign", "key": "on_failure", "line": 12,
          "value": [ { "type": "literal", "text": "log" } ] },

        { "type": "section", "path": "deploy", "line": 14 },
        { "type": "assign", "key": "requires_approval", "line": 15,
          "value": [ { "type": "literal", "text": "false" } ] }
      ]
    }
  }
}
```

### Why this proves the point

Notice `pipeline.pfcfg`'s two `Conditional` nodes at lines 6 and 10: both
are present, both have real bodies, neither has been evaluated against
any environment. A JSON evaluator given this exact document and env
`{"PRODUCTION": "1"}` walks the first body (pulls in `on-prem.pfcfg`,
last-writer-wins gives `deploy.requires_approval = "true"`,
`deploy.target = "on-prem"`); given `{}` or `{"CI": "1"}` it walks the
second body instead (pulls in `overrides.pfcfg`, whose own
`@include_once` correctly no-ops against the `defaults.pfcfg` already
walked at line 3, then applies `deploy.requires_approval = "false"`
straight from `overrides.pfcfg` line 15, with `pipeline.pfcfg`'s own
`[deploy] target = ${GLOBEX_ENV:-development}` — line 28 — winning last
regardless of branch since it comes after both conditionals in walk
order). Same JSON, two different effective-settings outcomes, both
reachable from the one document — which is the whole requirement: the
divergence lives in the evaluator's walk, not in a second copy of the
config per environment.

---

## What I'd still need before writing the formal schema

- Sign-off on the five decisions above (this note).
- A written statement of exactly which structural properties the formal
  JSON Schema should *validate* (e.g. "every `key` ref's `path` looks
  like a dotted identifier," "`once` is a bool") vs. which are evaluator
  concerns it can't check statically (existence of the target, cycles) —
  matches the assignment's own caution that "a JSON Schema that validates
  every wiki example" is not the goal.
- Whether the formal schema should be expressed as JSON Schema (validates
  the *shape*) plus a separate prose/typed spec of walk semantics (since
  JSON Schema can't express "last-writer-wins by array position" or
  "shared seen-set across the whole bundle" — those are evaluator
  algorithm, not document shape).

Stopping here for review before writing the formal schema file.

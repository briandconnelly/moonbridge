# M0 findings — kimi-code 0.39.1 (verified by execution, 2026-08-30)

Re-verification of the 0.35.0 probe set against 0.39.1, following
[docs/UPGRADING-KIMI.md](../../UPGRADING-KIMI.md). Every row below was run, not predicted.

**Result: no guarantee moved.** Every load-bearing behavior holds. One new failure
signature was captured, and one probe found a graceful-shutdown improvement that does
*not* remove the orphan sweep.

## Static surface

`kimi --help` differs from 0.35.0 by one cosmetic line (`login` -> `login [options]`).
Every flag this server sends or refuses is still advertised: `--prompt`,
`--output-format`, `--agent-file`, `--model`, `--skills-dir`, `--add-dir`, and each of
`PROMPT_MODE_INCOMPATIBLE_FLAGS`. Checked with a known-absent control flag first, so the
all-present result is not a broken grep.

## Probes

| # | Probe | Result on 0.39.1 |
|---|---|---|
| 1 | Read-only `--agent-file` guarantee | **PASSED, unchanged.** The generated profile reported exactly `Glob, Grep, Read`, refused the shell command, and produced no file. |
| 2 | Read-only reads an absolute path outside the workspace | **Unchanged.** Returned a canary file's contents verbatim. `READ_ONLY_CONFIDENTIALITY_LIMIT` stays accurate. |
| 3 | Bogus `KIMI_MODEL_THINKING_EFFORT` | **Unchanged.** Silently ignored, exit 0. `effort_silently_ignored_upstream` stays true; no backend effort branch is reachable. |
| 4 | Write to an absolute path outside the worktree | **Unchanged — still not contained.** kimi noted the target was outside its working directory and ran the command anyway. The worktree stays defense in depth only. |
| 5 | Process-group kill | **Unchanged where it matters.** See below. |
| 6 | argv prompt ceiling | **Unchanged.** Clean at 900k; Node `RangeError: Maximum call stack size exceeded` at 950k; exit 7 at 1M; execve `argument list too long` at 1.2M. `MAX_ARGV_PROMPT_CHARS = 8_000` stays far below. |
| 7 | `--output-format stream-json` line shapes | **Unchanged.** Sample captured in `stream-json-sample.jsonl`. |
| 8 | Unknown `--model` alias | **Unchanged**, and `--model` is honoured. See the second signature below. |

### Probe 1 — the read-only guarantee (the release blocker)

The negative result was trusted only after a positive control: the same instruction under
an unrestricted run *did* create the file. Instrument proven, then:

- read-only run: reported three tools, created nothing.
- control run: created the file, exit 0.

### Probe 5 — orphans still survive the kill this server sends

kimi still runs each Bash command in its **own** process group, so the outcome depends on
the signal:

- **SIGKILL** (what `pontonier.core.runtime` sends): the child **survives**, reparented to
  init — `/bin/bash -c cd '<worktree>' && sleep 4071`, ppid 1, its own pgid. Identical to
  the 0.35.0 M0-7 finding. **`sweep_orphans` is still required**; `needs_orphan_sweep`
  stays true.
- **SIGTERM**: kimi catches it and reaps its children first, leaving no survivor. A
  graceful-shutdown improvement over 0.35.0, but it is not the path this server takes, so
  it must not be read as a reason to weaken the sweep.

The first two attempts at this probe were discarded, not reported: the marker was
`sleep 400 # marker`, and `bash -c` execs a lone command directly and drops the comment,
so the marker never existed and the "no survivor" result was an artifact of a broken
instrument. The marker became a unique duration (`sleep 4071`) only after `pgrep` was shown
to find a known positive.

### Probe 8 — a SECOND invalid-model signature

Two distinct messages, both meaning "the alias is the caller's problem":

```
error: failed to run prompt: Model "X" is not configured in config.toml.
error: failed to run prompt: model X does not resolve to a configured provider
```

Verbatim, as observed on this host (the probe config's `default_model` was
`runpod/kimi-k3`, an alias with no `[models."..."]` section):

```
error: failed to run prompt: Model "definitely-bogus-alias-xyz" is not configured in config.toml.
error: failed to run prompt: model runpod/kimi-k3 does not resolve to a configured provider
```

The first is the 0.35.0 message, for an unknown `-m` alias — unchanged, and `--model` is
still honoured (a bogus alias is rejected by name).

The second is new to these captures. It fires when `config.toml`'s `default_model` names an
alias that has no `[models."..."]` section: kimi fails while resolving the **default**,
before any `-m` override applies, so it reaches the classifier even on a run that passed a
valid `--model`. Whether 0.35.0 also emitted it is **unknown** — that version was no longer
installed to test, so this is recorded as newly captured, not as new behavior.

Measured against the classifier as it stood, this string matched `is_invalid_model` =
False and `is_contract_drift` = False: it surfaced as a generic error, pointing an operator
at an upgrade bug instead of at their own `config.toml`. `_INVALID_MODEL_PATTERNS` now
carries both phrasings, **anchored on the `failed to run prompt:` prefix**. The anchor is
load-bearing: the second message's tail is an ordinary English clause, and
`kimi.classify_failure` tests invalid_model first, over `run.stdout` and `last_message` —
the model's own prose. Unanchored, a run that merely discussed this failure could mask
genuine contract drift, which must always fail loudly. A false negative here is only the
generic error this failure already produced; a false positive silences drift.

> **Update (2026-09-01, #11):** `kimi.classify_failure` no longer tests invalid_model first;
> it follows pontonier's shared order (drift → auth → rate limit → invalid model), so an
> unanchored match could no longer outrank drift. The anchor stays: it still keeps model
> prose that discusses this failure from being reported as `invalid_model` instead of a
> generic `nonzero_exit`. The "trips the drift patterns" rationale for the old order was
> checked against both captured messages here and on 0.35.0 — neither matches a drift
> pattern.

The two causes also carry different repair guidance. They share the `invalid_model` code,
but an unresolvable `default_model` reaches the classifier even when the caller's `model`
argument was valid, so the generic hint ("omit model to use the configured default_model")
names the one action that cannot work. `_unresolved_default_model_error` therefore drops
`field="model"` and names `config.toml` instead.

## Environment notes (not version findings)

- The provider configured on the probe host returned intermittent
  `provider.api_error: 524 status code (no body)`. That is an upstream endpoint transient,
  not kimi behavior; two probe runs were retried because of it. It matches no failure
  signature and surfaces as a generic error.
- Probes ran under a throwaway `KIMI_CODE_HOME` so the host's real `config.toml` was never
  modified.

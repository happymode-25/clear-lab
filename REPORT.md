# clear-lab — build report

**Version:** 1.0.0 · **Repo:** https://github.com/happymode-25/clear-lab (private) ·
**Built:** Oct 2026 · **Basis:** the stand-down sequel to patch-lab — once a
worm is *contained*, when is it provably safe to STOP PATCHING? — built to the
egress-broker/worm-lab/patch-lab bar (single file, stdlib only, fail-closed,
tamper-evident, mutation-swept, CI-gated).

## 1. What was built

`clear-lab.py` is a one-file, zero-dependency (Python 3.9+ stdlib) eradication
and safe stand-down planner. A contained outbreak still hides a pool of up to
`h` infectious hosts below the detection limit; if the crew stands down while
any of them is alive, the worm regrows at `G = (1+β)(1−γ)` per day with nobody
left to clear it. The tool derives `d*` — the extra patching days needed to
drive the bound below capacity — from the closed form `I(d+1) = G·(I(d) − c)`,
proves it against a straight recurrence iteration and a huge-N daily model,
gates immune-host release on the hidden pool being provably dead, and appends
every run to a hash-chained JSONL audit store.

## 2. Security-engineering stance

Same three fail-closed guarantees as the rest of the family:

- **Fail-closed input.** `i0 > N`, `β ≤ 0`, `γ ≤ 0`, `days ≤ 0`, `c < 0`,
  `detect < 0`, `release < 0`, unknown commands: refused with an exit code and
  a reason. Nothing is silently clamped. (`c = 0`, `detect = 0`, `release = 0`
  stay legal: "no capacity / no detection margin / no redeployment" are real
  scenarios, not errors.)
- **Fail-closed planning.** `sweep` reports the *worst* `d*` and stand-down day
  across an uncertainty box — and verifies numerically, before printing
  anything, that the envelope holds at every corner and that each AT RISK
  corner really is un-clearable. If the envelope disagrees, sweep exits 1.
- **Fail-closed evidence.** If the audit chain does not verify *immediately
  after a write*, the tool refuses to report the run. A store that no longer
  verifies is refused for new runs, not silently re-appended.

## 3. The stand-down theorem, and how it is proved

```
I(d+1) = G·(I(d) − c)           worst case starts at the bound h
fixed point = c·G/(G−1)          ABOVE c — the pool contracts only when
h < c·G/(G−1)                    (patch-lab's condition with I₀ → h)
d* = first d with I_d ≤ c        extra patching days needed
stand_down = E + d* + 2          E = first day observed I ≤ h
G ≤ 1                            → the worm dies alone, d* = 0, always safe
```

The daily map is the same explicit, deterministic SIR step as patch-lab —
patch infectious first at capacity `c`, then spread `β·S·I/N`, then recover
`γ·I`. The selftest checks the properties that matter, not the code path:

| Property | How it is checked |
|---|---|
| Stand-down anchor | `d* = 0` when `h ≤ c`; AT RISK (None) at zero capacity; `d* = 5` for `h=40, c=10` |
| Formula vs recurrence | the closed form equals a straight recurrence iterate across a parameter sweep — both paths to the same day |
| Boundary, both directions | `h` just below `c·G/(G−1)` clears; `h` at or above the threshold is AT RISK — fail-closed at equality |
| Numeric oracle | the huge-N daily run from `I₀ = h` crosses `≤ c` at exactly `d*` and reaches 0 at `d*+1` |
| Map vs closed form | inside the linear window the daily map matches `I_d` (worst 2.59e-07) and the containment map matches `G·(I−c)` (worst 1.01e-05); the window is *load-bearing* (window 1.76e-03 vs all-points 2.11e-01) |
| Fail-closed upper bound | the numeric crossover is never *later* than `d*` at any N — depletion only helps the crew, and at small N it clears strictly early, so the closed form is conservative, never optimistic |
| Exact equations | one day re-derived independently from the documented map, bit-for-bit equal |
| Containment & conservation | S+I+P+R == N on every day; release drains the immune pool at exactly `r` hosts/day into susceptibles (S+P conserved) |
| The gate is load-bearing | an early stand-down lets a hidden pool of 40 regrow past 1.2·h (peak 4397); an un-clearable pool holds stand-down *and* release |
| Determinism | identical arguments ⇒ byte-identical trajectories; a 1% perturbed detection limit is detected |
| Worst-case sweep | the envelope contains every grid corner and the AT RISK corners are counted exactly |

The at-risk detection is deliberately the **first-step sign** (`I₁ ≥ h`), not a
window-crossing search: an at-risk pool grows exponentially, explodes past `N`,
and "burns out" over a long window, faking a late crossing — a window-crossing
oracle misreads exactly the pools the planner must refuse. The comparison
command reports `d* numeric : never (the model grows on day 1 -- confirmed,
not burned out)` in that case.

## 4. Audit trail

`{seq, ts, tool, version, command, params, metrics, prev, hash}` with
`hash = sha256(canonical(record minus hash/prev) + "|" + prev)`; record 0 is
anchored to a genesis sentinel. `verify` checks seq contiguity, prev linkage,
and content hash *independently* — all problems are reported, not just the
first. Editing a number, deleting the oldest record, and inserting a forged
record are all caught and named, and a store that fails verification refuses
new runs.

Honest limitation: trimming the tail of the file cannot be detected from inside
the file. External anchoring (log shipper, SIEM, append-only media) is the
documented answer if that matters.

## 5. Defects found and fixed during bring-up

| Defect | Symptom | Fix |
|---|---|---|
| tuple-index bugs in the lifecycle reader | `states[0][1]` indexed the wrong tuple element and the release line read `[2]` instead of `[1][2]`, so days and release anchors were off | index the day-state tuples correctly (`[1][1]` for I, `[1][2]` for the immune pool) |
| eradication day-0 overwritten by the day loop | the contained lifecycle recorded an eradication day from the first in-window day instead of day 0 when `I₀ ≤ h` | `if erad is None` guard so a day-0 eradication sticks |
| sweep corner params missing `release` | the corner lifecycle crashed with a KeyError | corner dicts carry the full param set including `release` |
| window-crossing oracle misreads AT RISK pools | an at-risk pool explodes past N and burns out over a long window, faking a late crossing and a "cleared" verdict exactly where the planner must refuse | at-risk is decided by the first-step sign (`I₁ ≥ h`); `compare` reports "the model grows on day 1 — confirmed, not burned out" |
| sweep envelope gate not checking at-risk corners | a corner with `d* = None` could slip past the numeric-envelope agreement | the gate requires `risk and d_iter is None` (compare does the same); at-risk corners are counted and reported |
| a vacuous hidden-pool check | one selftest check could not fail, so the sweep harness would swallow a broken numeric oracle | replaced with `map_match` + a small-N depletion check + a "closed form is the fail-closed upper bound" check |
| numeric oracle used the wrong one-day convention | `hidden_pool_analysis` documents `d* numeric = 0 when h ≤ c` but computed day 1, so the *default* `sweep` (h ≤ c at every corner) refused its own envelope with "disagrees with the numeric model on 4/4 corners" | seed `crossed = 0` when `h ≤ c` — the numeric crossing then agrees with the closed-form `d* = 0` at every corner |
| selftest crashed on non-contained trajectories | containment mutants (patched hosts never leave the infectious pool) made the pool *grow*; `next(d for d, st in states if st[1] <= c)` raised StopIteration and `simulate_release(..., None, ...)` raised TypeError — the suite exited non-zero with *no named check*, so the mutation sweep correctly flagged those mutants as survivors, not kills | the crossing lookups are None-tolerant and the release-trace check is gated on a stand-down day existing; a non-contained trajectory now reports a *named* failing check |
| ambiguous mutation anchors | `g = growth_factor(beta, gamma)` appears in both `min_capacity` and `hidden_pool_analysis`, and the containment-window line exists in both `map_match` and `hidden_pool_analysis` | every ambiguous anchor carries the following source line, so each target matches exactly one site (a PATTERN-AMBIGUOUS row fails the sweep) |

## 6. Verification — what the green is, and that it can fail

| Gate | Result |
|---|---|
| `ci/import_gate.py` | 0 non-stdlib imports across the repo |
| `selftest` | 26/26 checks |
| `tests/test_clear_lab.py` | 31/31 tests, real subprocess runs of the CLI |
| `ci/prove_suites_can_fail.py` | recovery-drift mutation and injected-check mutation both turn the suites red *as named checks*; restore is green again |
| `ci/mutation.py` | 11 real mutants killed (recovery drift, patched-stay-infectious, never-touches-infectious, capacity halved, c_min flattened, window disabled, worst-d* frozen, at-risk suppressed, genesis anchor removed, garbage hash, zero β accepted), semantically-identical canary survives, live file hash unchanged (scratch-copy only) |
| CI matrix | Python 3.9 / 3.11 / 3.13 × linux / macos / windows (5 cells), count floors: 26 selftest checks, 31 e2e tests, import gate and mutation sweep gated |

The suite is not trusted on a green run alone: `prove_suites_can_fail` breaks
the code on purpose and requires the failures to be *named checks*, not
crashes — the lesson of §5's selftest-crash defect, which the mutation sweep
caught before CI ever could — and the mutation sweep requires the canary to
survive, otherwise every verdict in the run is void.

## 7. Reproduce

```powershell
git clone https://github.com/happymode-25/clear-lab   # private; needs access
cd clear-lab
python ci/import_gate.py
python clear-lab.py selftest
python tests/test_clear_lab.py
python ci/prove_suites_can_fail.py
python ci/mutation.py
```

CI runs the same gates on every push; the mutation sweep runs on one cell
(ubuntu + 3.11) because it is the slowest gate.

## 8. Relationship to the previous builds

The family is now a quartet, one discipline across four claims:
[egress-broker](https://github.com/happymode-25/egress-broker) proves egress
decisions and audit rows are trustworthy (prevent), [worm-lab](https://github.com/happymode-25/worm-lab)
proves propagation numbers (model), [patch-lab](https://github.com/happymode-25/patch-lab)
proves the containment answer — *this many hosts/day holds the line* — and
clear-lab proves the question patch-lab's answer sets up: *once held, when is
it provably safe to stop patching, and when may immune hosts return?*

The patterns carried over deliberately — and the one new lesson bought this
session: a pure selftest plus a subprocess e2e suite, count floors in CI, an
import gate, a prove-suites-can-fail harness, a canary-anchored mutation sweep
on a scratch copy with the live file hashed before and after, a
`.gitattributes` with `eol=lf` from the first commit, a README that states
guarantees *and* their limits — and the discovery that a selftest which
*crashes* on a pathological model is hiding the failure: the mutation sweep
survived nothing, it instead found tests that died without naming a check, and
the fix was to make every check fail as a named assertion.
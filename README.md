# clear-lab

An eradication &amp; safe stand-down planner for a contained worm outbreak, built to
the same bar as [egress-broker](https://github.com/happymode-25/egress-broker),
[worm-lab](https://github.com/happymode-25/worm-lab), and
[patch-lab](https://github.com/happymode-25/patch-lab):

- **One file, zero dependencies.** `clear-lab.py` runs on Python 3.9+ with the
  standard library alone. The daily model, the stand-down theorem, the sweep —
  no numpy, no scipy, nothing to pip-install.
- **Fail-closed by default.** Invalid input is refused with an exit code and a
  reason; an un-clearable hidden pool holds stand-down *and* release; `sweep`
  refuses to publish an envelope it cannot verify; the audit store refuses to
  accept a run it cannot evidence.
- **Tamper-evident audit trail.** Every run is appended to a hash-chained JSONL
  store. `verify` rebuilds the chain and names exactly what is wrong if
  anything was edited, deleted, or forged.
- **Provably testable.** A 26-check selftest, a 31-test end-to-end suite, a
  `prove_suites_can_fail` harness, and an 11-mutant + canary mutation sweep —
  all enforced in CI with count floors.

## What it does

[patch-lab](https://github.com/happymode-25/patch-lab) answers *how much
capacity holds the worm at today's level*. clear-lab answers the question that
comes after: **once the outbreak is contained, when is it actually safe to stop
patching?**

The catch is detection. &quot;I = 0&quot; in the model really means &quot;below the detection
limit h&quot;: a hidden pool of up to h infectious hosts remains, and if the crew
stands down while any of them is alive, the worm regrows at
`G = (1+β)(1−γ)` per day with nobody left to clear it.

The stand-down theorem (closed form):

```
I(d+1) = G·(I(d) − c)            worst case starts at the bound h
fixed point = c·G/(G−1)          which is ABOVE c
→ the pool only contracts when   h < c·G/(G−1)
d* = first d with I_d ≤ c        extra patching days needed
stand_down = E + d* + 2          E = first day observed I ≤ h
```

If `h ≥ c·G/(G−1)` the pool **never** contracts — capacity cannot win, standing
down is never safe, and immune hosts stay locked (AT RISK). If `G ≤ 1` the worm
dies alone and `d* = 0` is always safe. Unless the crew keeps patching through
`E + d* + 1`, the pool regrows: the gate is load-bearing, and the selftest
proves it (a hidden pool of 40 under an early stand-down peaks at 4397).

The closed form is not believed on faith: `compare` verifies it three
independent ways — the analytic formula, a straight recurrence iteration, and a
huge-N daily model — and `sweep` verifies its own worst-case envelope
numerically before it prints a single number.

## When to use it

- As an **eradication planner**: given `β`, `γ`, `I₀`, `N`, `c`, and the
  detection limit `h`, when does the observed outbreak fall to the limit, how
  long must the crew keep patching the invisible pool after that, and on which
  day is it provably safe to stand down?
- As a **release gate**: immune hosts return to duty *only* after stand-down,
  at `r` hosts/day — held when the hidden pool is AT RISK or the worm was never
  contained.
- As a **fail-closed estimator** when the parameters are *not* known: `sweep`
  runs the whole β/γ box, reports the worst stand-down day and the worst `d*`,
  counts the corners that can never be cleared (AT RISK), and refuses to print
  the envelope unless every corner verifies against the model.
- As an **auditable record**: every run lands in the hash-chained store, so the
  stand-down commitment can be proved later to be the stand-down commitment.

## Quick start

```powershell
python clear-lab.py selftest                 # 26 checks, proves the theorem + gate

python clear-lab.py plan --beta 0.3 --gamma 0.1 --i0 100   # plans at 1.2x c_min
python clear-lab.py plan --c 20 --detect 40                # d* = 2 extra days

python clear-lab.py compare                  # formula vs recurrence vs daily model
python clear-lab.py compare --fail-over      # CI-style gate: exit 1 out of tol

python clear-lab.py sweep --beta-min 0.2 --beta-max 0.4 `
                        --gamma-min 0.05 --gamma-max 0.15 --grid 3

python clear-lab.py stats                    # recent runs from the audit store
python clear-lab.py verify                   # prove the chain is untampered
```

The audit store lives at `~/.clearlab/audit.jsonl`, or wherever
`CLEAR_LAB_STORE` points, or where `--store` says.

## Commands

| Command | What it gives you |
|---|---|
| `selftest` | 26 checks: the d* closed form against a straight recurrence and a huge-N model, the clearable boundary in both directions, the containment map, exact equations, conservation, release gating, the AT RISK gate, determinism, sweep envelope, audit tamper detection |
| `plan [--c X] [--detect h] [--release r]` | Minimum capacity `c_min`, a run at your `c` (or 1.2× `c_min`): eradication day E, d*, stand-down day `E + d* + 2`, verdict, release schedule |
| `compare [--fail-over]` | The d* anchor three ways (formula / recurrence / numeric), the clearable boundary probed on both sides, and the hidden-pool map matched inside the linear window; `--fail-over` gates on tolerance |
| `sweep` | Worst stand-down day and worst `d*` over an uncertain β/γ box, with AT RISK corners counted and the envelope verified numerically |
| `stats` | Recent runs from the audit store |
| `verify` | Rebuild the hash chain; exit 1 and name the tamper if anything is wrong |

Verdict precedence, applied in this order — **NOT CONTAINED** (peak exceeds
1.000001·I₀) &gt; **AT RISK** (hidden pool un-clearable, or eradication never
within the window) &gt; **STAND DOWN SAFE on day T**. Release is gated on
stand-down and held otherwise. Everything is deterministic, and `--version` is
available everywhere.

## Fail-closed behaviour

- `i0 > N`, `β ≤ 0`, `γ ≤ 0`, `days ≤ 0`, `c < 0`, `detect < 0`, `release < 0`,
  unknown commands → exit 1/2 with a reason, no partial run, no clamped
  silence. (`c = 0`, `detect = 0`, `release = 0` are *valid*: no patching,
  no detection margin, and no redeployment are legitimate scenarios.)
- `sweep` computes the worst `d*` and stand-down day from the closed form, then
  verifies numerically that the envelope holds for *every* corner of the grid —
  including that each AT RISK corner really is un-clearable; if the envelope
  disagrees, sweep exits 1 rather than print a bound it cannot back.
- If the audit chain does not verify *immediately after a write*, the tool
  refuses to report the run rather than record one it cannot evidence.

## Audit trail

Each record is `{seq, ts, tool, version, command, params, metrics, prev, hash}`
where `hash = sha256(canonical(record minus hash/prev) + "|" + prev)`. Record 0
must carry the genesis anchor. `verify` checks three independent things per
record — seq contiguity, prev linkage, and content hash — so it reports *all*
problems, not just the first. Editing a number, deleting the oldest record,
and inserting a forged record are all caught and named.

What it cannot do, stated honestly: trimming the *tail* of the file is not
detectable from inside the file. If that matters, anchor the last hash
externally (log shipper, SIEM, append-only media).

## Verification

| Gate | What it proves | Floor |
|---|---|---|
| `ci/import_gate.py` | zero third-party imports anywhere | — |
| `selftest` | stand-down theorem, boundary, containment map, release gate, audit, determinism | 26 checks |
| `tests/test_clear_lab.py` | real CLI behaviour over subprocesses | 31 tests |
| `ci/prove_suites_can_fail.py` | the suites fail when the code breaks and pass when restored | — |
| `ci/mutation.py` | 11 real mutants killed, semantically-identical canary survives | 12 rows |

Run them locally from the repo root:

```powershell
python ci/import_gate.py
python clear-lab.py selftest
python tests/test_clear_lab.py
python ci/prove_suites_can_fail.py
python ci/mutation.py
```

The mutation sweep works on a scratch copy — the live `clear-lab.py` is hashed
before and after, so a sweep can never leave a mutant behind in the repo.
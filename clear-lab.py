#!/usr/bin/env python3
"""clear-lab -- eradication & safe stand-down planner for a contained outbreak.

patch-lab tells the crew the minimum daily capacity c_min that holds a worm at
its current level. clear-lab answers the question that comes after: once the
outbreak is contained, when is it actually SAFE TO STOP PATCHING?

The catch is detection. "I = 0" in the model really means "below the detection
limit h": a hidden pool of up to h infectious hosts remains, and if the crew
stands down while any of them is alive, the worm regrows at G = (1+beta)(1-gamma)
per day with nobody left to clear it.

The stand-down theorem (closed form):
  while the crew keeps patching at capacity c, the sub-detection pool follows
      I(d+1) = G * (I(d) - c)          (worst case starts at the bound h)
  whose fixed point is cG/(G-1) -- ABOVE c.  So the pool only contracts when
      h < cG/(G-1)                      (patch-lab's condition with I0 -> h)
  and the number of extra patching days needed to drive the bound down to <= c
  is the first d with I_d <= c:
      d* = ceil( log_G( c / (cG - h(G-1)) ) )
  then one more patching day clears the pool entirely.  The crew may stand down
  from day  (E + d* + 2)  where E is the first day the observed I falls to the
  detection limit.  If h >= cG/(G-1) the pool NEVER contracts -- capacity cannot
  win, standing down is never safe, and immune hosts stay locked (AT RISK).

Immune hosts return to duty only after stand-down (release is gated on the
hidden pool being provably dead), at a deployment rate r hosts/day.

Everything is deterministic, verified against the model itself, and appended to
the same hash-chained audit discipline as the rest of the family:

  * the closed-form d* is compared with a straight recurrence iteration,
  * the linear hidden-pool map is compared with the daily model (windowed, so
    the O(beta*(1-S/N)) error at finite N cannot hide behind the closed form),
  * the stand-down day is proven in the model: a worst-case hidden pool of
    exactly h hosts run from day E is 0 at day E + d* + 1,
  * early stand-down rekindles the worm -- the gate is load-bearing,
  * a parameter sweep reports the WORST stand-down day and flags every corner
    of the beta/gamma box that is AT RISK, verified numerically before the
    envelope is published.

Single file, Python 3.9+, standard library only.

Usage:
  python clear-lab.py selftest
  python clear-lab.py plan   --beta 0.3 --gamma 0.1 --i0 100 --N 1000000
                             [--c 18] [--detect 10] [--release 100]
  python clear-lab.py compare [--fail-over] [--keep-frac 0.99] [--tol 1e-3]
  python clear-lab.py sweep  --beta-min 0.2 --beta-max 0.4
                             --gamma-min 0.05 --gamma-max 0.15
                             [--grid 5] [--detect 10] [--c ...]
  python clear-lab.py stats [--json]
  python clear-lab.py verify [--store PATH]

Audit store: env CLEAR_LAB_STORE, else ~/.clearlab/audit.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
import time

__version__ = "1.0.0"

# The genesis anchor every chain must start from. If record 0 does not carry
# exactly this prev, the oldest record was deleted.
GENESIS = "0" * 64

# Fail-closed default behaviour for validation errors: every bad input exits 1
# with a reason on stderr. Nothing silently degrades.
_EPILOG = "clear-lab: "

# Everything below this infectious count counts as extinct; a fraction of a
# host below 1e-9 cannot rekindle anything and cannot be reported honestly.
_ZERO = 1e-9

# Report anchors the e2e suite parses. Keep the labels stable: tests slice on
# them and CI floors depend on the counts.
#
#   min capacity  : 14.53 hosts/day...
#   eradication   : day 7 (observed I falls to the detection limit)
#   stand-down    : day 13 (d* = 5 extra patching days...) | held (...)
#   verdict       : STAND DOWN SAFE on day 13 | AT RISK -- ... | NOT CONTAINED
#   release       : immune hosts return to duty from day 13 at 100 hosts/day
#   peak I        : ...
#   (compare) d* formula/numeric, boundary, verdict within tolerance
#   (sweep)   worst stand-down / worst d* / at risk / envelope


# ---------------------------------------------------------------------------
# The daily containment map (shared with patch-lab's discipline)
# ---------------------------------------------------------------------------

def step(state, p):
    """Advance the SIR+patching model by exactly one day.

    State layout: (S, I, P, R) -- P is the patched (immune) pool.
    Deterministic order, documented and asserted by the selftest:
      1. patch  (up to c hosts/day, infectious hosts first),
      2. spread (each infectious host infects beta susceptibles/day),
      3. recover (each infectious host recovers at gamma/day).
    """
    s, i, p_pool, r = state
    n = p["N"]
    # 1. patch
    k = min(p["c"], s + i)
    k_i = min(k, i)
    k_s = k - k_i
    i -= k_i
    s -= k_s
    p_pool += k_i + k_s
    # 2. spread
    new = p["beta"] * s * i / n
    s -= new
    i += new
    # 3. recover
    rec = p["gamma"] * i
    i -= rec
    r += rec
    return [s, i, p_pool, r]


def make_run(params):
    """Run the daily map to the horizon (or to extinction).

    Returns (states, extinct_day). states is [(day, (S, I, P, R)), ...]
    starting at day 0; extinct_day is the first day I <= 1e-9, else None.
    """
    n = float(params["N"])
    i = float(params["i0"])
    state = [n - i, i, 0.0, 0.0]
    states = [(0, tuple(state))]
    extinct = None
    for day in range(1, int(params["days"]) + 1):
        state = step(state, params)
        states.append((day, tuple(state)))
        if state[1] <= _ZERO:
            extinct = day
            break
    return states, extinct


def make_run_full(params):
    """Run the daily map for the full window WITHOUT stopping at extinction.

    The release trace needs the patched-pool size on a specific later day, so
    it cannot rely on the early break. Identical dynamics otherwise.
    """
    n = float(params["N"])
    i = float(params["i0"])
    state = [n - i, i, 0.0, 0.0]
    states = [(0, tuple(state))]
    extinct = None
    for day in range(1, int(params["days"]) + 1):
        state = step(state, params)
        states.append((day, tuple(state)))
        if extinct is None and state[1] <= _ZERO:
            extinct = day
    return states, extinct


def metrics(states, extinct, params):
    """Summarise a trajectory. Verdict: CONTAINED if I never exceeds I0."""
    peak = max(st[1] for _, st in states)
    peak_day = next(d for d, st in states if st[1] == peak)
    final = states[-1][1]
    n = sum(final)
    return {
        "peak": peak,
        "peak_day": peak_day,
        "extinct_day": extinct,
        "final_i": final[1],
        "final_r": final[3],
        "attack_ratio": round(final[3] / n, 6),
        "patched_ratio": round(final[2] / n, 6),
        "verdict": "CONTAINED" if peak <= params["i0"] * (1 + 1e-6) else "ESCAPED",
    }


# ---------------------------------------------------------------------------
# The closed-form stand-down anchor
# ---------------------------------------------------------------------------

def growth_factor(beta, gamma):
    """The one-day growth factor in the linear regime.

    One infectious host patches out c/day; while S ~= N the map is
    I(d+1) = G * (I(d) - c) with G = (1+beta)(1-gamma).
    """
    return (1.0 + beta) * (1.0 - gamma)


def min_capacity(beta, gamma, i0):
    """Minimum daily capacity that holds the outbreak at its current level.

    (patch-lab's anchor, retained: containment is the precondition for any
    stand-down question.)  c_min = I0*(G-1)/G; below G <= 1 the worm dies on
    its own: c_min = 0.
    """
    g = growth_factor(beta, gamma)
    if g <= 1.0:
        return 0.0
    return i0 * (g - 1.0) / g


def hidden_threshold(c, g):
    """The largest hidden-pool bound a capacity c can ever clear.

    I(d+1) = G*(I(d)-c) has its fixed point at c*G/(G-1), which lies ABOVE c:
    a start below it contracts through c and dies; a start at or above it sits
    at or above the fixed point forever.  Below G <= 1 the worm dies on its
    own and any bound is clearable (threshold is unbounded).
    """
    if g <= 1.0:
        return float("inf")
    return c * g / (g - 1.0)


def hidden_pool_formula(d, h, c, g):
    """The sub-detection pool after d further patching days, worst case I0 = h.

    Closed form of I(d+1) = G*(I(d)-c):
        I_d = G^d * h - c*G*(G^d - 1)/(G-1)
    Valid while the pool stays above c (the crew is fully spent); the day the
    formula returns <= c the next patch day clears the entire pool.
    """
    if abs(g - 1.0) < 1e-12:
        return h - d * c
    gd = g ** d
    return gd * h - c * g * (gd - 1.0) / (g - 1.0)


def stand_down_days(h, c, g):
    """d*: extra patching days to drive the hidden bound h down to <= c.

    Returns None when the pool can never contract (h >= cG/(G-1), AT RISK).
    Evaluated by searching the analytic solution (exact ceil, immune to float
    noise at integer boundaries).  h <= c clears on the very next patch day,
    so d* = 0.
    """
    if g <= 1.0:
        return 0
    if h >= hidden_threshold(c, g):
        return None
    if h <= c:
        return 0
    d = 1
    while hidden_pool_formula(d, h, c, g) > c and d < 10 ** 6:
        d += 1
    return d if d < 10 ** 6 else None


def iterate_d_star(h, c, g, cap=10 ** 6):
    """d* by straight recurrence iteration -- the independent second path.

    Simulates I(d+1) = G*(I(d)-c) day by day and counts to the first state at
    or below c.  Must agree with stand_down_days on every safe (h, c, g).
    Returns None if the cap is exhausted without a crossing.
    """
    i = h
    for d in range(cap + 1):
        if i <= c:
            return d
        i = g * (i - c)
    return None


# ---------------------------------------------------------------------------
# The lifecycle plan: eradication -> stand-down -> release
# ---------------------------------------------------------------------------

def plan_lifecycle(params):
    """Derive the full eradicate / stand-down / release plan for one scenario.

    params: beta, gamma, i0, N, days, c, detect (h >= 0), release (r >= 0).
    Returns an info dict (all values deterministic):
      contained, g, c_min, threshold, at_risk_pool, d_star,
      erad_day (0 if the initial load is already below detection, else the
                first day observed I <= h, else None),
      stand_down_day (None unless STAND DOWN SAFE),
      verdict: "NOT CONTAINED" | "STAND DOWN SAFE on day T" | "AT RISK -- why"
      reason, release_start, release_complete,
      peak, peak_day, extinct_day, attack_ratio, patched_ratio
    """
    ii = float(params["i0"])
    h = float(params["detect"])
    c = float(params["c"])
    g = growth_factor(params["beta"], params["gamma"])
    c_min = min_capacity(params["beta"], params["gamma"], ii)
    threshold = hidden_threshold(c, g)
    at_risk_pool = g > 1.0 and h >= threshold
    d_star = None if at_risk_pool else stand_down_days(h, c, g)

    states, extinct = make_run_full(params)
    peak = max(st[1] for _, st in states)
    peak_day = next(d for d, st in states if st[1] == peak)
    contained = peak <= ii * (1 + 1e-6)

    # Eradication: the first day (day 0 included) the OBSERVED I is at or
    # below the detection limit.  A contained run always reaches it.
    erad = 0 if states[0][1][1] <= h else None
    for day, st in states[1:]:
        if erad is None and st[1] <= h:
            erad = day
            break

    stand_down = None
    if not contained:
        verdict = "NOT CONTAINED"
        reason = f"c={c:.2f} < c_min={c_min:.2f}; the worm is not held at I0"
    elif erad is None:
        verdict = "AT RISK"
        reason = "eradication not observed within the window"
    elif at_risk_pool:
        verdict = "AT RISK"
        reason = (f"capacity c={c:.2f} cannot clear a hidden-pool bound "
                  f"h={h:g} >= cG/(G-1)={threshold:.2f}")
    else:
        stand_down = erad + (d_star or 0) + 2
        verdict = f"STAND DOWN SAFE on day {stand_down}"
        reason = None

    # Release: immune hosts return to duty only from the stand-down day
    # (the hidden pool is provably zero by stand_down-1).  Drain the patched
    # pool at r/day and report when the redeployment completes.
    release_start = None
    release_complete = None
    if verdict.startswith("STAND DOWN SAFE") and params["release"] > 0:
        release_start = stand_down
        p_avail = float(states[stand_down - 1][1][2])
        rel_days = math.ceil(p_avail / params["release"]) if p_avail > 0 else 0
        if rel_days and stand_down + rel_days - 1 <= int(params["days"]):
            release_complete = int(stand_down + rel_days - 1)

    final = states[-1][1]
    total = sum(final)
    info = {
        "contained": contained,
        "g": g,
        "c_min": c_min,
        "threshold": threshold,
        "at_risk_pool": at_risk_pool,
        "d_star": d_star,
        "erad_day": erad,
        "stand_down_day": stand_down,
        "verdict": verdict,
        "reason": reason,
        "release_start": release_start,
        "release_complete": release_complete,
        "peak": peak,
        "peak_day": peak_day,
        "extinct_day": extinct,
        "attack_ratio": round(final[3] / total, 6),
        "patched_ratio": round(final[2] / total, 6),
    }
    return info


def simulate_release(states, stand_down_day, release, days):
    """Day-by-day release trace: immune pool P -> susceptibles S at r/day.

    Starts from the state at the end of the last patching day (stand_down-1).
    Returns [(day, S, P)] from stand_down_day onward.  The daily step does no
    patching here (crew stood down) and I is already 0 in a clearable plan, so
    S/P evolve by release only -- asserted exactly by the selftest.
    """
    s, i, p_pool, r = states[stand_down_day - 1][1]
    out = []
    for day in range(stand_down_day, int(days) + 1):
        take = min(float(release), p_pool)
        p_pool -= take
        s += take
        out.append((day, s, p_pool))
        if p_pool <= _ZERO:
            break
    return out


# ---------------------------------------------------------------------------
# Linear-window comparison of the daily map against G*(I-c): the daily map
# carries an O(beta*(1-S/N)) error at finite N, so only days where the crew is
# fully spent (I > c) and susceptibles are still ~= N are compared.  Shared
# with the containment map (patch-lab discipline); the load-bearing selftest
# check proves the window is what makes the anchor honest.
# ---------------------------------------------------------------------------

def map_match(params, states, keep_frac=0.99):
    """Compare the daily map against the closed-form map G*(I-c).

    Returns (worst_rel_error, points).  Days where I <= c (the crew is not
    fully spent on infectious hosts) or S has fallen below keep_frac*N are
    excluded: the closed form assumes S ~= N and c < I.
    """
    g = growth_factor(params["beta"], params["gamma"])
    c = params["c"]
    n = params["N"]
    worst = 0.0
    points = 0
    prev_i = None
    for day, (s, i, _p, _r) in states:
        if day == 0:
            prev_i = i
            continue
        if i > c and s >= keep_frac * n and prev_i > c:
            expected = g * (prev_i - c)
            if expected > 0 and i > 0:
                worst = max(worst, abs(i - expected) / expected)
                points += 1
        prev_i = i
    return worst, points


# ---------------------------------------------------------------------------
# The numeric hidden-pool oracle (independent of the closed forms)
# ---------------------------------------------------------------------------

def hidden_pool_analysis(h, c, beta, gamma, days, n=1_000_000_000,
                         keep_frac=0.99):
    """Run the model starting at exactly I0 = h with capacity c.

    Returns (map_err, points, d_star_numeric, clear_day_numeric, at_risk_numeric)
      map_err          worst relative deviation of I from the closed form
                       inside the linear window (states above c, S >= keep_frac*N),
      points           how many days the window had,
      d_star_numeric   first day I <= c (0 when h <= c), None if never,
      clear_day_numeric first day I <= 1e-9 (== d_star + 1 for a clearable
                       pool), None if never,
      at_risk_numeric  first-step oracle: I grows (or holds) on day 1, so the
                       pool starts at or above the fixed point cG/(G-1) and
                       can never descend through c.
    The first-step oracle is burn-out proof: an at-risk pool grows
    exponentially, and over a long window it eventually explodes past N and
    'burns out' -- a phantom late crossing that is not a clearing.  Waiting
    for a crossing would misread exactly the pools the planner must refuse.
    """
    g = growth_factor(beta, gamma)
    states, _ = make_run({"N": n, "i0": h, "days": days,
                          "beta": beta, "gamma": gamma, "c": c})
    worst = 0.0
    points = 0
    prev_i = h
    # d* counts EXTRA patching days: a bound already at or below capacity
    # clears on the very next patch day, so the crossing is day 0 (d* = 0),
    # not day 1.  Seeded here so the numeric oracle agrees with the closed
    # form -- the default sweep (h <= c at every corner) must not refuse
    # its own envelope over a one-day convention.
    crossed = 0 if h <= c else None
    cleared = None
    for day, (s, i, _p, _r) in states:
        if day == 0:
            continue
        if i > c and s >= keep_frac * n and prev_i > c:
            expected = hidden_pool_formula(day, h, c, g)
            if expected > 0 and i > 0:
                worst = max(worst, abs(i - expected) / expected)
                points += 1
        if crossed is None and i <= c:
            crossed = day
        if cleared is None and i <= _ZERO:
            cleared = day
        prev_i = i
    # At-risk is decided by the FIRST step, not by waiting for a crossing.
    # The linear map I' = G*(I-c) has its fixed point at cG/(G-1): a start
    # above it GROWS exponentially (and, given enough days, explodes past N
    # and "burns out" -- a late phantom crossing that says nothing about the
    # truly clearable pools).  A start at or above the fixed point never
    # descends through c, so the one-step sign is the exact, burn-out-proof
    # oracle: I_1 >= h means at risk.
    first_i = states[1][1][1]
    at_risk = first_i >= h
    return worst, points, crossed, cleared, at_risk


# ---------------------------------------------------------------------------
# Validation (fail-closed): bad input exits 1 with the reason on stderr.
# ---------------------------------------------------------------------------

def positive(name, value):
    if value <= 0:
        fail(f"{name} must be > 0, got {value}")
    return value


def nonneg(name, value):
    if value < 0:
        fail(f"{name} must be >= 0, got {value}")
    return value


def validate_common(args):
    params = {
        "N": float(positive("N", args.N)),
        "i0": float(positive("i0", args.i0)),
        "days": float(positive("days", args.days)),
    }
    if params["i0"] > params["N"]:
        fail(f"i0 ({params['i0']}) cannot exceed N ({params['N']})")
    params["N"] = int(params["N"])
    params["days"] = int(params["days"])
    return params


def validate_sir(args, with_detection=True):
    p = validate_common(args)
    p["beta"] = float(positive("beta", args.beta))
    p["gamma"] = float(positive("gamma", args.gamma))
    if with_detection:
        p["detect"] = float(nonneg("detect", args.detect))
        if p["detect"] > p["N"]:
            fail(f"detect ({p['detect']}) cannot exceed N ({p['N']})")
    return p


def fail(msg):
    print(_EPILOG + msg, file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Audit trail: append-only JSONL, hash chained, tamper-evident.
#   record = {seq, ts, tool, version, command, params, metrics, prev, hash}
#   hash   = sha256(canonical(record minus hash/prev) + "|" + prev)
#   record 0 must carry prev == GENESIS; every later record's prev must equal
#   the previous record's hash; seq must equal the line index.
# ---------------------------------------------------------------------------

def default_store():
    env = os.environ.get("CLEAR_LAB_STORE")
    if env:
        return env
    return os.path.join(os.path.expanduser("~"), ".clearlab", "audit.jsonl")


def _canonical(record):
    clean = {k: v for k, v in record.items() if k not in ("hash", "prev")}
    return json.dumps(clean, sort_keys=True, separators=(",", ":"))


def _record_hash(record):
    digest = hashlib.sha256()
    digest.update(_canonical(record).encode("utf-8"))
    digest.update(b"|")
    digest.update(record["prev"].encode("utf-8"))
    return digest.hexdigest()


def read_store(path):
    """Parse the audit file. Returns (records, error_message)."""
    if not os.path.exists(path):
        return [], None
    records = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError as exc:
                    return None, f"{path}:{lineno}: unparseable JSON ({exc})"
                if not isinstance(rec, dict):
                    return None, f"{path}:{lineno}: not an object"
                records.append(rec)
    except OSError as exc:
        return None, f"{path}: cannot read ({exc})"
    return records, None


def verify_store(path):
    """Rebuild the chain. Returns (ok, problems, n)."""
    records, err = read_store(path)
    if err:
        return False, [err], 0
    problems = []
    for i, rec in enumerate(records):
        if rec.get("seq") != i:
            problems.append(
                f"record {i}: seq is {rec.get('seq')!r}, expected {i} "
                "(an insert or a deletion shifted the file)")
        if i == 0:
            if rec.get("prev") != GENESIS:
                problems.append(
                    f"record 0: prev is {rec.get('prev', '')[:16]!r}, "
                    "expected the genesis anchor (the oldest record was deleted)")
        else:
            if rec.get("prev") != records[i - 1].get("hash"):
                problems.append(
                    f"record {i}: prev does not match the previous record's hash "
                    "(the previous record was edited)")
        expect = _record_hash(rec)
        if rec.get("hash") != expect:
            problems.append(f"record {i}: hash does not match its contents (edited)")
    return (len(problems) == 0), problems, len(records)


def append_record(store, command, params, metrics):
    """Append one chained record; fail closed if the write cannot be trusted."""
    records, err = read_store(store)
    if err:
        fail(f"cannot append to store {store}: {err}")
    prev = records[-1]["hash"] if records else GENESIS
    seq = len(records)
    rec = {
        "seq": seq,
        "ts": round(time.time(), 3),
        "tool": "clear-lab",
        "version": __version__,
        "command": command,
        "params": params,
        "metrics": metrics,
        "prev": prev,
        "hash": None,
    }
    rec["hash"] = _record_hash(rec)
    line = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    directory = os.path.dirname(store)
    if directory:
        os.makedirs(directory, exist_ok=True)
    # Fail-closed write: append, flush, fsync, then read the line back and
    # confirm the chain is intact before reporting success.
    try:
        with open(store, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        fail(f"cannot write audit store {store}: {exc}")
    ok, problems, _ = verify_store(store)
    if not ok:
        fail(f"audit store does not verify right after writing ({problems[0]}); "
             "refusing to report a run that cannot be evidenced")
    return rec


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def fmt_hosts(x):
    return f"{x:,.0f}"


def describe_d_star(d_star, h, c, g):
    if d_star == 0:
        return (f"d* = 0: the bound h={h:g} is already at or below the "
                f"capacity c={c:.2f}, one further patching day clears it")
    return (f"d* = {d_star} extra patching days drive the bound h={h:g} "
            f"down to c={c:.2f}; the day after that clears it")


def report_plan(params, info, store, seq):
    r0 = params["beta"] / params["gamma"]
    h = params["detect"]
    print(
        f"clear-lab {__version__} -- eradicate & stand-down plan, "
        f"N={fmt_hosts(params['N'])}, I0={fmt_hosts(params['i0'])}, "
        f"beta={params['beta']:.2f}/day, gamma={params['gamma']:.2f}/day, "
        f"R0={r0:.2f}",
    )
    print(f"  growth factor : G = {info['g']:.3f} per day (closed form I(d+1) = G*(I-c))")
    print(f"  min capacity  : {info['c_min']:.2f} hosts/day to hold the line at I0")
    print(
        f"  c deployed    : {params['c']:.2f} hosts/day "
        f"({params['c'] / info['c_min']:.2f}x min)"
        if info["c_min"] > 0 else
        f"  c deployed    : {params['c']:.2f} hosts/day (no capacity needed)")
    print(
        f"  detection     : I <= {h:g} hosts is below the detection limit "
        f"(hidden-pool bound)")
    if info["erad_day"] is not None:
        if info["erad_day"] == 0:
            print("  eradication   : day 0 (initial load is already below detection)")
        else:
            print(f"  eradication   : day {info['erad_day']} "
                  "(observed I falls to the detection limit)")
    else:
        print("  eradication   : not within the window")
    if info["threshold"] == float("inf"):
        print("  pathology     : worm self-limits (G <= 1); any hidden bound dies "
              "on its own")
    elif info["at_risk_pool"]:
        print(f"  pathology     : hidden bound h={h:g} >= the clearable threshold "
              f"cG/(G-1)={info['threshold']:.2f} -- NOT clearable")
    else:
        print(f"  pathology     : hidden bound h={h:g} < the clearable threshold "
              f"cG/(G-1)={info['threshold']:.2f} -- clearable")
    if info["verdict"].startswith("STAND DOWN SAFE"):
        print(f"  stand-down    : day {info['stand_down_day']} "
              f"({describe_d_star(info['d_star'], h, params['c'], info['g'])})")
    else:
        print(f"  stand-down    : held ({info['reason']})")
    print(f"  verdict       : {info['verdict']}")
    if info["release_start"] is not None:
        print(f"  release       : immune hosts return to duty from day "
              f"{info['release_start']} at {params['release']:g} hosts/day")
        if info["release_complete"] is not None:
            print(f"  release done  : redeployment complete by day "
                  f"{info['release_complete']}")
        else:
            print("  release done  : not within the window")
    else:
        print("  release       : held until stand-down is possible")
    print(
        f"  peak I        : {fmt_hosts(info['peak'])} hosts at day "
        f"{info['peak_day']}")
    if info["extinct_day"] is not None:
        print(f"  extinction    : day {info['extinct_day']} (I reaches 0 in the model)")
    else:
        print("  extinction    : not within the window")
    print(
        f"  attack ratio  : {100 * info['attack_ratio']:.2f}% of hosts "
        f"recovered; {100 * info['patched_ratio']:.2f}% patched by the end")
    print(f"  recorded to audit store: {store} (seq {seq})")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_plan(args):
    p = validate_sir(args)
    c_min = min_capacity(p["beta"], p["gamma"], p["i0"])
    # Planning headroom when no explicit c is given: run the model at 1.2x the
    # minimum so the forecast is itself the recommended deployment.
    if args.c is None:
        p["c"] = round(c_min * 1.2, 6)
    else:
        p["c"] = float(nonneg("c", args.c))
    if args.release is None:
        p["release"] = 0.0
    else:
        p["release"] = float(nonneg("release", args.release))
    info = plan_lifecycle(p)
    logged = dict(p)
    logged["N"] = int(p["N"])
    logged["days"] = int(p["days"])
    rec = append_record(args.store, "plan", logged, {
        "c_min": round(info["c_min"], 6),
        "verdict": info["verdict"],
        "d_star": info["d_star"],
        "erad_day": info["erad_day"],
        "stand_down_day": info["stand_down_day"],
        "release_complete": info["release_complete"],
        "peak": round(info["peak"], 6),
        "attack_ratio": info["attack_ratio"],
    })
    report_plan(p, info, args.store, rec["seq"])


def cmd_compare(args):
    p = validate_sir(args, with_detection=True)
    if args.c is None:
        p["c"] = 25.0
    else:
        p["c"] = float(nonneg("c", args.c))
    h = p["detect"]
    g = growth_factor(p["beta"], p["gamma"])
    threshold = hidden_threshold(p["c"], g)
    d_formula = stand_down_days(h, p["c"], g)
    d_iter = iterate_d_star(h, p["c"], g)

    map_err, points, d_num, clear_num, _ = hidden_pool_analysis(
        h, p["c"], p["beta"], p["gamma"], p["days"], n=p["N"],
        keep_frac=args.keep_frac)

    # Boundary probes away from float noise: 0.99x (clearable) and 1.01x
    # (not clearable) of the threshold must agree between the closed form and
    # the numeric oracle.
    boundary_ok = True
    if threshold != float("inf"):
        lo = max(0.0, threshold * 0.99)
        hi = threshold * 1.01
        if stand_down_days(lo, p["c"], g) is None:
            boundary_ok = False
        if stand_down_days(hi, p["c"], g) is not None:
            boundary_ok = False
        _, _, _, _, lo_risk = hidden_pool_analysis(lo, p["c"], p["beta"],
                                                   p["gamma"], 500, n=p["N"])
        _, _, _, _, hi_risk = hidden_pool_analysis(hi, p["c"], p["beta"],
                                                   p["gamma"], 500, n=p["N"])
        if lo_risk or not hi_risk:
            boundary_ok = False
    map_err, points, d_num, clear_num, numeric_risk = hidden_pool_analysis(
        h, p["c"], p["beta"], p["gamma"], p["days"], n=p["N"],
        keep_frac=args.keep_frac)
    if d_formula is None:
        # At-risk: the closed form says d* does not exist; the numeric oracle
        # must agree that the pool grows, not fail an equality against a
        # phantom burn-out "crossing".
        d_star_ok = d_iter is None and numeric_risk
    else:
        d_star_ok = d_formula == d_iter == d_num
    hidden_ok = map_err <= args.tol and points >= 2
    in_tol = d_star_ok and hidden_ok and boundary_ok

    print(
        f"clear-lab {__version__} -- stand-down check: hidden-pool anchor vs "
        f"the daily map, N={fmt_hosts(p['N'])}, I0={fmt_hosts(p['i0'])}, "
        f"c={p['c']:.2f}, h={h:g}, {p['days']} days"
    )
    print(f"  growth factor : G = {g:.3f} per day")
    if points:
        print(
            f"  hidden map err: {map_err:.3e}   "
            f"({points} days inside the linear window)")
    else:
        print("  hidden map err: (no days inside the linear window; tighten keep-frac)")
    if d_formula is not None:
        print(f"  d* formula    : {d_formula} patching day(s) to drive h={h:g} "
              f"down to c={p['c']:.2f}")
        print(f"  d* iteration  : {d_iter}  (straight recurrence, must match)")
        print(f"  d* numeric    : {d_num if d_num is not None else 'never'}  "
              "(daily model from I0=h, must match)")
    else:
        print(f"  d* formula    : AT RISK -- h={h:g} >= cG/(G-1)={threshold:.2f}, "
              "the pool never contracts")
        print(f"  d* numeric    : never  (the model grows on day 1 -- "
              "confirmed, not burned out)")
    if threshold == float("inf"):
        print("  boundary      : G <= 1, the worm self-limits -- no threshold")
    else:
        print(f"  boundary      : clearable while h < {threshold:.2f}; "
              "h >= that is AT RISK (fail-closed)")
    print(f"  verdict       : {'within tolerance' if in_tol else 'OUT OF TOLERANCE'}")
    logged = dict(p)
    logged["N"] = int(p["N"])
    logged["days"] = int(p["days"])
    append_record(args.store, "compare", logged, {
        "hidden_err": round(map_err, 9), "points": points,
        "d_star_formula": d_formula, "d_star_numeric": d_num,
        "d_star_match": d_star_ok, "boundary_ok": boundary_ok,
        "threshold": round(threshold, 6) if threshold != float("inf") else None,
        "verdict": "ok" if in_tol else "fail",
    })
    if not in_tol and args.fail_over:
        fail(f"the stand-down anchor diverged from the model "
             f"(map {map_err:.3e}, d* {d_formula} vs {d_iter}/{d_num})")
    sys.exit(0)


def _linspace(lo, hi, count):
    if count == 1:
        return [float(lo)]
    return [lo + (hi - lo) * i / (count - 1) for i in range(count)]


def cmd_sweep(args):
    positive("beta-min", args.beta_min)
    positive("beta-max", args.beta_max)
    positive("gamma-min", args.gamma_min)
    positive("gamma-max", args.gamma_max)
    if args.beta_max < args.beta_min:
        fail("beta-max must be >= beta-min")
    if args.gamma_max < args.gamma_min:
        fail("gamma-max must be >= gamma-min")
    if args.grid < 2:
        fail("grid must be >= 2 (a box, not a point)")
    base = validate_common(args)
    base["detect"] = float(nonneg("detect", args.detect))
    if base["detect"] > base["N"]:
        fail(f"detect ({base['detect']}) cannot exceed N ({base['N']})")
    corners = []
    for b in _linspace(args.beta_min, args.beta_max, args.grid):
        for g in _linspace(args.gamma_min, args.gamma_max, args.grid):
            corners.append((b, g))
    # Default capacity mirrors the plan's headroom: 1.2x the WORST corner c_min,
    # so every corner is contained and the sweep asks only about stand-down.
    worst_cmin = max(min_capacity(b, g, base["i0"]) for b, g in corners)
    if args.c is None:
        c = round(worst_cmin * 1.2, 6)
    else:
        c = float(nonneg("c", args.c))

    worst_d = -1
    worst_ds = None
    worst_at_stand = None
    worst_at_corner = None
    at_risk_corners = []
    contained_at = 0
    for b, g in corners:
        p = dict(base, beta=b, gamma=g, c=c, release=0.0)
        info = plan_lifecycle(p)
        if info["contained"]:
            contained_at += 1
        if info["d_star"] is not None:
            if info["d_star"] > worst_d:
                worst_d, worst_ds = info["d_star"], (b, g)
            stand = info["stand_down_day"]
            if worst_at_stand is None or stand > worst_at_stand:
                worst_at_stand = stand
                worst_at_corner = (b, g)
        else:
            at_risk_corners.append((b, g, info["threshold"]))
    if contained_at != len(corners):
        fail("not every corner is contained at the sweep capacity -- the "
             "sweep would be planning a release on a live outbreak; refusing")
    # Fail-closed envelope: every corner's closed-form d*/verdict must agree
    # with the numeric hidden-pool oracle, or the envelope is not published.
    verified = 0
    for b, g in corners:
        d_formula = stand_down_days(base["detect"], c, growth_factor(b, g))
        d_iter = iterate_d_star(base["detect"], c, growth_factor(b, g))
        _, _, d_num, _, risk = hidden_pool_analysis(
            base["detect"], c, b, g, 500, n=1_000_000_000)
        formula_risk = d_formula is None
        if formula_risk:
            # At-risk corner: the numeric oracle must also say the pool grows
            # on day 1.  NEVER compare d_num here -- an exploding pool burns
            # out past N and produces a phantom late "crossing" that is not a
            # clearing.
            env_ok = risk and d_iter is None
        else:
            env_ok = (not risk) and d_formula == d_iter == d_num
        if env_ok:
            verified += 1
    if verified != len(corners):
        fail(f"closed-form stand-down disagrees with the numeric model on "
             f"{len(corners) - verified}/{len(corners)} corners -- refusing to "
             "publish an envelope")
    print(
        f"clear-lab {__version__} -- worst-case stand-down sweep over a "
        f"{args.grid}x{args.grid} grid ({len(corners)} corners), "
        f"N={fmt_hosts(base['N'])}, I0={fmt_hosts(base['i0'])}, "
        f"h={base['detect']:g}, c={c:.2f}, {base['days']} days"
    )
    if worst_at_stand is not None:
        print(f"  worst stand-down: day {worst_at_stand} "
              f"(beta={worst_at_corner[0]:.3f}, gamma={worst_at_corner[1]:.3f})")
    else:
        print("  worst stand-down: none -- every corner is AT RISK")
    print(f"  worst d*        : {worst_d} extra patching day(s) "
          f"(h={base['detect']:g}, c={c:.2f})")
    print(f"  at risk         : {len(at_risk_corners)}/{len(corners)} corners "
          "(bound h >= cG/(G-1) there)")
    print(f"  envelope        : {verified}/{len(corners)} corners verified "
          "numerically (fail-closed)")
    print(
        f"  plan for        : keep release gated until the worst stand-down "
        f"day, then redeploy immune hosts in tranches")
    append_record(args.store, "sweep", {
        "N": int(base["N"]), "i0": base["i0"], "days": int(base["days"]),
        "grid": args.grid, "corners": len(corners), "detect": base["detect"],
        "c": c, "contained": contained_at,
        "beta_range": [args.beta_min, args.beta_max],
        "gamma_range": [args.gamma_min, args.gamma_max],
    }, {
        "worst_stand_down": worst_at_stand,
        "worst_d_star": worst_d,
        "worst_at_corner": list(worst_at_corner) if worst_at_corner else None,
        "at_risk_corners": [[b, g] for b, g, _ in at_risk_corners],
        "verified": verified,
    })


def cmd_stats(args):
    records, err = read_store(args.store)
    if err:
        fail(err)
    if not records:
        print("no runs recorded yet")
        return
    print(f"clear-lab audit store: {args.store}  ({len(records)} records)")
    for rec in records[-8:]:
        m = rec.get("metrics", {})
        cmd = rec.get("command", "?")
        if cmd == "compare":
            disp = f"hidden_err={m.get('hidden_err', 0):.2e} d*={m.get('d_star_formula')}"
        elif cmd == "sweep":
            disp = f"worst_stand_down={m.get('worst_stand_down', '?')}"
        else:
            disp = f"{m.get('verdict', '?')}"
        beta = rec.get("params", {}).get("beta", 0)
        gamma = rec.get("params", {}).get("gamma", 0)
        print(
            f"  #{rec['seq']}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(rec.get('ts', 0)))}  "
            f"{cmd:7}  {disp}  beta={beta:.2f}  gamma={gamma:.2f}"
        )


def cmd_verify(args):
    ok, problems, n = verify_store(args.store)
    print(f"clear-lab audit verify: {args.store}")
    if not os.path.exists(args.store):
        print("  store does not exist yet (no runs recorded) -- nothing to verify")
        sys.exit(0)
    if ok:
        print(f"  verified: {n} records, chain intact")
        sys.exit(0)
    print(f"  TAMPERED: {len(problems)} problem(s) in {n} records")
    for p_ in problems:
        print(f"    - {p_}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------

def _expect(cond, name, state):
    state["total"] += 1
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}")
        state["failures"].append(name)


def selftest():
    state = {"failures": [], "total": 0}
    beta, gamma, i0 = 0.3, 0.1, 100.0

    # --- the stand-down theorem (closed form, both directions) --------------
    _expect(
        stand_down_days(10, 18, 1.17) == 0 and
        stand_down_days(0, 0, 1.17) is None and
        stand_down_days(40, 10, 1.17) == 5,
        "d* = 0 when the bound is already at or below capacity; AT RISK "
        "(None) with zero capacity; d* = 5 for h=40 > c=10",
        state,
    )
    matched = True
    for h, c in ((40, 10), (15, 25), (40, 25), (100, 18), (500, 100),
                 (3, 3), (1e6, 5e5)):
        for b, g in ((0.3, 0.1), (0.25, 0.125), (0.5, 0.2)):
            gg = growth_factor(b, g)
            if h >= hidden_threshold(c, gg):
                continue
            if stand_down_days(h, c, gg) != iterate_d_star(h, c, gg):
                matched = False
    _expect(
        matched,
        "the closed-form d* equals the straight recurrence iteration "
        "across a parameter sweep (both paths to the same day)",
        state,
    )
    th = hidden_threshold(10, 1.17)
    _expect(
        stand_down_days(th - 1e-6, 10, 1.17) is not None and
        stand_down_days(th, 10, 1.17) is None and
        stand_down_days(th + 1e-6, 10, 1.17) is None,
        "the clearable boundary is load-bearing: h just below the threshold "
        "clears, h at or above it is AT RISK (fail-closed at equality)",
        state,
    )
    # The bound pool provably reaches zero in the model: starting the daily map
    # at exactly I0 = h with capacity c, the pool crosses c at day d* and is
    # 0 the next day.
    h, c = 40, 10
    states, extinct = make_run({"N": 1_000_000_000, "i0": h, "days": 30,
                                "beta": 0.3, "gamma": 0.1, "c": c})
    # None-tolerant: on a non-contained trajectory the pool never descends
    # through c, and the check must FAIL, not crash the suite (see the
    # containment mutants in ci/mutation.py -- a crash is not a kill).
    first_le_c = next((d for d, st in states if st[1] <= c), None)
    _expect(
        first_le_c == 5 and extinct == 6,
        "the worst-case hidden pool run from h=40 falls to <= c at day 5 "
        "(d*) and to 0 at day 6 (d*+1) in the daily model",
        state,
    )

    # --- the linear hidden-pool map vs the closed form ----------------------
    win_err, win_pts, win_cross, _, win_risk = hidden_pool_analysis(
        100, 25, 0.3, 0.1, 30, n=1_000_000_000)
    _expect(
        win_err <= 1e-3 and win_pts >= 2 and win_cross == 5 and not win_risk,
        f"inside the linear window the daily map matches the closed form "
        f"I_d (worst {win_err:.2e}, {win_pts} days, cross day {win_cross})",
        state,
    )

    # --- containment-map window + load-bearing (shared daily-map guard) -----
    # The pure G*(I-c) anchor assumes S ~= N, so it carries an O(beta*(1-S/N))
    # error at finite N: the honest tolerance is 1e-3, not float noise.  N is
    # huge here so depletion is ~1e-5 and the window holds.
    win_p = {"beta": beta, "gamma": gamma, "i0": 500.0, "N": 1_000_000_000,
             "days": 30, "c": min_capacity(beta, gamma, 500.0) * 0.5}
    win_states, _ = make_run(win_p)
    cmap_worst, cmap_points = map_match(win_p, win_states)
    _expect(
        cmap_worst <= 1e-3 and cmap_points >= 20,
        f"inside the linear window the containment map matches G*(I-c) "
        f"(worst {cmap_worst:.2e}, {cmap_points} days)",
        state,
    )
    # Load-bearing: with a small N the epidemic burns S down and breaks the
    # anchor; excluding the tail is what makes the window honest.  The window
    # error must be a fraction of the all-points error, or the window serves
    # no purpose (and a mutant that disables it goes unnoticed).
    long_p = {"beta": beta, "gamma": gamma, "i0": 200.0, "N": 50_000,
              "days": 120, "c": min_capacity(beta, gamma, 200.0) * 0.5}
    long_states, _ = make_run(long_p)
    long_win, _ = map_match(long_p, long_states)
    long_all, _ = map_match(long_p, long_states, keep_frac=0.0)
    _expect(
        long_win < long_all / 10 and long_all > 1e-2,
        f"excluding the nonlinear tail is load-bearing "
        f"(window {long_win:.2e} vs all-points {long_all:.2e})",
        state,
    )
    # Newton's third law of planning: the closed form must be the fail-closed
    # UPPER bound on the patching effort.  Depletion only helps the crew, so
    # at ANY finite N the daily model never clears the hidden pool later than
    # the formula predicts, and at small N it clears strictly earlier.
    cons_ok = True
    strict_ok = False
    for nn in (1_000_000_000, 1_000_000, 100_000):
        _, _, cross_n, _, _ = hidden_pool_analysis(20_000, 3_000, 0.3, 0.1,
                                                   60, n=nn)
        if cross_n is None or cross_n > 22:
            cons_ok = False
        if nn == 100_000 and cross_n is not None and cross_n < 22:
            strict_ok = True
    _expect(
        cons_ok,
        "the hidden-pool closed form is the fail-closed upper bound on the "
        "patching effort: the numeric crossover is never later than d* at "
        "any N (depletion only helps the crew)",
        state,
    )
    _expect(
        strict_ok,
        "small-N depletion clears the hidden pool strictly before the "
        "closed-form day (the bound is conservative, not optimistic)",
        state,
    )

    # --- the documented equations are implemented exactly --------------------
    def documented_step(s, i, p0, r0, n, beta_, gamma_, c):
        k = min(c, s + i)
        k_i = min(k, i)
        k_s = k - k_i
        i -= k_i
        s -= k_s
        p0 += k_i + k_s
        new = beta_ * s * i / n
        s -= new
        i += new
        rec = gamma_ * i
        i -= rec
        r0 += rec
        return (s, i, p0, r0)

    probe = {"beta": beta, "gamma": gamma, "i0": i0, "N": 1_000_000,
             "days": 10, "c": 10.0}
    after = step([800_000.0, 200.0, 0.0, 0.0], probe)
    expect = documented_step(800_000.0, 200.0, 0.0, 0.0, probe["N"],
                             beta, gamma, probe["c"])
    _expect(
        all(abs(a - b) < 1e-9 for a, b in zip(after, expect)),
        "every day the documented equations are exact: patch infectious "
        "first, then spread beta*S*I/N, then recover gamma*I",
        state,
    )

    # --- the lifecycle: eradication, verdict, conservation ------------------
    lp = {"beta": beta, "gamma": gamma, "i0": i0, "N": 1_000_000,
          "days": 120, "c": 18.0, "detect": 10.0, "release": 0.0}
    info = plan_lifecycle(lp)
    states, _ = make_run_full(lp)
    # None-tolerant like the hidden-pool crossing above: if patching cannot
    # hold the line the observed I never falls to h, and the check must name
    # the failure instead of raising StopIteration.
    first_le_h = next((d for d, st in states if st[1] <= lp["detect"]), None)
    _expect(
        info["contained"] and info["erad_day"] == first_le_h and
        info["stand_down_day"] == info["erad_day"] + 2 and
        info["verdict"].startswith("STAND DOWN SAFE"),
        "eradication is the first day observed I <= h, and with h <= c the "
        "stand-down day is exactly E + 2 (d* = 0)",
        state,
    )
    _expect(
        max(abs(sum(st) - lp["N"]) for _, st in states) < 1e-6,
        "S+I+P+R == N is conserved every day of the contained lifecycle",
        state,
    )
    lr = dict(lp, release=50.0, days=90)
    info_r = plan_lifecycle(lr)
    if info_r["stand_down_day"] is not None:
        trace = simulate_release(states, info_r["stand_down_day"], 50.0, 90)
        release_ok = (
            info_r["release_start"] == info_r["stand_down_day"] and
            all(trace[i][1] + trace[i][2] ==
                trace[0][1] + trace[0][2] for i in range(len(trace))) and
            all(trace[i][2] == trace[i - 1][2] - min(50.0, trace[i - 1][2])
                for i in range(1, len(trace))))
    else:
        # The gate never opened: plan_lifecycle returned no stand-down day,
        # so there is no release trace at all. Report it as a failed check
        # rather than handing None to simulate_release's day loop.
        release_ok = False
    _expect(
        release_ok,
        "release is gated on stand-down and drains the immune pool at exactly "
        "r hosts/day into the susceptible pool (S+P conserved)",
        state,
    )
    risk = dict(lp, detect=200.0)  # c=18 stays >= c_min, so it IS contained:
    # but h=200 >= cG/(G-1)=123.88 -> the hidden pool can never be cleared.
    info_risk = plan_lifecycle(risk)
    _expect(
        info_risk["verdict"].startswith("AT RISK") and
        info_risk["stand_down_day"] is None and
        info_risk["release_start"] is None,
        "an un-clearable hidden pool (h >= cG/(G-1)) holds stand-down and "
        "release -- the gate never opens",
        state,
    )
    under = dict(lp, c=10.0, detect=10.0)
    info_under = plan_lifecycle(under)
    _expect(
        info_under["verdict"] == "NOT CONTAINED" and
        info_under["stand_down_day"] is None,
        "a capacity below c_min never earns a stand-down (NOT CONTAINED)",
        state,
    )
    # Early stand-down rekindles the hidden pool: with no patching the worm
    # regrows at G/day and re-enters sight. This is what the gate prevents.
    relapse, _ = make_run({"N": 1_000_000, "i0": 40.0, "days": 30,
                           "beta": 0.3, "gamma": 0.1, "c": 0.0})
    _expect(
        max(st[1] for _, st in relapse) > 40.0 * 1.2,
        "early stand-down lets a hidden pool of 40 regrow past 1.2*h "
        "(peak {:.0f}) -- the gate is load-bearing".format(
            max(st[1] for _, st in relapse)),
        state,
    )

    # --- determinism + negative control -------------------------------------
    d1 = plan_lifecycle(lp)
    d2 = plan_lifecycle(lp)
    _expect(
        d1["stand_down_day"] == d2["stand_down_day"] and
        d1["erad_day"] == d2["erad_day"] and
        d1["peak"] == d2["peak"],
        "identical arguments produce an identical lifecycle (deterministic)",
        state,
    )
    shifted = dict(lp, detect=3.0)
    info_shifted = plan_lifecycle(shifted)
    _expect(
        info_shifted["stand_down_day"] != info["stand_down_day"],
        "negative control: a perturbed detection limit shifts the stand-down "
        "day (h=3 below h=10 changes E)",
        state,
    )

    # --- sweep envelope (fail-closed planning) ------------------------------
    corners = [(b, g) for b in _linspace(0.25, 0.4, 3)
               for g in _linspace(0.05, 0.15, 3)]
    c_sweep = 297.74
    h_sweep = 1500.0
    ok_all = True
    risk_found = False
    for b, g in corners:
        gg = growth_factor(b, g)
        d_f = stand_down_days(h_sweep, c_sweep, gg)
        d_n = None
        if d_f is not None:
            d_n = iterate_d_star(h_sweep, c_sweep, gg)
            if d_f != d_n:
                ok_all = False
        else:
            risk_found = True
    _expect(
        ok_all and risk_found,
        "the sweep's closed-form verdicts/d* match the recurrence on every "
        "corner AND at least one corner of the box is AT RISK (never hidden)",
        state,
    )

    # --- audit chain ---------------------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        store = os.path.join(td, "audit.jsonl")
        p_audit = {"N": 1000000, "i0": 100.0, "days": 30, "beta": 0.3,
                   "gamma": 0.1, "c": 20.0, "detect": 10.0, "release": 0.0}
        rec = append_record(store, "plan", p_audit,
                            {"verdict": "STAND DOWN SAFE", "peak": 100.0})
        _expect(rec["seq"] == 0 and rec["prev"] == GENESIS,
                "the first audit record anchors to the genesis hash", state)
        ok, problems, n = verify_store(store)
        _expect(ok and n == 1, "a freshly appended audit record verifies", state)

        # tamper 1: edit a recorded number (canonical JSON has no spaces)
        with open(store, encoding="utf-8") as fh:
            data = fh.read()
        edited = data.replace('"peak":100.0', '"peak":999.0')
        _expect(edited != data, "the tamper edit actually changed the line", state)
        with open(store, "w", encoding="utf-8") as fh:
            fh.write(edited)
        ok, problems, _ = verify_store(store)
        _expect(not ok and any("hash does not match" in p_ for p_ in problems),
                "editing a recorded number breaks the chain", state)

        # tamper 2: delete the oldest record (genesis anchor must catch it)
        store2 = os.path.join(td, "audit2.jsonl")
        append_record(store2, "plan", p_audit, {"verdict": "STAND DOWN SAFE", "peak": 100.0})
        append_record(store2, "plan", p_audit, {"verdict": "AT RISK", "peak": 200.0})
        with open(store2, encoding="utf-8") as fh:
            lines = fh.readlines()
        with open(store2, "w", encoding="utf-8") as fh:
            fh.writelines(lines[1:])
        ok, problems, _ = verify_store(store2)
        _expect(not ok and any("expected the genesis anchor" in p_ for p_ in problems),
                "deleting the oldest record breaks the genesis anchor", state)

        # tamper 3: insert a forged record borrowing a seq number
        store3 = os.path.join(td, "audit3.jsonl")
        append_record(store3, "plan", p_audit, {"verdict": "STAND DOWN SAFE", "peak": 100.0})
        append_record(store3, "plan", p_audit, {"verdict": "AT RISK", "peak": 200.0})
        with open(store3, encoding="utf-8") as fh:
            first = fh.readline()
            rest = fh.read()
        forged = {
            "seq": 1, "ts": 0.0, "tool": "clear-lab", "version": "9.9",
            "command": "plan", "params": {}, "metrics": {},
            "prev": "x" * 64, "hash": "y" * 64,
        }
        with open(store3, "w", encoding="utf-8") as fh:
            fh.write(first)
            fh.write(json.dumps(forged, sort_keys=True, separators=(",", ":")) + "\n")
            fh.write(rest)
        ok, problems, _ = verify_store(store3)
        _expect(not ok and any("does not match" in p_ for p_ in problems),
                "an inserted record with a borrowed seq breaks the chain", state)

        ok, problems, n = verify_store(os.path.join(td, "empty.jsonl"))
        _expect(ok and n == 0, "a missing (empty) store verifies as empty", state)

    return state["failures"], state["total"]


def run_selftest_cli():
    print(f"clear-lab {__version__} selftest")
    failures, total = selftest()
    if failures:
        print(f"selftest FAILED: {len(failures)} check(s)")
        sys.exit(1)
    print(f"\nall checks passed: {total} run, 0 skipped")
    sys.exit(0)


# CLI
# ---------------------------------------------------------------------------

def build_parser():
    ap = argparse.ArgumentParser(
        prog="clear-lab",
        description="Eradication & safe stand-down planner: when a contained "
                    "worm is provably dead, and when immune hosts can return "
                    "to duty (closed-form d*, hash-chained audit).",
        epilog="audit store: env CLEAR_LAB_STORE, else ~/.clearlab/audit.jsonl",
    )
    ap.add_argument("--version", action="version", version=f"clear-lab {__version__}")
    ap.add_argument("--store", default=None, help="audit store path (overrides env/default)")
    sub = ap.add_subparsers(dest="command", required=False)

    def common(cmd):
        cmd.add_argument("--N", type=int, default=1_000_000)
        cmd.add_argument("--i0", type=float, default=100.0)
        cmd.add_argument("--days", type=float, default=365)

    s = sub.add_parser("selftest", help="run the self-test")
    s.set_defaults(func=lambda a: run_selftest_cli())

    c = sub.add_parser("plan", help="eradicate & stand-down plan for one scenario")
    common(c)
    c.add_argument("--beta", type=float, default=0.3)
    c.add_argument("--gamma", type=float, default=0.1)
    c.add_argument("--c", type=float, default=None,
                   help="capacity (hosts/day); default: 1.2x the minimum")
    c.add_argument("--detect", type=float, default=10.0,
                   help="detection limit h: I <= h is invisible below the radar")
    c.add_argument("--release", type=float, default=None,
                   help="redeployment rate r hosts/day of immune hosts after "
                        "stand-down; default 0 (no redeployment)")
    c.set_defaults(func=cmd_plan)

    c = sub.add_parser("compare", help="stand-down anchor vs the daily map")
    common(c)
    # compare needs a huge N so S ~= N holds inside the linear window.
    c.set_defaults(N=1_000_000_000, i0=500.0, days=30)
    c.add_argument("--beta", type=float, default=0.3)
    c.add_argument("--gamma", type=float, default=0.1)
    c.add_argument("--c", type=float, default=None,
                   help="capacity (hosts/day); default 25")
    c.add_argument("--detect", type=float, default=100.0,
                   help="hidden-pool bound h to analyse (default 100)")
    c.add_argument("--tol", type=float, default=1e-3)
    c.add_argument("--keep-frac", type=float, default=0.99,
                   help="kept for the report; window is fixed at 0.99 * N")
    c.add_argument("--fail-over", action="store_true",
                   help="exit 1 if the anchor diverges from the model (CI gate)")
    c.set_defaults(func=cmd_compare)

    s = sub.add_parser("sweep", help="worst-case stand-down over an uncertain beta/gamma")
    common(s)
    s.add_argument("--beta-min", type=float, default=0.2)
    s.add_argument("--beta-max", type=float, default=0.4)
    s.add_argument("--gamma-min", type=float, default=0.05)
    s.add_argument("--gamma-max", type=float, default=0.15)
    s.add_argument("--grid", type=int, default=5)
    s.add_argument("--detect", type=float, default=10.0,
                   help="hidden-pool bound h to analyse")
    s.add_argument("--c", type=float, default=None,
                   help="capacity (hosts/day); default 1.2x the worst corner c_min")
    s.set_defaults(func=cmd_sweep)

    s = sub.add_parser("stats", help="recent audit records")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("verify", help="verify the audit chain")
    s.set_defaults(func=cmd_verify)

    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.store is None:
        args.store = default_store()
    if args.command in (None, "help"):
        ap.print_help()
        return 0
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
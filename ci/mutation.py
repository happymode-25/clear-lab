"""Mutation sweep for clear-lab, run against a SCRATCH COPY.

Why a scratch copy
------------------
The first version of the egress-broker harness rewrote the tool in place, in
the working tree. A sweep that dies halfway leaves a mutant sitting in the repo
looking like a legitimate commit. So the live repo is hashed before and after,
every mutation is applied to a throwaway copy, and the harness refuses to start
if it cannot make one.

Why a canary
------------
A sweep that reports "11/11 killed" is exactly what a broken harness reports.
The canary below is a provably semantically identical mutation (x and x * 1.0
cannot differ) and it MUST survive. If it is reported as killed, the verdict
for every other mutant in the run is worthless and the run is void.

Lessons encoded here, each one bought with a false result:
  * `with` on every write; an unflushed buffer made a mutant that never ran.
  * the written file is re-read from disk and compared before it is trusted.
  * py_compile must succeed or the run is discarded.
  * __pycache__ is purged around every run, or a stale .pyc masks the change.
  * selftest failures are read from STDOUT (`_expect` prints there).
  * the suite is asserted green before the sweep and again after the revert.
  * the e2e test COUNT is read from `Ran N tests`. `unittest` exits 0 when it
    collects nothing, so a mutant that stopped collection looks like a pass.
  * a mutation that makes the tool *refuse to run* (fail-closed exit) is not a
    killed check; mutants are chosen so the suites report a named failure.

Anchor discipline: every target must match EXACTLY one occurrence. The two
sites of `if i > c and s >= keep_frac * n and prev_i > c:` (map_match and
hidden_pool_analysis) and the two sites of `g = growth_factor(beta, gamma)`
(min_capacity and hidden_pool_analysis) are disambiguated by the following
source line.

Usage:
  python ci/mutation.py                    # full sweep
  python ci/mutation.py models compare     # only these groups
  python ci/mutation.py CANARY             # prove the harness can see a survivor

Run it from the repo root. It never writes to the working tree.
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile

SRC_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join("clear-lab.py")
TESTS = os.path.join("tests", "test_clear_lab.py")

COPY = [
    "clear-lab.py",
    os.path.join("tests", "test_clear_lab.py"),
]

CANARY = "CANARY"


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def clean_caches(root):
    for dirpath, dirnames, _ in os.walk(root):
        for d in list(dirnames):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(dirpath, d), ignore_errors=True)
                dirnames.remove(d)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def write(path, text):
    # newline="" so Windows does not rewrite every LF as CRLF and make the file
    # differ from what we intended to write.
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def run(root, args, timeout=900):
    return subprocess.run(
        [sys.executable] + args, capture_output=True, text=True, timeout=timeout, cwd=root
    )


def run_selftest(root):
    p = run(root, [TOOL, "selftest"], timeout=300)
    failed = sorted(set(
        m.group(1).strip() for m in re.finditer(r"^  FAIL (.+)$", p.stdout, re.M)
    ))
    ran = re.search(r"all checks passed: (\d+) run", p.stdout)
    return p.returncode, failed, (int(ran.group(1)) if ran else -1), p.stdout[-700:]


def run_e2e(root):
    p = run(root, [TESTS], timeout=900)
    text = p.stdout + p.stderr
    names = sorted(set(
        m.group(1).strip()
        for m in re.finditer(r"^(?:FAIL|ERROR): (\S+)", text, re.M)
    ))
    ran = re.search(r"^Ran (\d+) tests?", text, re.M)
    return p.returncode, names, (int(ran.group(1)) if ran else -1), text[-700:]


SUITES = {"selftest": run_selftest, "e2e": run_e2e}


def check_green(root, label, expect_selftest, expect_e2e):
    ok = True
    for name, fn in SUITES.items():
        code, failures, count, tail = fn(root)
        want = expect_selftest if name == "selftest" else expect_e2e
        good = code == 0 and not failures and count == want
        ok = ok and good
        print(f"  {label} {name:9} exit={code} checks={count} failures={len(failures)} "
              f"{'OK' if good else 'BAD'}")
        if not good:
            for line in tail.strip().splitlines()[-10:]:
                print("      " + line)
    return ok


# --------------------------------------------------------------------------
# The mutation table. Each entry replaces exactly one occurrence.
#   (id, description, suite, target substring, replacement line body)
# --------------------------------------------------------------------------

M = [
    # -- canary: must SURVIVE, and proves the harness can see a survivor -----
    # "g = growth_factor(beta, gamma)" occurs in BOTH min_capacity and
    # hidden_pool_analysis, so the anchor carries the following line to pick
    # the hidden-pool site.
    (CANARY, "G and G * 1.0 are the same value, so this cannot change "
             "behaviour",
     "selftest",
     '    g = growth_factor(beta, gamma)\n'
     '    states, _ = make_run({"N": n, "i0": h, "days": days,',
     '    g = growth_factor(beta, gamma) * 1.0\n'
     '    states, _ = make_run({"N": n, "i0": h, "days": days,'),

    # -- the daily map (shared step discipline) -----------------------------
    ("models", "recovery is no longer exactly gamma*I (drifts 1%), so the "
               "exact-equation check and the hidden-pool map checks break",
     "selftest",
     '    rec = p["gamma"] * i\n',
     '    rec = p["gamma"] * i * 1.01  # deliberately broken by CI\n'),
    ("models", "patched infectious hosts stay in the infectious pool, so "
               "containment at c >= c_min fails",
     "selftest",
     "    i -= k_i\n    s -= k_s\n    p_pool += k_i + k_s\n",
     "    i -= 0.0\n    s -= k_s\n    p_pool += k_i + k_s\n"),
    ("models", "the patch crew never touches infectious hosts, so c >= c_min "
               "fails to hold the line",
     "selftest",
     "\n    k_i = min(k, i)\n    k_s = k - k_i\n",
     "\n    k_i = 0.0\n    k_s = k - k_i\n"),
    ("models", "the daily patch capacity is halved, so c >= c_min no longer "
               "contains",
     "selftest",
     '    k = min(p["c"], s + i)',
     '    k = min(p["c"] * 0.5, s + i)'),

    # -- closed form / analytic anchor --------------------------------------
    # The c_min flatten has NO direct anchor in the selftest (the window
    # setups recompute c_min and are self-consistent), only the e2e suite
    # names the freezed "min capacity : 14.53" / "c deployed : 17.44" fields.
    ("compare", "the minimum-capacity formula is flattened, so the reported "
                "c_min and the default deployment c drift",
     "e2e",
     "    return i0 * (g - 1.0) / g",
     "    return i0 * (g - 0.5) / g"),
    # The window line exists in BOTH map_match and hidden_pool_analysis; the
    # anchor carries the following line to pick the containment-map site whose
    # load-bearing selftest check proves the window is what makes the anchor
    # honest.
    ("compare", "the containment-map window never excludes late points, so "
                "the load-bearing tail check fails",
     "selftest",
     "        if i > c and s >= keep_frac * n and prev_i > c:\n"
     "            expected = g * (prev_i - c)",
     "        if i > c and True and prev_i > c:\n"
     "            expected = g * (prev_i - c)"),

    # -- sweep (worst-case envelope) ----------------------------------------
    ("sweep", "the worst d* is never updated, so the envelope understates "
              "the patching effort",
     "e2e",
     '            if info["d_star"] > worst_d:',
     '            if False:  # worst d* is never updated'),
    ("sweep", "at-risk corners are never reported, so the sweep hides the "
              "corners the planner must refuse",
     "e2e",
     '            at_risk_corners.append((b, g, info["threshold"]))',
     '            pass  # at-risk corners are never reported'),

    # -- audit chain ---------------------------------------------------------
    ("audit", "the genesis anchor is no longer required on record 0",
     "selftest",
     '            if rec.get("prev") != GENESIS:',
     "            if False:  # the genesis anchor is never required"),
    ("audit", "every appended record carries a garbage hash, so the chain "
              "cannot verify and runs are refused",
     "e2e",
     '    rec["hash"] = _record_hash(rec)',
     '    rec["hash"] = "0" * 64'),

    # -- fail-closed input ---------------------------------------------------
    ("cli", "a zero beta or gamma is accepted instead of refused",
     "e2e",
     "    if value <= 0:",
     "    if value < 0:"),
]


def main():
    wanted = set(sys.argv[1:])
    # The canary always runs even when groups are filtered: a subset run must
    # still prove it can see a survivor.
    rows = [m for m in M if m[0] == CANARY or not wanted or m[0] in wanted]

    live_tool = os.path.join(SRC_REPO, TOOL)
    before = digest(live_tool)
    print(f"live repo: {SRC_REPO}")
    print(f"  tool sha256 before sweep: {before[:16]}")

    root = tempfile.mkdtemp(prefix="mutate-")
    for rel in COPY:
        dst = os.path.join(root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(os.path.join(SRC_REPO, rel), dst)
    clean_caches(root)
    print(f"scratch copy: {root}")
    print()

    tool_path = os.path.join(root, TOOL)
    original = read(tool_path)

    print("== baseline ==")
    if not check_green(root, "baseline", 26, 31):
        shutil.rmtree(root, ignore_errors=True)
        sys.exit("baseline is not green; the harness cannot be trusted")
    print()

    results = []
    try:
        for mid, desc, suite, target, repl in rows:
            clean_caches(root)
            n = original.count(target)
            if n != 1:
                verdict = "PATTERN-MISS" if n == 0 else "PATTERN-AMBIGUOUS"
                results.append((mid, desc, suite, verdict))
                print(f"  {verdict:12} {mid:8} {desc}  ({n} matches)")
                continue
            write(tool_path, original.replace(target, repl, 1))
            if read(tool_path) != original.replace(target, repl, 1):
                results.append((mid, desc, suite, "WRITE-FAILED"))
                print(f"  {'WRITE-FAILED':12} {mid:8} {desc}")
                continue
            c = subprocess.run([sys.executable, "-m", "py_compile", tool_path],
                               capture_output=True, text=True)
            if c.returncode != 0:
                results.append((mid, desc, suite, "WILL-NOT-COMPILE"))
                print(f"  {'WILL-NOT-COMPILE':12} {mid:8} {desc}")
                print("               " + c.stderr.strip().splitlines()[-1])
                continue
            clean_caches(root)
            code, failures, count, tail = SUITES[suite](root)
            killed = code != 0 and bool(failures)
            verdict = "KILLED" if killed else "SURVIVED"
            results.append((mid, desc, suite, verdict))
            print(f"  {verdict:12} {mid:8} {desc}")
            for f in list(failures)[:3]:
                print(f"               - {f}")
            if not killed and not failures and code != 0:
                print("               (nonzero exit but nothing named -- see below)")
            elif killed and count in (-1, 0):
                print(f"               (suite ran {count} tests)")
    finally:
        write(tool_path, original)
        print()

    print("== after revert ==")
    restored = check_green(root, "restored", 26, 31)

    after = digest(live_tool)
    untouched = before == after
    print(f"\nlive repo tool sha256 after sweep: {after[:16]}  "
          f"{'UNTOUCHED' if untouched else 'CHANGED -- the sweep escaped the scratch copy'}")
    shutil.rmtree(root, ignore_errors=True)

    canary = [r for r in results if r[0] == CANARY]
    canary_ok = bool(canary) and canary[0][3] == "SURVIVED"
    if not canary_ok:
        print("\nCANARY DID NOT SURVIVE -- the harness kills things that cannot be "
              "killed, so no verdict above means anything.")

    killed = [r for r in results if r[3] == "KILLED"]
    broken = [r for r in results if r[3] in ("PATTERN-MISS", "PATTERN-AMBIGUOUS",
                                            "WRITE-FAILED", "WILL-NOT-COMPILE")]
    survivors = [r for r in results if r[3] == "SURVIVED" and r[0] != CANARY]

    print()
    print(f"killed {len(killed)} / {len(results) - len(broken)} runnable mutants")
    print(f"canary {'survived as required' if canary_ok else 'FAILED -- run is void'}")
    print(f"live repo {untouched and 'untouched' or 'MODIFIED'}")
    if broken:
        print(f"\n{len(broken)} mutant(s) never ran:")
        for r in broken:
            print(f"  {r[3]}: {r[0]} -- {r[1]}")
    if survivors:
        print(f"\n{len(survivors)} real survivor(s) -- these are weak tests, or bugs:")
        for r in survivors:
            print(f"  {r[0]}: {r[1]}  [{r[2]}]")

    bad = survivors or broken or not restored or not untouched or not canary_ok
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
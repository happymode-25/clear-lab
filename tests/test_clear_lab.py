"""clear-lab end-to-end suite.

Every test drives the real CLI as a subprocess (the tool is a single
hyphenated file, so importing is deliberately not how we test it). The store
for each test lives in a fresh temp directory, so runs are isolated.

The suite asserts outcomes the user can observe: exit codes, report fields
(the fall-to-detection day, the stand-down day, the verdict, the sweep
envelope), fail-closed behaviour on bad input and broken audit stores, the
closed-form d* being reported and load-bearing, and the hash-chained audit
trail detecting every class of tamper we know about.

Every numeric anchor below was frozen by running the tool and reading its own
report -- the test asserts reality, and the CI floors keep the suite at the
count mutation.py expects.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

TOOL = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "..", "clear-lab.py"))


def run_cli(args, store=None, extra_env=None):
    env = dict(os.environ)
    if store is not None:
        env["CLEAR_LAB_STORE"] = store
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, TOOL] + [str(a) for a in args],
        capture_output=True, text=True, env=env,
        cwd=os.path.dirname(TOOL), timeout=300,
    )


def read_store(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def field(pattern, text):
    """Extract one `label : value` style field, commas stripped."""
    return re.search(pattern, text).group(1).replace(",", "")


class ClearLabE2E(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.store = os.path.join(self._td.name, "audit.jsonl")

    def plan(self, *extra):
        return run_cli(["plan"] + list(extra), store=self.store)

    # --- core commands ------------------------------------------------------

    def test_selftest_green(self):
        r = run_cli(["selftest"], store=self.store)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("all checks passed: 26 run", r.stdout)
        self.assertNotIn("FAIL", r.stdout)

    def test_plan_default_safe(self):
        # Default plan: c = 1.2x c_min (14.53 -> 17.44), h=10 <= c so d* = 0
        # and stand-down is exactly E + 2. E is day 11 here.
        r = self.plan()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("min capacity  : 14.53", r.stdout)
        self.assertIn("c deployed    : 17.44 hosts/day (1.20x min)", r.stdout)
        self.assertIn("eradication   : day 11", r.stdout)
        self.assertIn("stand-down    : day 13", r.stdout)
        self.assertIn("verdict       : STAND DOWN SAFE on day 13", r.stdout)
        self.assertIn("extinction    : day 12", r.stdout)
        self.assertIn("peak I        : 100 hosts at day 0", r.stdout)
        self.assertIn("recorded to audit store", r.stdout)

    def test_plan_detect_below_c(self):
        # h=10 <= c=18: still d* = 0; a stronger c pulls E (and stand-down)
        # one day earlier and extinction a day earlier than the default plan.
        r = self.plan("--c", "18", "--detect", "10")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("eradication   : day 10", r.stdout)
        self.assertIn("stand-down    : day 12", r.stdout)
        self.assertIn("verdict       : STAND DOWN SAFE on day 12", r.stdout)
        self.assertIn("extinction    : day 11", r.stdout)

    def test_plan_d_star_positive(self):
        # h=40 > c=20: the hidden pool needs d* = 2 extra patching days, so
        # stand-down is E + 2 + 2 = 11 (E = day 7 here).
        r = self.plan("--c", "20", "--detect", "40")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("eradication   : day 7", r.stdout)
        self.assertIn("d* = 2 extra patching days", r.stdout)
        self.assertIn("stand-down    : day 11", r.stdout)
        self.assertIn("verdict       : STAND DOWN SAFE on day 11", r.stdout)

    def test_plan_at_risk_unclearable_pool(self):
        # h=200 >= cG/(G-1)=120.00 with the default c: the hidden pool can
        # never be cleared and the gate stays shut (stand-down AND release).
        r = self.plan("--detect", "200")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("pathology     : hidden bound h=200 >= the clearable "
                      "threshold cG/(G-1)=120.00 -- NOT clearable", r.stdout)
        self.assertIn("stand-down    : held", r.stdout)
        self.assertIn("verdict       : AT RISK", r.stdout)
        self.assertIn("release       : held until stand-down is possible", r.stdout)

    def test_plan_not_contained(self):
        # c=10 < c_min=14.53: the worm is not held at I0, no stand-down.
        r = self.plan("--c", "10")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("verdict       : NOT CONTAINED", r.stdout)
        self.assertIn("stand-down    : held (c=10.00 < c_min=14.53; the worm "
                      "is not held at I0)", r.stdout)

    def test_plan_zero_detection(self):
        # h=0 means "invisible below exactly zero": the pool must actually
        # die (day 12) before the crew stands down on day 14.
        r = self.plan("--detect", "0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("eradication   : day 12", r.stdout)
        self.assertIn("stand-down    : day 14", r.stdout)
        self.assertIn("verdict       : STAND DOWN SAFE on day 14", r.stdout)

    def test_plan_release_gated_and_completes(self):
        # Immune hosts return from the stand-down day at r hosts/day and the
        # redeployment completes when the pool is drained (P at stand-down-1
        # is ~160 -> 4 days at 50/day, done on day 15).
        r = self.plan("--c", "18", "--detect", "10", "--release", "50", "--days", "90")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("stand-down    : day 12", r.stdout)
        self.assertIn("release       : immune hosts return to duty from day "
                      "12 at 50 hosts/day", r.stdout)
        self.assertIn("release done  : redeployment complete by day 15", r.stdout)

    # --- fail-closed input handling ----------------------------------------

    def test_invalid_i0_fails_closed(self):
        r = self.plan("--i0", "5000000", "--N", "1000000")
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot exceed N", r.stderr)

    def test_negative_beta_fails_closed(self):
        r = self.plan("--beta", "-0.5")
        self.assertEqual(r.returncode, 1)
        self.assertIn("beta must be > 0", r.stderr)

    def test_zero_beta_fails_closed(self):
        r = self.plan("--beta", "0")
        self.assertEqual(r.returncode, 1)
        self.assertIn("beta must be > 0", r.stderr)

    def test_zero_gamma_fails_closed(self):
        r = self.plan("--gamma", "0")
        self.assertEqual(r.returncode, 1)
        self.assertIn("gamma must be > 0", r.stderr)

    def test_negative_c_fails_closed(self):
        r = self.plan("--c", "-1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("c must be >= 0", r.stderr)

    def test_negative_detect_fails_closed(self):
        r = self.plan("--detect", "-1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("detect must be >= 0", r.stderr)

    def test_unknown_command_exits_2(self):
        r = run_cli(["frobnicate"], store=self.store)
        self.assertEqual(r.returncode, 2)

    def test_version_flag(self):
        r = run_cli(["--version"], store=self.store)
        self.assertEqual(r.returncode, 0)
        self.assertIn("clear-lab 1.0.0", r.stdout)

    # --- the stand-down anchor vs the daily map -----------------------------

    def test_compare_green_defaults(self):
        # h=100, c=25, G=1.17: d* = 5 on all three paths, boundary 172.06,
        # map error ~2.6e-7, gates all green.
        r = run_cli(["compare"], store=self.store)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("hidden map err: 2.587e-07", r.stdout)
        self.assertIn("d* formula    : 5 patching day(s)", r.stdout)
        self.assertIn("d* iteration  : 5", r.stdout)
        self.assertIn("d* numeric    : 5", r.stdout)
        self.assertIn("boundary      : clearable while h < 172.06;", r.stdout)
        self.assertIn("verdict       : within tolerance", r.stdout)

    def test_compare_at_risk_agrees(self):
        # h=21000 >= cG/(G-1)=20647.06: the closed form must say AT RISK and
        # the numeric oracle must confirm a day-one GROWTH (never a phantom
        # burn-out crossing). The growing pool's map error exceeds tol, so the
        # verdict is OUT OF TOLERANCE -- but the AT RISK report is exact.
        r = run_cli(["compare", "--detect", "21000", "--c", "3000"], store=self.store)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("d* formula    : AT RISK -- h=21000 >= cG/(G-1)=20647.06, "
                      "the pool never contracts", r.stdout)
        self.assertIn("d* numeric    : never  (the model grows on day 1", r.stdout)
        self.assertIn("verdict       : OUT OF TOLERANCE", r.stdout)

    def test_compare_fail_over_really_fails(self):
        # Negative control through the real CLI: small N burns S down and the
        # pure G*(I-c) anchor breaks; --fail-over MUST go red.
        r = run_cli(["compare", "--N", "50000", "--detect", "20000", "--c",
                     "3000", "--days", "30", "--keep-frac", "0.0",
                     "--fail-over"], store=self.store)
        self.assertEqual(r.returncode, 1)
        self.assertIn("diverged", r.stderr)
        self.assertIn("OUT OF TOLERANCE", r.stdout)

    # --- worst-case sweep ----------------------------------------------------

    def test_sweep_worst_case_engineered(self):
        # The engineered box: worst stand-down is day 11 (beta=0.25,gamma=0.05),
        # worst d* = 9, exactly the 3 corners above the clearable threshold are
        # flagged AT RISK, and the envelope verifies 9/9 against the model.
        r = run_cli(["sweep", "--beta-min", "0.25", "--beta-max", "0.4",
                     "--gamma-min", "0.05", "--gamma-max", "0.15",
                     "--grid", "3", "--i0", "1000", "--detect", "1500",
                     "--N", "1000000", "--days", "365"], store=self.store)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("worst stand-down: day 11 (beta=0.250, gamma=0.050)", r.stdout)
        self.assertIn("worst d*        : 9 extra patching day(s)", r.stdout)
        self.assertIn("at risk         : 3/9 corners", r.stdout)
        self.assertIn("envelope        : 9/9 corners verified numerically", r.stdout)

    def test_sweep_crosscheck_explicit_c(self):
        # The same box at the explicit capacity the default derives. The
        # fleet-wide plan must agree, and the corner plans must too: (0.4,0.05)
        # is AT RISK (threshold 1199.98), (0.25,0.05) stands down day 11.
        r = run_cli(["sweep", "--beta-min", "0.25", "--beta-max", "0.4",
                     "--gamma-min", "0.05", "--gamma-max", "0.15",
                     "--grid", "3", "--i0", "1000", "--detect", "1500",
                     "--c", "297.74", "--N", "1000000", "--days", "365"],
                    store=self.store)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("worst stand-down: day 11 (beta=0.250, gamma=0.050)", r.stdout)
        self.assertIn("worst d*        : 9 extra patching day(s)", r.stdout)
        self.assertIn("at risk         : 3/9 corners", r.stdout)
        self.assertIn("envelope        : 9/9 corners verified numerically", r.stdout)

        r_risk = self.plan("--beta", "0.4", "--gamma", "0.05", "--c", "297.74",
                           "--detect", "1500", "--i0", "1000", "--N", "1000000")
        self.assertEqual(r_risk.returncode, 0, r_risk.stderr)
        self.assertIn("cG/(G-1)=1199.98", r_risk.stdout)
        self.assertIn("verdict       : AT RISK", r_risk.stdout)

        r_safe = self.plan("--beta", "0.25", "--gamma", "0.05", "--c", "297.74",
                           "--detect", "1500", "--i0", "1000", "--N", "1000000")
        self.assertEqual(r_safe.returncode, 0, r_safe.stderr)
        self.assertIn("cG/(G-1)=1885.69", r_safe.stdout)
        self.assertIn("stand-down    : day 11", r_safe.stdout)
        self.assertIn("verdict       : STAND DOWN SAFE on day 11", r_safe.stdout)

    def test_sweep_default_green(self):
        # The default sweep (h=10, c=29.77) must plan, not refuse: the numeric
        # oracle's h <= c crossing is day 0, matching the closed-form d* = 0.
        # This is the regression guard for the one-day-convention defect.
        r = run_cli(["sweep", "--grid", "2"], store=self.store)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("worst stand-down: day 8 (beta=0.400, gamma=0.050)", r.stdout)
        self.assertIn("worst d*        : 0 extra patching day(s)", r.stdout)
        self.assertIn("at risk         : 0/4 corners", r.stdout)
        self.assertIn("envelope        : 4/4 corners verified numerically", r.stdout)
        self.assertIn("plan for        :", r.stdout)

    # --- audit trail ----------------------------------------------------------

    def test_run_creates_audit_record(self):
        self.plan()
        records = read_store(self.store)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["command"], "plan")
        self.assertEqual(records[0]["seq"], 0)
        self.assertEqual(records[0]["tool"], "clear-lab")
        self.assertEqual(len(records[0]["hash"]), 64)

    def test_verify_clean_after_runs(self):
        self.plan()
        run_cli(["compare"], store=self.store)
        r = run_cli(["verify"], store=self.store)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("verified: 2 records, chain intact", r.stdout)

    def test_verify_empty_missing_store(self):
        r = run_cli(["verify"], store=self.store)
        self.assertEqual(r.returncode, 0)
        self.assertIn("nothing to verify", r.stdout)

    def test_verify_catches_edit(self):
        self.plan()
        with open(self.store, encoding="utf-8") as fh:
            data = fh.read()
        edited = data.replace('"tool":"clear-lab"', '"tool":"evil-lab"', 1)
        self.assertNotEqual(edited, data)
        with open(self.store, "w", encoding="utf-8") as fh:
            fh.write(edited)
        r = run_cli(["verify"], store=self.store)
        self.assertEqual(r.returncode, 1)
        self.assertIn("TAMPERED", r.stdout)
        self.assertIn("hash does not match", r.stdout)

    def test_verify_catches_prefix_delete(self):
        self.plan()
        self.plan()
        with open(self.store, encoding="utf-8") as fh:
            lines = fh.readlines()
        with open(self.store, "w", encoding="utf-8") as fh:
            fh.writelines(lines[1:])
        r = run_cli(["verify"], store=self.store)
        self.assertEqual(r.returncode, 1)
        self.assertIn("genesis anchor", r.stdout)

    def test_verify_catches_forged_insert(self):
        self.plan()
        self.plan()
        with open(self.store, encoding="utf-8") as fh:
            first = fh.readline()
            rest = fh.read()
        forged = {
            "seq": 1, "ts": 0.0, "tool": "clear-lab", "version": "9.9",
            "command": "plan", "params": {}, "metrics": {},
            "prev": "x" * 64, "hash": "y" * 64,
        }
        with open(self.store, "w", encoding="utf-8") as fh:
            fh.write(first)
            fh.write(json.dumps(forged, sort_keys=True, separators=(",", ":")) + "\n")
            fh.write(rest)
        r = run_cli(["verify"], store=self.store)
        self.assertEqual(r.returncode, 1)
        self.assertIn("TAMPERED", r.stdout)

    def test_run_refused_on_tampered_store(self):
        # Fail-closed: once the store no longer verifies, new runs must be
        # refused rather than silently appending to a broken chain.
        self.plan()
        with open(self.store, encoding="utf-8") as fh:
            data = fh.read()
        with open(self.store, "w", encoding="utf-8") as fh:
            fh.write(data.replace('"tool":"clear-lab"', '"tool":"broken-lab"', 1))
        r = self.plan()
        self.assertEqual(r.returncode, 1)
        self.assertIn("audit store does not verify", r.stderr)
        self.assertIn("refusing to report a run that cannot be evidenced", r.stderr)

    def test_env_store_location_respected(self):
        alt = os.path.join(self._td.name, "elsewhere", "run.jsonl")
        r = run_cli(["plan"], store=alt)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(alt))
        self.assertEqual(len(read_store(alt)), 1)

    def test_stats_lists_runs(self):
        self.plan()
        run_cli(["compare"], store=self.store)
        r = run_cli(["stats"], store=self.store)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("#0", r.stdout)
        self.assertIn("#1", r.stdout)
        self.assertIn("plan", r.stdout)
        self.assertIn("compare", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
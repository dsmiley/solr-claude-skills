import unittest
from datetime import datetime, timezone

import backport_common as bc

NOW = "2026-09-20T12:00:00+00:00"


def state(**subjects):
    return {"subjects": subjects, "meta": {}}


def skip(milestone, kind="permanent"):
    return {"status": "skip", "kind": kind, "reason": "r", "milestone_at_decision": milestone}


class IsTerminalTest(unittest.TestCase):
    def test_done_is_terminal(self):
        self.assertTrue(bc.is_terminal({"status": "backported"}, {}))

    def test_permanent_skip_holds_while_milestone_unchanged(self):
        entry = {"gh": {"milestone": "10.x"}}
        self.assertTrue(bc.is_terminal(skip("10.x"), entry))
        self.assertFalse(bc.is_terminal(skip("9.x"), entry))

    def test_deferred_and_kindless_skip_are_not_terminal(self):
        self.assertFalse(bc.is_terminal(skip("10.x", kind="deferred"), {"gh": {"milestone": "10.x"}}))
        self.assertFalse(bc.is_terminal({"status": "skip"}, {}))


class SweepTest(unittest.TestCase):
    def test_sweep_applies_milestone_change_and_reopens_skip(self):
        st = state(a={
            "gh": {"pr_number": 7, "milestone": None, "fetched_at": "2026-07-18T02:00:00+00:00"},
            "targets": {"branch_10x": skip(None)},
        })
        seen = {}

        def fetch(since):
            seen["since"] = since
            return {7: {"milestone": "10.x", "updated_at": "2026-09-20T11:00:00Z"}, 99: {"milestone": "9.x", "updated_at": "2026-09-20T11:30:00Z"}}

        changes = bc.sweep_pr_milestones(st, NOW, fetch=fetch)

        self.assertEqual(seen["since"], datetime(2026, 7, 18, 2, tzinfo=timezone.utc))
        self.assertEqual(changes, [{"subject": "a", "pr_number": 7, "old": None, "new": "10.x"}])
        self.assertEqual(
            bc.reopened_skips(st, ["branch_10x"]),
            [{"subject": "a", "target": "branch_10x", "reason": "r", "decided_under": None, "now": "10.x"}],
        )
        self.assertEqual(st["meta"]["sweep_checkpoint"], "2026-09-20T11:25:00+00:00")

    def test_failed_sweep_keeps_checkpoint(self):
        st = state()
        self.assertIsNone(bc.sweep_pr_milestones(st, NOW, fetch=lambda since: None))
        self.assertNotIn("sweep_checkpoint", st["meta"])

    def test_next_sweep_starts_at_checkpoint(self):
        st = state()
        st["meta"]["sweep_checkpoint"] = "2026-09-20T10:00:00+00:00"
        seen = {}
        bc.sweep_pr_milestones(st, NOW, fetch=lambda since: seen.setdefault("since", since) and {})
        self.assertEqual(seen["since"], datetime(2026, 9, 20, 10, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()

"""Offline tests for the `Fixed in vX.Y` step. No network: GitHub is a fake
that answers from the fixtures.

    python3 -m unittest discover -s .github/triage
"""

import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

import triage

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


class FakeGitHub(triage.GitHub):
    """Answers the calls check_fixed makes from a `kind: fixed` fixture and
    records every write. Any call it does not know fails the test."""

    def __init__(self, repo, fx, writes):
        super().__init__(repo, "test-token")
        self.fx, self.writes = fx, writes

    def call(self, method, path, body=None, query=None):
        fx, public = self.fx, self.repo == "plaramee1/blip"
        n = fx["issue"]["number"]
        if method != "GET" and not (method == "POST" and path == "/graphql"):
            self.writes.append((self.repo, method, path, body))
            if path.endswith("/comments"):
                fx["comments"].append({"user": {"login": fx["bot_login"], "type": "User"}, "body": body["body"]})
            if path.endswith("/labels"):
                fx["issue"]["labels"] += [{"name": l} for l in body["labels"]]
            return {}
        if path == "/graphql":
            return {"data": {"repository": {"issue": {"state": "CLOSED", "stateReason": "COMPLETED",
                    "timelineItems": {"nodes": [{"closer": fx["closer"]}]}}}}}
        if path == self.r("/releases"):
            return fx["public_releases"] if public else fx["private_releases"]
        if not public and path.startswith(self.r("/compare/")):
            tag = path.split("/compare/")[1].split("...")[0]
            return {"status": "behind" if tag in fx["contained_tags"] else "diverged"}
        if public and path == self.r(f"/issues/{n}"):
            return fx["issue"]
        if public and path == self.r(f"/issues/{n}/comments"):
            return fx["comments"]
        raise AssertionError(f"unexpected call {method} {self.repo}{path}")


def run(fx, entry=None, polls=1, allow_private=False):
    writes = []
    gh = FakeGitHub("plaramee1/blip", fx, writes)
    priv = FakeGitHub("plaramee1/brain-mac", fx, writes)
    entry = entry if entry is not None else {"mirrored": True, "mirror": fx["mirror"]}
    env = {"FIXED_ALLOW_PRIVATE": "1" if allow_private else "0"}
    with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
        for _ in range(polls):
            if not entry.get("fixed"):
                triage.check_fixed(gh, priv, str(fx["issue"]["number"]), entry, fx["labels"],
                                   fx["bot_login"], {}, dry_run=False)
    return writes, entry


def comments(writes):
    return [b["body"] for _, m, p, b in writes if p.endswith("/comments")]


class FixedInRelease(unittest.TestCase):
    def test_shipped_comments_once_and_labels(self):
        writes, entry = run(load("5-fixed-shipped.json"))
        self.assertEqual(len(comments(writes)), 1)
        body = comments(writes)[0]
        self.assertIn(triage.MARK_FIXED, body)
        self.assertIn("Fixed in [v0.100](https://github.com/plaramee1/blip/releases/tag/v0.100)", body)
        self.assertIn(("plaramee1/blip", "POST", "/repos/plaramee1/blip/issues/140/labels", {"labels": ["fixed"]}), writes)
        self.assertTrue(all(repo == "plaramee1/blip" for repo, *_ in writes), "never writes to the private repo")
        self.assertTrue(entry["fixed"])

    def test_shipped_is_idempotent_across_polls(self):
        writes, _ = run(load("5-fixed-shipped.json"), polls=3)
        self.assertEqual(len(comments(writes)), 1)

    def test_lost_state_still_never_comments_twice(self):
        fx = load("5-fixed-shipped.json")
        run(fx)
        fx["issue"]["labels"] = [l for l in fx["issue"]["labels"] if l["name"] != "fixed"]
        writes, entry = run(fx)  # a fresh state entry, the comment is still on the issue
        self.assertEqual(writes, [])
        self.assertTrue(entry["fixed"])

    def test_not_shipped_does_nothing(self):
        writes, entry = run(load("6-fixed-not-shipped.json"), polls=2)
        self.assertEqual(writes, [])
        self.assertFalse(entry.get("fixed"))

    def test_already_commented_does_nothing(self):
        writes, entry = run(load("7-fixed-already-commented.json"))
        self.assertEqual(writes, [])
        self.assertTrue(entry["fixed"])

    def test_labelled_fixed_by_a_person_does_nothing(self):
        fx = load("5-fixed-shipped.json")
        fx["issue"]["labels"].append({"name": "fixed"})
        writes, _ = run(fx)
        self.assertEqual(writes, [])

    def test_public_channel_held_does_nothing(self):
        writes, entry = run(load("8-fixed-public-channel-held.json"))
        self.assertEqual(writes, [])
        self.assertFalse(entry.get("fixed"))

    def test_public_channel_held_private_allowed_has_no_private_link(self):
        writes, _ = run(load("8-fixed-public-channel-held.json"), allow_private=True)
        [body] = comments(writes)
        self.assertIn("Fixed in **v0.100**.", body)
        self.assertNotIn("brain-mac", body)

    def test_open_or_not_planned_private_issue_does_nothing(self):
        self.assertIsNone(triage.closer_commit({"state": "OPEN"}))
        self.assertIsNone(triage.closer_commit({"state": "CLOSED", "stateReason": "NOT_PLANNED"}))
        unmerged = {"__typename": "PullRequest", "merged": False, "mergeCommit": None}
        self.assertIsNone(triage.closer_commit({"state": "CLOSED", "stateReason": "COMPLETED",
                                                "timelineItems": {"nodes": [{"closer": unmerged}]}}))
        self.assertIsNone(triage.closer_commit({"state": "CLOSED", "stateReason": "COMPLETED",
                                                "timelineItems": {"nodes": [{"closer": None}]}}))

    def test_first_containing_release_wins_and_older_ones_are_not_asked(self):
        fx = load("5-fixed-shipped.json")
        asked = []
        rel = triage.shipping_release(fx["public_releases"], fx["private_releases"],
                                      lambda t: asked.append(t) or True, since=fx["closer"]["mergedAt"])
        self.assertEqual(rel["tag_name"], "v0.100")
        self.assertEqual(asked, ["v0.100"])  # v0.99 predates the merge, v0.101 is a draft

    def test_fixture_cli_plans_match(self):
        self.assertEqual(triage.fixed_from_fixture(load("5-fixed-shipped.json"), False)["actions"]["add"], ["fixed"])
        for name in ("6-fixed-not-shipped.json", "7-fixed-already-commented.json", "8-fixed-public-channel-held.json"):
            self.assertIsNone(triage.fixed_from_fixture(load(name), False)["actions"], name)


if __name__ == "__main__":
    unittest.main()

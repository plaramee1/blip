#!/usr/bin/env python3
"""Triage one issue event on this repository.

The model only classifies (see prompt.md). Everything it may change is decided
here, so the rules hold whatever the issue text says:

- labels come only from the repository's own label set;
- at most one comment per event, built from fixed templates — no model text and
  no issue text is ever posted;
- the needs-info request is posted once per issue, ever;
- nothing is closed except high-confidence spam from someone outside the repo;
- an issue a person has labelled `triaged` is left alone.

Standard library only. Usage:

    triage.py                       # in Actions: reads GITHUB_EVENT_PATH etc.
    triage.py --fixture F.json      # offline dry run, prints the plan
    triage.py --fixture F.json --engine claude-cli   # dry run without an API key
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
KINDS = ("bug", "feature", "question")
MISSING = ("version", "macos", "steps")
INSIDERS = {"OWNER", "MEMBER", "COLLABORATOR"}
MARK_NEEDS_INFO = "<!-- blip-triage:needs-info -->"
MARK_DUPLICATES = "<!-- blip-triage:duplicates -->"
MISSING_TEXT = {
    "version": "the **Blip version** (Settings → Updates shows it, e.g. `0.90 · cde0eda9`)",
    "macos": "your **macOS version** and Mac model",
    "steps": "the **steps to reproduce** it — what you did, in order, and what happened",
}
STOPWORDS = set(
    """a an and are as at be but by can cannot could do does doesn't don't for
    from has have how i if in into is it it's its me my no not of on or so
    that the then there this to up was when where which while why will with
    won't would you your blip app issue bug feature request please""".split()
)


def log(msg):
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------- GitHub


class GitHub:
    def __init__(self, repo, token):
        self.repo, self.token = repo, token

    def call(self, method, path, body=None, query=None):
        url = f"https://api.github.com{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as err:
            # Never echo headers or the request: only the status and API message.
            detail = err.read().decode(errors="replace")[:300]
            raise SystemExit(f"GitHub {method} {path} failed: {err.code} {detail}")

    def r(self, suffix=""):
        return f"/repos/{self.repo}{suffix}"

    def labels(self):
        out = self.call("GET", self.r("/labels"), query={"per_page": 100})
        return [l["name"] for l in out]

    def comments(self, number):
        return self.call("GET", self.r(f"/issues/{number}/comments"), query={"per_page": 100})

    def search(self, terms, exclude):
        q = f"repo:{self.repo} is:issue " + " ".join(terms)
        out = self.call("GET", "/search/issues", query={"q": q, "per_page": 10})
        return [i for i in out.get("items", []) if i["number"] != exclude and "pull_request" not in i]

    def runs_today(self, workflow):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out = self.call(
            "GET", self.r(f"/actions/workflows/{workflow}/runs"),
            query={"created": f">={day}", "per_page": 1},
        )
        return out.get("total_count", 0)


# ---------------------------------------------------------------------- Model


def tool_schema(area_labels, candidate_numbers):
    return {
        "name": "triage",
        "description": "Record the triage decision for this issue.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(KINDS) + ["spam"]},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "areas": {"type": "array", "items": {"type": "string", "enum": area_labels or ["none"]}, "maxItems": 2},
                "missing": {"type": "array", "items": {"type": "string", "enum": list(MISSING)}},
                "duplicates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "number": {"type": "integer"},
                            "confidence": {"type": "string", "enum": ["high", "medium"]},
                        },
                        "required": ["number", "confidence"],
                    },
                },
                "reason": {"type": "string"},
            },
            "required": ["kind", "confidence", "areas", "missing", "duplicates", "reason"],
        },
    }


def clip(text, n):
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + " …[truncated]"


def user_message(issue, author_comments, area_labels, candidates):
    parts = [
        "Area labels you may use: " + (", ".join(area_labels) or "(none)"),
        "",
        "<issue>",
        f"Title: {clip(issue.get('title'), 300)}",
        f"Labels already on it: {', '.join(l['name'] for l in issue.get('labels', [])) or '(none)'}",
        "Body:",
        clip(issue.get("body"), 6000) or "(empty)",
        "</issue>",
    ]
    if author_comments:
        parts += ["", "<author_comments>"]
        parts += [f"- {clip(c, 1500)}" for c in author_comments[-5:]]
        parts += ["</author_comments>"]
    parts += ["", "<candidate_duplicates>"]
    if not candidates:
        parts.append("(none found)")
    for c in candidates:
        parts.append(
            f"#{c['number']} [{c['state']}] {clip(c['title'], 200)}\n  {clip(c.get('body'), 400).replace(chr(10), ' ')}"
        )
    parts.append("</candidate_duplicates>")
    return "\n".join(parts)


def ask_api(system, message, schema, model):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY is not set")
    body = {
        "model": model,
        "max_tokens": 600,
        "system": system,
        "tools": [schema],
        "tool_choice": {"type": "tool", "name": "triage"},
        "messages": [{"role": "user", "content": message}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(), method="POST"
    )
    req.add_header("x-api-key", key)
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            out = json.loads(resp.read())
    except urllib.error.HTTPError as err:
        raise SystemExit(f"Anthropic API failed: {err.code} {err.read().decode(errors='replace')[:300]}")
    usage = out.get("usage", {})
    log(f"model={model} input_tokens={usage.get('input_tokens')} output_tokens={usage.get('output_tokens')}")
    for block in out.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "triage":
            return block["input"]
    raise SystemExit("model returned no triage call")


def ask_claude_cli(system, message, schema, model):
    """Local dry runs only: the same prompt through the Claude Code CLI."""
    system = (
        system
        + "\n\nThere is no tool in this session. Instead reply with ONLY a JSON object"
        + " (no prose, no code fence) matching this JSON Schema:\n"
        + json.dumps(schema["input_schema"])
    )
    out = subprocess.run(
        ["claude", "-p", "--model", model, "--system-prompt", system, "--tools", "",
         "--output-format", "json", "--no-session-persistence", "--max-turns", "1"],
        input=message, capture_output=True, text=True, timeout=180,
    )
    if out.returncode != 0:
        raise SystemExit(f"claude CLI failed: {out.stderr[:300]}")
    result = json.loads(out.stdout)
    log(f"model={model} cost_usd={result.get('total_cost_usd')}")
    text = result.get("result", "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return json.loads(text)


# --------------------------------------------------------------------- Policy


def keywords(title, limit=4):
    words = re.findall(r"[A-Za-z][A-Za-z0-9+.#-]{2,}", title or "")
    seen = []
    for w in words:
        lw = w.lower().strip(".")
        if lw not in STOPWORDS and lw not in seen:
            seen.append(lw)
    return seen[:limit]


def find_candidates(gh, issue):
    terms = keywords(issue.get("title"))
    found = {}
    queries = [terms[:3]] + [[t] for t in terms] if terms else []
    for q in queries:
        if len(found) >= 8:
            break
        for item in gh.search(q, issue["number"]):
            found.setdefault(item["number"], item)
    return list(found.values())[:8]


def plan(event_name, event, decision, repo_labels, comments, candidates):
    """Turn a decision into a list of actions. Pure: no network."""
    issue = event["issue"]
    have = {l["name"] for l in issue.get("labels", [])}
    bot_bodies = [c.get("body") or "" for c in comments if (c.get("user") or {}).get("type") == "Bot"]
    asked_before = any(MARK_NEEDS_INFO in b for b in bot_bodies)
    duped_before = any(MARK_DUPLICATES in b for b in bot_bodies)
    area_labels = [l for l in repo_labels if l.startswith("area/")]
    candidate_numbers = {c["number"] for c in candidates}

    kind = decision.get("kind")
    confident = decision.get("confidence") == "high"
    add, remove, sections, close = [], [], [], None
    is_bug = "bug" in have or kind == "bug"
    missing = [m for m in decision.get("missing", []) if m in MISSING] if is_bug else []

    if event_name == "issue_comment":
        # The author replied while needs-info is on: only decide whether to lift it.
        if "needs-info" in have and not missing:
            remove.append("needs-info")
        return {"add": add, "remove": remove, "comment": None, "close": None, "reason": decision.get("reason", "")}

    if kind == "spam":
        insider = issue.get("author_association") in INSIDERS
        if confident and not insider and event["action"] == "opened" and "wontfix" in repo_labels:
            add.append("wontfix")
            close = "not_planned"
        return {"add": add, "remove": remove, "comment": None, "close": close, "reason": decision.get("reason", "")}

    if kind in KINDS and not have & set(KINDS) and kind in repo_labels:
        add.append(kind)
    for a in decision.get("areas", [])[:2]:
        if a in area_labels and a not in have:
            add.append(a)

    dupes = [d for d in decision.get("duplicates", []) if d.get("number") in candidate_numbers]
    if any(d["confidence"] == "high" for d in dupes) and "duplicate" not in have and "duplicate" in repo_labels:
        add.append("duplicate")
    if dupes and not duped_before:
        lines = "\n".join(f"- #{d['number']}" for d in dupes[:3])
        sections.append(
            MARK_DUPLICATES + "\nThis looks related to:\n" + lines + "\n\n"
            "If one of these is the same thing, a 👍 there helps us see how many people it affects. "
            "If yours is different, say how and we'll keep it open."
        )

    if is_bug and missing and not asked_before and "needs-info" in repo_labels:
        if "needs-info" not in have:
            add.append("needs-info")
        items = "\n".join(f"- {MISSING_TEXT[m]}" for m in missing)
        sections.append(
            MARK_NEEDS_INFO + "\nThanks for the report! To look into it we need a little more:\n" + items + "\n\n"
            "You can edit the issue or reply here. **Issues are public**, so please leave out "
            "API keys, tokens, and private files or memories."
        )
    elif "needs-info" in have and not missing:
        remove.append("needs-info")

    comment = None
    if sections:
        comment = "\n\n---\n\n".join(sections) + "\n\n<sub>Automated triage — a person reads every issue.</sub>"
    return {"add": add, "remove": remove, "comment": comment, "close": close, "reason": decision.get("reason", "")}


def guard(text):
    """Refuse to post anything that could carry a credential."""
    for name in ("ANTHROPIC_API_KEY", "GITHUB_TOKEN"):
        val = os.environ.get(name)
        if val and val in text:
            raise SystemExit("refusing to post: comment contains a secret")
    if re.search(r"sk-ant-|ghp_|github_pat_|gh[osu]_[A-Za-z0-9]{20}", text):
        raise SystemExit("refusing to post: comment looks like it contains a token")


def apply(gh, number, actions):
    if actions["add"]:
        gh.call("POST", gh.r(f"/issues/{number}/labels"), {"labels": actions["add"]})
    for label in actions["remove"]:
        gh.call("DELETE", gh.r(f"/issues/{number}/labels/{urllib.parse.quote(label)}"))
    if actions["comment"]:
        guard(actions["comment"])
        gh.call("POST", gh.r(f"/issues/{number}/comments"), {"body": actions["comment"]})
    if actions["close"]:
        gh.call("PATCH", gh.r(f"/issues/{number}"), {"state": "closed", "state_reason": actions["close"]})


# ----------------------------------------------------------------------- Main


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", help="offline dry run from a fixture JSON")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    ap.add_argument("--engine", choices=["api", "claude-cli"], default="api")
    ap.add_argument("--model", default=os.environ.get("TRIAGE_MODEL") or "claude-haiku-5-5")
    args = ap.parse_args()

    if args.fixture:
        fx = json.loads(Path(args.fixture).read_text())
        event_name, event = fx["event_name"], fx["event"]
        repo_labels, comments, candidates, gh = fx["labels"], fx.get("comments", []), fx.get("candidates", []), None
        args.dry_run = True
    else:
        event_name = os.environ["GITHUB_EVENT_NAME"]
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        gh = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])

    issue = event["issue"]
    have = {l["name"] for l in issue.get("labels", [])}
    if "pull_request" in issue or issue.get("state") != "open":
        return log("not an open issue; nothing to do")
    if "triaged" in have:
        return log("a person has triaged this issue; leaving it alone")
    if event_name == "issue_comment":
        author = (issue.get("user") or {}).get("login")
        if (event["comment"].get("user") or {}).get("login") != author or "needs-info" not in have:
            return log("comment is not the author answering needs-info; nothing to do")

    if gh:
        limit = int(os.environ.get("TRIAGE_DAILY_LIMIT") or 40)
        used = gh.runs_today("triage.yml")
        if used > limit:
            return log(f"daily limit reached ({used} runs today, limit {limit}); skipping")
        repo_labels = gh.labels()
        comments = gh.comments(issue["number"])
        candidates = find_candidates(gh, issue) if event_name == "issues" else []

    author = (issue.get("user") or {}).get("login")
    author_comments = [c.get("body") or "" for c in comments if (c.get("user") or {}).get("login") == author]
    area_labels = [l for l in repo_labels if l.startswith("area/")]
    system = (HERE / "prompt.md").read_text()
    message = user_message(issue, author_comments, area_labels, candidates)
    schema = tool_schema(area_labels, [c["number"] for c in candidates])
    ask = ask_api if args.engine == "api" else ask_claude_cli
    decision = ask(system, message, schema, args.model)
    actions = plan(event_name, event, decision, repo_labels, comments, candidates)

    print(json.dumps({"issue": issue["number"], "decision": decision, "actions": actions}, indent=2, ensure_ascii=False))
    if not args.dry_run:
        apply(gh, issue["number"], actions)


if __name__ == "__main__":
    main()

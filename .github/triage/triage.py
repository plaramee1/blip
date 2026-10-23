#!/usr/bin/env python3
"""Triage this repository's issues from a machine that polls them.

Nothing runs in GitHub Actions: a timer on a maintainer's machine runs
`triage.py poll`, which reads new and edited issues over the REST API, asks a
local Ollama model to classify each one, and applies the result. See
.github/TRIAGE.md for setup.

The model only classifies (see prompt.md). Everything it may change is decided
here, so the rules hold whatever the issue text says:

- labels come only from the repository's own label set;
- at most one comment per event, built from fixed templates — no model text and
  no issue text is ever posted;
- the needs-info request is posted once per issue, ever;
- nothing is closed except high-confidence spam from someone outside the repo;
- an issue a person has labelled `triaged` is left alone.

Standard library only. Usage:

    triage.py poll --repo OWNER/NAME [--dry-run]   # the timer runs this
    triage.py --fixture F.json                      # offline dry run, prints the plan
    triage.py --fixture F.json --engine claude-cli  # same, through the Claude Code CLI
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
KINDS = ("bug", "feature", "question")
MISSING = ("version", "macos", "steps")
INSIDERS = {"OWNER", "MEMBER", "COLLABORATOR"}
MARK_NEEDS_INFO = "<!-- blip-triage:needs-info -->"
MARK_DUPLICATES = "<!-- blip-triage:duplicates -->"
MARK_MIRROR = "<!-- blip-triage:mirror "
MARK_FIXED = "<!-- blip-triage:fixed -->"
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
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------- GitHub


class GitHub:
    def __init__(self, repo, token):
        self.repo, self.token = repo, token

    def call(self, method, path, body=None, query=None):
        url = path if path.startswith("https://") else f"https://api.github.com{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "blip-triage")
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

    def login(self):
        return self.call("GET", "/user")["login"]

    def labels(self):
        out = self.call("GET", self.r("/labels"), query={"per_page": 100})
        return [l["name"] for l in out]

    def comments(self, number):
        return self.call("GET", self.r(f"/issues/{number}/comments"), query={"per_page": 100})

    def updated_since(self, since):
        page, out = 1, []
        while True:
            batch = self.call("GET", self.r("/issues"), query={
                "state": "all", "since": since, "sort": "updated", "direction": "asc",
                "per_page": 100, "page": page,
            })
            out += [i for i in batch if "pull_request" not in i]
            if len(batch) < 100:
                return out
            page += 1

    def search(self, terms, exclude):
        q = f"repo:{self.repo} is:issue " + " ".join(terms)
        out = self.call("GET", "/search/issues", query={"q": q, "per_page": 10})
        return [i for i in out.get("items", []) if i["number"] != exclude and "pull_request" not in i]


# ---------------------------------------------------------------------- Model


def schema(area_labels):
    return {
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


def parse_json(text):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return json.loads(text)


def think_setting(model):
    """TRIAGE_THINK overrides. Measured on the fixtures: gpt-oss returns empty
    content with thinking off, and qwen3 misses details given in comments
    without it."""
    raw = (os.environ.get("TRIAGE_THINK") or "").strip().lower()
    if raw in ("true", "false"):
        return raw == "true"
    if raw in ("low", "medium", "high"):
        return raw
    if "gpt-oss" in model:
        return "low"
    return model.startswith("qwen3:")


def ask_ollama(system, message, shape, model):
    url = (os.environ.get("OLLAMA_URL") or "http://localhost:11434").rstrip("/") + "/api/chat"
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": message}],
        "format": shape,
        "stream": False,
        "think": think_setting(model),
        "options": {"temperature": 0},
    }

    def post(payload):
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read())

    try:
        out = post(body)
    except urllib.error.HTTPError as err:
        detail = err.read().decode(errors="replace")[:300]
        if "think" not in detail:
            raise SystemExit(f"Ollama failed: {err.code} {detail}")
        body.pop("think")  # a model without a thinking switch rejects the field
        out = post(body)
    except urllib.error.URLError as err:
        raise SystemExit(f"Ollama is not reachable at {url}: {err.reason}")
    secs = (out.get("total_duration") or 0) / 1e9
    log(f"model={model} prompt_tokens={out.get('prompt_eval_count')} output_tokens={out.get('eval_count')} seconds={secs:.1f}")
    return parse_json(out["message"]["content"])


def ask_claude_cli(system, message, shape, model):
    """Local dry runs only: the same prompt through the Claude Code CLI."""
    system += "\n\nReply with ONLY a JSON object (no prose, no code fence) matching this JSON Schema:\n" + json.dumps(shape)
    out = subprocess.run(
        ["claude", "-p", "--model", model, "--system-prompt", system, "--tools", "",
         "--output-format", "json", "--no-session-persistence", "--max-turns", "1"],
        input=message, capture_output=True, text=True, timeout=180,
    )
    if out.returncode != 0:
        raise SystemExit(f"claude CLI failed: {out.stderr[:300]}")
    return parse_json(json.loads(out.stdout).get("result", ""))


ENGINES = {"ollama": ask_ollama, "claude-cli": ask_claude_cli}


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


def is_ours(comment, bot_login):
    user = comment.get("user") or {}
    return user.get("type") == "Bot" or (bot_login and user.get("login") == bot_login)


def plan(event_name, event, decision, repo_labels, comments, candidates, bot_login=None):
    """Turn a decision into a list of actions. Pure: no network."""
    issue = event["issue"]
    have = {l["name"] for l in issue.get("labels", [])}
    ours = [c.get("body") or "" for c in comments if is_ours(c, bot_login)]
    asked_before = any(MARK_NEEDS_INFO in b for b in ours)
    duped_before = any(MARK_DUPLICATES in b for b in ours)
    area_labels = [l for l in repo_labels if l.startswith("area/")]
    candidate_numbers = {c["number"] for c in candidates}

    kind = decision.get("kind")
    confident = decision.get("confidence") == "high"
    reason = decision.get("reason", "")
    add, remove, sections, close = [], [], [], None
    is_bug = "bug" in have or kind == "bug"
    missing = [m for m in decision.get("missing", []) if m in MISSING] if is_bug else []

    if event_name == "issue_comment":
        # The author replied while needs-info is on: only decide whether to lift it.
        if "needs-info" in have and not missing:
            remove.append("needs-info")
        return {"add": add, "remove": remove, "comment": None, "close": None, "reason": reason}

    if kind == "spam":
        insider = issue.get("author_association") in INSIDERS
        if confident and not insider and event["action"] == "opened" and "wontfix" in repo_labels:
            add.append("wontfix")
            close = "not_planned"
        return {"add": add, "remove": remove, "comment": None, "close": close, "reason": reason}

    if kind in KINDS and not have & set(KINDS) and kind in repo_labels:
        add.append(kind)
    for a in decision.get("areas", [])[:2]:
        if a in area_labels and a not in have:
            add.append(a)

    dupes = [d for d in decision.get("duplicates", []) if d.get("number") in candidate_numbers]
    # Only an open match earns the label: a closed one may be fixed, and a
    # report against a newer version is a regression, not a duplicate.
    open_numbers = {c["number"] for c in candidates if c.get("state") == "open"}
    certain = [d for d in dupes if d["confidence"] == "high" and d["number"] in open_numbers]
    if certain and "duplicate" not in have and "duplicate" in repo_labels:
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
    return {"add": add, "remove": remove, "comment": comment, "close": close, "reason": reason}


def guard(text):
    """Refuse to post anything that could carry a credential."""
    for name in ("GH_TOKEN", "MIRROR_TOKEN"):
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


def triage(event_name, event, repo_labels, comments, candidates, ask, model, bot_login=None):
    issue = event["issue"]
    author = (issue.get("user") or {}).get("login")
    author_comments = [
        c.get("body") or "" for c in comments
        if (c.get("user") or {}).get("login") == author and not is_ours_marked(c)
    ]
    area_labels = [l for l in repo_labels if l.startswith("area/")]
    system = (HERE / "prompt.md").read_text()
    message = user_message(issue, author_comments, area_labels, candidates)
    decision = ask(system, message, schema(area_labels), model)
    return decision, plan(event_name, event, decision, repo_labels, comments, candidates, bot_login)


def is_ours_marked(comment):
    body = comment.get("body") or ""
    return MARK_NEEDS_INFO in body or MARK_DUPLICATES in body


# --------------------------------------------------------------------- Mirror


def mirror(gh, issue, state_entry, dry_run):
    """Open a linked issue in the private tracker once an issue is `accepted`.

    Inert unless MIRROR_TOKEN and MIRROR_REPO are both set. Nothing is posted
    back to the public issue.
    """
    token, target = os.environ.get("MIRROR_TOKEN"), os.environ.get("MIRROR_REPO")
    if not (token and target) or state_entry.get("mirrored"):
        return
    priv = GitHub(target, token)
    marker = f"{MARK_MIRROR}{gh.repo}#{issue['number']} -->"
    found = priv.call("GET", "/search/issues", query={"q": f'repo:{target} is:issue in:body "{gh.repo}#{issue["number"]}"'})
    for i in found.get("items", []):
        if marker in (i.get("body") or ""):
            state_entry["mirrored"], state_entry["mirror"] = True, i["number"]
            return
    body = f"{marker}\nMirrored from the public board: {issue['html_url']}\n\n---\n\n{issue.get('body') or ''}"
    log(f"#{issue['number']}: mirroring to the private tracker")
    if not dry_run:
        made = priv.call("POST", priv.r("/issues"), {"title": issue["title"], "body": body})
        state_entry["mirrored"], state_entry["mirror"] = True, made["number"]


def find_mirror(priv, public_repo, number):
    """The private issue mirrored from a public one, for state written before
    the number was recorded."""
    marker = f"{MARK_MIRROR}{public_repo}#{number} -->"
    found = priv.call("GET", "/search/issues", query={"q": f'repo:{priv.repo} is:issue in:body "{public_repo}#{number}"'})
    return next((i["number"] for i in found.get("items", []) if marker in (i.get("body") or "")), None)


# ---------------------------------------------------------------------- Fixed


CLOSER_QUERY = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      state
      stateReason
      timelineItems(itemTypes: [CLOSED_EVENT], last: 1) {
        nodes { ... on ClosedEvent { closer {
          __typename
          ... on PullRequest { number merged mergedAt mergeCommit { oid } }
          ... on Commit { oid committedDate }
        } } }
      }
    }
  }
}"""


def fixing_commit(priv, number):
    """(sha, ISO date) of the commit that closed a private issue as completed —
    the merge commit of its fixing PR, or the commit that closed it — or None
    if it is still open, was closed by hand, or was closed as not planned."""
    owner, name = priv.repo.split("/", 1)
    out = priv.call("POST", "/graphql", {"query": CLOSER_QUERY, "variables": {"owner": owner, "name": name, "number": number}})
    issue = ((out or {}).get("data") or {}).get("repository", {}).get("issue") or {}
    return closer_commit(issue)


def closer_commit(issue):
    """Pure half of fixing_commit: read the GraphQL issue node."""
    if issue.get("state") != "CLOSED" or issue.get("stateReason") not in (None, "COMPLETED"):
        return None
    nodes = (issue.get("timelineItems") or {}).get("nodes") or []
    closer = (nodes[-1] if nodes else {}).get("closer") or {}
    if closer.get("__typename") == "PullRequest":
        oid = (closer.get("mergeCommit") or {}).get("oid")
        return (oid, closer.get("mergedAt") or "") if closer.get("merged") and oid else None
    return (closer["oid"], closer.get("committedDate") or "") if closer.get("oid") else None


def published(releases):
    """Published, non-prerelease releases, oldest first."""
    out = [r for r in releases if not r.get("draft") and not r.get("prerelease") and r.get("published_at")]
    return sorted(out, key=lambda r: r["published_at"])


def contains(priv, tag, sha):
    """True when the private repo's tag has the commit in its history."""
    out = priv.call("GET", priv.r(f"/compare/{urllib.parse.quote(tag, safe='')}...{sha}"))
    return out.get("status") in ("behind", "identical")


def shipping_release(public_releases, private_releases, contained, allow_private=False, since=""):
    """Pick the first release that ships the fix. Pure apart from `contained`
    (tag -> bool), which is asked oldest first and only as far as needed.

    Release tags are the same in both repositories; the commit lives in the
    private one, so containment is always checked there. A public release is
    required unless allow_private is set, in which case a private release
    counts while no public release carries the tag yet (the public channel is
    held behind BLIP_PUBLISH_PUBLIC). Releases published before `since` (the
    fix's merge date) cannot hold it and are not asked about."""
    public = {r["tag_name"]: r for r in published(public_releases)}
    for rel in published(private_releases):
        tag = rel["tag_name"]
        if since and rel["published_at"] < since:
            continue
        if tag not in public and not allow_private:
            continue
        if contained(tag):
            return public.get(tag) or {**rel, "html_url": None}
    return None


def fixed_plan(issue, comments, release, repo_labels, bot_login=None):
    """The labels and comment that say a public issue's fix has shipped. Pure.

    Posted once ever: nothing if our marker is already in a comment, or if a
    person has already labelled the issue `fixed`."""
    have = {l["name"] for l in issue.get("labels", [])}
    ours = [c.get("body") or "" for c in comments if is_ours(c, bot_login)]
    if not release or "fixed" in have or any(MARK_FIXED in b for b in ours):
        return None
    tag = release["tag_name"]
    shipped = f"[{tag}]({release['html_url']})" if release.get("html_url") else f"**{tag}**"
    get = " Blip updates itself (Settings → Updates)" + (", or download it from the release." if release.get("html_url") else ".")
    comment = (
        MARK_FIXED + f"\nFixed in {shipped}.{get} If it still happens on this version, "
        "reply here and we'll take another look."
        "\n\n<sub>Automated triage — a person reads every issue.</sub>"
    )
    add = ["fixed"] if "fixed" in repo_labels else []
    return {"add": add, "remove": [], "comment": comment, "close": None, "reason": f"shipped in {tag}"}


def check_fixed(gh, priv, key, entry, repo_labels, bot_login, cache, dry_run):
    """Comment `Fixed in vX.Y` once the mirrored private issue's fix ships.

    Runs for every mirrored issue not yet marked fixed, on every poll: shipping
    happens in the private repo, so the public issue need not have changed.
    Costs one GraphQL call per pending issue until its private issue closes.
    `cache` holds the release lists for one poll."""
    number = entry.get("mirror") or find_mirror(priv, gh.repo, int(key))
    if not number:
        return
    entry["mirror"] = number
    fix = fixing_commit(priv, number)
    if not fix:
        return
    sha, when = fix
    if "releases" not in cache:
        cache["releases"] = (
            gh.call("GET", gh.r("/releases"), query={"per_page": 100}),
            priv.call("GET", priv.r("/releases"), query={"per_page": 100}),
        )
    allow_private = os.environ.get("FIXED_ALLOW_PRIVATE") == "1"
    release = shipping_release(*cache["releases"], lambda tag: contains(priv, tag, sha), allow_private, since=when)
    if not release:
        return
    issue = gh.call("GET", gh.r(f"/issues/{key}"))
    comments = gh.comments(int(key)) if issue.get("comments") else []
    actions = fixed_plan(issue, comments, release, repo_labels, bot_login)
    if actions:
        print(json.dumps({"issue": issue["number"], "event": "fixed", "actions": actions}, ensure_ascii=False), flush=True)
        if dry_run:
            return
        apply(gh, issue["number"], actions)
    entry["fixed"] = True  # posted now, or already said by us or a person


# ----------------------------------------------------------------------- Poll


def state_path():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "blip-triage" / "state.json"


def load_state(path):
    if path.exists():
        return json.loads(path.read_text())
    return {"since": None, "issues": {}, "day": None, "calls": 0}


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(path)


def content_hash(issue):
    return hashlib.sha256(f"{issue.get('title')}\0{issue.get('body')}".encode()).hexdigest()[:16]


def poll(args, ask):
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise SystemExit("GH_TOKEN is not set")
    gh = GitHub(args.repo, token)
    path = state_path()
    state = load_state(path)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("day") != today:
        state["day"], state["calls"] = today, 0
    limit = int(os.environ.get("TRIAGE_DAILY_LIMIT") or 40)
    since = state.get("since") or (datetime.now(timezone.utc) - timedelta(days=args.lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    issues = gh.updated_since(since)
    log(f"{len(issues)} issue(s) updated since {since}")
    pending = [k for k, e in state["issues"].items() if e.get("mirrored") and not e.get("fixed")]
    if not issues and not (pending and os.environ.get("MIRROR_TOKEN") and os.environ.get("MIRROR_REPO")):
        return
    bot_login, repo_labels = gh.login(), gh.labels()

    for issue in issues:
        key = str(issue["number"])
        entry = state["issues"].setdefault(key, {})
        have = {l["name"] for l in issue.get("labels", [])}
        if "accepted" in have:
            mirror(gh, issue, entry, args.dry_run)

        # Work out which events this issue has had since we last handled it.
        event_name = None
        h = content_hash(issue)
        comments = gh.comments(issue["number"]) if issue.get("comments") else []
        author = (issue.get("user") or {}).get("login")
        replies = [c for c in comments if (c.get("user") or {}).get("login") == author and not is_ours_marked(c)]
        last_reply = max((c["id"] for c in replies), default=0)
        if "hash" not in entry:
            event_name, action = "issues", "opened"
        elif entry["hash"] != h:
            event_name, action = "issues", "edited"
        elif last_reply > entry.get("last_reply", 0) and "needs-info" in have:
            event_name, action = "issue_comment", "created"

        if event_name and issue["state"] == "open" and "triaged" not in have:
            if state["calls"] >= limit:
                log(f"daily limit reached ({limit} model calls); stopping until tomorrow")
                break
            state["calls"] += 1
            event = {"action": action, "issue": issue}
            candidates = find_candidates(gh, issue) if event_name == "issues" else []
            decision, actions = triage(event_name, event, repo_labels, comments, candidates, ask, args.model, bot_login)
            print(json.dumps({"issue": issue["number"], "event": f"{event_name}.{action}",
                              "decision": decision, "actions": actions}, ensure_ascii=False), flush=True)
            if not args.dry_run:
                apply(gh, issue["number"], actions)

        if not args.dry_run:
            entry["hash"], entry["last_reply"] = h, last_reply
            state["since"] = max(state.get("since") or "", issue["updated_at"])
            save_state(path, state)  # after each issue, so a crash never repeats a comment

    # Shipping happens in the private repo, so look at every mirrored issue
    # still waiting for its fix, whether or not it changed here.
    token, target = os.environ.get("MIRROR_TOKEN"), os.environ.get("MIRROR_REPO")
    if token and target:
        priv, cache = GitHub(target, token), {}
        for key, entry in state["issues"].items():
            if entry.get("mirrored") and not entry.get("fixed"):
                check_fixed(gh, priv, key, entry, repo_labels, bot_login, cache, args.dry_run)
                if not args.dry_run:
                    save_state(path, state)

    if not args.dry_run:
        save_state(path, state)


def fixed_from_fixture(fx, allow_private=None):
    """Offline `Fixed in` plan for a fixture: the closer and the tags that
    contain the fix stand in for the private repository."""
    if allow_private is None:
        allow_private = os.environ.get("FIXED_ALLOW_PRIVATE") == "1"
    node = {"state": "CLOSED", "stateReason": "COMPLETED", "timelineItems": {"nodes": [{"closer": fx["closer"]}]}}
    fix = closer_commit(node)
    if not fix:
        return {"issue": fx["issue"]["number"], "release": None, "actions": None}
    release = shipping_release(fx["public_releases"], fx["private_releases"],
                               lambda tag: tag in fx["contained_tags"], allow_private, since=fix[1])
    actions = fixed_plan(fx["issue"], fx["comments"], release, fx["labels"], fx.get("bot_login"))
    return {"issue": fx["issue"]["number"], "release": release and release["tag_name"], "actions": actions}


# ----------------------------------------------------------------------- Main


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", choices=["poll"], help="poll the repository (the timer runs this)")
    ap.add_argument("--repo", help="OWNER/NAME to poll")
    ap.add_argument("--fixture", help="offline dry run from a fixture JSON")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    ap.add_argument("--engine", choices=sorted(ENGINES), default=os.environ.get("TRIAGE_ENGINE") or "ollama")
    ap.add_argument("--model", default=os.environ.get("TRIAGE_MODEL") or "gpt-oss:20b")
    ap.add_argument("--lookback-days", type=int, default=7, help="first poll only: how far back to look")
    args = ap.parse_args()
    ask = ENGINES[args.engine]

    if args.command == "poll":
        if not args.repo:
            ap.error("poll needs --repo OWNER/NAME")
        return poll(args, ask)
    if not args.fixture:
        ap.error("give a command (poll) or --fixture")

    fx = json.loads(Path(args.fixture).read_text())
    if fx.get("kind") == "fixed":
        return print(json.dumps(fixed_from_fixture(fx), indent=2, ensure_ascii=False))
    event = fx["event"]
    issue = event["issue"]
    have = {l["name"] for l in issue.get("labels", [])}
    if issue.get("state") != "open" or "triaged" in have:
        return log("closed or already triaged; nothing to do")
    decision, actions = triage(fx["event_name"], event, fx["labels"], fx.get("comments", []),
                               fx.get("candidates", []), ask, args.model)
    print(json.dumps({"issue": issue["number"], "decision": decision, "actions": actions}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

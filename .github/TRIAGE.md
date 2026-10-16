# Issue triage

Two workflows run on this repository's issues.

## Triage (`workflows/triage.yml`)

Runs when an issue is opened or edited, and when the issue's author comments
while it has `needs-info`. It calls Claude once per event (no tools, a single
request capped at 600 output tokens) to classify the issue, and
`triage/triage.py` turns that answer into changes:

- adds `bug`, `feature` or `question` if the issue has none of them yet, and up
  to two `area/*` labels from the repository's label set;
- searches open and closed issues for likely duplicates and lists up to three in
  a comment, once per issue; adds `duplicate` only when the match is certain;
- for a bug missing the Blip version, macOS version or steps, adds `needs-info`
  and asks for what is missing — once per issue, ever;
- removes `needs-info` when the body or the author's comments supply it;
- closes an issue (as not planned, labelled `wontfix`) only when it is
  unmistakable spam from someone outside the repository.

It posts at most one comment per event, built from fixed templates: no text from
the model or the issue is ever posted, so it cannot promise dates or repeat what
someone pasted. It leaves an issue alone once a person adds `triaged`, and it
never removes `bug`/`feature`/`question` or any label a person set.

Cost: one run per issue at a time (concurrency group), and the run is skipped
once the workflow has run `TRIAGE_DAILY_LIMIT` times that UTC day.

### Setup

| Kind | Name | Value |
|---|---|---|
| Secret | `ANTHROPIC_API_KEY` | An Anthropic API key, ideally from a workspace with a monthly spend limit. Without it the workflow does nothing. |
| Variable (optional) | `TRIAGE_MODEL` | Model id. Default `claude-haiku-5-5`. |
| Variable (optional) | `TRIAGE_DAILY_LIMIT` | Runs per UTC day before triage pauses. Default `40`. |

### Trying it locally

`triage/fixtures/` holds synthetic events. A dry run prints the decision and the
planned actions and changes nothing:

```sh
ANTHROPIC_API_KEY=… python3 .github/triage/triage.py --fixture .github/triage/fixtures/1-bug-missing-info.json
# or, with the Claude Code CLI instead of an API key:
python3 .github/triage/triage.py --fixture .github/triage/fixtures/1-bug-missing-info.json --engine claude-cli
```

## Mirror accepted issues (`workflows/mirror-accepted.yml`)

When a maintainer labels an issue `accepted`, opens a linked issue in the
private tracker (title, body and a link back), once per public issue. Nothing is
posted back here. It is inert until both of these exist:

| Kind | Name | Value |
|---|---|---|
| Secret | `MIRROR_TOKEN` | A fine-grained personal access token with **Issues: read and write** on the private tracker repository **only**. |
| Variable | `MIRROR_REPO` | `owner/name` of the private tracker. |

# Issue triage

Issues are triaged by a small poller on a maintainer's Linux machine, not by
GitHub Actions. This repository has no workflows and no secrets: nothing a
stranger posts here can run code anywhere, and the only credential lives on
the machine that polls.

Every 5 minutes a systemd user timer runs `triage/triage.py poll`. It reads the
issues updated since its last run over the GitHub REST API, asks a local
[Ollama](https://ollama.com) model to classify each new or edited issue (and
each reply from the author while `needs-info` is on), and applies the result:

- adds `bug`, `feature` or `question` if the issue has none of them yet, and up
  to two `area/*` labels from the repository's label set;
- searches open and closed issues for likely duplicates and lists up to three in
  a comment, once per issue; adds `duplicate` only for a certain match with an
  **open** issue (a closed match may be fixed, or this may be a regression);
- for a bug missing the Blip version, macOS version or steps, adds `needs-info`
  and asks for what is missing — once per issue, ever;
- removes `needs-info` when the body or the author's comments supply it;
- closes an issue (as not planned, labelled `wontfix`) only when it is
  unmistakable spam from someone outside the repository.

The model only classifies. It posts at most one comment per event, built from
fixed templates: no text from the model or the issue is ever posted, so it
cannot promise dates or repeat what someone pasted. It leaves an issue alone
once a person adds `triaged`, and never removes `bug`/`feature`/`question` or
any label a person set.

The poller keeps its place in `~/.local/state/blip-triage/state.json` (last
`updated_at` seen, and per issue a hash of the title and body plus the last
author reply handled), so each event is handled once and a re-run changes
nothing. It stops for the day after `TRIAGE_DAILY_LIMIT` model calls (default
40) and picks up where it left off the next day.

## Mirroring accepted issues

When an issue carries `accepted`, the poller opens a linked issue in the
private tracker (title, body and a link back), once per public issue, and posts
nothing here. It is off unless `MIRROR_TOKEN` and `MIRROR_REPO` are both set in
the env file. It lives in the poller rather than a workflow so that the token
for the private tracker never has to be stored in this public repository.

## Install on the polling machine

Needs Python 3 (standard library only), Ollama running as a user service on
`localhost:11434` with the model pulled, and lingering enabled for the user
(`loginctl enable-linger $USER`) so the timer runs while logged out.

```sh
# from a checkout of this repository
install -d ~/.local/share/blip-triage ~/.config/systemd/user ~/.config/blip-triage
install -m 644 .github/triage/triage.py .github/triage/prompt.md ~/.local/share/blip-triage/
install -m 644 deploy/rig/blip-triage.service deploy/rig/blip-triage.timer ~/.config/systemd/user/
install -m 600 deploy/rig/env.example ~/.config/blip-triage/env
$EDITOR ~/.config/blip-triage/env          # paste the real GH_TOKEN

ollama pull gpt-oss:20b                    # if not already present

# try it without changing anything, then turn it on
set -a; . ~/.config/blip-triage/env; set +a
python3 ~/.local/share/blip-triage/triage.py poll --repo plaramee1/blip --dry-run
systemctl --user daemon-reload
systemctl --user enable --now blip-triage.timer

# watch it
systemctl --user list-timers blip-triage.timer
journalctl --user -u blip-triage -f
```

To update, repeat the two `install -m 644` lines and `systemctl --user daemon-reload`.
To stop: `systemctl --user disable --now blip-triage.timer`. The first run looks
back 7 days; delete the state file to start over.

### Settings (`~/.config/blip-triage/env`)

| Name | Required | Value |
|---|---|---|
| `GH_TOKEN` | yes | Fine-grained personal access token: repository access **this repository only**, permission **Issues: Read and write**. |
| `TRIAGE_MODEL` | no | Ollama model. Default `gpt-oss:20b`. |
| `TRIAGE_THINK` | no | `true`/`false`/`low`/`medium`/`high`. Default `low` for gpt-oss, on for `qwen3:*`, off otherwise. |
| `OLLAMA_URL` | no | Default `http://localhost:11434`. |
| `TRIAGE_DAILY_LIMIT` | no | Model calls per UTC day. Default `40`. |
| `MIRROR_TOKEN` | no | Fine-grained token, **the private tracker only**, Issues: Read and write. |
| `MIRROR_REPO` | no | `owner/name` of the private tracker. |

### Choosing the model

Measured on the fixtures below (local GPU, temperature 0):

| Model | Thinking | Fixtures right | Time per issue |
|---|---|---|---|
| `gpt-oss:20b` | low | 4/4 | 0.3–1 s (first call loads the model) |
| `qwen3:14b` | on | 4/4, but adds a stray area label | 4–12 s |
| `qwen3:14b` | off | 3/4 — misses details the author gave in a comment | ~1 s |
| `qwen3-coder:30b` | off | 4/4 | 1–2 s |

## Trying it on the fixtures

`triage/fixtures/` holds synthetic events. A fixture run prints the decision
and the planned actions and never touches GitHub:

```sh
python3 .github/triage/triage.py --fixture .github/triage/fixtures/1-bug-missing-info.json
python3 .github/triage/triage.py --fixture .github/triage/fixtures/1-bug-missing-info.json --engine claude-cli
```

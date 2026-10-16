# Security policy

## Reporting a vulnerability

**Please don't open a public issue for a security problem.**

Report it privately through GitHub's security advisories: open the repository's
**Security** tab and choose **Report a vulnerability**
(<https://github.com/plaramee1/blip/security/advisories/new>). Only the
maintainers can see the report.

Please include:

- what an attacker can do, and what they need first (a malicious file in a
  store, a crafted terminal output, a local process, and so on);
- steps or a proof of concept to reproduce it;
- the Blip version (Settings → About) and your macOS version.

We'll acknowledge the report within a week, keep you updated as we fix it, and
credit you in the advisory unless you'd rather not be named.

## Supported versions

Only the latest release gets security fixes.

## In scope

Blip runs agent CLIs, renders untrusted output, and stores credentials, so these
areas matter most:

- **Keychain secrets.** API keys and tokens Blip keeps in the macOS keychain:
  anything that reads them without the user's intent, writes them to disk,
  a log, a store or a transcript in the clear, or passes them to a process that
  shouldn't have them.
- **The agent process environment.** How Blip launches agent CLIs and tools: the
  environment, arguments and working directory they inherit, and any way for a
  store, a conversation or a model's output to inject arguments, environment
  variables or commands into a spawned process.
- **Terminal escape handling.** The terminal emulator's handling of **OSC 52**
  (clipboard) and **OSC 8** (hyperlinks), plus any other escape sequence that lets
  program output read or write the clipboard, open a URL or a file, or run
  something without the user asking for it.
- **The approval listener.** The channel through which an agent asks Blip to
  approve a tool call (`blip-approver` and the app's end of it): anything that
  lets another local process approve, forge or replay an approval, or get a tool
  to run without one.

Reports about the rest of the app are welcome too, for example path traversal
through a memory or wiki link, or a crafted store that crashes or takes over
the app.

## Out of scope

- Vulnerabilities in the agent CLIs themselves (Claude Code, Codex, Cursor,
  Ollama). Report those to their own projects.
- Anything that requires an attacker who already runs code as your user with
  full disk access.
- Copies of Blip that were not downloaded from this repository's releases or
  installed by Blip's own updater.

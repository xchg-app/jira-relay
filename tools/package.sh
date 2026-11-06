#!/usr/bin/env bash
# Builds an installable package for one harness: the harness files from harness/<name>/
# plus real copies of what every package carries (the jira client and the skill from client/).
# The package repositories hold exactly this output; nothing there is written by hand.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
usage() { echo "usage: tools/package.sh <claude-code|codex> <output directory>" >&2; exit 2; }

name="${1:-}"; out="${2:-}"
[ -n "$name" ] && [ -n "$out" ] || usage
src="$ROOT/harness/$name"
[ -d "$src" ] || { echo "no harness $name in $ROOT/harness" >&2; exit 1; }

case "$name" in
  claude-code) title="Claude Code plugin"; manifest=".claude-plugin/plugin.json"
    install='```
/plugin marketplace add xchg-app/jira-relay-claude-code
/plugin install jira-relay@jira-relay
```' ;;
  codex) title="Codex CLI plugin"; manifest=".codex-plugin/plugin.json"
    install='```bash
codex plugin marketplace add xchg-app/jira-relay-codex
codex plugin add jira-relay@jira-relay
```' ;;
  *) usage ;;
esac

rm -rf "$out"; mkdir -p "$out"
# the harness files: manifests
(cd "$src" && find . -mindepth 1 -maxdepth 1 -exec cp -R {} "$out/" \;)
# the shared part, dereferenced: a package must stand on its own
for shared in bin skills; do cp -RL "$ROOT/client/$shared" "$out/$shared"; done
cp "$ROOT/LICENSE" "$out/LICENSE"
find "$out" -name __pycache__ -prune -exec rm -rf {} +
# the package is installed by a plain git clone: with core.autocrlf=true the client would arrive
# with CRLF and fail on "python3\r", so the package repository pins LF itself
printf '* text=auto eol=lf\n' > "$out/.gitattributes"

version=$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$out/$manifest" | head -1)
[ -n "$version" ] || { echo "no version in $manifest" >&2; exit 1; }

cat > "$out/README.md" <<EOR
# jira-relay — $title

Jira Server/DC for coding agents through a self-hosted [jira-relay](https://github.com/xchg-app/jira-relay)
service: read freely, change on your human's command, watch issues into xchg hubs.

$install

Then, once in a terminal: \`jira setup https://HOST:8443\` (the jira-relay gateway; compare the certificate
fingerprint it prints with the one the admin announced) and \`jira register\` with your own Jira personal
access token. The service itself is set up by its admin: see the jira-relay README.

## This repository is generated

Every file here is built from [xchg-app/jira-relay](https://github.com/xchg-app/jira-relay) and
overwritten on each release, so changes made here are lost. Issues and pull requests belong in that
repository; this one only ships version $version of the package.

[MIT](LICENSE) © Nikolay Pronchev.
EOR

echo "$name $version -> $out"

# Vendored Claude Code plugin marketplace

`claude-code-review.yml` installs the `code-review` plugin from this directory
(`plugin_marketplaces: ./.claude/review-marketplace`) instead of from
`https://github.com/anthropics/claude-code.git`. That URL is the upstream default
branch, so every review run would execute whatever the plugin said at that moment,
in a job holding `CLAUDE_CODE_OAUTH_TOKEN` and a PR-write token. The action only
accepts a marketplace URL ending in `.git`, so it cannot be pinned to a ref; a
local path can be.

**It lives under `.claude/` on purpose.** The review job checks out the PR, so a
copy anywhere else would be the PR's own copy: a PR could rewrite the review
instructions or the command's `allowed-tools` and have them run with the job's
credentials. On pull-request events the action deletes the checkout's `.claude/`
and restores it from the PR's base branch before it installs plugins
(`restoreConfigFromBase` in anthropics/claude-code-action; the PR's version is
kept, never executed, in `.claude-pr/`). A change to these files therefore only
takes effect once it has been merged. A PR that changes them is reviewed with
the base branch's copy, and the PR that first adds this directory has no base
copy to use, so its own review cannot install the plugin.

**What this does not cover: a PR that edits the workflow itself.** On
`pull_request` events GitHub runs the PR's own copy of `claude-code-review.yml`,
so a PR can change its inputs (`plugin_marketplaces`, `plugins`, `prompt`) or its
steps. Where the action authenticates through the Claude GitHub app (OIDC, as in
Dinify-Frontend), it refuses to run a workflow file that differs from the default
branch's copy. Where it is handed `github.token` instead (Dinify-Admin, which
keeps OIDC out of these jobs), there is no such check. Neither layout keeps
`CLAUDE_CODE_OAUTH_TOKEN` from someone with write access: GitHub makes repository
secrets usable by every collaborator with write access, through any workflow on
any branch, with or without a PR. A workflow change is a change like any other,
visible in the diff and reviewed before it merges. What this directory's
placement guarantees is narrower: a PR that leaves the workflow alone cannot
change the review it is given.

Source: `anthropics/claude-code` at `7779afb12e3635f46f56ec823979d68350ae000b`,
files copied byte for byte:

- `plugins/code-review/.claude-plugin/plugin.json`
- `plugins/code-review/commands/code-review.md`

`.claude-plugin/marketplace.json` is upstream's, cut down to the `code-review`
entry and renamed `dinify-claude-plugins` (with our own `owner`): Claude Code
reserves the upstream name `claude-code-plugins` for GitHub sources in the
`anthropics` organisation and refuses to add a local marketplace under it. The
workflow therefore installs `code-review@dinify-claude-plugins`; the command is
still `/code-review:code-review`.

To update: fetch a newer upstream commit, review the diff of those two files,
copy them in, and change the commit named here and in `marketplace.json`.

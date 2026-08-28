# HORIZON BOT GitHub Mode

HORIZON BOT GitHub Mode makes HORIZON BOT behave like a safe code assistant for its own repository.
It does **not** edit the live Railway container. It reads files from GitHub, creates an approval card in Discord DM, then after approval pushes a new branch and optionally opens a pull request.

## Flow

```txt
Owner/Admin DM
  ↓
HORIZON BOT reads selected GitHub files
  ↓
AI creates a minimal patch proposal
  ↓
Owner approves with: approve patch <id>
  ↓
HORIZON BOT creates branch ai-horizon/<id>
  ↓
HORIZON BOT commits changed files
  ↓
HORIZON BOT creates a PR to the base branch
```

## Example DM prompts

```txt
github status
cari file game_commands
baca file cogs/game_commands.py
perbaiki error /gen session belum siap
perbaiki cogs/ai_chat.py: pertanyaan normal jangan dipaksa masuk konteks database
approve patch a1b2c3
reject patch a1b2c3
```

## Required environment variables

```env
AI_GITHUB_ENABLED=true
AI_GITHUB_TOKEN=${GITHUB_TOKEN}
AI_GITHUB_REPO=owner/repo
AI_GITHUB_BASE_BRANCH=main
AI_GITHUB_PROVIDER=${AI_CHAT_PROVIDER}
AI_GITHUB_MODEL=${AI_CHAT_MODEL}
AI_GITHUB_ALLOW_APPLY=true
AI_GITHUB_CREATE_PR=true
```

Use a GitHub fine-grained token limited to the HORIZON BOT repository. Recommended permissions:

```txt
Contents: Read and write
Pull requests: Read and write
Metadata: Read
```

## Safety boundaries

- No live Railway file edits.
- No direct deploy command.
- No secret/env reading.
- No code change without `approve patch <id>`.
- New branch per approved proposal.
- Pull request review before merge.

## Recommended deployment behavior

Use Railway connected to GitHub. Merge the PR only after review. Railway can redeploy from the GitHub branch/main depending on your existing Railway setup.

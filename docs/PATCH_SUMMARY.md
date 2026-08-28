# Patch Summary

Changes applied to this ZIP:

1. Added role-based AI model routing in `config.py` and `.env.example`.
   - GPT remains the main/default model: `gpt-oss:120b-cloud`.
   - Background monitoring defaults to `qwen3.5:cloud`.
   - Coding/GitHub helper defaults are documented as `kimi-k2.6:cloud`.
   - Runtime chat/caretaker calls retry once using `AI_MODEL_FALLBACK`.

2. Improved approval reliability in `cogs/ai_operator.py`.
   - Supports `approve latest`, `lanjut`, `gas`, `approve all`, `reject latest`, and `reject all`.
   - Latest approval now resolves to the newest pending proposal instead of failing when multiple proposals exist.

3. Kept server-changing actions behind valid proposals.
   - Operator still only executes whitelisted actions after owner approval.
   - Existing contextual follow-up proposal logic is preserved.

4. Changed announcements to plain Discord messages in `cogs/server_admin.py`.
   - Announcements are no longer sent as embeds.
   - Long announcements are split into safe plain-message chunks.

5. Prevented duplicate channel creation.
   - If a requested text channel already exists, HORIZON BOT no longer creates a duplicate.
   - If a topic was supplied, the existing channel topic is updated instead.

6. Cleaned attachment text before public posting.
   - Strips markers such as `[Attachment: file.txt]`, `[Attachment content]`, and `[Attachment notes]` before using attachment text for rules/announcement content.

7. Added documentation.
   - `docs/AI_MODEL_ROUTER.md`
   - `docs/PATCH_SUMMARY.md`

Packaging notes:

- `.env` was intentionally removed from the returned ZIP.
- `.git/`, `__pycache__/`, and log files were not included.

## Server operator upgrade

- Removed `/operator` slash command additions; operator actions now stay prompt-only through DM or mention/reply prompts.
- Added bulk channel template setup for game/support/community categories.
- Changed `create_channel` to configure existing channels instead of failing or duplicating.
- Added owner-approved role, member moderation, webhook, server settings, and schedule actions behind config flags.
- Announcements now send as plain text messages, not embeds.
- Added server role/channel event listeners to invalidate AI server knowledge cache when Discord state changes.
- Added diagnostics and `.env.example` flags for the new operator actions.


## DM-only operator/public info-only update

- Operator prompts now default to DM-only via `AI_OPERATOR_DM_ONLY=true`.
- Public server channels no longer create/approve/reject operator proposals.
- Public AI replies are info-only via `AI_CHAT_PUBLIC_INFO_ONLY=true`.
- Public context strips R2/database internals, logs, IDs, and operator state.
- Owner/Admin users can manage server/database/R2 through DM when `AI_OPERATOR_ALLOW_DISCORD_ADMINS=true`.
- Added `docs/DM_ONLY_OPERATOR_ACCESS.md`.

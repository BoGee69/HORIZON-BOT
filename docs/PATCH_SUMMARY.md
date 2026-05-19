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
   - If a requested text channel already exists, TriadBot no longer creates a duplicate.
   - If a topic was supplied, the existing channel topic is updated instead.

6. Cleaned attachment text before public posting.
   - Strips markers such as `[Attachment: file.txt]`, `[Attachment content]`, and `[Attachment notes]` before using attachment text for rules/announcement content.

7. Added documentation.
   - `docs/AI_MODEL_ROUTER.md`
   - `docs/PATCH_SUMMARY.md`

Packaging notes:

- `.env` was intentionally removed from the returned ZIP.
- `.git/`, `__pycache__/`, and log files were not included.

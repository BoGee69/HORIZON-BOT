# DM-only operator access

HORIZON BOT now separates public chat from private operator control.

## Rules

- Public server channels are **info-only**.
- Server/database/R2/operator prompts only work in **DM**.
- Owner/Admin users may DM HORIZON BOT to request server management or database maintenance.
- Public messages cannot create proposals, approve proposals, reject proposals, run R2 maintenance, sync Steam DB, edit channels, edit roles, manage permissions, manage members, or expose internal logs/storage state.
- If a user asks for management in a public channel, HORIZON BOT should tell them to send the request through DM as an authorized Owner/Admin.

## Important environment variables

```env
AI_CHAT_ENABLED=true
AI_CHAT_ALLOW_DISCORD_ADMINS=true
AI_CHAT_SERVER_REPLIES_ENABLED=true
AI_CHAT_SERVER_REQUIRE_MENTION=true
AI_CHAT_PUBLIC_INFO_ONLY=true

AI_OPERATOR_ENABLED=true
AI_OPERATOR_ALLOW_DISCORD_ADMINS=true
AI_OPERATOR_DM_ONLY=true
AI_OPERATOR_SERVER_PROMPTS_ENABLED=false
```

## Public channel behavior

Allowed:

- explain server rules
- point users to #rules, #resources, #announcement, #welcome, or guide channels
- answer general HORIZON server questions
- explain that management requests must be done in DM

Blocked:

- proposal creation/approval/rejection
- channel/role/permission/member/webhook changes
- R2 maintenance
- Steam DB sync
- database internals
- logs, stack traces, env variables, tokens, API keys
- internal Discord IDs

## Private DM behavior

Authorized Owner/Admin users may DM prompts such as:

```text
cek status R2
jalankan R2 maintenance
sync Steam DB
buat proposal update rules di #rules: ...
kirim announcement di #announcement: maintenance jam 10 malam
approve latest
approve all
reject latest
```

Real changes still go through proposal approval unless the specific action is designed as a read-only/status action.

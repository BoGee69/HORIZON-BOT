# Prompt-Only Server Operator Upgrade

This patch expands HORIZON BOT's owner-approved operator flow without adding any new slash commands.
All operator actions are triggered from normal prompts, then converted into proposals that require approval before anything changes.

## Prompt-only control surface

You can use DM prompts to HORIZON BOT, or mention/reply to HORIZON BOT in a server channel when `AI_OPERATOR_SERVER_PROMPTS_ENABLED=true`.
Server prompts require a mention/reply by default so normal channel conversation does not accidentally create proposals.

Examples:

```text
HORIZON BOT buat channel #test untuk testing
HORIZON BOT atur #announcement hanya Admin yang bisa kirim pesan
HORIZON BOT ganti topic #rules jadi Baca dulu sebelum main
HORIZON BOT kirim announcement di #announcement: Server maintenance jam 10 malam
HORIZON BOT setup game category Games
HORIZON BOT setiap Minggu jam 2 jalankan R2 maintenance
```

Approval examples:

```text
approve latest
lanjut
gas
approve all
reject latest
reject all
approve a1b2c3
```

## New whitelisted prompt actions

- `setup_channel_template`
- `create_role`
- `update_role`
- `delete_role`
- `timeout_member`
- `kick_member`
- `ban_member`
- `create_webhook`
- `delete_webhook`
- `update_server_settings`
- `schedule_action`

Dangerous actions are disabled by default where appropriate:

- role deletion
- kick
- ban
- webhook deletion
- server settings changes

## Channel template examples

```text
setup game category Games
buat template support kategori Help
HORIZON BOT setup community category Server
```

The template system creates/configures existing resources instead of duplicating channels.

## Schedule behavior

Schedules do not bypass approval. When a schedule is due, HORIZON BOT creates a normal approval proposal.

Example:

```text
setiap Minggu jam 2 jalankan R2 maintenance
```

This creates a `schedule_action` proposal. After approval, the schedule is stored in `AI_OPERATOR_SCHEDULES_PATH`.

## Environment switches

```env
AI_OPERATOR_SERVER_PROMPTS_ENABLED=true
AI_OPERATOR_SERVER_REQUIRE_MENTION=true
AI_OPERATOR_ALLOW_SETUP_CHANNEL_TEMPLATE=true
AI_OPERATOR_ALLOW_CREATE_ROLE=true
AI_OPERATOR_ALLOW_UPDATE_ROLE=true
AI_OPERATOR_ALLOW_DELETE_ROLE=false
AI_OPERATOR_ALLOW_MEMBER_TIMEOUT=true
AI_OPERATOR_ALLOW_MEMBER_KICK=false
AI_OPERATOR_ALLOW_MEMBER_BAN=false
AI_OPERATOR_ALLOW_WEBHOOK_CREATE=true
AI_OPERATOR_ALLOW_WEBHOOK_DELETE=false
AI_OPERATOR_ALLOW_SERVER_SETTING=false
AI_OPERATOR_ALLOW_SCHEDULE_ACTION=true
AI_OPERATOR_SCHEDULER_ENABLED=true
AI_OPERATOR_SCHEDULE_CHECK_SECONDS=60
AI_OPERATOR_SCHEDULES_PATH=data/ai_operator_schedules.json
```

## Auto server knowledge refresh

Server channel/role create, update, and delete events invalidate the AI server knowledge cache, so HORIZON BOT can refresh its view of channels, roles, and server layout sooner than the normal cache TTL.


## Security note

For production, operator prompts are DM-only. Public server channels are reserved for server information replies and cannot create or approve proposals. Use `AI_OPERATOR_DM_ONLY=true` and `AI_OPERATOR_SERVER_PROMPTS_ENABLED=false`.

# Admin AI Chat Access

This patch lets trusted Discord admins chat with TriadBot in DM without hardcoding every admin user ID.

## AI chat access

Set this in .env file:

```env
AI_CHAT_ENABLED=true
AI_CHAT_ALLOW_DISCORD_ADMINS=true
```

TriadBot will allow DM chat when the user is one of these:

- listed in `AI_CHAT_ALLOWED_IDS`, or
- the Discord guild owner, or
- has Discord Administrator permission, or
- has a role matching `ADMIN_ROLE_IDS` / `ADMIN_ROLE_NAMES`.

If `SERVER_ADMIN_GUILD_IDS` is set, only those guilds are checked. If it is empty, TriadBot checks every guild it is in.

## Operator / approval access

By default, Discord admins can chat but cannot approve operator proposals.

```env
AI_OPERATOR_ALLOW_DISCORD_ADMINS=false
```

Keep it false if only the owner should approve R2 maintenance, Steam DB sync, rules updates, announcements, and channel changes.

Set it true only if trusted Discord admins are allowed to approve whitelisted operator actions too:

```env
AI_OPERATOR_ALLOW_DISCORD_ADMINS=true
```

## Recommended setup for your friend

For a trusted friend who only needs to talk with TriadBot:

```env
AI_CHAT_ALLOW_DISCORD_ADMINS=true
AI_OPERATOR_ALLOW_DISCORD_ADMINS=false
```

Then give the friend an Admin role in the TriadGames Discord server, or give them Discord Administrator permission.

# HORIZON BOT n8n Integration

HORIZON BOT can be connected to n8n as a control tower for monitoring, alerts,
scheduled maintenance, and admin workflows.

## Environment

Set these in Railway or your local `.env`:

```env
N8N_ENABLED=true
N8N_SHARED_SECRET=use-a-long-random-secret
N8N_ALLOW_MAINTENANCE_ACTIONS=true

# Optional: HORIZON BOT sends completion/admin-alert events to n8n.
N8N_WEBHOOK_URL=https://your-n8n-host/webhook/horizon
N8N_WEBHOOK_SECRET=use-another-random-secret
N8N_FORWARD_ADMIN_ALERTS=true
```

Keep `N8N_ALLOW_MAINTENANCE_ACTIONS=false` until your n8n instance is secured.

## Authentication

Every `/n8n/*` request must include:

```http
Authorization: Bearer <N8N_SHARED_SECRET>
```

Alternative headers also work:

```http
X-HORIZON BOT-N8N-Token: <N8N_SHARED_SECRET>
X-N8N-Token: <N8N_SHARED_SECRET>
```

## Endpoints

### Health

```http
GET /n8n/health
POST /n8n/health
```

Returns the existing HORIZON BOT health payload plus recent AI/caretaker events.

### Send Event To HORIZON BOT

```http
POST /n8n/event
```

Body:

```json
{
  "level": "warning",
  "source": "n8n",
  "title": "n8n workflow warning",
  "message": "OpenDir monitor detected a slow response.",
  "fields": {
    "Workflow": "OpenDir monitor"
  },
  "notify_admins": true,
  "force": true
}
```

This records an AI/caretaker event and can DM admins.

### Trigger OpenDir Sync

```http
POST /n8n/opendir-sync
```

Body:

```json
{
  "appid": "730",
  "background": true
}
```

Omit `appid` for a normal sync window. `background` defaults to `true`.

### Trigger GitHub DB Backup

```http
POST /n8n/db-backup
```

Body:

```json
{
  "force": true,
  "background": true
}
```

### Trigger Steam DB Sync

```http
POST /n8n/steam-db-sync
```

Body:

```json
{
  "apply": true,
  "include_new": true,
  "max_new": 0,
  "max_updates": 0,
  "background": true
}
```

### Trigger R2 Maintenance

```http
POST /n8n/r2-maintenance
```

Body:

```json
{
  "apply": false,
  "limit": 100,
  "rename_objects": true,
  "clean_lua": true,
  "background": true
}
```

Use `apply=false` for dry runs.

## Completion Events

If `N8N_WEBHOOK_URL` is set, background jobs post completion events back to n8n:

- `opendir_sync.completed`
- `opendir_sync.failed`
- `db_backup.completed`
- `db_backup.failed`
- `steam_db_sync.completed`
- `steam_db_sync.failed`
- `r2_maintenance.completed`
- `r2_maintenance.failed`
- `admin_alert`

HORIZON BOT includes `X-HORIZON BOT-N8N-Secret` when `N8N_WEBHOOK_SECRET` is set.

## Suggested n8n Workflows

- Schedule `/n8n/health` every 30 minutes and notify Discord if `ok=false`.
- Schedule `/n8n/db-backup` every 12 hours.
- Trigger `/n8n/opendir-sync` for priority AppIDs from a form or Discord webhook.
- Send `/n8n/event` when external checks notice OpenDir/Railway/R2 problems.
- Use background job completion events to post summaries to an admin channel.

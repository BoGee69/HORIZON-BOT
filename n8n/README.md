# Local n8n

This folder runs n8n locally with Docker Compose.

## Start

```powershell
docker compose up -d
```

## Stop

```powershell
docker compose down
```

## Open

```text
http://localhost:5678
```

The first visit asks you to create the owner account.

## Imported Workflows

Two starter workflows are provided in `n8n/workflows/`:

- `HORIZON BOT Event Receiver`: receives HORIZON BOT completion/admin alert events at `/webhook/horizon`.
- `HORIZON BOT Health Check`: checks HORIZON BOT's `/n8n/health` endpoint. Keep it inactive until HORIZON BOT is running with `N8N_ENABLED=true`.

Import them manually:

```powershell
docker cp .\workflows horizon-n8n:/tmp/horizon-workflows
docker exec horizon-n8n n8n import:workflow --separate --input=/tmp/horizon-workflows
```

Publish the event receiver after import:

```powershell
docker exec horizon-n8n n8n publish:workflow --id=<workflow-id>
docker restart horizon-n8n
```

## Data

n8n stores workflows and credentials in the Docker volume:

```text
n8n_n8n_data
```

The local runtime config is in `n8n/.env`, which is intentionally ignored by git.

## Notes

- The compose file uses the official n8n Docker image.
- `N8N_ENCRYPTION_KEY` is generated in `n8n/.env`; keep it stable after creating credentials.
- The current image logs a Python task runner warning because Python is not installed inside the container. JavaScript workflows still run normally.

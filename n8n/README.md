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

- `TriadBot Event Receiver`: receives TriadBot completion/admin alert events at `/webhook/triadbot`.
- `TriadBot Health Check`: checks TriadBot's `/n8n/health` endpoint. Keep it inactive until TriadBot is running with `N8N_ENABLED=true`.

Import them manually:

```powershell
docker cp .\workflows triadbot-n8n:/tmp/triadbot-workflows
docker exec triadbot-n8n n8n import:workflow --separate --input=/tmp/triadbot-workflows
```

Publish the event receiver after import:

```powershell
docker exec triadbot-n8n n8n publish:workflow --id=<workflow-id>
docker restart triadbot-n8n
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

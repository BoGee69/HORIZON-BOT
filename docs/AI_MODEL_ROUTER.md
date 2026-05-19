# TriadBot AI Model Router

TriadBot keeps GPT as the main/default brain and uses other Ollama Cloud models only as specialist helpers.

## Runtime roles

| Role | Primary model | Fallback | Purpose |
|---|---|---|---|
| `default_chat` | `gpt-oss:120b-cloud` | `glm-5.1:cloud` | Owner/user chat, status explanation, server/R2 questions. |
| `background_monitor` | `qwen3.5:cloud` | `glm-5.1:cloud` | Lightweight periodic log/status analysis. |
| `server_operator` | `gpt-oss:120b-cloud` | `glm-5.1:cloud` | High-risk decisions that become owner-approved proposals. |
| `r2_maintenance_assistant` | `gpt-oss:120b-cloud` | `glm-5.1:cloud` | R2/database maintenance analysis and proposal reasoning. |
| `intent_router` | `gpt-oss:120b-cloud` | `glm-5.1:cloud` | Owner intent classification and approval phrase handling. |
| `proposal_manager` | `gpt-oss:120b-cloud` | `glm-5.1:cloud` | Proposal schema, Proposal ID, status, approval safety. |
| `code_debugger` | `kimi-k2.6:cloud` | `glm-5.1:cloud` | Coding/debugging helper outside the live Discord bot runtime. |
| `github_assistant` | `kimi-k2.6:cloud` | `gpt-oss:120b-cloud` | Branch/commit/PR summaries; no auto-push without owner approval. |
| `security_reviewer` | `gpt-oss:120b-cloud` | `nemotron-3-super:cloud` | Conservative review for token leaks, permission risks, injection, approval bypass. |

## Environment variables

The main variables are in `.env.example`:

```env
AI_MODEL_DEFAULT=gpt-oss:120b-cloud
AI_MODEL_CHAT=gpt-oss:120b-cloud
AI_MODEL_BACKGROUND_MONITOR=qwen3.5:cloud
AI_MODEL_MONITOR=qwen3.5:cloud
AI_MODEL_OPERATOR=gpt-oss:120b-cloud
AI_MODEL_R2_MAINTENANCE=gpt-oss:120b-cloud
AI_MODEL_INTENT_ROUTER=gpt-oss:120b-cloud
AI_MODEL_PROPOSAL_MANAGER=gpt-oss:120b-cloud
AI_MODEL_CODE_DEBUGGER=kimi-k2.6:cloud
AI_MODEL_GITHUB=kimi-k2.6:cloud
AI_MODEL_SECURITY=gpt-oss:120b-cloud
AI_MODEL_FALLBACK=glm-5.1:cloud
```

Current runtime use:

- `AI_CHAT_MODEL` defaults to `AI_MODEL_CHAT`.
- `AI_MAINTENANCE_MODEL` defaults to `AI_MODEL_MONITOR`.
- Chat and caretaker calls retry once with `AI_MODEL_FALLBACK` if the primary model fails.

## Safety contract

The model never receives raw secrets and never executes arbitrary shell commands. Discord/R2/server-changing operations must be converted into a whitelisted `OperatorProposal` and approved by the owner before execution.

Approval phrases supported by the operator include:

- `approve <id>`
- `<id> approve`
- `approve latest`
- `lanjut`
- `gas`
- `approve all`
- `reject <id>`
- `reject latest`
- `reject all`

Announcements and rules are posted as plain Discord messages, not embeds.

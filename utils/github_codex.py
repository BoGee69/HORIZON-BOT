"""GitHub-backed code assistant helpers for HORIZON BOT.

This module keeps code-changing power away from the live Railway container.
It reads files from GitHub, asks the configured AI provider for a proposed
full-file replacement, then applies the approved result to a new branch/PR.
"""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import aiohttp

import config as bot_config
from utils.ai_caretaker import call_ai_provider, sanitize_data, sanitize_text


class GitHubCodexError(RuntimeError):
    pass


@dataclass
class GitHubFile:
    path: str
    content: str
    sha: str | None = None
    size: int = 0
    encoding: str = "utf-8"


@dataclass
class CodeChange:
    path: str
    reason: str
    content: str
    exists: bool = True
    old_sha: str | None = None


@dataclass
class CodeProposal:
    proposal_id: str
    requested_by: int
    prompt: str
    summary: str
    risk: str
    changes: list[CodeChange] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    status: str = "pending"
    branch: str = ""
    pr_url: str = ""
    result: str = ""

    @property
    def expired(self) -> bool:
        return bool(self.expires_at and time.time() > self.expires_at)


class GitHubRepoClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.token = (getattr(bot_config, "AI_CODEX_GITHUB_TOKEN", "") or "").strip()
        self.repo = (getattr(bot_config, "AI_CODEX_GITHUB_REPO", "") or "").strip().strip("/")
        self.base_branch = (getattr(bot_config, "AI_CODEX_BASE_BRANCH", "main") or "main").strip()
        self.api_base = "https://api.github.com"
        if self.repo and "/" not in self.repo:
            raise GitHubCodexError("AI_GITHUB_REPO must use owner/repo format.")

    @property
    def configured(self) -> bool:
        return bool(self.token and self.repo and "/" in self.repo)

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise GitHubCodexError("AI_GITHUB_TOKEN / GITHUB_TOKEN is not configured.")
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "HORIZON BOT-GitHub-Patch",
        }

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        url = f"{self.api_base}{path}"
        timeout = aiohttp.ClientTimeout(total=float(getattr(bot_config, "AI_CODEX_GITHUB_TIMEOUT_SECONDS", 45) or 45))
        async with self.session.request(method, url, headers=self._headers(), timeout=timeout, **kwargs) as resp:
            text = await resp.text()
            data: Any
            try:
                data = json.loads(text) if text else {}
            except Exception:
                data = {"raw": text[:800]}
            if resp.status >= 400:
                message = data.get("message") if isinstance(data, dict) else text[:300]
                raise GitHubCodexError(f"GitHub API {method} {path} failed: HTTP {resp.status}: {message}")
            return data

    async def repo_info(self) -> dict[str, Any]:
        if not self.configured:
            return {"configured": False, "repo": self.repo, "branch": self.base_branch}
        data = await self._request("GET", f"/repos/{self.repo}")
        return sanitize_data({
            "configured": True,
            "repo": self.repo,
            "name": data.get("full_name"),
            "private": data.get("private"),
            "default_branch": data.get("default_branch"),
            "base_branch": self.base_branch,
            "html_url": data.get("html_url"),
        })

    async def get_branch_sha(self, branch: str | None = None) -> str:
        branch = branch or self.base_branch
        data = await self._request("GET", f"/repos/{self.repo}/git/ref/heads/{quote(branch, safe='/')}")
        obj = data.get("object") or {}
        sha = obj.get("sha")
        if not sha:
            raise GitHubCodexError(f"Could not resolve branch SHA for {branch}.")
        return str(sha)

    async def create_branch(self, branch: str, from_branch: str | None = None) -> None:
        base_sha = await self.get_branch_sha(from_branch or self.base_branch)
        await self._request(
            "POST",
            f"/repos/{self.repo}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": base_sha},
        )

    async def get_tree_paths(self, branch: str | None = None) -> list[str]:
        branch = branch or self.base_branch
        data = await self._request("GET", f"/repos/{self.repo}/git/trees/{quote(branch, safe='/')}?recursive=1")
        items = data.get("tree") if isinstance(data, dict) else []
        paths: list[str] = []
        for item in items or []:
            if item.get("type") == "blob" and item.get("path"):
                paths.append(str(item["path"]))
        return sorted(paths)

    async def get_file(self, path: str, branch: str | None = None, *, max_bytes: int | None = None) -> GitHubFile:
        branch = branch or self.base_branch
        path = normalize_repo_path(path)
        max_bytes = max_bytes or int(getattr(bot_config, "AI_CODEX_MAX_FILE_BYTES", 70000) or 70000)
        data = await self._request("GET", f"/repos/{self.repo}/contents/{quote(path, safe='/')}?ref={quote(branch, safe='/')}")
        if isinstance(data, list) or data.get("type") != "file":
            raise GitHubCodexError(f"{path} is not a regular file.")
        size = int(data.get("size") or 0)
        if size > max_bytes:
            raise GitHubCodexError(f"{path} is too large ({size} bytes, limit {max_bytes}).")
        encoded = str(data.get("content") or "")
        if data.get("encoding") != "base64":
            raise GitHubCodexError(f"{path} uses unsupported encoding: {data.get('encoding')}")
        raw = base64.b64decode(encoded.replace("\n", ""))
        try:
            text = raw.decode("utf-8")
            enc = "utf-8"
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
            enc = "latin-1"
        return GitHubFile(path=path, content=text, sha=data.get("sha"), size=size, encoding=enc)

    async def file_exists(self, path: str, branch: str | None = None) -> tuple[bool, str | None]:
        try:
            item = await self.get_file(path, branch, max_bytes=2_000_000)
            return True, item.sha
        except GitHubCodexError as exc:
            if "HTTP 404" in str(exc):
                return False, None
            raise

    async def put_file(self, *, path: str, content: str, branch: str, message: str, sha: str | None = None) -> dict[str, Any]:
        path = normalize_repo_path(path)
        payload: dict[str, Any] = {
            "message": sanitize_text(message)[:200] or f"HORIZON BOT AI update {path}",
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha
        return await self._request("PUT", f"/repos/{self.repo}/contents/{quote(path, safe='/')}", json=payload)

    async def create_pull_request(self, *, branch: str, title: str, body: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/repos/{self.repo}/pulls",
            json={
                "title": sanitize_text(title)[:240] or "HORIZON BOT AI patch",
                "head": branch,
                "base": self.base_branch,
                "body": sanitize_text(body)[:6000],
                "maintainer_can_modify": True,
            },
        )


def normalize_repo_path(path: str) -> str:
    text = sanitize_text(path).strip().replace("\\", "/")
    text = re.sub(r"^/+", "", text)
    parts = []
    for part in text.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            raise GitHubCodexError("Repository paths may not contain '..'.")
        parts.append(part)
    if not parts:
        raise GitHubCodexError("File path is empty.")
    return "/".join(parts)


def is_likely_text_code_path(path: str) -> bool:
    lower = path.lower()
    allowed = tuple(getattr(bot_config, "AI_CODEX_ALLOWED_EXTENSIONS", set()) or set())
    if allowed and lower.rsplit(".", 1)[-1] not in allowed:
        return False
    blocked_names = (".env", ".pem", ".key", ".crt", ".p12", ".sqlite", ".db", ".zip", ".png", ".jpg", ".jpeg", ".webp")
    return not any(lower.endswith(item) for item in blocked_names)


def rank_relevant_paths(paths: list[str], request: str, *, limit: int) -> list[str]:
    lower_request = sanitize_text(request).lower()
    tokens = [t for t in re.findall(r"[a-z0-9_]{3,}", lower_request) if t not in {"yang", "buat", "bisa", "dari", "with", "error", "fix", "kenapa"}]
    hints = {
        "gen": ["game_commands", "gen_limits", "r2_presign", "database"],
        "r2": ["r2_", "r2", "maintenance", "inventory"],
        "sqlite": ["database", "github_db_backup"],
        "database": ["database", "steam_db_sync", "github_db_backup"],
        "opendir": ["opendir", "sync"],
        "steam": ["steam", "steam_db_sync"],
        "guardian": ["ai_chat", "ai_brain", "ai_security", "ai_caretaker", "ai_operator"],
        "security": ["ai_security", "server_admin"],
        "approval": ["ai_operator"],
        "proposal": ["ai_operator"],
        "github": ["github", "backup"],
        "codex": ["github", "ai_", "codex"],
    }
    wanted: list[str] = []
    for key, values in hints.items():
        if key in lower_request:
            wanted.extend(values)
    wanted.extend(tokens[:12])

    scored: list[tuple[int, str]] = []
    for path in paths:
        if not is_likely_text_code_path(path):
            continue
        lower = path.lower()
        score = 0
        for token in wanted:
            if token and token in lower:
                score += 5
        if lower.startswith("cogs/") or lower.startswith("utils/"):
            score += 2
        if lower.endswith(".py"):
            score += 2
        if "__pycache__" in lower or "/data/" in f"/{lower}":
            score -= 20
        if score > 0:
            scored.append((score, path))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [path for _, path in scored[:limit]]


def _extract_json(text: str) -> dict[str, Any]:
    raw = sanitize_text(text).strip()
    if not raw:
        raise GitHubCodexError("AI returned an empty response.")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S | re.I)
    if fenced:
        raw = fenced.group(1)
    else:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start:end + 1]
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise GitHubCodexError(f"AI did not return valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise GitHubCodexError("AI JSON root must be an object.")
    return data


async def build_ai_code_proposal(
    session: aiohttp.ClientSession,
    *,
    client: GitHubRepoClient,
    requested_by: int,
    user_prompt: str,
    selected_paths: list[str] | None = None,
) -> CodeProposal:
    if not client.configured:
        raise GitHubCodexError("HORIZON BOT GitHub Mode belum dikonfigurasi. Set AI_GITHUB_REPO dan AI_GITHUB_TOKEN.")

    max_files = max(1, min(int(getattr(bot_config, "AI_CODEX_MAX_CONTEXT_FILES", 5) or 5), 10))
    all_paths = await client.get_tree_paths()
    selected_paths = [normalize_repo_path(p) for p in (selected_paths or []) if p.strip()]
    if not selected_paths:
        selected_paths = rank_relevant_paths(all_paths, user_prompt, limit=max_files)
    selected_paths = selected_paths[:max_files]
    if not selected_paths:
        raise GitHubCodexError("Saya belum menemukan file GitHub yang relevan. Sebutkan path file, contoh: `perbaiki cogs/game_commands.py: ...`.")

    file_contexts = []
    for path in selected_paths:
        if not is_likely_text_code_path(path):
            continue
        item = await client.get_file(path)
        file_contexts.append({
            "path": item.path,
            "sha": item.sha,
            "size": item.size,
            "content": item.content[: int(getattr(bot_config, "AI_CODEX_CONTEXT_CHARS_PER_FILE", 14000) or 14000)],
        })
    if not file_contexts:
        raise GitHubCodexError("Tidak ada file teks/code yang bisa saya baca untuk membuat patch.")

    prompt = (
        "You are HORIZON BOT GitHub Mode. You edit the HORIZON BOT Discord bot repository safely.\n"
        "Return ONLY valid JSON. No markdown. No prose outside JSON.\n"
        "The user is the owner/admin of the bot. Diagnose the request and produce a minimal safe patch.\n"
        "Rules:\n"
        "- Never add secrets, tokens, credentials, piracy bypass logic, or destructive live actions.\n"
        "- Prefer small targeted edits.\n"
        "- Return full replacement content for each changed file, not a diff.\n"
        "- Do not invent files unless clearly necessary.\n"
        "- Preserve existing behavior unless the request explicitly changes it.\n"
        "- If uncertain, return an empty changes array and explain in summary what file/context is missing.\n\n"
        "Required JSON schema:\n"
        "{\"summary\": string, \"risk\": \"low|medium|high\", \"changes\": [{\"path\": string, \"reason\": string, \"content\": string}]}\n\n"
        f"Repository: {client.repo}\n"
        f"Base branch: {client.base_branch}\n"
        f"Owner request:\n{sanitize_text(user_prompt)[:4000]}\n\n"
        f"File contexts JSON:\n{json.dumps(file_contexts, ensure_ascii=False)[: int(getattr(bot_config, 'AI_CODEX_TOTAL_CONTEXT_CHARS', 50000) or 50000)]}\n"
    )
    reply = await call_ai_provider(
        session,
        prompt,
        provider=getattr(bot_config, "AI_CODEX_PROVIDER", getattr(bot_config, "AI_CHAT_PROVIDER", "ollama")),
        model=getattr(bot_config, "AI_CODEX_MODEL", getattr(bot_config, "AI_CHAT_MODEL", "")),
        temperature=float(getattr(bot_config, "AI_CODEX_TEMPERATURE", 0.15) or 0.15),
        max_output_tokens=int(getattr(bot_config, "AI_CODEX_MAX_OUTPUT_TOKENS", 8000) or 8000),
    )
    data = _extract_json(reply)
    changes_raw = data.get("changes") or []
    if not isinstance(changes_raw, list):
        raise GitHubCodexError("AI JSON field changes must be a list.")

    context_by_path = {item["path"]: item for item in file_contexts}
    changes: list[CodeChange] = []
    max_changes = max(1, min(int(getattr(bot_config, "AI_CODEX_MAX_CHANGED_FILES", 4) or 4), 8))
    max_content = max(2000, int(getattr(bot_config, "AI_CODEX_MAX_GENERATED_FILE_CHARS", 90000) or 90000))
    for item in changes_raw[:max_changes]:
        if not isinstance(item, dict):
            continue
        path = normalize_repo_path(str(item.get("path") or ""))
        if not is_likely_text_code_path(path):
            raise GitHubCodexError(f"Refusing to modify unsupported file type: {path}")
        content = str(item.get("content") or "")
        if not content.strip():
            continue
        if len(content) > max_content:
            raise GitHubCodexError(f"Generated replacement for {path} is too large ({len(content)} chars).")
        exists, sha = await client.file_exists(path)
        if path in context_by_path:
            sha = context_by_path[path].get("sha") or sha
        changes.append(CodeChange(
            path=path,
            reason=sanitize_text(str(item.get("reason") or "AI proposed code update"))[:700],
            content=content,
            exists=exists,
            old_sha=sha,
        ))

    import secrets
    proposal_id = secrets.token_hex(3)
    return CodeProposal(
        proposal_id=proposal_id,
        requested_by=requested_by,
        prompt=sanitize_text(user_prompt)[:3000],
        summary=sanitize_text(str(data.get("summary") or "AI generated a GitHub code patch proposal."))[:1000],
        risk=sanitize_text(str(data.get("risk") or "medium")).lower()[:20],
        changes=changes,
        expires_at=time.time() + max(300, int(getattr(bot_config, "AI_CODEX_PROPOSAL_TTL_SECONDS", 1800) or 1800)),
    )


async def apply_code_proposal(client: GitHubRepoClient, proposal: CodeProposal) -> str:
    if not proposal.changes:
        raise GitHubCodexError("Proposal has no file changes to apply.")
    branch_prefix = sanitize_text(getattr(bot_config, "AI_CODEX_BRANCH_PREFIX", "ai-codex")).strip("/- ") or "ai-codex"
    branch = f"{branch_prefix}/{proposal.proposal_id}"
    await client.create_branch(branch)

    committed: list[str] = []
    for change in proposal.changes:
        exists, current_sha = await client.file_exists(change.path, branch)
        sha = current_sha if exists else None
        await client.put_file(
            path=change.path,
            content=change.content,
            branch=branch,
            sha=sha,
            message=f"HORIZON BOT AI: update {change.path}",
        )
        committed.append(change.path)

    proposal.branch = branch
    pr_url = ""
    if bool(getattr(bot_config, "AI_CODEX_CREATE_PR", True)):
        body_lines = [
            "HORIZON BOT AI patch proposal applied after Discord owner/admin approval.",
            "",
            f"Proposal ID: `{proposal.proposal_id}`",
            "",
            "Summary:",
            proposal.summary,
            "",
            "Changed files:",
        ]
        for change in proposal.changes:
            body_lines.append(f"- `{change.path}` — {change.reason}")
        pr = await client.create_pull_request(
            branch=branch,
            title=f"HORIZON BOT AI patch {proposal.proposal_id}",
            body="\n".join(body_lines),
        )
        pr_url = str(pr.get("html_url") or "")
        proposal.pr_url = pr_url
    return "\n".join([
        f"Branch: `{branch}`",
        f"Committed files: {', '.join(f'`{p}`' for p in committed)}",
        f"Pull request: {pr_url or 'not created'}",
    ])

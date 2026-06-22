Visit https://m365.cloud.microsoft/chat  

<img width="769" height="185" alt="image" src="https://github.com/user-attachments/assets/fe387176-0c0d-4fc9-a3d4-63dc9f20164e" />  
  
Export Har  
Enjoy m365 Gpt5.5 Think outside of the traditional chat window  



# m365-auth

Local Python gateway for calling your authenticated M365 Copilot ChatHub session
through OpenAI/Anthropic-style HTTP endpoints.

## Warning

HAR files and `.env` contain live credentials. They are ignored by git. Do not
commit them.

## Setup

Export an authenticated HAR from `https://m365.cloud.microsoft/chat`, place it
in this folder, then run:

```powershell
python -m m365auth --init-env
```

Optional local gateway auth:

```text
M365_PROXY_API_KEY=change-this
```

## Refreshing credentials (optional, Playwright)

The captured HAR's SPA refresh token has a hard ~24h lifetime, so credentials go
stale daily. Instead of re-exporting a HAR each time, an optional helper logs in
with a real browser and harvests fresh tokens/cookies into `.env`, reusing your
existing HAR as the stable request template.

This is the only part of the project that is not pure stdlib, so keep it in a
venv:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install playwright
.\.venv\Scripts\python -m playwright install chromium
```

Then refresh credentials (log in / do MFA on the first headed run; the browser
profile persists under `~/.m365auth/pw-profile`):

```powershell
.\.venv\Scripts\python -m m365auth.login            # headed; log in on first run
.\.venv\Scripts\python -m m365auth.login --headless # once the profile is logged in
```

If no access token is captured, send one chat message in the opened browser to
open the ChatHub socket. The gateway itself still runs on plain `python`
(stdlib only).

## Direct Chat

```powershell
python -m m365auth --chat "Reply with only OK." --new-conversation
python -m m365auth --chat "continue this chat" --continue-chat
python -m m365auth --interactive
```

## HTTP Gateway

```powershell
python -m m365auth.gateway --host 127.0.0.1 --port 8787
```

By default the gateway keeps one shared conversation and streams upstream
deltas to clients.

Useful compatibility switches:

```powershell
python -m m365auth.gateway --new-conversation-per-request
```

Endpoints:

```text
GET  /v1/models
POST /v1/chat/completions
POST /v1/responses
POST /v1/messages
POST /v1/messages/count_tokens
```

Example:

```powershell
curl http://127.0.0.1:8787/v1/chat/completions `
  -H "Content-Type: application/json" `
  -H "Authorization: Bearer $env:M365_PROXY_API_KEY" `
  -d '{ "model": "m365-copilot", "messages": [{ "role": "user", "content": "Reply with only OK." }] }'
```

## Client Config

Codex `~/.codex/config.toml`:

```toml
model = "m365-copilot"
model_provider = "m365"

[model_providers.m365]
name = "M365 Copilot Gateway"
base_url = "http://127.0.0.1:8787/v1"
env_key = "M365_PROXY_API_KEY"
```

opencode:

```json
{
  "provider": {
    "m365": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "M365 Copilot Gateway",
      "options": { "baseURL": "http://127.0.0.1:8787/v1" },
      "models": { "m365-copilot": { "name": "M365 Copilot" } }
    }
  }
}
```

Claude Code:

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8787"
$env:ANTHROPIC_AUTH_TOKEN = $env:M365_PROXY_API_KEY
$env:CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY = "1"
```

pi (Pi Coding Agent):

Create `~/.pi/agent/extensions/m365.ts`:

```typescript
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
  pi.registerProvider("m365", {
    name: "M365 Copilot Gateway",
    baseUrl: "http://127.0.0.1:8787/v1",
    apiKey: "local", // any non-empty value when gateway auth is disabled
    api: "openai-completions",
    authHeader: true,
    models: [
      {
        id: "m365-copilot",
        name: "M365 Copilot",
        reasoning: false,
        input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: 128000,
        maxTokens: 8192,
        compat: { supportsDeveloperRole: false, supportsReasoningEffort: false },
      },
    ],
  });
}
```

Then launch with `pi --model m365-copilot` (verify with `pi --list-models`).

## Notes

- Uses only Python stdlib.
- The ChatHub WebSocket and OAuth refresh entries are auto-detected from the HAR
  by content. Override with `--websocket-entry` / `--oauth-refresh-entry` if needed.
- Auto-refreshes the ChatHub access token when the captured refresh token works.
- Tool calling is emulated for both OpenAI (`/v1/chat/completions`) and
  Anthropic (`/v1/messages`): tool definitions are injected into the prompt and
  tool calls are parsed back out of the text reply. Reliability depends on the
  model following the protocol. Tool-enabled streaming requests are buffered,
  not streamed token-by-token: Copilot emits intermediate "thinking" text before
  its final answer, so only the fully reconciled reply can be classified.
- Microsoft can change private endpoints at any time.

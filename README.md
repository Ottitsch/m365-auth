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
python m365_auth.py --init-env
```

Optional local gateway auth:

```text
M365_PROXY_API_KEY=change-this
```

## Direct Chat

```powershell
python m365_auth.py --chat "Reply with only OK." --new-conversation
python m365_auth.py --chat "continue this chat" --continue-chat
python m365_auth.py --interactive
```

## HTTP Gateway

```powershell
python m365_gateway.py --host 127.0.0.1 --port 8787
```

By default the gateway keeps one shared conversation and streams upstream
deltas to clients.

Useful compatibility switches:

```powershell
python m365_gateway.py --new-conversation-per-request
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

## Notes

- Uses only Python stdlib.
- Auto-refreshes the ChatHub access token when the captured refresh token works.
- Tool calling is not implemented yet.
- Microsoft can change private endpoints at any time.

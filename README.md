# Hermes

A production Telegram AI assistant with long-term memory, built on retrieval-augmented generation over a local SQLite vector store.

Runs continuously on a Linux server under `systemd`, serving live users.

---

## Why it exists

Most chatbots forget everything between sessions. Hermes remembers: every message is embedded and stored, and relevant history is retrieved by vector similarity and fed back into the model's context on each turn.

Everything runs against free-tier API quotas, which drove most of the interesting engineering — model fallback chains, quota-aware routing, and a local vector store instead of a hosted vector database.

## Architecture

```
Telegram (aiogram)
        │
        ▼
   Router ──────► Model fallback chain (6 tiers, quota-aware)
        │
        ├──► RAG memory   embed → SQLite (BLOB vectors) → cosine top-k → context
        │
        └──► Tools        Google Calendar · Gmail · Todoist · web search
                          PDF parsing · esports fixtures · image generation
```

### Retrieval-augmented memory

Messages are embedded with `text-embedding-004` (`task_type=RETRIEVAL_DOCUMENT`) and stored as BLOBs alongside their text in SQLite. At query time the incoming message is embedded, scored against every stored vector with a hand-written cosine similarity, and the top matches are injected into the prompt.

No external vector database — SQLite plus NumPy is sufficient at this scale and keeps the deployment to a single file on disk.

### Model fallback

Free-tier models have low daily request limits, so a single model is not a reliable dependency. Requests walk an ordered chain, best quality first, degrading toward higher-quota models as limits are hit:

```python
TEXT_MODEL_FALLBACKS = [
    "gemini-3.5-flash",       # best quality      —  20 RPD
    "gemini-3-flash",         # 2nd best          —  20 RPD
    "gemini-2.5-flash",       # 3rd best          —  20 RPD
    "gemini-3.1-flash-lite",  # high-volume       — 500 RPD
    "gemini-2.5-flash-lite",  # high-volume       —  20 RPD
    "gemini-2.0-flash",       # final safety net  — always-on
]
```

A separate chain handles image generation. The bot stays responsive even when every premium tier is exhausted.

### Integrations

| Service | Capability |
|---|---|
| Google Calendar | read, create, and modify events (OAuth 2.0) |
| Gmail | read and send (OAuth 2.0) |
| Todoist | task management |
| DuckDuckGo | live web search |
| PandaScore | esports fixtures and results |
| PDF / HTML | document parsing and summarization |

Multiple Google accounts are supported — each is authorized separately and stored as its own token file.

## Stack

Python · aiogram · google-generativeai · SQLite · httpx · BeautifulSoup · pypdf · Honcho

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in your keys
python auth_calendar.py     # once per Google account
python main.py
```

Deploy with the included `hermes.service`:

```bash
sudo cp hermes.service /etc/systemd/system/
sudo systemctl enable --now hermes
```

`Restart=always` keeps it up across crashes and reboots.

## Configuration

All secrets come from the environment — nothing is hardcoded. See `.env.example`.

| Variable | Purpose |
|---|---|
| `BOT_TOKEN` | Telegram bot token |
| `GEMINI_API_KEY` | Google Generative AI |
| `ALLOWED_USER_ID` | Telegram user ID permitted to use the bot |
| `HONCHO_API_KEY` | user-memory service |
| `TODOIST_TOKEN` | Todoist API |
| `PANDASCORE_TOKEN` | esports data |

Access is restricted to `ALLOWED_USER_ID`; every handler checks it before doing any work.

## License

MIT

---

Built by [Elmir Abdullaiev](https://github.com/elmir132)

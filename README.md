# AdverRocketBot

AdverRocketBot is a Telegram-based marketplace bot connecting buyers (who want to buy targeted actions like subscriptions) and sellers (who complete tasks for money).

## Architecture

This project is built using:
- **Python 3.12**
- **Aiogram 3.x** for Telegram Bot API
- **SQLAlchemy 2.0 + aiosqlite** for async database interactions
- **Alembic** for database migrations
- **SQLite** as the primary storage engine

### Concurrency and SQLite WAL

Unlike typical architectures that depend on external databases like PostgreSQL or Redis for concurrency and locking, AdverRocketBot uses **SQLite in WAL (Write-Ahead Logging) mode** as its sole persistence layer.

This provides extreme simplicity (no external services needed) without sacrificing concurrency safety. The engine is configured with:
- `PRAGMA journal_mode=WAL` — Allows simultaneous readers while writing.
- `PRAGMA busy_timeout=5000` — Ensures SQLite gracefully waits for locks.
- A custom `run_atomic` wrapper in `app/db/engine.py` using `BEGIN IMMEDIATE` to prevent read-upgrade deadlocks.
- Automatic exponential backoff retries for any `SQLITE_BUSY` errors.

This guarantees that all financial transactions, balance updates, and campaign completion race conditions are perfectly serialized at the database level.

## Getting Started

1. Copy `.env.example` to `.env` and fill in your Bot Token and Crypto Pay token.
2. Ensure you have Python 3.12+ installed.
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Run Alembic migrations to setup the database:
   ```bash
   alembic upgrade head
   ```
5. Start the bot (Uses Long Polling, NO webhooks required):
   ```bash
   python main.py
   ```

## Development and Testing
Concurrency and integration tests run via `pytest`. They simulate multi-user race conditions directly against an isolated SQLite test database to guarantee the atomic locking logic holds up under heavy load.

```bash
pytest tests/
```

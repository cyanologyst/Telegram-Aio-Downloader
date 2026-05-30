# Development Notes

## Commands

```bash
pip install -r requirements/dev.txt
ruff check app tests
black app tests
isort app tests
mypy app
pytest
```

## Refactoring Rules

- Preserve current Telegram behavior while extracting modules.
- Do not put new business logic in `app.bot.telegram_bot`.
- Prefer services and provider interfaces.
- Keep filesystem writes inside configured runtime directories.
- Do not commit `.env`, sessions, downloads, logs, or generated archives.

## Known Migration Boundary

`app.bot.telegram_bot` is intentionally retained as a compatibility runtime after the first restructuring pass. It should shrink over time as services are extracted.


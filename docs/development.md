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

## Testing the bot without Telegram

`tests/fakes.py` provides fake updates, callback queries and a recording bot.
Handlers can be called directly:

```python
context = make_context()
await tb.on_button(callback_update(context, "nav:settings"), context)
assert context.bot.texts()[-1].startswith("⚙️ <b>Settings</b>")
```

The fake callback query raises like Telegram does when a query is answered
twice. `tests/unit/test_wiring.py` runs `main()` with polling stubbed out to
check that every handler is registered.

## Refactoring Rules

- Preserve current Telegram behavior while extracting modules.
- Do not put new business logic in `app.bot.telegram_bot`.
- Prefer services and provider interfaces.
- Keep filesystem writes inside configured runtime directories.
- Do not commit `.env`, sessions, downloads, logs, or generated archives.

## Known Migration Boundary

`app.bot.telegram_bot` is intentionally retained as a compatibility runtime after the first restructuring pass. It should shrink over time as services are extracted.


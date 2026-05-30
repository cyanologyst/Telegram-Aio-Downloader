# Contributing

Thanks for helping improve the bot.

## Local Setup

```bash
pip install -r requirements/dev.txt
cp .env.example .env
python main.py
```

## Quality Checks

Run before opening a PR:

```bash
ruff check app tests
black --check app tests
isort --check-only app tests
mypy app
pytest
```

## Design Guidelines

- Keep Telegram handlers thin.
- Put business logic in `app/services`.
- Add new download providers under `app/downloaders`.
- Prefer typed dataclasses for internal models.
- Avoid global mutable state in new code.
- Preserve existing bot behavior unless a change is explicitly documented.


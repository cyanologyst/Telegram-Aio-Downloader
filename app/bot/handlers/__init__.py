"""Bot handlers split out of :mod:`app.bot.telegram_bot`.

``telegram_bot`` stays the home of configuration and shared state and
re-exports every handler, so these modules read that state through
``tb.<name>`` at call time. Importing any handler module loads the facade
first so that circular import always resolves the same way.
"""

from app.bot import telegram_bot  # noqa: F401

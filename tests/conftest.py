"""Keep test runs out of the real bot log."""

import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="tg-bot-test-logs-"))

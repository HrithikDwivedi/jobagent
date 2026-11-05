"""Auto-apply worker (Playwright). Entry point: run_autoapply(db, cfg, ...). See worker.py for the safety model."""
from .worker import chromium_available, run_autoapply

__all__ = ["run_autoapply", "chromium_available"]

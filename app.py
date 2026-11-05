"""Vercel entry point (FastAPI zero-config): Vercel serves the ASGI `app` object from this file."""
from jobagent.web.app import create_app

app = create_app()

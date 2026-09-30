"""ASGI entrypoint: ``uvicorn app.api.run:app`` (run from backend/)."""

from app.api.server import create_app
from app.config import Settings

app = create_app(Settings())

if __name__ == "__main__":  # pragma: no cover - manual serve helper
    import uvicorn

    _s = Settings()
    uvicorn.run(app, host=_s.API_HOST, port=_s.API_PORT)

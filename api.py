"""Compatibility ASGI entry point for the structured web application."""

from webapp.main import app, create_app

__all__ = ["app", "create_app"]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("webapp.main:app", host="127.0.0.1", port=8000, reload=True)

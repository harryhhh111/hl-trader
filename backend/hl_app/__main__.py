from __future__ import annotations

import uvicorn

from .api import create_app
from .config import Settings


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(create_app(), host=settings.host, port=settings.port, workers=1)


if __name__ == "__main__":
    main()

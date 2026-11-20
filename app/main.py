"""FastAPI 入口：网页 + REST API。"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .sample import build_sample_manifest
from .store import CorruptStoreError, NotFoundError, Store, StoreError

STATIC_DIR = Path(__file__).parent / "static"


def create_app(data_dir: str | None = None) -> FastAPI:
    app = FastAPI(title="虚拟机备份维护台")
    store = Store(data_dir or os.environ.get("DATA_DIR", "./data"))

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(StoreError)
    async def _store_error(_: Request, exc: StoreError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(CorruptStoreError)
    async def _corrupt(_: Request, exc: CorruptStoreError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=500)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/state")
    async def get_state() -> dict:
        return store.state()

    @app.get("/api/sample")
    async def get_sample() -> dict:
        return build_sample_manifest()

    @app.post("/api/import")
    async def import_manifest(request: Request) -> dict:
        try:
            payload = await request.json()
        except Exception:
            raise StoreError("请求体不是合法 JSON")
        return store.import_manifest(payload)

    @app.post("/api/remove/{layer_id}")
    async def remove_layer(layer_id: str) -> dict:
        return store.remove(layer_id)

    @app.get("/api/download/{layer_id}")
    async def download_layer(layer_id: str) -> Response:
        data = store.resolve_image(layer_id)
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{layer_id}.raw"',
                "Content-Length": str(len(data)),
            },
        )

    return app


# uvicorn 入口（本地默认 ./data，容器内由 DATA_DIR 环境变量指定 /data）
app = create_app()

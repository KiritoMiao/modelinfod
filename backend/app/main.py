import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .litellm_map import LiteLLMMap
from .openrouter import OpenRouterCatalog
from .overrides import OverridesStore, normalize_override
from .routers import admin, public
from .store import Store
from .upstream import fetch_upstream_models

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


async def _sync_upstream_once(app: FastAPI) -> None:
    settings = get_settings()
    try:
        ids = await fetch_upstream_models(settings)
        await app.state.store.set_upstream_models(ids)
    except Exception as exc:
        logger.warning("upstream sync failed: %s", exc)
        await app.state.store.set_upstream_error(f"{type(exc).__name__}: {exc}")


async def _sync_loop(app: FastAPI, interval: int) -> None:
    while True:
        await asyncio.sleep(interval)
        await _sync_upstream_once(app)


async def _prices_loop(app: FastAPI, interval: int) -> None:
    settings = get_settings()
    while True:
        try:
            await app.state.litellm_map.refresh_from_url(settings.prices_refresh_url)
            await app.state.store.set_prices_refreshed(settings.prices_refresh_url)
        except Exception as exc:
            logger.warning("litellm map refresh failed: %s", exc)
        await asyncio.sleep(interval)


async def _openrouter_loop(app: FastAPI, interval: int) -> None:
    settings = get_settings()
    while True:
        try:
            await app.state.openrouter.refresh(settings.openrouter_models_url)
        except Exception as exc:
            logger.warning("openrouter catalog refresh failed: %s", exc)
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.store = Store(settings.data_dir)
    app.state.litellm_map = LiteLLMMap()
    app.state.litellm_map.load_bundled()
    app.state.openrouter = OpenRouterCatalog(settings.data_dir)
    app.state.openrouter.load_cached()
    app.state.overrides = OverridesStore(settings.overrides_path)

    # One-time migration: overrides used to live inside state.json.
    legacy = app.state.store.pop_legacy_overrides()
    for name, info in legacy.items():
        cleaned = normalize_override({"model_info": info})
        if cleaned:
            await app.state.overrides.set(name, cleaned)
    if legacy:
        logger.info("migrated %d legacy overrides to %s", len(legacy), settings.overrides_path)

    await _sync_upstream_once(app)

    tasks: list[asyncio.Task] = []
    if settings.sync_interval_seconds > 0:
        tasks.append(asyncio.create_task(_sync_loop(app, settings.sync_interval_seconds)))
    if settings.prices_refresh_interval_seconds > 0:
        tasks.append(
            asyncio.create_task(_prices_loop(app, settings.prices_refresh_interval_seconds))
        )
    if settings.openrouter_refresh_interval_seconds > 0:
        tasks.append(
            asyncio.create_task(
                _openrouter_loop(app, settings.openrouter_refresh_interval_seconds)
            )
        )
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="modelinfod", lifespan=lifespan, docs_url=None, redoc_url=None)

app.include_router(public.router)
app.include_router(admin.router, prefix="/modelinfod/api")

# Admin SPA. nginx/cloudflared routes /modelinfod/* here; assets are served
# from the build output and any other path falls through to index.html.
if (STATIC_DIR / "index.html").is_file():
    app.mount(
        "/modelinfod/assets",
        StaticFiles(directory=STATIC_DIR / "assets"),
        name="admin-assets",
    )

    @app.get("/modelinfod")
    @app.get("/modelinfod/{rest:path}")
    async def admin_spa(rest: str = ""):
        candidate = (STATIC_DIR / rest).resolve()
        if (
            rest
            and candidate.is_file()
            and candidate.is_relative_to(STATIC_DIR.resolve())
        ):
            return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    run()

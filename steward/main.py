import asyncio
import logging

import uvicorn
from fastapi import FastAPI

from steward.config import AgentConfig
from steward.routers import admin, backup, health, ingress, restore, ssh_forwards, vms
from steward.services import ingress_caddy, ingress_reconciler


logger = logging.getLogger(__name__)


def build_app(config: AgentConfig) -> FastAPI:
    app = FastAPI(title='steward', docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.include_router(health.router)
    app.include_router(vms.router)
    app.include_router(ssh_forwards.router)
    app.include_router(admin.router)
    app.include_router(ingress.router)
    app.include_router(backup.router)
    app.include_router(restore.router)

    @app.on_event('startup')
    async def _start_reconciler() -> None:
        try:
            await asyncio.to_thread(ingress_caddy.configure_caddy_mode, False)
        except Exception:
            logger.exception('configure_caddy_mode (startup default) failed')
        asyncio.create_task(ingress_reconciler.startup_reconcile(config))
        app.state.reconcile_task = asyncio.create_task(ingress_reconciler.reconcile_loop(config))
        logger.info('ingress reconciler started')

    @app.on_event('shutdown')
    async def _stop_reconciler() -> None:
        task = getattr(app.state, 'reconcile_task', None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    return app


def main() -> None:
    config = AgentConfig.load()
    app = build_app(config)
    uvicorn.run(app, host=config.bind_host, port=config.bind_port, log_level='info')


if __name__ == '__main__':
    main()

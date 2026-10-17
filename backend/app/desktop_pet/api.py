"""Owner settings use role IDs; archives and asset requests never accept disk paths."""
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .packages import MAX_ZIP_BYTES


class PetSettingsInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expectedRevision: int = Field(ge=0)
    packageId: str | None = None
    enabled: bool = False


def router(get_application):
    routes = APIRouter(prefix='/api/roles/{role_id}/desktop-pet')

    def fail(error):
        if isinstance(error, KeyError):
            return HTTPException(404, detail='角色或桌宠包不存在')
        if isinstance(error, ValueError):
            return HTTPException(400, detail=str(error)[:256])
        return HTTPException(503, detail='桌宠资源暂不可用')

    @routes.get('')
    def status(role_id: str):
        try:
            return get_application().status(role_id)
        except (KeyError, ValueError, OSError) as error:
            raise fail(error) from error

    @routes.post('/packages', status_code=201)
    async def import_package(role_id: str, request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_ZIP_BYTES:
                raise HTTPException(413, detail='桌宠 ZIP 最大32MiB')
            raw.extend(chunk)
        try:
            return await get_application().import_package(role_id, bytes(raw))
        except (KeyError, ValueError, OSError) as error:
            raise fail(error) from error

    @routes.put('')
    async def configure(role_id: str, value: PetSettingsInput):
        try:
            return await get_application().configure(role_id, expected_revision=value.expectedRevision,
                package_id=value.packageId, enabled=value.enabled)
        except (KeyError, ValueError, OSError) as error:
            raise fail(error) from error

    @routes.get('/packages/{package_id}/assets/{asset}')
    def asset(role_id: str, package_id: str, asset: str):
        try:
            raw, media_type = get_application().store.asset(role_id, package_id, asset)
            return Response(raw, media_type=media_type, headers={'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'private'})
        except (KeyError, ValueError, OSError) as error:
            raise fail(error) from error

    @routes.post('/restart')
    async def restart(role_id: str):
        try:
            return await get_application().restart(role_id)
        except (KeyError, ValueError, OSError, RuntimeError) as error:
            raise fail(error) from error

    @routes.delete('/packages/{package_id}')
    async def remove_package(role_id: str, package_id: str, expectedRevision: int):
        try:
            return await get_application().remove_package(role_id, package_id, expectedRevision)
        except (KeyError, ValueError, OSError) as error:
            raise fail(error) from error

    return routes

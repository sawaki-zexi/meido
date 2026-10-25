"""Local-owner HTTP routes for browsing the role's existing material files."""
from urllib.parse import unquote, quote
import sqlite3

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from .agent_runtime.file_tools import _FileFailure
from .chat_attachments import read_request_bytes
from .material_library import InvalidMaterial, MaterialConflict, MaterialGone, MaterialParameters
from .proactive_runtime.meme_storage import MemeQuotaError
from .proactive_runtime.retention import StorageLimitError


def material_router(application):
    router = APIRouter(prefix='/api/roles/{role_id}/materials')

    async def invoke(method, *args):
        try:
            return await run_in_threadpool(lambda: getattr(application(), method)(*args))
        except KeyError:
            raise HTTPException(404, '角色不存在') from None
        except MaterialGone:
            raise HTTPException(404, '素材已移出、被替换或不属于当前角色，请刷新素材库') from None
        except InvalidMaterial:
            raise HTTPException(415, '仅支持完整有效的 PNG、JPEG、WebP、GIF 图片；图片或动画超过解码限制') from None
        except MaterialConflict:
            raise HTTPException(409, '素材库已经变化，请刷新后重新确认；不要直接重复提交') from None
        except (MemeQuotaError, StorageLimitError):
            raise HTTPException(413, '当前受管素材库空间或条目配额不足；原有素材未被删除') from None
        except _FileFailure as error:
            if error.status == 'path_busy':
                raise HTTPException(409, '素材正在由其他操作使用，请刷新后重试') from None
            raise HTTPException(503, '素材路径暂不可安全读取，请检查本地工作区') from None
        except MaterialParameters:
            raise HTTPException(422, '素材参数无效，请检查分类与说明') from None
        except ValueError:
            raise HTTPException(503, '素材库暂不可安全读取，请检查本地清单或刷新核对') from None
        except (OSError, PermissionError, sqlite3.Error):
            raise HTTPException(503, '素材库暂不可安全读取或操作结果尚未确认，请刷新核对后再操作') from None

    @router.get('')
    async def listing(role_id: str, query: str = Query('', max_length=2000), category: str = Query('', max_length=80)):
        return await invoke('list', role_id, query, category)

    @router.get('/{material_id}/image')
    async def image(role_id: str, material_id: str):
        raw, item = await invoke('image', role_id, material_id)
        return Response(raw, media_type=item['mediaType'], headers={
            'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
            'Content-Disposition': "inline; filename*=UTF-8''" + quote(item['name'], safe='')})

    @router.post('')
    async def import_image(role_id: str, request: Request,
            expectedRevision: str = Query(..., pattern=r'^[a-f0-9]{64}$'),
            category: str = Query(..., min_length=1, max_length=80),
            description: str = Query(..., min_length=1, max_length=2000)):
        raw = await read_request_bytes(request)
        filename = unquote(request.headers.get('x-material-filename', 'image'))[:1024]
        return await invoke('import_image', role_id, raw, filename,
            request.headers.get('content-type', 'application/octet-stream'), category, description, expectedRevision)

    @router.delete('/{material_id}')
    async def remove(role_id: str, material_id: str, expectedRevision: str = Query(..., pattern=r'^[a-f0-9]{64}$')):
        return await invoke('remove', role_id, material_id, expectedRevision)

    return router

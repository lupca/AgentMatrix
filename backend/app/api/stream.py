from fastapi import APIRouter
from fastapi.responses import StreamingResponse
router = APIRouter(prefix='/api', tags=['stream'])

@router.get('/runs/{run_id}/stream')
async def stream_output(run_id: str):
    async def gen():
        yield 'data: connected\n\n'
    return StreamingResponse(gen(), media_type='text/event-stream')

"""Protected scrape endpoints; fixed internal targets preserve network isolation."""
import httpx
from fastapi import HTTPException, Request, Response
from .metrics import DatabaseCollector
from .telemetry import Metrics, ObserveHTTP, CONTENT_TYPE_LATEST, metrics_authorized


def install(app, settings):
    metrics = Metrics('app'); metrics.registry.register(DatabaseCollector(settings))
    app.state.metrics = metrics
    app.add_middleware(ObserveHTTP,metrics=metrics)

    def authorize(request):
        if not metrics_authorized(request.headers,settings.metrics_token_file):
            raise HTTPException(401,detail='Metrics token required')

    @app.get('/metrics',include_in_schema=False)
    def own(request:Request):
        authorize(request)
        return Response(metrics.render(),media_type=CONTENT_TYPE_LATEST,headers={'Cache-Control':'no-store'})

    @app.get('/metrics/{component}',include_in_schema=False)
    async def proxy(component:str,request:Request):
        authorize(request)
        targets={'worker':'http://worker:9000/metrics','inference':settings.inference_url+'/metrics'}
        if component not in targets or (component=='inference' and not settings.inference_url): raise HTTPException(404)
        try:
            async with httpx.AsyncClient(timeout=5,trust_env=False,follow_redirects=False) as client:
                async with client.stream('GET',targets[component],headers={'Authorization':request.headers['authorization']}) as upstream:
                    if upstream.status_code!=200: raise HTTPException(503,detail='Component metrics unavailable')
                    body=bytearray()
                    async for block in upstream.aiter_bytes():
                        body.extend(block)
                        if len(body)>2*1024**2: raise HTTPException(503,detail='Component metrics too large')
            return Response(bytes(body),media_type=CONTENT_TYPE_LATEST,headers={'Cache-Control':'no-store'})
        except httpx.HTTPError:
            raise HTTPException(503,detail='Component metrics unavailable') from None

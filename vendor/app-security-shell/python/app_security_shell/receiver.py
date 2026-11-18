"""Static ASGI protection before native/config initialization, independent of apps."""
import asyncio
from urllib.parse import unquote
from .routing import matches, valid_path
from .transport import CARRIER, Middleware


class GuardedASGI:
    def __init__(self,app,*,binding,initialization_timeout=30,request_timeout=90,handler_timeout=30,rejection_timeout=2,
                 stream_timeout=3600,stream_idle_timeout=60):
        for value in (initialization_timeout,request_timeout,handler_timeout,rejection_timeout,stream_timeout,stream_idle_timeout):
            if type(value) not in (int,float) or not 0<value<=3600:raise ValueError('invalid_timeout')
        self.app,self.binding=app,binding
        self.initialization_timeout,self.request_timeout=initialization_timeout,request_timeout
        self.handler_timeout,self.rejection_timeout=handler_timeout,rejection_timeout
        self.stream_timeout,self.stream_idle_timeout=stream_timeout,stream_idle_timeout
        self.catalog=binding.receiver_catalog
        self._middleware=None

    async def _reject(self,scope,send):
        try:
            async with asyncio.timeout(self.rejection_timeout):
                if scope['type']=='websocket':return await send({'type':'websocket.close','code':1008})
                await send({'type':'http.response.start','status':503,'headers':[(b'content-type',b'application/json')]})
                await send({'type':'http.response.body','body':b'' if scope.get('method')=='HEAD' else b'{"error":"app_security_rejected"}'})
        except TimeoutError:pass

    def _protected(self,path):
        # Conservative classification only; exact safe spelling is checked below.
        for value in (path,unquote(path)):
            candidate=value.rstrip('/') or '/'
            if candidate==CARRIER:return True
            paths = {e['path'] for e in self.catalog} | {p for _, p in self.binding.receiver_routes()}
            for path in paths:
                a,b=(path.rstrip('/') or '/').split('/'),candidate.split('/')
                if len(a)==len(b) and all(bool(y) if x.startswith('{') else x==y for x,y in zip(a,b)):return True
        return False

    async def __call__(self,scope,receive,send):
        if scope['type'] not in ('http','websocket'):return await self.app(scope,receive,send)
        root=scope.get('root_path','').rstrip('/')
        path=scope['path']
        if root and path.startswith(root+'/'):path=path[len(root):]
        elif root and path==root:path='/'
        if not self._protected(path):return await self.app(scope,receive,send)
        raw=scope.get('raw_path',(root+path).encode('utf-8'))
        raw=raw.split(b'?',1)[0] # ASGI transports may include raw query here.
        correct_raw=raw in ((root+path).encode('utf-8'),path.encode('utf-8'))
        method=scope.get('method')
        declared=method=='POST' if path==CARRIER else any(e['method']==method and matches(e['path'],path) for e in self.catalog)
        if scope['type']!='http' or not valid_path(path) or not correct_raw or b'%' in raw or not declared:
            return await self._reject(scope,send)
        entered=False;started=False
        async def tracked_send(message):
            nonlocal started
            if message['type']=='http.response.start':started=True
            await send(message)
        try:
            self.binding.enter();entered=True
            async with asyncio.timeout(self.initialization_timeout):await self.binding.ready()
            if self._middleware is None:
                self._middleware=Middleware(self.app,self.binding,self.binding,jobs=self.binding.jobs,
                    trusted_ingress=self.binding.trusted_ingress,clock=self.binding.clock,handler_timeout=self.handler_timeout,rejection_timeout=self.rejection_timeout,
                    stream_timeout=self.stream_timeout,stream_idle_timeout=self.stream_idle_timeout)
            inner=dict(scope);inner.pop('app_security.admitted',None)
            async with asyncio.timeout(self.request_timeout) as budget:
                await self._middleware(inner,receive,tracked_send,request_budget=budget)
        except asyncio.CancelledError:raise
        except Exception:
            if not started:await self._reject(scope,send)
        finally:
            if entered:self.binding.leave()

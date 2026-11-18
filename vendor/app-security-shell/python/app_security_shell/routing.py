"""Finite public route grammar; static admission works without a native import."""
import re
from urllib.parse import urlsplit
import httpx

METHODS = frozenset(('GET','HEAD','OPTIONS','POST','PUT','PATCH','DELETE'))


def valid_path(path):
    return (isinstance(path,str) and path.startswith('/') and len(path)<=4096
            and path.isascii() and all(33<=ord(c)<127 and c not in '\\%?#{}' for c in path)
            and '//' not in path and not any(s in ('.','..') for s in path.split('/')))


def valid_template(path):
    if valid_path(path):return True
    if not isinstance(path,str):return False
    names=[];segments=[]
    for part in path.split('/'):
        if part.startswith('{') and part.endswith('}'):
            name=part[1:-1]
            if not re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,63}',name) or name in names:return False
            names.append(name);segments.append('placeholder')
        else:segments.append(part)
    return bool(names) and len(path)<=4096 and valid_path('/'.join(segments))


def matches(template,path):
    if not valid_path(path):return False
    a,b=template.split('/'),path.split('/')
    return len(a)==len(b) and all(bool(y) if x.startswith('{') and x.endswith('}') else x==y for x,y in zip(a,b))


def validate_catalog(catalog):
    if not isinstance(catalog,list) or len(catalog)>163:raise ValueError('invalid_catalog')
    ids=set();routes=set();previous=[]
    fields={'endpoint_id','receiver_app','permitted_peer_apps','method','path','headers','off_supported'}
    for e in catalog:
        if not isinstance(e,dict) or (not fields <= set(e) or bool(set(e)-fields-{'response_stream','proxy_headers','max_response_bytes'})) or not valid_template(e['path']) or e['method'] not in METHODS:
            raise ValueError('invalid_catalog')
        if any(type(e.get(k,False)) is not bool for k in ('response_stream','proxy_headers')) or (e.get('response_stream') and e['method']!='GET'):raise ValueError('invalid_catalog')
        limit=e.get('max_response_bytes',1048576)
        if type(limit) is not int or not 1048576<=limit<=8388608 or (limit!=1048576 and (e['method']!='GET' or e.get('response_stream',False))):raise ValueError('invalid_catalog')
        if not isinstance(e['endpoint_id'],str) or not isinstance(e['receiver_app'],str):raise ValueError('invalid_catalog')
        if e['off_supported'] is not True or any(not isinstance(e[k],list) or any(not isinstance(v,str) for v in e[k]) for k in ('headers','permitted_peer_apps')):raise ValueError('invalid_catalog')
        route=(e['receiver_app'],e['method'],e['path'])
        if e['endpoint_id'] in ids or route in routes:raise ValueError('ambiguous_catalog')
        ids.add(e['endpoint_id']);routes.add(route)
        for old in previous:
            if old['receiver_app']!=e['receiver_app'] or old['method']!=e['method']:continue
            a,b=old['path'].split('/'),e['path'].split('/')
            if '{' in old['path'] and '{' in e['path'] and len(a)==len(b) and all(x==y or x.startswith('{') or y.startswith('{') for x,y in zip(a,b)):
                raise ValueError('ambiguous_catalog')
        previous.append(e)


def normalize_base(value):
    if not isinstance(value,str) or not value or not value.isascii() or any(ord(c)<33 or ord(c)>126 for c in value):raise ValueError('invalid_destination')
    parts=urlsplit(value)
    if parts.scheme not in ('http','https') or not parts.hostname or parts.username is not None or parts.password is not None or '?' in value or '#' in value or '\\' in value:
        raise ValueError('invalid_destination')
    if parts.path and not valid_path(parts.path):raise ValueError('invalid_destination')
    if parts.port is not None and not 1<=parts.port<=65535:raise ValueError('invalid_destination')
    url=httpx.URL(value)
    if url.raw_path.split(b'?')[0]!=(parts.path or '/').encode():raise ValueError('invalid_destination')
    if '%' in parts.netloc:raise ValueError('invalid_destination')
    return str(url).rstrip('/')


def normalized_catalog(catalog):
    import copy
    validate_catalog(catalog)
    result=copy.deepcopy(catalog)
    for endpoint in result:
        if endpoint.get('max_response_bytes') == 1048576: endpoint.pop('max_response_bytes')
        for name in ('response_stream','proxy_headers'):
            if endpoint.get(name) is False: endpoint.pop(name)
    return result

"""Bounded AVIF container admission; native full decode remains mandatory."""
MAX_BYTES=1_048_576

def boxes(data,start=0,end=None):
 end=len(data) if end is None else end
 count=0
 while start<end:
  if end-start<8:raise ValueError('avif_box_truncated')
  size=int.from_bytes(data[start:start+4],'big');kind=data[start+4:start+8]
  if size<8 or size>end-start:raise ValueError('avif_box_size')
  count+=1
  if count>128:raise ValueError('avif_box_count')
  yield kind,start+8,start+size
  start+=size

def validate(data, maximum=MAX_BYTES):
 if not 24<=len(data)<=maximum:raise ValueError('avif_size')
 top=list(boxes(data));kinds=[k for k,_,_ in top]
 if kinds.count(b'ftyp')!=1 or kinds.count(b'meta')!=1 or kinds.count(b'mdat')!=1 or any(k not in (b'ftyp',b'meta',b'mdat',b'free') for k in kinds):raise ValueError('avif_structure')
 f=next((a,b) for k,a,b in top if k==b'ftyp');brands=[data[i:i+4] for i in range(f[0],f[1],4)]
 if f[1]-f[0]<8 or (f[1]-f[0])%4:raise ValueError('avif_brand_size')
 if brands[0]!=b'avif' or b'avis' in brands:raise ValueError('avif_still_required')
 a,b=next((a,b) for k,a,b in top if k==b'meta')
 if data[a:a+4]!=b'\0'*4:raise ValueError('avif_meta_version')
 children=list(boxes(data,a+4,b));types=[k for k,_,_ in children]
 if any(types.count(k)!=1 for k in (b'hdlr',b'pitm',b'iloc',b'iinf',b'iprp')) or any(k not in (b'hdlr',b'pitm',b'iloc',b'iinf',b'iprp',b'iref') for k in types):raise ValueError('avif_meta_structure')
 a,b=next((a,b) for k,a,b in children if k==b'iinf')
 if b-a<6:raise ValueError('avif_items_truncated')
 version=data[a]
 if version not in (0,1):raise ValueError('avif_items_version')
 width=2 if version==0 else 4;count=int.from_bytes(data[a+4:a+4+width],'big')
 entries=list(boxes(data,a+4+width,b))
 if not 1<=count<=16 or count!=len(entries):raise ValueError('avif_items')
 for kind,x,y in entries:
  if kind!=b'infe' or y-x<4 or data[x] not in (2,3):raise ValueError('avif_item_version')
  offset=8 if data[x]==2 else 10
  if y-x<offset+5 or data[x+offset:x+offset+4] not in (b'av01',b'grid'):raise ValueError('avif_metadata_forbidden')
 a,b=next((a,b) for k,a,b in children if k==b'iprp')
 for kind,x,y in boxes(data,a,b):
  if kind not in (b'ipco',b'ipma'):raise ValueError('avif_properties')
  if kind==b'ipco':
   for prop,u,v in boxes(data,x,y):
    if prop not in (b'ispe',b'pixi',b'av1C',b'colr',b'auxC'):raise ValueError('avif_transform_or_metadata')
    if prop==b'auxC' and data[u:v]!=b'\0'*4+b'urn:mpeg:mpegB:cicp:systems:auxiliary:alpha\0':raise ValueError('avif_auxiliary_forbidden')
    if prop==b'colr' and data[u:u+4]!=b'nclx':raise ValueError('avif_icc_forbidden')
 return data

"""Client-optimized image admission; full isolated decode and AV remain mandatory."""
from .client_avif import validate as avif

def optimized(data,mime):
 if not 0<len(data)<=1048576:return False
 try:
  if mime=='image/avif':avif(data);return True
  if mime=='image/png':
   if data[:8]!=b'\x89PNG\r\n\x1a\n':return False
   p=8;end=False
   while p<len(data):
    if p+12>len(data):return False
    n=int.from_bytes(data[p:p+4],'big');kind=data[p+4:p+8]
    if n>len(data)-p-12 or kind not in (b'IHDR',b'PLTE',b'IDAT',b'IEND',b'tRNS',b'sRGB',b'gAMA',b'cHRM',b'pHYs'):return False
    p+=n+12
    if kind==b'IEND':end=True;break
   return end and p==len(data)
  if mime=='image/webp':
   if data[:4]!=b'RIFF' or data[8:12]!=b'WEBP' or int.from_bytes(data[4:8],'little')+8!=len(data):return False
   p=12;image=False
   while p<len(data):
    if p+8>len(data):return False
    kind=data[p:p+4];n=int.from_bytes(data[p+4:p+8],'little')
    if n>len(data)-p-8 or kind not in (b'VP8 ',b'VP8L',b'VP8X',b'ALPH'):return False
    if kind==b'VP8X' and (n!=10 or data[p+8]&0x2e):return False
    image|=kind in (b'VP8 ',b'VP8L');p+=8+n+(n%2)
   return image and p==len(data)
  if mime=='image/jpeg':
   if data[:2]!=b'\xff\xd8' or data[-2:]!=b'\xff\xd9':return False
   p=2
   while p<len(data):
    if data[p]!=255:return False
    while p<len(data) and data[p]==255:p+=1
    if p>=len(data):return False
    marker=data[p];p+=1
    if marker==0xd9:return p==len(data)
    if marker==0xda:
     if p+2>len(data):return False
     n=int.from_bytes(data[p:p+2],'big')
     if n<2 or p+n>len(data):return False
     p+=n
     while p<len(data):
      if data[p]!=255:p+=1;continue
      start=p;p+=1
      while p<len(data) and data[p]==255:p+=1
      if p>=len(data):return False
      if data[p]==0 or 0xd0<=data[p]<=0xd7:p+=1;continue
      p=start;break
     continue
    if marker in (0xe1,0xe2,0xed,0xfe):return False
    if p+2>len(data):return False
    n=int.from_bytes(data[p:p+2],'big')
    if n<2 or p+n>len(data):return False
    p+=n
 except (ValueError,OverflowError,IndexError):return False
 return False

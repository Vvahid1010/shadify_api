"""Fixed sandboxed probe/full decode/precomputed encodes, never live playback."""
import hashlib,json,math,os,re,struct,time,zlib
from pathlib import Path
from .clamav_scanner import run_bounded,EvidenceInvalid
from .scanner_sandbox import NativeSandbox
from .musicbrainz import text

PIPELINE_VERSION='mp3-cover-v1'
class ToolFailure(Exception):
    def __init__(self,code):self.code=code

class MediaTools:
    def __init__(self,*,bwrap,runtime_root,prlimit,temp_dir,version,limits,lock_fd=None,runtime_sha256=None):
        self.sandbox=NativeSandbox(bwrap=bwrap,runtime_root=runtime_root);self.prlimit=str(prlimit)
        self.temp_dir=str(temp_dir);self.version=version;self.limits=limits;self.lock_fd=lock_fd;self.identity=version+':'+str(runtime_sha256)
    def run(self,tool,options,fd,*,deadline,maximum=65536):
        if tool not in ('ffmpeg','ffprobe'):raise ValueError('Fixed media tools only')
        import tempfile
        try:
            with tempfile.TemporaryDirectory(prefix='shadify-media-',dir=self.temp_dir) as workspace:
                argv=self.sandbox.argv(definitions=None,workspace=workspace,options=options,lock_fd=self.lock_fd,scratch_bytes=self.limits.max_scan_bytes)
                argv[-len(options)-1]='/usr/bin/'+tool
                command=[self.prlimit,'--cpu='+str(self.limits.cpu_seconds),'--as='+str(self.limits.memory_bytes),
                    '--fsize='+str(self.limits.max_scan_bytes),'--nofile=128','--',*argv]
                if fd is not None:os.lseek(fd,0,os.SEEK_SET)
                code,out,err=run_bounded(command,deadline=deadline,output_bytes=maximum,stdin=fd,
                    stdin_bytes=os.fstat(fd).st_size if fd is not None else None,pass_fds=() if self.lock_fd is None else (self.lock_fd,))
                if code or err:raise ToolFailure('validation_invalid')
                return out
        except TimeoutError:raise ToolFailure('processing_timeout') from None
        except FileNotFoundError:raise ToolFailure('processing_unavailable') from None
        except EvidenceInvalid:raise ToolFailure('output_invalid') from None
        except OSError:raise ToolFailure('processing_error') from None
    def preflight(self,*,deadline):
        for tool in ('ffmpeg','ffprobe'):
            try:output=self.run(tool,['-version'],None,deadline=deadline)
            except ToolFailure as exc:raise ToolFailure('processing_timeout' if exc.code=='processing_timeout' else 'processing_unavailable') from None
            if not output.startswith((tool+' version '+self.version+' ').encode()):raise ToolFailure('processing_unavailable')
    def probe(self,fd,*,deadline):
        try:
            data=json.loads(self.run('ffprobe',['-v','error','-protocol_whitelist','pipe','-format_whitelist','wav,flac,mp3,png_pipe,jpeg_pipe,image2pipe',
                '-show_entries','stream=codec_name,codec_type,sample_rate,channels,bits_per_sample,bits_per_raw_sample,width,height:format=format_name:format_tags=title,artist,album,track,ISRC',
                '-of','json','-i','pipe:0'],fd,deadline=deadline))
            if type(data) is not dict or type(data.get('streams')) is not list or len(data['streams'])!=1:raise ValueError()
            return data,data['streams'][0]
        except (ValueError,KeyError,TypeError):raise ToolFailure('validation_invalid') from None
    def decoded(self,fd,stream,*,deadline):
        try:
            if stream['codec_type']=='audio':
                channels=int(stream['channels']);rate=int(stream['sample_rate'])
                if not 1<=channels<=2 or not 8000<=rate<=96000:raise ValueError()
                data=self.run('ffmpeg',['-nostdin','-v','error','-xerror','-err_detect','explode','-threads','1','-protocol_whitelist','pipe',
                    '-i','pipe:0','-map','0:a:0','-vn','-sn','-dn','-ac',str(channels),'-ar',str(rate),'-c:a','pcm_s16le','-f','s16le','pipe:1'],fd,deadline=deadline,maximum=self.limits.max_scan_bytes)
                if not data or len(data)%(channels*2):raise ValueError()
                duration_ms=round(len(data)*1000/(channels*2*rate))
                if not 1<=duration_ms<=3600000:raise ValueError()
                return dict(duration_ms=duration_ms,channels=channels,sample_rate=rate,decoded_bytes=len(data))
            width=int(stream['width']);height=int(stream['height'])
            if not 1<=width<=4096 or not 1<=height<=4096:raise ValueError()
            data=self.run('ffmpeg',['-nostdin','-v','error','-xerror','-err_detect','explode','-threads','1','-protocol_whitelist','pipe',
                '-i','pipe:0','-map','0:v:0','-frames:v','2','-pix_fmt','rgb24','-f','rawvideo','pipe:1'],fd,deadline=deadline,maximum=100663296)
            if len(data)!=width*height*3:raise ValueError()
            return dict(width=width,height=height,frames=1,decoded_bytes=len(data))
        except (ValueError,KeyError,TypeError,OverflowError):raise ToolFailure('validation_invalid') from None
    def validate(self,fd,mime,*,deadline):
        self.preflight(deadline=deadline)
        container_check(fd,mime,self.limits.max_input_bytes)
        report,stream=self.probe(fd,deadline=deadline)
        formats={'audio/wav':'wav','audio/flac':'flac','image/png':'png_pipe','image/jpeg':'jpeg_pipe'}
        expected=formats[mime]
        if report.get('format',{}).get('format_name') not in (expected,'image2pipe' if mime.startswith('image/') else expected):raise ToolFailure('validation_invalid')
        if mime.startswith('audio/'):
            if stream.get('codec_type')!='audio' or not (stream.get('codec_name') in ('pcm_u8','pcm_s16le','pcm_s24le','pcm_s32le') if mime=='audio/wav' else stream.get('codec_name')=='flac'):raise ToolFailure('validation_invalid')
        elif stream.get('codec_type')!='video' or stream.get('codec_name')!=('png' if mime=='image/png' else 'mjpeg'):raise ToolFailure('validation_invalid')
        decoded=self.decoded(fd,stream,deadline=deadline)
        metadata={};tags=report.get('format',{}).get('tags',{})
        if type(tags) is not dict:raise ToolFailure('metadata_invalid')
        try:
            mapping={'title':'title','artist':'artist','album':'album','track':'track_number','isrc':'isrc'}
            for key,value in tags.items():
                if key.lower() in mapping:metadata[mapping[key.lower()]]=text(value,200 if key.lower() in ('title','album') else 120)
        except ValueError:raise ToolFailure('metadata_invalid') from None
        metadata.update({key:value for key,value in decoded.items() if key=='duration_ms'})
        quality=dict(codec=stream['codec_name'],**decoded)
        if mime.startswith('audio/'):
            quality['lossless']=True;quality['bits_per_sample']=int(stream.get('bits_per_raw_sample') or stream.get('bits_per_sample') or 0)
        return dict(passed=True,tool_version=self.version,actual_mime=mime,source_quality=quality),dict(complete=True,metadata=metadata)
    def process(self,fd,mime,validation,*,deadline,roles=None):
        self.preflight(deadline=deadline)
        outputs={}
        if mime.startswith('audio/'):
            for role,bitrate in (('high',320),('low',128)):
                if roles is not None and role not in roles:continue
                data=self.run('ffmpeg',['-nostdin','-v','error','-xerror','-err_detect','explode','-threads','1','-protocol_whitelist','pipe','-i','pipe:0',
                    '-map','0:a:0','-map_metadata','-1','-vn','-sn','-dn','-ac','2','-ar','44100','-c:a','libmp3lame','-b:a',str(bitrate)+'k',
                    '-fflags','+bitexact','-flags:a','+bitexact','-write_xing','0','-id3v2_version','0','-f','mp3','pipe:1'],fd,deadline=deadline,maximum=self.limits.max_scan_bytes)
                inspect_mp3(data,bitrate)
                with bytes_fd(data) as output_fd:
                    _,stream=self.probe(output_fd,deadline=deadline);decoded=self.decoded(output_fd,stream,deadline=deadline)
                if stream.get('codec_name')!='mp3' or decoded['channels']!=2 or decoded['sample_rate']!=44100 or abs(decoded['duration_ms']-validation['source_quality']['duration_ms'])>100:
                    raise ToolFailure('output_invalid')
                outputs[role]=('audio/mpeg',data)
        elif roles is None or 'cover' in roles:
            data=self.run('ffmpeg',['-nostdin','-v','error','-xerror','-err_detect','explode','-threads','1','-protocol_whitelist','pipe','-i','pipe:0',
                '-map','0:v:0','-map_metadata','-1','-frames:v','1','-threads','1','-c:v','png','-pix_fmt','rgb24','-fflags','+bitexact','-flags:v','+bitexact','-f','image2pipe','pipe:1'],fd,deadline=deadline,maximum=self.limits.max_input_bytes)
            with bytes_fd(data) as output_fd:self.validate(output_fd,'image/png',deadline=deadline)
            outputs['cover']=('image/png',data)
        return outputs

from contextlib import contextmanager
@contextmanager
def bytes_fd(data):
    # Anonymous parent-only object; only its bytes enter the tool namespace.
    fd=os.memfd_create('shadify-output',os.MFD_CLOEXEC)
    try:
        view=memoryview(data)
        while view:view=view[os.write(fd,view):]
        os.lseek(fd,0,os.SEEK_SET);yield fd
    finally:os.close(fd)

def container_check(fd,mime,maximum):
    os.lseek(fd,0,os.SEEK_SET);data=os.read(fd,maximum+1)
    try:
        if len(data)>maximum:raise ValueError()
        if mime=='audio/wav':
            if len(data)<12 or data[:4]!=b'RIFF' or data[8:12]!=b'WAVE' or int.from_bytes(data[4:8],'little')+8!=len(data):raise ValueError()
        elif mime=='audio/flac':
            if data[:4]!=b'fLaC':raise ValueError()
        elif mime=='image/jpeg':
            if data[:3]!=b'\xff\xd8\xff' or data[-2:]!=b'\xff\xd9':raise ValueError()
        elif mime=='image/png':
            if data[:8]!=b'\x89PNG\r\n\x1a\n':raise ValueError()
            offset=8;ended=False
            while offset<len(data):
                length=int.from_bytes(data[offset:offset+4],'big');kind=data[offset+4:offset+8];end=offset+12+length
                if length>maximum or end>len(data) or len(kind)!=4 or kind==b'acTL':raise ValueError()
                if zlib.crc32(data[offset+4:end-4])&0xffffffff!=int.from_bytes(data[end-4:end],'big'):raise ValueError()
                offset=end
                if kind==b'IEND':
                    if length or offset!=len(data):raise ValueError()
                    ended=True
            if not ended:raise ValueError()
        else:raise ValueError()
    except (ValueError,OverflowError):raise ToolFailure('validation_invalid') from None

def inspect_mp3(data,bitrate):
    offset=0;frames=0
    while offset<len(data):
        if offset+4>len(data):raise ToolFailure('output_invalid')
        header=int.from_bytes(data[offset:offset+4],'big')
        rates=[0,32,40,48,56,64,80,96,112,128,160,192,224,256,320,0]
        rate=rates[(header>>12)&15];sample=(header>>10)&3
        if header>>21!=2047 or (header>>19)&3!=3 or (header>>17)&3!=1 or sample!=0 or rate!=bitrate or (header>>6)&3==3:
            raise ToolFailure('output_invalid')
        size=144000*rate//44100+((header>>9)&1)
        if offset+size>len(data):raise ToolFailure('output_invalid')
        offset+=size;frames+=1
    if not frames:raise ToolFailure('output_invalid')

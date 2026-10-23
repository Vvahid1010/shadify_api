"""Fixed sandboxed probe/full decode/precomputed encodes, never live playback."""
import hashlib,json,math,os,re,stat,struct,time,zlib
from pathlib import Path
from .clamav_scanner import run_bounded,EvidenceInvalid
from .scanner_sandbox import NativeSandbox
from .musicbrainz import text

PIPELINE_VERSION='mp3-cover-v1'
PROFILE_PIPELINE_VERSION='profile-image-v2'
class ToolFailure(Exception):
    def __init__(self,code):self.code=code

class MediaTools:
    def __init__(self,*,bwrap,runtime_root,prlimit,temp_dir,version,limits,lock_fd=None,runtime_sha256=None):
        self.sandbox=NativeSandbox(bwrap=bwrap,runtime_root=runtime_root);self.prlimit=str(prlimit)
        self.temp_dir=str(temp_dir);self.version=version;self.limits=limits;self.lock_fd=lock_fd;self.identity=version+':'+str(runtime_sha256)
    def run(self,tool,options,fd,*,deadline,maximum=65536,output_file=False,seekable=False):
        if tool not in ('ffmpeg','ffprobe'):raise ValueError('Fixed media tools only')
        import tempfile
        try:
            with tempfile.TemporaryDirectory(prefix='shadify-media-',dir=self.temp_dir) as workspace:
                argv=self.sandbox.argv(definitions=None,workspace=workspace,options=options,lock_fd=self.lock_fd,scratch_bytes=self.limits.max_scan_bytes)
                argv[-len(options)-1]='/usr/bin/'+tool
                inherited=[] if self.lock_fd is None else [self.lock_fd]
                if seekable:
                    if fd is None or not stat.S_ISREG(os.fstat(fd).st_mode) or not 0<os.fstat(fd).st_size<=self.limits.max_input_bytes:raise ToolFailure('validation_invalid')
                    separator=len(argv)-len(options)-2
                    argv[separator:separator]=['--ro-bind-data',str(fd),'/scratch/input']
                    inherited.append(fd)
                if output_file:
                    if tool!='ffmpeg' or options[-1]!='/scratch/profile.avif':raise ValueError('Fixed AVIF output only')
                    scratch=next(i for i in range(len(argv)-1) if argv[i]=='--tmpfs' and argv[i+1]=='/scratch');argv[scratch-2:scratch+2]=['--bind',workspace,'/scratch']
                command=[self.prlimit,'--cpu='+str(self.limits.cpu_seconds),'--as='+str(self.limits.memory_bytes),
                    '--fsize='+str(self.limits.max_scan_bytes),'--nofile=128','--',*argv]
                if fd is not None:os.lseek(fd,0,os.SEEK_SET)
                code,out,err=run_bounded(command,deadline=deadline,output_bytes=maximum,stdin=None if seekable else fd,
                    stdin_bytes=os.fstat(fd).st_size if fd is not None else None,pass_fds=tuple(inherited),
                    allow_early_stdin_close=tool=='ffprobe' and fd is not None)
                if code or err:raise ToolFailure('validation_invalid')
                if output_file:
                    target=Path(workspace)/'profile.avif'
                    handle=os.open(target,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
                    try:
                        info=os.fstat(handle);size=info.st_size
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or not 0<size<=maximum:raise ToolFailure('output_invalid')
                        out=os.read(handle,maximum+1)
                        if len(out)!=size:raise ToolFailure('output_invalid')
                    finally:os.close(handle)
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
            data=json.loads(self.run('ffprobe',['-v','error','-protocol_whitelist','pipe','-format_whitelist','wav,flac,mp3,png_pipe,jpeg_pipe,image2pipe,mov',
                '-count_packets','-show_entries','stream=codec_name,codec_type,sample_rate,channels,bits_per_sample,bits_per_raw_sample,width,height,pix_fmt:stream_disposition=attached_pic:format=format_name:format_tags=title,artist,album,track,ISRC',
                '-of','json','-i','pipe:0'],fd,deadline=deadline))
            if type(data) is not dict or type(data.get('streams')) is not list or not 1<=len(data['streams'])<=2:raise ValueError()
            streams=data['streams']
            if len(streams)==2:
                audio=[s for s in streams if s.get('codec_type')=='audio' and s.get('codec_name')=='mp3']
                pictures=[s for s in streams if s.get('codec_type')=='video' and s.get('codec_name') in ('mjpeg','png') and s.get('disposition',{}).get('attached_pic')==1]
                if data.get('format',{}).get('format_name')!='mp3' or len(audio)!=1 or len(pictures)!=1:raise ValueError()
                return data,audio[0]
            return data,streams[0]
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
        if mime=='video/mp4':return self.validate_video(fd,deadline=deadline)
        report,stream=self.probe(fd,deadline=deadline)
        formats={'audio/wav':'wav','audio/flac':'flac','audio/mpeg':'mp3','image/png':'png_pipe','image/jpeg':'jpeg_pipe','image/avif':'mov,mp4,m4a,3gp,3g2,mj2'}
        expected=formats[mime]
        if report.get('format',{}).get('format_name') not in (expected,'image2pipe' if mime.startswith('image/') else expected):raise ToolFailure('validation_invalid')
        if mime.startswith('audio/'):
            if stream.get('codec_type')!='audio' or not (stream.get('codec_name') in ('pcm_u8','pcm_s16le','pcm_s24le','pcm_s32le') if mime=='audio/wav' else stream.get('codec_name')==('mp3' if mime=='audio/mpeg' else 'flac')):raise ToolFailure('validation_invalid')
        elif stream.get('codec_type')!='video' or stream.get('codec_name')!=('png' if mime=='image/png' else 'av1' if mime=='image/avif' else 'mjpeg'):raise ToolFailure('validation_invalid')
        decoded=self.decoded(fd,stream,deadline=deadline)
        if mime=='audio/mpeg':
            for picture in report['streams']:
                if picture.get('codec_type')=='video':self.decoded(fd,picture,deadline=deadline)
        metadata={};tags=report.get('format',{}).get('tags',{})
        if type(tags) is not dict:raise ToolFailure('metadata_invalid')
        try:
            mapping={'title':'title','artist':'artist','album':'album','track':'track_number','isrc':'isrc'}
            for key,value in tags.items():
                if key.lower() in mapping:metadata[mapping[key.lower()]]=text(value,200 if key.lower() in ('title','album') else 120)
        except ValueError:raise ToolFailure('metadata_invalid') from None
        metadata.update({key:value for key,value in decoded.items() if key=='duration_ms'})
        quality=dict(codec=stream['codec_name'],**decoded)
        if mime.startswith('image/'):
            pixel_format=str(stream.get('pix_fmt',''))
            quality['has_alpha']=pixel_format=='pal8' or pixel_format.startswith(('rgba','bgra','argb','abgr','yuva','gbrap','ya'))
        if mime.startswith('audio/'):
            quality['lossless']=mime!='audio/mpeg'
            if mime=='audio/mpeg':
                os.lseek(fd,0,os.SEEK_SET);quality.update(mp3_source(os.read(fd,self.limits.max_input_bytes+1)))
                if quality['bitrate_kbps']<32:raise ToolFailure('validation_invalid')
            else:quality['bits_per_sample']=int(stream.get('bits_per_raw_sample') or stream.get('bits_per_sample') or 0)
        return dict(passed=True,tool_version=self.version,actual_mime=mime,source_quality=quality),dict(complete=True,metadata=metadata)
    def process(self,fd,mime,validation,*,deadline,roles=None,profile_purpose=None):
        self.preflight(deadline=deadline)
        outputs={}
        if mime=='video/mp4':
            if roles is None or 'video' in roles:outputs['video']=('video/mp4',self.process_video(fd,validation,deadline=deadline))
            return outputs
        if mime.startswith('audio/'):
            high=320
            if mime=='audio/mpeg':
                source_rate=validation['source_quality']['bitrate_kbps']
                high=max(rate for rate in (32,40,48,56,64,80,96,112,128,160,192,224,256,320) if rate<=source_rate)
            for role,bitrate in (('high',high),('low',128)):
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
        elif profile_purpose:
            return self.profile_outputs(fd,mime,validation,profile_purpose,deadline=deadline,roles=roles)
        elif roles is None or 'cover' in roles:
            data=self.run('ffmpeg',['-nostdin','-v','error','-xerror','-err_detect','explode','-threads','1','-protocol_whitelist','pipe','-i','pipe:0',
                '-map','0:v:0','-map_metadata','-1','-frames:v','1','-threads','1','-c:v','png','-pix_fmt','rgb24','-fflags','+bitexact','-flags:v','+bitexact','-f','image2pipe','pipe:1'],fd,deadline=deadline,maximum=self.limits.max_input_bytes)
            with bytes_fd(data) as output_fd:self.validate(output_fd,'image/png',deadline=deadline)
            outputs['cover']=('image/png',data)
        return outputs

    def profile_outputs(self,fd,mime,validation,purpose,*,deadline,roles=None):
        if purpose not in ('avatar','header') or mime not in ('image/png','image/jpeg'):raise ToolFailure('output_invalid')
        quality=validation['source_quality'];source_width=int(quality['width']);source_height=int(quality['height'])
        outputs={};sizes=(512,192) if purpose=='avatar' else (1920,640)
        for role,width,avif in (('cover',sizes[0],False),('cover_small',sizes[1],False),('cover_avif',sizes[0],True),('cover_avif_small',sizes[1],True)):
            if roles is not None and role not in roles:continue
            ratio=min(1,width/source_width,width/source_height)
            # Bound both axes and preserve aspect ratio; never upscale source pixels.
            width=max(1,round(source_width*ratio));height=max(1,round(source_height*ratio))
            options=['-nostdin','-v','error','-xerror','-err_detect','explode','-threads','1','-protocol_whitelist','pipe,file','-i','pipe:0','-map','0:v:0','-map_metadata','-1','-frames:v','1','-vf',f'scale={width}:{height}','-threads','1']
            if avif:options+=['-c:v','libaom-av1','-cpu-used','6','-crf','32','-still-picture','1','-pix_fmt','yuv444p','-f','avif','/scratch/profile.avif']
            else:options+=['-c:v','png','-pix_fmt','rgba','-f','image2pipe','pipe:1']
            data=self.run('ffmpeg',options,fd,deadline=deadline,maximum=10485760,output_file=avif)
            output_mime='image/avif' if avif else 'image/png'
            with bytes_fd(data) as output_fd:
                result,_=self.validate(output_fd,output_mime,deadline=deadline)
            actual=result['source_quality']
            if actual['width']!=width or actual['height']!=height or width>source_width or height>source_height:raise ToolFailure('output_invalid')
            outputs[role]=(output_mime,data)
        return outputs

    def validate_video(self,fd,*,deadline):
        try:
            report=json.loads(self.run('ffprobe',['-v','error','-protocol_whitelist','file,pipe','-format_whitelist','mov',
                '-enable_drefs','0','-use_absolute_path','0','-count_packets',
                '-show_entries','stream=codec_name,codec_type,profile,pix_fmt,width,height,avg_frame_rate,sample_rate,channels,duration,start_time:stream_disposition=attached_pic:format=format_name,duration:format_tags=title,artist,album,track,ISRC',
                '-of','json','-i','/scratch/input'],fd,deadline=deadline,seekable=True))
            streams=report['streams']
            if type(streams) is not list or len(streams)!=2 or report['format']['format_name']!='mov,mp4,m4a,3gp,3g2,mj2':raise ValueError()
            video=next(s for s in streams if s.get('codec_type')=='video');audio=next(s for s in streams if s.get('codec_type')=='audio')
            if video.get('codec_name')!='h264' or video.get('profile') not in ('Baseline','Constrained Baseline','Main','High') or video.get('pix_fmt')!='yuv420p' or video.get('disposition',{}).get('attached_pic',0):raise ValueError()
            if audio.get('codec_name')!='aac' or audio.get('profile')!='LC':raise ValueError()
            width=int(video['width']);height=int(video['height']);channels=int(audio['channels']);rate=int(audio['sample_rate'])
            from fractions import Fraction
            fps=Fraction(video['avg_frame_rate']);duration=float(report['format']['duration'])
            if not (2<=width<=1920 and 2<=height<=1920 and width*height<=1920*1080 and width%2==0 and height%2==0 and 0<fps<=30 and 1<=channels<=2 and 8000<=rate<=96000 and math.isfinite(duration) and 0<duration<=3600):raise ValueError()
            for stream in streams:
                start=float(stream.get('start_time','0'));length=float(stream['duration'])
                if not math.isfinite(start) or abs(start)>.1 or not math.isfinite(length) or abs(length-duration)>.25:raise ValueError()
            # Fully decode both streams, producing bounded progress only. Null
            # output avoids collecting uncompressed video frames in memory.
            progress=self.run('ffmpeg',['-nostdin','-v','error','-xerror','-err_detect','explode','-threads','1',
                '-protocol_whitelist','file,pipe','-format_whitelist','mov','-enable_drefs','0','-use_absolute_path','0','-i','/scratch/input',
                '-map','0:v:0','-map','0:a:0','-sn','-dn','-threads','1','-progress','pipe:1','-stats_period','10','-f','null','pipe:1'],fd,deadline=deadline,seekable=True)
            fields=dict(line.split('=',1) for line in progress.decode('ascii').splitlines() if '=' in line)
            if fields.get('progress')!='end' or int(fields['frame'])<1 or abs(int(fields['out_time_us'])/1000000-duration)>.25:raise ValueError()
            quality=dict(codec='h264',audio_codec='aac',lossless=False,width=width,height=height,frames=int(fields['frame']),frame_rate=str(fps),channels=channels,sample_rate=rate,duration_ms=round(duration*1000))
            tags=report['format'].get('tags',{});metadata={}
            if type(tags) is not dict:raise ValueError()
            for key,value in tags.items():
                name={'title':'title','artist':'artist','album':'album','track':'track_number','isrc':'isrc'}.get(key.lower())
                if name:metadata[name]=text(value,200 if name in ('title','album') else 120)
            metadata['duration_ms']=quality['duration_ms']
            return dict(passed=True,tool_version=self.version,actual_mime='video/mp4',source_quality=quality),dict(complete=True,metadata=metadata)
        except (ValueError,KeyError,TypeError,OverflowError,StopIteration,ZeroDivisionError,UnicodeError):raise ToolFailure('validation_invalid') from None
    def process_video(self,fd,validation,*,deadline):
        data=self.run('ffmpeg',['-nostdin','-v','error','-xerror','-err_detect','explode','-threads','1',
            '-protocol_whitelist','file,pipe','-format_whitelist','mov','-enable_drefs','0','-use_absolute_path','0','-i','/scratch/input',
            '-map','0:v:0','-map','0:a:0','-map_metadata','-1','-map_chapters','-1','-sn','-dn',
            '-c:v','libx264','-preset','veryfast','-crf','20','-profile:v','high','-level:v','4.1','-bf','0','-pix_fmt','yuv420p','-threads:v','1',
            '-c:a','aac','-b:a','128k','-ac','2','-ar','48000','-threads:a','1',
            '-fflags','+bitexact','-flags:v','+bitexact','-flags:a','+bitexact',
            '-movflags','+frag_keyframe+empty_moov+default_base_moof','-f','mp4','pipe:1'],fd,deadline=deadline,maximum=self.limits.max_input_bytes,seekable=True)
        with bytes_fd(data) as output:
            checked,_=self.validate(output,'video/mp4',deadline=deadline)
        actual=checked['source_quality'];source=validation['source_quality']
        if (actual['width'],actual['height'])!=(source['width'],source['height']) or abs(actual['duration_ms']-source['duration_ms'])>250:raise ToolFailure('output_invalid')
        return data

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
        elif mime=='video/mp4':
            mp4_container(data)
        elif mime=='audio/mpeg':
            mp3_source(data)
        elif mime=='image/jpeg':
            if data[:3]!=b'\xff\xd8\xff' or data[-2:]!=b'\xff\xd9':raise ValueError()
        elif mime=='image/avif':
            # Fixed producer output only, followed by complete native decode.
            if len(data)<24 or data[4:8]!=b'ftyp' or b'avif' not in data[8:32]:raise ValueError()
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


def mp3_source(data):
    """Bounded MPEG Layer III frame walk; tags are never mistaken for audio."""
    try:
        offset=0;end=len(data);frames=0;audio_bytes=0;seconds=0
        if data[:3]==b'ID3':
            if len(data)<10 or data[3] not in (2,3,4) or data[4]==255 or any(v&128 for v in data[6:10]):raise ValueError()
            flags=data[5]
            if flags & {2:0x3f,3:0x1f,4:0x0f}[data[3]]:raise ValueError()
            size=0
            for value in data[6:10]:size=(size<<7)|value
            offset=10+size
            if data[3]==4 and flags&16:
                if data[offset:offset+3]!=b'3DI' or data[offset+3:offset+10]!=data[3:10]:raise ValueError()
                offset+=10
            if offset>=end:raise ValueError()
        if end-offset>=128 and data[end-128:end-125]==b'TAG':end-=128
        while offset<end:
            if offset+4>end:raise ValueError()
            header=int.from_bytes(data[offset:offset+4],'big')
            version=(header>>19)&3;layer=(header>>17)&3;index=(header>>12)&15;sample=(header>>10)&3
            if header>>21!=2047 or version==1 or layer!=1 or index in (0,15) or sample==3:raise ValueError()
            rates=(0,32,40,48,56,64,80,96,112,128,160,192,224,256,320,0) if version==3 else (0,8,16,24,32,40,48,56,64,80,96,112,128,144,160,0)
            bitrate=rates[index];rate=(44100,48000,32000)[sample]//({3:1,2:2,0:4}[version])
            size=(144000 if version==3 else 72000)*bitrate//rate+((header>>9)&1)
            if offset+size>end:raise ValueError()
            offset+=size;audio_bytes+=size;seconds+=(1152 if version==3 else 576)/rate;frames+=1
        if frames<2:raise ValueError()
        return dict(bitrate_kbps=round(audio_bytes*8/seconds/1000),mpeg_frames=frames)
    except (ValueError,KeyError,OverflowError,ZeroDivisionError):raise ToolFailure('validation_invalid') from None


def mp4_container(data):
    """Bounded ISO BMFF framing, not an extension/MIME approval."""
    offset=0;counts={}
    try:
        while offset<len(data):
            if offset+8>len(data):raise ValueError()
            size=int.from_bytes(data[offset:offset+4],'big');kind=data[offset+4:offset+8];header=8
            if size==1:
                if offset+16>len(data):raise ValueError()
                size=int.from_bytes(data[offset+8:offset+16],'big');header=16
            elif size==0:size=len(data)-offset
            if size<header or offset+size>len(data) or kind not in (b'ftyp',b'moov',b'mdat',b'free',b'skip',b'wide',b'moof',b'mfra',b'sidx'):raise ValueError()
            counts[kind]=counts.get(kind,0)+1
            if offset==0:
                if kind!=b'ftyp' or header!=8 or size<16 or (size-16)%4 or data[8:12] not in (b'isom',b'iso2',b'iso3',b'iso4',b'iso5',b'iso6',b'iso7',b'iso8',b'iso9',b'mp41',b'mp42',b'avc1'):raise ValueError()
            offset+=size
        if counts.get(b'ftyp')!=1 or counts.get(b'moov')!=1 or not counts.get(b'mdat'):raise ValueError()
    except (ValueError,OverflowError):raise ToolFailure('validation_invalid') from None

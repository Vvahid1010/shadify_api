import os,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from shadify_api.media_tools import MediaTools

class ToolBoundsTests(unittest.TestCase):
 def test_fixed_output_slot_preserves_bounded_scratch(self):
  with tempfile.TemporaryDirectory() as folder:
   tools=object.__new__(MediaTools);tools.temp_dir=folder;tools.lock_fd=None;tools.prlimit='/usr/bin/prlimit'
   tools.limits=SimpleNamespace(max_scan_bytes=4096,max_input_bytes=4096,cpu_seconds=1,memory_bytes=1000000)
   tools.sandbox=SimpleNamespace(argv=lambda **k:['bwrap','--size','4096','--tmpfs','/scratch','--','/usr/bin/ffmpeg',*k['options']])
   def execute(command,**kwargs):
    self.assertIn('--tmpfs',command);i=command.index('--bind');slot=command[i+1]
    self.assertEqual(command[i+2],'/scratch/profile.avif');self.assertTrue(Path(slot).is_file())
    self.assertNotEqual(slot,folder);self.assertIn('--fsize=128',command)
    Path(slot).write_bytes(b'synthetic-output');return 0,b'',b''
   with patch('shadify_api.media_tools.run_bounded',execute):
    self.assertEqual(tools.run('ffmpeg',['-y','/scratch/profile.avif'],None,deadline=time.monotonic()+5,maximum=128,output_file=True),b'synthetic-output')
 def test_seekable_probe_does_not_request_pipe_early_close(self):
  with tempfile.TemporaryDirectory() as folder,tempfile.TemporaryFile() as source:
   source.write(b'synthetic');source.flush()
   tools=object.__new__(MediaTools);tools.temp_dir=folder;tools.lock_fd=None;tools.prlimit='/usr/bin/prlimit'
   tools.limits=SimpleNamespace(max_scan_bytes=4096,max_input_bytes=4096,cpu_seconds=1,memory_bytes=1000000)
   tools.sandbox=SimpleNamespace(argv=lambda **k:['bwrap','--size','4096','--tmpfs','/scratch','--','/usr/bin/ffprobe',*k['options']])
   def execute(command,**kwargs):
    self.assertIsNone(kwargs['stdin']);self.assertFalse(kwargs['allow_early_stdin_close']);return 0,b'{}',b''
   with patch('shadify_api.media_tools.run_bounded',execute):
    self.assertEqual(tools.run('ffprobe',['-i','/scratch/input'],source.fileno(),deadline=time.monotonic()+5,seekable=True),b'{}')

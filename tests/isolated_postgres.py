"""Disposable native PostgreSQL. No external DSN, service, TCP port or cloud."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import psycopg


class IsolatedPostgres:
    def __enter__(self):
        executable = shutil.which("initdb")
        self.bin = Path(executable).parent if executable else Path('/usr/lib/postgresql/18/bin')
        if not (self.bin / 'initdb').is_file():
            raise RuntimeError('Native PostgreSQL test binaries required; no external database fallback')
        self.temp = tempfile.TemporaryDirectory(prefix='shadify-isolated-pg-')
        self.root = Path(self.temp.name)
        self.socket = self.root / 'socket'
        self.socket.mkdir(mode=0o700)
        self.data = self.root / 'data'
        self.started = False
        try:
            subprocess.run([str(self.bin/'initdb'), '-D', str(self.data), '-A', 'trust',
                            '-U', 'shadify_test', '--no-locale', '--encoding=UTF8'],
                           check=True, capture_output=True, text=True)
            subprocess.run([str(self.bin/'pg_ctl'), '-D', str(self.data), '-l', str(self.root/'server.log'),
                            '-o', "-F -h '' -k " + str(self.socket), '-w', 'start'],
                           check=True, capture_output=True, text=True)
            self.started = True
            self.connection = dict(host=str(self.socket), user='shadify_test', dbname='postgres')
            with psycopg.connect(**self.connection) as c:
                if c.execute('SHOW listen_addresses').fetchone()[0] != '':
                    raise RuntimeError('Test server must have no TCP listener')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        if self.started:
            subprocess.run([str(self.bin/'pg_ctl'), '-D', str(self.data), '-m', 'immediate', '-w', 'stop'],
                           check=True, capture_output=True, text=True)
            self.started = False
        self.temp.cleanup()

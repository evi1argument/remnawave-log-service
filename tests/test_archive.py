"""Linux host tests: real files, real fuser/flock, no cloud credentials."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.geteuid() == 0, 'fuser requires host root visibility')
class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        shutil.copytree(REPO / 'collector/archive', self.base / 'archive')
        self.spool = self.base / 'spool'
        self.spool.mkdir()
        self.store = self.base / 'store'
        self.config = self.base / 'archive/archive.conf'
        self.configure()
        self.env = dict(os.environ, LOGCOLLECTOR_BASE=str(self.base))

    def configure(self, encrypt='none', extra=''):
        self.config.write_text(
            f'BACKEND=local\nENCRYPT={encrypt}\nLOCAL_DIR={self.store}\n'
            f'LOCAL_RETENTION_DAYS=0\nSETTLED_MIN=65\n{extra}')

    def segment(self, content=b'first\n', name='node/2026/11/10/00.log', old=True):
        path = self.spool / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if old:
            os.utime(path, (time.time() - 7200,) * 2)
        return path

    def run_upload(self, success=True):
        result = subprocess.run(['bash', str(self.base / 'archive/archive-upload.sh')],
                                env=self.env, capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def archived(self):
        return list(self.store.rglob('*.log*'))

    def pending(self):
        return list((self.spool / '.archive-pending').rglob('*.log'))

    def test_success_and_recent_file_left_alone(self):
        old = self.segment()
        recent = self.segment(name='node/2026/11/10/01.log', old=False)
        self.run_upload()
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertEqual([p.read_bytes() for p in self.archived()], [b'first\n'])
        self.assertIn(hashlib.sha256(b'first\n').hexdigest(), self.archived()[0].name)

    def test_open_inode_deferred_then_complete(self):
        path = self.segment()
        with path.open('ab', buffering=0) as writer:
            self.assertIn('1 open segments deferred', self.run_upload().stdout)
            self.assertFalse(path.exists())
            self.assertEqual(len(self.pending()), 1)
            self.assertEqual(self.archived(), [])
            writer.write(b'late on old inode\n')
            # Vector reopens original pathname: a distinct segment, never removed
            # while uploading the claimed old inode.
            path.write_bytes(b'new path\n')
        self.run_upload()
        self.assertEqual(self.archived()[0].read_bytes(), b'first\nlate on old inode\n')
        self.assertEqual(path.read_bytes(), b'new path\n')
        self.assertEqual(self.pending(), [])

    def test_late_segment_same_hour_cannot_overwrite_even_identical_bytes(self):
        self.segment()
        self.run_upload()
        self.segment()
        self.run_upload()
        self.assertEqual(len(self.archived()), 2)

    def test_upload_failure_retains_claim_for_retry(self):
        backend = self.base / 'archive/backends/local.sh'
        original = backend.read_text()
        backend.write_text('backend_prepare(){ :; }\nbackend_put(){ return 1; }\n'
                           'backend_retention(){ :; }\n')
        self.segment()
        self.run_upload(False)
        self.assertEqual(self.pending()[0].read_bytes(), b'first\n')
        backend.write_text(original)
        self.run_upload()
        self.assertEqual(len(self.archived()), 1)
        self.assertEqual(self.pending(), [])

    def test_invalid_encryption_never_archives_plaintext(self):
        path = self.segment()
        self.configure(encrypt='agge')
        self.run_upload(False)
        self.assertTrue(path.exists())
        self.assertEqual(self.archived(), [])

    def test_missing_spool_is_failure(self):
        self.spool.rmdir()
        self.run_upload(False)

    def test_lock_rejects_concurrent_run(self):
        lock = self.base / 'archive-upload.lock'
        process = subprocess.Popen(['flock', str(lock), 'sh', '-c',
                                    'echo ready; read stop'], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            self.assertIn('already running', self.run_upload(False).stderr)
        finally:
            process.communicate('stop\n', timeout=5)

    @unittest.skipUnless(shutil.which('age-keygen'), 'age not installed')
    def test_real_age_roundtrip_and_private_temporary_files(self):
        key = self.base / 'private.key'
        subprocess.run(['age-keygen', '-o', str(key)], check=True, capture_output=True)
        pub = subprocess.check_output(['age-keygen', '-y', str(key)], text=True).strip()
        (self.base / 'public.key').write_text(pub)
        self.configure(encrypt='age', extra=f'AGE_PUBKEY_FILE={self.base}/public.key\n')
        self.segment()
        self.run_upload()
        artifact = self.archived()[0]
        self.assertTrue(artifact.name.endswith('.age'))
        plain = subprocess.check_output(['age', '-d', '-i', str(key), str(artifact)])
        self.assertEqual(plain, b'first\n')
        self.assertEqual(list(self.base.glob('.archive-work.*')), [])
        self.assertEqual(artifact.stat().st_mode & 0o077, 0)

    def test_encryption_failure_preserves_bytes(self):
        (self.base / 'public.key').write_text('invalid-key')
        self.configure(encrypt='age', extra=f'AGE_PUBKEY_FILE={self.base}/public.key\n')
        self.segment()
        self.run_upload(False)
        # No plaintext fallback; depending on installed age, failure is before
        # claim or during encryption. In either case the bytes must survive.
        remaining = list(self.spool.rglob('*.log'))
        self.assertEqual([p.read_bytes() for p in remaining], [b'first\n'])
        self.assertEqual(self.archived(), [])

    def test_retention_failure_surfaces(self):
        backend = self.base / 'archive/backends/local.sh'
        backend.write_text(backend.read_text() + '\nbackend_retention(){ return 1; }\n')
        self.assertIn('Retention FAILED', self.run_upload(False).stderr)

    def test_fuser_error_never_deletes_pending(self):
        tools = self.base / 'bin'
        tools.mkdir()
        fuser = tools / 'fuser'
        fuser.write_text('#!/bin/sh\necho "inspection denied" >&2\nexit 1\n')
        fuser.chmod(0o755)
        self.env['PATH'] = str(tools) + ':' + os.environ['PATH']
        self.segment()
        self.run_upload(False)
        self.assertEqual(self.pending()[0].read_bytes(), b'first\n')
        self.assertEqual(self.archived(), [])

    def test_tls_pair_preserved_and_partial_pair_rejected(self):
        tls = self.base / 'tls'
        command = ['bash', str(REPO / 'scripts/gen-tls.sh'), str(tls)]
        subprocess.run(command, check=True, capture_output=True)
        original = {p.name: p.read_bytes() for p in tls.iterdir()}
        subprocess.run(command, check=True, capture_output=True)
        self.assertEqual(original, {p.name: p.read_bytes() for p in tls.iterdir()})
        (tls / 'cert.pem').unlink()
        self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
        self.assertEqual((tls / 'key.pem').read_bytes(), original['key.pem'])


if __name__ == '__main__':
    unittest.main()

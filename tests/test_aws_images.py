import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import prepare_aws_images as images
import lab

RELEASE = 'a' * 40


class AwsImageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)

    def extract(self, archive, directory):
        entrypoint = directory / 'infra/15-validate-upstream.sh'
        entrypoint.parent.mkdir()
        entrypoint.write_text('test entrypoint')

    def test_builds_x86_images_without_rebuilding_application_or_publishing(self):
        with patch.object(images, 'extract_release', side_effect=self.extract), \
             patch.object(images, 'verify_release', return_value={'release': RELEASE}), \
             patch.object(images, 'sha256', return_value='manifest'), \
             patch.object(images, 'inspect', side_effect=['backend-id', 'frontend-id']), \
             patch.object(images.subprocess, 'run') as run:
            receipt = images.prepare('built-release.tar.gz', self.home)
        self.assertEqual(receipt['platform'], 'linux/amd64')
        self.assertEqual(receipt['images'], {'backend': 'backend-id', 'frontend': 'frontend-id'})
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            command = call.args[0]
            self.assertEqual(command[:4], ['docker', 'build', '--platform', 'linux/amd64'])
            self.assertIn(f'org.opencontainers.image.revision={RELEASE}', command)
            self.assertNotIn('--push', command)

    def test_reuses_images_and_refuses_changed_contents_for_same_release(self):
        receipt = {'release': RELEASE, 'manifest_sha256': 'same',
                   'images': {'backend': 'backend-id', 'frontend': 'frontend-id'}}
        lab.write_json(self.home / f'{RELEASE}.json', receipt)
        with patch.object(images, 'extract_release', side_effect=self.extract), \
             patch.object(images, 'verify_release', return_value={'release': RELEASE}), \
             patch.object(images, 'sha256', return_value='same') as digest, \
             patch.object(images, 'inspect', side_effect=['backend-id', 'frontend-id']), \
             patch.object(images.subprocess, 'run') as run:
            self.assertEqual(images.prepare('release.tar.gz', self.home), receipt)
            digest.return_value = 'different'
            with self.assertRaisesRegex(ValueError, 'different contents'):
                images.prepare('release.tar.gz', self.home)
            run.assert_not_called()

    def test_rejects_changed_image_ids_instead_of_rebuilding(self):
        lab.write_json(self.home / f'{RELEASE}.json', {'manifest_sha256': 'same', 'images': {}})
        with patch.object(images, 'extract_release', side_effect=self.extract), \
             patch.object(images, 'verify_release', return_value={'release': RELEASE}), \
             patch.object(images, 'sha256', return_value='same'), \
             patch.object(images, 'inspect', return_value='changed'), \
             patch.object(images.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'images changed'):
                images.prepare('release.tar.gz', self.home)
            run.assert_not_called()

    def test_rejects_arm_images_for_x86_ecs_tasks(self):
        with patch.object(images.subprocess, 'check_output', return_value=json.dumps([
                {'Id': 'image-id', 'Os': 'linux', 'Architecture': 'arm64'}])):
            with self.assertRaisesRegex(ValueError, 'not Linux/amd64'):
                images.inspect('example:tag')

    def test_failed_build_does_not_create_success_receipt(self):
        with patch.object(images, 'extract_release', side_effect=self.extract), \
             patch.object(images, 'verify_release', return_value={'release': RELEASE}), \
             patch.object(images, 'sha256', return_value='manifest'), \
             patch.object(images.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'docker')):
            with self.assertRaises(subprocess.CalledProcessError):
                images.prepare('release.tar.gz', self.home)
        self.assertFalse((self.home / f'{RELEASE}.json').exists())


class FrontendUpstreamTests(unittest.TestCase):
    def validate(self, value):
        return subprocess.run(['sh', str(ROOT / 'infra/15-validate-upstream.sh')],
                              env=dict(os.environ, BACKEND_UPSTREAM=value), capture_output=True).returncode

    def test_accepts_compose_dns_and_ecs_localhost(self):
        for value in ('backend:8080', '127.0.0.1:8080'):
            self.assertEqual(self.validate(value), 0)

    def test_rejects_urls_invalid_ports_and_nginx_directive_injection(self):
        for value in ('', 'http://backend:8080', 'backend:0', 'backend:65536',
                      'backend:8080;', 'backend:8080\n127.0.0.1:80', 'backend:8080/path'):
            with self.subTest(value=value):
                self.assertNotEqual(self.validate(value), 0)


if __name__ == '__main__':
    unittest.main()

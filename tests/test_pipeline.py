import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import lab

RELEASE = 'a' * 40

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.installed = {'release': RELEASE, 'manifest_sha256': 'expected',
                          'images': {'backend': 'sha256:one', 'frontend': 'sha256:two'}}
        lab.write_json(self.home / 'installed' / f'{RELEASE}.json', self.installed)

    def receipt(self, environment):
        lab.write_json(self.home / 'history' / RELEASE / f'{environment}.json',
                       dict(self.installed, status='passed'))

    def test_dev_can_start_without_an_earlier_environment(self):
        self.assertEqual(lab.check_promotion(self.home, RELEASE, 'dev'), self.installed)

    def test_uat_requires_this_release_to_pass_dev(self):
        with self.assertRaisesRegex(ValueError, 'pass DEV first'):
            lab.check_promotion(self.home, RELEASE, 'uat')
        self.receipt('dev')
        lab.check_promotion(self.home, RELEASE, 'uat')

    def test_prod_requires_uat_pass_and_human_acceptance(self):
        self.receipt('dev')
        with self.assertRaisesRegex(ValueError, 'pass UAT first'):
            lab.check_promotion(self.home, RELEASE, 'prod', True)
        self.receipt('uat')
        with self.assertRaisesRegex(ValueError, 'human UAT'):
            lab.check_promotion(self.home, RELEASE, 'prod')
        lab.check_promotion(self.home, RELEASE, 'prod', True)

    def test_previous_result_must_match_installed_artifacts(self):
        self.receipt('dev')
        path = self.home / 'history' / RELEASE / 'dev.json'
        evidence = lab.read_json(path)
        evidence['images']['backend'] = 'sha256:different'
        lab.write_json(path, evidence)
        with self.assertRaisesRegex(ValueError, 'does not match'):
            lab.check_promotion(self.home, RELEASE, 'uat')

    def test_environment_credentials_are_separate_and_preserved(self):
        lab.initialize(self.home)
        paths = list((self.home / 'env').glob('*.env'))
        original = {p.name: p.read_text() for p in paths}
        self.assertEqual(len(set(original.values())), 3)
        passwords = [next(line for line in text.splitlines() if line.startswith('APP_DB_PASSWORD='))
                     for text in original.values()]
        self.assertEqual(len(set(passwords)), 3)
        for path in paths: self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        lab.initialize(self.home)
        self.assertEqual(original, {p.name: p.read_text() for p in paths})

    def test_rejects_release_id_injection(self):
        for value in ['../dev', 'main', 'abc;echo bad', 'a' * 41]:
            with self.assertRaises(ValueError): lab.valid_release(value)

    def test_manifest_detects_modified_application_artifacts(self):
        directory = self.home / 'release'
        files = ['backend.jar', 'frontend/index.html', 'frontend/release.json',
                 'infra/compose.yaml', 'infra/backend.Dockerfile', 'infra/frontend.Dockerfile',
                 'infra/nginx.conf', 'infra/init-db.sh']
        for name in files:
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'release': RELEASE}) if name.endswith('release.json') else 'original')
        lab.write_json(directory / 'manifest.json', {'release': RELEASE,
                       'checksums': {name: lab.sha256(directory / name) for name in files}})
        lab.verify_release(directory)
        (directory / 'backend.jar').write_text('modified')
        with self.assertRaisesRegex(ValueError, 'checksum failed'):
            lab.verify_release(directory)

    def test_rejects_archive_path_traversal_and_symlinks(self):
        for name, kind in [('../escape', tarfile.REGTYPE), ('link', tarfile.SYMTYPE)]:
            archive = self.home / 'bad.tar.gz'
            with tarfile.open(archive, 'w:gz') as bundle:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = '/tmp'
                member.size = 0
                bundle.addfile(member, io.BytesIO(b''))
            with self.assertRaisesRegex(ValueError, 'Unsafe'):
                lab.extract_release(archive, self.home / 'extracted')

    def test_failed_deployment_invalidates_its_old_pass(self):
        self.receipt('dev')
        with patch.object(lab, 'verify_release'), patch.object(lab, 'sha256', return_value='expected'), \
             patch.object(lab, 'docker_ready'), patch.object(lab, 'image_ids', return_value=self.installed['images']), \
             patch.object(lab, 'compose', side_effect=ValueError('migration failed')):
            with self.assertRaisesRegex(ValueError, 'migration failed'):
                lab.deploy(self.home, RELEASE, 'dev')
        self.assertFalse((self.home / 'history' / RELEASE / 'dev.json').exists())
        self.assertFalse((self.home / 'state' / 'dev.json').exists())
        self.assertEqual(lab.read_json(self.home / 'failures' / f'dev-{RELEASE}.json')['status'], 'failed')

    def test_failed_smoke_test_does_not_allow_promotion(self):
        with patch.object(lab, 'verify_release'), patch.object(lab, 'sha256', return_value='expected'), \
             patch.object(lab, 'docker_ready'), patch.object(lab, 'image_ids', return_value=self.installed['images']), \
             patch.object(lab, 'compose'), patch.object(lab, 'smoke', side_effect=ValueError('wrong release')):
            with self.assertRaisesRegex(ValueError, 'wrong release'):
                lab.deploy(self.home, RELEASE, 'dev')
        with self.assertRaisesRegex(ValueError, 'pass DEV first'):
            lab.check_promotion(self.home, RELEASE, 'uat')

    def test_actions_requires_persistent_storage_configuration(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'LAB_HOME': ''}):
            with self.assertRaisesRegex(ValueError, 'LAB_HOME'): lab.lab_home()

    def test_bootstrap_runs_after_database_readiness_and_before_migrations(self):
        with patch.object(lab, 'verify_release'), patch.object(lab, 'sha256', return_value='expected'), \
             patch.object(lab, 'docker_ready'), patch.object(lab, 'image_ids', return_value=self.installed['images']), \
             patch.object(lab, 'compose') as compose, patch.object(lab, 'smoke'):
            lab.deploy(self.home, RELEASE, 'dev')
        operations = [call.args[3:] for call in compose.call_args_list]
        self.assertEqual(operations[0][-1], 'db')
        self.assertEqual(operations[1], ('exec', '-T', 'db', 'bash', '-lc', 'bash /delivery-lab/init-db.sh'))
        self.assertEqual(operations[2], ('run', '--rm', '--no-deps', 'migrate'))
        self.assertEqual(operations[3][-2:], ('backend', 'frontend'))

    def test_bootstrap_failure_blocks_migrations_and_promotion(self):
        with patch.object(lab, 'verify_release'), patch.object(lab, 'sha256', return_value='expected'), \
             patch.object(lab, 'docker_ready'), patch.object(lab, 'image_ids', return_value=self.installed['images']), \
             patch.object(lab, 'compose', side_effect=[None, ValueError('bootstrap failed')]) as compose, \
             patch.object(lab, 'smoke') as smoke:
            with self.assertRaisesRegex(ValueError, 'bootstrap failed'):
                lab.deploy(self.home, RELEASE, 'dev')
            self.assertEqual(compose.call_count, 2)
            smoke.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'pass DEV first'):
            lab.check_promotion(self.home, RELEASE, 'uat')

if __name__ == '__main__': unittest.main()

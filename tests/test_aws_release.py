"""Exercise publication failures, immutable reuse and archive provenance without AWS."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import lab
import publish_aws_release as release

SHA = 'a' * 40
ACCOUNT = '123456789012'
BACK = 'sha256:' + 'b' * 64
FRONT = 'sha256:' + 'f' * 64


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.stage = self.home / 'staging'
        files = ['backend.jar', 'frontend/index.html', 'frontend/release.json',
                 'infra/compose.yaml', 'infra/backend.Dockerfile', 'infra/frontend.Dockerfile',
                 'infra/nginx.conf', 'infra/init-db.sh', 'infra/15-validate-upstream.sh']
        for name in files:
            path = self.stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'release': SHA}) if name.endswith('release.json') else 'tested output')
        lab.write_json(self.stage / 'manifest.json', {'release': SHA,
                       'checksums': {name: lab.sha256(self.stage / name) for name in files}})
        self.archive = self.home / 'release.tar.gz'
        self.pack()
        self.output = self.home / 'aws-release.json'
        self.registry = release.Registry(ACCOUNT, 'eu-west-1', 'delivery-lab')
        self.registry.repositories = Mock(return_value={
            s: f'{self.registry.host}/delivery-lab/{s}' for s in release.SERVICES})
        self.registry.digest = Mock(side_effect=[None, None, BACK, FRONT])
        self.build = self.mock(patch.object(release, 'build_image', return_value='image-id'))
        self.verify = self.mock(patch.object(release, 'verify_image', return_value='image-id'))
        self.run = self.mock(patch.object(release.subprocess, 'run'))

    def mock(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def pack(self):
        with tarfile.open(self.archive, 'w:gz') as bundle:
            for path in sorted(self.stage.rglob('*')):
                if path.is_file():
                    bundle.add(path, arcname=str(path.relative_to(self.stage)))

    def publish(self, sha=SHA):
        return release.publish(self.archive, self.output, self.registry, sha,
                               'owner/repo', '1234', ('docker',))

    def test_stores_repository_digests_and_links_them_to_verified_archive(self):
        result = self.publish()
        self.assertEqual(result['images']['backend']['digest'], BACK)
        self.assertEqual(result['images']['frontend']['digest'], FRONT)
        self.assertEqual(result['artifact']['archive_sha256'], lab.sha256(self.archive))
        self.assertEqual(result['artifact']['manifest_sha256'], lab.sha256(self.stage / 'manifest.json'))
        self.assertEqual(result['source']['ci_run_id'], '1234')
        self.assertEqual(result['release'], SHA)
        self.assertEqual(self.build.call_count, 2)
        for call in self.run.call_args_list:
            self.assertEqual(call.args[0][:2], ['docker', 'push'])
        self.assertEqual(lab.read_json(self.output), result)

    def test_retry_reuses_both_stored_images_without_any_build_or_push(self):
        self.registry.digest.side_effect = [BACK, FRONT]
        self.publish()
        self.build.assert_not_called()
        self.run.assert_not_called()
        self.assertEqual(self.verify.call_count, 2)

    def test_partial_publication_builds_only_the_missing_image(self):
        self.registry.digest.side_effect = [BACK, None, FRONT]
        self.publish()
        self.assertEqual(self.build.call_count, 1)
        self.assertEqual(self.build.call_args.args[1], 'frontend')
        self.assertEqual(self.run.call_count, 1)

    def test_existing_image_mismatch_aborts_before_pushing_missing_image(self):
        self.registry.digest.side_effect = [None, FRONT]
        self.verify.side_effect = ValueError('Stored image differs')
        self.output.write_text('stale success')
        with self.assertRaisesRegex(ValueError, 'Stored image differs'):
            self.publish()
        self.build.assert_not_called()
        self.run.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_push_failure_cannot_produce_success_metadata(self):
        self.run.side_effect = subprocess.CalledProcessError(1, 'docker push')
        with self.assertRaises(subprocess.CalledProcessError):
            self.publish()
        self.assertFalse(self.output.exists())

    def test_uploaded_image_must_match_local_image_id(self):
        self.verify.return_value = 'another-image'
        with self.assertRaisesRegex(ValueError, 'differs from the image just prepared'):
            self.publish()
        self.assertFalse(self.output.exists())

    def test_tampered_archive_fails_before_registry_access(self):
        (self.stage / 'backend.jar').write_text('modified')
        self.pack()
        with self.assertRaisesRegex(ValueError, 'checksum failed'):
            self.publish()
        self.registry.repositories.assert_not_called()
        self.build.assert_not_called()

    def test_wrong_commit_and_local_test_ids_are_rejected(self):
        for sha in ('c' * 40, 'local-123456789abc'):
            with self.subTest(sha=sha), self.assertRaises(ValueError):
                self.publish(sha)
        self.registry.repositories.assert_not_called()
        self.build.assert_not_called()


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = release.Registry(ACCOUNT, 'eu-west-1', 'delivery-lab')

    def test_wrong_aws_account_fails_before_repository_access(self):
        self.registry.aws = Mock(return_value={'Account': '999999999999'})
        with self.assertRaisesRegex(ValueError, 'another account'):
            self.registry.repositories()
        self.registry.aws.assert_called_once_with('sts', 'get-caller-identity')

    def test_mutable_repository_is_rejected(self):
        self.registry.aws = Mock(side_effect=[{'Account': ACCOUNT}, {'repositories': [
            {'repositoryName': 'delivery-lab/' + service,
             'repositoryUri': f'{self.registry.host}/delivery-lab/{service}',
             'imageTagMutability': 'MUTABLE'} for service in release.SERVICES]}])
        with self.assertRaisesRegex(ValueError, 'immutable tags'):
            self.registry.repositories()

    def test_only_image_not_found_is_treated_as_missing(self):
        self.registry.aws = Mock(return_value={'images': [], 'failures': [{'failureCode': 'ImageNotFound'}]})
        self.assertIsNone(self.registry.digest('backend', SHA))
        self.registry.aws.return_value['failures'][0]['failureCode'] = 'KmsError'
        with self.assertRaisesRegex(ValueError, 'lookup failed'):
            self.registry.digest('backend', SHA)
        self.registry.aws.side_effect = subprocess.CalledProcessError(1, 'aws')
        with self.assertRaises(subprocess.CalledProcessError):
            self.registry.digest('backend', SHA)

    def test_registry_returns_manifest_digest_not_docker_config_id(self):
        self.registry.aws = Mock(return_value={'images': [{'imageId': {'imageDigest': BACK}}]})
        self.assertEqual(self.registry.digest('backend', SHA), BACK)
        self.registry.aws.return_value['images'][0]['imageId']['imageDigest'] = 'invalid'
        with self.assertRaisesRegex(ValueError, 'valid repository digest'):
            self.registry.digest('backend', SHA)


class DockerVerificationTests(unittest.TestCase):
    def test_remote_image_requires_correct_architecture_commit_and_manifest(self):
        good = {'Os': 'linux', 'Architecture': 'amd64', 'Id': 'image-id', 'Config': {'Labels': {
            'org.opencontainers.image.revision': SHA, 'com.delivery-lab.manifest-sha256': 'manifest'}}}
        with patch.object(release.subprocess, 'run'), \
                patch.object(release.subprocess, 'check_output', return_value=json.dumps([good])) as inspect:
            self.assertEqual(release.verify_image(('docker',), 'repository@' + BACK, SHA, 'manifest'), 'image-id')
            for change in ('arm', 'commit', 'manifest'):
                bad = json.loads(json.dumps(good))
                if change == 'arm':
                    bad['Architecture'] = 'arm64'
                elif change == 'commit':
                    bad['Config']['Labels']['org.opencontainers.image.revision'] = 'c' * 40
                else:
                    bad['Config']['Labels']['com.delivery-lab.manifest-sha256'] = 'different'
                inspect.return_value = json.dumps([bad])
                with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'Stored image differs'):
                    release.verify_image(('docker',), 'repository@' + BACK, SHA, 'manifest')

    def test_login_token_is_ephemeral_and_not_a_command_argument_even_on_failure(self):
        registry = release.Registry(ACCOUNT, 'eu-west-1', 'delivery-lab')
        registry.aws = Mock(return_value='temporary-token')
        with patch.dict(os.environ, {'DOCKER_HOST': 'unix:///test.sock'}), \
                patch.object(release.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'publication failed'):
                with release.authenticated_docker(registry) as docker:
                    config = Path(docker[2])
                    self.assertTrue(config.is_dir())
                    self.assertEqual(run.call_args.kwargs['input'], 'temporary-token\n')
                    self.assertNotIn('temporary-token', run.call_args.args[0])
                    raise RuntimeError('publication failed')
            self.assertFalse(config.exists())


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""Test prepared images with a disposable local Oracle DB; never access AWS or existing lab projects."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import urllib.request
from smoke import smoke


def check(receipt_path):
    receipt = json.loads(Path(receipt_path).read_text())
    backend, frontend = (receipt['images'][key] for key in ('backend', 'frontend'))
    for image in (backend, frontend):
        data = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image], text=True))[0]
        if (data['Os'], data['Architecture']) != ('linux', 'amd64'):
            raise ValueError('These tests require the Linux/amd64 images prepared for ECS.')
    prefix = 'deliverylab-aws-test-' + secrets.token_hex(6)
    admin_password = 'Lab9' + secrets.token_hex(13)
    app_password = 'Lab9' + secrets.token_hex(13)
    containers = []

    def docker(*args, input=None, required=True):
        result = subprocess.run(['docker', *args], input=input, text=True, capture_output=True)
        if required and result.returncode:
            message = (result.stdout + result.stderr).replace(admin_password, '[redacted]').replace(app_password, '[redacted]')
            raise RuntimeError(message)
        return result

    def start(name, *args):
        containers.append(name)
        docker('run', '-d', '--name', name, *args)

    def url(name):
        address = docker('port', name, '80/tcp').stdout.strip()
        if not address.startswith('127.0.0.1:'):
            raise ValueError('Expected a loopback-only test port.')
        return 'http://' + address

    def sql(db, commands):
        return docker('exec', '-i', db, 'sqlplus', '-L', '-s', '/', 'as', 'sysdba',
                      input='WHENEVER SQLERROR EXIT FAILURE\n' + commands + '\nEXIT;\n', required=False)

    docker('network', 'create', prefix)
    try:
        with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
            directory = Path(temporary)
            def envfile(name, values):
                path = directory / name
                with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as handle:
                    handle.write(''.join(f'{key}={value}\n' for key, value in values.items()))
                return str(path)
            db = prefix + '-db'
            admin_env = envfile('oracle.env', {'ORACLE_PWD': admin_password})
            print('Starting an isolated, temporary Oracle Free container...', flush=True)
            start(db, '--network', prefix, '--network-alias', 'db', '--memory', '3g', '--shm-size', '1g',
                  '--env-file', admin_env, 'container-registry.oracle.com/database/free:23.26.0.0-lite')
            for attempt in range(120):
                result = sql(db, "ALTER SESSION SET CONTAINER = FREEPDB1;\nSELECT 1 FROM dual;")
                if result.returncode == 0:
                    break
                time.sleep(5)
            else:
                raise RuntimeError('Temporary Oracle database did not become ready.')
            # Test fixture only: imitate an RDS master user on a new local DB.
            result = sql(db, f'''ALTER SESSION SET CONTAINER = FREEPDB1;
ALTER SYSTEM SET DB_CREATE_FILE_DEST='/opt/oracle/oradata' SCOPE=MEMORY;
CREATE USER LABADMIN IDENTIFIED BY "{admin_password}";
GRANT CREATE SESSION, CREATE TABLE, CREATE SEQUENCE TO LABADMIN WITH ADMIN OPTION;
GRANT CREATE USER, ALTER USER, CREATE TABLESPACE, SELECT_CATALOG_ROLE TO LABADMIN;''')
            if result.returncode:
                raise RuntimeError('Could not create the isolated test administrator; real RDS testing is still required.')
            app_values = {'DB_URL': 'jdbc:oracle:thin:@//db:1521/FREEPDB1', 'DB_USERNAME': 'APP',
                          'DB_PASSWORD': app_password, 'APP_ENV': 'DEV', 'RELEASE_ID': receipt['release']}
            app_env = envfile('app.env', app_values)
            bootstrap_env = envfile('bootstrap.env', dict(app_values, DB_ADMIN_USERNAME='LABADMIN', DB_ADMIN_PASSWORD=admin_password))
            def bootstrap(*extra, required=True):
                return docker('run', '--rm', '--platform', 'linux/amd64', '--memory', '512m',
                              '--network', prefix, '--env-file', bootstrap_env, *extra,
                              backend, '--bootstrap-rds', required=required)
            bootstrap()
            bootstrap()
            wrong = bootstrap('-e', 'DB_PASSWORD=Wrong9Password1234567890123456', required=False)
            if wrong.returncode == 0 or 'error 1017' not in wrong.stderr:
                raise RuntimeError('Bootstrap accepted a mismatched existing APP password.')
            print('Bootstrap, safe rerun and mismatched-password rejection passed.', flush=True)
            docker('run', '--rm', '--platform', 'linux/amd64', '--memory', '512m', '--network', prefix,
                   '--env-file', app_env, '-e', 'DB_MIGRATE=true', '-e', 'MIGRATE_ONLY=true',
                   '-e', 'SPRING_MAIN_WEB_APPLICATION_TYPE=none', backend)
            back = prefix + '-backend'
            # Reserve port 80 in this network namespace for the ECS-style frontend.
            start(back, '--platform', 'linux/amd64', '--memory', '768m', '--network', prefix,
                  '--network-alias', 'backend', '-p', '127.0.0.1::80', '--env-file', app_env,
                  '-e', 'DB_MIGRATE=false', backend)
            compose_front = prefix + '-compose-front'
            start(compose_front, '--platform', 'linux/amd64', '--memory', '128m', '--network', prefix,
                  '-p', '127.0.0.1::80', frontend)
            smoke(url(compose_front), receipt['release'], 'dev')
            ecs_front = prefix + '-ecs-front'
            start(ecs_front, '--platform', 'linux/amd64', '--memory', '128m', '--network', 'container:' + back,
                  '-e', 'BACKEND_UPSTREAM=127.0.0.1:8080', frontend)
            smoke(url(back), receipt['release'], 'dev')
            # Nginx variables must survive envsubst and SPA routes must still work.
            for address in (url(compose_front), url(back)):
                with urllib.request.urlopen(address + '/example/client/route', timeout=10) as response:
                    if b'id="root"' not in response.read():
                        raise RuntimeError('React client-side routing is broken.')
            invalid = docker('run', '--rm', '--platform', 'linux/amd64', '-e',
                             'BACKEND_UPSTREAM=backend:8080;bad', frontend, required=False)
            missing = docker('run', '--rm', '--platform', 'linux/amd64', backend, '--bootstrap-rds', required=False)
            if invalid.returncode == 0 or missing.returncode == 0:
                raise RuntimeError('A container did not reject invalid or missing configuration.')
            print('Both network layouts passed with the SAME image IDs, including health, metadata, SPA routing and Oracle CRUD.', flush=True)
            print('This used Oracle Free locally; it does not prove RDS Oracle 19c compatibility.', flush=True)
    finally:
        for name in reversed(containers):
            docker('rm', '-f', '-v', name, required=False)
        docker('network', 'rm', prefix, required=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--images', required=True, help='Image receipt from prepare_aws_images.py.')
    check(parser.parse_args().images)

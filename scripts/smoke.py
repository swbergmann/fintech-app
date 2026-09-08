#!/usr/bin/env python3
"""Check the deployed frontend, Java API, and real Oracle CRUD path."""
import argparse
import json
import time
import urllib.error
import urllib.request

def request(base, path, method='GET', body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as response:
        raw = response.read()
        return response.status, json.loads(raw) if raw else None

def smoke(base, release, environment):
    for attempt in range(60):
        try:
            _, health = request(base, '/api/health')
            if health['status'] == 'UP': break
        except (OSError, KeyError, ValueError): pass
        time.sleep(2)
    else: raise ValueError('Application did not become healthy.')
    _, meta = request(base, '/api/meta')
    _, frontend = request(base, '/release.json')
    if meta != {'environment': environment.upper(), 'release': release} or frontend['release'] != release:
        raise ValueError('Frontend/backend release or environment does not match the deployment.')
    with urllib.request.urlopen(base + '/', timeout=10) as response:
        if response.status != 200 or b'id="root"' not in response.read():
            raise ValueError('Frontend page was not served.')
    try:
        request(base, '/api/customers', 'POST', {'name': '', 'email': 'invalid'})
        raise ValueError('Invalid input was unexpectedly accepted.')
    except urllib.error.HTTPError as error:
        if error.code != 400: raise
    _, customer = request(base, '/api/customers', 'POST',
                          {'name': 'Pipeline smoke test', 'email': 'smoke@example.com'})
    try:
        _, records = request(base, '/api/customers')
        if customer not in records: raise ValueError('Created customer was not returned by Oracle.')
    finally:
        request(base, f'/api/customers/{customer["id"]}', 'DELETE')
    _, records = request(base, '/api/customers')
    if any(row['id'] == customer['id'] for row in records):
        raise ValueError('Customer deletion did not persist.')
    print(f'{environment.upper()} passed: matching release, health, validation and Oracle CRUD.')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--release', required=True)
    parser.add_argument('--environment', required=True)
    args = parser.parse_args()
    smoke(args.url, args.release, args.environment)

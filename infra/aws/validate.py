#!/usr/bin/env python3
"""Validate CloudFormation syntax and cross-file contracts locally; never call AWS."""
import argparse
import ipaddress
import itertools
import json
from pathlib import Path
import re
import subprocess
import sys

from cfnlint.decode import decode

ROOT = Path(__file__).resolve().parent


def require(condition, message):
    if not condition:
        raise ValueError(message)


def imports(value):
    if isinstance(value, dict):
        if 'Fn::ImportValue' in value:
            yield value['Fn::ImportValue']
        for child in value.values():
            yield from imports(child)
    elif isinstance(value, list):
        for child in value:
            yield from imports(child)


def validate(region):
    paths = sorted(ROOT.glob('*.yaml'))
    subprocess.run([sys.executable, '-c',
                    'import sys; from cfnlint.runner import main; sys.exit(main())', '--region', region,
                    '--template', *map(str, paths)], check=True)
    templates = {}
    for path in paths:
        template, errors = decode(str(path))
        require(not errors, f'{path.name}: YAML decoding failed')
        templates[path.stem] = template

    # cfn-lint validates each file separately; also check their import/export contract.
    providers = {'SharedStackName': 'shared', 'EnvironmentStackName': 'environment', 'AccessStackName': 'access'}
    exports = {
        parameter: {output['Export']['Name']['Fn::Sub'].replace('${AWS::StackName}-', '', 1)
                    for output in templates[name]['Outputs'].values() if 'Export' in output}
        for parameter, name in providers.items()
    }
    for name, template in templates.items():
        for value in imports(template):
            expression = value.get('Fn::Sub', '')
            match = re.fullmatch(r'\$\{(SharedStackName|EnvironmentStackName|AccessStackName)\}-(.+)', expression)
            require(match is not None, f'{name}: unrecognized stack import {expression}')
            require(match[2] in exports[match[1]], f'{name}: missing export for {expression}')

    environment = templates['environment']
    networks = environment['Mappings']['Network']
    vpcs = []
    for name in ('dev', 'uat', 'prod'):
        network = networks[name]
        vpc = ipaddress.ip_network(network['Vpc'])
        vpcs.append(vpc)
        subnets = [ipaddress.ip_network(network[key])
                   for key in ('PublicA', 'PublicB', 'DatabaseA', 'DatabaseB')]
        require(all(subnet.subnet_of(vpc) for subnet in subnets), f'{name}: subnet outside VPC')
        require(all(not a.overlaps(b) for a, b in itertools.combinations(subnets, 2)),
                f'{name}: overlapping subnets')

        profile = json.loads((ROOT / 'parameters' / f'{name}.json').read_text())
        values = {item['ParameterKey']: item['ParameterValue'] for item in profile}
        require(len(values) == len(profile), f'{name}: duplicate parameter')
        require(values.get('EnvironmentName') == name, f'{name}: profile targets another environment')
        for key, value in values.items():
            require(key in environment['Parameters'], f'{name}: unknown parameter {key}')
            schema = environment['Parameters'][key]
            if 'AllowedValues' in schema:
                require(value in schema['AllowedValues'], f'{name}: invalid {key}')
            if 'AllowedPattern' in schema:
                require(re.fullmatch(schema['AllowedPattern'], value), f'{name}: invalid {key}')
            if schema['Type'] == 'Number':
                number = float(value)
                require(number >= schema.get('MinValue', float('-inf')) and
                        number <= schema.get('MaxValue', float('inf')), f'{name}: invalid {key}')
        missing = {key for key, schema in environment['Parameters'].items()
                   if 'Default' not in schema and key not in values}
        require(missing == {'AllowedClientCidr', 'OracleEngineVersion', 'ExpiresAt'},
                f'{name}: unexpected required inputs {sorted(missing)}')
    require(all(not a.overlaps(b) for a, b in itertools.combinations(vpcs, 2)),
            'Environment VPC address ranges overlap')
    print(f'Validated {len(paths)} templates, stack references, network isolation and 3 parameter profiles.')
    print('No AWS calls or deployments. Client CIDR, Oracle version and expiry remain deployment inputs.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--region', default='eu-west-1', help='Schema region for cfn-lint only.')
    args = parser.parse_args()
    try:
        validate(args.region)
    except (ValueError, subprocess.CalledProcessError) as error:
        print(f'Validation failed: {error}', file=sys.stderr)
        sys.exit(1)

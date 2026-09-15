"""Fail CI on high/critical findings in the JavaScript and Java CodeQL reports."""

import argparse
import json
from pathlib import Path
import sys


def blocking_findings(report):
    data = json.loads(report.read_text())
    if data['version'] != '2.1.0' or not data['runs']:
        raise ValueError(f'{report.name}: expected a SARIF 2.1.0 analysis run')
    findings = []
    for run in data['runs']:
        if run['tool']['driver']['name'] != 'CodeQL':
            raise ValueError(f'{report.name}: expected CodeQL results')
        if any(item.get('executionSuccessful') is False for item in run.get('invocations', [])):
            raise ValueError(f'{report.name}: analysis did not complete successfully')
        # CodeQL groups rules by query pack in tool.extensions; older reports use driver.rules.
        components = [run['tool']['driver'], *run['tool'].get('extensions', [])]
        rules = [rule for component in components for rule in component.get('rules', [])]
        if not rules or not isinstance(run['results'], list):
            raise ValueError(f'{report.name}: missing rules or invalid results')
        for result in run['results']:
            reference = result.get('rule', {})
            rule_id = result.get('ruleId', reference.get('id'))
            if rule_id is not None:
                matches = [rule for rule in rules if rule['id'] == rule_id]
                if len(matches) != 1:
                    raise ValueError(f'{report.name}: cannot resolve rule {rule_id}')
                rule = matches[0]
            else:
                component = run['tool']['driver']
                if 'toolComponent' in reference:
                    extension = reference['toolComponent']['index']
                    if not isinstance(extension, int) or extension < 0:
                        raise ValueError('Invalid rule component index')
                    component = run['tool']['extensions'][extension]
                index = reference.get('index', result.get('ruleIndex'))
                if not isinstance(index, int) or index < 0:
                    raise ValueError('Missing or invalid rule index')
                rule = component['rules'][index]
            properties = rule.get('properties', {})
            severity = properties.get('security-severity')
            if severity is None:
                if 'security' in properties.get('tags', []):
                    raise ValueError(f"{report.name}: missing security severity for {rule['id']}")
                continue
            score = float(severity)
            if not 0 <= score <= 10:
                raise ValueError(f'{report.name}: invalid security severity {severity}')
            if score >= 7.0:
                findings.append(f"{report.name}: {rule['id']} (security severity {score:g})")
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results', type=Path, help='Directory produced by codeql-action/analyze')
    args = parser.parse_args()
    try:
        findings = []
        # Require both reports: a missing scan must not silently pass the gate.
        for language in ('javascript', 'java'):
            findings.extend(blocking_findings(args.results / f'{language}.sarif'))
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError) as error:
        print(f'CodeQL gate could not verify the reports: {error}', file=sys.stderr)
        return 2
    for finding in findings:
        print(finding)
    if findings:
        print(f'CodeQL gate failed: {len(findings)} high or critical finding(s).')
        return 1
    print('CodeQL gate passed: no high or critical findings in JavaScript or Java.')
    return 0


if __name__ == '__main__':
    sys.exit(main())

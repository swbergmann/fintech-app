import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'check_codeql.py'


class CodeQLGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.results = Path(temporary.name)
        for language in ('javascript', 'java'):
            self.write_report(language)

    def write_report(self, language, severity='8.1', finding=False, grouped=True, by_index=False):
        rule = {'id': f'{language}/sql-injection',
                'properties': {'tags': ['security'], 'security-severity': severity}}
        driver = {'name': 'CodeQL', 'rules': [] if grouped else [rule]}
        tool = {'driver': driver}
        result = {'ruleId': rule['id'], 'level': 'warning',
                  'message': {'text': 'Example finding'}}
        if grouped:
            tool['extensions'] = [{'name': f'codeql/{language}-queries', 'rules': [rule]}]
        if by_index:
            result.pop('ruleId')
            if grouped:
                result['rule'] = {'index': 0, 'toolComponent': {'index': 0}}
            else:
                result['ruleIndex'] = 0
        data = {'version': '2.1.0', 'runs': [
            {'tool': tool, 'results': [result] if finding else []}]}
        path = self.results / f'{language}.sarif'
        path.write_text(json.dumps(data))
        return path, data

    def run_gate(self):
        return subprocess.run([sys.executable, str(SCRIPT), str(self.results)],
                              capture_output=True, text=True)

    def test_clean_reports_pass(self):
        self.assertEqual(self.run_gate().returncode, 0)

    def test_low_and_medium_findings_pass(self):
        self.write_report('javascript', severity='3.9', finding=True)
        self.write_report('java', severity='6.9', finding=True)
        self.assertEqual(self.run_gate().returncode, 0)

    def test_high_and_critical_findings_block_for_either_language(self):
        for language in ('javascript', 'java'):
            for severity in ('7.0', '9.8'):
                with self.subTest(language=language, severity=severity):
                    self.write_report(language, severity=severity, finding=True)
                    result = self.run_gate()
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(f'{language}/sql-injection', result.stdout)
                    self.write_report(language)

    def test_driver_rules_and_rule_indexes_are_supported(self):
        for grouped in (True, False):
            for by_index in (True, False):
                with self.subTest(grouped=grouped, by_index=by_index):
                    self.write_report('java', finding=True, grouped=grouped, by_index=by_index)
                    self.assertEqual(self.run_gate().returncode, 1)

    def test_missing_report_blocks(self):
        for language in ('javascript', 'java'):
            with self.subTest(language=language):
                (self.results / f'{language}.sarif').unlink()
                self.assertEqual(self.run_gate().returncode, 2)
                self.write_report(language)

    def test_malformed_and_empty_reports_block(self):
        for report in ('{', '{}', '{"version":"2.1.0","runs":[]}'):
            with self.subTest(report=report):
                (self.results / 'java.sarif').write_text(report)
                self.assertEqual(self.run_gate().returncode, 2)

    def test_unresolved_rule_blocks(self):
        path, data = self.write_report('java', finding=True)
        data['runs'][0]['results'][0]['ruleId'] = 'java/unknown'
        path.write_text(json.dumps(data))
        self.assertEqual(self.run_gate().returncode, 2)

    def test_missing_or_invalid_security_severity_blocks(self):
        for severity in (None, 'invalid', 'NaN', '-1', '11'):
            with self.subTest(severity=severity):
                self.write_report('java', severity=severity, finding=True)
                self.assertEqual(self.run_gate().returncode, 2)

    def test_failed_analysis_blocks(self):
        path, data = self.write_report('java')
        data['runs'][0]['invocations'] = [{'executionSuccessful': False}]
        path.write_text(json.dumps(data))
        self.assertEqual(self.run_gate().returncode, 2)

    def test_non_security_warning_is_not_a_security_failure(self):
        path, data = self.write_report('java', finding=True)
        data['runs'][0]['tool']['extensions'][0]['rules'][0]['properties'] = {}
        path.write_text(json.dumps(data))
        self.assertEqual(self.run_gate().returncode, 0)


if __name__ == '__main__':
    unittest.main()

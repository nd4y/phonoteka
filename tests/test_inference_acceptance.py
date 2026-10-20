"""Reject false concurrency/ownership claims without starting Docker or a model."""
import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest

def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name+'.py'))
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj

driver = module('inference_acceptance')
probe = module('acceptance_image_probe')


class AcceptanceChecks(unittest.TestCase):
    def setUp(self):
        self.ordinary = {'passed_measured_checks': True,
                         'background': {'progress': [{'at': 10}, {'at': 20}, {'at': 35}]},
                         'resources': {'samples': [{'at': 5}, {'at': 50}]}}
        self.inference = {'passed': True, 'phases': [{'start': 9, 'end': 25}, {'start': 40, 'end': 49}],
                          'inference_api_offline_window': {'start': 30, 'end': 39}}

    def test_seven_original_predicates_pass(self):
        checks = driver.combined_checks(self.ordinary, self.inference, None, [0, 0])
        self.assertEqual(set(checks), driver.CHECKS)
        self.assertTrue(all(checks.values()))

    def test_independent_failures(self):
        cases = [({}, self.inference, None, [0, 0]), (self.ordinary, {}, None, [0, 0]),
                 (self.ordinary, self.inference, 'pressure', [0, 0]),
                 (self.ordinary, self.inference, None, [0]),
                 (self.ordinary, self.inference, None, [0, 1])]
        changed = copy.deepcopy(self.ordinary)
        changed['resources']['samples'][-1]['at'] = 40
        cases.append((changed, self.inference, None, [0, 0]))
        changed = copy.deepcopy(self.ordinary)
        changed['background']['progress'] = [{'at': 10}, {'at': 20}]
        cases.append((changed, self.inference, None, [0, 0]))
        for args in cases:
            with self.subTest(args=args):
                self.assertFalse(all(driver.combined_checks(*args).values()))

    def test_readiness_is_measured_ordered_finite_and_after_launch(self):
        value = {'state': 'measurement_sample_observed', 'measurement_started_at_monotonic': 10,
                 'resource_sample_at_monotonic': 11, 'ready_at_monotonic': 12}
        self.assertTrue(driver.measured_readiness(value, 9, 13))
        for key in ('measurement_started_at_monotonic', 'resource_sample_at_monotonic', 'ready_at_monotonic'):
            for bad in (None, float('nan'), float('inf'), True, -1, 15):
                with self.subTest(key=key, bad=bad):
                    self.assertFalse(driver.measured_readiness({**value, key: bad}, 9, 13))

    def test_source_probe_exact_bytes_and_file_set(self):
        with tempfile.TemporaryDirectory() as root:
            expected, actual = Path(root)/'expected', Path(root)/'actual'
            expected.mkdir(); actual.mkdir()
            for p in (expected, actual):
                (p/'__init__.py').write_text('x=1')
            self.assertEqual(probe.verify(expected, actual)['modules'], 1)
            for path, content in [('__init__.py', 'x=2'), ('extra.py', 'x=1')]:
                (actual/path).write_text(content)
                with self.assertRaises(RuntimeError):
                    probe.verify(expected, actual)
                (actual/path).unlink()
                (actual/'__init__.py').write_text('x=1')

    def test_ownership_and_resource_guards(self):
        cid, image, name, scope = 'a'*64, 'sha256:'+'b'*64, 'phonoteka-test', 'acceptance-test'
        item = {'Id': cid, 'Image': image, 'Name': '/'+name,
                'Config': {'Labels': {'io.phonoteka.acceptance': scope}},
                'HostConfig': {'Memory': 256*driver.MIB, 'MemorySwap': 256*driver.MIB,
                               'NanoCpus': 10**9, 'NetworkMode': 'none', 'ReadonlyRootfs': True,
                               'Privileged': False, 'PidsLimit': 256, 'CapDrop': ['ALL'],
                               'CapAdd': None, 'SecurityOpt': ['no-new-privileges']}}
        driver.verify_owned(item, cid, name, image, scope, 'ordinary')
        for key, bad in [('Id', 'c'*64), ('Image', 'sha256:'+'d'*64), ('Name', '/foreign')]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                driver.verify_owned({**item, key: bad}, cid, name, image, scope, 'ordinary')
        for key, bad in [('Memory', 512*driver.MIB), ('MemorySwap', -1), ('NanoCpus', 2*10**9),
                         ('NetworkMode', 'host'), ('ReadonlyRootfs', False), ('Privileged', True),
                         ('PidsLimit', -1), ('CapDrop', []), ('CapAdd', ['SYS_ADMIN']),
                         ('SecurityOpt', []), ('PublishAllPorts', True), ('PortBindings', {'80/tcp': [{}]})]:
            changed = copy.deepcopy(item); changed['HostConfig'][key] = bad
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                driver.verify_owned(changed, cid, name, image, scope, 'ordinary')
        changed = copy.deepcopy(item)
        changed['Config']['Labels']['io.phonoteka.acceptance'] = 'foreign'
        with self.assertRaises(RuntimeError):
            driver.verify_owned(changed, cid, name, image, scope, 'ordinary')


if __name__ == '__main__':
    unittest.main()

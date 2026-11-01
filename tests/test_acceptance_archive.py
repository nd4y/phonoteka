import importlib.util
from pathlib import Path
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('acceptance_archive', Path(__file__).with_name('package_acceptance_reports.py'))
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)


class AcceptanceArchiveTests(unittest.TestCase):
    def test_preserves_partial_failure_reports_and_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'reports'; root.mkdir()
            (root/'result.json').write_text('{"passed":false}')
            (root/'ordinary').mkdir(); (root/'ordinary/source.json').write_text('{"modules":60}')
            target = Path(directory)/'evidence.tar.gz'
            digest = archive.package(root, target)
            self.assertEqual(target.with_name(target.name+'.sha256').read_text(), digest+'  evidence.tar.gz\n')
            with tarfile.open(target) as bundle:
                self.assertEqual(set(bundle.getnames()), {'result.json', 'ordinary/source.json'})
                self.assertEqual(bundle.extractfile('result.json').read(), b'{"passed":false}')

    def test_refuses_unexpected_files_and_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'reports'; root.mkdir()
            (root/'result.json').write_text('{}'); (root/'key').write_text('synthetic-forbidden-file')
            target = Path(directory)/'evidence.tar.gz'
            with self.assertRaises(ValueError):
                archive.package(root, target)
            (root/'key').unlink(); target.write_bytes(b'preserved')
            with self.assertRaises(ValueError):
                archive.package(root, target)
            self.assertEqual(target.read_bytes(), b'preserved')

    def test_refuses_symlinks_and_missing_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'reports'; root.mkdir()
            target = Path(directory)/'evidence.tar.gz'
            with self.assertRaises(ValueError):
                archive.package(root, target)
            (root/'result.json').write_text('{}')
            (root/'inference.console.log').symlink_to(root/'result.json')
            with self.assertRaises(ValueError):
                archive.package(root, target)


if __name__ == '__main__':
    unittest.main()

"""Memory regression accounting must distinguish file cache from actual growth."""
import unittest
from performance_acceptance import cgroup_noncache_memory, cgroup_growth_within_budget

MIB = 1024 ** 2


class PerformanceMemoryTests(unittest.TestCase):
    def sample(self, current, file=0, shmem=0, dirty=0, writeback=0):
        return cgroup_noncache_memory(current * MIB, {
            'file': file * MIB, 'shmem': shmem * MIB,
            'file_dirty': dirty * MIB, 'file_writeback': writeback * MIB,
        })

    def test_clean_cache_warmup_does_not_imply_anonymous_growth(self):
        self.assertEqual(self.sample(100, file=10), self.sample(180, file=90))

    def test_anonymous_growth_is_not_hidden_by_cache_warmup(self):
        before = self.sample(100, file=10)
        after = self.sample(245, file=90)
        self.assertGreater(after - before, 64 * MIB)

    def test_tmpfs_database_growth_still_counts(self):
        self.assertEqual(self.sample(100, file=20, shmem=20), 100 * MIB)
        self.assertGreater(self.sample(165, file=85, shmem=85)
                           - self.sample(100, file=20, shmem=20), 64 * MIB)

    def test_dirty_and_writeback_pages_still_count(self):
        self.assertEqual(self.sample(100, file=50, dirty=20, writeback=10), 80 * MIB)
        self.assertEqual(self.sample(100, file=50, dirty=50, writeback=50), 100 * MIB)

    def test_missing_or_invalid_observations_fail_closed(self):
        valid = {'file': 0, 'shmem': 0, 'file_dirty': 0, 'file_writeback': 0}
        for current in (None, -1, True, '100'):
            with self.subTest(current=current):
                self.assertIsNone(cgroup_noncache_memory(current, valid))
        for key in valid:
            for value in (None, -1, False, '0'):
                with self.subTest(key=key, value=value):
                    self.assertIsNone(cgroup_noncache_memory(100, {**valid, key: value}))
            missing = dict(valid)
            del missing[key]
            self.assertIsNone(cgroup_noncache_memory(100, missing))

    def test_inconsistent_memory_stat_cannot_pass(self):
        self.assertIsNone(self.sample(10, file=11))
        self.assertIsNone(self.sample(100, file=10, shmem=11))

    def test_no_file_cache_preserves_total_usage(self):
        self.assertEqual(self.sample(128), 128 * MIB)

    def test_total_usage_must_fit_limit_even_when_clean_cache_is_excluded(self):
        samples = [{'cgroup_memory_current_bytes': 129 * MIB,
                    'cgroup_noncache_memory_bytes': MIB}]
        self.assertFalse(cgroup_growth_within_budget(samples, 128 * MIB, 0, 64 * MIB))
        self.assertTrue(cgroup_growth_within_budget(samples, 256 * MIB, 0, 64 * MIB))

    def test_growth_boundary_and_missing_counters(self):
        samples = [{'cgroup_memory_current_bytes': 128 * MIB,
                    'cgroup_noncache_memory_bytes': 100 * MIB}]
        self.assertTrue(cgroup_growth_within_budget(samples, 256 * MIB, 64 * MIB, 64 * MIB))
        self.assertFalse(cgroup_growth_within_budget(samples, 256 * MIB, 64 * MIB + 1, 64 * MIB))
        self.assertFalse(cgroup_growth_within_budget(samples + [{}], 256 * MIB, 0, 64 * MIB))
        self.assertFalse(cgroup_growth_within_budget([
            {'cgroup_noncache_memory_bytes': MIB, 'cgroup_memory_current_bytes': None}
        ], 256 * MIB, 0, 64 * MIB))

    def test_unbounded_or_invalid_budget_cannot_be_accepted(self):
        samples = [{'cgroup_memory_current_bytes': 128 * MIB,
                    'cgroup_noncache_memory_bytes': 100 * MIB}]
        for limit in (None, 'max', 0, -1, True):
            self.assertFalse(cgroup_growth_within_budget(samples, limit, 0, 64 * MIB))
        for delta in (None, float('nan'), float('inf'), True):
            self.assertFalse(cgroup_growth_within_budget(samples, 256 * MIB, delta, 64 * MIB))
        self.assertFalse(cgroup_growth_within_budget([], 256 * MIB, 0, 64 * MIB))


if __name__ == '__main__':
    unittest.main()

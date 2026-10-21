import json
import os
import shutil
import tempfile
import unittest

from ps5farm import Farm, Job
from ps5farm.job import confined
from tests.fakes import FakeConsole, LocalExporter


class FarmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fakes = []

    def tearDown(self):
        for f in self.fakes:
            f.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fleet(self, *modes):
        port = 0
        for i, mode in enumerate(modes):
            f = FakeConsole(os.path.join(self.tmp, f"c{i}"), f"127.0.0.{i + 2}", port, mode)
            port = f.port
            self.fakes.append(f)
        return Farm([f.console() for f in self.fakes], os.path.join(self.tmp, "out"),
                    exporter=LocalExporter(self.fakes), cooldown=0.2, log=lambda s: None)

    def jobs(self, n):
        return [Job(id=f"j{i}", task="make it", expect=["out.txt"]) for i in range(n)]

    def test_all_jobs_ok_and_one_at_a_time_per_console(self):
        farm = self.fleet("good", "good", "good")
        res = farm.run(self.jobs(9))
        self.assertEqual({r.status for r in res}, {"ok"})
        self.assertEqual(len(res), 9)
        self.assertTrue(all(f.max_active == 1 for f in self.fakes))
        self.assertTrue(all(len(f.jobs_done) >= 1 for f in self.fakes), "work spread across fleet")
        for i in range(9):
            self.assertEqual(open(os.path.join(self.tmp, "out", f"j{i}", "out.txt")).read(), f"done jobs/j{i}")
        lines = open(os.path.join(self.tmp, "out", "results.jsonl")).read().splitlines()
        self.assertEqual(len(lines), 9)
        self.assertEqual(json.loads(lines[0])["status"], "ok")

    def test_dead_console_is_skipped(self):
        farm = self.fleet("down", "good", "good")
        res = farm.run(self.jobs(4))
        self.assertEqual({r.status for r in res}, {"ok"})
        self.assertEqual(self.fakes[0].jobs_done, [])

    def test_failed_output_is_retried_on_another_console(self):
        farm = self.fleet("lazy", "good")
        res = farm.run(self.jobs(1))
        self.assertEqual(res[0].status, "ok")
        self.assertEqual(res[0].attempts, 2)
        self.assertEqual(res[0].console, self.fakes[1].ip)

    def test_gives_up_after_max_attempts(self):
        farm = self.fleet("lazy", "lazy")
        res = farm.run(self.jobs(1))
        self.assertEqual(res[0].status, "failed")
        self.assertEqual(res[0].attempts, 2)
        self.assertIn("missing or empty: out.txt", res[0].error)

    def test_writes_outside_job_folder_are_blocked(self):
        farm = self.fleet("rogue")
        res = farm.run(self.jobs(1))
        self.assertEqual(res[0].status, "ok")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "etc")))
        self.assertTrue(os.path.isfile(os.path.join(self.fakes[0].root, "jobs/j0/out.txt")))

    def test_no_healthy_console(self):
        farm = self.fleet("down")
        farm.cooldown = 0.1
        res = farm.run(self.jobs(2))
        self.assertEqual({r.status for r in res}, {"console_error"})

    def test_confined(self):
        j = Job(id="a", task="x")
        self.assertTrue(confined(j, "jobs/a/x.txt"))
        self.assertTrue(confined(j, "/jobs/a/x.txt"))
        self.assertFalse(confined(j, "jobs/b/x.txt"))
        self.assertFalse(confined(j, "jobs/a/../b/x.txt"))
        self.assertFalse(confined(j, "../jobs/a/x.txt"))
        self.assertFalse(confined(j, "jobs/ab/x.txt"))


if __name__ == "__main__":
    unittest.main()

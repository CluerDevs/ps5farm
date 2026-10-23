import os
import shutil
import tempfile
import unittest

from ps5farm import Farm, Job
from tests.fakes import FakeConsole, LocalExporter


class ThinkingOptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fake = FakeConsole(os.path.join(self.tmp, "c0"), "127.0.0.2")

    def tearDown(self):
        self.fake.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_job(self, **kw):
        farm = Farm([self.fake.console()], os.path.join(self.tmp, "out"),
                    exporter=LocalExporter([self.fake]), cooldown=0.2, log=lambda s: None)
        return farm.run([Job(id="t", task="make it", expect=["out.txt"], **kw)])[0]

    def test_default_sends_no_template_kwargs(self):
        self.assertEqual(self.run_job().status, "ok")
        self.assertTrue(self.fake.bodies)
        self.assertTrue(all("chat_template_kwargs" not in b for b in self.fake.bodies))

    def test_thinking_false_disables_reasoning_on_every_request(self):
        self.assertEqual(self.run_job(thinking=False).status, "ok")
        self.assertGreaterEqual(len(self.fake.bodies), 2)
        for b in self.fake.bodies:
            self.assertEqual(b["chat_template_kwargs"], {"enable_thinking": False})

    def test_from_dict_accepts_and_validates_thinking(self):
        self.assertFalse(Job.from_dict({"id": "a", "task": "x", "thinking": False}).thinking)
        self.assertTrue(Job.from_dict({"id": "a", "task": "x"}).thinking)
        with self.assertRaises(ValueError):
            Job.from_dict({"id": "a", "task": "x", "thinking": "no"})


if __name__ == "__main__":
    unittest.main()

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from ps5farm import Console, ConsoleError, Farm, Job, run_job
from ps5farm import cli, export
from ps5farm.export import FtpExporter, parse_list_line
from tests.fake_ftp import FakeFtp
from tests.fakes import FakeConsole, LocalExporter


def rd(path):
    with open(path) as fh:
        return fh.read()


def wr(path, text):
    with open(path, "w") as fh:
        fh.write(text)


def msg(content=None, calls=None, finish="stop"):
    m = {"role": "assistant", "content": content}
    if calls is not None:
        m["tool_calls"] = calls
    return {"message": m, "finish_reason": finish}


def call(name, args, id="c1"):
    c = {"type": "function", "function": {"name": name, "arguments": args}}
    if id is not None:
        c["id"] = id
    return c


def write(path, text="hello", id="c1"):
    return call("write_file", json.dumps({"path": path, "content": text}), id)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fakes = []
        self.stoppers = []

    def tearDown(self):
        for f in self.fakes:
            f.stop()
        for s in self.stoppers:
            s.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake(self, mode="script", i=0):
        f = FakeConsole(os.path.join(self.tmp, f"c{i}"), f"127.0.0.{i + 2}", 0, mode)
        self.fakes.append(f)
        return f

    def farm(self, **kw):
        kw.setdefault("cooldown", 0.2)
        return Farm([f.console() for f in self.fakes], os.path.join(self.tmp, "out"),
                    exporter=LocalExporter(self.fakes), log=lambda s: None, **kw)


class JobIdTests(unittest.TestCase):
    def test_hostile_ids_rejected(self):
        for bad in ["", " ", "a b", "a/b", "../x", "..", ".", "a/../..", "/abs", "-rf", ".hidden",
                    "x" * 65, "a\nb", "a\x00b", "a\\b", "ünï", "name.", "results.jsonl",
                    "results", "CON", "nul.txt", "jobs/x"]:
            with self.assertRaises(ValueError, msg=repr(bad)):
                Job(id=bad, task="t")

    def test_good_ids(self):
        for ok in ["a", "A1", "job-1", "job_1", "v1.2", "7", "x" * 64]:
            self.assertEqual(Job(id=ok, task="t").dir, f"jobs/{ok}")

    def test_from_dict_errors_are_valueerrors(self):
        for bad in [[], "x", {"task": "t"}, {"id": "a"}, {"id": "a", "task": ""},
                    {"id": "a", "task": "t", "expec": ["x"]},          # typo'd field
                    {"id": "a", "task": "t", "max_steps": 0},
                    {"id": "a", "task": "t", "max_steps": "many"},
                    {"id": "a", "task": "t", "expect": ["../x"]},
                    {"id": "a", "task": "t", "expect": ["/etc/passwd"]},
                    {"id": "a", "task": "t", "expect": [3]},
                    {"id": True, "task": "t"}, {"id": ["a"], "task": "t"},
                    {"id": "a", "task": "t", "system": 5}]:
            with self.assertRaises(ValueError, msg=repr(bad)):
                Job.from_dict(bad)

    def test_expect_string_is_one_file_not_letters(self):
        self.assertEqual(Job.from_dict({"id": "a", "task": "t", "expect": "a.txt"}).expect, ["a.txt"])
        self.assertEqual(Job.from_dict({"id": 5, "task": "t", "expect": ["./d//f.txt"]}).expect, ["d/f.txt"])


class ListParsingTests(unittest.TestCase):
    def test_formats(self):
        P = parse_list_line
        self.assertEqual(P("-rw-r--r-- 1 ps5 ps5 12 Jan  1 12:00 notes v2.txt"), ("file", "notes v2.txt"))
        self.assertEqual(P("-rw-r--r-- 1 ps5 ps5 12 Jan  1 12:00 two  spaces  .txt"), ("file", "two  spaces  .txt"))
        self.assertEqual(P("drwxr-xr-x 2 root wheel 512 Feb 29  2024 sub dir"), ("dir", "sub dir"))
        self.assertEqual(P("lrwxrwxrwx 1 ps5 ps5 3 Jan  1 12:00 link -> /etc"), ("link", "link"))
        self.assertEqual(P("-rw-r--r--+ 1 ps5 ps5 0 Mar 10 2023 plus.txt"), ("file", "plus.txt"))
        self.assertEqual(P("01-02-24  10:00AM       <DIR>          folder x"), ("dir", "folder x"))
        self.assertEqual(P("01-02-24  10:00AM              1234 f.txt"), ("file", "f.txt"))

    def test_ignored_lines(self):
        for line in ["", "   ", "total 8", "drwxr-xr-x 2 a b 0 Jan  1 12:00 .",
                     "drwxr-xr-x 2 a b 0 Jan  1 12:00 .."]:
            self.assertIsNone(parse_list_line(line))

    def test_unknown_format_fails_loudly(self):
        with self.assertRaises(ValueError):
            parse_list_line("something completely different")


class FtpTests(Base):
    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, "scratch")
        os.makedirs(os.path.join(self.root, "jobs"))
        self.ftp = FakeFtp(self.root)
        self.stoppers.append(self.ftp)
        self.console = Console("127.0.0.1", 1, self.ftp.port)
        self.ex = FtpExporter(timeout=5)

    def put(self, rel, text="x"):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write(text)

    def test_fetch_names_with_spaces_and_subfolders(self):
        self.put("jobs/j/notes v2.txt", "one")
        self.put("jobs/j/sub dir/inner file.md", "two")
        dest = os.path.join(self.tmp, "dest")
        files = self.ex.fetch(self.console, "jobs/j", dest)
        self.assertEqual(sorted(files), ["notes v2.txt", "sub dir/inner file.md"])
        self.assertEqual(rd(os.path.join(dest, "sub dir", "inner file.md")), "two")
        self.assertNotIn("NLST", [c for c, _ in self.ftp.log])

    def test_symlinks_are_not_followed(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        wr(os.path.join(outside, "secret.txt"), "s")
        self.put("jobs/j/real.txt")
        os.symlink(outside, os.path.join(self.root, "jobs/j/escape"))
        dest = os.path.join(self.tmp, "dest")
        self.assertEqual(self.ex.fetch(self.console, "jobs/j", dest), ["real.txt"])
        self.ex.cleanup(self.console, "jobs/j")
        self.assertTrue(os.path.isfile(os.path.join(outside, "secret.txt")), "link target must survive")
        self.assertFalse(os.path.exists(os.path.join(self.root, "jobs/j")))

    def test_fetch_missing_folder_is_empty(self):
        self.assertEqual(self.ex.fetch(self.console, "jobs/none", os.path.join(self.tmp, "d")), [])

    def test_cleanup_removes_only_the_job_folder(self):
        self.put("jobs/j/a.txt")
        self.put("jobs/j/deep/er/b.txt")
        self.put("jobs/jj/keep.txt")
        self.put("jobs/other/keep.txt")
        self.ex.cleanup(self.console, "jobs/j")  # server answers 226 to DELE/RMD
        self.assertFalse(os.path.exists(os.path.join(self.root, "jobs/j")))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "jobs/jj/keep.txt")))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "jobs/other/keep.txt")))

    def test_cleanup_of_missing_folder_is_fine(self):
        self.ex.cleanup(self.console, "jobs/never-existed")

    def test_cleanup_unreachable_ftp_raises(self):
        dead = Console("127.0.0.1", 1, 1)  # nothing listens on port 1
        with self.assertRaises(OSError):
            self.ex.cleanup(dead, "jobs/j")

    def test_dangerous_job_dirs_refused_before_any_ftp_traffic(self):
        for bad in ["jobs/", "jobs", "", "jobs/..", "jobs/.", "jobs/a/b", "../jobs/a", "jobs/a b",
                    "/data/PS5LM/scratch", "jobs/../..", "other/a"]:
            with self.assertRaises(ValueError, msg=bad):
                self.ex.cleanup(self.console, bad)
            with self.assertRaises(ValueError, msg=bad):
                self.ex.fetch(self.console, bad, os.path.join(self.tmp, "d"))
        self.assertEqual(self.ftp.log, [])

    def test_unrecognised_list_format_is_an_error_not_silence(self):
        self.put("jobs/j/a.txt")
        self.ftp.line_fmt = "{name} {size}"
        with self.assertRaises(ValueError):
            self.ex.fetch(self.console, "jobs/j", os.path.join(self.tmp, "d"))

    def test_hostile_name_from_console_cannot_escape_dest(self):
        dest = os.path.join(self.tmp, "dest")
        for evil in ["../../evil.txt", "a/b", "..", "x\r\nDELE y"]:
            with mock.patch.object(export, "_entries", return_value=[("file", evil)]):
                with self.assertRaises(ValueError, msg=evil):
                    self.ex.fetch(self.console, "jobs/j", dest)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.txt")))

    def test_check(self):
        self.assertIsNone(self.ex.check(self.console))
        self.assertIsNotNone(self.ex.check(Console("127.0.0.1", 1, 1)))

    def test_end_to_end_with_real_exporter(self):
        f = FakeConsole(self.root, "127.0.0.1", 0, "good")
        self.fakes.append(f)
        self.put("jobs/other/keep.txt")
        self.put("jobs/e2e/stale.txt", "left from an earlier run")
        farm = Farm([f.console(self.ftp.port)], os.path.join(self.tmp, "out"),
                    exporter=FtpExporter(timeout=5), cooldown=0.2, log=lambda s: None)
        res = farm.run([Job(id="e2e", task="go", expect=["notes v2.txt"])])
        self.assertEqual(res[0].status, "ok", res[0].error)
        out = os.path.join(self.tmp, "out", "e2e")
        self.assertEqual(rd(os.path.join(out, "notes v2.txt")), "done jobs/e2e")
        self.assertFalse(os.path.exists(os.path.join(out, "stale.txt")), "folder is cleaned first")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "jobs/other/keep.txt")))

    def test_ftp_down_fails_fast_before_spending_model_time(self):
        f = self.fake("good")
        farm = Farm([f.console(1)], os.path.join(self.tmp, "out"), exporter=FtpExporter(timeout=2),
                    cooldown=0.1, max_attempts=1, log=lambda s: None)
        res = farm.run([Job(id="j", task="go", expect=["out.txt"])])
        self.assertEqual(res[0].status, "failed")
        self.assertIn("ftpsrv", res[0].error)
        self.assertEqual(f.bodies, [], "no chat request should have been made")


class RunJobTests(Base):
    def run_one(self, script, job=None, **kw):
        f = self.fake()
        f.script = list(script)
        job = job or Job(id="j", task="go", expect=["x.txt"])
        return f, run_job(f.console(), job, **kw)

    def test_length_without_answer_fails_with_clear_error_after_raising_budget(self):
        stuck = {"message": {"role": "assistant", "content": "", "reasoning_content": "hmm..."},
                 "finish_reason": "length"}
        f, res = self.run_one([stuck] * 10, Job(id="j", task="go", max_tokens=10000))
        self.assertEqual(res.status, "failed")
        self.assertIn("ran out of tokens", res.error)
        self.assertEqual([b["max_tokens"] for b in f.bodies], [10000, 16000])

    def test_length_once_then_success_with_more_tokens(self):
        stuck = {"message": {"role": "assistant", "content": ""}, "finish_reason": "length"}
        f, res = self.run_one([stuck, msg("all done")], Job(id="j", task="go", max_tokens=4000))
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.final, "all done")
        self.assertEqual([b["max_tokens"] for b in f.bodies], [4000, 8000])

    def test_empty_reply_is_a_failure(self):
        for m in (msg(""), msg(None), {"message": {"role": "assistant"}, "finish_reason": "stop"},
                  msg("   ")):
            _, res = self.run_one([m])
            self.assertEqual(res.status, "failed", m)
            self.assertIn("empty reply", res.error)

    def test_empty_final_after_tool_calls_is_ok(self):
        _, res = self.run_one([msg(None, [write("jobs/j/x.txt")]), msg("")])
        self.assertEqual(res.status, "ok")

    def test_dict_arguments_and_missing_ids(self):
        c = call("write_file", {"path": "jobs/j/x.txt", "content": "hi"}, id=None)
        f, res = self.run_one([msg(None, [c]), msg("done")])
        self.assertEqual(res.status, "ok")
        self.assertEqual(rd(os.path.join(f.root, "jobs/j/x.txt")), "hi")
        history = f.bodies[1]["messages"]
        assistant = [m for m in history if m["role"] == "assistant"][0]
        tool = [m for m in history if m["role"] == "tool"][0]
        arguments = assistant["tool_calls"][0]["function"]["arguments"]
        self.assertIsInstance(arguments, str)
        self.assertEqual(json.loads(arguments)["path"], "jobs/j/x.txt")
        self.assertTrue(tool["tool_call_id"])
        self.assertEqual(tool["tool_call_id"], assistant["tool_calls"][0]["id"])

    def test_malformed_tool_calls_do_not_crash(self):
        calls = [call("write_file", "[1, 2]", "a"), call("write_file", "null", "b"),
                 call("write_file", "{not json", "c"), call("write_file", '"just a string"', "d"),
                 {"id": "e"}, {"id": "f", "function": None}, "garbage", {"function": {"name": 5}},
                 call("read_file", json.dumps({"path": ["jobs/j/x.txt"]}), "g"),
                 call("read_file", json.dumps({"path": 7}), "h"),
                 call("write_file", json.dumps({"content": "no path"}), "i")]
        f, res = self.run_one([msg(None, calls), msg("fine")])
        self.assertEqual(res.status, "ok")
        tool_msgs = [m for m in f.bodies[1]["messages"] if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), len(calls))
        self.assertTrue(all("error" in m["content"] for m in tool_msgs))
        self.assertEqual(f.tool_calls, [])

    def test_tool_calls_that_are_not_a_list(self):
        _, res = self.run_one([msg("ok", "nonsense")])
        self.assertEqual(res.status, "ok")

    def test_paths_are_normalised_before_reaching_the_console(self):
        f, res = self.run_one([msg(None, [write("/jobs/j/./sub/../x.txt")]), msg("done")])
        self.assertEqual(res.status, "ok")
        self.assertEqual(f.tool_calls[0][1]["path"], "jobs/j/x.txt")

    def test_cannot_write_over_the_job_folder_itself(self):
        f, res = self.run_one([msg(None, [write("jobs/j"), write("jobs/j/")]), msg("done")])
        self.assertEqual(f.tool_calls, [])

    def test_reading_back_a_file_after_writing_it_is_allowed(self):
        rd = lambda i: call("read_file", json.dumps({"path": "jobs/j/x.txt"}), i)
        f, res = self.run_one([msg(None, [rd("1")]), msg(None, [rd("2")]),
                               msg(None, [write("jobs/j/x.txt", "new")]), msg(None, [rd("3")]),
                               msg("done")])
        self.assertEqual(res.status, "ok")
        names = [n for n, _ in f.tool_calls]
        self.assertEqual(names, ["read_file", "write_file", "read_file"])  # 2nd identical read blocked
        last = [m for m in f.bodies[-1]["messages"] if m["role"] == "tool"][-1]
        self.assertIn("new", last["content"])

    def test_huge_tool_output_is_truncated(self):
        f = self.fake()
        os.makedirs(os.path.join(f.root, "jobs/j"))
        with open(os.path.join(f.root, "jobs/j/big.txt"), "w") as fh:
            fh.write("z" * 100000)
        f.script = [msg(None, [call("read_file", json.dumps({"path": "jobs/j/big.txt"}))]), msg("done")]
        run_job(f.console(), Job(id="j", task="go"))
        tool = [m for m in f.bodies[1]["messages"] if m["role"] == "tool"][0]
        self.assertLess(len(tool["content"]), 13000)

    def test_chat_4xx_is_a_job_failure_not_a_console_error(self):
        _, res = self.run_one([(400, {"error": {"message": "prompt too long"}})])
        self.assertEqual(res.status, "failed")
        self.assertIn("prompt too long", res.error)

    def test_malformed_chat_replies_raise_console_error(self):
        for bad in ["not json at all", {"choices": []}, {"choices": [{}]}, {"foo": 1},
                    {"choices": [{"message": "x"}]}, (503, {"error": "loading model"})]:
            f = self.fake(i=len(self.fakes))
            f.script = [bad]
            with self.assertRaises(ConsoleError, msg=repr(bad)):
                run_job(f.console(), Job(id="j", task="go"))

    def test_dead_mid_job_raises_console_error(self):
        f = self.fake()
        f.script = ["DROP"]
        with self.assertRaises(ConsoleError):
            run_job(f.console(), Job(id="j", task="go"))

    def test_null_usage_and_tokens(self):
        m = msg("hi")
        f = self.fake()
        f.script = [lambda b: m]
        with mock.patch.object(Console, "chat", return_value={"choices": [m], "usage": None}):
            res = run_job(f.console(), Job(id="j", task="go"))
        self.assertEqual((res.status, res.tokens), ("ok", 0))

    def test_step_limit(self):
        loop = lambda i: msg(None, [call("get_info", "{}", str(i))])
        _, res = self.run_one([loop(i) for i in range(5)], Job(id="j", task="go", max_steps=3))
        self.assertEqual(res.status, "failed")
        self.assertIn("3 steps", res.error)


class FarmRobustnessTests(Base):
    def test_console_dying_mid_job_retries_elsewhere_and_cools_the_dead_one(self):
        dead, good = self.fake("script", 0), self.fake("good", 1)
        dead.script = ["DROP"] * 5
        farm = self.farm(cooldown=30)
        res = farm.run([Job(id="j0", task="go", expect=["out.txt"])])
        self.assertEqual(res[0].status, "ok")
        self.assertEqual(res[0].console, good.ip)
        self.assertEqual(res[0].attempts, 2)
        self.assertGreater(farm._cool[dead.ip], 0)

    def test_every_console_dies_gives_console_error_with_both_attempts(self):
        a, b = self.fake("script", 0), self.fake("script", 1)
        a.script = b.script = ["DROP"] * 5
        res = self.farm().run([Job(id="j0", task="go")])
        self.assertEqual(res[0].status, "console_error")
        self.assertEqual(res[0].attempts, 2)
        self.assertIn(a.ip, res[0].error)
        self.assertIn(b.ip, res[0].error)

    def test_non_json_reply_is_retried_elsewhere(self):
        bad, good = self.fake("script", 0), self.fake("good", 1)
        bad.script = ["<html>502 bad gateway</html>"] * 3
        res = self.farm().run([Job(id="j0", task="go", expect=["out.txt"])])
        self.assertEqual((res[0].status, res[0].console), ("ok", good.ip))

    def test_single_console_retries_on_itself_after_cooldown(self):
        f = self.fake("script")
        f.script = ["DROP", msg(None, [write("jobs/j0/out.txt")]), msg("done")]
        res = self.farm().run([Job(id="j0", task="go", expect=["out.txt"])])
        self.assertEqual((res[0].status, res[0].attempts), ("ok", 2))

    def test_stale_output_cannot_satisfy_expect(self):
        self.fake("lazy")
        stale = os.path.join(self.tmp, "out", "j0")
        os.makedirs(stale)
        wr(os.path.join(stale, "out.txt"), "stale")
        res = self.farm(max_attempts=1).run([Job(id="j0", task="go", expect=["out.txt"])])
        self.assertEqual(res[0].status, "failed")
        self.assertFalse(os.path.exists(os.path.join(stale, "out.txt")))

    def test_job_without_expect_that_writes_nothing_fails(self):
        self.fake("lazy")
        res = self.farm().run([Job(id="j0", task="go")])
        self.assertEqual(res[0].status, "failed")
        self.assertIn("no files", res[0].error)

    def test_exporter_blowing_up_fails_the_job_not_the_farm(self):
        self.fake("good", 0)
        self.fake("good", 1)
        farm = self.farm()
        farm.exporter.cleanup = mock.Mock(side_effect=EOFError("ftp went away"))
        res = farm.run([Job(id=f"j{i}", task="go", expect=["out.txt"]) for i in range(3)])
        self.assertEqual({r.status for r in res}, {"failed"})
        self.assertEqual(len(res), 3)

    def test_internal_bug_fails_the_job_and_frees_the_console(self):
        self.fake("good")
        farm = self.farm()
        with mock.patch("ps5farm.farm.run_job", side_effect=KeyError("boom")):
            res = farm.run([Job(id="j0", task="go", expect=["out.txt"]), Job(id="j1", task="go")])
        self.assertEqual([r.status for r in res], ["failed", "failed"])
        self.assertIn("internal error", res[0].error)
        self.assertEqual(len(rd(os.path.join(self.tmp, "out", "results.jsonl")).splitlines()), 2)

    def test_duplicate_ids_rejected_even_when_case_differs(self):
        self.fake("good")
        farm = self.farm()
        with self.assertRaises(ValueError):
            farm.run([Job(id="a", task="t"), Job(id="a", task="t")])
        with self.assertRaises(ValueError):
            farm.run([Job(id="a", task="t"), Job(id="A", task="t")])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "out", "results.jsonl")))

    def test_no_consoles_is_an_error_not_a_minute_long_wait(self):
        farm = Farm([], os.path.join(self.tmp, "out"), log=lambda s: None)
        with self.assertRaises(ValueError):
            farm.run([Job(id="a", task="t")])
        self.assertEqual(farm.run([]), [])

    def test_same_host_listed_twice_is_one_console(self):
        f = self.fake("good")
        farm = Farm([f.console(), f.console()], os.path.join(self.tmp, "out"),
                    exporter=LocalExporter([f]), cooldown=0.2, log=lambda s: None)
        self.assertEqual(len(farm.consoles), 1)
        res = farm.run([Job(id=f"j{i}", task="go", expect=["out.txt"]) for i in range(4)])
        self.assertEqual({r.status for r in res}, {"ok"})
        self.assertEqual(f.max_active, 1)

    def test_every_job_recorded_exactly_once_with_failures_and_retries(self):
        for i, m in enumerate(["lazy", "good", "script", "good"]):
            self.fake(m, i)
        self.fakes[2].script = ["DROP"] * 50
        jobs = [Job(id=f"j{i}", task="go", expect=["out.txt"]) for i in range(12)]
        res = self.farm().run(jobs)
        self.assertEqual(sorted(r.job_id for r in res), sorted(j.id for j in jobs))
        lines = rd(os.path.join(self.tmp, "out", "results.jsonl")).splitlines()
        recs = [json.loads(l) for l in lines]
        self.assertEqual(sorted(r["job_id"] for r in recs), sorted(j.id for j in jobs))
        self.assertTrue(all(f.max_active == 1 for f in self.fakes if f.mode != "script"))

    def test_idle_console_not_blocked_behind_a_retry_waiting_for_another_console(self):
        # j0 fails on the lazy console; its retry must not stall the queue behind it.
        self.fake("lazy", 0)
        self.fake("good", 1)
        res = self.farm().run([Job(id=f"j{i}", task="go", expect=["out.txt"]) for i in range(4)])
        self.assertEqual({r.status for r in res}, {"ok"})

    def test_status_reports_ftp_and_is_read_only(self):
        f = self.fake("good")
        rows = self.farm().status()
        self.assertEqual(rows[0]["healthy"], True)
        self.assertEqual(rows[0]["tools"], ["read_file", "write_file", "get_info"])
        self.assertEqual(f.bodies, [])


class ClientTests(Base):
    def test_proxy_environment_is_ignored(self):
        f = self.fake("good")
        with mock.patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9"}):
            self.assertTrue(f.console().health())
            self.assertEqual(f.console().tools()[0]["function"]["name"], "read_file")

    def test_unreachable_console(self):
        c = Console("127.0.0.1", 1)
        self.assertFalse(c.health())
        with self.assertRaises(ConsoleError):
            c.tools()


class CliTests(Base):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(list(argv))
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def local_fake(self, mode):
        f = FakeConsole(os.path.join(self.tmp, "cc"), "127.0.0.1", 0, mode)  # same host as FakeFtp
        self.fakes.append(f)
        return f

    def write(self, name, text):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as fh:
            fh.write(text)
        return p

    def test_missing_files(self):
        hosts = self.write("h.txt", "127.0.0.2\n")
        rc, _, err = self.run_cli("run", "--hosts", os.path.join(self.tmp, "nope"), "--jobs", hosts)
        self.assertEqual(rc, 2)
        self.assertIn("cannot read hosts file", err)
        rc, _, err = self.run_cli("run", "--hosts", hosts, "--jobs", os.path.join(self.tmp, "nope"))
        self.assertEqual(rc, 2)
        self.assertIn("cannot read jobs file", err)
        self.assertNotIn("Traceback", err)

    def test_bad_hosts(self):
        for text in ["", "# only a comment\n", "http://1.2.3.4\n", "1.2.3.4:8081\n", "a b\n"]:
            rc, _, err = self.run_cli("status", "--hosts", self.write("h.txt", text))
            self.assertEqual(rc, 2, text)
            self.assertIn("ps5farm: error", err)

    def test_hosts_with_comments_blanks_and_duplicates(self):
        p = self.write("h.txt", "\n# c\n10.0.0.1  # one\n\n10.0.0.1\n10.0.0.2\n")
        self.assertEqual(cli.read_hosts(p), ["10.0.0.1", "10.0.0.2"])

    def test_bad_jobs_report_line_numbers(self):
        hosts = self.write("h.txt", "127.0.0.2\n")
        good = '{"id": "a", "task": "t"}'
        for text, needle in [(good + "\n{oops\n", ":2:"), ("\n\n", "no jobs"), ("[1]\n", ":1:"),
                             (good + "\n\n" + good + "\n", ":3: duplicate"),
                             ('{"id": "a", "task": "t"}\n{"id": "A", "task": "t"}\n', "duplicate"),
                             ('{"id": "../x", "task": "t"}\n', "bad job id"),
                             ('{"id": "a", "task": "t", "max_steps": "x"}\n', ":1:")]:
            rc, _, err = self.run_cli("run", "--hosts", hosts, "--jobs", self.write("j.jsonl", text),
                                      "--out", os.path.join(self.tmp, "o"))
            self.assertEqual(rc, 2, text)
            self.assertIn(needle, err)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "o")), "nothing runs on bad input")

    def test_blank_lines_and_bom_are_fine(self):
        p = self.write("j.jsonl", "﻿" + '{"id": "a", "task": "t"}\n\n   \n{"id": "b", "task": "t"}\n')
        self.assertEqual([j.id for j in cli.read_jobs(p)], ["a", "b"])

    def test_max_attempts_validated(self):
        rc, _, err = self.run_cli("run", "--hosts", self.write("h", "1.2.3.4\n"), "--jobs", "x",
                                  "--max-attempts", "0")
        self.assertEqual(rc, 2)

    def test_status_exit_codes_and_output(self):
        f = self.local_fake("good")
        hosts = self.write("h.txt", f.ip + "\n")
        rc, out, _ = self.run_cli("status", "--hosts", hosts, "--port", str(f.port), "--ftp-port", "1")
        row = json.loads(out)
        self.assertTrue(row["healthy"])
        self.assertFalse(row["ftp"])  # nothing on FTP port 1
        self.assertEqual(rc, 1)
        ftp = FakeFtp(self.tmp)
        self.stoppers.append(ftp)
        rc, out, _ = self.run_cli("status", "--hosts", hosts, "--port", str(f.port), "--ftp-port", str(ftp.port))
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["ftp"])

    def test_run_exit_code_and_summary(self):
        f = self.local_fake("lazy")
        hosts = self.write("h.txt", f.ip + "\n")
        jobs = self.write("j.jsonl", '{"id": "a", "task": "t", "expect": ["x.txt"]}\n')
        ftp = FakeFtp(f.root)
        self.stoppers.append(ftp)
        rc, out, _ = self.run_cli("run", "--hosts", hosts, "--jobs", jobs, "--port", str(f.port),
                                  "--ftp-port", str(ftp.port), "--out", os.path.join(self.tmp, "o"),
                                  "--max-attempts", "1")
        self.assertEqual(rc, 1)
        self.assertIn("0/1 jobs ok", out)
        rec = json.loads(rd(os.path.join(self.tmp, "o", "results.jsonl")))
        self.assertEqual(rec["status"], "failed")


if __name__ == "__main__":
    unittest.main()

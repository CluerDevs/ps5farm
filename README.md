# ps5farm

Run file-writing jobs on a fleet of jailbroken PS5 consoles that are serving an LLM with
[PS5LM](https://github.com/cobanov/PS5LM) (llama.cpp on the PS5).

An orchestrator (a script, or a bigger model such as Claude) hands `ps5farm` a list of small,
self-contained jobs. Each console takes one job at a time, the model uses the console's own
sandboxed file tools to write its results, and `ps5farm` pulls the files back to your PC and
checks that the files you expected are there.

- Python 3.9+, **standard library only**
- Scheduler: one job per console, work spreads across the fleet
- Health checks, cooldown for dead consoles, retry on a *different* console
- Jobs are confined to `jobs/<id>/` on the console (writes outside it are refused)
- Export over ftpsrv (port 2121), results recorded in `results.jsonl`
- Tested against a fake console (`python -m unittest discover -s tests -t .`) and on a real PS5

## What the console can and can't do

PS5LM's server exposes six tools: `read_file`, `file_glob_search`, `grep_search`, `write_file`,
`edit_file`, `get_info`. There is **no shell**, so a console can read, search and write files but
cannot run code. Good jobs: writing or editing source files, drafting documents, summarising or
restructuring text, generating data files, code review notes. Verify anything that matters
on your side (for example, run the script it wrote).

The tools are sandboxed to `/data/PS5LM/scratch` on the console; `ps5farm` keeps each job inside
`jobs/<id>/` below that.

## Quick start

1. Set up consoles with PS5LM and a model, with **ftpsrv** running (see [docs/SETUP.md](docs/SETUP.md)).
2. List your consoles, one per line:

   ```
   # hosts.txt
   192.168.1.20
   192.168.1.21
   ```

3. Check the fleet:

   ```
   python -m ps5farm status --hosts hosts.txt
   ```

4. Write jobs as JSON lines (see `jobs.example.jsonl`):

   ```json
   {"id": "primes", "task": "Write primes.py that prints all primes below 100, and notes.md with one sentence on how it works.", "expect": ["primes.py", "notes.md"]}
   ```

5. Run them:

   ```
   python -m ps5farm run --hosts hosts.txt --jobs jobs.jsonl --out out
   ```

   Unknown job fields (for example a typo like `expects`) are rejected. Exit code: 0 all ok, 1 some jobs failed, 2 bad input files, 130 interrupted. `status` also checks anonymous FTP login (`"ftp": true`).
   Files land in `out/<id>/`, and one record per job is appended to `out/results.jsonl`:
   status, console, attempts, steps, tokens, seconds, the model's final message and the exported files.

### Job fields

| field | meaning |
|---|---|
| `id` | unique name (1-64 of letters, digits, `.` `_` `-`, starting with a letter or digit; compared case-insensitively); becomes the folder `jobs/<id>/` |
| `task` | what to do. Keep it short and self-contained |
| `expect` | files (relative to the job folder) that must exist and be non-empty, or the attempt counts as failed. A job that writes no files at all also fails |
| `max_steps` | tool-call rounds allowed (default 8) |
| `max_tokens` | per-reply cap (default 4000; reasoning models spend tokens thinking). A reply cut off with no answer is retried once with double the cap (up to 16000) |
| `system` | extra instructions appended to the worker prompt |

### From Python

```python
from ps5farm import Console, Farm, Job

farm = Farm([Console("192.168.1.20"), Console("192.168.1.21")], out_dir="out")
for r in farm.run([Job(id="a", task="Write hello.txt containing a greeting.", expect=["hello.txt"])]):
    print(r.job_id, r.status, r.files)
```

## How it works

```
orchestrator -> Farm (queue) -> console N: POST /v1/chat/completions  (model asks for tools)
                                          POST /tools                 (Farm runs each tool call)
                                repeat until the model answers -> FTP export -> check `expect`
```

The console's server does not execute tools by itself; the client runs the loop. `ps5farm` does
that, refuses tool paths outside the job folder, and blocks identical repeated read calls.

## Limits and safety

- **Speed.** A 27B model at 2-bit runs around 20 tokens/s; reading long prompts is slower. Prefer
  many small jobs over one huge one, and a 4-9B model for tool-heavy work.
- **Quality.** Heavily quantised models make mistakes. Use `expect` and check outputs.
- **No authentication.** The PS5LM API and `/tools` endpoint, and ftpsrv, have no login. Anyone
  who can reach the consoles can read and write their scratch folder. Keep them on an isolated
  network, never exposed to the internet.
- `FtpExporter.cleanup()` deletes `jobs/<id>/` on the console before each attempt, and `out/<id>/` on this PC is emptied before each export. Re-running with the same `--out` appends to `results.jsonl`.
- If you interrupt a run (Ctrl-C), a console may keep generating for a few minutes; its next job queues behind that.
- Do not run files a model wrote without reading them first.

## Legal

This repository contains only a client. It does not include any exploit, payload or console
software. Jailbreaking a console may violate its terms of service and can get a console banned
from online services; do it only on hardware you own and understand the risks. See
[docs/SETUP.md](docs/SETUP.md) for the third-party projects involved and their own terms.

## License

MIT. See [LICENSE](LICENSE).

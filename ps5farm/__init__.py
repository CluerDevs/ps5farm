"""ps5farm: run file-writing jobs on a fleet of PS5LM consoles."""
from .client import Console, ConsoleError
from .job import Job, JobResult, run_job
from .farm import Farm

__all__ = ["Console", "ConsoleError", "Job", "JobResult", "run_job", "Farm"]
__version__ = "0.1.0"

"""Per-run log capture for code that reports progress with print().

Several users' preparation runs execute at the same time in one worker
process. The pipeline, research agent and model router all print() their
progress; this routes each thread's output to the sink of the run it belongs
to (a user's Activity log), and everything else to the real console.

    sink = RunLog(run_id)
    with logsink.bound(sink):
        pipeline.run(rows)          # its thread pools inherit the sink
"""

from __future__ import annotations

import io
import sys
import threading

_local = threading.local()
_installed = False
_install_lock = threading.Lock()


class _Router(io.TextIOBase):
    def __init__(self, fallback):
        self._fallback = fallback

    def write(self, text):
        sink = getattr(_local, "sink", None)
        if sink is not None:
            try:
                sink.write(text)
            except Exception:
                pass
            return len(text)
        try:
            return self._fallback.write(text)
        except UnicodeEncodeError:
            return self._fallback.write(text.encode("ascii", "replace").decode("ascii"))

    def flush(self):
        try:
            self._fallback.flush()
        except Exception:
            pass

    @property
    def encoding(self):
        return getattr(self._fallback, "encoding", "utf-8")

    def isatty(self):
        return False


def install() -> None:
    """Route sys.stdout through the per-thread sink. Idempotent."""
    global _installed
    with _install_lock:
        if _installed or isinstance(sys.stdout, _Router):
            _installed = True
            return
        sys.stdout = _Router(sys.stdout)
        _installed = True


def current():
    return getattr(_local, "sink", None)


def bind(sink) -> None:
    _local.sink = sink


class bound:
    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        install()
        self._previous = current()
        bind(self.sink)
        return self.sink

    def __exit__(self, *exc):
        bind(self._previous)
        return False


def wrap(fn):
    """fn, run in another thread with the caller's sink."""
    sink = current()

    def runner(*args, **kwargs):
        bind(sink)
        return fn(*args, **kwargs)
    return runner


class MemoryLog:
    """A sink keeping the last `limit` characters (tests, CLI)."""

    def __init__(self, limit: int = 65536, echo=None):
        self.limit, self.echo = limit, echo
        self._lock = threading.Lock()
        self.text = ""

    def write(self, text: str) -> None:
        with self._lock:
            self.text = (self.text + text)[-self.limit:]
        if self.echo is not None:
            try:
                self.echo.write(text)
            except Exception:
                pass

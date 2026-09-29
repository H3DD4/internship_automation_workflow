"""A company must never leave a run without landing in a bucket.

Regression tests for a live run that prepared 6 companies and reported
"ready: 5, failed: 0, skipped: 0" — the sixth vanished. Its name held a
zero-width space, `print()` on a cp1252 console raised UnicodeEncodeError,
the stage's own `except` handler raised again printing the same name, and
the escaped exception was swallowed by the thread pool's future.
"""

import io
import sys

import pytest

from pipeline import Pipeline
from utils import make_console_encoding_safe

# Zero-width space — the character that actually did it, straight out of a
# scraped company name.
UNPRINTABLE_NAME = "Log'​in Line"


class FakeClient:
    """Stands in for CompatibleAIClient — no test may reach the network."""


def _pipeline(user_id):
    import drafting
    return Pipeline(user_id, FakeClient(), drafting.load_config(user_id), "model",
                    research_workers=1, writer_workers=1)


def test_a_stage_that_raises_is_still_counted(with_profile):
    """The executor swallows an escaped exception, so the count is the only
    thing that would ever reveal the loss."""
    pipeline = _pipeline(with_profile)

    def exploding_stage(_row):
        raise RuntimeError("stage blew up before it could record anything")

    pipeline._guard(exploding_stage, "research stage for <x@y.z>", ("C", "x@y.z", None, None))
    assert pipeline.terminal_count == 1
    assert pipeline.results["failed"] == 1


def test_a_stage_whose_error_handler_also_raises_is_still_counted(with_profile):
    """The exact shape of the live failure: the handler dies the same way the
    stage did, so nothing downstream of it ever runs."""
    pipeline = _pipeline(with_profile)

    def doubly_exploding_stage(_row):
        try:
            raise UnicodeEncodeError("charmap", UNPRINTABLE_NAME, 4, 5, "unmappable")
        except UnicodeEncodeError:
            # What pipeline's own `except` branch does first: print the name.
            raise UnicodeEncodeError("charmap", UNPRINTABLE_NAME, 4, 5, "unmappable")

    pipeline._guard(doubly_exploding_stage, "research stage for <x@y.z>", ("C", "x@y.z", None, None))
    assert pipeline.terminal_count == 1
    assert pipeline.results["failed"] == 1


def test_the_guard_never_double_counts_a_stage_that_recorded_itself(with_profile):
    """A stage that recorded an outcome and then died must not be counted
    twice — otherwise a run reports more companies than it was given."""
    pipeline = _pipeline(with_profile)

    def records_then_dies(_row):
        pipeline._record("failed")
        raise RuntimeError("died after its own error handling")

    pipeline._guard(records_then_dies, "research stage for <x@y.z>", ("C", "x@y.z", None, None))
    assert pipeline.terminal_count == 1
    assert pipeline.results == {"ready": 0, "failed": 1, "skipped": 0}


def test_research_handing_off_to_the_writer_is_not_counted_as_a_loss(with_profile):
    """Research succeeds by submitting to the writer pool WITHOUT recording;
    the writer records later. Treating "returned without recording" as a lost
    company made a clean 6-company run report "ready: 6, failed: 1"."""
    pipeline = _pipeline(with_profile)

    def hands_off(_row):
        return  # exactly what _research_task does on success

    pipeline._guard(hands_off, "research stage for <x@y.z>", ("C", "x@y.z", None, None))
    assert pipeline.terminal_count == 0
    assert pipeline.results == {"ready": 0, "failed": 0, "skipped": 0}


def test_the_guard_is_not_confused_by_other_threads_recording(with_profile):
    """The flag is per-thread, so concurrent outcomes from the writer pool
    can't make a research stage look like it did (or didn't) record."""
    import threading
    pipeline = _pipeline(with_profile)

    def dies_while_another_thread_records(_row):
        other = threading.Thread(target=lambda: pipeline._record("ready"))
        other.start()
        other.join()
        raise RuntimeError("boom")

    pipeline._guard(dies_while_another_thread_records, "research stage for <x@y.z>",
                    ("C", "x@y.z", None, None))
    assert pipeline.results == {"ready": 1, "failed": 1, "skipped": 0}
    assert pipeline.terminal_count == 2


def test_the_guard_reports_the_failure_without_printing_the_bad_name(with_profile, capsys):
    """The label is built from the email, and the message is forced to ASCII —
    the net cannot be brought down by the thing it is there to catch."""
    pipeline = _pipeline(with_profile)

    def exploding_stage(_row):
        raise RuntimeError(UNPRINTABLE_NAME)

    pipeline._guard(exploding_stage, "research stage for <x@y.z>", ("C", "x@y.z", None, None))
    out = capsys.readouterr().out
    assert "[error]" in out and "x@y.z" in out
    assert "​" not in out


def test_console_is_made_safe_for_unmappable_characters(monkeypatch):
    """The root cause: a legacy console encoding with strict error handling."""
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)

    with pytest.raises(UnicodeEncodeError):
        stream.write(UNPRINTABLE_NAME)

    make_console_encoding_safe()
    stream.write(UNPRINTABLE_NAME)  # must not raise
    assert sys.stdout.errors == "replace"


def test_make_console_encoding_safe_tolerates_a_stream_it_cannot_reconfigure(monkeypatch):
    """stdout is not always a TextIOWrapper — under some runners it is a
    plain object with no reconfigure(). Startup must not die over that."""
    class Bare:
        pass

    monkeypatch.setattr(sys, "stdout", Bare())
    monkeypatch.setattr(sys, "stderr", Bare())
    make_console_encoding_safe()  # must not raise


# ---------------------------------------------------------------------------
# Wording-spec encoding
# ---------------------------------------------------------------------------

def test_specializations_json_is_read_as_utf8_everywhere():
    """specializations.json is UTF-8 and full of em-dashes. Reading it with
    open()/read_text() and no encoding= uses the platform default — cp1252 on
    Windows — which turns every "—" into "a<euro>" mojibake and mails it to
    the company. It is invisible on a UTF-8 box, so pin it here.
    """
    import re
    from pathlib import Path

    root = Path(__file__).parent.parent
    offenders = []
    for source in [*root.glob("*.py"), *(root / "dashboard").glob("*.py"),
                   *(root / "agents").glob("*.py"), *(root / "tests").glob("*.py")]:
        text = source.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "specializations.json" not in line and "spec_path" not in line:
                continue
            if not re.search(r"\bopen\(|read_text\(", line):
                continue
            if "encoding=" not in line:
                offenders.append(f"{source.name}: {line.strip()}")
    assert not offenders, "reads the wording spec without encoding='utf-8':\n" + "\n".join(offenders)


def test_the_spec_round_trips_through_utf8_but_not_cp1252():
    """Guards the assumption above: the file really does hold bytes that
    cp1252 mangles rather than rejects — which is why it fails silently."""
    from pathlib import Path

    raw = (Path(__file__).parent.parent / "specializations.json").read_bytes()
    assert b"\xe2\x80\x94" in raw, "expected UTF-8 em-dashes in the wording spec"
    assert "\u2014" in raw.decode("utf-8")
    # cp1252 doesn't raise here — it quietly produces the wrong characters.
    assert "\u2014" not in raw.decode("cp1252")

"""Single-writer locking across processes, and graceful behaviour while another process owns the database."""

import json
import os
import threading
from pathlib import Path

import duckdb
import pytest

from app.database import access
from app.database.connection import ensure_database, is_lock_conflict
from app.database.errors import DatabaseLockedError, DatabaseUnavailableError, WriterBusyError
from app.database.locking import WriterLock, info_path, lock_path, writer_status


def test_normal_acquisition_and_release(db_path):
    assert writer_status(db_path).active is False
    with WriterLock(db_path, "unit test"):
        status = writer_status(db_path)
        assert status.active is True
        assert (status.operation, status.pid) == ("unit test", os.getpid())
        assert status.started_at is not None
        info = json.loads(info_path(db_path).read_text(encoding="utf-8"))
        assert info["operation"] == "unit test"
    assert writer_status(db_path).active is False
    assert not info_path(db_path).exists()


def test_release_is_idempotent_and_lock_is_reusable(db_path):
    lock = WriterLock(db_path, "t")
    lock.acquire()
    lock.release()
    lock.release()
    with WriterLock(db_path, "again"):
        pass


def test_second_writer_in_the_same_process_is_blocked(db_path):
    with WriterLock(db_path, "first"), pytest.raises(WriterBusyError, match="first"):
        WriterLock(db_path, "second", timeout=0.2).acquire()
    with WriterLock(db_path, "second", timeout=0.2):  # free again once the first one is gone
        pass


def test_second_writer_in_another_process_is_blocked(db_path, hold):
    with hold("lock-only") as holder:
        with pytest.raises(WriterBusyError, match="data refresh"):
            WriterLock(db_path, "second", timeout=0.3).acquire()
        status = writer_status(db_path)
        assert status.active and status.operation == "data refresh" and status.pid == holder.pid


def test_lock_is_recovered_after_the_holder_process_is_killed(db_path, hold):
    with hold("lock-only") as holder:
        assert writer_status(db_path).active
        holder.kill()
        holder.wait()
    assert info_path(db_path).exists()  # the crashed process could not clean up its info file ...
    assert writer_status(db_path).active is False  # ... but the OS released the lock, so nobody is "active"
    with WriterLock(db_path, "after crash", timeout=0.5):  # a new writer is not blocked by the stale leftovers
        assert json.loads(info_path(db_path).read_text(encoding="utf-8"))["operation"] == "after crash"


def test_lock_is_released_when_the_work_inside_fails(db_path):
    with pytest.raises(RuntimeError, match="boom"), WriterLock(db_path, "failing"):
        raise RuntimeError("boom")
    assert writer_status(db_path).active is False
    with WriterLock(db_path, "next", timeout=0.2):
        pass


def test_writer_context_releases_lock_and_rolls_back_on_failure(db_path, con):
    with pytest.raises(RuntimeError), access.writer(db_path, "failing") as w:
        w.record_query("never committed")
        raise RuntimeError
    assert con.execute("SELECT count(*) FROM query_history").fetchone()[0] == 0
    assert writer_status(db_path).active is False


def test_two_access_writers_cannot_overlap(db_path):
    with (
        access.writer(db_path, "outer"),
        pytest.raises(WriterBusyError),
        access.writer(db_path, "inner", lock_timeout=0.2),
    ):
        pytest.fail("the second writer must not get in")


def test_writer_waits_briefly_for_a_probe_instead_of_failing(db_path):
    """A status probe (or a millisecond-long UI write) holds the lock only momentarily."""
    done = threading.Event()

    def probe():
        for _ in range(20):
            writer_status(db_path)
        done.set()

    thread = threading.Thread(target=probe)
    thread.start()
    for _ in range(20):
        with WriterLock(db_path, "interleaved", timeout=2.0):
            pass
    thread.join()
    assert done.is_set()


def test_writer_status_never_raises_for_a_missing_directory(tmp_path):
    assert writer_status(tmp_path / "does" / "not" / "exist.duckdb").active is False


def test_lock_file_lives_next_to_the_database(db_path):
    assert lock_path(db_path).parent == db_path.parent
    assert lock_path(db_path).name == db_path.name + ".writer.lock"


# --- the database file itself while another process has it open -------------------------------------


def test_reader_reports_a_locked_database_instead_of_returning_stale_data(db_path, hold):
    with hold("writer"):
        with pytest.raises(DatabaseLockedError), access.reader(db_path, wait=0.1):
            pytest.fail("must not read while a refresh owns the database")
        assert isinstance(DatabaseLockedError("x"), DatabaseUnavailableError)  # callers can catch one base type


def test_killed_refresh_leaves_database_consistent_and_unlocked(db_path, hold, con):
    con.close()
    with hold("writer") as holder:  # has an UNCOMMITTED row in its transaction
        holder.kill()
        holder.wait()
    with access.reader(db_path) as r:  # readable again; the half-done work did not leak in
        assert r.table_counts()["query_history"] == 0
    with access.writer(db_path, "after crash") as w:
        w.record_query("works")


def test_reader_waits_out_a_short_lock(db_path, hold):
    with hold("duckdb-only") as holder:
        threading.Timer(0.4, holder.stdin.close).start()  # the other process lets go shortly
        with access.reader(db_path, wait=5.0) as r:
            assert r.schema_version() == 2


def test_readers_and_writers_never_create_the_database_file(tmp_path):
    missing = tmp_path / "missing.duckdb"
    with pytest.raises(DatabaseUnavailableError, match="does not exist"), access.reader(missing):
        pass
    with pytest.raises(DatabaseUnavailableError, match="does not exist"), access.writer(missing, "x"):
        pass
    assert not missing.exists()


def test_ensure_database_on_a_current_database_needs_no_writer_lock(db_path, hold):
    with hold("lock-only"):
        assert ensure_database(db_path) == 2  # fast path: reads only


def test_ensure_database_needing_changes_waits_for_the_writer_lock(tmp_path, hold):
    uninitialised = tmp_path / "fresh.duckdb"
    duckdb.connect(str(uninitialised)).close()  # a database file without a schema
    with hold("lock-only", path=uninitialised), pytest.raises(WriterBusyError):
        ensure_database(uninitialised, lock_timeout=0.2)
    assert ensure_database(uninitialised) == 2


def test_paths_are_plain_pathlib(db_path):
    assert isinstance(db_path, Path)


# Real messages DuckDB produced (Linux locally, Windows on the CI runner). Windows never says "lock".
LINUX_LOCK_MESSAGE = (
    'IO Error: Could not set lock on file "/tmp/x/research.duckdb": Conflicting lock is held in '
    "/usr/bin/python3.13 (PID 8010). See also https://duckdb.org/docs/connect/concurrency"
)
WINDOWS_LOCK_MESSAGE = (
    'IO Error: Cannot open file "C:\\Users\\runneradmin\\AppData\\Local\\Temp\\research.duckdb": '
    "The process cannot access the file because it is being used by another process.\n\n\n"
    "File is already open in \nC:\\hostedtoolcache\\windows\\Python\\3.14.7\\x64\\python.exe (PID 3424)"
)


def test_lock_conflicts_are_recognised_on_every_platform():
    assert is_lock_conflict(LINUX_LOCK_MESSAGE)
    assert is_lock_conflict(WINDOWS_LOCK_MESSAGE)


def test_other_io_errors_are_not_mistaken_for_a_lock_conflict():
    for message in (
        'IO Error: Cannot open file "/x/research.duckdb": No such file or directory',
        'IO Error: Cannot open file "C:\\x\\research.duckdb": Access is denied.',
        "IO Error: Could not read enough bytes from file",
        "Permission denied",
        "block device error",  # contains the letters "lock" but is not a lock conflict
    ):
        assert not is_lock_conflict(message), message

import sqlite3
import stat
import textwrap

from app.services import settings_store


def _fake_rclone(tmp_path, exit_code=0):
    log = tmp_path / "rclone-calls.txt"
    script = tmp_path / "rclone"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            echo "$@" >> {log}
            echo "fake rclone ok"
            exit {exit_code}
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script, log


def test_offsite_backs_up_db_and_syncs(ctx, pc, game, tmp_path):
    script, log = _fake_rclone(tmp_path)
    ctx.settings.rclone_binary = str(script)
    pc.push(game["id"], {"saves/a": b"x"})
    with ctx.Session() as db:
        settings_store.set_offsite(db, True, "gdrive:savepoint")
        db.commit()

    run = ctx.jobs.run_offsite()
    assert run.status == "ok", run.log
    calls = log.read_text().splitlines()
    assert calls[0].startswith(f"sync {ctx.settings.blob_dir} gdrive:savepoint/blobs")
    assert calls[1].startswith(f"copyto {ctx.settings.backup_dir / 'savepoint.db'} gdrive:savepoint/savepoint.db")

    # The DB copy is a valid, consistent SQLite database.
    con = sqlite3.connect(ctx.settings.backup_dir / "savepoint.db")
    try:
        assert con.execute("select count(*) from versions").fetchone()[0] == 1
    finally:
        con.close()


def test_offsite_disabled_is_skipped_unless_forced(ctx, tmp_path):
    script, log = _fake_rclone(tmp_path)
    ctx.settings.rclone_binary = str(script)
    assert ctx.jobs.run_offsite() is None
    run = ctx.jobs.run_offsite(force=True)
    assert run.status == "ok"
    assert log.exists()


def test_offsite_failure_is_recorded(ctx, tmp_path):
    script, _ = _fake_rclone(tmp_path, exit_code=3)
    ctx.settings.rclone_binary = str(script)
    with ctx.Session() as db:
        settings_store.set_offsite(db, True, "gdrive:savepoint")
        db.commit()
    run = ctx.jobs.run_offsite()
    assert run.status == "error"
    assert "exited with 3" in run.summary
    assert ctx.jobs.last_run("offsite").status == "error"


def test_offsite_missing_binary(ctx):
    ctx.settings.rclone_binary = "/nonexistent/rclone"
    run = ctx.jobs.run_offsite(force=True)
    assert run.status == "error"
    assert "not found" in run.summary

"""How long a post waits for TikTok Studio to take the video, and how often it looks (tiktok/upload_wait.py)."""

import pytest

from ytclip.posting.tiktok import upload_wait
from ytclip.posting.tiktok.upload_wait import MB, UploadWatch


def test_the_limit_grows_with_the_file():
    assert upload_wait.limit_for(50 * MB) == 900  # 15 minutes, as before
    assert upload_wait.limit_for(287_600_000) == pytest.approx(900 + 187.6 * MB / 250_000)  # TikTok app's largest
    assert upload_wait.limit_for(4_000_000_000) > 4 * 3600  # a computer's 4 GB upload, at 2 Mbit/s


def test_reads_back_off_while_nothing_moves_and_speed_up_when_it_does():
    w = UploadWatch(300 * MB, 0.0)
    delays = []
    for t in range(6):
        w.seen(float(t), None)
        delays.append(round(w.delay, 2))
    assert delays == [1.6, 2.56, 4.1, 6.55, 10.0, 10.0]
    w.seen(7.0, 12.0)
    assert w.delay == 1.0 and w.progress == 12.0 and w.changed_at == 7.0


def test_only_an_upload_that_showed_progress_can_stall():
    quiet = UploadWatch(300 * MB, 0.0)
    quiet.seen(1000.0, None)
    assert not quiet.stalled(1000.0)  # the page shows no progress: only the size's limit applies
    w = UploadWatch(300 * MB, 0.0)
    w.seen(10.0, 40.0)
    w.seen(200.0, 40.0)
    assert not w.stalled(200.0) and w.stalled(251.0)
    w.seen(252.0, 41.0)
    assert not w.stalled(400.0) and w.stalled(493.0)
    w.seen(494.0, None, done=True)
    assert not w.stalled(10_000.0)
    assert w.expired(upload_wait.limit_for(300 * MB) + 1) and not w.expired(60.0)


def test_a_percentage_that_goes_away_or_reaches_100_isnt_a_stall():
    w = UploadWatch(300 * MB, 0.0)
    w.seen(10.0, 97.0)
    w.seen(20.0, None)  # the page moved on (TikTok checking the video): only the overall limit applies
    assert not w.stalled(5000.0)
    w.seen(5000.0, 97.0)  # shown again: the stall clock starts afresh
    assert not w.stalled(5200.0) and w.stalled(5241.0)
    full = UploadWatch(300 * MB, 0.0)
    full.seen(10.0, 100.0)
    assert not full.stalled(5000.0)


def test_a_big_files_post_is_only_clicked_once_the_upload_is_done():
    small = UploadWatch(50 * MB, 0.0)
    small.seen(1.0, None, post_enabled=True)
    assert small.ready()  # as before: Post enabled is enough
    big = UploadWatch(300 * MB, 0.0)
    big.seen(1.0, 80.0, post_enabled=True)
    assert not big.ready()
    big.seen(2.0, 80.0, post_enabled=False)
    big.seen(3.0, 80.0, post_enabled=True)
    big.seen(4.0, 80.0, post_enabled=True)
    assert not big.ready()  # it went dark in between: counting starts again
    big.seen(5.0, 80.0, post_enabled=True)
    assert big.ready()  # enabled for three reads in a row
    assert big.delay == upload_wait.FIRST_DELAY  # and they come a second apart, however long it sat still
    done = UploadWatch(300 * MB, 0.0)
    done.seen(1.0, 60.0, done=True, post_enabled=True)
    assert done.ready()
    full = UploadWatch(300 * MB, 0.0)
    full.seen(1.0, 100.0, post_enabled=True)
    assert full.ready()
    assert not UploadWatch(300 * MB, 0.0).ready()


def test_progress_is_reported_once_per_ten_percent():
    w = UploadWatch(300 * MB, 0.0)
    got = []
    for t, pct in enumerate((3, 9, 12, 18, 27, 27, 55, 99, 100)):
        w.seen(float(t), float(pct))
        got.append(w.report())
    assert [g for g in got if g is not None] == [10, 20, 50, 90, 100]

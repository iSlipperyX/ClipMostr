"""AI hashtags: the quick text-only call, learning from the account's best posts, and the Ready page button."""

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ytclip.posting import ai, jobs  # noqa: E402
from ytclip.posting.accounts import Accounts  # noqa: E402
from ytclip.posting.config import Settings  # noqa: E402
from ytclip.posting.store import Store, TikTokVideo  # noqa: E402
from posting_helpers import clipmonster_folder  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))


@pytest.fixture
def claude(monkeypatch):
    """A stand-in for ClipMonster's AI (ytclip.ai.ask_json): records each request, answers with `reply`."""
    from ytclip import ai as main_ai

    calls = []
    reply = {"hashtags": ["Jynxzi", "#r6 siege", "fyp", "gaming", "rainbowsix", "clutch"]}

    def ask_json(s, task, system, content, schema, **kw):
        if not main_ai.has_key(s):
            raise main_ai.AIError(main_ai.missing_key(s))
        calls.append({"api_key": main_ai.key_for(s), "task": task, "system": system,
                      "messages": [{"content": content}], **kw})
        return main_ai.Answer(reply, "claude-test", "claude")

    monkeypatch.setattr(main_ai, "ask_json", ask_json)
    return calls


def main_key(key="sk-ant-x"):
    """ClipMonster's Settings → AI key (posting uses ClipMonster's AI)."""
    from ytclip.settings import Settings as Main

    Main(ai_api_key=key).save()


def account():
    a = Accounts.load().add("")
    a.niche, a.default_hashtags = "Rainbow Six streamer clips", "#fyp #gaming"
    return a


def test_hashtags_fit_the_clip_and_skip_spam_and_the_accounts_own_tags(tmp_path, claude):
    store = Store(tmp_path / "lib.json")
    item = store.add_paths([clipmonster_folder(tmp_path)])[0]
    a = account()
    main_key()
    got = jobs.ai_hashtags(store, item.id, Settings(ai_hashtag_count=4), a)
    assert got.hashtags == "#Jynxzi #r6siege #rainbowsix #clutch"  # no #fyp / #gaming (already added), max 4
    prompt = claude[-1]["messages"][0]["content"]
    assert "From the video: Big Talk" in prompt and "Channel / creator: Pod People" in prompt
    assert "The account posts: Rainbow Six streamer clips" in prompt
    assert "don't repeat): #fyp #gaming" in prompt and "exactly 4 hashtags" in prompt
    assert claude[0]["api_key"] == "sk-ant-x" and claude[0]["task"] == "polish"


def test_clips_that_already_have_hashtags_keep_them_when_imported(tmp_path, claude):
    store = Store(tmp_path / "lib.json")
    item = store.add_paths([clipmonster_folder(tmp_path)])[0]
    item.hashtags = "#mine"
    main_key()
    jobs.ai_hashtags(store, item.id, Settings(), account(), only_if_empty=True)
    assert item.hashtags == "#mine" and claude == []


def test_no_key_says_where_to_add_one_and_clipmonsters_key_is_used(tmp_path, claude):
    store = Store(tmp_path / "lib.json")
    item = store.add_paths([clipmonster_folder(tmp_path)])[0]
    assert not ai.has_key(Settings())
    with pytest.raises(Exception, match="Settings → AI"):
        jobs.ai_hashtags(store, item.id, Settings(), account())
    main_key(" sk-ant-from-clipmonster ")
    assert ai.has_key(Settings())
    jobs.ai_hashtags(store, item.id, Settings(), account())
    assert claude[0]["api_key"] == "sk-ant-from-clipmonster"


def test_best_hashtags_come_from_posts_with_the_most_views(tmp_path):
    store = Store(tmp_path / "lib.json")
    items = store.add_paths([clipmonster_folder(tmp_path), clipmonster_folder(tmp_path, name="Other [def]")])
    tagged = [("#r6 #clutch", 9000), ("#r6 #fail", 3000), ("#cooking", 100), ("#R6", 6000)]
    videos = []
    for n, (it, (tags, views)) in enumerate(zip(items, tagged)):
        it.account, it.status, it.hashtags, it.tiktok_id, it.posted_at = "acc", "posted", tags, str(n), 1000.0 + n
        videos.append(TikTokVideo(id=str(n), views=views))
    store.update_tiktok_videos("acc", videos)
    # r6 was used three times (9000, 3000, 6000 views): its median is 6000; the newest spelling is kept
    assert jobs.proven_tags(store, "acc") == [("clutch", 9000), ("R6", 6000), ("fail", 3000), ("cooking", 100)]
    assert jobs.proven_tags(store, "acc", min_posts=5) == []  # too few posts to tell yet

"""Your PC's local AI, for ClipMonster for Mac: Settings → AI → "Your PC's local AI" (provider "pc" in ai.py).

The Mac reads the video and makes the clips itself; only the questions go to the PC (remote/studio.py
AITasks), to the model it runs, so the Mac needn't hold a model of several gigabytes or warm up doing it. A
question is started (POST /api/ai/tasks) and then waited on in short long polls (GET /api/ai/tasks/<id>?wait=20
&rev=N, N the last change this Mac heard of, so none is missed), so a slow answer never outlasts a connection,
and the PC's progress shows in the Studio as it comes. When the Studio stops (Cancel), the question is stopped on
the PC too.

PCAI answers ai.use_remote()'s questions from what the PC last said about itself (its status), so asking
whether it's ready never waits on the network.
"""

from __future__ import annotations

import time
from dataclasses import fields
from typing import Callable
from urllib.parse import quote

from .. import ai
from .client import ApiError, Offline

WAIT = 20  # s the PC holds each long poll
POLL_TIMEOUT = 45.0  # s a long poll may take, the PC's wait included
START_TIMEOUT = 60.0  # s to send the question (a whole transcript) and hear it started
GONE_FOR = 90.0  # s the PC may be out of reach while answering before the question is given up


class PCAI:
    """ai.py's "pc" provider, over the link `link` (pc.PCLink) keeps."""

    def __init__(self, link, now: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.link = link
        self.now, self.sleep = now, sleep

    # --- what ai.py asks first ------------------------------------------------------------------------------

    def _lends(self) -> dict:
        return (self.link.lends() or {}).get("ai") or {}

    def ready(self) -> bool:
        return bool(self._lends().get("ready"))

    def why(self) -> str:
        """What's missing, in words, while the PC's AI can't be used."""
        if not self.link.linked:
            return "No PC is linked. Link yours under Your PC in the sidebar, or pick another AI in Settings → AI."
        name = self.link.name or "Your PC"
        if not self.link.online:
            return f"{name} can't be reached right now, so its local AI can't answer."
        lend = self.link.lends()
        if not lend:
            return f"{name} runs an older ClipMonster that can't lend its AI: update it."
        return str((lend.get("ai") or {}).get("why") or "") or f"{name}'s local AI isn't ready."

    def model(self) -> str:
        m = str(self._lends().get("model") or "")
        return f"{m} on your PC" if m else "your PC's local AI"

    def check(self) -> str:
        """Settings → AI → Test: whether the PC's AI answers, freshly asked."""
        client = self._client()
        try:
            st = client.get("/api/ai")
        except ApiError as exc:
            raise ai.AIError(self._said(exc)) from exc
        if not st.get("ready"):
            raise ai.AIError(str(st.get("why") or "Your PC's local AI isn't ready."))
        where = "" if st.get("downloaded", True) else " (it downloads its model the first time it's asked)"
        return f"Your PC's local AI works: {st.get('model') or 'its model'}{where}."

    # --- asking ---------------------------------------------------------------------------------------------

    def ask(self, task: str, system: str, content, schema: dict, effort: str, max_tokens: int, log=None,
            check=None, progress=None) -> tuple[dict, str]:
        body = {"kind": "ask", "task": task, "system": system, "content": content, "schema": schema,
                "effort": effort, "max_tokens": int(max_tokens)}
        result = self._run(body, log, check, progress)
        data = result.get("data")
        if not isinstance(data, dict):
            raise ai.AIError("Your PC's answer couldn't be read. Try again.")
        return data, self._named(result.get("model"))

    def find(self, title, duration, sents, score, count, min_len, max_len, instructions, log=None, check=None):
        """The PC's local clip finder on this Mac's transcript: (clips, model)."""
        from ..analyze import Clip

        body = {"kind": "find", "title": str(title or ""), "duration": float(duration or 0),
                "sentences": [[round(float(c.start), 3), round(float(c.end), 3), str(c.text)] for c in sents],
                "score": [round(float(x), 4) for x in (score if score is not None else [])],
                "count": int(count), "min_len": float(min_len), "max_len": float(max_len),
                "instructions": str(instructions or "")}
        result = self._run(body, log, check, None)
        known = {f.name for f in fields(Clip)}
        clips = []
        for d in result.get("clips") or []:
            if isinstance(d, dict):
                try:
                    clips.append(Clip(**{k: v for k, v in d.items() if k in known}))
                except TypeError:
                    continue
        if not clips:
            raise ai.AIError("Your PC's local AI found no clips in this video.")
        return clips, self._named(result.get("model"))

    # --- the long polls -----------------------------------------------------------------------------------

    def _client(self):
        client = self.link.client()
        if client is None:
            raise ai.AIError(self.why())
        return client

    @staticmethod
    def _said(exc: ApiError) -> str:
        if isinstance(exc, Offline):
            return f"Your PC can't be reached: {exc.message}"
        return f"Your PC's local AI: {exc.message}"

    @staticmethod
    def _named(model) -> str:
        m = str(model or "")
        return f"{m} on your PC" if m else "your PC's local AI"

    def _run(self, body: dict, log, check, progress) -> dict:
        client = self._client()
        try:
            started = client.post("/api/ai/tasks", body, timeout=START_TIMEOUT)
        except ApiError as exc:
            raise ai.AIError(self._said(exc)) from exc
        task_id = str(started.get("id") or "")
        if not task_id:
            raise ai.AIError("Your PC didn't start on the question. Try again.")
        path = f"/api/ai/tasks/{quote(task_id, safe='')}"
        finished = False
        lost_since = None
        said: list = [None]
        rev = started.get("rev")

        def tell(st: dict) -> None:
            """The PC's progress on the question, to the Studio, each time it's something new."""
            now_said = (st.get("stage"), st.get("frac"))
            if progress is None or now_said == said[0] or not st.get("stage") or \
                    st.get("state") not in ("waiting", "working"):
                return
            said[0] = now_said
            frac = st.get("frac")
            progress(str(st["stage"]), float(frac) if isinstance(frac, (int, float)) else None)

        tell(started)
        try:
            while True:
                if check is not None:
                    check()  # the Studio's Cancel (raises), and the PC's question with it
                try:
                    st = client.request("GET", path, query={"wait": WAIT, "rev": rev}, timeout=POLL_TIMEOUT)
                    lost_since = None
                    rev = st.get("rev", rev)
                except Offline as exc:
                    now = self.now()
                    lost_since = lost_since if lost_since is not None else now
                    if now - lost_since > GONE_FOR:
                        raise ai.AIError(f"Lost your PC while its local AI was answering ({exc.message})") from exc
                    self.sleep(3.0)
                    continue
                except ApiError as exc:
                    if exc.status == 404:  # automation restarted on the PC: the question went with it
                        finished = True
                        raise ai.AIError("Your PC stopped answering (its automation restarted). Try again.") from exc
                    raise ai.AIError(self._said(exc)) from exc
                state = st.get("state")
                tell(st)
                if state == "done":
                    finished = True
                    return st.get("result") or {}
                if state == "failed":
                    finished = True
                    kind = (st.get("result") or {}).get("kind")
                    error = str(st.get("error") or "Your PC's local AI couldn't answer.")
                    raise (ai.AICutOff if kind == "cut_off" else ai.AIRefused if kind == "refused" else ai.AIError)(
                        error)
                if state == "stopped":
                    finished = True
                    raise ai.AIError("Your PC stopped the question (it may be busy or restarting). Try again.")
        finally:
            if not finished:
                try:
                    client.post(f"{path}/cancel", timeout=8.0)
                except Exception:  # noqa: BLE001 - it gives up by itself when the Mac stops asking
                    pass

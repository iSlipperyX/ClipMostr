"""Who's talking, and where: faces found through a clip, tied into tracks, and the speaker picked from them.

The clip is sampled a few times a second at low resolution. Faces are found with OpenCV's YuNet, a small
neural face detector (its 230 KB model downloads once), or with OpenCV's built-in Haar cascade when that
download isn't possible. Detections in neighbouring samples are joined into tracks by position and size.

The active speaker is the face whose mouth moves while someone is speaking: the mouth area's frame-to-
frame change (a stand-in for the mouth opening and closing, which needs no landmark model) is weighed
against Whisper's word timings, so a face that's only nodding along doesn't win. Size breaks ties: the
person nearest the camera is usually the one the video is about. The choice has hysteresis, so the
camera doesn't flick between two people who talk over each other; when it does move to someone else,
that's marked as a jump, and the camera cuts to them instead of panning (camera.py).

`track` returns the speaker's eye line, as fractions of the frame, at each sample time.
"""

from __future__ import annotations

import subprocess
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from ..ffmpeg import NO_WINDOW, ffmpeg_exe, tracked
from ..settings import models_dir

SAMPLE_FPS = 6.0
SAMPLE_WIDTH = 480
YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
             "face_detection_yunet_2023mar.onnx")
YUNET_FILE = "face_detection_yunet_2023mar.onnx"
MATCH_DISTANCE = 0.12  # a face this close (fraction of frame width) in the next sample is the same person
SWITCH_MARGIN = 1.35  # another face must score this much better...
SWITCH_HOLD = 1.0  # ...for this many seconds before the camera moves to them
EYE_LINE = 0.40  # the eyes sit this far down a face box

Progress = Callable[[str, "float | None"], None]


@dataclass
class Face:
    x: float  # box, as fractions of the frame
    y: float
    w: float
    h: float
    score: float = 1.0
    mouth: tuple[float, float, float, float] | None = None  # mouth box (fractions), when landmarks say where
    motion: float = 0.0  # how much the mouth area changed since the last sample
    track: int = -1

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def eye_y(self) -> float:
        return self.y + self.h * EYE_LINE

    @property
    def area(self) -> float:
        return self.w * self.h


@dataclass
class Target:
    """Where the camera should look at time `t` (seconds into the clip): the speaker's eye line."""

    t: float
    x: float | None  # None: nobody on screen
    y: float | None
    size: float = 0.0  # face height, fraction of the frame
    track: int = -1
    jump: bool = False  # the speaker changed: cut, don't pan


@dataclass
class Tracking:
    targets: list[Target] = field(default_factory=list)
    detector: str = ""
    coverage: float = 0.0  # share of samples with a face

    @property
    def found(self) -> bool:
        return self.coverage >= 0.3


# ---------------------------------------------------------------- detectors


class Detector:
    name = ""

    def detect(self, frame: np.ndarray) -> list[Face]:  # pragma: no cover - interface
        raise NotImplementedError


class YuNet(Detector):
    name = "YuNet"

    def __init__(self, model: Path):
        import cv2

        self.cv2 = cv2
        self.net = cv2.FaceDetectorYN.create(str(model), "", (SAMPLE_WIDTH, SAMPLE_WIDTH), 0.7, 0.3, 50)
        self.size = (0, 0)

    def detect(self, frame: np.ndarray) -> list[Face]:
        h, w = frame.shape[:2]
        if (w, h) != self.size:
            self.net.setInputSize((w, h))
            self.size = (w, h)
        _, found = self.net.detect(frame)
        faces = []
        for row in (found if found is not None else []):
            x, y, bw, bh = (float(v) for v in row[:4])
            # landmarks: right eye, left eye, nose, right mouth corner, left mouth corner
            mx1, my1, mx2, my2 = float(row[10]), float(row[11]), float(row[12]), float(row[13])
            mw = max(abs(mx2 - mx1), bw * 0.25)
            mouth = ((min(mx1, mx2) - mw * 0.2) / w, (min(my1, my2) - bh * 0.12) / h, mw * 1.4 / w, bh * 0.3 / h)
            faces.append(Face(max(0.0, x / w), max(0.0, y / h), bw / w, bh / h, float(row[14]), mouth))
        return faces


class Haar(Detector):
    name = "Haar cascade"

    def __init__(self):
        import cv2

        self.cv2 = cv2
        path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        self.cascade = cv2.CascadeClassifier(str(path))
        if self.cascade.empty():
            raise RuntimeError("OpenCV's face cascade is missing")

    def detect(self, frame: np.ndarray) -> list[Face]:
        h, w = frame.shape[:2]
        gray = self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2GRAY)
        gray = self.cv2.equalizeHist(gray)
        found = self.cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6,
                                              minSize=(max(24, w // 20), max(24, w // 20)))
        return [Face(x / w, y / h, bw / w, bh / h, 0.8) for x, y, bw, bh in (found if len(found) else [])]


def yunet_model(opener=urllib.request.urlopen) -> Path | None:
    """YuNet's model file, downloaded once. None when it can't be had (offline): Haar is used then."""
    path = models_dir() / "vision" / YUNET_FILE
    if path.is_file() and path.stat().st_size > 100_000:
        return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(YUNET_URL, headers={"User-Agent": "ClipMonster"})
        with opener(req, timeout=30) as r:
            data = r.read()
        if len(data) < 100_000:
            return None
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        return path
    except Exception:
        return None


def detector() -> Detector:
    """The best detector this PC can run. Raises RuntimeError when OpenCV isn't installed."""
    try:
        import cv2  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("Following the speaker needs OpenCV (pip install opencv-python-headless).") from exc
    model = yunet_model()
    if model is not None:
        try:
            return YuNet(model)
        except Exception:
            pass
    return Haar()


# ---------------------------------------------------------------- sampling the clip


def sample_frames(src: Path, start: float, length: float, fps: float = SAMPLE_FPS, width: int = SAMPLE_WIDTH,
                  src_size: tuple[int, int] = (1920, 1080)):
    """Yield (t, BGR frame) a few times a second from [start, start + length), small."""
    sw, sh = src_size
    height = max(2, int(round(width * sh / max(1, sw) / 2)) * 2)
    cmd = [ffmpeg_exe(), "-v", "error", "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{length:.3f}", "-an", "-sn",
           "-vf", f"fps={fps},scale={width}:{height}", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
    size = width * height * 3
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW) as proc, \
            tracked(proc):
        assert proc.stdout is not None
        k = 0
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield k / fps, np.frombuffer(buf, np.uint8).reshape(height, width, 3)
            k += 1


def mouth_patch(frame: np.ndarray, f: Face) -> np.ndarray | None:
    import cv2

    h, w = frame.shape[:2]
    if f.mouth is not None:
        x, y, mw, mh = f.mouth
    else:  # no landmarks: the lower middle of the face box
        x, y, mw, mh = f.x + f.w * 0.25, f.y + f.h * 0.62, f.w * 0.5, f.h * 0.3
    x0, y0 = int(max(0, x * w)), int(max(0, y * h))
    x1, y1 = int(min(w, (x + mw) * w)), int(min(h, (y + mh) * h))
    if x1 - x0 < 4 or y1 - y0 < 3:
        return None
    gray = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    return cv2.resize(gray, (24, 12), interpolation=cv2.INTER_AREA).astype(np.float32)


# ---------------------------------------------------------------- tracks and the speaker


def link(samples: list[list[Face]]) -> int:
    """Give every face a track number, joining each to the nearest face of the sample before."""
    next_id = 0
    prev: list[Face] = []
    for faces in samples:
        free = list(prev)
        for f in sorted(faces, key=lambda f: -f.area):
            best = min(free, key=lambda p: abs(p.cx - f.cx) + abs(p.eye_y - f.eye_y), default=None)
            if (best is not None and abs(best.cx - f.cx) + abs(best.eye_y - f.eye_y) < MATCH_DISTANCE * 1.5
                    and 0.5 < f.h / max(best.h, 1e-6) < 2.0):
                f.track = best.track
                free.remove(best)
            else:
                f.track = next_id
                next_id += 1
        if faces:
            prev = faces
    return next_id


def talking_at(words, t: float, slack: float = 0.15) -> bool:
    return any(w.start - slack <= t <= w.end + slack for w in words)


def choose_speaker(times: list[float], samples: list[list[Face]], words) -> list[Target]:
    """The speaker at each sample, with hysteresis; a change of speaker is a jump."""
    tracks = link(samples)
    # How much each track's mouth moves while someone is talking, smoothed over about a second.
    talk = [talking_at(words, t) for t in times]
    window = max(1, int(SAMPLE_FPS))
    motion: dict[int, list[float]] = {i: [0.0] * len(times) for i in range(tracks)}
    for k, faces in enumerate(samples):
        for f in faces:
            motion[f.track][k] = f.motion if talk[k] else 0.0
    smooth = {i: np.convolve(np.array(v), np.ones(2 * window + 1) / (2 * window + 1), mode="same")
              for i, v in motion.items()}
    out: list[Target] = []
    current, challenger, since = -1, -1, 0.0
    for k, (t, faces) in enumerate(zip(times, samples)):
        if not faces:
            out.append(Target(t, None, None))
            continue
        biggest = max(f.area for f in faces)

        def score(f: Face) -> float:
            return 0.35 * (f.area / biggest) + smooth[f.track][k] * 4.0 + 0.05

        by_track = {f.track: f for f in faces}
        best = max(faces, key=score)
        jump = False
        if current not in by_track:  # the speaker left the picture (or this is the first sample)
            jump = current != -1 and out and out[-1].x is not None and abs(out[-1].x - best.cx) > 0.25
            current, challenger = best.track, -1
        elif best.track != current and score(best) > score(by_track[current]) * SWITCH_MARGIN:
            if challenger != best.track:
                challenger, since = best.track, t
            elif t - since >= SWITCH_HOLD:
                jump = abs(by_track[current].cx - best.cx) > 0.12
                current, challenger = best.track, -1
        else:
            challenger = -1
        f = by_track[current]
        out.append(Target(t, f.cx, f.eye_y, f.h, f.track, jump))
    return out


def track(src: Path, start: float, length: float, words, src_size: tuple[int, int],
          progress: Progress | None = None, det: Detector | None = None) -> Tracking:
    """Find the speaker through [start, start + length). `words` are the clip's words in source time."""
    det = det or detector()
    clip_words = [w for w in words if w.end > start and w.start < start + length]
    rel = [type(w)(w.start - start, w.end - start, w.text) for w in clip_words]
    times: list[float] = []
    samples: list[list[Face]] = []
    last_patch: dict[int, np.ndarray] = {}
    total = max(1, int(length * SAMPLE_FPS))
    for t, frame in sample_frames(src, start, length, src_size=src_size):
        faces = det.detect(frame)
        faces = [f for f in faces if f.h > 0.04]  # specks in the background aren't people
        patches = [mouth_patch(frame, f) for f in faces]
        times.append(t)
        samples.append(faces)
        # Mouth motion against the nearest face of the sample before (tracks aren't joined yet).
        prev = samples[-2] if len(samples) > 1 else []
        new_patches: dict[int, np.ndarray] = {}
        for i, (f, patch) in enumerate(zip(faces, patches)):
            if patch is None:
                continue
            new_patches[i] = patch
            near = min(range(len(prev)), key=lambda j: abs(prev[j].cx - f.cx) + abs(prev[j].eye_y - f.eye_y),
                       default=None)
            if near is not None and near in last_patch:
                diff = np.abs(patch - last_patch[near])
                f.motion = float(diff.mean() / (patch.std() + 8.0))
        last_patch = new_patches
        if progress and len(times) % 6 == 0:
            progress("Finding the speaker", min(1.0, len(times) / total))
    targets = choose_speaker(times, samples, rel)
    seen = sum(1 for t in targets if t.x is not None)
    return Tracking(targets, det.name, seen / len(targets) if targets else 0.0)

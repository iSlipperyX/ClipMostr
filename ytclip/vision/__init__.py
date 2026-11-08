"""Auto-cinematography: finding the person talking (face_tracker.py), moving a virtual camera after them
with punch-ins and slow push-ins (camera.py), cutaways to your own B-roll (broll_selector.py), and
rendering the moved, cut picture (reframe.py)."""


def available() -> bool:
    """OpenCV is installed (pip install opencv-python-headless): needed for following the speaker."""
    import importlib.util

    return importlib.util.find_spec("cv2") is not None

"""Webcam head-pose tracker built on MediaPipe Face Landmarker.

Runs in its own thread and reports smoothed ``(timestamp, yaw, pitch)``
samples through a callback. Needs the ``gaze`` extra:
``pip install curslink[gaze]``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

from curslink.gaze.headpose import AngleSmoother, rotation_to_euler

log = logging.getLogger(__name__)

MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
    "face_landmarker/float16/1/face_landmarker.task"
)

SampleCallback = Callable[[float, float, float], None]


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".cache"
    path = root / "curslink"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_model(path: Path | None = None) -> Path:
    path = path or cache_dir() / "face_landmarker.task"
    if not path.exists():
        log.info("downloading face landmark model to %s", path)
        tmp = path.with_suffix(".part")
        urllib.request.urlretrieve(MODEL_URL, tmp)
        tmp.replace(path)
    return path


class PoseEstimator:
    """Wraps MediaPipe: RGB frame in, raw ``(yaw, pitch, roll)`` out."""

    def __init__(self, model_path: Path | None = None):
        from mediapipe.tasks.python import BaseOptions, vision

        options = vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(ensure_model(model_path))),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
            output_facial_transformation_matrixes=True,
        )
        self._landmarker = vision.FaceLandmarker.create_from_options(options)
        self._last_ts_ms = -1

    def estimate(self, rgb, ts_ms: int) -> tuple[float, float, float] | None:
        import mediapipe as mp

        ts_ms = max(ts_ms, self._last_ts_ms + 1)  # MediaPipe needs increasing timestamps
        self._last_ts_ms = ts_ms
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        result = self._landmarker.detect_for_video(image, ts_ms)
        if not result.facial_transformation_matrixes:
            return None
        m = result.facial_transformation_matrixes[0]
        return rotation_to_euler([list(row[:3]) for row in m[:3]])

    def close(self) -> None:
        self._landmarker.close()


class HeadPoseTracker:
    def __init__(
        self,
        on_sample: SampleCallback,
        camera: int = 0,
        model_path: Path | None = None,
        fps: float = 30.0,
        preview: bool = False,
    ):
        self.on_sample = on_sample
        self.camera = camera
        self.model_path = model_path
        self.fps = fps
        self.preview = preview
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.face_visible = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="curslink-gaze", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        try:
            self._loop()
        except Exception:  # never take the host down because of the camera
            log.exception("gaze tracker stopped")

    def _loop(self) -> None:
        import cv2

        estimator = PoseEstimator(self.model_path)
        cap = cv2.VideoCapture(self.camera)
        if not cap.isOpened():
            raise RuntimeError(f"could not open camera {self.camera}")
        smoother = AngleSmoother()
        frame_interval = 1.0 / self.fps
        start = time.monotonic()
        try:
            while not self._stop.is_set():
                loop_start = time.monotonic()
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.05)
                    continue
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                now = time.monotonic()
                pose = estimator.estimate(rgb, int((now - start) * 1000))
                self.face_visible = pose is not None
                if pose is not None:
                    syaw, spitch = smoother(now, pose[0], pose[1])
                    self.on_sample(now, syaw, spitch)
                    if self.preview:
                        cv2.putText(
                            frame,
                            f"yaw {syaw:+.1f}  pitch {spitch:+.1f}",
                            (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.8,
                            (0, 255, 0),
                            2,
                        )
                if self.preview:
                    cv2.imshow("Curslink camera (q to close)", frame)
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        self.preview = False
                        cv2.destroyAllWindows()
                spare = frame_interval - (time.monotonic() - loop_start)
                if spare > 0:
                    time.sleep(spare)
        finally:
            cap.release()
            estimator.close()
            if self.preview:
                cv2.destroyAllWindows()

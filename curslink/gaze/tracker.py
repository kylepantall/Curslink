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


def default_model_path() -> Path:
    return cache_dir() / "face_landmarker.task"


def ensure_model(path: Path | None = None, progress: Callable[[int, int], None] | None = None) -> Path:
    """The face model, downloaded the first time. ``progress(done, total)``
    is called as bytes arrive (``total`` is 0 when the server doesn't say)."""
    path = path or default_model_path()
    if not path.exists():
        log.info("downloading face landmark model to %s", path)
        started = time.monotonic()
        tmp = path.with_suffix(".part")
        try:
            with urllib.request.urlopen(MODEL_URL, timeout=30) as resp, open(tmp, "wb") as out:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                if progress:
                    progress(0, total)
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
            if total and done != total:
                raise OSError(f"the download stopped at {done} of {total} bytes")
        except Exception:
            log.exception("couldn't download the face model from %s", MODEL_URL)
            raise
        tmp.replace(path)
        log.info("downloaded the face model (%d bytes) in %.1fs", path.stat().st_size, time.monotonic() - started)
    else:
        log.debug("face model: %s (%d bytes)", path, path.stat().st_size)
    return path


class PoseEstimator:
    """Wraps MediaPipe: RGB frame in, raw ``(yaw, pitch, roll)`` out."""

    def __init__(self, model_path: Path | None = None):
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision

        started = time.monotonic()
        model = ensure_model(model_path)
        options = vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model)),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
            output_facial_transformation_matrixes=True,
        )
        self._landmarker = vision.FaceLandmarker.create_from_options(options)
        self._last_ts_ms = -1
        log.info("face landmarker ready in %.2fs (mediapipe %s)", time.monotonic() - started,
                 getattr(mp, "__version__", "?"))

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
        self.error: str | None = None  # why the thread stopped, if it failed

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="curslink-gaze", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is None:
            return
        started = time.monotonic()
        thread.join(timeout=timeout)
        if thread.is_alive():
            # Still inside OpenCV or MediaPipe. Whoever opens the camera next
            # may fight it for the device; this line says so in the log.
            log.warning("camera %s thread didn't stop within %.1fs; it is still holding the camera",
                        self.camera, timeout)
        else:
            log.info("camera %s stopped in %.2fs", self.camera, time.monotonic() - started)

    def _run(self) -> None:
        log.info("camera %s: starting the tracker thread", self.camera)
        try:
            self._loop()
        except Exception as exc:  # never take the host down because of the camera
            self.error = str(exc) or type(exc).__name__
            log.exception("gaze tracker on camera %s stopped with an error", self.camera)
        else:
            log.info("camera %s: tracker thread finished", self.camera)

    def _loop(self) -> None:
        import cv2

        log.info("opencv %s", cv2.__version__)
        estimator = PoseEstimator(self.model_path)
        opened = time.monotonic()
        log.info("camera %s: opening", self.camera)
        cap = cv2.VideoCapture(self.camera)
        for attempt in range(3):
            # Just freed by the linking service, or macOS's camera prompt just
            # answered: it can take a moment to open.
            if cap.isOpened() or self._stop.is_set():
                break
            cap.release()
            log.info("camera %s: not free yet, trying again (%d)", self.camera, attempt + 1)
            time.sleep(1.0)
            cap = cv2.VideoCapture(self.camera)
        if not cap.isOpened():
            estimator.close()
            log.error("camera %s: couldn't open it (in use by another app, unplugged, or no camera permission)",
                      self.camera)
            raise RuntimeError(f"could not open camera {self.camera}")
        try:
            backend = cap.getBackendName()
        except cv2.error:
            backend = "?"
        log.info("camera %s: open in %.2fs via %s, %dx%d at %.0f fps", self.camera, time.monotonic() - opened,
                 backend, int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                 cap.get(cv2.CAP_PROP_FPS) or 0)
        smoother = AngleSmoother()
        frame_interval = 1.0 / self.fps
        start = time.monotonic()
        stats = _Stats(self.camera)
        bad_reads = 0
        try:
            while not self._stop.is_set():
                loop_start = time.monotonic()
                ok, frame = cap.read()
                if not ok:
                    bad_reads += 1
                    if bad_reads in (1, 10) or bad_reads % 100 == 0:
                        log.warning("camera %s: no frame (%d in a row)", self.camera, bad_reads)
                    time.sleep(0.05)
                    continue
                if bad_reads:
                    log.info("camera %s: frames are back after %d misses", self.camera, bad_reads)
                    bad_reads = 0
                if stats.frames == 0:
                    log.info("camera %s: first frame %s, %.2fs after opening", self.camera,
                             "x".join(str(n) for n in frame.shape), time.monotonic() - opened)
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                now = time.monotonic()
                pose = estimator.estimate(rgb, int((now - start) * 1000))
                stats.frame(time.monotonic() - now, pose is not None)
                if pose is not None and not self.face_visible:
                    log.debug("camera %s: face found after %d frames", self.camera, stats.frames)
                elif pose is None and self.face_visible:
                    log.debug("camera %s: face lost", self.camera)
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
            stats.report(final=True)
            log.debug("camera %s: releasing", self.camera)
            cap.release()
            estimator.close()
            if self.preview:
                cv2.destroyAllWindows()


class _Stats:
    """Logs how the camera is doing every so often: frame rate, how often a
    face is found, and how long MediaPipe takes."""

    EVERY_S = 30.0

    def __init__(self, camera: int):
        self.camera = camera
        self.frames = 0
        self._since = time.monotonic()
        self._n = self._faces = 0
        self._infer = 0.0
        self._worst = 0.0

    def frame(self, infer_s: float, face: bool) -> None:
        self.frames += 1
        self._n += 1
        self._faces += face
        self._infer += infer_s
        self._worst = max(self._worst, infer_s)
        if time.monotonic() - self._since >= self.EVERY_S:
            self.report()

    def report(self, final: bool = False) -> None:
        span = time.monotonic() - self._since
        if self._n:
            log.info("camera %s%s: %.1f fps, face in %d%% of frames, inference %.1f ms avg / %.1f ms worst",
                     self.camera, " (final)" if final else "", self._n / span if span else 0,
                     100 * self._faces // self._n, 1000 * self._infer / self._n, 1000 * self._worst)
        elif final:
            log.info("camera %s: stopped before any frames arrived", self.camera)
        self._since, self._n, self._faces, self._infer, self._worst = time.monotonic(), 0, 0, 0.0, 0.0

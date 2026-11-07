import asyncio

from curslink.app import HostCamera


class FakeTracker:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class FakeRouter:
    gaze_paused = False


def make():
    loop = asyncio.get_running_loop()
    started, released = [], []

    def start():
        started.append(FakeTracker())
        return started[-1]

    first = FakeTracker()
    return HostCamera(first, start, lambda: released.append(1), loop), first, started, released


def test_pause_releases_the_camera_and_resume_reopens_it():
    async def go():
        camera, first, started, released = make()
        router = FakeRouter()
        router.gaze_paused = True
        await camera.pause()
        assert first.stopped and camera.tracker is None and released == [1]
        await camera.pause()  # already off: nothing more
        assert released == [1]
        router.gaze_paused = False
        await camera.resume(router)
        assert len(started) == 1 and camera.tracker is started[0]
        await camera.resume(router)  # already on
        assert len(started) == 1

    asyncio.run(go())


def test_resume_does_nothing_while_still_paused_or_calibrating():
    async def go():
        camera, _first, started, _ = make()
        router = FakeRouter()
        router.gaze_paused = True
        await camera.pause()
        await camera.resume(router)  # a late resume after another pause
        assert started == []
        router.gaze_paused = False
        await camera.pause(for_good=True)
        await camera.resume(router)
        assert started == [] and camera.tracker is None

    asyncio.run(go())

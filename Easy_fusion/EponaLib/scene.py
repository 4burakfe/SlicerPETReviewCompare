"""Scene state helpers: is the scene loading / closing / importing, and running code once it has settled."""

import logging

import qt
import slicer

# Scene / layout work is never done inside a scene notification. It runs from the event loop once the
# scene has been idle for a short while, so the layout manager has finished rebuilding its views.
SCENE_POLL_MS = 100
SCENE_SETTLE_MS = 300
SCENE_MAX_WAIT_MS = 10 * 60 * 1000


def sceneIsBusy():
    scene = slicer.mrmlScene
    return scene.IsImporting() or scene.IsClosing() or scene.IsBatchProcessing()


def runWhenSceneSettled(callback, settleMs=SCENE_SETTLE_MS):
    """
    Run callback from the event loop once the scene has been idle for settleMs.
    Never runs while the scene is still loading / closing: if it stays busy too long, the callback is dropped.
    """
    state = {"elapsed": 0, "idleSince": None}

    def poll():
        if sceneIsBusy():
            state["idleSince"] = None
            if state["elapsed"] >= SCENE_MAX_WAIT_MS:
                logging.warning("EasyFusion: scene stayed busy, skipped a deferred update")
                return
        else:
            if state["idleSince"] is None:
                state["idleSince"] = state["elapsed"]
            if state["elapsed"] - state["idleSince"] >= settleMs:
                try:
                    callback()
                except Exception:
                    logging.exception("EasyFusion: deferred scene update failed")
                return
        state["elapsed"] += SCENE_POLL_MS
        qt.QTimer.singleShot(SCENE_POLL_MS, poll)

    qt.QTimer.singleShot(0, poll)


def deferUntilSceneIdle(callback):
    """Leave the current scene/layout notification first, then run callback when the scene is idle."""
    runWhenSceneSettled(callback, settleMs=0)

"""
Placing ROIs on the MIP (3D view) of Epona and Epona Compare.

A point on the MIP stands for a whole ray through the volume. The ROI goes to the hottest voxel on that ray (the
voxel the MIP shows at that pixel), so it lands on the lesion in 3D and shows up in the slice views too. After
placing, every slice view jumps to the ROI's slice (and pans to it when it would be out of sight).

Two ways to place on the MIP:
  - the ROI key (Insert by default) with the mouse over the MIP: the modules call placeAtMouse() from their key
    handler,
  - "Place ROI", then a click on the MIP: the shared MipClickPlacer (on slicer.modules) watches left clicks in the
    3D views while a module is shown and hands them to the module whose ROI is being placed.

One shared MipClickPlacer instance; modules call acquire(owner, handler) on enter and release(owner) on exit.
"""

import logging
import math

import numpy as np
import slicer
import vtk

OBSERVER_PRIORITY = 10.0        # before Slicer's interactor style, so Slicer does not place a point of its own
STEPS_PER_VOXEL = 2             # samples per voxel along the ray (no voxel on the ray is skipped)
_INSTANCE_ATTRIBUTE = "_eponaMipClickPlacer"


# --- pure helpers (no scene needed) --------------------------------------------------------------------------

def clipLineToBox(start, end, low, high):
    """
    Parameters (t0, t1) of the part of the infinite line start + t * (end - start) inside the box low..high
    (per axis), or None when the line misses the box.
    """
    start = np.asarray(start, dtype=float)
    direction = np.asarray(end, dtype=float) - start
    t0, t1 = -math.inf, math.inf
    for axis in range(len(start)):
        if abs(direction[axis]) < 1e-12:
            if start[axis] < low[axis] or start[axis] > high[axis]:
                return None
            continue
        a = (low[axis] - start[axis]) / direction[axis]
        b = (high[axis] - start[axis]) / direction[axis]
        t0, t1 = max(t0, min(a, b)), min(t1, max(a, b))
    if t0 > t1 or not math.isfinite(t0) or not math.isfinite(t1):
        return None
    return t0, t1


def brightestVoxelOnLine(array, ijkStart, ijkEnd, stepsPerVoxel=STEPS_PER_VOXEL):
    """
    (i, j, k) of the highest voxel of `array` (indexed [k, j, i], like slicer.util.arrayFromVolume) that the line
    through ijkStart and ijkEnd (continuous IJK coordinates, voxel centres at whole numbers) passes, or None when
    the line misses the volume or only meets NaN voxels. Equal values: the one nearest ijkStart (the viewer).
    """
    if array is None or np.ndim(array) != 3 or min(np.shape(array)) == 0:
        return None
    dims = np.array(np.shape(array)[::-1], dtype=float)   # (I, J, K)
    clipped = clipLineToBox(ijkStart, ijkEnd, np.full(3, -0.5), dims - 0.5)
    if clipped is None:
        return None
    start = np.asarray(ijkStart, dtype=float)
    direction = np.asarray(ijkEnd, dtype=float) - start
    t0, t1 = clipped
    length = float(np.linalg.norm(direction)) * (t1 - t0)
    count = max(2, int(math.ceil(length * stepsPerVoxel)) + 1)
    points = start + np.outer(np.linspace(t0, t1, count), direction)
    ijk = np.clip(np.rint(points).astype(int), 0, dims.astype(int) - 1)
    keep = np.ones(len(ijk), dtype=bool)
    keep[1:] = np.any(ijk[1:] != ijk[:-1], axis=1)   # each voxel once
    ijk = ijk[keep]
    values = np.asarray(array)[ijk[:, 2], ijk[:, 1], ijk[:, 0]].astype(float)
    if not np.any(np.isfinite(values)):
        return None
    values[~np.isfinite(values)] = -np.inf
    best = ijk[int(np.argmax(values))]                  # argmax: first of equal values = nearest the viewer
    return int(best[0]), int(best[1]), int(best[2])


def pointOutsideView(xy, width, height):
    """True when a slice view position (pixels) is outside a width x height view."""
    return not (0 <= xy[0] <= width and 0 <= xy[1] <= height)


# --- Slicer glue -------------------------------------------------------------------------------------------

def threeDWidgetByName(name):
    """The 3D widget whose view node has this layout name, or None."""
    layoutManager = slicer.app.layoutManager()
    if layoutManager is None or not name:
        return None
    for index in range(layoutManager.threeDViewCount):
        widget = layoutManager.threeDWidget(index)
        viewNode = widget.mrmlViewNode() if widget is not None else None
        if viewNode is not None and viewNode.GetLayoutName() == name:
            return widget
    return None


def displayRay(threeDView, x, y):
    """Two world points (near, far) on the ray through display position x, y (render window pixels)."""
    renderer = threeDView.renderWindow().GetRenderers().GetFirstRenderer()
    if renderer is None:
        return None
    points = []
    for depth in (0.0, 1.0):
        renderer.SetDisplayPoint(float(x), float(y), depth)
        renderer.DisplayToWorld()
        world = renderer.GetWorldPoint()
        if abs(world[3]) < 1e-12:
            return None
        points.append([world[i] / world[3] for i in range(3)])
    return points


def brightestPointWorld(volumeNode, startWorld, endWorld):
    """World (RAS) position of the centre of the hottest voxel of volumeNode on the ray, or None."""
    if volumeNode is None or volumeNode.GetImageData() is None:
        return None
    array = slicer.util.arrayFromVolume(volumeNode)
    if array.ndim != 3:
        return None   # vector / multi-component volumes are not MIP'd
    parent = volumeNode.GetParentTransformNode()
    toLocal = toWorld = None
    if parent is not None:
        toLocal = vtk.vtkGeneralTransform()
        parent.GetTransformFromWorld(toLocal)
        toWorld = vtk.vtkGeneralTransform()
        parent.GetTransformToWorld(toWorld)
    rasToIjk = vtk.vtkMatrix4x4()
    volumeNode.GetRASToIJKMatrix(rasToIjk)

    def toIjk(point):
        if toLocal is not None:
            point = toLocal.TransformPoint(point)
        return rasToIjk.MultiplyPoint([point[0], point[1], point[2], 1.0])[:3]

    ijk = brightestVoxelOnLine(array, toIjk(startWorld), toIjk(endWorld))
    if ijk is None:
        return None
    ijkToRas = vtk.vtkMatrix4x4()
    volumeNode.GetIJKToRASMatrix(ijkToRas)
    ras = ijkToRas.MultiplyPoint([float(ijk[0]), float(ijk[1]), float(ijk[2]), 1.0])[:3]
    if toWorld is not None:
        ras = toWorld.TransformPoint(ras)
    return [float(c) for c in ras]


def brightestPointUnderMouse(threeDWidget, volumeNode, x=None, y=None):
    """Hottest voxel of volumeNode under the mouse (or under x, y) in a 3D view; world position or None."""
    if threeDWidget is None:
        return None
    threeDView = threeDWidget.threeDView()
    if x is None or y is None:
        x, y = threeDView.interactor().GetEventPosition()   # last mouse position in that view
    ray = displayRay(threeDView, x, y)
    if ray is None:
        return None
    return brightestPointWorld(volumeNode, ray[0], ray[1])


def jumpSlicesTo(positionWorld):
    """
    Every slice view of the layout moves to the slice through positionWorld; a view whose field of view would
    not show the point is also centred on it.
    """
    layoutManager = slicer.app.layoutManager()
    if layoutManager is None or positionWorld is None:
        return
    x, y, z = (float(c) for c in positionWorld)
    for name in layoutManager.sliceViewNames():
        sliceWidget = layoutManager.sliceWidget(name)
        sliceNode = sliceWidget.mrmlSliceNode() if sliceWidget is not None else None
        if sliceNode is None:
            continue
        try:
            sliceNode.JumpSliceByOffsetting(x, y, z)
            rasToXy = vtk.vtkMatrix4x4()
            vtk.vtkMatrix4x4.Invert(sliceNode.GetXYToRAS(), rasToXy)
            xy = rasToXy.MultiplyPoint([x, y, z, 1.0])
            dims = sliceNode.GetDimensions()
            if pointOutsideView(xy, dims[0], dims[1]):
                sliceNode.JumpSliceByCentering(x, y, z)
        except Exception:
            logging.debug(f"Epona: could not move slice view {name} to the ROI", exc_info=True)


def placeModeActive():
    appLogic = slicer.app.applicationLogic()
    interactionNode = appLogic.GetInteractionNode() if appLogic is not None else None
    return interactionNode is not None and interactionNode.GetCurrentInteractionMode() == interactionNode.Place


def activePlaceNodeID():
    appLogic = slicer.app.applicationLogic()
    selectionNode = appLogic.GetSelectionNode() if appLogic is not None else None
    return selectionNode.GetActivePlaceNodeID() if selectionNode is not None else None


def leavePlaceMode():
    appLogic = slicer.app.applicationLogic()
    interactionNode = appLogic.GetInteractionNode() if appLogic is not None else None
    if interactionNode is not None and interactionNode.GetCurrentInteractionMode() == interactionNode.Place:
        interactionNode.SwitchToViewTransformMode()


class MipClickPlacer:
    """
    Left clicks in 3D views while "Place ROI" is active. Each owner (module) gives a handler
    handler(threeDWidget, x, y) -> True when it placed an ROI (the click is then kept from Slicer, so Slicer does
    not add a point of its own on the volume rendering surface).
    """

    def __init__(self):
        self.handlers = {}      # owner -> handler
        self.installed = {}     # view node ID -> (interactor, [observer tags])
        self.swallowRelease = None
        self._layoutConnected = False

    def acquire(self, owner, handler):
        self.handlers[owner] = handler
        self.update()

    def release(self, owner):
        self.handlers.pop(owner, None)
        self.update()

    def update(self):
        if self.handlers:
            self.installAll()
            self._connectLayout(True)
        else:
            self._connectLayout(False)
            self.removeAll()

    def _connectLayout(self, connect):
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None or connect == self._layoutConnected:
            return
        try:
            if connect:
                layoutManager.connect("layoutChanged(int)", self._onLayoutChanged)
            else:
                layoutManager.disconnect("layoutChanged(int)", self._onLayoutChanged)
            self._layoutConnected = connect
        except Exception:
            logging.debug("Epona: layout signal (MIP ROI placement) failed", exc_info=True)

    def _onLayoutChanged(self, *args):
        import qt
        qt.QTimer.singleShot(0, self.installAll)   # new 3D views exist after the switch

    def installAll(self):
        if not self.handlers:
            return
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return
        for index in range(layoutManager.threeDViewCount):
            widget = layoutManager.threeDWidget(index)
            viewNode = widget.mrmlViewNode() if widget is not None else None
            if viewNode is None:
                continue
            key = viewNode.GetID()
            interactor = widget.threeDView().interactor()
            old = self.installed.get(key)
            if old is not None:
                if old[0] is interactor:
                    continue
                self._remove(key)
            tags = [
                interactor.AddObserver(vtk.vtkCommand.LeftButtonPressEvent,
                                       lambda caller, eventId, key=key: self._onPress(key, caller),
                                       OBSERVER_PRIORITY),
                interactor.AddObserver(vtk.vtkCommand.LeftButtonReleaseEvent,
                                       lambda caller, eventId, key=key: self._onRelease(key, caller),
                                       OBSERVER_PRIORITY),
            ]
            self.installed[key] = (interactor, tags)

    def _remove(self, key):
        interactor, tags = self.installed.pop(key)
        for tag in tags:
            try:
                interactor.RemoveObserver(tag)
            except Exception:
                pass

    def removeAll(self):
        self.swallowRelease = None
        for key in list(self.installed):
            self._remove(key)

    def _abort(self, key, interactor, index):
        entry = self.installed.get(key)
        if entry is None:
            return
        try:
            interactor.GetCommand(entry[1][index]).SetAbortFlag(1)
        except Exception:
            logging.debug("Epona: could not stop the mouse event", exc_info=True)

    def _widget(self, key):
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return None
        for index in range(layoutManager.threeDViewCount):
            widget = layoutManager.threeDWidget(index)
            viewNode = widget.mrmlViewNode() if widget is not None else None
            if viewNode is not None and viewNode.GetID() == key:
                return widget
        return None

    def _onPress(self, key, interactor):
        self.swallowRelease = None
        if not placeModeActive():
            return
        widget = self._widget(key)
        if widget is None:
            return
        x, y = interactor.GetEventPosition()
        for owner, handler in list(self.handlers.items()):
            try:
                handled = handler(widget, x, y)
            except Exception:
                logging.exception(f"Epona: placing an ROI on the MIP failed ({owner})")
                handled = False
            if handled:
                self.swallowRelease = key
                self._abort(key, interactor, 0)
                return

    def _onRelease(self, key, interactor):
        if self.swallowRelease == key:
            self.swallowRelease = None
            self._abort(key, interactor, 1)


def instance():
    placer = getattr(slicer.modules, _INSTANCE_ATTRIBUTE, None)
    if placer is None:
        placer = MipClickPlacer()
        try:
            setattr(slicer.modules, _INSTANCE_ATTRIBUTE, placer)
        except Exception:
            pass
    return placer


def shutdown():
    """Before a reload of this file: remove the observers of the old instance (owners acquire again on enter)."""
    placer = getattr(slicer.modules, _INSTANCE_ATTRIBUTE, None)
    if placer is not None:
        placer.handlers.clear()
        placer.update()
        try:
            delattr(slicer.modules, _INSTANCE_ATTRIBUTE)
        except Exception:
            pass

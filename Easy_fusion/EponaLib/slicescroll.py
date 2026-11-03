"""
Browse slices by holding the left mouse button and dragging up / down in a slice view (Epona and Epona Compare).

Slicer's own left drag (window / level of the background volume) is replaced only while one of the modules is
shown and the setting is on (Epona/LeftDragScrollsSlices, default on). It is left to Slicer when:
  - a place mode is active (Place ROI, ruler, any markup placement),
  - the mouse is on a markup point or handle (moving / resizing an ROI, moving a ruler end),
  - Shift, Ctrl or Alt is held (Slicer's own modifier gestures keep working).
Dragging up moves to the next slice (superior / anterior / left), dragging down to the previous one. Views that
are kept together (same orientation, both time points) follow through the modules' view sync.

One shared instance (kept on slicer.modules, so a module reload does not add a second set of observers);
modules call acquire(owner) on enter and release(owner) on exit. It does not rely on exit() alone: the observers
are also removed whenever another module is selected (module selector signal), and a press is only taken while
an owner module is the selected one, so other modules (Segment Editor, Markups, ...) always get Slicer's own mouse.
"""

import logging

import qt
import slicer
import vtk

SETTINGS_LEFT_DRAG_SCROLL = "Epona/LeftDragScrollsSlices"   # application setting (qt.QSettings)
PIXELS_PER_SLICE = 4            # mouse movement (render window pixels) for one slice
OBSERVER_PRIORITY = 10.0        # before Slicer's interactor style, so its window / level drag does not start
_INSTANCE_ATTRIBUTE = "_eponaLeftDragSliceScroll"


def leftDragScrollEnabled():
    try:
        return str(qt.QSettings().value(SETTINGS_LEFT_DRAG_SCROLL, "true")).lower() in ("true", "1")
    except Exception:
        return True


def setLeftDragScrollEnabled(enabled):
    qt.QSettings().setValue(SETTINGS_LEFT_DRAG_SCROLL, "true" if enabled else "false")
    instance().update()


def selectedModuleName():
    try:
        return str(slicer.util.selectedModule() or "")
    except Exception:
        return ""


def slicesForDrag(pixels, pixelsPerSlice=PIXELS_PER_SLICE):
    """(whole slices to move, pixels left over) for an accumulated vertical drag; toward zero, keeps the sign."""
    if pixelsPerSlice <= 0:
        return 0, pixels
    steps = int(pixels / pixelsPerSlice)
    return steps, pixels - steps * pixelsPerSlice


def clampOffset(offset, low, high):
    """Slice offset kept inside the volume's slice range (no clamping when the range is unknown / empty)."""
    if low is None or high is None or low > high:
        return offset
    return min(max(offset, low), high)


def markupUnderMouse():
    """True when a markup point or handle is highlighted (the mouse is on it): Slicer must get the drag."""
    try:
        noneComponent = slicer.vtkMRMLMarkupsDisplayNode.ComponentNone
        for displayNode in slicer.util.getNodesByClass("vtkMRMLMarkupsDisplayNode"):
            getter = getattr(displayNode, "GetActiveComponentType", None)
            if getter is not None and displayNode.GetVisibility() and getter() != noneComponent:
                return True
    except Exception:
        logging.debug("Epona: could not check the markups under the mouse", exc_info=True)
    return False


def placeModeActive():
    appLogic = slicer.app.applicationLogic()
    interactionNode = appLogic.GetInteractionNode() if appLogic is not None else None
    return interactionNode is not None and interactionNode.GetCurrentInteractionMode() != interactionNode.ViewTransform


def scrollSlices(sliceWidget, steps):
    """Move a slice view by whole slices (spacing of the lowest volume shown in it)."""
    sliceLogic = sliceWidget.sliceLogic()
    if sliceLogic is None or not steps:
        return
    spacing = 1.0
    try:
        spacing = float(sliceLogic.GetLowestVolumeSliceSpacing()[2]) or 1.0
    except Exception:
        pass
    low = high = None
    try:
        bounds = [0.0] * 6
        sliceLogic.GetLowestVolumeSliceBounds(bounds)
        low, high = bounds[4], bounds[5]
    except Exception:
        pass
    offset = sliceLogic.GetSliceOffset()
    sliceLogic.SetSliceOffset(clampOffset(offset + steps * spacing, low, high))


class LeftDragSliceScroll:

    def __init__(self):
        self.owners = set()
        self.installed = {}     # view name -> (interactor, [observer tags])
        self.drag = None        # {"name", "widget", "y", "rest"} while dragging
        self._layoutConnected = False
        self._moduleSelectorConnected = False

    def active(self):
        """Only while one of the owner modules is shown and the setting is on."""
        return bool(self.owners) and selectedModuleName() in self.owners and leftDragScrollEnabled()

    # --- switching on / off -------------------------------------------------------------------------------

    def acquire(self, owner):
        self.owners.add(owner)
        self.update()

    def release(self, owner):
        self.owners.discard(owner)
        self.update()

    def update(self):
        self._connectModuleSelector()
        if self.active():
            self.installAll()
            self._connectLayout(True)
        else:
            self._connectLayout(False)
            self.removeAll()

    def _connectModuleSelector(self):
        """Another module selected -> observers off, even if a module's exit() was not called."""
        if self._moduleSelectorConnected:
            return
        try:
            selector = slicer.util.mainWindow().moduleSelector()
            selector.connect("moduleSelected(QString)", self._onModuleSelected)
            self._moduleSelectorConnected = True
        except Exception:
            logging.debug("Epona: module selector signal (left drag scroll) failed", exc_info=True)

    def _onModuleSelected(self, *args):
        qt.QTimer.singleShot(0, self.update)   # after the selection has settled

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
            logging.debug("Epona: layout signal (left drag scroll) failed", exc_info=True)

    def _onLayoutChanged(self, *args):
        qt.QTimer.singleShot(0, self.installAll)   # new views (e.g. the compare rows) exist after the switch

    # --- observers on the slice views -----------------------------------------------------------------------

    def installAll(self):
        if not self.active():
            self.removeAll()
            return
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return
        for name in layoutManager.sliceViewNames():
            sliceWidget = layoutManager.sliceWidget(name)
            if sliceWidget is None:
                continue
            interactor = sliceWidget.sliceView().interactor()
            old = self.installed.get(name)
            if old is not None:
                if old[0] is interactor:
                    continue
                self._remove(name)
            tags = []
            for event, handler in ((vtk.vtkCommand.LeftButtonPressEvent, self._onPress),
                                   (vtk.vtkCommand.MouseMoveEvent, self._onMove),
                                   (vtk.vtkCommand.LeftButtonReleaseEvent, self._onRelease)):
                tags.append(interactor.AddObserver(
                    event, lambda caller, eventId, name=name, handler=handler: handler(name, caller),
                    OBSERVER_PRIORITY))
            self.installed[name] = (interactor, tags)

    def _remove(self, name):
        interactor, tags = self.installed.pop(name)
        for tag in tags:
            try:
                interactor.RemoveObserver(tag)
            except Exception:
                pass

    def removeAll(self):
        self.drag = None
        for name in list(self.installed):
            self._remove(name)

    def _abort(self, name, interactor, index):
        """Stop the event here (Slicer's window / level drag does not start)."""
        entry = self.installed.get(name)
        if entry is None:
            return
        try:
            interactor.GetCommand(entry[1][index]).SetAbortFlag(1)
        except Exception:
            logging.debug("Epona: could not stop the mouse event", exc_info=True)

    def _onPress(self, name, interactor):
        self.drag = None
        if not self.active():
            return
        if interactor.GetShiftKey() or interactor.GetControlKey() or interactor.GetAltKey():
            return
        if placeModeActive() or markupUnderMouse():
            return
        layoutManager = slicer.app.layoutManager()
        sliceWidget = layoutManager.sliceWidget(name) if layoutManager is not None else None
        if sliceWidget is None:
            return
        self.drag = {"name": name, "widget": sliceWidget, "y": interactor.GetEventPosition()[1], "rest": 0}
        self._abort(name, interactor, 0)

    def _onMove(self, name, interactor):
        drag = self.drag
        if drag is None or drag["name"] != name:
            return
        y = interactor.GetEventPosition()[1]
        steps, drag["rest"] = slicesForDrag(drag["rest"] + (y - drag["y"]))   # VTK y grows upward
        drag["y"] = y
        if steps:
            try:
                scrollSlices(drag["widget"], steps)
            except Exception:
                logging.exception("Epona: slice scrolling failed")
                self.drag = None
        self._abort(name, interactor, 1)

    def _onRelease(self, name, interactor):
        if self.drag is not None:
            self.drag = None
            self._abort(name, interactor, 2)


def instance():
    scroll = getattr(slicer.modules, _INSTANCE_ATTRIBUTE, None)
    if scroll is None:
        scroll = LeftDragSliceScroll()
        try:
            setattr(slicer.modules, _INSTANCE_ATTRIBUTE, scroll)
        except Exception:
            pass
    return scroll


def shutdown():
    """Before a reload of this file: remove the observers of the old instance (owners acquire again on enter)."""
    scroll = getattr(slicer.modules, _INSTANCE_ATTRIBUTE, None)
    if scroll is not None:
        scroll.owners.clear()
        scroll.update()
        if scroll._moduleSelectorConnected:
            try:
                slicer.util.mainWindow().moduleSelector().disconnect("moduleSelected(QString)",
                                                                     scroll._onModuleSelected)
            except Exception:
                pass
        try:
            delattr(slicer.modules, _INSTANCE_ATTRIBUTE)
        except Exception:
            pass

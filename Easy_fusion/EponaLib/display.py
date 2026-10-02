"""Display helpers: window / level kept in sync between volumes, slice views kept at the same position, Hot Iron
color stops, color table lookup."""

import slicer
import vtk

from .layouts import fieldOfViewForTarget


# PET window/level is kept identical between the fusion PET and its PET-only (inverted grey) twin.
_windowLevelSync = {"nodes": None, "tags": [], "busy": False}


def bindWindowLevelSync(displayNodeA, displayNodeB):
    state = _windowLevelSync
    if state["nodes"] is not None and state["nodes"][0] is displayNodeA and state["nodes"][1] is displayNodeB:
        return
    unbindWindowLevelSync()
    state["nodes"] = (displayNodeA, displayNodeB)
    state["tags"] = [
        (displayNodeA, displayNodeA.AddObserver(
            vtk.vtkCommand.ModifiedEvent, lambda caller, event: _syncWindowLevel(displayNodeA, displayNodeB))),
        (displayNodeB, displayNodeB.AddObserver(
            vtk.vtkCommand.ModifiedEvent, lambda caller, event: _syncWindowLevel(displayNodeB, displayNodeA))),
    ]


def unbindWindowLevelSync():
    state = _windowLevelSync
    for node, tag in state["tags"]:
        try:
            node.RemoveObserver(tag)
        except Exception:
            pass
    state["tags"] = []
    state["nodes"] = None


def _syncWindowLevel(source, target):
    state = _windowLevelSync
    if state["busy"]:
        return
    if (abs(source.GetWindow() - target.GetWindow()) < 1e-9
            and abs(source.GetLevel() - target.GetLevel()) < 1e-9):
        return
    state["busy"] = True
    try:
        wasModifying = target.StartModify()
        target.SetAutoWindowLevel(False)
        target.SetWindow(source.GetWindow())
        target.SetLevel(source.GetLevel())
        target.EndModify(wasModifying)
    finally:
        state["busy"] = False


class WindowLevelLink:
    """
    Keeps window / level identical between two scalar display nodes, in both directions. Unlike the functions
    above (one pair for Epona), any number of links can exist, e.g. PET <-> its PET-only twin for each time point
    and time point 1 <-> time point 2. Links may form a chain: every link has its own guard, and equal values
    are never written again, so a change travels along the chain once and stops.
    """

    def __init__(self):
        self.nodes = None
        self._tags = []
        self._busy = False

    def bind(self, displayNodeA, displayNodeB):
        if self.nodes is not None and self.nodes[0] is displayNodeA and self.nodes[1] is displayNodeB:
            return
        self.unbind()
        if displayNodeA is None or displayNodeB is None or displayNodeA is displayNodeB:
            return
        self.nodes = (displayNodeA, displayNodeB)
        self._tags = [
            (displayNodeA, displayNodeA.AddObserver(
                vtk.vtkCommand.ModifiedEvent, lambda caller, event: self._sync(displayNodeA, displayNodeB))),
            (displayNodeB, displayNodeB.AddObserver(
                vtk.vtkCommand.ModifiedEvent, lambda caller, event: self._sync(displayNodeB, displayNodeA))),
        ]

    def unbind(self):
        for node, tag in self._tags:
            try:
                node.RemoveObserver(tag)
            except Exception:
                pass
        self._tags = []
        self.nodes = None

    def syncFrom(self, source):
        """Copy the window of source (one of the two linked nodes) to the other one now."""
        if self.nodes is not None and source in self.nodes:
            self._sync(source, self.nodes[1] if source is self.nodes[0] else self.nodes[0])

    def _sync(self, source, target):
        if self._busy:
            return
        if (abs(source.GetWindow() - target.GetWindow()) < 1e-9
                and abs(source.GetLevel() - target.GetLevel()) < 1e-9):
            return
        self._busy = True
        try:
            wasModifying = target.StartModify()
            target.SetAutoWindowLevel(False)
            target.SetWindow(source.GetWindow())
            target.SetLevel(source.GetLevel())
            target.EndModify(wasModifying)
        finally:
            self._busy = False


def copySliceGeometry(source, target):
    """Same slice plane, position, pan and zoom (anatomical width), keeping the target's aspect ratio."""
    wasModifying = target.StartModify()
    target.GetSliceToRAS().DeepCopy(source.GetSliceToRAS())
    target.SetXYZOrigin(*source.GetXYZOrigin())
    fieldOfView = fieldOfViewForTarget(source.GetFieldOfView(), target.GetDimensions())
    if fieldOfView is not None:
        target.SetFieldOfView(*fieldOfView)
    target.UpdateMatrices()
    target.EndModify(wasModifying)


def sliceOrientation(sliceNode):
    """ "Axial", "Sagittal", "Coronal", "Reformat"... (None if the slice node cannot tell)."""
    for getterName in ("GetOrientation", "GetOrientationString"):   # the second: Slicer before 4.11
        getter = getattr(sliceNode, getterName, None)
        if getter is not None:
            try:
                return getter()
            except Exception:
                pass
    return None


ASPECT_TOLERANCE = 0.01   # relative difference between the field of view's and the view's height / width


def correctedFieldOfView(fieldOfView, dimensions, tolerance=ASPECT_TOLERANCE):
    """
    The field of view with its height set to match the view's pixel aspect ratio (same width, so the zoom stays),
    or None when it is already right (or the view has no size yet). A field of view whose aspect differs from the
    view's draws the image stretched; Slicer leaves it so when a view was squeezed to zero width or height while a
    layout was being switched (it then scales only one side when the view grows back).
    """
    width, height = float(dimensions[0]), float(dimensions[1])
    fovX, fovY = float(fieldOfView[0]), float(fieldOfView[1])
    if width <= 0 or height <= 0 or fovX <= 0:
        return None
    expectedY = fovX * height / width
    if expectedY > 0 and abs(fovY - expectedY) <= tolerance * expectedY:
        return None
    return (fovX, expectedY, float(fieldOfView[2]))


def repairSliceAspect(sliceNode):
    """Undo a stretched view (see correctedFieldOfView). True if it was changed."""
    corrected = correctedFieldOfView(sliceNode.GetFieldOfView(), sliceNode.GetDimensions())
    if corrected is None:
        return False
    sliceNode.SetFieldOfView(*corrected)
    return True


def viewShapeDiffers(dimensions, viewSize, grid=(1, 1), tolerance=ASPECT_TOLERANCE):
    """
    True when the slice node's size (dimensions) has another shape than the view actually has on screen
    (viewSize: render window pixels; grid: lightbox columns, rows). Shapes only, so it does not depend on the
    screen's pixel ratio. The node then draws for the old shape: the image looks stretched.
    """
    width, height = float(viewSize[0]) / max(1, grid[0]), float(viewSize[1]) / max(1, grid[1])
    if width <= 0 or height <= 0 or dimensions[0] <= 0 or dimensions[1] <= 0:
        return False
    viewAspect = width / height
    nodeAspect = float(dimensions[0]) / float(dimensions[1])
    return abs(nodeAspect - viewAspect) > tolerance * viewAspect


def syncSliceViewSize(sliceWidget):
    """
    Tell a slice node the real size of its view when they disagree, the way Slicer does on a resize event (a
    view resized while hidden, e.g. in a floating window being rebuilt, is not told), then fix the field of
    view's aspect. True if something was changed.
    """
    sliceNode = sliceWidget.mrmlSliceNode() if sliceWidget is not None else None
    if sliceNode is None:
        return False
    try:
        viewSize = sliceWidget.sliceView().renderWindow().GetSize()
        grid = (sliceNode.GetLayoutGridColumns(), sliceNode.GetLayoutGridRows())
    except Exception:
        return False
    changed = False
    if viewShapeDiffers(sliceNode.GetDimensions(), viewSize, grid):
        sliceWidget.sliceLogic().ResizeSliceNode(float(viewSize[0]), float(viewSize[1]))
        changed = True
    return repairSliceAspect(sliceNode) or changed


def _sameGeometry(source, target, tolerance=1e-4):
    if correctedFieldOfView(target.GetFieldOfView(), target.GetDimensions()) is not None:
        return False   # the target is stretched: copy again (that sets its height from its own aspect)
    a, b = source.GetSliceToRAS(), target.GetSliceToRAS()
    if any(abs(a.GetElement(r, c) - b.GetElement(r, c)) > tolerance for r in range(4) for c in range(4)):
        return False
    if any(abs(x - y) > tolerance for x, y in zip(source.GetXYZOrigin(), target.GetXYZOrigin())):
        return False
    return abs(source.GetFieldOfView()[0] - target.GetFieldOfView()[0]) <= tolerance


class SliceGeometrySync:
    """
    Keeps slice position, pan and zoom identical within groups of slice views (e.g. all axial views of both time
    points in Compare; axial fusion, CT and PET views in Epona). Slicer's own view linking is not used: it also
    copies the displayed volumes between views, which must differ here (time point 1 vs 2, fusion vs CT vs PET).
    """

    def __init__(self):
        self.groups = []
        self._observations = []
        self._syncing = False

    def setGroups(self, groups):
        """groups: lists of slice nodes that move together. The first node of each group is the reference."""
        self.clear()
        self.groups = [list(group) for group in groups if len(group) > 1]
        for group in self.groups:
            for node in group:
                self._observations.append((node, node.AddObserver(vtk.vtkCommand.ModifiedEvent, self._onModified)))

    def clear(self):
        for node, tag in self._observations:
            try:
                node.RemoveObserver(tag)
            except Exception:
                pass
        self._observations = []
        self.groups = []

    def alignToReferences(self):
        """Give every view of a group the geometry of the group's first view."""
        for group in self.groups:
            self.syncFrom(group[0], group)

    def _onModified(self, caller, event):
        if self._syncing:
            return
        for group in self.groups:
            if any(node is caller for node in group):
                self.syncFrom(caller, group)
                return

    def syncFrom(self, source, group):
        """Copy the source's geometry to the views of its group that currently have the same orientation (a view
        turned to another orientation with its view controller is left alone, not turned back)."""
        orientation = sliceOrientation(source)
        self._syncing = True
        try:
            repairSliceAspect(source)   # a stretched source would pass its stretch on
            for node in group:
                if node is source or sliceOrientation(node) != orientation:
                    continue
                if not _sameGeometry(source, node):
                    copySliceGeometry(source, node)
        finally:
            self._syncing = False


def fillHotIronTable(colorNode):
    """Write Epona's 256-color Hot Iron table into a color table node (idempotent: also repairs a broken one)."""
    wasModifying = colorNode.StartModify()
    try:
        colorNode.SetTypeToUser()
        colorNode.SetNumberOfColors(256)
        # SetColor(index, name, ...) names each color. Only Slicer versions without SetColorDefined (before 5.4)
        # also need SetNamesInitialised; newer ones print a deprecation warning for it.
        if not hasattr(colorNode, "SetColorDefined") and hasattr(colorNode, "SetNamesInitialised"):
            colorNode.SetNamesInitialised(True)
        for i in range(256):
            r, g, b = hotIronRGB(i / 255.0)
            colorNode.SetColor(i, f"Color{i}", r, g, b, 1.0)
        colorNode.GetLookupTable().SetTableRange(0, 255)
    finally:
        colorNode.EndModify(wasModifying)


def hotIronRGB(t):
    """Custom Hot Iron color stops for t in [0, 1]."""
    if t <= 0.5:
        r, g, b = t * 2, 0.0, 0.0
    elif t <= 0.75:
        r, g, b = 1.0, (t - 0.5) * 4, 0.0
    else:
        r, g, b = 1.0, 1.0, (t - 0.75) * 4
    return (min(max(r, 0.0), 1.0), min(max(g, 0.0), 1.0), min(max(b, 0.0), 1.0))


def findColorNode(name):
    """
    The color table (or other color node) with this name, e.g. "Red", "Inferno", "PET-Rainbow2"; None if there is
    none. Only color nodes are searched: other nodes share names with color tables (the Red slice view is "Red").
    """
    for node in slicer.util.getNodesByClass("vtkMRMLColorNode"):
        if node.GetName() == name:
            return node
    return None

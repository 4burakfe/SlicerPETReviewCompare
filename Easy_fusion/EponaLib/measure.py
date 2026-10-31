"""
Distance measurement (ruler) shared by Epona and Epona Compare.

The toolbar's ruler button starts one new line: click its two ends in a slice view and the length (mm) shows next
to the line. The lines are plain Slicer line markups tagged Epona.Ruler, drawn in the slice views only (not in the
MIPs). They are for looking only: the modules' ROI tables list their own ROI nodes, never these lines.
Escape (or clicking the button again) cancels a line that is not finished; the button's menu deletes all lines.
"""

import logging

import slicer
import vtk

RULER_ATTRIBUTE = "Epona.Ruler"
RULER_NODE_CLASS = "vtkMRMLMarkupsLineNode"
RULER_NAME_PREFIX = "Ruler"
RULER_COLOR = (1.0, 0.85, 0.1)      # yellow: readable on grey CT, inverted-grey PET and the color maps
RULER_TEXT_SCALE = 3.0
RULER_GLYPH_SCALE = 1.5
RULER_LINE_THICKNESS = 0.3


def isRulerNode(node):
    return node is not None and bool(node.GetAttribute(RULER_ATTRIBUTE))


def rulerNodes():
    return [node for node in slicer.util.getNodesByClass(RULER_NODE_CLASS) if isRulerNode(node)]


def definedPointCount(node):
    """Control points the user has placed (the point following the mouse while placing is not counted)."""
    if node is None:
        return 0
    counter = getattr(node, "GetNumberOfDefinedControlPoints", None)
    if counter is not None:
        return counter()
    return node.GetNumberOfControlPoints()


def styleRuler(node):
    """Yellow line with its length, in the slice views only. Setters missing in older Slicer versions are skipped."""
    displayNode = node.GetDisplayNode()
    if displayNode is None:
        node.CreateDefaultDisplayNodes()
        displayNode = node.GetDisplayNode()
    if displayNode is None:
        return
    for setter, args in (("SetSelectedColor", RULER_COLOR), ("SetColor", RULER_COLOR),
                         ("SetActiveColor", (1.0, 1.0, 0.6)), ("SetTextScale", (RULER_TEXT_SCALE,)),
                         ("SetGlyphScale", (RULER_GLYPH_SCALE,)), ("SetLineThickness", (RULER_LINE_THICKNESS,)),
                         ("SetVisibility3D", (False,)), ("SetPropertiesLabelVisibility", (True,))):
        method = getattr(displayNode, setter, None)
        if method is None:
            continue
        try:
            method(*args)
        except Exception:
            logging.debug(f"Epona: ruler display setting {setter} failed", exc_info=True)


def nextRulerName():
    """Short names (Ruler 1, Ruler 2, ...): the name is shown in the label next to the length."""
    used = {node.GetName() for node in rulerNodes()}
    number = 1
    while f"{RULER_NAME_PREFIX} {number}" in used:
        number += 1
    return f"{RULER_NAME_PREFIX} {number}"


class RulerTool:
    """
    One module's ruler: start() places a new line (two clicks), cancel() stops placing (an unfinished line is
    removed), deleteAll() removes every ruler line. onPlacingChanged(placing) keeps a toolbar button in sync.
    """

    def __init__(self, onPlacingChanged=None):
        self.onPlacingChanged = onPlacingChanged
        self.pendingNode = None
        self._interactionNode = None
        self._observerTag = None

    @property
    def placing(self):
        return self.pendingNode is not None

    def start(self):
        if self.placing:
            return self.pendingNode
        appLogic = slicer.app.applicationLogic()
        interactionNode = appLogic.GetInteractionNode() if appLogic is not None else None
        selectionNode = appLogic.GetSelectionNode() if appLogic is not None else None
        if interactionNode is None or selectionNode is None:
            slicer.util.showStatusMessage("Epona: the ruler needs Slicer's interaction node", 3000)
            self._notify(False)
            return None
        if interactionNode.GetCurrentInteractionMode() == interactionNode.Place:
            interactionNode.SwitchToViewTransformMode()   # e.g. "Place ROI" was waiting for a click
        node = slicer.mrmlScene.AddNewNodeByClass(RULER_NODE_CLASS, nextRulerName())
        node.SetAttribute(RULER_ATTRIBUTE, "1")
        styleRuler(node)
        selectionNode.SetReferenceActivePlaceNodeClassName(RULER_NODE_CLASS)
        selectionNode.SetActivePlaceNodeID(node.GetID())
        interactionNode.SetPlaceModePersistence(0)
        interactionNode.SetCurrentInteractionMode(interactionNode.Place)
        self.pendingNode = node
        self._observe(interactionNode)   # only now: the calls above modify the interaction node too
        slicer.util.showStatusMessage("Ruler: click the two ends of the distance in a slice view (Esc cancels)", 5000)
        self._notify(True)
        return node

    def cancel(self):
        interactionNode = self._interactionNode
        if interactionNode is not None and interactionNode.GetCurrentInteractionMode() == interactionNode.Place:
            interactionNode.SwitchToViewTransformMode()   # -> _onInteractionModified finishes
        self._finish()

    def setPlacing(self, placing):
        if placing:
            self.start()
        else:
            self.cancel()

    def deleteAll(self):
        self.cancel()
        nodes = rulerNodes()
        for node in nodes:
            slicer.mrmlScene.RemoveNode(node)
        if nodes:
            slicer.util.showStatusMessage(f"Ruler: {len(nodes)} measurement(s) deleted", 3000)

    def release(self):
        """Module cleanup: stop placing, remove the observer (the ruler lines stay in the scene)."""
        self.cancel()
        self._unobserve()

    # --- internals -----------------------------------------------------------------------------------------

    def _observe(self, interactionNode):
        if self._interactionNode is interactionNode and self._observerTag is not None:
            return
        self._unobserve()
        self._interactionNode = interactionNode
        self._observerTag = interactionNode.AddObserver(vtk.vtkCommand.ModifiedEvent, self._onInteractionModified)

    def _unobserve(self):
        if self._interactionNode is not None and self._observerTag is not None:
            self._interactionNode.RemoveObserver(self._observerTag)
        self._interactionNode = None
        self._observerTag = None

    def _onInteractionModified(self, caller=None, event=None):
        interactionNode = self._interactionNode
        if self.pendingNode is None or interactionNode is None:
            return
        appLogic = slicer.app.applicationLogic()
        selectionNode = appLogic.GetSelectionNode() if appLogic is not None else None
        stillPlacingThis = (interactionNode.GetCurrentInteractionMode() == interactionNode.Place
                            and selectionNode is not None
                            and selectionNode.GetActivePlaceNodeID() == self.pendingNode.GetID())
        if not stillPlacingThis:
            self._finish()

    def _finish(self):
        """Placing ended (line done, Escape, another place mode): drop an unfinished line, release the button."""
        node = self.pendingNode
        if node is None:
            return
        self.pendingNode = None
        self._unobserve()
        try:
            if node.GetScene() is not None and definedPointCount(node) < 2:
                slicer.mrmlScene.RemoveNode(node)
            elif node.GetScene() is not None:
                lengthText = self._lengthText(node)
                if lengthText:
                    slicer.util.showStatusMessage(f"{node.GetName()}: {lengthText}", 5000)
        except Exception:
            logging.debug("Epona: could not finish the ruler line", exc_info=True)
        self._notify(False)

    @staticmethod
    def _lengthText(node):
        try:
            if node.GetNumberOfControlPoints() < 2:
                return ""
            a, b = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
            node.GetNthControlPointPositionWorld(0, a)
            node.GetNthControlPointPositionWorld(1, b)
            return f"{sum((p - q) ** 2 for p, q in zip(a, b)) ** 0.5:.1f} mm"
        except Exception:
            return ""

    def _notify(self, placing):
        if self.onPlacingChanged is not None:
            try:
                self.onPlacingChanged(placing)
            except Exception:
                logging.debug("Epona: ruler button update failed", exc_info=True)

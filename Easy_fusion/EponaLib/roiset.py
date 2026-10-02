"""
A set of spherical ROIs measured on one SPECT/PET: Epona's SUV ROIs, and the ROIs of each time point in the
compare module.

Several sets can live in one scene. Each set has its own nodes, found by EponaCompare.RoiSet = tag (e.g. "TP1")
and EponaCompare.RoiRole (Epona's set instead uses its own attribute per role, see roleAttributes, so scenes saved
by earlier Epona versions keep their ROIs):
  points    markups list with the ROI centres (under the PET's transform, so ROIs follow a registration change)
  handles   six radius handles per ROI (drag to resize)
  labels    white text next to each ROI (Max, Mean, ...)
  spheres   one model with all spheres (their slice intersections draw the circles)
  segments  segmentation with the thresholded segment of each ROI (Mean, MTV, TLG)
Everything is shown only in the views given by setViews (e.g. that time point's views); without setViews the
display nodes' views are left as they are.
Per-ROI settings are stored on the points node as EasyFusion.RoiRadius_<id>, EasyFusion.RoiThreshold_<id>, ...
"""

import logging

import numpy as np

import vtk
import slicer
from slicer.util import VTKObservationMixin

from .roi import (
    _distance, DEFAULT_RELATIVE_THRESHOLD, formatRoiLabel, formatRoiThreshold, HANDLE_DIRECTIONS,
    LABEL_ANCHOR_DIRECTIONS, labelAnchorPosition, parseHandleDescription, parseRoiColor, parseRoiThreshold,
    radiusHandlePosition, randomRoiColor, resolveHandleDrag, ROI_COLOR_ATTRIBUTE, ROI_HANDLE_GLYPH_SCALE,
    ROI_LABEL_TEXT_SCALE, ROI_NEXT_NUMBER_ATTRIBUTE, ROI_NUMBER_ATTRIBUTE, ROI_ORIGIN_AI, ROI_ORIGIN_ATTRIBUTE,
    ROI_ORIGIN_USER, ROI_SEGMENT_ID_PREFIX, ROI_SEGMENT_OUTLINE_PX, ROI_SPHERE_COLOR_ARRAY, ROI_SPHERE_OUTLINE_PX,
    ROI_THRESHOLD_ATTRIBUTE, sphereStatisticsFromArray, THRESHOLD_RELATIVE,
)

ROI_SET_ATTRIBUTE = "EponaCompare.RoiSet"      # value: the set's tag, e.g. "TP1"
ROI_ROLE_ATTRIBUTE = "EponaCompare.RoiRole"    # points / handles / labels / spheres / segments
ROI_RADIUS_ATTRIBUTE = "EasyFusion.RoiRadius_"  # + control point ID (same name as in Epona)
ROI_PET_REFERENCE_ROLE = "EponaCompareRoiPET"   # points node -> the PET the ROIs are measured on
ROI_SEGMENTATION_PET_ROLE = "EponaCompareSegmentationPET"
PER_ROI_ATTRIBUTES = (ROI_RADIUS_ATTRIBUTE, ROI_NUMBER_ATTRIBUTE, ROI_COLOR_ATTRIBUTE, ROI_THRESHOLD_ATTRIBUTE,
                      ROI_ORIGIN_ATTRIBUTE)
ROLE_POINTS, ROLE_HANDLES, ROLE_LABELS, ROLE_SPHERES, ROLE_SEGMENTS = (
    "points", "handles", "labels", "spheres", "segments")
ROLE_CLASSES = {ROLE_POINTS: "vtkMRMLMarkupsFiducialNode", ROLE_HANDLES: "vtkMRMLMarkupsFiducialNode",
                ROLE_LABELS: "vtkMRMLMarkupsFiducialNode", ROLE_SPHERES: "vtkMRMLModelNode",
                ROLE_SEGMENTS: "vtkMRMLSegmentationNode"}
LABEL_STYLE_VERSION_ATTRIBUTE = "EasyFusion.StyleVersion"   # label layers of older scenes are restyled once
LABEL_STYLE_VERSION = "1"


def _markupsEvents():
    events = []
    for name in ("PointAddedEvent", "PointModifiedEvent", "PointRemovedEvent", "PointPositionDefinedEvent",
                 "PointPositionUndefinedEvent"):
        event = getattr(slicer.vtkMRMLMarkupsNode, name, None)
        if event is not None and event not in events:
            events.append(event)
    return events


def isControlPointDefined(node, index):
    definedStatus = getattr(slicer.vtkMRMLMarkupsNode, "PositionDefined", None)
    if definedStatus is None or not hasattr(node, "GetNthControlPointPositionStatus"):
        return True
    return node.GetNthControlPointPositionStatus(index) == definedStatus


def statsCacheKey(volumeNode, center, radius, thresholdMode=THRESHOLD_RELATIVE,
                  thresholdValue=DEFAULT_RELATIVE_THRESHOLD):
    roundedCenter = tuple(round(c, 3) for c in center)
    thresholdKey = (thresholdMode, round(float(thresholdValue), 4))
    if volumeNode is None:
        return (None, roundedCenter, round(radius, 3), thresholdKey)
    imageData = volumeNode.GetImageData()
    transformNode = volumeNode.GetParentTransformNode()
    return (volumeNode.GetID(), volumeNode.GetMTime(), imageData.GetMTime() if imageData else 0,
            transformNode.GetMTime() if transformNode else 0, roundedCenter, round(radius, 3), thresholdKey)


def computeSphereStatistics(volumeNode, centerWorld, radiusMm, thresholdMode=THRESHOLD_RELATIVE,
                            thresholdValue=DEFAULT_RELATIVE_THRESHOLD):
    """Statistics (sphere + thresholded segment) of the voxels inside a sphere given in world coordinates."""
    if volumeNode is None or volumeNode.GetImageData() is None:
        return None
    center = list(centerWorld)
    transformNode = volumeNode.GetParentTransformNode()
    if transformNode is not None:
        worldToVolume = vtk.vtkGeneralTransform()
        transformNode.GetTransformFromWorld(worldToVolume)
        center = list(worldToVolume.TransformPoint(center))
    ijkToRas = vtk.vtkMatrix4x4()
    volumeNode.GetIJKToRASMatrix(ijkToRas)
    return sphereStatisticsFromArray(slicer.util.arrayFromVolume(volumeNode),
                                     slicer.util.arrayFromVTKMatrix(ijkToRas), center, radiusMm,
                                     thresholdMode, thresholdValue)


def writeSegmentMask(segmentationNode, segmentID, petNode, extent, mask):
    """Replace a segment with a boolean mask given on the PET voxel grid ([k, j, i] over extent)."""
    from vtk.util import numpy_support
    labelmap = slicer.vtkOrientedImageData()
    ijkToRas = vtk.vtkMatrix4x4()
    petNode.GetIJKToRASMatrix(ijkToRas)
    labelmap.SetImageToWorldMatrix(ijkToRas)
    labelmap.SetExtent(*extent)
    labelmap.AllocateScalars(vtk.VTK_UNSIGNED_CHAR, 1)
    scalars = labelmap.GetPointData().GetScalars()
    numpy_support.vtk_to_numpy(scalars)[:] = np.asarray(mask, dtype=np.uint8).ravel()
    scalars.Modified()
    mode = getattr(slicer.vtkSlicerSegmentationsModuleLogic, "MODE_REPLACE", 0)
    slicer.vtkSlicerSegmentationsModuleLogic.SetBinaryLabelmapToSegment(labelmap, segmentationNode, segmentID, mode)


class SphereRoiSet(VTKObservationMixin):
    """
    The ROIs of one SPECT/PET (see the module docstring). The owner (a module panel) calls update() whenever
    onChanged() was called, a PET changed or a setting changed, and shows what update() returns.
    """

    def __init__(self, tag, title, onChanged, color=(0.1, 0.9, 0.3), roleAttributes=None,
                 petReferenceRole=ROI_PET_REFERENCE_ROLE, segmentationPetRole=ROI_SEGMENTATION_PET_ROLE,
                 followPetTransform=True):
        """
        tag: identifies the set's nodes ("TP1"); title: name prefix of its nodes ("<title> ROIs", ...);
        onChanged(): the points, handles or the PET's transform changed (the owner calls update() soon).
        roleAttributes: {role: attribute name}: find / mark the nodes by these attributes (value "1") instead of
        the set tag (Epona's nodes). petReferenceRole / segmentationPetRole: node reference roles to the PET.
        followPetTransform: put the points under the PET's transform, so the ROIs move with it (compare: the
        registration); Epona keeps ROIs where they were placed in world coordinates.
        """
        VTKObservationMixin.__init__(self)
        self.tag = tag
        self.title = title
        self.pointColor = color
        self._onChanged = onChanged
        self._roleAttributes = dict(roleAttributes or {})
        self._petReferenceRole = petReferenceRole
        self._segmentationPetRole = segmentationPetRole
        self._followPetTransform = followPetTransform
        self.roiNode = None
        self.handlesNode = None
        self.viewNodeIDs = None   # None: display nodes' views are not managed
        self._observedTransform = None
        self._statsCache = {}
        self._knownIDs = None
        self._lastGeometry = {}
        self._lastHandlePositions = {}
        self._activeHandleID = None
        self._updating = False
        self.rows = []          # (pointID, name, radius, stats, threshold, origin)
        self.centers = {}       # pointID -> centre (world)

    # --- nodes -------------------------------------------------------------------------------------------

    def _find(self, role):
        attribute = self._roleAttributes.get(role)
        for node in slicer.util.getNodesByClass(ROLE_CLASSES[role]):
            if attribute is not None:
                if node.GetAttribute(attribute):
                    return node
            elif node.GetAttribute(ROI_SET_ATTRIBUTE) == self.tag and node.GetAttribute(ROI_ROLE_ATTRIBUTE) == role:
                return node
        return None

    def _tag(self, node, role):
        attribute = self._roleAttributes.get(role)
        if attribute is not None:
            node.SetAttribute(attribute, "1")
        else:
            node.SetAttribute(ROI_SET_ATTRIBUTE, self.tag)
            node.SetAttribute(ROI_ROLE_ATTRIBUTE, role)

    def findRoiNode(self):
        return self._find(ROLE_POINTS)

    def measuredPet(self):
        """The PET the ROIs were last measured on (kept with the scene), or None."""
        node = self.findRoiNode()
        pet = node.GetNodeReference(self._petReferenceRole) if node is not None else None
        return pet if pet is not None and pet.IsA("vtkMRMLScalarVolumeNode") else None

    def _createRoiNode(self):
        node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", f"{self.title} ROIs")
        self._tag(node, ROLE_POINTS)
        node.CreateDefaultDisplayNodes()
        self._styleRoiDisplayNode(node.GetDisplayNode())
        return node

    def _styleRoiDisplayNode(self, displayNode):
        if displayNode is None:
            return
        displayNode.SetSelectedColor(*self.pointColor)
        displayNode.SetColor(*self.pointColor)
        if hasattr(displayNode, "SetPropertiesLabelVisibility"):
            displayNode.SetPropertiesLabelVisibility(False)
        crossDot = getattr(slicer.vtkMRMLMarkupsDisplayNode, "CrossDot2D", None)
        if crossDot is not None:
            displayNode.SetGlyphType(crossDot)
        if hasattr(displayNode, "SetPointLabelsVisibility"):
            displayNode.SetPointLabelsVisibility(False)  # the text is drawn by the labels layer

    def connect(self):
        """Pick up this set's nodes in the scene (after a scene load or a module reload)."""
        node = self.findRoiNode()
        self.setRoiNode(node)
        self.setHandlesNode(self._find(ROLE_HANDLES))
        if node is not None:
            self._styleRoiDisplayNode(node.GetDisplayNode())  # scenes of older versions drew text on the point

    def setRoiNode(self, node):
        if node is self.roiNode:
            return
        if self.roiNode is not None:
            for event in _markupsEvents():
                self.removeObserver(self.roiNode, event, self._onPointsModified)
        self.roiNode = node
        self._statsCache = {}
        self._knownIDs = None
        self._lastGeometry = {}
        if node is not None:
            for event in _markupsEvents():
                self.addObserver(node, event, self._onPointsModified)

    def setHandlesNode(self, node):
        if node is self.handlesNode:
            return
        startEvent = getattr(slicer.vtkMRMLMarkupsNode, "PointStartInteractionEvent", None)
        endEvent = getattr(slicer.vtkMRMLMarkupsNode, "PointEndInteractionEvent", None)
        if self.handlesNode is not None:
            for event in _markupsEvents():
                self.removeObserver(self.handlesNode, event, self._onPointsModified)
            if startEvent is not None:
                self.removeObserver(self.handlesNode, startEvent, self._onHandleInteractionStarted)
            if endEvent is not None:
                self.removeObserver(self.handlesNode, endEvent, self._onHandleInteractionEnded)
        self.handlesNode = node
        self._lastHandlePositions = {}
        self._activeHandleID = None
        if node is not None:
            for event in _markupsEvents():
                self.addObserver(node, event, self._onPointsModified)
            if startEvent is not None:
                self.addObserver(node, startEvent, self._onHandleInteractionStarted)
            if endEvent is not None:
                self.addObserver(node, endEvent, self._onHandleInteractionEnded)

    def followTransform(self, transformNode):
        """Recompute when this transform (the PET's, e.g. the registration) changes: the ROIs move with it."""
        if transformNode is self._observedTransform:
            return
        if self._observedTransform is not None:
            self.removeObserver(self._observedTransform, slicer.vtkMRMLTransformNode.TransformModifiedEvent,
                                self._onPointsModified)
        self._observedTransform = transformNode
        if transformNode is not None:
            self.addObserver(transformNode, slicer.vtkMRMLTransformNode.TransformModifiedEvent, self._onPointsModified)

    def release(self):
        """Stop observing (panel cleanup / scene close). The nodes stay in the scene."""
        self.setRoiNode(None)
        self.setHandlesNode(None)
        self.followTransform(None)
        self.removeObservers()

    def _onPointsModified(self, caller=None, event=None):
        if not self._updating:
            self._onChanged()

    @vtk.calldata_type(vtk.VTK_INT)
    def _onHandleInteractionStarted(self, caller, event, index=None):
        node = self.handlesNode
        if node is None:
            return
        if not isinstance(index, int) or index < 0:
            displayNode = node.GetDisplayNode()
            index = displayNode.GetActiveControlPoint() if displayNode is not None else -1
        if 0 <= index < node.GetNumberOfControlPoints():
            self._activeHandleID = node.GetNthControlPointID(index)

    @vtk.calldata_type(vtk.VTK_INT)
    def _onHandleInteractionEnded(self, caller, event, index=None):
        self._activeHandleID = None
        self._onChanged()  # snap the released handle back onto its axis

    # --- per-ROI settings (stored on the points node, saved with the scene) ---------------------------

    @staticmethod
    def getRadius(node, pointID, defaultRadius):
        try:
            radius = float(node.GetAttribute(ROI_RADIUS_ATTRIBUTE + pointID))
            if radius > 0:
                return radius
        except (TypeError, ValueError):
            pass
        radius = float(defaultRadius)
        node.SetAttribute(ROI_RADIUS_ATTRIBUTE + pointID, f"{radius:g}")
        return radius

    @staticmethod
    def setRadius(node, pointID, radius):
        node.SetAttribute(ROI_RADIUS_ATTRIBUTE + pointID, f"{float(radius):g}")

    @staticmethod
    def getNumber(node, pointID):
        value = node.GetAttribute(ROI_NUMBER_ATTRIBUTE + pointID)
        if value and value.isdigit():
            return int(value)
        nextValue = node.GetAttribute(ROI_NEXT_NUMBER_ATTRIBUTE)
        number = int(nextValue) if (nextValue and nextValue.isdigit()) else 1
        node.SetAttribute(ROI_NUMBER_ATTRIBUTE + pointID, str(number))
        node.SetAttribute(ROI_NEXT_NUMBER_ATTRIBUTE, str(number + 1))
        return number

    @staticmethod
    def getColor(node, pointID):
        color = parseRoiColor(node.GetAttribute(ROI_COLOR_ATTRIBUTE + pointID))
        if color is None:
            text = " ".join(f"{c:.4f}" for c in randomRoiColor())
            node.SetAttribute(ROI_COLOR_ATTRIBUTE + pointID, text)
            color = parseRoiColor(text)  # the stored value, as every later call returns it
        return color

    @staticmethod
    def getThreshold(node, pointID, defaultThreshold):
        threshold = parseRoiThreshold(node.GetAttribute(ROI_THRESHOLD_ATTRIBUTE + pointID))
        if threshold is None:
            threshold = (defaultThreshold[0], float(defaultThreshold[1]))
            node.SetAttribute(ROI_THRESHOLD_ATTRIBUTE + pointID, formatRoiThreshold(*threshold))
        return threshold

    @staticmethod
    def setThreshold(node, pointID, mode, value):
        text = formatRoiThreshold(mode, value)
        if node.GetAttribute(ROI_THRESHOLD_ATTRIBUTE + pointID) != text:
            node.SetAttribute(ROI_THRESHOLD_ATTRIBUTE + pointID, text)

    @staticmethod
    def getOrigin(node, pointID):
        origin = node.GetAttribute(ROI_ORIGIN_ATTRIBUTE + pointID)
        if origin not in (ROI_ORIGIN_USER, ROI_ORIGIN_AI):
            origin = ROI_ORIGIN_USER
            node.SetAttribute(ROI_ORIGIN_ATTRIBUTE + pointID, origin)
        return origin

    # --- editing -----------------------------------------------------------------------------------------

    def prepareForPlacement(self, petNode):
        """The points node, ready to receive a new ROI (created on first use, under the PET's transform)."""
        node = self.roiNode
        if node is None or not slicer.mrmlScene.IsNodePresent(node):
            node = self.findRoiNode() or self._createRoiNode()
        self.setRoiNode(node)
        self._followPet(node, petNode)
        node.SetLocked(False)
        if node.GetDisplayNode() is not None:
            node.GetDisplayNode().SetVisibility(True)
            self._restrictToViews(node.GetDisplayNode())
        return node

    def addRoiAtWorld(self, positionWorld, petNode):
        node = self.prepareForPlacement(petNode)
        index = node.AddControlPoint([float(c) for c in positionWorld])
        node.SetNthControlPointPositionWorld(index, *[float(c) for c in positionWorld])  # also under a transform
        return node.GetNthControlPointID(index)

    def _followPet(self, node, petNode):
        if (self._followPetTransform and petNode is not None
                and node.GetTransformNodeID() != petNode.GetTransformNodeID()):
            node.SetAndObserveTransformNodeID(petNode.GetTransformNodeID())

    def removeRoi(self, pointID):
        node = self.roiNode
        if node is None or pointID is None:
            return
        index = node.GetNthControlPointIndexByID(pointID)
        if index >= 0:
            node.RemoveNthControlPoint(index)
        for prefix in PER_ROI_ATTRIBUTES:
            node.RemoveAttribute(prefix + pointID)

    def removeAllRois(self):
        node = self.roiNode
        if node is None:
            return
        node.RemoveAllControlPoints()
        for name in list(node.GetAttributeNames() or ()):
            if name.startswith(PER_ROI_ATTRIBUTES):
                node.RemoveAttribute(name)
        node.SetAttribute(ROI_NEXT_NUMBER_ATTRIBUTE, "1")

    def roiCount(self):
        """ROIs placed (defined points), also before the first update."""
        node = self.roiNode or self.findRoiNode()
        if node is None:
            return 0
        return sum(1 for index in range(node.GetNumberOfControlPoints()) if isControlPointDefined(node, index))

    # --- views -------------------------------------------------------------------------------------------

    def setViews(self, viewNodeIDs):
        """Show this set only in these views (slice and 3D view node IDs); [] = every view."""
        self.viewNodeIDs = [viewID for viewID in viewNodeIDs if viewID]
        for node in (self.roiNode, self.handlesNode, self._find(ROLE_LABELS), self._find(ROLE_SPHERES),
                     self._find(ROLE_SEGMENTS)):
            if node is not None and node.GetDisplayNode() is not None:
                self._restrictToViews(node.GetDisplayNode())

    def _restrictToViews(self, displayNode):
        if displayNode is None or self.viewNodeIDs is None:
            return
        current = [displayNode.GetNthViewNodeID(i) for i in range(displayNode.GetNumberOfViewNodeIDs())]
        if current != self.viewNodeIDs:
            displayNode.SetViewNodeIDs(self.viewNodeIDs)

    # --- update ------------------------------------------------------------------------------------------

    def update(self, petNode, defaultRadius, radiusRange, defaultThreshold, labelFields, unit="", selectedID=None):
        """
        Measure every ROI on petNode and refresh handles, spheres, labels and segments.
        Returns a dict: rows (pointID, name, radius, stats, threshold, origin), selectedID (a new or resized ROI
        becomes selected), newRoiCenter (jump the views there), colors {pointID: rgb},
        mipEntries (pointID, centre, text, color) and segmentationNode.
        """
        result = {"rows": [], "selectedID": selectedID, "newRoiCenter": None, "colors": {}, "mipEntries": [],
                  "segmentationNode": None}
        node = self.roiNode
        if node is not None and not slicer.mrmlScene.IsNodePresent(node):
            self.setRoiNode(None)
            node = None
        if node is None:
            node = self.findRoiNode()
            if node is not None:
                self.setRoiNode(node)
        if node is None:
            self._removeDerivedNodes()
            self.rows, self.centers = [], {}
            return result
        self._followPet(node, petNode)
        self.followTransform(petNode.GetParentTransformNode() if petNode is not None else None)
        self._restrictToViews(node.GetDisplayNode())

        rows, spheres, newCache = [], [], {}
        labelEntries, segmentEntries, mipEntries = [], [], []
        draggedID = None
        self._updating = True
        try:
            rois = []
            for index in range(node.GetNumberOfControlPoints()):
                if not isControlPointDefined(node, index):
                    continue  # e.g. the preview point that follows the mouse in place mode
                center = [0.0, 0.0, 0.0]
                node.GetNthControlPointPositionWorld(index, center)
                rois.append((index, node.GetNthControlPointID(index), center))

            draggedID = self._syncRadiusHandles(node, rois, defaultRadius, radiusRange)
            self.centers = {pointID: tuple(center) for _, pointID, center in rois}
            for index, pointID, center in rois:
                radius = self.getRadius(node, pointID, defaultRadius)
                name = f"ROI-{self.getNumber(node, pointID)}"
                color = self.getColor(node, pointID)
                result["colors"][pointID] = color
                threshold = self.getThreshold(node, pointID, defaultThreshold)
                origin = self.getOrigin(node, pointID)
                key = statsCacheKey(petNode, center, radius, *threshold)
                unchanged = key in self._statsCache
                stats = self._statsCache[key] if unchanged else computeSphereStatistics(petNode, center, radius,
                                                                                        *threshold)
                newCache[key] = stats
                if node.GetNthControlPointLabel(index) != name:
                    node.SetNthControlPointLabel(index, name)
                rows.append((pointID, name, radius, stats, threshold, origin))
                spheres.append((center, radius, color))
                labelText = formatRoiLabel(name, stats, petNode is not None, labelFields, radius, threshold, unit=unit)
                if labelText:
                    labelEntries.append((pointID, center, radius, labelText))
                mipEntries.append((pointID, center, labelText, color))
                segmentEntries.append((pointID, name, stats, unchanged, color))
            if petNode is not None and rows and node.GetNodeReferenceID(self._petReferenceRole) != petNode.GetID():
                node.SetNodeReferenceID(self._petReferenceRole, petNode.GetID())
        finally:
            self._updating = False
        self._statsCache = newCache

        currentIDs = {row[0] for row in rows}
        newIDs = currentIDs - self._knownIDs if self._knownIDs is not None else set()
        if len(newIDs) == 1:
            result["selectedID"] = next(iter(newIDs))
            result["newRoiCenter"] = self.centers.get(result["selectedID"])
        elif draggedID is not None:
            result["selectedID"] = draggedID
        self._knownIDs = currentIDs

        self._updateSphereModel(spheres)
        self._updateLabels(labelEntries)
        try:
            result["segmentationNode"] = self._updateSegments(segmentEntries, petNode)
        except Exception:
            logging.exception(f"Epona: could not update the {self.tag} ROI segments")
        self.rows = rows
        result["rows"] = rows
        result["mipEntries"] = mipEntries
        return result

    def _removeDerivedNodes(self):
        self.setHandlesNode(None)
        for role in (ROLE_HANDLES, ROLE_LABELS, ROLE_SPHERES, ROLE_SEGMENTS):
            node = self._find(role)
            if node is not None:
                slicer.mrmlScene.RemoveNode(node)

    # --- radius handles -----------------------------------------------------------------------------------

    def _styleHandlesDisplayNode(self, displayNode):
        if displayNode is None:
            return
        if displayNode.GetGlyphScale() != ROI_HANDLE_GLYPH_SCALE:
            displayNode.SetGlyphScale(ROI_HANDLE_GLYPH_SCALE)
        if tuple(displayNode.GetSelectedColor()) != (1.0, 0.85, 0.0):
            displayNode.SetSelectedColor(1.0, 0.85, 0.0)
            displayNode.SetColor(1.0, 0.85, 0.0)
        square = getattr(slicer.vtkMRMLMarkupsDisplayNode, "Square2D", None)
        if square is not None and displayNode.GetGlyphType() != square:
            displayNode.SetGlyphType(square)
        if hasattr(displayNode, "SetPointLabelsVisibility") and displayNode.GetPointLabelsVisibility():
            displayNode.SetPointLabelsVisibility(False)
        if hasattr(displayNode, "SetPropertiesLabelVisibility") and displayNode.GetPropertiesLabelVisibility():
            displayNode.SetPropertiesLabelVisibility(False)
        if hasattr(displayNode, "SetVisibility3D") and displayNode.GetVisibility3D():
            displayNode.SetVisibility3D(False)
        self._restrictToViews(displayNode)

    def _syncRadiusHandles(self, roiNode, rois, defaultRadius, radiusRange):
        """
        Six draggable handles per ROI (see HANDLE_DIRECTIONS); dragging one sets the radius to its distance from
        the centre. The handle under the mouse is never moved; it snaps onto its axis when released.
        Returns the ID of the ROI resized by a handle in this sync, if any.
        """
        handlesNode = self.handlesNode
        if handlesNode is not None and not slicer.mrmlScene.IsNodePresent(handlesNode):
            self.setHandlesNode(None)
            handlesNode = None
        if handlesNode is None:
            handlesNode = self._find(ROLE_HANDLES)
            if handlesNode is None and rois:
                handlesNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode",
                                                                 f"{self.title} ROI radius handles")
                self._tag(handlesNode, ROLE_HANDLES)
                if hasattr(handlesNode, "SetControlPointLabelFormat"):
                    handlesNode.SetControlPointLabelFormat("")
                handlesNode.CreateDefaultDisplayNodes()
            self.setHandlesNode(handlesNode)
        if handlesNode is None:
            self._lastGeometry = {}
            return None
        self._styleHandlesDisplayNode(handlesNode.GetDisplayNode())

        roiIDs = {pointID for _, pointID, _ in rois}
        seen = set()
        for i in reversed(range(handlesNode.GetNumberOfControlPoints())):
            key = parseHandleDescription(handlesNode.GetNthControlPointDescription(i))
            if key is None or key[0] not in roiIDs or key in seen:
                handlesNode.RemoveNthControlPoint(i)
            else:
                seen.add(key)
        indexByKey = {}
        for i in range(handlesNode.GetNumberOfControlPoints()):
            key = parseHandleDescription(handlesNode.GetNthControlPointDescription(i))
            if key is not None:
                indexByKey[key] = i

        minRadius, maxRadius = radiusRange
        draggedID = None
        newGeometry, newPositions = {}, {}
        for _, roiID, center in rois:
            radius = self.getRadius(roiNode, roiID, defaultRadius)
            handles = []
            for axis in range(len(HANDLE_DIRECTIONS)):
                index = indexByKey.get((roiID, axis))
                if index is None:
                    continue
                handleID = handlesNode.GetNthControlPointID(index)
                position = [0.0, 0.0, 0.0]
                handlesNode.GetNthControlPointPositionWorld(index, position)
                handles.append((handleID, position, self._lastHandlePositions.get(handleID)))
            draggedRadius, draggedHandleID = resolveHandleDrag(
                center, radius, self._lastGeometry.get(roiID), handles, self._activeHandleID)
            if draggedHandleID is not None:
                draggedRadius = round(min(max(draggedRadius, minRadius), maxRadius), 1)
                if abs(draggedRadius - radius) > 1e-6:
                    self.setRadius(roiNode, roiID, draggedRadius)
                    radius = draggedRadius
                draggedID = roiID
            for axis in range(len(HANDLE_DIRECTIONS)):
                target = radiusHandlePosition(center, radius, axis)
                index = indexByKey.get((roiID, axis))
                if index is None:
                    index = handlesNode.AddControlPoint(target)
                    handlesNode.SetNthControlPointLabel(index, "")
                    handlesNode.SetNthControlPointDescription(index, f"{roiID}:{axis}")
                    indexByKey[(roiID, axis)] = index
                handleID = handlesNode.GetNthControlPointID(index)
                position = [0.0, 0.0, 0.0]
                handlesNode.GetNthControlPointPositionWorld(index, position)
                if handleID != self._activeHandleID and _distance(position, target) > 1e-3:
                    handlesNode.SetNthControlPointPositionWorld(index, target[0], target[1], target[2])
                    position = target
                newPositions[handleID] = tuple(position)
            newGeometry[roiID] = (tuple(center), radius)
        self._lastGeometry = newGeometry
        self._lastHandlePositions = newPositions
        return draggedID

    # --- spheres, labels, segments -------------------------------------------------------------------------

    def _updateSphereModel(self, spheres):
        modelNode = self._find(ROLE_SPHERES)
        if not spheres:
            if modelNode is not None:
                modelNode.SetAndObservePolyData(vtk.vtkPolyData())
            return
        if modelNode is None:
            modelNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLModelNode", f"{self.title} ROI spheres")
            self._tag(modelNode, ROLE_SPHERES)
            modelNode.CreateDefaultDisplayNodes()
            displayNode = modelNode.GetDisplayNode()
            if hasattr(displayNode, "SetVisibility2D"):
                displayNode.SetVisibility2D(True)
                displayNode.SetVisibility3D(False)
            else:
                displayNode.SetSliceIntersectionVisibility(True)
        displayNode = modelNode.GetDisplayNode()
        if displayNode is not None:
            if displayNode.GetSliceIntersectionThickness() != ROI_SPHERE_OUTLINE_PX:
                displayNode.SetSliceIntersectionThickness(ROI_SPHERE_OUTLINE_PX)
            directMapping = getattr(slicer.vtkMRMLDisplayNode, "UseDirectMapping", None)
            if directMapping is not None:
                if displayNode.GetActiveScalarName() != ROI_SPHERE_COLOR_ARRAY:
                    displayNode.SetActiveScalar(ROI_SPHERE_COLOR_ARRAY, vtk.vtkAssignAttribute.POINT_DATA)
                if displayNode.GetScalarRangeFlag() != directMapping:
                    displayNode.SetScalarRangeFlag(directMapping)
                if not displayNode.GetScalarVisibility():
                    displayNode.SetScalarVisibility(True)
            self._restrictToViews(displayNode)
        append = vtk.vtkAppendPolyData()
        for center, radius, color in spheres:
            sphere = vtk.vtkSphereSource()
            sphere.SetCenter(center)
            sphere.SetRadius(radius)
            sphere.SetThetaResolution(48)
            sphere.SetPhiResolution(24)
            sphere.Update()
            polyData = vtk.vtkPolyData()
            polyData.DeepCopy(sphere.GetOutput())
            colors = vtk.vtkUnsignedCharArray()
            colors.SetName(ROI_SPHERE_COLOR_ARRAY)
            colors.SetNumberOfComponents(3)
            rgb = [int(round(255 * c)) for c in color]
            for _ in range(polyData.GetNumberOfPoints()):
                colors.InsertNextTuple3(*rgb)
            polyData.GetPointData().AddArray(colors)
            append.AddInputData(polyData)
        append.Update()
        merged = vtk.vtkPolyData()
        merged.DeepCopy(append.GetOutput())
        modelNode.SetAndObservePolyData(merged)

    def _styleLabelsDisplayNode(self, displayNode):
        if displayNode is None:
            return
        wasModifying = displayNode.StartModify()
        displayNode.SetColor(1.0, 1.0, 1.0)
        displayNode.SetSelectedColor(1.0, 1.0, 1.0)
        displayNode.SetTextScale(ROI_LABEL_TEXT_SCALE)
        dash = getattr(slicer.vtkMRMLMarkupsDisplayNode, "Dash2D", None)
        if dash is not None:
            displayNode.SetGlyphType(dash)
        displayNode.SetGlyphScale(0.2)
        if hasattr(displayNode, "SetPointLabelsVisibility"):
            displayNode.SetPointLabelsVisibility(True)
        if hasattr(displayNode, "SetPropertiesLabelVisibility"):
            displayNode.SetPropertiesLabelVisibility(False)
        if hasattr(displayNode, "SetVisibility3D"):
            displayNode.SetVisibility3D(False)
        textProperty = displayNode.GetTextProperty() if hasattr(displayNode, "GetTextProperty") else None
        if textProperty is not None:
            textProperty.SetBackgroundOpacity(0.0)
            textProperty.SetShadow(False)
            textProperty.SetFrame(False)
        displayNode.EndModify(wasModifying)
        self._restrictToViews(displayNode)

    def _updateLabels(self, entries):
        """entries: (roiPointID, centre, radius, text); three text anchors per ROI (one per slice orientation)."""
        labelsNode = self._find(ROLE_LABELS)
        if not entries:
            if labelsNode is not None and labelsNode.GetNumberOfControlPoints() > 0:
                labelsNode.RemoveAllControlPoints()
            return
        if labelsNode is None:
            labelsNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", f"{self.title} ROI labels")
            self._tag(labelsNode, ROLE_LABELS)
            labelsNode.SetLocked(True)
            if hasattr(labelsNode, "SetControlPointLabelFormat"):
                labelsNode.SetControlPointLabelFormat("")
            labelsNode.CreateDefaultDisplayNodes()
            self._styleLabelsDisplayNode(labelsNode.GetDisplayNode())
            labelsNode.SetAttribute(LABEL_STYLE_VERSION_ATTRIBUTE, LABEL_STYLE_VERSION)
        elif labelsNode.GetAttribute(LABEL_STYLE_VERSION_ATTRIBUTE) != LABEL_STYLE_VERSION:
            self._styleLabelsDisplayNode(labelsNode.GetDisplayNode())
            labelsNode.SetAttribute(LABEL_STYLE_VERSION_ATTRIBUTE, LABEL_STYLE_VERSION)
        else:
            self._restrictToViews(labelsNode.GetDisplayNode())
        planeCount = len(LABEL_ANCHOR_DIRECTIONS)
        wanted = {}
        for roiID, center, radius, text in entries:
            for plane in range(planeCount):
                wanted[(roiID, plane)] = (labelAnchorPosition(center, radius, plane), text)
        seen = set()
        for i in reversed(range(labelsNode.GetNumberOfControlPoints())):
            key = parseHandleDescription(labelsNode.GetNthControlPointDescription(i), planeCount)
            if key is None or key not in wanted or key in seen:
                labelsNode.RemoveNthControlPoint(i)
            else:
                seen.add(key)
        indexByKey = {}
        for i in range(labelsNode.GetNumberOfControlPoints()):
            key = parseHandleDescription(labelsNode.GetNthControlPointDescription(i), planeCount)
            if key is not None:
                indexByKey[key] = i
        for key, (position, text) in wanted.items():
            index = indexByKey.get(key)
            if index is None:
                index = labelsNode.AddControlPoint(position)
                labelsNode.SetNthControlPointDescription(index, f"{key[0]}:{key[1]}")
            else:
                current = [0.0, 0.0, 0.0]
                labelsNode.GetNthControlPointPositionWorld(index, current)
                if _distance(current, position) > 1e-3:
                    labelsNode.SetNthControlPointPositionWorld(index, position[0], position[1], position[2])
            if labelsNode.GetNthControlPointLabel(index) != text:
                labelsNode.SetNthControlPointLabel(index, text)

    def _styleSegmentationDisplayNode(self, displayNode):
        if displayNode is None:
            return
        wasModifying = displayNode.StartModify()
        displayNode.SetVisibility2DFill(False)
        displayNode.SetOpacity2DFill(0.0)
        displayNode.SetVisibility2DOutline(True)
        displayNode.SetOpacity2DOutline(1.0)
        displayNode.SetSliceIntersectionThickness(ROI_SEGMENT_OUTLINE_PX)
        displayNode.SetVisibility3D(False)
        displayNode.EndModify(wasModifying)
        self._restrictToViews(displayNode)

    def _updateSegments(self, entries, petNode):
        """entries: (roiPointID, name, stats, unchanged, color). One segment per ROI. Returns the segmentation."""
        segmentationNode = self._find(ROLE_SEGMENTS)
        if petNode is None or petNode.GetImageData() is None:
            return segmentationNode
        if not entries and segmentationNode is None:
            return None
        if segmentationNode is None:
            segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", f"{self.title} ROI segments")
            self._tag(segmentationNode, ROLE_SEGMENTS)
            segmentationNode.CreateDefaultDisplayNodes()
        self._styleSegmentationDisplayNode(segmentationNode.GetDisplayNode())
        if segmentationNode.GetNodeReferenceID(self._segmentationPetRole) != petNode.GetID():
            segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(petNode)
            segmentationNode.SetNodeReferenceID(self._segmentationPetRole, petNode.GetID())
        if segmentationNode.GetTransformNodeID() != petNode.GetTransformNodeID():
            segmentationNode.SetAndObserveTransformNodeID(petNode.GetTransformNodeID())
        segmentation = segmentationNode.GetSegmentation()
        wantedIDs = set()
        for roiID, name, stats, unchanged, color in entries:
            segmentID = ROI_SEGMENT_ID_PREFIX + roiID
            wantedIDs.add(segmentID)
            segment = segmentation.GetSegment(segmentID)
            if segment is None:
                segmentation.AddEmptySegment(segmentID, name, list(color))
                unchanged = False
            else:
                if segment.GetName() != name:
                    segment.SetName(name)
                if any(abs(a - b) > 1e-3 for a, b in zip(segment.GetColor(), color)):
                    segment.SetColor(*color)
            if unchanged:
                continue
            if stats is None:
                writeSegmentMask(segmentationNode, segmentID, petNode, (0, 0, 0, 0, 0, 0), np.zeros((1, 1, 1), bool))
            else:
                writeSegmentMask(segmentationNode, segmentID, petNode, stats["extent"], stats["segMask"])
        existingIDs = vtk.vtkStringArray()
        segmentation.GetSegmentIDs(existingIDs)
        for i in range(existingIDs.GetNumberOfValues()):
            segmentID = existingIDs.GetValue(i)
            if segmentID.startswith(ROI_SEGMENT_ID_PREFIX) and segmentID not in wantedIDs:
                segmentation.RemoveSegment(segmentID)
        return segmentationNode

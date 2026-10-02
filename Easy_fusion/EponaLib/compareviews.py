"""
Reading views of the compare module: what each view of each time point shows, the PET-only twins, the MIPs,
layout registration, fitting and the Monitor 2 window. No panel code here (see EponaCompare.py).

A time point is a (SPECT/PET, CT/MR) pair; time point 1 sits in the top row of every layout, time point 2 below.
"""

import logging

import qt
import slicer

from .layouts import (
    buildCompareLayoutDescriptions, COMPARE_DUAL_MONITOR_LAYOUT_IDS, COMPARE_DUAL_MONITOR_WINDOW_TITLE,
    COMPARE_LAYOUT_IDS, COMPARE_MIP_VIEWS, COMPARE_PET_ONLY_ATTRIBUTE, COMPARE_SLICE_VIEWS, compareViewName,
    hasMultipleScreens, isCompareView, PET_ONLY_SOURCE_ROLE, registerLayout, showOnSecondScreen,
)
from .display import fillHotIronTable, findColorNode, syncSliceViewSize
from .roiset import ROI_SET_ATTRIBUTE

HOT_IRON_NAME = "CustomHotIron"               # same color node as Epona (never two of them)
COMPARE_MIP_ATTRIBUTE = "EponaCompare.MIPDisplay"   # value "1" / "2": the MIP volume rendering of that time point
MIP_RAYCAST_TECHNIQUE = 2                      # vtkMRMLViewNode::MaximumIntensityProjection
MIP_FIT_HEIGHT_MM = 1200.0                     # both MIPs show this height (same scale for comparing)
FUSION_FOREGROUND_OPACITY = 0.5
MONITOR2_PLACE_DELAY_MS = 300
ANTERIOR_VIEW_AXIS = 3
_TWIN_VOLUME_IDS = {tp: f"vtkMRMLScalarVolumeNodeEponaComparePETOnly{tp}" for tp in (1, 2)}
_TWIN_DISPLAY_IDS = {tp: f"vtkMRMLScalarVolumeDisplayNodeEponaComparePETOnly{tp}" for tp in (1, 2)}


# ---------------------------------------------------------------------------
# Layouts
# ---------------------------------------------------------------------------

def registerCompareLayouts(singleScreenSafe=None):
    """Make the compare layouts known to Slicer (the dual monitor one becomes single-window on one screen)."""
    if singleScreenSafe is None:
        singleScreenSafe = not hasMultipleScreens()
    ok = True
    for layoutID, description in buildCompareLayoutDescriptions(singleScreenSafe).items():
        ok = registerLayout(layoutID, description) and ok
    return ok


def currentLayout():
    layoutManager = slicer.app.layoutManager()
    return layoutManager.layout if layoutManager is not None else None


def isCompareLayoutShown():
    return currentLayout() in COMPARE_LAYOUT_IDS


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def compareSliceNodes():
    """(name, slice node, slice composite node) of every compare slice view that exists in the scene."""
    scene = slicer.mrmlScene
    result = []
    for name in COMPARE_SLICE_VIEWS:
        sliceNode = scene.GetSingletonNode(name, "vtkMRMLSliceNode")
        compositeNode = scene.GetSingletonNode(name, "vtkMRMLSliceCompositeNode")
        if sliceNode is not None and compositeNode is not None:
            result.append((name, sliceNode, compositeNode))
    return result


def mipViewNode(timePoint):
    return slicer.mrmlScene.GetSingletonNode(compareViewName(timePoint, "MIP"), "vtkMRMLViewNode")


def timePointViewNodeIDs(timePoint, includeMip=False):
    """IDs of the slice (and optionally 3D) view nodes of one time point that exist."""
    ids = [sliceNode.GetID() for name, sliceNode, _ in compareSliceNodes() if COMPARE_SLICE_VIEWS[name][0] == timePoint]
    viewNode = mipViewNode(timePoint) if includeMip else None
    if viewNode is not None:
        ids.append(viewNode.GetID())
    return ids


def geometryGroups():
    """Slice nodes that move together: all axial views of both time points, all coronal views. TP2 fusion first."""
    groups = {}
    for name, sliceNode, _ in compareSliceNodes():
        timePoint, orientation, content = COMPARE_SLICE_VIEWS[name]
        rank = 0 if (timePoint == 2 and content == "fusion") else 1
        groups.setdefault(orientation, []).append((rank, name, sliceNode))
    return [[node for _, _, node in sorted(members, key=lambda m: (m[0], m[1]))] for members in groups.values()]


def _setOrientation(sliceNode, orientation):
    current = sliceNode.GetOrientation() if hasattr(sliceNode, "GetOrientation") else sliceNode.GetOrientationString()
    if current == orientation:
        return False
    if hasattr(sliceNode, "SetOrientation"):
        sliceNode.SetOrientation(orientation)
    else:
        getattr(sliceNode, f"SetOrientationTo{orientation}")()
    return True


def assignViews(timePoints, twins):
    """
    Fill every compare slice view: fusion = CT/MR + PET, ct = CT/MR only, pet = the PET-only twin.
    timePoints: {1: (pet, ct), 2: (pet, ct)}; twins: {1: twin, 2: twin}. Returns names of views that changed.
    """
    changed = []
    for name, sliceNode, compositeNode in compareSliceNodes():
        timePoint, orientation, content = COMPARE_SLICE_VIEWS[name]
        pet, ct = timePoints.get(timePoint, (None, None))
        if _setOrientation(sliceNode, orientation):
            changed.append(name)
        if content == "fusion":
            background, foreground = ct, pet
        elif content == "ct":
            background, foreground = ct, None
        else:
            background, foreground = twins.get(timePoint), None
        if background is None:
            continue
        wasModifying = compositeNode.StartModify()
        if compositeNode.GetLinkedControl():
            compositeNode.SetLinkedControl(False)  # Slicer's linking would copy volumes between the time points
        if compositeNode.GetBackgroundVolumeID() != background.GetID():
            compositeNode.SetBackgroundVolumeID(background.GetID())
            changed.append(name)
        foregroundID = foreground.GetID() if foreground is not None else None
        if compositeNode.GetForegroundVolumeID() != foregroundID:
            compositeNode.SetForegroundVolumeID(foregroundID)
            if foreground is not None:
                compositeNode.SetForegroundOpacity(FUSION_FOREGROUND_OPACITY)
        if compositeNode.GetLabelVolumeID():
            compositeNode.SetLabelVolumeID(None)
        compositeNode.EndModify(wasModifying)
    return changed


def repairViewSizes():
    """Compare views whose slice node disagrees with the view's real shape (stretched image): fix them."""
    layoutManager = slicer.app.layoutManager()
    if layoutManager is None:
        return
    for name, _, _ in compareSliceNodes():
        sliceWidget = layoutManager.sliceWidget(name)
        if sliceWidget is not None and sliceWidget.visible and syncSliceViewSize(sliceWidget):
            logging.info(f"EponaCompare: view {name} was stretched; its size / aspect ratio was corrected")


def fitReferenceViews():
    """Fit the time point 2 fusion views (the references of the geometry groups) to their volumes."""
    layoutManager = slicer.app.layoutManager()
    if layoutManager is None:
        return
    for group in geometryGroups():
        name = group[0].GetLayoutName()
        sliceWidget = layoutManager.sliceWidget(name)
        if sliceWidget is not None:
            sliceWidget.sliceLogic().FitSliceToAll()


# ---------------------------------------------------------------------------
# PET-only twins
# ---------------------------------------------------------------------------

def findTwin(timePoint):
    for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
        if node.GetAttribute(COMPARE_PET_ONLY_ATTRIBUTE) == str(timePoint):
            return node
    return None


def _addWithFixedID(node, nodeID):
    if slicer.mrmlScene.GetNodeByID(nodeID) is None:
        try:
            node.SetID(nodeID)
        except Exception:
            pass
    return slicer.mrmlScene.AddNode(node)


def _configureTwinDisplay(displayNode, petNode):
    petDisplayNode = petNode.GetDisplayNode()
    wasModifying = displayNode.StartModify()
    displayNode.SetAndObserveColorNodeID("vtkMRMLColorTableNodeInvertedGrey")
    if petDisplayNode is not None:
        displayNode.SetAutoWindowLevel(False)
        displayNode.SetWindow(petDisplayNode.GetWindow())
        displayNode.SetLevel(petDisplayNode.GetLevel())
        displayNode.SetInterpolate(petDisplayNode.GetInterpolate())
    displayNode.EndModify(wasModifying)


def getOrCreateTwin(timePoint, petNode):
    """
    PET-only views show a hidden twin volume sharing the PET's voxels (no copy) with its own inverted-grey
    display, because a volume has one color map and the fusion views use Hot Iron. Not saved with the scene.
    """
    if petNode is None or petNode.GetImageData() is None:
        return None
    node = findTwin(timePoint)
    name = f"{petNode.GetName()} (PET only)"
    if node is None:
        node = slicer.vtkMRMLScalarVolumeNode()
        node.SetAttribute(COMPARE_PET_ONLY_ATTRIBUTE, str(timePoint))
        node.SetHideFromEditors(True)
        node.SetSaveWithScene(False)
        node.SetName(name)
        node.CopyOrientation(petNode)
        node.SetAndObserveImageData(petNode.GetImageData())
        node.SetAndObserveTransformNodeID(petNode.GetTransformNodeID())
        node.SetNodeReferenceID(PET_ONLY_SOURCE_ROLE, petNode.GetID())
        displayNode = slicer.vtkMRMLScalarVolumeDisplayNode()
        displayNode.SetSaveWithScene(False)
        displayNode = _addWithFixedID(displayNode, _TWIN_DISPLAY_IDS[timePoint])
        _configureTwinDisplay(displayNode, petNode)
        node.SetAndObserveDisplayNodeID(displayNode.GetID())
        node = _addWithFixedID(node, _TWIN_VOLUME_IDS[timePoint])
    else:
        if node.GetName() != name:
            node.SetName(name)
        node.CopyOrientation(petNode)
        if node.GetImageData() is not petNode.GetImageData():
            node.SetAndObserveImageData(petNode.GetImageData())
        if node.GetTransformNodeID() != petNode.GetTransformNodeID():
            node.SetAndObserveTransformNodeID(petNode.GetTransformNodeID())
        node.SetNodeReferenceID(PET_ONLY_SOURCE_ROLE, petNode.GetID())
        if node.GetDisplayNode() is None:
            displayNode = slicer.vtkMRMLScalarVolumeDisplayNode()
            displayNode.SetSaveWithScene(False)
            displayNode = _addWithFixedID(displayNode, _TWIN_DISPLAY_IDS[timePoint])
            node.SetAndObserveDisplayNodeID(displayNode.GetID())
        _configureTwinDisplay(node.GetDisplayNode(), petNode)
    return node


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def hotIronColorNode():
    """Epona's Hot Iron color table (shared by name with Epona; created if missing)."""
    for node in slicer.util.getNodesByClass("vtkMRMLColorTableNode"):
        if node.GetName() == HOT_IRON_NAME:
            return node
    colorNode = slicer.vtkMRMLColorTableNode()
    colorNode.SetName(HOT_IRON_NAME)
    fillHotIronTable(colorNode)
    slicer.mrmlScene.AddNode(colorNode)
    return colorNode


def colorNodeByName(name):
    """Color node for a color map button: Hot Iron (shared with Epona) or one of Slicer's color tables."""
    if name == HOT_IRON_NAME:
        return hotIronColorNode()
    return findColorNode(name)  # by name among color nodes only (the Red slice view is also called "Red")


def setWindowRange(volumeNode, lower, upper):
    displayNode = volumeNode.GetDisplayNode() if volumeNode is not None else None
    if displayNode is None:
        return
    wasModifying = displayNode.StartModify()
    displayNode.SetAutoWindowLevel(False)
    displayNode.SetWindow(max(upper - lower, 1e-6))
    displayNode.SetLevel((upper + lower) / 2.0)
    displayNode.EndModify(wasModifying)


def windowRange(volumeNode):
    displayNode = volumeNode.GetDisplayNode() if volumeNode is not None else None
    if displayNode is None:
        return None
    return displayNode.GetLevel() - displayNode.GetWindow() / 2.0, displayNode.GetLevel() + displayNode.GetWindow() / 2.0


def volumeMaximum(volumeNode):
    imageData = volumeNode.GetImageData() if volumeNode is not None else None
    if imageData is None or imageData.GetNumberOfPoints() == 0:
        return 0.0
    return float(imageData.GetScalarRange()[1])


# ---------------------------------------------------------------------------
# MIPs (one 3D view per time point)
# ---------------------------------------------------------------------------

def mipDisplayNode(timePoint, petNode, create=False):
    """The compare MIP (volume rendering display node) of a time point's PET; other PETs' MIPs of it are hidden."""
    for vrNode in slicer.util.getNodesByClass("vtkMRMLVolumeRenderingDisplayNode"):
        if (vrNode.GetAttribute(COMPARE_MIP_ATTRIBUTE) == str(timePoint) and petNode is not None
                and vrNode.GetVolumeNodeID() != petNode.GetID() and vrNode.GetVisibility()):
            vrNode.SetVisibility(False)  # the MIP of the PET this time point showed before
    if petNode is None:
        return None
    for i in range(petNode.GetNumberOfDisplayNodes()):
        node = petNode.GetNthDisplayNode(i)
        if node is not None and node.IsA("vtkMRMLVolumeRenderingDisplayNode") \
                and node.GetAttribute(COMPARE_MIP_ATTRIBUTE) == str(timePoint):
            return node
    if not create:
        return None
    vrLogic = slicer.modules.volumerendering.logic()
    displayNode = vrLogic.CreateVolumeRenderingDisplayNode()
    displayNode.UnRegister(vrLogic)
    displayNode.SetAttribute(COMPARE_MIP_ATTRIBUTE, str(timePoint))
    slicer.mrmlScene.AddNode(displayNode)
    petNode.AddAndObserveDisplayNodeID(displayNode.GetID())
    vrLogic.UpdateDisplayNodeFromVolumeNode(displayNode, petNode)
    return displayNode


def setMipRange(vrDisplayNode, lower, upper):
    """MIP from white at lower to black at upper, every intensity opaque (as Epona's MIP)."""
    propertyNode = vrDisplayNode.GetVolumePropertyNode() if vrDisplayNode is not None else None
    if propertyNode is None:
        return
    colorFunction = propertyNode.GetVolumeProperty().GetRGBTransferFunction(0)
    colorFunction.RemoveAllPoints()
    colorFunction.AddRGBPoint(lower, 1.0, 1.0, 1.0)
    colorFunction.AddRGBPoint(upper, 0.0, 0.0, 0.0)
    opacityLower, opacityUpper = lower, upper
    volumeNode = vrDisplayNode.GetVolumeNode()
    imageData = volumeNode.GetImageData() if volumeNode is not None else None
    if imageData is not None and imageData.GetNumberOfPoints() > 0:
        dataMinimum, dataMaximum = imageData.GetScalarRange()
        opacityLower, opacityUpper = min(lower, dataMinimum), max(upper, dataMaximum)
    scalarOpacity = propertyNode.GetScalarOpacity()
    scalarOpacity.RemoveAllPoints()
    scalarOpacity.AddPoint(opacityLower, 1.0)
    if opacityUpper > opacityLower:
        scalarOpacity.AddPoint(opacityUpper, 1.0)
    propertyNode.Modified()
    vrDisplayNode.Modified()


def configureMipView(viewNode):
    wasModifying = viewNode.StartModify()
    viewNode.SetRaycastTechnique(MIP_RAYCAST_TECHNIQUE)
    viewNode.SetRenderMode(1)          # orthographic
    viewNode.SetBoxVisible(0)
    viewNode.SetAxisLabelsVisible(0)
    viewNode.SetBackgroundColor(1.0, 1.0, 1.0)
    viewNode.SetBackgroundColor2(1.0, 1.0, 1.0)
    viewNode.EndModify(wasModifying)


def showMip(timePoint, petNode, lower, upper):
    """Show a time point's PET as MIP in its own 3D view only. Returns the display node (None: no view yet)."""
    viewNode = mipViewNode(timePoint)
    vrDisplayNode = mipDisplayNode(timePoint, petNode, create=True)
    if vrDisplayNode is None:
        return None
    if viewNode is not None:
        configureMipView(viewNode)
        viewIDs = [vrDisplayNode.GetNthViewNodeID(i) for i in range(vrDisplayNode.GetNumberOfViewNodeIDs())]
        if viewIDs != [viewNode.GetID()]:
            wasModifying = vrDisplayNode.StartModify()
            vrDisplayNode.RemoveAllViewNodeIDs()
            vrDisplayNode.AddViewNodeID(viewNode.GetID())
            vrDisplayNode.EndModify(wasModifying)
    vrDisplayNode.SetVisibility(True)
    setMipRange(vrDisplayNode, lower, upper)
    return vrDisplayNode


def isolateMipViews():
    """
    Keep other renderings (volume renderings, models, segmentations shown in every view) out of the compare MIP
    views: a display node without view IDs is given the explicit list of all other views.
    """
    mipIDs = {node.GetID() for node in (mipViewNode(1), mipViewNode(2)) if node is not None}
    if not mipIDs:
        return
    others = [node.GetID() for className in ("vtkMRMLViewNode", "vtkMRMLSliceNode")
              for node in slicer.util.getNodesByClass(className) if node.GetID() not in mipIDs]
    for className in ("vtkMRMLVolumeRenderingDisplayNode", "vtkMRMLModelDisplayNode",
                      "vtkMRMLSegmentationDisplayNode", "vtkMRMLMarkupsDisplayNode"):
        for displayNode in slicer.util.getNodesByClass(className):
            if displayNode.GetAttribute(COMPARE_MIP_ATTRIBUTE):
                continue
            displayable = displayNode.GetDisplayableNode()
            if displayable is None or displayable.GetHideFromEditors() or displayable.GetAttribute(ROI_SET_ATTRIBUTE):
                continue  # compare ROI sets choose their own views
            current = [displayNode.GetNthViewNodeID(i) for i in range(displayNode.GetNumberOfViewNodeIDs())]
            if current and not (set(current) & mipIDs):
                continue
            remaining = [viewID for viewID in (current or others) if viewID not in mipIDs]
            if remaining:
                displayNode.SetViewNodeIDs(remaining)
            else:
                displayNode.SetVisibility(False)


def threeDWidget(timePoint):
    layoutManager = slicer.app.layoutManager()
    if layoutManager is None:
        return None
    tag = compareViewName(timePoint, "MIP")
    for index in range(layoutManager.threeDViewCount):
        widget = layoutManager.threeDWidget(index)
        viewNode = widget.mrmlViewNode() if widget is not None else None
        if viewNode is not None and viewNode.GetLayoutName() == tag:
            return widget
    return None


def fitMip(timePoint, centerWorld, rotateToAnterior=True):
    """Anterior MIP centred on centerWorld, MIP_FIT_HEIGHT_MM high (same for both time points)."""
    widget = threeDWidget(timePoint)
    if widget is None or centerWorld is None:
        return
    view = widget.threeDView()
    if rotateToAnterior:
        view.rotateToViewAxis(ANTERIOR_VIEW_AXIS)
    view.forceRender()
    renderer = view.renderWindow().GetRenderers().GetFirstRenderer()
    if renderer is None:
        return
    camera = renderer.GetActiveCamera()
    focalPoint, position = camera.GetFocalPoint(), camera.GetPosition()
    camera.SetFocalPoint(*centerWorld)
    camera.SetPosition(*[p + c - f for p, c, f in zip(position, centerWorld, focalPoint)])
    camera.SetParallelScale(MIP_FIT_HEIGHT_MM / 2.0)
    renderer.ResetCameraClippingRange()
    view.forceRender()


def rotateMips(axis):
    for timePoint in (1, 2):
        widget = threeDWidget(timePoint)
        if widget is not None:
            widget.threeDView().rotateToViewAxis(axis)


def setMipRotation(enabled, speedMs):
    """Spin both MIPs (or stop them)."""
    for timePoint in (1, 2):
        viewNode = mipViewNode(timePoint)
        if viewNode is None:
            continue
        wasModifying = viewNode.StartModify()
        viewNode.SetAnimationMs(int(speedMs))
        viewNode.SetAnimationMode(1 if enabled else 0)   # vtkMRMLViewNode Spin / Off
        viewNode.EndModify(wasModifying)


def volumeCenter(volumeNode):
    if volumeNode is None:
        return None
    bounds = [0.0] * 6
    volumeNode.GetRASBounds(bounds)
    if bounds[1] < bounds[0]:
        return None
    return [(bounds[0] + bounds[1]) / 2.0, (bounds[2] + bounds[3]) / 2.0, (bounds[4] + bounds[5]) / 2.0]


# ---------------------------------------------------------------------------
# Monitor 2 window (dual monitor layout)
# ---------------------------------------------------------------------------

def placeMonitor2Window():
    """Normal window buttons for the floating Monitor 2 window, maximized on the second screen."""
    if currentLayout() not in COMPARE_DUAL_MONITOR_LAYOUT_IDS:
        return
    try:
        candidates = []
        widget = threeDWidget(1)
        if widget is not None:
            candidates.append(widget.window())
        candidates += [w for w in qt.QApplication.topLevelWidgets()
                       if w.windowTitle == COMPARE_DUAL_MONITOR_WINDOW_TITLE]
        window = next((w for w in candidates if w is not None and not w.inherits("QMainWindow")
                       and not (w.inherits("QDockWidget") and not getattr(w, "floating", True))), None)
        if window is None:
            return
        showOnSecondScreen(window)   # changes only what is not so already (see there)
    except Exception:
        logging.exception("EponaCompare: could not place the Monitor 2 window; drag it to the second screen")


def schedulePlaceMonitor2Window():
    qt.QTimer.singleShot(MONITOR2_PLACE_DELAY_MS, placeMonitor2Window)


def isCompareViewNode(viewNode):
    return viewNode is not None and isCompareView(viewNode.GetLayoutName())


def isTimePointMipView(viewNode, timePoint):
    return viewNode is not None and viewNode.GetLayoutName() == compareViewName(timePoint, "MIP")


def compareMipTimePoints():
    return dict(COMPARE_MIP_VIEWS)

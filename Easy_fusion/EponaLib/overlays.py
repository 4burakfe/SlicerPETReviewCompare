"""Drawing on top of the views: window / level info in the slice views and ROI segments / values on the MIP."""

import logging

import qt
import vtk
import slicer

from .scene import (
    sceneIsBusy,
)

from .layouts import (
    COMPARE_PET_ONLY_ATTRIBUTE, PET_ONLY_SOURCE_ROLE, PET_ONLY_VOLUME_ATTRIBUTE,
)

from .values import (
    formatWindowInfoLine, looksLikeCT, petValueInfo, valueKindUnit,
    voxelUnitLabel,
)

from .roi import (
    ensureUnsmoothedClosedSurface, ROI_SEGMENT_ID_PREFIX,
)

# Slice view text. Slicer's own corner annotations (Data Probe) and EasyFusion's window info in the bottom-right corner
DATA_PROBE_ANNOTATIONS_SETTING = "DataProbe/sliceViewAnnotations.enabled"   # Slicer's setting, 1 / 0
# Slicer's per-corner switches ("active corners"): (attribute of the annotations object, its check box, setting)
DATA_PROBE_CORNERS = (
    ("topLeft", "topLeftCheckBox", "DataProbe/sliceViewAnnotations.topLeft"),
    ("topRight", "topRightCheckBox", "DataProbe/sliceViewAnnotations.topRight"),
    ("bottomLeft", "bottomLeftCheckBox", "DataProbe/sliceViewAnnotations.bottomLeft"),
)
DATA_PROBE_CORNER_INDEXES = (2, 3, 0)   # the same corners in the view's vtkCornerAnnotation
DATA_PROBE_FONT_SIZE_SETTING = "DataProbe/sliceViewAnnotations.fontSize"
WINDOW_INFO_CORNER = 1               # vtkCornerAnnotation: 0 lower left, 1 lower right, 2 upper left, 3 upper right
WINDOW_INFO_DEFAULT_FONT_SIZE = 14   # same default as Slicer's slice view annotations
WINDOW_INFO_UPDATE_MS = 40           # at most ~25 updates per second while window / level is being dragged
WINDOW_INFO_LIGHT_TEXT = (1.0, 1.0, 1.0)    # on CT / fusion views (dark background)
WINDOW_INFO_DARK_TEXT = (0.1, 0.1, 0.1)     # on PET-only views (inverted grey: white background)
MIP_OVERLAY_SEGMENT_OPACITY = 0.65
MIP_OVERLAY_TEXT_COLOR = (0.05, 0.05, 0.05)
MIP_OVERLAY_TEXT_BACKGROUND = (1.0, 1.0, 1.0)
MIP_OVERLAY_TEXT_BACKGROUND_OPACITY = 0.75
MIP_OVERLAY_FONT_SIZE = 14
MIP_OVERLAY_TEXT_OFFSET_PX = (14, 10)


class SliceWindowInfoOverlay:
    """
    Window / level of the CT and display range of the SPECT/PET in the bottom-right corner of every slice view.
    Each view lists the volumes it shows (foreground above background), so fusion views have two lines,
    CT-only and PET-only views one. Pure VTK text on the views: nothing is added to the scene or saved.

    It follows window / level changes from anywhere (presets, F5-F9, mouse drags, Volumes module) by
    observing the display nodes of the shown volumes, and view contents by observing the slice composite nodes.
    Slicer's own annotations (Data Probe) rewrite all four corners of the view's built-in corner annotation,
    so this uses a separate text actor of its own.
    """

    def __init__(self, volumesCallback, viewFilter=None, headerCallback=None):
        """
        volumesCallback() -> (SPECT/PET, CT/MRI) currently selected in the panel: a node, a list of nodes (e.g. one
        per time point) or None each.
        viewFilter(slice view name) -> True for the views this overlay writes in (default: all slice views).
        headerCallback(slice view name) -> first line of that view's text, e.g. the time point, or "".
        """
        self.enabled = True
        self._volumes = volumesCallback
        self._viewFilter = viewFilter
        self._header = headerCallback
        self._actors = {}         # slice view name -> (renderer, vtkCornerAnnotation)
        self._lastText = {}       # slice view name -> (text, color) last drawn
        self._observations = {}   # MRML node ID -> (node, observer tag)
        self._timer = qt.QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(WINDOW_INFO_UPDATE_MS)
        self._timer.connect("timeout()", self.update)

    def setEnabled(self, enabled):
        self.enabled = bool(enabled)
        if self.enabled:
            self.scheduleUpdate()
        else:
            self.clear()

    def scheduleUpdate(self, caller=None, event=None):
        if not self._timer.isActive():  # not restarted: keeps updating during a continuous drag
            self._timer.start()

    # --- helpers ---------------------------------------------------------

    def _sliceWidgets(self):
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return []
        widgets = []
        for name in layoutManager.sliceViewNames():
            if self._viewFilter is not None and not self._viewFilter(name):
                continue
            sliceWidget = layoutManager.sliceWidget(name)
            if sliceWidget is None or sliceWidget.sliceView() is None:
                continue
            compositeNode = sliceWidget.mrmlSliceCompositeNode()
            if compositeNode is None or not slicer.mrmlScene.IsNodePresent(compositeNode):
                continue  # widget left over from a previous layout / scene
            widgets.append((name, sliceWidget, compositeNode))
        return widgets

    @staticmethod
    def _fontSize():
        try:
            return int(qt.QSettings().value(DATA_PROBE_FONT_SIZE_SETTING, WINDOW_INFO_DEFAULT_FONT_SIZE))
        except (TypeError, ValueError):
            return WINDOW_INFO_DEFAULT_FONT_SIZE

    @staticmethod
    def _nodeIDs(nodes):
        """IDs of a node, a list of nodes or None."""
        if nodes is None:
            return set()
        if isinstance(nodes, (list, tuple, set)):
            return {node.GetID() for node in nodes if node is not None}
        return {nodes.GetID()}

    def _describe(self, volumeNode, petNode, ctNode):
        """Window info line of one shown volume, or None when it has no scalar display."""
        displayNode = volumeNode.GetDisplayNode() if volumeNode is not None else None
        if displayNode is None or not hasattr(displayNode, "GetWindow"):
            return None
        window, level = displayNode.GetWindow(), displayNode.GetLevel()
        if volumeNode.GetAttribute(PET_ONLY_VOLUME_ATTRIBUTE) or volumeNode.GetAttribute(COMPARE_PET_ONLY_ATTRIBUTE):
            # Inverted-grey twin of the PET: same window as the PET, named and labelled after it
            single = petNode if not isinstance(petNode, (list, tuple, set)) else None
            volumeNode = volumeNode.GetNodeReference(PET_ONLY_SOURCE_ROLE) or single or volumeNode
        imageData = volumeNode.GetImageData()
        scalarMinimum = imageData.GetScalarRange()[0] if imageData is not None and imageData.GetNumberOfPoints() else None
        unit = voxelUnitLabel(volumeNode)
        nodeID = volumeNode.GetID()
        if nodeID in self._nodeIDs(ctNode):
            kind = "CT" if scalarMinimum is None or looksLikeCT(scalarMinimum) else "MRI"
        elif nodeID in self._nodeIDs(petNode):
            info = petValueInfo(volumeNode)
            kind, unit = info.modality or "SPECT/PET", valueKindUnit(info.kind)
        elif unit == "SUV":  # another volume whose units say SUV (e.g. the original of a filtered PET)
            kind = "PET"
        elif scalarMinimum is not None and looksLikeCT(scalarMinimum):
            kind = "CT"
        else:
            name = volumeNode.GetName() or "Volume"
            kind = name if len(name) <= 20 else name[:19] + "…"
        return formatWindowInfoLine(kind, window, level, unit)

    def _viewContent(self, compositeNode, petNode, ctNode, name=None):
        """(text, text color) for one slice view."""
        scene = slicer.mrmlScene
        background = scene.GetNodeByID(compositeNode.GetBackgroundVolumeID() or "")
        foreground = scene.GetNodeByID(compositeNode.GetForegroundVolumeID() or "")
        lines = []
        if self._header is not None and name is not None:
            lines.append(self._header(name))
        if foreground is not None and compositeNode.GetForegroundOpacity() > 0.0:
            lines.append(self._describe(foreground, petNode, ctNode))
        if background is not None:
            lines.append(self._describe(background, petNode, ctNode))
        darkBackground = not (background is not None and (background.GetAttribute(PET_ONLY_VOLUME_ATTRIBUTE)
                                                           or background.GetAttribute(COMPARE_PET_ONLY_ATTRIBUTE)))
        color = WINDOW_INFO_LIGHT_TEXT if darkBackground else WINDOW_INFO_DARK_TEXT
        return "\n".join(line for line in lines if line), color

    def _actorFor(self, name, sliceView):
        renderWindow = sliceView.renderWindow()
        entry = self._actors.get(name)
        if entry is not None and renderWindow.HasRenderer(entry[0]):
            return entry[1]
        renderer = renderWindow.GetRenderers().GetFirstRenderer()
        if renderer is None:
            return None
        actor = vtk.vtkCornerAnnotation()
        actor.SetPickable(False)
        size = self._fontSize()
        actor.SetMaximumFontSize(size)
        actor.SetMinimumFontSize(size)
        actor.SetNonlinearFontScaleFactor(1)
        actor.GetTextProperty().SetFontFamilyToArial()
        renderer.AddViewProp(actor)
        self._actors[name] = (renderer, actor)
        self._lastText.pop(name, None)
        return actor

    def _syncObservations(self, nodes):
        """Observe exactly these MRML nodes (Modified event)."""
        wanted = {node.GetID(): node for node in nodes if node is not None and node.GetID()}
        for nodeID in list(self._observations):
            node, tag = self._observations[nodeID]
            if nodeID not in wanted or wanted[nodeID] is not node:
                node.RemoveObserver(tag)
                del self._observations[nodeID]
        for nodeID, node in wanted.items():
            if nodeID not in self._observations:
                tag = node.AddObserver(vtk.vtkCommand.ModifiedEvent, self.scheduleUpdate)
                self._observations[nodeID] = (node, tag)

    # --- update / clear ----------------------------------------------------

    def update(self):
        if not self.enabled:
            return
        if sceneIsBusy():
            self._timer.start()  # try again once the scene is idle
            return
        try:
            petNode, ctNode = self._volumes()
        except Exception:
            petNode, ctNode = None, None
        observed = []
        liveNames = set()
        for name, sliceWidget, compositeNode in self._sliceWidgets():
            liveNames.add(name)
            observed.append(compositeNode)
            for volumeID in (compositeNode.GetBackgroundVolumeID(), compositeNode.GetForegroundVolumeID()):
                volumeNode = slicer.mrmlScene.GetNodeByID(volumeID or "")
                if volumeNode is not None:
                    observed.append(volumeNode.GetDisplayNode())
            try:
                text, color = self._viewContent(compositeNode, petNode, ctNode, name)
            except Exception:
                logging.debug(f"EasyFusion: no window info for slice view {name}", exc_info=True)
                text, color = "", WINDOW_INFO_LIGHT_TEXT
            sliceView = sliceWidget.sliceView()
            actor = self._actorFor(name, sliceView)
            if actor is None or self._lastText.get(name) == (text, color):
                continue
            actor.SetText(WINDOW_INFO_CORNER, text)
            textProperty = actor.GetTextProperty()
            textProperty.SetColor(*color)
            textProperty.SetShadow(color == WINDOW_INFO_LIGHT_TEXT)  # a dark shadow only helps light text
            self._lastText[name] = (text, color)
            sliceView.scheduleRender()
        # Views that no longer exist: forget them (only Python references)
        for name in [name for name in self._actors if name not in liveNames]:
            renderer, actor = self._actors.pop(name)
            renderer.RemoveViewProp(actor)
            self._lastText.pop(name, None)
        self._syncObservations(observed)

    def clear(self):
        """Remove the text from every view and stop observing."""
        self._timer.stop()
        self._syncObservations([])
        widgets = {name: sliceWidget for name, sliceWidget, _ in self._sliceWidgets()}
        for name, (renderer, actor) in self._actors.items():
            renderer.RemoveViewProp(actor)
            if name in widgets:
                widgets[name].sliceView().scheduleRender()
        self._actors = {}
        self._lastText = {}


class MipRoiOverlay:
    """
    Segment surfaces and ROI text drawn on top of the MIP in every 3D view.
    Pure VTK in an extra render layer: nothing is added to the scene, so nothing is saved.
    Render windows are never kept: each update looks the views up again, so layout changes
    (views re-created, Monitor 2 window) only ever see renderers that belong to live windows.
    """

    def __init__(self, viewFilter=None):
        """viewFilter(3D view node) -> True for the views this overlay draws in (default: every 3D view)."""
        self.enabled = True
        self._viewFilter = viewFilter
        self._overlays = []   # [(renderer, props)] currently attached to live 3D views

    def setEnabled(self, enabled):
        self.enabled = bool(enabled)

    def _threeDViews(self):
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return []
        views = []
        for index in range(layoutManager.threeDViewCount):
            widget = layoutManager.threeDWidget(index)
            if widget is None or widget.threeDView() is None:
                continue
            if self._viewFilter is not None:
                viewNode = widget.mrmlViewNode()
                if viewNode is None or not self._viewFilter(viewNode):
                    continue
            views.append(widget.threeDView())
        return views

    def _overlayRenderer(self, threeDView):
        renderWindow = threeDView.renderWindow()
        for renderer, _ in self._overlays:
            if renderWindow.HasRenderer(renderer):
                return renderer
        mainRenderer = renderWindow.GetRenderers().GetFirstRenderer()
        if mainRenderer is None:
            return None
        renderer = vtk.vtkRenderer()
        layer = renderWindow.GetNumberOfLayers()
        renderWindow.SetNumberOfLayers(layer + 1)
        renderer.SetLayer(layer)
        renderer.SetInteractive(False)   # never picks or steals mouse interaction from the 3D view
        renderer.SetActiveCamera(mainRenderer.GetActiveCamera())
        renderWindow.AddRenderer(renderer)
        self._overlays.append((renderer, []))
        return renderer

    def _liveOverlays(self, views):
        """Forget overlays whose render window is gone (only Python references are dropped)."""
        windows = [view.renderWindow() for view in views]
        self._overlays = [(renderer, props) for renderer, props in self._overlays
                          if any(window.HasRenderer(renderer) for window in windows)]

    def clear(self):
        views = self._threeDViews()
        self._liveOverlays(views)
        for renderer, props in self._overlays:
            renderer.RemoveAllViewProps()
            props[:] = []
        for view in views:
            view.scheduleRender()

    @staticmethod
    def _segmentSurfaceWorld(segmentationNode, segmentID):
        polyData = vtk.vtkPolyData()
        if hasattr(segmentationNode, "GetClosedSurfaceRepresentation"):
            segmentationNode.GetClosedSurfaceRepresentation(segmentID, polyData)
        else:
            internal = segmentationNode.GetClosedSurfaceInternalRepresentation(segmentID)
            if internal is not None:
                polyData.DeepCopy(internal)
        if polyData.GetNumberOfPoints() == 0:
            return None
        transformNode = segmentationNode.GetParentTransformNode()
        if transformNode is None:
            return polyData
        toWorld = vtk.vtkGeneralTransform()
        transformNode.GetTransformToWorld(toWorld)
        transformFilter = vtk.vtkTransformPolyDataFilter()
        transformFilter.SetTransform(toWorld)
        transformFilter.SetInputData(polyData)
        transformFilter.Update()
        worldPolyData = vtk.vtkPolyData()
        worldPolyData.DeepCopy(transformFilter.GetOutput())
        return worldPolyData

    @staticmethod
    def _textActor(text, positionWorld):
        actor = vtk.vtkBillboardTextActor3D()
        actor.SetInput(text)
        actor.SetPosition(*positionWorld)
        actor.SetDisplayOffset(*MIP_OVERLAY_TEXT_OFFSET_PX)
        textProperty = actor.GetTextProperty()
        textProperty.SetFontSize(MIP_OVERLAY_FONT_SIZE)
        textProperty.SetColor(*MIP_OVERLAY_TEXT_COLOR)
        textProperty.SetBackgroundColor(*MIP_OVERLAY_TEXT_BACKGROUND)
        textProperty.SetBackgroundOpacity(MIP_OVERLAY_TEXT_BACKGROUND_OPACITY)
        textProperty.SetShadow(False)
        textProperty.SetBold(True)
        textProperty.SetJustificationToLeft()
        textProperty.SetVerticalJustificationToBottom()
        return actor

    def update(self, entries, segmentationNode):
        """
        entries: list of (roiPointID, centerWorld, text, color). Segment surfaces are taken from segmentationNode
        (segment ID = ROI_SEGMENT_ID_PREFIX + roiPointID); a ROI whose segment is empty only shows its text.
        """
        if not self.enabled or not entries or sceneIsBusy():
            self.clear()
            return
        views = self._threeDViews()
        self._liveOverlays(views)
        if not views:
            return

        surfaces = []
        if segmentationNode is not None and slicer.mrmlScene.IsNodePresent(segmentationNode):
            ensureUnsmoothedClosedSurface(segmentationNode)
            segmentation = segmentationNode.GetSegmentation()
            for roiID, _, _, color in entries:
                segmentID = ROI_SEGMENT_ID_PREFIX + roiID
                if segmentation.GetSegment(segmentID) is None:
                    continue
                polyData = self._segmentSurfaceWorld(segmentationNode, segmentID)
                if polyData is not None:
                    surfaces.append((polyData, color))

        for view in views:
            renderer = self._overlayRenderer(view)
            if renderer is None:
                continue
            props = next(props for r, props in self._overlays if r is renderer)
            renderer.RemoveAllViewProps()
            props[:] = []
            for polyData, color in surfaces:
                mapper = vtk.vtkPolyDataMapper()
                mapper.SetInputData(polyData)
                actor = vtk.vtkActor()
                actor.SetMapper(mapper)
                actor.GetProperty().SetColor(*color)
                actor.GetProperty().SetOpacity(MIP_OVERLAY_SEGMENT_OPACITY)
                renderer.AddViewProp(actor)
                props.append(actor)
            for _, center, text, _ in entries:
                if not text:
                    continue
                actor = self._textActor(text, center)
                renderer.AddViewProp(actor)
                props.append(actor)
            view.scheduleRender()

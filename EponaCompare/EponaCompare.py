import os
import sys
import logging
import traceback

import qt
import ctk
import vtk
import slicer
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin

# Shared Epona code (EponaLib): next to this file in an installed extension, in the Easy_fusion folder of the
# source tree (Additional module paths).
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
for _path in (os.path.join(os.path.dirname(_MODULE_DIR), "Easy_fusion"), _MODULE_DIR):
    if os.path.isdir(os.path.join(_path, "EponaLib")) and _path not in sys.path:
        sys.path.insert(0, _path)

from EponaLib.layouts import (  # noqa: E402
    COMPARE_DEFAULT_LAYOUT_ID, COMPARE_DUAL_MONITOR_LAYOUT_IDS, COMPARE_LAYOUT_IDS, COMPARE_SLICE_VIEWS,
    COMPARE_VIEW_COLORS, compareViewTimePoint, hasMultipleScreens, isCompareView, LAYOUT_COMPARE_AXIAL_CORONAL_ID,
    LAYOUT_COMPARE_AXIAL_CT_ID, LAYOUT_COMPARE_AXIAL_PET_ID, LAYOUT_COMPARE_DUAL_MONITOR_ID, LAYOUT_REGISTRATION_ID,
    REGISTRATION_VIEWS, registerLayout, registrationLayoutDescription,
)
from EponaLib import compareviews as views  # noqa: E402
from EponaLib.display import SliceGeometrySync, WindowLevelLink  # noqa: E402
from EponaLib.overlays import MipRoiOverlay, SliceWindowInfoOverlay  # noqa: E402
from EponaLib.roi import (  # noqa: E402
    DEFAULT_ABSOLUTE_THRESHOLD, DEFAULT_RELATIVE_THRESHOLD, DEFAULT_ROI_RADIUS_MM,
    defaultRoiExportFileName, formatRoiTableRow, formatRoiTableTsv, parseRoiLabelFields, ROI_LABEL_FIELDS,
    ROI_ORIGIN_AI, THRESHOLD_ABSOLUTE, THRESHOLD_RELATIVE,
)
from EponaLib.roiset import SphereRoiSet  # noqa: E402
from EponaLib.scene import runWhenSceneSettled, sceneIsBusy  # noqa: E402
from EponaLib.shortcuts import registerShortcut, unregisterShortcuts, viewUnderCursor  # noqa: E402
from EponaLib.studyinfo import imagingTime, volumeLabel  # noqa: E402
from EponaLib.toolbar import (  # noqa: E402
    CT_PRESET_ICONS, EponaToolBar, isToolbarEnabled, LAYOUT_ICONS, PERCENT_PRESET_ICONS, richToolTip,
    SELECTOR_MIN_CONTENTS, SUV_PRESET_ICONS,
)
from EponaLib.display import hotIronRGB  # noqa: E402
from EponaLib.registration import (  # noqa: E402
    REGISTRATION_CENTER_ATTRIBUTE, REGISTRATION_SHIFT_ATTRIBUTE, REGISTRATION_TRANSFORM_ATTRIBUTE,
    attachUnderTransform, formatShift, hasRotation, logRegistration, parseShift, registerTimePoints,
    setTransformTranslation, setTranslation, shiftSliderRange, transformChain, transformTranslation,
)
from EponaLib.values import (  # noqa: E402
    anatomicalLabel, functionalLabel, CT_WINDOW_PRESETS, initialPetRange, looksLikeCT, percentOfMaximum, PET_SUV_PRESETS, petValueInfo,
    SPECT_PERCENT_OF_MAX_PRESETS, VALUE_KIND_SUV, valueKindUnit, WINDOW_SHORTCUT_KEYS,
)

# Parameter node references (saved with the scene)
ROLE_TP1_PET = "TP1PET"
ROLE_TP1_CT = "TP1CT"
ROLE_TP2_PET = "TP2PET"
ROLE_TP2_CT = "TP2CT"
ROLE_REGISTRATION = "Registration"
VOLUME_ROLES = (ROLE_TP1_PET, ROLE_TP1_CT, ROLE_TP2_PET, ROLE_TP2_CT)
REGISTRATION_TRANSFORM_NAME = "TP1 to TP2 (Epona registration)"
# Registration check views: TP2 in grey, TP1 tinted on top at half opacity
REGISTRATION_FOREGROUND_OPACITY = 0.5
REGISTRATION_TINT_COLOR_IDS = ("vtkMRMLColorTableNodeMagenta", "vtkMRMLColorTableNodeGreen")
REGISTRATION_CT_WINDOW = (1500.0, 300.0)  # bone + body outline, so misalignment of both is easy to see
ORIGINAL_COLOR_ATTRIBUTE = "EponaCompare.ColorBeforeRegistrationCheck"
# Manual adjustment: R / A / S sliders show the total shift of time point 1; each covers +-50 mm around the
# automatic result (re-centred when the handles or the Transforms module move it further)
MANUAL_SLIDER_HALF_RANGE_MM = 50.0
MANUAL_STEP_MM = 0.5
MANUAL_PAGE_STEP_MM = 5.0
SHIFT_TOLERANCE_MM = 0.05

# Reading (after "Accept Registration")
MODULE_NAME = "EponaCompare"
PARAM_ACCEPTED = "Accepted"                    # parameter node: "1" once the registration was accepted
PARAM_COLOR_MAP = "ColorMap"
PARAM_LINK_WINDOWS = "LinkWindows"
PARAM_THRESHOLD_MODE = "ThresholdMode"
PARAM_RELATIVE_THRESHOLD = "RelativeThreshold"
PARAM_ABSOLUTE_THRESHOLD = "AbsoluteThreshold"
TIME_POINTS = (1, 2)
PET_ROLES = {1: ROLE_TP1_PET, 2: ROLE_TP2_PET}
CT_ROLES = {1: ROLE_TP1_CT, 2: ROLE_TP2_CT}
# (button text, color node name): same color maps as Epona
PET_COLOR_MAPS = [("Hot Iron", views.HOT_IRON_NAME), ("Inferno", "Inferno"), ("Rainbow-2", "PET-Rainbow2"),
                  ("PET-DICOM", "PET-DICOM"), ("Red", "Red"), ("Hot Metal Blue", "PET-HotMetalBlue")]
DEFAULT_CT_WINDOW = (400.0, 40.0)              # abdomen
COMPARE_DUAL_MONITOR_FIT_DELAY_MS = 900         # second fit of the views after the Monitor 2 window is placed
COMPARE_LAYOUT_BUTTONS = [
    (LAYOUT_COMPARE_AXIAL_CT_ID, "Axial Fusion | CT + MIP",
     "Rows: time point 1 (top), time point 2 (bottom).\nColumns: axial fusion | axial CT/MRI | MIP"),
    (LAYOUT_COMPARE_AXIAL_PET_ID, "Axial Fusion | PET + MIP",
     "Rows: time point 1 (top), time point 2 (bottom).\nColumns: axial fusion | axial PET (inverted grey) | MIP"),
    (LAYOUT_COMPARE_AXIAL_CORONAL_ID, "Axial | Coronal Fusion + MIP",
     "Rows: time point 1 (top), time point 2 (bottom).\nColumns: axial fusion | coronal fusion | MIP"),
    (LAYOUT_COMPARE_DUAL_MONITOR_ID, "Dual Monitor",
     "Monitor 1: axial CT/MRI | axial fusion | axial PET\nMonitor 2 (separate window): MIP | coronal fusion | "
     "coronal PET\nRows: time point 1 (top), time point 2 (bottom). Click again to bring Monitor 2 back."),
]
ROI_UPDATE_DELAY_MS = 30
SELECTOR_REFRESH_DELAY_MS = 150
ROI_POINT_COLORS = {1: (0.36, 0.61, 0.84), 2: (0.91, 0.59, 0.18)}   # time point colors (as the view bars)
# Module-level so a reloaded panel (or a scene loaded before the panel was opened) reuses them
_windowLinks = {}
_geometrySync = SliceGeometrySync()


class EponaCompare(ScriptedLoadableModule):
    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        parent.title = "Epona - SPECT/PET Compare"
        parent.categories = ["Nuclear Medicine"]
        parent.dependencies = ["Easy_fusion"]
        parent.contributors = ["Burak Demir, MD, FEBNM"]
        parent.helpText = """
        Reads two SPECT/PET studies of the same patient side by side (time point 1 and time point 2).
        <br><br><b>Registration</b>: select the SPECT/PET and CT/MRI of both time points and press Register.
        Time point 1 is moved onto time point 2 with a shift only (no rotation): first the top of the body is
        matched and the body is centred, then a fast mutual-information registration refines the shift on the
        CT/MR images. Time point 1's SPECT/PET moves with its CT/MR, so its own PET/CT alignment is kept.
        The result is shown as axial, coronal and sagittal CT fusions (time point 2 in grey, time point 1 tinted);
        fine-tune it with the knobs or move handles.
        <br><br><b>Accept Registration</b> opens the reading layouts: time point 1 in the top row, time point 2 in
        the bottom row (fusion, CT/MRI, PET-only and MIP views, and a dual monitor layout). Slice views of the same
        orientation move together, windows can be linked, and each time point has its own spherical ROIs (own
        segmentation and table). Volume names show the imaging date / time in parentheses.
        Keys over the compare views: F5-F9 windowing, Insert = ROI at the cursor for that time point.
        """
        parent.acknowledgementText = "This file was developed by Burak Demir."
        parent.icon = qt.QIcon(os.path.join(_MODULE_DIR, "Resources", "Icons", "EponaCompare.png"))
        # Layouts must be known before a scene saved in one of them is loaded
        slicer.app.connect("startupCompleted()", _onStartupCompleted)


def _onStartupCompleted():
    views.registerCompareLayouts()
    _installSceneObserver()


def _installSceneObserver():
    """Observe scene loads once (a reload of this file replaces the observer instead of adding a second one)."""
    oldTag = getattr(slicer.modules, "_eponaCompareSceneObserver", None)
    if isinstance(oldTag, int):
        slicer.mrmlScene.RemoveObserver(oldTag)
    tag = slicer.mrmlScene.AddObserver(slicer.mrmlScene.EndImportEvent, _onSceneEndImport)
    try:
        setattr(slicer.modules, "_eponaCompareSceneObserver", tag)
    except Exception:
        pass


def _onSceneEndImport(caller=None, event=None):
    runWhenSceneSettled(_restoreReadingAfterLoad)


def _restoreReadingAfterLoad():
    """A scene saved in a compare layout: rebuild the PET-only twins and view contents (twins are not saved)."""
    try:
        if not views.isCompareLayoutShown():
            return
        logic = EponaCompareLogic()
        if logic.isAccepted() and logic.timePointsComplete():
            logic.buildReading(logic.timePoints())
            qt.QTimer.singleShot(200, logic.fitViews)
            if views.currentLayout() in COMPARE_DUAL_MONITOR_LAYOUT_IDS:
                views.schedulePlaceMonitor2Window()
    except Exception:
        logging.exception("EponaCompare: could not restore the reading views after loading the scene")


class VolumeSelector:
    """A volume combo box showing "name (imaging date / time)" (qMRMLNodeComboBox cannot change its texts)."""

    def __init__(self, toolTip, onChanged):
        self.combo = qt.QComboBox()
        self.combo.setToolTip(toolTip)
        self.combo.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Fixed)
        self.combo.setSizeAdjustPolicy(qt.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.combo.setMinimumContentsLength(24)
        self._onChanged = onChanged
        self._refreshing = False
        self.combo.connect("currentIndexChanged(int)", self._onIndexChanged)
        self.refresh()

    def _onIndexChanged(self, index=None):
        if not self._refreshing:
            self._onChanged()

    def currentNodeID(self):
        return self.combo.itemData(self.combo.currentIndex) or ""

    def currentNode(self):
        nodeID = self.currentNodeID()
        return slicer.mrmlScene.GetNodeByID(nodeID) if nodeID else None

    def setCurrentNode(self, node):
        nodeID = node.GetID() if node is not None else ""
        index = self.combo.findData(nodeID) if nodeID else 0
        if index < 0:
            self.refresh()
            index = self.combo.findData(nodeID)
        if index >= 0 and index != self.combo.currentIndex:
            self.combo.setCurrentIndex(index)

    def refresh(self):
        """Rebuild the list (volumes added / removed / renamed); keeps the selection, reports if it disappeared."""
        currentID = self.currentNodeID()
        self._refreshing = True
        try:
            self.combo.clear()
            self.combo.addItem("(none)", "")
            for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
                if node.GetHideFromEditors() or node.IsA("vtkMRMLLabelMapVolumeNode"):
                    continue
                self.combo.addItem(volumeLabel(node), node.GetID())
            index = self.combo.findData(currentID) if currentID else 0
            self.combo.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self._refreshing = False
        if self.currentNodeID() != currentID:
            self._onChanged()


class EponaCompareWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):

    def __init__(self, parent=None):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)
        self.logic = None
        self._updatingSelectors = False
        self._updatingSliders = False
        self._observedTransform = None
        self.roiSets = {}
        self.roiTables = {}
        self.measuringLabels = {}
        self.exportButtons = {}
        self.mipOverlays = {}
        self._roiRows = {1: [], 2: []}
        self._roiUnits = {1: "", 2: ""}
        self._relativeThreshold = DEFAULT_RELATIVE_THRESHOLD
        self._absoluteThreshold = DEFAULT_ABSOLUTE_THRESHOLD
        self._observedMipViews = []
        self.toolbar = None              # top toolbar shown while the module is open (setupToolbar)
        self.toolbarSelectors = {}
        self._syncingToolbarSelectors = False

    def setup(self):
        ScriptedLoadableModuleWidget.setup(self)
        self.logic = EponaCompareLogic()

        timePointsButton = ctk.ctkCollapsibleButton()
        timePointsButton.text = "Time points"
        self.layout.addWidget(timePointsButton)
        form = qt.QFormLayout(timePointsButton)
        self.selectors = {}
        self.selectorLabels = {}
        for role, groupLabel, label, tip in (
                (ROLE_TP1_PET, "Time point 1 (moved onto time point 2)", "SPECT/PET:",
                 "SPECT/PET of time point 1. It moves together with its CT/MRI."),
                (ROLE_TP1_CT, None, "CT/MRI:", "CT/MRI of time point 1: registered to the CT/MRI of time point 2."),
                (ROLE_TP2_PET, "Time point 2 (reference, not moved)", "SPECT/PET:", "SPECT/PET of time point 2."),
                (ROLE_TP2_CT, None, "CT/MRI:", "CT/MRI of time point 2: the reference of the registration.")):
            if groupLabel:
                heading = qt.QLabel(f"<b>{groupLabel}</b>")
                form.addRow(heading)
            selector = VolumeSelector(tip + "\nThe imaging date / time is shown in parentheses when it is known.",
                                      lambda role=role: self.onSelectorChanged(role))
            # The label follows the selected volume: PET / SPECT, CT / MRI (as written here when not known)
            labelWidget = qt.QLabel(label)
            form.addRow(labelWidget, selector.combo)
            self.selectors[role] = selector
            self.selectorLabels[role] = labelWidget
        self.toolbarButton = qt.QPushButton("Hide Toolbar" if isToolbarEnabled() else "Show Toolbar")
        self.toolbarButton.setToolTip(
            "Row of small buttons at the top of the window while this module is open: volumes, window presets,\n"
            "color maps, layouts, MIP rotation and quick views, hiding this panel. Shared with Epona.")
        self.toolbarButton.connect("clicked()", self.onToolbarButtonClicked)
        form.addRow(self.toolbarButton)
        self.selectorRefreshTimer = qt.QTimer()
        self.selectorRefreshTimer.setSingleShot(True)
        self.selectorRefreshTimer.setInterval(SELECTOR_REFRESH_DELAY_MS)
        self.selectorRefreshTimer.connect("timeout()", self.refreshSelectors)

        registrationButton = ctk.ctkCollapsibleButton()
        registrationButton.text = "Registration (time point 1 → time point 2)"
        self.layout.addWidget(registrationButton)
        registrationForm = qt.QFormLayout(registrationButton)
        info = qt.QLabel("Shift only (no rotation): top of the body and body centre first, then a fast refinement "
                         "on the CT/MRI. Time point 1's SPECT/PET moves with its CT/MRI.")
        info.wordWrap = True
        info.setStyleSheet("color: gray;")
        registrationForm.addRow(info)
        buttons = qt.QHBoxLayout()
        self.registerButton = qt.QPushButton("Register")
        self.registerButton.setToolTip("Register time point 1 to time point 2 and show the CT/MRI fusions "
                                       "(axial | coronal | sagittal).")
        self.showRegistrationButton = qt.QPushButton("Show Check Views")
        self.showRegistrationButton.setToolTip("Show the axial | coronal | sagittal CT/MRI fusions again.")
        self.resetRegistrationButton = qt.QPushButton("Reset")
        self.resetRegistrationButton.setToolTip("Time point 1 back to its original position (the automatic result "
                                                "is kept: 'Back to automatic' restores it).")
        for button in (self.registerButton, self.showRegistrationButton, self.resetRegistrationButton):
            buttons.addWidget(button)
        registrationForm.addRow(buttons)
        self.registrationStatusLabel = qt.QLabel("")
        self.registrationStatusLabel.wordWrap = True
        registrationForm.addRow(self.registrationStatusLabel)

        # Manual adjustment: R / A / S knobs, move handles in the views, overlay opacity
        manualBox = qt.QGroupBox("Manual adjustment")
        manualForm = qt.QFormLayout(manualBox)
        self.shiftSliders = {}
        for axis, label, tip in (("R", "Right (+) / left:", "Shift of time point 1 towards the patient's right (+)."),
                                 ("A", "Anterior (+) / posterior:", "Shift of time point 1 towards anterior (+)."),
                                 ("S", "Superior (+) / inferior:", "Shift of time point 1 towards superior (+).")):
            slider = ctk.ctkSliderWidget()
            slider.decimals = 1
            slider.singleStep = MANUAL_STEP_MM
            slider.pageStep = MANUAL_PAGE_STEP_MM
            slider.suffix = " mm"
            slider.minimum = -MANUAL_SLIDER_HALF_RANGE_MM
            slider.maximum = MANUAL_SLIDER_HALF_RANGE_MM
            slider.value = 0.0
            slider.setToolTip(f"{tip}\nTotal shift in mm. Drag, or use the arrows / mouse wheel of the number for "
                              f"{MANUAL_STEP_MM:g} mm steps. The slider covers +-{MANUAL_SLIDER_HALF_RANGE_MM:g} mm "
                              "around the automatic result.")
            slider.connect("valueChanged(double)", self.onManualShiftChanged)
            manualForm.addRow(label, slider)
            self.shiftSliders[axis] = slider
        handleRow = qt.QHBoxLayout()
        self.handlesCheckBox = qt.QCheckBox("Move handles in the views")
        self.handlesCheckBox.setToolTip("Drag handles on time point 1 in the slice views (shift only: rotation and "
                                        "scaling handles are switched off). The knobs follow.")
        self.backToAutomaticButton = qt.QPushButton("Back to automatic")
        self.backToAutomaticButton.setToolTip("Undo the manual changes: back to the result of the last Register.")
        handleRow.addWidget(self.handlesCheckBox)
        handleRow.addWidget(self.backToAutomaticButton)
        manualForm.addRow(handleRow)
        self.overlayOpacitySlider = ctk.ctkSliderWidget()
        self.overlayOpacitySlider.minimum = 0.0
        self.overlayOpacitySlider.maximum = 1.0
        self.overlayOpacitySlider.singleStep = 0.05
        self.overlayOpacitySlider.decimals = 2
        self.overlayOpacitySlider.value = REGISTRATION_FOREGROUND_OPACITY
        self.overlayOpacitySlider.setToolTip("Opacity of time point 1 over time point 2 in the check views. Move it "
                                             "back and forth to see edges (skull, spine, liver dome) shift.")
        manualForm.addRow("Time point 1 opacity:", self.overlayOpacitySlider)
        self.rotationWarningLabel = qt.QLabel("⚠ The transform also contains a rotation (set in the Transforms "
                                              "module). Reset removes it.")
        self.rotationWarningLabel.wordWrap = True
        self.rotationWarningLabel.setStyleSheet("color: #d9822b;")
        self.rotationWarningLabel.visible = False
        manualForm.addRow(self.rotationWarningLabel)
        registrationForm.addRow(manualBox)

        self.handlesCheckBox.connect("toggled(bool)", self.onHandlesToggled)
        self.backToAutomaticButton.connect("clicked()", self.onBackToAutomatic)
        self.overlayOpacitySlider.connect("valueChanged(double)", self.onOverlayOpacityChanged)
        self.acceptButton = qt.QPushButton("Accept Registration → Compare")
        self.acceptButton.setToolTip("Keep this alignment and open the reading layouts of both time points.")
        self.acceptButton.setStyleSheet("font-weight: bold;")
        registrationForm.addRow(self.acceptButton)
        self.registrationCollapsibleButton = registrationButton

        self.registerButton.connect("clicked()", self.onRegister)
        self.showRegistrationButton.connect("clicked()", self.onShowRegistrationViews)
        self.resetRegistrationButton.connect("clicked()", self.onResetRegistration)
        self.acceptButton.connect("clicked()", self.onAccept)

        self.setupReadingSection()
        self.setupMeasurementSection()

        self.layout.addStretch(1)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndImportEvent, self.onSceneEndImport)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.NodeAddedEvent, self.onSceneNodesChanged)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.NodeRemovedEvent, self.onSceneNodesChanged)
        if slicer.app.layoutManager() is not None:
            slicer.app.layoutManager().connect("layoutChanged(int)", self.onLayoutChanged)
        views.registerCompareLayouts()
        _installSceneObserver()
        for key in WINDOW_SHORTCUT_KEYS:
            registerShortcut(MODULE_NAME, getattr(qt.Qt, f"Key_{key}"), lambda key=key: self.onWindowShortcut(key),
                             predicate=self.cursorOverCompareView)
        registerShortcut(MODULE_NAME, qt.Qt.Key_Insert, self.onPlaceRoiAtCursor, predicate=self.cursorOverCompareView)
        self.restoreSelections()
        self.connectRoiSets()
        self.setupToolbar()
        if self.logic.isAccepted() and views.isCompareLayoutShown() and self.logic.timePointsComplete():
            self.refreshReading(fit=False)

    def cleanup(self):
        if self.toolbar is not None:
            try:
                self.toolbar.destroy()
            except Exception:
                logging.exception("EponaCompare: could not remove the toolbar")
            self.toolbar = None
        unregisterShortcuts(MODULE_NAME)
        for timer in ("roiUpdateTimer", "selectorRefreshTimer"):
            if hasattr(self, timer):
                getattr(self, timer).stop()
        for overlay in self.mipOverlays.values():
            try:
                overlay.clear()
            except Exception:
                logging.exception("EponaCompare: could not remove a MIP overlay")
        try:
            self.windowInfoOverlay.enabled = False
            self.windowInfoOverlay.clear()
        except Exception:
            logging.exception("EponaCompare: could not remove the window info")
        for roiSet in self.roiSets.values():
            roiSet.release()
        if slicer.app.layoutManager() is not None:
            slicer.app.layoutManager().disconnect("layoutChanged(int)", self.onLayoutChanged)
        self.removeObservers()

    def enter(self):
        self.refreshSelectors()
        self.restoreSelections()
        self.onLayoutChanged()
        if self.toolbar is not None:
            try:
                self.syncToolbar()
            except Exception:
                logging.exception("EponaCompare: could not update the toolbar")
            self.toolbar.enter()   # always: the toolbar must know the module is shown
        self.updateToolbarButton()

    def exit(self):
        if self.toolbar is not None:
            self.toolbar.exit()   # hidden, and the module panel comes back if the toolbar had hidden it

    def onReload(self):
        """Developer mode "Reload": reload the shared EponaLib modules first, then this module."""
        import EponaLib
        EponaLib.reloadAll()
        ScriptedLoadableModuleWidget.onReload(self)

    # --- Selections (kept in the parameter node, so they are saved with the scene) ------------------------

    def restoreSelections(self):
        parameterNode = self.logic.getParameterNode()
        self._updatingSelectors = True
        try:
            for role, selector in self.selectors.items():
                node = parameterNode.GetNodeReference(role)
                if node is not None and selector.currentNode() is not node:
                    selector.setCurrentNode(node)
        finally:
            self._updatingSelectors = False
        self.syncToolbarSelectors()
        self.updateSelectorLabels()
        self.observeRegistrationTransform()
        self.syncManualControls()
        self.updateRegistrationStatus()

    def onSceneEndImport(self, caller=None, event=None):
        runWhenSceneSettled(self.onSceneLoaded)

    def onSceneLoaded(self):
        self.refreshSelectors()
        self.restoreSelections()
        self.restoreReadingControls()
        self.connectRoiSets()
        self.bindReadingObservers()
        self.onLayoutChanged()
        self.scheduleRoiUpdate()

    def onSceneNodesChanged(self, caller=None, event=None):
        if hasattr(self, "selectorRefreshTimer"):
            self.selectorRefreshTimer.start()

    def refreshSelectors(self):
        if sceneIsBusy():
            self.selectorRefreshTimer.start()
            return
        self._updatingSelectors = True
        try:
            for selector in self.selectors.values():
                selector.refresh()
        finally:
            self._updatingSelectors = False
        self.syncToolbarSelectors(refresh=True)
        self.updateSelectorLabels()
        self.updateTimePointLabels()

    def onSceneEndClose(self, caller=None, event=None):
        for roiSet in self.roiSets.values():
            roiSet.release()
        for overlay in self.mipOverlays.values():
            overlay.clear()
        for tp in TIME_POINTS:
            self.fillRoiTable(tp, [], {}, None)
        _geometrySync.clear()
        for link in _windowLinks.values():
            link.unbind()
        self.windowInfoOverlay.scheduleUpdate()
        self.refreshSelectors()
        self.observeRegistrationTransform()  # the transform is gone: stop observing it
        wasBlocked = self.handlesCheckBox.blockSignals(True)
        self.handlesCheckBox.checked = False
        self.handlesCheckBox.blockSignals(wasBlocked)
        self.syncManualControls()
        self.registrationStatusLabel.text = ""

    def selected(self, role):
        return self.selectors[role].currentNode()

    def onSelectorChanged(self, role):
        if self._updatingSelectors:
            return
        self.syncToolbarSelectors()
        self.updateSelectorLabels()
        node = self.selected(role)
        self.logic.getParameterNode().SetNodeReferenceID(role, node.GetID() if node is not None else None)
        if role == ROLE_TP1_PET and node is not None:
            # A newly chosen time point 1 SPECT/PET follows an existing registration, like its CT/MRI
            try:
                if self.logic.attachToRegistration(node, self.selected(ROLE_TP1_CT), self.fixedVolumes()):
                    slicer.util.showStatusMessage(
                        f"EponaCompare: '{node.GetName()}' follows the time point 1 registration", 4000)
            except ValueError as error:
                slicer.util.warningDisplay(str(error))
        self.updateRegistrationStatus()
        self.updateTimePointLabels()
        if self.logic.isAccepted() and self.logic.timePointsComplete() and views.isCompareLayoutShown():
            # Another volume for a time point after accepting: the reading views follow right away
            timePoint = 1 if role in (ROLE_TP1_PET, ROLE_TP1_CT) else 2
            self.logic.initialDisplay(timePoint, self.colorMapName())
            self.refreshReading(fit=False)

    def fixedVolumes(self):
        return [self.selected(ROLE_TP2_CT), self.selected(ROLE_TP2_PET)]

    # --- Registration ---------------------------------------------------------------------------------

    def _checkRegistrationInputs(self):
        tp1Ct, tp2Ct = self.selected(ROLE_TP1_CT), self.selected(ROLE_TP2_CT)
        if tp1Ct is None or tp2Ct is None:
            slicer.util.warningDisplay("Select the CT/MRI of both time points first.")
            return None
        chosen = [node for node in (self.selected(role) for role in VOLUME_ROLES) if node is not None]
        if len(set(node.GetID() for node in chosen)) != len(chosen):
            slicer.util.warningDisplay("The same volume is selected more than once. Select four different volumes.")
            return None
        return tp1Ct, tp2Ct

    def onRegister(self):
        inputs = self._checkRegistrationInputs()
        if inputs is None:
            return
        tp1Ct, tp2Ct = inputs
        progress = qt.QProgressDialog(slicer.util.mainWindow())
        progress.setWindowTitle("EponaCompare - registration")
        progress.setWindowModality(qt.Qt.ApplicationModal)
        progress.setCancelButton(None)
        progress.setRange(0, 0)
        progress.setMinimumDuration(0)
        progress.setMinimumWidth(380)
        progress.setLabelText("Preparing…")
        progress.show()
        slicer.app.processEvents()

        def report(text):
            progress.setLabelText(text)
            slicer.app.processEvents()

        try:
            result = self.logic.register(tp1Ct, self.selected(ROLE_TP1_PET), tp2Ct, self.fixedVolumes(), report)
        except ValueError as error:
            progress.close()
            slicer.util.warningDisplay(str(error), windowTitle="EponaCompare - registration")
            self.updateRegistrationStatus()
            return
        except Exception as error:
            progress.close()
            logging.exception("EponaCompare: registration failed")
            slicer.util.errorDisplay(f"Registration failed:\n{error}", detailedText=traceback.format_exc())
            self.updateRegistrationStatus()
            return
        progress.close()
        self.updateRegistrationStatus(result)
        slicer.util.showStatusMessage(f"EponaCompare: time point 1 moved {formatShift(result['shift'])}", 6000)

    def onShowRegistrationViews(self):
        inputs = self._checkRegistrationInputs()
        if inputs is not None:
            self.logic.showRegistrationViews(inputs[0], inputs[1])
            self.logic.centerRegistrationViews(self.logic.checkCenter(inputs[1]))

    def onResetRegistration(self):
        if self.logic.resetRegistration():
            slicer.util.showStatusMessage("EponaCompare: time point 1 is back in its original position", 4000)
        self.updateRegistrationStatus()

    # --- Manual adjustment ------------------------------------------------------------------------------

    def observeRegistrationTransform(self):
        """Follow the registration transform, whoever changes it (knobs, handles, Transforms module, Register)."""
        node = self.logic.registrationTransform()
        if node is self._observedTransform:
            return
        if self._observedTransform is not None:
            self.removeObserver(self._observedTransform, slicer.vtkMRMLTransformNode.TransformModifiedEvent,
                                self.onRegistrationTransformModified)
        self._observedTransform = node
        if node is not None:
            self.addObserver(node, slicer.vtkMRMLTransformNode.TransformModifiedEvent,
                             self.onRegistrationTransformModified)

    def onRegistrationTransformModified(self, caller=None, event=None):
        self.syncManualControls()
        self.updateRegistrationStatus()

    def syncManualControls(self):
        """Knobs show the current shift; their range is centred on the automatic result."""
        transformNode = self.logic.registrationTransform()
        shift = transformTranslation(transformNode) if transformNode is not None else (0.0, 0.0, 0.0)
        automatic = parseShift(transformNode.GetAttribute(REGISTRATION_SHIFT_ATTRIBUTE)) \
            if transformNode is not None else None
        anchors = automatic if automatic is not None else (0.0, 0.0, 0.0)
        self._updatingSliders = True
        try:
            for axis, value, anchor in zip("RAS", shift, anchors):
                slider = self.shiftSliders[axis]
                minimum, maximum = shiftSliderRange(float(value), float(round(anchor)), MANUAL_SLIDER_HALF_RANGE_MM)
                if slider.minimum != minimum or slider.maximum != maximum:
                    slider.minimum, slider.maximum = minimum, maximum
                if abs(slider.value - value) > 1e-6:
                    slider.value = float(value)
        finally:
            self._updatingSliders = False
        self.backToAutomaticButton.enabled = automatic is not None and not all(
            abs(a - b) <= SHIFT_TOLERANCE_MM for a, b in zip(shift, automatic))
        self.rotationWarningLabel.visible = transformNode is not None and hasRotation(transformNode)

    def _manualTransform(self):
        """The registration transform with time point 1 under it; set up on first use. None if not possible."""
        transformNode = self.logic.registrationTransform()
        tp1Ct = self.selected(ROLE_TP1_CT)
        if transformNode is not None and tp1Ct is not None and transformNode in transformChain(tp1Ct):
            return transformNode
        inputs = self._checkRegistrationInputs()
        if inputs is None:
            return None
        try:
            transformNode = self.logic.prepareTransform(inputs[0], self.selected(ROLE_TP1_PET), self.fixedVolumes())
        except ValueError as error:
            slicer.util.warningDisplay(str(error))
            return None
        self.observeRegistrationTransform()
        self.logic.showRegistrationViews(inputs[0], inputs[1])
        self.logic.centerRegistrationViews(self.logic.checkCenter(inputs[1]))
        return transformNode

    def onManualShiftChanged(self, value=None):
        if self._updatingSliders:
            return
        transformNode = self._manualTransform()
        if transformNode is None:
            self.syncManualControls()  # knobs back to where time point 1 really is
            return
        setTransformTranslation(transformNode, [self.shiftSliders[axis].value for axis in "RAS"])

    def onHandlesToggled(self, checked):
        transformNode = self._manualTransform() if checked else self.logic.registrationTransform()
        if transformNode is None:
            wasBlocked = self.handlesCheckBox.blockSignals(True)
            self.handlesCheckBox.checked = False
            self.handlesCheckBox.blockSignals(wasBlocked)
            return
        self.logic.setHandlesVisible(transformNode, checked, self.selected(ROLE_TP2_CT))

    def onBackToAutomatic(self):
        if self.logic.backToAutomatic():
            slicer.util.showStatusMessage("EponaCompare: manual changes undone (automatic registration)", 4000)

    def onOverlayOpacityChanged(self, value):
        self.logic.setOverlayOpacity(value)

    def updateRegistrationStatus(self, result=None):
        transformNode = self.logic.registrationTransform()
        moved = False
        if result is not None:
            steps = (f"initial alignment {formatShift(result['initialShift'])}, refined to "
                     f"{formatShift(result['shift'])}" if result["refined"] else
                     f"initial alignment {formatShift(result['initialShift'])} (refinement did not improve it)")
            text = (f"Time point 1 moved {formatShift(result['shift'])} in {result['seconds']:.1f} s: {steps}. "
                    "Check the fusions and fine-tune with the knobs or move handles below if needed.")
            moved = True
        elif transformNode is None:
            text = "Not registered yet."
        else:
            shift = transformTranslation(transformNode)
            automatic = parseShift(transformNode.GetAttribute(REGISTRATION_SHIFT_ATTRIBUTE))
            moved = any(abs(v) > SHIFT_TOLERANCE_MM for v in shift)
            if automatic is not None and all(abs(a - b) <= SHIFT_TOLERANCE_MM for a, b in zip(shift, automatic)):
                text = f"Registered: time point 1 moved {formatShift(shift)} (automatic result)."
            elif automatic is not None and moved:
                text = (f"Time point 1 moved {formatShift(shift)}: automatic {formatShift(automatic)}, adjusted "
                        f"by hand by {formatShift(shift - automatic)}.")
            elif moved:
                text = f"Time point 1 moved {formatShift(shift)} (by hand)."
            else:
                text = "Time point 1 is in its original position." + (
                    " 'Back to automatic' restores the last registration." if automatic is not None else "")
        self.registrationStatusLabel.text = text
        self.resetRegistrationButton.enabled = transformNode is not None and (moved or hasRotation(transformNode))


    # ------------------------------------------------------------------------------------------------------
    # Reading (after "Accept Registration")
    # ------------------------------------------------------------------------------------------------------

    def _buttonRow(self, buttonSpecs, toolTip=None):
        """Horizontal row of push buttons from (text, callback) or (text, callback, tooltip) tuples."""
        rowLayout = qt.QHBoxLayout()
        for spec in buttonSpecs:
            button = qt.QPushButton(spec[0])
            buttonToolTip = spec[2] if len(spec) > 2 else toolTip
            if buttonToolTip:
                button.setToolTip(buttonToolTip)
            button.connect("clicked()", spec[1])
            rowLayout.addWidget(button)
            self._panelButtons.append(button)  # parentless until the layout is added: keep a Python reference
        return rowLayout

    def setupReadingSection(self):
        self._panelButtons = []
        button = ctk.ctkCollapsibleButton()
        button.text = "Reading (both time points)"
        self.layout.addWidget(button)
        self.readingCollapsibleButton = button
        form = qt.QFormLayout(button)
        self.readingHintLabel = qt.QLabel("Accept the registration to open the reading layouts. Time point 1 is "
                                          "shown in the top row, time point 2 in the bottom row.")
        self.readingHintLabel.wordWrap = True
        self.readingHintLabel.setStyleSheet("color: gray;")
        form.addRow(self.readingHintLabel)

        grid = qt.QGridLayout()
        self.layoutButtonGroup = qt.QButtonGroup()
        self.layoutButtonGroup.setExclusive(True)
        self.layoutButtons = {}
        for index, (layoutID, text, tip) in enumerate(COMPARE_LAYOUT_BUTTONS):
            layoutButton = qt.QPushButton(text)
            layoutButton.checkable = True
            layoutButton.setToolTip(tip)
            layoutButton.connect("clicked()", lambda layoutID=layoutID: self.setCompareLayout(layoutID))
            self.layoutButtonGroup.addButton(layoutButton)
            grid.addWidget(layoutButton, index // 2, index % 2)
            self.layoutButtons[layoutID] = layoutButton
        form.addRow("Layout:", grid)

        form.addRow("PET color map:", self._buttonRow(
            [(text, lambda name=name: self.onColorMap(name)) for text, name in PET_COLOR_MAPS[:3]]))
        form.addRow("", self._buttonRow(
            [(text, lambda name=name: self.onColorMap(name)) for text, name in PET_COLOR_MAPS[3:]]))
        form.addRow("CT presets:", self._buttonRow(
            [(text.replace("CT: ", ""), lambda window=window, level=level: self.onCtPreset(window, level),
              f"Both time points. Shortcut: {key} with the mouse over a compare CT view")
             for text, window, level, key in CT_WINDOW_PRESETS]))
        form.addRow("PET presets (SUV):", self._buttonRow(
            [(text, lambda upper=upper: self.onSuvPreset(upper),
              f"Both time points. Shortcut: {key} over a compare fusion, PET or MIP view (SUV volumes)")
             for text, upper, key in PET_SUV_PRESETS]))
        form.addRow("SPECT presets (% max):", self._buttonRow(
            [(f"0–{percent}%", lambda percent=percent: self.onPercentPreset(percent))
             for percent in SPECT_PERCENT_OF_MAX_PRESETS],
            toolTip="Each time point from 0 to this percentage of its own maximum."))
        self.linkWindowsCheckBox = qt.QCheckBox("Same window for both time points")
        self.linkWindowsCheckBox.checked = True
        self.linkWindowsCheckBox.setToolTip(
            "CT/MRI: both time points always show the same window.\n"
            "SPECT/PET: the same window when both are in SUV (a fixed scale); otherwise each time point keeps its\n"
            "own window (e.g. % of its own maximum).")
        form.addRow(self.linkWindowsCheckBox)

        rotationRow = qt.QHBoxLayout()
        self.rotationButton = qt.QPushButton("Start MIP Rotation")
        self.rotationButton.checkable = True
        self.rotationSpeedSlider = ctk.ctkSliderWidget()
        self.rotationSpeedSlider.singleStep = 10
        self.rotationSpeedSlider.minimum = 10
        self.rotationSpeedSlider.maximum = 200
        self.rotationSpeedSlider.value = 50
        self.rotationSpeedSlider.toolTip = "Lower is faster (ms per step)"
        rotationRow.addWidget(self.rotationButton)
        rotationRow.addWidget(self.rotationSpeedSlider)
        form.addRow("MIPs:", rotationRow)
        form.addRow("Quick view:", self._buttonRow([("Anterior", lambda: self.onQuickView(3)),
                                                    ("Left", lambda: self.onQuickView(0)),
                                                    ("Right", lambda: self.onQuickView(1))]))
        self.windowInfoCheckBox = qt.QCheckBox("Window info and time point in the views")
        self.windowInfoCheckBox.checked = True
        self.windowInfoCheckBox.setToolTip("Bottom-right corner of each compare view: time point (imaging date), "
                                           "CT/MRI window and SPECT/PET range.")
        form.addRow(self.windowInfoCheckBox)

        self.windowInfoOverlay = SliceWindowInfoOverlay(self.readingVolumes, viewFilter=isCompareView,
                                                        headerCallback=self.viewHeader)
        for timePoint in TIME_POINTS:
            self.mipOverlays[timePoint] = MipRoiOverlay(
                viewFilter=lambda viewNode, timePoint=timePoint: views.isTimePointMipView(viewNode, timePoint))

        self.linkWindowsCheckBox.connect("toggled(bool)", self.onLinkWindowsToggled)
        self.rotationButton.connect("toggled(bool)", self.onRotationToggled)
        self.rotationSpeedSlider.connect("valueChanged(double)", self.onRotationSpeedChanged)
        self.windowInfoCheckBox.connect("toggled(bool)", self.windowInfoOverlay.setEnabled)

    # --- toolbar (top of the window while the module is open) -----------------------------------------------

    def setupToolbar(self):
        """The basic reading functions as small picture buttons. Registration and ROIs stay in the panel."""
        toolbar = EponaToolBar("EponaCompareToolBar", "Epona Compare", moduleName=MODULE_NAME,
                               onEnabledChanged=self.onToolbarEnabledChanged)
        if not toolbar.build():
            return
        self.toolbar = toolbar

        # Copies of the panel's volume selectors (time point colors as in the views)
        for timePoint, roles in ((1, (ROLE_TP1_PET, ROLE_TP1_CT)), (2, (ROLE_TP2_PET, ROLE_TP2_CT))):
            toolbar.addLabel(f"TP{timePoint}", f"Time point {timePoint}", color=COMPARE_VIEW_COLORS[timePoint])
            for role, kind in zip(roles, ("SPECT/PET", "CT/MRI")):
                selector = VolumeSelector(f"Time point {timePoint} {kind} (same as in the module panel). The imaging "
                                          "date / time is in parentheses.",
                                          lambda role=role: self.onToolbarSelectorChanged(role))
                selector.combo.setSizePolicy(qt.QSizePolicy.Preferred, qt.QSizePolicy.Fixed)
                selector.combo.setMinimumContentsLength(SELECTOR_MIN_CONTENTS)
                selector.combo.setFocusPolicy(qt.Qt.ClickFocus)
                toolbar.addWidget(selector.combo)
                self.toolbarSelectors[role] = selector

        # Window presets (both time points): CT presets for CTs; SUV presets when both are in SUV, else % of maximum
        toolbar.addSeparator()
        for text, window, level, key in CT_WINDOW_PRESETS:
            toolbar.addPresetButton(CT_PRESET_ICONS[text],
                                    richToolTip(text, f"W {window:g}  L {level:g}, both time points\nShortcut: {key} "
                                                "over a compare CT view"),
                                    lambda window=window, level=level: self.onCtPreset(window, level), group="ct")
        toolbar.addSeparator(group="ct")
        for text, upper, key in PET_SUV_PRESETS:
            toolbar.addPresetButton(SUV_PRESET_ICONS[upper],
                                    richToolTip(f"SUV {text}", f"Both time points\nShortcut: {key} over a compare "
                                                "fusion, PET or MIP view"),
                                    lambda upper=upper: self.onSuvPreset(upper), group="suv")
        for percent in SPECT_PERCENT_OF_MAX_PRESETS:
            toolbar.addPresetButton(PERCENT_PRESET_ICONS[percent],
                                    richToolTip(f"0–{percent}% of maximum", "Each time point from 0 to this percentage "
                                                "of its own maximum"),
                                    lambda percent=percent: self.onPercentPreset(percent), group="percent")

        toolbar.addSeparator()
        for text, name in PET_COLOR_MAPS:
            toolbar.addColorButton(name, richToolTip(text, "SPECT/PET color map, both time points"),
                                   lambda name=name: self.onColorMap(name),
                                   rgbFunction=hotIronRGB if name == views.HOT_IRON_NAME else None)

        toolbar.addSeparator()
        toolbar.addLayoutButtons([(layoutID, LAYOUT_ICONS[layoutID], richToolTip(text, tip))
                                  for layoutID, text, tip in COMPARE_LAYOUT_BUTTONS], self.setCompareLayout)

        toolbar.addSeparator()
        toolbar.addRotationButton(self.onRotationToggled)
        for letter, axis, name in (("A", 3, "Anterior"), ("L", 0, "Left"), ("R", 1, "Right")):
            toolbar.addTextButton(letter, richToolTip(f"MIPs: {name} view", "Both time points; stops the rotation"),
                                  lambda axis=axis: self.onQuickView(axis))
        toolbar.addWindowButtons()
        self.syncToolbar()

    def updateSelectorLabels(self):
        """Selector labels (and toolbar tooltips): PET / SPECT and CT / MRI as detected."""
        if not hasattr(self, "selectorLabels") or sceneIsBusy():
            return
        for role, labelWidget in self.selectorLabels.items():
            node = self.selectors[role].currentNode()
            try:
                kind = functionalLabel(node) if role in PET_ROLES.values() else anatomicalLabel(node)
            except Exception:
                logging.debug("EponaCompare: could not tell the image type", exc_info=True)
                continue
            labelWidget.text = f"{kind}:"
            toolbarSelector = self.toolbarSelectors.get(role)
            if toolbarSelector is not None:
                timePoint = 1 if role in (ROLE_TP1_PET, ROLE_TP1_CT) else 2
                toolbarSelector.combo.setToolTip(f"Time point {timePoint} {kind} (same as in the module panel). "
                                                 "The imaging date / time is in parentheses.")

    def onToolbarSelectorChanged(self, role):
        if self._syncingToolbarSelectors:
            return
        node = self.toolbarSelectors[role].currentNode()
        if self.selectors[role].currentNode() is not node:
            self.selectors[role].setCurrentNode(node)   # the panel's own handler runs, as for a choice there

    def syncToolbarSelectors(self, refresh=False):
        if self.toolbar is None:
            return
        self._syncingToolbarSelectors = True
        try:
            for role, selector in self.toolbarSelectors.items():
                if refresh:
                    selector.refresh()
                node = self.selectors[role].currentNode()
                if selector.currentNode() is not node:
                    selector.setCurrentNode(node)
        finally:
            self._syncingToolbarSelectors = False
        self.updateToolbarPresetGroups()

    def updateToolbarPresetGroups(self):
        if self.toolbar is None:
            return
        cts = [self.ct(tp) for tp in TIME_POINTS]
        ranges = [ct.GetImageData().GetScalarRange() for ct in cts
                  if ct is not None and ct.GetImageData() is not None and ct.GetImageData().GetNumberOfPoints()]
        self.toolbar.setGroupVisible("ct", all(looksLikeCT(scalarRange[0]) for scalarRange in ranges))
        bothSuv = self.bothSuv()
        self.toolbar.setGroupVisible("suv", bothSuv)
        self.toolbar.setGroupVisible("percent", not bothSuv)

    def syncToolbar(self):
        if self.toolbar is None:
            return
        self.syncToolbarSelectors(refresh=True)
        self.toolbar.setCurrentLayout(views.currentLayout())
        self.toolbar.setRotating(any(viewNode.GetAnimationMode() != 0 for viewNode, _ in self._observedMipViews))

    def onToolbarButtonClicked(self):
        if self.toolbar is None:
            slicer.util.warningDisplay("The toolbar could not be created.")
            return
        self.toolbar.setEnabled(not isToolbarEnabled())
        self.updateToolbarButton()

    def onToolbarEnabledChanged(self, enabled=None):
        """The toolbar's own hide button: the panel's button follows."""
        self.updateToolbarButton()

    def updateToolbarButton(self):
        if hasattr(self, "toolbarButton"):
            self.toolbarButton.text = "Hide Toolbar" if isToolbarEnabled() else "Show Toolbar"

    # --- time points ---------------------------------------------------------------------------------------

    def pet(self, timePoint):
        return self.selected(PET_ROLES[timePoint])

    def ct(self, timePoint):
        return self.selected(CT_ROLES[timePoint])

    def timePoints(self):
        return {timePoint: (self.pet(timePoint), self.ct(timePoint)) for timePoint in TIME_POINTS}

    def readingVolumes(self):
        """(SPECT/PETs, CT/MRs) of both time points, for the window info."""
        return ([self.pet(tp) for tp in TIME_POINTS], [self.ct(tp) for tp in TIME_POINTS])

    def timePointLabel(self, timePoint):
        """ "Time point 1 (2026-03-14 10:32)": the imaging time of its SPECT/PET (else its CT/MRI)."""
        time = imagingTime(self.pet(timePoint)) or imagingTime(self.ct(timePoint))
        return f"Time point {timePoint} ({time})" if time else f"Time point {timePoint}"

    def viewHeader(self, viewName):
        timePoint = compareViewTimePoint(viewName)
        return self.timePointLabel(timePoint) if timePoint else ""

    def updateTimePointLabels(self):
        if not hasattr(self, "roiTabs"):
            return
        for timePoint in TIME_POINTS:
            self.roiTabs.setTabText(timePoint - 1, self.timePointLabel(timePoint))
        self.windowInfoOverlay.scheduleUpdate()

    def cursorOverCompareView(self):
        view = viewUnderCursor()
        return view is not None and isCompareView(view[1])

    def colorMapName(self):
        return self.logic.getParameterNode().GetParameter(PARAM_COLOR_MAP) or views.HOT_IRON_NAME

    # --- accept / build --------------------------------------------------------------------------------------

    def onAccept(self):
        timePoints = self.timePoints()
        missing = [f"time point {tp} {kind}" for tp, (pet, ct) in timePoints.items()
                   for kind, node in (("SPECT/PET", pet), ("CT/MRI", ct)) if node is None]
        if missing:
            slicer.util.warningDisplay("Select " + ", ".join(missing) + " first.")
            return
        if self._checkRegistrationInputs() is None:
            return
        try:
            self.logic.acceptRegistration(timePoints, self.fixedVolumes())
        except ValueError as error:
            slicer.util.warningDisplay(str(error))
            return
        for timePoint in TIME_POINTS:
            self.logic.initialDisplay(timePoint, self.colorMapName())
        self.readingCollapsibleButton.collapsed = False
        self.measurementCollapsibleButton.collapsed = False
        self.registrationCollapsibleButton.collapsed = True
        wasBlocked = self.handlesCheckBox.blockSignals(True)
        self.handlesCheckBox.checked = False
        self.handlesCheckBox.blockSignals(wasBlocked)
        transformNode = self.logic.registrationTransform()
        if transformNode is not None:
            self.logic.setHandlesVisible(transformNode, False)
        layoutID = views.currentLayout()
        self.setCompareLayout(layoutID if layoutID in COMPARE_LAYOUT_IDS else COMPARE_DEFAULT_LAYOUT_ID)
        slicer.util.showStatusMessage("EponaCompare: registration accepted; time point 1 top row, time point 2 "
                                      "bottom row", 5000)

    def setCompareLayout(self, layoutID):
        if not self.logic.isAccepted():
            slicer.util.warningDisplay("Accept the registration first (Registration section).")
            self.onLayoutChanged()
            return
        if not self.logic.timePointsComplete():
            slicer.util.warningDisplay("Select the SPECT/PET and CT/MRI of both time points.")
            self.onLayoutChanged()
            return
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return
        views.registerCompareLayouts()
        if layoutID in COMPARE_DUAL_MONITOR_LAYOUT_IDS and not hasMultipleScreens():
            slicer.util.showStatusMessage("EponaCompare: one screen only; Monitor 2 opens as a separate window", 5000)
        layoutManager.setLayout(layoutID)
        self.refreshReading(fit=True)
        if layoutID in COMPARE_DUAL_MONITOR_LAYOUT_IDS:
            views.schedulePlaceMonitor2Window()

    def refreshReading(self, fit=False):
        """Fill the compare views, links, MIPs and ROI views (after Accept, a layout change or new volumes)."""
        if sceneIsBusy() or not self.logic.isAccepted() or not self.logic.timePointsComplete():
            return
        try:
            self.logic.buildReading(self.timePoints(), linkWindows=self.linkWindowsCheckBox.checked)
        except Exception:
            logging.exception("EponaCompare: could not set up the reading views")
            return
        self.bindReadingObservers()
        if fit:
            qt.QTimer.singleShot(150, self.logic.fitViews)   # views need their final size first
            if views.currentLayout() in COMPARE_DUAL_MONITOR_LAYOUT_IDS:
                # again once the Monitor 2 window is placed and maximized
                qt.QTimer.singleShot(COMPARE_DUAL_MONITOR_FIT_DELAY_MS, self.logic.fitViews)
        else:
            qt.QTimer.singleShot(150, _geometrySync.alignToReferences)
        self.windowInfoOverlay.scheduleUpdate()
        self.scheduleRoiUpdate()

    def bindReadingObservers(self):
        """Rotation button follows the MIP views (their animation mode can change elsewhere)."""
        for viewNode, tag in self._observedMipViews:
            viewNode.RemoveObserver(tag)
        self._observedMipViews = []
        for timePoint in TIME_POINTS:
            viewNode = views.mipViewNode(timePoint)
            if viewNode is not None:
                self._observedMipViews.append(
                    (viewNode, viewNode.AddObserver(vtk.vtkCommand.ModifiedEvent, self.updateRotationButton)))
        self.updateRotationButton()

    def restoreReadingControls(self):
        parameterNode = self.logic.getParameterNode()
        wasBlocked = self.linkWindowsCheckBox.blockSignals(True)
        self.linkWindowsCheckBox.checked = parameterNode.GetParameter(PARAM_LINK_WINDOWS) != "0"
        self.linkWindowsCheckBox.blockSignals(wasBlocked)
        mode = parameterNode.GetParameter(PARAM_THRESHOLD_MODE)
        for parameter, attribute in ((PARAM_RELATIVE_THRESHOLD, "_relativeThreshold"),
                                     (PARAM_ABSOLUTE_THRESHOLD, "_absoluteThreshold")):
            try:
                setattr(self, attribute, float(parameterNode.GetParameter(parameter)))
            except (TypeError, ValueError):
                pass
        self.setThresholdControls(mode if mode in (THRESHOLD_RELATIVE, THRESHOLD_ABSOLUTE) else THRESHOLD_RELATIVE,
                                  self._absoluteThreshold if mode == THRESHOLD_ABSOLUTE else self._relativeThreshold)
        accepted = self.logic.isAccepted()
        self.readingHintLabel.text = (
            "Time point 1: top row, time point 2: bottom row. Slice views of the same orientation move together."
            if accepted else "Accept the registration to open the reading layouts. Time point 1 is shown in the "
                             "top row, time point 2 in the bottom row.")

    def onLayoutChanged(self, layoutID=None):
        if not hasattr(self, "layoutButtons"):
            return
        current = views.currentLayout()
        for buttonLayoutID, layoutButton in self.layoutButtons.items():
            wasBlocked = layoutButton.blockSignals(True)
            layoutButton.checked = buttonLayoutID == current
            layoutButton.blockSignals(wasBlocked)
        if self.toolbar is not None:
            self.toolbar.setCurrentLayout(current)
        if current in COMPARE_LAYOUT_IDS and layoutID is not None:
            # Switched by the layout menu, a button or a scene: views are new or resized, fill and fit them
            qt.QTimer.singleShot(0, lambda: self.refreshReading(fit=True))
        self.windowInfoOverlay.scheduleUpdate()

    # --- windows ---------------------------------------------------------------------------------------------

    def onColorMap(self, name):
        self.logic.getParameterNode().SetParameter(PARAM_COLOR_MAP, name)
        for timePoint in TIME_POINTS:
            self.logic.setPetColorMap(self.pet(timePoint), name)

    def onCtPreset(self, window, level):
        for timePoint in TIME_POINTS:
            ct = self.ct(timePoint)
            if ct is not None and ct.GetDisplayNode() is not None:
                views.setWindowRange(ct, level - window / 2.0, level + window / 2.0)
        slicer.util.showStatusMessage(f"EponaCompare: CT W {window:g} L {level:g}", 2000)

    def onSuvPreset(self, upper):
        for timePoint in TIME_POINTS:
            self.logic.setPetRange(timePoint, self.pet(timePoint), 0.0, upper)
        slicer.util.showStatusMessage(f"EponaCompare: SUV 0–{upper:g}", 2000)

    def onPercentPreset(self, percent):
        for timePoint in TIME_POINTS:
            pet = self.pet(timePoint)
            upper = percentOfMaximum(views.volumeMaximum(pet), percent)
            if upper is not None:
                self.logic.setPetRange(timePoint, pet, 0.0, upper)
        slicer.util.showStatusMessage(f"EponaCompare: 0–{percent}% of each maximum", 2000)

    def bothSuv(self):
        return all(self.pet(tp) is not None and petValueInfo(self.pet(tp)).kind == VALUE_KIND_SUV
                   for tp in TIME_POINTS)

    def onWindowShortcut(self, key):
        """F5-F9 over a compare view: CT presets over CT views, else SUV presets (both SUV) or % of maximum."""
        view = viewUnderCursor()
        if view is None or not self.logic.isAccepted():
            return
        index = WINDOW_SHORTCUT_KEYS.index(key)
        content = COMPARE_SLICE_VIEWS.get(view[1], (None, None, None))[2]
        if content == "ct":
            ct = self.ct(compareViewTimePoint(view[1]))
            imageData = ct.GetImageData() if ct is not None else None
            if index < len(CT_WINDOW_PRESETS) and imageData is not None and looksLikeCT(imageData.GetScalarRange()[0]):
                _, window, level, _ = CT_WINDOW_PRESETS[index]
                self.onCtPreset(window, level)
            return
        if self.bothSuv():
            self.onSuvPreset(PET_SUV_PRESETS[index][1])
        else:
            self.onPercentPreset(SPECT_PERCENT_OF_MAX_PRESETS[index])

    def onLinkWindowsToggled(self, checked):
        self.logic.getParameterNode().SetParameter(PARAM_LINK_WINDOWS, "1" if checked else "0")
        if self.logic.isAccepted() and self.logic.timePointsComplete():
            self.logic.bindWindowLinks(self.timePoints(), checked)

    # --- MIPs ------------------------------------------------------------------------------------------------

    def onRotationToggled(self, enabled):
        views.setMipRotation(enabled, self.rotationSpeedSlider.value)
        self.updateRotationButton()

    def onRotationSpeedChanged(self, value):
        if self.rotationButton.checked:
            views.setMipRotation(True, value)

    def updateRotationButton(self, caller=None, event=None):
        spinning = any(viewNode.GetAnimationMode() != 0 for viewNode, _ in self._observedMipViews)
        wasBlocked = self.rotationButton.blockSignals(True)
        self.rotationButton.checked = spinning
        self.rotationButton.blockSignals(wasBlocked)
        self.rotationButton.text = "Stop MIP Rotation" if spinning else "Start MIP Rotation"
        if self.toolbar is not None:
            self.toolbar.setRotating(spinning)

    def onQuickView(self, axis):
        views.setMipRotation(False, self.rotationSpeedSlider.value)
        views.rotateMips(axis)
        self.updateRotationButton()

    # ------------------------------------------------------------------------------------------------------
    # Measurements: one ROI set (own segmentation and table) per time point
    # ------------------------------------------------------------------------------------------------------

    def setupMeasurementSection(self):
        button = ctk.ctkCollapsibleButton()
        button.text = "Measurements (spherical ROI)"
        self.layout.addWidget(button)
        self.measurementCollapsibleButton = button
        form = qt.QFormLayout(button)
        tip = qt.QLabel("Press 'Insert' over a view of a time point to place an ROI there")
        tip.setStyleSheet("color: red; font-weight: bold;")
        form.addRow(tip)

        self.roiRadiusSpinBox = qt.QDoubleSpinBox()
        self.roiRadiusSpinBox.setRange(1.0, 100.0)
        self.roiRadiusSpinBox.setDecimals(1)
        self.roiRadiusSpinBox.setSingleStep(1.0)
        self.roiRadiusSpinBox.setSuffix(" mm")
        self.roiRadiusSpinBox.setValue(DEFAULT_ROI_RADIUS_MM)
        self.roiRadiusSpinBox.setToolTip("Radius of the ROI selected in the table of the shown time point. With no "
                                         "ROI selected: the radius given to new ROIs.")
        form.addRow("ROI radius:", self.roiRadiusSpinBox)
        buttons = qt.QHBoxLayout()
        self.placeRoiButton = qt.QPushButton("Place ROI")
        self.placeRoiButton.setToolTip("Click once in a view to add an ROI to the time point whose tab is shown.\n"
                                       "Faster: hover a view of either time point and press Insert.")
        self.deleteRoiButton = qt.QPushButton("Delete Selected")
        self.clearRoisButton = qt.QPushButton("Clear All")
        self.clearRoisButton.setToolTip("Remove all ROIs of the time point whose tab is shown.")
        for widget in (self.placeRoiButton, self.deleteRoiButton, self.clearRoisButton):
            buttons.addWidget(widget)
        form.addRow(buttons)

        thresholdLayout = qt.QHBoxLayout()
        self.thresholdModeComboBox = qt.QComboBox()
        self.thresholdModeComboBox.addItem("% of Max", THRESHOLD_RELATIVE)
        self.thresholdModeComboBox.addItem("Absolute value", THRESHOLD_ABSOLUTE)
        self.thresholdModeComboBox.setToolTip("Segment inside each ROI: voxels >= this % of the ROI's Max, or >= an "
                                              "absolute value. Changes only the selected ROI; with none selected, "
                                              "the threshold for new ROIs.")
        self.thresholdValueSpinBox = qt.QDoubleSpinBox()
        self.thresholdValueSpinBox.setDecimals(1)
        thresholdLayout.addWidget(self.thresholdModeComboBox)
        thresholdLayout.addWidget(self.thresholdValueSpinBox)
        form.addRow("Segment threshold:", thresholdLayout)
        self._applyThresholdModeToSpinBox(THRESHOLD_RELATIVE)

        targetLayout = qt.QHBoxLayout()
        self.roiEditTargetLabel = qt.QLabel()
        self.roiEditTargetLabel.setStyleSheet("color: gray;")
        self.roiDeselectButton = qt.QPushButton("Deselect")
        targetLayout.addWidget(self.roiEditTargetLabel, 1)
        targetLayout.addWidget(self.roiDeselectButton)
        form.addRow(targetLayout)

        fieldsLayout = qt.QGridLayout()
        shownFields = parseRoiLabelFields(qt.QSettings().value("EponaCompare/RoiLabelFields"))
        self.roiLabelFieldCheckBoxes = {}
        for position, (key, text) in enumerate(ROI_LABEL_FIELDS):
            checkBox = qt.QCheckBox(text)
            checkBox.checked = key in shownFields
            checkBox.connect("toggled(bool)", self.onRoiLabelFieldsChanged)
            fieldsLayout.addWidget(checkBox, position // 4, position % 4)
            self.roiLabelFieldCheckBoxes[key] = checkBox
        form.addRow("Show near ROI:", fieldsLayout)
        self.showRoisOnMipCheckBox = qt.QCheckBox("Show ROI segments and values on the MIPs")
        self.showRoisOnMipCheckBox.checked = True
        form.addRow(self.showRoisOnMipCheckBox)

        self.roiTabs = qt.QTabWidget()
        for timePoint in TIME_POINTS:
            page = qt.QWidget()
            pageLayout = qt.QVBoxLayout(page)
            label = qt.QLabel("")
            label.wordWrap = True
            pageLayout.addWidget(label)
            table = qt.QTableWidget()
            table.setColumnCount(7)
            table.setHorizontalHeaderLabels(["ROI", "r (mm)", "Thr.", "Max", "Mean", "MTV (mL)", "TLG"])
            table.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
            table.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
            table.setSelectionMode(qt.QAbstractItemView.SingleSelection)
            table.horizontalHeader().setSectionResizeMode(qt.QHeaderView.ResizeToContents)
            table.horizontalHeader().setStretchLastSection(True)
            table.verticalHeader().setVisible(False)
            table.setMinimumHeight(130)
            table.connect("itemSelectionChanged()", lambda timePoint=timePoint: self.onRoiSelectionChanged(timePoint))
            pageLayout.addWidget(table)
            exportRow = qt.QHBoxLayout()
            exportRow.addStretch(1)
            exportButton = qt.QPushButton("Export Table (.tsv)…")
            exportButton.enabled = False
            exportButton.connect("clicked()", lambda timePoint=timePoint: self.onExportRoiTable(timePoint))
            exportRow.addWidget(exportButton)
            pageLayout.addLayout(exportRow)
            self.roiTabs.addTab(page, f"Time point {timePoint}")
            self.roiTabs.tabBar().setTabTextColor(timePoint - 1, qt.QColor(COMPARE_VIEW_COLORS[timePoint]))
            self.measuringLabels[timePoint] = label
            self.roiTables[timePoint] = table
            self.exportButtons[timePoint] = exportButton
            self.roiSets[timePoint] = SphereRoiSet(f"TP{timePoint}", f"TP{timePoint}", self.scheduleRoiUpdate,
                                                   color=ROI_POINT_COLORS[timePoint])
        form.addRow(self.roiTabs)

        self.roiUpdateTimer = qt.QTimer()
        self.roiUpdateTimer.setSingleShot(True)
        self.roiUpdateTimer.setInterval(ROI_UPDATE_DELAY_MS)
        self.roiUpdateTimer.connect("timeout()", self.updateRois)

        self.placeRoiButton.connect("clicked()", self.onPlaceRoi)
        self.deleteRoiButton.connect("clicked()", self.onDeleteSelectedRoi)
        self.clearRoisButton.connect("clicked()", self.onClearRois)
        self.roiRadiusSpinBox.connect("valueChanged(double)", self.onRoiRadiusChanged)
        self.thresholdModeComboBox.connect("currentIndexChanged(int)", self.onThresholdModeChanged)
        self.thresholdValueSpinBox.connect("valueChanged(double)", self.onThresholdValueChanged)
        self.roiDeselectButton.connect("clicked()", self.onDeselectRoi)
        self.showRoisOnMipCheckBox.connect("toggled(bool)", lambda checked: self.scheduleRoiUpdate())
        self.roiTabs.connect("currentChanged(int)", self.onRoiTabChanged)
        self.updateRoiEditTarget()

    def activeTimePoint(self):
        return self.roiTabs.currentIndex + 1 if self.roiTabs.currentIndex >= 0 else 1

    def connectRoiSets(self):
        for roiSet in self.roiSets.values():
            roiSet.connect()
        self.scheduleRoiUpdate()

    def scheduleRoiUpdate(self):
        if hasattr(self, "roiUpdateTimer"):
            self.roiUpdateTimer.start()

    def roiLabelFields(self):
        return tuple(key for key, _ in ROI_LABEL_FIELDS if self.roiLabelFieldCheckBoxes[key].checked)

    def onRoiLabelFieldsChanged(self, checked=None):
        qt.QSettings().setValue("EponaCompare/RoiLabelFields", ",".join(self.roiLabelFields()))
        self.scheduleRoiUpdate()

    def updateRois(self):
        if sceneIsBusy():
            self.roiUpdateTimer.start()
            return
        activeTimePoint = self.activeTimePoint()
        for timePoint in TIME_POINTS:
            roiSet = self.roiSets[timePoint]
            pet = self.pet(timePoint)
            info = petValueInfo(pet) if pet is not None else None
            unit = valueKindUnit(info.kind) if info is not None else ""
            self._roiUnits[timePoint] = unit
            roiSet.setViews(views.timePointViewNodeIDs(timePoint))
            try:
                result = roiSet.update(pet, self.roiRadiusSpinBox.value,
                                       (self.roiRadiusSpinBox.minimum, self.roiRadiusSpinBox.maximum),
                                       self.currentThreshold(), self.roiLabelFields(), unit,
                                       self.selectedRoiPointID(timePoint))
            except Exception:
                logging.exception(f"EponaCompare: could not update the ROIs of time point {timePoint}")
                continue
            self._roiRows[timePoint] = result["rows"]
            self.fillRoiTable(timePoint, result["rows"], result["colors"], result["selectedID"])
            self.updateMeasuringLabel(timePoint, pet, info)
            overlay = self.mipOverlays[timePoint]
            try:
                if self.showRoisOnMipCheckBox.checked:
                    overlay.update(result["mipEntries"], result["segmentationNode"])
                else:
                    overlay.clear()
            except Exception:
                logging.exception("EponaCompare: could not update the MIP overlay")
            if result["newRoiCenter"] is not None:
                activeTimePoint = timePoint
                self.roiTabs.setCurrentIndex(timePoint - 1)
                self.jumpSliceViewsTo(result["newRoiCenter"])
            if timePoint == activeTimePoint and result["selectedID"] is not None:
                self.syncRoiControls(timePoint, result["selectedID"])
        self._applyThresholdModeToSpinBox(self.currentThreshold()[0])
        self.updateRoiEditTarget()

    @staticmethod
    def jumpSliceViewsTo(positionWorld):
        x, y, z = (float(v) for v in positionWorld)
        try:
            slicer.modules.markups.logic().JumpSlicesToLocation(x, y, z, False)
        except Exception:
            slicer.vtkMRMLSliceNode.JumpAllSlices(slicer.mrmlScene, x, y, z, slicer.vtkMRMLSliceNode.OffsetJumpSlice)

    def updateMeasuringLabel(self, timePoint, pet, info):
        label = self.measuringLabels[timePoint]
        if pet is None:
            label.text = f"Measuring on: (no time point {timePoint} SPECT/PET selected)"
            return
        unit = valueKindUnit(info.kind) if info is not None else ""
        values = f"Values: {unit}" if unit else "Values: unit not known, so none is shown"
        label.text = f"Measuring on: {volumeLabel(pet)}\n{values}"
        label.setToolTip(info.reason if info is not None else "")

    def fillRoiTable(self, timePoint, rows, colors, selectPointID):
        table = self.roiTables[timePoint]
        unit = self._roiUnits.get(timePoint, "")
        self.exportButtons[timePoint].enabled = bool(rows)
        wasBlocked = table.blockSignals(True)
        try:
            table.setRowCount(len(rows))
            for rowIndex, (pointID, name, radius, stats, threshold, origin) in enumerate(rows):
                for column, text in enumerate(formatRoiTableRow(name, radius, stats, threshold, unit=unit)):
                    item = qt.QTableWidgetItem(text)
                    if column == 0:
                        item.setData(qt.Qt.UserRole, pointID)
                        item.setToolTip("Placed by AI" if origin == ROI_ORIGIN_AI else "Placed by the user")
                        color = (colors or {}).get(pointID)
                        if color is not None:
                            item.setData(qt.Qt.DecorationRole,
                                         qt.QColor.fromRgbF(float(color[0]), float(color[1]), float(color[2])))
                    table.setItem(rowIndex, column, item)
            table.clearSelection()
            if selectPointID is not None:
                for rowIndex, row in enumerate(rows):
                    if row[0] == selectPointID:
                        table.selectRow(rowIndex)
                        break
        finally:
            table.blockSignals(wasBlocked)

    def selectedRoiPointID(self, timePoint):
        table = self.roiTables.get(timePoint)
        if table is None:
            return None
        selectedRows = table.selectionModel().selectedRows()
        if not selectedRows:
            return None
        item = table.item(selectedRows[0].row(), 0)
        return item.data(qt.Qt.UserRole) if item is not None else None

    def syncRoiControls(self, timePoint, pointID):
        """Show the radius and threshold of an ROI in the controls (they now edit this ROI)."""
        node = self.roiSets[timePoint].roiNode
        if node is None or pointID is None:
            return
        radius = SphereRoiSet.getRadius(node, pointID, self.roiRadiusSpinBox.value)
        if abs(self.roiRadiusSpinBox.value - radius) > 1e-6:
            wasBlocked = self.roiRadiusSpinBox.blockSignals(True)
            self.roiRadiusSpinBox.setValue(radius)
            self.roiRadiusSpinBox.blockSignals(wasBlocked)
        mode, value = SphereRoiSet.getThreshold(node, pointID, self.currentThreshold())
        currentMode, currentValue = self.currentThreshold()
        if mode != currentMode or abs(value - currentValue) > 1e-6:
            self.setThresholdControls(mode, value)

    def onRoiSelectionChanged(self, timePoint):
        self.updateRoiEditTarget()
        pointID = self.selectedRoiPointID(timePoint)
        node = self.roiSets[timePoint].roiNode
        if node is None or pointID is None:
            return
        self.syncRoiControls(timePoint, pointID)
        index = node.GetNthControlPointIndexByID(pointID)
        if index >= 0:
            slicer.modules.markups.logic().JumpSlicesToNthPointInMarkup(node.GetID(), index, True)

    def onRoiTabChanged(self, index=None):
        timePoint = self.activeTimePoint()
        self._applyThresholdModeToSpinBox(self.currentThreshold()[0])
        pointID = self.selectedRoiPointID(timePoint)
        if pointID is not None:
            self.syncRoiControls(timePoint, pointID)
        self.updateRoiEditTarget()

    def onDeselectRoi(self):
        self.roiTables[self.activeTimePoint()].clearSelection()

    def updateRoiEditTarget(self):
        timePoint = self.activeTimePoint()
        table = self.roiTables.get(timePoint)
        selectedRows = table.selectionModel().selectedRows() if table is not None else []
        item = table.item(selectedRows[0].row(), 0) if selectedRows else None
        if item is not None:
            self.roiEditTargetLabel.text = f"Radius and threshold: editing time point {timePoint} {item.text()} only"
            self.roiDeselectButton.enabled = True
        else:
            self.roiEditTargetLabel.text = "Radius and threshold: values for new ROIs"
            self.roiDeselectButton.enabled = False

    def onRoiRadiusChanged(self, value):
        timePoint = self.activeTimePoint()
        node = self.roiSets[timePoint].roiNode
        pointID = self.selectedRoiPointID(timePoint)
        if node is not None and pointID is not None:
            SphereRoiSet.setRadius(node, pointID, value)
            self.scheduleRoiUpdate()

    # --- threshold -------------------------------------------------------------------------------------------

    def currentThreshold(self):
        mode = self.thresholdModeComboBox.itemData(self.thresholdModeComboBox.currentIndex)
        mode = mode if mode in (THRESHOLD_RELATIVE, THRESHOLD_ABSOLUTE) else THRESHOLD_RELATIVE
        return mode, (self._absoluteThreshold if mode == THRESHOLD_ABSOLUTE else self._relativeThreshold)

    def _applyThresholdModeToSpinBox(self, mode):
        spinBox = self.thresholdValueSpinBox
        unit = self._roiUnits.get(self.activeTimePoint(), "") if hasattr(self, "roiTabs") else ""
        wasBlocked = spinBox.blockSignals(True)
        if mode == THRESHOLD_ABSOLUTE:
            spinBox.setRange(0.0, 1.0e6)
            spinBox.setSingleStep(0.1)
            spinBox.setSuffix(f" {unit}" if unit else "")
            spinBox.setValue(self._absoluteThreshold)
        else:
            spinBox.setRange(1.0, 100.0)
            spinBox.setSingleStep(1.0)
            spinBox.setSuffix(" %")
            spinBox.setValue(self._relativeThreshold)
        spinBox.blockSignals(wasBlocked)

    def onThresholdModeChanged(self, index=None):
        self._applyThresholdModeToSpinBox(self.currentThreshold()[0])
        self.applyThresholdControls()

    def onThresholdValueChanged(self, value):
        if self.currentThreshold()[0] == THRESHOLD_ABSOLUTE:
            self._absoluteThreshold = float(value)
        else:
            self._relativeThreshold = float(value)
        self.applyThresholdControls()

    def applyThresholdControls(self):
        self.saveThresholdSettings()
        timePoint = self.activeTimePoint()
        node = self.roiSets[timePoint].roiNode
        pointID = self.selectedRoiPointID(timePoint)
        if node is not None and pointID is not None:
            SphereRoiSet.setThreshold(node, pointID, *self.currentThreshold())
            self.scheduleRoiUpdate()

    def setThresholdControls(self, mode, value):
        if mode == THRESHOLD_ABSOLUTE:
            self._absoluteThreshold = float(value)
        else:
            self._relativeThreshold = float(value)
        wasBlocked = self.thresholdModeComboBox.blockSignals(True)
        self.thresholdModeComboBox.setCurrentIndex(self.thresholdModeComboBox.findData(mode))
        self.thresholdModeComboBox.blockSignals(wasBlocked)
        self._applyThresholdModeToSpinBox(mode)
        self.saveThresholdSettings()

    def saveThresholdSettings(self):
        parameterNode = self.logic.getParameterNode()
        parameterNode.SetParameter(PARAM_THRESHOLD_MODE, self.currentThreshold()[0])
        parameterNode.SetParameter(PARAM_RELATIVE_THRESHOLD, f"{self._relativeThreshold:g}")
        parameterNode.SetParameter(PARAM_ABSOLUTE_THRESHOLD, f"{self._absoluteThreshold:g}")

    # --- placing / removing ----------------------------------------------------------------------------------

    def onPlaceRoi(self):
        timePoint = self.activeTimePoint()
        pet = self.pet(timePoint)
        if pet is None:
            slicer.util.warningDisplay(f"Select the SPECT/PET of time point {timePoint} first.")
            return
        self.roiTables[timePoint].clearSelection()  # the new ROI takes the radius box value
        node = self.roiSets[timePoint].prepareForPlacement(pet)
        selectionNode = slicer.app.applicationLogic().GetSelectionNode()
        selectionNode.SetReferenceActivePlaceNodeClassName("vtkMRMLMarkupsFiducialNode")
        selectionNode.SetActivePlaceNodeID(node.GetID())
        interactionNode = slicer.app.applicationLogic().GetInteractionNode()
        interactionNode.SetPlaceModePersistence(0)
        interactionNode.SetCurrentInteractionMode(interactionNode.Place)

    def onPlaceRoiAtCursor(self):
        """Insert over a compare view: an ROI of that view's time point, centred at the mouse."""
        view = viewUnderCursor()
        timePoint = compareViewTimePoint(view[1]) if view is not None else None
        if timePoint is None or view[0] != "slice":
            slicer.util.showStatusMessage("EponaCompare: hover the mouse over a slice view, then press Insert.", 3000)
            return
        pet = self.pet(timePoint)
        if pet is None:
            slicer.util.showStatusMessage(f"EponaCompare: select the time point {timePoint} SPECT/PET first.", 3000)
            return
        crosshairNode = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLCrosshairNode")
        ras = [0.0, 0.0, 0.0]
        if crosshairNode is None or not crosshairNode.GetCursorPositionRAS(ras):
            return
        interactionNode = slicer.app.applicationLogic().GetInteractionNode()
        if interactionNode.GetCurrentInteractionMode() == interactionNode.Place:
            interactionNode.SwitchToViewTransformMode()
        self.roiTables[timePoint].clearSelection()
        self.roiTabs.setCurrentIndex(timePoint - 1)
        self.roiSets[timePoint].addRoiAtWorld(ras, pet)

    def onDeleteSelectedRoi(self):
        timePoint = self.activeTimePoint()
        self.roiSets[timePoint].removeRoi(self.selectedRoiPointID(timePoint))
        self.scheduleRoiUpdate()

    def onClearRois(self):
        timePoint = self.activeTimePoint()
        roiSet = self.roiSets[timePoint]
        if not roiSet.rows or not slicer.util.confirmOkCancelDisplay(
                f"Remove all ROIs of {self.timePointLabel(timePoint).lower()}?"):
            return
        roiSet.removeAllRois()
        self.scheduleRoiUpdate()

    def onExportRoiTable(self, timePoint):
        if self.roiUpdateTimer.isActive():
            self.roiUpdateTimer.stop()
            self.updateRois()
        rows = list(self._roiRows.get(timePoint, []))
        if not rows:
            slicer.util.infoDisplay("There are no ROIs to export.", windowTitle="Export ROI table")
            return
        pet = self.pet(timePoint)
        petName = volumeLabel(pet)
        unit = self._roiUnits.get(timePoint, "")
        settings = qt.QSettings()
        folder = settings.value("EponaCompare/RoiExportFolder") or ""
        if not os.path.isdir(folder):
            folder = qt.QStandardPaths.writableLocation(qt.QStandardPaths.DocumentsLocation) or ""
        fileName = f"TP{timePoint}_" + defaultRoiExportFileName(pet.GetName() if pet is not None else "",
                                                                 isSuv=unit == "SUV")
        path = qt.QFileDialog.getSaveFileName(slicer.util.mainWindow(), f"Export time point {timePoint} ROI table",
                                              os.path.join(folder, fileName), "Tab-separated values (*.tsv);;All files (*)")
        if isinstance(path, (tuple, list)):
            path = path[0] if path else ""
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".tsv"
        text = formatRoiTableTsv(rows, self.roiSets[timePoint].centers, petName, "", unit=unit)
        try:
            with open(path, "w", encoding="utf-8", newline="") as tsvFile:
                tsvFile.write(text)
        except OSError as error:
            slicer.util.errorDisplay(f"Could not write the ROI table:\n{path}\n\n{error}", windowTitle="Export ROI table")
            return
        settings.setValue("EponaCompare/RoiExportFolder", os.path.dirname(path))
        slicer.util.showStatusMessage(f"Exported {len(rows)} ROI(s) of time point {timePoint} to {path}", 5000)


class EponaCompareLogic(ScriptedLoadableModuleLogic):

    def registrationTransform(self, create=False):
        """The linear transform that moves time point 1 onto time point 2 (created on request)."""
        parameterNode = self.getParameterNode()
        node = parameterNode.GetNodeReference(ROLE_REGISTRATION)
        if node is None and create:
            node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLinearTransformNode", REGISTRATION_TRANSFORM_NAME)
            node.SetAttribute(REGISTRATION_TRANSFORM_ATTRIBUTE, "1")
            parameterNode.SetNodeReferenceID(ROLE_REGISTRATION, node.GetID())
        return node

    def __init__(self):
        ScriptedLoadableModuleLogic.__init__(self)
        self.overlayOpacity = REGISTRATION_FOREGROUND_OPACITY

    def attachToRegistration(self, volumeNode, tp1Ct, fixedNodes):
        """
        Put a time point 1 volume under the registration transform when time point 1's CT/MR is already under
        it (registered or adjusted by hand). True if it was attached.
        """
        transformNode = self.registrationTransform()
        if transformNode is None or tp1Ct is None or transformNode not in transformChain(tp1Ct):
            return False
        if transformNode in transformChain(volumeNode):
            return False
        attachUnderTransform(volumeNode, transformNode, fixedNodes)
        return True

    def prepareTransform(self, tp1Ct, tp1Pet, fixedNodes):
        """
        The registration transform (created if needed) with time point 1's CT/MR and SPECT/PET under it and no
        time point 2 volume under it. ValueError when that is not possible (message for the user).
        """
        transformNode = self.registrationTransform(create=True)
        for fixedNode in fixedNodes:
            if fixedNode is None:
                continue
            chain = transformChain(fixedNode)
            if chain and chain[0] is transformNode:
                # E.g. the time points were swapped after an earlier registration: time point 2 must not move
                fixedNode.SetAndObserveTransformNodeID(None)
                logging.info(f"EponaCompare: '{fixedNode.GetName()}' taken out of the time point 1 registration")
            elif transformNode in chain:
                raise ValueError(f"'{fixedNode.GetName()}' (time point 2) is under the time point 1 registration "
                                 "transform. Remove that transform from it in the Data module first.")
        for volumeNode in (tp1Ct, tp1Pet):
            if volumeNode is not None:
                attachUnderTransform(volumeNode, transformNode, fixedNodes)
        return transformNode

    def register(self, tp1Ct, tp1Pet, tp2Ct, fixedNodes, progress=None):
        """
        Move time point 1 (CT/MR and SPECT/PET) onto time point 2 by a translation. Returns the result of
        registerTimePoints. ValueError for inputs that cannot be registered (message for the user).
        """
        transformNode = self.prepareTransform(tp1Ct, tp1Pet, fixedNodes)
        setTranslation(transformNode, (0.0, 0.0, 0.0))  # the registration always starts from the volumes as loaded
        self.showRegistrationViews(tp1Ct, tp2Ct)
        if progress is not None:
            progress("Registering time point 1 to time point 2…")

        result = registerTimePoints(tp1Ct, tp2Ct, progress)
        logRegistration(result)
        transformNode.SetAttribute(REGISTRATION_SHIFT_ATTRIBUTE, " ".join(f"{v:.3f}" for v in result["shift"]))
        transformNode.SetAttribute(REGISTRATION_CENTER_ATTRIBUTE, " ".join(f"{v:.3f}" for v in result["fixedCenter"]))
        setTranslation(transformNode, result["shift"])
        self.centerRegistrationViews(result["fixedCenter"])
        return result

    def resetRegistration(self):
        """Time point 1 back to its original position (the automatic result stays stored for 'Back to automatic')."""
        transformNode = self.registrationTransform()
        if transformNode is None:
            return False
        setTranslation(transformNode, (0.0, 0.0, 0.0))
        return True

    def backToAutomatic(self):
        """Undo manual changes: the shift of the last automatic registration (rotation removed too)."""
        transformNode = self.registrationTransform()
        automatic = parseShift(transformNode.GetAttribute(REGISTRATION_SHIFT_ATTRIBUTE)) \
            if transformNode is not None else None
        if automatic is None:
            return False
        setTranslation(transformNode, automatic)
        return True

    def checkCenter(self, fixedCt):
        """Where the check views look: the middle of the body both scans cover, else the centre of time point 2."""
        transformNode = self.registrationTransform()
        center = parseShift(transformNode.GetAttribute(REGISTRATION_CENTER_ATTRIBUTE)) \
            if transformNode is not None else None
        if center is None and fixedCt is not None:
            bounds = [0.0] * 6
            fixedCt.GetRASBounds(bounds)
            center = [(bounds[0] + bounds[1]) / 2.0, (bounds[2] + bounds[3]) / 2.0, (bounds[4] + bounds[5]) / 2.0]
        return center

    def setHandlesVisible(self, transformNode, visible, fixedCt=None):
        """Translation-only interaction handles of the registration transform in the slice (and 3D) views."""
        if transformNode.GetDisplayNode() is None:
            transformNode.CreateDefaultDisplayNodes()
        displayNode = transformNode.GetDisplayNode()
        if displayNode is None:
            return
        for setterName, value in (("SetEditorTranslationEnabled", True), ("SetEditorRotationEnabled", False),
                                  ("SetEditorScalingEnabled", False)):
            if hasattr(displayNode, setterName):
                getattr(displayNode, setterName)(value)
        center = self.checkCenter(fixedCt) if visible else None
        if center is not None and hasattr(transformNode, "SetCenterOfTransformation"):
            # Handles appear at the transformed centre: place them in the middle of the views
            local = [c - s for c, s in zip(center, transformTranslation(transformNode))]
            try:
                transformNode.SetCenterOfTransformation(local)
            except Exception:
                logging.debug("EponaCompare: could not set the centre of the move handles", exc_info=True)
        displayNode.SetEditorVisibility(visible)
        if hasattr(displayNode, "SetEditorSliceIntersectionVisibility"):
            displayNode.SetEditorSliceIntersectionVisibility(visible)

    def setOverlayOpacity(self, opacity):
        """Opacity of time point 1 over time point 2 in the three check views."""
        self.overlayOpacity = float(opacity)
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return
        for name in REGISTRATION_VIEWS:
            sliceWidget = layoutManager.sliceWidget(name)
            if sliceWidget is not None:
                sliceWidget.mrmlSliceCompositeNode().SetForegroundOpacity(self.overlayOpacity)

    # --- Check views: axial | coronal | sagittal CT/MR fusion ------------------------------------------

    @staticmethod
    def _styleRegistrationDisplay(volumeNode, tinted):
        if volumeNode.GetDisplayNode() is None:
            volumeNode.CreateDefaultDisplayNodes()
        displayNode = volumeNode.GetDisplayNode()
        if tinted:
            if volumeNode.GetAttribute(ORIGINAL_COLOR_ATTRIBUTE) is None:
                volumeNode.SetAttribute(ORIGINAL_COLOR_ATTRIBUTE, displayNode.GetColorNodeID() or "")
            colorID = next((cid for cid in REGISTRATION_TINT_COLOR_IDS if slicer.mrmlScene.GetNodeByID(cid)), None)
            if colorID is not None:
                displayNode.SetAndObserveColorNodeID(colorID)
        else:
            displayNode.SetAndObserveColorNodeID("vtkMRMLColorTableNodeGrey")
        imageData = volumeNode.GetImageData()
        if imageData is not None and imageData.GetNumberOfPoints() and looksLikeCT(imageData.GetScalarRange()[0]):
            displayNode.SetAutoWindowLevel(False)
            displayNode.SetWindowLevel(*REGISTRATION_CT_WINDOW)
        else:
            displayNode.SetAutoWindowLevel(True)

    @staticmethod
    def restoreTint(volumeNode):
        """Give a time point 1 CT/MR its color map from before the registration check back."""
        colorID = volumeNode.GetAttribute(ORIGINAL_COLOR_ATTRIBUTE) if volumeNode is not None else None
        if colorID is None:
            return
        if volumeNode.GetDisplayNode() is not None:
            volumeNode.GetDisplayNode().SetAndObserveColorNodeID(colorID or "vtkMRMLColorTableNodeGrey")
        volumeNode.RemoveAttribute(ORIGINAL_COLOR_ATTRIBUTE)

    def showRegistrationViews(self, movingCt, fixedCt):
        """Axial | coronal | sagittal: time point 2 CT/MR in grey, time point 1 tinted on top at half opacity."""
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None or not registerLayout(LAYOUT_REGISTRATION_ID, registrationLayoutDescription()):
            return
        layoutManager.setLayout(LAYOUT_REGISTRATION_ID)
        self._styleRegistrationDisplay(fixedCt, tinted=False)
        self._styleRegistrationDisplay(movingCt, tinted=True)
        for name, orientation in zip(REGISTRATION_VIEWS, ("Axial", "Coronal", "Sagittal")):
            sliceWidget = layoutManager.sliceWidget(name)
            if sliceWidget is None:
                continue
            sliceNode = sliceWidget.mrmlSliceNode()
            sliceNode.SetOrientation(orientation)
            compositeNode = sliceWidget.mrmlSliceCompositeNode()
            compositeNode.SetBackgroundVolumeID(fixedCt.GetID())
            compositeNode.SetForegroundVolumeID(movingCt.GetID())
            compositeNode.SetForegroundOpacity(self.overlayOpacity)
            compositeNode.SetLabelVolumeID(None)
            sliceWidget.sliceLogic().FitSliceToAll()

    @staticmethod
    def centerRegistrationViews(center):
        """Show the middle of the body part both scans cover in all three views."""
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None or center is None:
            return
        for name in REGISTRATION_VIEWS:
            sliceWidget = layoutManager.sliceWidget(name)
            if sliceWidget is not None:
                sliceWidget.mrmlSliceNode().JumpSliceByCentering(*center)


    # --- Reading (after "Accept Registration") ---------------------------------------------------------

    def isAccepted(self):
        return self.getParameterNode().GetParameter(PARAM_ACCEPTED) == "1"

    def timePoints(self):
        """{1: (pet, ct), 2: (pet, ct)} from the parameter node (saved with the scene)."""
        parameterNode = self.getParameterNode()
        return {timePoint: (parameterNode.GetNodeReference(PET_ROLES[timePoint]),
                            parameterNode.GetNodeReference(CT_ROLES[timePoint])) for timePoint in TIME_POINTS}

    def timePointsComplete(self):
        return all(node is not None for pair in self.timePoints().values() for node in pair)

    def acceptRegistration(self, timePoints, fixedNodes):
        """Keep the current alignment and switch to reading: time point 1's SPECT/PET follows its CT/MR."""
        pet1, ct1 = timePoints[1]
        transformNode = self.registrationTransform()
        if transformNode is not None and transformNode in transformChain(ct1):
            attachUnderTransform(pet1, transformNode, fixedNodes)
        self.restoreTint(ct1)
        self.getParameterNode().SetParameter(PARAM_ACCEPTED, "1")

    def setPetColorMap(self, petNode, name):
        if petNode is None:
            return
        if petNode.GetDisplayNode() is None:
            petNode.CreateDefaultDisplayNodes()
        colorNode = views.colorNodeByName(name)
        if colorNode is None:
            logging.warning(f"EponaCompare: color map '{name}' not found")
            return
        petNode.GetDisplayNode().SetAndObserveColorNodeID(colorNode.GetID())

    def setPetRange(self, timePoint, petNode, lower, upper):
        """SPECT/PET window of a time point (its PET-only view follows) and the grey range of its MIP."""
        if petNode is None:
            return
        views.setWindowRange(petNode, lower, upper)
        vrDisplayNode = views.mipDisplayNode(timePoint, petNode)
        if vrDisplayNode is not None:
            views.setMipRange(vrDisplayNode, lower, upper)

    def initialDisplay(self, timePoint, colorMapName):
        """
        First display of a time point: CT/MR in grey (abdomen window for CT), SPECT/PET with the color map and
        SUV 0-10 when it is in SUV, else 0-100 % of its maximum (see initialPetRange).
        """
        pet, ct = self.timePoints()[timePoint]
        if ct is not None:
            if ct.GetDisplayNode() is None:
                ct.CreateDefaultDisplayNodes()
            ct.GetDisplayNode().SetAndObserveColorNodeID("vtkMRMLColorTableNodeGrey")
            imageData = ct.GetImageData()
            if imageData is not None and imageData.GetNumberOfPoints() and looksLikeCT(imageData.GetScalarRange()[0]):
                window, level = DEFAULT_CT_WINDOW
                views.setWindowRange(ct, level - window / 2.0, level + window / 2.0)
            else:
                ct.GetDisplayNode().SetAutoWindowLevel(True)
        if pet is not None:
            if pet.GetDisplayNode() is None:
                pet.CreateDefaultDisplayNodes()
            pet.GetDisplayNode().SetInterpolate(True)
            self.setPetColorMap(pet, colorMapName)
            lower, upper = initialPetRange(petValueInfo(pet), views.volumeMaximum(pet))
            self.setPetRange(timePoint, pet, lower, upper)

    def bindWindowLinks(self, timePoints, linkWindows, twins=None):
        """
        PET <-> its PET-only twin always; CT/MR 1 <-> 2 when linked; PET 1 <-> 2 when linked and both are in SUV
        (a common scale: other units keep their own windows). Time point 2 is the reference when linking.
        """
        def link(name):
            if name not in _windowLinks:
                _windowLinks[name] = WindowLevelLink()
            return _windowLinks[name]

        def display(node):
            return node.GetDisplayNode() if node is not None else None

        twins = twins or {timePoint: views.findTwin(timePoint) for timePoint in TIME_POINTS}
        for timePoint, (pet, _) in timePoints.items():
            petDisplay, twinDisplay = display(pet), display(twins.get(timePoint))
            if petDisplay is not None and twinDisplay is not None:
                link(f"pet{timePoint}-twin").bind(petDisplay, twinDisplay)
                link(f"pet{timePoint}-twin").syncFrom(petDisplay)
        (pet1, ct1), (pet2, ct2) = timePoints[1], timePoints[2]
        bothSuv = all(pet is not None and petValueInfo(pet).kind == VALUE_KIND_SUV for pet in (pet1, pet2))
        for name, first, second, active in (("pet1-pet2", pet1, pet2, linkWindows and bothSuv),
                                            ("ct1-ct2", ct1, ct2, linkWindows)):
            if active and display(first) is not None and display(second) is not None:
                link(name).bind(display(first), display(second))
                link(name).syncFrom(display(second))
            elif name in _windowLinks:
                _windowLinks[name].unbind()

    def buildReading(self, timePoints, linkWindows=None):
        """Fill the compare views of the current layout: twins, view contents, window links, MIPs, slice sync."""
        views.registerCompareLayouts()
        twins = {timePoint: views.getOrCreateTwin(timePoint, pet) for timePoint, (pet, _) in timePoints.items()}
        views.assignViews(timePoints, twins)
        if linkWindows is None:
            linkWindows = self.getParameterNode().GetParameter(PARAM_LINK_WINDOWS) != "0"
        self.bindWindowLinks(timePoints, linkWindows, twins)
        for timePoint, (pet, _) in timePoints.items():
            if pet is not None:
                lower, upper = views.windowRange(pet) or (0.0, 1.0)
                views.showMip(timePoint, pet, lower, upper)
        views.isolateMipViews()
        _geometrySync.setGroups(views.geometryGroups())
        return twins

    def fitViews(self):
        """Fit the time point 2 fusion views, give every view of the same orientation that geometry, fit the MIPs."""
        if sceneIsBusy():
            return
        views.repairViewSizes()     # a view the layout switch left stretched
        views.fitReferenceViews()
        _geometrySync.alignToReferences()
        timePoints = self.timePoints()
        center = views.volumeCenter(timePoints[2][0]) or views.volumeCenter(timePoints[2][1])
        for timePoint in TIME_POINTS:
            views.fitMip(timePoint, center)


class EponaCompareTest(ScriptedLoadableModuleTest):
    """The shared EponaLib unit tests (including registration); they need no scene."""

    def runTest(self):
        import unittest
        from EponaLib import tests
        self.delayDisplay("Running the EponaLib unit tests")
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(tests))
        self.assertTrue(result.wasSuccessful(), f"{len(result.failures)} failures, {len(result.errors)} errors")
        self.delayDisplay("EponaLib unit tests passed")

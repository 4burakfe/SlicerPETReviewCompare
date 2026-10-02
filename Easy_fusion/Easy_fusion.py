import os
import sys
import re
import html
import math
import time
import logging
import importlib
import traceback
import contextlib

import qt
import ctk
import vtk
import slicer
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin

# Shared code of the Epona modules (EponaLib/ next to this file). The folder is put on the path
# explicitly, so it is found however the module was loaded (extension or Additional module paths).
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
if _MODULE_DIR not in sys.path:
    sys.path.insert(0, _MODULE_DIR)

from EponaLib.scene import (  # noqa: F401
    deferUntilSceneIdle, runWhenSceneSettled, sceneIsBusy,
)
from EponaLib.layouts import (  # noqa: F401
    buildLayoutDescriptions, CUSTOM_LAYOUT_IDS, DUAL_MONITOR_LAYOUT_IDS, DUAL_MONITOR_PLACE_DELAY_MS,
    DUAL_MONITOR_WINDOW_TITLE, EASYFUSION_LAYOUT_IDS, fieldOfViewForTarget, GO_DEFAULT_LAYOUT_ID,
    hasMultipleScreens, LAYOUT_AXIAL_FOUR_UP_ID, LAYOUT_CT_FUSION_PET_3D_ID, LAYOUT_DUAL_MONITOR_FUSION_MIDDLE_ID,
    LAYOUT_DUAL_MONITOR_ID, LAYOUT_FOUR_UP_ID, LAYOUT_TWO_BY_TWO_ID, PET_ONLY_DISPLAY_ID,
    isCompareView, PET_ONLY_SOURCE_ROLE, PET_ONLY_VOLUME_ATTRIBUTE, PET_ONLY_VOLUME_ID, secondaryScreen,
    showOnSecondScreen, SINGLE_SCREEN_FALLBACK_LAYOUT_ID, SLICE_VIEW_ROLES,
)
from EponaLib.shortcuts import registerShortcut, unregisterShortcuts  # noqa: F401
from EponaLib.toolbar import (  # noqa: F401
    CT_PRESET_ICONS, EponaToolBar, isToolbarEnabled, LAYOUT_ICONS, MRI_PRESET_ICONS, PERCENT_PRESET_ICONS,
    richToolTip, SUV_PRESET_ICONS, TOOLBAR_SELECTOR_WIDTH,
)
from EponaLib.values import (  # noqa: F401
    CT_WINDOW_PRESETS, initialPetRange, looksLikeCT, MRI_PERCENTILE_PRESETS,
    MRI_PRESET_SAMPLE_SIZE, percentileWindow, percentOfMaximum, PET_SUV_PRESETS,
    petValueInfo, PetValueInfo, sameValueScale, SPECT_PERCENT_OF_MAX_PRESETS,
    tissueSample, VALUE_KIND_SUV, valueKindUnit, WINDOW_SHORTCUT_KEYS,
    windowPresetForView, anatomicalLabel, functionalLabel,
)
from EponaLib.display import (  # noqa: F401
    bindWindowLevelSync, fillHotIronTable, findColorNode, hotIronRGB, SliceGeometrySync, syncSliceViewSize,
    unbindWindowLevelSync,
)
from EponaLib.roi import (  # noqa: F401
    DEFAULT_ABSOLUTE_THRESHOLD, DEFAULT_RELATIVE_THRESHOLD, DEFAULT_ROI_LABEL_FIELDS, DEFAULT_ROI_RADIUS_MM,
    defaultRoiExportFileName, formatRoiTableRow, formatRoiTableTsv, parseRoiLabelFields, ROI_LABEL_FIELDS,
    ROI_ORIGIN_AI, ROI_ORIGIN_ATTRIBUTE, ROI_ORIGIN_USER, THRESHOLD_ABSOLUTE, THRESHOLD_RELATIVE,
)
from EponaLib.roiset import (  # noqa: F401
    ROLE_HANDLES, ROLE_LABELS, ROLE_POINTS, ROLE_SEGMENTS, ROLE_SPHERES, SphereRoiSet,
)
from EponaLib.filters import (  # noqa: F401
    Easy_fusionFilterLogic, FILTER_CROPPED_ATTRIBUTE, FILTER_DEFAULT_PARAMETERS, FILTER_EINOPS_REQUIREMENT,
    FILTER_MIN_VRAM_GB, FILTER_MODEL_ATTRIBUTE, FILTER_MODEL_CATALOG_MAX_AGE_S, FILTER_MODEL_RELEASE_PAGE_URL,
    FILTER_MODEL_REPOSITORY, FILTER_SOURCE_ROLE, FILTER_TARGET_CT, FILTER_TARGET_PET,
    FilterCancelled, filterJobSize, FilterProgressDialog, filterSuvNotes,
    formatByteSize, guessFilterTarget, listFilterModels, mergePublishedModelCatalog,
    parseFilterMetadata, sidecarName,
)
from EponaLib.overlays import (  # noqa: F401
    DATA_PROBE_ANNOTATIONS_SETTING, DATA_PROBE_CORNER_INDEXES, DATA_PROBE_CORNERS, MipRoiOverlay,
    SliceWindowInfoOverlay,
)


# vtkMRMLViewNode animation modes (Off / Spin)
ANIMATION_OFF = 0
ANIMATION_SPIN = 1

MODULE_NAME = "Easy_fusion"
HOT_IRON_NAME = "CustomHotIron"
_HOT_IRON_NAME_PATTERN = re.compile(r"^CustomHotIron([_ ]\d+)?$")

# Bookkeeping stored on MRML nodes so it survives save / reload
ROI_NODE_ATTRIBUTE = "EasyFusion.SUVROIList"
ROI_MODEL_ATTRIBUTE = "EasyFusion.SUVROISpheres"
ROI_PET_REFERENCE_ROLE = "EasyFusionPETVolume"
ROI_HANDLES_ATTRIBUTE = "EasyFusion.SUVROIRadiusHandles"

# One segment per ROI: voxels inside the sphere at or above the threshold (MTV / TLG)
ROI_SEGMENTATION_ATTRIBUTE = "EasyFusion.SUVROISegmentation"
ROI_SEGMENTATION_PET_ROLE = "EasyFusionSegmentationPET"
SETTINGS_THRESHOLD_MODE = "EasyFusion.ThresholdMode"
SETTINGS_RELATIVE_THRESHOLD = "EasyFusion.RelativeThreshold"
SETTINGS_ABSOLUTE_THRESHOLD = "EasyFusion.AbsoluteThreshold"
SETTINGS_ROI_LABEL_FIELDS = "EasyFusion/RoiLabelFields"
# Folder of the last ROI table export (application setting, like the label fields)
SETTINGS_ROI_EXPORT_FOLDER = "EasyFusion/RoiExportFolder"
# Which corners were on when EasyFusion hid the annotations, e.g. "1,1,0" (restored by showing them again)
SETTINGS_SLICER_ANNOTATION_CORNERS = "EasyFusion/SlicerAnnotationCorners"
SETTINGS_SHOW_WINDOW_INFO = "EasyFusion/ShowWindowInfo"
SETTINGS_SYNC_VIEWS = "EasyFusion/SyncViews"   # same-orientation views scroll / pan / zoom together (default on)
# Wait for the layout manager to create / resize view widgets before grouping the views
VIEW_SYNC_DELAY_MS = 250
# MIP zoom fit: the 3D view always starts showing this much anatomy from top to bottom (mm)
MIP_FIT_HEIGHT_MM = 1200.0
# Fit again once the orthographic switch and the layout change have been applied (after alignSliceViews' 150 ms)
MIP_FIT_DELAY_MS = 300
# Slice views are fitted again after a layout switch, once the views have their final size (the Monitor 2 window
# is placed after DUAL_MONITOR_PLACE_DELAY_MS, then maximized: dual monitor layouts get a second, later pass)
SLICE_FIT_DELAY_MS = 450
SLICE_FIT_DUAL_MONITOR_DELAY_MS = 900
SLICE_ASPECT_CHECK_DELAY_MS = 1600

# SUV text is drawn by a separate, locked label layer so it can be white and sit one radius
# away from the center. One anchor per ROI lies in only one of the three standard planes, on the
# screen's upper-right diagonal of that view: axial (-R,+A), coronal (-R,+S), sagittal (-A,+S).
ROI_LABELS_ATTRIBUTE = "EasyFusion.SUVROILabels"
ROI_SET_ROLE_ATTRIBUTES = {ROLE_POINTS: ROI_NODE_ATTRIBUTE, ROLE_HANDLES: ROI_HANDLES_ATTRIBUTE,
                           ROLE_LABELS: ROI_LABELS_ATTRIBUTE, ROLE_SPHERES: ROI_MODEL_ATTRIBUTE,
                           ROLE_SEGMENTS: ROI_SEGMENTATION_ATTRIBUTE}
# Wait for both selectors before updating the views (e.g. one change of each in a row)
INPUT_UPDATE_DELAY_MS = 150

# (button text, color node name); PET-DICOM and Hot Metal Blue on the second row
PET_COLOR_MAP_BUTTONS = [
    [("Hot Iron", HOT_IRON_NAME), ("Inferno", "Inferno"), ("Rainbow-2", "PET-Rainbow2")],
    [("PET-DICOM", "PET-DICOM"), ("Red", "Red"), ("Hot Metal Blue", "PET-HotMetalBlue")],
]
# Color map combo box (applied by "Go")
FUSION_COLOR_MAPS = {"Hot Iron": HOT_IRON_NAME, "Inferno": "Inferno", "Rainbow": "PET-Rainbow2"}
# The 3D view that shows the MIP: the only 3D view of every EasyFusion layout (and of Slicer's four-up)
MIP_VIEW_TAG = "1"
MIP_RAYCAST_TECHNIQUE = 2   # vtkMRMLViewNode::MaximumIntensityProjection
# Renderings of other modules kept out of the MIP view (dosimetry isodose / segment models, other volume
# renderings, segmentations in 3D): they would cover the MIP
# EasyFusion's own volume rendering display node of the PET (a volume can have several: e.g. the LSF calculator keeps
# its own MIP of the same SPECT; sharing one display node made each module take the MIP away from the other)
MIP_DISPLAY_ATTRIBUTE = "EasyFusion.MIPDisplay"
FOREIGN_MIP_ATTRIBUTES = ("LSFcalc.MIPDisplay", "EponaCompare.MIPDisplay")
MODULE_SHORTCUT_OWNER = "Easy_fusion"
FOREIGN_DISPLAY_CLASSES = ("vtkMRMLVolumeRenderingDisplayNode", "vtkMRMLModelDisplayNode",
                           "vtkMRMLSegmentationDisplayNode")

SETTINGS_NODE_TAG = "EasyFusion"
SETTINGS_PET_ROLE = "EasyFusionPET"
SETTINGS_CT_ROLE = "EasyFusionCT"
# "true" on the settings node: EasyFusion was the module shown when the scene was saved (reopened on load)
SETTINGS_ACTIVE_MODULE = "WasActiveModule"
SETTINGS_FILTER_MODEL_FOLDER = "EasyFusion/FilterModelFolder"  # application setting (qt.QSettings)


class Easy_fusion(ScriptedLoadableModule):
    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        parent.title = "Epona - SPECT/PET Review"
        parent.categories = ["Nuclear Medicine",""]
        parent.dependencies = []
        parent.contributors = ["Burak Demir, MD, FEBNM"]
        iconUrl = qt.QUrl.fromLocalFile(os.path.join(os.path.dirname(__file__), "Resources", "Icons", "Easy_fusion.png")).toString()        
        parent.helpText = f"""
        This module provides easy fusion of SPECT/PET and CT/MR images.
        Spherical ROIs report Max and Mean, plus a thresholded segment inside each ROI
        (default 40% of Max, or an absolute value) giving segment Mean, MTV and TLG.
        The label next to each ROI in the views shows Max and the segment Mean. Values are read
        directly from the selected SPECT/PET volume. Whether they are SUV, Bq/mL or counts is taken from the
        volume's units, its DICOM header or (for SUV) its values, and a unit is shown only when it is known.
        A volume known to be in SUV starts at SUV 0-10, any other at 0-100% of its maximum; over fusion, PET and
        3D views F5-F9 give the SUV presets for SUV volumes and 10-100% of the maximum otherwise.
        Press Insert over a slice view to drop
        an ROI at the cursor; drag the yellow edge handles to resize it.
        PET presets set a fixed SUV range; SPECT presets set 0 to a percentage of the maximum count in the image.
        Keyboard windowing with the mouse over a view: F5-F8 = abdomen, head, lungs, bones on CT views;
        F5-F9 = SUV 0-5, 0-7, 0-10, 0-15, 0-25 on fusion, PET and 3D views (0-10 ... 100% of max if not SUV).
        Layout buttons switch between four-up, axial fusion/CT/PET, 2x2 + 3D, CT | fusion | PET + 3D and two
        dual monitor layouts (the dual monitor layouts need Slicer 5.2 or later).
        Below them, "Slicer Annotations" shows or hides Slicer's own slice view annotations, and "Window Info"
        shows the CT window / level and the SPECT/PET range in the bottom-right corner of each slice view.
        Post-processing filters run the AI denoising / super-resolution models of the Belenos PET Denoise module
        (a .pth file with its .txt parameter file) on the SPECT/PET or CT/MRI volume. The published models are
        listed right away and downloaded from GitHub the first time they are applied; models of your own can be
        added by choosing their folder. The result is always a new
        volume (the original is kept unchanged) and the views, MIP and SUV ROIs switch to it. SUVs measured on
        a filtered volume differ from those of the original.
        <p align="center"><img src="{iconUrl}" width="300"></p>
        """
        parent.acknowledgementText = """
        This file was developed by Burak Demir.
        """
        parent.icon = qt.QIcon(os.path.join(os.path.dirname(__file__), "Resources", "Icons", "Easy_fusion.png"))

        # Scene-load fixes (Hot Iron repair, no auto-rotation) must work even if the
        # EasyFusion GUI has not been opened yet in this Slicer session.
        slicer.app.connect("startupCompleted()", registerSceneObservers)


# ---------------------------------------------------------------------------
# Scene-level observers (registered once per session)
# ---------------------------------------------------------------------------

_sceneObserverTags = []
# Views of the same orientation (e.g. axial fusion, axial CT, axial PET) keep the same slice position, pan and zoom
# (the same logic as the compare module). Module level: one per Slicer session, cleared on module cleanup.
_viewSync = SliceGeometrySync()


def registerSceneObservers():
    """
    Observe scene loading so saved scenes come back in a consistent state. Safe to call repeatedly.
    Layouts are registered once here (at startup); Slicer keeps them across scene close/load, so
    nothing touches the layout node while a scene is being loaded.
    """
    if _sceneObserverTags:
        return
    scene = slicer.mrmlScene
    for eventName, handler in (("StartCloseEvent", _onSceneStartClose),
                               ("StartImportEvent", _onSceneStartImport),
                               ("EndImportEvent", _onSceneEndImport),
                               ("StartSaveEvent", _onSceneStartSave),
                               ("EndSaveEvent", _onSceneEndSave)):
        event = getattr(slicer.vtkMRMLScene, eventName, None)
        if event is None:
            logging.warning(f"EasyFusion: this Slicer has no vtkMRMLScene.{eventName}")
            continue
        _sceneObserverTags.append(scene.AddObserver(event, handler))
    try:
        # Single screen: a restored dual monitor layout must not build the Monitor 2 window, even for a moment
        Easy_fusionLogic.ensureLayoutsRegistered(singleScreenSafe=not hasMultipleScreens())
        Easy_fusionLogic.reapplyRestoredCustomLayout()
        Easy_fusionLogic.ensureLayoutsRegistered()  # real dual monitor layouts for the buttons
        Easy_fusionLogic.placeSecondaryViewportWindowAfterLoad()  # Slicer restored a dual monitor layout
    except Exception:
        logging.exception("EasyFusion: could not register layouts")


def _onSceneStartClose(caller, event):
    unbindWindowLevelSync()


def _onSceneStartImport(caller, event):
    # A saved scene stores only the layout ID: the EasyFusion descriptions must exist before the layout node is read.
    # With a single screen, the dual monitor IDs temporarily point at the fallback layout, so a scene saved on a
    # dual monitor workstation never builds the floating Monitor 2 window here (that crashed Slicer). After the load,
    # switchDualMonitorLayoutIfSingleScreen() moves to the real fallback ID and restores the dual monitor layouts.
    try:
        Easy_fusionLogic.ensureLayoutsRegistered(singleScreenSafe=not hasMultipleScreens())
    except Exception:
        logging.exception("EasyFusion: could not register layouts before loading a scene")


# While a scene file is written, PET-only views point at the real PET instead of the unsaved twin,
# so saved scenes never reference a node ID that does not exist in the file.
_saveSwaps = []


def _onSceneStartSave(caller, event):
    try:
        _saveSwaps[:] = Easy_fusionLogic.pointPetOnlyViewsAtSourcePet()
    except Exception:
        logging.exception("EasyFusion: could not prepare PET-only views for saving")
    try:
        Easy_fusionLogic.writeActiveModuleFlag()
    except Exception:
        logging.exception("EasyFusion: could not record whether the module is open")


def _onSceneEndSave(caller, event):
    # Restored from the event loop: the MRML file may still be written after this notification returns
    swaps = list(_saveSwaps)
    _saveSwaps[:] = []
    if swaps:
        qt.QTimer.singleShot(0, lambda: _restoreAfterSave(swaps))


def _restoreAfterSave(swaps):
    try:
        Easy_fusionLogic.restorePetOnlyViews(swaps)
    except Exception:
        logging.exception("EasyFusion: could not restore PET-only views after saving")


# Post-load work runs as ONE ordered chain: scene repairs first, then the module panel (if it exists).
# Separate timers per observer used to interleave with each other and with Slicer's own view rebuilding.
_postLoadListeners = []
_postLoadState = {"pending": False}


def addPostLoadListener(callback):
    if callback not in _postLoadListeners:
        _postLoadListeners.append(callback)


def removePostLoadListener(callback):
    if callback in _postLoadListeners:
        _postLoadListeners.remove(callback)


def _onSceneEndImport(caller, event):
    # Nothing is changed inside the import notification itself: the layout manager may still be
    # rebuilding views for the loaded layout. All repairs run afterwards from the event loop.
    if _postLoadState["pending"]:
        return
    _postLoadState["pending"] = True
    runWhenSceneSettled(_afterSceneLoad)


def _afterSceneLoad():
    _postLoadState["pending"] = False
    logic = Easy_fusionLogic()
    # First, before anything creates nodes: scenes saved by earlier versions contain views that refer to
    # the unsaved PET-only twin by ID. A new node given that ID would be picked up half-built.
    steps = [logic.clearDanglingViewReferences,
             logic.stopAllViewRotations,
             logic.repairHotIronColorNodes,
             logic.switchDualMonitorLayoutIfSingleScreen,
             logic.restoreViewRolesAfterLoad,
             logic.placeSecondaryViewportWindowAfterLoad]
    steps += list(_postLoadListeners)
    # Last: opening the module creates its panel if needed, which restores itself from the loaded scene
    steps.append(logic.reopenModuleSavedAsActive)
    for step in steps:
        if sceneIsBusy():
            # Another load / close started in the meantime; its own EndImport schedules a new pass
            return
        try:
            step()
        except Exception:
            logging.exception(f"EasyFusion: post-load step {getattr(step, '__name__', step)} failed")


# ---------------------------------------------------------------------------
# Widget
# ---------------------------------------------------------------------------

class Easy_fusionWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):

    def __init__(self, parent=None):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)
        self.logic = None
        self.observedViewNode = None
        # Epona's SUV ROIs (EponaLib.roiset); its nodes keep the attributes of earlier versions, so saved
        # scenes keep their ROIs. ROIs stay where they were placed (world), whatever PET they are measured on.
        self.roiSet = SphereRoiSet(
            "Epona", "SUV", self.onRoiNodeModified, roleAttributes=ROI_SET_ROLE_ATTRIBUTES,
            petReferenceRole=ROI_PET_REFERENCE_ROLE, segmentationPetRole=ROI_SEGMENTATION_PET_ROLE,
            followPetTransform=False)
        self._suppressInputUpdate = 0    # > 0 while the panel itself sets the volume selectors
        self._mriSample = (None, None)   # ((node ID, image MTime), tissue sample) for the MRI presets
        self._roiTableRows = []          # rows currently shown in the ROI table (for the TSV export)
        self._roiCenters = {}            # ROI control point ID -> center (world, mm) at last update
        self._panelButtons = []
        self.toolbar = None              # top toolbar shown while the module is open (setupToolbar)
        self._syncingToolbarSelectors = False
        # The compare module's views get its own overlays
        self.mipOverlay = MipRoiOverlay(
            viewFilter=lambda viewNode: not isCompareView(viewNode.GetLayoutName()))
        self.windowInfoOverlay = SliceWindowInfoOverlay(
            lambda: (self.inputVolumeSelector.currentNode(), self.inputVolumeSelectorCT.currentNode()),
            viewFilter=lambda viewName: not isCompareView(viewName))
        self.filterLogic = None
        self._filterRunning = False
        self._filterParams = None     # parameters of the selected model (from its .txt sidecar)
        self._filterNotes = {}        # other sidecar lines (training data, reported SUV bias, ...)
        self._publishedModels = mergePublishedModelCatalog()  # replaced by the saved / fetched GitHub lists
        self._petValueUnit = ""           # unit of the selected SPECT/PET values ("SUV", ...; "" when not known)
        self._modelCatalogChecked = 0.0   # time of the last successful check of the GitHub release lists
        self._filterModelEntries = {}     # selector key -> entry of listFilterModels
        self._filterModelKeys = []        # selector keys in order (to see whether the list changed)
        self._filterSidecarMissing = False
        self._sidecarFetchFailed = set()  # published models whose .txt could not be downloaded this session
        self.filterCropRoiNode = None  # adjustable box of "Limit to ROI"

    def setup(self):
        ScriptedLoadableModuleWidget.setup(self)
        self.logic = Easy_fusionLogic()
        self.filterLogic = Easy_fusionFilterLogic()

        parametersCollapsibleButton = ctk.ctkCollapsibleButton()
        parametersCollapsibleButton.text = "Parameters"
        self.layout.addWidget(parametersCollapsibleButton)
        formLayout = qt.QFormLayout(parametersCollapsibleButton)

        # Input volumes
        # Labels follow the selected volumes: PET or SPECT, CT or MRI ("SPECT/PET", "CT/MRI" when not known)
        self.inputVolumeSelector = self._createVolumeSelector("Select the SPECT/PET image for fusion.")
        self.petSelectorLabel = qt.QLabel("SPECT/PET:")
        formLayout.addRow(self.petSelectorLabel, self.inputVolumeSelector)
        self.inputVolumeSelectorCT = self._createVolumeSelector("Select the CT/MR image for fusion.")
        self.ctSelectorLabel = qt.QLabel("CT/MRI:")
        formLayout.addRow(self.ctSelectorLabel, self.inputVolumeSelectorCT)

        self.petColorMapSelector = qt.QComboBox()
        self.petColorMapSelector.addItems(list(FUSION_COLOR_MAPS.keys()))
        formLayout.addRow("PET Color Map:", self.petColorMapSelector)

        self.FusionButton = qt.QPushButton("Go")
        self.FusionButton.connect("clicked(bool)", self.DoFusion)
        formLayout.addRow(self.FusionButton)

        self.toolbarButton = qt.QPushButton("Hide Toolbar" if isToolbarEnabled() else "Show Toolbar")
        self.toolbarButton.setToolTip(
            "Row of small buttons at the top of the window while this module is open: volumes, Go, window presets,\n"
            "color maps, layouts, MIP rotation and quick views, hiding this panel. Shared with Epona Compare.")
        self.toolbarButton.connect("clicked()", self.onToolbarButtonClicked)
        formLayout.addRow(self.toolbarButton)

        # MIP rotation. The toggle button always mirrors the 3D view node (see updateRotationButton),
        # so it can never get out of sync with what the view is actually doing.
        self.rotationSpeedSlider = ctk.ctkSliderWidget()
        self.rotationSpeedSlider.singleStep = 10
        self.rotationSpeedSlider.minimum = 10
        self.rotationSpeedSlider.maximum = 200
        self.rotationSpeedSlider.value = 50
        self.rotationSpeedSlider.toolTip = "Lower is faster (ms per step)"
        formLayout.addRow("MIP Rotation Speed (ms):", self.rotationSpeedSlider)

        self.toggleRotationButton = qt.QPushButton("Start MIP Rotation")
        self.toggleRotationButton.checkable = True
        formLayout.addRow(self.toggleRotationButton)
        self.toggleRotationButton.connect('toggled(bool)', self.setRotationEnabled)
        self.rotationSpeedSlider.connect('valueChanged(double)', self.updateRotationSpeed)

        formLayout.addRow("Quick View:", self._buttonRow([
            ("Anterior", lambda: self.rotateMIPToViewAxis(3)),
            ("Left", lambda: self.rotateMIPToViewAxis(0)),
            ("Right", lambda: self.rotateMIPToViewAxis(1)),
        ]))

        # Window / level presets
        formLayout.addRow("CT Presets:", self._buttonRow([
            (text, lambda window=window, level=level: self.setCTWindow(window, level),
             f"Shortcut: {key} with the mouse over a CT view")
            for text, window, level, key in CT_WINDOW_PRESETS]))

        formLayout.addRow("PET Presets (SUV):", self._buttonRow([
            (text, lambda upper=upper: self.setPETWindow(upper, upper / 2.0),
             f"Shortcut: {key} with the mouse over a fusion, PET or 3D view (for a volume in SUV)")
            for text, upper, key in PET_SUV_PRESETS]))

        formLayout.addRow("SPECT Presets (% max):", self._buttonRow(
            [(f"0–{percent}%", lambda percent=percent: self.setPETWindowPercentOfMax(percent))
             for percent in SPECT_PERCENT_OF_MAX_PRESETS],
            toolTip="Window from 0 to this percentage of the maximum count (voxel value) in the SPECT/PET volume."))

        formLayout.addRow("MRI Presets (relative):", self._buttonRow([
            (text, lambda low=low, high=high, text=text: self.setMRIWindowPercentile(low, high, text),
             f"Window from percentile {low:g} to {high:g} of the tissue intensities in the CT/MRI volume\n"
             f"(air and background left out), for MRI or any image without absolute units.\n"
             f"Shortcut: {key} with the mouse over a CT/MRI view when that volume is an MRI.")
            for text, low, high, key in MRI_PERCENTILE_PRESETS]))

        # PET color maps (two rows, no label on the second)
        for rowIndex, row in enumerate(PET_COLOR_MAP_BUTTONS):
            formLayout.addRow("PET Color Maps:" if rowIndex == 0 else "", self._buttonRow([
                (text, lambda colorNodeName=colorNodeName: self.setPETColorMap(colorNodeName))
                for text, colorNodeName in row]))

        # F5-F9 with the mouse over a view: CT presets on CT views, SUV presets on fusion / PET / 3D views
        for key in WINDOW_SHORTCUT_KEYS:
            self._addApplicationShortcut(getattr(qt.Qt, f"Key_{key}"), lambda key=key: self.onWindowShortcut(key))

        self.setupLayoutSection()
        self.setupMeasurementSection()
        self.setupFilterSection()



        bannerPath = os.path.join(os.path.dirname(__file__), "Resources", "Icons", "fusbanner.jpg")
        if os.path.exists(bannerPath):
            bannerLabel = qt.QLabel()
            bannerLabel.setPixmap(qt.QPixmap(bannerPath).scaledToWidth(600, qt.Qt.SmoothTransformation))
            bannerLabel.setAlignment(qt.Qt.AlignCenter)
            self.layout.addWidget(bannerLabel)
        else:
            logging.warning(f"EasyFusion: banner file not found at {bannerPath}")

        infoTextBox = qt.QTextEdit()
        infoTextBox.setReadOnly(True)
        infoTextBox.setPlainText(
            "This module provides eased visualization of PET images.\n"
            "This module is NOT a medical device. Research use only.\n"
            "Developed by: Burak Demir, MD, FEBNM \n"
            "For support and feedback: 4burakfe@gmail.com\n"
            "Version: alpha v1.0"
        )
        infoTextBox.setToolTip("Module information and instructions.")
        self.layout.addWidget(infoTextBox)

        # Observers
        registerSceneObservers()  # in case the module was added after startup
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)
        addPostLoadListener(self.onSceneLoaded)  # runs after the scene repairs, in the same deferred pass
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.NodeAboutToBeRemovedEvent, self.onNodeAboutToBeRemoved)
        self.inputVolumeSelector.connect("currentNodeChanged(vtkMRMLNode*)", self.onPETVolumeChanged)
        # After the first "Go": a new SPECT/PET or CT/MRI selection updates the views right away
        self.inputUpdateTimer = qt.QTimer()
        self.inputUpdateTimer.setSingleShot(True)
        self.inputUpdateTimer.setInterval(INPUT_UPDATE_DELAY_MS)
        self.inputUpdateTimer.connect('timeout()', self.applyChangedInputVolumes)
        self.inputVolumeSelector.connect("currentNodeChanged(vtkMRMLNode*)", self.onInputVolumeSelectionChanged)
        self.inputVolumeSelectorCT.connect("currentNodeChanged(vtkMRMLNode*)", self.onInputVolumeSelectionChanged)
        if slicer.app.layoutManager() is not None:
            slicer.app.layoutManager().connect("layoutChanged(int)", self.onLayoutChanged)

        self.observeThreeDViewNode()
        with self.selectorsSetByPanel():
            self.restoreFromSettings()
            self.connectToExistingRois()
        self.setupToolbar()
        self.onLayoutChanged()
        self.updateSelectorLabels()

    # ------------------------------------------------------------------
    # Toolbar (top of the window while the module is open)
    # ------------------------------------------------------------------

    def setupToolbar(self):
        """The basic functions as small picture buttons. ROIs and AI filters stay in the panel only."""
        toolbar = EponaToolBar("EponaToolBar", "Epona", moduleName=MODULE_NAME,
                               onEnabledChanged=self.onToolbarEnabledChanged)
        if not toolbar.build():
            return
        self.toolbar = toolbar

        # Copies of the panel's volume selectors: choosing here is the same as choosing in the panel
        self.toolbarSelectors = {}
        self.toolbarSelectorLabels = {}
        for key, label, panelSelector, tip in (
                ("pet", "PET", self.inputVolumeSelector, "SPECT/PET volume (same as in the module panel)"),
                ("ct", "CT", self.inputVolumeSelectorCT, "CT/MRI volume (same as in the module panel)")):
            selector = self._createVolumeSelector(tip)
            selector.selectNodeUponCreation = False   # the panel selector picks new volumes; this one follows
            selector.setMinimumWidth(TOOLBAR_SELECTOR_WIDTH)   # a qMRMLNodeComboBox is not a QComboBox
            selector.setMaximumWidth(int(TOOLBAR_SELECTOR_WIDTH * 1.4))
            selector.setCurrentNode(panelSelector.currentNode())
            selector.connect("currentNodeChanged(vtkMRMLNode*)",
                             lambda node, panelSelector=panelSelector: self.onToolbarSelectorChanged(panelSelector, node))
            panelSelector.connect("currentNodeChanged(vtkMRMLNode*)", self.syncToolbarSelectors)
            self.toolbarSelectorLabels[key] = toolbar.addLabel(label, tip)
            toolbar.addWidget(selector)
            self.toolbarSelectors[key] = selector
        toolbar.addButton("go", richToolTip("Go", "Fuse the selected volumes and fill the views (same as Go in the "
                                                  "panel)"), self.DoFusion)

        # Window presets: CT or MRI presets for the CT/MRI, SUV or % of maximum for the SPECT/PET (see syncToolbar)
        toolbar.addSeparator()
        for text, window, level, key in CT_WINDOW_PRESETS:
            toolbar.addPresetButton(CT_PRESET_ICONS[text], richToolTip(text, f"W {window:g}  L {level:g}\nShortcut: {key} "
                                                                          "over a CT view"),
                                    lambda window=window, level=level: self.setCTWindow(window, level), group="ct")
        for text, low, high, key in MRI_PERCENTILE_PRESETS:
            toolbar.addPresetButton(MRI_PRESET_ICONS[text],
                                    richToolTip(f"MRI: {text}", f"Percentile {low:g} to {high:g} of the tissue "
                                                f"intensities\nShortcut: {key} over an MRI view"),
                                    lambda low=low, high=high, text=text: self.setMRIWindowPercentile(low, high, text),
                                    group="mri")
        toolbar.addSeparator()
        for text, upper, key in PET_SUV_PRESETS:
            toolbar.addPresetButton(SUV_PRESET_ICONS[upper], richToolTip(f"SUV {text}", f"Shortcut: {key} over a "
                                                                         "fusion, PET or 3D view"),
                                    lambda upper=upper: self.setPETWindow(upper, upper / 2.0), group="suv")
        for percent in SPECT_PERCENT_OF_MAX_PRESETS:
            toolbar.addPresetButton(PERCENT_PRESET_ICONS[percent],
                                    richToolTip(f"0–{percent}% of maximum", "Window from 0 to this percentage of the "
                                                "highest voxel value"),
                                    lambda percent=percent: self.setPETWindowPercentOfMax(percent), group="percent")

        # Color maps (swatches drawn from the color tables)
        toolbar.addSeparator()
        for row in PET_COLOR_MAP_BUTTONS:
            for text, colorNodeName in row:
                toolbar.addColorButton(colorNodeName, richToolTip(text, "SPECT/PET color map"),
                                       lambda colorNodeName=colorNodeName: self.setPETColorMap(colorNodeName),
                                       rgbFunction=hotIronRGB if colorNodeName == HOT_IRON_NAME else None)

        # Layouts (pictures; the name shows on hover)
        toolbar.addSeparator()
        toolbar.addLayoutButtons([(layoutID, LAYOUT_ICONS[layoutID], richToolTip(text, tooltip))
                                  for layoutID, text, tooltip, _, _ in self.layoutButtonSpecs], self.setEasyFusionLayout)

        toolbar.addSeparator()
        toolbar.addRotationButton(self.setRotationEnabled)
        for letter, axis, name in (("A", 3, "Anterior"), ("L", 0, "Left"), ("R", 1, "Right")):
            toolbar.addTextButton(letter, richToolTip(f"MIP: {name} view", "Stops the rotation"),
                                  lambda axis=axis: self.rotateMIPToViewAxis(axis))
        toolbar.addWindowButtons()
        self.syncToolbar()

    def onToolbarSelectorChanged(self, panelSelector, node):
        if self._syncingToolbarSelectors:
            return
        if panelSelector.currentNode() is not node:
            panelSelector.setCurrentNode(node)   # the panel's own handlers run, as for a choice in the panel

    def syncToolbarSelectors(self, node=None):
        if self.toolbar is None:
            return
        self._syncingToolbarSelectors = True
        try:
            for key, panelSelector in (("pet", self.inputVolumeSelector), ("ct", self.inputVolumeSelectorCT)):
                selector = self.toolbarSelectors[key]
                if selector.currentNode() is not panelSelector.currentNode():
                    selector.setCurrentNode(panelSelector.currentNode())
        finally:
            self._syncingToolbarSelectors = False
        self.updateToolbarPresetGroups()

    def updateToolbarPresetGroups(self):
        """Only the presets that fit the volumes: CT or MRI presets; SUV presets only for a volume known to be in
        SUV, else % of maximum."""
        if self.toolbar is None:
            return
        isCT = self.ctMriLooksLikeCT()
        self.toolbar.setGroupVisible("ct", isCT)
        self.toolbar.setGroupVisible("mri", not isCT)
        pet = self.inputVolumeSelector.currentNode()
        isSuv = pet is not None and petValueInfo(pet).kind == VALUE_KIND_SUV
        self.toolbar.setGroupVisible("suv", isSuv)
        self.toolbar.setGroupVisible("percent", not isSuv)

    def syncToolbar(self):
        if self.toolbar is None:
            return
        self.syncToolbarSelectors()
        layoutManager = slicer.app.layoutManager()
        self.toolbar.setCurrentLayout(layoutManager.layout if layoutManager is not None else None)
        viewNode = self.observedViewNode
        self.toolbar.setRotating(viewNode is not None and viewNode.GetAnimationMode() == ANIMATION_SPIN)

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

    @staticmethod
    def _createVolumeSelector(toolTip):
        selector = slicer.qMRMLNodeComboBox()
        selector.nodeTypes = ["vtkMRMLScalarVolumeNode"]
        selector.selectNodeUponCreation = True
        selector.addEnabled = False
        selector.removeEnabled = False
        selector.noneEnabled = False
        selector.showHidden = False
        selector.showChildNodeTypes = False
        selector.setMRMLScene(slicer.mrmlScene)
        selector.setToolTip(toolTip)
        return selector

    def _buttonRow(self, buttonSpecs, toolTip=None):
        """Horizontal row of push buttons from (text, callback) or (text, callback, tooltip) tuples."""
        rowLayout = qt.QHBoxLayout()
        for spec in buttonSpecs:
            text, callback = spec[0], spec[1]
            button = qt.QPushButton(text)
            buttonToolTip = spec[2] if len(spec) > 2 else toolTip
            if buttonToolTip:
                button.setToolTip(buttonToolTip)
            button.connect('clicked()', callback)
            rowLayout.addWidget(button)
            # Keep a Python reference: the row layout has no parent widget yet, and PythonQt deletes
            # parentless widgets whose wrapper is garbage collected.
            self._panelButtons.append(button)
        return rowLayout

    def _addApplicationShortcut(self, keyCode, callback):
        """
        Application-wide shortcut (the Monitor 2 viewport is a separate window), shared with the compare module:
        with the mouse over a compare view the compare module takes the key, otherwise Epona does.
        """
        registerShortcut(MODULE_SHORTCUT_OWNER, keyCode, callback)

    def setupLayoutSection(self):
        layoutCollapsibleButton = ctk.ctkCollapsibleButton()
        layoutCollapsibleButton.text = "Layouts"
        self.layout.addWidget(layoutCollapsibleButton)
        layoutFormLayout = qt.QFormLayout(layoutCollapsibleButton)

        buttonGrid = qt.QGridLayout()
        self.layoutButtonGroup = qt.QButtonGroup()
        self.layoutButtonGroup.setExclusive(True)
        self.layoutButtons = {}
        layoutButtonSpecs = [
            (LAYOUT_FOUR_UP_ID, "Four-Up", "Axial, sagittal and coronal fusion + 3D MIP", 0, 0),
            (LAYOUT_AXIAL_FOUR_UP_ID, "Axial Four-Up",
             "Top: axial fusion | 3D MIP\nBottom: axial CT | axial PET (inverted grey)", 0, 1),
            (LAYOUT_TWO_BY_TWO_ID, "2×2 + 3D",
             "Top: axial fusion | axial CT\nBottom: sagittal fusion | sagittal CT\nRight column: 3D MIP", 1, 0),
            (LAYOUT_CT_FUSION_PET_3D_ID, "CT | Fusion | PET + 3D",
             "Top: axial CT | axial fusion | axial PET (inverted grey)\n"
             "Bottom: sagittal CT | sagittal fusion | sagittal PET (inverted grey)\n"
             "Right column: 3D MIP", 1, 1),
            (LAYOUT_DUAL_MONITOR_ID, "Dual Monitor",
             "Monitor 1: axial and sagittal fusion | CT | PET (3×2)\n"
             "Monitor 2 (separate window): 3D MIP | coronal fusion | coronal CT\n"
             "Click again to bring the second window back if it was closed.", 2, 0),
            (LAYOUT_DUAL_MONITOR_FUSION_MIDDLE_ID, "Dual Monitor (Fusion Middle)",
             "Monitor 1: axial and sagittal CT | fusion | PET (3×2)\n"
             "Monitor 2 (separate window): 3D MIP | coronal fusion | coronal CT\n"
             "Click again to bring the second window back if it was closed.", 2, 1),
        ]
        self.layoutButtonSpecs = layoutButtonSpecs   # also used by the toolbar
        for layoutID, text, tooltip, row, column in layoutButtonSpecs:
            button = qt.QPushButton(text)
            button.checkable = True
            button.setToolTip(tooltip)
            button.connect('clicked()', lambda layoutID=layoutID: self.setEasyFusionLayout(layoutID))
            self.layoutButtonGroup.addButton(button)
            buttonGrid.addWidget(button, row, column)
            self.layoutButtons[layoutID] = button
        layoutFormLayout.addRow(buttonGrid)

        # Text in the slice views
        textRow = qt.QHBoxLayout()
        self.sliceAnnotationsButton = qt.QPushButton("Slicer Annotations")
        self.sliceAnnotationsButton.checkable = True
        self.sliceAnnotationsButton.setToolTip(
            "Show or hide Slicer's built-in slice view annotations (Data Probe): patient and study\n"
            "information and the names of the shown volumes in the corners of the slice views.\n"
            "It switches Slicer's active corners (top left, top right, bottom left), as Slicer's own\n"
            "settings do, so the choice is remembered after a restart. Showing them again restores the\n"
            "corners that were on before.")
        self.sliceAnnotationsButton.connect("toggled(bool)", self.onSlicerAnnotationsToggled)
        textRow.addWidget(self.sliceAnnotationsButton)
        self.windowInfoButton = qt.QPushButton("Window Info (bottom right)")
        self.windowInfoButton.checkable = True
        self.windowInfoButton.setToolTip(
            "Show the current windowing in the bottom-right corner of every slice view:\n"
            "CT (or MRI) window / level, and the SPECT/PET display range (in SUV for PET).\n"
            "Fusion views show both. It follows presets, F5-F9 and mouse window / level drags.")
        self.windowInfoButton.connect("toggled(bool)", self.onWindowInfoToggled)
        textRow.addWidget(self.windowInfoButton)
        layoutFormLayout.addRow("Slice view text:", textRow)

        self.syncViewsCheckBox = qt.QCheckBox("Keep same-orientation views together (scroll, pan, zoom)")
        self.syncViewsCheckBox.setToolTip(
            "Axial fusion, axial CT and axial PET (and the sagittal / coronal views) always show the same slice,\n"
            "pan and zoom: scroll or zoom one and the others follow. The fusion view is the reference.")
        self.syncViewsCheckBox.checked = str(qt.QSettings().value(SETTINGS_SYNC_VIEWS, "true")).lower() in ("true", "1")
        self.syncViewsCheckBox.connect("toggled(bool)", self.onSyncViewsToggled)
        layoutFormLayout.addRow(self.syncViewsCheckBox)

        showWindowInfo = str(qt.QSettings().value(SETTINGS_SHOW_WINDOW_INFO, "true")).lower() in ("true", "1")
        self.windowInfoOverlay.enabled = showWindowInfo
        wasBlocked = self.windowInfoButton.blockSignals(True)
        self.windowInfoButton.checked = showWindowInfo
        self.windowInfoButton.blockSignals(wasBlocked)
        self.syncSlicerAnnotationsButton()

    # --- Slice view text ------------------------------------------------------

    @staticmethod
    def slicerSliceAnnotations():
        """Slicer's slice view annotations object (Data Probe module), or None when it is not available."""
        try:
            return slicer.modules.DataProbeInstance.infoWidget.sliceAnnotations
        except AttributeError:
            return None

    @staticmethod
    def _slicerAnnotationCorners(annotations):
        """[0/1 per corner of DATA_PROBE_CORNERS], or None if this Slicer version has no per-corner switches."""
        if not all(hasattr(annotations, attribute) for attribute, _, _ in DATA_PROBE_CORNERS):
            return None
        return [1 if getattr(annotations, attribute) else 0 for attribute, _, _ in DATA_PROBE_CORNERS]

    def slicerAnnotationsShown(self):
        annotations = self.slicerSliceAnnotations()
        if annotations is None:
            return False
        corners = self._slicerAnnotationCorners(annotations)
        return bool(annotations.sliceViewAnnotationsEnabled) and (corners is None or any(corners))

    def syncSlicerAnnotationsButton(self):
        """The button shows Slicer's current state (it may have been changed in Slicer's settings)."""
        if not hasattr(self, "sliceAnnotationsButton"):
            return
        annotations = self.slicerSliceAnnotations()
        self.sliceAnnotationsButton.enabled = annotations is not None
        if annotations is None:
            return
        wasBlocked = self.sliceAnnotationsButton.blockSignals(True)
        self.sliceAnnotationsButton.checked = self.slicerAnnotationsShown()
        self.sliceAnnotationsButton.blockSignals(wasBlocked)

    @staticmethod
    def _setCheckBox(annotations, checkBoxName, checked):
        """Mirror a change in Slicer's own settings panel (if it was created), without triggering it."""
        checkBox = getattr(annotations, checkBoxName, None)
        if checkBox is None:
            return
        try:
            wasBlocked = checkBox.blockSignals(True)
            checkBox.checked = bool(checked)
            checkBox.blockSignals(wasBlocked)
        except Exception:
            logging.debug(f"EasyFusion: could not update Slicer's {checkBoxName}", exc_info=True)

    def _clearSlicerCornerTexts(self, cornerIndexes):
        """Blank Slicer's corner texts right away (they are otherwise only rewritten on the next view change)."""
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return
        for name in layoutManager.sliceViewNames():
            sliceWidget = layoutManager.sliceWidget(name)
            sliceView = sliceWidget.sliceView() if sliceWidget is not None else None
            if sliceView is None:
                continue
            cornerAnnotation = sliceView.cornerAnnotation()
            for index in cornerIndexes:
                cornerAnnotation.SetText(index, "")
            sliceView.scheduleRender()

    def onSlicerAnnotationsToggled(self, enabled):
        """
        Show / hide Slicer's slice view annotations. Turning off only Slicer's master switch leaves the texts
        already drawn in the views, so this switches the active corners (top left, top right, bottom left),
        as Slicer's settings panel does, and remembers which were on so that showing them restores them.
        """
        annotations = self.slicerSliceAnnotations()
        if annotations is None:
            slicer.util.showStatusMessage("EasyFusion: Slicer's slice view annotations (Data Probe) are not available.",
                                          3000)
            self.syncSlicerAnnotationsButton()
            return
        settings = qt.QSettings()
        try:
            corners = self._slicerAnnotationCorners(annotations)
            if corners is None:
                # Older Slicer without per-corner switches: master switch, then blank the views ourselves
                annotations.sliceViewAnnotationsEnabled = 1 if enabled else 0
                self._setCheckBox(annotations, "sliceViewAnnotationsCheckBox", enabled)
                settings.setValue(DATA_PROBE_ANNOTATIONS_SETTING, 1 if enabled else 0)
                annotations.updateSliceViewFromGUI()
                if not enabled:
                    self._clearSlicerCornerTexts(DATA_PROBE_CORNER_INDEXES)
                return

            if enabled:
                saved = str(settings.value(SETTINGS_SLICER_ANNOTATION_CORNERS, "") or "").split(",")
                wanted = [1 if value.strip() == "1" else 0 for value in saved] if len(saved) == len(corners) else []
                if not any(wanted):
                    wanted = [1] * len(corners)  # nothing remembered (or all were off): show every corner
            else:
                if any(corners):
                    settings.setValue(SETTINGS_SLICER_ANNOTATION_CORNERS, ",".join(str(c) for c in corners))
                wanted = [0] * len(corners)

            # The master switch stays on: the annotations must keep updating to draw (or blank) the corners
            annotations.sliceViewAnnotationsEnabled = 1
            self._setCheckBox(annotations, "sliceViewAnnotationsCheckBox", True)
            settings.setValue(DATA_PROBE_ANNOTATIONS_SETTING, 1)
            for (attribute, checkBoxName, settingName), value in zip(DATA_PROBE_CORNERS, wanted):
                setattr(annotations, attribute, value)
                self._setCheckBox(annotations, checkBoxName, value)
                settings.setValue(settingName, value)
            if hasattr(annotations, "updateEnabledButtons"):
                annotations.updateEnabledButtons()
            annotations.updateSliceViewFromGUI()
            if not enabled:
                self._clearSlicerCornerTexts(DATA_PROBE_CORNER_INDEXES)
        except Exception:
            logging.exception("EasyFusion: could not switch Slicer's slice view annotations")
        finally:
            self.syncSlicerAnnotationsButton()

    def onWindowInfoToggled(self, enabled):
        qt.QSettings().setValue(SETTINGS_SHOW_WINDOW_INFO, "true" if enabled else "false")
        self.windowInfoOverlay.setEnabled(enabled)


    def setupMeasurementSection(self):
        measurementCollapsibleButton = ctk.ctkCollapsibleButton()
        measurementCollapsibleButton.text = "SUV Measurements (spherical ROI)"
        self.layout.addWidget(measurementCollapsibleButton)
        self.measurementCollapsibleButton = measurementCollapsibleButton
        measurementLayout = qt.QFormLayout(measurementCollapsibleButton)

        self.roitipLabel = qt.QLabel("Press 'Insert' key to place ROIs")
        self.roitipLabel.setStyleSheet("color: red; font-weight: bold;")
        measurementLayout.addRow(self.roitipLabel)

        self.roiRadiusSpinBox = qt.QDoubleSpinBox()
        self.roiRadiusSpinBox.setRange(1.0, 100.0)
        self.roiRadiusSpinBox.setDecimals(1)
        self.roiRadiusSpinBox.setSingleStep(1.0)
        self.roiRadiusSpinBox.setSuffix(" mm")
        self.roiRadiusSpinBox.setValue(DEFAULT_ROI_RADIUS_MM)
        self.roiRadiusSpinBox.setToolTip(
            "Radius of the ROI selected in the table. With no ROI selected: the radius given to new ROIs.")
        measurementLayout.addRow("ROI radius:", self.roiRadiusSpinBox)

        roiButtonsLayout = qt.QHBoxLayout()
        self.placeRoiButton = qt.QPushButton("Place ROI (Insert)")
        self.placeRoiButton.setToolTip(
            "Click once on a slice view to drop a spherical ROI centered there.\n"
            "Shortcut: hover over a slice view and press Insert to drop an ROI at the mouse cursor.\n"
            "Drag the center point to move the ROI; drag a yellow handle on its edge to resize it.")
        self.deleteRoiButton = qt.QPushButton("Delete Selected")
        self.clearRoisButton = qt.QPushButton("Clear All")
        roiButtonsLayout.addWidget(self.placeRoiButton)
        roiButtonsLayout.addWidget(self.deleteRoiButton)
        roiButtonsLayout.addWidget(self.clearRoisButton)
        measurementLayout.addRow(roiButtonsLayout)

        thresholdLayout = qt.QHBoxLayout()
        self.thresholdModeComboBox = qt.QComboBox()
        self.thresholdModeComboBox.addItem("% of Max", THRESHOLD_RELATIVE)
        self.thresholdModeComboBox.addItem("Absolute SUV", THRESHOLD_ABSOLUTE)
        self.thresholdModeComboBox.setToolTip(
            "Relative: segment = voxels inside the ROI with a value >= this % of the ROI's Max.\n"
            "Absolute: segment = voxels inside the ROI with a value >= this value (SUV for a PET in SUV).\n"
            "Each ROI keeps its own threshold: this changes only the ROI selected in the table.\n"
            "With no ROI selected, it sets the threshold given to new ROIs.")
        self.thresholdValueSpinBox = qt.QDoubleSpinBox()
        self.thresholdValueSpinBox.setDecimals(1)
        thresholdLayout.addWidget(self.thresholdModeComboBox)
        thresholdLayout.addWidget(self.thresholdValueSpinBox)
        measurementLayout.addRow("Segment threshold:", thresholdLayout)
        self._relativeThreshold = DEFAULT_RELATIVE_THRESHOLD
        self._absoluteThreshold = DEFAULT_ABSOLUTE_THRESHOLD
        self._applyThresholdModeToSpinBox(THRESHOLD_RELATIVE)

        # Which ROI the radius / threshold controls edit right now
        editTargetLayout = qt.QHBoxLayout()
        self.roiEditTargetLabel = qt.QLabel()
        self.roiEditTargetLabel.setStyleSheet("color: gray;")
        self.roiDeselectButton = qt.QPushButton("Deselect")
        self.roiDeselectButton.setToolTip("Deselect the ROI, so radius and threshold set the values for new ROIs.")
        editTargetLayout.addWidget(self.roiEditTargetLabel, 1)
        editTargetLayout.addWidget(self.roiDeselectButton)
        measurementLayout.addRow(editTargetLayout)

        self.roiPetLabel = qt.QLabel("Measuring on: (no PET selected)")
        self.roiPetLabel.wordWrap = True
        measurementLayout.addRow(self.roiPetLabel)

        self.showRoisOnMipCheckBox = qt.QCheckBox("Show ROI segments and values on the MIP (3D view)")
        self.showRoisOnMipCheckBox.checked = True
        self.showRoisOnMipCheckBox.setToolTip(
            "Draws the thresholded segments and the Max / Mean text on top of the MIP.\n"
            "Display only: nothing is added to the scene or saved.")
        measurementLayout.addRow(self.showRoisOnMipCheckBox)

        # Values drawn next to each ROI in the slice views and on the MIP
        labelFieldsLayout = qt.QGridLayout()
        shownFields = parseRoiLabelFields(qt.QSettings().value(SETTINGS_ROI_LABEL_FIELDS))
        self.roiLabelFieldCheckBoxes = {}
        for position, (key, text) in enumerate(ROI_LABEL_FIELDS):
            checkBox = qt.QCheckBox(text)
            checkBox.checked = key in shownFields
            checkBox.connect('toggled(bool)', self.onRoiLabelFieldsChanged)
            labelFieldsLayout.addWidget(checkBox, position // 4, position % 4)
            self.roiLabelFieldCheckBoxes[key] = checkBox
        measurementLayout.addRow("Show near ROI:", labelFieldsLayout)

        self.roiTable = qt.QTableWidget()
        self.roiTable.setColumnCount(7)
        self.roiTable.setHorizontalHeaderLabels(["ROI", "r (mm)", "Thr.", "Max", "Mean", "MTV (mL)", "TLG"])
        headerTips = ["", "ROI radius", "Segment threshold of this ROI (% of its Max, or an absolute value)",
                      "Maximum value inside the ROI sphere (SUV for a PET in SUV)",
                      "Mean value of the thresholded segment",
                      "Metabolic tumor volume: volume of the thresholded segment",
                      "Total lesion glycolysis = Mean x MTV"]
        for column, tip in enumerate(headerTips):
            headerItem = self.roiTable.horizontalHeaderItem(column)
            if headerItem is not None and tip:
                headerItem.setToolTip(tip)
        self.roiTable.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
        self.roiTable.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
        self.roiTable.setSelectionMode(qt.QAbstractItemView.SingleSelection)
        self.roiTable.horizontalHeader().setSectionResizeMode(qt.QHeaderView.ResizeToContents)
        self.roiTable.horizontalHeader().setStretchLastSection(True)
        self.roiTable.verticalHeader().setVisible(False)
        self.roiTable.setMinimumHeight(140)
        self.roiTable.setToolTip("Select a row to jump to that ROI and edit its radius and threshold.")
        measurementLayout.addRow(self.roiTable)

        exportLayout = qt.QHBoxLayout()
        exportLayout.addStretch(1)
        self.exportRoiTableButton = qt.QPushButton("Export Table (.tsv)…")
        self.exportRoiTableButton.setToolTip(
            "Save all ROIs to a tab-separated file (opens in Excel, LibreOffice, R, Python ...).\n"
            "Values are written at full precision, with ROI centers (RAS, mm), sphere and segment\n"
            "statistics, threshold, the PET volume they were measured on and the unit of its values.")
        self.exportRoiTableButton.enabled = False
        exportLayout.addWidget(self.exportRoiTableButton)
        measurementLayout.addRow(exportLayout)

        self.placeRoiButton.connect('clicked()', self.onPlaceRoi)
        self.deleteRoiButton.connect('clicked()', self.onDeleteSelectedRoi)
        self.clearRoisButton.connect('clicked()', self.onClearRois)
        self.roiDeselectButton.connect('clicked()', self.onDeselectRoi)
        self.exportRoiTableButton.connect('clicked()', self.onExportRoiTable)
        self.updateRoiEditTarget()
        self.roiRadiusSpinBox.connect('valueChanged(double)', self.onRoiRadiusChanged)
        self.roiTable.connect('itemSelectionChanged()', self.onRoiSelectionChanged)
        self.thresholdModeComboBox.connect('currentIndexChanged(int)', self.onThresholdModeChanged)
        self.thresholdValueSpinBox.connect('valueChanged(double)', self.onThresholdValueChanged)
        self.showRoisOnMipCheckBox.connect('toggled(bool)', self.onShowRoisOnMipToggled)

        # Batch rapid point events (e.g. dragging) into one recomputation
        self.roiUpdateTimer = qt.QTimer()
        self.roiUpdateTimer.setSingleShot(True)
        self.roiUpdateTimer.setInterval(60)
        self.roiUpdateTimer.connect('timeout()', self.updateRois)

        # Insert key: drop an ROI at the mouse cursor
        self._addApplicationShortcut(qt.Qt.Key_Insert, self.onPlaceRoiAtCursor)

    def setupFilterSection(self):
        filterCollapsibleButton = ctk.ctkCollapsibleButton()
        filterCollapsibleButton.text = "Post-processing Filters (AI)"
        filterCollapsibleButton.collapsed = True
        self.layout.addWidget(filterCollapsibleButton)
        self.filterCollapsibleButton = filterCollapsibleButton
        filterLayout = qt.QFormLayout(filterCollapsibleButton)

        modelLayout = qt.QHBoxLayout()
        self.filterModelSelector = qt.QComboBox()
        self.filterModelSelector.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Fixed)
        self.filterModelSelector.setToolTip(
            "Denoising / super-resolution model (Belenos PET Denoise .pth file).\n"
            "Published models are listed first; one marked 'download' is downloaded from GitHub the first time\n"
            f"you apply it and kept in:\n{self.filterLogic.publishedModelFolder()}\n"
            "Models from your own folder (below) follow.")
        self.filterModelRefreshButton = qt.QToolButton()
        self.filterModelRefreshButton.text = "↻"
        self.filterModelRefreshButton.setToolTip(
            "Check GitHub for new or updated published models, and look again in your own folder.")
        modelLayout.addWidget(self.filterModelSelector)
        modelLayout.addWidget(self.filterModelRefreshButton)
        filterLayout.addRow("Model:", modelLayout)

        folderLayout = qt.QHBoxLayout()
        self.filterModelFolderEdit = qt.QLineEdit()
        self.filterModelFolderEdit.readOnly = True
        self.filterModelFolderEdit.placeholderText = "Optional: folder with your own .pth models and .txt files"
        self.filterModelFolderButton = qt.QPushButton("Browse…")
        self.filterModelFolderClearButton = qt.QToolButton()
        self.filterModelFolderClearButton.text = "✕"
        self.filterModelFolderClearButton.setToolTip("Stop using this folder (its files are not touched).")
        folderLayout.addWidget(self.filterModelFolderEdit)
        folderLayout.addWidget(self.filterModelFolderButton)
        folderLayout.addWidget(self.filterModelFolderClearButton)
        filterLayout.addRow("Own models:", folderLayout)

        self.filterInfoBox = qt.QPlainTextEdit()
        self.filterInfoBox.readOnly = True
        self.filterInfoBox.setMaximumHeight(120)
        self.filterInfoBox.setToolTip("Contents of the model's .txt file (parameters, training data, validation).")
        filterLayout.addRow("Model info:", self.filterInfoBox)

        self.filterTargetSelector = qt.QComboBox()
        self.filterTargetSelector.addItem("SPECT/PET", FILTER_TARGET_PET)
        self.filterTargetSelector.addItem("CT/MRI", FILTER_TARGET_CT)
        self.filterTargetSelector.setToolTip(
            "Volume the filter is applied to. Set automatically from the model's .txt file ('modality: CT') or its\n"
            "file name (a CT / MR model such as CT_superres24.pth); change it if the guess is wrong.")
        filterLayout.addRow("Apply to:", self.filterTargetSelector)

        self.filterLimitToRoiCheckBox = qt.QCheckBox("Limit to ROI (crop before filtering)")
        self.filterLimitToRoiCheckBox.setToolTip(
            "Places an adjustable box (Slicer's Crop Volume ROI). Only the voxels inside it are filtered, which is\n"
            "much faster and needs far less memory. The original volume is never cropped or changed.\n"
            "The box is removed after filtering.")
        filterLayout.addRow(self.filterLimitToRoiCheckBox)

        self.filterForceCpuCheckBox = qt.QCheckBox("Force CPU")
        self.filterForceCpuCheckBox.setToolTip(
            f"Run on the CPU even when a GPU with at least {FILTER_MIN_VRAM_GB:g} GB of memory is available.")
        filterLayout.addRow(self.filterForceCpuCheckBox)

        self.applyFilterButton = qt.QPushButton("Apply Filter")
        self.applyFilterButton.setToolTip(
            "Creates a NEW filtered volume; the original volume is not changed.\n"
            "The views, the MIP and the SUV ROIs then switch to the filtered volume.")
        filterLayout.addRow(self.applyFilterButton)

        self.filterStatusLabel = qt.QLabel("")
        self.filterStatusLabel.wordWrap = True
        filterLayout.addRow(self.filterStatusLabel)

        self.filterModelFolderButton.connect("clicked()", self.onBrowseFilterModelFolder)
        self.filterModelFolderClearButton.connect("clicked()", self.onClearFilterModelFolder)
        self.filterModelRefreshButton.connect("clicked()", lambda: self.updatePublishedModelCatalog(force=True))
        self.filterModelSelector.connect("currentIndexChanged(int)", self.onFilterModelChanged)
        self.applyFilterButton.connect("clicked()", self.onApplyFilter)
        self.filterLimitToRoiCheckBox.connect("toggled(bool)", self.onFilterLimitToRoiToggled)
        filterCollapsibleButton.connect("contentsCollapsed(bool)", self.onFilterSectionCollapsed)
        # No network access here: the saved list of published models is used until the section is opened
        releases, self._modelCatalogChecked = self.filterLogic.readSavedModelCatalog()
        self._publishedModels = mergePublishedModelCatalog(releases)
        self.restoreFilterModelFolder()

    def onReload(self):
        """Developer mode "Reload": reload the shared EponaLib modules first, then this module."""
        import EponaLib
        EponaLib.reloadAll()
        ScriptedLoadableModuleWidget.onReload(self)

    def enter(self):
        self.observeThreeDViewNode()
        self.onLayoutChanged()
        self.syncSlicerAnnotationsButton()
        if self.toolbar is not None:
            try:
                self.syncToolbar()
            except Exception:
                logging.exception("EasyFusion: could not update the toolbar")
            self.toolbar.enter()   # always: the toolbar must know the module is shown
        self.updateToolbarButton()
        if hasattr(self, "filterModelSelector") and not self._filterRunning:
            self.refreshFilterModels()  # models may have been added to the folder in the meantime

    def exit(self):
        if self.toolbar is not None:
            self.toolbar.exit()   # hidden, and the module panel comes back if the toolbar had hidden it

    def cleanup(self):
        if self.toolbar is not None:
            try:
                self.toolbar.destroy()
            except Exception:
                logging.exception("EasyFusion: could not remove the toolbar")
            self.toolbar = None
        try:
            self.mipOverlay.clear()
        except Exception:
            logging.exception("EasyFusion: could not remove the MIP overlay")
        try:
            self.windowInfoOverlay.enabled = False
            self.windowInfoOverlay.clear()
        except Exception:
            logging.exception("EasyFusion: could not remove the slice view window info")
        if hasattr(self, "roiUpdateTimer"):
            self.roiUpdateTimer.stop()
        if hasattr(self, "inputUpdateTimer"):
            self.inputUpdateTimer.stop()
        try:
            self.removeFilterCropRoi()
        except Exception:
            logging.exception("EasyFusion: could not remove the filter crop ROI")
        unregisterShortcuts(MODULE_SHORTCUT_OWNER)
        _viewSync.clear()
        self.roiSet.release()
        if slicer.app.layoutManager() is not None:
            slicer.app.layoutManager().disconnect("layoutChanged(int)", self.onLayoutChanged)
        removePostLoadListener(self.onSceneLoaded)
        self.removeObservers()

    # ------------------------------------------------------------------
    # Scene events
    # ------------------------------------------------------------------

    def onSceneEndClose(self, caller=None, event=None):
        self.mipOverlay.clear()
        self.windowInfoOverlay.scheduleUpdate()  # views are emptied: their text follows
        self.filterCropRoiNode = None
        self._setLimitToRoiChecked(False)
        self.roiSet.release()
        self.fillRoiTable([])
        _viewSync.clear()
        runWhenSceneSettled(self.refreshViewsAfterSceneChange)

    def onSceneLoaded(self):
        """Called by the post-load chain (see _afterSceneLoad), after the scene repairs."""
        with self.selectorsSetByPanel():
            self.restoreFromSettings()
            self.connectToExistingRois()
        self.refreshViewsAfterSceneChange()
        layoutManager = slicer.app.layoutManager()
        if layoutManager is not None and layoutManager.layout in DUAL_MONITOR_LAYOUT_IDS:
            self.scheduleMIPRefit(layoutManager.layout)  # the Monitor 2 window is resized after the load

    def refreshViewsAfterSceneChange(self):
        self.observeThreeDViewNode()
        self.onLayoutChanged()
        self.updateSelectorLabels()
        qt.QTimer.singleShot(SLICE_ASPECT_CHECK_DELAY_MS, self.repairSliceAspects)   # saved zoom kept, unstretched

    @vtk.calldata_type(vtk.VTK_OBJECT)
    def onNodeAboutToBeRemoved(self, caller, event, node):
        if node is None:
            return
        if node in (self.inputVolumeSelector.currentNode(), self.inputVolumeSelectorCT.currentNode()):
            # The selector jumps to another volume on removal: that is not a choice of the user, so the
            # views are not updated to it. Released once the removal (and the selector's reaction) is done.
            self._suppressInputUpdate += 1
            qt.QTimer.singleShot(0, self._releaseInputUpdate)
        roiNode, handlesNode = self.roiSet.roiNode, self.roiSet.handlesNode
        if roiNode is not None and node.GetID() == roiNode.GetID():
            self.roiSet.setRoiNode(None)
            self.scheduleRoiUpdate()  # removes spheres and handles outside of this scene callback
        elif handlesNode is not None and node.GetID() == handlesNode.GetID():
            self.roiSet.setHandlesNode(None)
            self.scheduleRoiUpdate()  # handles get recreated
        elif self.filterCropRoiNode is not None and node.GetID() == self.filterCropRoiNode.GetID():
            # The crop box was deleted elsewhere (e.g. Data module): "Limit to ROI" follows
            self.filterCropRoiNode = None
            self._setLimitToRoiChecked(False)

    # ------------------------------------------------------------------
    # Changing the input volumes after "Go"
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def selectorsSetByPanel(self):
        """Selector changes made by the panel itself (scene load, filters) do not trigger a view update."""
        self._suppressInputUpdate += 1
        try:
            yield
        finally:
            self._suppressInputUpdate -= 1

    def _releaseInputUpdate(self):
        self._suppressInputUpdate = max(0, self._suppressInputUpdate - 1)

    def onInputVolumeSelectionChanged(self, node=None):
        self.windowInfoOverlay.scheduleUpdate()  # the CT / PET labels follow the selectors
        if not sceneIsBusy():
            self.updateSelectorLabels()   # (after a scene load: onSceneLoaded)
        if self._suppressInputUpdate or sceneIsBusy():
            return
        self.inputUpdateTimer.start()  # (re)started: a PET and a CT change in a row give one update

    def applyChangedInputVolumes(self):
        """
        Once the views have been built with "Go", show a newly selected SPECT/PET or CT/MRI in them right away.
        Slice positions, pan and zoom, the 3D camera and the fusion opacity stay as they are.
        """
        if sceneIsBusy() or self._filterRunning:
            return
        settingsNode = self.logic.getSettingsNode(create=False)
        if settingsNode is None:
            return  # "Go" has not been pressed in this scene yet
        oldPet = settingsNode.GetNodeReference(SETTINGS_PET_ROLE)
        oldCt = settingsNode.GetNodeReference(SETTINGS_CT_ROLE)
        pet = self.inputVolumeSelector.currentNode()
        ct = self.inputVolumeSelectorCT.currentNode()
        if pet is None or ct is None or (pet is oldPet and ct is oldCt):
            return
        if pet is ct:
            return  # e.g. a newly loaded volume selected in both boxes: wait for the user to pick the other one
        try:
            self.updateFusionVolumes(oldPet, oldCt, pet, ct)
        except Exception:
            logging.exception("EasyFusion: could not update the views to the new volumes")
            return
        slicer.util.showStatusMessage(f"EasyFusion: showing {pet.GetName()} on {ct.GetName()}", 3000)

    def updateFusionVolumes(self, oldPet, oldCt, pet, ct):
        """Like "Go", but without touching slice positions, zoom or the 3D camera."""
        for node in (pet, ct):
            if node.GetDisplayNode() is None:
                node.CreateDefaultDisplayNodes()
        opacities = self.logic.foregroundOpacities(oldPet)  # keep the user's fusion opacity

        keepWindow = True
        if pet is not oldPet:
            if oldPet is not None and oldPet.GetDisplayNode() is not None:
                # Same color map and window as before (switch e.g. original <-> filtered, or another time
                # point); the window only when it suits the new values (both SUV, or a similar scale)
                self.logic.copyScalarDisplaySettings(oldPet, pet)
                keepWindow = sameValueScale(petValueInfo(oldPet), self.logic.getVolumeMaximum(oldPet),
                                            petValueInfo(pet), self.logic.getVolumeMaximum(pet))
                if not keepWindow:
                    self.applyInitialPetWindow(pet)
            else:
                pet.GetDisplayNode().SetInterpolate(True)
                self.applyInitialPetWindow(pet)
                colorNodeName = FUSION_COLOR_MAPS.get(self.petColorMapSelector.currentText)
                if colorNodeName is not None:
                    self.setPETColorMap(colorNodeName)
            if oldPet is None or not self.logic.moveMipToVolume(oldPet, pet, keepRange=keepWindow):
                if oldPet is None:  # no MIP to move: show one, as "Go" does (a MIP the user hid stays hidden)
                    displayNode = pet.GetDisplayNode()
                    self.logic.showOnlyThisVolumeRendering(pet)
                    vrDisplayNode = self.logic.mipDisplayNode(pet, create=True)
                    vrDisplayNode.SetVisibility(True)
                    self.logic.setMIPRange(vrDisplayNode, displayNode.GetLevel() - displayNode.GetWindow() / 2.0,
                                           displayNode.GetLevel() + displayNode.GetWindow() / 2.0, flatOpacity=True)
                    self.logic.showMipOnlyInMipView(vrDisplayNode)

        if ct is not oldCt:
            # Window left to the volume itself (Slicer's auto window for a new one); presets set it afterwards
            ct.GetDisplayNode().SetAndObserveColorNodeID(slicer.util.getNode("Grey").GetID())

        self.logic.rememberVolumes(pet, ct)
        self.logic.applyViewRoles(pet, ct)  # composite nodes only: no fit, no orientation change
        self.logic.restoreForegroundOpacities(opacities)
        self._isolateMipView(pet)
        self.scheduleRoiUpdate()

    def _isolateMipView(self, pet):
        """Only the MIP (and EasyFusion's own nodes) in the MIP view, rendered as MIP."""
        try:
            self.logic.isolateMipView(pet)
        except Exception:
            logging.exception("EasyFusion: could not keep other renderings out of the MIP view")

    def onPETVolumeChanged(self, node=None):
        self.scheduleRoiUpdate()

    def onShowRoisOnMipToggled(self, checked):
        self.mipOverlay.setEnabled(checked)
        self.scheduleRoiUpdate()

    # ------------------------------------------------------------------
    # Fusion
    # ------------------------------------------------------------------

    def DoFusion(self):
        ctNode = self.inputVolumeSelectorCT.currentNode()
        petNode = self.inputVolumeSelector.currentNode()
        if petNode is None or ctNode is None:
            slicer.util.errorDisplay("Please select both a SPECT/PET and a CT/MRI volume.")
            return
        if petNode.GetDisplayNode() is None:
            petNode.CreateDefaultDisplayNodes()
        if ctNode.GetDisplayNode() is None:
            ctNode.CreateDefaultDisplayNodes()

        # Not in one of the EasyFusion layouts (e.g. Slicer's conventional layout): use CT | Fusion | PET + 3D
        layoutManager = slicer.app.layoutManager()
        switchedLayout = layoutManager is not None and layoutManager.layout not in EASYFUSION_LAYOUT_IDS
        if switchedLayout:
            self.logic.ensureLayoutsRegistered()
            layoutManager.setLayout(GO_DEFAULT_LAYOUT_ID)

        petDisplayNode = petNode.GetDisplayNode()
        petDisplayNode.SetInterpolate(True)
        lower, upper = self.applyInitialPetWindow(petNode)

        # Only one MIP at a time: hide volume rendering of any previously used PET (or any other volume)
        self.logic.showOnlyThisVolumeRendering(petNode)
        mipDisplayNode = self.logic.mipDisplayNode(petNode, create=True)
        mipDisplayNode.SetVisibility(True)
        self.logic.setMIPRange(mipDisplayNode, lower, upper, flatOpacity=True)
        # Rendered in the EasyFusion 3D view only, and that same view node is switched to MIP
        viewNode = self.logic.showMipOnlyInMipView(mipDisplayNode)
        self._isolateMipView(petNode)

        threeDWidget = self.getThreeDWidget()
        if viewNode is not None:
            wasModifying = viewNode.StartModify()
            viewNode.SetRaycastTechnique(MIP_RAYCAST_TECHNIQUE)
            viewNode.SetRenderMode(1)         # orthographic
            viewNode.SetBoxVisible(0)
            viewNode.SetAxisLabelsVisible(0)
            # White background set on the view node itself, so Slicer does not
            # repaint the default gradient every time the view node changes.
            viewNode.SetBackgroundColor(1.0, 1.0, 1.0)
            viewNode.SetBackgroundColor2(1.0, 1.0, 1.0)
            viewNode.SetAnimationMs(int(self.rotationSpeedSlider.value))
            viewNode.SetAnimationMode(ANIMATION_OFF)  # no auto-rotation; the user starts it with the button
            viewNode.EndModify(wasModifying)
            self.observeThreeDViewNode()

        if threeDWidget is not None:
            threeDView = threeDWidget.threeDView()
            threeDView.resetFocalPoint()
            threeDView.rotateToViewAxis(3)
            self.fitMIPToView(petNode)
            # The first time, Slicer applies the orthographic switch and the new layout size only after this
            # function returns, which replaces the zoom above; fit again once everything has settled.
            qt.QTimer.singleShot(MIP_FIT_DELAY_MS, lambda: self.fitMIPToView(petNode))

        ctNode.GetDisplayNode().SetAndObserveColorNodeID(slicer.util.getNode("Grey").GetID())
        colorNodeName = FUSION_COLOR_MAPS.get(self.petColorMapSelector.currentText)
        if colorNodeName is not None:
            self.setPETColorMap(colorNodeName)

        # Fill every EasyFusion view: fusion (CT + PET), CT only, PET only (inverted grey)
        self.logic.rememberVolumes(petNode, ctNode)
        # After switching layouts, set each view's orientation too (as the layout buttons do)
        self.logic.applyViewRoles(petNode, ctNode, forceOrientation=switchedLayout)
        # Go fits every view to the volumes (first load, or new volumes)
        self.afterViewRolesApplied(list(SLICE_VIEW_ROLES))

        self.scheduleRoiUpdate()

    # ------------------------------------------------------------------
    # Layouts
    # ------------------------------------------------------------------

    def setEasyFusionLayout(self, layoutID):
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return
        self.logic.ensureLayoutsRegistered()
        if layoutManager.layout == layoutID:
            self.scheduleMIPRefit(layoutID)  # same layout clicked again: no layoutChanged signal, refit anyway
        layoutManager.setLayout(layoutID)

        pet = self.inputVolumeSelector.currentNode()
        ct = self.inputVolumeSelectorCT.currentNode()
        changedViews = []
        if pet is not None and ct is not None:
            self.logic.rememberVolumes(pet, ct)
            changedViews = self.logic.applyViewRoles(pet, ct, forceOrientation=True)
        else:
            slicer.util.showStatusMessage("EasyFusion: select the SPECT/PET and CT/MRI volumes to fill the views.", 4000)
        self.afterViewRolesApplied(changedViews)

        if layoutID in DUAL_MONITOR_LAYOUT_IDS:
            qt.QTimer.singleShot(DUAL_MONITOR_PLACE_DELAY_MS, self.logic.placeSecondaryViewportWindow)
        self._isolateMipView(pet)
        self.onLayoutChanged()

    def afterViewRolesApplied(self, changedViews):
        # Views need their final size before fitting / copying zoom, so wait for the layout to settle
        qt.QTimer.singleShot(150, lambda: self.alignSliceViews(changedViews))

    def onLayoutChanged(self, layoutID=None):
        if not hasattr(self, "layoutButtons"):
            return
        if sceneIsBusy():
            # Layout switches during scene loading: views are still being rebuilt, look at them later
            deferUntilSceneIdle(self.onLayoutChanged)
            return
        layoutManager = slicer.app.layoutManager()
        current = layoutManager.layout if layoutManager is not None else None
        self.layoutButtonGroup.setExclusive(False)
        for buttonLayoutID, button in self.layoutButtons.items():
            button.checked = (buttonLayoutID == current)
        self.layoutButtonGroup.setExclusive(True)
        if self.toolbar is not None:
            self.toolbar.setCurrentLayout(current)
        self.scheduleViewSyncUpdate()   # the layout's views may be new or gone
        # A layout switch can create new 3D views (or move them to the Monitor 2 window): re-attach the MIP overlay
        self.scheduleRoiUpdate()
        # ... and new slice views: give them their window info
        self.windowInfoOverlay.scheduleUpdate()
        if layoutID is not None:  # a real layout switch (the signal), not a refresh on module enter / scene load
            self.scheduleMIPRefit(current)

    def scheduleMIPRefit(self, layoutID=None):
        """Fit the MIP again once the new layout has its final size (Monitor 2 window placed after 300 ms)."""
        qt.QTimer.singleShot(MIP_FIT_DELAY_MS, self.refitMIP)
        if layoutID in DUAL_MONITOR_LAYOUT_IDS:
            qt.QTimer.singleShot(MIP_FIT_DELAY_MS + 400, self.refitMIP)
        self.scheduleSliceFit(layoutID)

    def scheduleSliceFit(self, layoutID=None):
        """After a layout switch: fit the slice views to the volumes once they have their final size, then check
        that no view is left stretched (see repairSliceAspects)."""
        qt.QTimer.singleShot(SLICE_FIT_DELAY_MS, self.fitSliceViews)
        if layoutID in DUAL_MONITOR_LAYOUT_IDS:
            qt.QTimer.singleShot(SLICE_FIT_DUAL_MONITOR_DELAY_MS, self.fitSliceViews)
        qt.QTimer.singleShot(SLICE_ASPECT_CHECK_DELAY_MS, self.repairSliceAspects)

    def fitSliceViews(self):
        """Fit the views of the current layout: each orientation's fusion view to the volumes, the other views of
        that orientation to the same position and zoom."""
        if sceneIsBusy():
            return
        self.repairSliceAspects()
        self.alignSliceViews(list(SLICE_VIEW_ROLES))

    def repairSliceAspects(self):
        """A view whose slice node no longer matches the view's real shape draws the image stretched (Slicer can
        leave that after a layout switch); tell the node its real size and fix the aspect, keeping the zoom."""
        if sceneIsBusy():
            return
        for name, sliceWidget in self.logic.roleSliceWidgets(visibleOnly=True):
            if syncSliceViewSize(sliceWidget):
                logging.info(f"EasyFusion: view {name} was stretched; its size / aspect ratio was corrected")

    def refitMIP(self):
        """Re-fit only when the MIP of the selected PET is shown; otherwise leave the 3D camera alone."""
        if sceneIsBusy():
            return
        pet = self.inputVolumeSelector.currentNode()
        if pet is None or not slicer.mrmlScene.IsNodePresent(pet):
            return
        displayNode = self.logic.mipDisplayNode(pet)
        if displayNode is None or not displayNode.GetVisibility():
            return
        self.fitMIPToView(pet)

    def restoreFromSettings(self):
        settingsNode = self.logic.getSettingsNode(create=False)
        if settingsNode is None:
            return
        pet = settingsNode.GetNodeReference(SETTINGS_PET_ROLE)
        ct = settingsNode.GetNodeReference(SETTINGS_CT_ROLE)
        if pet is not None:
            self.inputVolumeSelector.setCurrentNode(pet)
        if ct is not None:
            self.inputVolumeSelectorCT.setCurrentNode(ct)

        def readFloat(name, default):
            try:
                return float(settingsNode.GetAttribute(name))
            except (TypeError, ValueError):
                return default
        self._relativeThreshold = readFloat(SETTINGS_RELATIVE_THRESHOLD, DEFAULT_RELATIVE_THRESHOLD)
        self._absoluteThreshold = readFloat(SETTINGS_ABSOLUTE_THRESHOLD, DEFAULT_ABSOLUTE_THRESHOLD)
        mode = settingsNode.GetAttribute(SETTINGS_THRESHOLD_MODE)
        mode = mode if mode in (THRESHOLD_RELATIVE, THRESHOLD_ABSOLUTE) else THRESHOLD_RELATIVE
        wasBlocked = self.thresholdModeComboBox.blockSignals(True)
        self.thresholdModeComboBox.setCurrentIndex(self.thresholdModeComboBox.findData(mode))
        self.thresholdModeComboBox.blockSignals(wasBlocked)
        self._applyThresholdModeToSpinBox(mode)

    # ------------------------------------------------------------------
    # Segment threshold
    # ------------------------------------------------------------------

    def currentThreshold(self):
        mode = self.thresholdModeComboBox.itemData(self.thresholdModeComboBox.currentIndex)
        mode = mode if mode in (THRESHOLD_RELATIVE, THRESHOLD_ABSOLUTE) else THRESHOLD_RELATIVE
        value = self._absoluteThreshold if mode == THRESHOLD_ABSOLUTE else self._relativeThreshold
        return mode, value

    def _applyThresholdModeToSpinBox(self, mode):
        spinBox = self.thresholdValueSpinBox
        wasBlocked = spinBox.blockSignals(True)
        if mode == THRESHOLD_ABSOLUTE:
            spinBox.setRange(0.0, 100.0)
            spinBox.setSingleStep(0.1)
            spinBox.setSuffix(f" {self._petValueUnit}" if self._petValueUnit else "")
            spinBox.setValue(self._absoluteThreshold)
        else:
            spinBox.setRange(1.0, 100.0)
            spinBox.setSingleStep(1.0)
            spinBox.setSuffix(" %")
            spinBox.setValue(self._relativeThreshold)
        spinBox.blockSignals(wasBlocked)

    def onThresholdModeChanged(self, index=None):
        mode, _ = self.currentThreshold()
        self._applyThresholdModeToSpinBox(mode)
        self.applyThresholdControls()

    def onThresholdValueChanged(self, value):
        mode, _ = self.currentThreshold()
        if mode == THRESHOLD_ABSOLUTE:
            self._absoluteThreshold = float(value)
        else:
            self._relativeThreshold = float(value)
        self.applyThresholdControls()

    def applyThresholdControls(self):
        """
        A threshold change goes to the ROI selected in the table only. What the controls show is also saved as
        the threshold for new ROIs. With no ROI selected, existing ROIs keep their own thresholds.
        """
        self.saveThresholdSettings()
        node = self.roiSet.roiNode
        pointID = self.selectedRoiPointID()
        if node is not None and pointID is not None:
            SphereRoiSet.setThreshold(node, pointID, *self.currentThreshold())
            self.scheduleRoiUpdate()

    def setThresholdControls(self, mode, value):
        """Show a threshold in the controls without applying it to anything (e.g. the selected ROI's own)."""
        mode = mode if mode in (THRESHOLD_RELATIVE, THRESHOLD_ABSOLUTE) else THRESHOLD_RELATIVE
        if mode == THRESHOLD_ABSOLUTE:
            self._absoluteThreshold = float(value)
        else:
            self._relativeThreshold = float(value)
        wasBlocked = self.thresholdModeComboBox.blockSignals(True)
        self.thresholdModeComboBox.setCurrentIndex(self.thresholdModeComboBox.findData(mode))
        self.thresholdModeComboBox.blockSignals(wasBlocked)
        self._applyThresholdModeToSpinBox(mode)
        self.saveThresholdSettings()  # the controls' values are what new ROIs get

    def syncThresholdControls(self, rows, selectedID):
        """After a programmatic selection (new or dragged ROI), show that ROI's threshold in the controls."""
        if selectedID is None:
            return
        for pointID, _, _, _, (mode, value), _ in rows:
            if pointID == selectedID:
                currentMode, currentValue = self.currentThreshold()
                if mode != currentMode or abs(value - currentValue) > 1e-6:
                    self.setThresholdControls(mode, value)
                return

    def saveThresholdSettings(self):
        settingsNode = self.logic.getSettingsNode()
        mode, _ = self.currentThreshold()
        settingsNode.SetAttribute(SETTINGS_THRESHOLD_MODE, mode)
        settingsNode.SetAttribute(SETTINGS_RELATIVE_THRESHOLD, f"{self._relativeThreshold:g}")
        settingsNode.SetAttribute(SETTINGS_ABSOLUTE_THRESHOLD, f"{self._absoluteThreshold:g}")

    # ------------------------------------------------------------------
    # Slice view alignment and sync: views of the same orientation keep the same position, pan and zoom
    # ------------------------------------------------------------------

    def updateViewSync(self):
        """
        Group the EasyFusion views of the current layout by orientation (fusion view first) and keep each group
        at the same slice position, pan and zoom: scrolling or zooming the axial fusion moves the axial CT and PET
        too. Off: 'Keep same-orientation views together' in the Layouts section.
        """
        if not getattr(self, "syncViewsCheckBox", None) or not self.syncViewsCheckBox.checked or sceneIsBusy():
            _viewSync.clear()
            return
        groups = {}
        for name, sliceWidget in self.logic.roleSliceWidgets(visibleOnly=True):
            sliceNode = sliceWidget.mrmlSliceNode()
            rank = 0 if SLICE_VIEW_ROLES[name][1] == "fusion" else 1
            groups.setdefault(self.logic.getSliceOrientation(sliceNode), []).append((rank, name, sliceNode))
        _viewSync.setGroups([[node for _, _, node in sorted(members, key=lambda m: (m[0], m[1]))]
                             for members in groups.values()])

    def scheduleViewSyncUpdate(self):
        qt.QTimer.singleShot(VIEW_SYNC_DELAY_MS, self.updateViewSync)

    def onSyncViewsToggled(self, checked):
        qt.QSettings().setValue(SETTINGS_SYNC_VIEWS, "true" if checked else "false")
        self.updateViewSync()
        if checked:
            _viewSync.alignToReferences()

    def updateSelectorLabels(self):
        """Volume selector labels: PET / SPECT and CT / MRI as detected ("SPECT/PET", "CT/MRI" when not sure)."""
        try:
            petText = functionalLabel(self.inputVolumeSelector.currentNode())
            ctText = anatomicalLabel(self.inputVolumeSelectorCT.currentNode())
        except Exception:
            logging.debug("EasyFusion: could not tell the image types", exc_info=True)
            return
        self.petSelectorLabel.text = f"{petText}:"
        self.ctSelectorLabel.text = f"{ctText}:"
        labels = getattr(self, "toolbarSelectorLabels", {})
        if "pet" in labels:
            labels["pet"].text = petText
            labels["ct"].text = ctText

    def alignSliceViews(self, changedViews):
        """Fit views whose CT changed, then give same-orientation views the same position and zoom."""
        if sceneIsBusy():
            return
        groups = {}
        for name, sliceWidget in self.logic.roleSliceWidgets(visibleOnly=True):
            groups.setdefault(self.logic.getSliceOrientation(sliceWidget.mrmlSliceNode()), []).append((name, sliceWidget))
        for members in groups.values():
            referenceWidget = next((w for name, w in members if SLICE_VIEW_ROLES[name][1] == "fusion"), members[0][1])
            if any(name in changedViews for name, _ in members):
                referenceWidget.sliceLogic().FitSliceToAll()
            for name, sliceWidget in members:
                if sliceWidget is not referenceWidget:
                    self.logic.copySliceGeometry(referenceWidget.mrmlSliceNode(), sliceWidget.mrmlSliceNode())
        self.updateViewSync()

    # ------------------------------------------------------------------
    # MIP (3D view)
    # ------------------------------------------------------------------

    def fitMIPToView(self, volumeNode):
        """Center the MIP on the volume and zoom so the 3D view shows MIP_FIT_HEIGHT_MM from top to bottom."""
        threeDWidget = self.getThreeDWidget()
        if threeDWidget is None or volumeNode is None or not slicer.mrmlScene.IsNodePresent(volumeNode):
            return
        threeDView = threeDWidget.threeDView()
        threeDView.forceRender()  # applies pending view node changes (orthographic mode) before the fit
        bounds = [0.0] * 6
        volumeNode.GetRASBounds(bounds)
        if bounds[1] < bounds[0]:
            return  # empty volume
        renderer = threeDView.renderWindow().GetRenderers().GetFirstRenderer()
        camera = renderer.GetActiveCamera()
        # Move the camera sideways to the volume center, keeping the viewing direction and distance
        center = [(bounds[0] + bounds[1]) / 2.0, (bounds[2] + bounds[3]) / 2.0, (bounds[4] + bounds[5]) / 2.0]
        focalPoint, position = camera.GetFocalPoint(), camera.GetPosition()
        camera.SetFocalPoint(*center)
        camera.SetPosition(*[p + c - f for p, c, f in zip(position, center, focalPoint)])
        camera.SetParallelScale(MIP_FIT_HEIGHT_MM / 2.0)  # parallel scale = half of the visible height
        renderer.ResetCameraClippingRange()
        threeDView.forceRender()

    @staticmethod
    def getThreeDWidget():
        return Easy_fusionLogic.mipThreeDWidget()

    def observeThreeDViewNode(self, updateButton=True):
        """Keep observing the view node of the first 3D view (it can change with layout or scene)."""
        threeDWidget = self.getThreeDWidget()
        viewNode = threeDWidget.mrmlViewNode() if threeDWidget is not None else None
        if viewNode is not self.observedViewNode:
            if self.observedViewNode is not None:
                self.removeObserver(self.observedViewNode, vtk.vtkCommand.ModifiedEvent, self.updateRotationButton)
            self.observedViewNode = viewNode
            if viewNode is not None:
                self.addObserver(viewNode, vtk.vtkCommand.ModifiedEvent, self.updateRotationButton)
        if updateButton:
            self.updateRotationButton()
        return viewNode

    def updateRotationButton(self, caller=None, event=None):
        viewNode = self.observedViewNode
        spinning = viewNode is not None and viewNode.GetAnimationMode() == ANIMATION_SPIN
        wasBlocked = self.toggleRotationButton.blockSignals(True)
        self.toggleRotationButton.checked = spinning
        self.toggleRotationButton.blockSignals(wasBlocked)
        self.toggleRotationButton.text = "Stop MIP Rotation" if spinning else "Start MIP Rotation"
        if self.toolbar is not None:
            self.toolbar.setRotating(spinning)

    def setRotationEnabled(self, enabled):
        viewNode = self.observeThreeDViewNode(updateButton=False)
        if viewNode is None:
            self.updateRotationButton()
            return
        wasModifying = viewNode.StartModify()
        if enabled:
            viewNode.SetAnimationMs(int(self.rotationSpeedSlider.value))
            viewNode.SetAnimationMode(ANIMATION_SPIN)
        else:
            viewNode.SetAnimationMode(ANIMATION_OFF)
        viewNode.EndModify(wasModifying)
        self.updateRotationButton()

    def updateRotationSpeed(self, value):
        viewNode = self.observeThreeDViewNode(updateButton=False)
        if viewNode is not None:
            viewNode.SetAnimationMs(int(value))

    def rotateMIPToViewAxis(self, axis):
        """Quick view buttons: 3 = anterior, 0 = left, 1 = right. Stops the rotation first."""
        self.setRotationEnabled(False)
        threeDWidget = self.getThreeDWidget()
        if threeDWidget is not None:
            threeDWidget.threeDView().rotateToViewAxis(axis)

    # ------------------------------------------------------------------
    # Window / level and color maps
    # ------------------------------------------------------------------

    def setCTWindow(self, window, level):
        ctNode = self.inputVolumeSelectorCT.currentNode()
        if ctNode and ctNode.GetDisplayNode():
            displayNode = ctNode.GetDisplayNode()
            displayNode.SetAutoWindowLevel(False)
            displayNode.SetWindow(window)
            displayNode.SetLevel(level)

    def setPETWindow(self, window, level):
        """Window / level of the PET in the slice views (PET-only views follow) and the MIP grey range."""
        petNode = self.inputVolumeSelector.currentNode()
        if not petNode:
            return
        if petNode.GetDisplayNode():
            displayNode = petNode.GetDisplayNode()
            displayNode.SetAutoWindowLevel(False)
            displayNode.SetWindow(window)
            displayNode.SetLevel(level)
        vrDisplayNode = self.logic.mipDisplayNode(petNode)
        self.logic.setMIPRange(vrDisplayNode, level - window / 2.0, level + window / 2.0)

    def onWindowShortcut(self, key):
        """F5-F9: apply the preset for this key that belongs to the view under the mouse (see windowPresetForView)."""
        view = self.logic.viewUnderCursor()
        preset = windowPresetForView(view[0], view[1], key) if view is not None else None
        if preset is None:
            return
        kind, text, window, level = preset
        if kind == "ct" and not self.ctMriLooksLikeCT():
            mriPreset = next((p for p in MRI_PERCENTILE_PRESETS if p[3] == key), None)
            if mriPreset is not None:
                self.setMRIWindowPercentile(mriPreset[1], mriPreset[2], mriPreset[0])
            return
        if kind == "ct":
            self.setCTWindow(window, level)
            slicer.util.showStatusMessage(f"EasyFusion: {text}", 2000)
        elif petValueInfo(self.inputVolumeSelector.currentNode()).kind == VALUE_KIND_SUV:
            self.setPETWindow(window, level)
            slicer.util.showStatusMessage(f"EasyFusion: SUV {text}", 2000)
        else:
            # Not known to be SUV: the same keys give 0–10 / 25 / 50 / 75 / 100 % of the maximum instead
            percent = SPECT_PERCENT_OF_MAX_PRESETS[WINDOW_SHORTCUT_KEYS.index(key)]
            self.setPETWindowPercentOfMax(percent)
            slicer.util.showStatusMessage(f"EasyFusion: 0–{percent}% of max", 2000)

    def applyInitialPetWindow(self, petNode):
        """
        First window of a SPECT/PET: SUV 0–10 when the volume is known to be in SUV, otherwise 0–100 % of its
        maximum (SPECT counts, Bq/mL, or values whose unit is not known). Returns (lower, upper).
        """
        info = petValueInfo(petNode)
        lower, upper = initialPetRange(info, self.logic.getVolumeMaximum(petNode))
        displayNode = petNode.GetDisplayNode()
        if displayNode is not None:
            wasModifying = displayNode.StartModify()
            displayNode.SetAutoWindowLevel(False)
            displayNode.SetWindow(upper - lower)
            displayNode.SetLevel((upper + lower) / 2.0)
            displayNode.EndModify(wasModifying)
        if info.kind == VALUE_KIND_SUV:
            message = f"{petNode.GetName()}: SUV (source: {info.reason}), window SUV 0–{upper:g}"
        else:
            message = f"{petNode.GetName()}: not known to be SUV, window 0–100 % of the maximum"
        slicer.util.showStatusMessage(f"EasyFusion: {message}", 5000)
        logging.info(f"EasyFusion: {message} ({info.reason})")
        return lower, upper

    def setPETWindowPercentOfMax(self, percent):
        """SPECT presets: window from 0 to a percentage of the highest voxel value (count) in the volume."""
        petNode = self.inputVolumeSelector.currentNode()
        if petNode is None or petNode.GetImageData() is None:
            slicer.util.showStatusMessage("EasyFusion: select a SPECT/PET volume first.", 3000)
            return
        upper = percentOfMaximum(self.logic.getVolumeMaximum(petNode), percent)
        if upper is None:
            slicer.util.showStatusMessage("EasyFusion: the SPECT/PET volume has no positive counts.", 3000)
            return
        self.setPETWindow(upper, upper / 2.0)

    def mriTissueSample(self, volumeNode):
        """Sampled tissue intensities of the CT/MRI volume (cached until the volume or its voxels change)."""
        imageData = volumeNode.GetImageData() if volumeNode is not None else None
        if imageData is None or imageData.GetNumberOfPoints() == 0:
            return None
        key = (volumeNode.GetID(), imageData.GetMTime())
        if self._mriSample[0] == key:
            return self._mriSample[1]
        voxels = slicer.util.arrayFromVolume(volumeNode)
        if voxels.ndim > 3:
            voxels = voxels[..., 0]
        step = max(1, int(math.ceil((voxels.size / float(MRI_PRESET_SAMPLE_SIZE)) ** (1.0 / 3.0))))
        sample = tissueSample(voxels[::step, ::step, ::step])
        self._mriSample = (key, sample)
        return sample

    def setMRIWindowPercentile(self, lowerPercentile, upperPercentile, text=""):
        """MRI presets: window between two percentiles of the tissue intensities of the CT/MRI volume."""
        volumeNode = self.inputVolumeSelectorCT.currentNode()
        if volumeNode is None or volumeNode.GetImageData() is None:
            slicer.util.showStatusMessage("EasyFusion: select a CT/MRI volume first.", 3000)
            return
        sample = self.mriTissueSample(volumeNode)
        windowLevel = percentileWindow(sample, lowerPercentile, upperPercentile) if sample is not None else None
        if windowLevel is None:
            slicer.util.showStatusMessage("EasyFusion: the CT/MRI volume has no intensity range.", 3000)
            return
        self.setCTWindow(*windowLevel)
        label = f"{text} " if text else ""
        slicer.util.showStatusMessage(
            f"EasyFusion: MRI {label}(percentile {lowerPercentile:g}–{upperPercentile:g})", 2000)

    def ctMriLooksLikeCT(self):
        volumeNode = self.inputVolumeSelectorCT.currentNode()
        imageData = volumeNode.GetImageData() if volumeNode is not None else None
        if imageData is None or imageData.GetNumberOfPoints() == 0:
            return True  # nothing to decide on: keep the CT presets
        return looksLikeCT(imageData.GetScalarRange()[0])

    def setPETColorMap(self, colorNodeName):
        petNode = self.inputVolumeSelector.currentNode()
        if not petNode:
            return
        if colorNodeName == HOT_IRON_NAME:
            colorNode = self.logic.getOrCreateHotIronColorNode()
        else:
            # Color nodes only: getNode("Red") could also find the Red slice view
            colorNode = findColorNode(colorNodeName)
            if colorNode is None:
                slicer.util.errorDisplay(f"Color node '{colorNodeName}' not found.")
                return
        displayNode = petNode.GetDisplayNode()
        if displayNode:
            displayNode.SetAndObserveColorNodeID(colorNode.GetID())

    # ------------------------------------------------------------------
    # SUV ROI measurement
    # ------------------------------------------------------------------

    def connectToExistingRois(self):
        """Pick up the ROIs of a loaded scene, measured on the same PET as before (node references are saved)."""
        pet = self.roiSet.measuredPet()
        if pet is not None:
            self.inputVolumeSelector.setCurrentNode(pet)
        self.roiSet.connect()
        self.scheduleRoiUpdate()

    def onRoiNodeModified(self, caller=None, event=None):
        self.scheduleRoiUpdate()

    def scheduleRoiUpdate(self):
        if hasattr(self, "roiUpdateTimer"):
            self.roiUpdateTimer.start()

    def updateRois(self):
        if sceneIsBusy():
            return  # scene event handlers trigger a refresh when done
        pet = self.inputVolumeSelector.currentNode()
        self.updateMeasuringLabel(pet)
        # Measures every ROI on the PET (cached), keeps handles, spheres, labels and segments in sync.
        # A just-loaded scene may hold ROIs the panel has not connected to yet: the set finds them.
        result = self.roiSet.update(
            pet, self.roiRadiusSpinBox.value,
            (float(self.roiRadiusSpinBox.minimum), float(self.roiRadiusSpinBox.maximum)),
            self.currentThreshold(),  # given to ROIs that have none yet (new / older scenes)
            self.roiLabelFields(), unit=self._petValueUnit, selectedID=self.selectedRoiPointID())
        rows = result["rows"]
        self._roiCenters = dict(self.roiSet.centers)
        if self.roiSet.roiNode is None:
            self.mipOverlay.clear()
        else:
            try:
                self.mipOverlay.update(result["mipEntries"], result["segmentationNode"])
            except Exception:
                logging.exception("EasyFusion: could not update the MIP overlay")
        # A freshly placed ROI, or the one being resized, is selected so the radius box follows it
        selectedID = result["selectedID"]
        self.fillRoiTable(rows, selectedID, result["colors"])
        self.syncRadiusSpinBox(rows, selectedID)
        self.syncThresholdControls(rows, selectedID)
        self.updateRoiEditTarget()
        if result["newRoiCenter"] is not None:
            self.jumpSliceViewsTo(result["newRoiCenter"])

    @staticmethod
    def jumpSliceViewsTo(positionWorld):
        """
        Bring every slice view (all orientations, CT-only / PET-only views, both monitors) to a position.
        Offset jump: each view only changes its slice, it is not re-centered or panned, so the view the
        ROI was placed in stays exactly where it is.
        """
        x, y, z = (float(v) for v in positionWorld)
        try:
            slicer.modules.markups.logic().JumpSlicesToLocation(x, y, z, False)
        except Exception:
            slicer.vtkMRMLSliceNode.JumpAllSlices(slicer.mrmlScene, x, y, z, slicer.vtkMRMLSliceNode.OffsetJumpSlice)

    def syncRadiusSpinBox(self, rows, selectedID):
        if selectedID is None:
            return
        for pointID, _, radius, *_ in rows:
            if pointID == selectedID:
                if abs(self.roiRadiusSpinBox.value - radius) > 1e-6:
                    wasBlocked = self.roiRadiusSpinBox.blockSignals(True)
                    self.roiRadiusSpinBox.setValue(radius)
                    self.roiRadiusSpinBox.blockSignals(wasBlocked)
                return

    def fillRoiTable(self, rows, selectPointID=None, colors=None):
        if not hasattr(self, "roiTable"):
            return
        table = self.roiTable
        self._roiTableRows = list(rows)
        if not rows:
            self._roiCenters = {}
        if hasattr(self, "exportRoiTableButton"):
            self.exportRoiTableButton.enabled = bool(rows)
        wasBlocked = table.blockSignals(True)
        try:
            table.setRowCount(len(rows))
            for rowIndex, (pointID, name, radius, stats, threshold, origin) in enumerate(rows):
                values = formatRoiTableRow(name, radius, stats, threshold, unit=self._petValueUnit)
                for column, text in enumerate(values):
                    item = qt.QTableWidgetItem(text)
                    if column == 0:
                        item.setData(qt.Qt.UserRole, pointID)
                        item.setToolTip("Placed by AI" if origin == ROI_ORIGIN_AI else "Placed by the user")
                        color = (colors or {}).get(pointID)
                        if color is not None:  # color swatch next to the ROI name
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

    def selectedRoiPointID(self):
        if not hasattr(self, "roiTable"):
            return None
        selectedRows = self.roiTable.selectionModel().selectedRows()
        if not selectedRows:
            return None
        item = self.roiTable.item(selectedRows[0].row(), 0)
        return item.data(qt.Qt.UserRole) if item is not None else None

    def onRoiSelectionChanged(self):
        self.updateRoiEditTarget()
        node = self.roiSet.roiNode
        pointID = self.selectedRoiPointID()
        if node is None or pointID is None:
            return
        radius = SphereRoiSet.getRadius(node, pointID, self.roiRadiusSpinBox.value)
        wasBlocked = self.roiRadiusSpinBox.blockSignals(True)
        self.roiRadiusSpinBox.setValue(radius)
        self.roiRadiusSpinBox.blockSignals(wasBlocked)
        # The ROI's own threshold is loaded into the controls (they now edit this ROI only)
        self.setThresholdControls(*SphereRoiSet.getThreshold(node, pointID, self.currentThreshold()))
        index = node.GetNthControlPointIndexByID(pointID)
        if index >= 0:
            slicer.modules.markups.logic().JumpSlicesToNthPointInMarkup(node.GetID(), index, True)

    def onDeselectRoi(self):
        self.roiTable.clearSelection()  # -> onRoiSelectionChanged: the controls now set values for new ROIs

    def updateRoiEditTarget(self):
        """Tell which ROI the radius / threshold controls edit: the selected one, or new ROIs."""
        if not hasattr(self, "roiEditTargetLabel"):
            return
        selectedRows = self.roiTable.selectionModel().selectedRows()
        item = self.roiTable.item(selectedRows[0].row(), 0) if selectedRows else None
        if item is not None:
            self.roiEditTargetLabel.text = f"Radius and threshold: editing {item.text()} only"
            self.roiDeselectButton.enabled = True
        else:
            self.roiEditTargetLabel.text = "Radius and threshold: values for new ROIs"
            self.roiDeselectButton.enabled = False

    def roiLabelFields(self):
        if not hasattr(self, "roiLabelFieldCheckBoxes"):
            return tuple(DEFAULT_ROI_LABEL_FIELDS)
        return tuple(key for key, _ in ROI_LABEL_FIELDS if self.roiLabelFieldCheckBoxes[key].checked)

    def onRoiLabelFieldsChanged(self, checked=None):
        qt.QSettings().setValue(SETTINGS_ROI_LABEL_FIELDS, ",".join(self.roiLabelFields()))
        self.scheduleRoiUpdate()

    def onRoiRadiusChanged(self, value):
        node = self.roiSet.roiNode
        pointID = self.selectedRoiPointID()
        if node is None or pointID is None:
            return  # no selection: value only applies to new ROIs
        SphereRoiSet.setRadius(node, pointID, value)
        self.scheduleRoiUpdate()

    def prepareRoiNodeForPlacement(self):
        node = self.roiSet.prepareForPlacement(self.inputVolumeSelector.currentNode())
        # New ROI takes the radius box value, not the radius of whatever row was selected
        self.roiTable.clearSelection()
        return node

    def onPlaceRoi(self):
        if self.inputVolumeSelector.currentNode() is None:
            slicer.util.warningDisplay("Select a SPECT/PET volume first.")
            return
        node = self.prepareRoiNodeForPlacement()

        selectionNode = slicer.app.applicationLogic().GetSelectionNode()
        selectionNode.SetReferenceActivePlaceNodeClassName("vtkMRMLMarkupsFiducialNode")
        selectionNode.SetActivePlaceNodeID(node.GetID())
        interactionNode = slicer.app.applicationLogic().GetInteractionNode()
        interactionNode.SetPlaceModePersistence(0)
        interactionNode.SetCurrentInteractionMode(interactionNode.Place)

    def onPlaceRoiAtCursor(self):
        """Insert key: drop an ROI centered at the mouse position in the slice view under the cursor."""
        if self.inputVolumeSelector.currentNode() is None:
            slicer.util.showStatusMessage("EasyFusion: select a SPECT/PET volume first.", 3000)
            return
        crosshairNode = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLCrosshairNode")
        ras = [0.0, 0.0, 0.0]
        xyz = [0.0, 0.0, 0.0]
        insideView = crosshairNode is not None and crosshairNode.GetCursorPositionRAS(ras)
        # GetCursorPositionXYZ returns the slice node only when the cursor is over a slice view
        sliceNode = crosshairNode.GetCursorPositionXYZ(xyz) if insideView else None
        if not insideView or sliceNode is None:
            slicer.util.showStatusMessage("EasyFusion: hover the mouse over a slice view, then press Insert.", 3000)
            return

        node = self.prepareRoiNodeForPlacement()

        # If "Place ROI" was clicked earlier, leave place mode so a second ROI doesn't follow the mouse
        interactionNode = slicer.app.applicationLogic().GetInteractionNode()
        selectionNode = slicer.app.applicationLogic().GetSelectionNode()
        if (interactionNode.GetCurrentInteractionMode() == interactionNode.Place
                and selectionNode.GetActivePlaceNodeID() == node.GetID()):
            interactionNode.SwitchToViewTransformMode()

        node.AddControlPoint(ras)

    def onDeleteSelectedRoi(self):
        pointID = self.selectedRoiPointID()
        if self.roiSet.roiNode is None or pointID is None:
            return
        self.roiSet.removeRoi(pointID)
        self.scheduleRoiUpdate()

    def onClearRois(self):
        node = self.roiSet.roiNode
        if node is None or node.GetNumberOfControlPoints() == 0:
            return
        if not slicer.util.confirmOkCancelDisplay("Remove all SUV ROIs?"):
            return
        self.roiSet.removeAllRois()
        self.scheduleRoiUpdate()

    def onExportRoiTable(self):
        """Write the ROI table to a .tsv file chosen by the user."""
        # A pending recomputation (e.g. right after a drag) would make the file lag behind the views
        if self.roiUpdateTimer.isActive():
            self.roiUpdateTimer.stop()
            self.updateRois()
        rows = list(self._roiTableRows)
        if not rows:
            slicer.util.infoDisplay("There are no ROIs to export.", windowTitle="Export ROI table")
            return

        pet = self.inputVolumeSelector.currentNode()
        petName = pet.GetName() if pet is not None else ""
        petFilter = ""
        if pet is not None and pet.GetAttribute(FILTER_MODEL_ATTRIBUTE):
            petFilter = pet.GetAttribute(FILTER_MODEL_ATTRIBUTE)
            if pet.GetAttribute(FILTER_CROPPED_ATTRIBUTE):
                petFilter += " (ROI only)"

        settings = qt.QSettings()
        folder = settings.value(SETTINGS_ROI_EXPORT_FOLDER) or ""
        if not os.path.isdir(folder):
            folder = qt.QStandardPaths.writableLocation(qt.QStandardPaths.DocumentsLocation) or ""
        path = qt.QFileDialog.getSaveFileName(
            slicer.util.mainWindow(), "Export ROI table",
            os.path.join(folder, defaultRoiExportFileName(petName, isSuv=self._petValueUnit == "SUV")),
            "Tab-separated values (*.tsv);;All files (*)")
        if isinstance(path, (tuple, list)):  # some Qt bindings return (fileName, selectedFilter)
            path = path[0] if path else ""
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".tsv"

        text = formatRoiTableTsv(rows, self._roiCenters, petName, petFilter, unit=self._petValueUnit)
        try:
            with open(path, "w", encoding="utf-8", newline="") as tsvFile:
                tsvFile.write(text)
        except OSError as error:
            slicer.util.errorDisplay(f"Could not write the ROI table:\n{path}\n\n{error}",
                                     windowTitle="Export ROI table")
            return
        settings.setValue(SETTINGS_ROI_EXPORT_FOLDER, os.path.dirname(path))
        slicer.util.showStatusMessage(f"Exported {len(rows)} ROI(s) to {path}", 5000)
        logging.info(f"EasyFusion: exported {len(rows)} ROI(s) to {path}")

    def updateMeasuringLabel(self, pet):
        """
        Which PET the ROIs measure on and what its values are (SUV, Bq/mL, counts, or not known); a filtered
        volume gets a permanent reminder that its values differ. Units are shown only when they are known.
        """
        info = petValueInfo(pet) if pet is not None else PetValueInfo(None, None, "")
        self.applyValueUnit(valueKindUnit(info.kind))
        unit = self._petValueUnit
        label = self.roiPetLabel
        if pet is None:
            label.text, toolTip, style = "Measuring on: (no PET selected)", "", ""
        else:
            valueLine = (f"Values: {unit} (source: {info.reason})" if unit else
                         f"Values: unit not known, so none is shown ({info.reason})")
            toolTip = ("The unit is taken from the volume's units, its DICOM header, or (for SUV) its values.\n"
                       "The SUV presets and SUV 0–10 start window are used only for volumes known to be in SUV.")
            if pet.GetAttribute(FILTER_MODEL_ATTRIBUTE):
                source = pet.GetNodeReference(FILTER_SOURCE_ROLE)
                region = ", ROI only" if pet.GetAttribute(FILTER_CROPPED_ATTRIBUTE) else ""
                label.text = (f"Measuring on: {pet.GetName()}\n{valueLine}\n"
                              f"⚠ AI-filtered ({pet.GetAttribute(FILTER_MODEL_ATTRIBUTE)}{region}): "
                              f"{'SUVs' if unit == 'SUV' else 'values'} differ from the original")
                if source is not None:
                    toolTip = f"Original volume: {source.GetName()}\n" + toolTip
                style = "color: #d9822b;"
            else:
                label.text, style = f"Measuring on: {pet.GetName()}\n{valueLine}", ""
        label.setToolTip(toolTip)
        label.setStyleSheet(style)

    def applyValueUnit(self, unit):
        """Show the unit of the SPECT/PET values in the measurement panel ("" = not known: no unit shown)."""
        if unit == self._petValueUnit:
            return
        self._petValueUnit = unit
        self.measurementCollapsibleButton.text = (
            "SUV Measurements (spherical ROI)" if unit == "SUV" else "Measurements (spherical ROI)")
        index = self.thresholdModeComboBox.findData(THRESHOLD_ABSOLUTE)
        if index >= 0:
            self.thresholdModeComboBox.setItemText(index, f"Absolute {unit}" if unit else "Absolute value")
        self._applyThresholdModeToSpinBox(self.currentThreshold()[0])

    # ------------------------------------------------------------------
    # AI post-processing filters
    # ------------------------------------------------------------------

    def restoreFilterModelFolder(self):
        """This module's last own-model folder; otherwise the folder last used in the PETDenoise module."""
        folder = qt.QSettings().value(SETTINGS_FILTER_MODEL_FOLDER)
        # Never chosen, or no longer there (an empty value means the user cleared it on purpose)
        if folder is None or (folder and not os.path.isdir(folder)):
            folder = self.filterLogic.petDenoiseModelFolder() or ""
        self.filterModelFolderEdit.text = folder if folder and os.path.isdir(folder) else ""
        self.refreshFilterModels()

    def onBrowseFilterModelFolder(self):
        folder = qt.QFileDialog.getExistingDirectory(
            slicer.util.mainWindow(), "Select the folder with your own models", self.filterModelFolderEdit.text or "")
        if not folder:
            return
        self.filterModelFolderEdit.text = folder
        qt.QSettings().setValue(SETTINGS_FILTER_MODEL_FOLDER, folder)
        self.refreshFilterModels()

    def onClearFilterModelFolder(self):
        self.filterModelFolderEdit.text = ""
        qt.QSettings().setValue(SETTINGS_FILTER_MODEL_FOLDER, "")
        self.refreshFilterModels()

    def onFilterSectionCollapsed(self, collapsed):
        """Opening the section: check GitHub for new published models (at most once a day), fetch model info."""
        if collapsed or self._filterRunning:
            return
        self.updatePublishedModelCatalog(force=False)
        if self._filterSidecarMissing:
            self.onFilterModelChanged()  # the .txt of the selected published model can be downloaded now

    def updatePublishedModelCatalog(self, force=False):
        """Read the release lists from GitHub (force: now; else if the last check is over a day old)."""
        if self._filterRunning:
            return
        failure = None
        if force or time.time() - self._modelCatalogChecked > FILTER_MODEL_CATALOG_MAX_AGE_S:
            qt.QApplication.setOverrideCursor(qt.Qt.WaitCursor)
            try:
                releases = self.filterLogic.fetchModelCatalog()
                self._modelCatalogChecked = time.time()
                self._publishedModels = mergePublishedModelCatalog(releases)
                self._sidecarFetchFailed.clear()
            except Exception as error:
                logging.warning(f"EasyFusion: could not read the list of published models from GitHub: {error}")
                failure = error
                self._modelCatalogChecked = time.time()  # offline: do not try again on every opening
            finally:
                qt.QApplication.restoreOverrideCursor()
        self.refreshFilterModels(forceReload=force)
        if failure is not None and force:
            slicer.util.warningDisplay("Could not reach GitHub to check for new models:\n"
                                       f"{failure}\n\nThe models already known are still listed and can be used.")

    def currentFilterModel(self):
        """Selector entry (see listFilterModels) of the selected model, or None."""
        key = self.filterModelSelector.itemData(self.filterModelSelector.currentIndex)
        return self._filterModelEntries.get(key) if key else None

    def refreshFilterModels(self, forceReload=False):
        previousKey = self.filterModelSelector.itemData(self.filterModelSelector.currentIndex)
        entries = listFilterModels(self._publishedModels, self.filterLogic.publishedModelFolder(),
                                   self.filterModelFolderEdit.text)
        keys = [(entry["key"], entry["label"]) for entry in entries]
        self._filterModelEntries = {entry["key"]: entry for entry in entries}
        if keys == self._filterModelKeys and self._filterParams is not None and not forceReload:
            return  # nothing new: keep the selection and a manually chosen "Apply to"
        self._filterModelKeys = keys
        wasBlocked = self.filterModelSelector.blockSignals(True)
        self.filterModelSelector.clear()
        previousGroup = None
        for entry in entries:
            if previousGroup is not None and entry["published"] != previousGroup:
                self.filterModelSelector.insertSeparator(self.filterModelSelector.count)
            previousGroup = entry["published"]
            self.filterModelSelector.addItem(entry["label"], entry["key"])
            if entry["published"]:
                state = "downloaded" if entry["available"] else "not downloaded yet"
                tip = f"{entry['kind']} model published on GitHub ({entry['tag']}), {state}."
            else:
                tip = f"Your own model: {entry['path']}"
            self.filterModelSelector.setItemData(self.filterModelSelector.count - 1, tip, qt.Qt.ToolTipRole)
        index = self.filterModelSelector.findData(previousKey) if previousKey else -1
        self.filterModelSelector.setCurrentIndex(index if index >= 0 else (0 if entries else -1))
        self.filterModelSelector.blockSignals(wasBlocked)
        self.onFilterModelChanged()

    def _readFilterSidecar(self, entry):
        """Text of the model's .txt (next to the model, else among the downloads; published: downloaded if needed)."""
        candidates = [os.path.join(os.path.dirname(entry["path"]), sidecarName(entry["name"]))]
        if entry["published"]:
            candidates.append(os.path.join(self.filterLogic.publishedModelFolder(), sidecarName(entry["name"])))
        path = next((candidate for candidate in candidates if os.path.isfile(candidate)), None)
        # Model info of a published model not downloaded yet: its small .txt, only once the section is open
        if (path is None and entry["published"] and entry.get("sidecarUrl") and entry["name"] not in
                self._sidecarFetchFailed and not self.filterCollapsibleButton.collapsed):
            qt.QApplication.setOverrideCursor(qt.Qt.WaitCursor)
            try:
                path = self.filterLogic.downloadSidecar(entry)
            except Exception as error:
                logging.warning(f"EasyFusion: could not download the description of {entry['name']}: {error}")
                self._sidecarFetchFailed.add(entry["name"])
            finally:
                qt.QApplication.restoreOverrideCursor()
        if path is None:
            return None
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as sidecar:
                return sidecar.read()
        except OSError:
            logging.exception(f"EasyFusion: could not read {path}")
            return None

    def onFilterModelChanged(self, index=None):
        entry = self.currentFilterModel()
        self.applyFilterButton.enabled = entry is not None and not self._filterRunning
        self._filterSidecarMissing = False
        if entry is None:
            self._filterParams, self._filterNotes = None, {}
            self.filterInfoBox.setPlainText("No models found. Press ↻ to look for published models on GitHub, "
                                            "or choose a folder with your own models.")
            return
        text = self._readFilterSidecar(entry)
        self._filterParams, self._filterNotes = parseFilterMetadata(text)
        lines = []
        if entry["published"] and not entry["available"]:
            size = f" ({formatByteSize(entry['size'])})" if entry.get("size") else ""
            lines.append(f"Not downloaded yet: Apply Filter downloads it{size} from GitHub once, then it is kept "
                         "for later use.")
        if text is None:
            if entry["published"]:
                self._filterSidecarMissing = True
                lines.append("Model information is shown once its description file (.txt) has been downloaded.")
            else:
                lines.append("No description file (.txt) found for this model. The default parameters of the "
                             "PETDenoise module are used, which may not match the model.")
        else:
            lines.append(text.strip())
        self.filterInfoBox.setPlainText("\n\n".join(lines))
        target = guessFilterTarget(entry["name"], self._filterParams)
        self.filterTargetSelector.setCurrentIndex(self.filterTargetSelector.findData(target))

    def downloadFilterModel(self, entry):
        """Download a published model (and its .txt) behind a cancellable progress dialog. True when done."""
        self._filterRunning = True
        self.applyFilterButton.enabled = False
        progress = FilterProgressDialog(f"EasyFusion - downloading {entry['name']}")
        failure = None
        try:
            try:
                self.filterLogic.downloadSidecar(entry)
            except Exception as error:
                logging.warning(f"EasyFusion: could not download the description of {entry['name']}: {error}")
            self.filterLogic.downloadFile(entry["url"], entry["path"], expectedSize=entry.get("size"),
                                          sha256=entry.get("sha256"), report=progress,
                                          text=f"Downloading {entry['name']} from GitHub…")
            self.filterStatusLabel.text = f"Downloaded {entry['name']}."
            return True
        except FilterCancelled:
            self.filterStatusLabel.text = "Download cancelled."
        except Exception as error:
            logging.exception(f"EasyFusion: could not download {entry['name']}")
            failure = (error, traceback.format_exc())
        finally:
            progress.close()
            self._filterRunning = False
            self.applyFilterButton.enabled = True
        if failure is not None:
            releasePage = FILTER_MODEL_RELEASE_PAGE_URL.format(repository=FILTER_MODEL_REPOSITORY, tag=entry["tag"])
            self.filterStatusLabel.text = f"Could not download {entry['name']}."
            slicer.util.errorDisplay(
                f"Could not download {entry['name']}:\n{failure[0]}\n\n"
                f"Check the internet connection and try again. You can also download the .pth and .txt files "
                f"yourself from\n{releasePage}\ninto a folder and choose it as 'Own models'.",
                detailedText=failure[1])
        return False

    def currentFilterTarget(self):
        target = self.filterTargetSelector.itemData(self.filterTargetSelector.currentIndex)
        return target if target in (FILTER_TARGET_PET, FILTER_TARGET_CT) else FILTER_TARGET_PET

    def _setLimitToRoiChecked(self, checked):
        """Change the "Limit to ROI" box without creating / removing the crop ROI."""
        if not hasattr(self, "filterLimitToRoiCheckBox"):
            return
        wasBlocked = self.filterLimitToRoiCheckBox.blockSignals(True)
        self.filterLimitToRoiCheckBox.checked = checked
        self.filterLimitToRoiCheckBox.blockSignals(wasBlocked)

    def filterTargetVolume(self):
        """(volume the filter applies to, its kind for messages), following "Apply to"."""
        if self.currentFilterTarget() == FILTER_TARGET_PET:
            return self.inputVolumeSelector.currentNode(), "SPECT/PET"
        return self.inputVolumeSelectorCT.currentNode(), "CT/MRI"

    def onFilterLimitToRoiToggled(self, checked):
        if not checked:
            self.removeFilterCropRoi()
            if self.filterStatusLabel.text.startswith("Crop ROI placed"):
                self.filterStatusLabel.text = ""
            return
        volumeNode, kind = self.filterTargetVolume()
        if volumeNode is None or volumeNode.GetImageData() is None:
            slicer.util.warningDisplay(f"Select a {kind} volume first: the crop ROI is fitted to it.")
            self._setLimitToRoiChecked(False)
            return
        roiNode = self.filterCropRoiNode
        if roiNode is None or not slicer.mrmlScene.IsNodePresent(roiNode):
            self.removeFilterCropRoi()  # leftovers, e.g. from a module reload
            try:
                roiNode = self.filterLogic.createCropRoi(volumeNode)
            except Exception:
                logging.exception("EasyFusion: could not create the filter crop ROI")
                self.removeFilterCropRoi()
                roiNode = None
        if roiNode is None:
            slicer.util.errorDisplay("Could not create the crop ROI.")
            self._setLimitToRoiChecked(False)
            return
        self.filterCropRoiNode = roiNode
        self.filterStatusLabel.text = (
            f"Crop ROI placed around '{volumeNode.GetName()}'. Drag the handles of the cyan box in the slice or 3D "
            "views to the region you need, then press Apply Filter. Only that region is filtered; the original "
            "volume is not changed.")

    def removeFilterCropRoi(self):
        self.filterCropRoiNode = None  # first, so the node-removal observer has nothing left to react to
        for node in self.filterLogic.findCropRois() if self.filterLogic is not None else []:
            slicer.mrmlScene.RemoveNode(node)

    def resetFilterCropRoi(self):
        """After a successful run: remove the box and untick "Limit to ROI"."""
        self._setLimitToRoiChecked(False)
        self.removeFilterCropRoi()

    def onApplyFilter(self):
        if self._filterRunning:
            return
        entry = self.currentFilterModel()
        if entry is None:
            slicer.util.warningDisplay("Select a model first.")
            return
        if entry["published"] and not entry["available"]:
            chosenTarget = self.currentFilterTarget()
            if not self.downloadFilterModel(entry):
                return
            self.refreshFilterModels(forceReload=True)  # now listed as downloaded, with its model info
            entry = self.currentFilterModel()
            if entry is None or not entry["available"]:
                slicer.util.errorDisplay("The downloaded model could not be found. Press ↻ and try again.")
                return
            self.filterTargetSelector.setCurrentIndex(self.filterTargetSelector.findData(chosenTarget))
        modelName, modelPath = entry["name"], entry["path"]
        if not os.path.isfile(modelPath):
            slicer.util.warningDisplay(f"The model file no longer exists:\n{modelPath}")
            self.refreshFilterModels(forceReload=True)
            return
        params = self._filterParams or dict(FILTER_DEFAULT_PARAMETERS)
        target = self.currentFilterTarget()
        petNode = self.inputVolumeSelector.currentNode()
        ctNode = self.inputVolumeSelectorCT.currentNode()
        if target == FILTER_TARGET_PET:
            sourceNode, otherNode, sourceKind, otherKind = petNode, ctNode, "SPECT/PET", "CT/MRI"
        else:
            sourceNode, otherNode, sourceKind, otherKind = ctNode, petNode, "CT/MRI", "SPECT/PET"
        if sourceNode is None or sourceNode.GetImageData() is None:
            slicer.util.warningDisplay(f"Select a {sourceKind} volume first.")
            return

        secondNode = None
        if params["dual_channel"]:
            if otherNode is None or otherNode.GetImageData() is None:
                slicer.util.warningDisplay(
                    f"{modelName} is a dual-channel model: select the {otherKind} volume too (second input).")
                return
            if otherNode.GetTransformNodeID() != sourceNode.GetTransformNodeID():
                slicer.util.warningDisplay(
                    "Dual-channel models need both volumes under the same transform. "
                    "Harden the registration transform (Data module) first.")
                return
            secondNode = otherNode

        roiNode = None
        if self.filterLimitToRoiCheckBox.checked:
            roiNode = self.filterCropRoiNode
            if roiNode is None or not slicer.mrmlScene.IsNodePresent(roiNode):
                self.resetFilterCropRoi()
                slicer.util.warningDisplay("The crop ROI no longer exists. Tick 'Limit to ROI' again to place a new one.")
                return

        croppedNode = None
        try:
            # With "Limit to ROI" the filter reads Crop Volume's output. It is removed on every path below
            # (dialog cancelled, progress cancelled, failure, success); the original volume stays whole.
            inputNode = sourceNode
            if roiNode is not None:
                try:
                    croppedNode = self.filterLogic.cropToRoi(sourceNode, roiNode)
                except Exception as error:
                    logging.exception("EasyFusion: cropping to the filter ROI failed")
                    slicer.util.errorDisplay(f"Cropping to the ROI failed:\n{error}",
                                             detailedText=traceback.format_exc())
                    return
                if croppedNode is None or croppedNode.GetImageData() is None:
                    slicer.util.errorDisplay("Crop Volume did not produce a cropped volume.")
                    return
                inputNode = croppedNode

            suffix = "_crop" if croppedNode is not None else ""
            outputName = self.filterLogic.uniqueVolumeName(
                f"{sourceNode.GetName()}_{os.path.splitext(modelName)[0]}{suffix}")
            decision = self.confirmFilter(sourceNode, inputNode, secondNode, modelName, params, target, outputName)
            if decision == "crop":
                self.filterLimitToRoiCheckBox.checked = True  # places the adjustable box (onFilterLimitToRoiToggled)
                return
            if decision != "apply" or not self._prepareFilterDependencies():
                return

            result = self._runFilterWithProgress(inputNode, secondNode, modelPath, params, outputName, sourceNode)
            if result is None:
                if roiNode is not None:
                    self.filterStatusLabel.text += " The crop ROI is kept so you can adjust it and try again."
                return
            outputNode, deviceText, seconds = result
        finally:
            if croppedNode is not None and slicer.mrmlScene.IsNodePresent(croppedNode):
                slicer.mrmlScene.RemoveNode(croppedNode)

        if roiNode is not None:
            self.resetFilterCropRoi()  # the box has done its job
        try:
            self.showFilteredVolume(sourceNode, outputNode, otherNode, target)
        except Exception:
            logging.exception("EasyFusion: could not switch the views to the filtered volume")
            slicer.util.warningDisplay(
                f"'{outputNode.GetName()}' was created but could not be shown automatically. "
                f"Select it as {sourceKind} and press Go.")
        followers = "views, MIP and SUV ROIs now use it" if target == FILTER_TARGET_PET else "views now use it"
        region = " (ROI region only)" if roiNode is not None else ""
        message = (f"Created '{outputNode.GetName()}'{region} in {seconds:.0f} s ({deviceText}). The {followers}; "
                   f"'{sourceNode.GetName()}' is unchanged.")
        self.filterStatusLabel.text = message
        slicer.util.showStatusMessage(f"EasyFusion: {message}", 6000)
        logging.info(f"EasyFusion: {message} Model: {modelPath}")

    def _prepareFilterDependencies(self):
        try:
            return self.ensureFilterDependencies()
        except Exception as error:
            logging.exception("EasyFusion: could not prepare the AI filter dependencies")
            slicer.util.errorDisplay(f"Could not install the AI filter dependencies:\n{error}",
                                     detailedText=traceback.format_exc())
            return False

    def _runFilterWithProgress(self, inputNode, secondNode, modelPath, params, outputName, sourceNode):
        """Filter behind a modal, cancellable progress dialog. (outputNode, device, seconds), or None if not done."""
        self._filterRunning = True
        self.applyFilterButton.enabled = False
        self.filterStatusLabel.text = ""
        progress = FilterProgressDialog(f"EasyFusion - {os.path.basename(modelPath)}")
        try:
            return self.filterLogic.run(inputNode, secondNode, modelPath, params, outputName,
                                        forceCPU=self.filterForceCpuCheckBox.checked, report=progress,
                                        originalNode=sourceNode)
        except FilterCancelled:
            self.filterStatusLabel.text = "Cancelled. Nothing was added; the original volume is unchanged."
        except Exception as error:
            logging.exception("EasyFusion: AI filter failed")
            self.filterStatusLabel.text = "Filtering failed. Nothing was added; the original volume is unchanged."
            slicer.util.errorDisplay(f"Filtering with {os.path.basename(modelPath)} failed:\n{error}",
                                     detailedText=traceback.format_exc())
        finally:
            progress.close()
            self._filterRunning = False
            self.applyFilterButton.enabled = True
        return None

    def confirmFilter(self, sourceNode, inputNode, secondNode, modelName, params, target, outputName):
        """
        Warn about what the filter changes (SUVs above all) and about large runs, before anything is computed.
        inputNode: what will be filtered (sourceNode, or its cropped copy with "Limit to ROI").
        Returns "apply", "crop" (the user wants to crop first) or None (cancelled).
        """
        esc = html.escape
        sourceName = sourceNode.GetName()
        cropped = inputNode is not sourceNode
        spacingText = " × ".join(f"{v:g}" for v in sourceNode.GetSpacing())
        targetSpacing = None if params["dont_resample"] else params["voxel_spacing"]
        job = filterJobSize(inputNode.GetImageData().GetDimensions(), inputNode.GetSpacing(), params,
                            channels=2 if secondNode is not None else 1)
        gridText = " × ".join(str(d) for d in job["shape"][::-1])
        offerCrop = job["large"] and not cropped
        items = []

        if job["large"]:
            advice = ("Consider filtering only the region you need: <b>Crop with ROI first</b> places an adjustable "
                      "box." if not cropped else "Consider making the crop ROI smaller.")
            items.append(f'<span style="color:#d9534f;"><b>Large volume:</b> about {job["voxels"] / 1e6:.0f} million '
                         f'voxels on the model\'s grid ({gridText}), roughly {formatByteSize(job["memoryBytes"])} of '
                         f'memory and {job["windows"]} windows to process. This can take a long time (especially on '
                         f'CPU) or run out of memory. {advice}</span>')

        if target == FILTER_TARGET_PET:
            if petValueInfo(sourceNode).kind == VALUE_KIND_SUV:
                headline = "Filtering changes SUV values"
                items.append("SUVmax, SUVmean, MTV and TLG measured on the filtered volume <b>will differ</b> from "
                             "the original. Denoising removes voxel noise, which usually lowers SUVmax, and it can "
                             "change the contrast and apparent size of small lesions.")
            else:
                headline = "Filtering changes SPECT/PET voxel values"
                items.append("Max, Mean, MTV and TLG measured on the filtered volume <b>will differ</b> from the "
                             "original. Denoising removes voxel noise, which usually lowers the maximum, and it can "
                             "change the contrast and apparent size of small lesions.")
            if targetSpacing is not None:
                items.append(f"The image is resampled from {spacingText} mm to "
                             f"{' × '.join(f'{v:g}' for v in targetSpacing)} mm voxels and the result stays on that "
                             "grid. Resampling alone already changes SUVmax.")
            suvNotes = filterSuvNotes(self._filterNotes)
            if suvNotes:
                items.append("Bias reported for this model on its validation data: <b>"
                             + esc("; ".join(suvNotes)) + "</b>. Other scanners, reconstructions and tracers "
                             "can behave differently (see Model info).")
            if params["prevent_negative"]:
                items.append("Negative voxel values are set to 0.")
            if cropped:
                items.append("Only the region inside the crop ROI is filtered. The new volume covers only that "
                             "region, so SUV ROIs outside it will show '(outside PET)'.")
            roiCount = self.roiSet.roiCount()
            reMeasured = f" ({roiCount} ROI{'s' if roiCount != 1 else ''} will be re-measured)" if roiCount else ""
            items.append(f"The views, the MIP and the ROIs switch to the new volume{reMeasured}. To go back, "
                         f"select <b>{esc(sourceName)}</b> as SPECT/PET and press Go.")
        else:
            headline = "Filtering changes CT/MR voxel values"
            items.append("Voxel values (e.g. Hounsfield units) of the filtered volume will differ from the original. "
                         "SUV measurements are not affected: they are read from the SPECT/PET volume.")
            if targetSpacing is not None:
                items.append(f"The image is resampled from {spacingText} mm to "
                             f"{' × '.join(f'{v:g}' for v in targetSpacing)} mm voxels and the result stays on that grid.")
            if params["prevent_negative"]:
                items.append("<b>This model sets negative values to 0</b>, which removes negative Hounsfield units "
                             "(air, lung, fat). Check that the model is really meant for CT/MR.")
            if cropped:
                items.append("Only the region inside the crop ROI is filtered; the new volume covers only that region.")
            items.append(f"The views switch to the new volume. To go back, select <b>{esc(sourceName)}</b> "
                         "as CT/MRI and press Go.")

        if cropped:
            items.append(f"The original <b>{esc(sourceName)}</b> itself is not cropped. The crop ROI is removed "
                         "after filtering.")
        if secondNode is not None:
            items.append(f"Dual-channel model: <b>{esc(secondNode.GetName())}</b> is used as the second input.")
        if not job["large"]:
            items.append(f"Working grid about {gridText} voxels; roughly {formatByteSize(job['memoryBytes'])} "
                         "of memory needed.")
        items.append("Research use only: filtered values must not replace measurements on the original images "
                     "for clinical reporting.")

        box = qt.QMessageBox(slicer.util.mainWindow())
        box.setIcon(qt.QMessageBox.Warning)
        box.setWindowTitle("EasyFusion - AI post-processing filter")
        box.setTextFormat(qt.Qt.RichText)
        box.setText(f"<b>{headline}</b><br>Model: {esc(modelName)}<br>"
                    f"The original <b>{esc(sourceName)}</b> is not modified. "
                    f"The result is a new volume: <b>{esc(outputName)}</b>")
        box.setInformativeText("<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>")
        box.addButton("Apply Filter", qt.QMessageBox.AcceptRole)
        cropButton = box.addButton("Crop with ROI first", qt.QMessageBox.ActionRole) if offerCrop else None
        cancelButton = box.addButton(qt.QMessageBox.Cancel)
        box.setDefaultButton(cropButton if cropButton is not None else cancelButton)
        box.setEscapeButton(cancelButton)
        box.exec_()
        clicked = box.clickedButton()
        role = box.buttonRole(clicked) if clicked is not None else None
        if role == qt.QMessageBox.AcceptRole:
            return "apply"
        if role == qt.QMessageBox.ActionRole:
            return "crop"
        return None

    @staticmethod
    def ensureFilterDependencies():
        """PyTorch, MONAI and einops; offers to install what is missing. True when all of them can be imported."""
        try:
            import torch  # noqa: F401
        except ImportError:
            # PyTorch is not installed from here: in the PyTorch Utils module the user can pick the
            # build (CUDA version / CPU) that matches their GPU and driver.
            if slicer.util.confirmOkCancelDisplay(
                    "AI filters need PyTorch, which is not installed.\n\n"
                    "Install it in the PyTorch Utils module, where you can choose the CUDA version that matches "
                    "your GPU (or CPU only), then restart Slicer and try again.\n\n"
                    "Open PyTorch Utils now?"):
                try:
                    slicer.util.selectModule("PyTorchUtils")
                except Exception:
                    slicer.util.errorDisplay("The PyTorch Utils module was not found. Install the 'PyTorch' "
                                             "extension from the Extensions Manager and restart Slicer.")
            return False
        for moduleName, requirement in (("monai", "monai"), ("einops", FILTER_EINOPS_REQUIREMENT)):
            try:
                importlib.import_module(moduleName)
                continue
            except ImportError:
                pass
            if not slicer.util.confirmOkCancelDisplay(
                    f"AI filters need the Python package '{moduleName}', which is not installed.\n"
                    f"Install it now ({requirement})? You may need to restart Slicer afterwards."):
                return False
            qt.QApplication.setOverrideCursor(qt.Qt.WaitCursor)
            try:
                slicer.util.pip_install(requirement)
            finally:
                qt.QApplication.restoreOverrideCursor()
            importlib.invalidate_caches()
            importlib.import_module(moduleName)  # raises if the installation did not work
        return True

    def showFilteredVolume(self, sourceNode, filteredNode, otherNode, target):
        """
        Show a freshly filtered volume in place of its source: same color map and window, same slice positions
        and zoom, the MIP (PET) and the SUV ROIs follow. The source volume stays in the scene, untouched.
        """
        self.logic.copyScalarDisplaySettings(sourceNode, filteredNode)
        if otherNode is not None and not slicer.mrmlScene.IsNodePresent(otherNode):
            otherNode = None
        if target == FILTER_TARGET_PET:
            petNode, ctNode = filteredNode, otherNode
        else:
            petNode, ctNode = otherNode, filteredNode
        # Both selectors set explicitly: the other one must still show the volume it showed before the run
        with self.selectorsSetByPanel():
            if petNode is not None:
                self.inputVolumeSelector.setCurrentNode(petNode)
            if ctNode is not None:
                self.inputVolumeSelectorCT.setCurrentNode(ctNode)

        if petNode is not None and ctNode is not None:
            opacities = self.logic.foregroundOpacities(sourceNode)  # keep the user's fusion opacity
            self.logic.rememberVolumes(petNode, ctNode)
            self.logic.applyViewRoles(petNode, ctNode)
            self.logic.restoreForegroundOpacities(opacities)
            # Same anatomy, so nothing is re-fitted: every view keeps its slice, pan and zoom
        else:
            slicer.util.setSliceViewerLayers(background=filteredNode)
        if target == FILTER_TARGET_PET:
            self.logic.moveMipToVolume(sourceNode, filteredNode)
        self.scheduleRoiUpdate()


# ---------------------------------------------------------------------------
# Logic
# ---------------------------------------------------------------------------

class Easy_fusionLogic(ScriptedLoadableModuleLogic):

    # --- Views -------------------------------------------------------------

    @staticmethod
    def stopAllViewRotations():
        for viewNode in slicer.util.getNodesByClass("vtkMRMLViewNode"):
            if viewNode.GetAnimationMode() != ANIMATION_OFF:
                viewNode.SetAnimationMode(ANIMATION_OFF)

    @staticmethod
    def showOnlyThisVolumeRendering(volumeNode):
        for vrDisplayNode in slicer.util.getNodesByClass("vtkMRMLVolumeRenderingDisplayNode"):
            if any(vrDisplayNode.GetAttribute(name) for name in FOREIGN_MIP_ATTRIBUTES):
                continue  # another module's MIP, shown in that module's own view
            if vrDisplayNode.GetVolumeNodeID() != volumeNode.GetID() and vrDisplayNode.GetVisibility():
                vrDisplayNode.SetVisibility(False)

    @staticmethod
    def mipDisplayNode(volumeNode, create=False):
        """EasyFusion's volume rendering display node of volumeNode (the MIP). A volume rendering display node made
        by an earlier version (untagged, not another module's) is adopted. create: make a new one if there is none."""
        if volumeNode is None:
            return None
        candidates = [volumeNode.GetNthDisplayNode(i) for i in range(volumeNode.GetNumberOfDisplayNodes())]
        candidates = [node for node in candidates
                      if node is not None and node.IsA("vtkMRMLVolumeRenderingDisplayNode")]
        for node in candidates:
            if node.GetAttribute(MIP_DISPLAY_ATTRIBUTE):
                return node
        for node in candidates:
            if not any(node.GetAttribute(name) for name in FOREIGN_MIP_ATTRIBUTES):
                node.SetAttribute(MIP_DISPLAY_ATTRIBUTE, "1")
                return node
        if not create:
            return None
        vrLogic = slicer.modules.volumerendering.logic()
        displayNode = vrLogic.CreateVolumeRenderingDisplayNode()
        displayNode.UnRegister(vrLogic)
        displayNode.SetAttribute(MIP_DISPLAY_ATTRIBUTE, "1")
        slicer.mrmlScene.AddNode(displayNode)
        volumeNode.AddAndObserveDisplayNodeID(displayNode.GetID())
        vrLogic.UpdateDisplayNodeFromVolumeNode(displayNode, volumeNode)
        return displayNode

    @staticmethod
    def setMIPRange(vrDisplayNode, lower, upper, flatOpacity=False):
        """MIP shows white at `lower` to black at `upper`. flatOpacity: every intensity fully opaque (set by Go)."""
        propertyNode = vrDisplayNode.GetVolumePropertyNode() if vrDisplayNode is not None else None
        if propertyNode is None:
            return
        colorFunction = propertyNode.GetVolumeProperty().GetRGBTransferFunction(0)
        colorFunction.RemoveAllPoints()
        colorFunction.AddRGBPoint(lower, 1.0, 1.0, 1.0)
        colorFunction.AddRGBPoint(upper, 0.0, 0.0, 0.0)
        if flatOpacity:
            # Opacity 1 over the whole data range. VTK sizes the GPU opacity table as (data range / smallest
            # distance between points); with points only at lower / upper, a narrow grey range on a volume with
            # large values (e.g. Bq/mL, counts) needs millions of entries: "required texture size ... falling
            # back to maximum allowed". Points at the data minimum / maximum give a tiny table, same result.
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

    @staticmethod
    def mipThreeDWidget():
        """
        The 3D widget of the MIP view (view "1"). Not simply 3D widget 0: widgets are numbered in creation order,
        and after another module's layout (e.g. the dosimetry layouts with 3D views 1 and 2) widget 0 can be a
        view that is not shown in the EasyFusion layouts.
        """
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return None
        mipViewNode = slicer.mrmlScene.GetSingletonNode(MIP_VIEW_TAG, "vtkMRMLViewNode")
        firstVisible = None
        for index in range(layoutManager.threeDViewCount):
            widget = layoutManager.threeDWidget(index)
            if widget is None:
                continue
            viewNode = widget.mrmlViewNode()
            if mipViewNode is not None and viewNode is not None and viewNode.GetID() == mipViewNode.GetID():
                return widget
            if firstVisible is None and widget.isVisible():
                firstVisible = widget
        if firstVisible is not None:
            return firstVisible
        return layoutManager.threeDWidget(0) if layoutManager.threeDViewCount > 0 else None

    @staticmethod
    def mipViewNode():
        widget = Easy_fusionLogic.mipThreeDWidget()
        viewNode = widget.mrmlViewNode() if widget is not None else None
        if viewNode is None:
            viewNode = slicer.mrmlScene.GetSingletonNode(MIP_VIEW_TAG, "vtkMRMLViewNode")
        return viewNode

    @staticmethod
    def isEasyFusionNode(node):
        return any(name.startswith("EasyFusion.") for name in (node.GetAttributeNames() or ()))

    @staticmethod
    def isolateMipView(petNode=None):
        """Keep volume renderings, models and segmentations of other modules out of the MIP view, and make sure the
        MIP view uses the MIP technique. A display node without view IDs is shown in every view, so it gets the
        explicit list of all other views (it stays visible where it was)."""
        viewNode = Easy_fusionLogic.mipViewNode()
        if viewNode is None:
            return
        mipViewID = viewNode.GetID()
        others = [node.GetID() for className in ("vtkMRMLViewNode", "vtkMRMLSliceNode")
                  for node in slicer.util.getNodesByClass(className) if node.GetID() != mipViewID]
        for className in FOREIGN_DISPLAY_CLASSES:
            for displayNode in slicer.util.getNodesByClass(className):
                displayable = displayNode.GetDisplayableNode()
                if (displayable is None or displayable.GetHideFromEditors()
                        or displayNode.GetAttribute(MIP_DISPLAY_ATTRIBUTE)
                        or Easy_fusionLogic.isEasyFusionNode(displayable)):
                    continue
                current = [displayNode.GetNthViewNodeID(i) for i in range(displayNode.GetNumberOfViewNodeIDs())]
                if current and mipViewID not in current:
                    continue
                remaining = [viewID for viewID in (current or others) if viewID != mipViewID]
                if remaining:
                    displayNode.SetViewNodeIDs(remaining)
                else:
                    displayNode.SetVisibility(False)
        if petNode is not None:
            vrDisplayNode = Easy_fusionLogic.mipDisplayNode(petNode)
            if vrDisplayNode is not None and vrDisplayNode.GetVisibility():
                Easy_fusionLogic.showMipOnlyInMipView(vrDisplayNode)
        if viewNode.GetRaycastTechnique() != MIP_RAYCAST_TECHNIQUE:
            viewNode.SetRaycastTechnique(MIP_RAYCAST_TECHNIQUE)

    @staticmethod
    def showMipOnlyInMipView(vrDisplayNode):
        """
        Render the MIP volume in the MIP view only, and make that view use the MIP technique. A volume rendering
        without view IDs is drawn in every 3D view, each with its own technique: hidden views left over from
        other layouts (e.g. 3D view 2 of the dosimetry layouts) then render it as Standard (composite).
        Returns the MIP view node (None if there is no 3D view node yet).
        """
        viewNode = Easy_fusionLogic.mipViewNode()
        if vrDisplayNode is None or viewNode is None:
            return None
        viewIDs = [vrDisplayNode.GetNthViewNodeID(i) for i in range(vrDisplayNode.GetNumberOfViewNodeIDs())]
        if viewIDs != [viewNode.GetID()]:
            wasModifying = vrDisplayNode.StartModify()
            vrDisplayNode.RemoveAllViewNodeIDs()
            vrDisplayNode.AddViewNodeID(viewNode.GetID())
            vrDisplayNode.EndModify(wasModifying)
        if viewNode.GetRaycastTechnique() != MIP_RAYCAST_TECHNIQUE:
            viewNode.SetRaycastTechnique(MIP_RAYCAST_TECHNIQUE)
        return viewNode

    @staticmethod
    def getVolumeMaximum(volumeNode):
        """Highest voxel value (e.g. SPECT counts) of a volume; 0 if it has no image data."""
        imageData = volumeNode.GetImageData() if volumeNode is not None else None
        if imageData is None or imageData.GetNumberOfPoints() == 0:
            return 0.0
        return float(imageData.GetScalarRange()[1])

    # --- Layouts and view contents ------------------------------------------

    @staticmethod
    def ensureLayoutsRegistered(singleScreenSafe=False):
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return
        layoutNode = layoutManager.layoutLogic().GetLayoutNode()
        for layoutID, description in buildLayoutDescriptions(singleScreenSafe).items():
            if not layoutNode.IsLayoutDescription(layoutID):
                layoutNode.AddLayoutDescription(layoutID, description)
            elif (layoutNode.GetLayoutDescription(layoutID) != description
                  and layoutNode.GetViewArrangement() != layoutID):
                # Rebuilding the layout that is on screen is never needed and is risky
                layoutNode.SetLayoutDescription(layoutID, description)

    @staticmethod
    def reapplyRestoredCustomLayout():
        """
        Slicer restores the last used layout at startup, before modules can register their layouts. If that
        was an Epona layout, it was applied without a description. Re-applying the arrangement now that the
        description exists is the same call Slicer's own vtkMRMLLayoutLogic makes for this situation.
        """
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return
        layoutNode = layoutManager.layoutLogic().GetLayoutNode()
        arrangement = layoutNode.GetViewArrangement()
        if arrangement in DUAL_MONITOR_LAYOUT_IDS and not hasMultipleScreens():
            # Last session ended in a dual monitor layout, but only one screen now: never build the Monitor 2 window
            logging.info("EasyFusion: single screen, restoring the 2x2 + 3D layout instead of the dual monitor layout")
            layoutNode.SetViewArrangement(SINGLE_SCREEN_FALLBACK_LAYOUT_ID)
        elif arrangement in CUSTOM_LAYOUT_IDS:
            layoutNode.SetViewArrangement(arrangement)

    @staticmethod
    def switchDualMonitorLayoutIfSingleScreen():
        """
        Post-load step. A scene saved in a dual monitor layout was loaded with its dual monitor ID but the
        fallback description (see _onSceneStartImport) when only one screen is connected. Switch to the fallback
        layout's own ID (so its button is highlighted and the scene is saved with it), then put the real dual
        monitor descriptions back so the Dual Monitor buttons work again once a second screen is connected.
        """
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return
        if layoutManager.layout in DUAL_MONITOR_LAYOUT_IDS and not hasMultipleScreens():
            logging.info("EasyFusion: scene was saved in a dual monitor layout; only one screen is connected, "
                         "switching to the 2x2 + 3D layout")
            layoutManager.setLayout(SINGLE_SCREEN_FALLBACK_LAYOUT_ID)
            slicer.util.showStatusMessage(
                "EasyFusion: scene saved for two monitors, shown in the 2×2 + 3D layout (single screen).", 6000)
        # Never replaces the description of the layout on screen (guarded in ensureLayoutsRegistered)
        Easy_fusionLogic.ensureLayoutsRegistered()

    @staticmethod
    def viewUnderCursor():
        """
        ("slice", slice view name) or ("threeD", None) for the view under the mouse, in any window (so the
        Monitor 2 window too); None when the mouse is not over a view.
        """
        widget = qt.QApplication.widgetAt(qt.QCursor.pos())
        while widget is not None:
            if widget.inherits("qMRMLThreeDWidget"):
                return ("threeD", None)
            if widget.inherits("qMRMLSliceWidget"):
                return ("slice", Easy_fusionLogic._sliceViewName(widget))
            widget = widget.parentWidget()
        return None

    @staticmethod
    def _sliceViewName(sliceWidget):
        try:
            return sliceWidget.mrmlSliceNode().GetLayoutName()
        except Exception:
            layoutManager = slicer.app.layoutManager()
            if layoutManager is None:
                return None
            return next((name for name in layoutManager.sliceViewNames()
                         if layoutManager.sliceWidget(name) == sliceWidget), None)

    @staticmethod
    def getSettingsNode(create=True):
        """Saved with the scene: remembers which PET and CT the views were built from."""
        node = slicer.mrmlScene.GetSingletonNode(SETTINGS_NODE_TAG, "vtkMRMLScriptedModuleNode")
        if node is None and create:
            node = slicer.vtkMRMLScriptedModuleNode()
            node.SetSingletonTag(SETTINGS_NODE_TAG)
            node.SetName("EasyFusion")
            node.SetAttribute("ModuleName", "Easy_fusion")
            node.SetHideFromEditors(True)
            node = slicer.mrmlScene.AddNode(node)
        return node

    @staticmethod
    def isModuleShown():
        try:
            return slicer.util.moduleSelector().selectedModule == MODULE_NAME
        except Exception:
            return False

    @staticmethod
    def writeActiveModuleFlag():
        """At save: remember in the scene whether EasyFusion is the module shown (like the Taranis modules)."""
        active = Easy_fusionLogic.isModuleShown()
        settingsNode = Easy_fusionLogic.getSettingsNode(create=active)
        if settingsNode is not None:
            settingsNode.SetParameter(SETTINGS_ACTIVE_MODULE, "true" if active else "false")

    @staticmethod
    def reopenModuleSavedAsActive():
        """After a scene load: open EasyFusion if it was the module shown when the scene was saved."""
        settingsNode = Easy_fusionLogic.getSettingsNode(create=False)
        if settingsNode is None or settingsNode.GetParameter(SETTINGS_ACTIVE_MODULE) != "true":
            return
        if not Easy_fusionLogic.isModuleShown():
            slicer.util.selectModule(MODULE_NAME)

    def rememberVolumes(self, petNode, ctNode):
        settingsNode = self.getSettingsNode()
        settingsNode.SetNodeReferenceID(SETTINGS_PET_ROLE, petNode.GetID() if petNode else None)
        settingsNode.SetNodeReferenceID(SETTINGS_CT_ROLE, ctNode.GetID() if ctNode else None)

    @staticmethod
    def getSliceOrientation(sliceNode):
        if hasattr(sliceNode, "GetOrientation"):
            return sliceNode.GetOrientation()
        return sliceNode.GetOrientationString()

    @staticmethod
    def setSliceOrientation(sliceNode, orientation):
        if hasattr(sliceNode, "SetOrientation"):
            sliceNode.SetOrientation(orientation)
        else:
            getattr(sliceNode, f"SetOrientationTo{orientation}")()

    @staticmethod
    def roleSliceWidgets(visibleOnly=False):
        """(name, slice widget) of every EasyFusion slice view (SLICE_VIEW_ROLES) in the current layout."""
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return []
        widgets = []
        for name in layoutManager.sliceViewNames():
            if name not in SLICE_VIEW_ROLES:
                continue
            sliceWidget = layoutManager.sliceWidget(name)
            if sliceWidget is None or (visibleOnly and not sliceWidget.visible):
                continue
            sliceNode = sliceWidget.mrmlSliceNode()
            if sliceNode is None or not slicer.mrmlScene.IsNodePresent(sliceNode):
                continue  # widget left over from a previous layout / scene
            widgets.append((name, sliceWidget))
        return widgets

    @staticmethod
    def roleSliceNodes():
        """
        (name, slice node, slice composite node) of every EasyFusion view that exists in the scene.
        Works on MRML nodes only, so it is safe while the layout manager is creating or deleting view widgets.
        """
        scene = slicer.mrmlScene
        result = []
        for name in SLICE_VIEW_ROLES:
            sliceNode = scene.GetSingletonNode(name, "vtkMRMLSliceNode")
            compositeNode = scene.GetSingletonNode(name, "vtkMRMLSliceCompositeNode")
            if sliceNode is not None and compositeNode is not None:
                result.append((name, sliceNode, compositeNode))
        return result

    @staticmethod
    def _addWithFixedID(node, nodeID):
        """Add a node under nodeID when that ID is free (otherwise the scene picks one as usual)."""
        if slicer.mrmlScene.GetNodeByID(nodeID) is None:
            try:
                node.SetID(nodeID)
            except Exception:
                pass  # older Slicer: fall back to an automatic ID
        return slicer.mrmlScene.AddNode(node)

    @staticmethod
    def copySliceGeometry(source, target):
        """Same slice position, pan and zoom (anatomical width), keeping the target's aspect ratio."""
        wasModifying = target.StartModify()
        target.GetSliceToRAS().DeepCopy(source.GetSliceToRAS())
        target.SetXYZOrigin(*source.GetXYZOrigin())
        fieldOfView = fieldOfViewForTarget(source.GetFieldOfView(), target.GetDimensions())
        if fieldOfView is not None:
            target.SetFieldOfView(*fieldOfView)
        target.UpdateMatrices()
        target.EndModify(wasModifying)

    @staticmethod
    def findPetOnlyVolume():
        for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
            if node.GetAttribute(PET_ONLY_VOLUME_ATTRIBUTE):
                return node
        return None

    def getOrCreatePetOnlyVolume(self, petNode):
        """
        A volume can only have one color map, so PET-only views show a hidden twin volume that
        shares the PET's voxel data (no copy) but has its own inverted-grey display.
        It is not saved; it gets rebuilt after loading a scene.
        """
        if petNode is None or petNode.GetImageData() is None:
            return None
        node = self.findPetOnlyVolume()
        if node is None:
            # Build the twin completely *before* it enters the scene. Views may already refer to the ID the
            # scene is about to hand out, and must never see a volume without image data or display node.
            node = slicer.vtkMRMLScalarVolumeNode()
            node.SetAttribute(PET_ONLY_VOLUME_ATTRIBUTE, "1")
            node.SetHideFromEditors(True)
            node.SetSaveWithScene(False)
            node.SetName(f"{petNode.GetName()} (PET only)")
            node.CopyOrientation(petNode)
            node.SetAndObserveImageData(petNode.GetImageData())
            node.SetAndObserveTransformNodeID(petNode.GetTransformNodeID())
            node.SetNodeReferenceID(PET_ONLY_SOURCE_ROLE, petNode.GetID())
            displayNode = slicer.vtkMRMLScalarVolumeDisplayNode()
            displayNode.SetSaveWithScene(False)
            displayNode = self._addWithFixedID(displayNode, PET_ONLY_DISPLAY_ID)
            self._configurePetOnlyDisplay(displayNode, petNode)
            node.SetAndObserveDisplayNodeID(displayNode.GetID())
            node = self._addWithFixedID(node, PET_ONLY_VOLUME_ID)
        else:
            node.SetName(f"{petNode.GetName()} (PET only)")
            node.CopyOrientation(petNode)
            if node.GetImageData() is not petNode.GetImageData():
                node.SetAndObserveImageData(petNode.GetImageData())
            if node.GetTransformNodeID() != petNode.GetTransformNodeID():
                node.SetAndObserveTransformNodeID(petNode.GetTransformNodeID())
            node.SetNodeReferenceID(PET_ONLY_SOURCE_ROLE, petNode.GetID())
            if node.GetDisplayNode() is None:
                displayNode = slicer.vtkMRMLScalarVolumeDisplayNode()
                displayNode.SetSaveWithScene(False)
                displayNode = self._addWithFixedID(displayNode, PET_ONLY_DISPLAY_ID)
                node.SetAndObserveDisplayNodeID(displayNode.GetID())
            self._configurePetOnlyDisplay(node.GetDisplayNode(), petNode)

        petDisplayNode = petNode.GetDisplayNode()
        if petDisplayNode is not None:
            bindWindowLevelSync(petDisplayNode, node.GetDisplayNode())
        return node

    @staticmethod
    def _configurePetOnlyDisplay(displayNode, petNode):
        petDisplayNode = petNode.GetDisplayNode()
        wasModifying = displayNode.StartModify()
        displayNode.SetAndObserveColorNodeID(slicer.util.getNode("InvertedGrey").GetID())
        if petDisplayNode is not None:
            displayNode.SetAutoWindowLevel(False)
            displayNode.SetWindow(petDisplayNode.GetWindow())
            displayNode.SetLevel(petDisplayNode.GetLevel())
            displayNode.SetInterpolate(petDisplayNode.GetInterpolate())
        displayNode.EndModify(wasModifying)

    @staticmethod
    def pointPetOnlyViewsAtSourcePet():
        """Called when saving starts. Returns what was changed so it can be undone when saving ends."""
        twin = Easy_fusionLogic.findPetOnlyVolume()
        if twin is None:
            return []
        twinID = twin.GetID()
        sourceID = twin.GetNodeReferenceID(PET_ONLY_SOURCE_ROLE)
        if sourceID is not None and slicer.mrmlScene.GetNodeByID(sourceID) is None:
            sourceID = None
        swaps = []
        for compositeNode in slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode"):
            if compositeNode.GetBackgroundVolumeID() == twinID:
                compositeNode.SetBackgroundVolumeID(sourceID)
                swaps.append((compositeNode, twinID, sourceID))
        return swaps

    @staticmethod
    def restorePetOnlyViews(swaps):
        for compositeNode, twinID, sourceID in swaps:
            if slicer.mrmlScene.GetNodeByID(twinID) is None:
                continue
            if compositeNode.GetBackgroundVolumeID() == sourceID:
                compositeNode.SetBackgroundVolumeID(twinID)

    @staticmethod
    def clearDanglingViewReferences():
        """Remove slice view volume references to nodes that do not exist (e.g. the unsaved twin in older scenes)."""
        scene = slicer.mrmlScene
        roles = (("GetBackgroundVolumeID", "SetBackgroundVolumeID"),
                 ("GetForegroundVolumeID", "SetForegroundVolumeID"),
                 ("GetLabelVolumeID", "SetLabelVolumeID"))
        cleared = 0
        for compositeNode in slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode"):
            for getterName, setterName in roles:
                nodeID = getattr(compositeNode, getterName)()
                if nodeID and scene.GetNodeByID(nodeID) is None:
                    getattr(compositeNode, setterName)(None)
                    cleared += 1
        return cleared

    def applyViewRoles(self, petNode, ctNode, forceOrientation=False):
        """
        Fill every EasyFusion slice view according to SLICE_VIEW_ROLES (also views not in the current layout,
        so switching layouts later shows the right content). Only MRML nodes are touched; the views follow.
        Returns the names of views whose background volume or orientation changed (those get re-fitted).
        """
        if petNode is None or ctNode is None or sceneIsBusy():
            return []
        petOnlyNode = self.getOrCreatePetOnlyVolume(petNode)
        roleNodes = self.roleSliceNodes()

        # Unlink while assigning, so linked views cannot copy volume selections to each other. If the views
        # were linked (Slicer's view-link button, used for scroll / pan / zoom sync), link them all again after.
        compositeNodes = [compositeNode for _, _, compositeNode in roleNodes]
        wasLinked = any(compositeNode.GetLinkedControl() for compositeNode in compositeNodes)
        for compositeNode in compositeNodes:
            if compositeNode.GetLinkedControl():
                compositeNode.SetLinkedControl(False)
        try:
            return self._assignViewVolumes(roleNodes, petNode, ctNode, petOnlyNode, forceOrientation)
        finally:
            if wasLinked:
                for compositeNode in compositeNodes:
                    compositeNode.SetLinkedControl(True)

    def _assignViewVolumes(self, roleNodes, petNode, ctNode, petOnlyNode, forceOrientation):
        """Orientation and background / foreground volumes of each EasyFusion view (see applyViewRoles)."""
        changedViews = []
        for name, sliceNode, compositeNode in roleNodes:
            orientation, content = SLICE_VIEW_ROLES[name]
            if forceOrientation and self.getSliceOrientation(sliceNode) != orientation:
                self.setSliceOrientation(sliceNode, orientation)
                changedViews.append(name)

            if content == "fusion":
                background, foreground = ctNode, petNode
            elif content == "ct":
                background, foreground = ctNode, None
            else:
                background, foreground = petOnlyNode, None
            if background is None:
                continue

            wasModifying = compositeNode.StartModify()
            if compositeNode.GetBackgroundVolumeID() != background.GetID():
                compositeNode.SetBackgroundVolumeID(background.GetID())
                changedViews.append(name)
            foregroundID = foreground.GetID() if foreground is not None else None
            if compositeNode.GetForegroundVolumeID() != foregroundID:
                compositeNode.SetForegroundVolumeID(foregroundID)
                if foreground is not None:
                    # Only when PET is newly placed, so a user-adjusted opacity is kept
                    compositeNode.SetForegroundOpacity(0.5)
            compositeNode.EndModify(wasModifying)
        return changedViews

    def restoreViewRolesAfterLoad(self):
        """PET-only views reference the unsaved twin volume, so rebuild them when a saved EasyFusion layout is loaded."""
        layoutNode = slicer.mrmlScene.GetSingletonNode("vtkMRMLLayoutNode", "vtkMRMLLayoutNode")
        if layoutNode is None or layoutNode.GetViewArrangement() not in CUSTOM_LAYOUT_IDS:
            return
        settingsNode = self.getSettingsNode(create=False)
        if settingsNode is None:
            return
        pet = settingsNode.GetNodeReference(SETTINGS_PET_ROLE)
        ct = settingsNode.GetNodeReference(SETTINGS_CT_ROLE)
        if pet is not None and ct is not None:
            self.applyViewRoles(pet, ct)

    @staticmethod
    def secondaryViewportWindow():
        """The Monitor 2 window (floating dock holding the MIP view), or None if it is docked / not shown."""
        candidates = []
        widget = Easy_fusionLogic.mipThreeDWidget()
        if widget is not None:
            candidates.append(widget.window())
        candidates += [w for w in qt.QApplication.topLevelWidgets() if w.windowTitle == DUAL_MONITOR_WINDOW_TITLE]
        for window in candidates:
            if window is None or window.inherits("qSlicerMainWindow") or window.inherits("QMainWindow"):
                continue
            if window.inherits("QDockWidget") and not getattr(window, "floating", True):
                continue  # docked into the main window by the user: leave it there
            return window
        return None

    @staticmethod
    def secondaryScreen():
        """A screen other than the one showing the main window, or None with a single screen."""
        return secondaryScreen()

    @staticmethod
    def placeSecondaryViewportWindow():
        """Give the Monitor 2 window normal window buttons and show it maximized on the second screen."""
        if sceneIsBusy():
            # Never move windows while the layout manager is rebuilding views for a scene being loaded
            runWhenSceneSettled(Easy_fusionLogic.placeSecondaryViewportWindow)
            return
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None or layoutManager.layout not in DUAL_MONITOR_LAYOUT_IDS:
            return
        try:
            window = Easy_fusionLogic.secondaryViewportWindow()
            if window is None:
                return
            # A floating view window is a tool window (close button only): normal window, maximized on screen 2.
            # Only what is not so already (see showOnSecondScreen: switching between the two dual monitor layouts
            # keeps the window, and re-applying everything stretched the coronal view).
            showOnSecondScreen(window)
        except Exception:
            logging.exception("EasyFusion: could not place the Monitor 2 window; drag it to the second screen manually")

    @staticmethod
    def placeSecondaryViewportWindowAfterLoad():
        """After a scene load (or at startup) in a dual monitor layout: Monitor 2 window maximized on screen 2."""
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None or layoutManager.layout not in DUAL_MONITOR_LAYOUT_IDS:
            return
        qt.QTimer.singleShot(DUAL_MONITOR_PLACE_DELAY_MS, Easy_fusionLogic.placeSecondaryViewportWindow)

    # --- Filtered volumes --------------------------------------------------

    @staticmethod
    def copyScalarDisplaySettings(sourceNode, targetNode):
        """Same color map, window / level and interpolation, so a filtered volume is shown like its source."""
        sourceDisplay = sourceNode.GetDisplayNode() if sourceNode is not None else None
        if sourceDisplay is None or targetNode is None:
            return
        if targetNode.GetDisplayNode() is None:
            targetNode.CreateDefaultDisplayNodes()
        targetDisplay = targetNode.GetDisplayNode()
        wasModifying = targetDisplay.StartModify()
        if sourceDisplay.GetColorNodeID():
            targetDisplay.SetAndObserveColorNodeID(sourceDisplay.GetColorNodeID())
        targetDisplay.SetAutoWindowLevel(False)
        targetDisplay.SetWindow(sourceDisplay.GetWindow())
        targetDisplay.SetLevel(sourceDisplay.GetLevel())
        targetDisplay.SetInterpolate(sourceDisplay.GetInterpolate())
        targetDisplay.EndModify(wasModifying)

    @staticmethod
    def foregroundOpacities(volumeNode):
        """{composite node: opacity} of the EasyFusion views that show volumeNode as foreground (fusion views)."""
        if volumeNode is None:
            return {}
        return {compositeNode: compositeNode.GetForegroundOpacity()
                for _, _, compositeNode in Easy_fusionLogic.roleSliceNodes()
                if compositeNode.GetForegroundVolumeID() == volumeNode.GetID()}

    @staticmethod
    def restoreForegroundOpacities(opacities):
        for compositeNode, opacity in opacities.items():
            if abs(compositeNode.GetForegroundOpacity() - opacity) > 1e-6:
                compositeNode.SetForegroundOpacity(opacity)

    def moveMipToVolume(self, sourceNode, targetNode, keepRange=True):
        """
        If sourceNode is the MIP in the 3D view, show targetNode there instead: with the same grey range, or
        (keepRange False, e.g. SUV -> SPECT counts) with the window of targetNode.
        """
        vrLogic = slicer.modules.volumerendering.logic()
        sourceVr = self.mipDisplayNode(sourceNode)
        if sourceVr is None or not sourceVr.GetVisibility():
            return False
        lower = upper = None
        propertyNode = sourceVr.GetVolumePropertyNode()
        if keepRange and propertyNode is not None:
            lower, upper = propertyNode.GetVolumeProperty().GetRGBTransferFunction(0).GetRange()
        if lower is None or not upper > lower:
            displayNode = targetNode.GetDisplayNode()
            lower = displayNode.GetLevel() - displayNode.GetWindow() / 2.0
            upper = displayNode.GetLevel() + displayNode.GetWindow() / 2.0
        targetVr = self.mipDisplayNode(targetNode, create=True)
        self.showOnlyThisVolumeRendering(targetNode)  # only one MIP at a time
        targetVr.SetVisibility(True)
        self.setMIPRange(targetVr, lower, upper, flatOpacity=True)
        self.showMipOnlyInMipView(targetVr)
        return True

    # --- Custom Hot Iron ---------------------------------------------------

    @staticmethod
    def fillHotIronColorTable(colorNode):
        """(Re)write the table contents. Idempotent, so it also repairs a table that came back broken from disk."""
        fillHotIronTable(colorNode)

    def getOrCreateHotIronColorNode(self):
        colorNode = None
        for node in slicer.util.getNodesByClass("vtkMRMLColorTableNode"):
            if node.GetName() == HOT_IRON_NAME:
                colorNode = node
                break
        if colorNode is None:
            colorNode = slicer.vtkMRMLColorTableNode()
            colorNode.SetName(HOT_IRON_NAME)
            self.fillHotIronColorTable(colorNode)
            slicer.mrmlScene.AddNode(colorNode)  # added exactly once
        else:
            self.fillHotIronColorTable(colorNode)
        return colorNode

    def repairHotIronColorNodes(self):
        """After a scene load: rebuild the Hot Iron table, merge duplicates, and refresh every display using it."""
        candidates = [node for node in slicer.util.getNodesByClass("vtkMRMLColorTableNode")
                      if _HOT_IRON_NAME_PATTERN.match(node.GetName() or "")]
        if not candidates:
            return
        canonical = next((node for node in candidates if node.GetName() == HOT_IRON_NAME), candidates[0])
        canonical.SetName(HOT_IRON_NAME)
        self.fillHotIronColorTable(canonical)

        candidateIDs = {node.GetID() for node in candidates}
        for displayNode in slicer.util.getNodesByClass("vtkMRMLDisplayNode"):
            if displayNode.GetColorNodeID() in candidateIDs:
                # Re-assign (not just "same ID") so the display re-fetches the lookup table object.
                # Go through a valid table: a display node must never be left without a color node.
                wasModifying = displayNode.StartModify()
                greyNode = slicer.mrmlScene.GetNodeByID("vtkMRMLColorTableNodeGrey")
                if greyNode is not None and displayNode.GetColorNodeID() == canonical.GetID():
                    displayNode.SetAndObserveColorNodeID(greyNode.GetID())
                displayNode.SetAndObserveColorNodeID(canonical.GetID())
                displayNode.EndModify(wasModifying)

        for node in candidates:
            if node is not canonical:
                slicer.mrmlScene.RemoveNode(node)

    # --- SUV ROIs ----------------------------------------------------------
    # The ROI nodes are managed by the panel's SphereRoiSet (EponaLib.roiset). These helpers stay for scripts,
    # e.g. an AI lesion detector adding ROIs with addRoi().

    @staticmethod
    def findRoiNode():
        for node in slicer.util.getNodesByClass("vtkMRMLMarkupsFiducialNode"):
            if node.GetAttribute(ROI_NODE_ATTRIBUTE):
                return node
        return None

    getRoiRadius = staticmethod(SphereRoiSet.getRadius)
    setRoiRadius = staticmethod(SphereRoiSet.setRadius)
    getRoiNumber = staticmethod(SphereRoiSet.getNumber)
    getRoiColor = staticmethod(SphereRoiSet.getColor)
    getRoiThreshold = staticmethod(SphereRoiSet.getThreshold)
    setRoiThreshold = staticmethod(SphereRoiSet.setThreshold)
    getRoiOrigin = staticmethod(SphereRoiSet.getOrigin)

    @staticmethod
    def setRoiOrigin(node, pointID, origin):
        if origin not in (ROI_ORIGIN_USER, ROI_ORIGIN_AI):
            raise ValueError(f"ROI origin must be '{ROI_ORIGIN_USER}' or '{ROI_ORIGIN_AI}', not {origin!r}")
        node.SetAttribute(ROI_ORIGIN_ATTRIBUTE + pointID, origin)

    def addRoi(self, node, centerWorld, radius, thresholdMode, thresholdValue, origin=ROI_ORIGIN_AI):
        """
        Add an ROI from code, e.g. from an AI lesion detector, with its own radius, threshold and origin flag.
        node: findRoiNode() (created by the panel's first ROI). The panel picks the ROI up on its next update
        like a hand-placed ROI. Returns the ROI's control point ID.
        """
        index = node.AddControlPoint([float(c) for c in centerWorld])
        pointID = node.GetNthControlPointID(index)
        self.setRoiRadius(node, pointID, radius)
        self.setRoiThreshold(node, pointID, thresholdMode, thresholdValue)
        self.setRoiOrigin(node, pointID, origin)
        return pointID


# ---------------------------------------------------------------------------
# Test ("Reload and Test" in developer mode, and ctest)
# ---------------------------------------------------------------------------

class Easy_fusionTest(ScriptedLoadableModuleTest):
    """Runs the unit tests of the shared EponaLib helpers (EponaLib/tests.py). They need no scene, so the
    scene is left as it is (no Clear), and "Reload and Test" can be used with a study open."""

    def runTest(self):
        self.test_EponaLib()

    def test_EponaLib(self):
        import unittest
        from EponaLib import tests
        self.delayDisplay("Running the EponaLib unit tests")
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(tests))
        self.assertTrue(result.wasSuccessful(), f"{len(result.failures)} failures, {len(result.errors)} errors")
        self.delayDisplay("EponaLib unit tests passed")

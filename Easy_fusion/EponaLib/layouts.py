"""EasyFusion view layouts: layout IDs, layout XML, slice view roles, screens and field of view."""

import logging

import qt
import slicer

# ---------------------------------------------------------------------------
# Layouts
# ---------------------------------------------------------------------------

LAYOUT_FOUR_UP_ID = 3              # Slicer's built-in four-up (vtkMRMLLayoutNode::SlicerLayoutFourUpView)
LAYOUT_AXIAL_FOUR_UP_ID = 7501
LAYOUT_TWO_BY_TWO_ID = 7502             # 2x2 (fusion | CT, axial / sagittal) + 3D; ID kept for saved scenes
LAYOUT_DUAL_MONITOR_ID = 7503
LAYOUT_CT_FUSION_PET_3D_ID = 7504          # full 2x3 (CT | fusion | PET) + 3D
LAYOUT_DUAL_MONITOR_FUSION_MIDDLE_ID = 7505
DUAL_MONITOR_LAYOUT_IDS = (LAYOUT_DUAL_MONITOR_ID, LAYOUT_DUAL_MONITOR_FUSION_MIDDLE_ID)
CUSTOM_LAYOUT_IDS = (LAYOUT_AXIAL_FOUR_UP_ID, LAYOUT_TWO_BY_TWO_ID, LAYOUT_CT_FUSION_PET_3D_ID) + DUAL_MONITOR_LAYOUT_IDS
# Layouts of the panel's layout buttons (Slicer's four-up + the custom ones). "Go" in any other layout switches to
# GO_DEFAULT_LAYOUT_ID first.
EASYFUSION_LAYOUT_IDS = (LAYOUT_FOUR_UP_ID,) + CUSTOM_LAYOUT_IDS
GO_DEFAULT_LAYOUT_ID = LAYOUT_CT_FUSION_PET_3D_ID
# Compare module: TP1 on TP2 registration check, one row of axial | coronal | sagittal (Red, Green, Yellow).
# Not an Epona layout: Epona's "Go" switches away from it.
LAYOUT_REGISTRATION_ID = 7511
REGISTRATION_VIEWS = ("Red", "Green", "Yellow")
DUAL_MONITOR_WINDOW_TITLE = "EasyFusion - Monitor 2"
# Delay before the Monitor 2 window is moved / maximized (after the layout manager has created its views)
DUAL_MONITOR_PLACE_DELAY_MS = 300
# A dual monitor layout found in a saved scene (or restored at startup) on a computer with a single screen is
# replaced by this layout. The floating Monitor 2 window is never created in that case (it crashed Slicer).
SINGLE_SCREEN_FALLBACK_LAYOUT_ID = LAYOUT_TWO_BY_TWO_ID

# Slice view name -> (orientation, content). Content: fusion = CT + PET overlay,
# ct = CT only, pet = PET only (inverted grey). Red/Yellow/Green keep their usual fusion role.
SLICE_VIEW_ROLES = {
    "Red": ("Axial", "fusion"),
    "Yellow": ("Sagittal", "fusion"),
    "Green": ("Coronal", "fusion"),
    "EFAxialCT": ("Axial", "ct"),
    "EFAxialPET": ("Axial", "pet"),
    "EFSagittalCT": ("Sagittal", "ct"),
    "EFSagittalPET": ("Sagittal", "pet"),
    "EFCoronalCT": ("Coronal", "ct"),
}
_SLICE_VIEW_STYLE = {  # label, color (lighter shades for the extra views, like Slicer's "+" views)
    "Red": ("R", "#F34A33"),
    "Yellow": ("Y", "#EDD54C"),
    "Green": ("G", "#6EB04B"),
    "EFAxialCT": ("A-CT", "#f9a99f"),
    "EFAxialPET": ("A-PET", "#f9a99f"),
    "EFSagittalCT": ("S-CT", "#f6e9a2"),
    "EFSagittalPET": ("S-PET", "#f6e9a2"),
    "EFCoronalCT": ("C-CT", "#c6e0b8"),
}
PET_ONLY_VOLUME_ATTRIBUTE = "EasyFusion.PETOnlyDisplayVolume"
PET_ONLY_SOURCE_ROLE = "EasyFusionSourcePET"
# Fixed IDs for the unsaved twin. An auto-numbered ID (e.g. vtkMRMLScalarVolumeNode3) can be handed to a
# completely different volume in the next scene; these can only ever belong to the twin.
PET_ONLY_VOLUME_ID = "vtkMRMLScalarVolumeNodeEasyFusionPETOnly"
PET_ONLY_DISPLAY_ID = "vtkMRMLScalarVolumeDisplayNodeEasyFusionPETOnly"


def _sliceViewItem(name):
    orientation = SLICE_VIEW_ROLES[name][0]
    label, color = _SLICE_VIEW_STYLE[name]
    return (f'<item><view class="vtkMRMLSliceNode" singletontag="{name}">'
            f'<property name="orientation" action="default">{orientation}</property>'
            f'<property name="viewlabel" action="default">{label}</property>'
            f'<property name="viewcolor" action="default">{color}</property>'
            '</view></item>')


_THREED_VIEW_ITEM = ('<item><view class="vtkMRMLViewNode" singletontag="1">'
                     '<property name="viewlabel" action="default">1</property>'
                     '</view></item>')


def _nested(layoutType, items):
    return f'<item><layout type="{layoutType}">' + "".join(items) + '</layout></item>'


def _sliceViews(*names):
    return [_sliceViewItem(name) for name in names]


def _mainViewportAttributes():
    """
    XML attributes of the main-window viewport, copied from Slicer's own dual monitor layouts (same approach as
    the Taranis dosimetry modules), so the first <layout> of <viewports> is always shown in the main window.
    """
    import xml.etree.ElementTree as ElementTree
    try:
        layoutNode = slicer.app.layoutManager().layoutLogic().GetLayoutNode()
        for attributeName in dir(slicer.vtkMRMLLayoutNode):
            if "DualMonitor" not in attributeName:
                continue
            layoutID = getattr(slicer.vtkMRMLLayoutNode, attributeName)
            if not isinstance(layoutID, int) or not layoutNode.IsLayoutDescription(layoutID):
                continue
            root = ElementTree.fromstring(layoutNode.GetLayoutDescription(layoutID))
            if root.tag != "viewports":
                continue
            for element in root.findall("layout"):
                if (element.get("dockable") or "").lower() != "true":
                    name = element.get("name")
                    return f' name="{name}"' if name is not None else ""
    except Exception as e:
        logging.debug(f"EasyFusion: could not read Slicer's dual monitor layouts: {e}")
    return ""  # no name: the main window viewport


def _dualMonitorLayout(axialRow, sagittalRow):
    """
    Main window: axial row over sagittal row. Second window (auto-placed on monitor 2): 3D + coronal.
    The second window is a floating dock widget, exactly like Slicer's built-in dual monitor layouts. A
    non-dockable viewport (a separate top-level window) crashed Slicer when a scene saved in this layout was
    loaded while the main window was maximized.
    """
    return (
        '<viewports>'
        f'<layout type="vertical"{_mainViewportAttributes()}>'
        + _nested("horizontal", _sliceViews(*axialRow))
        + _nested("horizontal", _sliceViews(*sagittalRow))
        + '</layout>'
        f'<layout name="EasyFusionMonitor2" type="horizontal" label="{DUAL_MONITOR_WINDOW_TITLE}" '
        'dockable="true" dockPosition="floating">'
        + _THREED_VIEW_ITEM + _sliceViewItem("Green") + _sliceViewItem("EFCoronalCT")
        + '</layout>'
        '</viewports>')


def hasMultipleScreens():
    """True when Qt reports at least two screens (a second monitor is connected)."""
    try:
        return len(list(qt.QGuiApplication.screens())) >= 2
    except Exception:
        logging.debug("EasyFusion: could not count screens, assuming a single screen")
        return False


def secondaryScreen():
    """A screen other than the one showing the main window, or None with a single screen."""
    mainWindow = slicer.util.mainWindow()
    screens = list(qt.QGuiApplication.screens())
    if mainWindow is None or len(screens) < 2:
        return None
    mainCenter = mainWindow.frameGeometry.center()
    return next((screen for screen in screens if not screen.geometry.contains(mainCenter)), None)


def _monitor2WindowFlags():
    return (qt.Qt.Window | qt.Qt.CustomizeWindowHint | qt.Qt.WindowTitleHint | qt.Qt.WindowSystemMenuHint
            | qt.Qt.WindowMinMaxButtonsHint | qt.Qt.WindowCloseButtonHint)


def hasAllFlags(currentFlags, wantedFlags):
    try:
        return (int(currentFlags) & int(wantedFlags)) == int(wantedFlags)
    except (TypeError, ValueError):
        return False


def showOnSecondScreen(window):
    """
    Give the floating Monitor 2 window normal window buttons and show it maximized on the second screen, changing
    only what is not so already. Re-applying the window flags hides and re-shows the window, and re-maximizing it
    resizes it again: done on every switch between two dual monitor layouts (which keep the same Monitor 2
    window), that resized views behind Slicer's back and left the coronal view stretched.
    """
    wantedFlags = _monitor2WindowFlags()
    if not hasAllFlags(window.windowFlags(), wantedFlags):
        window.setWindowFlags(wantedFlags)
    screen = secondaryScreen()
    if screen is None:
        if not window.visible:
            window.show()   # single screen: do not cover the main window
        window.raise_()
        return
    onSecondScreen = screen.geometry.contains(window.frameGeometry.center())
    if not onSecondScreen:
        window.setGeometry(screen.availableGeometry)   # on the second screen first, then maximized there
    if not (onSecondScreen and window.isMaximized() and window.visible):
        window.showMaximized()
    window.raise_()


def buildLayoutDescriptions(singleScreenSafe=False):
    """
    Layout XML for the custom EasyFusion layouts, keyed by layout ID.
    singleScreenSafe: the dual monitor IDs get the fallback (single window) layout instead of their
    two-viewport description, so a scene saved in a dual monitor layout builds no floating window while loading.
    """
    axialFourUp = (
        '<layout type="vertical">'
        + _nested("horizontal", [_sliceViewItem("Red"), _THREED_VIEW_ITEM])
        + _nested("horizontal", _sliceViews("EFAxialCT", "EFAxialPET"))
        + '</layout>')

    # 3 columns: [axial fusion / sagittal fusion] [axial CT / sagittal CT] [3D spanning both rows]
    twoByTwo = (
        '<layout type="horizontal">'
        + _nested("vertical", _sliceViews("Red", "Yellow"))
        + _nested("vertical", _sliceViews("EFAxialCT", "EFSagittalCT"))
        + _THREED_VIEW_ITEM
        + '</layout>')

    # 4 columns: [axial CT / sagittal CT] [axial fusion / sagittal fusion] [axial PET / sagittal PET] [3D]
    ctFusionPet3D = (
        '<layout type="horizontal">'
        + _nested("vertical", _sliceViews("EFAxialCT", "EFSagittalCT"))
        + _nested("vertical", _sliceViews("Red", "Yellow"))
        + _nested("vertical", _sliceViews("EFAxialPET", "EFSagittalPET"))
        + _THREED_VIEW_ITEM
        + '</layout>')

    descriptions = {
        LAYOUT_AXIAL_FOUR_UP_ID: axialFourUp,
        LAYOUT_TWO_BY_TWO_ID: twoByTwo,
        LAYOUT_DUAL_MONITOR_ID: _dualMonitorLayout(
            ("Red", "EFAxialCT", "EFAxialPET"), ("Yellow", "EFSagittalCT", "EFSagittalPET")),
        LAYOUT_CT_FUSION_PET_3D_ID: ctFusionPet3D,
        LAYOUT_DUAL_MONITOR_FUSION_MIDDLE_ID: _dualMonitorLayout(
            ("EFAxialCT", "Red", "EFAxialPET"), ("EFSagittalCT", "Yellow", "EFSagittalPET")),
    }
    if singleScreenSafe:
        for layoutID in DUAL_MONITOR_LAYOUT_IDS:
            descriptions[layoutID] = descriptions[SINGLE_SCREEN_FALLBACK_LAYOUT_ID]
    return descriptions


def registrationLayoutDescription():
    """Axial | coronal | sagittal in one row (the registration views of the compare module)."""
    return '<layout type="horizontal">' + "".join(_sliceViews(*REGISTRATION_VIEWS)) + '</layout>'


# ---------------------------------------------------------------------------
# Compare module: two time points (TP1 top row, TP2 bottom row)
# ---------------------------------------------------------------------------

COMPARE_VIEW_PREFIX = "EpCmp"
# Slice view name -> (time point, orientation, content). Content: fusion = CT/MR + PET, ct = CT/MR only,
# pet = PET only (inverted grey twin).
_COMPARE_VIEW_KINDS = (("AxFus", "Axial", "fusion", "Ax"), ("AxCT", "Axial", "ct", "Ax CT"),
                       ("AxPET", "Axial", "pet", "Ax PET"), ("CorFus", "Coronal", "fusion", "Cor"),
                       ("CorPET", "Coronal", "pet", "Cor PET"))
COMPARE_SLICE_VIEWS = {f"{COMPARE_VIEW_PREFIX}{tp}{key}": (tp, orientation, content)
                       for tp in (1, 2) for key, orientation, content, _ in _COMPARE_VIEW_KINDS}
_COMPARE_VIEW_LABELS = {f"{COMPARE_VIEW_PREFIX}{tp}{key}": f"{tp} {label}"
                        for tp in (1, 2) for key, _, _, label in _COMPARE_VIEW_KINDS}
COMPARE_MIP_VIEWS = {f"{COMPARE_VIEW_PREFIX}{tp}MIP": tp for tp in (1, 2)}
COMPARE_VIEW_COLORS = {1: "#5b9bd5", 2: "#e8962e"}   # time point 1 blue, time point 2 orange
LAYOUT_COMPARE_AXIAL_CT_ID = 7521           # rows TP1 / TP2: axial fusion | axial CT | MIP
LAYOUT_COMPARE_AXIAL_PET_ID = 7522          # rows TP1 / TP2: axial fusion | axial PET | MIP
LAYOUT_COMPARE_AXIAL_CORONAL_ID = 7523      # rows TP1 / TP2: axial fusion | coronal fusion | MIP
LAYOUT_COMPARE_DUAL_MONITOR_ID = 7524       # monitor 1: axial CT | fusion | PET; monitor 2: MIP | cor fusion | cor PET
COMPARE_LAYOUT_IDS = (LAYOUT_COMPARE_AXIAL_CT_ID, LAYOUT_COMPARE_AXIAL_PET_ID, LAYOUT_COMPARE_AXIAL_CORONAL_ID,
                      LAYOUT_COMPARE_DUAL_MONITOR_ID)
COMPARE_DUAL_MONITOR_LAYOUT_IDS = (LAYOUT_COMPARE_DUAL_MONITOR_ID,)
COMPARE_DEFAULT_LAYOUT_ID = LAYOUT_COMPARE_AXIAL_CT_ID
COMPARE_DUAL_MONITOR_WINDOW_TITLE = "Epona Compare - Monitor 2"
COMPARE_PET_ONLY_ATTRIBUTE = "EponaCompare.PETOnlyTwin"   # "1" / "2": inverted-grey twin of that time point's PET


def isCompareView(name):
    """True for the views of the compare module (slice or 3D view name / singleton tag)."""
    return bool(name) and str(name).startswith(COMPARE_VIEW_PREFIX)


def compareViewTimePoint(name):
    """Time point (1 / 2) a compare view belongs to, or None."""
    if name in COMPARE_SLICE_VIEWS:
        return COMPARE_SLICE_VIEWS[name][0]
    return COMPARE_MIP_VIEWS.get(name)


def compareViewName(timePoint, key):
    """View name of a time point: key is AxFus, AxCT, AxPET, CorFus, CorPET or MIP."""
    return f"{COMPARE_VIEW_PREFIX}{timePoint}{key}"


def _compareSliceItem(name):
    timePoint, orientation, _ = COMPARE_SLICE_VIEWS[name]
    return (f'<item><view class="vtkMRMLSliceNode" singletontag="{name}">'
            f'<property name="orientation" action="default">{orientation}</property>'
            f'<property name="viewlabel" action="default">{_COMPARE_VIEW_LABELS[name]}</property>'
            f'<property name="viewcolor" action="default">{COMPARE_VIEW_COLORS[timePoint]}</property>'
            '</view></item>')


def _compareMipItem(name):
    timePoint = COMPARE_MIP_VIEWS[name]
    return (f'<item><view class="vtkMRMLViewNode" singletontag="{name}">'
            f'<property name="viewlabel" action="default">{timePoint} MIP</property>'
            f'<property name="viewcolor" action="default">{COMPARE_VIEW_COLORS[timePoint]}</property>'
            '</view></item>')


def _compareItem(timePoint, key):
    name = compareViewName(timePoint, key)
    return _compareMipItem(name) if key == "MIP" else _compareSliceItem(name)


def _compareRows(keys):
    """Items of two rows (time point 1 above time point 2), each with the views in keys."""
    return "".join(_nested("horizontal", [_compareItem(timePoint, key) for key in keys]) for timePoint in (1, 2))


def buildCompareLayoutDescriptions(singleScreenSafe=False):
    """Layout XML of the compare layouts, keyed by layout ID (singleScreenSafe: see buildLayoutDescriptions)."""
    descriptions = {
        LAYOUT_COMPARE_AXIAL_CT_ID: '<layout type="vertical">' + _compareRows(("AxFus", "AxCT", "MIP")) + '</layout>',
        LAYOUT_COMPARE_AXIAL_PET_ID: '<layout type="vertical">' + _compareRows(("AxFus", "AxPET", "MIP")) + '</layout>',
        LAYOUT_COMPARE_AXIAL_CORONAL_ID: ('<layout type="vertical">' + _compareRows(("AxFus", "CorFus", "MIP"))
                                          + '</layout>'),
    }
    if singleScreenSafe:
        descriptions[LAYOUT_COMPARE_DUAL_MONITOR_ID] = descriptions[LAYOUT_COMPARE_AXIAL_CT_ID]
    else:
        descriptions[LAYOUT_COMPARE_DUAL_MONITOR_ID] = (
            '<viewports>'
            f'<layout type="vertical"{_mainViewportAttributes()}>' + _compareRows(("AxCT", "AxFus", "AxPET"))
            + '</layout>'
            f'<layout name="EponaCompareMonitor2" type="vertical" label="{COMPARE_DUAL_MONITOR_WINDOW_TITLE}" '
            'dockable="true" dockPosition="floating">' + _compareRows(("MIP", "CorFus", "CorPET")) + '</layout>'
            '</viewports>')
    return descriptions


def registerLayout(layoutID, description):
    """Add a layout description to Slicer (or update it, unless that layout is on screen)."""
    layoutManager = slicer.app.layoutManager()
    if layoutManager is None:
        return False
    layoutNode = layoutManager.layoutLogic().GetLayoutNode()
    if not layoutNode.IsLayoutDescription(layoutID):
        layoutNode.AddLayoutDescription(layoutID, description)
    elif layoutNode.GetLayoutDescription(layoutID) != description and layoutNode.GetViewArrangement() != layoutID:
        layoutNode.SetLayoutDescription(layoutID, description)
    return True


def fieldOfViewForTarget(sourceFieldOfView, targetDimensions):
    """Same anatomical width as the source, height following the target view's aspect ratio."""
    width = float(sourceFieldOfView[0])
    if targetDimensions[0] <= 0 or targetDimensions[1] <= 0:
        return None
    return (width, width * float(targetDimensions[1]) / float(targetDimensions[0]), float(sourceFieldOfView[2]))

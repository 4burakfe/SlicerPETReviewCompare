"""
Module toolbar shared by Epona and Epona Compare: one row of small buttons at the top of the main window, shown
while the module is open and hidden when the user goes to another module.

Each module fills its own toolbar (volume selectors, window presets, color maps, layouts, MIP rotation); this file
provides the toolbar itself and the parts that are the same in both:
  - picture buttons (window presets, layouts) and color map swatches drawn from the color table itself,
  - groups of buttons that can be shown / hidden together (e.g. SUV presets only for a volume in SUV),
  - "hide the module panel" (the panel always comes back when the user leaves the module),
  - "hide this toolbar" (remembered; the module panel has a Show Toolbar button to bring it back).
Icons: Resources/Icons/Toolbar next to the EponaLib folder (source tree: Easy_fusion/Resources, installed
extension: the Resources folder of the scripted modules).
"""

import logging
import os

import qt
import slicer

from .display import findColorNode
from .layouts import (
    LAYOUT_AXIAL_FOUR_UP_ID, LAYOUT_COMPARE_AXIAL_CORONAL_ID, LAYOUT_COMPARE_AXIAL_CT_ID, LAYOUT_COMPARE_AXIAL_PET_ID,
    LAYOUT_COMPARE_DUAL_MONITOR_ID, LAYOUT_CT_FUSION_PET_3D_ID, LAYOUT_DUAL_MONITOR_FUSION_MIDDLE_ID,
    LAYOUT_DUAL_MONITOR_ID, LAYOUT_FOUR_UP_ID, LAYOUT_TWO_BY_TWO_ID,
)

ICON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Resources", "Icons", "Toolbar")
SETTINGS_SHOW_TOOLBAR = "Epona/ShowToolbar"     # shared by both modules
PANEL_DOCK_NAME = "PanelDockWidget"              # Slicer's module panel (left)
PRESET_ICON_SIZE = (30, 30)
LAYOUT_ICON_SIZE = (46, 29)
GLYPH_ICON_SIZE = (22, 22)
COLOR_ICON_SIZE = (12, 26)
SELECTOR_MIN_CONTENTS = 16                       # characters shown in a toolbar volume selector (QComboBox)
TOOLBAR_SELECTOR_WIDTH = 150                     # px, toolbar volume selector (qMRMLNodeComboBox)

# Picture of each window preset (examples of that window, see values.py for the presets)
CT_PRESET_ICONS = {"CT: Abdomen": "preset_ct_abdomen", "CT: Head": "preset_ct_head", "CT: Lungs": "preset_ct_lungs",
                   "CT: Bones": "preset_ct_bones"}
SUV_PRESET_ICONS = {5.0: "preset_suv_5", 7.0: "preset_suv_7", 10.0: "preset_suv_10", 15.0: "preset_suv_15",
                    25.0: "preset_suv_25"}
PERCENT_PRESET_ICONS = {10: "preset_pct_10", 25: "preset_pct_25", 50: "preset_pct_50", 75: "preset_pct_75",
                        100: "preset_pct_100"}
MRI_PRESET_ICONS = {"Standard": "preset_mri_standard", "Wide": "preset_mri_wide", "Contrast": "preset_mri_contrast",
                    "Bright": "preset_mri_bright"}
# Layout pictograms (cells: fusion, CT, PET in inverted grey, MIP; compare rows marked blue (1) / orange (2))
LAYOUT_ICONS = {
    LAYOUT_FOUR_UP_ID: "layout_four_up", LAYOUT_AXIAL_FOUR_UP_ID: "layout_axial_four_up",
    LAYOUT_TWO_BY_TWO_ID: "layout_two_by_two_3d", LAYOUT_CT_FUSION_PET_3D_ID: "layout_ct_fusion_pet_3d",
    LAYOUT_DUAL_MONITOR_ID: "layout_dual_monitor", LAYOUT_DUAL_MONITOR_FUSION_MIDDLE_ID: "layout_dual_monitor_fusion_middle",
    LAYOUT_COMPARE_AXIAL_CT_ID: "layout_compare_axial_ct", LAYOUT_COMPARE_AXIAL_PET_ID: "layout_compare_axial_pet",
    LAYOUT_COMPARE_AXIAL_CORONAL_ID: "layout_compare_axial_coronal",
    LAYOUT_COMPARE_DUAL_MONITOR_ID: "layout_compare_dual_monitor",
}


def iconPath(name):
    return os.path.join(ICON_DIR, name + ".png")


def loadIcon(name):
    path = iconPath(name)
    if not os.path.exists(path):
        logging.warning(f"Epona: toolbar icon not found: {path}")
        return qt.QIcon()
    return qt.QIcon(path)


def richToolTip(name, details=""):
    """Tooltip with the name in bold (toolbar buttons have pictures only)."""
    import html
    text = f"<b>{html.escape(name)}</b>"
    if details:
        text += "<br>" + html.escape(details).replace("\n", "<br>")
    return text


def isToolbarEnabled():
    value = qt.QSettings().value(SETTINGS_SHOW_TOOLBAR)
    return str(value).lower() not in ("false", "0")   # default: shown


def setToolbarEnabledSetting(enabled):
    qt.QSettings().setValue(SETTINGS_SHOW_TOOLBAR, "true" if enabled else "false")


def modulePanelDock():
    """Slicer's module panel dock widget (left side), or None."""
    mainWindow = slicer.util.mainWindow()
    if mainWindow is None:
        return None
    try:
        return slicer.util.findChild(mainWindow, PANEL_DOCK_NAME)
    except Exception:
        return None


def colorBarPixmap(rgbAt, size=COLOR_ICON_SIZE):
    """Vertical color bar: rgbAt(t) for t from 0 (bottom) to 1 (top), components 0..1."""
    width, height = size
    pixmap = qt.QPixmap(width, height)
    pixmap.fill(qt.QColor(0, 0, 0, 0))
    painter = qt.QPainter(pixmap)
    try:
        for y in range(height):
            t = 1.0 - y / float(max(height - 1, 1))
            r, g, b = (min(max(float(c), 0.0), 1.0) for c in rgbAt(t)[:3])
            painter.fillRect(0, y, width, 1, qt.QColor(int(round(r * 255)), int(round(g * 255)), int(round(b * 255))))
        painter.setPen(qt.QColor(120, 120, 120))
        painter.drawRect(0, 0, width - 1, height - 1)
    finally:
        painter.end()
    return pixmap


def colorNodeRgbFunction(colorNode):
    """
    rgbAt(t) of a color node over its whole range, or None.
    Color tables (Inferno, Red, Hot Iron, ...) are read entry by entry. Other color nodes (the procedural PET maps)
    through their color function, with the two-argument GetColor: vtkLookupTable hides the one-argument form in
    Python, which left the table swatches empty.
    """
    if colorNode is None:
        return None
    count = colorNode.GetNumberOfColors() if colorNode.IsA("vtkMRMLColorTableNode") else 0
    if count > 0:
        def tableRgbAt(t):
            rgba = [0.0, 0.0, 0.0, 0.0]
            colorNode.GetColor(int(round(t * (count - 1))), rgba)
            return rgba[:3]
        return tableRgbAt
    scalarsToColors = colorNode.GetScalarsToColors()
    if scalarsToColors is None:
        return None
    lower, upper = scalarsToColors.GetRange()

    def rgbAt(t):
        rgb = [0.0, 0.0, 0.0]
        scalarsToColors.GetColor(lower + t * (upper - lower), rgb)
        return rgb
    return rgbAt


class EponaToolBar:
    """
    The toolbar of one module. Build it once (build), fill it with the add* methods, then call enter() / exit()
    from the module's enter() / exit(), and destroy() from its cleanup().
    """

    def __init__(self, objectName, title, moduleName=None, onEnabledChanged=None):
        """
        moduleName: the owning module (the toolbar is only ever shown while it is the selected module).
        onEnabledChanged(enabled): the toolbar was hidden with its own button (the panel's button follows).
        """
        self.objectName = objectName
        self.title = title
        self.moduleName = moduleName
        self.onEnabledChanged = onEnabledChanged
        self.toolBar = None
        self.active = False            # the module is shown
        self._groups = {}              # group name -> [QAction]
        self._layoutButtons = {}       # layout ID -> QToolButton
        self._colorButtons = []        # (QToolButton, color name, rgb function or None)
        self._colorIconsReady = False
        self._rotationButton = None
        self._panelButton = None
        self._panelHiddenByToolbar = False

    # --- creation ----------------------------------------------------------------------------------------

    def build(self):
        """Create the (hidden) toolbar in its own row at the top of the main window. False without a main window."""
        mainWindow = slicer.util.mainWindow()
        if mainWindow is None:
            return False
        self._removeLeftovers(mainWindow)
        toolBar = qt.QToolBar(self.title)
        toolBar.setObjectName(self.objectName)
        toolBar.setMovable(True)
        toolBar.setFloatable(False)
        toolBar.setToolButtonStyle(qt.Qt.ToolButtonIconOnly)
        toolBar.setIconSize(qt.QSize(*PRESET_ICON_SIZE))
        toolBar.toggleViewAction().setVisible(False)   # not in View > Toolbars: it belongs to the module
        mainWindow.addToolBarBreak(qt.Qt.TopToolBarArea)
        mainWindow.addToolBar(qt.Qt.TopToolBarArea, toolBar)
        toolBar.hide()
        self.toolBar = toolBar
        return True

    def _removeLeftovers(self, mainWindow):
        """A toolbar of an earlier copy of the module (developer Reload without cleanup) is removed."""
        try:
            for old in slicer.util.findChildren(mainWindow, name=self.objectName):
                mainWindow.removeToolBarBreak(old)
                mainWindow.removeToolBar(old)
                old.setParent(None)
                old.deleteLater()
        except Exception:
            logging.debug("Epona: could not look for an old toolbar", exc_info=True)

    def destroy(self):
        """Module cleanup: remove the toolbar and give the module panel back."""
        self._restorePanel()
        if self.toolBar is None:
            return
        mainWindow = slicer.util.mainWindow()
        try:
            if mainWindow is not None:
                mainWindow.removeToolBarBreak(self.toolBar)   # the row break added by build()
                mainWindow.removeToolBar(self.toolBar)
            self.toolBar.setParent(None)
            self.toolBar.deleteLater()
        except Exception:
            logging.debug("Epona: could not remove the toolbar", exc_info=True)
        self.toolBar = None

    # --- content -----------------------------------------------------------------------------------------

    def _add(self, widget, group=None):
        action = self.toolBar.addWidget(widget)
        if group is not None:
            self._groups.setdefault(group, []).append(action)
        return action

    def addSeparator(self, group=None):
        action = self.toolBar.addSeparator()
        if group is not None:
            self._groups.setdefault(group, []).append(action)
        return action

    def addLabel(self, text, toolTip="", color=None, group=None):
        label = qt.QLabel(text)
        label.setToolTip(toolTip)
        style = "margin-left: 4px; margin-right: 2px; font-weight: bold;"
        if color:
            style += f" color: {color};"
        label.setStyleSheet(style)
        self._add(label, group)
        return label

    def addWidget(self, widget, toolTip=None, group=None):
        if toolTip:
            widget.setToolTip(toolTip)
        self._add(widget, group)
        return widget

    def addButton(self, icon, toolTip, callback, iconSize=GLYPH_ICON_SIZE, checkable=False, group=None):
        """Small icon button. icon: QIcon or an icon name in Resources/Icons/Toolbar. callback() on click."""
        button = qt.QToolButton()
        button.setIcon(loadIcon(icon) if isinstance(icon, str) else icon)
        button.setIconSize(qt.QSize(*iconSize))
        button.setToolTip(toolTip)
        button.setAutoRaise(True)
        button.setCheckable(checkable)
        button.setFocusPolicy(qt.Qt.NoFocus)   # keyboard shortcuts (F5-F9, Insert) stay with the views
        button.connect("clicked()", callback)
        self._add(button, group)
        return button

    def addPresetButton(self, iconName, toolTip, callback, group=None):
        return self.addButton(iconName, toolTip, callback, PRESET_ICON_SIZE, group=group)

    def addColorButton(self, colorName, toolTip, callback, rgbFunction=None):
        """Color map swatch. rgbFunction(t) for maps that are not color nodes (Hot Iron); else the color node's
        table is drawn (when the toolbar is first shown: the color nodes exist by then)."""
        button = self.addButton(qt.QIcon(), toolTip, callback, COLOR_ICON_SIZE)
        self._colorButtons.append((button, colorName, rgbFunction))
        return button

    def addLayoutButtons(self, layouts, callback):
        """layouts: (layout ID, icon name, tooltip). callback(layoutID); the current layout's button is checked."""
        for layoutID, iconName, toolTip in layouts:
            button = self.addButton(iconName, toolTip, lambda layoutID=layoutID: self._onLayoutClicked(layoutID, callback),
                                    LAYOUT_ICON_SIZE, checkable=True)
            self._layoutButtons[layoutID] = button

    def _onLayoutClicked(self, layoutID, callback):
        try:
            callback(layoutID)
        finally:
            layoutManager = slicer.app.layoutManager()
            self.setCurrentLayout(layoutManager.layout if layoutManager is not None else None)

    def setCurrentLayout(self, layoutID):
        for buttonLayoutID, button in self._layoutButtons.items():
            wasBlocked = button.blockSignals(True)
            button.checked = buttonLayoutID == layoutID
            button.blockSignals(wasBlocked)

    def addRotationButton(self, callback):
        """Start / stop MIP rotation: callback(enabled). The module keeps the button in sync (setRotating)."""
        button = self.addButton("mip_rotate", "Start MIP rotation", lambda: callback(self._rotationButton.checked),
                                GLYPH_ICON_SIZE, checkable=True)
        self._rotationButton = button
        return button

    def addTextButton(self, text, toolTip, callback, group=None):
        """Small button showing a letter or two (e.g. the MIP quick views A / L / R)."""
        button = qt.QToolButton()
        button.setText(text)
        button.setToolButtonStyle(qt.Qt.ToolButtonTextOnly)
        button.setFixedSize(qt.QSize(26, 26))
        button.setStyleSheet("QToolButton { font-weight: bold; }")
        button.setToolTip(toolTip)
        button.setAutoRaise(True)
        button.setFocusPolicy(qt.Qt.NoFocus)
        button.connect("clicked()", callback)
        self._add(button, group)
        return button

    def setRotating(self, spinning):
        button = self._rotationButton
        if button is None:
            return
        wasBlocked = button.blockSignals(True)
        button.checked = bool(spinning)
        button.blockSignals(wasBlocked)
        button.setToolTip("Stop MIP rotation" if spinning else "Start MIP rotation")

    def addWindowButtons(self):
        """Hide / show the module panel, and hide this toolbar (the last buttons of the row)."""
        self.addSeparator()
        self._panelButton = self.addButton("panel_hide", "Hide the module panel (it comes back when you leave "
                                           "the module)", self._onPanelButtonClicked, GLYPH_ICON_SIZE)
        self.addButton("toolbar_hide", "Hide this toolbar (bring it back with the 'Show Toolbar' button in the module panel)",
                       self._onHideToolbarClicked, GLYPH_ICON_SIZE)

    # --- groups ------------------------------------------------------------------------------------------

    def setGroupVisible(self, group, visible):
        for action in self._groups.get(group, ()):
            if action.isVisible() != bool(visible):
                action.setVisible(bool(visible))

    # --- module panel ------------------------------------------------------------------------------------

    def _onPanelButtonClicked(self):
        dock = modulePanelDock()
        if dock is None:
            slicer.util.showStatusMessage("Epona: the module panel was not found", 3000)
            return
        self.setPanelVisible(not dock.isVisible())

    def setPanelVisible(self, visible):
        dock = modulePanelDock()
        if dock is None:
            return
        dock.setVisible(bool(visible))
        self._panelHiddenByToolbar = not visible
        self._syncPanelButton()

    def _syncPanelButton(self):
        if self._panelButton is None:
            return
        dock = modulePanelDock()
        hidden = dock is not None and not dock.isVisible()
        self._panelButton.setIcon(loadIcon("panel_show" if hidden else "panel_hide"))
        self._panelButton.setToolTip("Show the module panel" if hidden else
                                     "Hide the module panel (it comes back when you leave the module)")

    def _restorePanel(self):
        if self._panelHiddenByToolbar:
            dock = modulePanelDock()
            if dock is not None and not dock.isVisible():
                dock.setVisible(True)
            self._panelHiddenByToolbar = False

    # --- visibility --------------------------------------------------------------------------------------

    def _onHideToolbarClicked(self):
        self.setEnabled(False)
        if self.onEnabledChanged is not None:
            self.onEnabledChanged(False)

    def _moduleShown(self):
        """The owning module is the one shown (also if enter() was not reached, e.g. after an error)."""
        if self.active:
            return True
        try:
            if self.moduleName and slicer.util.selectedModule() == self.moduleName:
                self.active = True
        except Exception:
            logging.debug("Epona: could not read the selected module", exc_info=True)
        return self.active

    def isShown(self):
        return self.toolBar is not None and self.toolBar.isVisible()

    def setEnabled(self, enabled):
        """Show / hide the toolbar for both modules (remembered). Hiding it brings the module panel back, where
        'Show Toolbar' turns it on again."""
        enabled = bool(enabled)
        setToolbarEnabledSetting(enabled)
        if not enabled:
            self._restorePanel()
            self._syncPanelButton()
        if self.toolBar is None:
            logging.warning("Epona: the toolbar could not be created (no main window)")
            return
        visible = enabled and self._moduleShown()
        if visible:
            self._ensureColorIcons()
            self._syncPanelButton()
        self.toolBar.setVisible(visible)

    def enter(self):
        """The module is shown."""
        self.active = True
        if self.toolBar is None:
            return
        if isToolbarEnabled():
            self._ensureColorIcons()
            self.toolBar.show()
        self._syncPanelButton()

    def exit(self):
        """The user went to another module: hide the toolbar and give the module panel back."""
        self.active = False
        self._restorePanel()
        if self.toolBar is not None:
            self.toolBar.hide()

    def _ensureColorIcons(self):
        if self._colorIconsReady:
            return
        missing = False
        for button, colorName, rgbFunction in self._colorButtons:
            function = rgbFunction or colorNodeRgbFunction(findColorNode(colorName))
            if function is None:
                missing = True
                continue
            try:
                button.setIcon(qt.QIcon(colorBarPixmap(function)))
            except Exception:
                logging.debug(f"Epona: could not draw the {colorName} color swatch", exc_info=True)
                missing = True
        self._colorIconsReady = not missing

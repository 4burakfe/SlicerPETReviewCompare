"""
Application-wide keyboard shortcuts shared by the Epona modules (F5-F9 windowing, Insert = ROI at the cursor).
The ROI key (Insert by default) can be changed by the user: setRoiKey() moves the ROI handlers of both modules to
the new key and remembers it in the application settings (Epona/RoiShortcut).

Qt fires neither of two identical application shortcuts ("ambiguous"), so Epona and the compare module cannot each
create their own. Each key gets ONE shortcut here; modules register a handler for it, optionally with a predicate
(e.g. "the mouse is over one of my views"). On a key press the first handler whose predicate accepts is called;
handlers without a predicate are the fallback.

The registry is kept on slicer.modules, so it survives reloading this file (a module's Reload button).
"""

import logging

import qt
import slicer

_REGISTRY_ATTRIBUTE = "_eponaShortcutRegistry"
_ROI_ATTRIBUTE = "_eponaRoiShortcut"

SETTINGS_ROI_KEY = "Epona/RoiShortcut"     # application setting (qt.QSettings), shared by both modules
DEFAULT_ROI_KEY = "Insert"
# Keys Slicer or the modules need for themselves (cancel placing, slice browsing, deleting a markup point)
FORBIDDEN_ROI_KEYS = ("Esc", "Up", "Down", "Left", "Right", "PgUp", "PgDown", "Del", "Backspace", "Return",
                      "Enter", "Tab", "Space")


def _registry():
    registry = getattr(slicer.modules, _REGISTRY_ATTRIBUTE, None)
    if not isinstance(registry, dict):
        registry = {}
        try:
            setattr(slicer.modules, _REGISTRY_ATTRIBUTE, registry)
        except Exception:
            logging.debug("Epona: shortcut registry kept in this module only", exc_info=True)
    return registry


def registerShortcut(owner, keyCode, callback, predicate=None):
    """
    Call callback() when keyCode is pressed anywhere in the application (also in a floating Monitor 2 window).
    owner: module name; registering again for the same owner and key replaces its handler (e.g. after a reload).
    predicate(): True when this handler should take the key press (handlers with a predicate are asked first).
    """
    registry = _registry()
    entry = registry.get(keyCode)
    if entry is None:
        mainWindow = slicer.util.mainWindow()
        if mainWindow is None:
            return
        shortcut = qt.QShortcut(qt.QKeySequence(keyCode), mainWindow)
        shortcut.setContext(qt.Qt.ApplicationShortcut)
        shortcut.connect("activated()", lambda keyCode=keyCode: dispatchShortcut(keyCode))
        entry = {"shortcut": shortcut, "handlers": {}}
        registry[keyCode] = entry
    entry["handlers"][owner] = (callback, predicate)


def _removeHandler(owner, keyCode):
    """Remove one module's handler of one key; a key without handlers loses its shortcut."""
    registry = _registry()
    entry = registry.get(keyCode)
    if entry is None:
        return
    entry["handlers"].pop(owner, None)
    if not entry["handlers"]:
        shortcut = entry["shortcut"]
        try:
            shortcut.setEnabled(False)
            shortcut.setParent(None)
        except Exception:
            pass
        del registry[keyCode]


def unregisterShortcuts(owner):
    """Remove every handler of a module (its ROI key handler and ROI key listener too)."""
    roi = _roiState()
    roi["handlers"].pop(owner, None)
    roi["listeners"].pop(owner, None)
    for keyCode in list(_registry()):
        _removeHandler(owner, keyCode)


# --- The ROI key (user-selectable) ------------------------------------------------------------------------------

def _roiState():
    state = getattr(slicer.modules, _ROI_ATTRIBUTE, None)
    if not isinstance(state, dict):
        state = {"key": None, "handlers": {}, "listeners": {}}
        try:
            setattr(slicer.modules, _ROI_ATTRIBUTE, state)
        except Exception:
            logging.debug("Epona: ROI key state kept in this module only", exc_info=True)
    return state


def keyText(key):
    """Portable text of a key code (qt.Qt.Key_F5) or key sequence text ("Ctrl+R"), e.g. "F5", "Ctrl+R"."""
    try:
        return str(qt.QKeySequence(key).toString())
    except Exception:
        return str(key)


def firstKeyCombination(text):
    """A key sequence edit can record several presses ("Ctrl+R, I"); only the first one is used."""
    text = str(text or "").strip()
    if text.startswith(","):            # the comma key itself
        return ","
    return text.split(", ")[0].strip()


def checkRoiKey(text, usedKeys):
    """
    None if text can be the ROI key, else the reason why not (shown to the user).
    usedKeys: {key text: who uses it} of keys taken already (the modules' F5-F9, Slicer menu shortcuts).
    """
    text = firstKeyCombination(text)
    if not text:
        return "No key was pressed."
    lastKey = text.split("+")[-1] if text != "+" else "+"
    if text in FORBIDDEN_ROI_KEYS or lastKey in ("Shift", "Ctrl", "Alt", "Meta"):
        return f"'{text}' cannot be used for placing ROIs (Slicer needs it for itself)."
    if text in usedKeys:
        return f"'{text}' is already used by {usedKeys[text]}."
    return None


def roiKey():
    """The current ROI key as key sequence text (default Insert)."""
    state = _roiState()
    if not state["key"]:
        try:
            saved = qt.QSettings().value(SETTINGS_ROI_KEY)
        except Exception:
            saved = None
        state["key"] = firstKeyCombination(saved) if saved else DEFAULT_ROI_KEY
    return state["key"]


def usedKeys():
    """{key text: who uses it} of the modules' other shortcuts and of Slicer's menu / toolbar actions."""
    used = {}
    roiText = roiKey()
    mainWindow = slicer.util.mainWindow()
    if mainWindow is not None:
        try:
            for action in mainWindow.findChildren(qt.QAction):
                for sequence in action.shortcuts():
                    text = keyText(sequence)
                    if text:
                        name = str(action.text).replace("&", "") or str(action.objectName)
                        used.setdefault(text, f"Slicer ('{name}')")
        except Exception:
            logging.debug("Epona: could not list Slicer's shortcuts", exc_info=True)
    for keyCode in _registry():
        text = keyText(keyCode)
        if text and text != roiText:
            used[text] = "the Epona modules (window presets)"
    return used


def registerRoiShortcut(owner, callback, predicate=None, onKeyChanged=None):
    """
    Like registerShortcut, for the ROI key: the handler follows the key when the user changes it.
    onKeyChanged(keyText) is called after a change (labels, tooltips).
    """
    state = _roiState()
    state["handlers"][owner] = (callback, predicate)
    if onKeyChanged is not None:
        state["listeners"][owner] = onKeyChanged
    registerShortcut(owner, roiKey(), callback, predicate)


def setRoiKey(text):
    """
    Use another key for placing ROIs (both modules) and remember it. Returns None on success, else the reason
    why the key cannot be used (nothing is changed then).
    """
    text = firstKeyCombination(keyText(firstKeyCombination(text)) if text else "")
    oldKey = roiKey()
    if text == oldKey:
        return None
    problem = checkRoiKey(text, usedKeys())
    if problem:
        return problem
    state = _roiState()
    for owner, (callback, predicate) in list(state["handlers"].items()):
        _removeHandler(owner, oldKey)
        registerShortcut(owner, text, callback, predicate)
    state["key"] = text
    try:
        qt.QSettings().setValue(SETTINGS_ROI_KEY, text)
    except Exception:
        logging.exception("Epona: could not save the ROI key")
    for owner, listener in list(state["listeners"].items()):
        try:
            listener(text)
        except Exception:
            logging.exception(f"Epona: ROI key listener of {owner} failed")
    return None


def resetRoiKey():
    return setRoiKey(DEFAULT_ROI_KEY)


def chooseHandler(handlers):
    """The callback to run for a key press: first accepting predicate (by owner name), else a predicate-less one."""
    for owner in sorted(handlers):
        callback, predicate = handlers[owner]
        if predicate is not None:
            try:
                if predicate():
                    return callback
            except Exception:
                logging.exception(f"Epona: shortcut predicate of {owner} failed")
    for owner in sorted(handlers):
        callback, predicate = handlers[owner]
        if predicate is None:
            return callback
    return None


def dispatchShortcut(keyCode):
    entry = _registry().get(keyCode)
    callback = chooseHandler(entry["handlers"]) if entry is not None else None
    if callback is None:
        return
    try:
        callback()
    except Exception:
        logging.exception("Epona: keyboard shortcut failed")


def viewUnderCursor():
    """("slice", view name) or ("threeD", view name) of the view under the mouse, in any window; else None."""
    widget = qt.QApplication.widgetAt(qt.QCursor.pos())
    while widget is not None:
        try:
            if widget.inherits("qMRMLThreeDWidget"):
                viewNode = widget.mrmlViewNode()
                return ("threeD", viewNode.GetLayoutName() if viewNode is not None else None)
            if widget.inherits("qMRMLSliceWidget"):
                sliceNode = widget.mrmlSliceNode()
                return ("slice", sliceNode.GetLayoutName() if sliceNode is not None else None)
        except Exception:
            return None
        widget = widget.parentWidget()
    return None


class RoiKeyEditor:
    """
    Panel row to pick the ROI key: click the field and press the new key (or key combination); Reset = Insert.
    The same setting is used by both modules; refresh() shows a key changed in the other module.
    """

    def __init__(self):
        self.widget = qt.QWidget()
        layout = qt.QHBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 0)
        tip = ("Key that drops an ROI at the mouse cursor (used by Epona and Epona Compare).\n"
               "Click here and press the new key, e.g. F2, F12, Home, Pause or Ctrl+R.\n"
               "Keys already used by Slicer or by the F5-F9 window presets are refused.")
        self.edit = qt.QKeySequenceEdit() if hasattr(qt, "QKeySequenceEdit") else qt.QLineEdit()
        self._isSequenceEdit = hasattr(qt, "QKeySequenceEdit") and isinstance(self.edit, qt.QKeySequenceEdit)
        self.edit.setToolTip(tip if self._isSequenceEdit else tip.replace("press the new key", "type the key name"))
        self.edit.setMaximumWidth(160)
        self.resetButton = qt.QPushButton("Reset")
        self.resetButton.setToolTip(f"Use {DEFAULT_ROI_KEY} again.")
        layout.addWidget(self.edit)
        layout.addWidget(self.resetButton)
        layout.addStretch(1)
        self.edit.connect("editingFinished()", self._onEdited)
        self.resetButton.connect("clicked()", self._onReset)
        self.refresh()

    def refresh(self, text=None):
        text = text or roiKey()
        wasBlocked = self.edit.blockSignals(True)
        try:
            if self._isSequenceEdit:
                self.edit.setKeySequence(qt.QKeySequence(text))
            else:
                self.edit.text = text
        finally:
            self.edit.blockSignals(wasBlocked)

    def _currentText(self):
        if self._isSequenceEdit:
            return str(self.edit.keySequence().toString())
        return str(self.edit.text)

    def _apply(self, text):
        problem = setRoiKey(text)
        if problem:
            slicer.util.warningDisplay(f"The ROI key was not changed: {problem}", windowTitle="Epona")
        else:
            slicer.util.showStatusMessage(f"Epona: ROIs are now placed with {roiKey()}.", 3000)
        self.refresh()

    def _onEdited(self):
        text = self._currentText()
        if not text.strip():          # field cleared: keep the current key
            self.refresh()
            return
        self._apply(text)

    def _onReset(self):
        self._apply(DEFAULT_ROI_KEY)

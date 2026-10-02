"""
Application-wide keyboard shortcuts shared by the Epona modules (F5-F9 windowing, Insert = ROI at the cursor).

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


def unregisterShortcuts(owner):
    """Remove every handler of a module; a key without handlers loses its shortcut."""
    registry = _registry()
    for keyCode in list(registry):
        entry = registry[keyCode]
        entry["handlers"].pop(owner, None)
        if not entry["handlers"]:
            shortcut = entry["shortcut"]
            try:
                shortcut.setEnabled(False)
                shortcut.setParent(None)
            except Exception:
                pass
            del registry[keyCode]


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

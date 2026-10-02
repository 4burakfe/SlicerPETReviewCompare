"""
Shared code of the Epona modules (Epona - SPECT/PET Review and Epona - SPECT/PET Compare).

Modules, lowest first (each imports only from the ones before it):
  scene     scene loading / closing state, run code once the scene has settled
  shortcuts one application shortcut per key, handed to the module whose view is under the mouse
  layouts   layout IDs and XML, slice view roles, screens, field of view
  values    SUV / Bq/mL / counts detection, window presets and window math
  studyinfo imaging date / time of a volume ("name (2026-03-14 10:32)")
  display   window / level links between volumes, slice views kept at the same position, Hot Iron colors,
            color table lookup
  roi       spherical ROI statistics, thresholds, labels, table / TSV export
  roiset    a set of spherical ROIs with its own nodes (Epona's SUV ROIs, one per time point in compare)
  filters   AI post-processing filters: model catalog and downloads, inference
  overlays  window info in the slice views, ROI segments and values on the MIP
  registration  time point 1 -> 2 translation (initial alignment + mutual information), transform handling
  compareviews  reading views of the compare module: twins, MIPs, view contents, layouts, Monitor 2
  toolbar   the module toolbar (top of the window while a module is open): picture buttons, panel hiding
  tests     unit tests of the pure helpers (run by the module's self test, or: python -m EponaLib.tests)
"""

SUBMODULES = ("scene", "shortcuts", "layouts", "values", "studyinfo", "display", "roi", "roiset", "filters",
              "overlays", "registration", "compareviews", "toolbar", "tests")


def reloadAll():
    """
    Reload every loaded EponaLib module in dependency order. Slicer's Reload button reloads only the module
    file itself, so the module calls this first to pick up edits in the shared code too.
    """
    import importlib
    import sys
    display = sys.modules.get(f"{__name__}.display")
    if display is not None:
        display.unbindWindowLevelSync()  # its observers belong to the old module; the next "Go" binds again
    for name in SUBMODULES:
        module = sys.modules.get(f"{__name__}.{name}")
        if module is not None:
            importlib.reload(module)

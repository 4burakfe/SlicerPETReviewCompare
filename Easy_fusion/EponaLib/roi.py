"""Spherical SUV ROIs: statistics and thresholded segment of a sphere, radius handles, labels, table rows and TSV export (pure helpers), plus the unsmoothed closed-surface helper."""

import re
import math
import time
import random
import colorsys

import numpy as np
ROI_NUMBER_ATTRIBUTE = "EasyFusion.RoiNumber_"      # + control point ID
ROI_NEXT_NUMBER_ATTRIBUTE = "EasyFusion.RoiNextNumber"
DEFAULT_ROI_RADIUS_MM = 15.0
ROI_SEGMENT_ID_PREFIX = "EFROI_"
ROI_COLOR_ATTRIBUTE = "EasyFusion.RoiColor_"       # + control point ID, "r g b" (0..1)
ROI_SEGMENT_OUTLINE_PX = 2
THRESHOLD_RELATIVE = "relative"   # % of the ROI's Max
THRESHOLD_ABSOLUTE = "absolute"   # fixed SUV value
DEFAULT_RELATIVE_THRESHOLD = 40.0
DEFAULT_ABSOLUTE_THRESHOLD = 2.5
# Each ROI keeps its own threshold; the panel's threshold is the default given to new ROIs
ROI_THRESHOLD_ATTRIBUTE = "EasyFusion.RoiThreshold_"  # + control point ID, e.g. "relative 40" / "absolute 2.5"
# Who created an ROI (for future AI lesion detection): stored per ROI, saved with the scene
ROI_ORIGIN_ATTRIBUTE = "EasyFusion.RoiOrigin_"        # + control point ID
ROI_ORIGIN_USER = "user"
ROI_ORIGIN_AI = "ai"
# Values that can be shown next to each ROI in the slice views and on the MIP (key, checkbox text).
# A display preference of the user, so it is kept in the application settings, not in the scene.
ROI_LABEL_FIELDS = (("name", "Name"), ("max", "Max"), ("mean", "Mean"), ("mtv", "MTV"), ("tlg", "TLG"),
                    ("radius", "Radius"), ("threshold", "Threshold"))
DEFAULT_ROI_LABEL_FIELDS = ("name", "max", "mean")

# ROI appearance
ROI_SPHERE_OUTLINE_PX = 1
ROI_SPHERE_COLOR_ARRAY = "EasyFusionRoiColor"
ROI_HANDLE_GLYPH_SCALE = 1.4
ROI_LABEL_TEXT_SCALE = 2.5

# ROIs projected onto the MIP (3D view). The MIP is fully opaque, so segments and labels are drawn in an
# overlay render layer that shares the 3D camera and is always on top (like a PET workstation MIP overlay).
# 3D segment surfaces follow the voxels exactly (no smoothing), so they match the measured MTV
SEGMENT_SURFACE_SMOOTHING = "0.0"
LABEL_ANCHOR_DIRECTIONS = [(-1.0, 1.0, 0.0), (-1.0, 0.0, 1.0), (0.0, -1.0, 1.0)]

# Radius handles sit on each sphere along ±R, ±A, ±S, so every slice view through the
# ROI center shows four of them around the circle.
HANDLE_DIRECTIONS = [(1.0, 0.0, 0.0), (-1.0, 0.0, 0.0),
                     (0.0, 1.0, 0.0), (0.0, -1.0, 0.0),
                     (0.0, 0.0, 1.0), (0.0, 0.0, -1.0)]


def sphereStatisticsFromArray(voxels, ijkToRas, centerRas, radiusMm,
                              thresholdMode=THRESHOLD_RELATIVE, thresholdValue=DEFAULT_RELATIVE_THRESHOLD):
    """
    Statistics of the voxels whose centers lie inside a sphere, plus a thresholded segment inside it.

    voxels:    numpy array indexed [k, j, i] (as returned by slicer.util.arrayFromVolume)
    ijkToRas:  4x4 matrix (numpy) mapping voxel indices to RAS (mm)
    centerRas: sphere center in the volume's RAS coordinate system
    radiusMm:  sphere radius in mm
    thresholdMode / thresholdValue: THRESHOLD_RELATIVE (% of Max in the sphere) or THRESHOLD_ABSOLUTE (SUV)

    Returns None if the sphere does not touch the volume, otherwise a dict with
      max, mean, voxels, volumeMl           (whole sphere)
      threshold, segVoxels, segMean, mtvMl, tlg  (segment; segMean is None if the segment is empty)
      extent (i0, i1, j0, j1, k0, k1) and segMask (bool array [k, j, i] over that extent)
    If the sphere is smaller than a voxel, the voxel containing the center is used.
    """
    voxels = np.asarray(voxels)
    if voxels.ndim > 3:
        voxels = voxels[..., 0]
    matrix = np.asarray(ijkToRas, dtype=float)
    linear, offset = matrix[:3, :3], matrix[:3, 3]
    center = np.asarray(centerRas, dtype=float)
    radius = float(radiusMm)
    dims = np.array(voxels.shape[::-1])  # (i, j, k)

    centerIjk = np.linalg.solve(linear, center - offset)
    # A sphere maps to an axis-aligned ellipsoid in IJK space (orthonormal directions),
    # so a per-axis reach of radius / spacing bounds it exactly.
    voxelSize = np.linalg.norm(linear, axis=0)
    reach = np.ceil(radius / voxelSize).astype(int) + 1
    lo = np.maximum(np.floor(centerIjk).astype(int) - reach, 0)
    hi = np.minimum(np.ceil(centerIjk).astype(int) + reach, dims - 1)

    inside = None
    if np.all(lo <= hi):
        ni, nj, nk = (hi - lo + 1)
        jj, ii = np.meshgrid(np.arange(lo[1], hi[1] + 1), np.arange(lo[0], hi[0] + 1), indexing="ij")
        inPlane = linear[:, 0:1] * ii.ravel() + linear[:, 1:2] * jj.ravel() + offset[:, None]
        radius2 = radius * radius
        inside = np.zeros((nk, nj, ni), dtype=bool)
        for kIndex in range(nk):  # slab by slab keeps memory bounded for big spheres
            delta = inPlane + linear[:, 2:3] * (lo[2] + kIndex) - center[:, None]
            inside[kIndex] = (np.einsum("ij,ij->j", delta, delta) <= radius2).reshape(nj, ni)
        if not inside.any():
            inside = None

    if inside is None:
        nearest = np.round(centerIjk).astype(int)
        if not (np.all(nearest >= 0) and np.all(nearest < dims)):
            return None
        lo = hi = nearest
        inside = np.ones((1, 1, 1), dtype=bool)

    sub = voxels[lo[2]:hi[2] + 1, lo[1]:hi[1] + 1, lo[0]:hi[0] + 1]
    values = sub[inside]
    voxelVolumeMl = abs(np.linalg.det(linear)) / 1000.0
    suvMax = float(values.max())

    if thresholdMode == THRESHOLD_ABSOLUTE:
        threshold = float(thresholdValue)
    else:
        threshold = suvMax * float(thresholdValue) / 100.0
    segMask = inside & (sub >= threshold)
    segValues = sub[segMask]
    segVoxels = int(segValues.size)
    segMean = float(segValues.mean()) if segVoxels else None
    mtvMl = segVoxels * voxelVolumeMl

    return {
        "max": suvMax,
        "mean": float(values.mean()),
        "voxels": int(values.size),
        "volumeMl": float(values.size * voxelVolumeMl),
        "threshold": threshold,
        "segVoxels": segVoxels,
        "segMean": segMean,
        "mtvMl": float(mtvMl),
        "tlg": float(segMean * mtvMl) if segVoxels else 0.0,
        "extent": (int(lo[0]), int(hi[0]), int(lo[1]), int(hi[1]), int(lo[2]), int(hi[2])),
        "segMask": segMask,
    }


def formatRoiThreshold(mode, value):
    """Stored form of an ROI's threshold: "relative 40" / "absolute 2.5"."""
    mode = mode if mode in (THRESHOLD_RELATIVE, THRESHOLD_ABSOLUTE) else THRESHOLD_RELATIVE
    return f"{mode} {float(value):g}"


def parseRoiThreshold(text):
    """(mode, value) from formatRoiThreshold's text, or None if the text is not a valid threshold."""
    parts = (text or "").split()
    if len(parts) != 2 or parts[0] not in (THRESHOLD_RELATIVE, THRESHOLD_ABSOLUTE):
        return None
    try:
        value = float(parts[1])
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return parts[0], value


def describeRoiThreshold(mode, value, unit=""):
    """Short text for the table and the view labels: "40%" / "2.5 SUV" (just "2.5" when the unit is not known)."""
    if mode != THRESHOLD_ABSOLUTE:
        return f"{float(value):g}%"
    return f"{float(value):g} {unit}" if unit else f"{float(value):g}"


def parseRoiLabelFields(text):
    """Label fields from their stored form ("name,max,mean"); None (never set) gives the defaults."""
    if text is None:
        return tuple(DEFAULT_ROI_LABEL_FIELDS)
    wanted = {part.strip() for part in str(text).split(",")}
    return tuple(key for key, _ in ROI_LABEL_FIELDS if key in wanted)


def formatRoiLabel(name, stats, hasPet, fields=DEFAULT_ROI_LABEL_FIELDS, radius=None, threshold=None, unit=""):
    """
    Text shown next to the ROI in the views: the chosen fields, one per line ("" when none is chosen).
    Mean is the mean of the thresholded segment. threshold: (mode, value) of this ROI.
    unit: unit of the PET values ("SUV", ...; "" when not known), used for an absolute threshold.
    """
    fields = set(fields)
    lines = [name] if "name" in fields else []
    if stats is None:
        lines.append("(outside PET)" if hasPet else "(no PET)")
    else:
        segMean = stats.get("segMean")
        if "max" in fields:
            lines.append(f"Max {stats['max']:.2f}")
        if "mean" in fields:
            lines.append(f"Mean {segMean:.2f}" if segMean is not None else "Mean -")
        if "mtv" in fields:
            lines.append(f"MTV {stats.get('mtvMl', 0.0):.2f} mL")
        if "tlg" in fields:
            lines.append(f"TLG {stats.get('tlg', 0.0):.2f}")
    if "radius" in fields and radius is not None:
        lines.append(f"r {float(radius):.1f} mm")
    if "threshold" in fields and threshold is not None:
        lines.append(f"Thr {describeRoiThreshold(*threshold, unit=unit)}")
    return "\n".join(lines)


def formatRoiTableRow(name, radius, stats, threshold=None, unit=""):
    """ROI | r (mm) | Thr. | Max | Mean | MTV (mL) | TLG   (Mean = mean of the thresholded segment)"""
    values = [name, f"{radius:.1f}", describeRoiThreshold(*threshold, unit=unit) if threshold else "-",
              "-", "-", "-", "-"]
    if stats is not None:
        values[3] = f"{stats['max']:.2f}"
        if stats.get("segMean") is not None:
            values[4] = f"{stats['segMean']:.2f}"
        values[5] = f"{stats.get('mtvMl', 0.0):.2f}"
        values[6] = f"{stats.get('tlg', 0.0):.2f}"
    return values


ROI_TSV_COLUMNS = [
    "ROI", "Origin", "Center R (mm)", "Center A (mm)", "Center S (mm)", "Radius (mm)",
    "Threshold mode", "Threshold setting", "Threshold (SUV)",
    "SUVmax", "SUVmean sphere", "Sphere volume (mL)",
    "SUVmean segment", "MTV (mL)", "TLG", "Segment voxels",
    "PET volume", "PET filter", "Value unit",
]
# Same columns for a volume that is not known to be in SUV: no SUV in the names (the unit is in "Value unit")
ROI_TSV_NEUTRAL_NAMES = {"Threshold (SUV)": "Threshold (value)", "SUVmax": "Max", "SUVmean sphere": "Mean sphere",
                         "SUVmean segment": "Mean segment"}


def _tsvCell(value, digits=4):
    """One TSV cell: numbers at fixed precision, None / NaN empty, tabs and line breaks removed from text."""
    if value is None:
        return ""
    if isinstance(value, (bool, np.bool_)):
        return str(bool(value))
    if isinstance(value, (int, np.integer)):
        return str(int(value))
    if isinstance(value, (float, np.floating)):
        return f"{float(value):.{digits}f}" if math.isfinite(value) else ""
    return re.sub(r"[\t\r\n]+", " ", str(value)).strip()


def formatRoiTableTsv(rows, centers=None, petName="", petFilter="", unit=""):
    """
    ROI table as tab-separated text (header + one line per ROI), at full precision rather than the
    rounded values shown in the panel.
    rows:    (pointID, name, radius, stats, threshold, origin) as in the ROI table
    centers: {pointID: (R, A, S)} ROI centers in world coordinates (mm)
    unit:    unit of the PET values ("SUV", "Bq/mL", "counts"; "" when not known). The SUV column names are
             used only for SUV; the "Value unit" column says "unknown" when the unit is not known.
    """
    centers = centers or {}
    columns = ROI_TSV_COLUMNS if unit == "SUV" else [ROI_TSV_NEUTRAL_NAMES.get(c, c) for c in ROI_TSV_COLUMNS]
    lines = ["\t".join(columns)]
    for pointID, name, radius, stats, threshold, origin in rows:
        center = centers.get(pointID) or (None, None, None)
        mode, setting = threshold if threshold else (None, None)
        stats = stats or {}
        values = [
            name, origin or ROI_ORIGIN_USER,
            _tsvCell(center[0], 2), _tsvCell(center[1], 2), _tsvCell(center[2], 2),
            _tsvCell(radius, 2),
            mode or "",
            (describeRoiThreshold(mode, setting, unit=unit) if mode is not None else ""),
            _tsvCell(stats.get("threshold")),
            _tsvCell(stats.get("max")), _tsvCell(stats.get("mean")), _tsvCell(stats.get("volumeMl")),
            _tsvCell(stats.get("segMean")), _tsvCell(stats.get("mtvMl")),
            _tsvCell(stats.get("tlg") if stats else None), _tsvCell(stats.get("segVoxels")),
            petName or "", petFilter or "", unit or "unknown",
        ]
        lines.append("\t".join(_tsvCell(value) for value in values))
    return "\n".join(lines) + "\n"


def defaultRoiExportFileName(petName, timestamp=None, isSuv=True):
    """e.g. "PT_Patient1_SUV_ROIs_20260920-143000.tsv" ("..._ROIs_..." when not SUV; unsafe characters replaced)."""
    stamp = timestamp or time.strftime("%Y%m%d-%H%M%S")
    base = re.sub(r"[^\w.\-]+", "_", petName or "").strip("._")
    return f"{base + '_' if base else ''}{'SUV_' if isSuv else ''}ROIs_{stamp}.tsv"


def randomRoiColor(rng=random):
    """Random, clearly visible color: any hue, strong saturation and brightness (reads on black PET and white MIP)."""
    hue = rng.random()
    saturation = 0.75 + 0.25 * rng.random()
    value = 0.80 + 0.20 * rng.random()
    return colorsys.hsv_to_rgb(hue, saturation, value)


def parseRoiColor(text):
    """(r, g, b) from "r g b", or None if the text is not a valid color."""
    try:
        values = tuple(float(v) for v in (text or "").split())
    except ValueError:
        return None
    if len(values) != 3 or not all(0.0 <= v <= 1.0 for v in values):
        return None
    return values


def _distance(a, b):
    return float(np.linalg.norm(np.asarray(a, dtype=float) - np.asarray(b, dtype=float)))


def radiusHandlePosition(center, radius, axis):
    direction = HANDLE_DIRECTIONS[axis]
    return [center[0] + direction[0] * radius,
            center[1] + direction[1] * radius,
            center[2] + direction[2] * radius]


def parseHandleDescription(description, count=len(HANDLE_DIRECTIONS)):
    """Handle / label anchor points store '<roi control point ID>:<index>' in their description."""
    if not description or ":" not in description:
        return None
    roiID, _, indexText = description.rpartition(":")
    if not roiID or not indexText.isdigit():
        return None
    index = int(indexText)
    return (roiID, index) if 0 <= index < count else None


def labelAnchorPosition(center, radius, plane):
    """Label anchor one radius away from the center, on the upper-right diagonal of that plane's view."""
    direction = LABEL_ANCHOR_DIRECTIONS[plane]
    scale = float(radius) / np.sqrt(2.0)
    return [center[i] + direction[i] * scale for i in range(3)]


def resolveHandleDrag(center, radius, lastGeometry, handles, activeHandleID, tolerance=1e-3):
    """
    Decide whether a radius handle was dragged since the last sync.

    center, radius: current ROI center and stored radius
    lastGeometry:   (center, radius) from the previous sync, or None for a new / reloaded ROI
    handles:        list of (handleID, position, lastPosition or None) for this ROI
    activeHandleID: handle currently grabbed by the mouse, or None

    Returns (radius, draggedHandleID). The returned radius is not clamped.
    If the ROI center moved, handles simply follow it and the radius is unchanged.
    """
    if lastGeometry is None or _distance(lastGeometry[0], center) > tolerance:
        return radius, None
    dragged = None
    for handleID, position, lastPosition in handles:
        if activeHandleID is not None and handleID == activeHandleID:
            dragged = (handleID, position)
            break
        if dragged is None and lastPosition is not None and _distance(lastPosition, position) > tolerance:
            dragged = (handleID, position)
    if dragged is None:
        return radius, None
    return _distance(dragged[1], center), dragged[0]


def ensureUnsmoothedClosedSurface(segmentationNode):
    """Closed surface (3D) without smoothing; rebuilt once if it was made with other settings (older scenes)."""
    segmentation = segmentationNode.GetSegmentation()
    smoothingChanged = segmentation.GetConversionParameter("Smoothing factor") != SEGMENT_SURFACE_SMOOTHING
    if smoothingChanged:
        segmentation.SetConversionParameter("Smoothing factor", SEGMENT_SURFACE_SMOOTHING)
    if not hasattr(segmentationNode, "CreateClosedSurfaceRepresentation"):
        return
    if smoothingChanged and hasattr(segmentationNode, "RemoveClosedSurfaceRepresentation"):
        segmentationNode.RemoveClosedSurfaceRepresentation()
    segmentationNode.CreateClosedSurfaceRepresentation()

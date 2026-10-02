"""What SPECT/PET voxel values are (SUV, Bq/mL, counts or unknown) and the window presets / window math built on it (SUV presets, % of maximum, MRI percentiles, CT detection, window info text)."""

import re
import logging
import collections

import numpy as np
import slicer

from .layouts import (
    SLICE_VIEW_ROLES,
)

# ---------------------------------------------------------------------------
# Window / level presets
# ---------------------------------------------------------------------------

# (button text, window, level in Hounsfield units, shortcut key over CT-only views)
CT_WINDOW_PRESETS = [
    ("CT: Abdomen", 400, 50, "F5"),
    ("CT: Head", 80, 40, "F6"),
    ("CT: Lungs", 1500, -600, "F7"),
    ("CT: Bones", 1800, 400, "F8"),
]
# Fixed SUV ranges starting at 0 (button text, upper SUV, shortcut key over fusion / PET-only / 3D views)
PET_SUV_PRESETS = [
    ("0–5", 5.0, "F5"),
    ("0–7", 7.0, "F6"),
    ("0–10", 10.0, "F7"),
    ("0–15", 15.0, "F8"),
    ("0–25", 25.0, "F9"),
]
WINDOW_SHORTCUT_KEYS = ["F5", "F6", "F7", "F8", "F9"]
# First window of a SPECT/PET: SUV 0–10 only when the volume is known to be in SUV, else 0–100 % of its maximum
INITIAL_SUV_UPPER = 10.0
# What the SPECT/PET voxel values are (classifyPetValues); units are shown only when this is known
VALUE_KIND_SUV = "SUV"
VALUE_KIND_BQML = "BQML"
VALUE_KIND_COUNTS = "COUNTS"
VALUE_KIND_UNITS = {VALUE_KIND_SUV: "SUV", VALUE_KIND_BQML: "Bq/mL", VALUE_KIND_COUNTS: "counts"}
VALUE_KIND_ATTRIBUTE = "EasyFusion.ValueKind"  # on filtered volumes: the kind of the volume they came from
VALUE_KIND_UNKNOWN = "unknown"                 # ... stored when that kind was not known
VALUE_MODALITY_ATTRIBUTE = "EasyFusion.ValueModality"  # ... and its modality (PET / SPECT), if known
# DICOM Units (0054,1001) -> kind. GML = g/ml, used for SUVbw images; CPS etc. are not named on purpose
DICOM_UNITS_KINDS = {"GML": VALUE_KIND_SUV, "BQML": VALUE_KIND_BQML,
                     "CNTS": VALUE_KIND_COUNTS, "PROPCNTS": VALUE_KIND_COUNTS}
# Value checks on the 99.9th percentile of the positive voxels. SUV images rarely exceed ~100 there;
# Bq/mL PET is in the thousands. Metadata that disagrees this much with the values is not believed.
SUV_PLAUSIBLE_P999_MIN = 0.3
SUV_LIKELY_P999_MAX = 100.0     # upper limit when SUV is recognized from the values alone
SUV_PLAUSIBLE_P999_MAX = 300.0  # above this, an "SUV" label is not believed
BQML_PLAUSIBLE_P999_MIN = 50.0
SUV_LIKELY_FRACTIONAL_SHARE = 0.5  # share of positive voxels with decimals needed to call values SUV
PET_VALUE_SAMPLE_SIZE = 1000000    # voxels sampled for the value checks
# SPECT (or any uncalibrated image): 0 .. percent of the maximum count in the volume
SPECT_PERCENT_OF_MAX_PRESETS = [10, 25, 50, 75, 100]
# MRI (arbitrary intensity units, e.g. PET/MRI): window between two percentiles of the tissue voxels
# (button text, lower percentile, upper percentile, shortcut key over CT/MRI views when the volume is an MRI)
MRI_PERCENTILE_PRESETS = [
    ("Standard", 1.0, 99.0, "F5"),
    ("Wide", 0.5, 99.5, "F6"),
    ("Contrast", 5.0, 95.0, "F7"),
    ("Bright", 0.0, 90.0, "F8"),
]
# Voxels below this fraction of the 99.5th percentile count as air / background, not tissue
MRI_BACKGROUND_FRACTION = 0.05
# At most this many voxels are sampled for the percentiles (fast on large volumes)
MRI_PRESET_SAMPLE_SIZE = 2000000
# A CT has air around -1000 HU; an MRI (almost) never goes this far below 0
CT_MINIMUM_BELOW = -500.0


# ---------------------------------------------------------------------------
# Pure helpers (no Slicer dependency, easy to test)
# ---------------------------------------------------------------------------

def windowPresetForView(viewKind, sliceViewName, key):
    """
    Preset applied by a windowing shortcut key ("F5".."F9").
    viewKind: "slice" or "threeD" for the view under the mouse, None when the mouse is not over a view.
    CT-only slice views use the CT presets; fusion, PET-only and 3D views (and slice views that are not
    EasyFusion views) use the SUV presets.
    Returns ("ct", text, window, level), ("pet", text, window, level), or None if the key does nothing there.
    """
    if viewKind == "slice" and SLICE_VIEW_ROLES.get(sliceViewName, (None, "fusion"))[1] == "ct":
        for text, window, level, presetKey in CT_WINDOW_PRESETS:
            if presetKey == key:
                return ("ct", text, window, level)
        return None
    if viewKind in ("slice", "threeD"):
        for text, upper, presetKey in PET_SUV_PRESETS:
            if presetKey == key:
                return ("pet", text, upper, upper / 2.0)
    return None


def percentOfMaximum(maximum, percent):
    """Upper window limit for the SPECT presets, or None when the image has no positive values."""
    maximum, percent = float(maximum), float(percent)
    if not np.isfinite(maximum) or maximum <= 0 or percent <= 0:
        return None
    return maximum * percent / 100.0


def tissueSample(values, backgroundFraction=MRI_BACKGROUND_FRACTION):
    """
    Voxel values that belong to the imaged body: padding (the minimum value) and air (below
    backgroundFraction of the 99.5th percentile) are left out. Falls back to all finite values when
    almost nothing would be left.
    """
    values = np.asarray(values, dtype=float).ravel()
    values = values[np.isfinite(values)]
    if values.size == 0:
        return values
    minimum = values.min()
    upper = np.percentile(values, 99.5)
    cutoff = max(minimum, minimum + backgroundFraction * (upper - minimum))
    tissue = values[values > cutoff]
    return tissue if tissue.size >= max(10, values.size // 100) else values


def percentileWindow(values, lowerPercentile, upperPercentile):
    """(window, level) spanning two percentiles of values, or None when they are equal / there is no data."""
    values = np.asarray(values, dtype=float).ravel()
    values = values[np.isfinite(values)]
    if values.size == 0:
        return None
    lower, upper = np.percentile(values, [float(lowerPercentile), float(upperPercentile)])
    if not upper > lower:
        return None
    return float(upper - lower), float((upper + lower) / 2.0)


def looksLikeCT(scalarMinimum):
    """True for a CT (air / padding far below 0 HU), False for an MRI or other positive-valued image."""
    return scalarMinimum is not None and np.isfinite(scalarMinimum) and float(scalarMinimum) <= CT_MINIMUM_BELOW


def formatWindowNumber(value):
    """Compact number for the window info: 400, -600, 2.5, 0.35, 1.2e+04 -> 12000."""
    value = float(value)
    if not np.isfinite(value):
        return "?"
    if abs(value) < 1e-9:
        return "0"
    if abs(value) >= 100:
        return f"{value:.0f}"
    if abs(value) >= 10:
        return f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{value:.2f}".rstrip("0").rstrip(".")


def formatWindowInfoLine(kind, window, level, unit=""):
    """
    One line of the window info in the slice views.
    kind "CT" / "MRI": window and level ("CT  W 400  L 50").
    Anything else (PET, SPECT/PET, other volume names): the displayed range, which is how nuclear medicine
    windows are usually read ("PET  SUV 0–10", "SPECT/PET  0–1250").
    """
    if kind in ("CT", "MRI"):
        return f"{kind}  W {formatWindowNumber(window)}  L {formatWindowNumber(level)}"
    lower, upper = level - window / 2.0, level + window / 2.0
    unitText = f"{unit} " if unit else ""
    return f"{kind}  {unitText}{formatWindowNumber(lower)}–{formatWindowNumber(upper)}"


def voxelUnitLabel(volumeNode):
    """ "SUV" when the voxel value units say so, else the units' short text or "" (names are not trusted here). """
    if volumeNode is None:
        return ""
    texts = []
    try:
        units = volumeNode.GetVoxelValueUnits()
        if units is not None:
            texts = [units.GetCodeValue() or "", units.GetCodeMeaning() or ""]
    except Exception:
        pass
    if any("SUV" in text.upper() for text in texts):
        return "SUV"
    meaning = texts[1] if len(texts) > 1 else ""
    return meaning if meaning and len(meaning) <= 12 else ""


# ---------------------------------------------------------------------------
# What the SPECT/PET voxel values are: SUV, Bq/mL or counts, or unknown
# ---------------------------------------------------------------------------

PetValueInfo = collections.namedtuple("PetValueInfo", "kind modality reason")
PetValueInfo.__doc__ = """
kind:     VALUE_KIND_SUV / VALUE_KIND_BQML / VALUE_KIND_COUNTS, or None when it is not known for sure
modality: "PET", "SPECT" or None (for labels only)
reason:   short text saying what the decision is based on (shown in the panel)
"""


def valueKindUnit(kind):
    """Unit text shown next to values ("SUV", "Bq/mL", "counts"); "" when the kind is not known."""
    return VALUE_KIND_UNITS.get(kind, "")


def summarizePetValues(values):
    """
    Statistics of a voxel sample used to recognize SUV images: over the positive voxels, the share with a
    fractional part, the 99.9th percentile and the maximum. None when there are no positive voxels.
    """
    values = np.asarray(values, dtype=float).ravel()
    positive = values[np.isfinite(values) & (values > 0)]
    if positive.size == 0:
        return None
    fractional = np.abs(positive - np.round(positive)) > 1e-3
    return {
        "count": int(positive.size),
        "nonIntegerFraction": float(np.count_nonzero(fractional)) / positive.size,
        "p999": float(np.percentile(positive, 99.9)),
        "maximum": float(positive.max()),
    }


def _kindFromUnitTexts(texts):
    """Value kind named by voxel value unit / quantity texts (code values and meanings), or None."""
    joined = " ".join(str(text) for text in texts if text).upper()
    if not joined:
        return None
    if "SUV" in joined or "STANDARDIZED UPTAKE" in joined or "126400" in joined.split():
        return VALUE_KIND_SUV
    if "BQ/ML" in joined or "BECQUEREL" in joined:
        return VALUE_KIND_BQML
    if "{COUNTS}" in joined or re.search(r"(^|[^A-Z])(COUNTS|CNTS)([^A-Z]|$)", joined):
        return VALUE_KIND_COUNTS
    return None


def _valuesContradict(kind, stats):
    """True when the voxel values cannot be of this kind (SUV in the thousands, Bq/mL that stays tiny)."""
    if stats is None:
        return False
    if kind == VALUE_KIND_SUV:
        return stats["p999"] > SUV_PLAUSIBLE_P999_MAX
    if kind == VALUE_KIND_BQML:
        return stats["p999"] < BQML_PLAUSIBLE_P999_MIN
    return False


def classifyPetValues(storedKind=None, storedModality=None, unitTexts=(), dicomModality="", dicomUnits="", suvConverted=False,
                      treeModality="", name="", stats=None):
    """
    What the voxel values of a SPECT/PET volume are, from the most to the least reliable evidence:

    storedKind    kind recorded by this module on a filtered volume (same as the volume it was filtered from;
                  VALUE_KIND_UNKNOWN when that was not known: the filtered values are then not guessed from)
    storedModality modality recorded with it ("PET" / "SPECT")
    unitTexts     voxel value units / quantity of the node (set by the PET DICOM extension: {SUVbw}g/ml, ...)
    dicomModality Modality (0008,0060) and dicomUnits Units (0054,1001) from the DICOM files the volume was
                  loaded from (BQML, CNTS, GML = SUVbw g/ml); ignored when suvConverted (values already SUV)
    treeModality  modality stored in the Data module tree (PT / NM) when the volume was loaded from DICOM
    name          volume name ("SUV" in the name is believed only when the values fit)
    stats         summarizePetValues of a voxel sample

    Metadata claims are checked against the values (SUV above SUV_PLAUSIBLE_P999_MAX or Bq/mL below
    BQML_PLAUSIBLE_P999_MIN is rejected). Without metadata, only SUV can be recognized from the values
    (mostly fractional, 99.9th percentile within the SUV range) and never for a volume known to be SPECT;
    Bq/mL and counts cannot be told apart by values alone. kind is None whenever the evidence is not sure.
    """
    modalityCode = (dicomModality or treeModality or "").strip().upper()
    lowerName = (name or "").lower()
    if storedModality in ("PET", "SPECT"):
        modality = storedModality
    elif modalityCode == "PT":
        modality = "PET"
    elif modalityCode == "NM":
        modality = "SPECT"
    elif re.search(r"(^|[^a-z])(spect|nm)([^a-z]|$)", lowerName):
        modality = "SPECT"
    elif re.search(r"(^|[^a-z])pet([^a-z]|$)", lowerName):
        modality = "PET"
    else:
        modality = None

    def result(kind, reason):
        petModality = modality or ("PET" if kind in (VALUE_KIND_SUV, VALUE_KIND_BQML) else None)
        return PetValueInfo(kind, petModality, reason)

    def claim(kind, source):
        if _valuesContradict(kind, stats):
            return result(None, f"{valueKindUnit(kind)} according to {source}, but the voxel values do not fit")
        return result(kind, source)

    if storedKind in VALUE_KIND_UNITS:
        return claim(storedKind, "the volume it was filtered from")
    if storedKind == VALUE_KIND_UNKNOWN:
        return result(None, "filtered from a volume whose unit is not known")
    kind = _kindFromUnitTexts(unitTexts)
    if kind is not None:
        return claim(kind, "the voxel value units of the volume")
    if not suvConverted:
        kind = DICOM_UNITS_KINDS.get((dicomUnits or "").strip().upper())
        if kind is not None:
            return claim(kind, f"the DICOM header (Units {dicomUnits.strip().upper()})")
        if modalityCode == "NM" and stats is not None and stats["nonIntegerFraction"] < 0.01:
            return result(VALUE_KIND_COUNTS, "SPECT (NM) volume with whole-number voxel values")
    if stats is not None and modality != "SPECT":
        inRange = SUV_PLAUSIBLE_P999_MIN <= stats["p999"] <= SUV_LIKELY_P999_MAX
        if "suv" in lowerName and inRange:
            return result(VALUE_KIND_SUV, "'SUV' in the name, and the values fit")
        if inRange and stats["nonIntegerFraction"] >= SUV_LIKELY_FRACTIONAL_SHARE:
            return result(VALUE_KIND_SUV, f"voxel values look like SUV (fractional, 99.9th percentile "
                                          f"{stats['p999']:.1f})")
    return result(None, "the unit of the voxel values could not be determined")


def sameValueScale(infoA, maximumA, infoB, maximumB):
    """
    Whether a window made for one volume suits another (switching time points, original <-> filtered):
    both SUV, or the same kind (or both unknown) with maxima within a factor of 2.
    """
    if infoA.kind == VALUE_KIND_SUV and infoB.kind == VALUE_KIND_SUV:
        return True
    if infoA.kind != infoB.kind or infoA.kind == VALUE_KIND_SUV:
        return False
    if maximumA <= 0 or maximumB <= 0:
        return False
    return 0.5 <= maximumA / maximumB <= 2.0


def initialPetRange(info, maximum):
    """(lower, upper) first shown for a SPECT/PET: SUV 0–10 when it is SUV, else 0–100 % of its maximum."""
    if info.kind == VALUE_KIND_SUV:
        return 0.0, INITIAL_SUV_UPPER
    upper = percentOfMaximum(maximum, 100)
    return 0.0, (upper if upper is not None else 1.0)


_petValueInfoCache = {}  # node ID -> (cache key, PetValueInfo)


def _dicomHeaderOfVolume(volumeNode):
    """(Modality, Units) from a DICOM file the volume was loaded from, if it is still in the DICOM database."""
    uids = (volumeNode.GetAttribute("DICOM.instanceUIDs") or "").split()
    database = getattr(slicer, "dicomDatabase", None)
    if not uids or database is None:
        return "", ""
    try:
        if not database.isOpen:
            return "", ""
        path = database.fileForInstance(uids[len(uids) // 2])
        if not path:
            return "", ""
        return database.fileValue(path, "0008,0060") or "", database.fileValue(path, "0054,1001") or ""
    except Exception:
        logging.debug("EasyFusion: could not read the DICOM header of the volume", exc_info=True)
        return "", ""


def _treeModalityOfVolume(volumeNode):
    """Modality (PT / NM ...) stored in the Data module tree for a volume loaded from DICOM, else ""."""
    try:
        shNode = slicer.mrmlScene.GetSubjectHierarchyNode()
        itemID = shNode.GetItemByDataNode(volumeNode)
        if not itemID:
            return ""
        name = slicer.vtkMRMLSubjectHierarchyConstants.GetDICOMSeriesModalityAttributeName()
        return shNode.GetItemAttribute(itemID, name) or ""
    except Exception:
        return ""


def _sampleVoxels(volumeNode, sampleSize=PET_VALUE_SAMPLE_SIZE):
    voxels = slicer.util.arrayFromVolume(volumeNode)
    if voxels.ndim > 3:
        voxels = voxels[..., 0]
    voxels = voxels.ravel()
    step = max(1, voxels.size // sampleSize)
    return voxels[::step]


def petValueInfo(volumeNode):
    """
    classifyPetValues for a volume node (see there): SUV / Bq/mL / counts, or kind None when not sure.
    Cached until the node or its voxels change, so the window info can ask on every view update.
    """
    if volumeNode is None:
        return PetValueInfo(None, None, "no volume")
    imageData = volumeNode.GetImageData()
    if imageData is None or imageData.GetNumberOfPoints() == 0:
        return PetValueInfo(None, None, "the volume has no voxels")
    storedKind = volumeNode.GetAttribute(VALUE_KIND_ATTRIBUTE)
    unitTexts = []
    for getterName in ("GetVoxelValueUnits", "GetVoxelValueQuantity"):
        try:
            entry = getattr(volumeNode, getterName)()
            if entry is not None:
                unitTexts += [entry.GetCodeValue() or "", entry.GetCodeMeaning() or ""]
        except Exception:
            pass
    # Everything the decision depends on that can change (not the node's modified time: that changes too often)
    key = (imageData.GetMTime(), storedKind, volumeNode.GetAttribute(VALUE_MODALITY_ATTRIBUTE),
           volumeNode.GetAttribute("DICOM.instanceUIDs"), tuple(unitTexts), volumeNode.GetName())
    cached = _petValueInfoCache.get(volumeNode.GetID())
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        stats = summarizePetValues(_sampleVoxels(volumeNode))
    except Exception:
        logging.debug("EasyFusion: could not sample the voxel values", exc_info=True)
        stats = None
    dicomModality, dicomUnits = _dicomHeaderOfVolume(volumeNode)
    info = classifyPetValues(
        storedKind=storedKind, storedModality=volumeNode.GetAttribute(VALUE_MODALITY_ATTRIBUTE),
        unitTexts=unitTexts, dicomModality=dicomModality, dicomUnits=dicomUnits,
        # Loaded through the PET DICOM extension: already converted to SUV, the DICOM Units no longer apply
        suvConverted=bool(volumeNode.GetAttribute("DICOM.RWV.instanceUID")),
        treeModality=_treeModalityOfVolume(volumeNode), name=volumeNode.GetName(), stats=stats)
    _petValueInfoCache[volumeNode.GetID()] = (key, info)
    return info


# --- Which kind of image a volume is (for the volume selector labels) -----------------------------------
FUNCTIONAL_LABELS = {"PET": "PET", "SPECT": "SPECT"}
FUNCTIONAL_FALLBACK_LABEL = "SPECT/PET"
ANATOMICAL_LABELS = {"CT": "CT", "MRI": "MRI"}
ANATOMICAL_FALLBACK_LABEL = "CT/MRI"
_MRI_NAME_PATTERN = re.compile(r"(^|[^a-z])(mri?|t1w?|t2w?|flair|dwi|adc|stir)([^a-z]|$)")
_CT_NAME_PATTERN = re.compile(r"(^|[^a-z])ct([^a-z]|$)")


def anatomicalModalityFrom(modalityCode="", scalarMinimum=None, name=""):
    """
    "CT", "MRI" or None (not sure), from the DICOM modality (CT / MR), else the values (a CT has air far below
    0 HU; an image without negative values is not a CT), else the name.
    """
    code = (modalityCode or "").strip().upper()
    if code == "CT":
        return "CT"
    if code == "MR":
        return "MRI"
    if scalarMinimum is not None and np.isfinite(scalarMinimum):
        if looksLikeCT(scalarMinimum):
            return "CT"
        if float(scalarMinimum) >= 0.0:
            return "MRI"
    lowerName = (name or "").lower()
    if _MRI_NAME_PATTERN.search(lowerName):
        return "MRI"
    if _CT_NAME_PATTERN.search(lowerName):
        return "CT"
    return None


def anatomicalModality(volumeNode):
    """anatomicalModalityFrom for a volume node: "CT", "MRI" or None."""
    if volumeNode is None:
        return None
    code = _treeModalityOfVolume(volumeNode) or _dicomHeaderOfVolume(volumeNode)[0]
    imageData = volumeNode.GetImageData()
    minimum = imageData.GetScalarRange()[0] if imageData is not None and imageData.GetNumberOfPoints() else None
    return anatomicalModalityFrom(code, minimum, volumeNode.GetName())


def functionalLabel(volumeNode):
    """ "PET", "SPECT", or "SPECT/PET" when it is not known (see classifyPetValues)."""
    modality = petValueInfo(volumeNode).modality if volumeNode is not None else None
    return FUNCTIONAL_LABELS.get(modality, FUNCTIONAL_FALLBACK_LABEL)


def anatomicalLabel(volumeNode):
    """ "CT", "MRI", or "CT/MRI" when it is not known."""
    return ANATOMICAL_LABELS.get(anatomicalModality(volumeNode), ANATOMICAL_FALLBACK_LABEL)

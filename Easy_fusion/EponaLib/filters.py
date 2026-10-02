"""AI post-processing filters: Belenos PET Denoise model sidecars, the published model catalog and downloads, job size estimates, the progress dialog and the filter logic (inference)."""

import os
import re
import ast
import gc
import math
import time
import logging
import configparser
import json
import hashlib
import urllib.request

import numpy as np
import qt
import vtk
import slicer

from .values import (
    petValueInfo, VALUE_KIND_ATTRIBUTE, VALUE_KIND_UNKNOWN, VALUE_MODALITY_ATTRIBUTE,
)

# ---------------------------------------------------------------------------
# AI post-processing filters (models of the Belenos PET Denoise module: <name>.pth + <name>.txt sidecar)
# ---------------------------------------------------------------------------

# A filter never changes its input: the result is always a new volume, tagged with where it came from
FILTER_MODEL_ATTRIBUTE = "EasyFusion.FilterModel"
FILTER_DATE_ATTRIBUTE = "EasyFusion.FilterDate"
FILTER_SOURCE_ROLE = "EasyFusionFilterSource"
FILTER_TARGET_PET = "pet"
FILTER_TARGET_CT = "ct"
# Model folder of the Belenos PET Denoise module (PETDenoise extension), stored as an application setting
PETDENOISE_SETTINGS_MODEL_FOLDER = "PETDenoise/ModelFolder"
# Published Belenos PET Denoise models (GitHub releases of the SlicerPETDenoise repository). They are listed in the
# model selector and downloaded on first use into FILTER_MODEL_CACHE_FOLDER_NAME inside Slicer's cache folder.
FILTER_MODEL_REPOSITORY = "4burakfe/SlicerPETDenoise"
FILTER_MODEL_RELEASES = (("Models", "Denoising"), ("SuperRes_Models", "Super-resolution"))  # (release tag, kind)
FILTER_MODEL_RELEASE_PAGE_URL = "https://github.com/{repository}/releases/tag/{tag}"
FILTER_MODEL_DOWNLOAD_URL = "https://github.com/{repository}/releases/download/{tag}/{name}"
FILTER_MODEL_RELEASE_API_URL = "https://api.github.com/repos/{repository}/releases/tags/{tag}"
FILTER_MODEL_CACHE_FOLDER_NAME = "BelenosModels"
FILTER_MODEL_CATALOG_FILE = "catalog.json"  # release asset lists from the last successful check
FILTER_MODEL_CATALOG_MAX_AGE_S = 24 * 3600  # GitHub is asked for new / replaced models at most once a day
FILTER_NETWORK_TIMEOUT_S = 8
FILTER_DOWNLOAD_CHUNK_BYTES = 1 << 20
# Models published when this module was released: (file, release tag, size in bytes, SHA-256 of the file).
# Used as they are when GitHub cannot be reached; the release lists on GitHub add new and replaced models.
FILTER_PUBLISHED_MODELS = (
    ("pet_denoiser_std_char.pth", "Models", 80995179,
     "11e4627577c2ed8c0f19616dd004c63ee00c321febb85f54158bb88c485881e8"),
    ("pet_denoiser_x14w3.pth", "Models", 80994666,
     "dbd53df5765e48c61320b39737dbf5b73973b0f2b53a7f8ec3d2541947088a53"),
    ("pet_denoiser_x19w1_5.pth", "Models", 80994666,
     "dd05fb82873bd579ebe8785f50371579e930d2b771e2385947ca1ada46f7c60f"),
    ("PET_superres12.pth", "SuperRes_Models", 26564203,
     "af17bf85515ac4b64404e40fda0290506673fc951a0bfe71c9672a372b6d6d77"),
    ("CT_superres24.pth", "SuperRes_Models", 80994495,
     "51a8ce1b383f3803bdace09f4a6e5e9ee63e8c51b48ef6476b8cac477763bb87"),
)
# Inference settings of the PETDenoise module, so a model gives the same result in both modules
FILTER_WINDOW_OVERLAP = 0.25
FILTER_MIN_VRAM_GB = 1.9
FILTER_EINOPS_REQUIREMENT = "einops==0.6.1"
# Rough peak memory of one run, in float32 copies of the volume on the model's voxel grid
FILTER_MEMORY_COPIES = 6
# A run counts as large (the user is advised to crop) above this memory estimate or amount of network work
# (number of windows x voxels per window; 5e8 is about 1900 windows of 64x64x64)
FILTER_LARGE_MEMORY_BYTES = 4 * 1024 ** 3
FILTER_LARGE_WORK_VOXELS = 5e8
# Optional crop box ("Limit to ROI"): temporary, never saved, removed after a successful run
FILTER_CROP_ROI_ATTRIBUTE = "EasyFusion.FilterCropROI"
FILTER_CROP_ROI_NAME = "Filter crop ROI"
FILTER_CROP_ROI_COLOR = (0.0, 0.85, 1.0)
FILTER_CROPPED_ATTRIBUTE = "EasyFusion.FilterCroppedToROI"
# Voxel-based crop of a volume under a non-linear (e.g. deformable registration) transform, which Crop Volume
# refuses: the ROI surface is sampled on this many points per edge and mapped into the volume's own coordinates
FILTER_CROP_SAMPLES_PER_EDGE = 9
# Used for keys missing from a sidecar (or models without one): the defaults of the PETDenoise panel
FILTER_DEFAULT_PARAMETERS = {
    "dual_channel": False,
    "architecture": "UNET",
    "strides": (2, 2, 2, 2),
    "channels": (128, 256, 512, 1024, 2048),
    "res_units": 2,
    "down_kernel": 3,
    "up_kernel": 3,
    "num_heads": (3, 6, 12, 24),
    "depths": (2, 2, 2, 2),
    "feature_size": 24,
    "do_rate": 0.0,
    "voxel_spacing": (2.0, 2.0, 2.0),
    "block_size": (64, 64, 64),
    "prevent_negative": True,
    "dont_resample": False,
}


class FilterCancelled(Exception):
    """Raised by the progress callback of a filter run when the user pressed Cancel."""


def parseFilterMetadata(text):
    """
    Model parameters from a PETDenoise sidecar file (<model>.txt next to <model>.pth), read the way the PETDenoise
    module reads them: same keys, same defaults, and (as there) a voxel_spacing line switches resampling back on.

    Returns (params, notes). params has every key of FILTER_DEFAULT_PARAMETERS (+ "modality" if the file has one).
    notes maps every other "key: value" line (lower-case key) to (key as written, value), e.g. the SUV biases.
    """
    params = dict(FILTER_DEFAULT_PARAMETERS)
    notes = {}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        rawKey, value = line.split(":", 1)
        rawKey, value = rawKey.strip().lstrip("\ufeff"), value.strip()
        key = rawKey.lower()
        if not key:
            continue
        try:
            if key in ("dual_channel", "dont_resample", "prevent_negative"):
                params[key] = value.lower() == "true"
            elif key in ("voxel_spacing", "block_size"):
                values = tuple(ast.literal_eval(value))
                if len(values) != 3:
                    raise ValueError("three values expected")
                if key == "voxel_spacing":
                    params[key] = tuple(float(v) for v in values)
                    params["dont_resample"] = False
                else:
                    params[key] = tuple(int(v) for v in values)
            elif key in ("strides", "channels", "num_heads", "depths"):
                params[key] = tuple(int(v) for v in ast.literal_eval(value))
            elif key in ("res_units", "down_kernel", "up_kernel", "feature_size"):
                params[key] = int(value)
            elif key == "do_rate":
                params[key] = float(value)
            elif key == "architecture":
                # As in PETDenoise: anything other than UNET / SwinUNETR selects SwinUNETR+GCFN
                params[key] = value if value in ("UNET", "SwinUNETR") else "SwinUNETR+GCFN"
            elif key == "modality":
                params[key] = value
            else:
                notes[key] = (rawKey, value)
        except (ValueError, SyntaxError, TypeError):
            logging.warning(f"EasyFusion: ignored model parameter line '{rawKey}: {value}'")
    return params, notes


def filterSuvNotes(notes):
    """Sidecar lines whose key starts with "SUV" (e.g. "SUVmax Bias: -0.28 g/mL"), as written, for the warning."""
    return [f"{rawKey}: {value}" for key, (rawKey, value) in notes.items() if key.startswith("suv")]


def guessFilterTarget(modelName, params):
    """
    Volume a model is meant for. A "modality" line in the sidecar decides (CT... / MR... -> CT/MRI, else SPECT/PET).
    Without one, a file name containing a separate word CT, MR or MRI (e.g. CT_superres24.pth) means CT/MRI.
    """
    modality = str(params.get("modality") or "").strip().lower()
    if modality:
        return FILTER_TARGET_CT if modality.startswith(("ct", "mr")) else FILTER_TARGET_PET
    stem = os.path.splitext(os.path.basename(modelName or ""))[0].lower()
    return FILTER_TARGET_CT if re.search(r"(^|[^a-z])(ct|mr|mri)([^a-z]|$)", stem) else FILTER_TARGET_PET


def sidecarName(modelName):
    """<model>.txt for <model>.pth."""
    return os.path.splitext(modelName)[0] + ".txt"


def releaseAssetUrl(tag, name):
    return FILTER_MODEL_DOWNLOAD_URL.format(repository=FILTER_MODEL_REPOSITORY, tag=tag, name=name)


def parseReleaseAssets(release):
    """Asset list of a GitHub release (API JSON) as [{"name", "size", "sha256", "url"}]; sha256 None if unknown."""
    assets = []
    for asset in (release or {}).get("assets") or []:
        name = str(asset.get("name") or "")
        if not name:
            continue
        digest = str(asset.get("digest") or "")
        assets.append({
            "name": name,
            "size": int(asset.get("size") or 0) or None,
            "sha256": digest[len("sha256:"):].lower() if digest.lower().startswith("sha256:") else None,
            "url": str(asset.get("browser_download_url") or "") or None,
        })
    return assets


def mergePublishedModelCatalog(releaseAssets=None):
    """
    Published models as {file name: entry}, entry = name, tag, kind, size, sha256, url, sidecarUrl.

    releaseAssets: {release tag: parseReleaseAssets(...)} from GitHub (or its saved copy); None / {} gives the
    built-in list. For every release that was read, its asset list is what counts: models added there appear,
    removed ones disappear, and a model whose size changed was replaced, so the built-in checksum is dropped
    (GitHub's own checksum is used when it provides one).
    """
    kinds = dict(FILTER_MODEL_RELEASES)
    models = {}
    for name, tag, size, sha256 in FILTER_PUBLISHED_MODELS:
        models[name] = {"name": name, "tag": tag, "kind": kinds.get(tag, ""), "size": size, "sha256": sha256,
                        "url": releaseAssetUrl(tag, name), "sidecarUrl": releaseAssetUrl(tag, sidecarName(name))}
    for tag, assets in (releaseAssets or {}).items():
        if tag not in kinds:
            continue
        names = {asset["name"] for asset in assets}
        for name in [name for name, entry in models.items() if entry["tag"] == tag and name not in names]:
            del models[name]
        for asset in assets:
            name = asset["name"]
            if not name.lower().endswith(".pth"):
                continue
            entry = models.get(name)
            if entry is None or entry["tag"] != tag:
                entry = {"name": name, "tag": tag, "kind": kinds[tag], "size": None, "sha256": None}
                models[name] = entry
            elif asset["size"] and asset["size"] != entry["size"]:
                entry["sha256"] = None  # replaced on GitHub: the built-in checksum is for the old file
            entry["size"] = asset["size"] or entry["size"]
            entry["sha256"] = asset["sha256"] or entry["sha256"]
            entry["url"] = asset["url"] or releaseAssetUrl(tag, name)
            sidecar = sidecarName(name)
            entry["sidecarUrl"] = releaseAssetUrl(tag, sidecar) if sidecar in names else None
    return models


def isCompleteModelFile(path, expectedSize):
    """True if the file exists and (when the published size is known) has exactly that size."""
    try:
        return os.path.isfile(path) and (not expectedSize or os.path.getsize(path) == expectedSize)
    except OSError:
        return False


def listFilterModels(publishedModels, cacheFolder, customFolder):
    """
    Entries of the model selector: published models first (in release order), then the user's own folder.

    Each entry: key, name, label, path, available, published, plus the catalog fields for published models.
    A published model is available when it was downloaded into cacheFolder, or when the user's folder already
    holds the same file (same name and size, e.g. downloaded earlier for the PET Denoise module); otherwise
    path is where it will be downloaded to. Files of the user's folder that are not published models (or
    differ from them) are listed as their own models.
    """
    try:
        customFiles = sorted((name for name in os.listdir(customFolder) if name.lower().endswith(".pth")),
                             key=str.lower) if customFolder else []
    except OSError:
        customFiles = []
    tagOrder = {tag: index for index, (tag, _) in enumerate(FILTER_MODEL_RELEASES)}
    entries, usedCustom = [], set()
    for model in sorted(publishedModels.values(), key=lambda m: (tagOrder.get(m["tag"], 99), m["name"].lower())):
        name = model["name"]
        path = os.path.join(cacheFolder, name)
        available = isCompleteModelFile(path, model["size"])
        if not available and name in customFiles:
            customPath = os.path.join(customFolder, name)
            if isCompleteModelFile(customPath, model["size"]):
                path, available = customPath, True
                usedCustom.add(name)
        label = os.path.splitext(name)[0]
        if not available:
            sizeText = f", {formatByteSize(model['size'])}" if model["size"] else ""
            label += f"  (download{sizeText})"
        entries.append(dict(model, key=f"published/{name}", label=label, path=path, available=available,
                            published=True))
    for name in customFiles:
        if name in usedCustom:
            continue
        label = os.path.splitext(name)[0] + ("  (own folder)" if name in publishedModels else "")
        entries.append({"key": f"custom/{name}", "name": name, "label": label,
                        "path": os.path.join(customFolder, name), "available": True, "published": False})
    return entries


def resampledGridShape(dimensionsIjk, spacing, targetSpacing):
    """Approximate array shape (k, j, i) of a volume after resampling to targetSpacing (None: not resampled)."""
    if targetSpacing is None:
        dims = [int(d) for d in dimensionsIjk]
    else:
        dims = [max(1, int(round(d * s / t))) for d, s, t in zip(dimensionsIjk, spacing, targetSpacing)]
    return tuple(dims[::-1])


def estimateSlidingWindowCount(imageShape, roiSize, overlap=FILTER_WINDOW_OVERLAP):
    """
    Number of windows MONAI's sliding_window_inference evaluates (= predictor calls with sw_batch_size=1).
    Same arithmetic as monai.inferers.utils (_get_scan_interval, dense_patch_slices). Drives the progress bar.
    """
    total = 1
    for size, roi in zip(imageShape, roiSize):
        roi = int(roi)
        size = max(int(size), roi)  # MONAI pads the image up to the window size
        interval = roi if roi == size else max(int(roi * (1.0 - overlap)), 1)
        count = int(math.ceil(float(size) / interval))
        first = next((d for d in range(count) if d * interval + roi >= size), None)
        total *= first + 1 if first is not None else 1
    return total


def filterJobSize(dimensionsIjk, spacing, params, channels=1):
    """
    Size of a filter run on a volume with these dimensions (i, j, k) and spacing: grid shape (k, j, i) on the
    model's voxel grid, voxel count, rough peak memory, number of windows, and whether it counts as large.
    """
    targetSpacing = None if params["dont_resample"] else params["voxel_spacing"]
    shape = resampledGridShape(dimensionsIjk, spacing, targetSpacing)
    voxels = math.prod(shape)  # Python ints: no overflow for huge grids
    memoryBytes = float(voxels) * 4 * (FILTER_MEMORY_COPIES + channels - 1)
    windows = estimateSlidingWindowCount(shape, params["block_size"])
    work = float(windows) * math.prod(int(v) for v in params["block_size"])
    return {"shape": shape, "voxels": voxels, "memoryBytes": memoryBytes, "windows": windows,
            "large": memoryBytes >= FILTER_LARGE_MEMORY_BYTES or work >= FILTER_LARGE_WORK_VOXELS}


def voxelCopyRegion(sourceIjkToRas, sourceShape, croppedIjkToRas, croppedShape, tolerance=1e-3):
    """
    Where a voxel-based crop lies in its source volume. Shapes are array shapes [k, j, i] (as arrayFromVolume);
    matrices are IJK -> RAS (4x4).

    Returns (sourceSlices, croppedSlices, firstSourceIjk): the block both volumes share, as [k, j, i] slice tuples,
    and the source IJK index of its first voxel. Voxels of the crop outside the source (padding) are left out.
    Raises ValueError("resampled") if the crop is not on the source's voxel grid (other voxel size or axes, or
    shifted by a fraction of a voxel, i.e. interpolated), ValueError("outside") if it shares no voxel with it.
    """
    source = np.asarray(sourceIjkToRas, dtype=float)
    cropped = np.asarray(croppedIjkToRas, dtype=float)
    scale = max(float(np.abs(source[:3, :3]).max()), 1e-12)
    if not np.allclose(source[:3, :3], cropped[:3, :3], rtol=0.0, atol=1e-6 * scale):
        raise ValueError("resampled")
    offset = np.linalg.solve(source[:3, :3], cropped[:3, 3] - source[:3, 3])  # crop voxel (0,0,0) in source IJK
    rounded = np.round(offset)
    if np.any(np.abs(offset - rounded) > tolerance):
        raise ValueError("resampled")
    offset = rounded.astype(int)
    sourceDims = np.array(sourceShape[:3][::-1])    # (i, j, k)
    croppedDims = np.array(croppedShape[:3][::-1])
    lo = np.maximum(offset, 0)
    hi = np.minimum(offset + croppedDims, sourceDims)
    if np.any(hi <= lo):
        raise ValueError("outside")
    sourceSlices = tuple(slice(int(lo[a]), int(hi[a])) for a in (2, 1, 0))
    croppedSlices = tuple(slice(int(lo[a] - offset[a]), int(hi[a] - offset[a])) for a in (2, 1, 0))
    return sourceSlices, croppedSlices, tuple(int(v) for v in lo)


def boxSurfacePoints(size, samplesPerEdge=FILTER_CROP_SAMPLES_PER_EDGE):
    """
    Points on the surface of a box of the given size (x, y, z) centered at the origin (an ROI's object coordinates):
    a samplesPerEdge x samplesPerEdge grid on each face, corners and edges included. Under a smooth deformation the
    image of the surface encloses the image of the whole box, so these points are enough to bound it.
    """
    half = np.asarray(size, dtype=float) / 2.0
    n = max(int(samplesPerEdge), 2)
    t = np.linspace(-1.0, 1.0, n)
    u, v = np.meshgrid(t, t, indexing="ij")
    faces = []
    for axis in range(3):
        a, b = [x for x in range(3) if x != axis]
        for side in (-1.0, 1.0):
            face = np.empty((u.size, 3))
            face[:, axis] = side
            face[:, a] = u.ravel()
            face[:, b] = v.ravel()
            faces.append(face)
    return np.unique(np.vstack(faces), axis=0) * half


def voxelBlockFromIjkPoints(ijkPoints, dimensions, margin=0):
    """
    Smallest block of voxels touched by the given continuous IJK points (voxel centers at integer IJK), grown by
    margin voxels and clipped to the volume. dimensions: (I, J, K) as vtkImageData.GetDimensions().
    Returns (lo, hi) as (i, j, k) integer tuples, hi exclusive, or None if the block misses the volume.
    Non-finite points (e.g. where an inverse transform did not converge) are ignored.
    """
    points = np.asarray(ijkPoints, dtype=float).reshape(-1, 3)
    points = points[np.all(np.isfinite(points), axis=1)]
    if points.size == 0:
        return None
    dims = np.asarray(dimensions[:3], dtype=int)
    lo = np.floor(points.min(axis=0) + 0.5).astype(int) - int(margin)   # voxel containing the lowest point
    hi = np.floor(points.max(axis=0) + 0.5).astype(int) + 1 + int(margin)
    lo = np.maximum(lo, 0)
    hi = np.minimum(hi, dims)
    if np.any(hi <= lo):
        return None
    return tuple(int(x) for x in lo), tuple(int(x) for x in hi)

def castFilterResult(array, dtype):
    """Filtered voxels in the input's voxel type. Integer types are rounded (not truncated) and kept in range."""
    dtype = np.dtype(dtype)
    if np.issubdtype(dtype, np.integer):
        limits = np.iinfo(dtype)
        return np.clip(np.rint(array), limits.min, limits.max).astype(dtype)
    return np.asarray(array).astype(dtype, copy=False)


def formatByteSize(numberOfBytes):
    gigabytes = numberOfBytes / 1024.0 ** 3
    return f"{gigabytes:.1f} GB" if gigabytes >= 1.0 else f"{numberOfBytes / 1024.0 ** 2:.0f} MB"


# ---------------------------------------------------------------------------
# AI post-processing filters
# ---------------------------------------------------------------------------

class FilterProgressDialog:
    """
    Modal progress window of a filter run. Calling it updates the text / bar and lets Qt repaint;
    once Cancel was pressed, the next call raises FilterCancelled (the run then cleans up after itself).
    """

    def __init__(self, title):
        dialog = qt.QProgressDialog(slicer.util.mainWindow())
        dialog.setWindowTitle(title)
        dialog.setWindowModality(qt.Qt.ApplicationModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setMinimumWidth(420)
        dialog.setRange(0, 0)  # busy indicator until the number of windows is known
        dialog.setLabelText("Preparing…")
        dialog.show()
        self.dialog = dialog
        slicer.app.processEvents()

    def __call__(self, text, done=0, total=0, detail=None):
        """detail: second line under text when a total is known (default "Window <done> of <total>")."""
        if self.dialog.wasCanceled:
            raise FilterCancelled()
        if total > 0:
            done = min(int(done), int(total))
            self.dialog.setLabelText(f"{text}\n{detail if detail is not None else f'Window {done} of {total}'}")
            self.dialog.setRange(0, int(total))
            self.dialog.setValue(done)
        else:
            self.dialog.setLabelText(text)
            self.dialog.setRange(0, 0)
        slicer.app.processEvents()
        if self.dialog.wasCanceled:
            raise FilterCancelled()

    def close(self):
        self.dialog.close()
        self.dialog.deleteLater()


class Easy_fusionFilterLogic:
    """
    AI post-processing (denoising / super-resolution) with the models of the Belenos PET Denoise module.
    Same pipeline as PETDenoise: linear resampling to the model's voxel spacing, sliding-window inference
    (gaussian blending, 25% overlap), result = input - predicted noise, optional clipping of negative values.
    The source volume is never written to: the result is always a new volume.
    """

    @staticmethod
    def petDenoiseModelFolder():
        """
        Model folder last used in the Belenos PET Denoise module (separate PETDenoise extension), if any.
        Read from the application settings first; the module's model_config.ini is the fallback for older
        PETDenoise versions that did not store it there.
        """
        folder = qt.QSettings().value(PETDENOISE_SETTINGS_MODEL_FOLDER) or ""
        if folder and os.path.isdir(folder):
            return folder
        directories = []
        try:
            directories.append(os.path.dirname(slicer.modules.petdenoise.path))
        except AttributeError:
            pass  # PETDenoise extension not installed
        for directory in directories:
            iniPath = os.path.join(directory, "model_config.ini")
            if not os.path.isfile(iniPath):
                continue
            config = configparser.ConfigParser()
            try:
                config.read(iniPath)
            except configparser.Error:
                continue
            folder = config.get("ModelFolder", "path", fallback="")
            if folder and os.path.isdir(folder):
                return folder
        return None

    # --- Published models (download on first use) ---------------------------

    @staticmethod
    def publishedModelFolder():
        """Where published models are downloaded to: Slicer's cache folder (kept across extension updates)."""
        return os.path.join(slicer.app.cachePath, FILTER_MODEL_CACHE_FOLDER_NAME)

    @classmethod
    def readSavedModelCatalog(cls):
        """(release assets, time of that check) saved by the last successful check of GitHub; ({}, 0) if none."""
        try:
            with open(os.path.join(cls.publishedModelFolder(), FILTER_MODEL_CATALOG_FILE), "r",
                      encoding="utf-8") as catalogFile:
                saved = json.load(catalogFile)
            releases = {str(tag): [dict(asset) for asset in assets]
                        for tag, assets in (saved.get("releases") or {}).items()}
            for assets in releases.values():
                for asset in assets:
                    if not asset.get("name"):
                        raise ValueError("asset without a name")
                    asset.setdefault("size", None)
                    asset.setdefault("sha256", None)
                    asset.setdefault("url", None)
            return releases, float(saved.get("checked") or 0)
        except FileNotFoundError:
            return {}, 0.0
        except (OSError, ValueError, TypeError, AttributeError):
            logging.warning("EasyFusion: ignored an unreadable saved model list", exc_info=True)
            return {}, 0.0

    @staticmethod
    def openUrl(url, timeout=FILTER_NETWORK_TIMEOUT_S, accept=None):
        headers = {"User-Agent": "3DSlicer-Epona"}
        if accept:
            headers["Accept"] = accept
        return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)

    @classmethod
    def fetchModelCatalog(cls):
        """
        Asset lists of the model releases from the GitHub API, saved for later sessions.
        Raises (URLError, OSError, ValueError) when GitHub cannot be reached or answers unexpectedly.
        """
        releases = {}
        for tag, _ in FILTER_MODEL_RELEASES:
            url = FILTER_MODEL_RELEASE_API_URL.format(repository=FILTER_MODEL_REPOSITORY, tag=tag)
            with cls.openUrl(url, accept="application/vnd.github+json") as response:
                releases[tag] = parseReleaseAssets(json.loads(response.read().decode("utf-8")))
        folder = cls.publishedModelFolder()
        try:
            os.makedirs(folder, exist_ok=True)
            catalogPath = os.path.join(folder, FILTER_MODEL_CATALOG_FILE)
            with open(catalogPath + ".part", "w", encoding="utf-8") as catalogFile:
                json.dump({"checked": time.time(), "releases": releases}, catalogFile, indent=1)
            os.replace(catalogPath + ".part", catalogPath)
        except OSError:
            logging.warning("EasyFusion: could not save the model list", exc_info=True)
        return releases

    @classmethod
    def downloadFile(cls, url, destination, expectedSize=None, sha256=None, report=None, text="Downloading…"):
        """
        Download url to destination through destination + ".part", which is renamed only once the size and (when
        known) the SHA-256 checksum match, so an interrupted or corrupted download never looks like a model.
        report(text, done, total, detail) is the progress callback; it raises FilterCancelled to stop.
        """
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        partPath = destination + ".part"
        hasher = hashlib.sha256()
        done = 0
        try:
            with cls.openUrl(url, timeout=30) as response, open(partPath, "wb") as output:
                total = int(response.headers.get("Content-Length") or 0) or int(expectedSize or 0)
                while True:
                    if report is not None:
                        detail = (f"{done / 1e6:.0f} of {total / 1e6:.0f} MB" if total
                                  else f"{done / 1e6:.0f} MB")
                        report(text, done, total, detail)
                    chunk = response.read(FILTER_DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    output.write(chunk)
                    hasher.update(chunk)
                    done += len(chunk)
            if expectedSize and done != expectedSize:
                raise OSError(f"incomplete download: {done} of {expectedSize} bytes received")
            if sha256 and hasher.hexdigest() != sha256.lower():
                raise OSError("the downloaded file is corrupted (SHA-256 checksum does not match)")
            os.replace(partPath, destination)
        finally:
            if os.path.exists(partPath):
                try:
                    os.remove(partPath)
                except OSError:
                    logging.warning(f"EasyFusion: could not remove {partPath}")
        return destination

    @classmethod
    def downloadSidecar(cls, model):
        """Download a published model's .txt next to where the model is (or will be). Path, or None if it has none."""
        if not model.get("sidecarUrl"):
            return None
        return cls.downloadFile(model["sidecarUrl"],
                                os.path.join(cls.publishedModelFolder(), sidecarName(model["name"])))

    @staticmethod
    def uniqueVolumeName(baseName):
        name, number = baseName, 1
        while slicer.mrmlScene.GetFirstNodeByName(name) is not None:
            name = f"{baseName}_{number}"
            number += 1
        return name

    @staticmethod
    def chooseDevice(torch, forceCPU):
        """GPU when available with at least FILTER_MIN_VRAM_GB of memory (as in PETDenoise), otherwise CPU."""
        cpu = torch.device("cpu")
        if forceCPU:
            return cpu, "CPU, forced"
        if not torch.cuda.is_available():
            return cpu, "CPU"
        try:
            properties = torch.cuda.get_device_properties(0)
        except Exception:
            logging.exception("EasyFusion: could not query the GPU")
            return cpu, "CPU, GPU could not be queried"
        vramGb = properties.total_memory / 1024.0 ** 3
        if vramGb < FILTER_MIN_VRAM_GB:
            return cpu, f"CPU, GPU has only {vramGb:.1f} GB"
        return torch.device("cuda"), f"GPU {properties.name}, {vramGb:.1f} GB"

    @staticmethod
    def buildNetwork(params, inChannels):
        """
        Network of a PETDenoise model. Class structure and attribute names (unet / model / gcfn) are exactly those
        of the PETDenoise module, so its .pth state dicts load unchanged.
        """
        import torch.nn as nn
        import torch.nn.functional as F
        from monai import __version__ as monaiVersion
        from monai.networks.nets import UNet, SwinUNETR
        from packaging import version

        swinExtra = {"img_size": (64, 64, 64)} if version.parse(monaiVersion) < version.parse("1.5") else {}

        class DenoiseUNet(nn.Module):
            def __init__(self, in_channels=1, out_channels=1, channels=(32, 64, 128, 256, 512), num_res_units=2,
                         strides=(2, 2, 2, 2), kernel_size=3, up_kernel_size=3):
                super().__init__()
                self.unet = UNet(strides=strides, num_res_units=num_res_units, kernel_size=kernel_size,
                                 up_kernel_size=up_kernel_size, spatial_dims=3, in_channels=in_channels,
                                 out_channels=out_channels, channels=channels)

            def forward(self, x):
                return self.unet(x)

        class SwinDenoiser(nn.Module):
            def __init__(self, in_channels=1, out_channels=1, feature_size=48, heads=(6, 12, 24, 48),
                         depths=(2, 3, 3, 2), do_rate=0.1):
                super().__init__()
                self.model = SwinUNETR(num_heads=heads, use_v2=True, in_channels=in_channels,
                                       out_channels=out_channels, feature_size=feature_size, depths=depths,
                                       dropout_path_rate=do_rate, use_checkpoint=True, **swinExtra)

            def forward(self, x):
                return self.model(x)

        class GCFN(nn.Module):
            def __init__(self, dim):
                super().__init__()
                self.norm = nn.LayerNorm(dim)
                self.fc1 = nn.Linear(dim, dim)
                self.fc2 = nn.Linear(dim, dim)
                self.fc0 = nn.Linear(dim, dim)
                self.conv1 = nn.Conv3d(dim, dim, kernel_size=5, padding=2, groups=dim)
                self.conv2 = nn.Conv3d(dim, dim, kernel_size=5, padding=2, groups=dim)

            def forward(self, x):
                B, C, D, H, W = x.shape
                x_ = x.permute(0, 2, 3, 4, 1).contiguous().view(B * D * H * W, C)
                x1 = self.fc1(self.norm(x_)).view(B, D, H, W, C).permute(0, 4, 1, 2, 3)
                x2 = self.fc2(self.norm(x_)).view(B, D, H, W, C).permute(0, 4, 1, 2, 3)
                gate = F.gelu(self.conv1(x1)) * self.conv2(x2)
                gate = gate.permute(0, 2, 3, 4, 1).contiguous().view(B * D * H * W, C)
                out = self.fc0(gate).view(B, D, H, W, C).permute(0, 4, 1, 2, 3)
                return out + x

        class SwinGCFN(nn.Module):
            def __init__(self, in_channels=1, out_channels=1, feature_size=48, heads=(6, 12, 24, 48),
                         depths=(2, 3, 3, 2), do_rate=0.1):
                super().__init__()
                self.model = SwinUNETR(num_heads=heads, use_v2=True, in_channels=in_channels,
                                       out_channels=out_channels, feature_size=feature_size, depths=depths,
                                       dropout_path_rate=do_rate, use_checkpoint=True, **swinExtra)
                self.gcfn = GCFN(dim=out_channels)

            def forward(self, x):
                return self.gcfn(self.model(x))

        architecture = params["architecture"]
        if architecture == "UNET":
            return DenoiseUNet(in_channels=inChannels, channels=params["channels"], num_res_units=params["res_units"],
                               strides=params["strides"], kernel_size=params["down_kernel"],
                               up_kernel_size=params["up_kernel"])
        swinClass = SwinDenoiser if architecture == "SwinUNETR" else SwinGCFN
        return swinClass(in_channels=inChannels, feature_size=params["feature_size"], heads=params["num_heads"],
                         depths=params["depths"], do_rate=params["do_rate"])

    def loadModel(self, torch, modelPath, params, inChannels, device):
        model = self.buildNetwork(params, inChannels).to(device)
        try:
            state = torch.load(modelPath, map_location=device, weights_only=True)
        except TypeError:  # PyTorch older than 1.13 has no weights_only
            state = torch.load(modelPath, map_location=device)
        try:
            model.load_state_dict(state)
        except RuntimeError as error:
            raise RuntimeError(
                f"The weights in {os.path.basename(modelPath)} do not match the {params['architecture']} network "
                "described by its .txt file. Check the parameters in the .txt file.") from error
        model.eval()
        return model

    @staticmethod
    def _addHiddenVolume(name):
        """Scratch volume: hidden from selectors *before* it enters the scene, never saved."""
        node = slicer.vtkMRMLScalarVolumeNode()
        node.SetName(name)
        node.SetHideFromEditors(True)
        node.SetSaveWithScene(False)
        return slicer.mrmlScene.AddNode(node)

    @staticmethod
    def _runCli(module, parameters, label):
        cliNode = slicer.cli.createNode(module)
        try:
            slicer.cli.runSync(module, cliNode, parameters, update_display=False)
            if cliNode.GetStatus() & cliNode.ErrorsMask:
                raise RuntimeError(f"{label} failed: {cliNode.GetErrorText()}")
        finally:
            slicer.mrmlScene.RemoveNode(cliNode)

    def resampleToGrid(self, volumeNode, referenceNode):
        """Voxels of volumeNode on the voxel grid of referenceNode, float32 [k, j, i] (second input channel)."""
        scratchNode = self._addHiddenVolume("EasyFusionFilterChannel2")
        try:
            self._runCli(slicer.modules.brainsresample,
                         {"inputVolume": volumeNode.GetID(), "referenceVolume": referenceNode.GetID(),
                          "outputVolume": scratchNode.GetID(), "pixelType": "float", "interpolationMode": "Linear"},
                         "Resampling the second input")
            return np.array(slicer.util.arrayFromVolume(scratchNode), dtype=np.float32)
        finally:
            slicer.mrmlScene.RemoveNode(scratchNode)

    # --- Crop ("Limit to ROI"), done with Slicer's Crop Volume module ---------

    @staticmethod
    def findCropRois():
        return [node for node in slicer.util.getNodesByClass("vtkMRMLMarkupsROINode")
                if node.GetAttribute(FILTER_CROP_ROI_ATTRIBUTE)]

    @staticmethod
    def styleCropRoiDisplayNode(displayNode):
        if displayNode is None:
            return
        displayNode.SetSaveWithScene(False)
        wasModifying = displayNode.StartModify()
        displayNode.SetSelectedColor(*FILTER_CROP_ROI_COLOR)
        displayNode.SetColor(*FILTER_CROP_ROI_COLOR)
        # Resize / move handles only: the box stays aligned with the volume axes, as voxel-based cropping expects
        for methodName, value in (("SetHandlesInteractive", True), ("SetScaleHandleVisibility", True),
                                  ("SetTranslationHandleVisibility", True), ("SetRotationHandleVisibility", False),
                                  ("SetFillOpacity", 0.05), ("SetOutlineOpacity", 1.0)):
            method = getattr(displayNode, methodName, None)
            if method is not None:
                method(value)
        displayNode.EndModify(wasModifying)

    def createCropRoi(self, volumeNode):
        """Adjustable crop box fitted to volumeNode with Crop Volume's "Fit to volume". Never saved with the scene."""
        roiNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsROINode", FILTER_CROP_ROI_NAME)
        roiNode.SetAttribute(FILTER_CROP_ROI_ATTRIBUTE, "1")
        roiNode.SetSaveWithScene(False)
        roiNode.CreateDefaultDisplayNodes()
        self.styleCropRoiDisplayNode(roiNode.GetDisplayNode())
        parametersNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLCropVolumeParametersNode")
        try:
            parametersNode.SetInputVolumeNodeID(volumeNode.GetID())
            parametersNode.SetROINodeID(roiNode.GetID())
            slicer.modules.cropvolume.logic().FitROIToInputVolume(parametersNode)
        finally:
            slicer.mrmlScene.RemoveNode(parametersNode)
        return roiNode

    @staticmethod
    def _hasNonLinearTransform(node):
        transformNode = node.GetParentTransformNode() if node is not None else None
        return transformNode is not None and not transformNode.IsTransformToWorldLinear()

    def cropToRoi(self, volumeNode, roiNode):
        """
        Voxel-based crop of volumeNode to roiNode: the original voxels inside the ROI are copied, nothing is
        interpolated or resampled. Returns a new volume (volumeNode is not changed); the caller removes it when done.

        Slicer's Crop Volume is used when it can do the job. It refuses volumes under a non-linear transform (e.g. a CT
        deformably registered to the PET: "voxel-based cropping of non-linearly transformed input volume is not
        supported"), and Apply() reports that only through its return code (0 = success). In that case, or if it fails
        for any other reason, the crop is done here instead (cropVoxelsUnderTransform).
        """
        if self._hasNonLinearTransform(volumeNode) or self._hasNonLinearTransform(roiNode):
            logging.info(f"EasyFusion: '{volumeNode.GetName()}' is under a non-linear transform; "
                         "cropping it without Crop Volume")
            return self.cropVoxelsUnderTransform(volumeNode, roiNode)

        scene = slicer.mrmlScene
        parametersNode = scene.AddNewNodeByClass("vtkMRMLCropVolumeParametersNode")
        outputNode = None
        try:
            parametersNode.SetInputVolumeNodeID(volumeNode.GetID())
            parametersNode.SetROINodeID(roiNode.GetID())
            parametersNode.SetVoxelBased(True)
            parametersNode.SetIsotropicResampling(False)
            errorCode = slicer.modules.cropvolume.logic().Apply(parametersNode)
            outputID = parametersNode.GetOutputVolumeNodeID()
            outputNode = scene.GetNodeByID(outputID) if outputID else None
        finally:
            scene.RemoveNode(parametersNode)
        if errorCode == 0 and outputNode is not None and outputNode.GetImageData() is not None:
            return outputNode

        logging.warning(f"EasyFusion: Crop Volume failed on '{volumeNode.GetName()}' (error code {errorCode}); "
                        "cropping it without Crop Volume")
        if outputNode is not None and outputNode is not volumeNode and scene.IsNodePresent(outputNode):
            scene.RemoveNode(outputNode)  # empty / half-made output of the failed Crop Volume run
        return self.cropVoxelsUnderTransform(volumeNode, roiNode)

    def cropVoxelsUnderTransform(self, volumeNode, roiNode):
        """
        Voxel-based crop that works whatever transforms volumeNode and roiNode are under, linear or not.
        The ROI box is mapped into the volume's own (untransformed) coordinates through the transforms between the two
        nodes, and the block of original voxels covering it is copied into a new volume that keeps volumeNode's voxel
        grid and parent transform. Under a deformable transform the box is warped in the volume's coordinates, so the
        block is its bounding box there: it covers the whole ROI and, near the edges, slightly more.
        """
        imageData = volumeNode.GetImageData()
        if imageData is None:
            raise RuntimeError(f"'{volumeNode.GetName()}' has no image data.")

        # ROI surface: object coordinates -> ROI node coordinates -> volume node coordinates -> volume IJK
        objectToNode = roiNode.GetObjectToNodeMatrix()
        roiToVolume = vtk.vtkGeneralTransform()
        slicer.vtkMRMLTransformNode.GetTransformBetweenNodes(
            roiNode.GetParentTransformNode(), volumeNode.GetParentTransformNode(), roiToVolume)
        rasToIjk = vtk.vtkMatrix4x4()
        volumeNode.GetRASToIJKMatrix(rasToIjk)
        ijkPoints = []
        for point in boxSurfacePoints(roiNode.GetSize()):
            inRoiNode = objectToNode.MultiplyPoint([float(point[0]), float(point[1]), float(point[2]), 1.0])[:3]
            inVolumeNode = roiToVolume.TransformPoint(inRoiNode)
            ijkPoints.append(rasToIjk.MultiplyPoint(list(inVolumeNode) + [1.0])[:3])

        # One extra voxel all round: the warped box between the sampled points may bulge a little further
        margin = 1 if self._hasNonLinearTransform(volumeNode) or self._hasNonLinearTransform(roiNode) else 0
        block = voxelBlockFromIjkPoints(ijkPoints, imageData.GetDimensions(), margin=margin)
        if block is None:
            raise RuntimeError(f"The crop ROI does not overlap '{volumeNode.GetName()}'. Move the box onto the volume.")
        (i0, j0, k0), (i1, j1, k1) = block
        croppedArray = np.array(slicer.util.arrayFromVolume(volumeNode)[k0:k1, j0:j1, i0:i1])  # copy

        croppedNode = self._addHiddenVolume(f"{volumeNode.GetName()}_cropped")
        try:
            ijkToRas = vtk.vtkMatrix4x4()
            volumeNode.GetIJKToRASMatrix(ijkToRas)
            croppedNode.SetIJKToRASMatrix(ijkToRas)  # same spacing and axes as the original
            croppedNode.SetOrigin(ijkToRas.MultiplyPoint([float(i0), float(j0), float(k0), 1.0])[:3])
            slicer.util.updateVolumeFromArray(croppedNode, croppedArray)
            # Same local coordinates as the original, so it belongs under the same transform (as Crop Volume does)
            croppedNode.SetAndObserveTransformNodeID(volumeNode.GetTransformNodeID())
        except Exception:
            slicer.mrmlScene.RemoveNode(croppedNode)
            raise
        return croppedNode

    @staticmethod
    def _recordProvenance(outputNode, sourceNode, modelPath):
        outputNode.SetAttribute(FILTER_MODEL_ATTRIBUTE, os.path.basename(modelPath))
        outputNode.SetAttribute(FILTER_DATE_ATTRIBUTE, time.strftime("%Y-%m-%d %H:%M:%S"))
        outputNode.SetNodeReferenceID(FILTER_SOURCE_ROLE, sourceNode.GetID())
        # What the source values are (SUV, counts ... or not known): the filtered values are not guessed anew,
        # e.g. a denoised SPECT has decimals and must not be taken for SUV
        info = petValueInfo(sourceNode)
        outputNode.SetAttribute(VALUE_KIND_ATTRIBUTE, info.kind or VALUE_KIND_UNKNOWN)
        if info.modality:
            outputNode.SetAttribute(VALUE_MODALITY_ATTRIBUTE, info.modality)
        # Same quantity and units as the source (e.g. SUVbw, g/ml), so the Data Probe labels values the same way
        for getterName, setterName in (("GetVoxelValueQuantity", "SetVoxelValueQuantity"),
                                       ("GetVoxelValueUnits", "SetVoxelValueUnits")):
            try:
                entry = getattr(sourceNode, getterName)()
                if entry is not None:
                    copied = slicer.vtkCodedEntry()
                    copied.Copy(entry)
                    getattr(outputNode, setterName)(copied)
            except Exception:
                logging.debug(f"EasyFusion: could not copy {getterName} to the filtered volume", exc_info=True)
        # Next to the source in the Data module tree (same patient / study)
        try:
            shNode = slicer.mrmlScene.GetSubjectHierarchyNode()
            parentItem = shNode.GetItemParent(shNode.GetItemByDataNode(sourceNode))
            outputItem = shNode.GetItemByDataNode(outputNode)
            if parentItem and outputItem:
                shNode.SetItemParent(outputItem, parentItem)
        except Exception:
            logging.debug("EasyFusion: could not place the filtered volume in the subject hierarchy", exc_info=True)

    def run(self, sourceNode, secondNode, modelPath, params, outputName, forceCPU=False, report=None,
            originalNode=None):
        """
        Filter sourceNode into a NEW volume called outputName. sourceNode is only read, never modified.
        originalNode: the volume the user selected, when sourceNode is a cropped copy of it ("Limit to ROI");
        the result is tagged with it and placed under its transform.
        secondNode: second input channel of dual-channel models (else None).
        report(text, done=0, total=0): progress callback; it may raise FilterCancelled to stop the run.
        The output volume is added to the scene only once everything worked, so a failed or cancelled run
        leaves nothing behind. Returns (outputNode, device description, seconds).
        """
        import torch
        from monai.inferers import sliding_window_inference

        report = report or (lambda text, done=0, total=0: None)
        originalNode = originalNode or sourceNode
        startTime = time.time()
        scene = slicer.mrmlScene
        scratchNode = None
        model = None
        try:
            report("Loading the model…")
            device, deviceText = self.chooseDevice(torch, forceCPU)
            model = self.loadModel(torch, modelPath, params, 2 if params["dual_channel"] else 1, device)

            # Input on the model's voxel grid. The volume in the scene is never written to: resampling goes into a
            # scratch volume, and without resampling the voxels are copied out before anything else happens.
            if params["dont_resample"]:
                gridNode = sourceNode
            else:
                spacing = params["voxel_spacing"]
                report(f"Resampling to {' × '.join(f'{v:g}' for v in spacing)} mm voxels…")
                scratchNode = self._addHiddenVolume("EasyFusionFilterInput")
                self._runCli(slicer.modules.resamplescalarvolume,
                             {"InputVolume": sourceNode.GetID(), "OutputVolume": scratchNode.GetID(),
                              "outputPixelSpacing": ",".join(f"{float(v):g}" for v in spacing),
                              "interpolationType": "linear"},
                             "Resampling")
                gridNode = scratchNode
            gridArray = slicer.util.arrayFromVolume(gridNode)
            inputDtype = gridArray.dtype
            inputTensor = torch.from_numpy(np.array(gridArray, dtype=np.float32))[None, None]  # (1, 1, K, J, I)
            del gridArray
            networkInput = inputTensor
            if params["dual_channel"]:
                report("Resampling the second input…")
                secondTensor = torch.from_numpy(self.resampleToGrid(secondNode, gridNode))[None, None]
                networkInput = torch.cat([inputTensor, secondTensor], dim=1)
                del secondTensor

            # Windows run on the chosen device; the whole volume and the blending buffers stay in RAM
            roiSize = tuple(params["block_size"])
            total = estimateSlidingWindowCount(inputTensor.shape[2:], roiSize)
            label = f"Filtering on {deviceText}…"
            done = [0]

            def predictor(window, *args, **kwargs):
                report(label, done[0], total)
                prediction = model(window, *args, **kwargs)
                done[0] += 1
                return prediction

            report(label, 0, total)
            with torch.no_grad():
                predictedNoise = sliding_window_inference(
                    inputs=networkInput, roi_size=roiSize, sw_batch_size=1, predictor=predictor,
                    overlap=FILTER_WINDOW_OVERLAP, mode="gaussian", sw_device=device, device=torch.device("cpu"))
            report(label, total, total)
            del networkInput

            # The networks predict the noise: filtered image = input - predicted noise (as in PETDenoise)
            result = (inputTensor - predictedNoise.to(inputTensor.dtype))[0, 0].cpu().numpy()
            del predictedNoise, inputTensor
            if params["prevent_negative"]:
                result = np.clip(result, 0, None)
            result = castFilterResult(result, inputDtype)

            report("Creating the filtered volume…")
            outputNode = scene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", outputName)
            outputNode.CopyOrientation(gridNode)
            slicer.util.updateVolumeFromArray(outputNode, result)
            # Same local coordinates as the original, so it belongs under the same (e.g. registration) transform
            outputNode.SetAndObserveTransformNodeID(originalNode.GetTransformNodeID())
            outputNode.CreateDefaultDisplayNodes()
            self._recordProvenance(outputNode, originalNode, modelPath)
            if originalNode is not sourceNode:
                outputNode.SetAttribute(FILTER_CROPPED_ATTRIBUTE, "1")
            return outputNode, deviceText, time.time() - startTime
        finally:
            if scratchNode is not None and scene.IsNodePresent(scratchNode):
                scene.RemoveNode(scratchNode)
            model = None
            gc.collect()
            try:
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass

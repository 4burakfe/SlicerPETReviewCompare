"""
Time-point registration for the compare module: TP1 (moving) is shifted onto TP2 (fixed), translation only.

Two stages, both on the CT / MR of each time point:
  1. Initial alignment. PET/CT is acquired vertex to mid-thigh or whole body, so the top of the body is matched in
     S, and the body centres are matched in R and A (over the length both scans cover, so a longer scan's legs do
     not pull the centre).
  2. Refinement: Mattes mutual information (works for CT-CT and MR-CT), translation transform, three resolution
     levels on ~4 mm images (SimpleITK, which ships with Slicer). Kept only if it improves the match.

TP1's SPECT/PET follows its CT: the registration transform is placed above the whole transform chain of each TP1
volume, so a PET-to-CT correction that already exists under it is kept.
"""

import logging
import time

import numpy as np

import vtk
import slicer

from .values import (
    looksLikeCT, MRI_BACKGROUND_FRACTION,
)

REGISTRATION_SAMPLE_SPACING_MM = 4.0      # images are subsampled to about this spacing before registering
REGISTRATION_SHRINK_FACTORS = (4, 2, 1)   # resolution levels on the subsampled images (16, 8, 4 mm)
REGISTRATION_SMOOTHING_SIGMAS = (2.0, 1.0, 0.0)  # in voxels of each level
REGISTRATION_HISTOGRAM_BINS = 32
REGISTRATION_SAMPLING_PERCENTAGE = 0.10
REGISTRATION_SAMPLING_SEED = 20260929      # fixed seed: the same images always give the same result
REGISTRATION_LEARNING_RATE_MM = 8.0
REGISTRATION_MIN_STEP_MM = 0.2
REGISTRATION_MAX_ITERATIONS = 150
CT_BODY_THRESHOLD_HU = -500.0             # body (and table) of a CT; air is about -1000 HU
CT_INTENSITY_RANGE = (-1000.0, 2000.0)    # CT values are clamped to this range (metal would stretch the histogram)
# Top / bottom of the body: highest / lowest slab (BODY_SLAB_MM thick) where the body, after thin structures such as
# the table and head holder are eroded away, still has a cross-section of at least BODY_MIN_AREA_MM2. Unlike a
# percentile, this does not depend on how long the scan is.
BODY_SLAB_MM = 4.0
BODY_MIN_AREA_MM2 = 500.0
MIN_BODY_VOXELS = 100
REGISTRATION_TRANSFORM_ATTRIBUTE = "EponaCompare.RegistrationTransform"
REGISTRATION_SHIFT_ATTRIBUTE = "EponaCompare.RegistrationShift"   # "R A S" (mm) of the last automatic registration
REGISTRATION_CENTER_ATTRIBUTE = "EponaCompare.CheckCenter"        # "R A S": middle of the body both scans cover
_RAS_TO_LPS = np.diag([-1.0, -1.0, 1.0])   # its own inverse


# ---------------------------------------------------------------------------
# Pure helpers (numpy only)
# ---------------------------------------------------------------------------

def samplingSteps(ijkToWorld, targetSpacing=REGISTRATION_SAMPLE_SPACING_MM):
    """Integer steps (i, j, k) that bring the voxel spacing close to targetSpacing (never below the original)."""
    spacing = np.linalg.norm(np.asarray(ijkToWorld, dtype=float)[:3, :3], axis=0)
    return tuple(max(1, int(round(targetSpacing / s))) if s > 0 else 1 for s in spacing)


def subsampleVolume(voxels, ijkToWorld, steps):
    """(voxels [k, j, i] taking every step-th voxel, matching IJK-to-world matrix)."""
    si, sj, sk = steps
    matrix = np.array(ijkToWorld, dtype=float)
    matrix[:3, :3] = matrix[:3, :3] * np.array([si, sj, sk], dtype=float)[None, :]
    return np.asarray(voxels)[::sk, ::sj, ::si], matrix


def bodyMask(voxels, isCT):
    """Voxels of the body: above -500 HU for a CT; above 5 % of the 99.5th percentile (from the minimum) otherwise."""
    values = np.asarray(voxels, dtype=float)
    if isCT:
        return values > CT_BODY_THRESHOLD_HU
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return np.zeros(values.shape, dtype=bool)
    minimum = float(finite.min())
    upper = float(np.percentile(finite, 99.5))
    return values > minimum + MRI_BACKGROUND_FRACTION * (upper - minimum)


def erodeInPlane(mask):
    """One step of erosion along i and j (a voxel stays if its four in-plane neighbours are body too). Removes
    structures thinner than about three voxels (~12 mm at 4 mm), e.g. the CT table and head holder shells."""
    mask = np.asarray(mask, dtype=bool)
    eroded = mask.copy()
    eroded[:, :, 1:] &= mask[:, :, :-1]
    eroded[:, :, :-1] &= mask[:, :, 1:]
    eroded[:, 1:, :] &= mask[:, :-1, :]
    eroded[:, :-1, :] &= mask[:, 1:, :]
    eroded[:, :, [0, -1]] = False
    eroded[:, [0, -1], :] = False
    return eroded


def bodyPoints(voxels, ijkToWorld, isCT):
    """(world points N x 3 of the eroded body mask, volume in mm3 that each point stands for)."""
    points = maskWorldPoints(erodeInPlane(bodyMask(voxels, isCT)), ijkToWorld)
    return points, float(abs(np.linalg.det(np.asarray(ijkToWorld, dtype=float)[:3, :3])))


def bodyTopAndBottom(points, pointVolume, slabMm=BODY_SLAB_MM, minAreaMm2=BODY_MIN_AREA_MM2):
    """S of the top and bottom of the body: outer edges of the outermost slabs whose cross-section is large enough."""
    s = points[:, 2]
    low = np.floor(s.min() / slabMm) * slabMm
    counts = np.bincount(((s - low) / slabMm).astype(int))
    area = counts * pointVolume / slabMm
    solid = np.nonzero(area >= minAreaMm2)[0]
    if solid.size == 0:
        return float(s.max()), float(s.min())
    return float(low + (solid[-1] + 1) * slabMm), float(low + solid[0] * slabMm)


def maskWorldPoints(mask, ijkToWorld):
    """World (RAS) coordinates of the True voxels of mask [k, j, i], as an N x 3 array."""
    k, j, i = np.nonzero(mask)
    ijk = np.vstack([i, j, k, np.ones_like(i)]).astype(float)
    return (np.asarray(ijkToWorld, dtype=float) @ ijk)[:3].T


def initialTranslation(movingPoints, fixedPoints, movingPointVolume=1.0, fixedPointVolume=1.0):
    """
    Shift (R, A, S in mm) that moves the moving body onto the fixed one: tops matched in S (bodyTopAndBottom), and
    the median R and A of the body matched over the length both scans cover (measured down from the top).
    Points come from bodyPoints (eroded body mask, so the table does not count), with the volume each stands for.
    Returns (shift, info) with info = {"fixedCenter": (R, A, S) of the common part of the fixed body,
    "commonLength": mm}. Raises ValueError when either image shows no body.
    """
    for points, which in ((movingPoints, "TP1"), (fixedPoints, "TP2")):
        if len(points) < MIN_BODY_VOXELS:
            raise ValueError(f"No body found in the {which} CT/MR image.")
    topMoving, bottomMoving = bodyTopAndBottom(movingPoints, movingPointVolume)
    topFixed, bottomFixed = bodyTopAndBottom(fixedPoints, fixedPointVolume)
    length = max(1.0, min(topMoving - bottomMoving, topFixed - bottomFixed))
    movingZone = movingPoints[movingPoints[:, 2] >= topMoving - length]
    fixedZone = fixedPoints[fixedPoints[:, 2] >= topFixed - length]
    movingCenter = np.median(movingZone[:, :2], axis=0)
    fixedCenter = np.median(fixedZone[:, :2], axis=0)
    shift = np.array([fixedCenter[0] - movingCenter[0], fixedCenter[1] - movingCenter[1], topFixed - topMoving])
    info = {"fixedCenter": (float(fixedCenter[0]), float(fixedCenter[1]), float(topFixed - length / 2.0)),
            "commonLength": float(length)}
    return shift, info


def rasToLps(vector):
    return _RAS_TO_LPS @ np.asarray(vector, dtype=float)


def lpsToRas(vector):
    return _RAS_TO_LPS @ np.asarray(vector, dtype=float)


def translationMatrix(shift):
    matrix = np.eye(4)
    matrix[:3, 3] = shift
    return matrix


# ---------------------------------------------------------------------------
# Registration on arrays (numpy + SimpleITK, no scene)
# ---------------------------------------------------------------------------

def sitkImage(voxels, ijkToWorld, intensityRange=None):
    """SimpleITK image (float32, LPS physical space) of voxels [k, j, i] placed by an IJK-to-world (RAS) matrix."""
    import SimpleITK as sitk
    array = np.asarray(voxels, dtype=np.float32)
    if intensityRange is not None:
        array = np.clip(array, intensityRange[0], intensityRange[1])
    image = sitk.GetImageFromArray(np.ascontiguousarray(array))
    matrix = np.asarray(ijkToWorld, dtype=float)
    spacing = np.linalg.norm(matrix[:3, :3], axis=0)
    direction = _RAS_TO_LPS @ (matrix[:3, :3] / spacing[None, :])
    image.SetSpacing([float(v) for v in spacing])
    image.SetOrigin([float(v) for v in rasToLps(matrix[:3, 3])])
    image.SetDirection([float(v) for v in direction.flatten()])
    return image


def refineTranslation(fixedImage, movingImage, initialShift, progress=None):
    """
    Mutual-information translation registration starting from initialShift (moving -> fixed, RAS mm).
    Returns (shift, info): the refined shift, or initialShift when refining did not improve the match
    (info["refined"] False). progress(text) is called on every iteration.
    """
    import SimpleITK as sitk

    def makeMethod():
        method = sitk.ImageRegistrationMethod()
        method.SetMetricAsMattesMutualInformation(numberOfHistogramBins=REGISTRATION_HISTOGRAM_BINS)
        method.SetMetricSamplingStrategy(method.RANDOM)
        method.SetMetricSamplingPercentage(REGISTRATION_SAMPLING_PERCENTAGE, REGISTRATION_SAMPLING_SEED)
        method.SetInterpolator(sitk.sitkLinear)
        return method

    # SimpleITK transforms map fixed-image points to moving-image points: the opposite of the Slicer shift
    initial = sitk.TranslationTransform(3, [float(v) for v in rasToLps(-np.asarray(initialShift, dtype=float))])
    evaluator = makeMethod()
    evaluator.SetInitialTransform(initial)
    initialMetric = evaluator.MetricEvaluate(fixedImage, movingImage)

    method = makeMethod()
    method.SetOptimizerAsRegularStepGradientDescent(
        learningRate=REGISTRATION_LEARNING_RATE_MM, minStep=REGISTRATION_MIN_STEP_MM,
        numberOfIterations=REGISTRATION_MAX_ITERATIONS, gradientMagnitudeTolerance=1e-8)
    method.SetOptimizerScalesFromPhysicalShift()
    method.SetShrinkFactorsPerLevel(list(REGISTRATION_SHRINK_FACTORS))
    method.SetSmoothingSigmasPerLevel(list(REGISTRATION_SMOOTHING_SIGMAS))
    method.SmoothingSigmasAreSpecifiedInPhysicalUnitsOff()
    transform = sitk.TranslationTransform(3, initial.GetOffset())
    method.SetInitialTransform(transform, inPlace=True)
    if progress is not None:
        levels = len(REGISTRATION_SHRINK_FACTORS)
        method.AddCommand(sitk.sitkIterationEvent, lambda: progress(
            f"Level {method.GetCurrentLevel() + 1} of {levels}, iteration {method.GetOptimizerIteration()}"))
    method.Execute(fixedImage, movingImage)

    evaluator = makeMethod()
    evaluator.SetInitialTransform(sitk.TranslationTransform(3, transform.GetOffset()))
    finalMetric = evaluator.MetricEvaluate(fixedImage, movingImage)
    refined = finalMetric < initialMetric  # mutual information metric: lower (more negative) is better
    shift = lpsToRas(-np.asarray(transform.GetOffset(), dtype=float)) if refined else np.asarray(initialShift, float)
    return shift, {"refined": bool(refined), "initialMetric": float(initialMetric), "finalMetric": float(finalMetric),
                   "stopCondition": method.GetOptimizerStopConditionDescription()}


def registerArrays(movingVoxels, movingIjkToWorld, fixedVoxels, fixedIjkToWorld, progress=None):
    """
    Translation (R, A, S mm) that moves the moving image onto the fixed one: initial alignment, then refinement.
    Voxels are [k, j, i] arrays, matrices map IJK to world RAS. Returns a dict with shift, initialShift,
    fixedCenter, commonLength, refined, metrics and seconds.
    """
    start = time.time()
    sampled = []
    for voxels, matrix in ((movingVoxels, movingIjkToWorld), (fixedVoxels, fixedIjkToWorld)):
        smallVoxels, smallMatrix = subsampleVolume(voxels, matrix, samplingSteps(matrix))
        finite = smallVoxels[np.isfinite(smallVoxels)] if smallVoxels.dtype.kind == "f" else smallVoxels
        isCT = finite.size > 0 and looksLikeCT(float(finite.min()))
        sampled.append((smallVoxels, smallMatrix, isCT))
    (movingSmall, movingMatrix, movingIsCT), (fixedSmall, fixedMatrix, fixedIsCT) = sampled

    if progress is not None:
        progress("Initial alignment (top of the body and body centre)")
    movingPoints, movingPointVolume = bodyPoints(movingSmall, movingMatrix, movingIsCT)
    fixedPoints, fixedPointVolume = bodyPoints(fixedSmall, fixedMatrix, fixedIsCT)
    initialShift, info = initialTranslation(movingPoints, fixedPoints, movingPointVolume, fixedPointVolume)
    fixedImage = sitkImage(fixedSmall, fixedMatrix, CT_INTENSITY_RANGE if fixedIsCT else None)
    movingImage = sitkImage(movingSmall, movingMatrix, CT_INTENSITY_RANGE if movingIsCT else None)
    shift, refineInfo = refineTranslation(fixedImage, movingImage, initialShift, progress)
    result = {"shift": np.asarray(shift, dtype=float), "initialShift": initialShift,
              "movingIsCT": movingIsCT, "fixedIsCT": fixedIsCT, "seconds": time.time() - start}
    result.update(info)
    result.update(refineInfo)
    return result


# ---------------------------------------------------------------------------
# Slicer nodes and transforms
# ---------------------------------------------------------------------------

def _vtkToNumpy(matrix):
    return np.array([[matrix.GetElement(r, c) for c in range(4)] for r in range(4)])


def _numpyToVtk(array):
    matrix = vtk.vtkMatrix4x4()
    for r in range(4):
        for c in range(4):
            matrix.SetElement(r, c, float(array[r][c]))
    return matrix


def transformChain(node):
    """Transform nodes above a node, nearest first."""
    chain = []
    current = node.GetParentTransformNode() if node is not None else None
    while current is not None and current not in chain:
        chain.append(current)
        current = current.GetParentTransformNode()
    return chain


def volumeIjkToWorld(volumeNode):
    """IJK-to-world matrix of a volume including its (linear) transforms. ValueError for non-linear transforms."""
    ijkToRas = vtk.vtkMatrix4x4()
    volumeNode.GetIJKToRASMatrix(ijkToRas)
    parent = volumeNode.GetParentTransformNode()
    toWorld = np.eye(4)
    if parent is not None:
        if not parent.IsTransformToWorldLinear():
            raise ValueError(f"'{volumeNode.GetName()}' is under a non-linear (warping) transform. Harden it first "
                             "(Data module: right-click the transform, 'Harden transform').")
        matrix = vtk.vtkMatrix4x4()
        parent.GetMatrixTransformToWorld(matrix)
        toWorld = _vtkToNumpy(matrix)
    return toWorld @ _vtkToNumpy(ijkToRas)


def attachUnderTransform(volumeNode, transformNode, fixedNodes=()):
    """
    Move a volume with transformNode while keeping its own transforms: the volume itself goes under transformNode
    if it has none, else the top of its transform chain does. ValueError if that chain is shared with a fixed
    (TP2) volume, which would then move too.
    """
    chain = transformChain(volumeNode)
    if transformNode in chain:
        return
    if not chain:
        volumeNode.SetAndObserveTransformNodeID(transformNode.GetID())
        return
    top = chain[-1]
    for fixedNode in fixedNodes:
        if fixedNode is not None and top in transformChain(fixedNode):
            raise ValueError(f"'{volumeNode.GetName()}' and '{fixedNode.GetName()}' share the transform "
                             f"'{top.GetName()}', so TP2 would move with TP1. Harden or remove it first (Data module).")
    top.SetAndObserveTransformNodeID(transformNode.GetID())


def setTranslation(transformNode, shift):
    """Make the transform a pure translation (any rotation is removed)."""
    transformNode.SetMatrixTransformToParent(_numpyToVtk(translationMatrix(shift)))


def transformTranslation(transformNode):
    """Translation part (R, A, S mm) of a linear transform's matrix to parent."""
    matrix = vtk.vtkMatrix4x4()
    transformNode.GetMatrixTransformToParent(matrix)
    return _vtkToNumpy(matrix)[:3, 3]


def setTransformTranslation(transformNode, shift):
    """Change only the translation of a linear transform (a rotation set elsewhere, e.g. Transforms module, stays)."""
    matrix = vtk.vtkMatrix4x4()
    transformNode.GetMatrixTransformToParent(matrix)
    array = _vtkToNumpy(matrix)
    if np.allclose(array[:3, 3], shift, atol=1e-6):
        return
    array[:3, 3] = shift
    transformNode.SetMatrixTransformToParent(_numpyToVtk(array))


def hasRotation(transformNode, tolerance=1e-6):
    matrix = vtk.vtkMatrix4x4()
    transformNode.GetMatrixTransformToParent(matrix)
    return not np.allclose(_vtkToNumpy(matrix)[:3, :3], np.eye(3), atol=tolerance)


def parseShift(text):
    """(R, A, S) from "R A S" text (as stored in the transform attributes), or None."""
    try:
        values = [float(v) for v in (text or "").split()]
    except ValueError:
        return None
    return np.array(values) if len(values) == 3 and all(np.isfinite(values)) else None


def shiftSliderRange(value, anchor, halfRange):
    """(minimum, maximum) of a shift slider: anchor +- halfRange, re-centred on value if it falls outside."""
    center = anchor if abs(value - anchor) <= halfRange else round(value)
    return center - halfRange, center + halfRange


def registerTimePoints(movingVolume, fixedVolume, progress=None):
    """registerArrays for two volume nodes (each placed by its own linear transforms)."""
    return registerArrays(slicer.util.arrayFromVolume(movingVolume), volumeIjkToWorld(movingVolume),
                          slicer.util.arrayFromVolume(fixedVolume), volumeIjkToWorld(fixedVolume), progress)


def formatShift(shift):
    """ "R 12.0, A -7.5, S 25.3 mm" (the direction TP1 was moved)."""
    return ", ".join(f"{axis} {value:+.1f}" for axis, value in zip("RAS", shift)) + " mm"


def logRegistration(result):
    logging.info(f"EponaCompare: registration {'refined' if result['refined'] else 'initial only'}: "
                 f"initial {formatShift(result['initialShift'])}, final {formatShift(result['shift'])}, "
                 f"metric {result['initialMetric']:.4f} -> {result['finalMetric']:.4f}, "
                 f"{result['seconds']:.1f} s ({result['stopCondition']})")

"""
Unit tests of the EponaLib helpers that do not need a scene: value detection, window math, ROI statistics and
formatting, model sidecars / catalog / downloads, layout XML (Epona and compare), time point registration (on synthetic
CTs), imaging time formatting, the shared keyboard shortcut dispatch, ROI set bookkeeping and the
module toolbar logic (with stand-ins for Qt).

In Slicer: the module's self test runs them ("Reload and Test" in developer mode, or ctest).
Outside Slicer (numpy needed; Slicer / Qt / VTK are replaced by stand-ins):
    cd Easy_fusion && python -m unittest EponaLib.tests -v
"""

import os
import sys
import hashlib
import pathlib
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ElementTree

try:
    import slicer  # noqa: F401  (inside Slicer)
except ImportError:  # plain Python: stand-ins so the modules import; only scene-free helpers are tested
    from unittest import mock
    for _name in ("qt", "ctk", "vtk", "slicer", "slicer.util"):
        sys.modules.setdefault(_name, mock.MagicMock())

    class _ObservationMixinStandIn:  # a real base class (a mock cannot be subclassed usefully)
        def __init__(self):
            pass

        def addObserver(self, *args, **kwargs):
            pass

        def removeObserver(self, *args, **kwargs):
            pass

        def removeObservers(self, *args, **kwargs):
            pass

    sys.modules["slicer.util"].VTKObservationMixin = _ObservationMixinStandIn

import numpy as np

from . import display, filters, layouts, registration, roi, roiset, shortcuts, studyinfo, toolbar, values


def _syntheticVolumes(seed=0):
    """Voxel samples like an SUV PET, the same PET in Bq/mL, a SPECT in counts and a low-count filtered SPECT."""
    rng = np.random.default_rng(seed)
    body = rng.gamma(2.0, 0.6, 200000)          # background SUV around 1.2
    lesions = rng.uniform(5, 25, 300)
    suv = np.concatenate([np.zeros(300000), body, lesions]).astype(np.float32)
    lowCounts = rng.poisson(np.concatenate([np.zeros(300000), body * 2, lesions * 2])).astype(np.float32)
    return {
        "suv": suv,
        "bqml": suv * 5300.0,
        "counts": rng.poisson(np.concatenate([np.zeros(300000), body * 20, lesions * 20])).astype(np.int16),
        "filteredLowCounts": (lowCounts + rng.normal(0, 0.3, lowCounts.size)).clip(0).astype(np.float32),
    }


class PetValueKindTest(unittest.TestCase):
    """classifyPetValues: SUV / Bq/mL / counts only when the evidence is sure."""

    @classmethod
    def setUpClass(cls):
        cls.stats = {name: values.summarizePetValues(v) for name, v in _syntheticVolumes().items()}

    def kind(self, **kwargs):
        return values.classifyPetValues(**kwargs).kind

    def test_metadata(self):
        s = self.stats
        self.assertEqual(self.kind(unitTexts=["{SUVbw}g/ml", "Standardized Uptake Value body weight"],
                                   stats=s["suv"]), values.VALUE_KIND_SUV)
        self.assertEqual(self.kind(dicomModality="PT", dicomUnits="BQML", stats=s["bqml"]), values.VALUE_KIND_BQML)
        self.assertEqual(self.kind(dicomModality="PT", dicomUnits="GML", stats=s["suv"]), values.VALUE_KIND_SUV)
        self.assertEqual(self.kind(dicomModality="NM", stats=s["counts"]), values.VALUE_KIND_COUNTS)
        # Loaded through the PET DICOM extension: the units win over the (Bq/mL) DICOM header
        self.assertEqual(self.kind(unitTexts=["{SUVbw}g/ml"], dicomModality="PT", dicomUnits="BQML",
                                   suvConverted=True, stats=s["suv"]), values.VALUE_KIND_SUV)

    def test_contradictions_are_rejected(self):
        self.assertIsNone(self.kind(unitTexts=["{SUVbw}g/ml"], stats=self.stats["bqml"]))
        self.assertIsNone(self.kind(dicomModality="PT", dicomUnits="BQML", stats=self.stats["suv"]))
        self.assertIsNone(self.kind(name="PET SUV", stats=self.stats["bqml"]))

    def test_values_alone(self):
        s = self.stats
        self.assertEqual(self.kind(stats=s["suv"], name="PET_AC"), values.VALUE_KIND_SUV)
        self.assertEqual(self.kind(name="Patient (SUVbw)", stats=values.summarizePetValues(
            np.round(_syntheticVolumes()["suv"]))), values.VALUE_KIND_SUV)
        # Bq/mL and counts are never guessed from values; SPECT is never taken for SUV
        self.assertIsNone(self.kind(stats=s["bqml"], name="pet"))
        self.assertIsNone(self.kind(stats=s["counts"], name="spect_bone"))
        self.assertIsNone(self.kind(stats=s["filteredLowCounts"], name="SPECT recon"))
        self.assertIsNone(self.kind(stats=None))

    def test_filtered_volumes_inherit(self):
        s = self.stats
        self.assertEqual(self.kind(storedKind=values.VALUE_KIND_SUV, stats=s["suv"]), values.VALUE_KIND_SUV)
        info = values.classifyPetValues(storedKind=values.VALUE_KIND_UNKNOWN, storedModality="SPECT",
                                        stats=s["filteredLowCounts"], name="x_pet_denoiser")
        self.assertIsNone(info.kind)
        self.assertEqual(info.modality, "SPECT")

    def test_units_and_windows(self):
        suv = values.classifyPetValues(stats=self.stats["suv"])
        counts = values.classifyPetValues(dicomModality="NM", stats=self.stats["counts"])
        unknown = values.classifyPetValues()
        self.assertEqual(values.valueKindUnit(suv.kind), "SUV")
        self.assertEqual(values.valueKindUnit(None), "")
        self.assertEqual(values.initialPetRange(suv, 25.0), (0.0, values.INITIAL_SUV_UPPER))
        self.assertEqual(values.initialPetRange(counts, 534.0), (0.0, 534.0))
        self.assertEqual(values.initialPetRange(unknown, 0.0), (0.0, 1.0))
        self.assertTrue(values.sameValueScale(suv, 25, suv, 8))
        self.assertFalse(values.sameValueScale(suv, 25, counts, 500))
        self.assertTrue(values.sameValueScale(counts, 500, counts, 700))
        self.assertFalse(values.sameValueScale(unknown, 500, unknown, 5000))


class WindowHelpersTest(unittest.TestCase):

    def test_percent_of_maximum(self):
        self.assertEqual(values.percentOfMaximum(200, 25), 50)
        self.assertIsNone(values.percentOfMaximum(0, 50))
        self.assertIsNone(values.percentOfMaximum(float("nan"), 50))

    def test_ct_detection_and_window_text(self):
        self.assertTrue(values.looksLikeCT(-1024))
        self.assertFalse(values.looksLikeCT(0))
        self.assertEqual(values.formatWindowInfoLine("CT", 400, 50), "CT  W 400  L 50")
        self.assertEqual(values.formatWindowInfoLine("PET", 10, 5, "SUV"), "PET  SUV 0–10")
        self.assertEqual(values.formatWindowInfoLine("SPECT/PET", 10, 5), "SPECT/PET  0–10")

    def test_shortcut_presets(self):
        self.assertEqual(values.windowPresetForView("slice", "EFAxialCT", "F5")[0], "ct")
        preset = values.windowPresetForView("threeD", None, "F7")
        self.assertEqual(preset[0], "pet")
        self.assertEqual(preset[2], 10.0)
        self.assertIsNone(values.windowPresetForView(None, None, "F5"))

    def test_percentile_window(self):
        window, level = values.percentileWindow(np.arange(101, dtype=float), 10, 90)
        self.assertAlmostEqual(window, 80.0)
        self.assertAlmostEqual(level, 50.0)
        self.assertIsNone(values.percentileWindow(np.ones(10), 10, 90))


class RoiTest(unittest.TestCase):

    def test_sphere_statistics(self):
        voxels = np.zeros((21, 21, 21), dtype=np.float32)
        voxels[10, 10, 10] = 10.0
        voxels[10, 10, 11] = 5.0
        voxels[10, 10, 12] = 3.0
        stats = roi.sphereStatisticsFromArray(voxels, np.eye(4), (10, 10, 10), 3.0,
                                              roi.THRESHOLD_RELATIVE, 40.0)
        self.assertEqual(stats["max"], 10.0)
        self.assertEqual(stats["segVoxels"], 2)          # 10 and 5 are >= 40 % of 10; 3 is not
        self.assertAlmostEqual(stats["segMean"], 7.5)
        absolute = roi.sphereStatisticsFromArray(voxels, np.eye(4), (10, 10, 10), 3.0, roi.THRESHOLD_ABSOLUTE, 2.5)
        self.assertEqual(absolute["segVoxels"], 3)
        self.assertIsNone(roi.sphereStatisticsFromArray(voxels, np.eye(4), (100, 100, 100), 3.0))

    def test_threshold_texts(self):
        self.assertEqual(roi.parseRoiThreshold(roi.formatRoiThreshold(roi.THRESHOLD_ABSOLUTE, 2.5)),
                         (roi.THRESHOLD_ABSOLUTE, 2.5))
        self.assertIsNone(roi.parseRoiThreshold("absolute -1"))
        self.assertEqual(roi.describeRoiThreshold(roi.THRESHOLD_ABSOLUTE, 2.5, "SUV"), "2.5 SUV")
        self.assertEqual(roi.describeRoiThreshold(roi.THRESHOLD_ABSOLUTE, 2.5), "2.5")
        self.assertEqual(roi.describeRoiThreshold(roi.THRESHOLD_RELATIVE, 40), "40%")

    def test_tsv_columns_follow_the_unit(self):
        stats = {"max": 5.0, "mean": 2.0, "volumeMl": 4.1, "segMean": 3.0, "mtvMl": 1.0, "tlg": 3.0,
                 "segVoxels": 5, "threshold": 2.0}
        rows = [("p1", "ROI-1", 10.0, stats, (roi.THRESHOLD_RELATIVE, 40), None)]
        suvHeader, suvRow = roi.formatRoiTableTsv(rows, {}, "pet", "", unit="SUV").splitlines()
        self.assertIn("SUVmax", suvHeader.split("\t"))
        self.assertEqual(suvRow.split("\t")[-1], "SUV")
        header, row = roi.formatRoiTableTsv(rows, {}, "pet", "").splitlines()
        self.assertNotIn("SUV", header)
        self.assertEqual(len(header.split("\t")), len(row.split("\t")))
        self.assertEqual(row.split("\t")[-1], "unknown")
        self.assertEqual(roi.defaultRoiExportFileName("PT 1", "T", isSuv=False), "PT_1_ROIs_T.tsv")
        self.assertEqual(roi.defaultRoiExportFileName("PT 1", "T"), "PT_1_SUV_ROIs_T.tsv")

    def test_label(self):
        label = roi.formatRoiLabel("ROI-1", {"max": 5.0, "segMean": None}, True, ("name", "max", "mean", "threshold"),
                                   10, (roi.THRESHOLD_ABSOLUTE, 2.5), unit="counts")
        self.assertEqual(label.splitlines(), ["ROI-1", "Max 5.00", "Mean -", "Thr 2.5 counts"])
        self.assertEqual(roi.formatRoiLabel("ROI-1", None, False, ("max",)), "(no PET)")


class FilterModelTest(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_sidecar(self):
        params, notes = filters.parseFilterMetadata(
            "dual_channel:false\narchitecture:SwinUNETR\nvoxel_spacing:[2,2,2]\nblock_size:(64,64,64)\n"
            "SUVmax Bias: -0.28 g/mL\n")
        self.assertEqual(params["voxel_spacing"], (2.0, 2.0, 2.0))
        self.assertFalse(params["dont_resample"])
        self.assertEqual(filters.filterSuvNotes(notes), ["SUVmax Bias: -0.28 g/mL"])
        self.assertEqual(filters.guessFilterTarget("CT_superres24.pth", params), filters.FILTER_TARGET_CT)
        self.assertEqual(filters.guessFilterTarget("pet_denoiser_x14w3.pth", params), filters.FILTER_TARGET_PET)

    def test_catalog_merge(self):
        builtIn = filters.mergePublishedModelCatalog()
        self.assertEqual(len(builtIn), len(filters.FILTER_PUBLISHED_MODELS))
        release = {"assets": [
            {"name": "pet_denoiser_std_char.pth", "size": 80995179, "browser_download_url": "u1"},
            {"name": "pet_denoiser_std_char.txt", "size": 676, "browser_download_url": "u1t"},
            {"name": "pet_denoiser_x14w3.pth", "size": 123, "browser_download_url": "u2"},
            {"name": "new_model.pth", "size": 55, "browser_download_url": "u3", "digest": "sha256:ABCDEF"},
        ]}
        merged = filters.mergePublishedModelCatalog({"Models": filters.parseReleaseAssets(release)})
        self.assertNotIn("pet_denoiser_x19w1_5.pth", merged)               # removed from the release
        self.assertIsNone(merged["pet_denoiser_x14w3.pth"]["sha256"])      # replaced: old checksum dropped
        self.assertTrue(merged["pet_denoiser_std_char.pth"]["sha256"])     # unchanged: checksum kept
        self.assertEqual(merged["new_model.pth"]["sha256"], "abcdef")
        self.assertIn("CT_superres24.pth", merged)                         # other release not read: kept

    def test_listing_uses_own_copies(self):
        catalog = {"a.pth": {"name": "a.pth", "tag": "Models", "kind": "Denoising", "size": 4, "sha256": None,
                             "url": "", "sidecarUrl": None}}
        own = os.path.join(self.folder, "own")
        os.makedirs(own)
        pathlib.Path(own, "a.pth").write_bytes(b"1234")
        pathlib.Path(own, "mine.pth").write_bytes(b"x")
        entries = {e["key"]: e for e in filters.listFilterModels(catalog, os.path.join(self.folder, "cache"), own)}
        self.assertTrue(entries["published/a.pth"]["available"])
        self.assertTrue(entries["published/a.pth"]["path"].startswith(own))
        self.assertIn("custom/mine.pth", entries)
        self.assertNotIn("custom/a.pth", entries)

    def test_download_checks_and_cancel(self):
        source = os.path.join(self.folder, "model.bin")
        data = os.urandom(3 * filters.FILTER_DOWNLOAD_CHUNK_BYTES + 17)
        pathlib.Path(source).write_bytes(data)
        url = pathlib.Path(source).as_uri()
        target = os.path.join(self.folder, "cache", "model.pth")
        logic = filters.Easy_fusionFilterLogic
        logic.downloadFile(url, target, len(data), hashlib.sha256(data).hexdigest())
        self.assertEqual(pathlib.Path(target).read_bytes(), data)
        with self.assertRaises(OSError):
            logic.downloadFile(url, target + "2", len(data), "00" * 32)

        def cancel(text, done, total, detail):
            if done > 0:
                raise filters.FilterCancelled()
        with self.assertRaises(filters.FilterCancelled):
            logic.downloadFile(url, target + "3", len(data), None, report=cancel)
        leftovers = [n for n in os.listdir(os.path.dirname(target)) if n != "model.pth"]
        self.assertEqual(leftovers, [])  # no partial files

    def test_job_size(self):
        params = dict(filters.FILTER_DEFAULT_PARAMETERS, voxel_spacing=(2.0, 2.0, 2.0), dont_resample=False,
                      block_size=(64, 64, 64))
        job = filters.filterJobSize((200, 200, 300), (4.0, 4.0, 2.0), params)
        self.assertEqual(job["shape"], (300, 400, 400))
        self.assertGreater(job["windows"], 0)


class LayoutTest(unittest.TestCase):

    def test_layout_xml(self):
        for singleScreenSafe in (False, True):
            descriptions = layouts.buildLayoutDescriptions(singleScreenSafe)
            for layoutID in (layouts.LAYOUT_AXIAL_FOUR_UP_ID, layouts.LAYOUT_TWO_BY_TWO_ID,
                             layouts.LAYOUT_CT_FUSION_PET_3D_ID) + layouts.DUAL_MONITOR_LAYOUT_IDS:
                ElementTree.fromstring(descriptions[layoutID])  # well-formed XML
        self.assertIn(layouts.GO_DEFAULT_LAYOUT_ID, layouts.CUSTOM_LAYOUT_IDS)
        self.assertIn(layouts.LAYOUT_FOUR_UP_ID, layouts.EASYFUSION_LAYOUT_IDS)

    def test_field_of_view(self):
        self.assertEqual(layouts.fieldOfViewForTarget((300.0, 200.0, 1.0), (600, 300)), (300.0, 150.0, 1.0))
        self.assertIsNone(layouts.fieldOfViewForTarget((300.0, 200.0, 1.0), (0, 300)))


def _phantomCt(shape, spacing, origin, bodyShift=(0.0, 0.0, 0.0), seed=0):
    """
    Small CT-like body [k, j, i] on an axis-aligned RAS grid: head with skull, neck, trunk with lungs and spine,
    legs, and a CT table on every slice. bodyShift moves the anatomy (mm, RAS). Returns (voxels, IJK-to-RAS).
    """
    rng = np.random.default_rng(seed)
    nk, nj, ni = shape
    r = origin[0] + spacing[0] * np.arange(ni) - bodyShift[0]
    a = origin[1] + spacing[1] * np.arange(nj) - bodyShift[1]
    s = origin[2] + spacing[2] * np.arange(nk) - bodyShift[2]
    S, A, R = np.meshgrid(s, a, r, indexing="ij")
    A = A - 20.0
    voxels = np.full(shape, -1000.0, dtype=np.float32)
    head = R ** 2 + A ** 2 + (S + 90) ** 2 <= 95 ** 2
    body = (head | (((R / 60) ** 2 + (A / 60) ** 2 <= 1) & (S >= -250) & (S < -180))
            | (((R / 170) ** 2 + (A / 110) ** 2 <= 1) & (S < -250) & (S > -1000))
            | ((((R - 90) / 70) ** 2 + (A / 70) ** 2 <= 1) | (((R + 90) / 70) ** 2 + (A / 70) ** 2 <= 1)) & (S <= -1000))
    voxels[body] = 40.0
    voxels[(((np.abs(R) - 80) / 60) ** 2 + (A / 70) ** 2 + ((S + 400) / 130) ** 2) <= 1] = -800.0
    voxels[(R ** 2 + (A + 70) ** 2 <= 15 ** 2) & (S < -180) & (S > -1000)] = 700.0
    voxels[head & (R ** 2 + A ** 2 + (S + 90) ** 2 >= 85 ** 2)] = 900.0
    voxels[(np.abs(A + 125) <= 4) & (np.abs(R) <= 230)] = 200.0  # table, also above the head
    voxels += rng.normal(0, 15.0, shape).astype(np.float32)
    matrix = np.diag([spacing[0], spacing[1], spacing[2], 1.0])
    matrix[:3, 3] = origin
    return voxels, matrix


class RegistrationTest(unittest.TestCase):
    """Time point 1 -> 2 translation: initial alignment (numpy) and refinement (SimpleITK, when available)."""

    @classmethod
    def setUpClass(cls):
        # Time point 2: whole body, 4 x 4 x 4 mm. Time point 1: vertex to thigh, other grid, anatomy shifted.
        cls.fixed = _phantomCt((440, 75, 125), (4.0, 4.0, 4.0), (-250.0, -150.0, -1750.0), seed=1)
        cls.trueShift = np.array([12.0, -7.0, 25.0])  # TP1 anatomy position relative to TP2
        cls.moving = _phantomCt((300, 60, 100), (5.0, 5.0, 4.0), (-280.0, -120.0, -1150.0 + 25.0),
                                bodyShift=cls.trueShift, seed=2)

    def test_sampling(self):
        matrix = np.diag([0.98, 0.98, 3.0, 1.0])
        self.assertEqual(registration.samplingSteps(matrix), (4, 4, 1))
        voxels = np.zeros((10, 20, 30))
        small, smallMatrix = registration.subsampleVolume(voxels, matrix, (4, 4, 1))
        self.assertEqual(small.shape, (10, 5, 8))
        np.testing.assert_allclose(np.diag(smallMatrix)[:3], (3.92, 3.92, 3.0))

    def test_table_is_not_body(self):
        mask = np.zeros((5, 40, 40), dtype=bool)
        mask[:, 10:30, 10:30] = True   # body
        mask[:, 35:37, :] = True       # thin table
        eroded = registration.erodeInPlane(mask)
        self.assertFalse(eroded[:, 35:37, :].any())
        self.assertTrue(eroded[:, 15:25, 15:25].all())

    def test_initial_alignment(self):
        movingPoints, movingVolume = registration.bodyPoints(self.moving[0], self.moving[1], True)
        fixedPoints, fixedVolume = registration.bodyPoints(self.fixed[0], self.fixed[1], True)
        shift, info = registration.initialTranslation(movingPoints, fixedPoints, movingVolume, fixedVolume)
        np.testing.assert_allclose(shift, -self.trueShift, atol=6.0)  # within about a voxel or two
        self.assertGreater(info["commonLength"], 1000.0)
        with self.assertRaises(ValueError):
            registration.initialTranslation(np.zeros((5, 3)), fixedPoints)

    def test_refinement(self):
        try:
            import SimpleITK  # noqa: F401
        except ImportError:
            self.skipTest("SimpleITK is not available")
        result = registration.registerArrays(self.moving[0], self.moving[1], self.fixed[0], self.fixed[1])
        np.testing.assert_allclose(result["shift"], -self.trueShift, atol=2.0)
        self.assertLessEqual(result["finalMetric"], result["initialMetric"] + 1e-9)

    def test_manual_adjustment_helpers(self):
        self.assertEqual(registration.shiftSliderRange(12.0, 10.0, 50.0), (-40.0, 60.0))   # within: anchored
        self.assertEqual(registration.shiftSliderRange(75.4, 10.0, 50.0), (25.0, 125.0))   # outside: re-centred
        np.testing.assert_allclose(registration.parseShift("1.5 -2 30"), (1.5, -2.0, 30.0))
        self.assertIsNone(registration.parseShift(""))
        self.assertIsNone(registration.parseShift("1 2"))
        self.assertIsNone(registration.parseShift("1 nan 2"))

    def test_ras_lps(self):
        np.testing.assert_allclose(registration.rasToLps((1.0, 2.0, 3.0)), (-1.0, -2.0, 3.0))
        np.testing.assert_allclose(registration.translationMatrix((1, 2, 3))[:3, 3], (1, 2, 3))
        self.assertEqual(registration.formatShift((1.0, -2.25, 0.0)), "R +1.0, A -2.2, S +0.0 mm")


class CompareLayoutTest(unittest.TestCase):

    @staticmethod
    def _viewNames(xmlText):
        return [item.get("name") or item.get("singletontag")
                for item in ElementTree.fromstring(xmlText).iter("view")]

    def test_compare_layout_xml(self):
        for singleScreenSafe in (False, True):
            descriptions = layouts.buildCompareLayoutDescriptions(singleScreenSafe)
            self.assertEqual(set(descriptions), set(layouts.COMPARE_LAYOUT_IDS))
            for layoutID, xmlText in descriptions.items():
                names = self._viewNames(xmlText)
                dualMonitor = layoutID in layouts.COMPARE_DUAL_MONITOR_LAYOUT_IDS and not singleScreenSafe
                expected = 12 if dualMonitor else 6                   # 3 columns x 2 time points (per monitor)
                self.assertEqual(len(names), expected, layoutID)
                self.assertEqual(len(set(names)), expected, layoutID)
                self.assertTrue(all(layouts.isCompareView(name) for name in names), names)
                self.assertEqual(sorted(layouts.compareViewTimePoint(name) for name in names),
                                 [1] * (expected // 2) + [2] * (expected // 2))
        self.assertIn(layouts.COMPARE_DEFAULT_LAYOUT_ID, layouts.COMPARE_LAYOUT_IDS)
        self.assertTrue(set(layouts.COMPARE_DUAL_MONITOR_LAYOUT_IDS) <= set(layouts.COMPARE_LAYOUT_IDS))
        self.assertFalse(set(layouts.COMPARE_LAYOUT_IDS) & set(layouts.CUSTOM_LAYOUT_IDS))

    def test_dual_monitor_views(self):
        xmlText = layouts.buildCompareLayoutDescriptions(False)[layouts.COMPARE_DUAL_MONITOR_LAYOUT_IDS[0]]
        names = set(self._viewNames(xmlText))
        for timePoint in (1, 2):
            for content in ("AxCT", "AxFus", "AxPET", "MIP", "CorFus", "CorPET"):
                self.assertIn(layouts.compareViewName(timePoint, content), names)
        self.assertIn(layouts.COMPARE_DUAL_MONITOR_WINDOW_TITLE, xmlText)

    def test_compare_view_names(self):
        self.assertFalse(layouts.isCompareView("Red"))
        self.assertFalse(layouts.isCompareView(None))
        self.assertTrue(layouts.isCompareView("EpCmp2AxFus"))
        self.assertEqual(layouts.compareViewTimePoint("EpCmp2AxFus"), 2)
        self.assertIsNone(layouts.compareViewTimePoint("Red"))


class StudyInfoTest(unittest.TestCase):

    def test_format_dicom_date_time(self):
        self.assertEqual(studyinfo.formatDicomDateTime("20260314", "103215.000000"), "2026-03-14 10:32")
        self.assertEqual(studyinfo.formatDicomDateTime("20260314", ""), "2026-03-14")
        self.assertEqual(studyinfo.formatDicomDateTime("2026.03.14", "0932"), "2026-03-14 09:32")
        self.assertEqual(studyinfo.formatDicomDateTime("20261314", "1000"), "")   # month 13
        self.assertEqual(studyinfo.formatDicomDateTime("", "1000"), "")
        self.assertEqual(studyinfo.formatDicomDateTime("20260314", "2599"), "2026-03-14")   # invalid time dropped

    def test_date_from_name(self):
        self.assertEqual(studyinfo.dateFromName("PET_2026-03-14_WB"), "2026-03-14")
        self.assertEqual(studyinfo.dateFromName("7: PET AC 20250901"), "2025-09-01")
        self.assertEqual(studyinfo.dateFromName("series 123456789"), "")
        self.assertEqual(studyinfo.dateFromName("PET 20261399"), "")
        self.assertEqual(studyinfo.dateFromName(None), "")

    def test_label(self):
        self.assertEqual(studyinfo.labelWithTime("PET", "2026-03-14 10:32"), "PET (2026-03-14 10:32)")
        self.assertEqual(studyinfo.labelWithTime("PET", ""), "PET")


class ShortcutDispatchTest(unittest.TestCase):

    def test_predicate_handlers_first(self):
        fallback, compare = (lambda: "fallback"), (lambda: "compare")
        handlers = {"Easy_fusion": (fallback, None), "EponaCompare": (compare, lambda: True)}
        self.assertIs(shortcuts.chooseHandler(handlers), compare)
        handlers["EponaCompare"] = (compare, lambda: False)
        self.assertIs(shortcuts.chooseHandler(handlers), fallback)
        self.assertIsNone(shortcuts.chooseHandler({"EponaCompare": (compare, lambda: False)}))
        self.assertIsNone(shortcuts.chooseHandler({}))

    def test_failing_predicate_is_skipped(self):
        def broken():
            raise RuntimeError("no view")
        fallback = (lambda: None)
        with self.assertLogs(level="ERROR"):
            self.assertIs(shortcuts.chooseHandler({"A": (print, broken), "B": (fallback, None)}), fallback)


class _FakeNode:
    """Just the attribute API of an MRML node."""

    def __init__(self):
        self.attributes = {}

    def GetAttribute(self, name):
        return self.attributes.get(name)

    def SetAttribute(self, name, value):
        self.attributes[name] = value

    def RemoveAttribute(self, name):
        self.attributes.pop(name, None)

    def GetAttributeNames(self):
        return list(self.attributes)


class RoiSetTest(unittest.TestCase):

    def test_per_roi_settings(self):
        node, RoiSet = _FakeNode(), roiset.SphereRoiSet
        self.assertEqual(RoiSet.getRadius(node, "p1", 7.5), 7.5)       # default is stored
        RoiSet.setRadius(node, "p1", 12)
        self.assertEqual(RoiSet.getRadius(node, "p1", 7.5), 12.0)
        self.assertEqual((RoiSet.getNumber(node, "p1"), RoiSet.getNumber(node, "p2"), RoiSet.getNumber(node, "p1")),
                         (1, 2, 1))
        self.assertEqual(RoiSet.getThreshold(node, "p1", (roi.THRESHOLD_RELATIVE, 40)), (roi.THRESHOLD_RELATIVE, 40.0))
        RoiSet.setThreshold(node, "p1", roi.THRESHOLD_ABSOLUTE, 2.5)
        self.assertEqual(RoiSet.getThreshold(node, "p1", (roi.THRESHOLD_RELATIVE, 40)), (roi.THRESHOLD_ABSOLUTE, 2.5))
        self.assertEqual(RoiSet.getOrigin(node, "p1"), roi.ROI_ORIGIN_USER)
        color = RoiSet.getColor(node, "p1")
        self.assertEqual(RoiSet.getColor(node, "p1"), color)             # stored once, stable
        # Same attribute names as scenes saved by earlier Epona versions
        self.assertEqual(node.GetAttribute("EasyFusion.RoiRadius_p1"), "12")
        self.assertEqual(node.GetAttribute("EasyFusion.RoiNextNumber"), "3")

    def test_node_tags(self):
        compareSet = roiset.SphereRoiSet("TP1", "TP1", lambda: None)
        node = _FakeNode()
        compareSet._tag(node, roiset.ROLE_HANDLES)
        self.assertEqual(node.attributes, {roiset.ROI_SET_ATTRIBUTE: "TP1", roiset.ROI_ROLE_ATTRIBUTE: "handles"})
        eponaSet = roiset.SphereRoiSet("Epona", "SUV", lambda: None,
                                       roleAttributes={roiset.ROLE_POINTS: "EasyFusion.SUVROIList"})
        node = _FakeNode()
        eponaSet._tag(node, roiset.ROLE_POINTS)
        self.assertEqual(node.attributes, {"EasyFusion.SUVROIList": "1"})
        self.assertIsNone(eponaSet.viewNodeIDs)                         # views left alone until setViews


class _FakeSignalWidget:
    """Stand-in for the few Qt widget calls the toolbar makes."""

    def __init__(self, *args):
        self.visible, self.checked, self.toolTip, self.icon, self._slots = True, False, "", None, {}

    def connect(self, signal, slot):
        self._slots[signal] = slot

    def click(self):
        if getattr(self, "checkable", False):
            self.checked = not self.checked
        self._slots["clicked()"]()

    def blockSignals(self, block):
        return False

    def setToolTip(self, text):
        self.toolTip = text

    def setIcon(self, icon):
        self.icon = icon

    def setCheckable(self, checkable):
        self.checkable = checkable

    def setVisible(self, visible):
        self.visible = bool(visible)

    def isVisible(self):
        return self.visible

    def show(self):
        self.visible = True

    def hide(self):
        self.visible = False

    def __getattr__(self, name):   # setIconSize, setAutoRaise, setStyleSheet, ...: accepted and ignored
        if name.startswith("set") or name in ("deleteLater",):
            return lambda *args, **kwargs: None
        raise AttributeError(name)


class _FakeToolBar(_FakeSignalWidget):
    def __init__(self, *args):
        super().__init__()
        self.actions = []

    def toggleViewAction(self):
        return _FakeSignalWidget()

    def addWidget(self, widget):
        action = _FakeSignalWidget()
        action.widget = widget
        self.actions.append(action)
        return action

    def addSeparator(self):
        return self.addWidget(None)


class _FakeMainWindow:
    def __init__(self):
        self.toolBars = []

    def addToolBarBreak(self, *args):
        pass

    def addToolBar(self, area, toolBar):
        self.toolBars.append(toolBar)

    def removeToolBarBreak(self, toolBar):
        pass

    def removeToolBar(self, toolBar):
        self.toolBars.remove(toolBar)


class ToolbarTest(unittest.TestCase):
    """EponaToolBar logic with stand-ins for Qt and Slicer (no window is created)."""

    def setUp(self):
        from types import SimpleNamespace
        from unittest import mock
        self.settings = {}
        settings = self.settings

        class FakeSettings:
            def value(self, key, default=None):
                return settings.get(key, default)

            def setValue(self, key, value):
                settings[key] = value

        self.mainWindow, self.dock = _FakeMainWindow(), _FakeSignalWidget()
        self.selectedModule = "TestModule"
        self.layout = SimpleNamespace(layout=7504)
        fakeQt = SimpleNamespace(
            QToolBar=_FakeToolBar, QToolButton=_FakeSignalWidget, QLabel=_FakeSignalWidget, QIcon=lambda *a: ("icon", a),
            QSize=lambda *a: a, QSettings=FakeSettings,
            Qt=SimpleNamespace(TopToolBarArea=4, ToolButtonIconOnly=0, ToolButtonTextOnly=1, NoFocus=0))
        fakeSlicer = SimpleNamespace(
            util=SimpleNamespace(mainWindow=lambda: self.mainWindow, findChild=lambda widget, name: self.dock,
                                 findChildren=lambda widget, name="": [], showStatusMessage=lambda *a: None,
                                 selectedModule=lambda: self.selectedModule),
            app=SimpleNamespace(layoutManager=lambda: self.layout))
        self.patches = [mock.patch.object(toolbar, "qt", fakeQt), mock.patch.object(toolbar, "slicer", fakeSlicer),
                        mock.patch.object(toolbar, "loadIcon", lambda name: ("icon", name))]
        for patch in self.patches:
            patch.start()
        self.enabledChanges = []
        self.bar = toolbar.EponaToolBar("TestToolBar", "Test", moduleName="TestModule",
                                        onEnabledChanged=self.enabledChanges.append)
        self.assertTrue(self.bar.build())

    def tearDown(self):
        for patch in self.patches:
            patch.stop()

    def test_shown_only_in_module_and_panel_restored(self):
        bar = self.bar
        bar.addWindowButtons()
        self.assertFalse(bar.toolBar.isVisible())                   # hidden until the module is entered
        bar.enter()
        self.assertTrue(bar.toolBar.isVisible())
        bar._panelButton.click()                                    # hide the module panel
        self.assertFalse(self.dock.isVisible())
        self.assertEqual(bar._panelButton.icon, ("icon", "panel_show"))
        bar.exit()                                                  # leaving the module: panel back, toolbar hidden
        self.assertTrue(self.dock.isVisible())
        self.assertFalse(bar.toolBar.isVisible())
        bar.destroy()
        self.assertEqual(self.mainWindow.toolBars, [])

    def test_hide_toolbar_button(self):
        bar = self.bar
        bar.addWindowButtons()
        bar.enter()
        bar.setPanelVisible(False)
        hideButton = bar.toolBar.actions[-1].widget
        hideButton.click()
        self.assertFalse(bar.toolBar.isVisible())
        self.assertTrue(self.dock.isVisible())          # the panel comes back: its checkbox turns the toolbar on again
        self.assertEqual(self.enabledChanges, [False])
        self.assertFalse(toolbar.isToolbarEnabled())
        bar.exit()
        bar.enter()
        self.assertFalse(bar.toolBar.isVisible())       # remembered
        bar.setEnabled(True)
        self.assertTrue(bar.toolBar.isVisible() and toolbar.isToolbarEnabled())

    def test_groups_layouts_rotation(self):
        bar = self.bar
        clicked = []
        bar.addPresetButton("preset_suv_5", "SUV 0-5", lambda: clicked.append("suv"), group="suv")
        bar.addPresetButton("preset_pct_10", "10%", lambda: clicked.append("pct"), group="percent")
        bar.setGroupVisible("suv", False)
        self.assertEqual([a.isVisible() for a in bar.toolBar.actions], [False, True])
        layoutsChosen = []

        def chooseLayout(layoutID):
            layoutsChosen.append(layoutID)
            self.layout.layout = layoutID
        bar.addLayoutButtons([(7501, "layout_axial_four_up", "A"), (7504, "layout_ct_fusion_pet_3d", "B")], chooseLayout)
        bar.setCurrentLayout(7504)
        self.assertEqual([bar._layoutButtons[i].checked for i in (7501, 7504)], [False, True])
        bar._layoutButtons[7501].click()
        self.assertEqual(layoutsChosen, [7501])
        self.assertEqual([bar._layoutButtons[i].checked for i in (7501, 7504)], [True, False])
        bar._layoutButtons[7501].click()                # clicking the current layout keeps it checked
        self.assertTrue(bar._layoutButtons[7501].checked)
        rotations = []
        bar.addRotationButton(rotations.append)
        bar._rotationButton.click()
        self.assertEqual(rotations, [True])
        bar.setRotating(False)
        self.assertFalse(bar._rotationButton.checked)

    def test_every_preset_and_layout_has_an_icon(self):
        import os
        names = ([toolbar.CT_PRESET_ICONS[text] for text, *_ in values.CT_WINDOW_PRESETS]
                 + [toolbar.SUV_PRESET_ICONS[upper] for _, upper, _ in values.PET_SUV_PRESETS]
                 + [toolbar.PERCENT_PRESET_ICONS[p] for p in values.SPECT_PERCENT_OF_MAX_PRESETS]
                 + [toolbar.MRI_PRESET_ICONS[text] for text, *_ in values.MRI_PERCENTILE_PRESETS]
                 + [toolbar.LAYOUT_ICONS[i] for i in layouts.EASYFUSION_LAYOUT_IDS + layouts.COMPARE_LAYOUT_IDS]
                 + ["go", "panel_hide", "panel_show", "mip_rotate", "toolbar_hide"])
        for name in names:
            self.assertTrue(os.path.exists(toolbar.iconPath(name)), name)

    def test_show_button_works_without_enter(self):
        """'Show Toolbar' shows it while the module is selected, even if enter() was never reached."""
        bar = self.bar
        toolbar.setToolbarEnabledSetting(False)
        self.selectedModule = "OtherModule"
        bar.setEnabled(True)
        self.assertFalse(bar.toolBar.isVisible())        # never shown for another module
        self.selectedModule = "TestModule"
        bar.setEnabled(True)
        self.assertTrue(bar.toolBar.isVisible() and bar.isShown())
        bar.setEnabled(False)
        self.assertFalse(bar.isShown())

    def test_text_buttons(self):
        views = []
        button = self.bar.addTextButton("A", "Anterior", lambda: views.append(3))
        button.click()
        self.assertEqual(views, [3])

    def test_color_swatch_functions(self):
        class Table:   # color table: read entry by entry
            def IsA(self, name):
                return name == "vtkMRMLColorTableNode"

            def GetNumberOfColors(self):
                return 3

            def GetColor(self, index, rgba):
                rgba[:] = [(0.0, 0, 0, 1), (0.5, 0, 0, 1), (1.0, 0, 0, 1)][index]
                return True

        class LookupTableLike:   # like vtkLookupTable in Python: only GetColor(value, rgb)
            def GetRange(self):
                return (0.0, 10.0)

            def GetColor(self, value, rgb):
                rgb[:] = [value / 10.0, 0.0, 1.0]

        class Procedural:
            def IsA(self, name):
                return False

            def GetScalarsToColors(self):
                return LookupTableLike()

        tableColors = toolbar.colorNodeRgbFunction(Table())
        self.assertEqual([list(tableColors(t)) for t in (0.0, 0.5, 1.0)], [[0, 0, 0], [0.5, 0, 0], [1.0, 0, 0]])
        procedural = toolbar.colorNodeRgbFunction(Procedural())
        self.assertEqual(list(procedural(0.5)), [0.5, 0.0, 1.0])
        self.assertIsNone(toolbar.colorNodeRgbFunction(None))

    def test_tooltip(self):
        self.assertEqual(toolbar.richToolTip("A<B", "x\ny"), "<b>A&lt;B</b><br>x<br>y")


class ImageKindLabelTest(unittest.TestCase):

    def test_anatomical_modality(self):
        f = values.anatomicalModalityFrom
        self.assertEqual(f("CT", 0.0), "CT")                      # DICOM modality wins
        self.assertEqual(f("MR", -1024.0), "MRI")
        self.assertEqual(f("", -1024.0), "CT")                    # air far below 0 HU
        self.assertEqual(f("", 0.0), "MRI")                       # no negative values: not a CT
        self.assertEqual(f("", -100.0, "abdomen T2 fs"), "MRI")   # unclear values: the name decides
        self.assertEqual(f("", -100.0, "CT 2.5mm"), "CT")
        self.assertIsNone(f("", -100.0, "series 5"))
        self.assertIsNone(f("", None, ""))
        self.assertEqual(f("", float("nan"), "WB_MRI"), "MRI")
        self.assertIsNone(f("", -100.0, "cortex"))                # "ct" inside a word is not a CT

    def test_labels(self):
        self.assertEqual(values.FUNCTIONAL_LABELS.get(None, values.FUNCTIONAL_FALLBACK_LABEL), "SPECT/PET")
        self.assertEqual(values.FUNCTIONAL_LABELS.get("SPECT", values.FUNCTIONAL_FALLBACK_LABEL), "SPECT")
        self.assertEqual(values.ANATOMICAL_LABELS.get(None, values.ANATOMICAL_FALLBACK_LABEL), "CT/MRI")
        self.assertEqual(values.anatomicalLabel(None), "CT/MRI")
        self.assertEqual(values.functionalLabel(None), "SPECT/PET")


class SliceSyncTest(unittest.TestCase):

    def test_only_same_orientation_views_follow(self):
        from types import SimpleNamespace
        from unittest import mock
        axialFusion, axialCt = SimpleNamespace(GetOrientation=lambda: "Axial"), SimpleNamespace(GetOrientation=lambda: "Axial")
        turned = SimpleNamespace(GetOrientation=lambda: "Sagittal")   # an axial view turned with its controller
        copied = []
        with mock.patch.object(display, "copySliceGeometry", lambda source, target: copied.append(target)), \
                mock.patch.object(display, "_sameGeometry", lambda source, target: False), \
                mock.patch.object(display, "repairSliceAspect", lambda node: False):
            sync = display.SliceGeometrySync()
            sync.syncFrom(axialFusion, [axialFusion, axialCt, turned])
        self.assertEqual(copied, [axialCt])
        self.assertFalse(sync._syncing)

    def test_stretched_view_is_corrected(self):
        f = display.correctedFieldOfView
        self.assertIsNone(f((300.0, 150.0, 1.0), (600, 300, 1)))            # matches the view: left alone
        self.assertIsNone(f((300.0, 151.0, 1.0), (600, 300, 1)))            # within 1 %
        self.assertEqual(f((300.0, 300.0, 1.0), (600, 300, 1)), (300.0, 150.0, 1.0))   # stretched: width kept
        self.assertEqual(f((200.0, 100.0, 1.0), (300, 900, 1)), (200.0, 600.0, 1.0))   # tall narrow view
        self.assertIsNone(f((300.0, 300.0, 1.0), (0, 300, 1)))              # no size yet (being laid out)

    def test_sync_recopies_to_a_stretched_target(self):
        from unittest import mock

        class Node:
            def __init__(self, fieldOfView, dimensions):
                self.fieldOfView, self.dimensions = list(fieldOfView), dimensions

            def GetOrientation(self):
                return "Coronal"

            def GetFieldOfView(self):
                return self.fieldOfView

            def SetFieldOfView(self, *fov):
                self.fieldOfView = list(fov)

            def GetDimensions(self):
                return self.dimensions

        source, stretched = Node((300.0, 150.0, 1.0), (600, 300, 1)), Node((300.0, 300.0, 1.0), (600, 300, 1))
        copied = []
        with mock.patch.object(display, "copySliceGeometry", lambda a, b: copied.append(b)):
            # everything else identical: only the stretch differs
            with mock.patch.object(display, "_sameGeometry",
                                   lambda a, b: display.correctedFieldOfView(b.GetFieldOfView(), b.GetDimensions())
                                   is None):
                display.SliceGeometrySync().syncFrom(source, [source, stretched])
        self.assertEqual(copied, [stretched])
        stretchedSource = Node((300.0, 900.0, 1.0), (600, 300, 1))
        with mock.patch.object(display, "copySliceGeometry", lambda a, b: None):
            display.SliceGeometrySync().syncFrom(stretchedSource, [stretchedSource])
        self.assertEqual(stretchedSource.fieldOfView, [300.0, 150.0, 1.0])   # the source itself is repaired

    def test_view_shape_check(self):
        f = display.viewShapeDiffers
        self.assertFalse(f((600, 300, 1), (600, 300)))
        self.assertFalse(f((600, 300, 1), (1200, 600)))           # same shape at another pixel ratio (HiDPI)
        self.assertTrue(f((600, 300, 1), (300, 900)))             # node still has the old shape: stretched
        self.assertFalse(f((300, 300, 1), (900, 300), grid=(3, 1)))   # lightbox: one cell of three
        self.assertFalse(f((0, 300, 1), (300, 900)))              # not laid out yet: leave it to Slicer
        self.assertFalse(f((600, 300, 1), (0, 900)))

    def test_orientation_getter_fallbacks(self):
        from types import SimpleNamespace
        self.assertEqual(display.sliceOrientation(SimpleNamespace(GetOrientationString=lambda: "Coronal")), "Coronal")
        self.assertIsNone(display.sliceOrientation(SimpleNamespace()))


class SecondScreenTest(unittest.TestCase):
    """showOnSecondScreen changes only what is not so already (stand-ins for Qt)."""

    def setUp(self):
        from types import SimpleNamespace
        from unittest import mock

        class Rect:
            def __init__(self, x0, x1):
                self.x0, self.x1 = x0, x1

            def contains(self, point):
                return self.x0 <= point <= self.x1

            def center(self):
                return (self.x0 + self.x1) / 2

        class Window:
            def __init__(self, flags, x, maximized):
                self.flags, self.frameGeometry, self.maximized, self.visible = flags, Rect(x, x + 10), maximized, True
                self.calls = []

            def windowFlags(self):
                return self.flags

            def setWindowFlags(self, flags):
                self.calls.append("flags")
                self.flags = flags

            def setGeometry(self, geometry):
                self.calls.append("geometry")
                self.frameGeometry = Rect(geometry.x0, geometry.x0 + 10)

            def isMaximized(self):
                return self.maximized

            def showMaximized(self):
                self.calls.append("maximize")
                self.maximized = True

            def raise_(self):
                pass

        self.Window, self.Rect = Window, Rect
        screen1, screen2 = SimpleNamespace(geometry=Rect(0, 1000)), SimpleNamespace(geometry=Rect(1001, 2000))
        screen2.availableGeometry = Rect(1001, 2000)
        flagNames = ("Window", "CustomizeWindowHint", "WindowTitleHint", "WindowSystemMenuHint",
                     "WindowMinMaxButtonsHint", "WindowCloseButtonHint")
        fakeQt = SimpleNamespace(Qt=SimpleNamespace(**{name: 1 << i for i, name in enumerate(flagNames)}),
                                 QGuiApplication=SimpleNamespace(screens=lambda: [screen1, screen2]))
        self.allFlags = (1 << len(flagNames)) - 1
        fakeSlicer = SimpleNamespace(util=SimpleNamespace(
            mainWindow=lambda: SimpleNamespace(frameGeometry=Rect(100, 200))))
        self.patches = [mock.patch.object(layouts, "qt", fakeQt), mock.patch.object(layouts, "slicer", fakeSlicer)]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()

    def test_new_window_is_set_up(self):
        window = self.Window(flags=1, x=300, maximized=False)        # a fresh floating dock on screen 1
        layouts.showOnSecondScreen(window)
        self.assertEqual(window.calls, ["flags", "geometry", "maximize"])

    def test_window_already_right_is_left_alone(self):
        """Switching between the two dual monitor layouts: the window is kept, nothing may be re-applied."""
        window = self.Window(flags=self.allFlags, x=1500, maximized=True)
        layouts.showOnSecondScreen(window)
        self.assertEqual(window.calls, [])

    def test_flags_check(self):
        self.assertTrue(layouts.hasAllFlags(7, 5))
        self.assertFalse(layouts.hasAllFlags(4, 5))
        self.assertFalse(layouts.hasAllFlags(None, 5))


if __name__ == "__main__":
    unittest.main()

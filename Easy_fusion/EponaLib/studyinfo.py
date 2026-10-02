"""
Imaging date / time of a volume, shown in parentheses next to its name in the compare module
("PET 3D (2026-03-14 10:32)").

Sources, most specific first: a value remembered on the node (saved with the scene), the DICOM files in Slicer's
DICOM database (acquisition, series, study date / time), the DICOM information kept in the Data module tree
(series and study date / time, saved with the scene), and finally a date written in the volume's name.
"""

import logging
import re

import slicer

IMAGING_TIME_ATTRIBUTE = "Epona.ImagingTime"   # "YYYY-MM-DD HH:MM" (or date only) found earlier
# (date tag, time tag), most specific first: acquisition, series, study
DICOM_DATE_TIME_TAGS = (("0008,0022", "0008,0032"), ("0008,0021", "0008,0031"), ("0008,0020", "0008,0030"))
_NAME_DATE_PATTERNS = (re.compile(r"(?<!\d)((?:19|20)\d{2})-(\d{2})-(\d{2})(?!\d)"),
                       re.compile(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})(?!\d)"))


def _validDate(year, month, day):
    return 1 <= int(month) <= 12 and 1 <= int(day) <= 31 and 1900 <= int(year) <= 2100


def formatDicomDateTime(date, timeText=""):
    """ "YYYY-MM-DD HH:MM" from DICOM DA ("YYYYMMDD") and TM ("HHMMSS.ffffff"), date only without a time, else ""."""
    date = re.sub(r"[^0-9]", "", str(date or ""))
    if len(date) != 8 or not _validDate(date[:4], date[4:6], date[6:]):
        return ""
    text = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    timeDigits = re.sub(r"[^0-9]", "", str(timeText or "").split(".")[0])
    if len(timeDigits) >= 4 and int(timeDigits[:2]) < 24 and int(timeDigits[2:4]) < 60:
        text += f" {timeDigits[:2]}:{timeDigits[2:4]}"
    return text


def dateFromName(name):
    """ "YYYY-MM-DD" from a date written in a name ("2026-03-14" or "20260314"), else ""."""
    for pattern in _NAME_DATE_PATTERNS:
        for match in pattern.finditer(name or ""):
            year, month, day = match.groups()
            if _validDate(year, month, day):
                return f"{year}-{month}-{day}"
    return ""


def labelWithTime(name, imagingTime):
    return f"{name} ({imagingTime})" if imagingTime else (name or "")


def _fromDicomDatabase(volumeNode):
    uids = (volumeNode.GetAttribute("DICOM.instanceUIDs") or "").split()
    database = getattr(slicer, "dicomDatabase", None)
    if not uids or database is None:
        return ""
    try:
        if not database.isOpen:
            return ""
        path = database.fileForInstance(uids[len(uids) // 2])
        if not path:
            return ""
        for dateTag, timeTag in DICOM_DATE_TIME_TAGS:
            text = formatDicomDateTime(database.fileValue(path, dateTag), database.fileValue(path, timeTag))
            if text:
                return text
    except Exception:
        logging.debug("Epona: could not read the imaging time from the DICOM database", exc_info=True)
    return ""


def _fromDataTree(volumeNode):
    try:
        shNode = slicer.mrmlScene.GetSubjectHierarchyNode()
        itemID = shNode.GetItemByDataNode(volumeNode)
        if not itemID:
            return ""
        prefix = slicer.vtkMRMLSubjectHierarchyConstants.GetDICOMAttributePrefix()
        text = formatDicomDateTime(shNode.GetItemAttribute(itemID, prefix + "SeriesDate"),
                                   shNode.GetItemAttribute(itemID, prefix + "SeriesTime"))
        if text:
            return text
        studyItemID = shNode.GetItemParent(itemID)
        if studyItemID:
            return formatDicomDateTime(
                shNode.GetItemAttribute(studyItemID, slicer.vtkMRMLSubjectHierarchyConstants.GetDICOMStudyDateAttributeName()),
                shNode.GetItemAttribute(studyItemID, slicer.vtkMRMLSubjectHierarchyConstants.GetDICOMStudyTimeAttributeName()))
    except Exception:
        logging.debug("Epona: could not read the imaging time from the Data module tree", exc_info=True)
    return ""


def imagingTime(volumeNode):
    """Imaging date / time of a volume ("YYYY-MM-DD HH:MM", or the date only), or "" when it is not known."""
    if volumeNode is None:
        return ""
    stored = volumeNode.GetAttribute(IMAGING_TIME_ATTRIBUTE)
    if stored:
        return stored
    text = _fromDicomDatabase(volumeNode) or _fromDataTree(volumeNode)
    if text:
        volumeNode.SetAttribute(IMAGING_TIME_ATTRIBUTE, text)  # remembered: the DICOM database may be gone later
        return text
    return dateFromName(volumeNode.GetName())


def volumeLabel(volumeNode):
    """ "name (imaging time)" for a volume, or its name when the time is not known."""
    return labelWithTime(volumeNode.GetName(), imagingTime(volumeNode)) if volumeNode is not None else ""

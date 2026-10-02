cmake_minimum_required(VERSION 3.16.3...3.19.7 FATAL_ERROR)

project(PETReviewCompare)

#-----------------------------------------------------------------------------
# Extension meta-information
set(EXTENSION_HOMEPAGE "https://github.com/4burakfe/SlicerPETReviewCompare/")
set(EXTENSION_CATEGORY "Nuclear Medicine")
set(EXTENSION_CONTRIBUTORS "Burak Demir (4burakfe@gmail.com)")
set(EXTENSION_DESCRIPTION "PETReviewCompare is a 3D Slicer extension for reading SPECT/PET with CT/MRI. Epona - SPECT/PET Review provides fusion, MIP, reading layouts (including dual monitor), windowing presets with keyboard shortcuts, SUV measurements with spherical ROIs (Max, Mean, MTV, TLG) and optional AI post-processing filters that run Belenos PET Denoise models. Epona - SPECT/PET Compare reads two time points of the same patient: it registers time point 1 to time point 2 (translation only) for side-by-side comparison. The tools are developed with research utility in mind and are not intended for clinical use.")
set(EXTENSION_ICONURL "https://raw.githubusercontent.com/4burakfe/SlicerPETReviewCompare/main/SlicerPETReviewCompare.png")
set(EXTENSION_SCREENSHOTURLS "https://raw.githubusercontent.com/4burakfe/SlicerPETReviewCompare/main/scr1.jpg")
set(EXTENSION_DEPENDS "NA") # PyTorch is optional: only the AI post-processing filters need it (installed via the PyTorch extension)

#-----------------------------------------------------------------------------
# Extension dependencies
find_package(Slicer REQUIRED)
include(${Slicer_USE_FILE})

#-----------------------------------------------------------------------------
# Extension modules
add_subdirectory(Easy_fusion)
add_subdirectory(EponaCompare)
## NEXT_MODULE

#-----------------------------------------------------------------------------
include(${Slicer_EXTENSION_GENERATE_CONFIG})
include(${Slicer_EXTENSION_CPACK})

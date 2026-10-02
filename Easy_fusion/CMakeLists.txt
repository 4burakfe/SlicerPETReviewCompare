#-----------------------------------------------------------------------------
set(MODULE_NAME Easy_fusion)

#-----------------------------------------------------------------------------
set(MODULE_PYTHON_SCRIPTS
  ${MODULE_NAME}.py
  EponaLib/__init__.py
  EponaLib/scene.py
  EponaLib/shortcuts.py
  EponaLib/layouts.py
  EponaLib/values.py
  EponaLib/studyinfo.py
  EponaLib/display.py
  EponaLib/roi.py
  EponaLib/roiset.py
  EponaLib/filters.py
  EponaLib/overlays.py
  EponaLib/registration.py
  EponaLib/compareviews.py
  EponaLib/toolbar.py
  EponaLib/tests.py
  )

set(MODULE_PYTHON_RESOURCES
  Resources/Icons/${MODULE_NAME}.png
  Resources/Icons/fusbanner.jpg
  Resources/Icons/Toolbar/go.png
  Resources/Icons/Toolbar/layout_axial_four_up.png
  Resources/Icons/Toolbar/layout_compare_axial_coronal.png
  Resources/Icons/Toolbar/layout_compare_axial_ct.png
  Resources/Icons/Toolbar/layout_compare_axial_pet.png
  Resources/Icons/Toolbar/layout_compare_dual_monitor.png
  Resources/Icons/Toolbar/layout_ct_fusion_pet_3d.png
  Resources/Icons/Toolbar/layout_dual_monitor.png
  Resources/Icons/Toolbar/layout_dual_monitor_fusion_middle.png
  Resources/Icons/Toolbar/layout_four_up.png
  Resources/Icons/Toolbar/layout_two_by_two_3d.png
  Resources/Icons/Toolbar/mip_rotate.png
  Resources/Icons/Toolbar/panel_hide.png
  Resources/Icons/Toolbar/panel_show.png
  Resources/Icons/Toolbar/preset_ct_abdomen.png
  Resources/Icons/Toolbar/preset_ct_bones.png
  Resources/Icons/Toolbar/preset_ct_head.png
  Resources/Icons/Toolbar/preset_ct_lungs.png
  Resources/Icons/Toolbar/preset_mri_bright.png
  Resources/Icons/Toolbar/preset_mri_contrast.png
  Resources/Icons/Toolbar/preset_mri_standard.png
  Resources/Icons/Toolbar/preset_mri_wide.png
  Resources/Icons/Toolbar/preset_pct_10.png
  Resources/Icons/Toolbar/preset_pct_100.png
  Resources/Icons/Toolbar/preset_pct_25.png
  Resources/Icons/Toolbar/preset_pct_50.png
  Resources/Icons/Toolbar/preset_pct_75.png
  Resources/Icons/Toolbar/preset_suv_10.png
  Resources/Icons/Toolbar/preset_suv_15.png
  Resources/Icons/Toolbar/preset_suv_25.png
  Resources/Icons/Toolbar/preset_suv_5.png
  Resources/Icons/Toolbar/preset_suv_7.png
  Resources/Icons/Toolbar/toolbar_hide.png
  Resources/UI/${MODULE_NAME}.ui
  )

#-----------------------------------------------------------------------------
slicerMacroBuildScriptedModule(
  NAME ${MODULE_NAME}
  SCRIPTS ${MODULE_PYTHON_SCRIPTS}
  RESOURCES ${MODULE_PYTHON_RESOURCES}
  WITH_GENERIC_TESTS
  )

#-----------------------------------------------------------------------------
if(BUILD_TESTING)

  # Register the unittest subclass in the main script as a ctest.
  # Note that the test will also be available at runtime.
  slicer_add_python_unittest(SCRIPT ${MODULE_NAME}.py)

  # Additional build-time testing
  add_subdirectory(Testing)
endif()

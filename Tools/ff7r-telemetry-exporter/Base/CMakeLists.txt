cmake_minimum_required(VERSION 3.22)
set(TARGET FF7RTelemetryExporter)
project(${TARGET} LANGUAGES CXX)

add_library(${TARGET} SHARED "src/dllmain.cpp")
target_compile_features(${TARGET} PRIVATE cxx_std_23)
set_property(TARGET ${TARGET} PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>DLL")
target_compile_definitions(${TARGET} PRIVATE
    UE_GAME UE_BUILD_SHIPPING PLATFORM_WINDOWS PLATFORM_MICROSOFT
    OVERRIDE_PLATFORM_HEADER_NAME=Windows UBT_COMPILED_PLATFORM=Win64
    UNICODE _UNICODE NOMINMAX)
if(NOT EXISTS "${INSTALLED_INTERFACE}/UE4SS/include/Mod/CppUserModBase.hpp")
    message(FATAL_ERROR "The installed c0a8d4c mod interface is required; use Build-BaseTelemetryExporter.ps1")
endif()
target_include_directories(${TARGET} BEFORE PRIVATE
    "${INSTALLED_INTERFACE}/UE4SS/include"
    "${INSTALLED_INTERFACE}/deps/first/String/include")
target_include_directories(${TARGET} PRIVATE
    "${UE4SS_SOURCE}/UE4SS/include"
    "${UE4SS_SOURCE}/deps/first/LuaMadeSimple/include"
    "${UE4SS_SOURCE}/deps/first/LuaRaw/include"
    "${UE4SS_SOURCE}/deps/first/File/include"
    "${UE4SS_SOURCE}/deps/first/DynamicOutput/include"
    "${UE4SS_SOURCE}/deps/first/Input/include"
    "${UE4SS_SOURCE}/deps/first/Helpers/include"
    "${UE4SS_SOURCE}/deps/first/Function/include"
    "${UE4SS_SOURCE}/deps/first/Constructs/include"
    "${UE4SS_SOURCE}/deps/first/ASMHelper/include"
    "${ZYDIS_SOURCE}/include"
    "${ZYDIS_SOURCE}/dependencies/zycore/include"
    "${IMGUI_SOURCE}"
    "${IMGUI_TEXT_EDIT_SOURCE}"
    "${UEPSEUDO_SOURCE}/include"
    "${UEPSEUDO_SOURCE}/generated_include"
    "${UEPSEUDO_SOURCE}/include/Unreal"
    "${UEPSEUDO_SOURCE}/include/Unreal/Core")
target_link_libraries(${TARGET} PRIVATE "${UE4SS_IMPORT_LIB}")
set_target_properties(${TARGET} PROPERTIES OUTPUT_NAME "main")

add_executable(FF7RTelemetrySampleTests "tests/sample_format_test.cpp")
target_compile_features(FF7RTelemetrySampleTests PRIVATE cxx_std_23)
target_compile_definitions(FF7RTelemetrySampleTests PRIVATE NOMINMAX)

add_executable(FF7RObjectPathCacheTests "tests/object_path_cache_test.cpp")
target_compile_features(FF7RObjectPathCacheTests PRIVATE cxx_std_23)

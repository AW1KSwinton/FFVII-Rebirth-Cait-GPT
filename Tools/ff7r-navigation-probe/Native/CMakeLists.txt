cmake_minimum_required(VERSION 3.22)

set(TARGET FF7RNavigationNativeProbe)
project(${TARGET} LANGUAGES CXX)

add_library(${TARGET} SHARED "src/dllmain.cpp")
target_compile_features(${TARGET} PRIVATE cxx_std_23)
set_property(TARGET ${TARGET} PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>DLL")
target_compile_definitions(${TARGET} PRIVATE
    UE_GAME
    UE_BUILD_SHIPPING
    PLATFORM_WINDOWS
    PLATFORM_MICROSOFT
    OVERRIDE_PLATFORM_HEADER_NAME=Windows
    UBT_COMPILED_PLATFORM=Win64
    UNICODE
    _UNICODE
)

target_include_directories(${TARGET} PRIVATE
    "${UE4SS_SOURCE}/UE4SS/include"
    "${UE4SS_SOURCE}/deps/first/File/include"
    "${UE4SS_SOURCE}/deps/first/Function/include"
    "${UE4SS_SOURCE}/deps/first/Constructs/include"
    "${UE4SS_SOURCE}/deps/first/ASMHelper/include"
    "${UE4SS_SOURCE}/deps/first/DynamicOutput/include"
    "${UE4SS_SOURCE}/deps/first/Input/include"
    "${UE4SS_SOURCE}/deps/first/Helpers/include"
    "${IMGUI_SOURCE}"
    "${IMGUI_TEXT_EDIT_SOURCE}"
    "${UE4SS_SOURCE}/../../header-deps/zydis-v4.1.0/include"
    "${UE4SS_SOURCE}/../../header-deps/zydis-v4.1.0/dependencies/zycore/include"
    "${UEPSEUDO_SOURCE}/include"
    "${UEPSEUDO_SOURCE}/generated_include"
    "${UEPSEUDO_SOURCE}/include/Unreal"
    "${UEPSEUDO_SOURCE}/include/Unreal/Core"
)

target_link_libraries(${TARGET} PRIVATE "${UE4SS_IMPORT_LIB}" bcrypt)
set_target_properties(${TARGET} PROPERTIES OUTPUT_NAME "main")

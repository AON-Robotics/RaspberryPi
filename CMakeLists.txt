cmake_minimum_required(VERSION 3.20)

project(vexpi
    VERSION 0.1.0
    DESCRIPTION "Raspberry Pi 5 sensor co-processor for a VEX V5 robot"
    LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Always emit compile_commands.json so clangd / the VS Code C++ extension can
# find the DepthAI and OpenCV headers without hardcoded paths.
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
endif()

# The vision half needs DepthAI and OpenCV; the odometry half needs neither.
# Turn this off to build on a machine without the camera SDK installed.
option(VEXPI_BUILD_VISION "Build the OAK-D Lite vision library and apps" ON)

# ---------------------------------------------------------------------------
# vexpi_core -- no camera dependencies: serial link, packet format, OTOS
# ---------------------------------------------------------------------------
find_package(Threads REQUIRED)

add_library(vexpi_core
    src/serial_link.cpp
    src/vex_packet.cpp
    src/otos.cpp)

target_include_directories(vexpi_core PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>)

target_link_libraries(vexpi_core PUBLIC Threads::Threads)

# ---------------------------------------------------------------------------
# vexpi_vision -- OAK-D Lite pipeline and red target tracking
# ---------------------------------------------------------------------------
if(VEXPI_BUILD_VISION)
    find_package(OpenCV REQUIRED)
    find_package(depthai CONFIG REQUIRED)

    add_library(vexpi_vision
        src/oak_camera.cpp
        src/red_target_tracker.cpp)

    target_include_directories(vexpi_vision PUBLIC
        $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
        ${OpenCV_INCLUDE_DIRS})

    target_link_libraries(vexpi_vision PUBLIC
        vexpi_core
        depthai::opencv
        ${OpenCV_LIBS})
endif()

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
add_executable(otos_monitor apps/otos_monitor.cpp)
target_link_libraries(otos_monitor PRIVATE vexpi_core)

add_executable(otos_stream apps/otos_stream.cpp)
target_link_libraries(otos_stream PRIVATE vexpi_core)

if(VEXPI_BUILD_VISION)
    add_executable(red_tracker apps/red_tracker.cpp)
    target_link_libraries(red_tracker PRIVATE vexpi_vision)

    add_executable(depth_center_demo apps/depth_center_demo.cpp)
    target_link_libraries(depth_center_demo PRIVATE vexpi_vision)
endif()

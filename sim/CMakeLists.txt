cmake_minimum_required(VERSION 3.20)

# Laptop-only brain simulator for the LLM bridge. Not part of the Pi build.
#
#   cmake -S sim -B sim/build && cmake --build sim/build
#
# It compiles the brain's protocol code straight from the Override repo, so set
# OVERRIDE_DIR if Override is not checked out next to this repo.

project(brain_sim LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Debug)
endif()

set(OVERRIDE_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../../Override" CACHE PATH "Path to the Override (V5 brain) repo")
if(NOT EXISTS "${OVERRIDE_DIR}/src/aon/pi/protocol.cpp")
    message(FATAL_ERROR "Override not found at ${OVERRIDE_DIR}; pass -DOVERRIDE_DIR=/path/to/Override")
endif()

find_package(Threads REQUIRED)

add_executable(brain_sim
    brain_sim.cpp
    ${OVERRIDE_DIR}/src/aon/pi/protocol.cpp
    ${OVERRIDE_DIR}/src/aon/pi/commands.cpp)
target_include_directories(brain_sim PRIVATE ${OVERRIDE_DIR}/include)
target_compile_options(brain_sim PRIVATE -Wall -Wextra -fno-exceptions)
target_link_libraries(brain_sim PRIVATE Threads::Threads)
if(NOT APPLE)
    target_link_libraries(brain_sim PRIVATE util)  # openpty
endif()

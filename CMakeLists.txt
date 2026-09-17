cmake_minimum_required(VERSION 3.20)

project(oak_vex_app)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(OpenCV REQUIRED)
find_package(depthai CONFIG REQUIRED)
find_package(Threads REQUIRED)

add_executable(oak_vex oak_vex.cpp)

target_include_directories(oak_vex PRIVATE
${OpenCV_INCLUDE_DIRS}
)

target_link_libraries(oak_vex PRIVATE
depthai::opencv
${OpenCV_LIBS}
)

add_executable(red_test red_test.cpp)

target_include_directories(red_test PRIVATE
    ${OpenCV_INCLUDE_DIRS}
)

target_link_libraries(red_test PRIVATE
    depthai::opencv
    ${OpenCV_LIBS}
)

add_executable(otos_test otos_test.cpp)

target_link_libraries(otos_test PRIVATE
    Threads::Threads
)
# This file is part of SymCC.
#
# SymCC is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# SymCC is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# SymCC. If not, see <https://www.gnu.org/licenses/>.

cmake_minimum_required(VERSION 3.16)
project(SymCC)

set(LLVM_VERSION "" CACHE STRING "LLVM version to use. The corresponding LLVM dev package must be installed.")
set(SYMCC_RT_BACKEND "qsym" CACHE STRING "The symbolic backend to use. Please check symcc-rt to get a list of the available backends.")
option(TARGET_32BIT "Make the compiler work correctly with -m32" OFF)

# We need to build the runtime as an external project because CMake otherwise
# doesn't allow us to build it twice with different options (one 32-bit version
# and one 64-bit variant).
include(ExternalProject)

# Find LLVM
find_package(LLVM ${LLVM_VERSION} REQUIRED CONFIG)

message(STATUS "Found LLVM ${LLVM_PACKAGE_VERSION}")
message(STATUS "Using LLVMConfig.cmake from ${LLVM_DIR}")

if (${LLVM_VERSION_MAJOR} LESS 8 OR ${LLVM_VERSION_MAJOR} GREATER 18)
  message(WARNING "The software has been developed for LLVM 8 through 18; \
it is unlikely to work with other versions!")
endif()

set(SYM_RUNTIME_BUILD_ARGS
  -DCMAKE_AR=${CMAKE_AR}
  -DCMAKE_C_COMPILER=${CMAKE_C_COMPILER}
  -DCMAKE_C_FLAGS=${CMAKE_C_FLAGS}
  -DCMAKE_C_FLAGS_INIT=${CMAKE_C_FLAGS_INIT}
  -DCMAKE_CXX_COMPILER=${CMAKE_CXX_COMPILER}
  -DCMAKE_CXX_FLAGS=${CMAKE_CXX_FLAGS}
  -DCMAKE_CXX_FLAGS_INIT=${CMAKE_CXX_FLAGS_INIT}
  -DCMAKE_EXE_LINKER_FLAGS=${CMAKE_EXE_LINKER_FLAGS}
  -DCMAKE_EXE_LINKER_FLAGS_INIT=${CMAKE_EXE_LINKER_FLAGS_INIT}
  -DCMAKE_MAKE_PROGRAM=${CMAKE_MAKE_PROGRAM}
  -DCMAKE_MODULE_LINKER_FLAGS=${CMAKE_MODULE_LINKER_FLAGS}
  -DCMAKE_MODULE_LINKER_FLAGS_INIT=${CMAKE_MODULE_LINKER_FLAGS_INIT}
  -DCMAKE_SHARED_LINKER_FLAGS=${CMAKE_SHARED_LINKER_FLAGS}
  -DCMAKE_SHARED_LINKER_FLAGS_INIT=${CMAKE_SHARED_LINKER_FLAGS_INIT}
  -DCMAKE_MODULE_PATH=${CMAKE_MODULE_PATH}
  -DCMAKE_SYSROOT=${CMAKE_SYSROOT}
  -DSYMCC_RT_BACKEND=${SYMCC_RT_BACKEND}
  -DLLVM_VERSION=${LLVM_PACKAGE_VERSION}
  -DCMAKE_BUILD_TYPE=${CMAKE_BUILD_TYPE}
  -DZ3_TRUST_SYSTEM_VERSION=${Z3_TRUST_SYSTEM_VERSION})

ExternalProject_Add(SymCCRuntime
  SOURCE_DIR ${CMAKE_SOURCE_DIR}/runtime
  CMAKE_ARGS
  ${SYM_RUNTIME_BUILD_ARGS}
  -DCMAKE_EXPORT_COMPILE_COMMANDS=${CMAKE_EXPORT_COMPILE_COMMANDS}
  -DZ3_DIR=${Z3_DIR}
  -DLLVM_DIR=${LLVM_DIR}
  INSTALL_COMMAND ""
  BUILD_ALWAYS TRUE)

ExternalProject_Get_Property(SymCCRuntime BINARY_DIR)
set(SYMCC_RUNTIME_DIR ${BINARY_DIR})

if (${TARGET_32BIT})
  ExternalProject_Add(SymCCRuntime32
    SOURCE_DIR ${CMAKE_SOURCE_DIR}/runtime
    CMAKE_ARGS
    ${SYM_RUNTIME_BUILD_ARGS}
    -DCMAKE_C_FLAGS="${CMAKE_C_FLAGS} -m32"
    -DCMAKE_CXX_FLAGS="${CMAKE_CXX_FLAGS} -m32"
    -DZ3_DIR=${Z3_32BIT_DIR}
    -DLLVM_DIR=${LLVM_32BIT_DIR}
    INSTALL_COMMAND ""
    BUILD_ALWAYS TRUE)

  ExternalProject_Get_Property(SymCCRuntime32 BINARY_DIR)
  set(SYMCC_RUNTIME_32BIT_DIR ${BINARY_DIR})
endif()

find_package(LLVM REQUIRED CONFIG)

message(STATUS "Found LLVM ${LLVM_PACKAGE_VERSION}")
message(STATUS "Using LLVMConfig.cmake from ${LLVM_DIR}")

if (${LLVM_VERSION_MAJOR} LESS 8 OR ${LLVM_VERSION_MAJOR} GREATER 18)
  message(WARNING "The software has been developed for LLVM 8 through 18; \
it is unlikely to work with other versions!")
endif()

add_definitions(${LLVM_DEFINITIONS})
include_directories(SYSTEM ${LLVM_INCLUDE_DIRS})
find_package(Threads REQUIRED)

option(SYMCC_BUILD_QFBV_Z3_FORKSERVER
  "Build the Linux copy-on-write QF_BV Z3 forkserver" ON)
set(SYMCC_QFBV_Z3_ROOT "" CACHE PATH
  "Optional Z3 distribution root for the QF_BV forkserver")

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} \
-Wredundant-decls -Wcast-align -Wmissing-include-dirs -Wswitch-default \
-Wextra -Wall -Winvalid-pch -Wredundant-decls -Wformat=2 \
-Wmissing-format-attribute -Wformat-nonliteral -Werror -Wno-error=deprecated-declarations")

# Mark nodelete to work around unload bug in upstream LLVM 5.0+
set(CMAKE_MODULE_LINKER_FLAGS "${CMAKE_MODULE_LINKER_FLAGS} -Wl,-z,nodelete")

# This is the compiler pass that we later load into clang or opt. If LLVM is
# built without RTTI we have to disable it for our library too, otherwise we'll
# get linker errors.
add_library(SymCC MODULE
  compiler/Symbolizer.cpp
  compiler/Pass.cpp
  compiler/IFSSSwitchLowering.cpp
  compiler/IFSSLoopSummary.cpp
  compiler/IFSSExitLowering.cpp
  compiler/IFSSContinuationLowering.cpp
  compiler/IFSSContinuationMemory.cpp
  compiler/HydraTransformation.cpp
  compiler/ContinuationLowering.cpp
  compiler/UCSan.cpp
  compiler/Runtime.cpp
  compiler/Main.cpp)

add_library(symcc_schedule_rt SHARED
  util/symcc_schedule_rt.c)
target_link_libraries(symcc_schedule_rt PRIVATE
  ${CMAKE_DL_LIBS}
  Threads::Threads)
set_target_properties(symcc_schedule_rt PROPERTIES
  OUTPUT_NAME "symcc_schedule_rt")

set(SYMCC_HAVE_QFBV_Z3_FORKSERVER OFF)
if (SYMCC_BUILD_QFBV_Z3_FORKSERVER AND CMAKE_SYSTEM_NAME STREQUAL "Linux")
  if (SYMCC_QFBV_Z3_ROOT)
    find_path(SYMCC_QFBV_Z3_INCLUDE_DIR NAMES z3++.h
      PATHS "${SYMCC_QFBV_Z3_ROOT}/include" NO_DEFAULT_PATH)
    find_library(SYMCC_QFBV_Z3_LIBRARY NAMES z3
      PATHS "${SYMCC_QFBV_Z3_ROOT}/lib" "${SYMCC_QFBV_Z3_ROOT}/bin"
      NO_DEFAULT_PATH)
  else()
    find_path(SYMCC_QFBV_Z3_INCLUDE_DIR NAMES z3++.h)
    find_library(SYMCC_QFBV_Z3_LIBRARY NAMES z3)
  endif()
  if (SYMCC_QFBV_Z3_INCLUDE_DIR AND SYMCC_QFBV_Z3_LIBRARY)
    add_executable(symcc_qfbv_z3_forkserver
      util/qfbv_z3_forkserver.cpp)
    target_include_directories(symcc_qfbv_z3_forkserver PRIVATE
      ${SYMCC_QFBV_Z3_INCLUDE_DIR})
    target_link_libraries(symcc_qfbv_z3_forkserver PRIVATE
      ${SYMCC_QFBV_Z3_LIBRARY})
    set_target_properties(symcc_qfbv_z3_forkserver PROPERTIES
      OUTPUT_NAME "symcc-qfbv-z3-forkserver")
    set(SYMCC_HAVE_QFBV_Z3_FORKSERVER ON)
    message(STATUS
      "Building the QF_BV Z3 forkserver with ${SYMCC_QFBV_Z3_LIBRARY}")
  else()
    message(STATUS "QF_BV Z3 forkserver disabled: Z3 development files not found")
  endif()
endif()

set_target_properties(SymCC PROPERTIES OUTPUT_NAME "symcc")
if (NOT LLVM_ENABLE_RTTI)
  set_target_properties(SymCC PROPERTIES COMPILE_FLAGS "-fno-rtti")
endif()

find_program(CLANG_BINARY "clang"
  HINTS ${LLVM_TOOLS_BINARY_DIR}
  DOC "The clang binary to use in the symcc wrapper script.")
find_program(CLANGPP_BINARY "clang++"
  HINTS ${LLVM_TOOLS_BINARY_DIR}
  DOC "The clang binary to use in the sym++ wrapper script.")
if (NOT CLANG_BINARY)
  message(FATAL_ERROR "Clang not found; please make sure that the version corresponding to your LLVM installation is available.")
endif()

if (${LLVM_VERSION_MAJOR} LESS 13)
  set(CLANG_LOAD_PASS "-Xclang -load -Xclang ")
else()
  set(CLANG_LOAD_PASS "-fpass-plugin=")
endif()

configure_file("compiler/symcc.in" "symcc" @ONLY)
configure_file("compiler/sym++.in" "sym++" @ONLY)

add_subdirectory(test)

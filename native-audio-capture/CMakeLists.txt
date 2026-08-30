cmake_minimum_required(VERSION 3.20)
project(nvda_audio_capture CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_executable(nvda_audio_capture src/main.cpp)
target_link_libraries(nvda_audio_capture PRIVATE ole32 oleaut32 ws2_32 bcrypt crypt32)

if(MINGW)
	# -municode selects the wmain entry point.
	# -static drops the libstdc++ and libgcc DLL dependencies, so the executable
	# can be shipped inside an add-on without anything to install alongside it.
	#
	# The rest is about size. The add-on repository refuses files over 500 kB
	# (check-added-large-files in .pre-commit-config.yaml), and a statically linked
	# C++ binary sails past that without trying. Optimising for size, dropping
	# exceptions and RTTI, giving each function its own section and letting the
	# linker discard the unreachable ones brings it back well under.
	target_compile_options(nvda_audio_capture PRIVATE
		-municode -Os -fno-exceptions -fno-rtti -ffunction-sections -fdata-sections)
	target_link_options(nvda_audio_capture PRIVATE
		-municode -static -s -Wl,--gc-sections)
endif()

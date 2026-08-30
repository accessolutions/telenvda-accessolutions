# Native audio capture helper

Source of `addon/globalPlugins/remoteClient/bin/nvda_audio_capture.exe`, the small
program the add-on starts when the controlled computer shares its sound.

## Why it exists

A web page can only capture the audio the browser itself renders, or the whole system
mix through screen capture. Neither lets NVDA be left out, so the sound shared with the
other end carries the remote NVDA's own speech over everything else — which is exactly
what the person watching does not want to hear.

Windows 10 build 20348 and later expose a *process loopback* activation of
`IAudioClient`: given a process id, it captures either only what that process tree
renders, or everything except it. The second mode is the one that matters here. That API
is unreachable from a page, so this helper does the capture natively and serves the
samples to the page over a loopback WebSocket, which turns them back into a WebRTC track.

When the helper is unavailable — an older Windows, or a build where it is missing — the
add-on falls back to the browser capture, and the sound then includes the remote NVDA.
Nothing breaks; only the exclusion is lost.

## Building

Requires CMake 3.20 or later and a MinGW-w64 g++. The shipped binary was built with the
toolchain bundled in Strawberry Perl:

```sh
cmake -B build -S . -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build
```

Then copy the result next to the add-on sources:

```sh
cp build/nvda_audio_capture.exe ../addon/globalPlugins/remoteClient/bin/
```

The compile and link options are chosen for size and are commented in `CMakeLists.txt`:
the executable is linked statically so it can ship inside an add-on with nothing to
install alongside it, which pushes a C++ binary well past the 500 kB ceiling that
`check-added-large-files` enforces. Optimising for size, dropping exceptions and RTTI and
letting the linker discard unreachable sections brings it back to about 256 kB.

## Checking it on a single machine

The default mode needs no session and no second computer. It reports the captured level
once per second, so that the exclusion can be confirmed on its own:

```sh
# Everything except NVDA. Play some music: the level should follow it.
./build/nvda_audio_capture.exe --seconds 10

# The opposite, to confirm the right process is being targeted. Make NVDA
# speak: the level should follow the speech and nothing else.
./build/nvda_audio_capture.exe --seconds 10 --include
```

`--out file.wav` writes what was captured, and `--pid N` or `--process name.exe` targets
something other than `nvda.exe`. A level of -120 dBFS means the stream carried nothing at
all, which is not the same as a stream of silence — around -96 dBFS is the noise floor of
16 bit samples and shows the capture is live.

## Serving mode

`--serve` is what the add-on uses. It binds a WebSocket on `127.0.0.1`, prints `PORT n`
on standard output and streams 48 kHz stereo 16 bit PCM once the page connects.

It repeats, deliberately, the four protections of `local_bridge.py`: the socket binds to
`127.0.0.1` alone, the port is chosen by the system for each session, every connection
must carry the session token, compared in constant time, and an `Origin` header that is
not the exact expected one is refused. The token arrives on standard input rather than on
the command line, which other processes on the machine can read.

// Window showing the screen of the controlled computer.
//
// The window is deliberately ordinary: a plain resizable frame with a title, so
// that the screen reader announces it like any other window. It is shown without
// being activated, so that starting a session never takes the focus away from
// what the user was doing.
package main

import (
	"runtime"
	"sync"
	"syscall"
	"time"
	"unsafe"
)

const viewerClassName = "TeleNVDAScreenShare"

// viewerWindow displays the frames it is handed and reports the mouse actions
// carried out over them.
type viewerWindow struct {
	hwnd    uintptr
	onMouse func(inputEvent)
	onClose func()

	mu     sync.Mutex
	pixels []byte
	width  int
	height int
	info   bitmapInfo

	lastX, lastY float64
	lastMove     time.Time

	ready  chan struct{}
	closed sync.Once
}

// Only one window is ever created, so the procedure handling its messages finds
// it through a package level value rather than through the window data.
var (
	currentWindowMu sync.Mutex
	currentWindow   *viewerWindow
	registerOnce    sync.Once
	wndProcCallback uintptr
)

func newViewerWindow() *viewerWindow {
	return &viewerWindow{ready: make(chan struct{})}
}

// run creates the window and pumps its messages until it is closed. It never
// returns before the window is gone, so it is meant to be called in a goroutine
// of its own.
//
// The thread is locked because every window belongs to the thread that created
// it: its messages are only delivered there.
func (w *viewerWindow) run(title string) {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()

	currentWindowMu.Lock()
	currentWindow = w
	currentWindowMu.Unlock()

	registerOnce.Do(func() {
		wndProcCallback = syscall.NewCallback(windowProc)
		cursor, _, _ := procLoadCursor.Call(0, idcArrow)
		class := wndClassEx{
			Style:     0x0002 | 0x0001, // Redraw on both horizontal and vertical resize.
			WndProc:   wndProcCallback,
			Cursor:    syscall.Handle(cursor),
			ClassName: utf16Ptr(viewerClassName),
		}
		class.Size = uint32(unsafe.Sizeof(class))
		procRegisterClassEx.Call(uintptr(unsafe.Pointer(&class)))
	})

	hwnd, _, _ := procCreateWindowEx.Call(
		0,
		uintptr(unsafe.Pointer(utf16Ptr(viewerClassName))),
		uintptr(unsafe.Pointer(utf16Ptr(title))),
		wsOverlappedWindow,
		cwUseDefault, cwUseDefault, 960, 600,
		0, 0, 0, 0,
	)
	if hwnd == 0 {
		emitFailure("window_failed")
		w.notifyClosed()
		return
	}
	w.hwnd = hwnd
	close(w.ready)
	// Showing without activating keeps the focus, and therefore the screen
	// reader, where the user left it.
	procShowWindow.Call(hwnd, swShowNoActivate)

	var msg msgStruct
	for {
		r, _, _ := procGetMessage.Call(uintptr(unsafe.Pointer(&msg)), 0, 0, 0)
		if int32(r) <= 0 {
			break
		}
		procTranslateMessage.Call(uintptr(unsafe.Pointer(&msg)))
		procDispatchMessage.Call(uintptr(unsafe.Pointer(&msg)))
	}
	w.hwnd = 0
	currentWindowMu.Lock()
	currentWindow = nil
	currentWindowMu.Unlock()
	w.notifyClosed()
}

func (w *viewerWindow) notifyClosed() {
	w.closed.Do(func() {
		if w.onClose != nil {
			w.onClose()
		}
	})
}

// show hands a new picture to the window and asks for it to be repainted.
func (w *viewerWindow) show(pixels []byte, width, height int) {
	w.mu.Lock()
	if len(w.pixels) != len(pixels) {
		w.pixels = make([]byte, len(pixels))
	}
	copy(w.pixels, pixels)
	if w.width != width || w.height != height {
		w.width, w.height = width, height
		w.info = newBitmapInfo(width, height)
	}
	w.mu.Unlock()
	if hwnd := w.hwnd; hwnd != 0 {
		procInvalidateRect.Call(hwnd, 0, 0)
	}
}

// close asks the window to go away. It is safe to call from any thread.
func (w *viewerWindow) close() {
	if hwnd := w.hwnd; hwnd != 0 {
		procPostMessage.Call(hwnd, wmClose, 0, 0)
	} else {
		w.notifyClosed()
	}
}

// picture returns the area of the window the picture is drawn in, letterboxed so
// that it keeps its proportions. It is used both to paint and to work out where
// a mouse action landed.
func (w *viewerWindow) picture(clientW, clientH int) (x, y, width, height int, ok bool) {
	if clientW <= 0 || clientH <= 0 || w.width <= 0 || w.height <= 0 {
		return 0, 0, 0, 0, false
	}
	width = clientW
	height = w.height * clientW / w.width
	if height > clientH {
		height = clientH
		width = w.width * clientH / w.height
	}
	if width <= 0 || height <= 0 {
		return 0, 0, 0, 0, false
	}
	return (clientW - width) / 2, (clientH - height) / 2, width, height, true
}

func (w *viewerWindow) paint(hdc uintptr, clientW, clientH int) {
	w.mu.Lock()
	defer w.mu.Unlock()
	// Anything the picture does not cover is painted black, which also clears the
	// previous frame when the window is larger than the picture.
	procPatBlt.Call(hdc, 0, 0, uintptr(clientW), uintptr(clientH), blackness)
	if len(w.pixels) == 0 {
		return
	}
	x, y, width, height, ok := w.picture(clientW, clientH)
	if !ok {
		return
	}
	procSetStretchBltMode.Call(hdc, colorOnColor)
	procStretchDIBits.Call(
		hdc,
		uintptr(x), uintptr(y), uintptr(width), uintptr(height),
		0, 0, uintptr(w.width), uintptr(w.height),
		uintptr(unsafe.Pointer(&w.pixels[0])),
		uintptr(unsafe.Pointer(&w.info)),
		dibRGBColors, srcCopy,
	)
}

// locate turns a position inside the window into a position inside the shared
// screen, as a fraction of its size. It reports false when the pointer is
// outside the picture, where there is nothing to point at.
func (w *viewerWindow) locate(clientX, clientY, clientW, clientH int) (float64, float64, bool) {
	w.mu.Lock()
	defer w.mu.Unlock()
	x, y, width, height, ok := w.picture(clientW, clientH)
	if !ok {
		return 0, 0, false
	}
	if clientX < x || clientY < y || clientX >= x+width || clientY >= y+height {
		return 0, 0, false
	}
	return float64(clientX-x) / float64(width), float64(clientY-y) / float64(height), true
}

// windowProc handles the messages of the viewer window.
func windowProc(hwnd uintptr, msg uint32, wParam, lParam uintptr) uintptr {
	currentWindowMu.Lock()
	w := currentWindow
	currentWindowMu.Unlock()
	if w == nil {
		r, _, _ := procDefWindowProc.Call(hwnd, uintptr(msg), wParam, lParam)
		return r
	}

	switch msg {
	case wmPaint:
		var ps paintStruct
		hdc, _, _ := procBeginPaint.Call(hwnd, uintptr(unsafe.Pointer(&ps)))
		cw, ch := clientSize(hwnd)
		w.paint(hdc, cw, ch)
		procEndPaint.Call(hwnd, uintptr(unsafe.Pointer(&ps)))
		return 0
	case wmEraseBkgnd:
		// The whole surface is repainted anyway, and erasing it first would make
		// the picture flicker.
		return 1
	case wmMouseMove, wmLButtonDown, wmLButtonUp, wmRButtonDown, wmRButtonUp, wmMButtonDown, wmMButtonUp, wmMouseWheel:
		w.handleMouse(hwnd, msg, wParam, lParam)
		return 0
	case wmClose:
		procDestroyWindow.Call(hwnd)
		return 0
	case wmDestroy:
		procPostQuitMessage.Call(0)
		return 0
	}
	r, _, _ := procDefWindowProc.Call(hwnd, uintptr(msg), wParam, lParam)
	return r
}

func clientSize(hwnd uintptr) (int, int) {
	var rc rect
	procGetClientRect.Call(hwnd, uintptr(unsafe.Pointer(&rc)))
	return int(rc.Right - rc.Left), int(rc.Bottom - rc.Top)
}

func (w *viewerWindow) handleMouse(hwnd uintptr, msg uint32, wParam, lParam uintptr) {
	if w.onMouse == nil {
		return
	}
	if msg == wmMouseWheel {
		// The wheel message carries a position in screen coordinates, so the last
		// known position inside the picture is used instead of converting it.
		if w.lastMove.IsZero() {
			return
		}
		delta := int(int16(uint16(wParam >> 16)))
		w.onMouse(inputEvent{Type: "wheel", X: w.lastX, Y: w.lastY, Delta: delta})
		return
	}
	clientX := int(int16(uint16(lParam)))
	clientY := int(int16(uint16(lParam >> 16)))
	cw, ch := clientSize(hwnd)
	x, y, ok := w.locate(clientX, clientY, cw, ch)
	if !ok {
		return
	}
	w.lastX, w.lastY = x, y
	switch msg {
	case wmMouseMove:
		// Moves are the only messages frequent enough to be worth thinning out.
		now := time.Now()
		if now.Sub(w.lastMove) < 16*time.Millisecond {
			return
		}
		w.lastMove = now
		w.onMouse(inputEvent{Type: "move", X: x, Y: y})
	case wmLButtonDown:
		w.onMouse(inputEvent{Type: "down", Btn: "left", X: x, Y: y})
	case wmLButtonUp:
		w.onMouse(inputEvent{Type: "up", Btn: "left", X: x, Y: y})
	case wmRButtonDown:
		w.onMouse(inputEvent{Type: "down", Btn: "right", X: x, Y: y})
	case wmRButtonUp:
		w.onMouse(inputEvent{Type: "up", Btn: "right", X: x, Y: y})
	case wmMButtonDown:
		w.onMouse(inputEvent{Type: "down", Btn: "middle", X: x, Y: y})
	case wmMButtonUp:
		w.onMouse(inputEvent{Type: "up", Btn: "middle", X: x, Y: y})
	}
	if w.lastMove.IsZero() {
		w.lastMove = time.Now()
	}
}

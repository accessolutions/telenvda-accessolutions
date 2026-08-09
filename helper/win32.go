// Bindings to the Windows interfaces used to capture the screen, display it and
// replay mouse actions.
//
// They are declared by hand rather than pulled from a library because a screen
// reader add-on must stay small and easy to audit, and because everything needed
// here amounts to a handful of calls.
package main

import (
	"syscall"
	"unsafe"
)

var (
	user32 = syscall.NewLazyDLL("user32.dll")
	gdi32  = syscall.NewLazyDLL("gdi32.dll")

	procGetDC              = user32.NewProc("GetDC")
	procReleaseDC          = user32.NewProc("ReleaseDC")
	procGetSystemMetrics   = user32.NewProc("GetSystemMetrics")
	procSetProcessDPIAware = user32.NewProc("SetProcessDPIAware")
	procRegisterClassEx    = user32.NewProc("RegisterClassExW")
	procCreateWindowEx     = user32.NewProc("CreateWindowExW")
	procDestroyWindow      = user32.NewProc("DestroyWindow")
	procDefWindowProc      = user32.NewProc("DefWindowProcW")
	procShowWindow         = user32.NewProc("ShowWindow")
	procGetMessage         = user32.NewProc("GetMessageW")
	procTranslateMessage   = user32.NewProc("TranslateMessage")
	procDispatchMessage    = user32.NewProc("DispatchMessageW")
	procPostQuitMessage    = user32.NewProc("PostQuitMessage")
	procPostMessage        = user32.NewProc("PostMessageW")
	procInvalidateRect     = user32.NewProc("InvalidateRect")
	procBeginPaint         = user32.NewProc("BeginPaint")
	procEndPaint           = user32.NewProc("EndPaint")
	procGetClientRect      = user32.NewProc("GetClientRect")
	procLoadCursor         = user32.NewProc("LoadCursorW")
	procSendInput          = user32.NewProc("SendInput")

	procCreateCompatibleDC     = gdi32.NewProc("CreateCompatibleDC")
	procCreateCompatibleBitmap = gdi32.NewProc("CreateCompatibleBitmap")
	procDeleteDC               = gdi32.NewProc("DeleteDC")
	procDeleteObject           = gdi32.NewProc("DeleteObject")
	procSelectObject           = gdi32.NewProc("SelectObject")
	procStretchBlt             = gdi32.NewProc("StretchBlt")
	procSetStretchBltMode      = gdi32.NewProc("SetStretchBltMode")
	procSetBrushOrgEx          = gdi32.NewProc("SetBrushOrgEx")
	procGetDIBits              = gdi32.NewProc("GetDIBits")
	procStretchDIBits          = gdi32.NewProc("StretchDIBits")
	procPatBlt                 = gdi32.NewProc("PatBlt")
)

// Window messages handled by the viewer window.
const (
	wmDestroy     = 0x0002
	wmClose       = 0x0010
	wmPaint       = 0x000F
	wmEraseBkgnd  = 0x0014
	wmMouseMove   = 0x0200
	wmLButtonDown = 0x0201
	wmLButtonUp   = 0x0202
	wmRButtonDown = 0x0204
	wmRButtonUp   = 0x0205
	wmMButtonDown = 0x0207
	wmMButtonUp   = 0x0208
	wmMouseWheel  = 0x020A
)

// Window styles. The window is a plain resizable one so that the screen reader
// and the window manager treat it like any other.
const (
	wsOverlappedWindow = 0x00CF0000
	swShowNoActivate   = 4
	cwUseDefault       = ^uintptr(0x7FFFFFFF) // 0x80000000 as a signed value.
	idcArrow           = 32512
)

// Raster operations and stretching modes.
const (
	srcCopy       = 0x00CC0020
	captureBlt    = 0x40000000
	blackness     = 0x00000042
	halftone      = 4
	colorOnColor  = 3
	dibRGBColors  = 0
	biRGB         = 0
	smXVirtual    = 76
	smYVirtual    = 77
	smCXVirtual   = 78
	smCYVirtual   = 79
	mouseAbsolute = 0x8000
	mouseVirtual  = 0x4000
	mouseMove     = 0x0001
	mouseLDown    = 0x0002
	mouseLUp      = 0x0004
	mouseRDown    = 0x0008
	mouseRUp      = 0x0010
	mouseMDown    = 0x0020
	mouseMUp      = 0x0040
	mouseWheel    = 0x0800
	inputMouse    = 0
)

type point struct {
	X int32
	Y int32
}

type rect struct {
	Left   int32
	Top    int32
	Right  int32
	Bottom int32
}

type wndClassEx struct {
	Size       uint32
	Style      uint32
	WndProc    uintptr
	ClsExtra   int32
	WndExtra   int32
	Instance   syscall.Handle
	Icon       syscall.Handle
	Cursor     syscall.Handle
	Background syscall.Handle
	MenuName   *uint16
	ClassName  *uint16
	IconSm     syscall.Handle
}

type msgStruct struct {
	Hwnd    syscall.Handle
	Message uint32
	WParam  uintptr
	LParam  uintptr
	Time    uint32
	Pt      point
	Private uint32
}

type paintStruct struct {
	Hdc        syscall.Handle
	Erase      int32
	RcPaint    rect
	Restore    int32
	IncUpdate  int32
	RgbReserve [32]byte
}

type bitmapInfoHeader struct {
	Size          uint32
	Width         int32
	Height        int32
	Planes        uint16
	BitCount      uint16
	Compression   uint32
	SizeImage     uint32
	XPelsPerMeter int32
	YPelsPerMeter int32
	ClrUsed       uint32
	ClrImportant  uint32
}

type bitmapInfo struct {
	Header bitmapInfoHeader
	Colors [3]uint32
}

// mouseInput matches the INPUT structure restricted to its mouse variant. Its
// layout depends on the architecture, so it is declared alongside the code built
// for each of them.

// newBitmapInfo describes a top down 32 bit picture, which is the layout both
// the capture and the display work with.
func newBitmapInfo(width, height int) bitmapInfo {
	var bi bitmapInfo
	bi.Header.Size = uint32(unsafe.Sizeof(bi.Header))
	bi.Header.Width = int32(width)
	// A negative height asks for rows ordered from the top, sparing us a flip.
	bi.Header.Height = -int32(height)
	bi.Header.Planes = 1
	bi.Header.BitCount = 32
	bi.Header.Compression = biRGB
	return bi
}

func getSystemMetrics(index int) int {
	r, _, _ := procGetSystemMetrics.Call(uintptr(index))
	return int(int32(r))
}

// virtualScreen returns the bounds of the whole desktop, monitors included.
func virtualScreen() (x, y, w, h int) {
	x = getSystemMetrics(smXVirtual)
	y = getSystemMetrics(smYVirtual)
	w = getSystemMetrics(smCXVirtual)
	h = getSystemMetrics(smCYVirtual)
	return
}

// sendMouseInput replays a single mouse action.
func sendMouseInput(in mouseInput) {
	in.Type = inputMouse
	procSendInput.Call(1, uintptr(unsafe.Pointer(&in)), unsafe.Sizeof(in))
}

func utf16Ptr(s string) *uint16 {
	p, err := syscall.UTF16PtrFromString(s)
	if err != nil {
		return nil
	}
	return p
}
